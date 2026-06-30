import asyncio
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Optional
from anthropic import AsyncAnthropic
from duckduckgo_search import DDGS

MSK = timezone(timedelta(hours=3))

def now_msk() -> datetime:
    return datetime.now(tz=MSK)

client = AsyncAnthropic()

# pending futures: hq_message_id → Future[str]
# заполняется в ask_mary_for_schedule, разрешается в miss_ives/bot.py
_pending_mary: dict[int, asyncio.Future] = {}

HQ_CHAT_ID: int = 0          # устанавливается из main.py
MARY_BOT_ID: int = 0         # устанавливается из main.py
MARY_BOT_USERNAME: str = ""  # устанавливается из main.py


async def ask_mary_for_schedule(bot, user_id: int, owner_name: str = "") -> str:
    """Запрашивает расписание у Мери через текстовый запрос в HQ.
    Сначала пишет натуральный запрос для отображения, затем команду для доставки."""
    if not HQ_CHAT_ID:
        return "Расписание недоступно."
    try:
        name_display = owner_name or f"#{user_id}"
        # Натуральное сообщение для отображения в Штабе
        await bot.send_message(
            chat_id=HQ_CHAT_ID,
            text=f"Мери, расскажи о планах {name_display} на ближайшие дни? 📅",
        )
        # Команда с [user:X] — Мери распознаёт по sender_id + шаблону в _handle_hq
        msg = await bot.send_message(
            chat_id=HQ_CHAT_ID,
            text=f"Мери, расписание [user:{user_id}]",
        )
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        _pending_mary[msg.message_id] = future
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=10.0)
        except asyncio.TimeoutError:
            return "расписание временно недоступно"
        finally:
            _pending_mary.pop(msg.message_id, None)
    except Exception as e:
        return f"Ошибка запроса расписания: {e}"


def resolve_mary_response(reply_to_id: int, text: str):
    """Вызывается из bot.py когда Мери отвечает в HQ на наше сообщение."""
    future = _pending_mary.pop(reply_to_id, None)
    if future and not future.done():
        future.set_result(text)


async def process_direct(text: str, owner_name: str = "", tasks_context: str = "") -> str:
    """Прямой разговор с владельцем (Мисс Айвз отвечает как секретарь)."""
    now = now_msk()
    who = f" Твоего начальника зовут {owner_name}." if owner_name else ""
    schedule_block = (
        f"\n\nРасписание {owner_name} на ближайшие дни:\n{tasks_context}"
        if tasks_context else ""
    )
    system = (
        f"Ты — Мисс Айвз, личный секретарь.{who}{schedule_block}\n\n"
        f"Сейчас {now.strftime('%H:%M %d.%m.%Y')} МСК.\n"
        "Ты ведёшь деловую переписку от лица начальника через Telegram Business "
        "и помогаешь ему по секретарским вопросам.\n"
        "Отвечай вежливо, по делу, коротко (1-3 предложения), на русском.\n"
        "Обращайся к начальнику по имени, если оно известно.\n"
        "Смайлики используй очень редко — почти никогда, максимум один и только когда это правда уместно.\n"
        "Не упоминай, что ты ИИ или бот."
    )
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": text}],
    )
    return next((b.text for b in response.content if b.type == "text"), "")


def _web_search(query: str) -> str:
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=3))
        if not results:
            return "Ничего не найдено."
        return "\n\n".join(f"{r['title']}\n{r['href']}" for r in results)
    except Exception as e:
        return f"Ошибка поиска: {e}"


_BIZ_TOOLS = [
    {
        "name": "web_search",
        "description": "Найти информацию в интернете: адреса, ссылки на сайты, места, контакты и т.п.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Поисковый запрос"}
            },
            "required": ["query"],
        },
    }
]


async def generate_business_reply(
    owner_name: str,
    sender_name: str,
    conversation: list[dict],
    tasks_context: str = "",
    custom_instruction: str = "",
) -> tuple[str, bool]:
    """Генерирует ответ от имени владельца (owner_name). Возвращает (текст, использовался_ли_поиск)."""
    now = now_msk()
    current_time_str = now.strftime("%H:%M %d.%m.%Y")

    schedule_block = (
        f"\n\nРасписание {owner_name} (используй если спросят о планах):\n{tasks_context}"
        if tasks_context else ""
    )
    custom_block = f"\n\nОсобая инструкция для этого чата: {custom_instruction}" if custom_instruction else ""

    system = (
        f"Ты ведёшь переписку от лица {owner_name}.{schedule_block}{custom_block}\n\n"
        f"Сейчас: {current_time_str} МСК.\n\n"
        f"Правила:\n"
        f"- Пиши как живой человек: коротко, неформально, без пафоса\n"
        f"- 1-2 предложения максимум\n"
        f"- Никогда не упоминай что ты ИИ или бот\n"
        f"- Если просят ссылку или найти место — используй web_search, затем пришли ссылку\n"
        f"- Если поиск не дал точного результата — сгенерируй ссылку Яндекс Карт: https://yandex.ru/maps/?text=ЗАПРОС\n"
        f"- Эмодзи: максимум 1 штука на сообщение\n"
        f"- Если спрашивают о планах — смотри расписание выше\n"
        f"- Отвечай на русском"
    )

    messages = list(conversation)
    used_search = False

    for _ in range(4):
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=system,
            tools=_BIZ_TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), ""), used_search
        if response.stop_reason == "tool_use":
            used_search = True
            tool_results = []
            for block in response.content:
                if block.type == "tool_use" and block.name == "web_search":
                    result = await asyncio.get_running_loop().run_in_executor(
                        None, _web_search, block.input["query"]
                    )
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
        else:
            break

    return next((b.text for b in response.content if b.type == "text"), ""), used_search
