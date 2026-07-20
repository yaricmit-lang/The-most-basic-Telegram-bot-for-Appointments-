"""Галерея примеров вида маникюра: управление у мастера, показ клиенту, миграция."""
import asyncio
import os
import sqlite3
import sys

DB = "/private/tmp/claude-501/-Users-admin-Claude/f8db589b-7a41-413d-a71d-7e652b630980/scratchpad/svcph.db"
os.environ["DB_PATH"] = DB
if os.path.exists(DB):
    os.remove(DB)
sys.path.insert(0, "/Users/admin/Claude/appointment-bot")

import db
import config
config.ADMIN_IDS = frozenset()  # isolate tests from the developer's real .env

db.init_db()
mid = db.add_master("Test Master", 111111111)
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
    # === мастер наполняет галерею ===
    st = FakeState(ph_sid=design)
    for i in range(3):
        await ah.got_service_photo(FakeMsg(photo=f"PH_{i}"), st)
    assert db.service_photos_count(design) == 3
    assert db.service_cover(design) == "PH_0", "обложка — первое фото"
    print("мастер добавил 3 примера, обложка:", db.service_cover(design), "✅")

    # альбом от мастера -> один ответ, но все фото сохраняются
    st = FakeState(ph_sid=simple)
    m1, m2, m3 = (FakeMsg(photo=f"A_{i}", group="album1") for i in range(3))
    await ah.got_service_photo(m1, st)
    await ah.got_service_photo(m2, st)
    await ah.got_service_photo(m3, st)
    assert db.service_photos_count(simple) == 3, "все фото альбома должны сохраниться"
    answered = sum(1 for m in (m1, m2, m3) if m.replies)
    assert answered == 1, f"на альбом должен быть 1 ответ, а не {answered}"
    print("альбом: 3 фото сохранены, ответ один ✅")

    # лимит галереи
    for i in range(20):
        db.add_service_photo(simple, f"X_{i}")
    assert db.service_photos_count(simple) == db.SVC_PHOTO_MAX
    print(f"лимит галереи соблюдён: {db.SVC_PHOTO_MAX} ✅")

    # === экран галереи у мастера ===
    bot = FakeBot()
    await ah.send_gallery(bot, 111111111, design)
    assert bot.albums[0][1] == ["PH_0", "PH_1", "PH_2"], "должен уйти альбом примеров"
    text, markup = bot.msgs[0]
    print("\nэкран галереи:")
    for line in text.splitlines():
        if line.strip():
            print("   ", line)
    print("  кнопки:", labels(markup))
    assert ["🗑 1", "🗑 2", "🗑 3"] == [x for x in labels(markup) if x.startswith("🗑")]
    assert any("Add photo" in x for x in labels(markup))
    assert any("Rename" in x for x in labels(markup)), "rename should be right there"

    # удаление по номеру убирает именно это фото
    photos = db.service_photos(design)
    cb = FakeCb(f"a:svcphdel:{photos[1]['id']}", uid=111111111)
    await ah.cb_service_photo_del(cb, FakeState())
    left = [p["file_id"] for p in db.service_photos(design)]
    print("\nпосле удаления №2 осталось:", left)
    assert left == ["PH_0", "PH_2"], "должно удалиться ровно второе фото"

    # удаление обложки -> обложкой становится следующее
    cb = FakeCb(f"a:svcphdel:{db.service_photos(design)[0]['id']}", uid=111111111)
    await ah.cb_service_photo_del(cb, FakeState())
    assert db.service_cover(design) == "PH_2", "обложка должна перейти на следующее фото"
    print("обложка после удаления первого:", db.service_cover(design), "✅")

    # карточка услуги показывает счётчик
    db.add_service_photo(design, "PH_NEW")
    text, markup = ah.svc_detail(design)
    assert "🖼 Examples: 2" in text
    assert any("Examples (2)" in x for x in labels(markup))
    print("карточка вида: счётчик примеров ✅")

    # === клиент ===
    cb = FakeCb("bk")
    await ch.cb_book(cb, FakeState())
    text, markup = cb.message.replies[-1]
    assert "Choose a manicure style" in text and "by photo" in text
    assert all(x.startswith("🖼") for x in labels(markup) if "дизайном" in x), \
        "оба вида с примерами должны быть помечены"
    print("\nсписок видов: подпись и маркеры ✅")

    # тап по виду -> обложка + кнопка показа примеров
    cb = FakeCb(f"svc:{design}")
    st = FakeState()
    await ch.cb_service(cb, st)
    photo, caption, markup = cb.message.photos_sent[0]
    print("\nкарточка вида у клиента:")
    print("    обложка:", photo)
    print("  кнопки:", labels(markup))
    assert photo == "PH_2"
    assert any("Show examples (2)" in x for x in labels(markup)), "missing the show-examples button"
    assert f"svcex:{design}" in cbs(markup)

    # нажатие «Показать примеры» -> альбом всех примеров
    cb = FakeCb(f"svcex:{design}")
    st = FakeState()
    await ch.cb_service_examples(cb, st)
    assert cb.bot.albums, "должен уйти альбом примеров"
    chat_id, files = cb.bot.albums[0]
    print("\nпоказ примеров клиенту -> альбом:", files)
    assert files == ["PH_2", "PH_NEW"], "клиент должен увидеть все примеры вида"
    assert chat_id == 999
    text, markup = cb.message.replies[-1]
    assert "examples: 2" in text
    assert f"svcgo:{design}" in cbs(markup), "из примеров можно сразу выбрать время"
    assert st.data["service_id"] == design, "вид должен запомниться"
    print("  из показа примеров можно сразу выбрать время ✅")

    # один пример -> кнопки показа нет (обложка и есть пример)
    one = db.add_service("Маникюр без покрытия", 60, "30 евро", False, 1, 25)
    db.add_service_photo(one, "SINGLE")
    cb = FakeCb(f"svc:{one}")
    await ch.cb_service(cb, FakeState())
    _, _, markup = cb.message.photos_sent[0]
    assert not any("Show examples" in x for x in labels(markup)), \
        "при одном фото кнопка показа лишняя"
    print("один пример -> кнопки показа нет ✅")

    # вид без примеров -> сразу к датам
    plain = db.add_service("Снятие", 30, "10 евро", False, 1, 0)
    cb = FakeCb(f"svc:{plain}")
    st = FakeState()
    await ch.cb_service(cb, st)
    assert not cb.message.photos_sent
    assert st.data.get("master_id") == mid
    print("вид без примеров -> без лишнего шага ✅")

    # === миграция: старое одиночное фото переезжает в галерею ===
    OLD = DB + ".old"
    if os.path.exists(OLD):
        os.remove(OLD)
    c = sqlite3.connect(OLD)
    c.executescript("""
    CREATE TABLE services(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
        duration_min INTEGER NOT NULL, price TEXT DEFAULT '', is_group INTEGER DEFAULT 0,
        capacity INTEGER DEFAULT 1, comeback_days INTEGER DEFAULT 21,
        photo_id TEXT DEFAULT '', active INTEGER DEFAULT 1);
    INSERT INTO services(title,duration_min,photo_id) VALUES('Маникюр',120,'OLD_PHOTO');
    """)
    c.commit()
    c.close()

    import importlib

    import config
    os.environ["DB_PATH"] = OLD
    importlib.reload(config)
    db._conn = None
    db.init_db()
    assert db.service_cover(1) == "OLD_PHOTO", "старое фото должно стать обложкой"
    assert db.get_service(1)["photo_id"] == "", "колонка должна погаснуть после переноса"
    db.init_db()  # повторный запуск не должен задваивать
    assert db.service_photos_count(1) == 1, "повторная миграция не должна дублировать фото"
    print("\nмиграция старого фото в галерею ✅ (повторный запуск не дублирует)")

    print()
    print("ВСЕ ПРОВЕРКИ ГАЛЕРЕИ ПРОШЛИ ✅")


asyncio.run(main())
