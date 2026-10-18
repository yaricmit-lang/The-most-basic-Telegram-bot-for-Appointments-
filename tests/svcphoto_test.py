"""Service example gallery: management by the master, display to the client, migration."""
import asyncio
import os
import tempfile
import sqlite3
import sys

DB = os.path.join(tempfile.gettempdir(), "svcph.db")
os.environ["DB_PATH"] = DB
if os.path.exists(DB):
    os.remove(DB)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import config
config.ADMIN_IDS = frozenset()  # isolate tests from the developer's real .env

db.init_db()
mid = db.add_master("Test Master", 111111111)
# Titles stay in Russian on purpose: they let the "both styles get a gallery
# marker" check below key off the shared word in their titles.
simple = db.add_service("Короткий с простым дизайном", 90, "40 евро", False, 1, 25)
design = db.add_service("Длинный со сложным дизайном", 150, "60 евро", False, 1, 25)

from handlers import admin as ah
from handlers import client as ch


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
        self.albums, self.msgs = [], []

    async def send_media_group(self, chat_id, media):
        self.albums.append((chat_id, [m.media for m in media]))

    async def send_message(self, chat_id, text, reply_markup=None):
        self.msgs.append((text, reply_markup))


class FakeMsg:
    def __init__(self, photo=None, text="", group=None):
        self.photo = [type("P", (), {"file_id": photo})()] if photo else None
        self.text, self.media_group_id = text, group
        self.from_user = type("U", (), {"id": 111111111, "username": "test_owner"})()
        self.replies, self.photos_sent = [], []

    async def answer(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))

    async def answer_photo(self, photo, caption=None, reply_markup=None):
        self.photos_sent.append((photo, caption, reply_markup))

    async def edit_text(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))


class FakeCb:
    def __init__(self, data, uid=999, bot=None):
        self.data, self.bot = data, bot or FakeBot()
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
    # === the master fills the gallery ===
    st = FakeState(ph_sid=design)
    for i in range(3):
        await ah.got_service_photo(FakeMsg(photo=f"PH_{i}"), st)
    assert db.service_photos_count(design) == 3
    assert db.service_cover(design) == "PH_0", "the cover is the first photo"
    print("master added 3 examples, cover:", db.service_cover(design), "✅")

    # an album from the master -> one reply, but every photo is saved
    st = FakeState(ph_sid=simple)
    m1, m2, m3 = (FakeMsg(photo=f"A_{i}", group="album1") for i in range(3))
    await ah.got_service_photo(m1, st)
    await ah.got_service_photo(m2, st)
    await ah.got_service_photo(m3, st)
    assert db.service_photos_count(simple) == 3, "every photo in the album should be saved"
    answered = sum(1 for m in (m1, m2, m3) if m.replies)
    assert answered == 1, f"an album should get 1 reply, not {answered}"
    print("album: 3 photos saved, one reply ✅")

    # gallery limit
    for i in range(20):
        db.add_service_photo(simple, f"X_{i}")
    assert db.service_photos_count(simple) == db.SVC_PHOTO_MAX
    print(f"gallery limit enforced: {db.SVC_PHOTO_MAX} ✅")

    # === the master's gallery screen ===
    bot = FakeBot()
    await ah.send_gallery(bot, 111111111, design)
    assert bot.albums[0][1] == ["PH_0", "PH_1", "PH_2"], "the examples album should go out"
    text, markup = bot.msgs[0]
    print("\ngallery screen:")
    for line in text.splitlines():
        if line.strip():
            print("   ", line)
    print("  buttons:", labels(markup))
    assert ["\U0001F5D1 1", "\U0001F5D1 2", "\U0001F5D1 3"] == [x for x in labels(markup) if x.startswith("\U0001F5D1")]
    assert any("Add photo" in x for x in labels(markup))
    assert any("Rename" in x for x in labels(markup)), "renaming should be right here"

    # deleting by number removes exactly that photo
    photos = db.service_photos(design)
    cb = FakeCb(f"a:svcphdel:{photos[1]['id']}", uid=111111111)
    await ah.cb_service_photo_del(cb, FakeState())
    left = [p["file_id"] for p in db.service_photos(design)]
    print("\nafter deleting #2, left with:", left)
    assert left == ["PH_0", "PH_2"], "exactly the second photo should be gone"

    # deleting the cover -> the next photo becomes the cover
    cb = FakeCb(f"a:svcphdel:{db.service_photos(design)[0]['id']}", uid=111111111)
    await ah.cb_service_photo_del(cb, FakeState())
    assert db.service_cover(design) == "PH_2", "the cover should move to the next photo"
    print("cover after deleting the first one:", db.service_cover(design), "✅")

    # the service card shows the count
    db.add_service_photo(design, "PH_NEW")
    text, markup = ah.svc_detail(design)
    assert "Examples: 2" in text
    assert any("Examples (2)" in x for x in labels(markup))
    print("service card: example count ✅")

    # === client side ===
    cb = FakeCb("bk")
    await ch.cb_book(cb, FakeState())
    text, markup = cb.message.replies[-1]
    assert "Choose a service" in text and "by photo" in text
    assert all(x.startswith("\U0001F5BC") for x in labels(markup) if "дизайном" in x), \
        "both styles with examples should be marked"
    print("\nservice list: caption and markers ✅")

    # tapping a style -> cover photo + a "show examples" button
    cb = FakeCb(f"svc:{design}")
    st = FakeState()
    await ch.cb_service(cb, st)
    photo, caption, markup = cb.message.photos_sent[0]
    print("\nclient's style card:")
    print("    cover:", photo)
    print("  buttons:", labels(markup))
    assert photo == "PH_2"
    assert any("Show examples (2)" in x for x in labels(markup)), "missing the show-examples button"
    assert f"svcex:{design}" in cbs(markup)

    # tapping "Show examples" -> an album of all the examples
    cb = FakeCb(f"svcex:{design}")
    st = FakeState()
    await ch.cb_service_examples(cb, st)
    assert cb.bot.albums, "the examples album should go out"
    chat_id, files = cb.bot.albums[0]
    print("\nshowing examples to the client -> album:", files)
    assert files == ["PH_2", "PH_NEW"], "the client should see every example for this style"
    assert chat_id == 999
    text, markup = cb.message.replies[-1]
    assert "2 example(s)" in text
    assert f"svcgo:{design}" in cbs(markup), "picking a time should be reachable right from the examples"
    assert st.data["service_id"] == design, "the style should be remembered"
    print("  can jump straight to picking a time from the examples ✅")

    # a single example -> no show button (the cover already is the example)
    one = db.add_service("Manicure without coating", 60, "30 евро", False, 1, 25)
    db.add_service_photo(one, "SINGLE")
    cb = FakeCb(f"svc:{one}")
    await ch.cb_service(cb, FakeState())
    _, _, markup = cb.message.photos_sent[0]
    assert not any("Show examples" in x for x in labels(markup)), \
        "a single photo makes the show button redundant"
    print("single example -> no show button ✅")

    # a style with no examples -> straight to dates
    plain = db.add_service("Removal", 30, "10 евро", False, 1, 0)
    cb = FakeCb(f"svc:{plain}")
    st = FakeState()
    await ch.cb_service(cb, st)
    assert not cb.message.photos_sent
    assert st.data.get("master_id") == mid
    print("style without examples -> no extra step ✅")

    # === migration: an old single photo moves into the gallery ===
    OLD = DB + ".old"
    if os.path.exists(OLD):
        os.remove(OLD)
    c = sqlite3.connect(OLD)
    c.executescript("""
    CREATE TABLE services(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
        duration_min INTEGER NOT NULL, price TEXT DEFAULT '', is_group INTEGER DEFAULT 0,
        capacity INTEGER DEFAULT 1, comeback_days INTEGER DEFAULT 21,
        photo_id TEXT DEFAULT '', active INTEGER DEFAULT 1);
    INSERT INTO services(title,duration_min,photo_id) VALUES('Manicure',120,'OLD_PHOTO');
    """)
    c.commit()
    c.close()

    import importlib

    import config
    os.environ["DB_PATH"] = OLD
    importlib.reload(config)
    db._conn = None
    db.init_db()
    assert db.service_cover(1) == "OLD_PHOTO", "the old photo should become the cover"
    assert db.get_service(1)["photo_id"] == "", "the column should clear after migrating"
    db.init_db()  # running it again shouldn't duplicate
    assert db.service_photos_count(1) == 1, "a repeat migration shouldn't duplicate the photo"
    print("\nold photo migrated into the gallery ✅ (repeat run doesn't duplicate)")

    print()
    print("ALL GALLERY CHECKS PASSED ✅")


asyncio.run(main())
