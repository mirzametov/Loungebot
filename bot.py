from __future__ import annotations

import os
import json
from html import escape
from pathlib import Path
from urllib.parse import quote
import time
import logging
import sys

import telebot
from dotenv import load_dotenv
from telebot.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from loungebot.admin_stats import (
    UserInfo,
    active_subscribers_count,
    active_user_ids,
    admin_marked_recent_clients,
    admin_marked_visits_counts,
    admin_marked_visits_summary,
    admin_marked_recent_clients_page,
    find_user_id_by_username,
    get_user_stats,
    has_click_in_last_days,
    inc_click,
    subscribed_counts,
    top_by_clicks,
    top_admins_by_marked_visits,
    touch_user,
    unsubscribed_counts,
    filter_user_ids_by_broadcast_cooldown,
    record_broadcast_sent,
    top_users_by_visits_in_month,
    users_no_visits_between_days,
    users_no_visits_for_days,
    users_last_visit_older_than_days,
    visit_counts,
    user_visit_counts,
    add_visit_marked,
    can_add_visit_today_tyumen,
)
from loungebot.admin_roles import (
    add_admin_by_username,
    admin_user_ids,
    is_admin_user,
    list_admins,
    normalize_username,
    remove_admin_by_username,
    sync_from_user,
)
from loungebot.guest_cards import is_registered, register_card
from loungebot.level_cards import (
    add_visit_by_user_id,
    clear_staff_gold_by_user_id,
    ensure_level_card,
    find_card_by_number,
    find_card_by_user_id,
    list_cards,
    next_tier_info,
    set_staff_gold_by_user_id,
    tier_for_visits,
)
from loungebot.keyboards import (
    BTN_BOOKING,
    BTN_GUEST_CARD,
    BTN_LOCATION,
    BTN_MENU,
    BTN_REGISTER_CARD,
)

LOG_PATH = Path(__file__).with_name("bot.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("loungebot")

_BONUS_BY_PLACE = {1: 10, 2: 6, 3: 3}  # extra % for winners in the next month
_MEDAL_BY_PLACE = {1: "🥇", 2: "🥈", 3: "🥉"}

# Inline-mode image (cached photo file_id in Telegram).
_inline_photo_file_id: str | None = None


def _tyumen_now() -> datetime:
    try:
        return datetime.now(ZoneInfo("Asia/Tyumen"))
    except Exception:
        return datetime.now().astimezone()


def _prev_month(dt: datetime) -> tuple[int, int]:
    y = int(dt.year)
    m = int(dt.month)
    if m == 1:
        return (y - 1, 12)
    return (y, m - 1)


def _monthly_bonus_map_for_prev_month(now: datetime) -> dict[int, int]:
    """
    Bonus is granted in the current month based on previous month's leaderboard.
    Starts from March 2026 leaderboard (bonuses begin in April 2026).
    """
    prev_y, prev_m = _prev_month(now)
    if (prev_y, prev_m) < (2026, 3):
        return {}

    rows = top_users_by_visits_in_month(prev_y, prev_m, source=BOT_SOURCE, limit=3, active_only=False)
    out: dict[int, int] = {}
    place = 0
    for row in rows:
        try:
            uid = int(row.get("user_id") or 0)
        except Exception:
            continue
        if not is_eligible_for_competitions(uid):
            continue
        place += 1
        out[uid] = int(_BONUS_BY_PLACE.get(place, 0))
        if place >= 3:
            break
    return out


def is_eligible_for_competitions(user_id: int | None) -> bool:
    """
    Staff accounts do not participate in ratings/competitions (and future contests).
    """
    if user_id is None:
        return False
    try:
        uid = int(user_id)
    except Exception:
        return False
    return uid not in _staff_user_ids_known()


def _staff_level_label(user_id: int | None, username: str | None = None) -> str | None:
    """
    Returns special LEVEL label overrides for staff accounts, otherwise None.
    - ADMIN -> 'ADMIN🐧' (penguin)
    - SUPERADMIN -> 'SUPERADMIN🥷'
    """
    if user_id is None:
        return None
    uid = int(user_id)
    uname = normalize_username(username or "") if username else None
    if is_superadmin(uid):
        return "SUPERADMIN🥷"
    try:
        if is_admin_user(uid, uname):
            return "ADMIN🐧"
    except Exception:
        pass
    return None


def _iter_months_inclusive(start_y: int, start_m: int, end_y: int, end_m: int):
    """
    Yields (y, m) months from start to end inclusive.
    """
    y, m = int(start_y), int(start_m)
    while (y, m) <= (int(end_y), int(end_m)):
        yield (y, m)
        if m == 12:
            y += 1
            m = 1
        else:
            m += 1


def medals_for_user(user_id: int | None) -> str:
    """
    Returns medal emojis in chronological order of months earned.
    Only uses completed months (previous month and earlier).
    Launch: March 2026.
    """
    if user_id is None:
        return ""
    uid = int(user_id)
    if not is_eligible_for_competitions(uid):
        return ""

    now = _tyumen_now()
    # Completed month range ends at previous month.
    end_y, end_m = _prev_month(now)
    if (end_y, end_m) < (2026, 3):
        return ""

    staff = _staff_user_ids_known()
    medals: list[str] = []
    for y, m in _iter_months_inclusive(2026, 3, end_y, end_m):
        rows = top_users_by_visits_in_month(y, m, source=BOT_SOURCE, limit=3, active_only=False)
        place = 0
        for row in rows:
            try:
                ruid = int(row.get("user_id") or 0)
            except Exception:
                continue
            if not ruid or ruid in staff:
                continue
            place += 1
            if ruid == uid:
                em = _MEDAL_BY_PLACE.get(place)
                if em:
                    medals.append(em)
            if place >= 3:
                break

    return "".join(medals)


def bonus_discount_for_user(user_id: int | None) -> int:
    """
    Extra discount percent for current month (based on previous month results).
    """
    if user_id is None:
        return 0
    uid = int(user_id)
    if not is_eligible_for_competitions(uid):
        return 0
    now = _tyumen_now()
    m = _monthly_bonus_map_for_prev_month(now)
    return int(m.get(uid, 0))


def total_discount_for_user(user_id: int | None, base_discount: int) -> tuple[int, int]:
    bonus = bonus_discount_for_user(user_id)
    total = int(base_discount) + int(bonus)
    return (total, bonus)


def guest_card_text(display_name: str, *, user_id: int | None = None) -> str:
    card = find_card_by_user_id(int(user_id)) if user_id is not None else None
    level_label = card.level if card else "IRON⚙️"
    card_number = card.card_number if card else "4821"
    base_discount = card.discount if card else 3
    total_discount, bonus_discount = total_discount_for_user(user_id, base_discount)
    total_visits = card.visits if card else 0

    lvl_override = _staff_level_label(user_id, (card.username if card else None))
    if lvl_override:
        level_label = lvl_override

    if user_id is not None and is_superadmin(int(user_id)):
        header_line = f"Твой уровень: <b>{escape(level_label)}</b>"
    else:
        header_line = f"{display_name}, твой уровень: <b>{escape(level_label)}</b>"

    # Don't show "next tier" line for GOLD.
    progress_line = ""
    if lvl_override:
        progress_line = ""
    elif card and not str(level_label).startswith("GOLD"):
        next_info = next_tier_info(total_visits)
        if next_info is not None:
            next_level, remain = next_info
            progress_line = f"До <b>{escape(next_level)}</b> осталось: <b>{remain} визитов</b>"
    elif card is None:
        # Unregistered fallback copy.
        progress_line = "До <b>BRONZE🥉</b> осталось: <b>5 визитов</b>"

    if bonus_discount > 0:
        discount_line = (
            f"Скидка: <b>{base_discount}%</b>, плюс <b>{bonus_discount}%</b>\n"
            f"Общая скидка: <b>{total_discount}%</b>"
        )
    else:
        discount_line = f"Скидка: <b>{base_discount}%</b>"

    medals = medals_for_user(user_id)
    medals_line = f"Всего медалей: {medals}" if medals else ""

    # After card number: blank line, then 3 lines подряд (visits, discount, progress).
    mid_lines = [
        f"Всего визитов: <b>{total_visits}</b>",
        discount_line,
    ]
    if progress_line:
        mid_lines.append(progress_line)

    return (
        "<b>КАРТА LEVEL</b>\n\n"
        f"{header_line}\n"
        f"Номер карты: <b>{escape(card_number)}</b>\n"
        "\n"
        + "\n".join(mid_lines)
        + (f"\n{medals_line}" if medals_line else "")
        + "\n\n"
        "Твой уровень даёт:\n"
        f"• скидка <b>{total_discount}%</b> на меню <b><a href=\"https://t.me/nagrani_lounge\">Lounge</a></b>\n"
        f"• скидка <b>{total_discount}%</b> на <b><a href=\"https://t.me/prohvat72\">Прохват72</a></b>\n"
    )

def is_superadmin(user_id: int | None) -> bool:
    if user_id is None:
        return False
    raw = os.getenv("SUPERADMIN_IDS", "").strip()
    if not raw:
        # Default: your Telegram user id (developer machine).
        return int(user_id) == 864921585
    try:
        ids = {int(x.strip()) for x in raw.split(",") if x.strip()}
    except ValueError:
        return False
    return int(user_id) in ids


def is_menu_allowed(user_id: int | None) -> bool:
    """
    Menu is open for everyone by default.
    If you need to lock it again: set MENU_LOCKED=1 in env.
    """
    locked = (os.getenv("MENU_LOCKED", "") or "").strip() in {"1", "true", "True", "yes", "YES"}
    return not locked


def _tg_user_link(user_id: int, username: str | None = None) -> str:
    # `tg://user?id=` is flaky on some Telegram clients for users other than yourself.
    # Prefer a public @username link when available.
    if username:
        u = username.strip().lstrip("@")
        if u:
            return f"https://t.me/{u}"
    return f"tg://user?id={user_id}"


def _rank_prefix(i: int) -> str:
    if i == 1:
        return "🥇"
    if i == 2:
        return "🥈"
    if i == 3:
        return "🥉"
    return f"{i}. "

def _is_admin(user: telebot.types.User | None) -> bool:
    if user is None:
        return False
    try:
        return is_admin_user(user.id, user.username)
    except Exception:
        return False


def _is_staff(user: telebot.types.User | None) -> bool:
    if user is None:
        return False
    if is_superadmin(user.id):
        return True
    return _is_admin(user)


def _is_staff_user_id(user_id: int, username: str | None) -> bool:
    if is_superadmin(user_id):
        return True
    try:
        return is_admin_user(user_id, username)
    except Exception:
        return False


def main_inline_keyboard(*, superadmin: bool, admin: bool) -> InlineKeyboardMarkup:
    # "admin" here means non-superadmin staff account.
    # Superadmins keep the admin menu button as-is.
    keyboard = InlineKeyboardMarkup()
    keyboard.row(InlineKeyboardButton(text=BTN_GUEST_CARD, callback_data="main_guest_card"))
    keyboard.row(InlineKeyboardButton(text=BTN_MENU, callback_data="main_menu"))
    keyboard.row(InlineKeyboardButton(text=BTN_BOOKING, url=booking_deep_link()))
    keyboard.row(InlineKeyboardButton(text=BTN_LOCATION, callback_data="main_location"))
    if superadmin:
        keyboard.row(
            InlineKeyboardButton(
                text=f"👀 SuperAdmin {active_subscribers_count()}",
                callback_data="main_admin",
            )
        )
        keyboard.row(
            InlineKeyboardButton(
                text="➕ Добавить визит",
                callback_data="main_add_visit",
            )
        )
    elif admin:
        keyboard.row(
            InlineKeyboardButton(
                text="➕ Добавить визит",
                callback_data="main_add_visit",
            )
        )
    return keyboard


def guest_card_inline_keyboard() -> InlineKeyboardMarkup:
    # For new users: only registration button (no tabs yet).
    keyboard = InlineKeyboardMarkup()
    keyboard.row(InlineKeyboardButton(text=BTN_REGISTER_CARD, callback_data="register_card"))
    return keyboard


def guest_card_registered_inline_keyboard() -> InlineKeyboardMarkup:
    return level_keyboard(registered=True, active="card")


def level_keyboard(*, registered: bool, active: str) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()

    class _StyledInlineButton:
        def __init__(self, *, text: str, callback_data: str, style: str) -> None:
            self.text = text
            self.callback_data = callback_data
            self.style = style

        def to_dict(self) -> dict:
            return {"text": self.text, "callback_data": self.callback_data, "style": self.style}

    def _tab(text: str, tab: str) -> InlineKeyboardButton:
        if tab == active:
            return _StyledInlineButton(text=text, callback_data=f"level_tab:{tab}", style="primary")  # type: ignore[return-value]
        return InlineKeyboardButton(text=text, callback_data=f"level_tab:{tab}")

    if not registered:
        keyboard.row(InlineKeyboardButton(text=BTN_REGISTER_CARD, callback_data="register_card"))

    keyboard.row(_tab("🪪 Карта LEVEL", "card"), _tab("🏆 Рейтинг", "rating"))
    keyboard.row(
        _tab("🔥 Розыгрыш", "giveaway"),
        _tab("🧾 Условия", "visits"),
    )
    keyboard.row(InlineKeyboardButton(text="👈Назад", callback_data="back_to_main"))
    return keyboard


def location_inline_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    keyboard.row(
        InlineKeyboardButton(text="2GIS", url=LOCATION_2GIS_URL),
        InlineKeyboardButton(text="Яндекс", url=YANDEX_URL),
        InlineKeyboardButton(text="Телеграм", callback_data="location_telegram_geo"),
    )
    keyboard.row(InlineKeyboardButton(text="📸 Интерьер", callback_data="location_interior"))
    keyboard.row(InlineKeyboardButton(text="🚀 Новости бара", url=NEWS_URL))
    keyboard.row(InlineKeyboardButton(text="🏍 Наш прокат Прохват72", url=PROHVAT72_URL))
    keyboard.row(InlineKeyboardButton(text="🏁 Наши гонки На грани", url=RACES_URL))
    keyboard.row(InlineKeyboardButton(text="👈Назад", callback_data="back_to_main"))
    return keyboard


INTERIOR_DIR = Path("assets/interior")
INTERIOR_COUNT = 8


def _interior_photo_path(idx: int) -> Path:
    i = int(idx)
    if i < 1:
        i = 1
    if i > INTERIOR_COUNT:
        i = INTERIOR_COUNT
    return INTERIOR_DIR / f"{i}.jpg"


def interior_keyboard(idx: int) -> InlineKeyboardMarkup:
    i = int(idx)
    if i < 1:
        i = 1
    if i > INTERIOR_COUNT:
        i = INTERIOR_COUNT

    kb = InlineKeyboardMarkup()

    # Navigation row
    if i == 1:
        kb.row(InlineKeyboardButton(text="Следующая ➡️", callback_data="interior:2"))
    elif i == INTERIOR_COUNT:
        kb.row(InlineKeyboardButton(text="⬅️ Предыдущая", callback_data=f"interior:{INTERIOR_COUNT - 1}"))
    else:
        kb.row(
            InlineKeyboardButton(text="⬅️", callback_data=f"interior:{i - 1}"),
            InlineKeyboardButton(text="➡️", callback_data=f"interior:{i + 1}"),
        )

    kb.row(
        InlineKeyboardButton(text="👈 Назад", callback_data="interior_back"),
        InlineKeyboardButton(text="🏠 Домой", callback_data="back_to_main"),
    )
    return kb


def send_interior(chat_id: int, *, idx: int) -> None:
    p = _interior_photo_path(idx)
    if not p.exists():
        bot.send_message(chat_id, "Фото интерьера не найдено.", reply_markup=location_inline_keyboard())
        return
    with p.open("rb") as f:
        bot.send_photo(chat_id, f, reply_markup=interior_keyboard(idx))


def pitbike_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton(text="📸 Интерьер", callback_data="location_interior"))
    kb.row(InlineKeyboardButton(text="🏠 Домой", callback_data="back_to_main"))
    return kb


def send_pitbike_photo(chat_id: int) -> None:
    # Photo #1 is the pitbike shot.
    p = _interior_photo_path(1)
    if not p.exists():
        bot.send_message(chat_id, "Фото питбайка не найдено.")
        return
    with p.open("rb") as f:
        # Deep-link should just drop the photo without additional menus.
        bot.send_photo(chat_id, f)


def menu_inline_keyboard(
    *,
    active: str | None = None,
    drinks_rules: bool = False,
) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()

    class _StyledInlineButton:
        def __init__(self, *, text: str, callback_data: str, style: str) -> None:
            self.text = text
            self.callback_data = callback_data
            self.style = style

        def to_dict(self) -> dict:
            return {"text": self.text, "callback_data": self.callback_data, "style": self.style}

    def _tab(text: str, cb: str) -> InlineKeyboardButton:
        if active and cb == active:
            return _StyledInlineButton(text=text, callback_data=cb, style="primary")  # type: ignore[return-value]
        return InlineKeyboardButton(text=text, callback_data=cb)

    keyboard.row(_tab("💨 Кальян", "menu_hookah"), _tab("🫖 Чай", "menu_tea"))
    keyboard.row(_tab("🥤 Напитки", "menu_drinks"), _tab("🍷Алкоголь", "menu_rules"))
    keyboard.row(InlineKeyboardButton(text="👈 Назад", callback_data="back_to_main"), _tab("🍽 Еда", "menu_food"))

    return keyboard


def booking_deep_link() -> str:
    admin = BOOKING_ADMIN.lstrip("@").strip()
    message = quote(BOOKING_TEXT, safe="")
    return f"https://t.me/{admin}?text={message}"


def booking_inline_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    keyboard.row(InlineKeyboardButton(text=BTN_BOOKING, url=booking_deep_link()))
    keyboard.row(InlineKeyboardButton(text="👈Назад", callback_data="back_to_main"))
    return keyboard

def admin_menu_keyboard() -> InlineKeyboardMarkup:
    subs = active_subscribers_count()
    keyboard = InlineKeyboardMarkup()
    keyboard.row(InlineKeyboardButton(text="👥 Управление админами", callback_data="admin_admins"))
    keyboard.row(InlineKeyboardButton(text=f"📊 Статистика {subs}", callback_data="admin_stats"))
    keyboard.row(InlineKeyboardButton(text="📣 Рассылка", callback_data="admin_broadcast"))
    keyboard.row(InlineKeyboardButton(text="📚 Правила", callback_data="admin_rules"))
    keyboard.row(InlineKeyboardButton(text="👈Назад", callback_data="back_to_main"))
    return keyboard


def admin_bottom_keyboard(back_cb: str) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    keyboard.row(
        InlineKeyboardButton(text="👈Назад", callback_data=back_cb),
        InlineKeyboardButton(text="🏠 Домой", callback_data="back_to_main"),
    )
    return keyboard


def admin_rules_keyboard(active: str) -> InlineKeyboardMarkup:
    """
    Small tab buttons (up to 3 in a row) + back/home.
    """
    class _StyledInlineButton:
        def __init__(self, *, text: str, callback_data: str, style: str) -> None:
            self.text = text
            self.callback_data = callback_data
            self.style = style

        def to_dict(self) -> dict:
            # Telegram Bot API 9.4+: supports "style" for buttons.
            return {"text": self.text, "callback_data": self.callback_data, "style": self.style}

    keyboard = InlineKeyboardMarkup()

    def _tab(text: str, tab: str) -> InlineKeyboardButton:
        if tab == active:
            # Paint the whole button blue (primary).
            return _StyledInlineButton(text=text, callback_data=f"admin_rules:{tab}", style="primary")  # type: ignore[return-value]
        return InlineKeyboardButton(text=text, callback_data=f"admin_rules:{tab}")

    tabs: list[InlineKeyboardButton] = [
        _tab("Баллы", "points"),
        _tab("Визиты", "visits"),
        _tab("Рейтинг", "rating"),
        _tab("Рассылки", "broadcast"),
        _tab("Билд", "build"),
    ]

    def _layout(count: int) -> list[int]:
        # Layout rules:
        # 1-3 -> one row (count)
        # 4 -> 2+2
        # 5 -> 3+2
        # 6 -> 3+3
        # 7 -> 3+2+2
        if count <= 3:
            return [count]
        if count == 4:
            return [2, 2]
        if count == 5:
            return [3, 2]
        if count == 6:
            return [3, 3]
        if count == 7:
            return [3, 2, 2]
        # Fallback: pack by 3s.
        full = count // 3
        rem = count % 3
        out = [3] * full
        if rem:
            out.append(rem)
        return out

    i = 0
    for n in _layout(len(tabs)):
        row = tabs[i : i + n]
        i += n
        if row:
            keyboard.row(*row)
    keyboard.row(
        InlineKeyboardButton(text="👈Назад", callback_data="admin_menu"),
        InlineKeyboardButton(text="🏠 Домой", callback_data="back_to_main"),
    )
    return keyboard


def admin_rules_text(tab: str) -> str:
    tab = tab or "points"
    if tab == "visits":
        return (
            "<b>Правила визитов</b>\n\n"
            "<b>Условия</b>\n"
            "• чек от <b>1000₽</b>\n\n"
            "<b>Ограничения</b>\n"
            "• не чаще <b>1 раза в день</b> (специально обученный админ обновляет счетчик в 6 утра)\n"
            "• админ не может засчитать визит <b>самому себе</b>\n"
        )
    if tab == "rating":
        return (
            "<b>Правила рейтинга</b>\n\n"
            "<b>Как считается</b>\n"
            "• рейтинг строится по количеству <b>визитов за месяц</b>\n"
            "• админы по умолчанию получают карту <b>ADMIN</b>/<b>SUPERADMIN</b> и <b>не участвуют</b> в рейтингах и розыгрышах\n\n"
            "<b>Бонус победителям</b>\n"
            "• топ-3 прошлого месяца получают дополнительную скидку на <b>следующий месяц</b>:\n"
            "  - 🥇 +10%\n"
            "  - 🥈 +6%\n"
            "  - 🥉 +3%\n"
            "• бонус действует только в течение следующего месяца\n"
            "• общая скидка = скидка LEVEL + бонус рейтинга\n"
            "• у призёров в карте LEVEL отображаются <b>все медали</b>, которые они заработали\n"
        )
    if tab == "broadcast":
        return (
            "<b>Правила рассылок</b>\n\n"
            "<b>Кому уходят</b>\n"
            "• рассылки отправляются только <b>пользователям</b>\n"
            "• админам рассылки <b>не отправляются</b>\n\n"
            "<b>Сегменты</b>\n"
            "• <b>Всем</b> (только пользователи)\n"
            "• <b>Давно не был</b>: от N дней и диапазоны 7-14 / 14-30 / 30-60 / 60-120\n"
            "• <b>Апгрейд</b>: гости, которым осталось 1-2 визита до следующего уровня\n"
            "• <b>Конкурс</b>\n\n"
            "<b>Ограничение частоты</b>\n"
            "• обычные рассылки система <b>не отправляет</b> гостю чаще, чем <b>1 раз за 7 дней</b>\n"
            "• исключение: <b>Конкурс</b> система не запрещает отправлять в любое время (бот сам не делает рассылки)\n\n"
            "<b>Важно про 2 бота</b>\n"
            "• визиты помечаются источником (кальянная/прокат)\n"
            "• в сегментах «Давно не был» учитываются визиты только того источника, откуда отправляется рассылка\n"
        )
    if tab == "build":
        return (
            "<b>Как работает система</b>\n\n"
            "<b>Карты</b>\n"
            "• у каждого гостя есть карта LEVEL (привязана к Telegram)\n"
            "• номер карты 4-значный, выдаётся при регистрации\n\n"
            "<b>Визиты</b>\n"
            "• визиты добавляет админ по номеру карты через кнопку <b>Добавить визит</b>\n"
            "• уровень и скидка пересчитываются автоматически по количеству визитов\n\n"
            "<b>Админы</b>\n"
            "• у админов карта всегда <b>ADMIN🐧 10%</b> (без визитов)\n"
            "• у супер-админов карта всегда <b>SUPERADMIN🥷 10%</b> (без визитов)\n"
            "• админы <b>не участвуют</b> в рейтингах и розыгрышах\n"
            "• если админа разжаловать, staff-карта убирается и уровень снова считается по визитам"
        )
    # points (default)
    return (
        "<b>Уровни и скидки</b>\n\n"
        "• <b>IRON⚙️</b>: <b>3%</b> (сразу)\n"
        "• <b>BRONZE🥉</b>: <b>5%</b> (5 визитов)\n"
        "• <b>SILVER🥈</b>: <b>7%</b> (15 визитов)\n"
        "• <b>GOLD🥇</b>: <b>10%</b> (35 визитов)\n"
    )


def admins_manage_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    keyboard.row(InlineKeyboardButton(text="📋 Список админов", callback_data="admin_admins_list"))
    keyboard.row(InlineKeyboardButton(text="➕ Добавить админа", callback_data="admin_admins_add"))
    keyboard.row(
        InlineKeyboardButton(text="👈Назад", callback_data="admin_menu"),
        InlineKeyboardButton(text="🏠 Домой", callback_data="back_to_main"),
    )
    return keyboard


def admin_broadcast_menu_keyboard() -> InlineKeyboardMarkup:
    # Backward-compat (old UI). Now it shows the new root selection.
    return admin_broadcast_root_keyboard()


def _superadmin_ids() -> set[int]:
    raw = os.getenv("SUPERADMIN_IDS", "").strip()
    ids: set[int] = set()
    if not raw:
        # Keep in sync with is_superadmin() default.
        return {864921585}
    for part in raw.split(","):
        p = part.strip()
        if not p:
            continue
        try:
            ids.add(int(p))
        except Exception:
            continue
    return ids


def _staff_user_ids_known() -> set[int]:
    """
    Staff ids for filtering (never send broadcasts, never count in "Всем").

    Includes:
    - superadmins (env or default)
    - admins with synced user_id
    - admins whose @username matches an active user record (even if user_id wasn't synced into admin_roles yet)
    """
    ids = set(_superadmin_ids()) | set(admin_user_ids())
    try:
        admin_names = {normalize_username(r.username) for r in list_admins()}
        for uid in active_user_ids():
            st = get_user_stats(int(uid)) or {}
            u = st.get("username")
            if isinstance(u, str):
                u = normalize_username(u)
            else:
                u = ""
            if u and u in admin_names:
                ids.add(int(uid))
    except Exception:
        pass
    return ids


def admin_broadcast_root_keyboard() -> InlineKeyboardMarkup:
    """
    Root broadcast menu: choose target segment immediately.
    """
    staff = _staff_user_ids_known()
    active = set(active_user_ids())
    total_users = len([uid for uid in active if int(uid) not in staff])
    keyboard = InlineKeyboardMarkup()
    keyboard.row(
        InlineKeyboardButton(
            text=f"👥 Всем ({total_users})",
            callback_data="admin_broadcast_root:all",
        )
    )
    keyboard.row(InlineKeyboardButton(text="😴 Давно не был", callback_data="admin_broadcast_root:inactive"))
    keyboard.row(InlineKeyboardButton(text="🪪 Апгрейд", callback_data="admin_broadcast_root:upgrade"))
    keyboard.row(InlineKeyboardButton(text="🏆 Конкурс", callback_data="admin_broadcast_root:contest"))
    keyboard.row(
        InlineKeyboardButton(text="👈Назад", callback_data="admin_menu"),
        InlineKeyboardButton(text="🏠 Домой", callback_data="back_to_main"),
    )
    return keyboard


def admin_broadcast_inactive_keyboard() -> InlineKeyboardMarkup:
    staff = _staff_user_ids_known()

    def _cnt(days: int) -> int:
        return len([uid for uid in users_last_visit_older_than_days(days, source=BOT_SOURCE) if int(uid) not in staff])

    def _cnt_range(min_days: int, max_days: int) -> int:
        return len(
            [
                uid
                for uid in users_no_visits_between_days(min_days, max_days, source=BOT_SOURCE)
                if int(uid) not in staff
            ]
        )

    keyboard = InlineKeyboardMarkup()
    keyboard.row(
        InlineKeyboardButton(text=f"От 14 дней ({_cnt(14)})", callback_data="admin_broadcast_inactive:14"),
        InlineKeyboardButton(text=f"От 30 дней ({_cnt(30)})", callback_data="admin_broadcast_inactive:30"),
    )
    keyboard.row(
        InlineKeyboardButton(text=f"От 60 дней ({_cnt(60)})", callback_data="admin_broadcast_inactive:60"),
        InlineKeyboardButton(text=f"От 90 дней ({_cnt(90)})", callback_data="admin_broadcast_inactive:90"),
    )
    keyboard.row(
        InlineKeyboardButton(
            text=f"7-14 дней ({_cnt_range(7, 14)})",
            callback_data="admin_broadcast_inactive_range:7:14",
        ),
        InlineKeyboardButton(
            text=f"14-30 дней ({_cnt_range(14, 30)})",
            callback_data="admin_broadcast_inactive_range:14:30",
        ),
    )
    keyboard.row(
        InlineKeyboardButton(
            text=f"30-60 дней ({_cnt_range(30, 60)})",
            callback_data="admin_broadcast_inactive_range:30:60",
        ),
        InlineKeyboardButton(
            text=f"60-120 дней ({_cnt_range(60, 120)})",
            callback_data="admin_broadcast_inactive_range:60:120",
        ),
    )
    keyboard.row(
        InlineKeyboardButton(text="👈Назад", callback_data="admin_broadcast"),
        InlineKeyboardButton(text="🏠 Домой", callback_data="back_to_main"),
    )
    return keyboard


def _upgrade_targets_counts() -> dict[str, int]:
    """
    Counts of non-staff active users close to tier upgrades by visits.
    """
    staff = _staff_user_ids_known()
    active = set(active_user_ids())
    counts = {"b1": 0, "s2": 0, "s1": 0, "g2": 0, "g1": 0}
    for c in list_cards():
        try:
            uid = int(c.user_id)
        except Exception:
            continue
        if uid not in active:
            continue
        if uid in staff:
            continue
        if bool(getattr(c, "staff_gold", False)):
            continue
        v = int(getattr(c, "visits", 0) or 0)
        if v == 4:
            counts["b1"] += 1
        elif v == 13:
            counts["s2"] += 1
        elif v == 14:
            counts["s1"] += 1
        elif v == 33:
            counts["g2"] += 1
        elif v == 34:
            counts["g1"] += 1
    return counts


def admin_broadcast_upgrade_keyboard() -> InlineKeyboardMarkup:
    cnt = _upgrade_targets_counts()
    keyboard = InlineKeyboardMarkup()
    keyboard.row(InlineKeyboardButton(text=f"До BRONZE: 1 визит ({cnt['b1']})", callback_data="admin_broadcast_upgrade:b1"))
    keyboard.row(
        InlineKeyboardButton(text=f"До SILVER: 2 ({cnt['s2']})", callback_data="admin_broadcast_upgrade:s2"),
        InlineKeyboardButton(text=f"До SILVER: 1 ({cnt['s1']})", callback_data="admin_broadcast_upgrade:s1"),
    )
    keyboard.row(
        InlineKeyboardButton(text=f"До GOLD: 2 ({cnt['g2']})", callback_data="admin_broadcast_upgrade:g2"),
        InlineKeyboardButton(text=f"До GOLD: 1 ({cnt['g1']})", callback_data="admin_broadcast_upgrade:g1"),
    )
    keyboard.row(
        InlineKeyboardButton(text="👈Назад", callback_data="admin_broadcast"),
        InlineKeyboardButton(text="🏠 Домой", callback_data="back_to_main"),
    )
    return keyboard


def admin_broadcast_confirm_keyboard(back_cb: str) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    keyboard.row(InlineKeyboardButton(text="➕ Создать рассылку", callback_data="admin_broadcast_make"))
    keyboard.row(
        InlineKeyboardButton(text="👈Назад", callback_data=back_cb),
        InlineKeyboardButton(text="🏠 Домой", callback_data="back_to_main"),
    )
    return keyboard


def admin_broadcast_cancel_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    keyboard.row(InlineKeyboardButton(text="❌ Отмена", callback_data="admin_broadcast_cancel"))
    keyboard.row(
        InlineKeyboardButton(text="👈Назад", callback_data="admin_broadcast"),
        InlineKeyboardButton(text="🏠 Домой", callback_data="back_to_main"),
    )
    return keyboard


def admin_broadcast_post_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    keyboard.row(InlineKeyboardButton(text="✅ Отправить", callback_data="admin_broadcast_send"))
    keyboard.row(InlineKeyboardButton(text="🔁 Другой пост", callback_data="admin_broadcast_replace"))
    keyboard.row(InlineKeyboardButton(text="❌ Отмена", callback_data="admin_broadcast_cancel"))
    keyboard.row(
        InlineKeyboardButton(text="👈Назад", callback_data="admin_broadcast"),
        InlineKeyboardButton(text="🏠 Домой", callback_data="back_to_main"),
    )
    return keyboard


def _broadcast_targets(kind: str) -> tuple[str, list[int]]:
    kind = (kind or "").strip()
    staff = _staff_user_ids_known()
    active = set(active_user_ids())

    if kind == "all":
        # Broadcasts are never sent to staff accounts.
        targets = sorted([uid for uid in active if int(uid) not in staff])
        targets = filter_user_ids_by_broadcast_cooldown(targets, days=7)
        return ("Всем", targets)

    if kind == "contest":
        # Contest ignores the 7-day broadcast cooldown.
        targets = sorted([uid for uid in active if int(uid) not in staff])
        return ("Конкурс", targets)

    if kind.startswith("inactive:"):
        try:
            days = int(kind.split(":", 1)[1].strip())
        except Exception:
            days = 14
        targets = [
            uid
            for uid in users_last_visit_older_than_days(days, source=BOT_SOURCE)
            if int(uid) in active and int(uid) not in staff
        ]
        targets = filter_user_ids_by_broadcast_cooldown(targets, days=7)
        return (f"Давно не был: {days} дней", targets)

    if kind.startswith("inactive_range:"):
        try:
            rest = kind.split(":", 1)[1].strip()
            a, b = rest.split(":", 1)
            min_days = int(a.strip())
            max_days = int(b.strip())
        except Exception:
            min_days = 7
            max_days = 14
        targets = [
            uid
            for uid in users_no_visits_between_days(min_days, max_days, source=BOT_SOURCE)
            if int(uid) in active and int(uid) not in staff
        ]
        targets = filter_user_ids_by_broadcast_cooldown(targets, days=7)
        return (f"Давно не был: {min_days}-{max_days} дней", targets)

    if kind.startswith("upgrade:"):
        code = kind.split(":", 1)[1].strip()
        want_visits: int | None = None
        label = "Апгрейд"
        if code == "b1":
            want_visits = 4
            label = "Апгрейд: до BRONZE (1 визит)"
        elif code == "s2":
            want_visits = 13
            label = "Апгрейд: до SILVER (2 визита)"
        elif code == "s1":
            want_visits = 14
            label = "Апгрейд: до SILVER (1 визит)"
        elif code == "g2":
            want_visits = 33
            label = "Апгрейд: до GOLD (2 визита)"
        elif code == "g1":
            want_visits = 34
            label = "Апгрейд: до GOLD (1 визит)"

        targets: list[int] = []
        if want_visits is not None:
            for c in list_cards():
                try:
                    uid = int(c.user_id)
                except Exception:
                    continue
                if uid not in active:
                    continue
                if uid in staff:
                    continue
                if bool(getattr(c, "staff_gold", False)):
                    continue
                v = int(getattr(c, "visits", 0) or 0)
                if v == want_visits:
                    targets.append(uid)
        targets = sorted(set(targets))
        targets = filter_user_ids_by_broadcast_cooldown(targets, days=7)
        return (label, targets)

    # Backward-compat: old audience codes.
    if kind == "novis14":
        return _broadcast_targets("inactive:14")
    if kind == "novis30":
        return _broadcast_targets("inactive:30")
    return _broadcast_targets("all")


load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_SOURCE = (os.getenv("BOT_SOURCE", "lounge") or "lounge").strip().lower()
WELCOME_IMAGE_PATH = os.getenv("WELCOME_IMAGE_PATH", "assets/lounge_source.jpg")
# Inline preview image (should exist on VPS too). By default reuse the main welcome image.
INLINE_IMAGE_PATH = os.getenv("INLINE_IMAGE_PATH", WELCOME_IMAGE_PATH)
GUEST_CARD_URL = os.getenv("GUEST_CARD_URL", "https://example.com/guest-card")
MENU_URL = os.getenv("MENU_URL", "https://example.com/menu")
BOOKING_URL = os.getenv("BOOKING_URL", "https://example.com/booking")
LOCATION_URL = os.getenv("LOCATION_URL", "https://maps.google.com")
LOCATION_2GIS_URL = os.getenv("LOCATION_2GIS_URL", "https://2gis.ru/tyumen/geo/70000001110930565")
YANDEX_URL = os.getenv("YANDEX_URL", "https://yandex.ru/navi/org/na_grani/224347539954?si=q3cpc1dt8vaxpdygdhftk8wjxc")
NEWS_URL = os.getenv("NEWS_URL", "https://t.me/nagrani_lounge")
PROHVAT72_URL = os.getenv("PROHVAT72_URL", "https://t.me/prohvat72")
RACES_URL = os.getenv("RACES_URL", "https://t.me/na_grani_team")
LOCATION_ADDRESS = os.getenv("LOCATION_ADDRESS", "Мы находимся по адресу:\nФармана Салманова, 15")
LOCATION_LAT = float(os.getenv("LOCATION_LAT", "57.1583"))
LOCATION_LON = float(os.getenv("LOCATION_LON", "65.5572"))
BOOKING_ADMIN = os.getenv("BOOKING_ADMIN", "novopaha89")
BOOKING_TEXT = os.getenv(
    "BOOKING_TEXT",
    "Привет! Хочу забронировать столик.\n\n"
    "Данные для брони:\n"
    "• Дата: \n"
    "• Время: \n"
    "• Количество гостей: ",
)
# Support escaped newlines from .env values like "\\n".
BOOKING_TEXT = BOOKING_TEXT.replace("\\n", "\n")
LOCATION_ADDRESS = LOCATION_ADDRESS.replace("\\n", "\n")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in .env")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")


def _first_superadmin_id() -> int | None:
    raw = os.getenv("SUPERADMIN_IDS", "").strip()
    if not raw:
        return 864921585
    try:
        return int(raw.split(",")[0].strip())
    except Exception:
        return None


def _inline_cache_file() -> Path:
    return Path("data") / "inline_cache.json"


def _load_inline_cache() -> dict:
    try:
        return json.loads(_inline_cache_file().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_inline_cache(d: dict) -> None:
    try:
        Path("data").mkdir(parents=True, exist_ok=True)
        _inline_cache_file().write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def ensure_inline_photo_file_id() -> str | None:
    """
    Inline results can only show images by URL or cached file_id.
    We cache a local image by sending it once to the first superadmin chat.
    """
    global _inline_photo_file_id
    if _inline_photo_file_id:
        return _inline_photo_file_id

    # Prefer cached file_id if it matches current image file mtime.
    try:
        p = Path(INLINE_IMAGE_PATH)
        if not p.exists():
            p = Path(WELCOME_IMAGE_PATH)
        if not p.exists():
            return None
        mtime = int(p.stat().st_mtime)
        cached = _load_inline_cache()
        if (
            isinstance(cached, dict)
            and cached.get("path") == str(p)
            and int(cached.get("mtime") or 0) == mtime
            and isinstance(cached.get("photo_file_id"), str)
            and cached.get("photo_file_id")
        ):
            _inline_photo_file_id = str(cached["photo_file_id"])
            return _inline_photo_file_id
    except Exception:
        pass

    chat_id = _first_superadmin_id()
    if not chat_id:
        return None

    try:
        p = Path(INLINE_IMAGE_PATH)
        if not p.exists():
            p = Path(WELCOME_IMAGE_PATH)
        if not p.exists():
            return None

        with p.open("rb") as f:
            msg = bot.send_photo(chat_id, f, caption="cache", disable_notification=True)
        if not msg.photo:
            return None
        _inline_photo_file_id = msg.photo[-1].file_id
        try:
            _save_inline_cache(
                {"path": str(p), "mtime": int(p.stat().st_mtime), "photo_file_id": _inline_photo_file_id}
            )
        except Exception:
            pass
        try:
            bot.delete_message(chat_id, msg.message_id)
        except Exception:
            pass
        log.info("Cached inline photo file_id for %s", str(p))
        return _inline_photo_file_id
    except Exception as e:
        log.warning("Failed to cache inline photo: %s", e)
        return None


def _build_info_text() -> str:
    ver = "unknown"
    try:
        ver = (Path("VERSION").read_text(encoding="utf-8") or "").strip() or "unknown"
    except Exception:
        pass
    try:
        mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(Path(__file__).stat().st_mtime))
    except Exception:
        mtime = "unknown"
    return f"Build: <b>{escape(ver)}</b>\nSource: <b>{escape(BOT_SOURCE)}</b>\nFile: <code>bot.py</code> mtime {escape(mtime)}"

# Best-effort guards against duplicate UI actions.
_recent_callback_keys: dict[tuple[int, str, int], float] = {}
_main_menu_photo_file_id: str | None = None
_recent_message_keys: dict[tuple[int, int], float] = {}
_pending_admin_add: set[int] = set()
_pending_visit_add: dict[int, str] = {}  # chat_id -> back_cb
_pending_broadcast: dict[int, dict[str, object]] = {}  # chat_id -> state


def _pending_broadcast_file() -> Path:
    return Path("data") / "pending_broadcast.json"


def _load_pending_broadcast() -> None:
    """
    Best-effort persistence for the broadcast flow.
    Prevents losing state if polling restarts.
    """
    global _pending_broadcast
    try:
        p = _pending_broadcast_file()
        if not p.exists():
            return
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return
        now = time.time()
        out: dict[int, dict[str, object]] = {}
        for k, v in raw.items():
            try:
                chat_id = int(k)
            except Exception:
                continue
            if not isinstance(v, dict):
                continue
            ts = v.get("_ts")
            try:
                ts_f = float(ts) if ts is not None else 0.0
            except Exception:
                ts_f = 0.0
            # Expire after 2 hours.
            if ts_f and (now - ts_f) > 2 * 3600:
                continue
            out[chat_id] = v
        _pending_broadcast = out
    except Exception:
        return


def _save_pending_broadcast() -> None:
    try:
        Path("data").mkdir(parents=True, exist_ok=True)
        out: dict[str, dict[str, object]] = {}
        now = time.time()
        for chat_id, st in _pending_broadcast.items():
            if not isinstance(st, dict):
                continue
            # Don't persist huge/untrusted objects; keep only expected keys.
            d: dict[str, object] = {"_ts": now}
            for key in ("kind", "targets", "label", "stage", "src_chat_id", "src_message_id"):
                if key in st:
                    d[key] = st.get(key)
            out[str(int(chat_id))] = d
        _pending_broadcast_file().write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

def _callback_guard(call: telebot.types.CallbackQuery, window_s: float = 1.5) -> bool:
    """
    Prevent duplicate callback processing (double-taps, client retries, lag).
    Also answer callback ASAP to stop Telegram's loading spinner.
    """
    try:
        # Explicit empty text to avoid any client-side "updated" toasts.
        bot.answer_callback_query(call.id, text="", show_alert=False)
    except Exception:
        pass

    if call.message is None:
        # Inline-mode callbacks won't have a chat to reply to in this bot; but we still
        # answered the callback above to avoid "stuck" spinners.
        return False

    try:
        # If a broadcast flow is pending and user navigates anywhere outside broadcast UI,
        # cancel it immediately. This prevents "stuck" broadcast state from swallowing input.
        chat_id = call.message.chat.id
        data0 = (call.data or "").strip()
        if chat_id in _pending_broadcast and not data0.startswith("admin_broadcast"):
            _pending_broadcast.pop(chat_id, None)
            _save_pending_broadcast()

        user_id = call.from_user.id if call.from_user else 0
        data = call.data or ""
        msg_id = call.message.message_id
        key = (user_id, data, msg_id)
        now = time.time()
        last = _recent_callback_keys.get(key, 0.0)
        if now - last < window_s:
            return False
        _recent_callback_keys[key] = now
        if call.from_user:
            touch_user(
                UserInfo(
                    user_id=user_id,
                    first_name=call.from_user.first_name,
                    last_name=call.from_user.last_name,
                    username=call.from_user.username,
                )
            )
            sync_from_user(
                user_id,
                call.from_user.username,
                call.from_user.first_name,
                call.from_user.last_name,
            )
            # Staff accounts always have a dedicated staff card (no visits are added by this).
            if _is_staff(call.from_user):
                staff_level = _staff_level_label(user_id, call.from_user.username) or "ADMIN🐧"
                set_staff_gold_by_user_id(
                    user_id,
                    staff_level=staff_level,
                    username=call.from_user.username,
                    first_name=call.from_user.first_name,
                    last_name=call.from_user.last_name,
                )
            else:
                # If a user was previously staff and got demoted, drop staff card and
                # recalculate their LEVEL from visits.
                clear_staff_gold_by_user_id(user_id)
        inc_click(user_id)
    except Exception:
        # If we can't compute a key, still allow processing once.
        return True

    return True


def _message_guard(message: telebot.types.Message, window_s: float = 2.0) -> bool:
    """
    Prevent duplicate handling of the same incoming message/update.
    This fixes double responses when Telegram/client retries or polling restarts.
    """
    try:
        key = (message.chat.id, message.message_id)
        now = time.time()
        last = _recent_message_keys.get(key, 0.0)
        if now - last < window_s:
            return False
        _recent_message_keys[key] = now
        # Cheap bound to avoid unbounded growth.
        if len(_recent_message_keys) > 5000:
            cutoff = now - 60.0
            for k, ts in list(_recent_message_keys.items()):
                if ts < cutoff:
                    _recent_message_keys.pop(k, None)
    except Exception:
        return True
    try:
        if message.from_user:
            touch_user(
                UserInfo(
                    user_id=message.from_user.id,
                    first_name=message.from_user.first_name,
                    last_name=message.from_user.last_name,
                    username=message.from_user.username,
                )
            )
            sync_from_user(
                message.from_user.id,
                message.from_user.username,
                message.from_user.first_name,
                message.from_user.last_name,
            )
            # Staff accounts always have a dedicated staff card (no visits are added by this).
            if _is_staff(message.from_user):
                staff_level = _staff_level_label(message.from_user.id, message.from_user.username) or "ADMIN🐧"
                set_staff_gold_by_user_id(
                    message.from_user.id,
                    staff_level=staff_level,
                    username=message.from_user.username,
                    first_name=message.from_user.first_name,
                    last_name=message.from_user.last_name,
                )
            else:
                # If a user was previously staff and got demoted, drop staff card and
                # recalculate their LEVEL from visits.
                clear_staff_gold_by_user_id(message.from_user.id)
            inc_click(message.from_user.id)
    except Exception:
        pass
    return True


def _admin_label(username: str, first_name: str | None, last_name: str | None) -> str:
    """
    Button labels can't be HTML. Prefer real name if we have it, but always keep @username.
    """
    name = " ".join([x for x in [(first_name or "").strip(), (last_name or "").strip()] if x]).strip()
    if name:
        return f"{name} (@{username})"
    return f"@{username}"


def admins_list_keyboard(back_cb: str = "admin_admins") -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    admins = list_admins()
    for rec in admins:
        keyboard.row(
            InlineKeyboardButton(
                text=_admin_label(rec.username, rec.first_name, rec.last_name),
                callback_data=f"admin_view:{rec.username}",
            )
        )

    # Also show superadmins in this list (without any extra wording).
    admin_usernames = {normalize_username(r.username) for r in admins}
    for sid in sorted(_superadmin_ids()):
        stats = get_user_stats(int(sid)) or {}
        u = stats.get("username")
        if isinstance(u, str):
            u = normalize_username(u)
        else:
            u = ""
        # Avoid duplicates if a superadmin is also stored as an admin record.
        if u and u in admin_usernames:
            continue
        first = (stats.get("first_name") or "").strip() or None
        last = (stats.get("last_name") or "").strip() or None
        name = " ".join([x for x in [first or "", last or ""] if x]).strip()
        if u:
            label = _admin_label(u, first, last)
        else:
            label = name or str(sid)
        keyboard.row(InlineKeyboardButton(text=label, callback_data=f"admin_viewid:{int(sid)}"))
    keyboard.row(
        InlineKeyboardButton(text="👈Назад", callback_data=back_cb),
        InlineKeyboardButton(text="🏠 Домой", callback_data="back_to_main"),
    )
    return keyboard


def admin_view_readonly_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    keyboard.row(
        InlineKeyboardButton(text="👈Назад", callback_data="admin_admins_list"),
        InlineKeyboardButton(text="🏠 Домой", callback_data="back_to_main"),
    )
    return keyboard


def admin_view_keyboard(username: str) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    keyboard.row(InlineKeyboardButton(text="😔 Разжаловать", callback_data=f"admin_demote:{username}"))
    keyboard.row(
        InlineKeyboardButton(text="👈Назад", callback_data="admin_admins_list"),
        InlineKeyboardButton(text="🏠 Домой", callback_data="back_to_main"),
    )
    return keyboard


def admin_view_paged_keyboard(username: str, *, offset: int, total: int, page_size: int = 20) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    has_prev = offset > 0
    has_next = (offset + page_size) < total
    if has_prev or has_next:
        prev_off = max(0, offset - page_size)
        next_off = offset + page_size
        buttons = []
        if has_prev:
            buttons.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin_viewp:{username}:{prev_off}"))
        if has_next:
            buttons.append(InlineKeyboardButton(text="➡️", callback_data=f"admin_viewp:{username}:{next_off}"))
        keyboard.row(*buttons)
    keyboard.row(InlineKeyboardButton(text="😔 Разжаловать", callback_data=f"admin_demote:{username}"))
    keyboard.row(
        InlineKeyboardButton(text="👈Назад", callback_data="admin_admins_list"),
        InlineKeyboardButton(text="🏠 Домой", callback_data="back_to_main"),
    )
    return keyboard


def admin_viewid_paged_keyboard(user_id: int, *, offset: int, total: int, page_size: int = 20) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    has_prev = offset > 0
    has_next = (offset + page_size) < total
    if has_prev or has_next:
        prev_off = max(0, offset - page_size)
        next_off = offset + page_size
        buttons = []
        if has_prev:
            buttons.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin_viewidp:{int(user_id)}:{prev_off}"))
        if has_next:
            buttons.append(InlineKeyboardButton(text="➡️", callback_data=f"admin_viewidp:{int(user_id)}:{next_off}"))
        keyboard.row(*buttons)
    keyboard.row(
        InlineKeyboardButton(text="👈Назад", callback_data="admin_admins_list"),
        InlineKeyboardButton(text="🏠 Домой", callback_data="back_to_main"),
    )
    return keyboard


def _send_admin_view(chat_id: int, *, username: str, offset: int = 0) -> None:
    rec = next((r for r in list_admins() if r.username == username), None)
    if rec is None:
        bot.send_message(
            chat_id,
            "Админ не найден (возможно уже разжалован).",
            reply_markup=admins_list_keyboard("admin_admins"),
        )
        return

    name = " ".join([x for x in [(rec.first_name or "").strip(), (rec.last_name or "").strip()] if x]).strip()
    lines: list[str] = []
    lines.append("<b>Админ</b>")
    lines.append("")
    if name:
        lines.append(f"Имя: <b>{escape(name)}</b>")
    lines.append(f"Ник: <b>@{escape(rec.username)}</b>")

    total = 0
    if rec.user_id:
        v_today, v_7, v_30, v_total = admin_marked_visits_summary(int(rec.user_id), source=BOT_SOURCE)
        lines.append("")
        lines.append("<b>Рейтинг</b>")
        lines.append(f"Визитов за сегодня: <b>{v_today}</b>")
        lines.append(f"Визитов за 7 дней: <b>{v_7}</b>")
        lines.append(f"Визитов за 30 дней: <b>{v_30}</b>")
        lines.append(f"Всего визитов: <b>{v_total}</b>")

        lines.append("")
        lines.append("<b>Последние отмеченные</b>")
        recent, total = admin_marked_recent_clients_page(int(rec.user_id), source=BOT_SOURCE, offset=offset, limit=20)
        if not recent:
            lines.append("Нет данных.")
        else:
            for row in recent:
                uid = int(row["user_id"])
                stats = get_user_stats(uid) or {}
                uname = stats.get("username")
                if isinstance(uname, str):
                    uname = uname.strip().lstrip("@") or None
                else:
                    uname = None
                label = stats.get("first_name") or uname or str(uid)
                label = escape(str(label))
                card = find_card_by_user_id(uid)
                if card:
                    lines.append(
                        f'• <a href="{_tg_user_link(uid, uname)}">{label}</a> — карта <b>{escape(card.card_number)}</b>'
                    )
                else:
                    lines.append(f'• <a href="{_tg_user_link(uid, uname)}">{label}</a>')
    else:
        lines.append("")
        lines.append("Нет данных: админ ещё не писал боту (user_id неизвестен).")

    bot.send_message(
        chat_id,
        "\n".join(lines),
        reply_markup=admin_view_paged_keyboard(rec.username, offset=offset, total=total),
        disable_web_page_preview=True,
    )


def _send_admin_view_by_id(chat_id: int, *, user_id: int, offset: int = 0) -> None:
    uid = int(user_id)
    stats = get_user_stats(uid) or {}
    uname = stats.get("username")
    if isinstance(uname, str):
        uname = normalize_username(uname)
    else:
        uname = None

    first = (stats.get("first_name") or "").strip()
    last = (stats.get("last_name") or "").strip()
    name = " ".join([x for x in [first, last] if x]).strip()

    lines: list[str] = []
    lines.append("<b>Админ</b>")
    lines.append("")
    if name:
        lines.append(f"Имя: <b>{escape(name)}</b>")
    if uname:
        lines.append(f"Ник: <b>@{escape(uname)}</b>")
    else:
        lines.append(f"ID: <b>{uid}</b>")

    v_today, v_7, v_30, v_total = admin_marked_visits_summary(uid, source=BOT_SOURCE)
    lines.append("")
    lines.append("<b>Рейтинг</b>")
    lines.append(f"Визитов за сегодня: <b>{v_today}</b>")
    lines.append(f"Визитов за 7 дней: <b>{v_7}</b>")
    lines.append(f"Визитов за 30 дней: <b>{v_30}</b>")
    lines.append(f"Всего визитов: <b>{v_total}</b>")

    lines.append("")
    lines.append("<b>Последние отмеченные</b>")
    recent, total = admin_marked_recent_clients_page(uid, source=BOT_SOURCE, offset=offset, limit=20)
    if not recent:
        lines.append("Нет данных.")
    else:
        for row in recent:
            cuid = int(row["user_id"])
            cstats = get_user_stats(cuid) or {}
            cuname = cstats.get("username")
            if isinstance(cuname, str):
                cuname = cuname.strip().lstrip("@") or None
            else:
                cuname = None
            clabel = cstats.get("first_name") or cuname or str(cuid)
            clabel = escape(str(clabel))
            card = find_card_by_user_id(cuid)
            if card:
                lines.append(
                    f'• <a href="{_tg_user_link(cuid, cuname)}">{clabel}</a> — карта <b>{escape(card.card_number)}</b>'
                )
            else:
                lines.append(f'• <a href="{_tg_user_link(cuid, cuname)}">{clabel}</a>')

    bot.send_message(
        chat_id,
        "\n".join(lines),
        reply_markup=admin_viewid_paged_keyboard(uid, offset=offset, total=total),
        disable_web_page_preview=True,
    )


def admin_visit_done_keyboard(back_cb: str) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    keyboard.row(InlineKeyboardButton(text="🏠 Домой", callback_data="back_to_main"))
    return keyboard


def send_main_menu(chat_id: int, *, user: telebot.types.User | None) -> None:
    superadmin = is_superadmin(user.id if user else None)
    admin = (not superadmin) and _is_admin(user)
    keyboard = main_inline_keyboard(superadmin=superadmin, admin=admin)
    image_path = Path(WELCOME_IMAGE_PATH)

    if image_path.exists():
        global _main_menu_photo_file_id
        try:
            # Fast path: reuse cached file_id so Telegram doesn't re-upload the image.
            if _main_menu_photo_file_id:
                bot.send_photo(chat_id, _main_menu_photo_file_id, reply_markup=keyboard)
                return
        except Exception:
            _main_menu_photo_file_id = None

        with image_path.open("rb") as image:
            msg = bot.send_photo(chat_id, image, reply_markup=keyboard)
        try:
            if msg.photo:
                _main_menu_photo_file_id = msg.photo[-1].file_id
        except Exception:
            pass
    else:
        bot.send_message(
            chat_id,
            "Загрузите файл логотипа в assets/lounge_source.jpg, чтобы отправлять стартовую картинку.",
            reply_markup=keyboard,
        )


def user_display_name(user: telebot.types.User | None) -> str:
    if user is None:
        return "Гость"
    if user.first_name:
        return escape(user.first_name)
    if user.username:
        return escape(user.username)
    return "Гость"


def _extract_username_from_inline_query(text: str) -> str | None:
    s = (text or "").strip()
    if not s:
        return None
    # Accept "@name", "t.me/name", "https://t.me/name", "telegram.me/name"
    s = s.replace("\n", " ").strip()
    m = re.search(r"@([A-Za-z0-9_]{5,32})", s)
    if m:
        return m.group(1)
    m = re.search(r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{5,32})", s, flags=re.IGNORECASE)
    if m:
        return m.group(1)
    # If user typed just the username without @
    if re.fullmatch(r"[A-Za-z0-9_]{5,32}", s):
        return s
    return None


def _level_for_visits(total_visits: int) -> tuple[str, int]:
    """
    Temporary leveling rules (we can expand later).
    Returns (level_label, discount_percent).
    """
    # For now: always IRON with 3% until you provide full rules.
    return ("IRON⚙️", 3)


def _card_number_for_user(user_id: int) -> str:
    # Stable 4-digit card number (demo): last 4 digits of user_id.
    return f"{user_id % 10000:04d}"


def level_card_inline_text(*, username: str, user_id: int) -> str:
    _v7, _v30, vtotal = user_visit_counts(user_id)
    card = find_card_by_user_id(user_id)
    if card is None:
        # No registered card, no inline result should be returned (handled upstream).
        level_label, discount = _level_for_visits(vtotal)
        card_number = _card_number_for_user(user_id)
    else:
        level_label = card.level
        discount = card.discount
        card_number = card.card_number

    lvl_override = _staff_level_label(user_id, username)
    if lvl_override:
        level_label = lvl_override

    total_disc, bonus_disc = total_discount_for_user(user_id, int(discount))
    medals = medals_for_user(user_id)
    medals_line = f"Всего медалей: {medals}\n" if medals else ""
    u = username.strip().lstrip("@")
    return (
        f"<b>КАРТА LEVEL</b> <b>@{escape(u)}</b>\n\n"
        f"Уровень: <b>{escape(str(level_label))}</b>\n"
        f"Номер карты: <b>{escape(str(card_number))}</b>\n\n"
        f"Всего визитов: <b>{int(vtotal)}</b>\n"
        f"{medals_line}"
        f"Общая скидка: <b>{int(total_disc)}%</b>"
    )


def send_level_menu(chat_id: int, user: telebot.types.User | None, user_id: int | None) -> None:
    display_name = user_display_name(user)
    if user_id is not None and is_registered(user_id):
        ensure_level_card(
            user_id,
            username=(user.username if user else None),
            first_name=(user.first_name if user else None),
            last_name=(user.last_name if user else None),
        )
        bot.send_message(
            chat_id,
            guest_card_text(display_name, user_id=user_id),
            reply_markup=guest_card_registered_inline_keyboard(),
            disable_web_page_preview=True,
        )
        return

    bot.send_message(
        chat_id,
        "Карта <b>LEVEL</b> - это твой личный профиль гостя. Здесь растёт уровень скидки и не только…",
        reply_markup=guest_card_inline_keyboard(),
    )


def level_card_message_text(user: telebot.types.User | None, user_id: int | None) -> str:
    display_name = user_display_name(user)
    if user_id is not None and is_registered(user_id):
        ensure_level_card(
            user_id,
            username=(user.username if user else None),
            first_name=(user.first_name if user else None),
            last_name=(user.last_name if user else None),
        )
        return guest_card_text(display_name, user_id=user_id)
    return (
        "Карта <b>LEVEL</b> - это твой личный профиль гостя. Здесь растёт уровень скидки и не только…"
    )

def level_visits_text() -> str:
    return (
        "<b>🧾 О ВИЗИТАХ</b>\n\n"
        "Чтобы засчитались <b>скидка</b> и <b>визит</b>, нужно назвать номер карты <b>LEVEL</b> администратору\n\n"
        "Визит засчитывается при условии чека от <b>1000₽</b>\n"
        "Засчитать визит можно не чаще <b>1 раза в день</b> "
        "(специально обученный админ обновляет счетчик в 6 утра)\n\n"
        "Кстати, визиты <b>не сгорают</b>\n"
        f'Визиты общие: их можно засчитать и в баре, и в <b><a href="{PROHVAT72_URL}">Прохват72</a></b>\n\n'
        "<b>🏆 О РЕЙТИНГЕ</b>\n\n"
        "<b>Как считается</b>\n"
        "• топ-3 гостей по количеству визитов за месяц в баре\n\n"
        "<b>Бонус к скидке</b>\n"
        "• 🥇 +10% на следующий месяц\n"
        "• 🥈 +6% на следующий месяц\n"
        "• 🥉 +3% на следующий месяц\n"
        "• общая скидка = скидка LEVEL + бонус рейтинга\n\n"
        "<b>🔥 О РОЗЫГРЫШЕ</b>\n\n"
        "<b>Условия простые:</b>\n"
        "• Участвуют все владельцы карт <b>SILVER</b> и <b>GOLD</b>\n"
        "• У гостей с уровнем <b>GOLD</b> в 2 раза больше шансов на победу\n"
    )


def level_giveaway_text() -> str:
    bot_username = (os.getenv("BOT_USERNAME", "") or "").strip().lstrip("@")
    pitbike_link = f"https://t.me/{bot_username}?start=pitbike" if bot_username else ""
    pitbike_word = f'<b><a href="{pitbike_link}">питбайк</a></b>' if pitbike_link else "<b>питбайк</b>"
    return (
        "<b>РОЗЫГРЫШ</b>\n\n"
        "В конце года разыгрываем призы среди гостей с картами <b>LEVEL</b> уровня <b>SILVER</b> и <b>GOLD</b>\n\n"
        f"🥇 Тот самый {pitbike_word}\n"
        f"🥈 Сертификат <b><a href=\"{PROHVAT72_URL}\">Прохват72</a></b>\n"
        f"🥉 Сертификат <b><a href=\"{NEWS_URL}\">На Грани Lounge</a></b>\n\n"
        "Повышай уровень и участвуй в розыгрыше"
    )


def _level_rating_name(card: LevelCard) -> tuple[str, str | None]:
    uname = (card.username or "").strip().lstrip("@") or None
    name = " ".join([x for x in [(card.first_name or "").strip(), (card.last_name or "").strip()] if x]).strip()
    if name:
        return (name, uname)
    if uname:
        return (f"@{uname}", uname)
    return ("Гость", None)


def level_rating_text(*, superadmin: bool) -> str:
    tz = None
    try:
        tz = ZoneInfo("Asia/Tyumen")
    except Exception:
        tz = datetime.now().astimezone().tzinfo
    now = datetime.now(tz)  # type: ignore[arg-type]

    # Leaderboard launches from March 1st. Before that, show empty slots.
    LAUNCH = datetime(2026, 3, 1, 0, 0, 0, tzinfo=tz)  # type: ignore[arg-type]

    month_names = {
        1: "январь",
        2: "февраль",
        3: "март",
        4: "апрель",
        5: "май",
        6: "июнь",
        7: "июль",
        8: "август",
        9: "сентябрь",
        10: "октябрь",
        11: "ноябрь",
        12: "декабрь",
    }

    def _month_name(m: int) -> str:
        return str(month_names.get(int(m), ""))

    def _next_month(y: int, m: int) -> tuple[int, int]:
        y = int(y)
        m = int(m)
        if m >= 12:
            return (y + 1, 1)
        return (y, m + 1)

    # Before launch: always show March (starts March 1st).
    if now < LAUNCH:
        show_month_year, show_month = 2026, 3
    else:
        show_month_year, show_month = now.year, now.month
    m_nom = _month_name(show_month)
    _ny, next_m = _next_month(show_month_year, show_month)
    next_m_name = _month_name(next_m)

    staff = _staff_user_ids_known()
    rows: list[dict] = []
    if now >= LAUNCH:
        rows = top_users_by_visits_in_month(show_month_year, show_month, source=BOT_SOURCE, limit=3, active_only=True)
        rows = [r for r in rows if is_eligible_for_competitions(int(r.get("user_id") or 0))][:3]

    def _place_line(place: int) -> str:
        row = rows[place - 1] if 0 <= (place - 1) < len(rows) else None
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        prefix = medals.get(place, f"{place}.")
        if not row:
            return f"{prefix} - свободно"
        uid = int(row.get("user_id") or 0)
        # Do not make winners clickable (avoid random users DM'ing them).
        # Use Telegram profile name (cached in admin_stats when user interacts with the bot).
        stats = get_user_stats(uid) or {}
        first = (stats.get("first_name") or "").strip()
        last = (stats.get("last_name") or "").strip()
        full = " ".join([x for x in [first, last] if x]).strip()
        label = full or first or str(uid)
        if superadmin:
            uname = stats.get("username")
            if isinstance(uname, str):
                uname = uname.strip().lstrip("@") or None
            else:
                uname = None
            link = _tg_user_link(uid, uname)
            return f'{prefix} - <a href="{link}"><b>{escape(str(label))}</b></a>'
        return f"{prefix} - <b>{escape(str(label))}</b>"

    lines: list[str] = []
    lines.append("<b>РЕЙТИНГ ГОСТЕЙ</b>")
    lines.append("")
    lines.append(f"Топ по визитам за <b>{escape(m_nom)}</b> в баре")
    if now < LAUNCH:
        lines.append("(Стартуем 1 марта)")
    lines.append("")
    lines.append(_place_line(1))
    lines.append(_place_line(2))
    lines.append(_place_line(3))
    lines.append("")
    lines.append("Стань первым лидером бара.")
    lines.append("")
    lines.append("<b>Награды месяца:</b>")
    lines.append("Топ-3 получают <b>настоящие</b> медали")
    lines.append(f"Дополнительную <b>скидку</b> <b>на {escape(next_m_name)}</b>")
    return "\n".join(lines)


def send_location_menu(chat_id: int) -> None:
    bot.send_message(
        chat_id,
        LOCATION_ADDRESS,
        reply_markup=location_inline_keyboard(),
    )


def send_food_menu(chat_id: int) -> None:
    if not is_menu_allowed(chat_id):
        bot.send_message(chat_id, "Меню временно недоступно.")
        return
    bot.send_message(
        chat_id,
        "<b>КАЛЬЯН</b>\n\n"
        "<b>До 17:00 - 1 000₽</b>\n"
        "<b>После 17:00 - 1 400₽</b>\n\n"
        "Соберём вкус и крепость под тебя. Работаем на премиальных табаках\n\n"
        "Если за столом более четырёх гостей, необходимо заказать 2 кальяна единовременно, если более шести - 3 кальяна\n\n"
        "С 19:00 действует правило - 2 часа на один кальян",
        reply_markup=menu_inline_keyboard(active="menu_hookah"),
        disable_web_page_preview=True,
    )


def send_booking_menu(chat_id: int) -> None:
    bot.send_message(
        chat_id,
        "Нажмите кнопку ниже, чтобы открыть чат бронирования:",
        reply_markup=booking_inline_keyboard(),
    )


@bot.message_handler(commands=["start"])
def handle_start(message: telebot.types.Message) -> None:
    if not _message_guard(message):
        return
    log.info("cmd /start from user_id=%s chat_id=%s", getattr(message.from_user, "id", None), message.chat.id)

    # Deep-link: open the interior gallery on the pitbike photo.
    try:
        payload = (message.text or "").split(maxsplit=1)[1].strip()
    except Exception:
        payload = ""
    if payload == "pitbike":
        # Deep-link: show the pitbike photo directly (no gallery UI).
        send_pitbike_photo(message.chat.id)
        return

    send_main_menu(message.chat.id, user=message.from_user)


@bot.message_handler(commands=["level"])
def handle_level_command(message: telebot.types.Message) -> None:
    if not _message_guard(message):
        return
    log.info("cmd /level from user_id=%s chat_id=%s", getattr(message.from_user, "id", None), message.chat.id)
    user_id = message.from_user.id if message.from_user else None
    try:
        send_level_menu(message.chat.id, message.from_user, user_id)
    except Exception as e:
        bot.send_message(message.chat.id, f"Ошибка при открытии LEVEL: <code>{escape(str(e))}</code>")


@bot.message_handler(commands=["menu"])
def handle_menu_command(message: telebot.types.Message) -> None:
    if not _message_guard(message):
        return
    send_food_menu(message.chat.id)


@bot.message_handler(commands=["booking"])
def handle_booking_command(message: telebot.types.Message) -> None:
    if not _message_guard(message):
        return
    # Commands can't auto-open another chat; send the deep-link directly without extra menus.
    bot.send_message(
        message.chat.id,
        f'🛋 <a href="{booking_deep_link()}">Открыть чат бронирования</a>',
        disable_web_page_preview=True,
    )


@bot.message_handler(commands=["location"])
def handle_location_command(message: telebot.types.Message) -> None:
    if not _message_guard(message):
        return
    send_location_menu(message.chat.id)


@bot.message_handler(commands=["version", "ver", "v"])
def handle_version_command(message: telebot.types.Message) -> None:
    if not _message_guard(message):
        return
    # Visible to anyone; it's safe and helps verify which build is running.
    bot.send_message(message.chat.id, _build_info_text(), disable_web_page_preview=True)


@bot.callback_query_handler(func=lambda call: call.data == "main_admin")
def handle_admin_main(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    if not is_superadmin(call.from_user.id if call.from_user else None):
        return

    bot.send_message(
        call.message.chat.id,
        "<b>Меню супер-админа</b>",
        reply_markup=admin_menu_keyboard(),
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_menu")
def handle_admin_menu(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    if not is_superadmin(call.from_user.id if call.from_user else None):
        return

    _pending_admin_add.discard(call.message.chat.id)
    _pending_visit_add.pop(call.message.chat.id, None)
    bot.send_message(
        call.message.chat.id,
        "<b>Меню супер-админа</b>",
        reply_markup=admin_menu_keyboard(),
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_stats")
def handle_admin_stats(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    if not is_superadmin(call.from_user.id if call.from_user else None):
        return

    visits_today, visits_7, visits_30 = visit_counts(source=BOT_SOURCE)
    subs_today, subs_7, subs_30 = subscribed_counts()
    # Keep unsubscribed_counts() imported for later, but we don't show it in UI now.
    top = top_by_clicks(10)

    lines: list[str] = []
    lines.append("📊 <b>Статистика</b>")
    lines.append("")
    lines.append(f"Визитов за сегодня: <b>{visits_today}</b>")
    lines.append(f"Визитов за 7 дней: <b>{visits_7}</b>")
    lines.append(f"Визитов за 30 дней: <b>{visits_30}</b>")
    lines.append("")
    lines.append(f"Подписались за сегодня: <b>{subs_today}</b>")
    lines.append(f"Подписались за 7 дней: <b>{subs_7}</b>")
    lines.append(f"Подписались за 30 дней: <b>{subs_30}</b>")

    # Cards issued by LEVEL tier (computed from current visits; exclude staff cards).
    cards = list_cards()
    staff_ids = _staff_user_ids_known()
    c_iron = 0
    c_bronze = 0
    c_silver = 0
    c_gold = 0
    for c in cards:
        try:
            uid = int(getattr(c, "user_id", 0) or 0)
            if uid in staff_ids:
                continue
            lvl, _disc = tier_for_visits(int(getattr(c, "visits", 0) or 0))
        except Exception:
            lvl = "IRON⚙️"
        if str(lvl).startswith("GOLD"):
            c_gold += 1
        elif str(lvl).startswith("SILVER"):
            c_silver += 1
        elif str(lvl).startswith("BRONZE"):
            c_bronze += 1
        else:
            c_iron += 1

    lines.append("")
    lines.append("🪪 <b>Выдано карт</b> <b>LEVEL</b>")
    lines.append(f"<b>⚙️ IRON: {c_iron}</b>")
    lines.append(f"<b>🥉 BRONZE: {c_bronze}</b>")
    lines.append(f"<b>🥈 SILVER: {c_silver}</b>")
    lines.append(f"<b>🥇 GOLD: {c_gold}</b>")
    lines.append("")
    lines.append("<b>Топ подписчиков по кликам (ТОП-10)</b>")

    for i, row in enumerate(top, start=1):
        uid = int(row["user_id"])
        name = row.get("first_name") or row.get("username") or str(uid)
        name = escape(str(name))
        username = row.get("username")
        if isinstance(username, str):
            username = username.strip().lstrip("@") or None
        else:
            username = None
        clicks = int(row.get("clicks", 0) or 0)
        visits = int(row.get("visits", 0) or 0)
        prefix = _rank_prefix(i)
        lines.append(
            f'{prefix}<a href="{_tg_user_link(uid, username)}"><b>{name}</b></a> - кликов <b>{clicks}</b>, визитов <b>{visits}</b>'
        )

    lines.append("")
    lines.append("<b>Топ админов по визитам</b>")
    admin_rows = top_admins_by_marked_visits(source=BOT_SOURCE, days=30, limit=100)
    if not admin_rows:
        lines.append("Нет данных.")
    else:
        # Map admin_id -> (username, first, last) from our admin list (if known).
        admin_meta = {}
        for rec in list_admins():
            if rec.user_id is None:
                continue
            admin_meta[int(rec.user_id)] = (rec.username, rec.first_name, rec.last_name)

        for i, row in enumerate(admin_rows, start=1):
            aid = int(row["admin_id"])
            v = int(row["visits"])
            meta = admin_meta.get(aid)
            if meta:
                u, first, last = meta
            else:
                stats = get_user_stats(aid) or {}
                u = stats.get("username")
                first = stats.get("first_name")
                last = None

            u = (u or "").strip().lstrip("@") or None
            if u:
                label = f"@{u}"
            else:
                label = "Без ника"
            prefix = _rank_prefix(i)
            lines.append(f'{prefix}<a href="{_tg_user_link(aid, u)}"><b>{escape(label)}</b></a> - визитов <b>{v}</b>')

    bot.send_message(
        call.message.chat.id,
        "\n".join(lines),
        reply_markup=admin_bottom_keyboard("admin_menu"),
        disable_web_page_preview=True,
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_broadcast")
def handle_admin_broadcast(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    if not is_superadmin(call.from_user.id if call.from_user else None):
        return

    _pending_broadcast.pop(call.message.chat.id, None)
    _save_pending_broadcast()
    bot.send_message(
        call.message.chat.id,
        "<b>Рассылка</b>\n\nВыбери, кому отправлять:",
        reply_markup=admin_broadcast_root_keyboard(),
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_broadcast_create")
def handle_admin_broadcast_create(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    if not is_superadmin(call.from_user.id if call.from_user else None):
        return

    # Backward-compat: old UI entry.
    _pending_broadcast.pop(call.message.chat.id, None)
    _save_pending_broadcast()
    bot.send_message(
        call.message.chat.id,
        "<b>Рассылка</b>\n\nВыбери, кому отправлять:",
        reply_markup=admin_broadcast_root_keyboard(),
    )


@bot.callback_query_handler(func=lambda call: (call.data or "").startswith("admin_broadcast_root:"))
def handle_admin_broadcast_root(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    if not is_superadmin(call.from_user.id if call.from_user else None):
        return

    action = (call.data or "").split(":", 1)[1].strip()
    _pending_broadcast.pop(call.message.chat.id, None)
    _save_pending_broadcast()

    if action == "inactive":
        bot.send_message(
            call.message.chat.id,
            "<b>Давно не был</b>\n\nВыбери период:",
            reply_markup=admin_broadcast_inactive_keyboard(),
        )
        return

    if action == "upgrade":
        bot.send_message(
            call.message.chat.id,
            "<b>Апгрейд</b>\n\nВыбери сегмент:",
            reply_markup=admin_broadcast_upgrade_keyboard(),
        )
        return

    if action == "contest":
        label, targets = _broadcast_targets("contest")
        _pending_broadcast[call.message.chat.id] = {"kind": "contest", "targets": targets, "label": label}
        _save_pending_broadcast()
        bot.send_message(
            call.message.chat.id,
            f"<b>Рассылка</b>\n\nКому: <b>{escape(label)}</b>\nПолучателей: <b>{len(targets)}</b>",
            reply_markup=admin_broadcast_confirm_keyboard("admin_broadcast"),
            disable_web_page_preview=True,
        )
        return

    # action == "all"
    label, targets = _broadcast_targets("all")
    _pending_broadcast[call.message.chat.id] = {"kind": "all", "targets": targets, "label": label}
    _save_pending_broadcast()
    bot.send_message(
        call.message.chat.id,
        f"<b>Рассылка</b>\n\nКому: <b>{escape(label)}</b>\nПолучателей: <b>{len(targets)}</b>",
        reply_markup=admin_broadcast_confirm_keyboard("admin_broadcast"),
        disable_web_page_preview=True,
    )


@bot.callback_query_handler(func=lambda call: (call.data or "").startswith("admin_broadcast_inactive:"))
def handle_admin_broadcast_inactive(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    if not is_superadmin(call.from_user.id if call.from_user else None):
        return
    days_raw = (call.data or "").split(":", 1)[1].strip()
    try:
        days = int(days_raw)
    except Exception:
        days = 14
    kind = f"inactive:{days}"
    label, targets = _broadcast_targets(kind)
    _pending_broadcast[call.message.chat.id] = {"kind": kind, "targets": targets, "label": label}
    _save_pending_broadcast()
    bot.send_message(
        call.message.chat.id,
        f"<b>Рассылка</b>\n\nКому: <b>{escape(label)}</b>\nПолучателей: <b>{len(targets)}</b>",
        reply_markup=admin_broadcast_confirm_keyboard("admin_broadcast_root:inactive"),
        disable_web_page_preview=True,
    )


@bot.callback_query_handler(func=lambda call: (call.data or "").startswith("admin_broadcast_inactive_range:"))
def handle_admin_broadcast_inactive_range(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    if not is_superadmin(call.from_user.id if call.from_user else None):
        return

    rest = (call.data or "").split(":", 1)[1].strip()
    try:
        a, b = rest.split(":", 1)
        min_days = int(a.strip())
        max_days = int(b.strip())
    except Exception:
        min_days = 7
        max_days = 14

    kind = f"inactive_range:{min_days}:{max_days}"
    label, targets = _broadcast_targets(kind)
    _pending_broadcast[call.message.chat.id] = {"kind": kind, "targets": targets, "label": label}
    _save_pending_broadcast()
    bot.send_message(
        call.message.chat.id,
        f"<b>Рассылка</b>\n\nКому: <b>{escape(label)}</b>\nПолучателей: <b>{len(targets)}</b>",
        reply_markup=admin_broadcast_confirm_keyboard("admin_broadcast_root:inactive"),
        disable_web_page_preview=True,
    )


@bot.callback_query_handler(func=lambda call: (call.data or "").startswith("admin_broadcast_upgrade:"))
def handle_admin_broadcast_upgrade(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    if not is_superadmin(call.from_user.id if call.from_user else None):
        return
    code = (call.data or "").split(":", 1)[1].strip()
    kind = f"upgrade:{code}"
    label, targets = _broadcast_targets(kind)
    _pending_broadcast[call.message.chat.id] = {"kind": kind, "targets": targets, "label": label}
    _save_pending_broadcast()
    bot.send_message(
        call.message.chat.id,
        f"<b>Рассылка</b>\n\nКому: <b>{escape(label)}</b>\nПолучателей: <b>{len(targets)}</b>",
        reply_markup=admin_broadcast_confirm_keyboard("admin_broadcast_root:upgrade"),
        disable_web_page_preview=True,
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_broadcast_make")
def handle_admin_broadcast_make(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    if not is_superadmin(call.from_user.id if call.from_user else None):
        return

    state = _pending_broadcast.get(call.message.chat.id) or {}
    targets = state.get("targets")
    label = state.get("label") or "Аудитория"
    if not isinstance(targets, list):
        targets = []
    if not targets:
        _pending_broadcast.pop(call.message.chat.id, None)
        bot.send_message(call.message.chat.id, "Получателей нет.", reply_markup=admin_broadcast_root_keyboard())
        return

    # Now awaiting a ready-to-send post (forward/copy any message).
    _pending_broadcast[call.message.chat.id] = {
        "kind": state.get("kind"),
        "targets": targets,
        "label": label,
        "stage": "await_post",
    }
    _save_pending_broadcast()
    bot.send_message(
        call.message.chat.id,
        f"<b>Рассылка</b>\n\n"
        f"Кому: <b>{escape(str(label))}</b>\n"
        f"Получателей: <b>{len(targets)}</b>\n\n"
        "Перешли готовый пост сюда (текст/фото/видео и т.д.).\n"
        "Бот скопирует его гостям.",
        reply_markup=admin_broadcast_cancel_keyboard(),
        disable_web_page_preview=True,
    )


@bot.callback_query_handler(func=lambda call: (call.data or "").startswith("admin_broadcast_aud:"))
def handle_admin_broadcast_audience(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    if not is_superadmin(call.from_user.id if call.from_user else None):
        return

    # Backward-compat: old audience picker buttons map to the new "confirm -> create" flow.
    kind0 = (call.data or "").split(":", 1)[1].strip()
    if kind0 == "all":
        kind = "all"
        back_cb = "admin_broadcast"
    elif kind0 == "novis14":
        kind = "inactive:14"
        back_cb = "admin_broadcast_root:inactive"
    elif kind0 == "novis30":
        kind = "inactive:30"
        back_cb = "admin_broadcast_root:inactive"
    else:
        kind = "all"
        back_cb = "admin_broadcast"

    label, targets = _broadcast_targets(kind)
    _pending_broadcast[call.message.chat.id] = {"kind": kind, "targets": targets, "label": label}
    bot.send_message(
        call.message.chat.id,
        f"<b>Рассылка</b>\n\nКому: <b>{escape(label)}</b>\nПолучателей: <b>{len(targets)}</b>",
        reply_markup=admin_broadcast_confirm_keyboard(back_cb),
        disable_web_page_preview=True,
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_broadcast_cancel")
def handle_admin_broadcast_cancel(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    if not is_superadmin(call.from_user.id if call.from_user else None):
        return
    _pending_broadcast.pop(call.message.chat.id, None)
    _save_pending_broadcast()
    bot.send_message(call.message.chat.id, "Отменено.", reply_markup=admin_broadcast_root_keyboard())


@bot.callback_query_handler(func=lambda call: call.data == "admin_broadcast_replace")
def handle_admin_broadcast_replace(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    if not is_superadmin(call.from_user.id if call.from_user else None):
        return

    state = _pending_broadcast.get(call.message.chat.id) or {}
    targets = state.get("targets")
    label = state.get("label") or "Аудитория"
    if not isinstance(targets, list):
        targets = []
    _pending_broadcast[call.message.chat.id] = {
        "kind": state.get("kind"),
        "targets": targets,
        "label": label,
        "stage": "await_post",
    }
    _save_pending_broadcast()
    bot.send_message(
        call.message.chat.id,
        f"<b>Рассылка</b>\n\nКому: <b>{escape(str(label))}</b>\nПолучателей: <b>{len(targets)}</b>\n\nПерешли другой пост сюда.",
        reply_markup=admin_broadcast_cancel_keyboard(),
        disable_web_page_preview=True,
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_broadcast_send")
def handle_admin_broadcast_send(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    if not is_superadmin(call.from_user.id if call.from_user else None):
        return

    state = _pending_broadcast.get(call.message.chat.id) or {}
    targets = state.get("targets")
    if not isinstance(targets, list) or not targets:
        _pending_broadcast.pop(call.message.chat.id, None)
        _save_pending_broadcast()
        bot.send_message(
            call.message.chat.id,
            "Сессия рассылки сброшена.\n\nОткрой <b>Рассылка</b> и выбери аудиторию заново.",
            reply_markup=admin_broadcast_root_keyboard(),
        )
        return

    src_chat_id = state.get("src_chat_id")
    src_message_id = state.get("src_message_id")
    if not isinstance(src_chat_id, int) or not isinstance(src_message_id, int):
        bot.send_message(
            call.message.chat.id,
            "Сначала перешли пост для рассылки сообщением.",
            reply_markup=admin_broadcast_cancel_keyboard(),
        )
        return

    # Never broadcast to staff accounts.
    staff = _staff_user_ids_known()
    targets = [int(uid) for uid in targets if int(uid) not in staff]
    if not targets:
        _pending_broadcast.pop(call.message.chat.id, None)
        _save_pending_broadcast()
        bot.send_message(call.message.chat.id, "Получателей нет.", reply_markup=admin_broadcast_root_keyboard())
        return

    kind = str(state.get("kind") or "").strip().lower()
    # All broadcasts except contest are limited to once per 7 days per user.
    if kind and kind != "contest":
        targets = filter_user_ids_by_broadcast_cooldown(targets, days=7)
        if not targets:
            _pending_broadcast.pop(call.message.chat.id, None)
            _save_pending_broadcast()
            bot.send_message(call.message.chat.id, "Получателей нет.", reply_markup=admin_broadcast_root_keyboard())
            return

    _pending_broadcast.pop(call.message.chat.id, None)
    _save_pending_broadcast()
    bot.send_message(call.message.chat.id, f"Начинаю рассылку. Получателей: <b>{len(targets)}</b>")

    sent = 0
    failed = 0
    for uid in targets:
        try:
            bot.copy_message(int(uid), int(src_chat_id), int(src_message_id))
            try:
                record_broadcast_sent(int(uid), kind=(kind or "broadcast"), source=BOT_SOURCE)
            except Exception:
                pass
            sent += 1
        except Exception:
            failed += 1
        time.sleep(0.05)

    bot.send_message(
        call.message.chat.id,
        f"Готово.\nОтправлено: <b>{sent}</b>\nОшибок: <b>{failed}</b>",
        reply_markup=admin_broadcast_root_keyboard(),
        disable_web_page_preview=True,
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_rules" or (call.data or "").startswith("admin_rules:"))
def handle_admin_rules(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    if not is_superadmin(call.from_user.id if call.from_user else None):
        return

    data = call.data or "admin_rules"
    tab = "points"
    if ":" in data:
        _p = data.split(":", 1)[1].strip()
        if _p in {"points", "visits", "rating", "broadcast", "build"}:
            tab = _p

    text = admin_rules_text(tab)
    kb = admin_rules_keyboard(tab)

    # Try edit in-place to avoid extra messages.
    try:
        bot.edit_message_text(
            text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception:
        bot.send_message(
            call.message.chat.id,
            text,
            reply_markup=kb,
            disable_web_page_preview=True,
        )


@bot.callback_query_handler(func=lambda call: call.data == "admin_admins")
def handle_admin_admins(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    if not is_superadmin(call.from_user.id if call.from_user else None):
        return

    _pending_admin_add.discard(call.message.chat.id)
    _pending_visit_add.pop(call.message.chat.id, None)
    bot.send_message(
        call.message.chat.id,
        "<b>Управление админами</b>",
        reply_markup=admins_manage_keyboard(),
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_admins_list")
def handle_admin_admins_list(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    if not is_superadmin(call.from_user.id if call.from_user else None):
        return

    bot.send_message(
        call.message.chat.id,
        "<b>Админы</b>",
        reply_markup=admins_list_keyboard("admin_admins"),
        disable_web_page_preview=True,
    )


@bot.callback_query_handler(func=lambda call: call.data == "admin_admins_add")
def handle_admin_admins_add(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    if not is_superadmin(call.from_user.id if call.from_user else None):
        return

    _pending_admin_add.add(call.message.chat.id)
    bot.send_message(
        call.message.chat.id,
        "Пришли <b>@username</b> нового админа (обязательно с никнеймом в Telegram).",
        reply_markup=admin_bottom_keyboard("admin_admins"),
    )


@bot.message_handler(func=lambda m: m.chat is not None and m.chat.id in _pending_admin_add)
def handle_admin_add_input(message: telebot.types.Message) -> None:
    if not _message_guard(message):
        return
    if not is_superadmin(message.from_user.id if message.from_user else None):
        _pending_admin_add.discard(message.chat.id)
        return
    text = (message.text or "").strip()
    username = normalize_username(text)
    # Telegram username: 5-32 chars, latin letters/digits/_ (keep it strict).
    if not (5 <= len(username) <= 32) or not all((c.isalnum() or c == "_") for c in username):
        bot.send_message(message.chat.id, "Нужен корректный <b>@username</b>, например <code>@novopaha89</code>.")
        return

    add_admin_by_username(username)
    _pending_admin_add.discard(message.chat.id)
    # If we already know this admin's user_id, force staff card right away.
    try:
        uid = find_user_id_by_username(username)
        if uid is not None:
            set_staff_gold_by_user_id(uid, staff_level="ADMIN🐧", username=username)
    except Exception:
        pass
    bot.send_message(message.chat.id, f"Готово. Добавил админа: <b>@{escape(username)}</b>")
    bot.send_message(
        message.chat.id,
        "<b>Админы</b>",
        reply_markup=admins_list_keyboard("admin_admins"),
        disable_web_page_preview=True,
    )


@bot.message_handler(
    content_types=[
        "text",
        "photo",
        "video",
        "document",
        "audio",
        "voice",
        "animation",
        "sticker",
    ],
    # If other input flows are active (add-visit / add-admin), don't let broadcast capture the message.
    func=lambda m: (
        m.chat is not None
        and m.chat.id in _pending_broadcast
        and m.chat.id not in _pending_visit_add
        and m.chat.id not in _pending_admin_add
    ),
)
def handle_admin_broadcast_text(message: telebot.types.Message) -> None:
    if not _message_guard(message):
        return
    if not is_superadmin(message.from_user.id if message.from_user else None):
        _pending_broadcast.pop(message.chat.id, None)
        return

    state = _pending_broadcast.get(message.chat.id) or {}
    stage = str(state.get("stage") or "").strip().lower() or "await_post"
    targets = state.get("targets")
    if not isinstance(targets, list) or not targets:
        _pending_broadcast.pop(message.chat.id, None)
        bot.send_message(message.chat.id, "Получателей нет.", reply_markup=admin_broadcast_root_keyboard())
        return

    if stage != "await_post":
        bot.send_message(
            message.chat.id,
            "Пост уже получен. Нажми <b>Отправить</b> или <b>Другой пост</b>.",
            reply_markup=admin_broadcast_post_keyboard(),
        )
        return

    kind = str(state.get("kind") or "").strip().lower()
    label = state.get("label") or "Аудитория"

    # Don't accept commands as a "post".
    if message.content_type == "text":
        txt = (message.text or "").strip()
        if txt.startswith("/"):
            bot.send_message(
                message.chat.id,
                "Перешли готовый пост сообщением (или нажми <b>Отмена</b>).",
                reply_markup=admin_broadcast_cancel_keyboard(),
            )
            return

    # Store the post source; sending is confirmed via button.
    _pending_broadcast[message.chat.id] = {
        "kind": kind,
        "targets": targets,
        "label": label,
        "stage": "confirm",
        "src_chat_id": int(message.chat.id),
        "src_message_id": int(message.message_id),
    }
    _save_pending_broadcast()

    bot.send_message(message.chat.id, "Вот как будет выглядеть рассылка:")
    try:
        bot.copy_message(message.chat.id, message.chat.id, message.message_id)
    except Exception:
        pass

    bot.send_message(
        message.chat.id,
        f"<b>Рассылка</b>\n\nКому: <b>{escape(str(label))}</b>\nПолучателей: <b>{len(targets)}</b>\n\nОтправить?",
        reply_markup=admin_broadcast_post_keyboard(),
        disable_web_page_preview=True,
    )


@bot.callback_query_handler(func=lambda call: call.data in {"admin_add_visit", "admin_add_visit_admins"})
def handle_admin_add_visit(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    if not _is_staff(call.from_user):
        return

    # Remember where to go back.
    back_cb = "admin_menu" if call.data == "admin_add_visit" else "admin_admins"
    _pending_visit_add[call.message.chat.id] = back_cb
    _pending_admin_add.discard(call.message.chat.id)
    # If a broadcast flow was started in this chat, cancel it to avoid swallowing card-number input.
    _pending_broadcast.pop(call.message.chat.id, None)
    _save_pending_broadcast()
    bot.send_message(
        call.message.chat.id,
        "<b>ВВЕДИ НОМЕР КАРТЫ LEVEL</b>",
        reply_markup=admin_bottom_keyboard(back_cb),
    )


@bot.message_handler(func=lambda m: m.chat is not None and m.chat.id in _pending_visit_add)
def handle_admin_visit_input(message: telebot.types.Message) -> None:
    if not _message_guard(message):
        return
    if not _is_staff(message.from_user):
        _pending_visit_add.pop(message.chat.id, None)
        return

    back_cb = _pending_visit_add.get(message.chat.id, "admin_menu")
    card_number = (message.text or "").strip()
    if not card_number.isdigit():
        bot.send_message(
            message.chat.id,
            "Нужно число (номер карты).",
            reply_markup=admin_visit_done_keyboard(back_cb),
        )
        return

    card = find_card_by_number(card_number)
    if card is None:
        bot.send_message(
            message.chat.id,
            "Карта не найдена.",
            reply_markup=admin_visit_done_keyboard(back_cb),
        )
        return

    admin_id = message.from_user.id if message.from_user else 0
    # Block self-award.
    if admin_id and int(admin_id) == int(card.user_id):
        _pending_visit_add.pop(message.chat.id, None)
        bot.send_message(
            message.chat.id,
            "Нельзя засчитать визит самому себе.",
            reply_markup=admin_visit_done_keyboard(back_cb),
            disable_web_page_preview=True,
        )
        return

    if not can_add_visit_today_tyumen(card.user_id, source=BOT_SOURCE):
        _pending_visit_add.pop(message.chat.id, None)
        # Discount should still be shown even if visit can't be counted.
        current = find_card_by_user_id(card.user_id)
        base_discount = current.discount if current is not None else card.discount
        discount, _bonus = total_discount_for_user(card.user_id, int(base_discount))
        bot.send_message(
            message.chat.id,
            f"Сегодня уже визит был засчитан.\nМаксимум один визит в день.\nСкидка <b>{discount}%</b>",
            reply_markup=admin_visit_done_keyboard(back_cb),
            disable_web_page_preview=True,
        )
        return

    add_visit_marked(card.user_id, admin_id, source=BOT_SOURCE)
    # Keep a simple total counter on the client card, too.
    updated = add_visit_by_user_id(card.user_id, 1)
    _pending_visit_add.pop(message.chat.id, None)

    base_discount = updated.discount if updated is not None else card.discount
    discount, _bonus = total_discount_for_user(card.user_id, int(base_discount))
    bot.send_message(
        message.chat.id,
        f"Визит засчитан.\nСкидка <b>{discount}%</b>",
        reply_markup=admin_visit_done_keyboard(back_cb),
        disable_web_page_preview=True,
    )


@bot.callback_query_handler(func=lambda call: (call.data or "").startswith("admin_view:"))
def handle_admin_view(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    if not is_superadmin(call.from_user.id if call.from_user else None):
        return

    username = (call.data or "").split(":", 1)[1].strip()
    username = normalize_username(username)
    _send_admin_view(call.message.chat.id, username=username, offset=0)


@bot.callback_query_handler(func=lambda call: (call.data or "").startswith("admin_viewid:"))
def handle_admin_viewid(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    if not is_superadmin(call.from_user.id if call.from_user else None):
        return

    try:
        uid = int((call.data or "").split(":", 1)[1].strip())
    except Exception:
        return
    _send_admin_view_by_id(call.message.chat.id, user_id=uid, offset=0)


@bot.callback_query_handler(func=lambda call: (call.data or "").startswith("admin_viewp:"))
def handle_admin_view_paged(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    if not is_superadmin(call.from_user.id if call.from_user else None):
        return

    parts = (call.data or "").split(":", 2)
    if len(parts) != 3:
        return
    username = normalize_username(parts[1])
    try:
        offset = int(parts[2])
    except Exception:
        offset = 0
    _send_admin_view(call.message.chat.id, username=username, offset=offset)


@bot.callback_query_handler(func=lambda call: (call.data or "").startswith("admin_viewidp:"))
def handle_admin_viewid_paged(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    if not is_superadmin(call.from_user.id if call.from_user else None):
        return

    parts = (call.data or "").split(":", 2)
    if len(parts) != 3:
        return
    try:
        uid = int(parts[1])
    except Exception:
        return
    try:
        offset = int(parts[2])
    except Exception:
        offset = 0
    _send_admin_view_by_id(call.message.chat.id, user_id=uid, offset=offset)


@bot.callback_query_handler(func=lambda call: (call.data or "").startswith("admin_demote:"))
def handle_admin_demote(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    if not is_superadmin(call.from_user.id if call.from_user else None):
        return

    username = (call.data or "").split(":", 1)[1].strip()
    username = normalize_username(username)
    # Try resolve user_id before removing.
    uid = None
    try:
        rec = next((r for r in list_admins() if r.username == username), None)
        uid = (int(rec.user_id) if (rec and rec.user_id) else None)
    except Exception:
        uid = None
    if uid is None:
        try:
            uid = find_user_id_by_username(username)
        except Exception:
            uid = None

    remove_admin_by_username(username)
    if uid is not None:
        clear_staff_gold_by_user_id(uid)

    bot.send_message(
        call.message.chat.id,
        f"Разжаловал: <b>@{escape(username)}</b>",
        disable_web_page_preview=True,
    )
    bot.send_message(
        call.message.chat.id,
        "<b>Админы</b>",
        reply_markup=admins_list_keyboard("admin_admins"),
        disable_web_page_preview=True,
    )


@bot.callback_query_handler(func=lambda call: call.data == "main_guest_card")
def handle_guest_card(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    user_id = call.from_user.id if call.from_user else None
    try:
        send_level_menu(call.message.chat.id, call.from_user, user_id)
    except Exception as e:
        bot.send_message(call.message.chat.id, f"Ошибка при открытии LEVEL: <code>{escape(str(e))}</code>")

@bot.callback_query_handler(func=lambda call: (call.data or "").startswith("level_tab:"))
def handle_level_tab(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    user_id = call.from_user.id if call.from_user else None
    registered = bool(user_id is not None and is_registered(user_id))
    tab = (call.data or "").split(":", 1)[1].strip()
    if tab not in {"card", "rating", "visits", "giveaway"}:
        tab = "card"

    if tab == "rating":
        text = level_rating_text(superadmin=is_superadmin(user_id))
    elif tab == "giveaway":
        text = level_giveaway_text()
    elif tab == "visits":
        text = level_visits_text()
    else:
        text = level_card_message_text(call.from_user, user_id)

    kb = level_keyboard(registered=registered, active=tab)
    try:
        bot.edit_message_text(
            text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=kb,
            disable_web_page_preview=True,
            parse_mode="HTML",
        )
    except Exception:
        bot.send_message(
            call.message.chat.id,
            text,
            reply_markup=kb,
            disable_web_page_preview=True,
            parse_mode="HTML",
        )


@bot.callback_query_handler(func=lambda call: call.data == "main_location")
def handle_location(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    send_location_menu(call.message.chat.id)


@bot.callback_query_handler(func=lambda call: call.data == "location_interior")
def handle_location_interior(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    send_interior(call.message.chat.id, idx=1)


@bot.callback_query_handler(func=lambda call: call.data == "location_telegram_geo")
def handle_location_telegram_geo(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    try:
        bot.send_location(call.message.chat.id, latitude=LOCATION_LAT, longitude=LOCATION_LON)
    except Exception as e:
        bot.send_message(call.message.chat.id, f"Не удалось отправить геолокацию: <code>{escape(str(e))}</code>")


@bot.callback_query_handler(func=lambda call: (call.data or "").startswith("interior:"))
def handle_interior_nav(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    try:
        idx = int((call.data or "").split(":", 1)[1].strip())
    except Exception:
        idx = 1

    p = _interior_photo_path(idx)
    kb = interior_keyboard(idx)
    if not p.exists():
        bot.send_message(call.message.chat.id, "Фото интерьера не найдено.", reply_markup=location_inline_keyboard())
        return

    try:
        media = telebot.types.InputMediaPhoto(telebot.types.InputFile(p))
        bot.edit_message_media(
            media,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=kb,
        )
    except Exception as e:
        # Ignore "message is not modified" to prevent spam on repeated taps.
        if "message is not modified" in str(e).lower():
            return
        # Fallback: replace message (best-effort).
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        send_interior(call.message.chat.id, idx=idx)


@bot.callback_query_handler(func=lambda call: call.data == "interior_back")
def handle_interior_back(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    send_location_menu(call.message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data == "main_add_visit")
def handle_main_add_visit(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    # Only staff should use this.
    if not _is_staff(call.from_user):
        return

    _pending_visit_add[call.message.chat.id] = "back_to_main"
    _pending_admin_add.discard(call.message.chat.id)
    bot.send_message(
        call.message.chat.id,
        "<b>ВВЕДИ НОМЕР КАРТЫ LEVEL</b>",
        reply_markup=admin_bottom_keyboard("back_to_main"),
    )


@bot.callback_query_handler(func=lambda call: call.data == "main_menu")
def handle_menu(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if not is_menu_allowed(call.from_user.id if call.from_user else None):
        if call.message is not None:
            bot.send_message(call.message.chat.id, "Меню временно недоступно.")
        return
    send_food_menu(call.message.chat.id)


@bot.callback_query_handler(
    func=lambda call: (
        (call.data in {"menu_hookah", "menu_tea", "menu_drinks", "menu_food", "menu_watch", "menu_rules"})
    )
)
def handle_menu_sections(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if not is_menu_allowed(call.from_user.id if call.from_user else None):
        if call.message is not None:
            bot.send_message(call.message.chat.id, "Меню временно недоступно.")
        return

    if call.message is None:
        return

    raw = call.data or ""
    drinks_rules = False
    section_cb = raw

    def _text(cb: str, *, show_drinks_rules: bool) -> str:
        if cb == "menu_hookah":
            return (
                "<b>КАЛЬЯН</b>\n\n"
                "<b>До 17:00 - 1 000₽</b>\n"
                "<b>После 17:00 - 1 400₽</b>\n\n"
                "Соберём вкус и крепость под тебя. Работаем на премиальных табаках\n\n"
                "Если за столом более четырёх гостей, необходимо заказать 2 кальяна единовременно, если более шести - 3 кальяна\n\n"
                "С 19:00 действует правило - 2 часа на один кальян"
            )
        if cb == "menu_tea":
            return (
                "<b>КЛАССИЧЕСКИЙ ЧАЙ</b>\n"
                "<b>600</b><b>мл</b> / <b>320</b><b>₽</b>\n"
                "• Ассам\n"
                "• Эрл Грей\n"
                "• Зелёный с жасмином\n"
                "• Каркаде\n"
                "• Таёжный сбор\n\n"
                "<b>КИТАЙСКИЙ ЧАЙ</b>\n"
                "<b>600</b><b>мл</b> / <b>320</b><b>₽</b>\n"
                "• Сенча (Шу Сян Люй)\n"
                "• Молочный улун\n"
                "• Дянь хун маофен\n"
                "• Пуэр шу\n"
                "• Улун те гуань инь\n\n"
                "<b>ЧАЙ АВТОРСКИЙ</b>\n"
                "<b>900</b><b>мл</b> / <b>500</b><b>₽</b>\n"
                "• Брусника-клюква\n"
                "• Малина-базилик\n"
                "• Клюква-можжевельник\n"
                "• Облепиха\n"
                "• Апельсин-имбирь"
            )
        if cb == "menu_food":
            return "Со своей едой - <b>можно</b>\n\nГолодными не оставим, подскажем быструю доставку🚚"
        if cb == "menu_drinks":
            base = (
                "<b>БЕЗАЛКОГОЛЬНЫЕ НАПИТКИ</b>\n"
                "• Red Bull <b>355</b><b>мл</b> - <b>300</b><b>₽</b>\n"
                "• Coca-Cola <b>330</b><b>мл</b> - <b>220</b><b>₽</b>\n\n"
                "<b>МОРСЫ</b>\n"
                "<b>250</b><b>мл</b> - <b>120</b><b>₽</b>\n"
                "• Облепиха\n"
                "• Клюква\n"
                "• Брусника\n\n"
                "<b>АВТОРСКИЕ</b>\n"
                "<b>400</b><b>мл</b> - <b>290</b><b>₽</b>\n"
                "<b>1</b><b>л</b> - <b>550</b><b>₽</b>\n"
                "• Клубника - лемонграсс\n"
                "• Груша - персик - юдзу\n"
                "• Манго - маракуйя\n"
                "• Мохито"
            )
            if not show_drinks_rules:
                return f"{base}\n\nК нам нельзя со своими безалкогольными напитками"
            rules = (
                "К нам нельзя со своими безалкогольными напитками\n\n"
                "Мы предоставляем всё необходимое для комфортного распития: бокалы, лёд, штопор.\n\n"
                "Пробковый сбор:\n"
                "Пиво, сидр, медовуха - 100 руб/бут\n"
                "Вино, шампанское - 300 руб/бут\n"
                "Крепкий алкоголь (от 20%) - 500 руб/бут\n\n"
                "Гость несёт ответственность за порчу имущества заведения На Грани"
            )
            return f"{base}\n\n{rules}"
        if cb == "menu_rules":
            return (
                "Мы предоставляем всё необходимое для комфортного распития: бокалы, лёд, штопор.\n\n"
                "<b>Пробковый сбор:</b>\n"
                "Пиво, сидр, медовуха - <b>100 руб/бут</b>\n"
                "Вино, шампанское - <b>300 руб/бут</b>\n"
                "Крепкий алко (от 20%) - <b>500 руб/бут</b>\n\n"
                "Гость <b>несёт ответственность</b> за порчу имущества заведения <b>На Грани</b>"
            )
        if cb == "menu_watch":
            return "Раздел «Интерьер» находится в разработке 🚧"
        return "Выбери раздел меню:"

    text = _text(section_cb, show_drinks_rules=drinks_rules)
    kb = menu_inline_keyboard(active=section_cb, drinks_rules=drinks_rules)

    try:
        bot.edit_message_text(
            text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception as e:
        # If user taps the already-selected tab, Telegram replies "message is not modified".
        # In that case do nothing (no duplicate messages).
        if "message is not modified" in str(e).lower():
            return
        bot.send_message(call.message.chat.id, text, reply_markup=kb, disable_web_page_preview=True)


@bot.callback_query_handler(func=lambda call: call.data == "register_card")
def handle_register_card_callback(call: telebot.types.CallbackQuery) -> None:
    user = call.from_user
    if user:
        register_card(user.id)
        ensure_level_card(
            user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
        )

    if not _callback_guard(call):
        return

    bot.send_message(
        call.message.chat.id,
        "Готово, карта <b>LEVEL</b> зарегистрирована.",
    )
    bot.send_message(
        call.message.chat.id,
        guest_card_text(user_display_name(call.from_user), user_id=(user.id if user else None)),
        reply_markup=guest_card_registered_inline_keyboard(),
        disable_web_page_preview=True,
    )


@bot.callback_query_handler(func=lambda call: call.data == "back_to_main")
def handle_back_callback(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    _pending_admin_add.discard(call.message.chat.id)
    _pending_visit_add.pop(call.message.chat.id, None)
    send_main_menu(call.message.chat.id, user=call.from_user)


@bot.message_handler(
    func=lambda m: (
        not (getattr(m, "text", "") or "").startswith("/")
        and (m.chat is None or m.chat.id not in _pending_broadcast)
        and (m.chat is None or m.chat.id not in _pending_admin_add)
        and (m.chat is None or m.chat.id not in _pending_visit_add)
    )
)
def handle_fallback(message: telebot.types.Message) -> None:
    if not _message_guard(message):
        return
    bot.send_message(
        message.chat.id,
        "Нажмите /start, чтобы открыть главное меню.",
    )


@bot.inline_handler(func=lambda q: True)
def handle_inline_query(query: telebot.types.InlineQuery) -> None:
    """
    Inline usage:
    - "@YourBot @username"
    - "@YourBot https://t.me/username"
    Returns a LEVEL card message for that user (if we have them in stats db).
    """
    qtext = (query.query or "").strip()
    username = _extract_username_from_inline_query(qtext)
    if not username:
        # Show a hint so user sees that inline works.
        article = telebot.types.InlineQueryResultArticle(
            id="level:hint",
            title="Как вызвать карту LEVEL",
            input_message_content=telebot.types.InputTextMessageContent(
                "Напиши: @nagraniloungetestbot @username\n"
                "Пример: @nagraniloungetestbot @mirzametov13",
                parse_mode=None,
                disable_web_page_preview=True,
            ),
            description="Напиши @username после имени бота",
        )
        bot.answer_inline_query(query.id, [article], cache_time=1, is_personal=True)
        return

    user_id = find_user_id_by_username(username)
    if user_id is None:
        article = telebot.types.InlineQueryResultArticle(
            id=f"level:notfound:{username}",
            title=f"Нет данных для @{username}",
            input_message_content=telebot.types.InputTextMessageContent(
                "Пользователь ещё не открывал бота. Пусть нажмёт /start.",
                parse_mode=None,
                disable_web_page_preview=True,
            ),
            description="Пользователь не найден в базе бота",
        )
        bot.answer_inline_query(query.id, [article], cache_time=1, is_personal=True)
        return

    # Only show card if it's registered.
    if find_card_by_user_id(user_id) is None:
        article = telebot.types.InlineQueryResultArticle(
            id=f"level:nocard:{user_id}",
            title=f"Карта LEVEL не зарегистрирована (@{username})",
            input_message_content=telebot.types.InputTextMessageContent(
                "Карта LEVEL ещё не зарегистрирована.",
                parse_mode=None,
                disable_web_page_preview=True,
            ),
            description="Нет зарегистрированной карты LEVEL",
        )
        bot.answer_inline_query(query.id, [article], cache_time=1, is_personal=True)
        return

    msg = level_card_inline_text(username=username, user_id=user_id)

    results: list[telebot.types.InlineQueryResult] = []

    # Main tappable row (text-only on send).
    results.append(
        telebot.types.InlineQueryResultArticle(
            id=f"level:{user_id}",
            title=f"🪪 КАРТА LEVEL @{username}",
            description="Нажми",
            input_message_content=telebot.types.InputTextMessageContent(
                msg,
                parse_mode="HTML",
                disable_web_page_preview=True,
            ),
        )
    )

    bot.answer_inline_query(query.id, results, cache_time=1, is_personal=True)
    return


if __name__ == "__main__":
    # Keep the bot running even if Telegram API is temporarily unreachable
    # (DNS, network hiccups, etc). Without this, a startup failure in setMyCommands
    # can bring the whole bot down.
    backoff_s = 2
    while True:
        try:
            # Restore persisted broadcast state (if any).
            _load_pending_broadcast()
            try:
                bot.set_my_commands(
                    [
                        BotCommand("start", "Главное меню"),
                        BotCommand("level", "🪪 Карта LEVEL"),
                        BotCommand("menu", "🧉 Меню"),
                        BotCommand("booking", "🛋 Бронь"),
                        BotCommand("location", "🚕 Найти нас"),
                        BotCommand("version", "Версия сборки"),
                    ]
                )
            except Exception as e:
                # Commands are optional; polling can still work.
                log.warning("setMyCommands failed: %s", e)

            # Be explicit to ensure inline queries are delivered to the bot.
            log.info("Starting polling (skip_pending=%s)", True)
            bot.infinity_polling(skip_pending=True, allowed_updates=telebot.util.update_types)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log.exception("polling crashed: %s", e)
            time.sleep(backoff_s)
            backoff_s = min(backoff_s * 2, 60)
