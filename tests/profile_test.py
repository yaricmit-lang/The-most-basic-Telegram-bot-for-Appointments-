"""Client profile: viewing and editing your own data."""
import asyncio
import os
import tempfile
import sys
from datetime import datetime, timedelta

DB = os.path.join(tempfile.gettempdir(), "prof2.db")
os.environ["DB_PATH"] = DB
if os.path.exists(DB):
    os.remove(DB)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import config
config.ADMIN_IDS = frozenset()  # isolate tests from the developer's real .env

db.init_db()
mid = db.add_master("Test Master", 111111111)
sid = db.add_service("Manicure", 120, "€40", False, 1, 25)

from handlers import client as ch
import ui

tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
day_after = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")


class FakeState:
    def __init__(self):
        self.state, self.data = None, {}

    async def set_state(self, s):
        self.state = s

    async def get_state(self):
        return self.state

    async def clear(self):
        self.state, self.data = None, {}

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return self.data


class FakeMsg:
    def __init__(self, text="", user_id=999, username="test_owner", contact=None):
        self.text, self.contact = text, contact
        self.from_user = type("U", (), {"id": user_id, "username": username})()
        self.replies = []

    async def answer(self, text, reply_markup=None):
        self.replies.append(text)

    async def edit_text(self, text, reply_markup=None):
        self.replies.append(text)


async def main():
    client = db.upsert_client(999, "test_owner")
    db.set_client_name(client["id"], "Test Client")
    db.set_client_phone(client["id"], "+79995550001")
    client = db.get_client_by_tg(999)

    # --- the profile shows the data and is reachable from the menu
    text, markup = ch.profile_view(client)
    labels = [b.text for row in markup.inline_keyboard for b in row]
    print("profile:")
    for line in text.splitlines():
        if line.strip():
            print("   ", line)
    print("  buttons:", labels)
    assert "Test Client" in text and "+79995550001" in text and "@test_owner" in text
    assert any("name" in x.lower() for x in labels) and any("phone" in x.lower() for x in labels)

    menu = [b.callback_data for row in ui.main_menu_kb().inline_keyboard for b in row]
    assert "prof" in menu, "the profile should be reachable from the main menu"
    print("  reachable from the menu ✅")

    # --- changing the name
    st = FakeState()
    m = FakeMsg("Test Client Updated")
    await ch.got_profile_name(m, st)
    assert db.get_client_by_tg(999)["name"] == "Test Client Updated"
    assert st.state is None, "the state should reset"
    print("  name changed ->", db.get_client_by_tg(999)["name"], "✅")

    # garbage won't become the name
    st = FakeState()
    await ch.got_profile_name(FakeMsg("x" * 200), st)
    assert db.get_client_by_tg(999)["name"] == "Test Client Updated", "an overly long name isn't saved"
    print("  overly long name rejected ✅")

    # --- changing the phone: normalizing a Russian 8-prefix to +7
    st = FakeState()
    await ch.got_profile_phone(FakeMsg("8 999 555 00 02"), st)
    assert db.get_client_by_tg(999)["phone"] == "+79995550002"
    print("  phone '8 999...' ->", db.get_client_by_tg(999)["phone"], "✅")

    # garbage won't become the phone
    st = FakeState()
    await ch.got_profile_phone(FakeMsg("not a number"), st)
    assert db.get_client_by_tg(999)["phone"] == "+79995550002", "garbage shouldn't be saved"
    print("  garbage phone rejected ✅")

    # --- KEY CASE: the new data is visible to the master on an existing booking
    aid = db.create_appointment(db.get_client_by_tg(999)["id"], sid, mid,
                                f"{tomorrow} 11:00", f"{tomorrow} 13:00")
    a = db.get_appointment(aid)
    assert a["client_name"] == "Test Client Updated" and a["client_phone"] == "+79995550002"
    print("  the master sees the updated data on the booking ✅")

    # --- changing the phone pulls in an offline client's history with the same number
    off = db.create_client("Offline Client 2", "+79995556677")
    db.create_appointment(off, sid, mid, f"{day_after} 10:00", f"{day_after} 12:00",
                          created_by="admin")
    st = FakeState()
    await ch.got_profile_phone(FakeMsg("+7 999 555 66 77"), st)
    assert db.get_client(off) is None, "the offline duplicate should be merged"
    assert len(db.upcoming_for_client(db.get_client_by_tg(999)["id"])) == 2, \
        "a booking the master created manually should move onto the client"
    print("  offline client history pulled in by phone number ✅")

    print()
    print("ALL PROFILE CHECKS PASSED ✅")


asyncio.run(main())
