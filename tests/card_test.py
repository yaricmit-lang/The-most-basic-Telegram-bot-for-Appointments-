"""Reference cards: the exit-to-panel button, and the no-double-confirmation guard."""
import asyncio
import os
import tempfile
import sys

DB = os.path.join(tempfile.gettempdir(), "card.db")
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
cid = db.create_client("Test Client", "+79995550001", tg_id=999)

from handlers import admin as ah
from i18n import t


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text))


class FakeMsg:
    def __init__(self):
        self.text = ""
        self.markup = None

    async def edit_text(self, text, reply_markup=None):
        self.text, self.markup = text, reply_markup

    async def answer(self, text, reply_markup=None):
        self.text, self.markup = text, reply_markup


class FakeUser:
    id = 111111111


class FakeCb:
    def __init__(self, data, bot):
        self.data, self.bot = data, bot
        self.message = FakeMsg()
        self.from_user = FakeUser()
        self.answers = []

    async def answer(self, text="", show_alert=False):
        self.answers.append(text)


def buttons(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def targets(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def main():
    aid = db.create_appointment(cid, sid, mid, "2026-08-20 11:00", "2026-08-20 13:00")
    db.add_ref(aid, "file_x", "photo", 5)

    # --- "Answer later": there's an exit back to the owner panel
    bot = FakeBot()
    cb = FakeCb(f"a:reflater:{aid}", bot)
    await ah.cb_ref_later(cb)
    print("Postponed ->", buttons(cb.message.markup))
    assert "a:menu" in targets(cb.message.markup), "no exit to the owner panel"
    assert t("postponed_short") in cb.message.text
    # no status was set -> the end-of-day reminder will pick it up
    assert db.get_appointment(aid)["ref_status"] == ""
    print("  exit to panel ✅, no status set ✅")

    # --- "All set": the client is notified exactly once, there's an exit to the panel
    bot = FakeBot()
    cb = FakeCb(f"a:refok:{aid}", bot)
    await ah.cb_ref_ok(cb)
    print("All set ->", buttons(cb.message.markup))
    assert "a:menu" in targets(cb.message.markup), "no exit to the owner panel"
    assert db.get_appointment(aid)["ref_status"] == "ok"
    assert len(bot.sent) == 1, "the client should get exactly one confirmation"
    assert bot.sent[0][0] == 999
    print("  exit to panel ✅, client notified once ✅")

    # the status buttons are gone -> this screen can't confirm a second time
    assert not any(cb_data and cb_data.startswith("a:refok:") for cb_data in targets(cb.message.markup)), \
        "the confirm button should be gone"
    print('  "All set" button removed from the screen ✅')

    # --- a repeat tap (e.g. from an old message) doesn't send a second notification
    bot2 = FakeBot()
    cb2 = FakeCb(f"a:refok:{aid}", bot2)
    await ah.cb_ref_ok(cb2)
    assert not bot2.sent, "a second confirmation must not be sent to the client"
    assert cb2.answers == [t("already_confirmed_master")]
    print("  repeat tap -> nothing sent to the client ✅")

    print()
    print("ALL CARD CHECKS PASSED ✅")


asyncio.run(main())
