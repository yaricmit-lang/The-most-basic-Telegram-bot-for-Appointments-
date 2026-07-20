"""Shared helpers: formatting, keyboards, safe message editing."""
import html
import re

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

_PRICE_NUM = re.compile(r"(\d+(?:[.,]\d+)?)")


def price_with_ext(price_text, extra):
    """Service price + nail-extension surcharge.

    Price is stored as text ("$40", "40 USD"), so we add to the first number found,
    keeping the currency: price_with_ext('$40', 15) -> '$55'. If there's no number
    in the price, the surcharge is appended as a separate note.
    """
    price_text = price_text or ""
    if not extra:
        return price_text
    m = _PRICE_NUM.search(price_text)
    if m:
        total = float(m.group(1).replace(",", ".")) + extra
        num = str(int(total)) if total == int(total) else f"{total:.2f}"
        return price_text[:m.start()] + num + price_text[m.end():]
    note = f"+{extra} € (extension)"
    return f"{price_text} {note}" if price_text else note

WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WD_FULL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def esc(v):
    return html.escape(str(v or ""))


def btn(text, cb):
    return InlineKeyboardButton(text=text, callback_data=cb)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def fmt_dur(minutes):
    h, m = divmod(int(minutes), 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def fmt_dt(s):
    """'2026-07-14 10:00' -> '14.07 at 10:00'"""
    return f"{s[8:10]}.{s[5:7]} at {s[11:16]}"


def fmt_d(s):
    """'2026-07-14' -> '14.07'"""
    return f"{s[8:10]}.{s[5:7]}"


def date_label(d):
    return f"{WD[d.weekday()]} {d.strftime('%d.%m')}"


def main_menu_kb(admin=False):
    rows = [
        [btn("📅 Book", "bk")],
        [btn("📋 My bookings", "my")],
        [btn("👤 My profile", "prof")],
    ]
    if admin:
        rows.append([btn("⚙️ Owner panel", "a:menu")])
    return kb(rows)


_album_seen = set()


def first_of_album(message):
    """True if this is the first message of an album.

    Telegram delivers an album as several messages, and aiogram handles them
    concurrently. We must reply only once, or 5 photos would trigger 5 replies.
    """
    gid = message.media_group_id
    if not gid:
        return True
    if gid in _album_seen:
        return False
    if len(_album_seen) > 500:
        _album_seen.clear()
    _album_seen.add(gid)
    return True


async def safe_edit(message, text, markup=None):
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        # screen already shows exactly this — a repeat tap shouldn't create duplicates
        if "message is not modified" in str(e):
            return
        await message.answer(text, reply_markup=markup)
