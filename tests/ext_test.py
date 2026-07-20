"""Наращивание ногтей: тарифы, вопрос клиенту, длительность в расписании, цена."""
import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timedelta

DB = "/private/tmp/claude-501/-Users-admin-Claude/f8db589b-7a41-413d-a71d-7e652b630980/scratchpad/ext.db"
os.environ["DB_PATH"] = DB
if os.path.exists(DB):
    os.remove(DB)
sys.path.insert(0, "/Users/admin/Claude/appointment-bot")

import db
import config
config.ADMIN_IDS = frozenset()  # isolate tests from the developer's real .env
import slots as sl
import ui

db.init_db()
mid = db.add_master("Test Master", 111111111)
for wd in range(7):
    db.set_template(mid, wd, "10:00", "19:00", None, None)

med = db.add_service("Средний с простым дизайном", 120, "40€", False, 1, 25)
lng = db.add_service("Длинный с простым дизайном", 150, "60 евро", False, 1, 25)
shrt = db.add_service("Короткий с простым дизайном", 90, "30€", False, 1, 25)
db.update_service_field(med, "ext_price_per_nail", 5)
db.update_service_field(med, "ext_min_per_nail", 15)
db.update_service_field(lng, "ext_price_per_nail", 10)
db.update_service_field(lng, "ext_min_per_nail", 20)

from handlers import admin as ah
from handlers import client as ch

client = db.upsert_client(999, "olga")
db.set_client_name(client["id"], "Test Client")
db.set_client_phone(client["id"], "+79995550001")
tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")


class FakeState:
    def __init__(self, **data):
        self.state, self.data = None, dict(data)

    async def set_state(self, s):
        self.state = s

    async def clear(self):
        self.state, self.data = None, {}

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return self.data


class FakeBot:
    def __init__(self):
        self.msgs = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.msgs.append((chat_id, text))


class FakeMsg:
    def __init__(self):
        self.replies = []

    async def answer(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))

    async def edit_text(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))

    async def answer_photo(self, photo, caption=None, reply_markup=None):
        self.replies.append((caption, reply_markup))


class FakeCb:
    def __init__(self, data, uid=999):
        self.data, self.bot = data, FakeBot()
        self.message = FakeMsg()
        self.from_user = type("U", (), {"id": uid, "username": "olga"})()
        self.answers = []

    async def answer(self, text="", show_alert=False):
        self.answers.append(text)


def labels(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def cbs(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def main():
    # === хелперы длительности и цены ===
    m = db.get_service(med)
    assert db.effective_duration(m, 0) == 120
    assert db.effective_duration(m, 3) == 165, "средний: 3 ногтя = +45 мин"
    assert db.ext_extra_price(m, 3) == 15, "средний: 3 ногтя = +15 €"
    l = db.get_service(lng)
    assert db.effective_duration(l, 2) == 190, "длинный: 2 ногтя = +40 мин"
    assert db.ext_extra_price(l, 2) == 20
    assert ui.price_with_ext("40€", 15) == "55€"
    assert ui.price_with_ext("60 евро", 40) == "100 евро"
    assert ui.price_with_ext("", 15) == "+15 € (extension)"
    assert ui.price_with_ext("договорная", 15) == "договорная +15 € (extension)"
    assert ui.price_with_ext("40€", 0) == "40€"
    print("хелперы длительности и цены ✅")

    # === клиент: вопрос о наращивании для среднего/длинного, но не для короткого ===
    cb = FakeCb(f"svcgo:{med}")
    st = FakeState()
    await ch.cb_service_go(cb, st)
    text, markup = cb.message.replies[-1]
    print("\nвопрос клиенту:")
    for line in text.splitlines():
        if line.strip():
            print("   ", line)
    assert "extension" in text.lower() and "+5 €" in text and "+15 min" in text
    assert "ext:0" in cbs(markup) and "ext:10" in cbs(markup)

    cb = FakeCb(f"svcgo:{shrt}")
    st = FakeState()
    await ch.cb_service_go(cb, st)
    text, _ = cb.message.replies[-1]
    assert "extension" not in text.lower(), "для короткого вопрос не нужен"
    assert st.data.get("master_id") == mid, "короткий: сразу к датам"
    print("короткий -> без вопроса, сразу к датам ✅")

    # === выбор количества ногтей ведёт к датам и запоминается ===
    cb = FakeCb("ext:3")
    st = FakeState(service_id=med)
    await ch.cb_extension(cb, st)
    assert st.data["ext_nails"] == 3
    assert st.data.get("master_id") == mid
    print("выбрано 3 ногтя -> к датам ✅")

    # === бронирование: время сеанса растянуто наращиванием ===
    cb = FakeCb("mkap")
    st = FakeState(service_id=med, master_id=mid, date=tomorrow, time="10:00",
                   ext_nails=3, gs_id=None)
    await ch.cb_make_appointment(cb, st)
    a = db.upcoming_for_client(client["id"])[0]
    print("\nзапись:", a["starts_at"], "->", a["ends_at"],
          f"| ногтей: {a['ext_nails']}, доплата: {a['ext_price']} €")
    assert a["ends_at"] == f"{tomorrow} 12:45", "120 + 3*15 = 165 мин -> конец 12:45"
    assert a["ext_nails"] == 3 and a["ext_price"] == 15
    conf_text, _ = cb.message.replies[-1]
    assert "Extension: 3 nail(s)" in conf_text and "2h 45m" in conf_text
    master_msg = cb.bot.msgs[0][1]
    assert "Extension: 3 nail(s)" in master_msg and "+15 €" in master_msg, \
        "мастер должен видеть наращивание"
    print("мастер уведомлён с наращиванием ✅")

    # === расписание: буфер идёт после РАСТЯНУТОГО конца (12:45+15), а не после 12:00 ===
    free = sl.free_slots(mid, tomorrow, 120)
    print("\nслоты после записи 10:00–12:45:", free[:6], "...")
    assert "12:00" not in free and "12:30" not in free, \
        "время внутри наращивания должно быть занято"
    assert "13:00" in free, "12:45 + буфер 15 мин -> свободно с 13:00"

    # и наоборот: клиент с наращиванием не влезет в дыру, куда влез бы без него
    free_ext = sl.free_slots(mid, tomorrow, db.effective_duration(m, 3))
    assert len(free_ext) < len(free), "с наращиванием свободных стартов меньше"
    print("расписание учитывает наращивание ✅")

    # === карточка клиента показывает реальную длительность и доплату ===
    text, _ = ch.client_card(a)
    assert "2h 45m" in text and "Extension: 3 nail(s)" in text and "55€" in text
    print("карточка клиента: 2 ч 45 мин, +15 €, итог 55€ ✅")

    # === админ: ручная запись с наращиванием ===
    cb = FakeCb(f"a:nbsvc:{lng}", uid=111111111)
    st = FakeState()
    await ah.cb_nb_service(cb, st)
    text, markup = cb.message.replies[-1]
    assert "+10 €" in text and "a:nbext:4" in cbs(markup), "админа тоже спрашиваем"

    cb = FakeCb("a:nbext:4", uid=111111111)
    await ah.cb_nb_ext(cb, st)
    assert st.data["nb_ext"] == 4 and st.data.get("nb_master") == mid

    await st.update_data(nb_date=tomorrow, nb_time="14:00", nb_client_id=client["id"])
    msg = FakeMsg()
    await ah.show_nb_confirm(msg, st)
    text, _ = msg.replies[-1]
    print("\nподтверждение админа:")
    for line in text.splitlines():
        if line.strip():
            print("   ", line)
    assert "Extension: 4 nail(s) (+40 €)" in text
    assert "100 евро" in text
    assert "3h 50m" in text

    cb = FakeCb("a:nbok", uid=111111111)
    await ah.cb_nb_create(cb, st)
    a2 = [x for x in db.upcoming_for_client(client["id"])
          if x["starts_at"] == f"{tomorrow} 14:00"][0]
    assert a2["ends_at"] == f"{tomorrow} 17:50", "230 мин от 14:00 -> 17:50"
    assert a2["ext_nails"] == 4 and a2["ext_price"] == 40
    client_note = [t for cid, t in cb.bot.msgs if cid == 999]
    assert client_note and "Extension: 4 nail(s)" in client_note[0], \
        "клиент видит наращивание в «Вас записали»"
    print("ручная запись: конец 17:50, клиент уведомлён ✅")

    # === границы дня: запись в 23:59 не выпадает из сводок ===
    import notify
    db.create_appointment(client["id"], shrt, mid, f"{tomorrow} 23:59", f"{tomorrow} 23:59")
    b1, b2 = notify._day_bounds(tomorrow)
    late_ones = [x for x in db.appointments_between(b1, b2)
                 if x["starts_at"] == f"{tomorrow} 23:59"]
    assert late_ones, "запись в 23:59 должна попадать в дневные списки"
    print("границы дня исправлены (23:59 не теряется) ✅")

    # === миграция: тарифы подставляются по названиям существующих услуг ===
    OLD = DB + ".old"
    if os.path.exists(OLD):
        os.remove(OLD)
    c = sqlite3.connect(OLD)
    c.executescript("""
    CREATE TABLE services(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
        duration_min INTEGER NOT NULL, price TEXT DEFAULT '', is_group INTEGER DEFAULT 0,
        capacity INTEGER DEFAULT 1, comeback_days INTEGER DEFAULT 21,
        photo_id TEXT DEFAULT '', active INTEGER DEFAULT 1);
    INSERT INTO services(title,duration_min) VALUES
        ('Маникюр',120),('Короткий с простым дизайном',90),
        ('Средний со сложным дизайном',120),('Длинный с простым дизайном',150);
    """)
    c.commit()
    c.close()

    import importlib

    import config
    os.environ["DB_PATH"] = OLD
    importlib.reload(config)
    db._conn = None
    db.init_db()
    rates = {r["title"]: (r["ext_price_per_nail"], r["ext_min_per_nail"])
             for r in db.list_services()}
    print("\nтарифы после миграции:")
    for t, v in rates.items():
        print(f"    {t}: {v[0]} €/ноготь, {v[1]} мин")
    assert rates["Маникюр"] == (0, 0)
    assert rates["Короткий с простым дизайном"] == (0, 0)
    assert rates["Средний со сложным дизайном"] == (5, 15)
    assert rates["Длинный с простым дизайном"] == (10, 20)
    db.init_db()  # повторный запуск не перетирает
    print("миграция тарифов по названиям ✅")

    print()
    print("ВСЕ ПРОВЕРКИ НАРАЩИВАНИЯ ПРОШЛИ ✅")


asyncio.run(main())
