"""Without a "Done" button, a reference must still reach the master on its own."""
import asyncio
import os
import tempfile
import sys
from datetime import datetime, timedelta

DB = os.path.join(tempfile.gettempdir(), "nodone.db")
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

from handlers import client as ch
import scheduler
from i18n import t

tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")


class FakeBot:
    def __init__(self):
        self.msgs, self.albums = [], []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.msgs.append((chat_id, text))

    async def send_media_group(self, chat_id, media):
        self.albums.append((chat_id, len(media)))


async def main():
    aid = db.create_appointment(cid, sid, mid, f"{tomorrow} 14:30", f"{tomorrow} 16:30")
    a = db.get_appointment(aid)

    # --- the upload screen no longer has a "Done" button
    markup = ch.ref_kb(a)
    labels = [b.text for row in markup.inline_keyboard for b in row]
    print("empty reference screen ->", labels)
    assert not any("Done" in x for x in labels), 'the "Done" button should be gone'
    assert any(t("btn_back").split()[-1] in x for x in labels), "the exit button should remain"

    # --- after a photo is sent: delete-all + back, but no "Done"
    db.add_ref(aid, "file_1", "photo", 5)
    db.schedule_ref_notify(aid)          # this is exactly what the photo handler does
    a = db.get_appointment(aid)
    labels = [b.text for row in ch.ref_kb(a).inline_keyboard for b in row]
    print("after a photo         ->", labels)
    assert not any("Done" in x for x in labels)
    assert any("Delete" in x for x in labels)

    # --- KEY CASE: the client never taps anything, just walks away.
    # The delayed notification must still deliver the reference to the master.
    db.conn().execute(
        "UPDATE notifications SET due_at=? WHERE appointment_id=? AND type='refnew'",
        ((datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M"), aid),
    )
    db.conn().commit()
    bot = FakeBot()
    await scheduler._send_due(bot)
    print("notification to the master ->", bot.msgs)
    assert bot.msgs, 'the master must get the reference without a "Done" tap'
    assert bot.msgs[0][0] == 111111111
    assert "Reference for the booking" in bot.msgs[0][1]
    assert bot.albums == [(111111111, 1)], "the photo should go out together with the card"
    print("the reference arrived on its own ✅")

    # --- the same notification isn't sent a second time
    bot2 = FakeBot()
    await scheduler._send_due(bot2)
    assert not bot2.msgs, "the notification must not repeat"
    print("no duplicates ✅")

    print()
    print('ALL "NO-DONE-BUTTON" CHECKS PASSED ✅')


asyncio.run(main())
