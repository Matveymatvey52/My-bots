import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

DB_PATH = Path("data/tasks.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def init_db():
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id          INTEGER NOT NULL,
                date             TEXT    NOT NULL,
                time             TEXT,
                text             TEXT    NOT NULL,
                status           TEXT    DEFAULT 'active',
                reminder_minutes INTEGER DEFAULT NULL,
                reminder_sent    INTEGER DEFAULT 0,
                created_at       TEXT    DEFAULT (datetime('now'))
            )
        """)
        # Миграция: добавляем колонки если таблица уже существует без них
        for col, definition in [
            ("reminder_minutes", "INTEGER DEFAULT NULL"),
            ("reminder_sent", "INTEGER DEFAULT 0"),
            ("time_notified", "INTEGER DEFAULT 0"),
        ]:
            try:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {definition}")
            except Exception:
                pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                role       TEXT    NOT NULL,
                content    TEXT    NOT NULL,
                created_at TEXT    DEFAULT (datetime('now', 'localtime'))
            )
        """)
        conn.commit()


def clear_history(user_id: int):
    with _conn() as conn:
        conn.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
        conn.commit()


def save_message(user_id: int, role: str, content: str):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
            (user_id, role, content),
        )
        conn.commit()


def load_history(user_id: int, limit: int = 20) -> list[dict]:
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT role, content FROM (
                   SELECT role, content, created_at
                   FROM messages
                   WHERE user_id = ?
                   ORDER BY created_at DESC
                   LIMIT ?
               ) ORDER BY created_at ASC""",
            (user_id, limit),
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in rows]


def add_task(
    user_id: int,
    date: str,
    text: str,
    time: Optional[str] = None,
    reminder_minutes: Optional[int] = None,
) -> int:
    with _conn() as conn:
        cursor = conn.execute(
            "INSERT INTO tasks (user_id, date, time, text, reminder_minutes) VALUES (?, ?, ?, ?, ?)",
            (user_id, date, time, text, reminder_minutes),
        )
        conn.commit()
        return cursor.lastrowid


def get_tasks_for_day(user_id: int, date: str) -> list[dict]:
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM tasks
               WHERE user_id = ? AND date = ? AND status = 'active'
               ORDER BY time NULLS LAST""",
            (user_id, date),
        ).fetchall()
        return [dict(r) for r in rows]


def get_upcoming_tasks(user_id: int) -> list[dict]:
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM tasks
               WHERE user_id = ? AND date >= date('now') AND status = 'active'
               ORDER BY date, time NULLS LAST""",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_tasks_needing_reminder(current_dt: datetime) -> list[dict]:
    """Возвращает задачи, для которых пора отправить напоминание."""
    today = current_dt.strftime("%Y-%m-%d")
    current_time_str = current_dt.strftime("%H:%M")

    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM tasks
               WHERE date = ?
                 AND time IS NOT NULL
                 AND reminder_minutes IS NOT NULL
                 AND reminder_sent = 0
                 AND status = 'active'""",
            (today,),
        ).fetchall()

        result = []
        for row in rows:
            try:
                task_dt = datetime.strptime(f"{row['date']} {row['time']}", "%Y-%m-%d %H:%M")
                reminder_dt = task_dt - timedelta(minutes=int(row["reminder_minutes"]))
                if reminder_dt.strftime("%H:%M") == current_time_str:
                    result.append(dict(row))
            except Exception:
                pass
        return result


def delete_task(user_id: int, task_id: int) -> bool:
    with _conn() as conn:
        cursor = conn.execute(
            "UPDATE tasks SET status = 'deleted' WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def mark_reminder_sent(task_id: int):
    with _conn() as conn:
        conn.execute("UPDATE tasks SET reminder_sent = 1 WHERE id = ?", (task_id,))
        conn.commit()


def get_tasks_due_now(current_dt: datetime) -> list[dict]:
    """Задачи, время которых наступило прямо сейчас (для уведомления в момент события)."""
    today = current_dt.strftime("%Y-%m-%d")
    current_time_str = current_dt.strftime("%H:%M")
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM tasks
               WHERE date = ? AND time = ? AND status = 'active' AND time_notified = 0""",
            (today, current_time_str),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_time_notified(task_id: int):
    with _conn() as conn:
        conn.execute("UPDATE tasks SET time_notified = 1 WHERE id = ?", (task_id,))
        conn.commit()
