import os
import psycopg2
import psycopg2.pool
from contextlib import contextmanager
from datetime import datetime, timedelta
from psycopg2.extras import RealDictCursor
from typing import Optional

_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(1, 20, os.environ["DATABASE_URL"])
    return _pool


@contextmanager
def _db():
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def init_db():
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_registry (
                    name    TEXT PRIMARY KEY,
                    bot_id  BIGINT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_config (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id               SERIAL PRIMARY KEY,
                    user_id          BIGINT NOT NULL,
                    date             TEXT NOT NULL,
                    time             TEXT,
                    text             TEXT NOT NULL,
                    status           TEXT DEFAULT 'active',
                    reminder_minutes INTEGER DEFAULT NULL,
                    reminder_sent    SMALLINT DEFAULT 0,
                    time_notified    SMALLINT DEFAULT 0,
                    created_at       TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS business_connections (
                    connection_id TEXT PRIMARY KEY,
                    user_id       BIGINT NOT NULL,
                    can_reply     SMALLINT DEFAULT 0
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sent_summaries (
                    user_id BIGINT NOT NULL,
                    date    TEXT NOT NULL,
                    type    TEXT NOT NULL,
                    PRIMARY KEY (user_id, date, type)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id         SERIAL PRIMARY KEY,
                    user_id    BIGINT NOT NULL,
                    role       TEXT NOT NULL,
                    content    TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS biz_messages (
                    id         SERIAL PRIMARY KEY,
                    conn_id    TEXT NOT NULL,
                    chat_id    BIGINT NOT NULL,
                    role       TEXT NOT NULL,
                    content    TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS biz_chat_settings (
                    conn_id            TEXT NOT NULL,
                    chat_id            BIGINT NOT NULL,
                    muted              SMALLINT DEFAULT 0,
                    mute_until         TEXT DEFAULT NULL,
                    custom_instruction TEXT DEFAULT '',
                    PRIMARY KEY (conn_id, chat_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS photos (
                    id         SERIAL PRIMARY KEY,
                    user_id    BIGINT NOT NULL,
                    file_id    TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)


# ── История диалога ────────────────────────────

def clear_history(user_id: int):
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM messages WHERE user_id = %s", (user_id,))


def save_message(user_id: int, role: str, content: str):
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO messages (user_id, role, content) VALUES (%s, %s, %s)",
                (user_id, role, content),
            )


def load_history(user_id: int, limit: int = 20) -> list[dict]:
    with _db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT role, content FROM (
                       SELECT role, content, created_at
                       FROM messages
                       WHERE user_id = %s
                       ORDER BY created_at DESC
                       LIMIT %s
                   ) sub ORDER BY created_at ASC""",
                (user_id, limit),
            )
            return [dict(r) for r in cur.fetchall()]


# ── Задачи ────────────────────────────────────

def add_task(
    user_id: int,
    date: str,
    text: str,
    time: Optional[str] = None,
    reminder_minutes: Optional[int] = None,
) -> int:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tasks (user_id, date, time, text, reminder_minutes) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (user_id, date, time, text, reminder_minutes),
            )
            return cur.fetchone()[0]


def get_tasks_for_day(user_id: int, date: str) -> list[dict]:
    with _db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT * FROM tasks
                   WHERE user_id = %s AND date = %s AND status = 'active'
                   ORDER BY time NULLS LAST""",
                (user_id, date),
            )
            return [dict(r) for r in cur.fetchall()]


def get_upcoming_tasks(user_id: int) -> list[dict]:
    today = datetime.now().strftime("%Y-%m-%d")
    with _db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT * FROM tasks
                   WHERE user_id = %s AND date >= %s AND status = 'active'
                   ORDER BY date, time NULLS LAST""",
                (user_id, today),
            )
            return [dict(r) for r in cur.fetchall()]


def get_tasks_needing_reminder(current_dt: datetime) -> list[dict]:
    today = current_dt.strftime("%Y-%m-%d")
    current_time_str = current_dt.strftime("%H:%M")
    with _db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT * FROM tasks
                   WHERE date = %s
                     AND time IS NOT NULL
                     AND reminder_minutes IS NOT NULL
                     AND reminder_sent = 0
                     AND status = 'active'""",
                (today,),
            )
            rows = [dict(r) for r in cur.fetchall()]
    result = []
    for row in rows:
        try:
            task_dt = datetime.strptime(f"{row['date']} {row['time']}", "%Y-%m-%d %H:%M")
            reminder_dt = task_dt - timedelta(minutes=int(row["reminder_minutes"]))
            if reminder_dt.strftime("%H:%M") == current_time_str:
                result.append(row)
        except Exception:
            pass
    return result


def get_tasks_due_now(current_dt: datetime) -> list[dict]:
    today = current_dt.strftime("%Y-%m-%d")
    current_time_str = current_dt.strftime("%H:%M")
    with _db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT * FROM tasks
                   WHERE date = %s AND time = %s AND status = 'active' AND time_notified = 0""",
                (today, current_time_str),
            )
            return [dict(r) for r in cur.fetchall()]


def delete_task(user_id: int, task_id: int) -> bool:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET status = 'deleted' WHERE id = %s AND user_id = %s",
                (task_id, user_id),
            )
            return cur.rowcount > 0


def mark_reminder_sent(task_id: int):
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE tasks SET reminder_sent = 1 WHERE id = %s", (task_id,))


def mark_time_notified(task_id: int):
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE tasks SET time_notified = 1 WHERE id = %s", (task_id,))


# ── Бизнес-подключения ─────────────────────────

def save_business_connection(connection_id: str, user_id: int, can_reply: bool):
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO business_connections (connection_id, user_id, can_reply) VALUES (%s, %s, %s)
                   ON CONFLICT (connection_id) DO UPDATE SET user_id = EXCLUDED.user_id, can_reply = EXCLUDED.can_reply""",
                (connection_id, user_id, int(can_reply)),
            )


def delete_business_connection(connection_id: str):
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM business_connections WHERE connection_id = %s", (connection_id,))


def get_user_by_connection(connection_id: str) -> Optional[dict]:
    with _db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT user_id, can_reply FROM business_connections WHERE connection_id = %s",
                (connection_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def get_connection_for_user(user_id: int) -> Optional[dict]:
    with _db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT connection_id, user_id, can_reply FROM business_connections WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


# ── Сводки ────────────────────────────────────

def claim_summary_send(user_id: int, date: str, summary_type: str) -> bool:
    try:
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO sent_summaries (user_id, date, type) VALUES (%s, %s, %s)",
                    (user_id, date, summary_type),
                )
        return True
    except Exception:
        return False


# ── Статистика ────────────────────────────────

def get_bot_stats() -> dict:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(DISTINCT user_id) FROM messages")
            total_users = cur.fetchone()[0]

            cur.execute("SELECT COUNT(DISTINCT user_id) FROM messages WHERE created_at >= NOW() - INTERVAL '7 days'")
            active_7d = cur.fetchone()[0]

            cur.execute("SELECT COUNT(DISTINCT user_id) FROM messages WHERE created_at >= NOW() - INTERVAL '30 days'")
            active_30d = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM messages WHERE role = 'user'")
            total_messages = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM tasks WHERE status = 'active'")
            total_tasks = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM tasks WHERE created_at >= NOW() - INTERVAL '30 days'")
            tasks_30d = cur.fetchone()[0]

            cur.execute(
                "SELECT user_id, COUNT(*) as cnt FROM messages WHERE role = 'user' GROUP BY user_id ORDER BY cnt DESC"
            )
            per_user = cur.fetchall()

            return {
                "total_users": total_users,
                "active_7d": active_7d,
                "active_30d": active_30d,
                "total_messages": total_messages,
                "total_tasks": total_tasks,
                "tasks_30d": tasks_30d,
                "per_user": [(r[0], r[1]) for r in per_user],
            }


# ── Фото ──────────────────────────────────────

def save_photo(user_id: int, file_id: str) -> int:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO photos (user_id, file_id) VALUES (%s, %s) RETURNING id",
                (user_id, file_id),
            )
            return cur.fetchone()[0]


def get_photo(photo_id: int) -> Optional[dict]:
    with _db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT id, user_id, file_id FROM photos WHERE id = %s", (photo_id,))
            row = cur.fetchone()
            return dict(row) if row else None


# ── Бизнес-чат настройки ──────────────────────

def get_biz_chat_settings(conn_id: str, chat_id: int) -> dict:
    with _db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT muted, mute_until, custom_instruction FROM biz_chat_settings WHERE conn_id = %s AND chat_id = %s",
                (conn_id, chat_id),
            )
            row = cur.fetchone()
            if row:
                return dict(row)
            return {"muted": 0, "mute_until": None, "custom_instruction": ""}


def get_biz_chat_muted(conn_id: str, chat_id: int) -> bool:
    s = get_biz_chat_settings(conn_id, chat_id)
    if s["muted"] and s["mute_until"]:
        from datetime import timezone
        MSK = timezone(timedelta(hours=3))
        if datetime.now(tz=MSK).isoformat() >= s["mute_until"]:
            set_biz_chat_settings(conn_id, chat_id, muted=False, mute_until=None)
            return False
    return bool(s["muted"])


def set_biz_chat_settings(
    conn_id: str,
    chat_id: int,
    muted: Optional[bool] = None,
    mute_until: Optional[str] = None,
    custom_instruction: Optional[str] = None,
):
    current = get_biz_chat_settings(conn_id, chat_id)
    new_muted = int(muted) if muted is not None else current["muted"]
    new_until = mute_until if mute_until is not None else current["mute_until"]
    new_instr = custom_instruction if custom_instruction is not None else current["custom_instruction"]
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO biz_chat_settings (conn_id, chat_id, muted, mute_until, custom_instruction)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (conn_id, chat_id) DO UPDATE
                   SET muted = EXCLUDED.muted, mute_until = EXCLUDED.mute_until,
                       custom_instruction = EXCLUDED.custom_instruction""",
                (conn_id, chat_id, new_muted, new_until, new_instr),
            )


def set_biz_chat_muted(conn_id: str, chat_id: int, muted: bool):
    set_biz_chat_settings(conn_id, chat_id, muted=muted, mute_until=None)


def save_biz_message(conn_id: str, chat_id: int, role: str, content: str):
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO biz_messages (conn_id, chat_id, role, content) VALUES (%s, %s, %s, %s)",
                (conn_id, chat_id, role, content),
            )


def register_bot(name: str, bot_id: int):
    """Сохраняет ID бота в БД при старте."""
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO bot_registry (name, bot_id) VALUES (%s, %s) ON CONFLICT (name) DO UPDATE SET bot_id = EXCLUDED.bot_id",
                (name, bot_id),
            )


def get_bot_id(name: str) -> Optional[int]:
    """Читает ID бота по имени."""
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT bot_id FROM bot_registry WHERE name = %s", (name,))
            row = cur.fetchone()
            return row[0] if row else None


def set_config(key: str, value: str):
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO bot_config (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (key, value),
            )


def get_config(key: str) -> Optional[str]:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM bot_config WHERE key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row else None


def load_biz_history(conn_id: str, chat_id: int, limit: int = 30) -> list[dict]:
    with _db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT role, content FROM (
                       SELECT role, content, created_at FROM biz_messages
                       WHERE conn_id = %s AND chat_id = %s
                       ORDER BY created_at DESC LIMIT %s
                   ) sub ORDER BY created_at ASC""",
                (conn_id, chat_id, limit),
            )
            return [{"role": r["role"], "content": r["content"]} for r in cur.fetchall()]
