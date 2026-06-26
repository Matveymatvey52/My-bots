import json
from pathlib import Path

SETTINGS_DIR = Path("data/settings")
SETTINGS_DIR.mkdir(parents=True, exist_ok=True)


def _path(user_id: int) -> Path:
    return SETTINGS_DIR / f"{user_id}.json"


def load_settings(user_id: int) -> dict:
    path = _path(user_id)
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_settings(user_id: int, updates: dict):
    current = load_settings(user_id)
    current.update(updates)
    with open(_path(user_id), "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=2)


def is_onboarding_done(user_id: int) -> bool:
    return load_settings(user_id).get("onboarding_done", False)


def get_all_user_ids() -> list[int]:
    return [int(f.stem) for f in SETTINGS_DIR.glob("*.json")]
