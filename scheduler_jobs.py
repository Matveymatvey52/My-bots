# scheduler_jobs.py — планировщик для ежедневных сообщений.
#
# APScheduler — библиотека для запуска задач по расписанию.
# Мы используем AsyncIOScheduler, который работает в том же event loop, что и бот.
# Каждую минуту проверяем: не пора ли кому-то отправить утреннее/вечернее сообщение.

import json
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.ext import Application

from db import get_tasks_for_day
from settings import SETTINGS_DIR, get_all_user_ids, load_settings

logger = logging.getLogger(__name__)


def setup_scheduler(app: Application):
    """Создаёт и запускает планировщик. Вызывается один раз при старте бота."""

    # Планировщик, совместимый с asyncio (нужен для async-функций)
    scheduler = AsyncIOScheduler()

    # Каждую минуту вызываем проверку
    scheduler.add_job(
        send_scheduled_messages,
        trigger="cron",
        minute="*",       # каждую минуту
        args=[app],
    )

    scheduler.start()
    logger.info("Планировщик запущен")


async def send_scheduled_messages(app: Application):
    """
    Проверяет всех пользователей: не пора ли слать сообщение?
    Сравниваем текущее время (HH:MM) с настройками пользователя.

    Важно: используется время сервера/компьютера, на котором запущен бот.
    Если запускаешь локально — всё совпадёт с твоим часовым поясом.
    """
    now = datetime.now()
    current_time = now.strftime("%H:%M")  # например, "08:00"
    today = now.strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    for user_id in get_all_user_ids():
        s = load_settings(user_id)

        # Пропускаем тех, кто не закончил настройку
        if not s.get("onboarding_done"):
            continue

        name = s.get("name", "")

        # Утреннее сообщение — дела на сегодня
        if s.get("morning_time") == current_time:
            await _send_daily_summary(app, user_id, today, name, morning=True)

        # Вечернее сообщение — дела на завтра (если включено)
        if s.get("evening_enabled") and s.get("evening_time", "20:00") == current_time:
            await _send_daily_summary(app, user_id, tomorrow, name, morning=False)


async def _send_daily_summary(app: Application, user_id: int, date: str, name: str, morning: bool):
    """Составляет и отправляет сводку дел на указанный день."""

    tasks = get_tasks_for_day(user_id, date)

    if morning:
        header = f"☀️ Доброе утро, {name}! На сегодня:"
    else:
        header = f"🌙 Добрый вечер, {name}! На завтра:"

    if not tasks:
        text = f"{header}\n\nДел нет — можно отдыхать 🎉"
    else:
        lines = [header, ""]
        for t in tasks:
            # Если время указано — показываем его, иначе просто точка
            prefix = f"⏰ {t['time']} — " if t["time"] else "• "
            lines.append(f"{prefix}{t['text']}")
        text = "\n".join(lines)

    try:
        await app.bot.send_message(chat_id=user_id, text=text)
        logger.info("Отправлено %s сообщение пользователю %d", "утреннее" if morning else "вечернее", user_id)
    except Exception as e:
        # Бывает, если пользователь заблокировал бота
        logger.error("Не удалось отправить сообщение %d: %s", user_id, e)
