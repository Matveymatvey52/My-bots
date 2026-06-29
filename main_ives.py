"""
main_ives.py — запускает Мисс Айвз.
Railway сервис 2: python main_ives.py
"""

import asyncio
import logging
import os
import signal

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
)
logger = logging.getLogger(__name__)

from shared.db import init_db, register_bot, get_bot_id
from shared.hq import get_hq_chat_id

import miss_ives.bot as ives_bot_module
import miss_ives.agent as ives_agent_module

from miss_ives.bot import create_app as create_ives


async def _wait_for_mary_id(timeout: float = 60.0) -> int:
    """Ждёт пока Mary зарегистрирует свой ID в БД (до 60 сек)."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        mid = get_bot_id("mary")
        if mid:
            return mid
        logger.info("Жду ID Мери из БД...")
        await asyncio.sleep(3)
    logger.warning("Мери не зарегистрировалась за %d сек — продолжаю без её ID", int(timeout))
    return 0


async def main():
    init_db()
    logger.info("БД инициализирована (Miss Ives)")

    ives_app = create_ives()
    await ives_app.initialize()

    ives_me = await ives_app.bot.get_me()
    register_bot("ives", ives_me.id)
    logger.info("Мисс Айвз id=%d", ives_me.id)

    # Ждём ID Мери из БД (она может стартовать позже)
    mary_id = await _wait_for_mary_id(timeout=60.0)

    # Получаем username Мери для /schedule@MaryBot команды
    mary_username = ""
    if mary_id:
        try:
            mary_chat = await ives_app.bot.get_chat(mary_id)
            mary_username = mary_chat.username or ""
            logger.info("Мери username: @%s", mary_username)
        except Exception as e:
            logger.warning("Не смог получить username Мери: %s", e)

    hq_chat_id = get_hq_chat_id() or int(os.environ.get("HQ_CHAT_ID", 0))
    if not hq_chat_id:
        logger.warning("HQ_CHAT_ID не задан. Напиши /sethq в группе «Штаб».")

    # Инжектируем конфиг
    ives_bot_module.HQ_CHAT_ID  = hq_chat_id
    ives_bot_module.MARY_BOT_ID = mary_id
    ives_bot_module._my_id      = ives_me.id
    ives_agent_module.HQ_CHAT_ID       = hq_chat_id
    ives_agent_module.MARY_BOT_ID      = mary_id
    ives_agent_module.MARY_BOT_USERNAME = mary_username

    await ives_app.start()

    allowed = ["message", "callback_query", "business_connection", "business_message"]
    await ives_app.updater.start_polling(drop_pending_updates=True, allowed_updates=allowed)

    if hq_chat_id:
        try:
            await ives_app.bot.send_message(chat_id=hq_chat_id, text="🌙 Мисс Айвз онлайн!")
        except Exception:
            pass

    logger.info("Мисс Айвз запущена")

    stop_event = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()

    await ives_app.updater.stop()
    await ives_app.stop()
    await ives_app.shutdown()
    logger.info("Мисс Айвз остановлена")


if __name__ == "__main__":
    asyncio.run(main())
