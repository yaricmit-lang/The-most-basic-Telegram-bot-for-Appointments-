"""Общие хелперы: форматирование, клавиатуры, безопасное редактирование."""
import html
import re

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

_PRICE_NUM = re.compile(r"(\d+(?:[.,]\d+)?)")


def price_with_ext(price_text, extra):
    """Цена услуги + доплата за наращивание.

    Цена хранится текстом («40€», «40 евро»), поэтому прибавляем к первому числу,
    сохраняя валюту: price_with_ext('40€', 15) -> '55€'. Если числа в цене нет —
    доплата дописывается отдельной пометкой.
    """
    price_text = price_text or ""
    if not extra:
        return price_text
    m = _PRICE_NUM.search(price_text)
    if m:
        total = float(m.group(1).replace(",", ".")) + extra
        num = str(int(total)) if total == int(total) else f"{total:.2f}"
        return price_text[:m.start()] + num + price_text[m.end():]
    note = f"+{extra} € (наращивание)"
    return f"{price_text} {note}" if price_text else note

WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
WD_FULL = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


def esc(v):
    return html.escape(str(v or ""))


def btn(text, cb):
    return InlineKeyboardButton(text=text, callback_data=cb)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def fmt_dur(minutes):
    h, m = divmod(int(minutes), 60)
    if h and m:
        return f"{h} ч {m} мин"
    if h:
        return f"{h} ч"
    return f"{m} мин"


def fmt_dt(s):
    """'2026-07-14 10:00' -> '14.07 в 10:00'"""
    return f"{s[8:10]}.{s[5:7]} в {s[11:16]}"


def fmt_d(s):
    """'2026-07-14' -> '14.07'"""
    return f"{s[8:10]}.{s[5:7]}"


def date_label(d):
    return f"{WD[d.weekday()]} {d.strftime('%d.%m')}"


def main_menu_kb(admin=False):
    rows = [
        [btn("📅 Записаться", "bk")],
        [btn("📋 Мои записи", "my")],
        [btn("👤 Мои данные", "prof")],
    ]
    if admin:
        rows.append([btn("⚙️ Панель мастера", "a:menu")])
    return kb(rows)


_album_seen = set()


def first_of_album(message):
    """True — если это первое сообщение альбома.

    Telegram присылает альбом несколькими сообщениями, а aiogram обрабатывает их
    параллельно. Отвечать нужно один раз, иначе на 5 фото прилетит 5 ответов.
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
        # экран уже показывает ровно это — повторный клик не должен плодить дубликаты
        if "message is not modified" in str(e):
            return
        await message.answer(text, reply_markup=markup)
