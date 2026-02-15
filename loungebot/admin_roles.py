import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DATA_FILE = Path("data/admin_roles.json")


@dataclass(frozen=True)
class AdminRecord:
    username: str
    user_id: int | None
    first_name: str | None
    last_name: str | None


def _load() -> dict[str, Any]:
    if not DATA_FILE.exists():
        return {"admins": {}}
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"admins": {}}


def _save(data: dict[str, Any]) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DATA_FILE)


def normalize_username(value: str) -> str:
    s = value.strip()
    if s.startswith("@"):  # allow input with @
        s = s[1:]
    return s.lower()


def add_admin_by_username(username: str) -> None:
    u = normalize_username(username)
    data = _load()
    admins = data.setdefault("admins", {})
    admins.setdefault(u, {"user_id": None, "first_name": None, "last_name": None})
    _save(data)


def remove_admin_by_username(username: str) -> None:
    u = normalize_username(username)
    data = _load()
    admins = data.setdefault("admins", {})
    if u in admins:
        admins.pop(u)
        _save(data)


def list_admins() -> list[AdminRecord]:
    data = _load()
    admins: dict[str, Any] = data.get("admins", {})
    out: list[AdminRecord] = []
    for username, rec in admins.items():
        out.append(
            AdminRecord(
                username=username,
                user_id=rec.get("user_id"),
                first_name=rec.get("first_name"),
                last_name=rec.get("last_name"),
            )
        )
    out.sort(key=lambda r: r.username)
    return out


def admin_user_ids() -> set[int]:
    """
    Returns admin user_ids that we already know (synced from Telegram updates).
    """
    ids: set[int] = set()
    for rec in list_admins():
        if rec.user_id is None:
            continue
        try:
            ids.add(int(rec.user_id))
        except Exception:
            continue
    return ids


def sync_from_user(user_id: int, username: str | None, first_name: str | None, last_name: str | None) -> None:
    if not username:
        return
    u = normalize_username(username)
    data = _load()
    admins = data.setdefault("admins", {})
    rec = admins.get(u)
    if rec is None:
        return
    rec["user_id"] = user_id
    rec["first_name"] = first_name
    rec["last_name"] = last_name
    _save(data)


def is_admin_user(user_id: int, username: str | None) -> bool:
    if username:
        u = normalize_username(username)
        admins = _load().get("admins", {})
        return u in admins
    # Fallback by user_id if we already synced.
    admins = _load().get("admins", {})
    return any((rec.get("user_id") == user_id) for rec in admins.values())
