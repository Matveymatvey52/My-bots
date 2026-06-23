# scheduler_jobs.py — планировщик для ежедневных сообщений.
#
# APScheduler — библиотека для запуска задач по расписанию.
# Мы используем AsyncIOScheduler, который работает в том же event loop, что и бот.
# Каждую минуту проверяем: не пора ли кому-то отправить утреннее/вечернее сообщение.

import json
import logging
import random
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))

def now_msk() -> datetime:
    return datetime.now(tz=MSK)

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.ext import Application

from db import get_tasks_for_day, get_tasks_needing_reminder, get_tasks_due_now, mark_reminder_sent, mark_time_notified
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
    now = now_msk()
    current_time = now.strftime("%H:%M")  # например, "08:00"
    today = now.strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    # ── Предварительные напоминания ──
    for task in get_tasks_needing_reminder(now):
        mins = task["reminder_minutes"]
        label = f"через {mins} мин" if mins < 60 else f"через {mins // 60} ч"
        text = f"⏰ *Напоминание!* {label}: *{task['text']}* в {task['time']}"
        try:
            await app.bot.send_message(chat_id=task["user_id"], text=text, parse_mode="Markdown")
            mark_reminder_sent(task["id"])
        except Exception as e:
            logger.error("Не удалось отправить напоминание %d: %s", task["id"], e)

    # ── Уведомление в момент события ──
    for task in get_tasks_due_now(now):
        text = f"🔔 *{task['time']} — {task['text']}*\nВремя!"
        try:
            await app.bot.send_message(chat_id=task["user_id"], text=text, parse_mode="Markdown")
            mark_time_notified(task["id"])
        except Exception as e:
            logger.error("Не удалось отправить уведомление о событии %d: %s", task["id"], e)

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

    d = datetime.strptime(date, "%Y-%m-%d")
    date_label = d.strftime("%-d %B").replace(
        "January","января").replace("February","февраля").replace("March","марта").replace(
        "April","апреля").replace("May","мая").replace("June","июня").replace(
        "July","июля").replace("August","августа").replace("September","сентября").replace(
        "October","октября").replace("November","ноября").replace("December","декабря")

    if morning:
        header = f"☀️ *Доброе утро, {name}!*\n📅 На сегодня, {date_label}:"
    else:
        header = f"🌙 *Добрый вечер, {name}!*\n📅 На завтра, {date_label}:"

    if not tasks:
        text = f"{header}\n\nДел нет — можно отдыхать 🎉"
    else:
        task_lines = []
        for t in tasks:
            if t["time"]:
                task_lines.append(f"⏰ {t['time']} — {t['text']}")
            else:
                task_lines.append(f"• {t['text']}")
        closing = random.choice([
            "Насыщенный день! 💪",
            "Всё успеешь! 🙌",
            "Удачного дня! ⭐",
            "Держи темп! 🚀",
            "Ты справишься! 😊",
            "Продуктивного дня! ✨",
        ])
        text = header + "\n\n" + "\n\n".join(task_lines) + f"\n\n{closing}"

    try:
        await app.bot.send_message(chat_id=user_id, text=text)
        logger.info("Отправлено %s сообщение пользователю %d", "утреннее" if morning else "вечернее", user_id)
    except Exception as e:
        # Бывает, если пользователь заблокировал бота
        logger.error("Не удалось отправить сообщение %d: %s", user_id, e)
