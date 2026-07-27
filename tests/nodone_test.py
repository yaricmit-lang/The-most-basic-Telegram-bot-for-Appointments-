"""Без кнопки «Готово» референс должен доходить до мастера сам."""
import asyncio
import os
import sys
from datetime import datetime, timedelta

DB = "/private/tmp/claude-501/-Users-admin-Claude/f8db589b-7a41-413d-a71d-7e652b630980/scratchpad/nodone.db"
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
cid = db.create_client("Test Client", "+79995550001", tg_id=999)

from handlers import client as ch
import scheduler

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

    # --- на экране загрузки больше нет «Готово»
    markup = ch.ref_kb(a)
    labels = [b.text for row in markup.inline_keyboard for b in row]
    print("пустой экран референса ->", labels)
    assert not any("Готово" in x for x in labels), "кнопка Готово должна исчезнуть"
    assert any("Назад" in x for x in labels), "выход должен остаться"

    # --- после присланного фото: удалить всё + назад, но без Готово
    db.add_ref(aid, "file_1", "photo", 5)
    db.schedule_ref_notify(aid)          # ровно это делает обработчик фото
    a = db.get_appointment(aid)
    labels = [b.text for row in ch.ref_kb(a).inline_keyboard for b in row]
    print("после фото            ->", labels)
    assert not any("Готово" in x for x in labels)
    assert any("Удалить" in x for x in labels)

    # --- КЛЮЧЕВОЕ: клиент ничего не нажимает, просто уходит.
    # Отложенное уведомление обязано доставить референс мастеру.
    db.conn().execute(
        "UPDATE notifications SET due_at=? WHERE appointment_id=? AND type='refnew'",
        ((datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M"), aid),
    )
    db.conn().commit()
    bot = FakeBot()
    await scheduler._send_due(bot)
    print("уведомления мастеру   ->", bot.msgs)
    assert bot.msgs, "мастер обязан получить референс без нажатия «Готово»"
    assert bot.msgs[0][0] == 111111111
    assert "Референс к записи" in bot.msgs[0][1]
    assert bot.albums == [(111111111, 1)], "фото должно уйти вместе с карточкой"
    print("референс дошёл сам ✅")

    # --- второй раз то же самое уведомление не шлётся
    bot2 = FakeBot()
    await scheduler._send_due(bot2)
    assert not bot2.msgs, "уведомление не должно повторяться"
    print("без повторов ✅")

    print()
    print("ВСЕ ПРОВЕРКИ БЕЗ «ГОТОВО» ПРОШЛИ ✅")


asyncio.run(main())
