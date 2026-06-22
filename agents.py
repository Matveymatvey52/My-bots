# agents.py — здесь живут Алиса и Сэм.
#
# Как это работает:
# 1. Пользователь пишет что-то боту ("завтра в 10 встреча")
# 2. Мы отправляем это Алисе (запрос к Claude)
# 3. Алиса решает: просто ответить или создать задачу
# 4. Если создать — она вызывает инструмент create_task
# 5. Мы выполняем инструмент: просим Сэма записать задачу в БД
# 6. Отправляем результат обратно Алисе → она даёт финальный ответ пользователю

import os
from datetime import datetime
from typing import Optional
from anthropic import AsyncAnthropic
from db import add_task

# Клиент сам читает ANTHROPIC_API_KEY из переменных окружения
client = AsyncAnthropic()

# Хранилище истории диалогов: {user_id: [список сообщений]}
# Хранится в памяти — при перезапуске бота история сбрасывается
conversation_history: dict[int, list] = {}

# Максимум сообщений в истории (старые обрезаются)
MAX_HISTORY = 20


# ──────────────────────────────────────────────
# Системные промпты (роли персонажей)
# ──────────────────────────────────────────────

ALICE_SYSTEM = """\
Ты — Алиса, дружелюбный помощник-администратор в Telegram-боте.
Ты общаешься с пользователем по имени {name} на русском языке, живо и по-деловому.

Твои задачи:
1. Принимать дела в свободной форме. Например:
   • "завтра в 10 встреча с врачом"
   • "в пятницу сдать отчёт"
   • "через 3 дня день рождения мамы"
2. Понимать относительные даты ("сегодня", "завтра", "послезавтра",
   "в пятницу", "через неделю") и переводить их в конкретные числа.
3. Если пользователь говорит о деле с датой — вызвать инструмент create_task.
4. Если дата не указана — уточнить, когда именно.
5. На обычные вопросы и разговор отвечать дружески, без лишних формальностей.

Сегодня: {today} ({weekday}).

Когда добавляешь дело, скажи пользователю, что передала его Сэму.\
"""

SAM_SYSTEM = """\
Ты — Сэм, помощник по расписанию.
Ты получаешь задачу и кратко подтверждаешь запись одним предложением на русском.\
"""


# ──────────────────────────────────────────────
# Описание инструмента для Claude
# (именно так мы говорим Алисе: "вот что ты можешь сделать")
# ──────────────────────────────────────────────

CREATE_TASK_TOOL = {
    "name": "create_task",
    "description": "Записать дело в расписание пользователя.",
    "input_schema": {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "Дата в формате YYYY-MM-DD",
            },
            "time": {
                "type": "string",
                "description": "Время в формате HH:MM. Передай null, если время не указано.",
            },
            "text": {
                "type": "string",
                "description": "Краткое описание дела (1–2 предложения).",
            },
        },
        "required": ["date", "text"],
    },
}


# ──────────────────────────────────────────────
# Основная функция: пользователь → Алиса → ответ
# ──────────────────────────────────────────────

async def process_with_alice(user_id: int, user_message: str, user_name: str) -> str:
    """Обрабатывает сообщение пользователя через Алису и возвращает текст ответа."""

    # Формируем системный промпт с текущей датой
    today = datetime.now()
    weekdays = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    system = ALICE_SYSTEM.format(
        name=user_name,
        today=today.strftime("%d.%m.%Y"),
        weekday=weekdays[today.weekday()],
    )

    # Берём историю диалога этого пользователя
    history = conversation_history.get(user_id, []).copy()
    history.append({"role": "user", "content": user_message})

    # ── Шаг 1: первый запрос к Алисе ──
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=history[-MAX_HISTORY:],
        tools=[CREATE_TASK_TOOL],
    )

    sam_note = ""  # подтверждение от Сэма (если задача создавалась)

    if response.stop_reason == "tool_use":
        # Алиса решила создать задачу — обрабатываем вызов инструмента

        # Достаём блок с вызовом инструмента из ответа
        tool_block = next(b for b in response.content if b.type == "tool_use")
        task_data = tool_block.input

        # Очищаем поле time: Claude иногда возвращает строку "null" вместо null
        raw_time = task_data.get("time")
        time_val = None if (raw_time is None or str(raw_time).lower() in ("null", "none", "")) else raw_time

        # ── Шаг 2: Сэм записывает задачу в базу данных ──
        task_id = add_task(
            user_id=user_id,
            date=task_data["date"],
            text=task_data["text"],
            time=time_val,
        )

        # Сэм даёт короткое подтверждение
        sam_note = await _sam_confirmation(task_data, task_id)

        # ── Шаг 3: возвращаем результат Алисе, чтобы она ответила пользователю ──
        messages_with_result = history[-MAX_HISTORY:] + [
            # Ответ Алисы с вызовом инструмента
            {"role": "assistant", "content": response.content},
            # Результат выполнения инструмента
            {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_block.id,
                    "content": f"Задача успешно записана, id={task_id}.",
                }],
            },
        ]

        final = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=system,
            messages=messages_with_result,
            tools=[CREATE_TASK_TOOL],
        )
        alice_reply = next((b.text for b in final.content if b.type == "text"), "Записала!")

    else:
        # Обычный ответ без инструментов
        alice_reply = next((b.text for b in response.content if b.type == "text"), "")

    # Сохраняем в историю только текстовый обмен (без технических деталей tool_use)
    history.append({"role": "assistant", "content": alice_reply})
    conversation_history[user_id] = history[-MAX_HISTORY:]

    # Финальный ответ: текст Алисы + подтверждение Сэма (если было)
    if sam_note:
        return f"{alice_reply}\n\n✅ *Сэм:* {sam_note}"
    return alice_reply


async def _sam_confirmation(task_data: dict, task_id: int) -> str:
    """Сэм кратко подтверждает запись задачи."""
    date_str = task_data["date"]
    time_str = task_data.get("time") or ""
    text = task_data["text"]

    # Форматируем дату для красивого вывода
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        date_pretty = d.strftime("%d.%m.%Y")
    except ValueError:
        date_pretty = date_str

    when = f"{date_pretty}" + (f" в {time_str}" if time_str else "")
    prompt = f'Задача «{text}» записана на {when} (id={task_id}). Подтверди одним предложением.'

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=80,
        system=SAM_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return next((b.text for b in response.content if b.type == "text"), "Записал!")
