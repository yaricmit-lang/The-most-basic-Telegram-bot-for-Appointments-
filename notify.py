"""Уведомления мастеру/клиенту, референсы, переписка и лист ожидания."""
import logging
from datetime import datetime, timedelta

from aiogram.types import InputMediaPhoto, InputMediaVideo

import config
import db
import refs as refs_mod
from ui import btn, esc, fmt_d, fmt_dt, kb

log = logging.getLogger(__name__)


async def _send(bot, chat_id, text, markup=None):
    try:
        await bot.send_message(chat_id, text, reply_markup=markup)
        return True
    except Exception as e:
        log.warning("Не удалось отправить сообщение %s: %s", chat_id, e)
        return False


def _master_targets(appt=None, exclude=None):
    """Кому слать служебные уведомления: мастеру + админам."""
    ids = set(config.ADMIN_IDS)
    if appt is not None and appt["master_tg"]:
        ids.add(appt["master_tg"])
    if appt is None:
        owner = db.owner_master()
        if owner and owner["tg_id"]:
            ids.add(owner["tg_id"])
    if exclude:
        ids.discard(exclude)
    return ids


def _client_line(appt):
    line = f"{esc(appt['client_name'])} {esc(appt['client_phone'])}"
    if appt["client_username"]:
        line += f" @{esc(appt['client_username'])}"
    return line


def _day_bounds(date):
    """Границы дня для appointments_between: [date 00:00, следующий день 00:00)."""
    nxt = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    return date + " 00:00", nxt + " 00:00"


def ext_mark(appt):
    if not appt["ext_nails"]:
        return ""
    note = f", +{appt['ext_price']} €" if appt["ext_price"] else ""
    return f"\n➕ Наращивание: {appt['ext_nails']} ног.{note}"


async def master_new_appointment(bot, appt, actor_tg=None):
    text = (
        f"🆕 <b>Новая запись</b>\n"
        f"📆 {fmt_dt(appt['starts_at'])}–{appt['ends_at'][11:16]} — "
        f"{esc(appt['service_title'])}{ext_mark(appt)}\n"
        f"👤 {_client_line(appt)}\n"
        f"💇 Мастер: {esc(appt['master_name'])}"
    )
    if appt["created_by"] == "admin":
        text += "\n✍️ Создана вручную"
    for chat_id in _master_targets(appt, exclude=actor_tg):
        await _send(bot, chat_id, text)


async def master_cancelled(bot, appt, actor_tg=None):
    text = (
        f"❌ <b>Отмена записи</b>\n"
        f"📆 {fmt_dt(appt['starts_at'])} — {esc(appt['service_title'])}\n"
        f"👤 {_client_line(appt)}"
    )
    for chat_id in _master_targets(appt, exclude=actor_tg):
        await _send(bot, chat_id, text)


async def client_admin_created(bot, appt):
    """Ключевое: запись, созданную админом вручную, клиент тоже видит."""
    if not appt["client_tg"]:
        return
    text = (
        f"📌 <b>Вас записали:</b>\n"
        f"💅 {esc(appt['service_title'])}{ext_mark(appt)}\n"
        f"👤 Мастер: {esc(appt['master_name'])}\n"
        f"📆 {fmt_dt(appt['starts_at'])}\n\n"
        f"Если время не подходит — отмените запись."
    )
    markup = kb([
        [btn("✅ Подтверждаю", f"cfm:{appt['id']}")],
        [btn("❌ Отменить", f"ccl:{appt['id']}")],
    ])
    await _send(bot, appt["client_tg"], text, markup)


async def client_cancelled_by_admin(bot, appt):
    if not appt["client_tg"]:
        return
    text = (
        f"😔 К сожалению, ваша запись отменена:\n"
        f"{esc(appt['service_title'])}, {fmt_dt(appt['starts_at'])}\n\n"
        f"Вы можете выбрать другое время."
    )
    markup = kb([[btn("📅 Записаться", "bk")]])
    await _send(bot, appt["client_tg"], text, markup)


async def run_waitlist(bot, master_id, date, exclude_client=None):
    """Освободилось окно — зовём тех, кто ждал этот день."""
    for w in db.waitlist_for(master_id, date):
        if exclude_client and w["client_id"] == exclude_client:
            continue
        db.mark_waitlist_notified(w["id"])
        if not w["tg_id"]:
            continue
        text = (
            f"🔔 Хорошие новости! На {fmt_d(date)} освободилось окно.\n"
            f"Успейте записаться:"
        )
        markup = kb([
            [btn("⚡ Записаться", f"wlbk:{w['service_id']}:{w['master_id']}:{w['date']}")]
        ])
        await _send(bot, w["tg_id"], text, markup)


# ---------- референсы ----------

async def _send_media(bot, chat_id, appt):
    """Фото/видео альбомом, кружочки и GIF — отдельно (Telegram не мешает их в альбом)."""
    group, singles = [], []
    for r in db.refs_for(appt["id"]):
        if r["media_type"] == "photo":
            group.append(InputMediaPhoto(media=r["file_id"]))
        elif r["media_type"] == "video":
            group.append(InputMediaVideo(media=r["file_id"]))
        else:
            singles.append(r)
    try:
        if group:
            await bot.send_media_group(chat_id, media=group[:10])
        for r in singles:
            if r["media_type"] == "video_note":
                await bot.send_video_note(chat_id, r["file_id"])
            else:
                await bot.send_animation(chat_id, r["file_id"])
    except Exception as e:
        log.warning("Медиа референса не отправлены в %s: %s", chat_id, e)


def ref_card(appt):
    text = (
        f"🖼 <b>Референс к записи</b>\n"
        f"📆 {fmt_dt(appt['starts_at'])} — {esc(appt['service_title'])}\n"
        f"👤 {_client_line(appt)}"
    )
    if appt["ref_comment"]:
        text += f"\n💬 «{esc(appt['ref_comment'])}»"
    _, late = refs_mod.window(appt)
    if late:
        text += "\n\n⚠️ Запись в тот же день — референс прислан после дедлайна."
    markup = kb([
        [btn("✅ Всё есть", f"a:refok:{appt['id']}"),
         btn("❌ Нет материалов", f"a:refno:{appt['id']}")],
        [btn("✍️ Написать клиенту", f"a:rep:{appt['id']}")],
        [btn("⏰ Ответить позже", f"a:reflater:{appt['id']}")],
    ])
    return text, markup


async def send_reference_to(bot, chat_id, appt):
    """Медиа + карточка с кнопками-статусами одному адресату."""
    await _send_media(bot, chat_id, appt)
    text, markup = ref_card(appt)
    await _send(bot, chat_id, text, markup)


async def master_reference(bot, appt):
    """Клиент прислал/обновил референс → мастеру медиа + карточка с кнопками."""
    for chat_id in _master_targets(appt):
        await send_reference_to(bot, chat_id, appt)


async def client_ref_ok(bot, appt):
    if not appt["client_tg"]:
        return
    await _send(bot, appt["client_tg"],
                f"✅ Мастер посмотрел ваш референс к записи {fmt_dt(appt['starts_at'])} — "
                f"всё есть, материалы подготовит. До встречи!")


async def client_master_message(bot, appt, text):
    """Сообщение мастера клиенту (нет материалов / произвольное)."""
    if not appt["client_tg"]:
        return False
    body = (
        f"✉️ <b>Сообщение от мастера</b>\n"
        f"<i>по записи {fmt_dt(appt['starts_at'])}, {esc(appt['service_title'])}</i>\n\n"
        f"{esc(text)}"
    )
    markup = kb([
        [btn("✍️ Ответить", f"crep:{appt['id']}")],
        [btn("❌ Отменить запись", f"ccl:{appt['id']}")],
    ])
    return await _send(bot, appt["client_tg"], body, markup)


async def master_client_message(bot, appt, text):
    """Сообщение клиента мастеру."""
    body = (
        f"✉️ <b>Сообщение от клиента</b>\n"
        f"👤 {_client_line(appt)}\n"
        f"<i>по записи {fmt_dt(appt['starts_at'])}, {esc(appt['service_title'])}</i>\n\n"
        f"{esc(text)}"
    )
    markup = kb([[btn("✍️ Ответить", f"a:rep:{appt['id']}")]])
    for chat_id in _master_targets(appt):
        await _send(bot, chat_id, body, markup)


async def morning_digest(bot, date):
    """Сводка на день: все записи + референсы. Ничего не шлём, если записей нет."""
    appts = db.appointments_between(*_day_bounds(date))
    if not appts:
        return
    lines = [f"☀️ <b>Сегодня записей: {len(appts)}</b>\n"]
    rows = []
    for a in appts:
        ext = f" ➕{a['ext_nails']} ног." if a["ext_nails"] else ""
        lines.append(
            f"🕐 <b>{a['starts_at'][11:16]}–{a['ends_at'][11:16]}</b> — "
            f"{esc(a['service_title'])}{ext}, "
            f"{esc(a['client_name'] or '—')} {esc(a['client_phone'] or '')}"
        )
        s = refs_mod.summary(a)
        if s:
            mark = {"ok": "✅", "no_materials": "❌"}.get(a["ref_status"], "🖼")
            lines.append(f"      {mark} {esc(s)}")
            rows.append([btn(f"🖼 {a['starts_at'][11:16]} {a['client_name'] or ''}".strip(),
                             f"a:refv:{a['id']}")])
        else:
            lines.append("      ⚠️ референс не прислан")
    rows.append([btn("📆 Все записи", "a:apd:0")])
    for chat_id in _master_targets():
        await _send(bot, chat_id, "\n".join(lines), kb(rows))


async def materials_check(bot, date):
    """Конец рабочего дня: референсы на завтра, оставшиеся без ответа мастера."""
    pending = [
        a for a in db.appointments_between(*_day_bounds(date))
        if refs_mod.has_ref(a) and not a["ref_status"]
    ]
    if not pending:
        return False
    lines = [
        "🧰 <b>Проверьте материалы на завтра</b>\n",
        f"Референсов без ответа: {len(pending)}\n",
    ]
    rows = []
    for a in pending:
        lines.append(f"🕐 <b>{a['starts_at'][11:16]}</b> — {esc(a['client_name'] or '—')}, "
                     f"{esc(a['service_title'])}")
        s = refs_mod.summary(a)
        if s:
            lines.append(f"      🖼 {esc(s)}")
        rows.append([btn(f"🖼 {a['starts_at'][11:16]} {a['client_name'] or ''}".strip(),
                         f"a:refv:{a['id']}")])
    rows.append([btn("📆 Записи на завтра", "a:apd:1")])
    for chat_id in _master_targets():
        await _send(bot, chat_id, "\n".join(lines), kb(rows))
    return True


async def admins_feedback(bot, appt, rating=None, comment=None):
    if rating is not None:
        text = (
            f"⭐ Оценка {rating}/5 от {_client_line(appt)}\n"
            f"({esc(appt['service_title'])}, {fmt_dt(appt['starts_at'])})"
        )
    else:
        text = (
            f"💬 Отзыв от {_client_line(appt)}:\n«{esc(comment)}»\n"
            f"({esc(appt['service_title'])}, {fmt_dt(appt['starts_at'])})"
        )
    for chat_id in _master_targets(appt):
        await _send(bot, chat_id, text)
