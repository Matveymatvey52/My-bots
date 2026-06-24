# bot.py — главный файл бота.
#
# Здесь:
# • подключаем библиотеки и загружаем переменные окружения
# • описываем обработчики команд (/start, /tasks, /settings)
# • запускаем планировщик и сам бот

import asyncio
import logging
import os
import tempfile
from datetime import datetime, timezone, timedelta

MSK = timezone(timedelta(hours=3))

def now_msk() -> datetime:
    return datetime.now(tz=MSK)

from dotenv import load_dotenv
from telegram import Update, InlineQueryResultArticle, InputTextMessageContent
from telegram.ext import (
    Application,
    BusinessConnectionHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

from agents import generate_business_reply, process_with_mary
from db import (clear_history, delete_business_connection, get_bot_stats,
                get_tasks_for_day, get_upcoming_tasks, get_user_by_connection,
                init_db, load_biz_history, save_biz_message, save_business_connection)
from settings import get_all_user_ids
from scheduler_jobs import setup_scheduler
from settings import is_onboarding_done, load_settings, save_settings

# Загружаем переменные из файла .env (TELEGRAM_BOT_TOKEN и ANTHROPIC_API_KEY)
load_dotenv()

# Настраиваем логирование: все сообщения бота будут видны в терминале
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Онбординг — пошаговая настройка при первом запуске
# ──────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start — сбрасывает настройки и начинает онбординг заново."""
    user_id = update.effective_user.id

    # Telegram Business посылает /start bizChat<id> при нажатии «Управление ботом» — не сбрасываем бота
    if context.args and context.args[0].startswith("bizChat"):
        if is_onboarding_done(user_id):
            await update.message.reply_text("👋 Привет! Чем могу помочь?")
        return

    save_settings(user_id, {"onboarding_step": "ask_name", "onboarding_done": False})
    clear_history(user_id)
    await update.message.reply_text(
        "👋 Привет! Я *Мери* — твой личный помощник-планировщик.\n\n"
        "Вот что я умею:\n"
        "• 📝 Записывать твои дела и встречи — просто скажи мне о них\n"
        "• 🎤 Принимать голосовые сообщения — говори, я пойму\n"
        "• ⏰ Напоминать о событии за час, за 15 минут — когда скажешь\n"
        "• ☀️ Присылать сводку дел утром и 🌙 вечером\n"
        "• 🗓 Показывать расписание на день и все предстоящие дела\n"
        "• 💬 Понимать обычный текст — никаких форм и кнопок\n\n"
        "Просто пиши или говори как другу:\n"
        "_«завтра встреча с Максимом в 11, напомни за час»_\n"
        "_«в пятницу позвонить Кириллу и Андрею»_\n\n"
        "Давай познакомимся!",
        parse_mode="Markdown",
    )
    await update.message.reply_text("Как тебя зовут?")


def _extract_name(text: str) -> str:
    """Извлекает имя из фраз вроде 'меня зовут Матвей' → 'Матвей'."""
    text = text.strip().rstrip(".")
    for prefix in ["меня зовут ", "я ", "моё имя ", "мое имя ", "зовут меня ", "зовут "]:
        if text.lower().startswith(prefix):
            return text[len(prefix):].strip().capitalize()
    return text


# ──────────────────────────────────────────────
# Обработчик всех текстовых сообщений
# ──────────────────────────────────────────────

async def _route_text(
    user_id: int,
    text: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    voice_prefix: str = "",
):
    """Роутинг текста (из сообщения или голосового) через онбординг или основной режим."""
    settings = load_settings(user_id)
    step = settings.get("onboarding_step")

    # ── Шаг 1 онбординга: узнаём имя ──
    if step == "ask_name":
        name = _extract_name(text)
        save_settings(user_id, {"name": name, "onboarding_step": "ask_morning"})
        await update.message.reply_text(
            f"Отлично, *{name}*! 😊\n\n"
            "В какое время присылать утреннее сообщение с делами на день?\n"
            "Напиши в формате ЧЧ:ММ, например `08:00`",
            parse_mode="Markdown",
        )

    # ── Шаг 2 онбординга: время утреннего сообщения ──
    elif step == "ask_morning":
        try:
            parsed = datetime.strptime(text.strip(), "%H:%M")
        except ValueError:
            await update.message.reply_text(
                "Не понял формат 🤔 Напиши время так: `08:00`",
                parse_mode="Markdown",
            )
            return

        text = parsed.strftime("%H:%M")  # нормализуем: "7:00" → "07:00"
        save_settings(user_id, {"morning_time": text, "onboarding_step": "ask_evening"})
        await update.message.reply_text(
            f"Буду писать в *{text}* ☀️\n\n"
            "Хочешь ещё *вечернее* напоминание с делами на завтра?\n"
            "Ответь *да* или *нет*",
            parse_mode="Markdown",
        )

    # ── Шаг 3 онбординга: нужно ли вечернее сообщение ──
    elif step == "ask_evening":
        want_evening = text.lower() in ("да", "yes", "y", "д", "+", "хочу", "конечно")
        updates = {
            "evening_enabled": want_evening,
            "onboarding_done": True,
            "onboarding_step": None,
        }
        if want_evening:
            updates["evening_time"] = "20:00"

        save_settings(user_id, updates)
        name = settings.get("name", "")
        evening_note = "Вечернее напоминание в 20:00 включено 🌙\n" if want_evening else ""

        await update.message.reply_text(
            f"Готово, *{name}*! Настройки сохранены 🎉\n\n"
            f"Утреннее сообщение в {settings.get('morning_time', '—')} ☀️\n"
            f"{evening_note}\n"
            "Теперь просто говори мне о делах:\n"
            "_«завтра в 10 встреча с врачом»_\n"
            "_«в пятницу сдать отчёт»_\n\n"
            "📋 /tasks — дела на сегодня\n"
            "📅 /all — все предстоящие дела\n"
            "⚙️ /settings — настройки",
            parse_mode="Markdown",
        )

    # ── Обычный режим: передаём сообщение Мери ──
    elif is_onboarding_done(user_id):
        name = settings.get("name", "")
        await context.bot.send_chat_action(update.effective_chat.id, "typing")

        ts = now_msk().strftime("%d.%m %H:%M")
        await log_to_chat(context.bot, f"🕐 {ts}\n👤 *{name}:* {voice_prefix}{text}")

        async def send_sam(sam_text: str):
            await log_to_chat(context.bot, sam_text)

        reply = await process_with_mary(user_id, text, name, on_sam_message=send_sam)
        await update.message.reply_text(reply, parse_mode="Markdown")
        await log_to_chat(context.bot, f"🤖 *Мери:* {reply}")

    else:
        await update.message.reply_text("Напиши /start чтобы начать! 👋")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Игнорируем сообщения от самого себя во избежание петель
    bot_info = await context.bot.get_me()
    if update.effective_user.id == bot_info.id:
        return

    text = update.message.text.strip()

    # Для групповых чатов реагируем только если бот упомянут или это reply на его сообщение
    if update.message.chat.type in ("group", "supergroup"):
        is_mention = any(
            e.type == "mention" and f"@{bot_info.username}" in text
            for e in (update.message.entities or [])
        )
        is_reply_to_bot = (
            update.message.reply_to_message
            and update.message.reply_to_message.from_user
            and update.message.reply_to_message.from_user.id == bot_info.id
        )
        if not is_mention and not is_reply_to_bot:
            return
        # Убираем упоминание из текста перед обработкой
        text = text.replace(f"@{bot_info.username}", "").strip()

    await _route_text(user_id, text, update, context)


# ──────────────────────────────────────────────
# Команды
# ──────────────────────────────────────────────

async def tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tasks — показывает дела на сегодня."""
    user_id = update.effective_user.id
    today = now_msk().strftime("%Y-%m-%d")
    tasks = get_tasks_for_day(user_id, today)

    if not tasks:
        await update.message.reply_text("На сегодня дел нет 🎉 Отдыхай!")
        return

    d = now_msk()
    date_label = d.strftime("%-d %B").replace(
        "January","января").replace("February","февраля").replace("March","марта").replace(
        "April","апреля").replace("May","мая").replace("June","июня").replace(
        "July","июля").replace("August","августа").replace("September","сентября").replace(
        "October","октября").replace("November","ноября").replace("December","декабря")

    task_lines = []
    for t in tasks:
        if t["time"]:
            task_lines.append(f"⏰ {t['time']} — {t['text']}")
        else:
            task_lines.append(f"• {t['text']}")

    text = f"📋 *Дела на сегодня, {date_label}:*\n\n" + "\n\n".join(task_lines)
    await update.message.reply_text(text, parse_mode="Markdown")


async def all_tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/all — показывает все предстоящие дела."""
    user_id = update.effective_user.id
    tasks = get_upcoming_tasks(user_id)

    if not tasks:
        await update.message.reply_text("Предстоящих дел нет 🎉")
        return

    # Группируем дела по датам для удобного отображения
    by_date: dict[str, list] = {}
    for t in tasks:
        by_date.setdefault(t["date"], []).append(t)

    month_ru = {
        "January":"января","February":"февраля","March":"марта","April":"апреля",
        "May":"мая","June":"июня","July":"июля","August":"августа",
        "September":"сентября","October":"октября","November":"ноября","December":"декабря",
    }
    day_ru = {"Monday":"пн","Tuesday":"вт","Wednesday":"ср","Thursday":"чт",
              "Friday":"пт","Saturday":"сб","Sunday":"вс"}

    blocks = ["📅 *Все предстоящие дела:*"]
    for date, day_tasks in by_date.items():
        d = datetime.strptime(date, "%Y-%m-%d")
        month = month_ru[d.strftime("%B")]
        weekday = day_ru[d.strftime("%A")]
        date_str = f"{d.day} {month} ({weekday})"
        day_lines = [f"*{date_str}*"]
        for t in day_tasks:
            if t["time"]:
                day_lines.append(f"⏰ {t['time']} — {t['text']}")
            else:
                day_lines.append(f"• {t['text']}")
        blocks.append("\n\n".join(day_lines))

    await update.message.reply_text("\n\n".join(blocks), parse_mode="Markdown")


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/settings — показывает текущие настройки."""
    user_id = update.effective_user.id
    s = load_settings(user_id)

    evening = (
        f"включено ({s.get('evening_time', '20:00')})"
        if s.get("evening_enabled")
        else "выключено"
    )

    await update.message.reply_text(
        f"⚙️ *Настройки*\n\n"
        f"👤 Имя: {s.get('name', '—')}\n"
        f"☀️ Утреннее сообщение: {s.get('morning_time', '—')}\n"
        f"🌙 Вечернее сообщение: {evening}\n\n"
        "Чтобы изменить — напиши мне:\n"
        "_«поменяй утреннее на 09:30»_",
        parse_mode="Markdown",
    )


# ──────────────────────────────────────────────
# Запуск
# ──────────────────────────────────────────────

async def transcribe_voice(file_path: str) -> str:
    """Отправляет аудиофайл в AssemblyAI и возвращает распознанный текст."""
    import asyncio
    import assemblyai as aai
    aai.settings.api_key = os.environ["ASSEMBLYAI_API_KEY"]
    config = aai.TranscriptionConfig(language_code="ru")
    transcriber = aai.Transcriber(config=config)
    # AssemblyAI SDK синхронный — запускаем в отдельном потоке
    loop = asyncio.get_event_loop()
    transcript = await loop.run_in_executor(
        None, lambda: transcriber.transcribe(file_path)
    )
    if transcript.status == aai.TranscriptStatus.error:
        raise RuntimeError(transcript.error)
    return transcript.text


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик голосовых сообщений — работает и во время онбординга, и после."""
    user_id = update.effective_user.id

    # Отклоняем только если /start вообще не запускался
    if not load_settings(user_id):
        await update.message.reply_text("Напиши /start чтобы начать! 👋")
        return

    if not os.environ.get("ASSEMBLYAI_API_KEY"):
        await update.message.reply_text(
            "⚠️ Голосовые пока не настроены. Добавь ASSEMBLYAI_API_KEY в переменные."
        )
        return

    await context.bot.send_chat_action(update.effective_chat.id, "typing")

    voice = update.message.voice
    tg_file = await context.bot.get_file(voice.file_id)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name
    await tg_file.download_to_drive(tmp_path)

    try:
        recognized_text = await transcribe_voice(tmp_path)
    except Exception as e:
        logger.error("Ошибка транскрипции: %s", e)
        await update.message.reply_text("Не удалось распознать голосовое 😔 Попробуй ещё раз или напиши текстом.")
        return
    finally:
        os.unlink(tmp_path)

    await update.message.reply_text(
        f"🎤 Распознала: _{recognized_text}_",
        parse_mode="Markdown",
    )

    await _route_text(user_id, recognized_text, update, context, voice_prefix="🎤 ")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stats — статистика бота (только для владельца)."""
    ADMIN_ID = 6279401743
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


async def handle_business_connection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пользователь подключил/отключил бота как секретаря через Telegram Business."""
    conn = update.business_connection
    if conn.is_enabled:
        save_business_connection(conn.id, conn.user.id, conn.can_reply)
        await context.bot.send_message(
            chat_id=conn.user.id,
            text=(
                "🤝 Бизнес-подключение активно!\n\n"
                "Теперь я вижу твои входящие сообщения и буду уведомлять тебя "
                "о важных из них прямо здесь.\n\n"
                f"{'✅ Могу отвечать от твоего имени.' if conn.can_reply else '👁 Только чтение — отвечать не могу.'}"
            ),
        )
    else:
        delete_business_connection(conn.id)
        await context.bot.send_message(
            chat_id=conn.user.id,
            text="🔌 Бизнес-подключение отключено.",
        )


async def handle_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Входящее сообщение в чате пользователя через Business-подключение."""
    msg = update.business_message
    if not msg or not msg.text:
        return

    conn_id = msg.business_connection_id
    info = get_user_by_connection(conn_id)
    if not info:
        return

    user_id = info["user_id"]
    can_reply = bool(info["can_reply"])

    # Не реагируем на собственные сообщения владельца
    if msg.from_user and msg.from_user.id == user_id:
        return

    sender = msg.from_user.first_name or msg.from_user.username or "Неизвестный"

    # Уведомляем пользователя о входящем сообщении
    await context.bot.send_message(
        chat_id=user_id,
        text=f"📨 *{sender}:* {msg.text}",
        parse_mode="Markdown",
    )

    # Если разрешено отвечать — генерируем ответ от имени пользователя
    if can_reply:
        name = load_settings(user_id).get("name", "")

        # Сохраняем входящее сообщение в БД
        save_biz_message(conn_id, msg.chat.id, "user", msg.text)

        # Подгружаем реальное расписание
        upcoming = get_upcoming_tasks(user_id)
        if upcoming:
            task_lines = []
            for t in upcoming[:15]:
                time_str = f" в {t['time']}" if t["time"] else ""
                task_lines.append(f"• {t['date']}{time_str}: {t['text']}")
            tasks_context = "\n".join(task_lines)
        else:
            tasks_context = "Предстоящих задач нет."

        history = load_biz_history(conn_id, msg.chat.id)

        try:
            reply = await generate_business_reply(name, sender, history, tasks_context)
            logger.info("Business reply to %s: %s", sender, reply[:80])
            # Сохраняем ответ в БД
            save_biz_message(conn_id, msg.chat.id, "assistant", reply)
            # Задержка: ~1 сек на каждые 10 символов + базовые 3 сек, как будто человек набирает
            typing_delay = 3 + len(reply) / 10
            await asyncio.sleep(min(typing_delay, 12))  # не больше 12 сек
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


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Инлайн-режим: @Alice_time_bot [запрос] — показывает дела прямо в поле ввода."""
    user_id = update.inline_query.from_user.id
    query = update.inline_query.query.strip().lower()

    now = now_msk()
    today = now.strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    month_ru = {
        "January": "января", "February": "февраля", "March": "марта", "April": "апреля",
        "May": "мая", "June": "июня", "July": "июля", "August": "августа",
        "September": "сентября", "October": "октября", "November": "ноября", "December": "декабря",
    }

    def format_tasks(tasks, date_str) -> str:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        label = f"{d.day} {month_ru[d.strftime('%B')]}"
        if not tasks:
            return f"📅 {label}: дел нет 🎉"
        lines = [f"📅 {label}:"]
        for t in tasks:
            prefix = f"⏰ {t['time']} — " if t["time"] else "• "
            lines.append(f"{prefix}{t['text']}")
        return "\n".join(lines)

    results = []

    # Карточка «На сегодня»
    if not query or "сегодня" in query or "today" in query:
        tasks_today = get_tasks_for_day(user_id, today)
        text_today = format_tasks(tasks_today, today)
        results.append(InlineQueryResultArticle(
            id="today",
            title="📅 Дела на сегодня",
            description=f"{len(tasks_today)} дел" if tasks_today else "Дел нет",
            input_message_content=InputTextMessageContent(text_today),
        ))

    # Карточка «На завтра»
    if not query or "завтра" in query or "tomorrow" in query:
        tasks_tomorrow = get_tasks_for_day(user_id, tomorrow)
        text_tomorrow = format_tasks(tasks_tomorrow, tomorrow)
        results.append(InlineQueryResultArticle(
            id="tomorrow",
            title="📅 Дела на завтра",
            description=f"{len(tasks_tomorrow)} дел" if tasks_tomorrow else "Дел нет",
            input_message_content=InputTextMessageContent(text_tomorrow),
        ))

    # Карточка «Все предстоящие»
    if not query or "все" in query or "all" in query:
        all_tasks = get_upcoming_tasks(user_id)
        if all_tasks:
            by_date: dict[str, list] = {}
            for t in all_tasks:
                by_date.setdefault(t["date"], []).append(t)
            blocks = []
            for date, day_tasks in list(by_date.items())[:5]:
                blocks.append(format_tasks(day_tasks, date))
            text_all = "\n\n".join(blocks)
        else:
            text_all = "Предстоящих дел нет 🎉"
        results.append(InlineQueryResultArticle(
            id="all",
            title="📋 Все предстоящие дела",
            description=f"{len(all_tasks)} дел" if all_tasks else "Дел нет",
            input_message_content=InputTextMessageContent(text_all),
        ))

    await update.inline_query.answer(results, cache_time=30)


async def chatid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/chatid — показывает ID текущего чата. Используй в лог-чате, чтобы узнать его ID."""
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"ID этого чата: `{chat_id}`\n\nСкопируй и вставь в `.env` как:\n`LOG_CHAT_ID={chat_id}`",
        parse_mode="Markdown",
    )


async def log_to_chat(bot, text: str):
    """Отправляет одну строку в лог-чат (если LOG_CHAT_ID задан в .env)."""
    log_chat_id = os.environ.get("LOG_CHAT_ID")
    if not log_chat_id:
        return
    try:
        await bot.send_message(chat_id=int(log_chat_id), text=text, parse_mode="Markdown")
    except Exception:
        pass


async def post_init(app: Application):
    """Вызывается после инициализации бота, до начала polling.
    Здесь запускаем планировщик — он должен стартовать в том же event loop."""
    setup_scheduler(app)


def main():
    # Создаём таблицы в БД (если ещё не созданы)
    init_db()

    token = os.environ["TELEGRAM_BOT_TOKEN"]

    # Строим приложение
    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)   # hook для запуска планировщика
        .build()
    )

    # Регистрируем обработчики команд
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("tasks", tasks_command))
    app.add_handler(CommandHandler("all", all_tasks_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("chatid", chatid_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_handler(BusinessConnectionHandler(handle_business_connection))
    app.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, handle_business_message))

    # Обработчик текстовых сообщений (включая от ботов — для bot-to-bot)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Обработчик голосовых сообщений
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    # На Railway используем webhook — Telegram сам присылает обновления.
    # Локально используем polling — проще для разработки.
    webhook_url = os.environ.get("WEBHOOK_URL")
    if webhook_url:
        port = int(os.environ.get("PORT", 8080))
        logger.info("Бот запускается (webhook на порту %d)...", port)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            webhook_url=webhook_url,
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        logger.info("Бот запускается (polling)...")
        app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
