import asyncio
import logging
import os
import re
import time
from datetime import timezone, timedelta

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from sam.agent import process_with_sam
from shared.hq import get_hq_chat_id, set_hq_chat_id

load_dotenv()
logger = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))

HQ_CHAT_ID: int = 0          # устанавливается из .env в main.py
_my_id: int = 0               # id самого Сэма, устанавливается при старте
_rate_limit: dict[int, float] = {}   # sender_id → timestamp последнего ответа
RATE_LIMIT_SEC = 3


def _extract_user_id(text: str) -> tuple:
    """Извлекает [user:ID] из текста, возвращает (user_id, очищенный текст)."""
    m = re.search(r'\[user:(\d+)\]', text)
    if m:
        uid = int(m.group(1))
        clean = re.sub(r'\[user:\d+\]', '', text).strip()
        return uid, clean
    return None, text


async def handle_hq_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    chat_id = msg.chat.id
    if chat_id != HQ_CHAT_ID:
        return

    sender_id = msg.from_user.id if msg.from_user else 0

    # Игнорируем собственные сообщения
    if sender_id == _my_id:
        return

    text = msg.text.strip()

    # Реагируем только если обращаются к Сэму
    if not text.lower().startswith("сэм"):
        return

    # Rate limiting
    now = time.time()
    if now - _rate_limit.get(sender_id, 0) < RATE_LIMIT_SEC:
        return
    _rate_limit[sender_id] = now

    # Извлекаем user_id и задание
    body = re.sub(r'^сэм[\s,]+', '', text, flags=re.IGNORECASE).strip()
    user_id, task_description = _extract_user_id(body)

    if not user_id:
        logger.warning("Сэм: сообщение без [user:ID], игнорирую: %s", text[:80])
        return

    logger.info("Сэм получил задание для user %d: %s", user_id, task_description[:80])

    try:
        report = await process_with_sam(user_id, task_description)
    except Exception as e:
        logger.error("Сэм: ошибка выполнения: %s", e)
        report = f"Что-то пошло не так: {e}"

    # Отвечаем REPLY на конкретное сообщение Мери — она определит ответ по message_id
    await msg.reply_text(f"Мери, {report}")


async def sethq_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/sethq — Сэм тоже запоминает эту группу как Штаб."""
    chat_id = update.effective_chat.id
    set_hq_chat_id(chat_id)
    global HQ_CHAT_ID
    HQ_CHAT_ID = chat_id
    await update.message.reply_text("✅ Сэм запомнил Штаб!")


def create_app() -> Application:
    token = os.environ["SAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("sethq", sethq_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_hq_message))
    return app
