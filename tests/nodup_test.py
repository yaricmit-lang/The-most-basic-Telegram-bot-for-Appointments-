"""Защита от повторной отправки уведомлений на всех уровнях."""
import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timedelta

DB = "/private/tmp/claude-501/-Users-admin-Claude/f8db589b-7a41-413d-a71d-7e652b630980/scratchpad/nodup.db"
os.environ["DB_PATH"] = DB
if os.path.exists(DB):
    os.remove(DB)
sys.path.insert(0, "/Users/admin/Claude/appointment-bot")

import db
import config
config.ADMIN_IDS = frozenset()  # isolate tests from the developer's real .env
import scheduler

db.init_db()
mid = db.add_master("Test Master", 111111111)
sid = db.add_service("Маникюр", 120, "40€", False, 1, 25)
cid = db.create_client("Test Client", "+79995550001", tg_id=999)

from handlers import admin as ah
from handlers import client as ch


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text))

    async def send_media_group(self, chat_id, media):
        pass


class FakeMsg:
    async def answer(self, *a, **k):
        pass

    async def edit_text(self, *a, **k):
        pass


class FakeCb:
    def __init__(self, data, uid=999):
        self.data, self.bot = data, FakeBot()
        self.message = FakeMsg()
        self.from_user = type("U", (), {"id": uid, "username": "olga"})()
        self.answers = []

    async def answer(self, text="", show_alert=False):
        self.answers.append(text)


class FakeState:
    def __init__(self, **d):
        self.state, self.data = None, dict(d)

    async def set_state(self, s):
        self.state = s

    async def clear(self):
        self.state, self.data = None, {}

    async def update_data(self, **k):
        self.data.update(k)

    async def get_data(self):
        return self.data


def count_notif(aid, typ):
    return db.conn().execute(
        "SELECT COUNT(*) n FROM notifications WHERE appointment_id=? AND type=?", (aid, typ)
    ).fetchone()["n"]


async def main():
    far = (datetime.now() + timedelta(hours=30)).strftime("%Y-%m-%d %H:%M")
    aid = db.create_appointment(cid, sid, mid, far, far)

    # === 1. Повторный schedule_for_appointment не задваивает строки ===
    scheduler.schedule_for_appointment(aid, far)
    scheduler.schedule_for_appointment(aid, far)   # двойной вызов (двойной тап/переотправка)
    scheduler.schedule_for_appointment(aid, far)
    for typ in ("confirm24", "remind2", "refremind"):
        n = count_notif(aid, typ)
        print(f"  {typ}: строк={n}")
        assert n == 1, f"{typ} задвоился: {n} строк"
    print("1. schedule_for_appointment идемпотентен ✅")

    # === 2. Прямой add_notification-дубль игнорируется ===
    db.add_notification(aid, "feedback", db.now_str())
    db.add_notification(aid, "feedback", db.now_str())
    assert count_notif(aid, "feedback") == 1
    print("2. add_notification OR IGNORE ✅")

    # === 3. refnew МОЖЕТ повторяться (клиент дослал референс) ===
    db.add_ref(aid, "f1", "photo", 5)
    db.schedule_ref_notify(aid)
    # первый refnew отправим, затем клиент дослал -> второй refnew допустим
    db.mark_ref_notified(aid)
    db.add_ref(aid, "f2", "photo", 5)
    db.schedule_ref_notify(aid)
    assert count_notif(aid, "refnew") == 2, "refnew должен уметь повторяться"
    print("3. refnew повторяется легитимно (2 строки) ✅")

    # === 4. Одна строка уведомления не отправляется дважды (2 тика воркера) ===
    due = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")
    a2 = db.create_appointment(cid, sid, mid,
                               (datetime.now() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M"),
                               (datetime.now() + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M"))
    db.add_notification(a2, "remind2", due)
    bot = FakeBot()
    await scheduler._send_due(bot)
    await scheduler._send_due(bot)   # второй тик
    remind_sends = [s for s in bot.sent if "Через 2 часа" in s[1]]
    print(f"  отправок remind2 за 2 тика: {len(remind_sends)}")
    assert len(remind_sends) == 1, "одно уведомление отправлено дважды!"
    print("4. одна строка = одна отправка ✅")

    # === 5. Завершение визита дважды не задваивает feedback/comeback ===
    past_s = (datetime.now() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")
    past_e = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    a3 = db.create_appointment(cid, sid, mid, past_s, past_e)
    scheduler._complete_finished()
    scheduler._complete_finished()   # повторный проход
    assert count_notif(a3, "feedback") == 1 and count_notif(a3, "comeback") == 1
    print("5. _complete_finished идемпотентен ✅")

    # === 6. Двойной тап «Отменить» -> мастеру одно уведомление, лист ожидания один раз ===
    a4 = db.create_appointment(cid, sid, mid,
                               (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d %H:%M"),
                               (datetime.now() + timedelta(days=2, hours=2)).strftime("%Y-%m-%d %H:%M"))
    cb1 = FakeCb(f"doccl:{a4}")
    cb2 = FakeCb(f"doccl:{a4}")
    # имитируем гонку: оба прочитали статус до записи
    await asyncio.gather(ch.cb_cancel_do(cb1), ch.cb_cancel_do(cb2))
    master_msgs = [s for s in cb1.bot.sent + cb2.bot.sent if "Отмена записи" in s[1]]
    print(f"  уведомлений мастеру об отмене: {len(master_msgs)}")
    assert len(master_msgs) == 1, "мастер получил отмену дважды!"
    assert db.get_appointment(a4)["status"] == "cancelled"
    print("6. двойная отмена -> одно уведомление мастеру ✅")

    # === 7. Двойная оценка -> мастеру одна ===
    db.set_appointment_status(a3, "completed")
    cbf1 = FakeCb(f"fb:{a3}:5")
    cbf2 = FakeCb(f"fb:{a3}:4")
    await ch.cb_feedback(cbf1, FakeState())
    await ch.cb_feedback(cbf2, FakeState())
    rating_msgs = [s for s in cbf1.bot.sent + cbf2.bot.sent if "Оценка" in s[1]]
    print(f"  уведомлений мастеру об оценке: {len(rating_msgs)}")
    assert len(rating_msgs) == 1, "мастер получил оценку дважды!"
    print("7. двойная оценка -> одно уведомление мастеру ✅")

    # === 8. Двойное подтверждение «Всё есть» -> клиенту одно ===
    a5 = db.create_appointment(cid, sid, mid,
                               (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M"),
                               (datetime.now() + timedelta(days=3, hours=2)).strftime("%Y-%m-%d %H:%M"))
    db.add_ref(a5, "fx", "photo", 5)
    cbo1 = FakeCb(f"a:refok:{a5}", uid=111111111)
    cbo2 = FakeCb(f"a:refok:{a5}", uid=111111111)
    await ah.cb_ref_ok(cbo1)
    await ah.cb_ref_ok(cbo2)
    ok_msgs = [s for s in cbo1.bot.sent + cbo2.bot.sent if "всё есть" in s[1].lower()]
    print(f"  подтверждений клиенту «материалы есть»: {len(ok_msgs)}")
    assert len(ok_msgs) == 1, "клиент получил подтверждение дважды!"
    print("8. двойное «Всё есть» -> одно клиенту ✅")

    # === 9. Двойное подтверждение визита -> статус один раз ===
    a6 = db.create_appointment(cid, sid, mid,
                               (datetime.now() + timedelta(days=4)).strftime("%Y-%m-%d %H:%M"),
                               (datetime.now() + timedelta(days=4, hours=2)).strftime("%Y-%m-%d %H:%M"))
    cbc1 = FakeCb(f"cfm:{a6}")
    cbc2 = FakeCb(f"cfm:{a6}")
    await ch.cb_confirm_visit(cbc1)
    await ch.cb_confirm_visit(cbc2)
    assert db.get_appointment(a6)["status"] == "confirmed"
    print("9. двойное подтверждение визита -> ок ✅")

    # === 10. Миграция: существующие дубли строк схлопываются, индекс не даёт вставить ===
    OLD = DB + ".old"
    if os.path.exists(OLD):
        os.remove(OLD)
    oc = sqlite3.connect(OLD)
    oc.executescript("""
    CREATE TABLE notifications(id INTEGER PRIMARY KEY AUTOINCREMENT,
        appointment_id INTEGER NOT NULL, type TEXT NOT NULL, due_at TEXT NOT NULL, sent INTEGER DEFAULT 0);
    INSERT INTO notifications(appointment_id,type,due_at,sent) VALUES
        (1,'remind2','2026-07-20 09:00',1),
        (1,'remind2','2026-07-20 09:00',1),
        (1,'feedback','2026-07-20 12:00',0),
        (1,'feedback','2026-07-20 12:00',0),
        (2,'refnew','2026-07-20 10:00',1),
        (2,'refnew','2026-07-20 10:00',1);
    """)
    oc.commit()
    oc.close()

    import importlib

    import config
    os.environ["DB_PATH"] = OLD
    importlib.reload(config)
    db._conn = None
    db.init_db()
    r2 = db.conn().execute("SELECT COUNT(*) n FROM notifications WHERE appointment_id=1 AND type='remind2'").fetchone()["n"]
    fb = db.conn().execute("SELECT COUNT(*) n FROM notifications WHERE appointment_id=1 AND type='feedback'").fetchone()["n"]
    rn = db.conn().execute("SELECT COUNT(*) n FROM notifications WHERE appointment_id=2 AND type='refnew'").fetchone()["n"]
    print(f"  после схлопывания: remind2={r2}, feedback={fb}, refnew={rn}")
    assert r2 == 1 and fb == 1, "дубли не схлопнулись"
    assert rn == 2, "refnew не должен схлопываться"
    # индекс не даёт вставить новый дубль
    try:
        db.conn().execute("INSERT INTO notifications(appointment_id,type,due_at) VALUES(1,'remind2','x')")
        db.conn().commit()
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    assert raised, "уникальный индекс должен запрещать дубль remind2"
    db.init_db()  # повторная миграция не падает
    print("10. миграция схлопнула дубли + индекс защищает ✅")

    print()
    print("ВСЕ ПРОВЕРКИ АНТИ-ДУБЛЯ ПРОШЛИ ✅")


asyncio.run(main())
