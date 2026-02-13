from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    bot_token: str
    welcome_image_path: str
    guest_card_url: str
    menu_url: str
    booking_url: str
    location_url: str


def load_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is not set")

    return Settings(
        bot_token=token,
        welcome_image_path=os.getenv("WELCOME_IMAGE_PATH", "assets/lounge_source.jpg"),
        guest_card_url=os.getenv("GUEST_CARD_URL", "https://example.com/guest-card"),
        menu_url=os.getenv("MENU_URL", "https://example.com/menu"),
        booking_url=os.getenv("BOOKING_URL", "https://example.com/booking"),
        location_url=os.getenv("LOCATION_URL", "https://maps.google.com"),
    )
