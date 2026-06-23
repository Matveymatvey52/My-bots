# agents.py — Мери и Сэм.
#
# АЛИСА — администратор. Общается с пользователем.
#   Сама НЕ трогает базу данных — только пишет задание Сэму.
#   Инструмент: contact_sam(message) — отправить задание Сэму.
#
# СЭМ — менеджер расписания. Получает задание от Мери.
#   Выполняет его с помощью инструментов (create_task, update_settings).
#   Возвращает письменный отчёт → Мери передаёт его пользователю.
#
# Схема:
#   Пользователь → [Мери] → contact_sam → [Сэм] → инструменты → отчёт → [Мери] → Пользователь

import asyncio
import os
from datetime import datetime
from typing import Optional
from anthropic import AsyncAnthropic
from db import add_task, delete_task, get_tasks_for_day, get_upcoming_tasks, load_history, save_message
from settings import save_settings

# Клиент читает ANTHROPIC_API_KEY из переменных окружения
client = AsyncAnthropic()

MAX_HISTORY = 20


# ══════════════════════════════════════════════════
#  СИСТЕМНЫЕ ПРОМПТЫ
# ══════════════════════════════════════════════════

MARY_SYSTEM = """\
Ты — Мери, администратор-помощник в Telegram-боте.
Ты общаешься с пользователем по имени {name} на русском языке, дружелюбно и по-деловому.

У тебя есть внутренний механизм записи — ты используешь contact_sam для работы с данными.
ВАЖНО: В ответах пользователю НИКОГДА не упоминай «Сэма» — говори от первого лица, как будто всё делаешь сама.

Когда нужно что-то сделать с данными, используй contact_sam:
• Пользователь упоминает дело с датой/временем → запиши через contact_sam
• Пользователь хочет напоминание за N минут/часов → передай reminder_minutes в contact_sam
• Пользователь упоминает несколько дел → передай ВСЕ задачи одним сообщением в contact_sam
• Пользователь спрашивает про свои дела на день/завтра/другую дату → запроси через contact_sam
• Пользователь хочет удалить/отменить задачу → передай через contact_sam
• Пользователь хочет изменить настройки (имя, время утреннего/вечернего сообщения, вкл/выкл вечернее) → передай через contact_sam
• Пользователь хочет сменить имя → это ВСЕГДА возможно, передай через contact_sam с новым именем

Когда пишешь в contact_sam — формулируй чётко:
- Все задачи (можно несколько)
- Дата в формате YYYY-MM-DD
- Время в формате HH:MM (если есть)
- reminder_minutes: за сколько минут до события напомнить (если пользователь просит)
- Что изменить в настройках (если нужно)

После получения отчёта от Сэма — кратко сообщи пользователю что сделано, от своего имени.
Если в отчёте есть полезное наблюдение или совет — обязательно передай его пользователю.
На обычные вопросы отвечай сам(а), без contact_sam.

Форматирование: никогда не используй markdown-таблицы (Telegram их не отображает).
Списки дел всегда оформляй так (между каждым делом — пустая строка):
⏰ ЧЧ:ММ — Название

⏰ ЧЧ:ММ — Название

• Название (если без времени)
В своих ответах активно используй эмодзи — они делают общение живее 😊

Сегодня: {today} ({weekday}).\
"""

SAM_SYSTEM = """\
Ты — Сэм, менеджер расписания. Получаешь задания от Мери, выполняешь их инструментами.

Правила:
- Несколько задач → вызывай create_task для каждой отдельно
- Перед созданием задачи: если задание выглядит как уже существующая запись (та же дата+время+смысл), сначала get_tasks и проверь — если дубль, сообщи об этом вместо создания новой
- Удалить задачу → сначала get_tasks чтобы найти id, потом delete_task(task_id)
- В поле text ВСЕГДА добавляй в начало подходящий эмодзи по смыслу задачи:
  ✂️ стрижка/салон, 📞 звонок, 🤝 встреча, 🏥 врач/здоровье, 🎂 день рождения/праздник,
  🏋️ спорт/тренировка, 🍽️ обед/ужин/кафе, ✈️ поездка/перелёт, 💊 таблетки,
  📝 документы/отчёт, 🛒 покупки, 💰 финансы/оплата, 🎓 учёба, 🎮 досуг — и т.д.
- После выполнения напиши краткий отчёт с эмодзи (2-4 предложения): что сделал, детали.
- Можешь добавить одно короткое замечание если важно.\
"""


# ══════════════════════════════════════════════════
#  ИНСТРУМЕНТЫ
# ══════════════════════════════════════════════════

# Инструмент Мери — связаться с Сэмом
CONTACT_SAM_TOOL = {
    "name": "contact_sam",
    "description": "Передать задание Сэму (менеджеру расписания). Использовать когда нужно записать дело или изменить настройки.",
    "input_schema": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": (
                    "Чёткое задание для Сэма. Укажи: что делать, дату (YYYY-MM-DD), "
                    "время (HH:MM если есть), текст задачи или какие настройки менять."
                ),
            }
        },
        "required": ["message"],
    },
}

# Инструменты Сэма — реальные операции с данными
CREATE_TASK_TOOL = {
    "name": "create_task",
    "description": "Записать новое дело в расписание.",
    "input_schema": {
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "Дата: YYYY-MM-DD"},
            "time": {"type": "string", "description": "Время: HH:MM или null"},
            "text": {"type": "string", "description": "Текст задачи"},
            "reminder_minutes": {
                "type": "integer",
                "description": "За сколько минут до события напомнить. Например: 60 = за час, 15 = за 15 минут. null если напоминание не нужно.",
            },
        },
        "required": ["date", "text"],
    },
}

UPDATE_SETTINGS_TOOL = {
    "name": "update_settings",
    "description": "Изменить настройки пользователя.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Новое имя пользователя, или null если не меняется",
            },
            "morning_time": {
                "type": "string",
                "description": "Новое время утреннего сообщения HH:MM, или null если не меняется",
            },
            "evening_time": {
                "type": "string",
                "description": "Новое время вечернего сообщения HH:MM, или null если не меняется",
            },
            "evening_enabled": {
                "type": "boolean",
                "description": "True — включить вечернее, False — выключить, null — не менять",
            },
        },
        "required": [],
    },
}

DELETE_TASK_TOOL = {
    "name": "delete_task",
    "description": "Удалить (отменить) задачу из расписания по её id.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "integer", "description": "id задачи которую нужно удалить"},
        },
        "required": ["task_id"],
    },
}

GET_TASKS_TOOL = {
    "name": "get_tasks",
    "description": "Получить список дел пользователя на указанную дату или все предстоящие.",
    "input_schema": {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "Дата YYYY-MM-DD — дела на этот день. Или 'upcoming' — все предстоящие дела.",
            }
        },
        "required": ["date"],
    },
}

SAM_TOOLS = [CREATE_TASK_TOOL, DELETE_TASK_TOOL, UPDATE_SETTINGS_TOOL, GET_TASKS_TOOL]


# ══════════════════════════════════════════════════
#  ОСНОВНЫЕ ФУНКЦИИ
# ══════════════════════════════════════════════════

async def process_with_mary(user_id: int, user_message: str, user_name: str, on_sam_message=None) -> str:
    """Обёртка с таймаутом и обработкой ошибок API."""
    try:
        return await asyncio.wait_for(
            _process_with_mary(user_id, user_message, user_name, on_sam_message),
            timeout=60.0,
        )
    except asyncio.TimeoutError:
        return "Извини, что-то подвисло — попробуй написать ещё раз! 😅"
    except Exception as e:
        err = str(e)
        if "529" in err or "overloaded" in err.lower():
            return "Серверы немного перегружены — подожди секунду и попробуй ещё раз 🙏"
        if "529" in err or "rate" in err.lower():
            return "Слишком много запросов — попробуй через несколько секунд ⏳"
        return "Что-то пошло не так — попробуй ещё раз 😔"


async def _process_with_mary(user_id: int, user_message: str, user_name: str, on_sam_message=None) -> str:
    """
    Главная функция: принимает сообщение пользователя, отдаёт Мери.
    Если Мери решает делегировать Сэму — запускает process_with_sam.
    Возвращает финальный текст для отправки пользователю.
    """

    today = datetime.now()
    weekdays = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    mary_system = MARY_SYSTEM.format(
        name=user_name,
        today=today.strftime("%d.%m.%Y"),
        weekday=weekdays[today.weekday()],
    )

    # История диалога Мери с этим пользователем (из БД)
    history = load_history(user_id, limit=MAX_HISTORY)
    history.append({"role": "user", "content": user_message})

    # ── Запрос к Мери ──
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=mary_system,
        messages=history[-MAX_HISTORY:],
        tools=[CONTACT_SAM_TOOL],
    )

    if response.stop_reason == "tool_use":
        # Мери хочет связаться с Сэмом
        tool_block = next(b for b in response.content if b.type == "tool_use")
        sam_task = tool_block.input["message"]

        # Сэм принял задание
        if on_sam_message:
            await on_sam_message("⚙️ *Сэм:* Принял, выполняю...")

        # ── Сэм выполняет задание и даёт отчёт ──
        sam_report = await process_with_sam(user_id, sam_task)

        # Сэм отчитывается
        if on_sam_message:
            await on_sam_message(f"📋 *Сэм → Мери:* {sam_report}")

        # Возвращаем отчёт Сэма обратно Мери
        messages_with_sam = history[-MAX_HISTORY:] + [
            {"role": "assistant", "content": response.content},
            {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_block.id,
                    "content": f"Отчёт Сэма: {sam_report}",
                }],
            },
        ]

        # Мери формулирует финальный ответ пользователю на основе отчёта Сэма
        # tools не передаём — она уже получила результат, просто отвечает пользователю
        final = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=mary_system,
            messages=messages_with_sam,
        )
        mary_reply = next((b.text for b in final.content if b.type == "text"), "Готово!")

    else:
        # Мери отвечает напрямую (без Сэма)
        mary_reply = next((b.text for b in response.content if b.type == "text"), "")

    # Сохраняем оба сообщения в БД
    save_message(user_id, "user", user_message)
    save_message(user_id, "assistant", mary_reply)

    return mary_reply


async def process_with_sam(user_id: int, mary_message: str) -> str:
    """
    Сэм получает задание от Мери, выполняет его с помощью инструментов
    и возвращает текстовый отчёт.
    """

    # ── Сэм читает задание и решает что делать ──
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=SAM_SYSTEM,
        messages=[{"role": "user", "content": f"Задание от Мери:\n{mary_message}"}],
        tools=SAM_TOOLS,
    )

    if response.stop_reason == "tool_use":
        tool_blocks = [b for b in response.content if b.type == "tool_use"]
        tool_results = []

        for tool_block in tool_blocks:
            tool_name = tool_block.name
            tool_data = tool_block.input

            if tool_name == "create_task":
                raw_time = tool_data.get("time")
                time_val = None if (raw_time is None or str(raw_time).lower() in ("null", "none", "")) else raw_time
                raw_reminder = tool_data.get("reminder_minutes")
                reminder_val = int(raw_reminder) if isinstance(raw_reminder, (int, float)) and raw_reminder > 0 else None
                task_id = add_task(
                    user_id=user_id,
                    date=tool_data["date"],
                    text=tool_data["text"],
                    time=time_val,
                    reminder_minutes=reminder_val,
                )
                tool_result = f"Задача записана, id={task_id}."

            elif tool_name == "update_settings":
                updates = {}
                if tool_data.get("name") not in (None, "null", ""):
                    updates["name"] = tool_data["name"]
                if tool_data.get("morning_time") not in (None, "null", ""):
                    updates["morning_time"] = tool_data["morning_time"]
                if tool_data.get("evening_time") not in (None, "null", ""):
                    updates["evening_time"] = tool_data["evening_time"]
                if tool_data.get("evening_enabled") is not None:
                    updates["evening_enabled"] = tool_data["evening_enabled"]
                if updates:
                    save_settings(user_id, updates)
                tool_result = f"Настройки обновлены: {updates}."

            elif tool_name == "delete_task":
                task_id = int(tool_data["task_id"])
                ok = delete_task(user_id=user_id, task_id=task_id)
                tool_result = f"Задача {task_id} удалена." if ok else f"Задача {task_id} не найдена."

            elif tool_name == "get_tasks":
                date = tool_data.get("date", "")
                if date == "upcoming":
                    tasks = get_upcoming_tasks(user_id)
                else:
                    tasks = get_tasks_for_day(user_id, date)
                if not tasks:
                    tool_result = "Дел нет."
                else:
                    lines = []
                    for t in tasks:
                        prefix = f"⏰ {t['time']} — " if t["time"] else "• "
                        lines.append(f"[id={t['id']}] {prefix}{t['text']}")
                    tool_result = "\n\n".join(lines)

            else:
                tool_result = "Неизвестный инструмент."

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_block.id,
                "content": tool_result,
            })

        sam_messages = [
            {"role": "user", "content": f"Задание от Мери:\n{mary_message}"},
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": tool_results},
        ]

        report_response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=SAM_SYSTEM,
            messages=sam_messages,
        )
        return next((b.text for b in report_response.content if b.type == "text"), "Выполнено.")

    else:
        return next((b.text for b in response.content if b.type == "text"), "Выполнено.")
