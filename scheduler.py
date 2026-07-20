"""Background loop: completing past visits and sending scheduled notifications.

All notifications live in the notifications table and survive bot restarts.
Types: confirm24 (24h before, needs a reply), remind2 (2h before),
feedback (after the visit), comeback (invite to return after N days).
"""
import asyncio
import logging
from datetime import datetime, timedelta

import db
import notify
import refs
import slots
from ui import btn, esc, fmt_dt, kb

log = logging.getLogger(__name__)

# If today is a day off, the schedule has no "end of work day" — check in the evening.
EOD_FALLBACK = "20:00"


def schedule_for_appointment(appt_id, starts_at):
    st = datetime.strptime(starts_at, "%Y-%m-%d %H:%M")
    now = datetime.now()
    for typ, hours in (("confirm24", 24), ("remind2", 2)):
        due = st - timedelta(hours=hours)
        if due > now:
            db.add_notification(appt_id, typ, due.strftime("%Y-%m-%d %H:%M"))
    # reminder to send a reference — the evening before, ahead of the deadline
    ref_due = (st - timedelta(days=1)).replace(
        hour=db.get_int("ref_remind_hour", 20), minute=0
    )
    if ref_due > now:
        db.add_notification(appt_id, "refremind", ref_due.strftime("%Y-%m-%d %H:%M"))


def _complete_finished():
    for a in db.to_complete():
        db.set_appointment_status(a["id"], "completed")
        db.add_notification(a["id"], "feedback", db.now_str())
        svc = db.get_service(a["service_id"])
        if svc and svc["comeback_days"]:
            due_date = (datetime.now() + timedelta(days=svc["comeback_days"])).strftime("%Y-%m-%d")
            db.add_notification(a["id"], "comeback", f"{due_date} 12:00")


def _build_message(typ, appt):
    """(text, markup) or None if the notification is no longer relevant."""
    now = db.now_str()
    if typ == "confirm24":
        if appt["status"] != "created" or appt["starts_at"] <= now:
            return None
        text = (
            f"⏰ Reminder: <b>{fmt_dt(appt['starts_at'])}</b> — "
            f"{esc(appt['service_title'])} with {esc(appt['master_name'])}.\n\n"
            f"Please confirm your booking:"
        )
        markup = kb([
            [btn("✅ Confirm", f"cfm:{appt['id']}")],
            [btn("❌ Can't make it", f"ccl:{appt['id']}")],
        ])
        return text, markup
    if typ == "remind2":
        if appt["status"] not in ("created", "confirmed") or appt["starts_at"] <= now:
            return None
        text = (
            f"🔔 We'll see you in 2 hours: {esc(appt['service_title'])}, "
            f"{appt['starts_at'][11:16]}. See you soon!"
        )
        return text, None
    if typ == "feedback":
        if appt["status"] != "completed":
            return None
        text = (
            f"💬 How did it go? Rate your visit\n"
            f"«{esc(appt['service_title'])}» on {fmt_dt(appt['starts_at'])}:"
        )
        markup = kb([[btn(str(n), f"fb:{appt['id']}:{n}") for n in range(1, 6)]])
        return text, markup
    if typ == "refremind":
        if appt["status"] not in ("created", "confirmed") or appt["starts_at"] <= now:
            return None
        if refs.has_ref(appt) or not refs.is_open(appt):
            return None  # already sent, or the window is closed
        deadline, _ = refs.window(appt)
        text = (
            f"💅 Your visit is tomorrow: {esc(appt['service_title'])}, "
            f"{fmt_dt(appt['starts_at'])}.\n\n"
            f"Want to send a design reference? The master will have time to prepare.\n"
            f"Accepted until {fmt_dt(deadline)}."
        )
        markup = kb([
            [btn("📎 Send reference", f"ref:{appt['id']}")],
            [btn("Not needed", "refskip")],
        ])
        return text, markup
    if typ == "comeback":
        if appt["status"] != "completed" or db.client_has_upcoming(appt["client_id"]):
            return None
        svc = db.get_service(appt["service_id"])
        days = svc["comeback_days"] if svc else 21
        period = f"{days // 7} week(s)" if days % 7 == 0 else f"{days} day(s)"
        text = (
            f"✨ It's been {period} since «{esc(appt['service_title'])}» — "
            f"time for a touch-up!\n\nBook again?"
        )
        markup = kb([
            [btn("🔁 Book again", f"rpt:{appt['service_id']}:{appt['master_id']}")],
            [btn("🗂 Different service", "bk")],
        ])
        return text, markup
    return None


async def _send_due(bot):
    for n in db.due_notifications():
        if not db.claim_notification(n["id"]):
            continue  # another process already claimed this notification
        appt = db.get_appointment(n["appointment_id"])
        if not appt:
            continue
        # reference goes to the owner, not the client: the 1-minute delay lets the album merge
        if n["type"] == "refnew":
            if appt["status"] in ("created", "confirmed") and refs.has_ref(appt):
                await notify.master_reference(bot, appt)
            continue
        if not appt["client_tg"]:
            continue
        built = _build_message(n["type"], appt)
        if not built:
            continue
        text, markup = built
        try:
            await bot.send_message(appt["client_tg"], text, reply_markup=markup)
            log.info("Sent notification id=%s type=%s appt=%s client=%s",
                     n["id"], n["type"], appt["id"], appt["client_tg"])
        except Exception as e:
            log.warning("Notification %s to client %s not delivered: %s",
                        n["type"], appt["client_tg"], e)


async def _maybe_digest(bot):
    """Daily digest to the owner, once a day. A 3-hour window makes sure that after
    a long bot downtime the "morning" digest doesn't show up in the evening."""
    hour = db.get_int("digest_hour", 8)
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if not (hour <= now.hour < hour + 3):
        return
    if db.get_setting("last_digest_date") == today:
        return
    db.set_setting("last_digest_date", today)
    await notify.morning_digest(bot, today)


async def _maybe_eod_check(bot):
    """At the end of the work day (per schedule), remind to check tomorrow's
    materials — if any of tomorrow's bookings still have an unanswered reference."""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if db.get_setting("last_eod_date") == today:
        return
    owner = db.owner_master()
    if not owner:
        return
    win = slots.get_work_window(owner["id"], today)
    end_dt = datetime.strptime(f"{today} {win[1] if win else EOD_FALLBACK}", "%Y-%m-%d %H:%M")
    # 3-hour window: after a long bot downtime, the reminder won't fire in the middle of the night
    if not (end_dt <= now < end_dt + timedelta(hours=3)):
        return
    db.set_setting("last_eod_date", today)
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    await notify.materials_check(bot, tomorrow)


async def worker(bot):
    await asyncio.sleep(5)
    while True:
        try:
            _complete_finished()
            await _send_due(bot)
            await _maybe_digest(bot)
            await _maybe_eod_check(bot)
        except Exception:
            log.exception("Error in the background notification loop")
        await asyncio.sleep(60)
