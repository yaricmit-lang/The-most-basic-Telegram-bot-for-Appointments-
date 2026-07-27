"""Экраны референса: кнопка выхода в панель + защита от повторного подтверждения."""
import asyncio
import os
import sys

DB = "/private/tmp/claude-501/-Users-admin-Claude/f8db589b-7a41-413d-a71d-7e652b630980/scratchpad/card.db"
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

from handlers import admin as ah


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

    # --- «Ответить позже»: есть выход в панель мастера
    bot = FakeBot()
    cb = FakeCb(f"a:reflater:{aid}", bot)
    await ah.cb_ref_later(cb)
    print("Отложено ->", buttons(cb.message.markup))
    assert "a:menu" in targets(cb.message.markup), "нет выхода в панель мастера"
    assert "Отложено" in cb.message.text
    # статус не выставлен -> напоминание в конце дня подхватит
    assert db.get_appointment(aid)["ref_status"] == ""
    print("  выход в панель ✅, статус не выставлен ✅")

    # --- «Всё есть»: клиент уведомлён один раз, есть выход в панель
    bot = FakeBot()
    cb = FakeCb(f"a:refok:{aid}", bot)
    await ah.cb_ref_ok(cb)
    print("Всё есть ->", buttons(cb.message.markup))
    assert "a:menu" in targets(cb.message.markup), "нет выхода в панель мастера"
    assert db.get_appointment(aid)["ref_status"] == "ok"
    assert len(bot.sent) == 1, "клиент должен получить ровно одно подтверждение"
    assert bot.sent[0][0] == 999
    print("  выход в панель ✅, клиент уведомлён 1 раз ✅")

    # кнопки-статусы убраны -> повторно подтвердить с этого экрана нельзя
    assert not any(t and t.startswith("a:refok:") for t in targets(cb.message.markup)), \
        "кнопка подтверждения должна исчезнуть"
    print("  кнопка «Всё есть» убрана с экрана ✅")

    # --- повторное нажатие (например, со старого сообщения) не шлёт второй раз
    bot2 = FakeBot()
    cb2 = FakeCb(f"a:refok:{aid}", bot2)
    await ah.cb_ref_ok(cb2)
    assert not bot2.sent, "второе подтверждение клиенту слать нельзя"
    assert cb2.answers == ["Уже подтверждено"]
    print("  повторное нажатие -> клиенту ничего не ушло ✅")

    print()
    print("ВСЕ ПРОВЕРКИ ЭКРАНОВ ПРОШЛИ ✅")


asyncio.run(main())
