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
    """Создаёт таблицу tasks, если её ещё не существует.
    Вызывается один раз при запуске бота."""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,   -- кому принадлежит задача
                date       TEXT    NOT NULL,   -- дата: YYYY-MM-DD
                time       TEXT,               -- время: HH:MM (или NULL, если не указано)
                text       TEXT    NOT NULL,   -- текст задачи
                status     TEXT    DEFAULT 'active',
                created_at TEXT    DEFAULT (datetime('now'))
            )
        """)
        conn.commit()


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
