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
from shared.settings import load_settings

load_dotenv()
logger = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))

HQ_CHAT_ID: int = 0
_my_id: int = 0
_rate_limit: dict[int, float] = {}
RATE_LIMIT_SEC = 3


async def handle_hq_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Прямое обращение человека к Сэму в Штабе: 'Сэм, ...'"""
    msg = update.message
    if not msg or not msg.text:
        return

    chat_id = msg.chat.id
    if chat_id != HQ_CHAT_ID:
        return

    sender_id = msg.from_user.id if msg.from_user else 0
    if sender_id == _my_id:
        return

    text = msg.text.strip()
    if not text.lower().startswith("сэм"):
        return

    # Rate limiting
    now = time.time()
    if now - _rate_limit.get(sender_id, 0) < RATE_LIMIT_SEC:
        return
    _rate_limit[sender_id] = now

    body = re.sub(r'^сэм[\s,]+', '', text, flags=re.IGNORECASE).strip()
    name = load_settings(sender_id).get("name", "") or "пользователь"
    logger.info("Сэм: прямой запрос от %s (%d): %s", name, sender_id, body[:80])
    try:
        report = await process_with_sam(sender_id, body, requester=name)
    except Exception as e:
        logger.error("Сэм: ошибка выполнения: %s", e)
        report = f"Что-то пошло не так: {e}"
    await msg.reply_text(report)


async def sethq_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/sethq — Сэм тоже запоминает эту группу как Штаб."""
    chat_id = update.effective_chat.id
    set_hq_chat_id(chat_id)
    global HQ_CHAT_ID
    HQ_CHAT_ID = chat_id
    await update.message.reply_text("✅ Сэм запомнил Штаб!")


def create_app() -> Application:
    token = os.environ["SAM_BOT_TOKEN"]
    app = Application.builder().token(token).concurrent_updates(True).build()
    app.add_handler(CommandHandler("sethq", sethq_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_hq_message))
    return app
