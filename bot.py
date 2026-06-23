# bot.py — главный файл бота.
#
# Здесь:
# • подключаем библиотеки и загружаем переменные окружения
# • описываем обработчики команд (/start, /tasks, /settings)
# • запускаем планировщик и сам бот

import logging
import os
import tempfile
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from agents import process_with_alice
from db import get_tasks_for_day, get_upcoming_tasks, init_db
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
    # Запоминаем, что онбординг начался и текущий шаг — вопрос имени
    save_settings(user_id, {"onboarding_step": "ask_name", "onboarding_done": False})
    await update.message.reply_text(
        "👋 Привет! Я *Алиса* — твой личный помощник-планировщик.\n\n"
        "Вот что я умею:\n"
        "• 📝 Записывать твои дела и встречи — просто скажи мне о них\n"
        "• ⏰ Напоминать о событии за час, за 15 минут — когда скажешь\n"
        "• ☀️ Присылать сводку дел утром и 🌙 вечером\n"
        "• 🗓 Показывать расписание на день и все предстоящие дела\n"
        "• 💬 Понимать обычный текст — никаких форм и кнопок\n\n"
        "Просто пиши как другу:\n"
        "_«завтра встреча с Максимом в 11, напомни за час»_\n"
        "_«в пятницу позвонить Кириллу и Андрею»_\n\n"
        "Давай познакомимся!",
        parse_mode="Markdown",
    )
    await update.message.reply_text("Как тебя зовут?")


# ──────────────────────────────────────────────
# Обработчик всех текстовых сообщений
# ──────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главный обработчик. Смотрит, на каком шаге онбординга пользователь,
    и либо задаёт следующий вопрос настройки, либо отправляет сообщение Алисе."""

    user_id = update.effective_user.id
    text = update.message.text.strip()
    settings = load_settings(user_id)
    step = settings.get("onboarding_step")

    # ── Шаг 1 онбординга: узнаём имя ──
    if step == "ask_name":
        name = text
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
            datetime.strptime(text, "%H:%M")  # проверяем формат
        except ValueError:
            await update.message.reply_text(
                "Не понял формат 🤔 Напиши время так: `08:00`",
                parse_mode="Markdown",
            )
            return

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
            updates["evening_time"] = "20:00"  # время по умолчанию

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

    # ── Обычный режим: передаём сообщение Алисе ──
    elif is_onboarding_done(user_id):
        name = settings.get("name", "")
        await context.bot.send_chat_action(update.effective_chat.id, "typing")

        ts = datetime.now().strftime("%d.%m %H:%M")
        await log_to_chat(context.bot, f"🕐 {ts}\n👤 *{name}:* {text}")

        async def send_sam(sam_text: str):
            await log_to_chat(context.bot, sam_text)

        reply = await process_with_alice(user_id, text, name, on_sam_message=send_sam)
        await update.message.reply_text(reply, parse_mode="Markdown")
        await log_to_chat(context.bot, f"🤖 *Алиса:* {reply}")

    else:
        # Пользователь ещё не запустил /start
        await update.message.reply_text("Напиши /start чтобы начать! 👋")


# ──────────────────────────────────────────────
# Команды
# ──────────────────────────────────────────────

async def tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tasks — показывает дела на сегодня."""
    user_id = update.effective_user.id
    today = datetime.now().strftime("%Y-%m-%d")
    tasks = get_tasks_for_day(user_id, today)

    if not tasks:
        await update.message.reply_text("На сегодня дел нет 🎉 Отдыхай!")
        return

    d = datetime.now()
    date_label = d.strftime("%-d %B").replace(
        "January","января").replace("February","февраля").replace("March","марта").replace(
        "April","апреля").replace("May","мая").replace("June","июня").replace(
        "July","июля").replace("August","августа").replace("September","сентября").replace(
        "October","октября").replace("November","ноября").replace("December","декабря")

    lines = [f"📋 *Дела на сегодня, {date_label}:*\n"]
    for t in tasks:
        if t["time"]:
            lines.append(f"⏰ {t['time']} — {t['text']}")
        else:
            lines.append(f"• {t['text']}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


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

    lines = ["📅 *Все предстоящие дела:*\n"]
    for date, day_tasks in by_date.items():
        d = datetime.strptime(date, "%Y-%m-%d")
        month = month_ru[d.strftime("%B")]
        weekday = day_ru[d.strftime("%A")]
        date_str = f"{d.day} {month} ({weekday})"
        lines.append(f"*{date_str}*")
        for t in day_tasks:
            if t["time"]:
                lines.append(f"  ⏰ {t['time']} — {t['text']}")
            else:
                lines.append(f"  • {t['text']}")
        lines.append("")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


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
    """Обработчик голосовых сообщений.
    Скачивает аудио → Whisper → текст → Алиса → ответ."""
    user_id = update.effective_user.id

    if not is_onboarding_done(user_id):
        await update.message.reply_text("Сначала напиши /start! 👋")
        return

    # Проверяем, что ключ Groq задан
    if not os.environ.get("ASSEMBLYAI_API_KEY"):
        await update.message.reply_text(
            "⚠️ Голосовые пока не настроены. Добавь ASSEMBLYAI_API_KEY в переменные."
        )
        return

    await context.bot.send_chat_action(update.effective_chat.id, "typing")

    # Скачиваем голосовое во временный файл
    voice = update.message.voice
    tg_file = await context.bot.get_file(voice.file_id)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name
    await tg_file.download_to_drive(tmp_path)

    # Транскрибируем через Whisper
    try:
        recognized_text = await transcribe_voice(tmp_path)
    except Exception as e:
        logger.error("Ошибка транскрипции: %s", e)
        await update.message.reply_text("Не удалось распознать голосовое 😔 Попробуй отправить новое или написать текстом.")
        return
    finally:
        os.unlink(tmp_path)  # удаляем временный файл

    # Показываем что распознали — полезно для проверки
    await update.message.reply_text(
        f"🎤 Распознала: _{recognized_text}_",
        parse_mode="Markdown",
    )

    # Дальше — как обычное текстовое сообщение: передаём Алисе
    name = load_settings(user_id).get("name", "")

    ts = datetime.now().strftime("%d.%m %H:%M")
    await log_to_chat(context.bot, f"🕐 {ts}\n👤 *{name}:* 🎤 {recognized_text}")

    async def send_sam_voice(sam_text: str):
        await log_to_chat(context.bot, sam_text)

    reply = await process_with_alice(user_id, recognized_text, name, on_sam_message=send_sam_voice)
    await update.message.reply_text(reply, parse_mode="Markdown")
    await log_to_chat(context.bot, f"🤖 *Алиса:* {reply}")


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

    # Обработчик всех текстовых сообщений (кроме команд)
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
        )
    else:
        logger.info("Бот запускается (polling)...")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
