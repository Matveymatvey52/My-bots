import json
import logging
from shared.db import _db as get_connection

logger = logging.getLogger(__name__)


def load_settings(user_id: int) -> dict:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT data FROM user_settings WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
                if not row:
                    return {}
                data = row[0] if isinstance(row, (list, tuple)) else row["data"]
                return data if isinstance(data, dict) else json.loads(data)
    except Exception as e:
        logger.error("load_settings error for %d: %s", user_id, e)
        return {}


def save_settings(user_id: int, updates: dict):
    try:
        current = load_settings(user_id)
        current.update(updates)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO user_settings (user_id, data)
                    VALUES (%s, %s::jsonb)
                    ON CONFLICT (user_id) DO UPDATE SET data = EXCLUDED.data
                    """,
                    (user_id, json.dumps(current, ensure_ascii=False)),
                )
    except Exception as e:
        logger.error("save_settings error for %d: %s", user_id, e)


def is_onboarding_done(user_id: int) -> bool:
    return load_settings(user_id).get("onboarding_done", False)


def get_all_user_ids() -> list:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_id FROM user_settings WHERE data->>'onboarding_done' = 'true'"
                )
                rows = cur.fetchall()
                return [r[0] if isinstance(r, (list, tuple)) else r["user_id"] for r in rows]
    except Exception as e:
        logger.error("get_all_user_ids error: %s", e)
        return []
