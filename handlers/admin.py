"""Панель владельца: записи, услуги, мастера, график, группы, ручная запись, настройки.

Все callback'и админки начинаются с "a:", чтобы не пересекаться с клиентскими.
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
    # добавление услуги
    svc_title = State()
    svc_dur = State()
    svc_price = State()
    svc_cap = State()
    svc_comeback = State()
    svc_edit = State()      # data: field, sid
    svc_photo = State()     # data: ph_sid
    # мастера
    m_name = State()
    m_tg = State()
    m_edit = State()        # data: field, mid
    # график
    sched = State()         # data: sched_master, sched_wd
    ovr_date = State()      # data: ovr_master
    ovr_val = State()
    # группы
    gs_dt = State()         # data: gs_service, gs_master
    gs_cap = State()
    # настройки
    set_val = State()       # data: set_key
    owner_name = State()    # имя владельца-мастера (видят клиенты)
    # переписка с клиентом
    reply_text = State()    # data: rep_aid
    nomat_text = State()    # data: nomat_aid
    # ручная запись
    nb_time = State()
    nb_client = State()
    nb_name = State()
    nb_phone = State()


# ---------- парсинг ввода ----------

TIME_RANGE = re.compile(r"(\d{1,2})[:.](\d{2})\s*[-–—]\s*(\d{1,2})[:.](\d{2})")
TIME_RE = re.compile(r"^(\d{1,2})[:.](\d{2})$")
DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?$")
DT_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\s+(\d{1,2})[:.](\d{2})$")


def parse_schedule_text(text):
    """'10:00-19:00 обед 14:00-15:00' | '10:00-19:00' | 'выходной' -> dict | None"""
    t = text.strip().lower()
    if "выход" in t or "off" in t or t in ("-", "нет", "no", "none"):
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


# ---------- панель ----------

def panel_text():
    today = datetime.now().date()
    n = len(db.appointments_between(today.isoformat() + " 00:00",
                                    (today + timedelta(days=1)).isoformat() + " 00:00"))
    return f"⚙️ <b>Панель мастера</b>\nЗаписей на сегодня: {n}"


def panel_kb():
    return kb([
        [btn("📆 Записи", "a:ap"), btn("➕ Новая запись", "a:nb")],
        [btn("🧾 Услуги", "a:svcs"), btn("📅 График", "a:sch")],
        [btn("⚙️ Настройки", "a:set")],
        [btn("◀️ Главное меню", "menu")],
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


# ---------- записи ----------

@router.callback_query(F.data == "a:ap")
async def cb_appts(cb: CallbackQuery):
    await ui.safe_edit(cb.message, "За какой период показать записи?", kb([
        [btn("Сегодня", "a:apd:0"), btn("Завтра", "a:apd:1")],
        [btn("Неделя", "a:apd:7")],
        [btn("◀️ Назад", "a:menu")],
    ]))
    await cb.answer()


@router.callback_query(F.data.startswith("a:apd:"))
async def cb_appts_day(cb: CallbackQuery):
    n = int(cb.data.split(":")[2])
    today = datetime.now().date()
    if n == 7:
        d1, d2 = today, today + timedelta(days=7)
        title = "на неделю"
    else:
        d1 = today + timedelta(days=n)
        d2 = d1 + timedelta(days=1)
        title = "на " + ("сегодня" if n == 0 else "завтра")
    appts = db.appointments_between(d1.isoformat() + " 00:00", d2.isoformat() + " 00:00")

    if not appts:
        await ui.safe_edit(cb.message, f"Записей {title} нет.",
                           kb([[btn("◀️ Назад", "a:ap")]]))
        await cb.answer()
        return

    lines = [f"📆 <b>Записи {title}:</b>"]
    rows = []
    cur_date = None
    for a in appts:
        d = a["starts_at"][:10]
        if d != cur_date:
            cur_date = d
            lines.append(f"\n<b>{fmt_d(d)}</b>")
        mark = "✅" if a["status"] == "confirmed" else "🕐"
        who = esc(a["client_name"] or "—")
        ext = f" ➕{a['ext_nails']} ног." if a["ext_nails"] else ""
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
                row.append(btn("🖼 Референс", f"a:refv:{a['id']}"))
            rows.append(row)
    rows.append([btn("◀️ Назад", "a:ap")])
    await ui.safe_edit(cb.message, "\n".join(lines), kb(rows))
    await cb.answer()


@router.callback_query(F.data.startswith("a:ccl:"))
async def cb_admin_cancel_ask(cb: CallbackQuery):
    aid = int(cb.data.split(":")[2])
    a = db.get_appointment(aid)
    if not a or a["status"] in ("cancelled", "completed"):
        await cb.answer("Уже неактуально", show_alert=True)
        return
    await ui.safe_edit(
        cb.message,
        f"Отменить запись?\n\n{fmt_dt(a['starts_at'])} — {esc(a['service_title'])}\n"
        f"Клиент: {esc(a['client_name'])} {esc(a['client_phone'] or '')}",
        kb([[btn("❌ Да, отменить", f"a:ccl2:{aid}")], [btn("◀️ Назад", "a:ap")]]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("a:ccl2:"))
async def cb_admin_cancel_do(cb: CallbackQuery):
    aid = int(cb.data.split(":")[2])
    a = db.get_appointment(aid)
    if not a or a["status"] in ("cancelled", "completed"):
        await cb.answer("Уже неактуально", show_alert=True)
        return
    db.set_appointment_status(aid, "cancelled")
    db.drop_pending_notifications(aid)
    await notify.client_cancelled_by_admin(cb.bot, a)
    await notify.run_waitlist(cb.bot, a["master_id"], a["starts_at"][:10],
                              exclude_client=a["client_id"])
    await ui.safe_edit(cb.message, "Запись отменена, клиент уведомлён.",
                       kb([[btn("◀️ К записям", "a:ap")]]))
    await cb.answer()


# ---------- референсы и переписка с клиентом ----------

NO_MATERIALS_TEMPLATE = (
    "К сожалению, у меня нет подходящих материалов для этого дизайна. "
    "Давайте подберём альтернативу?"
)


@router.callback_query(F.data.startswith("a:refv:"))
async def cb_ref_view(cb: CallbackQuery):
    a = db.get_appointment(int(cb.data.split(":")[2]))
    if not a:
        await cb.answer("Запись не найдена", show_alert=True)
        return
    await notify.send_reference_to(cb.bot, cb.from_user.id, a)
    await cb.answer()


@router.callback_query(F.data.startswith("a:refok:"))
async def cb_ref_ok(cb: CallbackQuery):
    aid = int(cb.data.split(":")[2])
    a = db.get_appointment(aid)
    if not a:
        await cb.answer("Запись не найдена", show_alert=True)
        return
    # атомарно: клиенту «материалы есть» уходит ровно один раз
    if not db.set_ref_status_from(aid, "ok", ("", "no_materials")):
        await cb.answer("Уже подтверждено")
        return
    await notify.client_ref_ok(cb.bot, a)
    text, _ = notify.ref_card(db.get_appointment(aid))
    # кнопки-статусы убираем: повторное нажатие слало бы клиенту второе подтверждение
    await ui.safe_edit(cb.message, text + "\n\n✅ Вы подтвердили: материалы есть.", kb([
        [btn("↩️ Изменить ответ", f"a:refv:{aid}")],
        [btn("◀️ Панель мастера", "a:menu")],
    ]))
    await cb.answer("Клиент уведомлён")


@router.callback_query(F.data.startswith("a:reflater:"))
async def cb_ref_later(cb: CallbackQuery):
    aid = int(cb.data.split(":")[2])
    a = db.get_appointment(aid)
    if not a:
        await cb.answer("Запись не найдена", show_alert=True)
        return
    text, _ = notify.ref_card(a)
    # референс остаётся без статуса — его подхватит проверка в конце рабочего дня
    if a["starts_at"][:10] > datetime.now().strftime("%Y-%m-%d"):
        note = "⏰ <b>Отложено.</b> Напомню накануне визита, в конце рабочего дня."
    else:
        note = "⏰ <b>Отложено.</b> Референс остаётся в «📆 Записи»."
    await ui.safe_edit(cb.message, text + "\n\n" + note, kb([
        [btn("↩️ Ответить сейчас", f"a:refv:{aid}")],
        [btn("◀️ Панель мастера", "a:menu")],
    ]))
    await cb.answer("Отложено")


@router.callback_query(F.data.startswith("a:refno:"))
async def cb_ref_no(cb: CallbackQuery, state: FSMContext):
    aid = int(cb.data.split(":")[2])
    a = db.get_appointment(aid)
    if not a:
        await cb.answer("Запись не найдена", show_alert=True)
        return
    await state.set_state(Adm.nomat_text)
    await state.update_data(nomat_aid=aid)
    await cb.message.answer(
        f"Готовый текст для клиента {esc(a['client_name'] or '')}:\n\n"
        f"«{esc(NO_MATERIALS_TEMPLATE)}»\n\n"
        f"Отправить как есть — или пришлите свой текст, он добавится к этому сообщению.",
        reply_markup=kb([
            [btn("📨 Отправить как есть", f"a:nomatgo:{aid}")],
            [btn("◀️ Отмена", "a:menu")],
        ]),
    )
    await cb.answer()


async def _send_no_materials(bot, message, aid, text):
    a = db.get_appointment(aid)
    if not a:
        await message.answer("Запись не найдена")
        return
    db.set_ref_status(aid, "no_materials")
    db.add_message(aid, "master", text)
    sent = await notify.client_master_message(bot, a, text)
    note = ("✅ Сообщение отправлено клиенту." if sent
            else "⚠️ У клиента нет Telegram — сообщение не доставлено, позвоните ему.")
    await message.answer(note, reply_markup=kb([[btn("◀️ Панель", "a:menu")]]))


@router.callback_query(F.data.startswith("a:nomatgo:"))
async def cb_nomat_send(cb: CallbackQuery, state: FSMContext):
    aid = int(cb.data.split(":")[2])
    await state.clear()
    await _send_no_materials(cb.bot, cb.message, aid, NO_MATERIALS_TEMPLATE)
    await cb.answer("Отправлено")


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
        await cb.answer("Запись не найдена", show_alert=True)
        return
    await state.set_state(Adm.reply_text)
    await state.update_data(rep_aid=aid)
    await cb.message.answer(
        f"✍️ Что написать клиенту {esc(a['client_name'] or '')} "
        f"по записи {fmt_dt(a['starts_at'])}?",
        reply_markup=kb([[btn("◀️ Отмена", "a:menu")]]),
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
    await m.answer("✅ Отправлено клиенту." if sent
                   else "⚠️ У клиента нет Telegram — не доставлено, позвоните ему.",
                   reply_markup=kb([[btn("◀️ Панель", "a:menu")]]))


# ---------- услуги ----------

def svc_list_kb():
    rows = []
    for s in db.list_services(only_active=False):
        mark = "🟢" if s["active"] else "⚪"
        g = "👥 " if s["is_group"] else ""
        rows.append([btn(f"{mark} {g}{s['title']}", f"a:svc:{s['id']}")])
    rows.append([btn("➕ Добавить услугу", "a:svcadd")])
    rows.append([btn("◀️ Назад", "a:menu")])
    return kb(rows)


@router.callback_query(F.data == "a:svcs")
async def cb_services(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await ui.safe_edit(cb.message, "🧾 <b>Услуги</b>", svc_list_kb())
    await cb.answer()


def svc_detail(sid):
    s = db.get_service(sid)
    if not s:
        return "Услуга не найдена", kb([[btn("◀️ Назад", "a:svcs")]])
    n_photos = db.service_photos_count(sid)
    if s["ext_price_per_nail"] or s["ext_min_per_nail"]:
        ext_line = (f"➕ Наращивание: +{s['ext_price_per_nail']} € и "
                    f"+{s['ext_min_per_nail']} мин за ноготь")
    else:
        ext_line = "➕ Наращивание: не предлагается"
    text = (
        f"<b>{esc(s['title'])}</b>\n"
        f"⏱ Длительность: {fmt_dur(s['duration_min'])}\n"
        f"💰 Цена: {esc(s['price']) or '—'}\n"
        f"{ext_line}\n"
        f"🖼 Примеры: {n_photos if n_photos else 'нет'}\n"
        f"🔁 Приглашение вернуться: "
        f"{'через ' + str(s['comeback_days']) + ' дн.' if s['comeback_days'] else 'выключено'}\n"
        f"Статус: {'🟢 активна' if s['active'] else '⚪ скрыта'}"
    )
    if s["is_group"]:
        text += f"\n👥 Групповая, мест: {s['capacity']}"
    rows = [
        [btn("✏️ Название", f"a:svce:title:{sid}"),
         btn("⏱ Длительность", f"a:svce:dur:{sid}")],
        [btn("💰 Цена", f"a:svce:price:{sid}"),
         btn("🔁 Дней до напоминания", f"a:svce:cb:{sid}")],
        [btn("➕ Наращивание: €/ноготь", f"a:svce:extp:{sid}"),
         btn("➕ мин/ноготь", f"a:svce:extm:{sid}")],
        [btn(f"🖼 Примеры ({n_photos})" if n_photos else "🖼 Добавить примеры",
             f"a:svcph:{sid}")],
    ]
    if s["is_group"]:
        rows.append([btn("👥 Мест в группе", f"a:svce:cap:{sid}")])
    rows.append([btn("⚪ Скрыть" if s["active"] else "🟢 Включить", f"a:svct:{sid}")])
    rows.append([btn("◀️ Назад", "a:svcs")])
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
    "title": ("Новое название услуги:", "title", str),
    "dur": ("Длительность в минутах (например 90):", "duration_min", int),
    "price": ("Цена (текст, например «2500 ₽», или «-» чтобы убрать):", "price", str),
    "cb": ("Через сколько дней приглашать вернуться? (0 — не приглашать):", "comeback_days", int),
    "cap": ("Сколько мест в группе?", "capacity", int),
    "extp": ("Наращивание: доплата за 1 ноготь в € (0 — не предлагать):",
             "ext_price_per_nail", int),
    "extm": ("Наращивание: сколько минут добавляет 1 ноготь (0 — не добавляет):",
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
            await m.answer("Нужно число. Попробуйте ещё раз:")
            return
        value = int(raw)
        if column in ("duration_min", "capacity") and value <= 0:
            await m.answer("Число должно быть больше нуля:")
            return
    else:
        value = "" if raw == "-" else raw
    db.update_service_field(data["sid"], column, value)
    await state.clear()
    text, markup = svc_detail(data["sid"])
    await m.answer(text, reply_markup=markup)


async def send_gallery(bot, chat_id, sid):
    """Альбом примеров + управление: номера кнопок 🗑 совпадают с порядком фото."""
    s = db.get_service(sid)
    photos = db.service_photos(sid)
    if photos:
        try:
            await bot.send_media_group(
                chat_id, media=[InputMediaPhoto(media=p["file_id"]) for p in photos])
        except Exception as e:
            log.warning("Примеры услуги %s не показались: %s", sid, e)
    rows = []
    if photos:
        del_btns = [btn(f"🗑 {i}", f"a:svcphdel:{p['id']}")
                    for i, p in enumerate(photos, 1)]
        rows += [del_btns[i:i + 5] for i in range(0, len(del_btns), 5)]
    if len(photos) < db.SVC_PHOTO_MAX:
        rows.append([btn("➕ Добавить фото", f"a:svcphadd:{sid}")])
    rows.append([btn("✏️ Переименовать вид", f"a:svce:title:{sid}")])
    rows.append([btn("◀️ К виду маникюра", f"a:svc:{sid}")])

    text = f"🖼 <b>Примеры: {esc(s['title'])}</b>\n\n"
    if photos:
        text += (f"Сейчас {len(photos)} шт. — клиент увидит их по кнопке "
                 f"«Показать примеры». Первое фото — обложка.\n"
                 f"Номера кнопок 🗑 совпадают с порядком фото выше.")
    else:
        text += "Примеров пока нет. Первое фото станет обложкой вида."
    await bot.send_message(chat_id, text, reply_markup=kb(rows))


@router.callback_query(F.data.startswith("a:svcph:"))
async def cb_service_photos(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    sid = int(cb.data.split(":")[2])
    if not db.get_service(sid):
        await cb.answer("Вид не найден", show_alert=True)
        return
    await send_gallery(cb.bot, cb.from_user.id, sid)
    await cb.answer()


@router.callback_query(F.data.startswith("a:svcphadd:"))
async def cb_service_photo_add(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[2])
    if not db.get_service(sid):
        await cb.answer("Вид не найден", show_alert=True)
        return
    if db.service_photos_count(sid) >= db.SVC_PHOTO_MAX:
        await cb.answer(f"Уже максимум ({db.SVC_PHOTO_MAX})", show_alert=True)
        return
    await state.set_state(Adm.svc_photo)
    await state.update_data(ph_sid=sid)
    await cb.message.answer(
        "Пришлите фото-пример — можно несколько сразу.",
        reply_markup=kb([[btn("🖼 Готово, показать все", f"a:svcph:{sid}")]]),
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
        await m.answer(f"Больше {db.SVC_PHOTO_MAX} примеров не поместится — "
                       f"удалите лишние.",
                       reply_markup=kb([[btn("🖼 Показать все", f"a:svcph:{sid}")]]))
        return
    await m.answer(
        f"📎 Добавил. Всего примеров: {db.service_photos_count(sid)}.",
        reply_markup=kb([
            [btn("🖼 Готово, показать все", f"a:svcph:{sid}")],
            [btn("◀️ К виду маникюра", f"a:svc:{sid}")],
        ]),
    )


@router.message(Adm.svc_photo, F.text)
async def got_service_photo_wrong(m: Message, state: FSMContext):
    if m.text.startswith("/"):
        await state.clear()
        return
    await m.answer("Нужно именно фото — пришлите картинку 🙂")


@router.callback_query(F.data.startswith("a:svcphdel:"))
async def cb_service_photo_del(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[2])
    row = db.get_service_photo(pid)
    if not row:
        await cb.answer("Уже удалено")
        return
    await state.clear()
    db.delete_service_photo(pid)
    await cb.answer("Удалено")
    await send_gallery(cb.bot, cb.from_user.id, row["service_id"])


@router.callback_query(F.data == "a:svcadd")
async def cb_service_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.svc_title)
    await state.update_data(new_svc={})
    await cb.message.answer("Название услуги?")
    await cb.answer()


@router.message(Adm.svc_title, F.text)
async def got_svc_title(m: Message, state: FSMContext):
    data = await state.get_data()
    data["new_svc"]["title"] = m.text.strip()
    await state.update_data(new_svc=data["new_svc"])
    await state.set_state(Adm.svc_dur)
    await m.answer("Длительность в минутах? (например 90)")


@router.message(Adm.svc_dur, F.text)
async def got_svc_dur(m: Message, state: FSMContext):
    if not m.text.strip().isdigit() or int(m.text.strip()) <= 0:
        await m.answer("Нужно число минут, например 90:")
        return
    data = await state.get_data()
    data["new_svc"]["duration"] = int(m.text.strip())
    await state.update_data(new_svc=data["new_svc"])
    await state.set_state(Adm.svc_price)
    await m.answer("Цена? (текст, например «2500 ₽», или «-» чтобы пропустить)")


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
        "Через сколько дней присылать клиенту «пора обновить»?\n"
        "(число дней, 0 — не присылать; по умолчанию 21 — отправьте «-»)"
    )


@router.message(Adm.svc_comeback, F.text)
async def got_svc_comeback(m: Message, state: FSMContext):
    raw = m.text.strip()
    if raw == "-":
        days = 21
    elif raw.isdigit():
        days = int(raw)
    else:
        await m.answer("Нужно число дней, 0 или «-»:")
        return
    data = await state.get_data()
    ns = data["new_svc"]
    db.add_service(ns["title"], ns["duration"], ns["price"],
                   ns.get("is_group", False), ns.get("capacity", 1), days)
    await state.clear()
    await m.answer(f"✅ Услуга «{esc(ns['title'])}» добавлена.", reply_markup=svc_list_kb())


# ---------- мастера ----------

def masters_list_kb():
    rows = []
    for mst in db.list_masters(only_active=False):
        mark = "🟢" if mst["active"] else "⚪"
        rows.append([btn(f"{mark} {mst['name']}", f"a:mst:{mst['id']}")])
    rows.append([btn("➕ Добавить мастера", "a:mstadd")])
    rows.append([btn("◀️ Назад", "a:menu")])
    return kb(rows)


@router.callback_query(F.data == "a:msts")
async def cb_masters(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await ui.safe_edit(cb.message, "👤 <b>Мастера</b>", masters_list_kb())
    await cb.answer()


def master_detail(mid):
    mst = db.get_master(mid)
    if not mst:
        return "Мастер не найден", kb([[btn("◀️ Назад", "a:msts")]])
    text = (
        f"<b>{esc(mst['name'])}</b>\n"
        f"Telegram ID: {mst['tg_id'] or '— (уведомления идут админам)'}\n"
        f"Статус: {'🟢 работает' if mst['active'] else '⚪ скрыт'}"
    )
    rows = [
        [btn("✏️ Имя", f"a:mste:name:{mid}"),
         btn("🔗 Telegram ID", f"a:mste:tg:{mid}")],
        [btn("⚪ Скрыть" if mst["active"] else "🟢 Включить", f"a:mstt:{mid}")],
        [btn("◀️ Назад", "a:msts")],
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
        await cb.message.answer("Новое имя мастера:")
    else:
        await cb.message.answer(
            "Telegram ID мастера (число) — на него пойдут уведомления о записях.\n"
            "Мастер может узнать свой ID у @userinfobot. Отправьте «-», чтобы убрать."
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
            await m.answer("Нужно число или «-»:")
            return
    await state.clear()
    text, markup = master_detail(data["mid"])
    await m.answer(text, reply_markup=markup)


@router.callback_query(F.data == "a:mstadd")
async def cb_master_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.m_name)
    await cb.message.answer("Имя мастера?")
    await cb.answer()


@router.message(Adm.m_name, F.text)
async def got_master_name(m: Message, state: FSMContext):
    await state.update_data(m_name=m.text.strip())
    await state.set_state(Adm.m_tg)
    await m.answer(
        "Telegram ID мастера (число) для уведомлений, или «-» чтобы пропустить.\n"
        "Узнать ID: @userinfobot"
    )


@router.message(Adm.m_tg, F.text)
async def got_master_tg(m: Message, state: FSMContext):
    raw = m.text.strip()
    tg_id = None
    if raw != "-":
        if not raw.isdigit():
            await m.answer("Нужно число или «-»:")
            return
        tg_id = int(raw)
    data = await state.get_data()
    db.add_master(data["m_name"], tg_id)
    await state.clear()
    await m.answer(f"✅ Мастер «{esc(data['m_name'])}» добавлен.\n"
                   f"Не забудьте настроить график в разделе «📅 График».",
                   reply_markup=masters_list_kb())


# ---------- график ----------

@router.callback_query(F.data == "a:sch")
async def cb_schedule(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    owner = db.owner_master()
    if not owner:
        await ui.safe_edit(cb.message, "Мастер не найден — перезапустите бота.",
                           kb([[btn("◀️ Назад", "a:menu")]]))
        await cb.answer()
        return
    await show_week(cb.message, owner["id"])
    await cb.answer()


def week_text(mid):
    mst = db.get_master(mid)
    lines = [f"📅 <b>График — {esc(mst['name'])}</b>\n"]
    for wd in range(7):
        t = db.get_template(mid, wd)
        if not t or not t["work_start"]:
            lines.append(f"{WD[wd]}: выходной")
        else:
            s = f"{WD[wd]}: {t['work_start']}–{t['work_end']}"
            if t["break_start"]:
                s += f", обед {t['break_start']}–{t['break_end']}"
            lines.append(s)
    lines.append("\nНажмите день, чтобы изменить.")
    return "\n".join(lines)


def week_kb(mid):
    day_btns = [btn(WD[wd], f"a:tw:{mid}:{wd}") for wd in range(7)]
    rows = [day_btns[:4], day_btns[4:]]
    rows.append([btn("🗓 Исключения (даты)", f"a:ovrl:{mid}")])
    rows.append([btn("◀️ Назад", "a:menu")])
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
        f"График на <b>{WD_FULL[int(wd)]}</b>.\nОтправьте в одном из форматов:\n\n"
        f"<code>10:00-19:00 обед 14:00-15:00</code>\n"
        f"<code>10:00-19:00</code> (без обеда)\n"
        f"<code>выходной</code>"
    )
    await cb.answer()


@router.message(Adm.sched, F.text)
async def got_schedule(m: Message, state: FSMContext):
    parsed = parse_schedule_text(m.text)
    if parsed is None:
        await m.answer("Не понял формат 🤔 Пример: <code>10:00-19:00 обед 14:00-15:00</code>")
        return
    data = await state.get_data()
    db.set_template(data["sched_master"], data["sched_wd"],
                    parsed["ws"], parsed["we"], parsed["bs"], parsed["be"])
    await state.clear()
    mid = data["sched_master"]
    await m.answer("✅ Сохранено.\n\n" + week_text(mid), reply_markup=week_kb(mid))


def overrides_view(mid):
    today = datetime.now().strftime("%Y-%m-%d")
    ovrs = db.list_overrides(mid, today)
    lines = ["🗓 <b>Исключения по датам</b>",
             "Отдельный график или выходной на конкретную дату.\n"]
    rows = []
    for o in ovrs:
        if o["day_off"]:
            desc = "выходной"
        else:
            desc = f"{o['work_start']}–{o['work_end']}"
            if o["break_start"]:
                desc += f", обед {o['break_start']}–{o['break_end']}"
        lines.append(f"• {fmt_d(o['date'])}: {desc}")
        rows.append([btn(f"🗑 Убрать {fmt_d(o['date'])}", f"a:ovrdel:{mid}:{o['date']}")])
    if not ovrs:
        lines.append("Пока нет исключений.")
    rows.append([btn("➕ Добавить дату", f"a:ovradd:{mid}")])
    rows.append([btn("◀️ Назад", f"a:schm:{mid}")])
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
    await cb.answer("Удалено")


@router.callback_query(F.data.startswith("a:ovradd:"))
async def cb_override_add(cb: CallbackQuery, state: FSMContext):
    mid = int(cb.data.split(":")[2])
    await state.set_state(Adm.ovr_date)
    await state.update_data(ovr_master=mid)
    await cb.message.answer("Дата? (например <code>25.07</code> или <code>25.07.2026</code>)")
    await cb.answer()


@router.message(Adm.ovr_date, F.text)
async def got_override_date(m: Message, state: FSMContext):
    ds = parse_date(m.text)
    if not ds:
        await m.answer("Не понял дату. Формат: <code>25.07</code>")
        return
    await state.update_data(ovr_date=ds)
    await state.set_state(Adm.ovr_val)
    await m.answer(
        f"График на {fmt_d(ds)}:\n"
        f"<code>10:00-19:00 обед 14:00-15:00</code>, <code>10:00-19:00</code> "
        f"или <code>выходной</code>"
    )


@router.message(Adm.ovr_val, F.text)
async def got_override_val(m: Message, state: FSMContext):
    parsed = parse_schedule_text(m.text)
    if parsed is None:
        await m.answer("Не понял формат 🤔 Пример: <code>10:00-19:00</code> или <code>выходной</code>")
        return
    data = await state.get_data()
    db.set_override(data["ovr_master"], data["ovr_date"], parsed["day_off"],
                    parsed["ws"], parsed["we"], parsed["bs"], parsed["be"])
    await state.clear()
    await m.answer(f"✅ Исключение на {fmt_d(data['ovr_date'])} сохранено.",
                   reply_markup=kb([[btn("◀️ К графику", f"a:schm:{data['ovr_master']}")]]))


# ---------- группы ----------

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
    rows.append([btn("➕ Создать занятие", "a:gsadd")])
    rows.append([btn("◀️ Назад", "a:menu")])
    text = "👥 <b>Групповые занятия</b>" + ("" if sessions else "\nПока нет запланированных.")
    await ui.safe_edit(cb.message, text, kb(rows))
    await cb.answer()


@router.callback_query(F.data.startswith("a:gs:"))
async def cb_group_detail(cb: CallbackQuery):
    gid = int(cb.data.split(":")[2])
    g = db.get_group_session(gid)
    if not g:
        await cb.answer("Занятие не найдено", show_alert=True)
        return
    lines = [
        f"👥 <b>{esc(g['service_title'])}</b>",
        f"📆 {fmt_dt(g['starts_at'])} · {esc(g['master_name'])}",
        f"Мест: {g['booked']}/{g['capacity']}\n",
    ]
    parts = db.appointments_of_session(gid)
    if parts:
        lines.append("<b>Участники:</b>")
        for a in parts:
            lines.append(f"• {esc(a['client_name'] or '—')} {esc(a['client_phone'] or '')}")
    else:
        lines.append("Пока никто не записан.")
    await ui.safe_edit(cb.message, "\n".join(lines), kb([
        [btn("❌ Отменить занятие", f"a:gsccl:{gid}")],
        [btn("◀️ Назад", "a:grp")],
    ]))
    await cb.answer()


@router.callback_query(F.data.startswith("a:gsccl:"))
async def cb_group_cancel_ask(cb: CallbackQuery):
    gid = int(cb.data.split(":")[2])
    await ui.safe_edit(cb.message, "Отменить занятие? Все участники получат уведомление.",
                       kb([[btn("❌ Да, отменить", f"a:gsccl2:{gid}")],
                           [btn("◀️ Назад", f"a:gs:{gid}")]]))
    await cb.answer()


@router.callback_query(F.data.startswith("a:gsccl2:"))
async def cb_group_cancel_do(cb: CallbackQuery):
    gid = int(cb.data.split(":")[2])
    g = db.get_group_session(gid)
    if not g or g["status"] != "active":
        await cb.answer("Уже неактуально", show_alert=True)
        return
    db.set_group_session_status(gid, "cancelled")
    for a in db.appointments_of_session(gid):
        db.set_appointment_status(a["id"], "cancelled")
        db.drop_pending_notifications(a["id"])
        await notify.client_cancelled_by_admin(cb.bot, a)
    await ui.safe_edit(cb.message, "Занятие отменено, участники уведомлены.",
                       kb([[btn("◀️ К группам", "a:grp")]]))
    await cb.answer()


@router.callback_query(F.data == "a:gsadd")
async def cb_group_add(cb: CallbackQuery, state: FSMContext):
    group_services = [s for s in db.list_services() if s["is_group"]]
    if not group_services:
        await ui.safe_edit(cb.message,
                           "Сначала создайте групповую услугу в разделе «🧾 Услуги».",
                           kb([[btn("◀️ Назад", "a:grp")]]))
        await cb.answer()
        return
    rows = [[btn(s["title"], f"a:gssvc:{s['id']}")] for s in group_services]
    rows.append([btn("◀️ Назад", "a:grp")])
    await ui.safe_edit(cb.message, "Какая услуга?", kb(rows))
    await cb.answer()


@router.callback_query(F.data.startswith("a:gssvc:"))
async def cb_group_svc(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[2])
    await state.update_data(gs_service=sid)
    masters = db.list_masters()
    if len(masters) == 1:
        await state.update_data(gs_master=masters[0]["id"])
        await state.set_state(Adm.gs_dt)
        await cb.message.answer("Дата и время занятия? (например <code>25.07 18:00</code>)")
    else:
        rows = [[btn(mst["name"], f"a:gsmst:{mst['id']}")] for mst in masters]
        rows.append([btn("◀️ Назад", "a:gsadd")])
        await ui.safe_edit(cb.message, "Кто ведёт?", kb(rows))
    await cb.answer()


@router.callback_query(F.data.startswith("a:gsmst:"))
async def cb_group_master(cb: CallbackQuery, state: FSMContext):
    await state.update_data(gs_master=int(cb.data.split(":")[2]))
    await state.set_state(Adm.gs_dt)
    await cb.message.answer("Дата и время занятия? (например <code>25.07 18:00</code>)")
    await cb.answer()


@router.message(Adm.gs_dt, F.text)
async def got_group_dt(m: Message, state: FSMContext):
    dt = parse_datetime(m.text)
    if not dt:
        await m.answer("Не понял. Формат: <code>25.07 18:00</code>")
        return
    if dt <= db.now_str():
        await m.answer("Это время уже в прошлом 🙂 Введите будущую дату:")
        return
    await state.update_data(gs_dt=dt)
    await state.set_state(Adm.gs_cap)
    data = await state.get_data()
    svc = db.get_service(data["gs_service"])
    await m.answer(f"Сколько мест? (отправьте «-» для значения из услуги: {svc['capacity']})")


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
        await m.answer("Нужно число мест или «-»:")
        return
    db.create_group_session(data["gs_service"], data["gs_master"], data["gs_dt"], cap)
    await state.clear()
    await m.answer(
        f"✅ Занятие создано: {esc(svc['title'])}, {fmt_dt(data['gs_dt'])}, мест: {cap}.\n"
        f"Клиенты увидят его при выборе этой услуги.",
        reply_markup=kb([[btn("◀️ К группам", "a:grp")]]),
    )


# ---------- настройки ----------

SETTINGS_META = {
    "buffer_min": "Перерыв между сеансами (мин)",
    "slot_step": "Шаг сетки времени (мин)",
    "min_lead_min": "Минимум минут до записи",
    "horizon_days": "Запись открыта на дней вперёд",
    "ref_deadline_hour": "Референс принимать до (час дня записи)",
    "ref_max": "Макс. фото/видео в референсе",
    "digest_hour": "Сводка на день в (час)",
    "ref_remind_hour": "Напомнить про референс накануне в (час)",
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
    lines = ["⚙️ <b>Настройки</b>\n"]
    rows = []
    if owner:
        lines.append(f"Ваше имя (видят клиенты): <b>{esc(owner['name'])}</b>")
        rows.append([btn("✏️ Ваше имя (для клиентов)", "a:rename")])
    for key, label in SETTINGS_META.items():
        lines.append(f"{label}: <b>{db.get_setting(key)}</b>")
        rows.append([btn(f"✏️ {label}", f"a:sete:{key}")])
    rows.append([btn("◀️ Назад", "a:menu")])
    return "\n".join(lines), kb(rows)


@router.callback_query(F.data == "a:rename")
async def cb_owner_rename(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.owner_name)
    await cb.message.answer("Как вас представлять клиентам? (имя видно в подтверждениях записи)")
    await cb.answer()


@router.message(Adm.owner_name, F.text)
async def got_owner_name(m: Message, state: FSMContext):
    name = m.text.strip()
    if not name or name.startswith("/") or len(name) > 60:
        await m.answer("Напишите имя обычным текстом:")
        return
    owner = db.owner_master()
    if owner:
        db.update_master_field(owner["id"], "name", name)
    await state.clear()
    text, markup = settings_view()
    await m.answer("✅ Готово.\n\n" + text, reply_markup=markup)


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
    await cb.message.answer(f"{SETTINGS_META[key]} — новое значение (число):")
    await cb.answer()


@router.message(Adm.set_val, F.text)
async def got_setting(m: Message, state: FSMContext):
    if not m.text.strip().isdigit():
        await m.answer("Нужно число:")
        return
    data = await state.get_data()
    key = data["set_key"]
    value = int(m.text.strip())
    lo, hi = SETTINGS_RANGE.get(key, (0, 100000))
    if not lo <= value <= hi:
        await m.answer(f"Значение должно быть от {lo} до {hi}:")
        return
    db.set_setting(key, value)
    await state.clear()
    text, markup = settings_view()
    await m.answer(text, reply_markup=markup)


# ---------- ручная запись (живёт в той же системе) ----------

@router.callback_query(F.data == "a:nb")
async def cb_newbook(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    services = db.list_services()
    if not services:
        await ui.safe_edit(cb.message, "Сначала добавьте услуги.",
                           kb([[btn("◀️ Назад", "a:menu")]]))
        await cb.answer()
        return
    rows = []
    for s in services:
        g = "👥 " if s["is_group"] else ""
        rows.append([btn(f"{g}{s['title']} · {fmt_dur(s['duration_min'])}", f"a:nbsvc:{s['id']}")])
    rows.append([btn("◀️ Назад", "a:menu")])
    await ui.safe_edit(cb.message, "➕ <b>Новая запись</b>\nУслуга:", kb(rows))
    await cb.answer()


async def nb_choose_master(message, state: FSMContext):
    masters = db.list_masters()
    if len(masters) == 1:
        await state.update_data(nb_master=masters[0]["id"])
        await show_nb_dates(message, state)
        return
    rows = [[btn(mst["name"], f"a:nbmst:{mst['id']}")] for mst in masters]
    rows.append([btn("◀️ Назад", "a:nb")])
    await ui.safe_edit(message, "Мастер:", kb(rows))


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
                               "Нет запланированных занятий. Создайте его в «👥 Группы».",
                               kb([[btn("◀️ Назад", "a:nb")]]))
            await cb.answer()
            return
        rows.append([btn("◀️ Назад", "a:nb")])
        await ui.safe_edit(cb.message, "Какое занятие?", kb(rows))
        await cb.answer()
        return

    if svc["ext_price_per_nail"] or svc["ext_min_per_nail"]:
        nums = [btn(str(n), f"a:nbext:{n}") for n in range(1, 11)]
        rows = [[btn("Без наращивания", "a:nbext:0")], nums[:5], nums[5:],
                [btn("◀️ Назад", "a:nb")]]
        await ui.safe_edit(
            cb.message,
            f"Наращивание? Один ноготь: +{svc['ext_price_per_nail']} €, "
            f"+{svc['ext_min_per_nail']} мин.",
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
        await cb.answer("Начните заново: /admin", show_alert=True)
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
        await ui.safe_edit(message, "Нет рабочих дней в горизонте. Настройте график.",
                           kb([[btn("◀️ Назад", "a:nb")]]))
        return
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([btn("◀️ Назад", "a:nb")])
    await ui.safe_edit(message, "День:", kb(rows))


@router.callback_query(F.data.startswith("a:nbday:"))
async def cb_nb_day(cb: CallbackQuery, state: FSMContext):
    ds = cb.data.split(":", 2)[2]
    data = await state.get_data()
    if not data.get("nb_service") or not data.get("nb_master"):
        await cb.answer("Начните заново: /admin", show_alert=True)
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
    rows.append([btn("◀️ Назад", "a:nbback")])
    text = f"Время на {fmt_d(ds)} — выберите слот или пришлите своё (например <code>16:45</code>):"
    if not free:
        text = (f"Свободных слотов на {fmt_d(ds)} нет, но вы можете прислать время "
                f"вручную (например <code>16:45</code>):")
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
        await m.answer("Формат времени: <code>16:45</code>")
        return
    await state.update_data(nb_time=t)
    await ask_nb_client(m, state)


@router.callback_query(F.data.startswith("a:nbgs:"))
async def cb_nb_group(cb: CallbackQuery, state: FSMContext):
    gid = int(cb.data.split(":")[2])
    g = db.get_group_session(gid)
    if not g or g["status"] != "active":
        await cb.answer("Занятие недоступно", show_alert=True)
        return
    if g["booked"] >= g["capacity"]:
        await cb.answer("Мест нет", show_alert=True)
        return
    await state.update_data(nb_gs=gid, nb_service=g["service_id"], nb_master=g["master_id"])
    await ask_nb_client(cb.message, state)
    await cb.answer()


async def ask_nb_client(message, state: FSMContext):
    await state.set_state(Adm.nb_client)
    await message.answer(
        "Кого записываем?\nПришлите имя или телефон для поиска, "
        "либо слово <code>новый</code> для нового клиента."
    )


@router.message(Adm.nb_client, F.text)
async def got_nb_client_query(m: Message, state: FSMContext):
    q = m.text.strip()
    if q.lower() in ("новый", "new"):
        await state.set_state(Adm.nb_name)
        await m.answer("Имя клиента?")
        return
    found = db.search_clients(q)
    rows = []
    for c in found:
        label = f"{c['name'] or 'Без имени'} {c['phone'] or ''}"
        if c["username"]:
            label += f" @{c['username']}"
        rows.append([btn(label[:60], f"a:nbcl:{c['id']}")])
    rows.append([btn("➕ Новый клиент", "a:nbnew")])
    text = "Кто из них?" if found else "Никого не нашёл 🤔"
    await m.answer(text, reply_markup=kb(rows))


@router.callback_query(F.data == "a:nbnew")
async def cb_nb_new_client(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.nb_name)
    await cb.message.answer("Имя клиента?")
    await cb.answer()


@router.message(Adm.nb_name, F.text)
async def got_nb_name(m: Message, state: FSMContext):
    await state.update_data(nb_new_name=m.text.strip())
    await state.set_state(Adm.nb_phone)
    await m.answer("Телефон клиента? (или «-», если неизвестен)")


@router.message(Adm.nb_phone, F.text)
async def got_nb_phone(m: Message, state: FSMContext):
    raw = m.text.strip()
    phone = "" if raw == "-" else db.norm_phone(raw)
    if raw != "-" and len(phone) < 9:
        await m.answer("Не похоже на номер. Введите ещё раз или «-»:")
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
        await message.answer("Что-то потерялось, начните заново: /admin")
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
            await message.answer("Что-то потерялось, начните заново: /admin")
            return
        when = f"{fmt_d(data['nb_date'])} в {data['nb_time']}"
        master_name = db.get_master(data["nb_master"])["name"]
        duration = db.effective_duration(svc, nails)
        extra = db.ext_extra_price(svc, nails)
        if sl.conflict_exists(data["nb_master"], data["nb_date"], data["nb_time"],
                              duration):
            warn = "\n\n⚠️ Внимание: время пересекается с другой записью или буфером!"

    ext_line = f"➕ Наращивание: {nails} ног. (+{extra} €)\n" if nails else ""
    price_line = ui.price_with_ext(svc["price"], extra)
    text = (
        f"<b>Создать запись?</b>\n\n"
        f"💅 {esc(svc['title'])} ({fmt_dur(duration)})\n{ext_line}"
        f"👤 Мастер: {esc(master_name)}\n📆 {when}\n"
        + (f"💰 {esc(price_line)}\n" if price_line else "")
        + f"Клиент: {esc(client['name'] or '—')} {esc(client['phone'] or '')}"
        f"{warn}"
    )
    await message.answer(text, reply_markup=kb([
        [btn("✅ Создать", "a:nbok")],
        [btn("❌ Отмена", "a:menu")],
    ]))


@router.callback_query(F.data == "a:nbok")
async def cb_nb_create(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    client = db.get_client(data.get("nb_client_id") or 0)
    svc = db.get_service(data.get("nb_service") or 0)
    if not client or not svc:
        await cb.answer("Данные устарели, начните заново", show_alert=True)
        return

    nails = data.get("nb_ext") or 0
    if data.get("nb_gs"):
        g = db.get_group_session(data["nb_gs"])
        if not g or g["status"] != "active" or g["booked"] >= g["capacity"]:
            await cb.answer("Занятие недоступно или мест нет", show_alert=True)
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
    # клиент получает уведомление, даже если запись создал админ
    await notify.client_admin_created(cb.bot, appt)
    await notify.master_new_appointment(cb.bot, appt, actor_tg=cb.from_user.id)
    await state.clear()
    note = "" if appt["client_tg"] else "\n(у клиента нет Telegram — уведомление не отправлено)"
    await ui.safe_edit(cb.message,
                       f"✅ Запись создана: {fmt_dt(starts)}, {esc(svc['title'])}.{note}",
                       kb([[btn("➕ Ещё запись", "a:nb")], [btn("◀️ Панель", "a:menu")]]))
    await cb.answer()
