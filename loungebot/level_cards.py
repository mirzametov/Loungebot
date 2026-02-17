import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import random

# Level rules (visits thresholds).
TIERS: list[tuple[int, str, int]] = [
    (1, "IRON⚙️", 3),
    (5, "BRONZE🥉", 5),
    (15, "SILVER🥈", 7),
    (35, "GOLD🥇", 10),
]

DATA_FILE = Path("data/level_cards.json")


@dataclass(frozen=True)
class LevelCard:
    card_number: str
    user_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    level: str
    discount: int
    visits: int
    staff_gold: bool


def _load() -> dict[str, Any]:
    if not DATA_FILE.exists():
        return {"next_number": 4821, "by_number": {}, "by_user": {}}
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"next_number": 4821, "by_number": {}, "by_user": {}}
    if not isinstance(data, dict):
        return {"next_number": 4821, "by_number": {}, "by_user": {}}
    data.setdefault("next_number", 4821)
    data.setdefault("by_number", {})
    data.setdefault("by_user", {})
    return data


def _save(data: dict[str, Any]) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DATA_FILE)


def _to_card(card_number: str, rec: dict[str, Any]) -> LevelCard:
    return LevelCard(
        card_number=str(card_number),
        user_id=int(rec.get("user_id", 0) or 0),
        username=rec.get("username"),
        first_name=rec.get("first_name"),
        last_name=rec.get("last_name"),
        level=str(rec.get("level") or "IRON⚙️"),
        discount=int(rec.get("discount", 3) or 3),
        visits=int(rec.get("visits", 0) or 0),
        staff_gold=bool(rec.get("staff_gold", False)),
    )


def tier_for_visits(visits: int) -> tuple[str, int]:
    v = int(visits or 0)
    if v <= 0:
        return ("-", 0)
    level = TIERS[0][1]
    discount = TIERS[0][2]
    for threshold, lvl, disc in TIERS:
        if v >= threshold:
            level = lvl
            discount = disc
        else:
            break
    return (level, discount)


def next_tier_info(visits: int) -> tuple[str, int] | None:
    """
    Returns (next_level_label, remaining_visits) or None if already max.
    """
    v = int(visits or 0)
    for threshold, lvl, _disc in TIERS:
        if v < threshold:
            return (lvl, threshold - v)
    return None


def _recalc(rec: dict[str, Any]) -> None:
    if bool(rec.get("staff_gold", False)):
        # Staff cards are not part of standard tiers, and do not depend on visits.
        # Keep a dedicated label so staff never appears as GOLD in tier stats/raffles.
        staff_level = rec.get("staff_level")
        if isinstance(staff_level, str) and staff_level.strip():
            rec["level"] = staff_level.strip()
        else:
            # Backward-compat: older records only had staff_gold.
            rec["level"] = "ADMIN🐧"
        # Staff discount can be overridden (e.g. owners). Default: 10.
        rec["discount"] = int(rec.get("staff_discount", 10) or 10)
        return
    visits = int(rec.get("visits", 0) or 0)
    lvl, disc = tier_for_visits(visits)
    rec["level"] = lvl
    rec["discount"] = int(disc)

def _is_bad_number(n: int) -> bool:
    # Avoid 1111/2222/.../9999 (and 0000 if it ever appears).
    s = f"{n:04d}"
    return len(set(s)) == 1


def _alloc_random_card_number(by_number: dict[str, Any]) -> str:
    """
    Allocate a unique random 4-digit card number.
    Range: 0010..9998 (inclusive), excluding 0000/1111/2222/.../9999.
    """
    rng = random.SystemRandom()
    used = set(str(k) for k in by_number.keys())

    # Fast random attempts.
    for _ in range(5000):
        n = rng.randint(10, 9998)
        if _is_bad_number(n):
            continue
        s = f"{n:04d}"
        if s in used:
            continue
        return s

    # Fallback: deterministic scan.
    for n in range(10, 9999):
        if n > 9998:
            break
        if _is_bad_number(n):
            continue
        s = f"{n:04d}"
        if s not in used:
            return s
    raise RuntimeError("No available card numbers")


def ensure_level_card(
    user_id: int,
    *,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> LevelCard:
    """
    Create a LEVEL card for a user if not exists.
    Defaults: IRON⚙️, 3% discount. Card numbers are allocated sequentially.
    """
    data = _load()
    by_user: dict[str, Any] = data.get("by_user", {})
    by_number: dict[str, Any] = data.get("by_number", {})

    uid = str(int(user_id))
    existing_num = by_user.get(uid)
    if existing_num and str(existing_num) in by_number:
        rec = by_number[str(existing_num)]
        # refresh user info if present
        if isinstance(rec, dict):
            if username:
                rec["username"] = username
            if first_name:
                rec["first_name"] = first_name
            if last_name:
                rec["last_name"] = last_name
            # Ensure level/discount are consistent with visits.
            _recalc(rec)
            _save(data)
            return _to_card(str(existing_num), rec)

    # Allocate a random unique 4-digit card number.
    card_number = _alloc_random_card_number(by_number)
    by_user[uid] = card_number
    by_number[card_number] = {
        "user_id": int(user_id),
        "username": username,
        "first_name": first_name,
        "last_name": last_name,
        "level": "IRON⚙️",
        "discount": 3,
        "visits": 0,
        "staff_gold": False,
        "staff_level": None,
        "staff_discount": None,
    }
    _recalc(by_number[card_number])
    # Keep field for backward compatibility; not used for allocation anymore.
    data["next_number"] = int(data.get("next_number", 4821) or 4821)
    data["by_user"] = by_user
    data["by_number"] = by_number
    _save(data)
    return _to_card(card_number, by_number[card_number])


def find_card_by_number(card_number: str) -> LevelCard | None:
    s = (card_number or "").strip()
    if not s:
        return None
    data = _load()
    rec = (data.get("by_number") or {}).get(s)
    if not isinstance(rec, dict):
        return None
    return _to_card(s, rec)


def find_card_by_user_id(user_id: int) -> LevelCard | None:
    data = _load()
    by_user = data.get("by_user") or {}
    by_number = data.get("by_number") or {}
    num = by_user.get(str(int(user_id)))
    if not num:
        return None
    rec = by_number.get(str(num))
    if not isinstance(rec, dict):
        return None
    return _to_card(str(num), rec)


def add_visit_by_user_id(user_id: int, delta: int = 1) -> LevelCard | None:
    """
    Increment total confirmed visits for a user.
    """
    if delta <= 0:
        return find_card_by_user_id(user_id)
    data = _load()
    by_user = data.get("by_user") or {}
    by_number = data.get("by_number") or {}
    num = by_user.get(str(int(user_id)))
    if not num:
        return None
    rec = by_number.get(str(num))
    if not isinstance(rec, dict):
        return None
    rec["visits"] = int(rec.get("visits", 0) or 0) + int(delta)
    _recalc(rec)
    _save(data)
    return _to_card(str(num), rec)


def set_staff_gold_by_user_id(
    user_id: int,
    *,
    staff_level: str | None = None,
    staff_discount: int | None = None,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> LevelCard:
    # Ensure card exists.
    card = ensure_level_card(user_id, username=username, first_name=first_name, last_name=last_name)
    # Reload to ensure we see records created/updated by ensure_level_card().
    data = _load()
    by_user = data.get("by_user") or {}
    by_number = data.get("by_number") or {}
    num = by_user.get(str(int(user_id))) or card.card_number
    rec = by_number.get(str(num))
    if not isinstance(rec, dict):
        # Shouldn't happen, but keep safe.
        return card
    rec["staff_gold"] = True
    if staff_level:
        rec["staff_level"] = str(staff_level)
    if staff_discount is not None:
        try:
            rec["staff_discount"] = int(staff_discount)
        except Exception:
            rec["staff_discount"] = 10
    if username:
        rec["username"] = username
    if first_name:
        rec["first_name"] = first_name
    if last_name:
        rec["last_name"] = last_name
    _recalc(rec)
    _save(data)
    return _to_card(str(num), rec)


def clear_staff_gold_by_user_id(user_id: int) -> LevelCard | None:
    data = _load()
    by_user = data.get("by_user") or {}
    by_number = data.get("by_number") or {}
    num = by_user.get(str(int(user_id)))
    if not num:
        return None
    rec = by_number.get(str(num))
    if not isinstance(rec, dict):
        return None
    rec["staff_gold"] = False
    rec.pop("staff_level", None)
    rec.pop("staff_discount", None)
    _recalc(rec)
    _save(data)
    return _to_card(str(num), rec)


def list_cards() -> list[LevelCard]:
    """
    Returns all cards from storage (best-effort, unsorted).
    """
    data = _load()
    by_number = data.get("by_number") or {}
    out: list[LevelCard] = []
    if not isinstance(by_number, dict):
        return out
    for num, rec in by_number.items():
        if not isinstance(rec, dict):
            continue
        try:
            out.append(_to_card(str(num), rec))
        except Exception:
            continue
    return out
