"""
main_mary.py — запускает Мери + Сэм.
Railway сервис 1: python main_mary.py
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

import mary.bot as mary_bot_module
import mary.agent as mary_agent_module
import sam.bot as sam_bot_module

from mary.bot import create_app as create_mary
from sam.bot import create_app as create_sam


async def main():
    init_db()
    logger.info("БД инициализирована (Mary+Sam)")

    mary_app = create_mary()
    sam_app  = create_sam()

    await mary_app.initialize()
    await sam_app.initialize()

    mary_me = await mary_app.bot.get_me()
    sam_me  = await sam_app.bot.get_me()

    # Регистрируем себя в БД — Miss Ives прочтёт наш ID оттуда
    register_bot("mary", mary_me.id)
    register_bot("sam",  sam_me.id)
    logger.info("Мери id=%d  Сэм id=%d", mary_me.id, sam_me.id)

    # Читаем ID Miss Ives из БД (она могла стартануть раньше или позже)
    ives_id = get_bot_id("ives") or 0

    hq_chat_id = get_hq_chat_id() or int(os.environ.get("HQ_CHAT_ID", 0))
    if not hq_chat_id:
        logger.warning("HQ_CHAT_ID не задан. Напиши /sethq в группе «Штаб».")

    # Инжектируем конфиг
    mary_bot_module.HQ_CHAT_ID      = hq_chat_id
    mary_bot_module.SAM_BOT_ID      = sam_me.id
    mary_bot_module.MISS_IVES_BOT_ID = ives_id
    mary_bot_module._my_id          = mary_me.id
    mary_agent_module.HQ_CHAT_ID    = hq_chat_id
    mary_agent_module.SAM_BOT_ID    = sam_me.id
    mary_agent_module.MISS_IVES_BOT_ID = ives_id

    sam_bot_module.HQ_CHAT_ID = hq_chat_id
    sam_bot_module._my_id     = sam_me.id

    await mary_app.start()
    await sam_app.start()

    allowed = ["message", "callback_query", "inline_query"]
    await mary_app.updater.start_polling(drop_pending_updates=True, allowed_updates=allowed)
    await sam_app.updater.start_polling(drop_pending_updates=True, allowed_updates=["message"])

    if hq_chat_id:
        try:
            await mary_app.bot.send_message(chat_id=hq_chat_id, text="☀️ Мери и Сэм онлайн!")
        except Exception:
            pass

    logger.info("Мери и Сэм запущены")

    stop_event = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()

    await mary_app.updater.stop()
    await sam_app.updater.stop()
    await mary_app.stop()
    await sam_app.stop()
    await mary_app.shutdown()
    await sam_app.shutdown()
    logger.info("Мери и Сэм остановлены")


if __name__ == "__main__":
    asyncio.run(main())
