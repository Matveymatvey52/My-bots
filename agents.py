# agents.py — Алиса и Сэм.
#
# АЛИСА — администратор. Общается с пользователем.
#   Сама НЕ трогает базу данных — только пишет задание Сэму.
#   Инструмент: contact_sam(message) — отправить задание Сэму.
#
# СЭМ — менеджер расписания. Получает задание от Алисы.
#   Выполняет его с помощью инструментов (create_task, update_settings).
#   Возвращает письменный отчёт → Алиса передаёт его пользователю.
#
# Схема:
#   Пользователь → [Алиса] → contact_sam → [Сэм] → инструменты → отчёт → [Алиса] → Пользователь

import asyncio
import os
from datetime import datetime
from typing import Optional
from anthropic import AsyncAnthropic
from db import add_task, load_history, save_message
from settings import save_settings

# Клиент читает ANTHROPIC_API_KEY из переменных окружения
client = AsyncAnthropic()

MAX_HISTORY = 20


# ══════════════════════════════════════════════════
#  СИСТЕМНЫЕ ПРОМПТЫ
# ══════════════════════════════════════════════════

ALICE_SYSTEM = """\
Ты — Алиса, администратор-помощник в Telegram-боте.
Ты общаешься с пользователем по имени {name} на русском языке, дружелюбно и по-деловому.

У тебя есть коллега Сэм — он отвечает за расписание и базу данных.
Ты НЕ можешь сама записывать задачи или менять настройки — только Сэм.

Когда нужно что-то сделать с данными:
• Пользователь упоминает дело с датой/временем → напиши Сэму через contact_sam
• Пользователь хочет изменить настройки (время сообщений, вкл/выкл вечернее) → напиши Сэму через contact_sam

Когда пишешь Сэму — формулируй чётко:
- Что сделать
- Дата в формате YYYY-MM-DD
- Время в формате HH:MM (если есть)
- Что именно изменить в настройках (если нужно)

После получения отчёта от Сэма — кратко сообщи пользователю что сделано.
На обычные вопросы отвечай сам(а), без Сэма.

Сегодня: {today} ({weekday}).\
"""

SAM_SYSTEM = """\
Ты — Сэм, менеджер расписания. Ты получаешь задания от Алисы и выполняешь их.

Порядок работы:
1. Прочитай задание от Алисы
2. Используй нужный инструмент (create_task или update_settings)
3. После выполнения дай чёткий отчёт на русском языке

Отчёт должен содержать: что именно сделал, детали (дата, время, текст задачи или новые настройки).
Пиши кратко, по-деловому. Одно-два предложения.\
"""


# ══════════════════════════════════════════════════
#  ИНСТРУМЕНТЫ
# ══════════════════════════════════════════════════

# Инструмент Алисы — связаться с Сэмом
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

SAM_TOOLS = [CREATE_TASK_TOOL, UPDATE_SETTINGS_TOOL]


# ══════════════════════════════════════════════════
#  ОСНОВНЫЕ ФУНКЦИИ
# ══════════════════════════════════════════════════

async def process_with_alice(user_id: int, user_message: str, user_name: str, on_sam_message=None) -> str:
    """Обёртка с таймаутом — если что-то зависло, вернёт ошибку вместо бесконечного ожидания."""
    try:
        return await asyncio.wait_for(
            _process_with_alice(user_id, user_message, user_name, on_sam_message),
            timeout=60.0,
        )
    except asyncio.TimeoutError:
        return "Извини, что-то подвисло — попробуй написать ещё раз! 😅"


async def _process_with_alice(user_id: int, user_message: str, user_name: str, on_sam_message=None) -> str:
    """
    Главная функция: принимает сообщение пользователя, отдаёт Алисе.
    Если Алиса решает делегировать Сэму — запускает process_with_sam.
    Возвращает финальный текст для отправки пользователю.
    """

    today = datetime.now()
    weekdays = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    alice_system = ALICE_SYSTEM.format(
        name=user_name,
        today=today.strftime("%d.%m.%Y"),
        weekday=weekdays[today.weekday()],
    )

    # История диалога Алисы с этим пользователем (из БД)
    history = load_history(user_id, limit=MAX_HISTORY)
    history.append({"role": "user", "content": user_message})

    # ── Запрос к Алисе ──
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=alice_system,
        messages=history[-MAX_HISTORY:],
        tools=[CONTACT_SAM_TOOL],
    )

    if response.stop_reason == "tool_use":
        # Алиса хочет связаться с Сэмом
        tool_block = next(b for b in response.content if b.type == "tool_use")
        sam_task = tool_block.input["message"]

        # Сэм принял задание
        if on_sam_message:
            await on_sam_message("⚙️ *Сэм:* Принял, выполняю...")

        # ── Сэм выполняет задание и даёт отчёт ──
        sam_report = await process_with_sam(user_id, sam_task)

        # Сэм отчитывается
        if on_sam_message:
            await on_sam_message(f"📋 *Сэм → Алисе:* {sam_report}")

        # Возвращаем отчёт Сэма обратно Алисе
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

        # Алиса формулирует финальный ответ пользователю на основе отчёта Сэма
        # tools не передаём — она уже получила результат, просто отвечает пользователю
        final = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=alice_system,
            messages=messages_with_sam,
        )
        alice_reply = next((b.text for b in final.content if b.type == "text"), "Готово!")

    else:
        # Алиса отвечает напрямую (без Сэма)
        alice_reply = next((b.text for b in response.content if b.type == "text"), "")

    # Сохраняем оба сообщения в БД
    save_message(user_id, "user", user_message)
    save_message(user_id, "assistant", alice_reply)

    return alice_reply


async def process_with_sam(user_id: int, alice_message: str) -> str:
    """
    Сэм получает задание от Алисы, выполняет его с помощью инструментов
    и возвращает текстовый отчёт.
    """

    # ── Сэм читает задание и решает что делать ──
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SAM_SYSTEM,
        messages=[{"role": "user", "content": f"Задание от Алисы:\n{alice_message}"}],
        tools=SAM_TOOLS,
    )

    if response.stop_reason == "tool_use":
        tool_block = next(b for b in response.content if b.type == "tool_use")
        tool_name = tool_block.name
        tool_data = tool_block.input

        # ── Выполняем инструмент ──
        if tool_name == "create_task":
            raw_time = tool_data.get("time")
            time_val = None if (raw_time is None or str(raw_time).lower() in ("null", "none", "")) else raw_time
            task_id = add_task(
                user_id=user_id,
                date=tool_data["date"],
                text=tool_data["text"],
                time=time_val,
            )
            tool_result = f"Задача записана в базу данных, id={task_id}."

        elif tool_name == "update_settings":
            updates = {}
            if tool_data.get("morning_time") not in (None, "null", ""):
                updates["morning_time"] = tool_data["morning_time"]
            if tool_data.get("evening_time") not in (None, "null", ""):
                updates["evening_time"] = tool_data["evening_time"]
            if tool_data.get("evening_enabled") is not None:
                updates["evening_enabled"] = tool_data["evening_enabled"]
            if updates:
                save_settings(user_id, updates)
            tool_result = f"Настройки обновлены: {updates}."

        else:
            tool_result = "Неизвестный инструмент."

        # ── Сэм формулирует отчёт после выполнения ──
        sam_messages = [
            {"role": "user", "content": f"Задание от Алисы:\n{alice_message}"},
            {"role": "assistant", "content": response.content},
            {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_block.id,
                    "content": tool_result,
                }],
            },
        ]

        # tools не передаём — Сэм уже выполнил инструмент, просто пишет отчёт
        report_response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            system=SAM_SYSTEM,
            messages=sam_messages,
        )
        return next((b.text for b in report_response.content if b.type == "text"), "Выполнено.")

    else:
        # Сэм ответил текстом без инструмента (например, уточняет)
        return next((b.text for b in response.content if b.type == "text"), "Выполнено.")
