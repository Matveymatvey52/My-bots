import asyncio
import logging
import os
import random
import re
import time
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    BusinessConnectionHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from miss_ives import agent as ives_agent
from shared.hq import get_hq_chat_id, set_hq_chat_id
from shared.db import (
    delete_business_connection, get_biz_chat_muted, get_biz_chat_settings,
    get_connection_for_user, get_user_by_connection, load_biz_history,
    save_biz_message, save_business_connection, set_biz_chat_muted,
    set_biz_chat_settings,
)
from shared.settings import is_onboarding_done, load_settings

load_dotenv()
logger = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))

def now_msk() -> datetime:
    return datetime.now(tz=MSK)

HQ_CHAT_ID: int = 0
MARY_BOT_ID: int = 0
_my_id: int = 0
_rate_limit: dict[int, float] = {}

# Ожидание ввода кастомной инструкции: user_id → (conn_id, chat_id)
_pending_instruction: dict[int, tuple[str, int]] = {}


# ── Панель управления бизнес-чатом ───────────

def _build_biz_panel(conn_id: str, chat_id: int) -> tuple:
    s = get_biz_chat_settings(conn_id, chat_id)
    muted = get_biz_chat_muted(conn_id, chat_id)
    instr = s.get("custom_instruction") or ""
    history = load_biz_history(conn_id, chat_id, limit=6)

    lines = ["⚙️ Управление чатом\n"]
    if history:
        lines.append("Последние сообщения:")
        for m in history[-4:]:
            who = "← Они" if m["role"] == "user" else "→ Ты"
            preview = m["content"][:55] + ("…" if len(m["content"]) > 55 else "")
            lines.append(f"  {who}: {preview}")
        lines.append("")
    lines.append(f"Автоответ: {'🔇 выключен' if muted else '✅ включён'}")
    if instr:
        lines.append(f"Инструкция: {instr}")

    cid = f"{conn_id}:{chat_id}"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔇 Выключить" if not muted else "✅ Включить",
                              callback_data=f"biz:toggle:{cid}")],
        [
            InlineKeyboardButton("😊 Смайлики", callback_data=f"biz:preset:smile:{cid}"),
            InlineKeyboardButton("👔 Формально", callback_data=f"biz:preset:formal:{cid}"),
            InlineKeyboardButton("⚡ Кратко",    callback_data=f"biz:preset:short:{cid}"),
        ],
        [
            InlineKeyboardButton("🔇 На 1 час",  callback_data=f"biz:mute1:{cid}"),
            InlineKeyboardButton("🔇 На 3 часа", callback_data=f"biz:mute3:{cid}"),
        ],
        [InlineKeyboardButton("✏️ Своя инструкция", callback_data=f"biz:setinstr:{cid}")],
        [InlineKeyboardButton("🗑 Сбросить инструкцию", callback_data=f"biz:clearinstr:{cid}")],
    ])
    return "\n".join(lines), keyboard


# ── Бизнес-подключение ────────────────────────

async def handle_business_connection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = update.business_connection
    if conn.is_enabled:
        save_business_connection(conn.id, conn.user.id, conn.can_reply)
        await context.bot.send_message(
            chat_id=conn.user.id,
            text=(
                "🤝 Бизнес-подключение активно!\n\n"
                "Теперь я, Мисс Айвз, веду переписку от твоего имени.\n\n"
                f"{'✅ Могу отвечать от твоего имени.' if conn.can_reply else '👁 Только чтение.'}"
            ),
        )
    else:
        delete_business_connection(conn.id)
        await context.bot.send_message(
            chat_id=conn.user.id,
            text="🔌 Бизнес-подключение отключено.",
        )


# ── Входящие бизнес-сообщения ─────────────────

async def handle_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.business_message
    if not msg or not msg.text:
        return

    conn_id = msg.business_connection_id
    info = get_user_by_connection(conn_id)
    if not info:
        return

    user_id = info["user_id"]
    can_reply = bool(info["can_reply"])

    if msg.from_user and msg.from_user.id == user_id:
        return

    sender = msg.from_user.first_name or msg.from_user.username or "Неизвестный"

    await context.bot.send_message(
        chat_id=user_id,
        text=f"📨 *{sender}:* {msg.text}",
        parse_mode="Markdown",
    )

    if can_reply and not get_biz_chat_muted(conn_id, msg.chat.id):
        name = load_settings(user_id).get("name", "")

        save_biz_message(conn_id, msg.chat.id, "user", msg.text)

        # Запрашиваем расписание у Мери через HQ
        tasks_context = await ives_agent.ask_mary_for_schedule(
            context.bot, user_id,
            question="дай расписание на ближайшие дни"
        )

        history = load_biz_history(conn_id, msg.chat.id)
        chat_settings = get_biz_chat_settings(conn_id, msg.chat.id)
        custom_instr = chat_settings.get("custom_instruction") or ""

        try:
            reply, used_search = await ives_agent.generate_business_reply(
                name, sender, history, tasks_context, custom_instr
            )
            logger.info("Business reply to %s (search=%s): %s", sender, used_search, reply[:80])
            save_biz_message(conn_id, msg.chat.id, "assistant", reply)

            typing_delay = random.uniform(15, 30) if used_search else min(3 + len(reply) / 10, 12)
            await asyncio.sleep(typing_delay)

            await context.bot.send_message(
                chat_id=msg.chat.id,
                text=reply,
                business_connection_id=conn_id,
            )
        except Exception as e:
            logger.error("Не удалось отправить business-ответ: %s", e)
            await context.bot.send_message(
                chat_id=user_id,
                text=f"⚠️ Не смогла ответить {sender}: {e}",
            )


# ── Кнопки панели ─────────────────────────────

async def biz_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":", 3)
    action = parts[1]
    conn_id = parts[2]
    chat_id = int(parts[3])
    user_id = query.from_user.id

    PRESETS = {
        "smile":  "Используй много смайликов, пиши тепло и дружелюбно",
        "formal": "Общайся формально и вежливо, без смайликов",
        "short":  "Отвечай очень коротко — 1 предложение максимум",
    }

    if action == "toggle":
        muted = get_biz_chat_muted(conn_id, chat_id)
        set_biz_chat_muted(conn_id, chat_id, not muted)

    elif action.startswith("preset:"):
        preset_key = action.split(":")[1]
        instr = PRESETS.get(preset_key, "")
        set_biz_chat_settings(conn_id, chat_id, custom_instruction=instr)

    elif action == "mute1":
        until = (now_msk() + timedelta(hours=1)).isoformat()
        set_biz_chat_settings(conn_id, chat_id, muted=True, mute_until=until)

    elif action == "mute3":
        until = (now_msk() + timedelta(hours=3)).isoformat()
        set_biz_chat_settings(conn_id, chat_id, muted=True, mute_until=until)

    elif action == "setinstr":
        _pending_instruction[user_id] = (conn_id, chat_id)
        await query.message.reply_text(
            "✏️ Напиши инструкцию для этого чата — например:\n"
            "«общайся как старый друг», «это VIP клиент», «используй много смайликов»"
        )
        return

    elif action == "clearinstr":
        set_biz_chat_settings(conn_id, chat_id, custom_instruction="")

    text, keyboard = _build_biz_panel(conn_id, chat_id)
    await query.edit_message_text(text, reply_markup=keyboard)


# ── Личные сообщения Мисс Айвз ───────────────

async def handle_direct_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ЛС к Мисс Айвз (панель управления + ввод инструкции)."""
    msg = update.message
    if not msg or not msg.text:
        return

    # Игнорируем сообщения из HQ — там своя логика
    if msg.chat.id == HQ_CHAT_ID:
        return

    user_id = update.effective_user.id

    # Ввод кастомной инструкции
    if user_id in _pending_instruction:
        conn_id, chat_id = _pending_instruction.pop(user_id)
        set_biz_chat_settings(conn_id, chat_id, custom_instruction=msg.text.strip())
        text, keyboard = _build_biz_panel(conn_id, chat_id)
        await msg.reply_text(f"✅ Инструкция сохранена!\n\n{text}", reply_markup=keyboard)
        return

    # Открытие панели через /start bizChat<id>
    if msg.text.startswith("/start"):
        args = msg.text.split()[1:]
        if args and args[0].startswith("bizChat"):
            try:
                biz_chat_id = int(args[0][len("bizChat"):])
            except ValueError:
                biz_chat_id = None
            if biz_chat_id and is_onboarding_done(user_id):
                conn = get_connection_for_user(user_id)
                if conn:
                    text, keyboard = _build_biz_panel(conn["connection_id"], biz_chat_id)
                    await msg.reply_text(text, reply_markup=keyboard)
                    return
        await msg.reply_text("👋 Привет! Я Мисс Айвз — твой секретарь. Подключи меня через Telegram Business.")
        return


# ── HQ — слушаем ответы Мери ─────────────────

async def handle_hq_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Слушаем HQ: если Мери отвечает на наш запрос расписания — резолвим Future."""
    msg = update.message
    if not msg or not msg.text:
        return
    if msg.chat.id != HQ_CHAT_ID:
        return

    sender_id = msg.from_user.id if msg.from_user else 0

    # Игнорируем собственные сообщения
    if sender_id == _my_id:
        return

    # Rate limiting для защиты от петель
    now = time.time()
    if now - _rate_limit.get(sender_id, 0) < 3:
        return
    _rate_limit[sender_id] = now

    # Ответ Мери на наш запрос (reply на наше сообщение)
    if sender_id == MARY_BOT_ID and msg.reply_to_message:
        ives_agent.resolve_mary_response(msg.reply_to_message.message_id, msg.text)
        return


async def sethq_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/sethq — Мисс Айвз тоже запоминает эту группу как Штаб."""
    chat_id = update.effective_chat.id
    set_hq_chat_id(chat_id)
    global HQ_CHAT_ID
    HQ_CHAT_ID = chat_id
    ives_agent.HQ_CHAT_ID = chat_id
    await update.message.reply_text("✅ Мисс Айвз запомнила Штаб!")


async def route_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Роутер: HQ-сообщения → handle_hq_message, остальные → handle_direct_message."""
    msg = update.message
    if not msg:
        return
    if HQ_CHAT_ID and msg.chat.id == HQ_CHAT_ID:
        await handle_hq_message(update, context)
    else:
        await handle_direct_message(update, context)


def create_app() -> Application:
    token = os.environ["MISS_IVES_BOT_TOKEN"]
    app = Application.builder().token(token).build()

    app.add_handler(BusinessConnectionHandler(handle_business_connection))
    app.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, handle_business_message))
    app.add_handler(CallbackQueryHandler(biz_panel_callback, pattern=r"^biz:"))
    app.add_handler(CommandHandler("sethq", sethq_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, route_text))

    return app
