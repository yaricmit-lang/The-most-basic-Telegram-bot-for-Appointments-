"""Owner/client notifications, references, messaging, and waitlist."""
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
        log.warning("Failed to send message to %s: %s", chat_id, e)
        return False


def _master_targets(appt=None, exclude=None):
    """Who gets system notifications: the master + admins."""
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
    """Day bounds for appointments_between: [date 00:00, next day 00:00)."""
    nxt = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    return date + " 00:00", nxt + " 00:00"


def ext_mark(appt):
    if not appt["ext_nails"]:
        return ""
    note = f", +{appt['ext_price']} €" if appt["ext_price"] else ""
    return f"\n➕ Extension: {appt['ext_nails']} nail(s){note}"


async def master_new_appointment(bot, appt, actor_tg=None):
    text = (
        f"🆕 <b>New booking</b>\n"
        f"📆 {fmt_dt(appt['starts_at'])}–{appt['ends_at'][11:16]} — "
        f"{esc(appt['service_title'])}{ext_mark(appt)}\n"
        f"👤 {_client_line(appt)}\n"
        f"💇 Master: {esc(appt['master_name'])}"
    )
    if appt["created_by"] == "admin":
        text += "\n✍️ Created manually"
    for chat_id in _master_targets(appt, exclude=actor_tg):
        await _send(bot, chat_id, text)


async def master_cancelled(bot, appt, actor_tg=None):
    text = (
        f"❌ <b>Booking cancelled</b>\n"
        f"📆 {fmt_dt(appt['starts_at'])} — {esc(appt['service_title'])}\n"
        f"👤 {_client_line(appt)}"
    )
    for chat_id in _master_targets(appt, exclude=actor_tg):
        await _send(bot, chat_id, text)


async def client_admin_created(bot, appt):
    """Key rule: a booking the owner creates manually is still shown to the client."""
    if not appt["client_tg"]:
        return
    text = (
        f"📌 <b>You've been booked:</b>\n"
        f"💅 {esc(appt['service_title'])}{ext_mark(appt)}\n"
        f"👤 Master: {esc(appt['master_name'])}\n"
        f"📆 {fmt_dt(appt['starts_at'])}\n\n"
        f"If this time doesn't work — cancel the booking."
    )
    markup = kb([
        [btn("✅ Confirm", f"cfm:{appt['id']}")],
        [btn("❌ Cancel", f"ccl:{appt['id']}")],
    ])
    await _send(bot, appt["client_tg"], text, markup)


async def client_cancelled_by_admin(bot, appt):
    if not appt["client_tg"]:
        return
    text = (
        f"😔 Unfortunately your booking was cancelled:\n"
        f"{esc(appt['service_title'])}, {fmt_dt(appt['starts_at'])}\n\n"
        f"You can pick another time."
    )
    markup = kb([[btn("📅 Book", "bk")]])
    await _send(bot, appt["client_tg"], text, markup)


async def run_waitlist(bot, master_id, date, exclude_client=None):
    """A slot opened up — notify whoever was waiting for that day."""
    for w in db.waitlist_for(master_id, date):
        if exclude_client and w["client_id"] == exclude_client:
            continue
        db.mark_waitlist_notified(w["id"])
        if not w["tg_id"]:
            continue
        text = (
            f"🔔 Good news! A slot opened up on {fmt_d(date)}.\n"
            f"Grab it before it's gone:"
        )
        markup = kb([
            [btn("⚡ Book now", f"wlbk:{w['service_id']}:{w['master_id']}:{w['date']}")]
        ])
        await _send(bot, w["tg_id"], text, markup)


# ---------- reference photos ----------

async def _send_media(bot, chat_id, appt):
    """Photos/videos as an album; video notes and GIFs separately (Telegram can't mix them in)."""
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
        log.warning("Reference media failed to send to %s: %s", chat_id, e)


def ref_card(appt):
    text = (
        f"🖼 <b>Reference for the booking</b>\n"
        f"📆 {fmt_dt(appt['starts_at'])} — {esc(appt['service_title'])}\n"
        f"👤 {_client_line(appt)}"
    )
    if appt["ref_comment"]:
        text += f"\n💬 «{esc(appt['ref_comment'])}»"
    _, late = refs_mod.window(appt)
    if late:
        text += "\n\n⚠️ Same-day booking — the reference was sent after the deadline."
    markup = kb([
        [btn("✅ Got it", f"a:refok:{appt['id']}"),
         btn("❌ No materials", f"a:refno:{appt['id']}")],
        [btn("✍️ Message client", f"a:rep:{appt['id']}")],
        [btn("⏰ Reply later", f"a:reflater:{appt['id']}")],
    ])
    return text, markup


async def send_reference_to(bot, chat_id, appt):
    """Media + status-button card to a single recipient."""
    await _send_media(bot, chat_id, appt)
    text, markup = ref_card(appt)
    await _send(bot, chat_id, text, markup)


async def master_reference(bot, appt):
    """Client sent/updated a reference → owner gets the media + a card with buttons."""
    for chat_id in _master_targets(appt):
        await send_reference_to(bot, chat_id, appt)


async def client_ref_ok(bot, appt):
    if not appt["client_tg"]:
        return
    await _send(bot, appt["client_tg"],
                f"✅ The master looked at your reference for {fmt_dt(appt['starts_at'])} — "
                f"all set, materials will be ready. See you soon!")


async def client_master_message(bot, appt, text):
    """Message from the owner to the client (no materials / free text)."""
    if not appt["client_tg"]:
        return False
    body = (
        f"✉️ <b>Message from the master</b>\n"
        f"<i>about the booking on {fmt_dt(appt['starts_at'])}, {esc(appt['service_title'])}</i>\n\n"
        f"{esc(text)}"
    )
    markup = kb([
        [btn("✍️ Reply", f"crep:{appt['id']}")],
        [btn("❌ Cancel booking", f"ccl:{appt['id']}")],
    ])
    return await _send(bot, appt["client_tg"], body, markup)


async def master_client_message(bot, appt, text):
    """Message from the client to the owner."""
    body = (
        f"✉️ <b>Message from the client</b>\n"
        f"👤 {_client_line(appt)}\n"
        f"<i>about the booking on {fmt_dt(appt['starts_at'])}, {esc(appt['service_title'])}</i>\n\n"
        f"{esc(text)}"
    )
    markup = kb([[btn("✍️ Reply", f"a:rep:{appt['id']}")]])
    for chat_id in _master_targets(appt):
        await _send(bot, chat_id, body, markup)


async def morning_digest(bot, date):
    """Daily digest: all bookings + references. Nothing is sent if there are none."""
    appts = db.appointments_between(*_day_bounds(date))
    if not appts:
        return
    lines = [f"☀️ <b>Bookings today: {len(appts)}</b>\n"]
    rows = []
    for a in appts:
        ext = f" ➕{a['ext_nails']} nail(s)" if a["ext_nails"] else ""
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
            lines.append("      ⚠️ no reference sent")
    rows.append([btn("📆 All bookings", "a:apd:0")])
    for chat_id in _master_targets():
        await _send(bot, chat_id, "\n".join(lines), kb(rows))


async def materials_check(bot, date):
    """End of the work day: tomorrow's references still awaiting a reply."""
    pending = [
        a for a in db.appointments_between(*_day_bounds(date))
        if refs_mod.has_ref(a) and not a["ref_status"]
    ]
    if not pending:
        return False
    lines = [
        "🧰 <b>Check materials for tomorrow</b>\n",
        f"References awaiting reply: {len(pending)}\n",
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
    rows.append([btn("📆 Tomorrow's bookings", "a:apd:1")])
    for chat_id in _master_targets():
        await _send(bot, chat_id, "\n".join(lines), kb(rows))
    return True


async def admins_feedback(bot, appt, rating=None, comment=None):
    if rating is not None:
        text = (
            f"⭐ Rating {rating}/5 from {_client_line(appt)}\n"
            f"({esc(appt['service_title'])}, {fmt_dt(appt['starts_at'])})"
        )
    else:
        text = (
            f"💬 Review from {_client_line(appt)}:\n«{esc(comment)}»\n"
            f"({esc(appt['service_title'])}, {fmt_dt(appt['starts_at'])})"
        )
    for chat_id in _master_targets(appt):
        await _send(bot, chat_id, text)
