# db.py — работа с базой данных SQLite.
# SQLite — это файл на диске, никакого отдельного сервера не нужно.
# Таблица tasks хранит все дела всех пользователей.

import sqlite3
from pathlib import Path
from typing import Optional

# Файл базы данных создаётся в папке data/
DB_PATH = Path("data/tasks.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def _conn() -> sqlite3.Connection:
    """Открывает соединение с базой данных."""
    return sqlite3.connect(DB_PATH)


def init_db():
    """Создаёт таблицы, если их ещё не существует.
    Вызывается один раз при запуске бота."""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                date       TEXT    NOT NULL,
                time       TEXT,
                text       TEXT    NOT NULL,
                status     TEXT    DEFAULT 'active',
                created_at TEXT    DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                role       TEXT    NOT NULL,   -- 'user' или 'assistant'
                content    TEXT    NOT NULL,
                created_at TEXT    DEFAULT (datetime('now', 'localtime'))
            )
        """)
        conn.commit()


def save_message(user_id: int, role: str, content: str):
    """Сохраняет одно сообщение диалога в базу."""
    with _conn() as conn:
        conn.execute(
            "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
            (user_id, role, content),
        )
        conn.commit()


def load_history(user_id: int, limit: int = 20) -> list[dict]:
    """Возвращает последние N сообщений диалога (в хронологическом порядке)."""
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


def add_task(user_id: int, date: str, text: str, time: Optional[str] = None) -> int:
    """Добавляет новую задачу. Возвращает id созданной строки."""
    with _conn() as conn:
        cursor = conn.execute(
            "INSERT INTO tasks (user_id, date, time, text) VALUES (?, ?, ?, ?)",
            (user_id, date, time, text),
        )
        conn.commit()
        return cursor.lastrowid


def get_tasks_for_day(user_id: int, date: str) -> list[dict]:
    """Возвращает все активные задачи пользователя на конкретный день."""
    with _conn() as conn:
        conn.row_factory = sqlite3.Row  # обращаться по имени колонки, не по индексу
        rows = conn.execute(
            """SELECT * FROM tasks
               WHERE user_id = ? AND date = ? AND status = 'active'
               ORDER BY time NULLS LAST""",
            (user_id, date),
        ).fetchall()
        return [dict(r) for r in rows]


def get_upcoming_tasks(user_id: int) -> list[dict]:
    """Все будущие задачи пользователя (сегодня и позже)."""
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM tasks
               WHERE user_id = ? AND date >= date('now') AND status = 'active'
               ORDER BY date, time NULLS LAST""",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
