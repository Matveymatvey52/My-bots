"""HQ_CHAT_ID хранится в PostgreSQL — доступен любому процессу."""
from shared.db import get_config, set_config

_cached: int = 0


def get_hq_chat_id() -> int:
    global _cached
    if _cached:
        return _cached
    val = get_config("hq_chat_id")
    if val:
        _cached = int(val)
    return _cached


def set_hq_chat_id(chat_id: int):
    global _cached
    _cached = chat_id
    set_config("hq_chat_id", str(chat_id))
