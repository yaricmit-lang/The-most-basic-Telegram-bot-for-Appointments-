"""Клиентские сценарии: запись, повтор, отмена, подтверждение, отзывы, лист ожидания."""
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


# ---------- /start: для возвращающегося клиента — сразу «Повторить» ----------

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
            hello = f"С возвращением, {esc(client['name'])}! 👋" if client["name"] else "С возвращением! 👋"
            text = (
                f"{hello}\n\n"
                f"🔁 <b>Повторить:</b> {esc(svc['title'])}, "
                f"{esc(mst['name'])} ({fmt_dur(svc['duration_min'])})"
            )
            rows = [
                [btn("📅 Выбрать время", f"rpt:{svc['id']}:{mst['id']}")],
                [btn("🗂 Другая услуга", "bk")],
                [btn("📋 Мои записи", "my"), btn("👤 Мои данные", "prof")],
            ]
            if admin:
                rows.append([btn("⚙️ Панель мастера", "a:menu")])
            await m.answer(text, reply_markup=kb(rows))
            return

    await m.answer(
        "Привет! Я помогу записаться на услугу 💫\nВыберите действие:",
        reply_markup=ui.main_menu_kb(admin),
    )


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    db.upsert_client(cb.from_user.id, cb.from_user.username)
    await ui.safe_edit(cb.message, "Выберите действие:",
                       ui.main_menu_kb(config.is_admin(cb.from_user.id)))
    await cb.answer()


# ---------- личный кабинет ----------

def profile_view(client):
    lines = [
        "👤 <b>Мои данные</b>\n",
        f"Имя: <b>{esc(client['name']) or '—'}</b>",
        f"Телефон: <b>{esc(client['phone']) or '—'}</b>",
    ]
    if client["username"]:
        lines.append(f"Telegram: @{esc(client['username'])}")
    lines.append("\nЭти данные видит мастер в ваших записях.")
    rows = [
        [btn("✏️ Изменить имя", "profname")],
        [btn("📱 Изменить телефон", "profphone")],
        [btn("◀️ Меню", "menu")],
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
    await cb.message.answer("Как вас зовут?",
                            reply_markup=kb([[btn("◀️ Отмена", "prof")]]))
    await cb.answer()


@router.message(Profile.name, F.text)
async def got_profile_name(m: Message, state: FSMContext):
    name = m.text.strip()
    if name.startswith("/"):
        await state.clear()
        return
    if not name or len(name) > 100:
        await m.answer("Напишите, пожалуйста, имя обычным текстом 🙂")
        return
    client = db.upsert_client(m.from_user.id, m.from_user.username)
    db.set_client_name(client["id"], name)
    await state.clear()
    text, markup = profile_view(db.get_client_by_tg(m.from_user.id))
    await m.answer("✅ Имя обновлено.\n\n" + text, reply_markup=markup)


@router.callback_query(F.data == "profphone")
async def cb_profile_phone(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Profile.phone)
    contact_kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить мой номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await cb.message.answer("Пришлите номер кнопкой ниже или введите вручную:",
                            reply_markup=contact_kb)
    await cb.answer()


@router.message(Profile.phone, F.contact | F.text)
async def got_profile_phone(m: Message, state: FSMContext):
    if not m.contact and m.text.startswith("/"):
        await state.clear()
        await m.answer("Отменил.", reply_markup=ReplyKeyboardRemove())
        return
    raw = m.contact.phone_number if m.contact else m.text
    phone = db.norm_phone(raw)
    if len(phone) < 9:
        await m.answer("Хм, не похоже на номер телефона. Попробуйте ещё раз 🙂")
        return
    client = db.upsert_client(m.from_user.id, m.from_user.username)
    db.set_client_phone(client["id"], phone)
    db.merge_offline_client(client["id"], phone)
    await state.clear()
    await m.answer("✅ Телефон обновлён.", reply_markup=ReplyKeyboardRemove())
    text, markup = profile_view(db.get_client_by_tg(m.from_user.id))
    await m.answer(text, reply_markup=markup)


# ---------- выбор услуги / мастера / дня / времени ----------

@router.callback_query(F.data == "bk")
async def cb_book(cb: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await state.update_data(service_id=None, master_id=None, date=None, time=None,
                            gs_id=None, ext_nails=0, wl_date=None)
    services = db.list_services()
    if not services:
        await ui.safe_edit(cb.message, "Услуги пока не настроены. Загляните позже 🙌",
                           kb([[btn("◀️ Меню", "menu")]]))
        await cb.answer()
        return
    with_photos = db.services_with_photos()
    rows = []
    for s in services:
        label = f"{s['title']} · {fmt_dur(s['duration_min'])}"
        if s["price"]:
            label += f" · {s['price']}"
        if s["is_group"]:
            label = "👥 " + label
        if s["id"] in with_photos:
            label = "🖼 " + label
        rows.append([btn(label, f"svc:{s['id']}")])
    rows.append([btn("◀️ Меню", "menu")])
    text = "Выберите вид маникюра:"
    if with_photos:
        text += ("\n\n💡 Выбирайте желаемый маникюр по фото (🖼) — от дизайна "
                 "заметно зависят длительность работы и цена.")
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
        bits.append(f"+{p} € к цене")
    if mn:
        bits.append(f"+{mn} мин к сеансу")
    nums = [btn(str(n), f"ext:{n}") for n in range(1, 11)]
    rows = [[btn("Без наращивания", "ext:0")], nums[:5], nums[5:],
            [btn("◀️ Другой вид", "bk")]]
    await message.answer(
        f"💅 <b>{esc(svc['title'])}</b>\n\n"
        f"Нужно ли наращивание ногтей?\n"
        f"Один ноготь: {', '.join(bits)}.\n\n"
        f"Выберите, сколько ногтей наращиваем:",
        reply_markup=kb(rows),
    )


async def choose_master_or_dates(message, state: FSMContext, svc):
    if svc["is_group"]:
        await show_group_sessions(message, svc["id"])
        return
    masters = db.list_masters()
    if not masters:
        await ui.safe_edit(message, "Мастера пока не настроены 🙌",
                           kb([[btn("◀️ Меню", "menu")]]))
        return
    if len(masters) == 1:
        await state.update_data(master_id=masters[0]["id"])
        await show_dates(message, state)
        return
    rows = [[btn(f"👤 {m['name']}", f"mst:{m['id']}")] for m in masters]
    rows.append([btn("◀️ Назад", "bk")])
    await ui.safe_edit(message, f"<b>{esc(svc['title'])}</b>\nВыберите мастера:", kb(rows))


async def continue_to_time(message, state: FSMContext, svc):
    """После вопроса о наращивании: лист ожидания -> время, повтор -> даты, иначе мастер."""
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
        await cb.answer("Сессия устарела, начните заново", show_alert=True)
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
        await cb.answer("Услуга недоступна", show_alert=True)
        return
    await state.update_data(service_id=sid, gs_id=None, ext_nails=0, wl_date=None)

    # есть примеры — сначала показываем обложку и даём посмотреть остальные
    cover = db.service_cover(sid)
    if cover:
        n = db.service_photos_count(sid)
        caption = f"<b>{esc(svc['title'])}</b>\n⏱ {fmt_dur(svc['duration_min'])}"
        if svc["price"]:
            caption += f"\n💰 {esc(svc['price'])}"
        caption += "\n\nПримеры данного маникюра по сложности дизайна и длине."
        rows = []
        if n > 1:
            rows.append([btn(f"🖼 Показать примеры ({n})", f"svcex:{sid}")])
        rows.append([btn("📅 Выбрать время", f"svcgo:{sid}")])
        rows.append([btn("◀️ Другой вид", "bk")])
        try:
            await cb.message.answer_photo(cover, caption=caption, reply_markup=kb(rows))
            await cb.answer()
            return
        except Exception:
            pass  # фото не открылось — не мешаем записаться, идём дальше

    await start_booking_steps(cb.message, state, svc)
    await cb.answer()


@router.callback_query(F.data.startswith("svcex:"))
async def cb_service_examples(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[1])
    svc = db.get_service(sid)
    if not svc or not svc["active"]:
        await cb.answer("Вид недоступен", show_alert=True)
        return
    photos = db.service_photos(sid)
    if not photos:
        await cb.answer("Примеров пока нет", show_alert=True)
        return
    await state.update_data(service_id=sid, gs_id=None)
    try:
        await cb.bot.send_media_group(
            cb.from_user.id, media=[InputMediaPhoto(media=p["file_id"]) for p in photos])
    except Exception:
        await cb.answer("Не удалось показать примеры 😔", show_alert=True)
        return
    caption = f"🖼 <b>{esc(svc['title'])}</b> — примеров: {len(photos)}\n⏱ {fmt_dur(svc['duration_min'])}"
    if svc["price"]:
        caption += f" · 💰 {esc(svc['price'])}"
    await cb.message.answer(caption, reply_markup=kb([
        [btn("📅 Выбрать время", f"svcgo:{sid}")],
        [btn("◀️ Другой вид", "bk")],
    ]))
    await cb.answer()


@router.callback_query(F.data.startswith("svcgo:"))
async def cb_service_go(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[1])
    svc = db.get_service(sid)
    if not svc or not svc["active"]:
        await cb.answer("Услуга недоступна", show_alert=True)
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
        await ui.safe_edit(message, "Сессия устарела, начните заново 🙏",
                           kb([[btn("📅 Записаться", "bk")]]))
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
            "В ближайшие две недели нет рабочих дней. Загляните позже 🙏",
            kb([[btn("◀️ Назад", "bk")]]),
        )
        return
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([btn("◀️ Назад", "bk")])
    await ui.safe_edit(
        message,
        f"<b>{esc(svc['title'])}</b> — {esc(mst['name'])}\n"
        f"Выберите день (✖ — всё занято, можно встать в лист ожидания):",
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
        await ui.safe_edit(message, "Сессия устарела, начните заново 🙏",
                           kb([[btn("📅 Записаться", "bk")]]))
        return
    svc = db.get_service(data["service_id"])
    duration = db.effective_duration(svc, data.get("ext_nails") or 0)
    free = sl.free_slots(data["master_id"], ds, duration)
    await state.update_data(date=ds)

    if not free:
        await ui.safe_edit(
            message,
            f"На {fmt_d(ds)} всё занято 😔\n"
            f"Могу написать вам, если освободится окно.",
            kb([
                [btn("🔔 Сообщить, если освободится", f"wl:{ds}")],
                [btn("◀️ Другой день", "dts")],
            ]),
        )
        return
    buttons = [btn(t, f"tm:{t}") for t in free]
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    rows.append([btn("◀️ Другой день", "dts")])
    await ui.safe_edit(message, f"Свободное время на {fmt_d(ds)}:", kb(rows))


@router.callback_query(F.data.startswith("tm:"))
async def cb_time(cb: CallbackQuery, state: FSMContext):
    t = cb.data.split(":", 1)[1]
    await state.update_data(time=t)
    await ask_contacts_or_confirm(cb, state)


# ---------- групповые занятия: авто-лимит мест ----------

async def show_group_sessions(message, service_id):
    sessions = db.upcoming_group_sessions(service_id)
    rows = []
    for g in sessions:
        left = g["capacity"] - g["booked"]
        if left <= 0:
            continue
        rows.append([btn(
            f"{fmt_dt(g['starts_at'])} · {g['master_name']} · мест: {left}",
            f"gs:{g['id']}",
        )])
    if not rows:
        await ui.safe_edit(message, "Пока нет открытых групп с местами 😔",
                           kb([[btn("◀️ Назад", "bk")]]))
        return
    rows.append([btn("◀️ Назад", "bk")])
    await ui.safe_edit(message, "Выберите занятие:", kb(rows))


@router.callback_query(F.data.startswith("gs:"))
async def cb_group_session(cb: CallbackQuery, state: FSMContext):
    gid = int(cb.data.split(":")[1])
    g = db.get_group_session(gid)
    if not g or g["status"] != "active":
        await cb.answer("Занятие недоступно", show_alert=True)
        return
    if g["booked"] >= g["capacity"]:
        await cb.answer("Мест не осталось 😔", show_alert=True)
        return
    await state.update_data(gs_id=gid, service_id=g["service_id"], master_id=g["master_id"])
    await ask_contacts_or_confirm(cb, state)


# ---------- имя и телефон (один раз, дальше подставляются) ----------

async def ask_contacts_or_confirm(cb: CallbackQuery, state: FSMContext):
    client = db.upsert_client(cb.from_user.id, cb.from_user.username)
    if client["name"] and client["phone"]:
        await show_confirm(cb.message, state, client)
        await cb.answer()
        return
    await state.set_state(Booking.name)
    await cb.message.answer("Как вас зовут?")
    await cb.answer()


@router.message(Booking.name, F.text)
async def got_name(m: Message, state: FSMContext):
    name = m.text.strip()
    if not name or name.startswith("/") or len(name) > 100:
        await m.answer("Напишите, пожалуйста, имя обычным текстом 🙂")
        return
    client = db.get_client_by_tg(m.from_user.id)
    db.set_client_name(client["id"], name)
    await state.set_state(Booking.phone)
    contact_kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить мой номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await m.answer(
        "Оставьте номер телефона — нажмите кнопку ниже или введите вручную:",
        reply_markup=contact_kb,
    )


@router.message(Booking.phone, F.contact | F.text)
async def got_phone(m: Message, state: FSMContext):
    raw = m.contact.phone_number if m.contact else m.text
    phone = db.norm_phone(raw)
    if len(phone) < 9:
        await m.answer("Хм, не похоже на номер телефона. Попробуйте ещё раз 🙂")
        return
    client = db.get_client_by_tg(m.from_user.id)
    db.set_client_phone(client["id"], phone)
    db.merge_offline_client(client["id"], phone)
    await state.set_state(None)
    await m.answer("Спасибо! 🙌", reply_markup=ReplyKeyboardRemove())
    client = db.get_client_by_tg(m.from_user.id)
    await show_confirm(m, state, client, edit=False)


# ---------- подтверждение и создание ----------

def ext_note(svc, nails):
    """'+15 €, +45 мин' — сводка доплаты за наращивание."""
    bits = []
    extra = db.ext_extra_price(svc, nails)
    add_min = nails * (svc["ext_min_per_nail"] or 0)
    if extra:
        bits.append(f"+{extra} €")
    if add_min:
        bits.append(f"+{add_min} мин")
    return ", ".join(bits) if bits else "включено"


async def show_confirm(message, state: FSMContext, client, edit=True):
    data = await state.get_data()
    svc = db.get_service(data.get("service_id") or 0)
    if not svc:
        await message.answer("Сессия устарела, начните заново 🙏",
                             reply_markup=kb([[btn("📅 Записаться", "bk")]]))
        return

    nails = data.get("ext_nails") or 0
    if data.get("gs_id"):
        g = db.get_group_session(data["gs_id"])
        when = fmt_dt(g["starts_at"])
        master_name = g["master_name"]
        confirm_cb = "mkgs"
        back_row = [btn("◀️ Назад", f"svc:{svc['id']}")]
        duration, extra = svc["duration_min"], 0
        nails = 0
    else:
        if not data.get("date") or not data.get("time"):
            await message.answer("Сессия устарела, начните заново 🙏",
                                 reply_markup=kb([[btn("📅 Записаться", "bk")]]))
            return
        when = f"{fmt_d(data['date'])} в {data['time']}"
        master_name = db.get_master(data["master_id"])["name"]
        confirm_cb = "mkap"
        back_row = [btn("◀️ Назад", f"day:{data['date']}")]
        duration = db.effective_duration(svc, nails)
        extra = db.ext_extra_price(svc, nails)

    text = (
        f"<b>Проверьте запись:</b>\n\n"
        f"💅 {esc(svc['title'])} ({fmt_dur(duration)})\n"
    )
    if nails:
        text += f"➕ Наращивание: {nails} ног. ({ext_note(svc, nails)})\n"
    text += f"👤 Мастер: {esc(master_name)}\n📆 {when}\n"
    price_line = ui.price_with_ext(svc["price"], extra)
    if price_line:
        text += f"💰 {esc(price_line)}\n"
    text += f"\nИмя: {esc(client['name'])}\nТелефон: {esc(client['phone'])}"

    markup = kb([
        [btn("✅ Подтвердить", confirm_cb)],
        back_row,
        [btn("❌ Отмена", "menu")],
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
        await cb.answer("Сессия устарела, начните заново", show_alert=True)
        return
    svc = db.get_service(data["service_id"])
    nails = data.get("ext_nails") or 0
    duration = db.effective_duration(svc, nails)
    extra = db.ext_extra_price(svc, nails)

    if data["time"] not in sl.free_slots(data["master_id"], data["date"], duration):
        await cb.answer("Увы, это время только что заняли 😔", show_alert=True)
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
    ext_line = f"➕ Наращивание: {nails} ног. ({ext_note(svc, nails)})\n" if nails else ""
    await ui.safe_edit(
        cb.message,
        f"✅ <b>Вы записаны!</b>\n\n"
        f"💅 {esc(svc['title'])} ({fmt_dur(duration)})\n{ext_line}📆 {fmt_dt(starts)}\n\n"
        f"Хотите прислать пример дизайна? Так мастер успеет подготовить материалы.\n\n"
        f"Напомню за сутки и за 2 часа до визита.\n"
        f"Отменить запись можно в «📋 Мои записи».",
        kb([
            [btn("📎 Прислать референс", f"ref:{aid}")],
            [btn("📋 Мои записи", "my")],
            [btn("◀️ Меню", "menu")],
        ]),
    )
    await cb.answer("Запись создана!")


@router.callback_query(F.data == "mkgs")
async def cb_make_group(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    client = db.get_client_by_tg(cb.from_user.id)
    g = db.get_group_session(data.get("gs_id") or 0)
    if not client or not g or g["status"] != "active":
        await cb.answer("Занятие недоступно", show_alert=True)
        return
    if db.client_in_session(client["id"], g["id"]):
        await cb.answer("Вы уже записаны на это занятие 🙂", show_alert=True)
        return
    if g["booked"] >= g["capacity"]:
        await cb.answer("Мест не осталось 😔", show_alert=True)
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
        f"✅ <b>Вы записаны!</b>\n\n"
        f"👥 {esc(g['service_title'])}\n📆 {fmt_dt(starts)}\n"
        f"Свободных мест осталось: {left}\n\n"
        f"Напомню за сутки и за 2 часа.",
        kb([[btn("📋 Мои записи", "my")], [btn("◀️ Меню", "menu")]]),
    )
    await cb.answer("Запись создана!")


# ---------- повтор: услуга и мастер уже подставлены ----------

@router.callback_query(F.data.startswith("rpt:"))
async def cb_repeat(cb: CallbackQuery, state: FSMContext):
    _, sid, mid = cb.data.split(":")
    svc = db.get_service(int(sid))
    mst = db.get_master(int(mid))
    if not svc or not svc["active"] or not mst or not mst["active"]:
        await cb.answer("Эта услуга сейчас недоступна", show_alert=True)
        return
    await state.set_state(None)
    await state.update_data(service_id=svc["id"], master_id=mst["id"], gs_id=None,
                            ext_nails=0, wl_date=None)
    if svc["is_group"]:
        await show_group_sessions(cb.message, svc["id"])
    else:
        await start_booking_steps(cb.message, state, svc)
    await cb.answer()


# ---------- мои записи и отмена ----------

@router.callback_query(F.data == "my")
async def cb_my(cb: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    client = db.upsert_client(cb.from_user.id, cb.from_user.username)
    appts = db.upcoming_for_client(client["id"])
    if not appts:
        await ui.safe_edit(cb.message, "У вас нет активных записей.",
                           kb([[btn("📅 Записаться", "bk")], [btn("◀️ Меню", "menu")]]))
        await cb.answer()
        return
    rows = []
    for a in appts:
        mark = "✅" if a["status"] == "confirmed" else "🕐"
        pin = " 📎" if refs.has_ref(a) else ""
        rows.append([btn(f"{mark} {fmt_dt(a['starts_at'])} — {a['service_title']}{pin}",
                         f"myap:{a['id']}")])
    rows.append([btn("📅 Записаться ещё", "bk")])
    rows.append([btn("◀️ Меню", "menu")])
    await ui.safe_edit(cb.message, "<b>Ваши записи</b>\nВыберите запись:", kb(rows))
    await cb.answer()


def _own(a, cb):
    return a is not None and a["client_tg"] == cb.from_user.id


def _appt_minutes(a):
    """Реальная длительность записи (с наращиванием) из её времени."""
    s = datetime.strptime(a["starts_at"], "%Y-%m-%d %H:%M")
    e = datetime.strptime(a["ends_at"], "%Y-%m-%d %H:%M")
    return max(0, int((e - s).total_seconds() // 60))


def client_card(a):
    mark = "✅" if a["status"] == "confirmed" else "🕐"
    lines = [
        f"{mark} <b>Ваша запись</b>\n",
        f"💅 {esc(a['service_title'])} ({fmt_dur(_appt_minutes(a))})",
    ]
    if a["ext_nails"]:
        note = f" (+{a['ext_price']} €)" if a["ext_price"] else ""
        lines.append(f"➕ Наращивание: {a['ext_nails']} ног.{note}")
    lines += [
        f"👤 Мастер: {esc(a['master_name'])}",
        f"📆 {fmt_dt(a['starts_at'])}",
    ]
    price_line = ui.price_with_ext(a["price"], a["ext_price"])
    if price_line:
        lines.append(f"💰 {esc(price_line)}")

    summary = refs.summary(a)
    rows = []
    if summary:
        lines.append(f"\n🖼 Референс: {esc(summary)}")
        if a["ref_status"] == "ok":
            lines.append("✅ Мастер подтвердил: материалы есть")
        elif a["ref_status"] == "no_materials":
            lines.append("❌ Мастер написал: нет подходящих материалов")
    if refs.is_open(a):
        rows.append([btn("🖼 Изменить референс" if summary else "📎 Прислать референс",
                         f"ref:{a['id']}")])
    elif not summary:
        lines.append("\n⏳ Приём референса по этой записи закрыт.")
    rows.append([btn("✍️ Написать мастеру", f"msg:{a['id']}")])
    rows.append([btn("❌ Отменить запись", f"myccl:{a['id']}")])
    rows.append([btn("◀️ Назад", "my")])
    return "\n".join(lines), kb(rows)


@router.callback_query(F.data.startswith("myap:"))
async def cb_my_appt(cb: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    a = db.get_appointment(int(cb.data.split(":")[1]))
    if not _own(a, cb):
        await cb.answer("Запись не найдена", show_alert=True)
        return
    text, markup = client_card(a)
    await ui.safe_edit(cb.message, text, markup)
    await cb.answer()


# ---------- референс: фото/видео дизайна ----------

def ref_prompt(a):
    deadline, late = refs.window(a)
    limit = db.get_int("ref_max", 5)
    text = (
        f"📎 <b>Референс к записи</b> {fmt_dt(a['starts_at'])}\n\n"
        f"Пришлите фото или видео дизайна — можно несколько, до {limit}.\n"
        f"Текстом можно добавить комментарий или ссылку."
    )
    if late:
        text += ("\n\n⚠️ Вы записались в тот же день, поэтому мастер может не успеть "
                 "подготовить материалы. Прислать всё равно можно — до начала визита.")
    else:
        text += f"\n\n⏳ Принимаю до {fmt_dt(deadline)}."
    n = db.refs_count(a["id"])
    if n:
        text += f"\n\nСейчас прислано: {n}."
    if a["ref_comment"]:
        text += f"\n💬 «{esc(a['ref_comment'])}»"
    return text


def ref_kb(a):
    rows = []
    if refs.has_ref(a):
        rows.append([btn("🗑 Удалить всё", f"refdel:{a['id']}")])
    rows.append([btn("◀️ Назад", f"myap:{a['id']}")])
    return kb(rows)


@router.callback_query(F.data.startswith("ref:"))
async def cb_ref(cb: CallbackQuery, state: FSMContext):
    aid = int(cb.data.split(":")[1])
    a = db.get_appointment(aid)
    if not _own(a, cb):
        await cb.answer("Запись не найдена", show_alert=True)
        return
    if not refs.is_open(a):
        await ui.safe_edit(
            cb.message,
            "⏳ Приём референса по этой записи уже закрыт — мастер готовит материалы "
            "заранее.\n\nЕсли дизайн важно поменять, напишите мастеру напрямую.",
            kb([[btn("✍️ Написать мастеру", f"msg:{aid}")], [btn("◀️ Назад", f"myap:{aid}")]]),
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
        await m.answer("⏳ Приём референса по этой записи уже закрыт.")
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
        await m.answer(f"Уже прислано максимум ({limit}). "
                       f"Чтобы заменить — удалите всё и пришлите заново.",
                       reply_markup=ref_kb(a))
        return
    await m.answer(f"📎 Принял. Всего: {db.refs_count(a['id'])}.\n"
                   f"Можно прислать ещё — мастер получит всё автоматически.",
                   reply_markup=ref_kb(a))


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
        await m.answer("⏳ Приём референса по этой записи уже закрыт.")
        return
    db.set_ref_comment(a["id"], m.text.strip()[:500])
    db.schedule_ref_notify(a["id"])
    await m.answer("💬 Комментарий сохранён.", reply_markup=ref_kb(db.get_appointment(a["id"])))


@router.callback_query(F.data.startswith("refdone:"))
async def cb_ref_done(cb: CallbackQuery, state: FSMContext):
    """Кнопки «Готово» больше нет — референс уходит мастеру сам. Обработчик оставлен,
    чтобы кнопка в старых сообщениях не превращалась в вечный спиннер."""
    aid = int(cb.data.split(":")[1])
    a = db.get_appointment(aid)
    if not _own(a, cb):
        await cb.answer("Запись не найдена", show_alert=True)
        return
    if not refs.has_ref(a):
        await cb.answer("Вы ещё ничего не прислали", show_alert=True)
        return
    await state.set_state(None)
    db.mark_ref_notified(aid)  # чтобы отложенное уведомление не продублировало
    await notify.master_reference(cb.bot, a)
    text, markup = client_card(db.get_appointment(aid))
    await ui.safe_edit(cb.message, "✅ Референс отправлен мастеру!\n\n" + text, markup)
    await cb.answer("Отправлено!")


@router.callback_query(F.data.startswith("refdel:"))
async def cb_ref_del(cb: CallbackQuery):
    aid = int(cb.data.split(":")[1])
    a = db.get_appointment(aid)
    if not _own(a, cb):
        await cb.answer("Запись не найдена", show_alert=True)
        return
    if not refs.is_open(a):
        await cb.answer("Окно приёма референса закрыто", show_alert=True)
        return
    db.clear_refs(aid)
    a = db.get_appointment(aid)
    await ui.safe_edit(cb.message, ref_prompt(a), ref_kb(a))
    await cb.answer("Удалено")


@router.callback_query(F.data == "refskip")
async def cb_ref_skip(cb: CallbackQuery):
    await ui.safe_edit(cb.message,
                       "Хорошо! Если передумаете — референс можно прислать "
                       "в любой момент из «📋 Мои записи».",
                       kb([[btn("📋 Мои записи", "my")]]))
    await cb.answer()


# ---------- переписка с мастером ----------

@router.callback_query(F.data.startswith("msg:"))
@router.callback_query(F.data.startswith("crep:"))
async def cb_write_master(cb: CallbackQuery, state: FSMContext):
    aid = int(cb.data.split(":")[1])
    a = db.get_appointment(aid)
    if not _own(a, cb):
        await cb.answer("Запись не найдена", show_alert=True)
        return
    await state.set_state(Chat.to_master)
    await state.update_data(chat_aid=aid)
    await cb.message.answer(
        f"✍️ Напишите сообщение мастеру по записи {fmt_dt(a['starts_at'])}:",
        reply_markup=kb([[btn("◀️ Отмена", f"myap:{aid}")]]),
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
    await m.answer("✅ Сообщение отправлено мастеру.",
                   reply_markup=kb([[btn("◀️ К записи", f"myap:{a['id']}")]]))


@router.callback_query(F.data.startswith("myccl:"))
@router.callback_query(F.data.startswith("ccl:"))
async def cb_cancel_ask(cb: CallbackQuery):
    aid = int(cb.data.split(":")[1])
    a = db.get_appointment(aid)
    if not a or a["status"] in ("cancelled", "completed"):
        await cb.answer("Эта запись уже неактуальна", show_alert=True)
        return
    if a["client_tg"] != cb.from_user.id and not config.is_admin(cb.from_user.id):
        await cb.answer("Это не ваша запись", show_alert=True)
        return
    await ui.safe_edit(
        cb.message,
        f"Отменить запись?\n\n{esc(a['service_title'])}, {fmt_dt(a['starts_at'])}",
        kb([
            [btn("❌ Да, отменить", f"doccl:{aid}")],
            [btn("◀️ Нет, оставить", "my")],
        ]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("doccl:"))
async def cb_cancel_do(cb: CallbackQuery):
    aid = int(cb.data.split(":")[1])
    a = db.get_appointment(aid)
    if not a or a["status"] in ("cancelled", "completed"):
        await cb.answer("Уже неактуально", show_alert=True)
        return
    if a["client_tg"] != cb.from_user.id and not config.is_admin(cb.from_user.id):
        await cb.answer("Это не ваша запись", show_alert=True)
        return
    # атомарно: уведомление мастеру и лист ожидания — ровно один раз на отмену
    if not db.transition_status(aid, ("created", "confirmed"), "cancelled"):
        await cb.answer("Уже неактуально", show_alert=True)
        return
    db.drop_pending_notifications(aid)
    await notify.master_cancelled(cb.bot, a, actor_tg=cb.from_user.id)
    await notify.run_waitlist(cb.bot, a["master_id"], a["starts_at"][:10],
                              exclude_client=a["client_id"])
    await ui.safe_edit(
        cb.message,
        "Запись отменена. Будем ждать вас снова! 💛",
        kb([[btn("📅 Записаться", "bk")], [btn("◀️ Меню", "menu")]]),
    )
    await cb.answer()


# ---------- подтверждение за 24 часа ----------

@router.callback_query(F.data.startswith("cfm:"))
async def cb_confirm_visit(cb: CallbackQuery):
    aid = int(cb.data.split(":")[1])
    a = db.get_appointment(aid)
    if not a or a["client_tg"] != cb.from_user.id:
        await cb.answer("Запись не найдена", show_alert=True)
        return
    if a["status"] == "created":
        if not db.transition_status(aid, ("created",), "confirmed"):
            await cb.answer("Уже подтверждено 🙂")
            return
        await ui.safe_edit(cb.message,
                           f"✅ Спасибо, запись подтверждена!\n"
                           f"{esc(a['service_title'])}, {fmt_dt(a['starts_at'])}. Ждём вас!")
        await cb.answer("Подтверждено!")
    elif a["status"] == "confirmed":
        await cb.answer("Уже подтверждено 🙂")
    else:
        await cb.answer("Эта запись уже неактуальна", show_alert=True)


# ---------- лист ожидания ----------

@router.callback_query(F.data.startswith("wl:"))
async def cb_waitlist(cb: CallbackQuery, state: FSMContext):
    ds = cb.data.split(":", 1)[1]
    data = await state.get_data()
    if not data.get("service_id") or not data.get("master_id"):
        await cb.answer("Сессия устарела, начните заново", show_alert=True)
        return
    client = db.upsert_client(cb.from_user.id, cb.from_user.username)
    added = db.add_waitlist(client["id"], data["master_id"], data["service_id"], ds)
    text = (f"🔔 Готово! Если на {fmt_d(ds)} освободится окно — сразу напишу."
            if added else f"Вы уже в листе ожидания на {fmt_d(ds)} 🙂")
    await ui.safe_edit(cb.message, text,
                       kb([[btn("📅 Другой день", "dts")], [btn("◀️ Меню", "menu")]]))
    await cb.answer()


@router.callback_query(F.data.startswith("wlbk:"))
async def cb_waitlist_book(cb: CallbackQuery, state: FSMContext):
    _, sid, mid, ds = cb.data.split(":")
    svc = db.get_service(int(sid))
    if not svc or not svc["active"]:
        await cb.answer("Услуга недоступна", show_alert=True)
        return
    await state.set_state(None)
    await state.update_data(service_id=int(sid), master_id=int(mid), gs_id=None,
                            ext_nails=0, wl_date=ds)
    await start_booking_steps(cb.message, state, svc)
    await cb.answer()


# ---------- отзывы ----------

@router.callback_query(F.data.startswith("fb:"))
async def cb_feedback(cb: CallbackQuery, state: FSMContext):
    _, aid, rating = cb.data.split(":")
    aid, rating = int(aid), int(rating)
    a = db.get_appointment(aid)
    if not a or a["client_tg"] != cb.from_user.id:
        await cb.answer("Запись не найдена", show_alert=True)
        return
    # мастеру оценку шлём только при первом нажатии — повторный тап не задваивает
    if db.save_rating(aid, rating):
        await notify.admins_feedback(cb.bot, a, rating=rating)
    await state.set_state(Fb.comment)
    await state.update_data(fb_appt=aid)
    await ui.safe_edit(
        cb.message,
        f"Спасибо за оценку {'⭐' * rating}!\n"
        f"Хотите добавить пару слов? Просто напишите сообщение.",
        kb([[btn("Пропустить", "fbskip")]]),
    )
    await cb.answer()


@router.callback_query(F.data == "fbskip")
async def cb_feedback_skip(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await ui.safe_edit(cb.message, "Спасибо! Будем рады видеть вас снова 💛",
                       kb([[btn("📅 Записаться", "bk")]]))
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
    await m.answer("Спасибо за отзыв! 💛",
                   reply_markup=kb([[btn("📅 Записаться", "bk")]]))


# ---------- fallback ----------

@router.message(F.text)
async def fallback(m: Message, state: FSMContext):
    if await state.get_state():
        return
    db.upsert_client(m.from_user.id, m.from_user.username)
    await m.answer("Выберите действие:",
                   reply_markup=ui.main_menu_kb(config.is_admin(m.from_user.id)))
