import json
from pathlib import Path
from typing import Any

DATA_FILE = Path("data/guest_cards.json")


def _load_data() -> dict[str, Any]:
    if not DATA_FILE.exists():
        return {}

    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_data(data: dict[str, Any]) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def is_registered(user_id: int) -> bool:
    data = _load_data()
    user = data.get(str(user_id), {})
    return bool(user.get("registered", False))


def register_card(user_id: int) -> None:
    data = _load_data()
    data[str(user_id)] = {"registered": True}
    _save_data(data)
