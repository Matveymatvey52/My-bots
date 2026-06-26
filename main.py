"""
main.py — запускает всех трёх ботов в одном event loop.

Порядок старта:
1. Инициализируем все три приложения
2. Узнаём bot_id каждого бота через get_me()
3. Инжектируем ID в модули (чтобы боты узнавали друг друга в HQ)
4. Запускаем polling всех трёх параллельно
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

from shared.db import init_db
from shared.hq import get_hq_chat_id

import mary.bot as mary_bot_module
import mary.agent as mary_agent_module
import sam.bot as sam_bot_module
import miss_ives.bot as ives_bot_module
import miss_ives.agent as ives_agent_module

from mary.bot import create_app as create_mary
from sam.bot import create_app as create_sam
from miss_ives.bot import create_app as create_ives


async def main():
    # Инициализируем БД
    init_db()
    logger.info("БД инициализирована")

    # HQ_CHAT_ID читаем из файла (устанавливается командой /sethq)
    # Если не задан — боты запустятся, но HQ-функции заработают после /sethq
    hq_chat_id = get_hq_chat_id() or int(os.environ.get("HQ_CHAT_ID", 0))

    # Создаём приложения
    mary_app = create_mary()
    sam_app  = create_sam()
    ives_app = create_ives()

    # Инициализируем (без запуска polling)
    await mary_app.initialize()
    await sam_app.initialize()
    await ives_app.initialize()

    # Узнаём ID каждого бота
    mary_me = await mary_app.bot.get_me()
    sam_me  = await sam_app.bot.get_me()
    ives_me = await ives_app.bot.get_me()

    logger.info("Мери id=%d  Сэм id=%d  Мисс Айвз id=%d",
                mary_me.id, sam_me.id, ives_me.id)

    # ── Инжектируем ID и HQ_CHAT_ID в каждый модуль ──────────────────────

    # Мери знает ID Сэма и Мисс Айвз (для HQ роутинга)
    mary_bot_module.HQ_CHAT_ID     = hq_chat_id
    mary_bot_module.SAM_BOT_ID     = sam_me.id
    mary_bot_module.MISS_IVES_BOT_ID = ives_me.id
    mary_bot_module._my_id         = mary_me.id
    mary_agent_module.HQ_CHAT_ID   = hq_chat_id
    mary_agent_module.SAM_BOT_ID   = sam_me.id
    mary_agent_module.MISS_IVES_BOT_ID = ives_me.id

    # Сэм знает свой ID и HQ
    sam_bot_module.HQ_CHAT_ID = hq_chat_id
    sam_bot_module._my_id     = sam_me.id

    # Мисс Айвз знает ID Мери и HQ
    ives_bot_module.HQ_CHAT_ID  = hq_chat_id
    ives_bot_module.MARY_BOT_ID = mary_me.id
    ives_bot_module._my_id      = ives_me.id
    ives_agent_module.HQ_CHAT_ID  = hq_chat_id
    ives_agent_module.MARY_BOT_ID = mary_me.id

    # ── Запускаем polling всех трёх ──────────────────────────────────────

    await mary_app.start()
    await sam_app.start()
    await ives_app.start()

    await mary_app.updater.start_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query", "inline_query", "business_connection", "business_message"])
    await sam_app.updater.start_polling(drop_pending_updates=True, allowed_updates=["message"])
    await ives_app.updater.start_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query", "business_connection", "business_message"])

    # Анонс в HQ что все боты запущены
    try:
        await mary_app.bot.send_message(
            chat_id=hq_chat_id,
            text="✅ Все системы запущены!\nМери, Сэм и Мисс Айвз онлайн 🚀",
        )
    except Exception:
        pass

    logger.info("Все три бота запущены и слушают обновления")

    # Держим event loop живым до сигнала остановки
    stop_event = asyncio.Event()

    def _on_signal():
        logger.info("Получен сигнал завершения")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _on_signal)

    await stop_event.wait()

    # Graceful shutdown
    logger.info("Останавливаем ботов...")
    await mary_app.updater.stop()
    await sam_app.updater.stop()
    await ives_app.updater.stop()
    await mary_app.stop()
    await sam_app.stop()
    await ives_app.stop()
    await mary_app.shutdown()
    await sam_app.shutdown()
    await ives_app.shutdown()
    logger.info("Все боты остановлены")


if __name__ == "__main__":
    asyncio.run(main())
