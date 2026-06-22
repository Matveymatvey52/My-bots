# settings.py — сохранение настроек каждого пользователя в отдельный JSON-файл.
# Один файл = один пользователь (имя файла = его Telegram user_id).

import json
from pathlib import Path

# Папка создаётся автоматически при первом запуске
SETTINGS_DIR = Path("data/settings")
SETTINGS_DIR.mkdir(parents=True, exist_ok=True)


def _path(user_id: int) -> Path:
    """Путь к файлу настроек конкретного пользователя."""
    return SETTINGS_DIR / f"{user_id}.json"


def load_settings(user_id: int) -> dict:
    """Читает настройки. Если файла нет — возвращает пустой словарь."""
    path = _path(user_id)
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_settings(user_id: int, updates: dict):
    """Обновляет только переданные поля, остальное не трогает."""
    current = load_settings(user_id)
    current.update(updates)
    with open(_path(user_id), "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=2)


def is_onboarding_done(user_id: int) -> bool:
    """Прошёл ли пользователь первоначальную настройку?"""
    return load_settings(user_id).get("onboarding_done", False)


def get_all_user_ids() -> list[int]:
    """Возвращает список всех пользователей, у которых есть настройки."""
    return [int(f.stem) for f in SETTINGS_DIR.glob("*.json")]
