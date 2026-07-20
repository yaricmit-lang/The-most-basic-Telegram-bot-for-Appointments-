"""Owner panel: bookings, services, masters, schedule, groups, manual booking, settings.

All admin callback_data strings start with "a:" so they never collide with client ones.
"""
import logging
import re
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InputMediaPhoto, Message

import config
import db
import notify
import refs
import scheduler
import slots as sl
import ui
from ui import WD, WD_FULL, btn, esc, fmt_d, fmt_dt, fmt_dur, kb

log = logging.getLogger(__name__)

router = Router(name="admin")
router.message.filter(F.from_user.id.in_(config.ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(config.ADMIN_IDS))


class Adm(StatesGroup):
    # adding a service
    svc_title = State()
    svc_dur = State()
    svc_price = State()
    svc_cap = State()
    svc_comeback = State()
    svc_edit = State()      # data: field, sid
    svc_photo = State()     # data: ph_sid
    # masters
    m_name = State()
    m_tg = State()
    m_edit = State()        # data: field, mid
    # schedule
    sched = State()         # data: sched_master, sched_wd
    ovr_date = State()      # data: ovr_master
    ovr_val = State()
    # groups
    gs_dt = State()         # data: gs_service, gs_master
    gs_cap = State()
    # settings
    set_val = State()       # data: set_key
    owner_name = State()    # owner/master name (shown to clients)
    # messaging the client
    reply_text = State()    # data: rep_aid
    nomat_text = State()    # data: nomat_aid
    # manual booking
    nb_time = State()
    nb_client = State()
    nb_name = State()
    nb_phone = State()


# ---------- input parsing ----------

TIME_RANGE = re.compile(r"(\d{1,2})[:.](\d{2})\s*[-–—]\s*(\d{1,2})[:.](\d{2})")
TIME_RE = re.compile(r"^(\d{1,2})[:.](\d{2})$")
DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?$")
DT_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\s+(\d{1,2})[:.](\d{2})$")


def parse_schedule_text(text):
    """'10:00-19:00 lunch 14:00-15:00' | '10:00-19:00' | 'day off' -> dict | None"""
    t = text.strip().lower()
    if "off" in t or t in ("-", "no", "none"):
        return {"day_off": True, "ws": None, "we": None, "bs": None, "be": None}
    ranges = TIME_RANGE.findall(t)
    if not ranges:
        return None

    def norm(r):
        a, b = f"{int(r[0]):02d}:{r[1]}", f"{int(r[2]):02d}:{r[3]}"
        return (a, b) if a < b else None

    work = norm(ranges[0])
    if not work:
        return None
    bs = be = None
    if len(ranges) > 1:
        lunch = norm(ranges[1])
        if not lunch:
            return None
        bs, be = lunch
    return {"day_off": False, "ws": work[0], "we": work[1], "bs": bs, "be": be}


def parse_date(text):
    m = DATE_RE.match(text.strip())
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    year = m.group(3)
    now = datetime.now()
    try:
        if year:
            y = int(year)
            if y < 100:
                y += 2000
            d = datetime(y, month, day)
        else:
            d = datetime(now.year, month, day)
            if d.date() < now.date():
                d = datetime(now.year + 1, month, day)
    except ValueError:
        return None
    return d.strftime("%Y-%m-%d")


def parse_datetime(text):
    m = DT_RE.match(text.strip())
    if not m:
        return None
    ds = parse_date(f"{m.group(1)}.{m.group(2)}" + (f".{m.group(3)}" if m.group(3) else ""))
    if not ds:
        return None
    h, mi = int(m.group(4)), int(m.group(5))
    if h > 23 or mi > 59:
        return None
    return f"{ds} {h:02d}:{mi:02d}"


def parse_time(text):
    m = TIME_RE.match(text.strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


# ---------- panel ----------

def panel_text():
    today = datetime.now().date()
    n = len(db.appointments_between(today.isoformat() + " 00:00",
                                    (today + timedelta(days=1)).isoformat() + " 00:00"))
    return f"⚙️ <b>Owner panel</b>\nBookings today: {n}"


def panel_kb():
    return kb([
        [btn("📆 Bookings", "a:ap"), btn("➕ New booking", "a:nb")],
        [btn("🧾 Services", "a:svcs"), btn("📅 Schedule", "a:sch")],
        [btn("⚙️ Settings", "a:set")],
        [btn("◀️ Main menu", "menu")],
    ])


@router.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(panel_text(), reply_markup=panel_kb())


@router.callback_query(F.data == "a:menu")
async def cb_admin_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await ui.safe_edit(cb.message, panel_text(), panel_kb())
    await cb.answer()


# ---------- bookings ----------

@router.callback_query(F.data == "a:ap")
async def cb_appts(cb: CallbackQuery):
    await ui.safe_edit(cb.message, "Which period should I show?", kb([
        [btn("Today", "a:apd:0"), btn("Tomorrow", "a:apd:1")],
        [btn("Week", "a:apd:7")],
        [btn("◀️ Back", "a:menu")],
    ]))
    await cb.answer()


@router.callback_query(F.data.startswith("a:apd:"))
async def cb_appts_day(cb: CallbackQuery):
    n = int(cb.data.split(":")[2])
    today = datetime.now().date()
    if n == 7:
        d1, d2 = today, today + timedelta(days=7)
        title = "for the week"
    else:
        d1 = today + timedelta(days=n)
        d2 = d1 + timedelta(days=1)
        title = "for " + ("today" if n == 0 else "tomorrow")
    appts = db.appointments_between(d1.isoformat() + " 00:00", d2.isoformat() + " 00:00")

    if not appts:
        await ui.safe_edit(cb.message, f"No bookings {title}.",
                           kb([[btn("◀️ Back", "a:ap")]]))
        await cb.answer()
        return

    lines = [f"📆 <b>Bookings {title}:</b>"]
    rows = []
    cur_date = None
    for a in appts:
        d = a["starts_at"][:10]
        if d != cur_date:
            cur_date = d
            lines.append(f"\n<b>{fmt_d(d)}</b>")
        mark = "✅" if a["status"] == "confirmed" else "🕐"
        who = esc(a["client_name"] or "—")
        ext = f" ➕{a['ext_nails']} nail(s)" if a["ext_nails"] else ""
        lines.append(f"{mark} {a['starts_at'][11:16]}–{a['ends_at'][11:16]} "
                     f"{esc(a['service_title'])}{ext} — {who} {esc(a['client_phone'] or '')}")
        summary = refs.summary(a)
        if summary:
            status = {"ok": "✅", "no_materials": "❌"}.get(a["ref_status"], "🖼")
            lines.append(f"      {status} {esc(summary)}")
        if len(rows) < 20:
            row = [btn(f"❌ {fmt_dt(a['starts_at'])} {a['client_name'] or ''}",
                       f"a:ccl:{a['id']}")]
            if summary:
                row.append(btn("🖼 Reference", f"a:refv:{a['id']}"))
            rows.append(row)
    rows.append([btn("◀️ Back", "a:ap")])
    await ui.safe_edit(cb.message, "\n".join(lines), kb(rows))
    await cb.answer()


@router.callback_query(F.data.startswith("a:ccl:"))
async def cb_admin_cancel_ask(cb: CallbackQuery):
    aid = int(cb.data.split(":")[2])
    a = db.get_appointment(aid)
    if not a or a["status"] in ("cancelled", "completed"):
        await cb.answer("No longer active", show_alert=True)
        return
    await ui.safe_edit(
        cb.message,
        f"Cancel this booking?\n\n{fmt_dt(a['starts_at'])} — {esc(a['service_title'])}\n"
        f"Client: {esc(a['client_name'])} {esc(a['client_phone'] or '')}",
        kb([[btn("❌ Yes, cancel", f"a:ccl2:{aid}")], [btn("◀️ Back", "a:ap")]]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("a:ccl2:"))
async def cb_admin_cancel_do(cb: CallbackQuery):
    aid = int(cb.data.split(":")[2])
    a = db.get_appointment(aid)
    if not a or a["status"] in ("cancelled", "completed"):
        await cb.answer("No longer active", show_alert=True)
        return
    db.set_appointment_status(aid, "cancelled")
    db.drop_pending_notifications(aid)
    await notify.client_cancelled_by_admin(cb.bot, a)
    await notify.run_waitlist(cb.bot, a["master_id"], a["starts_at"][:10],
                              exclude_client=a["client_id"])
    await ui.safe_edit(cb.message, "Booking cancelled, client notified.",
                       kb([[btn("◀️ To bookings", "a:ap")]]))
    await cb.answer()


# ---------- references and messaging the client ----------

NO_MATERIALS_TEMPLATE = (
    "Unfortunately I don't have suitable materials for this design. "
    "Shall we find an alternative?"
)


@router.callback_query(F.data.startswith("a:refv:"))
async def cb_ref_view(cb: CallbackQuery):
    a = db.get_appointment(int(cb.data.split(":")[2]))
    if not a:
        await cb.answer("Booking not found", show_alert=True)
        return
    await notify.send_reference_to(cb.bot, cb.from_user.id, a)
    await cb.answer()


@router.callback_query(F.data.startswith("a:refok:"))
async def cb_ref_ok(cb: CallbackQuery):
    aid = int(cb.data.split(":")[2])
    a = db.get_appointment(aid)
    if not a:
        await cb.answer("Booking not found", show_alert=True)
        return
    # atomic: "materials are ready" is sent to the client exactly once
    if not db.set_ref_status_from(aid, "ok", ("", "no_materials")):
        await cb.answer("Already confirmed")
        return
    await notify.client_ref_ok(cb.bot, a)
    text, _ = notify.ref_card(db.get_appointment(aid))
    # remove the status buttons: a repeat tap would send the client a second confirmation
    await ui.safe_edit(cb.message, text + "\n\n✅ You confirmed: materials are ready.", kb([
        [btn("↩️ Change answer", f"a:refv:{aid}")],
        [btn("◀️ Owner panel", "a:menu")],
    ]))
    await cb.answer("Client notified")


@router.callback_query(F.data.startswith("a:reflater:"))
async def cb_ref_later(cb: CallbackQuery):
    aid = int(cb.data.split(":")[2])
    a = db.get_appointment(aid)
    if not a:
        await cb.answer("Booking not found", show_alert=True)
        return
    text, _ = notify.ref_card(a)
    # the reference stays without a status — the end-of-day check will pick it up
    if a["starts_at"][:10] > datetime.now().strftime("%Y-%m-%d"):
        note = "⏰ <b>Postponed.</b> I'll remind you the evening before the visit, at end of day."
    else:
        note = "⏰ <b>Postponed.</b> The reference stays in «📆 Bookings»."
    await ui.safe_edit(cb.message, text + "\n\n" + note, kb([
        [btn("↩️ Reply now", f"a:refv:{aid}")],
        [btn("◀️ Owner panel", "a:menu")],
    ]))
    await cb.answer("Postponed")


@router.callback_query(F.data.startswith("a:refno:"))
async def cb_ref_no(cb: CallbackQuery, state: FSMContext):
    aid = int(cb.data.split(":")[2])
    a = db.get_appointment(aid)
    if not a:
        await cb.answer("Booking not found", show_alert=True)
        return
    await state.set_state(Adm.nomat_text)
    await state.update_data(nomat_aid=aid)
    await cb.message.answer(
        f"Ready-made text for {esc(a['client_name'] or '')}:\n\n"
        f"«{esc(NO_MATERIALS_TEMPLATE)}»\n\n"
        f"Send as-is — or type your own text and it'll be appended to this message.",
        reply_markup=kb([
            [btn("📨 Send as-is", f"a:nomatgo:{aid}")],
            [btn("◀️ Cancel", "a:menu")],
        ]),
    )
    await cb.answer()


async def _send_no_materials(bot, message, aid, text):
    a = db.get_appointment(aid)
    if not a:
        await message.answer("Booking not found")
        return
    db.set_ref_status(aid, "no_materials")
    db.add_message(aid, "master", text)
    sent = await notify.client_master_message(bot, a, text)
    note = ("✅ Message sent to the client." if sent
            else "⚠️ The client has no Telegram — the message wasn't delivered, call them.")
    await message.answer(note, reply_markup=kb([[btn("◀️ Panel", "a:menu")]]))


@router.callback_query(F.data.startswith("a:nomatgo:"))
async def cb_nomat_send(cb: CallbackQuery, state: FSMContext):
    aid = int(cb.data.split(":")[2])
    await state.clear()
    await _send_no_materials(cb.bot, cb.message, aid, NO_MATERIALS_TEMPLATE)
    await cb.answer("Sent")


@router.message(Adm.nomat_text, F.text)
async def got_nomat_text(m: Message, state: FSMContext):
    if m.text.startswith("/"):
        await state.clear()
        return
    data = await state.get_data()
    aid = data.get("nomat_aid")
    await state.clear()
    if aid:
        await _send_no_materials(m.bot, m, aid,
                                 NO_MATERIALS_TEMPLATE + "\n\n" + m.text.strip()[:1000])


@router.callback_query(F.data.startswith("a:rep:"))
async def cb_reply(cb: CallbackQuery, state: FSMContext):
    aid = int(cb.data.split(":")[2])
    a = db.get_appointment(aid)
    if not a:
        await cb.answer("Booking not found", show_alert=True)
        return
    await state.set_state(Adm.reply_text)
    await state.update_data(rep_aid=aid)
    await cb.message.answer(
        f"✍️ What should I tell {esc(a['client_name'] or '')} "
        f"about the booking on {fmt_dt(a['starts_at'])}?",
        reply_markup=kb([[btn("◀️ Cancel", "a:menu")]]),
    )
    await cb.answer()


@router.message(Adm.reply_text, F.text)
async def got_reply(m: Message, state: FSMContext):
    if m.text.startswith("/"):
        await state.clear()
        return
    data = await state.get_data()
    a = db.get_appointment(data.get("rep_aid") or 0)
    await state.clear()
    if not a:
        return
    text = m.text.strip()[:1000]
    db.add_message(a["id"], "master", text)
    sent = await notify.client_master_message(m.bot, a, text)
    await m.answer("✅ Sent to the client." if sent
                   else "⚠️ The client has no Telegram — not delivered, call them.",
                   reply_markup=kb([[btn("◀️ Panel", "a:menu")]]))


# ---------- services ----------

def svc_list_kb():
    rows = []
    for s in db.list_services(only_active=False):
        mark = "🟢" if s["active"] else "⚪"
        g = "👥 " if s["is_group"] else ""
        rows.append([btn(f"{mark} {g}{s['title']}", f"a:svc:{s['id']}")])
    rows.append([btn("➕ Add service", "a:svcadd")])
    rows.append([btn("◀️ Back", "a:menu")])
    return kb(rows)


@router.callback_query(F.data == "a:svcs")
async def cb_services(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await ui.safe_edit(cb.message, "🧾 <b>Services</b>", svc_list_kb())
    await cb.answer()


def svc_detail(sid):
    s = db.get_service(sid)
    if not s:
        return "Service not found", kb([[btn("◀️ Back", "a:svcs")]])
    n_photos = db.service_photos_count(sid)
    if s["ext_price_per_nail"] or s["ext_min_per_nail"]:
        ext_line = (f"➕ Extension: +{s['ext_price_per_nail']} € and "
                    f"+{s['ext_min_per_nail']} min per nail")
    else:
        ext_line = "➕ Extension: not offered"
    text = (
        f"<b>{esc(s['title'])}</b>\n"
        f"⏱ Duration: {fmt_dur(s['duration_min'])}\n"
        f"💰 Price: {esc(s['price']) or '—'}\n"
        f"{ext_line}\n"
        f"🖼 Examples: {n_photos if n_photos else 'none'}\n"
        f"🔁 Comeback invite: "
        f"{'after ' + str(s['comeback_days']) + ' day(s)' if s['comeback_days'] else 'off'}\n"
        f"Status: {'🟢 active' if s['active'] else '⚪ hidden'}"
    )
    if s["is_group"]:
        text += f"\n👥 Group, capacity: {s['capacity']}"
    rows = [
        [btn("✏️ Name", f"a:svce:title:{sid}"),
         btn("⏱ Duration", f"a:svce:dur:{sid}")],
        [btn("💰 Price", f"a:svce:price:{sid}"),
         btn("🔁 Days until reminder", f"a:svce:cb:{sid}")],
        [btn("➕ Extension: €/nail", f"a:svce:extp:{sid}"),
         btn("➕ min/nail", f"a:svce:extm:{sid}")],
        [btn(f"🖼 Examples ({n_photos})" if n_photos else "🖼 Add examples",
             f"a:svcph:{sid}")],
    ]
    if s["is_group"]:
        rows.append([btn("👥 Group capacity", f"a:svce:cap:{sid}")])
    rows.append([btn("⚪ Hide" if s["active"] else "🟢 Enable", f"a:svct:{sid}")])
    rows.append([btn("◀️ Back", "a:svcs")])
    return text, kb(rows)


@router.callback_query(F.data.startswith("a:svc:"))
async def cb_service_detail(cb: CallbackQuery):
    text, markup = svc_detail(int(cb.data.split(":")[2]))
    await ui.safe_edit(cb.message, text, markup)
    await cb.answer()


@router.callback_query(F.data.startswith("a:svct:"))
async def cb_service_toggle(cb: CallbackQuery):
    sid = int(cb.data.split(":")[2])
    db.toggle_service(sid)
    text, markup = svc_detail(sid)
    await ui.safe_edit(cb.message, text, markup)
    await cb.answer()


SVC_FIELDS = {
    "title": ("New service name:", "title", str),
    "dur": ("Duration in minutes (e.g. 90):", "duration_min", int),
    "price": ("Price (text, e.g. \"$40\", or \"-\" to clear it):", "price", str),
    "cb": ("Remind to come back after how many days? (0 — never):", "comeback_days", int),
    "cap": ("How many spots in the group?", "capacity", int),
    "extp": ("Extension: surcharge per nail in € (0 — don't offer it):",
             "ext_price_per_nail", int),
    "extm": ("Extension: how many minutes does 1 nail add? (0 — none):",
             "ext_min_per_nail", int),
}


@router.callback_query(F.data.startswith("a:svce:"))
async def cb_service_edit(cb: CallbackQuery, state: FSMContext):
    _, _, field, sid = cb.data.split(":")
    prompt = SVC_FIELDS[field][0]
    await state.set_state(Adm.svc_edit)
    await state.update_data(field=field, sid=int(sid))
    await cb.message.answer(prompt)
    await cb.answer()


@router.message(Adm.svc_edit, F.text)
async def got_service_edit(m: Message, state: FSMContext):
    data = await state.get_data()
    _, column, typ = SVC_FIELDS[data["field"]]
    raw = m.text.strip()
    if typ is int:
        if not raw.isdigit():
            await m.answer("Needs to be a number. Try again:")
            return
        value = int(raw)
        if column in ("duration_min", "capacity") and value <= 0:
            await m.answer("The number must be greater than zero:")
            return
    else:
        value = "" if raw == "-" else raw
    db.update_service_field(data["sid"], column, value)
    await state.clear()
    text, markup = svc_detail(data["sid"])
    await m.answer(text, reply_markup=markup)


async def send_gallery(bot, chat_id, sid):
    """Example album + management: 🗑 button numbers match the photo order."""
    s = db.get_service(sid)
    photos = db.service_photos(sid)
    if photos:
        try:
            await bot.send_media_group(
                chat_id, media=[InputMediaPhoto(media=p["file_id"]) for p in photos])
        except Exception as e:
            log.warning("Examples for service %s failed to show: %s", sid, e)
    rows = []
    if photos:
        del_btns = [btn(f"🗑 {i}", f"a:svcphdel:{p['id']}")
                    for i, p in enumerate(photos, 1)]
        rows += [del_btns[i:i + 5] for i in range(0, len(del_btns), 5)]
    if len(photos) < db.SVC_PHOTO_MAX:
        rows.append([btn("➕ Add photo", f"a:svcphadd:{sid}")])
    rows.append([btn("✏️ Rename style", f"a:svce:title:{sid}")])
    rows.append([btn("◀️ To manicure style", f"a:svc:{sid}")])

    text = f"🖼 <b>Examples: {esc(s['title'])}</b>\n\n"
    if photos:
        text += (f"Currently {len(photos)}. Clients see them via the "
                 f"«Show examples» button. The first photo is the cover.\n"
                 f"The 🗑 button numbers match the photo order above.")
    else:
        text += "No examples yet. The first photo will become this style's cover."
    await bot.send_message(chat_id, text, reply_markup=kb(rows))


@router.callback_query(F.data.startswith("a:svcph:"))
async def cb_service_photos(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    sid = int(cb.data.split(":")[2])
    if not db.get_service(sid):
        await cb.answer("Style not found", show_alert=True)
        return
    await send_gallery(cb.bot, cb.from_user.id, sid)
    await cb.answer()


@router.callback_query(F.data.startswith("a:svcphadd:"))
async def cb_service_photo_add(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[2])
    if not db.get_service(sid):
        await cb.answer("Style not found", show_alert=True)
        return
    if db.service_photos_count(sid) >= db.SVC_PHOTO_MAX:
        await cb.answer(f"Already at the max ({db.SVC_PHOTO_MAX})", show_alert=True)
        return
    await state.set_state(Adm.svc_photo)
    await state.update_data(ph_sid=sid)
    await cb.message.answer(
        "Send an example photo — you can send several at once.",
        reply_markup=kb([[btn("🖼 Done, show all", f"a:svcph:{sid}")]]),
    )
    await cb.answer()


@router.message(Adm.svc_photo, F.photo)
async def got_service_photo(m: Message, state: FSMContext):
    data = await state.get_data()
    sid = data.get("ph_sid")
    if not sid or not db.get_service(sid):
        await state.clear()
        return
    added = db.add_service_photo(sid, m.photo[-1].file_id)
    if not ui.first_of_album(m):
        return
    if not added:
        await m.answer(f"More than {db.SVC_PHOTO_MAX} examples won't fit — "
                       f"delete some first.",
                       reply_markup=kb([[btn("🖼 Show all", f"a:svcph:{sid}")]]))
        return
    await m.answer(
        f"📎 Added. Total examples: {db.service_photos_count(sid)}.",
        reply_markup=kb([
            [btn("🖼 Done, show all", f"a:svcph:{sid}")],
            [btn("◀️ To manicure style", f"a:svc:{sid}")],
        ]),
    )


@router.message(Adm.svc_photo, F.text)
async def got_service_photo_wrong(m: Message, state: FSMContext):
    if m.text.startswith("/"):
        await state.clear()
        return
    await m.answer("I need an actual photo — please send an image 🙂")


@router.callback_query(F.data.startswith("a:svcphdel:"))
async def cb_service_photo_del(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[2])
    row = db.get_service_photo(pid)
    if not row:
        await cb.answer("Already deleted")
        return
    await state.clear()
    db.delete_service_photo(pid)
    await cb.answer("Deleted")
    await send_gallery(cb.bot, cb.from_user.id, row["service_id"])


@router.callback_query(F.data == "a:svcadd")
async def cb_service_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.svc_title)
    await state.update_data(new_svc={})
    await cb.message.answer("Service name?")
    await cb.answer()


@router.message(Adm.svc_title, F.text)
async def got_svc_title(m: Message, state: FSMContext):
    data = await state.get_data()
    data["new_svc"]["title"] = m.text.strip()
    await state.update_data(new_svc=data["new_svc"])
    await state.set_state(Adm.svc_dur)
    await m.answer("Duration in minutes? (e.g. 90)")


@router.message(Adm.svc_dur, F.text)
async def got_svc_dur(m: Message, state: FSMContext):
    if not m.text.strip().isdigit() or int(m.text.strip()) <= 0:
        await m.answer("Needs to be a number of minutes, e.g. 90:")
        return
    data = await state.get_data()
    data["new_svc"]["duration"] = int(m.text.strip())
    await state.update_data(new_svc=data["new_svc"])
    await state.set_state(Adm.svc_price)
    await m.answer("Price? (text, e.g. \"$40\", or \"-\" to skip)")


@router.message(Adm.svc_price, F.text)
async def got_svc_price(m: Message, state: FSMContext):
    data = await state.get_data()
    raw = m.text.strip()
    data["new_svc"]["price"] = "" if raw == "-" else raw
    data["new_svc"]["is_group"] = False
    data["new_svc"]["capacity"] = 1
    await state.update_data(new_svc=data["new_svc"])
    await state.set_state(Adm.svc_comeback)
    await m.answer(
        "After how many days should the client get a \"time to refresh\" nudge?\n"
        "(number of days, 0 — never; default 21 — send \"-\")"
    )


@router.message(Adm.svc_comeback, F.text)
async def got_svc_comeback(m: Message, state: FSMContext):
    raw = m.text.strip()
    if raw == "-":
        days = 21
    elif raw.isdigit():
        days = int(raw)
    else:
        await m.answer("Needs to be a number of days, 0, or \"-\":")
        return
    data = await state.get_data()
    ns = data["new_svc"]
    db.add_service(ns["title"], ns["duration"], ns["price"],
                   ns.get("is_group", False), ns.get("capacity", 1), days)
    await state.clear()
    await m.answer(f"✅ Service «{esc(ns['title'])}» added.", reply_markup=svc_list_kb())


# ---------- masters ----------

def masters_list_kb():
    rows = []
    for mst in db.list_masters(only_active=False):
        mark = "🟢" if mst["active"] else "⚪"
        rows.append([btn(f"{mark} {mst['name']}", f"a:mst:{mst['id']}")])
    rows.append([btn("➕ Add master", "a:mstadd")])
    rows.append([btn("◀️ Back", "a:menu")])
    return kb(rows)


@router.callback_query(F.data == "a:msts")
async def cb_masters(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await ui.safe_edit(cb.message, "👤 <b>Masters</b>", masters_list_kb())
    await cb.answer()


def master_detail(mid):
    mst = db.get_master(mid)
    if not mst:
        return "Master not found", kb([[btn("◀️ Back", "a:msts")]])
    text = (
        f"<b>{esc(mst['name'])}</b>\n"
        f"Telegram ID: {mst['tg_id'] or '— (notifications go to admins)'}\n"
        f"Status: {'🟢 active' if mst['active'] else '⚪ hidden'}"
    )
    rows = [
        [btn("✏️ Name", f"a:mste:name:{mid}"),
         btn("🔗 Telegram ID", f"a:mste:tg:{mid}")],
        [btn("⚪ Hide" if mst["active"] else "🟢 Enable", f"a:mstt:{mid}")],
        [btn("◀️ Back", "a:msts")],
    ]
    return text, kb(rows)


@router.callback_query(F.data.startswith("a:mst:"))
async def cb_master_detail(cb: CallbackQuery):
    text, markup = master_detail(int(cb.data.split(":")[2]))
    await ui.safe_edit(cb.message, text, markup)
    await cb.answer()


@router.callback_query(F.data.startswith("a:mstt:"))
async def cb_master_toggle(cb: CallbackQuery):
    mid = int(cb.data.split(":")[2])
    db.toggle_master(mid)
    text, markup = master_detail(mid)
    await ui.safe_edit(cb.message, text, markup)
    await cb.answer()


@router.callback_query(F.data.startswith("a:mste:"))
async def cb_master_edit(cb: CallbackQuery, state: FSMContext):
    _, _, field, mid = cb.data.split(":")
    await state.set_state(Adm.m_edit)
    await state.update_data(field=field, mid=int(mid))
    if field == "name":
        await cb.message.answer("New master name:")
    else:
        await cb.message.answer(
            "Master's Telegram ID (a number) — booking notifications go there.\n"
            "The master can find their ID via @userinfobot. Send \"-\" to clear it."
        )
    await cb.answer()


@router.message(Adm.m_edit, F.text)
async def got_master_edit(m: Message, state: FSMContext):
    data = await state.get_data()
    raw = m.text.strip()
    if data["field"] == "name":
        db.update_master_field(data["mid"], "name", raw)
    else:
        if raw == "-":
            db.update_master_field(data["mid"], "tg_id", None)
        elif raw.isdigit():
            db.update_master_field(data["mid"], "tg_id", int(raw))
        else:
            await m.answer("Needs to be a number or \"-\":")
            return
    await state.clear()
    text, markup = master_detail(data["mid"])
    await m.answer(text, reply_markup=markup)


@router.callback_query(F.data == "a:mstadd")
async def cb_master_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.m_name)
    await cb.message.answer("Master's name?")
    await cb.answer()


@router.message(Adm.m_name, F.text)
async def got_master_name(m: Message, state: FSMContext):
    await state.update_data(m_name=m.text.strip())
    await state.set_state(Adm.m_tg)
    await m.answer(
        "Master's Telegram ID (a number) for notifications, or \"-\" to skip.\n"
        "Find the ID via @userinfobot"
    )


@router.message(Adm.m_tg, F.text)
async def got_master_tg(m: Message, state: FSMContext):
    raw = m.text.strip()
    tg_id = None
    if raw != "-":
        if not raw.isdigit():
            await m.answer("Needs to be a number or \"-\":")
            return
        tg_id = int(raw)
    data = await state.get_data()
    db.add_master(data["m_name"], tg_id)
    await state.clear()
    await m.answer(f"✅ Master «{esc(data['m_name'])}» added.\n"
                   f"Don't forget to set up the schedule in «📅 Schedule».",
                   reply_markup=masters_list_kb())


# ---------- schedule ----------

@router.callback_query(F.data == "a:sch")
async def cb_schedule(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    owner = db.owner_master()
    if not owner:
        await ui.safe_edit(cb.message, "Master not found — please restart the bot.",
                           kb([[btn("◀️ Back", "a:menu")]]))
        await cb.answer()
        return
    await show_week(cb.message, owner["id"])
    await cb.answer()


def week_text(mid):
    mst = db.get_master(mid)
    lines = [f"📅 <b>Schedule — {esc(mst['name'])}</b>\n"]
    for wd in range(7):
        t = db.get_template(mid, wd)
        if not t or not t["work_start"]:
            lines.append(f"{WD[wd]}: day off")
        else:
            s = f"{WD[wd]}: {t['work_start']}–{t['work_end']}"
            if t["break_start"]:
                s += f", lunch {t['break_start']}–{t['break_end']}"
            lines.append(s)
    lines.append("\nTap a day to change it.")
    return "\n".join(lines)


def week_kb(mid):
    day_btns = [btn(WD[wd], f"a:tw:{mid}:{wd}") for wd in range(7)]
    rows = [day_btns[:4], day_btns[4:]]
    rows.append([btn("🗓 Exceptions (dates)", f"a:ovrl:{mid}")])
    rows.append([btn("◀️ Back", "a:menu")])
    return kb(rows)


async def show_week(message, mid):
    await ui.safe_edit(message, week_text(mid), week_kb(mid))


@router.callback_query(F.data.startswith("a:schm:"))
async def cb_schedule_master(cb: CallbackQuery):
    await show_week(cb.message, int(cb.data.split(":")[2]))
    await cb.answer()


@router.callback_query(F.data.startswith("a:tw:"))
async def cb_template_weekday(cb: CallbackQuery, state: FSMContext):
    _, _, mid, wd = cb.data.split(":")
    await state.set_state(Adm.sched)
    await state.update_data(sched_master=int(mid), sched_wd=int(wd))
    await cb.message.answer(
        f"Schedule for <b>{WD_FULL[int(wd)]}</b>.\nSend it in one of these formats:\n\n"
        f"<code>10:00-19:00 lunch 14:00-15:00</code>\n"
        f"<code>10:00-19:00</code> (no lunch break)\n"
        f"<code>day off</code>"
    )
    await cb.answer()


@router.message(Adm.sched, F.text)
async def got_schedule(m: Message, state: FSMContext):
    parsed = parse_schedule_text(m.text)
    if parsed is None:
        await m.answer("Didn't understand that format 🤔 Example: <code>10:00-19:00 lunch 14:00-15:00</code>")
        return
    data = await state.get_data()
    db.set_template(data["sched_master"], data["sched_wd"],
                    parsed["ws"], parsed["we"], parsed["bs"], parsed["be"])
    await state.clear()
    mid = data["sched_master"]
    await m.answer("✅ Saved.\n\n" + week_text(mid), reply_markup=week_kb(mid))


def overrides_view(mid):
    today = datetime.now().strftime("%Y-%m-%d")
    ovrs = db.list_overrides(mid, today)
    lines = ["🗓 <b>Date exceptions</b>",
             "A different schedule or a day off on a specific date.\n"]
    rows = []
    for o in ovrs:
        if o["day_off"]:
            desc = "day off"
        else:
            desc = f"{o['work_start']}–{o['work_end']}"
            if o["break_start"]:
                desc += f", lunch {o['break_start']}–{o['break_end']}"
        lines.append(f"• {fmt_d(o['date'])}: {desc}")
        rows.append([btn(f"🗑 Remove {fmt_d(o['date'])}", f"a:ovrdel:{mid}:{o['date']}")])
    if not ovrs:
        lines.append("No exceptions yet.")
    rows.append([btn("➕ Add a date", f"a:ovradd:{mid}")])
    rows.append([btn("◀️ Back", f"a:schm:{mid}")])
    return "\n".join(lines), kb(rows)


@router.callback_query(F.data.startswith("a:ovrl:"))
async def cb_overrides(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = overrides_view(int(cb.data.split(":")[2]))
    await ui.safe_edit(cb.message, text, markup)
    await cb.answer()


@router.callback_query(F.data.startswith("a:ovrdel:"))
async def cb_override_del(cb: CallbackQuery):
    _, _, mid, ds = cb.data.split(":")
    db.delete_override(int(mid), ds)
    text, markup = overrides_view(int(mid))
    await ui.safe_edit(cb.message, text, markup)
    await cb.answer("Deleted")


@router.callback_query(F.data.startswith("a:ovradd:"))
async def cb_override_add(cb: CallbackQuery, state: FSMContext):
    mid = int(cb.data.split(":")[2])
    await state.set_state(Adm.ovr_date)
    await state.update_data(ovr_master=mid)
    await cb.message.answer("Date? (e.g. <code>25.07</code> or <code>25.07.2026</code>)")
    await cb.answer()


@router.message(Adm.ovr_date, F.text)
async def got_override_date(m: Message, state: FSMContext):
    ds = parse_date(m.text)
    if not ds:
        await m.answer("Didn't understand the date. Format: <code>25.07</code>")
        return
    await state.update_data(ovr_date=ds)
    await state.set_state(Adm.ovr_val)
    await m.answer(
        f"Schedule for {fmt_d(ds)}:\n"
        f"<code>10:00-19:00 lunch 14:00-15:00</code>, <code>10:00-19:00</code> "
        f"or <code>day off</code>"
    )


@router.message(Adm.ovr_val, F.text)
async def got_override_val(m: Message, state: FSMContext):
    parsed = parse_schedule_text(m.text)
    if parsed is None:
        await m.answer("Didn't understand that format 🤔 Example: <code>10:00-19:00</code> or <code>day off</code>")
        return
    data = await state.get_data()
    db.set_override(data["ovr_master"], data["ovr_date"], parsed["day_off"],
                    parsed["ws"], parsed["we"], parsed["bs"], parsed["be"])
    await state.clear()
    await m.answer(f"✅ Exception for {fmt_d(data['ovr_date'])} saved.",
                   reply_markup=kb([[btn("◀️ To schedule", f"a:schm:{data['ovr_master']}")]]))


# ---------- groups ----------

@router.callback_query(F.data == "a:grp")
async def cb_groups(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    sessions = db.upcoming_group_sessions()
    rows = []
    for g in sessions:
        rows.append([btn(
            f"{fmt_dt(g['starts_at'])} {g['service_title']} ({g['booked']}/{g['capacity']})",
            f"a:gs:{g['id']}",
        )])
    rows.append([btn("➕ Create session", "a:gsadd")])
    rows.append([btn("◀️ Back", "a:menu")])
    text = "👥 <b>Group sessions</b>" + ("" if sessions else "\nNothing scheduled yet.")
    await ui.safe_edit(cb.message, text, kb(rows))
    await cb.answer()


@router.callback_query(F.data.startswith("a:gs:"))
async def cb_group_detail(cb: CallbackQuery):
    gid = int(cb.data.split(":")[2])
    g = db.get_group_session(gid)
    if not g:
        await cb.answer("Session not found", show_alert=True)
        return
    lines = [
        f"👥 <b>{esc(g['service_title'])}</b>",
        f"📆 {fmt_dt(g['starts_at'])} · {esc(g['master_name'])}",
        f"Spots: {g['booked']}/{g['capacity']}\n",
    ]
    parts = db.appointments_of_session(gid)
    if parts:
        lines.append("<b>Participants:</b>")
        for a in parts:
            lines.append(f"• {esc(a['client_name'] or '—')} {esc(a['client_phone'] or '')}")
    else:
        lines.append("No one booked yet.")
    await ui.safe_edit(cb.message, "\n".join(lines), kb([
        [btn("❌ Cancel session", f"a:gsccl:{gid}")],
        [btn("◀️ Back", "a:grp")],
    ]))
    await cb.answer()


@router.callback_query(F.data.startswith("a:gsccl:"))
async def cb_group_cancel_ask(cb: CallbackQuery):
    gid = int(cb.data.split(":")[2])
    await ui.safe_edit(cb.message, "Cancel this session? All participants will be notified.",
                       kb([[btn("❌ Yes, cancel", f"a:gsccl2:{gid}")],
                           [btn("◀️ Back", f"a:gs:{gid}")]]))
    await cb.answer()


@router.callback_query(F.data.startswith("a:gsccl2:"))
async def cb_group_cancel_do(cb: CallbackQuery):
    gid = int(cb.data.split(":")[2])
    g = db.get_group_session(gid)
    if not g or g["status"] != "active":
        await cb.answer("No longer active", show_alert=True)
        return
    db.set_group_session_status(gid, "cancelled")
    for a in db.appointments_of_session(gid):
        db.set_appointment_status(a["id"], "cancelled")
        db.drop_pending_notifications(a["id"])
        await notify.client_cancelled_by_admin(cb.bot, a)
    await ui.safe_edit(cb.message, "Session cancelled, participants notified.",
                       kb([[btn("◀️ To groups", "a:grp")]]))
    await cb.answer()


@router.callback_query(F.data == "a:gsadd")
async def cb_group_add(cb: CallbackQuery, state: FSMContext):
    group_services = [s for s in db.list_services() if s["is_group"]]
    if not group_services:
        await ui.safe_edit(cb.message,
                           "First create a group service in «🧾 Services».",
                           kb([[btn("◀️ Back", "a:grp")]]))
        await cb.answer()
        return
    rows = [[btn(s["title"], f"a:gssvc:{s['id']}")] for s in group_services]
    rows.append([btn("◀️ Back", "a:grp")])
    await ui.safe_edit(cb.message, "Which service?", kb(rows))
    await cb.answer()


@router.callback_query(F.data.startswith("a:gssvc:"))
async def cb_group_svc(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[2])
    await state.update_data(gs_service=sid)
    masters = db.list_masters()
    if len(masters) == 1:
        await state.update_data(gs_master=masters[0]["id"])
        await state.set_state(Adm.gs_dt)
        await cb.message.answer("Date and time of the session? (e.g. <code>25.07 18:00</code>)")
    else:
        rows = [[btn(mst["name"], f"a:gsmst:{mst['id']}")] for mst in masters]
        rows.append([btn("◀️ Back", "a:gsadd")])
        await ui.safe_edit(cb.message, "Who's running it?", kb(rows))
    await cb.answer()


@router.callback_query(F.data.startswith("a:gsmst:"))
async def cb_group_master(cb: CallbackQuery, state: FSMContext):
    await state.update_data(gs_master=int(cb.data.split(":")[2]))
    await state.set_state(Adm.gs_dt)
    await cb.message.answer("Date and time of the session? (e.g. <code>25.07 18:00</code>)")
    await cb.answer()


@router.message(Adm.gs_dt, F.text)
async def got_group_dt(m: Message, state: FSMContext):
    dt = parse_datetime(m.text)
    if not dt:
        await m.answer("Didn't understand that. Format: <code>25.07 18:00</code>")
        return
    if dt <= db.now_str():
        await m.answer("That's already in the past 🙂 Enter a future date:")
        return
    await state.update_data(gs_dt=dt)
    await state.set_state(Adm.gs_cap)
    data = await state.get_data()
    svc = db.get_service(data["gs_service"])
    await m.answer(f"How many spots? (send \"-\" for the service default: {svc['capacity']})")


@router.message(Adm.gs_cap, F.text)
async def got_group_cap(m: Message, state: FSMContext):
    data = await state.get_data()
    svc = db.get_service(data["gs_service"])
    raw = m.text.strip()
    if raw == "-":
        cap = svc["capacity"]
    elif raw.isdigit() and int(raw) > 0:
        cap = int(raw)
    else:
        await m.answer("Needs to be a number of spots or \"-\":")
        return
    db.create_group_session(data["gs_service"], data["gs_master"], data["gs_dt"], cap)
    await state.clear()
    await m.answer(
        f"✅ Session created: {esc(svc['title'])}, {fmt_dt(data['gs_dt'])}, spots: {cap}.\n"
        f"Clients will see it when choosing this service.",
        reply_markup=kb([[btn("◀️ To groups", "a:grp")]]),
    )


# ---------- settings ----------

SETTINGS_META = {
    "buffer_min": "Break between sessions (min)",
    "slot_step": "Time grid step (min)",
    "min_lead_min": "Minimum minutes before booking",
    "horizon_days": "Booking open this many days ahead",
    "ref_deadline_hour": "Accept references until (hour on booking day)",
    "ref_max": "Max photos/videos per reference",
    "digest_hour": "Daily digest sent at (hour)",
    "ref_remind_hour": "Reference reminder sent the evening before at (hour)",
}

SETTINGS_RANGE = {
    "buffer_min": (0, 240),
    "slot_step": (5, 240),
    "min_lead_min": (0, 10080),
    "horizon_days": (1, 90),
    "ref_deadline_hour": (0, 23),
    "ref_max": (1, 10),
    "digest_hour": (0, 23),
    "ref_remind_hour": (0, 23),
}


def settings_view():
    owner = db.owner_master()
    lines = ["⚙️ <b>Settings</b>\n"]
    rows = []
    if owner:
        lines.append(f"Your name (shown to clients): <b>{esc(owner['name'])}</b>")
        rows.append([btn("✏️ Your name (for clients)", "a:rename")])
    for key, label in SETTINGS_META.items():
        lines.append(f"{label}: <b>{db.get_setting(key)}</b>")
        rows.append([btn(f"✏️ {label}", f"a:sete:{key}")])
    rows.append([btn("◀️ Back", "a:menu")])
    return "\n".join(lines), kb(rows)


@router.callback_query(F.data == "a:rename")
async def cb_owner_rename(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.owner_name)
    await cb.message.answer("How should clients see your name? (shown on booking confirmations)")
    await cb.answer()


@router.message(Adm.owner_name, F.text)
async def got_owner_name(m: Message, state: FSMContext):
    name = m.text.strip()
    if not name or name.startswith("/") or len(name) > 60:
        await m.answer("Please type the name as plain text:")
        return
    owner = db.owner_master()
    if owner:
        db.update_master_field(owner["id"], "name", name)
    await state.clear()
    text, markup = settings_view()
    await m.answer("✅ Done.\n\n" + text, reply_markup=markup)


@router.callback_query(F.data == "a:set")
async def cb_settings(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = settings_view()
    await ui.safe_edit(cb.message, text, markup)
    await cb.answer()


@router.callback_query(F.data.startswith("a:sete:"))
async def cb_setting_edit(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[2]
    await state.set_state(Adm.set_val)
    await state.update_data(set_key=key)
    await cb.message.answer(f"{SETTINGS_META[key]} — new value (a number):")
    await cb.answer()


@router.message(Adm.set_val, F.text)
async def got_setting(m: Message, state: FSMContext):
    if not m.text.strip().isdigit():
        await m.answer("Needs to be a number:")
        return
    data = await state.get_data()
    key = data["set_key"]
    value = int(m.text.strip())
    lo, hi = SETTINGS_RANGE.get(key, (0, 100000))
    if not lo <= value <= hi:
        await m.answer(f"The value must be between {lo} and {hi}:")
        return
    db.set_setting(key, value)
    await state.clear()
    text, markup = settings_view()
    await m.answer(text, reply_markup=markup)


# ---------- manual booking (lives in the same system) ----------

@router.callback_query(F.data == "a:nb")
async def cb_newbook(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    services = db.list_services()
    if not services:
        await ui.safe_edit(cb.message, "Add services first.",
                           kb([[btn("◀️ Back", "a:menu")]]))
        await cb.answer()
        return
    rows = []
    for s in services:
        g = "👥 " if s["is_group"] else ""
        rows.append([btn(f"{g}{s['title']} · {fmt_dur(s['duration_min'])}", f"a:nbsvc:{s['id']}")])
    rows.append([btn("◀️ Back", "a:menu")])
    await ui.safe_edit(cb.message, "➕ <b>New booking</b>\nService:", kb(rows))
    await cb.answer()


async def nb_choose_master(message, state: FSMContext):
    masters = db.list_masters()
    if len(masters) == 1:
        await state.update_data(nb_master=masters[0]["id"])
        await show_nb_dates(message, state)
        return
    rows = [[btn(mst["name"], f"a:nbmst:{mst['id']}")] for mst in masters]
    rows.append([btn("◀️ Back", "a:nb")])
    await ui.safe_edit(message, "Master:", kb(rows))


@router.callback_query(F.data.startswith("a:nbsvc:"))
async def cb_nb_service(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[2])
    svc = db.get_service(sid)
    await state.update_data(nb_service=sid, nb_gs=None, nb_ext=0)

    if svc["is_group"]:
        sessions = db.upcoming_group_sessions(sid)
        rows = [[btn(f"{fmt_dt(g['starts_at'])} ({g['booked']}/{g['capacity']})",
                     f"a:nbgs:{g['id']}")] for g in sessions]
        if not rows:
            await ui.safe_edit(cb.message,
                               "No sessions scheduled yet. Create one in «👥 Groups».",
                               kb([[btn("◀️ Back", "a:nb")]]))
            await cb.answer()
            return
        rows.append([btn("◀️ Back", "a:nb")])
        await ui.safe_edit(cb.message, "Which session?", kb(rows))
        await cb.answer()
        return

    if svc["ext_price_per_nail"] or svc["ext_min_per_nail"]:
        nums = [btn(str(n), f"a:nbext:{n}") for n in range(1, 11)]
        rows = [[btn("No extension", "a:nbext:0")], nums[:5], nums[5:],
                [btn("◀️ Back", "a:nb")]]
        await ui.safe_edit(
            cb.message,
            f"Extension? Per nail: +{svc['ext_price_per_nail']} €, "
            f"+{svc['ext_min_per_nail']} min.",
            kb(rows),
        )
        await cb.answer()
        return

    await nb_choose_master(cb.message, state)
    await cb.answer()


@router.callback_query(F.data.startswith("a:nbext:"))
async def cb_nb_ext(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("nb_service"):
        await cb.answer("Please start over: /admin", show_alert=True)
        return
    nails = max(0, min(10, int(cb.data.split(":")[2])))
    await state.update_data(nb_ext=nails)
    await nb_choose_master(cb.message, state)
    await cb.answer()


@router.callback_query(F.data.startswith("a:nbmst:"))
async def cb_nb_master(cb: CallbackQuery, state: FSMContext):
    await state.update_data(nb_master=int(cb.data.split(":")[2]))
    await show_nb_dates(cb.message, state)
    await cb.answer()


async def show_nb_dates(message, state: FSMContext):
    data = await state.get_data()
    svc = db.get_service(data["nb_service"])
    duration = db.effective_duration(svc, data.get("nb_ext") or 0)
    horizon = db.get_int("horizon_days", 14)
    today = datetime.now().date()
    buttons = []
    for i in range(horizon):
        d = today + timedelta(days=i)
        ds = d.isoformat()
        if not sl.get_work_window(data["nb_master"], ds):
            continue
        free = sl.free_slots(data["nb_master"], ds, duration)
        buttons.append(btn(ui.date_label(d) + ("" if free else " ✖"), f"a:nbday:{ds}"))
    if not buttons:
        await ui.safe_edit(message, "No working days in the horizon. Set up the schedule.",
                           kb([[btn("◀️ Back", "a:nb")]]))
        return
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([btn("◀️ Back", "a:nb")])
    await ui.safe_edit(message, "Day:", kb(rows))


@router.callback_query(F.data.startswith("a:nbday:"))
async def cb_nb_day(cb: CallbackQuery, state: FSMContext):
    ds = cb.data.split(":", 2)[2]
    data = await state.get_data()
    if not data.get("nb_service") or not data.get("nb_master"):
        await cb.answer("Please start over: /admin", show_alert=True)
        return
    svc = db.get_service(data["nb_service"])
    duration = db.effective_duration(svc, data.get("nb_ext") or 0)
    free = sl.free_slots(data["nb_master"], ds, duration)
    await state.update_data(nb_date=ds)
    await state.set_state(Adm.nb_time)
    rows = []
    if free:
        buttons = [btn(t, f"a:nbtm:{t}") for t in free]
        rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    rows.append([btn("◀️ Back", "a:nbback")])
    text = f"Time on {fmt_d(ds)} — pick a slot or send your own (e.g. <code>16:45</code>):"
    if not free:
        text = (f"No free slots on {fmt_d(ds)}, but you can send a time "
                f"manually (e.g. <code>16:45</code>):")
    await ui.safe_edit(cb.message, text, kb(rows))
    await cb.answer()


@router.callback_query(F.data == "a:nbback")
async def cb_nb_back_dates(cb: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await show_nb_dates(cb.message, state)
    await cb.answer()


@router.callback_query(F.data.startswith("a:nbtm:"))
async def cb_nb_time(cb: CallbackQuery, state: FSMContext):
    t = cb.data.split(":", 2)[2]
    await state.update_data(nb_time=t)
    await ask_nb_client(cb.message, state)
    await cb.answer()


@router.message(Adm.nb_time, F.text)
async def got_nb_time(m: Message, state: FSMContext):
    t = parse_time(m.text)
    if not t:
        await m.answer("Time format: <code>16:45</code>")
        return
    await state.update_data(nb_time=t)
    await ask_nb_client(m, state)


@router.callback_query(F.data.startswith("a:nbgs:"))
async def cb_nb_group(cb: CallbackQuery, state: FSMContext):
    gid = int(cb.data.split(":")[2])
    g = db.get_group_session(gid)
    if not g or g["status"] != "active":
        await cb.answer("Session unavailable", show_alert=True)
        return
    if g["booked"] >= g["capacity"]:
        await cb.answer("No spots left", show_alert=True)
        return
    await state.update_data(nb_gs=gid, nb_service=g["service_id"], nb_master=g["master_id"])
    await ask_nb_client(cb.message, state)
    await cb.answer()


async def ask_nb_client(message, state: FSMContext):
    await state.set_state(Adm.nb_client)
    await message.answer(
        "Who are we booking?\nSend a name or phone number to search, "
        "or the word <code>new</code> for a new client."
    )


@router.message(Adm.nb_client, F.text)
async def got_nb_client_query(m: Message, state: FSMContext):
    q = m.text.strip()
    if q.lower() in ("new", "новый"):
        await state.set_state(Adm.nb_name)
        await m.answer("Client's name?")
        return
    found = db.search_clients(q)
    rows = []
    for c in found:
        label = f"{c['name'] or 'No name'} {c['phone'] or ''}"
        if c["username"]:
            label += f" @{c['username']}"
        rows.append([btn(label[:60], f"a:nbcl:{c['id']}")])
    rows.append([btn("➕ New client", "a:nbnew")])
    text = "Which one?" if found else "Couldn't find anyone 🤔"
    await m.answer(text, reply_markup=kb(rows))


@router.callback_query(F.data == "a:nbnew")
async def cb_nb_new_client(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.nb_name)
    await cb.message.answer("Client's name?")
    await cb.answer()


@router.message(Adm.nb_name, F.text)
async def got_nb_name(m: Message, state: FSMContext):
    await state.update_data(nb_new_name=m.text.strip())
    await state.set_state(Adm.nb_phone)
    await m.answer("Client's phone? (or \"-\" if unknown)")


@router.message(Adm.nb_phone, F.text)
async def got_nb_phone(m: Message, state: FSMContext):
    raw = m.text.strip()
    phone = "" if raw == "-" else db.norm_phone(raw)
    if raw != "-" and len(phone) < 9:
        await m.answer("That doesn't look like a phone number. Try again or \"-\":")
        return
    data = await state.get_data()
    cid = db.create_client(data["nb_new_name"], phone)
    await state.update_data(nb_client_id=cid)
    await show_nb_confirm(m, state)


@router.callback_query(F.data.startswith("a:nbcl:"))
async def cb_nb_client_pick(cb: CallbackQuery, state: FSMContext):
    await state.update_data(nb_client_id=int(cb.data.split(":")[2]))
    await show_nb_confirm(cb.message, state)
    await cb.answer()


async def show_nb_confirm(message, state: FSMContext):
    await state.set_state(None)
    data = await state.get_data()
    client = db.get_client(data.get("nb_client_id") or 0)
    svc = db.get_service(data.get("nb_service") or 0)
    if not client or not svc:
        await message.answer("Something got lost, start over: /admin")
        return

    warn = ""
    nails = data.get("nb_ext") or 0
    if data.get("nb_gs"):
        g = db.get_group_session(data["nb_gs"])
        when = fmt_dt(g["starts_at"])
        master_name = g["master_name"]
        nails = 0
        duration, extra = svc["duration_min"], 0
    else:
        if not data.get("nb_date") or not data.get("nb_time"):
            await message.answer("Something got lost, start over: /admin")
            return
        when = f"{fmt_d(data['nb_date'])} at {data['nb_time']}"
        master_name = db.get_master(data["nb_master"])["name"]
        duration = db.effective_duration(svc, nails)
        extra = db.ext_extra_price(svc, nails)
        if sl.conflict_exists(data["nb_master"], data["nb_date"], data["nb_time"],
                              duration):
            warn = "\n\n⚠️ Warning: this time overlaps another booking or its buffer!"

    ext_line = f"➕ Extension: {nails} nail(s) (+{extra} €)\n" if nails else ""
    price_line = ui.price_with_ext(svc["price"], extra)
    text = (
        f"<b>Create this booking?</b>\n\n"
        f"💅 {esc(svc['title'])} ({fmt_dur(duration)})\n{ext_line}"
        f"👤 Master: {esc(master_name)}\n📆 {when}\n"
        + (f"💰 {esc(price_line)}\n" if price_line else "")
        + f"Client: {esc(client['name'] or '—')} {esc(client['phone'] or '')}"
        f"{warn}"
    )
    await message.answer(text, reply_markup=kb([
        [btn("✅ Create", "a:nbok")],
        [btn("❌ Cancel", "a:menu")],
    ]))


@router.callback_query(F.data == "a:nbok")
async def cb_nb_create(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    client = db.get_client(data.get("nb_client_id") or 0)
    svc = db.get_service(data.get("nb_service") or 0)
    if not client or not svc:
        await cb.answer("Data is stale, please start over", show_alert=True)
        return

    nails = data.get("nb_ext") or 0
    if data.get("nb_gs"):
        g = db.get_group_session(data["nb_gs"])
        if not g or g["status"] != "active" or g["booked"] >= g["capacity"]:
            await cb.answer("Session unavailable or no spots left", show_alert=True)
            return
        starts = g["starts_at"]
        master_id = g["master_id"]
        gs_id = g["id"]
        nails = 0
    else:
        starts = f"{data['nb_date']} {data['nb_time']}"
        master_id = data["nb_master"]
        gs_id = None

    duration = db.effective_duration(svc, nails)
    extra = db.ext_extra_price(svc, nails)
    ends = (datetime.strptime(starts, "%Y-%m-%d %H:%M")
            + timedelta(minutes=duration)).strftime("%Y-%m-%d %H:%M")
    aid = db.create_appointment(client["id"], svc["id"], master_id, starts, ends,
                                created_by="admin", group_session_id=gs_id,
                                ext_nails=nails, ext_price=extra)
    scheduler.schedule_for_appointment(aid, starts)
    appt = db.get_appointment(aid)
    # the client gets notified even if the owner created the booking
    await notify.client_admin_created(cb.bot, appt)
    await notify.master_new_appointment(cb.bot, appt, actor_tg=cb.from_user.id)
    await state.clear()
    note = "" if appt["client_tg"] else "\n(the client has no Telegram — no notification sent)"
    await ui.safe_edit(cb.message,
                       f"✅ Booking created: {fmt_dt(starts)}, {esc(svc['title'])}.{note}",
                       kb([[btn("➕ Another booking", "a:nb")], [btn("◀️ Panel", "a:menu")]]))
    await cb.answer()
