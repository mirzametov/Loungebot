import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

DATA_FILE = Path("data/admin_stats.json")


@dataclass(frozen=True)
class UserInfo:
    user_id: int
    first_name: str | None
    username: str | None


def _now() -> datetime:
    return datetime.now().astimezone()

def _tyumen_tz():
    # Tyumen time: UTC+5. Try canonical tz names, fallback to fixed offset.
    try:
        return ZoneInfo("Asia/Tyumen")
    except Exception:
        try:
            return ZoneInfo("Asia/Yekaterinburg")
        except Exception:
            from datetime import timezone
            return timezone(timedelta(hours=5))


def _tyumen_window_start(now: datetime) -> datetime:
    """
    Business day boundary: 06:00 Tyumen time.
    Returns start timestamp of the current business day window.
    """
    tz = _tyumen_tz()
    local = now.astimezone(tz)
    start_today = local.replace(hour=6, minute=0, second=0, microsecond=0)
    if local < start_today:
        start_today = start_today - timedelta(days=1)
    return start_today


def _parse_event_ts(raw_ts: object, *, fallback_tz) -> datetime | None:
    if not raw_ts:
        return None
    try:
        ts = datetime.fromisoformat(str(raw_ts))
    except Exception:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=fallback_tz)
    return ts


def _load() -> dict[str, Any]:
    if not DATA_FILE.exists():
        return {"users": {}}
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"users": {}}


def _save(data: dict[str, Any]) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DATA_FILE)


def touch_user(user: UserInfo) -> None:
    data = _load()
    users = data.setdefault("users", {})
    uid = str(user.user_id)
    rec = users.get(uid)

    now = _now().isoformat()
    if rec is None:
        users[uid] = {
            "first_name": user.first_name,
            "username": user.username,
            "joined_at": now,
            "last_seen": now,
            "unsubscribed_at": None,
            "clicks": 0,
            "visits": 0,
            # Confirmed visits timestamps (added by admin actions).
            "visit_events": [],
            # For "clicked within last N days" checks.
            "last_click_at": None,
        }
    else:
        rec["first_name"] = user.first_name
        rec["username"] = user.username
        rec["last_seen"] = now
        # If user came back after block/unblock, treat as subscribed again.
        rec["unsubscribed_at"] = None
        rec.setdefault("visit_events", [])
        rec.setdefault("last_click_at", None)

    _save(data)


def inc_click(user_id: int) -> None:
    data = _load()
    users = data.setdefault("users", {})
    uid = str(user_id)
    rec = users.get(uid)
    if rec is None:
        # user record should be created by touch_user, but keep it safe.
        now = _now().isoformat()
        users[uid] = {
            "first_name": None,
            "username": None,
            "joined_at": now,
            "last_seen": now,
            "unsubscribed_at": None,
            "clicks": 1,
            "visits": 0,
            "visit_events": [],
            "last_click_at": now,
        }
    else:
        rec["clicks"] = int(rec.get("clicks", 0)) + 1
        now = _now().isoformat()
        rec["last_seen"] = now
        rec["last_click_at"] = now
        rec.setdefault("visit_events", [])
        rec.setdefault("last_click_at", None)

    _save(data)


def mark_unsubscribed(user_id: int) -> None:
    data = _load()
    users = data.setdefault("users", {})
    uid = str(user_id)
    rec = users.get(uid)
    if rec is None:
        return
    rec["unsubscribed_at"] = _now().isoformat()
    _save(data)


def active_subscribers_count() -> int:
    data = _load()
    users = data.get("users", {})
    return sum(1 for rec in users.values() if not rec.get("unsubscribed_at"))


def _count_by_window(ts_key: str, days: int) -> int:
    data = _load()
    users = data.get("users", {})
    now = _now()
    if days == 0:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start = now - timedelta(days=days)

    count = 0
    for rec in users.values():
        raw = rec.get(ts_key)
        if not raw:
            continue
        try:
            ts = datetime.fromisoformat(raw)
        except Exception:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=now.tzinfo)
        if ts >= start:
            count += 1
    return count


def subscribed_counts() -> tuple[int, int, int]:
    # today, 7d, 30d
    return (
        _count_by_window("joined_at", 0),
        _count_by_window("joined_at", 7),
        _count_by_window("joined_at", 30),
    )


def unsubscribed_counts() -> tuple[int, int, int]:
    return (
        _count_by_window("unsubscribed_at", 0),
        _count_by_window("unsubscribed_at", 7),
        _count_by_window("unsubscribed_at", 30),
    )


def visit_counts() -> tuple[int, int, int]:
    """
    Confirmed visits (marked by an admin) within windows: today / 7d / 30d.
    This is NOT "how many users clicked".
    """
    data = _load()
    users = data.get("users", {})
    now = _now()

    def _count_events(days: int) -> int:
        if days == 0:
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            start = now - timedelta(days=days)
        total = 0
        for rec in users.values():
            events = rec.get("visit_events") or []
            if not isinstance(events, list):
                continue
            for raw in events:
                if not raw:
                    continue
                # Backward compatible: raw can be ISO str or {"ts": "...", "by": admin_id}
                if isinstance(raw, dict):
                    raw_ts = raw.get("ts")
                else:
                    raw_ts = raw
                if not raw_ts:
                    continue
                try:
                    ts = datetime.fromisoformat(str(raw_ts))
                except Exception:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=now.tzinfo)
                if ts >= start:
                    total += 1
        return total

    return (_count_events(0), _count_events(7), _count_events(30))


def add_visit(user_id: int) -> None:
    """
    Confirm a visit for a user (called by admin actions).
    """
    data = _load()
    users = data.setdefault("users", {})
    uid = str(user_id)
    rec = users.get(uid)
    if rec is None:
        now = _now().isoformat()
        users[uid] = {
            "first_name": None,
            "username": None,
            "joined_at": now,
            "last_seen": now,
            "unsubscribed_at": None,
            "clicks": 0,
            "visits": 1,
            "visit_events": [now],
            "last_click_at": None,
        }
    else:
        rec["visits"] = int(rec.get("visits", 0) or 0) + 1
        events = rec.setdefault("visit_events", [])
        if isinstance(events, list):
            events.append(_now().isoformat())
        rec.setdefault("last_click_at", None)
    _save(data)

def add_visit_marked(user_id: int, admin_id: int) -> None:
    """
    Confirm a visit and attribute it to the admin who marked it.
    """
    data = _load()
    users = data.setdefault("users", {})
    uid = str(user_id)
    rec = users.get(uid)
    now = _now().isoformat()

    if rec is None:
        users[uid] = {
            "first_name": None,
            "username": None,
            "joined_at": now,
            "last_seen": now,
            "unsubscribed_at": None,
            "clicks": 0,
            "visits": 1,
            "visit_events": [{"ts": now, "by": int(admin_id)}],
            "last_click_at": None,
        }
    else:
        rec["visits"] = int(rec.get("visits", 0) or 0) + 1
        events = rec.setdefault("visit_events", [])
        if isinstance(events, list):
            events.append({"ts": now, "by": int(admin_id)})
        rec.setdefault("last_click_at", None)

    _save(data)


def can_add_visit_today_tyumen(user_id: int) -> bool:
    """
    Rule: per client, max 1 confirmed visit per business day.
    Business day resets at 06:00 Tyumen time.
    """
    data = _load()
    users = data.get("users", {})
    rec = users.get(str(user_id))
    if not isinstance(rec, dict):
        return True

    events = rec.get("visit_events") or []
    if not isinstance(events, list) or not events:
        return True

    now = _now()
    tz = _tyumen_tz()
    start = _tyumen_window_start(now)
    end = start + timedelta(days=1)

    for raw in events:
        if not raw:
            continue
        if isinstance(raw, dict):
            raw_ts = raw.get("ts")
        else:
            raw_ts = raw
        ts = _parse_event_ts(raw_ts, fallback_tz=tz)
        if ts is None:
            continue
        local = ts.astimezone(tz)
        if start <= local < end:
            return False
    return True


def user_visit_counts(user_id: int) -> tuple[int, int, int]:
    """
    Per-user confirmed visits:
    - visits in last 7 days
    - visits in last 30 days
    - total visits
    """
    data = _load()
    users = data.get("users", {})
    rec = users.get(str(user_id)) or {}
    now = _now()

    events = rec.get("visit_events") or []
    if not isinstance(events, list):
        events = []

    def _count_since(days: int) -> int:
        start = now - timedelta(days=days)
        cnt = 0
        for raw in events:
            if not raw:
                continue
            if isinstance(raw, dict):
                raw_ts = raw.get("ts")
            else:
                raw_ts = raw
            if not raw_ts:
                continue
            try:
                ts = datetime.fromisoformat(str(raw_ts))
            except Exception:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=now.tzinfo)
            if ts >= start:
                cnt += 1
        return cnt

    total = int(rec.get("visits", 0) or 0)
    # Prefer events length if it's higher (safer on old data).
    total = max(total, len(events))
    return (_count_since(7), _count_since(30), total)

def top_admins_by_marked_visits(days: int = 30, limit: int = 100) -> list[dict[str, Any]]:
    """
    Returns rows: {admin_id, visits} for admins who marked >=1 visit in the last `days`.
    """
    data = _load()
    users: dict[str, Any] = data.get("users", {})
    now = _now()
    start = now - timedelta(days=days)

    counts: dict[int, int] = {}
    for rec in users.values():
        if not isinstance(rec, dict):
            continue
        events = rec.get("visit_events") or []
        if not isinstance(events, list):
            continue
        for raw in events:
            if not raw:
                continue
            if not isinstance(raw, dict):
                # old format: no admin attribution
                continue
            by = raw.get("by")
            ts_raw = raw.get("ts")
            if by is None or not ts_raw:
                continue
            try:
                ts = datetime.fromisoformat(str(ts_raw))
            except Exception:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=now.tzinfo)
            if ts < start:
                continue
            try:
                aid = int(by)
            except Exception:
                continue
            counts[aid] = counts.get(aid, 0) + 1

    rows = [{"admin_id": aid, "visits": v} for aid, v in counts.items() if v > 0]
    rows.sort(key=lambda r: (int(r["visits"]), int(r["admin_id"])), reverse=True)
    return rows[:limit]


def admin_marked_visits_counts(admin_id: int, days: int = 30) -> tuple[int, int]:
    """
    Returns:
    - marked visits within last `days`
    - total marked visits (all time)
    """
    data = _load()
    users: dict[str, Any] = data.get("users", {})
    now = _now()
    start = now - timedelta(days=days)

    total = 0
    recent = 0
    for rec in users.values():
        if not isinstance(rec, dict):
            continue
        events = rec.get("visit_events") or []
        if not isinstance(events, list):
            continue
        for raw in events:
            if not isinstance(raw, dict):
                continue
            by = raw.get("by")
            ts_raw = raw.get("ts")
            if by is None or not ts_raw:
                continue
            try:
                if int(by) != int(admin_id):
                    continue
            except Exception:
                continue
            total += 1
            try:
                ts = datetime.fromisoformat(str(ts_raw))
            except Exception:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=now.tzinfo)
            if ts >= start:
                recent += 1
    return (recent, total)


def admin_marked_visits_summary(admin_id: int) -> tuple[int, int, int, int]:
    """
    Returns marked visits:
    - today
    - last 7 days
    - last 30 days
    - total (all time)
    """
    data = _load()
    users: dict[str, Any] = data.get("users", {})
    now = _now()
    start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_7 = now - timedelta(days=7)
    start_30 = now - timedelta(days=30)

    total = 0
    c_today = 0
    c_7 = 0
    c_30 = 0

    for rec in users.values():
        if not isinstance(rec, dict):
            continue
        events = rec.get("visit_events") or []
        if not isinstance(events, list):
            continue
        for raw in events:
            if not isinstance(raw, dict):
                continue
            by = raw.get("by")
            ts_raw = raw.get("ts")
            if by is None or not ts_raw:
                continue
            try:
                if int(by) != int(admin_id):
                    continue
            except Exception:
                continue
            total += 1
            try:
                ts = datetime.fromisoformat(str(ts_raw))
            except Exception:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=now.tzinfo)
            if ts >= start_today:
                c_today += 1
            if ts >= start_7:
                c_7 += 1
            if ts >= start_30:
                c_30 += 1

    return (c_today, c_7, c_30, total)


def admin_marked_recent_clients(admin_id: int, limit: int = 20) -> list[dict[str, Any]]:
    """
    Returns recent marked visits by an admin:
    [{user_id, ts}] sorted by ts desc, limited.
    """
    data = _load()
    users: dict[str, Any] = data.get("users", {})

    rows: list[dict[str, Any]] = []
    for uid, rec in users.items():
        if not isinstance(rec, dict):
            continue
        events = rec.get("visit_events") or []
        if not isinstance(events, list):
            continue
        for raw in events:
            if not isinstance(raw, dict):
                continue
            by = raw.get("by")
            ts_raw = raw.get("ts")
            if by is None or not ts_raw:
                continue
            try:
                if int(by) != int(admin_id):
                    continue
            except Exception:
                continue
            try:
                user_id = int(uid)
            except Exception:
                continue
            rows.append({"user_id": user_id, "ts": str(ts_raw)})

    def _key(r: dict[str, Any]) -> tuple[str, int]:
        # ISO strings sort chronologically as strings in the same format.
        return (str(r.get("ts") or ""), int(r.get("user_id") or 0))

    rows.sort(key=_key, reverse=True)
    return rows[:limit]


def admin_marked_recent_clients_page(admin_id: int, *, offset: int = 0, limit: int = 20) -> tuple[list[dict[str, Any]], int]:
    """
    Returns (rows, total_count) for marked visits by this admin, ordered by ts desc.
    """
    if offset < 0:
        offset = 0
    if limit <= 0:
        limit = 20

    data = _load()
    users: dict[str, Any] = data.get("users", {})

    rows: list[dict[str, Any]] = []
    for uid, rec in users.items():
        if not isinstance(rec, dict):
            continue
        events = rec.get("visit_events") or []
        if not isinstance(events, list):
            continue
        for raw in events:
            if not isinstance(raw, dict):
                continue
            by = raw.get("by")
            ts_raw = raw.get("ts")
            if by is None or not ts_raw:
                continue
            try:
                if int(by) != int(admin_id):
                    continue
            except Exception:
                continue
            try:
                user_id = int(uid)
            except Exception:
                continue
            rows.append({"user_id": user_id, "ts": str(ts_raw)})

    def _key(r: dict[str, Any]) -> tuple[str, int]:
        return (str(r.get("ts") or ""), int(r.get("user_id") or 0))

    rows.sort(key=_key, reverse=True)
    total = len(rows)
    return (rows[offset : offset + limit], total)


def find_user_id_by_username(username: str) -> int | None:
    """
    Lookup user_id by stored telegram username (case-insensitive, without @).
    """
    u = (username or "").strip().lstrip("@").lower()
    if not u:
        return None
    data = _load()
    users: dict[str, Any] = data.get("users", {})
    for uid, rec in users.items():
        if not isinstance(rec, dict):
            continue
        ru = (rec.get("username") or "").strip().lstrip("@").lower()
        if ru and ru == u:
            try:
                return int(uid)
            except Exception:
                return None
    return None


def top_by_clicks(limit: int = 50) -> list[dict[str, Any]]:
    data = _load()
    users: dict[str, Any] = data.get("users", {})

    rows = []
    for uid, rec in users.items():
        rows.append(
            {
                "user_id": int(uid),
                "first_name": rec.get("first_name"),
                "username": rec.get("username"),
                "clicks": int(rec.get("clicks", 0) or 0),
                "visits": int(rec.get("visits", 0) or 0),
                "last_click_at": rec.get("last_click_at"),
                "active": not bool(rec.get("unsubscribed_at")),
            }
        )

    rows.sort(key=lambda r: (r["clicks"], r["user_id"]), reverse=True)
    return rows[:limit]


def get_user_stats(user_id: int) -> dict[str, Any] | None:
    data = _load()
    users = data.get("users", {})
    rec = users.get(str(user_id))
    if not isinstance(rec, dict):
        return None
    return rec


def has_click_in_last_days(user_id: int, days: int) -> bool:
    rec = get_user_stats(user_id)
    if not rec:
        return False
    if int(rec.get("clicks", 0) or 0) <= 0:
        return False
    raw = rec.get("last_click_at")
    if not raw:
        return False
    now = _now()
    try:
        ts = datetime.fromisoformat(raw)
    except Exception:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=now.tzinfo)
    return ts >= (now - timedelta(days=days))
