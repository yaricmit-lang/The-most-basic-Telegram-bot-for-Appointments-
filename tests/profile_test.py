"""Личный кабинет клиента: просмотр и изменение своих данных."""
import asyncio
import os
import sys

DB = "/private/tmp/claude-501/-Users-admin-Claude/f8db589b-7a41-413d-a71d-7e652b630980/scratchpad/prof2.db"
os.environ["DB_PATH"] = DB
if os.path.exists(DB):
    os.remove(DB)
sys.path.insert(0, "/Users/admin/Claude/appointment-bot")

import db
import config
config.ADMIN_IDS = frozenset()  # isolate tests from the developer's real .env

db.init_db()
mid = db.add_master("Test Master", 111111111)
sid = db.add_service("Маникюр", 120, "40 евро", False, 1, 25)

from handlers import client as ch
import ui


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

    # --- кабинет показывает данные и доступен из меню
    text, markup = ch.profile_view(client)
    labels = [b.text for row in markup.inline_keyboard for b in row]
    print("кабинет:")
    for line in text.splitlines():
        if line.strip():
            print("   ", line)
    print("  кнопки:", labels)
    assert "Test Client" in text and "+79995550001" in text and "@test_owner" in text
    assert any("name" in x.lower() for x in labels) and any("phone" in x.lower() for x in labels)

    menu = [b.callback_data for row in ui.main_menu_kb().inline_keyboard for b in row]
    assert "prof" in menu, "кабинет должен быть доступен из главного меню"
    print("  доступен из меню ✅")

    # --- смена имени
    st = FakeState()
    m = FakeMsg("Test Client Updated")
    await ch.got_profile_name(m, st)
    assert db.get_client_by_tg(999)["name"] == "Test Client Updated"
    assert st.state is None, "состояние должно сброситься"
    print("  имя изменено ->", db.get_client_by_tg(999)["name"], "✅")

    # мусор именем не станет
    st = FakeState()
    await ch.got_profile_name(FakeMsg("x" * 200), st)
    assert db.get_client_by_tg(999)["name"] == "Test Client Updated", "слишком длинное имя не сохраняем"
    print("  слишком длинное имя отклонено ✅")

    # --- смена телефона: нормализация 8 -> +7
    st = FakeState()
    await ch.got_profile_phone(FakeMsg("8 999 555 00 02"), st)
    assert db.get_client_by_tg(999)["phone"] == "+79995550002"
    print("  телефон '8 900...' ->", db.get_client_by_tg(999)["phone"], "✅")

    # мусор телефоном не станет
    st = FakeState()
    await ch.got_profile_phone(FakeMsg("абвгд"), st)
    assert db.get_client_by_tg(999)["phone"] == "+79995550002", "мусор не должен сохраниться"
    print("  мусор в телефоне отклонён ✅")

    # --- КЛЮЧЕВОЕ: новые данные видны мастеру в существующей записи
    aid = db.create_appointment(db.get_client_by_tg(999)["id"], sid, mid,
                                "2026-08-20 11:00", "2026-08-20 13:00")
    a = db.get_appointment(aid)
    assert a["client_name"] == "Test Client Updated" and a["client_phone"] == "+79995550002"
    print("  мастер видит обновлённые данные в записи ✅")

    # --- смена телефона подтягивает историю офлайн-клиента с тем же номером
    off = db.create_client("Offline Client 2", "+79995556677")
    db.create_appointment(off, sid, mid, "2026-08-21 10:00", "2026-08-21 12:00",
                          created_by="admin")
    st = FakeState()
    await ch.got_profile_phone(FakeMsg("+7 999 555 66 77"), st)
    assert db.get_client(off) is None, "офлайн-дубль должен слиться"
    assert len(db.upcoming_for_client(db.get_client_by_tg(999)["id"])) == 2, \
        "запись, заведённая мастером вручную, должна переехать к клиенту"
    print("  история офлайн-клиента подтянулась по номеру ✅")

    print()
    print("ВСЕ ПРОВЕРКИ КАБИНЕТА ПРОШЛИ ✅")


asyncio.run(main())
