import asyncio
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional
from anthropic import AsyncAnthropic
from shared.db import load_history, save_message
from shared.settings import load_settings
from sam.agent import process_with_sam

MSK = timezone(timedelta(hours=3))

def now_msk() -> datetime:
    return datetime.now(tz=MSK)

client = AsyncAnthropic()
MAX_HISTORY = 20

HQ_CHAT_ID: int = 0          # устанавливается из main.py
SAM_BOT_ID: int = 0          # устанавливается из main.py
SAM_BOT = None               # sam_app.bot — для постинга от имени Сэма в Штаб
MISS_IVES_BOT_ID: int = 0


MARY_SYSTEM = """\
Ты — Мери, личный помощник-планировщик пользователя {name}.
Общаешься с пользователем на русском языке, дружелюбно и по-деловому.

Ты передаёшь задания Сэму через contact_sam — он отвечает за все операции с расписанием.
ВАЖНО: В ответах пользователю НИКОГДА не упоминай «Сэма» — говори от первого лица.

Когда нужно что-то сделать с данными, используй contact_sam:
• Упомянуто дело с датой/временем → запиши через contact_sam
• Несколько дел → передай ВСЕ одним сообщением Сэму
• Пользователь спрашивает про дела → запроси через contact_sam
• Пользователь хочет удалить/изменить задачу → передай через contact_sam
• Пользователь хочет изменить настройки → передай через contact_sam

Когда пишешь в contact_sam — указывай чётко:
- Что делать (дата YYYY-MM-DD, время HH:MM если есть, текст задачи)
- reminder_minutes если просят напомнить заранее

После отчёта Сэма — кратко сообщи пользователю от своего имени.
На обычные вопросы отвечай без contact_sam.

Форматирование: никаких markdown-таблиц. Списки дел:
⏰ ЧЧ:ММ — Название

• Название (без времени)

Используй эмодзи 😊 Сегодня: {today} ({weekday}).\
"""

CONTACT_SAM_TOOL = {
    "name": "contact_sam",
    "description": "Передать задание Сэму (менеджеру расписания).",
    "input_schema": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": (
                    "Задание для Сэма. Пиши строго по строкам:\n"
                    "действие: записать / удалить / показать / изменить настройки\n"
                    "дата: YYYY-MM-DD\n"
                    "время: HH:MM (если есть)\n"
                    "задача: текст с подходящим эмодзи\n"
                    "напомнить за: N минут (если просили, иначе не пиши)"
                ),
            }
        },
        "required": ["message"],
    },
}


async def ask_sam(mary_bot, user_id: int, task_description: str) -> str:
    """Прямой вызов Сэма + оба бота пишут в Штаб для живого отображения."""
    if not HQ_CHAT_ID:
        return "HQ не настроен."
    try:
        name = load_settings(user_id).get("name", "") or f"#{user_id}"

        # Полный блок задания виден в Штабе
        hq_note = f"Сэм, задание от {name}:\n{task_description}"
        await mary_bot.send_message(chat_id=HQ_CHAT_ID, text=hq_note)

        # Прямой вызов — никакого Telegram между Мери и Сэмом
        report = await process_with_sam(user_id, task_description)

        # Сэм пишет в Штаб от своего имени
        if SAM_BOT:
            try:
                await SAM_BOT.send_message(chat_id=HQ_CHAT_ID, text=report)
            except Exception as e:
                pass  # не критично, результат уже есть

        return report
    except Exception as e:
        return f"Ошибка при выполнении задания: {e}"


async def process_with_mary(
    user_id: int,
    user_message: str,
    user_name: str,
    bot=None,
    on_sam_message: Optional[Callable] = None,
) -> str:
    """Главная функция: сообщение пользователя → ответ Мери."""
    try:
        return await asyncio.wait_for(
            _process(user_id, user_message, user_name, bot, on_sam_message),
            timeout=60.0,
        )
    except asyncio.TimeoutError:
        return "Извини, что-то подвисло — попробуй написать ещё раз! 😅"
    except Exception as e:
        err = str(e)
        if "529" in err or "overloaded" in err.lower():
            return "Серверы немного перегружены — подожди секунду 🙏"
        if "rate" in err.lower():
            return "Слишком много запросов — попробуй через несколько секунд ⏳"
        return "Что-то пошло не так — попробуй ещё раз 😔"


async def _process(
    user_id: int,
    user_message: str,
    user_name: str,
    bot,
    on_sam_message: Optional[Callable],
) -> str:
    today = now_msk()
    weekdays = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    mary_system = MARY_SYSTEM.format(
        name=user_name,
        today=today.strftime("%d.%m.%Y"),
        weekday=weekdays[today.weekday()],
    )

    history = load_history(user_id, limit=MAX_HISTORY)
    history.append({"role": "user", "content": user_message})

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=mary_system,
        messages=history[-MAX_HISTORY:],
        tools=[CONTACT_SAM_TOOL],
    )

    if response.stop_reason == "tool_use":
        tool_block = next(b for b in response.content if b.type == "tool_use")
        sam_task = tool_block.input["message"]

        if on_sam_message:
            await on_sam_message("⚙️ *Сэм:* Принял, выполняю...")

        # Отправляем Сэму через HQ и ждём ответа
        sam_report = await ask_sam(bot, user_id, sam_task)

        if on_sam_message:
            await on_sam_message(f"📋 *Сэм → Мери:* {sam_report}")

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

        final = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=mary_system,
            messages=messages_with_sam,
        )
        mary_reply = next((b.text for b in final.content if b.type == "text"), "Готово!")
    else:
        mary_reply = next((b.text for b in response.content if b.type == "text"), "")

    save_message(user_id, "user", user_message)
    save_message(user_id, "assistant", mary_reply)
    return mary_reply
