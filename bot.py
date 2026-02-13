import os
from pathlib import Path
from urllib.parse import quote

import telebot
from dotenv import load_dotenv
from telebot.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)

from loungebot.guest_cards import is_registered, register_card
from loungebot.keyboards import (
    BTN_BACK,
    BTN_BOOKING,
    BTN_GUEST_CARD,
    BTN_LOCATION,
    BTN_MENU,
    BTN_REGISTER_CARD,
)


def guest_card_text() -> str:
    return (
        "<b>КАРТА ГОСТЯ</b>\n\n"
        "Евгений, твой уровень - <b>IRON⚙️</b>\n"
        "Номер карты: <b>4821</b>\n\n"
        "Всего визитов: <b>0</b>\n"
        "До <b>BRONZE🥉</b> осталось: <b>3 визита</b>\n\n"
        "Твой уровень даёт:\n"
        "• скидка <b>3%</b> на меню <b>Lounge</b>\n"
        "• скидка <b>3%</b> на <b>Прохват72</b>\n\n"
        "Покажи номер карты администратору,\n"
        "чтобы засчитать визит по карте гостя\n"
        "и применить скидку."
    )


def main_inline_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    keyboard.row(InlineKeyboardButton(text=BTN_GUEST_CARD, callback_data="main_guest_card"))
    keyboard.row(InlineKeyboardButton(text=BTN_MENU, callback_data="main_menu"))
    keyboard.row(InlineKeyboardButton(text=BTN_BOOKING, url=booking_deep_link()))
    keyboard.row(InlineKeyboardButton(text=BTN_LOCATION, callback_data="main_location"))
    return keyboard


def guest_card_inline_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    keyboard.row(
        InlineKeyboardButton(text=BTN_REGISTER_CARD, callback_data="register_card"),
    )
    keyboard.row(
        InlineKeyboardButton(text=BTN_BACK, callback_data="back_to_main"),
    )
    return keyboard


def guest_card_registered_inline_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    keyboard.row(
        InlineKeyboardButton(text=BTN_BACK, callback_data="back_to_main"),
    )
    return keyboard


def location_inline_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    keyboard.row(InlineKeyboardButton(text="🗺️ Ссылка 2ГИС", url=LOCATION_2GIS_URL))
    keyboard.row(InlineKeyboardButton(text="🚀 Новости бара", url=NEWS_URL))
    keyboard.row(InlineKeyboardButton(text="🏍 Прохват72", url=PROHVAT72_URL))
    keyboard.row(InlineKeyboardButton(text="👈Назад", callback_data="back_to_main"))
    return keyboard


def menu_inline_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup()
    keyboard.row(InlineKeyboardButton(text="💨 Подымить", callback_data="menu_hookah"))
    keyboard.row(InlineKeyboardButton(text="🍸 Выпить", callback_data="menu_drinks"))
    keyboard.row(InlineKeyboardButton(text="🍽 Поесть", callback_data="menu_food"))
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


def send_main_menu(chat_id: int) -> None:
    keyboard = main_inline_keyboard()
    image_path = Path(WELCOME_IMAGE_PATH)
    # Force-remove any legacy reply keyboard from older bot versions.
    try:
        temp_msg = bot.send_message(
            chat_id,
            "Обновляю меню...",
            reply_markup=ReplyKeyboardRemove(),
        )
        try:
            bot.delete_message(chat_id, temp_msg.message_id)
        except Exception:
            pass
    except Exception:
        pass

    if image_path.exists():
        with image_path.open("rb") as image:
            bot.send_photo(chat_id, image, reply_markup=keyboard)
    else:
        bot.send_message(
            chat_id,
            "Загрузите файл логотипа в assets/lounge_source.jpg, чтобы отправлять стартовую картинку.",
            reply_markup=keyboard,
        )


def send_level_menu(chat_id: int, user_id: int | None) -> None:
    if user_id is not None and is_registered(user_id):
        bot.send_message(
            chat_id,
            guest_card_text(),
            reply_markup=guest_card_registered_inline_keyboard(),
        )
        return

    bot.send_message(
        chat_id,
        "Карта <b>LEVEL</b> - это твой личный профиль гостя. Здесь растёт уровень скидки.",
        reply_markup=guest_card_inline_keyboard(),
    )


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
    send_main_menu(message.chat.id)


@bot.message_handler(commands=["level"])
def handle_level_command(message: telebot.types.Message) -> None:
    user_id = message.from_user.id if message.from_user else None
    send_level_menu(message.chat.id, user_id)


@bot.message_handler(commands=["menu"])
def handle_menu_command(message: telebot.types.Message) -> None:
    send_food_menu(message.chat.id)


@bot.message_handler(commands=["booking"])
def handle_booking_command(message: telebot.types.Message) -> None:
    send_booking_menu(message.chat.id)


@bot.message_handler(commands=["location"])
def handle_location_command(message: telebot.types.Message) -> None:
    send_location_menu(message.chat.id)


@bot.callback_query_handler(func=lambda call: call.data == "main_guest_card")
def handle_guest_card(call: telebot.types.CallbackQuery) -> None:
    if call.message is None:
        return
    user_id = call.from_user.id if call.from_user else None
    send_level_menu(call.message.chat.id, user_id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "main_location")
def handle_location(call: telebot.types.CallbackQuery) -> None:
    if call.message is None:
        return
    send_location_menu(call.message.chat.id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "main_menu")
def handle_menu(call: telebot.types.CallbackQuery) -> None:
    if call.message is None:
        return
    send_food_menu(call.message.chat.id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data in {"menu_hookah", "menu_drinks", "menu_food"})
def handle_menu_sections(call: telebot.types.CallbackQuery) -> None:
    if call.message is None:
        return

    label_map = {
        "menu_hookah": "Кальяны",
        "menu_drinks": "Выпивка",
        "menu_food": "Еда",
    }
    section = label_map[call.data]
    bot.send_message(
        call.message.chat.id,
        f"Раздел «{section}» скоро заполним.",
        reply_markup=menu_inline_keyboard(),
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "register_card")
def handle_register_card_callback(call: telebot.types.CallbackQuery) -> None:
    user = call.from_user
    if user:
        register_card(user.id)

    if call.message is None:
        return

    bot.send_message(
        call.message.chat.id,
        "Готово, карта гостя зарегистрирована.",
    )
    bot.send_message(
        call.message.chat.id,
        guest_card_text(),
        reply_markup=guest_card_registered_inline_keyboard(),
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "back_to_main")
def handle_back_callback(call: telebot.types.CallbackQuery) -> None:
    if call.message is None:
        return
    send_main_menu(call.message.chat.id)
    bot.answer_callback_query(call.id)


@bot.message_handler(func=lambda _m: True)
def handle_fallback(message: telebot.types.Message) -> None:
    bot.send_message(
        message.chat.id,
        "Нажмите /start, чтобы открыть главное меню.",
    )


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
    bot.infinity_polling(skip_pending=True)
