"""Client-facing flows: booking, repeat booking, cancellation, confirmation,
reviews, waitlist."""
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InputMediaPhoto, KeyboardButton,
                           Message, ReplyKeyboardMarkup, ReplyKeyboardRemove)

import config
import db
import notify
import refs
import scheduler
import ui
from i18n import t
from ui import btn, esc, fmt_d, fmt_dt, fmt_dur, kb

router = Router(name="client")


class Booking(StatesGroup):
    name = State()
    phone = State()


class Fb(StatesGroup):
    comment = State()


class Profile(StatesGroup):
    name = State()
    phone = State()


class RefFlow(StatesGroup):
    upload = State()     # data: ref_aid


class Chat(StatesGroup):
    to_master = State()  # data: chat_aid


# ---------- /start: returning clients see "Book again" right away ----------

@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    client = db.upsert_client(m.from_user.id, m.from_user.username)
    admin = config.is_admin(m.from_user.id)

    last = db.last_for_repeat(client["id"])
    if last:
        svc = db.get_service(last["service_id"])
        mst = db.get_master(last["master_id"])
        if svc and svc["active"] and mst and mst["active"]:
            hello = t("hello_named", name=esc(client["name"])) if client["name"] else t("hello")
            text = (
                f"{hello}\n\n"
                + t("repeat_block", service=esc(svc["title"]), master=esc(mst["name"]),
                    duration=fmt_dur(svc["duration_min"]))
            )
            rows = [
                [btn(t("btn_choose_time"), f"rpt:{svc['id']}:{mst['id']}")],
                [btn(t("btn_other_service"), "bk")],
                [btn(t("btn_my_appts"), "my"), btn(t("btn_my_profile"), "prof")],
            ]
            if admin:
                rows.append([btn(t("btn_master_panel"), "a:menu")])
            await m.answer(text, reply_markup=kb(rows))
            return

    await m.answer(t("start_greeting"), reply_markup=ui.main_menu_kb(admin))


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    db.upsert_client(cb.from_user.id, cb.from_user.username)
    await ui.safe_edit(cb.message, t("choose_action"),
                       ui.main_menu_kb(config.is_admin(cb.from_user.id)))
    await cb.answer()


# ---------- profile ----------

def profile_view(client):
    lines = [
        t("profile_title"),
        t("profile_name_line", name=esc(client["name"]) or "—"),
        t("profile_phone_line", phone=esc(client["phone"]) or "—"),
    ]
    if client["username"]:
        lines.append(t("profile_tg_line", username=esc(client["username"])))
    lines.append(t("profile_note"))
    rows = [
        [btn(t("btn_edit_name"), "profname")],
        [btn(t("btn_edit_phone"), "profphone")],
        [btn(t("btn_menu"), "menu")],
    ]
    return "\n".join(lines), kb(rows)


@router.callback_query(F.data == "prof")
async def cb_profile(cb: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    client = db.upsert_client(cb.from_user.id, cb.from_user.username)
    text, markup = profile_view(client)
    await ui.safe_edit(cb.message, text, markup)
    await cb.answer()


@router.callback_query(F.data == "profname")
async def cb_profile_name(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Profile.name)
    await cb.message.answer(t("ask_name"),
                            reply_markup=kb([[btn(t("btn_cancel"), "prof")]]))
    await cb.answer()


@router.message(Profile.name, F.text)
async def got_profile_name(m: Message, state: FSMContext):
    name = m.text.strip()
    if name.startswith("/"):
        await state.clear()
        return
    if not name or len(name) > 100:
        await m.answer(t("name_invalid"))
        return
    client = db.upsert_client(m.from_user.id, m.from_user.username)
    db.set_client_name(client["id"], name)
    await state.clear()
    text, markup = profile_view(db.get_client_by_tg(m.from_user.id))
    await m.answer(t("name_updated") + text, reply_markup=markup)


@router.callback_query(F.data == "profphone")
async def cb_profile_phone(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Profile.phone)
    contact_kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t("send_phone_btn"), request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await cb.message.answer(t("send_phone_prompt"), reply_markup=contact_kb)
    await cb.answer()


@router.message(Profile.phone, F.contact | F.text)
async def got_profile_phone(m: Message, state: FSMContext):
    if not m.contact and m.text.startswith("/"):
        await state.clear()
        await m.answer(t("cancelled_generic"), reply_markup=ReplyKeyboardRemove())
        return
    raw = m.contact.phone_number if m.contact else m.text
    phone = db.norm_phone(raw)
    if len(phone) < 9:
        await m.answer(t("phone_invalid"))
        return
    client = db.upsert_client(m.from_user.id, m.from_user.username)
    db.set_client_phone(client["id"], phone)
    db.merge_offline_client(client["id"], phone)
    await state.clear()
    await m.answer(t("phone_updated"), reply_markup=ReplyKeyboardRemove())
    text, markup = profile_view(db.get_client_by_tg(m.from_user.id))
    await m.answer(text, reply_markup=markup)


# ---------- service / master / day / time selection ----------

@router.callback_query(F.data == "bk")
async def cb_book(cb: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await state.update_data(service_id=None, master_id=None, date=None, time=None,
                            gs_id=None, ext_nails=0, wl_date=None)
    services = db.list_services()
    if not services:
        await ui.safe_edit(cb.message, t("services_empty"),
                           kb([[btn(t("btn_menu"), "menu")]]))
        await cb.answer()
        return
    with_photos = db.services_with_photos()
    rows = []
    for s in services:
        label = f"{s['title']} · {fmt_dur(s['duration_min'])}"
        if s["price"]:
            label += f" · {s['price']}"
        if s["is_group"]:
            label = "\U0001F465 " + label
        if s["id"] in with_photos:
            label = "\U0001F5BC " + label
        rows.append([btn(label, f"svc:{s['id']}")])
    rows.append([btn(t("btn_menu"), "menu")])
    text = t("choose_service")
    if with_photos:
        text += t("choose_service_hint")
    await ui.safe_edit(cb.message, text, kb(rows))
    await cb.answer()


def ext_enabled(svc):
    return not svc["is_group"] and (
        (svc["ext_price_per_nail"] or 0) > 0 or (svc["ext_min_per_nail"] or 0) > 0)


async def ask_extension(message, svc):
    p = svc["ext_price_per_nail"] or 0
    mn = svc["ext_min_per_nail"] or 0
    bits = []
    if p:
        bits.append(t("ext_price_bit", p=p))
    if mn:
        bits.append(t("ext_min_bit", m=mn))
    nums = [btn(str(n), f"ext:{n}") for n in range(1, 11)]
    rows = [[btn(t("ext_none_btn"), "ext:0")], nums[:5], nums[5:],
            [btn(t("btn_other_service"), "bk")]]
    await message.answer(
        t("ext_question", title=esc(svc["title"]), bits=", ".join(bits)),
        reply_markup=kb(rows),
    )


async def choose_master_or_dates(message, state: FSMContext, svc):
    if svc["is_group"]:
        await show_group_sessions(message, svc["id"])
        return
    masters = db.list_masters()
    if not masters:
        await ui.safe_edit(message, t("masters_empty"),
                           kb([[btn(t("btn_menu"), "menu")]]))
        return
    if len(masters) == 1:
        await state.update_data(master_id=masters[0]["id"])
        await show_dates(message, state)
        return
    rows = [[btn(f"\U0001F464 {m['name']}", f"mst:{m['id']}")] for m in masters]
    rows.append([btn(t("btn_back"), "bk")])
    await ui.safe_edit(message, t("choose_master", service=esc(svc["title"])), kb(rows))


async def continue_to_time(message, state: FSMContext, svc):
    """After the extension question: waitlist -> time, repeat -> dates, else master."""
    data = await state.get_data()
    if data.get("wl_date"):
        await show_times(message, state, data["wl_date"])
    elif data.get("master_id"):
        await show_dates(message, state)
    else:
        await choose_master_or_dates(message, state, svc)


async def start_booking_steps(message, state: FSMContext, svc):
    if ext_enabled(svc):
        await ask_extension(message, svc)
    else:
        await state.update_data(ext_nails=0)
        await continue_to_time(message, state, svc)


@router.callback_query(F.data.startswith("ext:"))
async def cb_extension(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    svc = db.get_service(data.get("service_id") or 0)
    if not svc or not svc["active"]:
        await cb.answer(t("session_expired"), show_alert=True)
        return
    nails = max(0, min(10, int(cb.data.split(":")[1])))
    await state.update_data(ext_nails=nails)
    await cb.answer()
    await continue_to_time(cb.message, state, svc)


@router.callback_query(F.data.startswith("svc:"))
async def cb_service(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[1])
    svc = db.get_service(sid)
    if not svc or not svc["active"]:
        await cb.answer(t("service_unavailable"), show_alert=True)
        return
    await state.update_data(service_id=sid, gs_id=None, ext_nails=0, wl_date=None)

    # a cover photo exists -- show it first, with a link to the rest of the gallery
    cover = db.service_cover(sid)
    if cover:
        n = db.service_photos_count(sid)
        caption = t("svc_caption", title=esc(svc["title"]), duration=fmt_dur(svc["duration_min"]))
        if svc["price"]:
            caption += t("svc_price_line", price=esc(svc["price"]))
        caption += t("svc_examples_hint")
        rows = []
        if n > 1:
            rows.append([btn(t("btn_show_examples", n=n), f"svcex:{sid}")])
        rows.append([btn(t("btn_choose_time"), f"svcgo:{sid}")])
        rows.append([btn(t("btn_other_service"), "bk")])
        try:
            await cb.message.answer_photo(cover, caption=caption, reply_markup=kb(rows))
            await cb.answer()
            return
        except Exception:
            pass  # the photo failed to load -- don't block booking, keep going

    await start_booking_steps(cb.message, state, svc)
    await cb.answer()


@router.callback_query(F.data.startswith("svcex:"))
async def cb_service_examples(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[1])
    svc = db.get_service(sid)
    if not svc or not svc["active"]:
        await cb.answer(t("service_disabled"), show_alert=True)
        return
    photos = db.service_photos(sid)
    if not photos:
        await cb.answer(t("no_examples"), show_alert=True)
        return
    await state.update_data(service_id=sid, gs_id=None)
    try:
        await cb.bot.send_media_group(
            cb.from_user.id, media=[InputMediaPhoto(media=p["file_id"]) for p in photos])
    except Exception:
        await cb.answer(t("examples_failed"), show_alert=True)
        return
    caption = t("svcex_caption", title=esc(svc["title"]), n=len(photos),
                duration=fmt_dur(svc["duration_min"]))
    if svc["price"]:
        caption += t("svcex_price_suffix", price=esc(svc["price"]))
    await cb.message.answer(caption, reply_markup=kb([
        [btn(t("btn_choose_time"), f"svcgo:{sid}")],
        [btn(t("btn_other_service"), "bk")],
    ]))
    await cb.answer()


@router.callback_query(F.data.startswith("svcgo:"))
async def cb_service_go(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[1])
    svc = db.get_service(sid)
    if not svc or not svc["active"]:
        await cb.answer(t("service_unavailable"), show_alert=True)
        return
    await state.update_data(service_id=sid, gs_id=None)
    await start_booking_steps(cb.message, state, svc)
    await cb.answer()


@router.callback_query(F.data.startswith("mst:"))
async def cb_master(cb: CallbackQuery, state: FSMContext):
    await state.update_data(master_id=int(cb.data.split(":")[1]))
    await show_dates(cb.message, state)
    await cb.answer()


@router.callback_query(F.data == "dts")
async def cb_back_to_dates(cb: CallbackQuery, state: FSMContext):
    await show_dates(cb.message, state)
    await cb.answer()


async def show_dates(message, state: FSMContext):
    import slots as sl
    data = await state.get_data()
    if not data.get("service_id") or not data.get("master_id"):
        await ui.safe_edit(message, t("session_expired"),
                           kb([[btn(t("btn_go_booking"), "bk")]]))
        return
    svc = db.get_service(data["service_id"])
    mst = db.get_master(data["master_id"])
    duration = db.effective_duration(svc, data.get("ext_nails") or 0)
    horizon = db.get_int("horizon_days", 14)
    today = datetime.now().date()

    buttons = []
    for i in range(horizon):
        d = today + timedelta(days=i)
        ds = d.isoformat()
        if not sl.get_work_window(mst["id"], ds):
            continue
        free = sl.free_slots(mst["id"], ds, duration)
        label = ui.date_label(d) + ("" if free else " ✖")
        buttons.append(btn(label, f"day:{ds}"))

    if not buttons:
        await ui.safe_edit(
            message,
            t("no_workdays"),
            kb([[btn(t("btn_back"), "bk")]]),
        )
        return
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([btn(t("btn_back"), "bk")])
    await ui.safe_edit(
        message,
        t("choose_day", service=esc(svc["title"]), master=esc(mst["name"])),
        kb(rows),
    )


@router.callback_query(F.data.startswith("day:"))
async def cb_day(cb: CallbackQuery, state: FSMContext):
    ds = cb.data.split(":", 1)[1]
    await show_times(cb.message, state, ds)
    await cb.answer()


async def show_times(message, state: FSMContext, ds):
    import slots as sl
    data = await state.get_data()
    if not data.get("service_id") or not data.get("master_id"):
        await ui.safe_edit(message, t("session_expired"),
                           kb([[btn(t("btn_go_booking"), "bk")]]))
        return
    svc = db.get_service(data["service_id"])
    duration = db.effective_duration(svc, data.get("ext_nails") or 0)
    free = sl.free_slots(data["master_id"], ds, duration)
    await state.update_data(date=ds)

    if not free:
        await ui.safe_edit(
            message,
            t("all_booked", date=fmt_d(ds)),
            kb([
                [btn(t("btn_notify_free"), f"wl:{ds}")],
                [btn(t("btn_other_day"), "dts")],
            ]),
        )
        return
    buttons = [btn(time, f"tm:{time}") for time in free]
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    rows.append([btn(t("btn_other_day"), "dts")])
    await ui.safe_edit(message, t("free_times", date=fmt_d(ds)), kb(rows))


@router.callback_query(F.data.startswith("tm:"))
async def cb_time(cb: CallbackQuery, state: FSMContext):
    time = cb.data.split(":", 1)[1]
    await state.update_data(time=time)
    await ask_contacts_or_confirm(cb, state)


# ---------- group sessions: capacity is enforced automatically ----------

async def show_group_sessions(message, service_id):
    sessions = db.upcoming_group_sessions(service_id)
    rows = []
    for g in sessions:
        left = g["capacity"] - g["booked"]
        if left <= 0:
            continue
        rows.append([btn(
            f"{fmt_dt(g['starts_at'])} · {g['master_name']} · {left} left",
            f"gs:{g['id']}",
        )])
    if not rows:
        await ui.safe_edit(message, t("no_groups"),
                           kb([[btn(t("btn_back"), "bk")]]))
        return
    rows.append([btn(t("btn_back"), "bk")])
    await ui.safe_edit(message, t("choose_group"), kb(rows))


@router.callback_query(F.data.startswith("gs:"))
async def cb_group_session(cb: CallbackQuery, state: FSMContext):
    gid = int(cb.data.split(":")[1])
    g = db.get_group_session(gid)
    if not g or g["status"] != "active":
        await cb.answer(t("group_unavailable"), show_alert=True)
        return
    if g["booked"] >= g["capacity"]:
        await cb.answer(t("group_full"), show_alert=True)
        return
    await state.update_data(gs_id=gid, service_id=g["service_id"], master_id=g["master_id"])
    await ask_contacts_or_confirm(cb, state)


# ---------- name and phone (asked once, reused afterwards) ----------

async def ask_contacts_or_confirm(cb: CallbackQuery, state: FSMContext):
    client = db.upsert_client(cb.from_user.id, cb.from_user.username)
    if client["name"] and client["phone"]:
        await show_confirm(cb.message, state, client)
        await cb.answer()
        return
    await state.set_state(Booking.name)
    await cb.message.answer(t("ask_name"))
    await cb.answer()


@router.message(Booking.name, F.text)
async def got_name(m: Message, state: FSMContext):
    name = m.text.strip()
    if not name or name.startswith("/") or len(name) > 100:
        await m.answer(t("name_invalid"))
        return
    client = db.get_client_by_tg(m.from_user.id)
    db.set_client_name(client["id"], name)
    await state.set_state(Booking.phone)
    contact_kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t("send_phone_btn"), request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await m.answer(t("send_phone_prompt"), reply_markup=contact_kb)


@router.message(Booking.phone, F.contact | F.text)
async def got_phone(m: Message, state: FSMContext):
    raw = m.contact.phone_number if m.contact else m.text
    phone = db.norm_phone(raw)
    if len(phone) < 9:
        await m.answer(t("phone_invalid"))
        return
    client = db.get_client_by_tg(m.from_user.id)
    db.set_client_phone(client["id"], phone)
    db.merge_offline_client(client["id"], phone)
    await state.set_state(None)
    await m.answer(t("thanks_emoji"), reply_markup=ReplyKeyboardRemove())
    client = db.get_client_by_tg(m.from_user.id)
    await show_confirm(m, state, client, edit=False)


# ---------- confirmation and creation ----------

def ext_note(svc, nails):
    """'+15 €, +45 min' -- summary of the extension surcharge."""
    bits = []
    extra = db.ext_extra_price(svc, nails)
    add_min = nails * (svc["ext_min_per_nail"] or 0)
    if extra:
        bits.append(f"+{extra} €")
    if add_min:
        bits.append(t("ext_min_note", m=add_min))
    return ", ".join(bits) if bits else t("ext_included")


async def show_confirm(message, state: FSMContext, client, edit=True):
    data = await state.get_data()
    svc = db.get_service(data.get("service_id") or 0)
    if not svc:
        await message.answer(t("session_expired"),
                             reply_markup=kb([[btn(t("btn_go_booking"), "bk")]]))
        return

    nails = data.get("ext_nails") or 0
    if data.get("gs_id"):
        g = db.get_group_session(data["gs_id"])
        when = fmt_dt(g["starts_at"])
        master_name = g["master_name"]
        confirm_cb = "mkgs"
        back_row = [btn(t("btn_back"), f"svc:{svc['id']}")]
        duration, extra = svc["duration_min"], 0
        nails = 0
    else:
        if not data.get("date") or not data.get("time"):
            await message.answer(t("session_expired"),
                                 reply_markup=kb([[btn(t("btn_go_booking"), "bk")]]))
            return
        when = t("date_at_time", date=fmt_d(data["date"]), time=data["time"])
        master_name = db.get_master(data["master_id"])["name"]
        confirm_cb = "mkap"
        back_row = [btn(t("btn_back"), f"day:{data['date']}")]
        duration = db.effective_duration(svc, nails)
        extra = db.ext_extra_price(svc, nails)

    text = t("confirm_check") + t("svc_line", title=esc(svc["title"]), duration=fmt_dur(duration))
    if nails:
        text += t("ext_line", n=nails, note=ext_note(svc, nails))
    text += t("master_line", master=esc(master_name), when=when)
    price_line = ui.price_with_ext(svc["price"], extra)
    if price_line:
        text += t("price_line", price=esc(price_line))
    text += t("name_phone_line", name=esc(client["name"]), phone=esc(client["phone"]))

    markup = kb([
        [btn(t("btn_confirm"), confirm_cb)],
        back_row,
        [btn(t("btn_cancel_x"), "menu")],
    ])
    if edit:
        await ui.safe_edit(message, text, markup)
    else:
        await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "mkap")
async def cb_make_appointment(cb: CallbackQuery, state: FSMContext):
    import slots as sl
    data = await state.get_data()
    client = db.get_client_by_tg(cb.from_user.id)
    if not client or not all(data.get(k) for k in ("service_id", "master_id", "date", "time")):
        await cb.answer(t("session_expired"), show_alert=True)
        return
    svc = db.get_service(data["service_id"])
    nails = data.get("ext_nails") or 0
    duration = db.effective_duration(svc, nails)
    extra = db.ext_extra_price(svc, nails)

    if data["time"] not in sl.free_slots(data["master_id"], data["date"], duration):
        await cb.answer(t("slot_taken"), show_alert=True)
        await show_times(cb.message, state, data["date"])
        return

    starts = f"{data['date']} {data['time']}"
    ends = (datetime.strptime(starts, "%Y-%m-%d %H:%M")
            + timedelta(minutes=duration)).strftime("%Y-%m-%d %H:%M")
    aid = db.create_appointment(client["id"], svc["id"], data["master_id"], starts, ends,
                                ext_nails=nails, ext_price=extra)
    scheduler.schedule_for_appointment(aid, starts)
    await notify.master_new_appointment(cb.bot, db.get_appointment(aid),
                                        actor_tg=cb.from_user.id)
    await state.clear()
    ext_line = t("ext_line", n=nails, note=ext_note(svc, nails)) if nails else ""
    await ui.safe_edit(
        cb.message,
        t("booked_success", title=esc(svc["title"]), duration=fmt_dur(duration),
          ext_line=ext_line, when=fmt_dt(starts)),
        kb([
            [btn(t("btn_send_ref"), f"ref:{aid}")],
            [btn(t("btn_my_appts"), "my")],
            [btn(t("btn_menu"), "menu")],
        ]),
    )
    await cb.answer(t("answer_booked"))


@router.callback_query(F.data == "mkgs")
async def cb_make_group(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    client = db.get_client_by_tg(cb.from_user.id)
    g = db.get_group_session(data.get("gs_id") or 0)
    if not client or not g or g["status"] != "active":
        await cb.answer(t("group_unavailable"), show_alert=True)
        return
    if db.client_in_session(client["id"], g["id"]):
        await cb.answer(t("already_in_session"), show_alert=True)
        return
    if g["booked"] >= g["capacity"]:
        await cb.answer(t("group_full"), show_alert=True)
        return

    starts = g["starts_at"]
    ends = (datetime.strptime(starts, "%Y-%m-%d %H:%M")
            + timedelta(minutes=g["duration_min"])).strftime("%Y-%m-%d %H:%M")
    aid = db.create_appointment(client["id"], g["service_id"], g["master_id"],
                                starts, ends, group_session_id=g["id"])
    scheduler.schedule_for_appointment(aid, starts)
    await notify.master_new_appointment(cb.bot, db.get_appointment(aid),
                                        actor_tg=cb.from_user.id)
    await state.clear()
    left = g["capacity"] - g["booked"] - 1
    await ui.safe_edit(
        cb.message,
        t("group_booked_success", title=esc(g["service_title"]), when=fmt_dt(starts), left=left),
        kb([[btn(t("btn_my_appts"), "my")], [btn(t("btn_menu"), "menu")]]),
    )
    await cb.answer(t("answer_booked"))


# ---------- repeat: service and master are pre-filled ----------

@router.callback_query(F.data.startswith("rpt:"))
async def cb_repeat(cb: CallbackQuery, state: FSMContext):
    _, sid, mid = cb.data.split(":")
    svc = db.get_service(int(sid))
    mst = db.get_master(int(mid))
    if not svc or not svc["active"] or not mst or not mst["active"]:
        await cb.answer(t("repeat_unavailable"), show_alert=True)
        return
    await state.set_state(None)
    await state.update_data(service_id=svc["id"], master_id=mst["id"], gs_id=None,
                            ext_nails=0, wl_date=None)
    if svc["is_group"]:
        await show_group_sessions(cb.message, svc["id"])
    else:
        await start_booking_steps(cb.message, state, svc)
    await cb.answer()


# ---------- my bookings and cancellation ----------

@router.callback_query(F.data == "my")
async def cb_my(cb: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    client = db.upsert_client(cb.from_user.id, cb.from_user.username)
    appts = db.upcoming_for_client(client["id"])
    if not appts:
        await ui.safe_edit(cb.message, t("no_active_appts"),
                           kb([[btn(t("btn_go_booking"), "bk")], [btn(t("btn_menu"), "menu")]]))
        await cb.answer()
        return
    rows = []
    for a in appts:
        mark = "✅" if a["status"] == "confirmed" else "\U0001F550"
        pin = " \U0001F4CE" if refs.has_ref(a) else ""
        rows.append([btn(f"{mark} {fmt_dt(a['starts_at'])} — {a['service_title']}{pin}",
                         f"myap:{a['id']}")])
    rows.append([btn(t("btn_book_more"), "bk")])
    rows.append([btn(t("btn_menu"), "menu")])
    await ui.safe_edit(cb.message, t("my_appts_title"), kb(rows))
    await cb.answer()


def _own(a, cb):
    return a is not None and a["client_tg"] == cb.from_user.id


def _appt_minutes(a):
    """Actual booking duration (with extensions) derived from its times."""
    s = datetime.strptime(a["starts_at"], "%Y-%m-%d %H:%M")
    e = datetime.strptime(a["ends_at"], "%Y-%m-%d %H:%M")
    return max(0, int((e - s).total_seconds() // 60))


def client_card(a):
    mark = "✅" if a["status"] == "confirmed" else "\U0001F550"
    lines = [
        t("your_appt_title", mark=mark),
        t("svc_line", title=esc(a["service_title"]), duration=fmt_dur(_appt_minutes(a))).rstrip("\n"),
    ]
    if a["ext_nails"]:
        note = f" (+{a['ext_price']} €)" if a["ext_price"] else ""
        lines.append(t("ext_nails_line", n=a["ext_nails"], note=note))
    lines += [
        t("master_only_line", master=esc(a["master_name"])),
        f"\U0001F4C6 {fmt_dt(a['starts_at'])}",
    ]
    price_line = ui.price_with_ext(a["price"], a["ext_price"])
    if price_line:
        lines.append(t("price_line", price=esc(price_line)).rstrip("\n"))

    summary = refs.summary(a)
    rows = []
    if summary:
        lines.append(t("ref_summary_line", summary=esc(summary)))
        if a["ref_status"] == "ok":
            lines.append(t("ref_ok_note"))
        elif a["ref_status"] == "no_materials":
            lines.append(t("ref_no_materials_note"))
    if refs.is_open(a):
        rows.append([btn(t("btn_change_ref") if summary else t("btn_send_ref"),
                         f"ref:{a['id']}")])
    elif not summary:
        lines.append(t("ref_closed_note"))
    rows.append([btn(t("btn_write_master"), f"msg:{a['id']}")])
    rows.append([btn(t("btn_cancel_appt"), f"myccl:{a['id']}")])
    rows.append([btn(t("btn_back"), "my")])
    return "\n".join(lines), kb(rows)


@router.callback_query(F.data.startswith("myap:"))
async def cb_my_appt(cb: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    a = db.get_appointment(int(cb.data.split(":")[1]))
    if not _own(a, cb):
        await cb.answer(t("appt_not_found"), show_alert=True)
        return
    text, markup = client_card(a)
    await ui.safe_edit(cb.message, text, markup)
    await cb.answer()


# ---------- reference: design photo/video ----------

def ref_prompt(a):
    deadline, late = refs.window(a)
    limit = db.get_int("ref_max", 5)
    text = t("ref_prompt_title", when=fmt_dt(a["starts_at"]), limit=limit)
    if late:
        text += t("ref_late_note")
    else:
        text += t("ref_deadline_note", deadline=fmt_dt(deadline))
    n = db.refs_count(a["id"])
    if n:
        text += t("ref_sent_count", n=n)
    if a["ref_comment"]:
        text += t("ref_card_comment", comment=esc(a["ref_comment"]))
    return text


def ref_kb(a):
    rows = []
    if refs.has_ref(a):
        rows.append([btn(t("btn_delete_ref"), f"refdel:{a['id']}")])
    rows.append([btn(t("btn_back"), f"myap:{a['id']}")])
    return kb(rows)


@router.callback_query(F.data.startswith("ref:"))
async def cb_ref(cb: CallbackQuery, state: FSMContext):
    aid = int(cb.data.split(":")[1])
    a = db.get_appointment(aid)
    if not _own(a, cb):
        await cb.answer(t("appt_not_found"), show_alert=True)
        return
    if not refs.is_open(a):
        await ui.safe_edit(
            cb.message,
            t("ref_window_closed_alert"),
            kb([[btn(t("btn_write_master"), f"msg:{aid}")], [btn(t("btn_back"), f"myap:{aid}")]]),
        )
        await cb.answer()
        return
    await state.set_state(RefFlow.upload)
    await state.update_data(ref_aid=aid)
    await cb.message.answer(ref_prompt(a), reply_markup=ref_kb(a))
    await cb.answer()


@router.message(RefFlow.upload, F.photo | F.video | F.video_note | F.animation)
async def got_ref_media(m: Message, state: FSMContext):
    data = await state.get_data()
    a = db.get_appointment(data.get("ref_aid") or 0)
    if not a or a["client_tg"] != m.from_user.id:
        await state.clear()
        return
    if not refs.is_open(a):
        await state.clear()
        await m.answer(t("ref_window_closed_short"))
        return
    got = refs.extract_media(m)
    if not got:
        return
    limit = db.get_int("ref_max", 5)
    added = db.add_ref(a["id"], got[0], got[1], limit)
    if m.caption:
        db.set_ref_comment(a["id"], m.caption.strip()[:500])
    if added:
        db.schedule_ref_notify(a["id"])
    if not ui.first_of_album(m):
        return
    a = db.get_appointment(a["id"])
    if not added:
        await m.answer(t("ref_limit_reached", limit=limit), reply_markup=ref_kb(a))
        return
    await m.answer(t("ref_received", n=db.refs_count(a["id"])), reply_markup=ref_kb(a))


@router.message(RefFlow.upload, F.text)
async def got_ref_comment(m: Message, state: FSMContext):
    if m.text.startswith("/"):
        await state.clear()
        return
    data = await state.get_data()
    a = db.get_appointment(data.get("ref_aid") or 0)
    if not a or a["client_tg"] != m.from_user.id:
        await state.clear()
        return
    if not refs.is_open(a):
        await state.clear()
        await m.answer(t("ref_window_closed_short"))
        return
    db.set_ref_comment(a["id"], m.text.strip()[:500])
    db.schedule_ref_notify(a["id"])
    await m.answer(t("ref_comment_saved"), reply_markup=ref_kb(db.get_appointment(a["id"])))


@router.callback_query(F.data.startswith("refdone:"))
async def cb_ref_done(cb: CallbackQuery, state: FSMContext):
    """The "Done" button no longer exists -- the reference is forwarded automatically.
    The handler stays so the button on old messages doesn't spin forever."""
    aid = int(cb.data.split(":")[1])
    a = db.get_appointment(aid)
    if not _own(a, cb):
        await cb.answer(t("appt_not_found"), show_alert=True)
        return
    if not refs.has_ref(a):
        await cb.answer(t("nothing_sent_yet"), show_alert=True)
        return
    await state.set_state(None)
    db.mark_ref_notified(aid)  # prevents the delayed notification from duplicating it
    await notify.master_reference(cb.bot, a)
    text, markup = client_card(db.get_appointment(aid))
    await ui.safe_edit(cb.message, t("ref_sent_to_master") + text, markup)
    await cb.answer(t("ref_sent_short"))


@router.callback_query(F.data.startswith("refdel:"))
async def cb_ref_del(cb: CallbackQuery):
    aid = int(cb.data.split(":")[1])
    a = db.get_appointment(aid)
    if not _own(a, cb):
        await cb.answer(t("appt_not_found"), show_alert=True)
        return
    if not refs.is_open(a):
        await cb.answer(t("ref_window_closed_short"), show_alert=True)
        return
    db.clear_refs(aid)
    a = db.get_appointment(aid)
    await ui.safe_edit(cb.message, ref_prompt(a), ref_kb(a))
    await cb.answer(t("ref_deleted"))


@router.callback_query(F.data == "refskip")
async def cb_ref_skip(cb: CallbackQuery):
    await ui.safe_edit(cb.message, t("ref_skip_note"),
                       kb([[btn(t("btn_my_appts"), "my")]]))
    await cb.answer()


# ---------- chat with the master ----------

@router.callback_query(F.data.startswith("msg:"))
@router.callback_query(F.data.startswith("crep:"))
async def cb_write_master(cb: CallbackQuery, state: FSMContext):
    aid = int(cb.data.split(":")[1])
    a = db.get_appointment(aid)
    if not _own(a, cb):
        await cb.answer(t("appt_not_found"), show_alert=True)
        return
    await state.set_state(Chat.to_master)
    await state.update_data(chat_aid=aid)
    await cb.message.answer(
        t("write_master_prompt", when=fmt_dt(a["starts_at"])),
        reply_markup=kb([[btn(t("btn_cancel"), f"myap:{aid}")]]),
    )
    await cb.answer()


@router.message(Chat.to_master, F.text)
async def got_msg_to_master(m: Message, state: FSMContext):
    if m.text.startswith("/"):
        await state.clear()
        return
    data = await state.get_data()
    a = db.get_appointment(data.get("chat_aid") or 0)
    await state.clear()
    if not a or a["client_tg"] != m.from_user.id:
        return
    text = m.text.strip()[:1000]
    db.add_message(a["id"], "client", text)
    await notify.master_client_message(m.bot, a, text)
    await m.answer(t("msg_sent_to_master"),
                   reply_markup=kb([[btn(t("btn_back_to_appt"), f"myap:{a['id']}")]]))


@router.callback_query(F.data.startswith("myccl:"))
@router.callback_query(F.data.startswith("ccl:"))
async def cb_cancel_ask(cb: CallbackQuery):
    aid = int(cb.data.split(":")[1])
    a = db.get_appointment(aid)
    if not a or a["status"] in ("cancelled", "completed"):
        await cb.answer(t("appt_irrelevant"), show_alert=True)
        return
    if a["client_tg"] != cb.from_user.id and not config.is_admin(cb.from_user.id):
        await cb.answer(t("not_your_appt"), show_alert=True)
        return
    await ui.safe_edit(
        cb.message,
        t("cancel_confirm_q", title=esc(a["service_title"]), when=fmt_dt(a["starts_at"])),
        kb([
            [btn(t("btn_yes_cancel"), f"doccl:{aid}")],
            [btn(t("btn_no_keep"), "my")],
        ]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("doccl:"))
async def cb_cancel_do(cb: CallbackQuery):
    aid = int(cb.data.split(":")[1])
    a = db.get_appointment(aid)
    if not a or a["status"] in ("cancelled", "completed"):
        await cb.answer(t("already_irrelevant"), show_alert=True)
        return
    if a["client_tg"] != cb.from_user.id and not config.is_admin(cb.from_user.id):
        await cb.answer(t("not_your_appt"), show_alert=True)
        return
    # atomic: the master notification and the waitlist fire exactly once per cancellation
    if not db.transition_status(aid, ("created", "confirmed"), "cancelled"):
        await cb.answer(t("already_irrelevant"), show_alert=True)
        return
    db.drop_pending_notifications(aid)
    await notify.master_cancelled(cb.bot, a, actor_tg=cb.from_user.id)
    await notify.run_waitlist(cb.bot, a["master_id"], a["starts_at"][:10],
                              exclude_client=a["client_id"])
    await ui.safe_edit(
        cb.message,
        t("appt_cancelled"),
        kb([[btn(t("btn_go_booking"), "bk")], [btn(t("btn_menu"), "menu")]]),
    )
    await cb.answer()


# ---------- 24-hour confirmation ----------

@router.callback_query(F.data.startswith("cfm:"))
async def cb_confirm_visit(cb: CallbackQuery):
    aid = int(cb.data.split(":")[1])
    a = db.get_appointment(aid)
    if not a or a["client_tg"] != cb.from_user.id:
        await cb.answer(t("appt_not_found"), show_alert=True)
        return
    if a["status"] == "created":
        if not db.transition_status(aid, ("created",), "confirmed"):
            await cb.answer(t("already_confirmed_alert"))
            return
        await ui.safe_edit(cb.message,
                           t("confirmed_text", title=esc(a["service_title"]),
                             when=fmt_dt(a["starts_at"])))
        await cb.answer(t("confirmed_short"))
    elif a["status"] == "confirmed":
        await cb.answer(t("already_confirmed_alert"))
    else:
        await cb.answer(t("appt_irrelevant"), show_alert=True)


# ---------- waitlist ----------

@router.callback_query(F.data.startswith("wl:"))
async def cb_waitlist(cb: CallbackQuery, state: FSMContext):
    ds = cb.data.split(":", 1)[1]
    data = await state.get_data()
    if not data.get("service_id") or not data.get("master_id"):
        await cb.answer(t("session_expired"), show_alert=True)
        return
    client = db.upsert_client(cb.from_user.id, cb.from_user.username)
    added = db.add_waitlist(client["id"], data["master_id"], data["service_id"], ds)
    text = (t("waitlist_added", date=fmt_d(ds)) if added
            else t("waitlist_already", date=fmt_d(ds)))
    await ui.safe_edit(cb.message, text,
                       kb([[btn(t("btn_other_day"), "dts")], [btn(t("btn_menu"), "menu")]]))
    await cb.answer()


@router.callback_query(F.data.startswith("wlbk:"))
async def cb_waitlist_book(cb: CallbackQuery, state: FSMContext):
    _, sid, mid, ds = cb.data.split(":")
    svc = db.get_service(int(sid))
    if not svc or not svc["active"]:
        await cb.answer(t("service_unavailable"), show_alert=True)
        return
    await state.set_state(None)
    await state.update_data(service_id=int(sid), master_id=int(mid), gs_id=None,
                            ext_nails=0, wl_date=ds)
    await start_booking_steps(cb.message, state, svc)
    await cb.answer()


# ---------- reviews ----------

@router.callback_query(F.data.startswith("fb:"))
async def cb_feedback(cb: CallbackQuery, state: FSMContext):
    _, aid, rating = cb.data.split(":")
    aid, rating = int(aid), int(rating)
    a = db.get_appointment(aid)
    if not a or a["client_tg"] != cb.from_user.id:
        await cb.answer(t("appt_not_found"), show_alert=True)
        return
    # the master gets the rating only on the first tap -- a repeat tap doesn't duplicate it
    if db.save_rating(aid, rating):
        await notify.admins_feedback(cb.bot, a, rating=rating)
    await state.set_state(Fb.comment)
    await state.update_data(fb_appt=aid)
    await ui.safe_edit(
        cb.message,
        t("feedback_thanks", stars="⭐" * rating),
        kb([[btn(t("btn_skip"), "fbskip")]]),
    )
    await cb.answer()


@router.callback_query(F.data == "fbskip")
async def cb_feedback_skip(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await ui.safe_edit(cb.message, t("feedback_skip_thanks"),
                       kb([[btn(t("btn_go_booking"), "bk")]]))
    await cb.answer()


@router.message(Fb.comment, F.text)
async def got_feedback_comment(m: Message, state: FSMContext):
    if m.text.startswith("/"):
        await state.clear()
        return
    data = await state.get_data()
    aid = data.get("fb_appt")
    await state.clear()
    if aid:
        db.save_comment(aid, m.text.strip())
        a = db.get_appointment(aid)
        if a:
            await notify.admins_feedback(m.bot, a, comment=m.text.strip())
    await m.answer(t("feedback_comment_thanks"),
                   reply_markup=kb([[btn(t("btn_go_booking"), "bk")]]))


# ---------- fallback ----------

@router.message(F.text)
async def fallback(m: Message, state: FSMContext):
    if await state.get_state():
        return
    db.upsert_client(m.from_user.id, m.from_user.username)
    await m.answer(t("choose_action"),
                   reply_markup=ui.main_menu_kb(config.is_admin(m.from_user.id)))
