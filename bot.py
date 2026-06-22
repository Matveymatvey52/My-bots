# bot.py — главный файл бота.
#
# Здесь:
# • подключаем библиотеки и загружаем переменные окружения
# • описываем обработчики команд (/start, /tasks, /settings)
# • запускаем планировщик и сам бот

import logging
import os
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
        "👋 Привет! Я *Алиса*, твой помощник-напоминалка.\n\n"
        "Расскажи мне о любом деле, и я запомню.\n"
        "Сначала пара вопросов для настройки:\n\n"
        "Как тебя зовут?",
        parse_mode="Markdown",
    )


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
        # Показываем «печатает...», пока Алиса думает
        await context.bot.send_chat_action(update.effective_chat.id, "typing")
        reply = await process_with_alice(user_id, text, name)
        await update.message.reply_text(reply, parse_mode="Markdown")

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

    lines = ["📋 *Дела на сегодня:*\n"]
    for t in tasks:
        prefix = f"⏰ {t['time']} — " if t["time"] else "• "
        lines.append(f"{prefix}{t['text']}")

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

    lines = ["📅 *Все предстоящие дела:*\n"]
    for date, day_tasks in by_date.items():
        # Красиво форматируем дату
        d = datetime.strptime(date, "%Y-%m-%d")
        date_str = d.strftime("%d.%m (%A)").replace(
            "Monday", "пн").replace("Tuesday", "вт").replace(
            "Wednesday", "ср").replace("Thursday", "чт").replace(
            "Friday", "пт").replace("Saturday", "сб").replace("Sunday", "вс")
        lines.append(f"*{date_str}*")
        for t in day_tasks:
            prefix = f"  ⏰ {t['time']} — " if t["time"] else "  • "
            lines.append(f"{prefix}{t['text']}")
        lines.append("")  # пустая строка между датами

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

    # Обработчик всех текстовых сообщений (кроме команд)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запускается...")
    # run_polling — бот постоянно спрашивает Telegram: «есть новые сообщения?»
    # drop_pending_updates=True — игнорируем сообщения, пришедшие пока бот был выключен
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
