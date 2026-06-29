import asyncio
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from telegram import Update, InlineQueryResultArticle, InputTextMessageContent
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

from mary import agent as mary_agent
import mary.agent as mary_agent_module
from shared.db import (
    clear_history, get_bot_stats, get_photo, get_tasks_for_day,
    get_upcoming_tasks, save_photo,
    get_tasks_needing_reminder, claim_summary_send, mark_reminder_sent,
    get_tasks_due_now, mark_time_notified,
)
from shared.settings import (
    get_all_user_ids, is_onboarding_done, load_settings, save_settings,
)
from shared.hq import get_hq_chat_id, set_hq_chat_id

load_dotenv()
logger = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))

def now_msk() -> datetime:
    return datetime.now(tz=MSK)

HQ_CHAT_ID: int = 0
SAM_BOT_ID: int = 0
MISS_IVES_BOT_ID: int = 0
_my_id: int = 0
_rate_limit: dict[int, float] = {}

# Ожидание фото: user_id → photo_db_id
_pending_photo: dict[int, int] = {}

ADMIN_ID = 6279401743


# ── Онбординг ─────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    save_settings(user_id, {"onboarding_step": "ask_name", "onboarding_done": False})
    clear_history(user_id)
    await update.message.reply_text(
        "👋 Привет! Я *Мери* — твой личный помощник-планировщик.\n\n"
        "Вот что я умею:\n"
        "• 📝 Записывать твои дела и встречи\n"
        "• 🎤 Принимать голосовые сообщения\n"
        "• ⏰ Напоминать о событиях\n"
        "• ☀️ Присылать сводку дел утром и 🌙 вечером\n"
        "• 🗓 Показывать расписание\n\n"
        "Давай познакомимся! Как тебя зовут?",
        parse_mode="Markdown",
    )


def _extract_name(text: str) -> str:
    text = text.strip().rstrip(".")
    for prefix in ["меня зовут ", "я ", "моё имя ", "мое имя ", "зовут меня ", "зовут "]:
        if text.lower().startswith(prefix):
            return text[len(prefix):].strip().capitalize()
    return text.capitalize()


# ── Роутинг текста ────────────────────────────

async def _route_text(
    user_id: int,
    text: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    voice_prefix: str = "",
):
    settings = load_settings(user_id)
    step = settings.get("onboarding_step")

    if step == "ask_name":
        name = _extract_name(text)
        save_settings(user_id, {"name": name, "onboarding_step": "ask_morning"})
        await update.message.reply_text(
            f"Отлично, *{name}*! 😊\n\n"
            "В какое время присылать утреннее сообщение с делами на день?\n"
            "Напиши в формате ЧЧ:ММ, например `08:00`",
            parse_mode="Markdown",
        )

    elif step == "ask_morning":
        try:
            parsed = datetime.strptime(text.strip(), "%H:%M")
        except ValueError:
            await update.message.reply_text("Не понял формат 🤔 Напиши так: `08:00`", parse_mode="Markdown")
            return
        t = parsed.strftime("%H:%M")
        save_settings(user_id, {"morning_time": t, "onboarding_step": "ask_evening"})
        await update.message.reply_text(
            f"Буду писать в *{t}* ☀️\n\nХочешь ещё *вечернее* напоминание? Ответь *да* или *нет*",
            parse_mode="Markdown",
        )

    elif step == "ask_evening":
        want = text.lower() in ("да", "yes", "y", "д", "+", "хочу", "конечно")
        updates = {"evening_enabled": want, "onboarding_done": True, "onboarding_step": None}
        if want:
            updates["evening_time"] = "20:00"
        save_settings(user_id, updates)
        name = settings.get("name", "")
        evening_note = "Вечернее напоминание в 20:00 включено 🌙\n" if want else ""
        await update.message.reply_text(
            f"Готово, *{name}*! 🎉\n\n"
            f"Утреннее сообщение в {settings.get('morning_time', '—')} ☀️\n"
            f"{evening_note}\n"
            "Теперь просто говори мне о делах!\n\n"
            "📋 /tasks — дела на сегодня\n"
            "📅 /all — все предстоящие дела\n"
            "⚙️ /settings — настройки",
            parse_mode="Markdown",
        )

    elif is_onboarding_done(user_id):
        name = settings.get("name", "")
        await context.bot.send_chat_action(update.effective_chat.id, "typing")

        ts = now_msk().strftime("%d.%m %H:%M")
        await _log_hq(context.bot, f"🕐 {ts}\n👤 *{name}:* {voice_prefix}{text}")

        async def send_sam_log(sam_text: str):
            await _log_hq(context.bot, sam_text)

        effective_text = text
        if user_id in _pending_photo:
            photo_db_id = _pending_photo.pop(user_id)
            effective_text = (
                f"[photo_context: пользователь прислал фото photo_id={photo_db_id}, "
                f"при создании задачи включи '[photo:{photo_db_id}]' в текст]\n{text}"
            )

        reply = await mary_agent.process_with_mary(
            user_id, effective_text, name,
            bot=context.bot,
            on_sam_message=send_sam_log,
        )
        await update.message.reply_text(reply, parse_mode="Markdown")
        await _log_hq(context.bot, f"🤖 *Мери:* {reply}")

    else:
        await update.message.reply_text("Напиши /start чтобы начать! 👋")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    # HQ группа — обрабатываем отдельно
    if msg.chat.id == HQ_CHAT_ID:
        await _handle_hq(update, context)
        return

    user_id = update.effective_user.id

    bot_info = await context.bot.get_me()
    if user_id == bot_info.id:
        return

    text = msg.text.strip()

    # Группы — только упоминание или reply
    if msg.chat.type in ("group", "supergroup"):
        is_mention = any(
            e.type == "mention" and f"@{bot_info.username}" in text
            for e in (msg.entities or [])
        )
        is_reply = (
            msg.reply_to_message
            and msg.reply_to_message.from_user
            and msg.reply_to_message.from_user.id == bot_info.id
        )
        if not is_mention and not is_reply:
            return
        text = text.replace(f"@{bot_info.username}", "").strip()

    await _route_text(user_id, text, update, context)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_onboarding_done(user_id):
        return
    file_id = update.message.photo[-1].file_id
    photo_db_id = save_photo(user_id, file_id)
    _pending_photo[user_id] = photo_db_id
    name = load_settings(user_id).get("name", "")
    caption = (update.message.caption or "").strip()
    if caption:
        prompt = (
            f"[photo_context: пользователь прислал фото photo_id={photo_db_id}, "
            f"при создании задачи включи '[photo:{photo_db_id}]' в текст]\n{caption}"
        )
    else:
        prompt = (
            f"Пользователь прислал фото (photo_id={photo_db_id}) без подписи. "
            f"Спроси когда напомнить и что с ним сделать. "
            f"При создании задачи включи '[photo:{photo_db_id}]' в текст."
        )
    reply = await mary_agent.process_with_mary(user_id, prompt, name, bot=context.bot)
    await update.message.reply_text(reply)


# ── HQ — связь с Сэмом и Мисс Айвз ──────────

async def _handle_hq(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    sender_id = msg.from_user.id if msg.from_user else 0

    # Игнорируем собственные сообщения
    if sender_id == _my_id:
        return

    text = msg.text.strip()

    # Сообщения Сэма: ack игнорируем, всё остальное — резолвим Future
    if sender_id == SAM_BOT_ID and msg.reply_to_message:
        if text != "⚙️ Принял, обрабатываю...":
            mary_agent.resolve_sam_response(msg.reply_to_message.message_id, text)
        return

    # Rate limiting для всех остальных
    now = time.time()
    if now - _rate_limit.get(sender_id, 0) < 3:
        return
    _rate_limit[sender_id] = now

    # Мисс Айвз спрашивает расписание
    if sender_id == MISS_IVES_BOT_ID and text.lower().startswith("мери"):
        body = re.sub(r'^мери[\s,]+', '', text, flags=re.IGNORECASE).strip()
        m = re.search(r'\[user:(\d+)\]', body)
        if m:
            uid = int(m.group(1))
            tasks = get_upcoming_tasks(uid)
            if tasks:
                lines = []
                for t in tasks[:15]:
                    time_str = f" в {t['time']}" if t["time"] else ""
                    lines.append(f"• {t['date']}{time_str}: {t['text']}")
                schedule = "\n".join(lines)
            else:
                schedule = "Предстоящих задач нет."
            await msg.reply_text(f"Мисс Айвз, вот расписание:\n{schedule}")
        return

    # Прочие сообщения от ботов игнорируем (чтобы не зациклиться)
    if sender_id in (SAM_BOT_ID, MISS_IVES_BOT_ID):
        return

    # Человек обращается к Мери по имени прямо в Штабе
    if text.lower().startswith("мери"):
        body = re.sub(r'^мери[\s,]+', '', text, flags=re.IGNORECASE).strip()
        if body:
            await _route_text(sender_id, body, update, context)


# ── Команды ───────────────────────────────────

async def tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    today = now_msk().strftime("%Y-%m-%d")
    tasks = get_tasks_for_day(user_id, today)

    if not tasks:
        await update.message.reply_text("На сегодня дел нет 🎉 Отдыхай!")
        return

    d = now_msk()
    date_label = _ru_date(d)
    lines = [f"⏰ {t['time']} — {t['text']}" if t["time"] else f"• {t['text']}" for t in tasks]
    await update.message.reply_text(
        f"📋 *Дела на сегодня, {date_label}:*\n\n" + "\n\n".join(lines),
        parse_mode="Markdown",
    )


async def all_tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tasks = get_upcoming_tasks(user_id)
    if not tasks:
        await update.message.reply_text("Предстоящих дел нет 🎉")
        return

    by_date: dict[str, list] = {}
    for t in tasks:
        by_date.setdefault(t["date"], []).append(t)

    blocks = ["📅 *Все предстоящие дела:*"]
    for date, day_tasks in by_date.items():
        d = datetime.strptime(date, "%Y-%m-%d")
        date_str = f"*{_ru_date(d)} ({_ru_weekday(d)})*"
        day_lines = [date_str]
        for t in day_tasks:
            day_lines.append(f"⏰ {t['time']} — {t['text']}" if t["time"] else f"• {t['text']}")
        blocks.append("\n".join(day_lines))

    await update.message.reply_text("\n\n".join(blocks), parse_mode="Markdown")


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    s = load_settings(user_id)
    evening = (
        f"включено ({s.get('evening_time', '20:00')})"
        if s.get("evening_enabled") else "выключено"
    )
    await update.message.reply_text(
        f"⚙️ *Настройки*\n\n"
        f"👤 Имя: {s.get('name', '—')}\n"
        f"☀️ Утреннее: {s.get('morning_time', '—')}\n"
        f"🌙 Вечернее: {evening}\n\n"
        "Чтобы изменить — напиши мне: _«поменяй утреннее на 09:30»_",
        parse_mode="Markdown",
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    s = get_bot_stats()
    all_users = get_all_user_ids()
    lines = [
        "📊 *Статистика бота*\n",
        f"👤 Запустили /start: *{len(all_users)}*",
        f"💬 Написали хоть раз: *{s['total_users']}*",
        f"🔥 Активны за 7 дней: *{s['active_7d']}*",
        f"📅 Активны за 30 дней: *{s['active_30d']}*\n",
        f"✉️ Всего сообщений: *{s['total_messages']}*",
        f"✅ Активных задач: *{s['total_tasks']}*",
        f"📝 Задач за 30 дней: *{s['tasks_30d']}*",
    ]
    if s["per_user"]:
        lines.append("\n*Топ по сообщениям:*")
        for uid, cnt in s["per_user"][:10]:
            lines.append(f"  `{uid}` — {cnt} сообщ.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def chatid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"ID этого чата: `{chat_id}`",
        parse_mode="Markdown",
    )


async def sethq_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/sethq — зарегистрировать эту группу как Штаб."""
    if update.effective_user.id != ADMIN_ID:
        return
    chat_id = update.effective_chat.id
    set_hq_chat_id(chat_id)
    global HQ_CHAT_ID
    HQ_CHAT_ID = chat_id
    mary_agent_module.HQ_CHAT_ID = chat_id
    await update.message.reply_text(
        f"✅ Штаб зарегистрирован! ID: `{chat_id}`\n"
        "Теперь Мери, Сэм и Мисс Айвз будут общаться здесь.",
        parse_mode="Markdown",
    )


# ── Голос ─────────────────────────────────────

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not load_settings(user_id):
        await update.message.reply_text("Напиши /start чтобы начать! 👋")
        return
    if not os.environ.get("ASSEMBLYAI_API_KEY"):
        await update.message.reply_text("⚠️ Голосовые пока не настроены.")
        return

    await context.bot.send_chat_action(update.effective_chat.id, "typing")
    voice = update.message.voice
    tg_file = await context.bot.get_file(voice.file_id)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name
    await tg_file.download_to_drive(tmp_path)

    try:
        import assemblyai as aai
        aai.settings.api_key = os.environ["ASSEMBLYAI_API_KEY"]
        config = aai.TranscriptionConfig(language_code="ru")
        transcriber = aai.Transcriber(config=config)
        loop = asyncio.get_running_loop()
        transcript = await loop.run_in_executor(None, lambda: transcriber.transcribe(tmp_path))
        if transcript.status == aai.TranscriptStatus.error:
            raise RuntimeError(transcript.error)
        recognized_text = transcript.text
    except Exception as e:
        logger.error("Ошибка транскрипции: %s", e)
        await update.message.reply_text("Не удалось распознать голосовое 😔 Попробуй текстом.")
        return
    finally:
        os.unlink(tmp_path)

    await update.message.reply_text(f"🎤 Распознала: _{recognized_text}_", parse_mode="Markdown")
    await _route_text(user_id, recognized_text, update, context, voice_prefix="🎤 ")


# ── Inline ────────────────────────────────────

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.inline_query.from_user.id
    query = update.inline_query.query.strip().lower()
    now = now_msk()
    today = now.strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    def fmt(tasks, date_str) -> str:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        label = _ru_date(d)
        if not tasks:
            return f"📅 {label}: дел нет 🎉"
        lines = [f"📅 {label}:"]
        for t in tasks:
            prefix = f"⏰ {t['time']} — " if t["time"] else "• "
            lines.append(f"{prefix}{t['text']}")
        return "\n".join(lines)

    results = []
    if not query or "сегодня" in query or "today" in query:
        t = get_tasks_for_day(user_id, today)
        results.append(InlineQueryResultArticle(
            id="today", title="📅 Дела на сегодня",
            description=f"{len(t)} дел" if t else "Дел нет",
            input_message_content=InputTextMessageContent(fmt(t, today)),
        ))
    if not query or "завтра" in query or "tomorrow" in query:
        t = get_tasks_for_day(user_id, tomorrow)
        results.append(InlineQueryResultArticle(
            id="tomorrow", title="📅 Дела на завтра",
            description=f"{len(t)} дел" if t else "Дел нет",
            input_message_content=InputTextMessageContent(fmt(t, tomorrow)),
        ))
    if not query or "все" in query or "all" in query:
        all_t = get_upcoming_tasks(user_id)
        by_date: dict[str, list] = {}
        for t in all_t:
            by_date.setdefault(t["date"], []).append(t)
        text_all = "\n\n".join(fmt(v, k) for k, v in list(by_date.items())[:5]) if by_date else "Предстоящих дел нет 🎉"
        results.append(InlineQueryResultArticle(
            id="all", title="📋 Все предстоящие дела",
            description=f"{len(all_t)} дел" if all_t else "Дел нет",
            input_message_content=InputTextMessageContent(text_all),
        ))

    await update.inline_query.answer(results, cache_time=30)


# ── Планировщик ───────────────────────────────

def setup_scheduler(app: Application):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _send_scheduled,
        trigger="cron",
        minute="*",
        args=[app],
    )
    scheduler.start()
    logger.info("Планировщик Мери запущен")


def _norm_time(t: str) -> str:
    try:
        return datetime.strptime(t.strip(), "%H:%M").strftime("%H:%M")
    except Exception:
        return t


async def _send_scheduled(app: Application):
    import random
    now = now_msk()
    current_time = now.strftime("%H:%M")
    today = now.strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    for task in get_tasks_needing_reminder(now):
        mins = task["reminder_minutes"]
        label = f"через {mins} мин" if mins < 60 else f"через {mins // 60} ч"
        raw = task["text"]
        photo_match = re.search(r'\[photo:(\d+)\]', raw)
        clean = re.sub(r'\[photo:\d+\]', '', raw).strip()
        text = f"⏰ *Напоминание!* {label}: *{clean}* в {task['time']}"
        try:
            await app.bot.send_message(chat_id=task["user_id"], text=text, parse_mode="Markdown")
            if photo_match:
                photo = get_photo(int(photo_match.group(1)))
                if photo:
                    await app.bot.send_photo(chat_id=task["user_id"], photo=photo["file_id"])
            mark_reminder_sent(task["id"])
        except Exception as e:
            logger.error("Ошибка напоминания %d: %s", task["id"], e)

    for task in get_tasks_due_now(now):
        raw = task["text"]
        photo_match = re.search(r'\[photo:(\d+)\]', raw)
        clean = re.sub(r'\[photo:\d+\]', '', raw).strip()
        text = f"⏰ *Пора:* *{clean}*"
        try:
            await app.bot.send_message(chat_id=task["user_id"], text=text, parse_mode="Markdown")
            if photo_match:
                photo = get_photo(int(photo_match.group(1)))
                if photo:
                    await app.bot.send_photo(chat_id=task["user_id"], photo=photo["file_id"])
            mark_time_notified(task["id"])
        except Exception as e:
            logger.error("Ошибка уведомления по времени %d: %s", task["id"], e)

    for user_id in get_all_user_ids():
        s = load_settings(user_id)
        if not s.get("onboarding_done"):
            continue
        name = s.get("name", "")

        if _norm_time(s.get("morning_time", "")) == current_time:
            if claim_summary_send(user_id, today, "morning"):
                await _send_summary(app, user_id, today, name, morning=True)

        if s.get("evening_enabled") and _norm_time(s.get("evening_time", "20:00")) == current_time:
            if claim_summary_send(user_id, tomorrow, "evening"):
                await _send_summary(app, user_id, tomorrow, name, morning=False)


async def _send_summary(app: Application, user_id: int, date: str, name: str, morning: bool):
    import random
    tasks = get_tasks_for_day(user_id, date)
    d = datetime.strptime(date, "%Y-%m-%d")
    date_label = _ru_date(d)

    if morning:
        header = f"☀️ Доброе утро, {name}!\n📅 На сегодня, {date_label}:"
    else:
        header = f"🌙 Добрый вечер, {name}!\n📅 На завтра, {date_label}:"

    if not tasks:
        text = f"{header}\n\nДел нет — можно отдыхать 🎉"
    else:
        lines = []
        for t in tasks:
            raw = t["text"]
            has_photo = bool(re.search(r'\[photo:\d+\]', raw))
            clean = re.sub(r'\[photo:\d+\]', '', raw).strip()
            label = f"{'📷 ' if has_photo else ''}{clean}"
            lines.append(f"⏰ {t['time']} — {label}" if t["time"] else f"• {label}")
        closing = random.choice([
            "Насыщенный день! 💪", "Всё успеешь! 🙌", "Удачного дня! ⭐",
            "Держи темп! 🚀", "Ты справишься! 😊", "Продуктивного дня! ✨",
        ])
        text = header + "\n\n" + "\n\n".join(lines) + f"\n\n{closing}"

    try:
        await app.bot.send_message(chat_id=user_id, text=text)
    except Exception as e:
        logger.error("Ошибка сводки %d: %s", user_id, e)


# ── Вспомогательные ───────────────────────────

async def _log_hq(bot, text: str):
    log_chat_id = os.environ.get("LOG_CHAT_ID")
    if log_chat_id:
        try:
            await bot.send_message(chat_id=int(log_chat_id), text=text, parse_mode="Markdown")
        except Exception:
            pass


def _ru_date(d: datetime) -> str:
    month_ru = {
        1:"января",2:"февраля",3:"марта",4:"апреля",5:"мая",6:"июня",
        7:"июля",8:"августа",9:"сентября",10:"октября",11:"ноября",12:"декабря",
    }
    return f"{d.day} {month_ru[d.month]}"


def _ru_weekday(d: datetime) -> str:
    return ["пн","вт","ср","чт","пт","сб","вс"][d.weekday()]


# ── Сборка приложения ─────────────────────────

async def post_init(app: Application):
    setup_scheduler(app)


def create_app() -> Application:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    # concurrent_updates(True): пока Мери выполняет длинный запрос, другие апдейты
    # (из HQ, от Miss Ives) не должны ждать в очереди.
    app = Application.builder().token(token).post_init(post_init).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("tasks", tasks_command))
    app.add_handler(CommandHandler("all", all_tasks_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("chatid", chatid_command))
    app.add_handler(CommandHandler("sethq", sethq_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    return app
