"""Shared helpers: formatting, keyboards, safe message editing."""
import html
import re

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
from i18n import t

_PRICE_NUM = re.compile(r"(\d+(?:[.,]\d+)?)")


def price_with_ext(price_text, extra):
    """Service price plus the extension surcharge.

    Price is stored as text ("40 €", "$40"), so we add to the first number we
    find while keeping the currency: price_with_ext('40 €', 15) -> '55 €'. If
    there's no number in the price, the surcharge is appended as a separate note.
    """
    price_text = price_text or ""
    if not extra:
        return price_text
    m = _PRICE_NUM.search(price_text)
    if m:
        total = float(m.group(1).replace(",", ".")) + extra
        num = str(int(total)) if total == int(total) else f"{total:.2f}"
        return price_text[:m.start()] + num + price_text[m.end():]
    note = f"+{extra} € ({t('ext_surcharge_note')})"
    return f"{price_text} {note}" if price_text else note

WD = (["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"] if config.BOT_LANG != "ru"
      else ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"])
WD_FULL = (["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
           if config.BOT_LANG != "ru" else
           ["Понедельник", "Вторник",
            "Среда", "Четверг", "Пятница",
            "Суббота", "Воскресенье"])


def esc(v):
    return html.escape(str(v or ""))


def btn(text, cb):
    return InlineKeyboardButton(text=text, callback_data=cb)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


_HOUR = "h" if config.BOT_LANG != "ru" else "ч"
_MIN = "min" if config.BOT_LANG != "ru" else "мин"


def fmt_dur(minutes):
    h, m = divmod(int(minutes), 60)
    if h and m:
        return f"{h} {_HOUR} {m} {_MIN}"
    if h:
        return f"{h} {_HOUR}"
    return f"{m} {_MIN}"


_GLUE = "at" if config.BOT_LANG != "ru" else "в"


def fmt_dt(s):
    """'2026-07-14 10:00' -> '14.07 at 10:00' ('14.07 в 10:00' for ru)"""
    return f"{s[8:10]}.{s[5:7]} {_GLUE} {s[11:16]}"


def fmt_d(s):
    """'2026-07-14' -> '14.07'"""
    return f"{s[8:10]}.{s[5:7]}"


def date_label(d):
    return f"{WD[d.weekday()]} {d.strftime('%d.%m')}"


def main_menu_kb(admin=False):
    rows = [
        [btn(t("btn_go_booking"), "bk")],
        [btn(t("btn_my_appts"), "my")],
        [btn(t("btn_my_profile"), "prof")],
    ]
    if admin:
        rows.append([btn(t("btn_master_panel"), "a:menu")])
    return kb(rows)


_album_seen = set()


def first_of_album(message):
    """True if this is the first message of an album.

    Telegram sends an album as several messages, and aiogram processes them
    concurrently. We must answer exactly once, or 5 photos get 5 replies.
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
        # the screen already shows exactly this -- a repeat tap shouldn't duplicate it
        if "message is not modified" in str(e):
            return
        await message.answer(text, reply_markup=markup)
