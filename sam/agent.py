import asyncio
from datetime import datetime, timezone, timedelta
from anthropic import AsyncAnthropic
from shared.db import (
    add_task, delete_task, get_tasks_for_day, get_upcoming_tasks,
)
from shared.settings import save_settings

MSK = timezone(timedelta(hours=3))

def now_msk() -> datetime:
    return datetime.now(tz=MSK)

client = AsyncAnthropic()

def _sam_system(requester: str) -> str:
    return f"""\
Ты — Сэм, менеджер расписания. Получаешь задания от {requester}, выполняешь их инструментами.

Правила:
- Повторяющиеся задачи (каждый час, каждые N минут) → ВСЕГДА используй create_recurring_tasks, не create_task вручную по одной
- Несколько разных задач → вызывай create_task для каждой отдельно
- Если задание содержит время и это время уже прошло — НЕ создавай задачу, сообщи что время уже прошло
- Если пользователь явно говорит «создай новую» или «всё равно создай» — создавай без проверок
- Удалить задачу → сначала get_tasks чтобы найти id, потом delete_task(task_id)
- В поле text ВСЕГДА добавляй подходящий эмодзи по смыслу:
  ✂️ стрижка/салон, 📞 звонок, 🤝 встреча, 🏥 врач/здоровье, 🎂 праздник,
  🏋️ спорт, 🍽️ обед/ужин, ✈️ поездка, 💊 таблетки, 📝 документы,
  🛒 покупки, 💰 финансы, 🎓 учёба, 🎮 досуг — и т.д.
- После выполнения пиши краткий отчёт для {requester} (2-3 предложения): что сделал, детали.
- Обращайся к собеседнику по имени ({requester}), не путай его с другими.\
"""

CREATE_TASK_TOOL = {
    "name": "create_task",
    "description": "Записать новое дело в расписание.",
    "input_schema": {
        "type": "object",
        "properties": {
            "date":             {"type": "string", "description": "Дата: YYYY-MM-DD"},
            "time":             {"type": "string", "description": "Время: HH:MM или null"},
            "text":             {"type": "string", "description": "Текст задачи"},
            "reminder_minutes": {"type": "integer", "description": "За сколько минут напомнить. null если не нужно."},
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
            "name":            {"type": "string",  "description": "Новое имя или null"},
            "morning_time":    {"type": "string",  "description": "Время утреннего HH:MM или null"},
            "evening_time":    {"type": "string",  "description": "Время вечернего HH:MM или null"},
            "evening_enabled": {"type": "boolean", "description": "True/False или null"},
        },
        "required": [],
    },
}

DELETE_TASK_TOOL = {
    "name": "delete_task",
    "description": "Удалить задачу из расписания по id.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "integer", "description": "id задачи"},
        },
        "required": ["task_id"],
    },
}

CREATE_RECURRING_TOOL = {
    "name": "create_recurring_tasks",
    "description": "Создать повторяющиеся задачи через равные промежутки времени. Используй когда пользователь просит напоминать каждый час, каждые N минут, несколько раз в день.",
    "input_schema": {
        "type": "object",
        "properties": {
            "date":             {"type": "string",  "description": "Дата: YYYY-MM-DD"},
            "start_time":       {"type": "string",  "description": "Начальное время: HH:MM"},
            "end_time":         {"type": "string",  "description": "Конечное время: HH:MM (включительно)"},
            "interval_minutes": {"type": "integer", "description": "Интервал в минутах (60 = каждый час, 30 = каждые полчаса)"},
            "text":             {"type": "string",  "description": "Текст задачи"},
            "reminder_minutes": {"type": "integer", "description": "За сколько минут до каждого события напомнить. null если не нужно."},
        },
        "required": ["date", "start_time", "end_time", "interval_minutes", "text"],
    },
}

GET_TASKS_TOOL = {
    "name": "get_tasks",
    "description": "Получить дела на дату или все предстоящие.",
    "input_schema": {
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD или 'upcoming'"},
        },
        "required": ["date"],
    },
}

SAM_TOOLS = [CREATE_TASK_TOOL, CREATE_RECURRING_TOOL, DELETE_TASK_TOOL, UPDATE_SETTINGS_TOOL, GET_TASKS_TOOL]


async def process_with_sam(user_id: int, task_message: str, requester: str = "Мери") -> str:
    """Выполняет задание инструментами, возвращает отчёт. requester — кто обратился (Мери или имя человека)."""
    now = now_msk()
    time_info = f"\n\n[Системная информация: сейчас {now.strftime('%H:%M')} МСК, дата {now.strftime('%Y-%m-%d')}]"
    messages = [{"role": "user", "content": f"Задание от {requester}:\n{task_message}{time_info}"}]

    for _ in range(6):
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=_sam_system(requester),
            messages=messages,
            tools=SAM_TOOLS,
        )

        if response.stop_reason != "tool_use":
            return next((b.text for b in response.content if b.type == "text"), "Выполнено.")

        tool_blocks = [b for b in response.content if b.type == "tool_use"]
        tool_results = []

        for tb in tool_blocks:
            name = tb.name
            data = tb.input

            if name == "create_recurring_tasks":
                from datetime import timedelta as _td
                date = data["date"]
                start = datetime.strptime(f"{date} {data['start_time']}", "%Y-%m-%d %H:%M")
                end   = datetime.strptime(f"{date} {data['end_time']}",   "%Y-%m-%d %H:%M")
                interval = int(data["interval_minutes"])
                raw_reminder = data.get("reminder_minutes")
                reminder_val = int(raw_reminder) if isinstance(raw_reminder, (int, float)) and raw_reminder > 0 else None
                created_times = []
                current = start
                while current <= end:
                    add_task(
                        user_id=user_id,
                        date=date,
                        text=data["text"],
                        time=current.strftime("%H:%M"),
                        reminder_minutes=reminder_val,
                    )
                    created_times.append(current.strftime("%H:%M"))
                    current += _td(minutes=interval)
                result = f"Создано {len(created_times)} задач: {', '.join(created_times)}."

            elif name == "create_task":
                raw_time = data.get("time")
                time_val = None if (raw_time is None or str(raw_time).lower() in ("null", "none", "")) else raw_time
                raw_reminder = data.get("reminder_minutes")
                reminder_val = int(raw_reminder) if isinstance(raw_reminder, (int, float)) and raw_reminder > 0 else None
                task_id = add_task(
                    user_id=user_id,
                    date=data["date"],
                    text=data["text"],
                    time=time_val,
                    reminder_minutes=reminder_val,
                )
                result = f"Задача записана, id={task_id}."

            elif name == "update_settings":
                def _norm(t):
                    try:
                        return datetime.strptime(t.strip(), "%H:%M").strftime("%H:%M")
                    except Exception:
                        return t

                updates = {}
                if data.get("name") not in (None, "null", ""):
                    updates["name"] = data["name"]
                if data.get("morning_time") not in (None, "null", ""):
                    updates["morning_time"] = _norm(data["morning_time"])
                if data.get("evening_time") not in (None, "null", ""):
                    updates["evening_time"] = _norm(data["evening_time"])
                if data.get("evening_enabled") is not None:
                    updates["evening_enabled"] = data["evening_enabled"]
                if updates:
                    save_settings(user_id, updates)
                result = f"Настройки обновлены: {updates}."

            elif name == "delete_task":
                task_id = int(data["task_id"])
                ok = delete_task(user_id=user_id, task_id=task_id)
                result = f"Задача {task_id} удалена." if ok else f"Задача {task_id} не найдена."

            elif name == "get_tasks":
                date = data.get("date", "")
                tasks = get_upcoming_tasks(user_id) if date == "upcoming" else get_tasks_for_day(user_id, date)
                if not tasks:
                    result = "Дел нет."
                else:
                    lines = []
                    for t in tasks:
                        prefix = f"⏰ {t['time']} — " if t["time"] else "• "
                        lines.append(f"[id={t['id']}] {prefix}{t['text']}")
                    result = "\n".join(lines)

            else:
                result = "Неизвестный инструмент."

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tb.id,
                "content": result,
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return "Выполнено."
