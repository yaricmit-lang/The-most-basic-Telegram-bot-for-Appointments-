import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = frozenset(
    int(x)
    for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",")
    if x.lstrip("-").isdigit()
)
DB_PATH = os.getenv("DB_PATH", "booking.db")


def is_admin(tg_id):
    return tg_id in ADMIN_IDS
