"""Nail extensions: rates, the client question, duration in the schedule, pricing."""
import asyncio
import os
import tempfile
import sqlite3
import sys
from datetime import datetime, timedelta

DB = os.path.join(tempfile.gettempdir(), "ext.db")
os.environ["DB_PATH"] = DB
if os.path.exists(DB):
    os.remove(DB)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import config
config.ADMIN_IDS = frozenset()  # isolate tests from the developer's real .env
import slots as sl
import ui
from i18n import t

db.init_db()
mid = db.add_master("Test Master", 111111111)
for wd in range(7):
    db.set_template(mid, wd, "10:00", "19:00", None, None)

# Titles stay in Russian on purpose: they exercise db._migrate's Cyrillic-aware
# keyword matching ("средн"/"длинн"), tested below.
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
    # === duration/price helpers ===
    m = db.get_service(med)
    assert db.effective_duration(m, 0) == 120
    assert db.effective_duration(m, 3) == 165, "medium: 3 nails = +45 min"
    assert db.ext_extra_price(m, 3) == 15, "medium: 3 nails = +€15"
    l = db.get_service(lng)
    assert db.effective_duration(l, 2) == 190, "long: 2 nails = +40 min"
    assert db.ext_extra_price(l, 2) == 20
    assert ui.price_with_ext("40€", 15) == "55€"
    assert ui.price_with_ext("60 евро", 40) == "100 евро"
    assert ui.price_with_ext("", 15) == f"+15 € ({t('ext_surcharge_note')})"
    assert ui.price_with_ext("negotiable", 15) == f"negotiable +15 € ({t('ext_surcharge_note')})"
    assert ui.price_with_ext("40€", 0) == "40€"
    print("duration/price helpers ✅")

    # === client: the extension question appears for medium/long, not for short ===
    cb = FakeCb(f"svcgo:{med}")
    st = FakeState()
    await ch.cb_service_go(cb, st)
    text, markup = cb.message.replies[-1]
    print("\nquestion to the client:")
    for line in text.splitlines():
        if line.strip():
            print("   ", line)
    assert "extension" in text.lower() and "+5 €" in text and "+15 min" in text
    assert "ext:0" in cbs(markup) and "ext:10" in cbs(markup)

    cb = FakeCb(f"svcgo:{shrt}")
    st = FakeState()
    await ch.cb_service_go(cb, st)
    text, _ = cb.message.replies[-1]
    assert "extension" not in text.lower(), "the short style doesn't need the question"
    assert st.data.get("master_id") == mid, "short: goes straight to dates"
    print("short -> no question, straight to dates ✅")

    # === choosing a nail count leads to dates and is remembered ===
    cb = FakeCb("ext:3")
    st = FakeState(service_id=med)
    await ch.cb_extension(cb, st)
    assert st.data["ext_nails"] == 3
    assert st.data.get("master_id") == mid
    print("picked 3 nails -> to dates ✅")

    # === booking: the session time is stretched by the extension ===
    cb = FakeCb("mkap")
    st = FakeState(service_id=med, master_id=mid, date=tomorrow, time="10:00",
                   ext_nails=3, gs_id=None)
    await ch.cb_make_appointment(cb, st)
    a = db.upcoming_for_client(client["id"])[0]
    print("\nbooking:", a["starts_at"], "->", a["ends_at"],
          f"| nails: {a['ext_nails']}, surcharge: {a['ext_price']} €")
    assert a["ends_at"] == f"{tomorrow} 12:45", "120 + 3*15 = 165 min -> ends at 12:45"
    assert a["ext_nails"] == 3 and a["ext_price"] == 15
    conf_text, _ = cb.message.replies[-1]
    assert "Extensions: 3 nail" in conf_text and "2 h 45 min" in conf_text
    master_msg = cb.bot.msgs[0][1]
    assert "Extensions: 3 nail" in master_msg and "+15 €" in master_msg, \
        "the master should see the extension"
    print("master notified with the extension ✅")

    # === schedule: the buffer follows the STRETCHED end (12:45+15), not 12:00 ===
    free = sl.free_slots(mid, tomorrow, 120)
    print("\nslots after the 10:00-12:45 booking:", free[:6], "...")
    assert "12:00" not in free and "12:30" not in free, \
        "time inside the extension should be busy"
    assert "13:00" in free, "12:45 + 15 min buffer -> free from 13:00"

    # and conversely: a client with extensions won't fit into a gap that would
    # fit them without the extension
    free_ext = sl.free_slots(mid, tomorrow, db.effective_duration(m, 3))
    assert len(free_ext) < len(free), "fewer free starts once extensions are factored in"
    print("the schedule accounts for extensions ✅")

    # === the client card shows the real duration and surcharge ===
    text, _ = ch.client_card(a)
    assert "2 h 45 min" in text and "Extensions: 3 nail" in text and "55€" in text
    print("client card: 2 h 45 min, +€15, total 55€ ✅")

    # === admin: manual booking with an extension ===
    cb = FakeCb(f"a:nbsvc:{lng}", uid=111111111)
    st = FakeState()
    await ah.cb_nb_service(cb, st)
    text, markup = cb.message.replies[-1]
    assert "+10 €" in text and "a:nbext:4" in cbs(markup), "the admin gets asked too"

    cb = FakeCb("a:nbext:4", uid=111111111)
    await ah.cb_nb_ext(cb, st)
    assert st.data["nb_ext"] == 4 and st.data.get("nb_master") == mid

    await st.update_data(nb_date=tomorrow, nb_time="14:00", nb_client_id=client["id"])
    msg = FakeMsg()
    await ah.show_nb_confirm(msg, st)
    text, _ = msg.replies[-1]
    print("\nadmin confirmation:")
    for line in text.splitlines():
        if line.strip():
            print("   ", line)
    assert "Extensions: 4 nail" in text and "+40 €" in text
    assert "100 евро" in text
    assert "3 h 50 min" in text

    cb = FakeCb("a:nbok", uid=111111111)
    await ah.cb_nb_create(cb, st)
    a2 = [x for x in db.upcoming_for_client(client["id"])
          if x["starts_at"] == f"{tomorrow} 14:00"][0]
    assert a2["ends_at"] == f"{tomorrow} 17:50", "230 min from 14:00 -> 17:50"
    assert a2["ext_nails"] == 4 and a2["ext_price"] == 40
    client_note = [txt for chat_id, txt in cb.bot.msgs if chat_id == 999]
    assert client_note and "Extensions: 4 nail" in client_note[0], \
        "the client sees the extension in \"You've been booked\""
    print("manual booking: ends 17:50, client notified ✅")

    # === day boundaries: a 23:59 booking isn't dropped from digests ===
    import notify
    db.create_appointment(client["id"], shrt, mid, f"{tomorrow} 23:59", f"{tomorrow} 23:59")
    b1, b2 = notify._day_bounds(tomorrow)
    late_ones = [x for x in db.appointments_between(b1, b2)
                 if x["starts_at"] == f"{tomorrow} 23:59"]
    assert late_ones, "a 23:59 booking should show up in the day's lists"
    print("day boundaries fixed (23:59 isn't lost) ✅")

    # === migration: rates are backfilled from the titles of existing services ===
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
    print("\nrates after migration:")
    for title, v in rates.items():
        print(f"    {title}: {v[0]} €/nail, {v[1]} min")
    assert rates["Маникюр"] == (0, 0)
    assert rates["Короткий с простым дизайном"] == (0, 0)
    assert rates["Средний со сложным дизайном"] == (5, 15)
    assert rates["Длинный с простым дизайном"] == (10, 20)
    db.init_db()  # running it again doesn't wipe the rates
    print("rate migration by title ✅")

    print()
    print("ALL EXTENSION CHECKS PASSED ✅")


asyncio.run(main())
