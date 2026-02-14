import os
from html import escape
from pathlib import Path
from urllib.parse import quote
import time

import telebot
from dotenv import load_dotenv
from telebot.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

import re

from loungebot.admin_stats import (
    UserInfo,
    active_subscribers_count,
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
    visit_counts,
    user_visit_counts,
    add_visit_marked,
    can_add_visit_today_tyumen,
)
from loungebot.admin_roles import (
    add_admin_by_username,
    is_admin_user,
    list_admins,
    normalize_username,
    remove_admin_by_username,
    sync_from_user,
)
from loungebot.guest_cards import is_registered, register_card
from loungebot.level_cards import (
    add_visit_by_user_id,
    ensure_level_card,
    find_card_by_number,
    find_card_by_user_id,
    next_tier_info,
)
from loungebot.keyboards import (
    BTN_BACK,
    BTN_BOOKING,
    BTN_GUEST_CARD,
    BTN_LOCATION,
    BTN_MENU,
    BTN_REGISTER_CARD,
)


def guest_card_text(display_name: str, *, user_id: int | None = None) -> str:
    card = find_card_by_user_id(int(user_id)) if user_id is not None else None
    level_label = card.level if card else "IRON⚙️"
    card_number = card.card_number if card else "4821"
    discount = card.discount if card else 3
    total_visits = card.visits if card else 0
    next_info = next_tier_info(total_visits) if card else ("BRONZE🥉", 5)
    if next_info is None:
        next_line = "Максимальный уровень\n\n"
    else:
        next_level, remain = next_info
        next_line = f"До <b>{escape(next_level)}</b> осталось: <b>{remain} визитов</b>\n\n"
    return (
        "<b>КАРТА LEVEL</b>\n\n"
        f"{display_name}, твой уровень - <b>{escape(level_label)}</b>\n"
        f"Номер карты: <b>{escape(card_number)}</b>\n\n"
        f"Всего визитов: <b>{total_visits}</b>\n"
        f"{next_line}"
        "Твой уровень даёт:\n"
        f"• скидка <b>{discount}%</b> на меню <b><a href=\"https://t.me/nagrani_lounge\">Lounge</a></b>\n"
        f"• скидка <b>{discount}%</b> на <b><a href=\"https://t.me/prohvat72\">Прохват72</a></b>\n\n"
        "Назови номер карты администратору,\n"
        "чтобы засчитать визит по карте level\n"
        "и применить скидку."
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
                text=f"👀 Супер-админ {active_subscribers_count()}",
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
    keyboard = InlineKeyboardMarkup()
    keyboard.row(
        InlineKeyboardButton(text=BTN_REGISTER_CARD, callback_data="register_card"),
    )
    keyboard.row(
        InlineKeyboardButton(text=BTN_BACK, callback_data="back_to_main"),
        InlineKeyboardButton(text="🧾 О визитах", callback_data="level_info"),
    )
    return keyboard


def guest_card_registered_inline_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    keyboard.row(
        InlineKeyboardButton(text=BTN_BACK, callback_data="back_to_main"),
        InlineKeyboardButton(text="🧾 О визитах", callback_data="level_info"),
    )
    return keyboard


def location_inline_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    keyboard.row(InlineKeyboardButton(text="🗺️ Ссылка 2ГИС", url=LOCATION_2GIS_URL))
    keyboard.row(InlineKeyboardButton(text="🚀 Новости бара", url=NEWS_URL))
    keyboard.row(InlineKeyboardButton(text="🏍 Наш прокат Прохват72", url=PROHVAT72_URL))
    keyboard.row(InlineKeyboardButton(text="🏁 Наши гонеи На грани", url=RACES_URL))
    keyboard.row(InlineKeyboardButton(text="👈Назад", callback_data="back_to_main"))
    return keyboard


def menu_inline_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    keyboard.row(InlineKeyboardButton(text="💨 Подымить", callback_data="menu_hookah"))
    keyboard.row(InlineKeyboardButton(text="🍸 Выпить", callback_data="menu_drinks"))
    keyboard.row(InlineKeyboardButton(text="🍽 Поесть", callback_data="menu_food"))
    keyboard.row(InlineKeyboardButton(text="👀 Посмотреть", callback_data="menu_watch"))
    keyboard.row(InlineKeyboardButton(text="👈Назад", callback_data="back_to_main"))
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
    keyboard.row(InlineKeyboardButton(text="👈Назад", callback_data="back_to_main"))
    return keyboard


def admin_bottom_keyboard(back_cb: str) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    keyboard.row(
        InlineKeyboardButton(text="👈Назад", callback_data=back_cb),
        InlineKeyboardButton(text="🏠 Домой", callback_data="back_to_main"),
    )
    return keyboard


def admins_manage_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    keyboard.row(InlineKeyboardButton(text="📋 Список админов", callback_data="admin_admins_list"))
    keyboard.row(InlineKeyboardButton(text="➕ Добавить админа", callback_data="admin_admins_add"))
    keyboard.row(
        InlineKeyboardButton(text="👈Назад", callback_data="admin_menu"),
        InlineKeyboardButton(text="🏠 Домой", callback_data="back_to_main"),
    )
    return keyboard


load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WELCOME_IMAGE_PATH = os.getenv("WELCOME_IMAGE_PATH", "assets/lounge_source.jpg")
GUEST_CARD_URL = os.getenv("GUEST_CARD_URL", "https://example.com/guest-card")
MENU_URL = os.getenv("MENU_URL", "https://example.com/menu")
BOOKING_URL = os.getenv("BOOKING_URL", "https://example.com/booking")
LOCATION_URL = os.getenv("LOCATION_URL", "https://maps.google.com")
LOCATION_2GIS_URL = os.getenv("LOCATION_2GIS_URL", "https://2gis.ru/tyumen/geo/70000001110930565")
NEWS_URL = os.getenv("NEWS_URL", "https://t.me/nagrani_lounge")
PROHVAT72_URL = os.getenv("PROHVAT72_URL", "https://t.me/prohvat72")
RACES_URL = os.getenv("RACES_URL", "https://t.me/na_grani_team")
LOCATION_ADDRESS = os.getenv("LOCATION_ADDRESS", "Мы находимся по адресу:\nФармана Салманова, 15")
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

# Best-effort guards against duplicate UI actions.
_recent_callback_keys: dict[tuple[int, str, int], float] = {}
_main_menu_photo_file_id: str | None = None
_recent_message_keys: dict[tuple[int, int], float] = {}
_pending_admin_add: set[int] = set()
_pending_visit_add: dict[int, str] = {}  # chat_id -> back_cb

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
                    username=call.from_user.username,
                )
            )
            sync_from_user(
                user_id,
                call.from_user.username,
                call.from_user.first_name,
                call.from_user.last_name,
            )
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
                    username=message.from_user.username,
                )
            )
            sync_from_user(
                message.from_user.id,
                message.from_user.username,
                message.from_user.first_name,
                message.from_user.last_name,
            )
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
    for rec in list_admins():
        keyboard.row(
            InlineKeyboardButton(
                text=_admin_label(rec.username, rec.first_name, rec.last_name),
                callback_data=f"admin_view:{rec.username}",
            )
        )
    keyboard.row(
        InlineKeyboardButton(text="👈Назад", callback_data=back_cb),
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
        v_today, v_7, v_30, v_total = admin_marked_visits_summary(int(rec.user_id))
        lines.append("")
        lines.append("<b>Рейтинг</b>")
        lines.append(f"Визитов за сегодня: <b>{v_today}</b>")
        lines.append(f"Визитов за 7 дней: <b>{v_7}</b>")
        lines.append(f"Визитов за 30 дней: <b>{v_30}</b>")
        lines.append(f"Всего визитов: <b>{v_total}</b>")

        lines.append("")
        lines.append("<b>Последние отмеченные</b>")
        recent, total = admin_marked_recent_clients_page(int(rec.user_id), offset=offset, limit=20)
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
    v7, v30, vtotal = user_visit_counts(user_id)
    card = find_card_by_user_id(user_id)
    if card is None:
        # No registered card, no inline result should be returned (handled upstream).
        level_label, discount = _level_for_visits(vtotal)
        card_number = _card_number_for_user(user_id)
    else:
        level_label = card.level
        discount = card.discount
        card_number = card.card_number
    u = username.strip().lstrip("@")
    return (
        f"КАРТА LEVEL @{u}\n\n"
        f"Уровень - {level_label}\n"
        f"Номер карты: {card_number}\n"
        f"Скидка - {discount}%\n\n"
        f"Визитов за 7 дней: {v7}\n"
        f"Визитов за 30 дней: {v30}\n"
        f"Всего визитов: {vtotal}"
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
        "Карта <b>LEVEL</b> - это твой личный профиль гостя. Здесь растёт уровень скидки.",
        reply_markup=guest_card_inline_keyboard(),
    )

def level_info_text() -> str:
    return (
        "<b>🧾 О визитах</b>\n\n"
        "Визит засчитывается при условии чека от <b>1000₽</b>.\n"
        "Засчитать визит можно не чаще <b>1 раза в день</b>.\n\n"
        "Скидка по твоему уровню действует всегда, даже если визит не засчитан."
    )


def level_info_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    keyboard.row(
        InlineKeyboardButton(text="👈Назад", callback_data="main_guest_card"),
        InlineKeyboardButton(text="🏠 Домой", callback_data="back_to_main"),
    )
    return keyboard


def send_location_menu(chat_id: int) -> None:
    bot.send_message(
        chat_id,
        LOCATION_ADDRESS,
        reply_markup=location_inline_keyboard(),
    )


def send_food_menu(chat_id: int) -> None:
    bot.send_message(
        chat_id,
        "Выбери раздел меню:",
        reply_markup=menu_inline_keyboard(),
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
    send_main_menu(message.chat.id, user=message.from_user)


@bot.message_handler(commands=["level"])
def handle_level_command(message: telebot.types.Message) -> None:
    if not _message_guard(message):
        return
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

    visits_today, visits_7, visits_30 = visit_counts()
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
    admin_rows = top_admins_by_marked_visits(days=30, limit=100)
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
    bot.send_message(message.chat.id, f"Готово. Добавил админа: <b>@{escape(username)}</b>")
    bot.send_message(
        message.chat.id,
        "<b>Админы</b>",
        reply_markup=admins_list_keyboard("admin_admins"),
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

    if not can_add_visit_today_tyumen(card.user_id):
        _pending_visit_add.pop(message.chat.id, None)
        # Discount should still be shown even if visit can't be counted.
        current = find_card_by_user_id(card.user_id)
        discount = current.discount if current is not None else card.discount
        bot.send_message(
            message.chat.id,
            f"Сегодня уже визит был засчитан.\nМаксимум один визит в день.\nСкидка <b>{discount}%</b>",
            reply_markup=admin_visit_done_keyboard(back_cb),
            disable_web_page_preview=True,
        )
        return

    admin_id = message.from_user.id if message.from_user else 0
    add_visit_marked(card.user_id, admin_id)
    # Keep a simple total counter on the client card, too.
    updated = add_visit_by_user_id(card.user_id, 1)
    _pending_visit_add.pop(message.chat.id, None)

    discount = updated.discount if updated is not None else card.discount
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
    remove_admin_by_username(username)

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

@bot.callback_query_handler(func=lambda call: call.data == "level_info")
def handle_level_info(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
    if call.message is None:
        return
    bot.send_message(
        call.message.chat.id,
        level_info_text(),
        reply_markup=level_info_keyboard(),
        disable_web_page_preview=True,
    )


@bot.callback_query_handler(func=lambda call: call.data == "main_location")
def handle_location(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return
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
    send_food_menu(call.message.chat.id)


@bot.callback_query_handler(func=lambda call: call.data in {"menu_hookah", "menu_drinks", "menu_food", "menu_watch"})
def handle_menu_sections(call: telebot.types.CallbackQuery) -> None:
    if not _callback_guard(call):
        return

    label_map = {
        "menu_hookah": "Кальяны",
        "menu_drinks": "Выпивка",
        "menu_food": "Еда",
        "menu_watch": "Посмотреть",
    }
    section = label_map[call.data]
    bot.send_message(
        call.message.chat.id,
        f"Раздел «{section}» находится в разработке 🚧",
        reply_markup=menu_inline_keyboard(),
    )


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
        "Готово, карта гостя зарегистрирована.",
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


@bot.message_handler(func=lambda m: not (getattr(m, "text", "") or "").startswith("/"))
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
    article = telebot.types.InlineQueryResultArticle(
        id=f"level:{user_id}",
        title=f"КАРТА LEVEL @{username}",
        input_message_content=telebot.types.InputTextMessageContent(
            msg,
            parse_mode=None,
            disable_web_page_preview=True,
        ),
        description="Профиль гостя (LEVEL)",
    )
    bot.answer_inline_query(query.id, [article], cache_time=1, is_personal=True)


if __name__ == "__main__":
    bot.set_my_commands(
        [
            BotCommand("start", "Главное меню"),
            BotCommand("level", "🪪 LEVEL"),
            BotCommand("menu", "🧉 Меню"),
            BotCommand("booking", "🛋 Бронь"),
            BotCommand("location", "🚕 Найти нас"),
        ]
    )
    # Be explicit to ensure inline queries are delivered to the bot.
    bot.infinity_polling(skip_pending=True, allowed_updates=telebot.util.update_types)
