"""Notifications to the master/client, references, chat, and waitlist."""
import logging
from datetime import datetime, timedelta

from aiogram.types import InputMediaPhoto, InputMediaVideo

import config
import db
import refs as refs_mod
from i18n import t
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
    """Who gets operational notifications: the master plus the admins."""
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
    """Day boundaries for appointments_between: [date 00:00, next day 00:00)."""
    nxt = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    return date + " 00:00", nxt + " 00:00"


def ext_mark(appt):
    if not appt["ext_nails"]:
        return ""
    note = f", +{appt['ext_price']} €" if appt["ext_price"] else ""
    return t("ext_mark", n=appt["ext_nails"], note=note)


async def master_new_appointment(bot, appt, actor_tg=None):
    text = t("notify_new_appt", when=fmt_dt(appt["starts_at"]), end=appt["ends_at"][11:16],
             service=esc(appt["service_title"]), ext=ext_mark(appt),
             client=_client_line(appt), master=esc(appt["master_name"]))
    if appt["created_by"] == "admin":
        text += t("notify_created_by_admin")
    for chat_id in _master_targets(appt, exclude=actor_tg):
        await _send(bot, chat_id, text)


async def master_cancelled(bot, appt, actor_tg=None):
    text = t("notify_cancelled", when=fmt_dt(appt["starts_at"]),
             service=esc(appt["service_title"]), client=_client_line(appt))
    for chat_id in _master_targets(appt, exclude=actor_tg):
        await _send(bot, chat_id, text)


async def client_admin_created(bot, appt):
    """Key behavior: a booking an admin creates manually is still shown to the client."""
    if not appt["client_tg"]:
        return
    text = t("notify_you_were_booked", service=esc(appt["service_title"]), ext=ext_mark(appt),
             master=esc(appt["master_name"]), when=fmt_dt(appt["starts_at"]))
    markup = kb([
        [btn(t("btn_confirm_yes"), f"cfm:{appt['id']}")],
        [btn(t("btn_cancel_x"), f"ccl:{appt['id']}")],
    ])
    await _send(bot, appt["client_tg"], text, markup)


async def client_cancelled_by_admin(bot, appt):
    if not appt["client_tg"]:
        return
    text = t("notify_cancelled_by_admin", service=esc(appt["service_title"]),
             when=fmt_dt(appt["starts_at"]))
    markup = kb([[btn(t("btn_go_booking"), "bk")]])
    await _send(bot, appt["client_tg"], text, markup)


async def run_waitlist(bot, master_id, date, exclude_client=None):
    """A slot opened up -- notify whoever was waiting for this day."""
    for w in db.waitlist_for(master_id, date):
        if exclude_client and w["client_id"] == exclude_client:
            continue
        db.mark_waitlist_notified(w["id"])
        if not w["tg_id"]:
            continue
        text = t("notify_waitlist_free", date=fmt_d(date))
        markup = kb([
            [btn(t("btn_choose_time"), f"wlbk:{w['service_id']}:{w['master_id']}:{w['date']}")]
        ])
        await _send(bot, w["tg_id"], text, markup)


# ---------- references ----------

async def _send_media(bot, chat_id, appt):
    """Photos/videos as an album; video notes and GIFs separately (Telegram
    won't mix them into an album)."""
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
        log.warning("Failed to send reference media to %s: %s", chat_id, e)


def ref_card(appt):
    text = t("ref_card_title", when=fmt_dt(appt["starts_at"]), service=esc(appt["service_title"]),
              client=_client_line(appt))
    if appt["ref_comment"]:
        text += t("ref_card_comment", comment=esc(appt["ref_comment"]))
    _, late = refs_mod.window(appt)
    if late:
        text += t("ref_card_late_warn")
    markup = kb([
        [btn(t("btn_all_set"), f"a:refok:{appt['id']}"),
         btn(t("btn_no_materials"), f"a:refno:{appt['id']}")],
        [btn(t("btn_message_client"), f"a:rep:{appt['id']}")],
        [btn(t("btn_answer_later"), f"a:reflater:{appt['id']}")],
    ])
    return text, markup


async def send_reference_to(bot, chat_id, appt):
    """Media plus the status-button card, to one recipient."""
    await _send_media(bot, chat_id, appt)
    text, markup = ref_card(appt)
    await _send(bot, chat_id, text, markup)


async def master_reference(bot, appt):
    """The client sent/updated a reference -> the master gets media + the status card."""
    for chat_id in _master_targets(appt):
        await send_reference_to(bot, chat_id, appt)


async def client_ref_ok(bot, appt):
    if not appt["client_tg"]:
        return
    await _send(bot, appt["client_tg"], t("notify_client_ref_ok", when=fmt_dt(appt["starts_at"])))


async def client_master_message(bot, appt, text):
    """A message from the master to the client (no-materials or free text)."""
    if not appt["client_tg"]:
        return False
    body = t("notify_master_message_header", when=fmt_dt(appt["starts_at"]),
             service=esc(appt["service_title"]), text=esc(text))
    markup = kb([
        [btn(t("btn_write_master"), f"crep:{appt['id']}")],
        [btn(t("btn_cancel_appt"), f"ccl:{appt['id']}")],
    ])
    return await _send(bot, appt["client_tg"], body, markup)


async def master_client_message(bot, appt, text):
    """A message from the client to the master."""
    body = t("notify_client_message_header", client=_client_line(appt),
             when=fmt_dt(appt["starts_at"]), service=esc(appt["service_title"]), text=esc(text))
    markup = kb([[btn(t("btn_write_master"), f"a:rep:{appt['id']}")]])
    for chat_id in _master_targets(appt):
        await _send(bot, chat_id, body, markup)


async def morning_digest(bot, date):
    """Digest for the day: all bookings + references. Nothing is sent if empty."""
    appts = db.appointments_between(*_day_bounds(date))
    if not appts:
        return
    lines = [t("morning_digest_title", n=len(appts))]
    rows = []
    for a in appts:
        ext = f" ➕{a['ext_nails']} nails" if a["ext_nails"] else ""
        lines.append(
            t("morning_digest_line", start=a["starts_at"][11:16], end=a["ends_at"][11:16],
              service=esc(a["service_title"]), ext=ext,
              client=esc(a["client_name"] or "—"), phone=esc(a["client_phone"] or ""))
        )
        s = refs_mod.summary(a)
        if s:
            mark = {"ok": "✅", "no_materials": "❌"}.get(a["ref_status"], "\U0001F5BC")
            lines.append(f"      {mark} {esc(s)}")
            rows.append([btn(f"\U0001F5BC {a['starts_at'][11:16]} {a['client_name'] or ''}".strip(),
                             f"a:refv:{a['id']}")])
        else:
            lines.append(t("morning_digest_no_ref"))
    rows.append([btn(t("morning_digest_appts_btn"), "a:apd:0")])
    for chat_id in _master_targets():
        await _send(bot, chat_id, "\n".join(lines), kb(rows))


async def materials_check(bot, date):
    """End of the working day: tomorrow's references still awaiting a reply."""
    pending = [
        a for a in db.appointments_between(*_day_bounds(date))
        if refs_mod.has_ref(a) and not a["ref_status"]
    ]
    if not pending:
        return False
    lines = [
        t("materials_check_title"),
        t("materials_check_count", n=len(pending)),
    ]
    rows = []
    for a in pending:
        lines.append(t("materials_check_line", time=a["starts_at"][11:16],
                        client=esc(a["client_name"] or "—"), service=esc(a["service_title"])))
        s = refs_mod.summary(a)
        if s:
            lines.append(f"      \U0001F5BC {esc(s)}")
        rows.append([btn(f"\U0001F5BC {a['starts_at'][11:16]} {a['client_name'] or ''}".strip(),
                         f"a:refv:{a['id']}")])
    rows.append([btn(t("materials_check_btn"), "a:apd:1")])
    for chat_id in _master_targets():
        await _send(bot, chat_id, "\n".join(lines), kb(rows))
    return True


async def admins_feedback(bot, appt, rating=None, comment=None):
    if rating is not None:
        text = t("notify_rating", rating=rating, client=_client_line(appt),
                 service=esc(appt["service_title"]), when=fmt_dt(appt["starts_at"]))
    else:
        text = t("notify_review", client=_client_line(appt), comment=esc(comment),
                 service=esc(appt["service_title"]), when=fmt_dt(appt["starts_at"]))
    for chat_id in _master_targets(appt):
        await _send(bot, chat_id, text)
