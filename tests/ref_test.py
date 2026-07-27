"""Тест логики референсов: окно приёма, дедлайн, лимит, статусы, миграция."""
import os
import sqlite3
import sys
from datetime import datetime, timedelta

DB = "/private/tmp/claude-501/-Users-admin-Claude/f8db589b-7a41-413d-a71d-7e652b630980/scratchpad/ref.db"
os.environ["DB_PATH"] = DB
if os.path.exists(DB):
    os.remove(DB)
sys.path.insert(0, "/Users/admin/Claude/appointment-bot")

import db
import config
config.ADMIN_IDS = frozenset()  # isolate tests from the developer's real .env
import refs
import scheduler

db.init_db()
mid = db.add_master("Test Master", 111111111)
sid = db.add_service("Маникюр", 120, "40 евро", False, 1, 25)
cid = db.create_client("Ира", "+79990001122", tg_id=222)

tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
today = datetime.now().strftime("%Y-%m-%d")


def mkappt(starts, created=None):
    aid = db.create_appointment(cid, sid, mid, starts, starts)
    if created:
        db.conn().execute("UPDATE appointments SET created_at=? WHERE id=?", (created, aid))
        db.conn().commit()
    return db.get_appointment(aid)


# === window(): чистая логика дедлайна, не зависит от текущего времени ===

# обычная запись (создана заранее): дедлайн = 5:00 дня записи
a_norm = mkappt("2026-08-20 14:00", created="2026-08-18 12:00")
deadline, late = refs.window(a_norm)
print("обычная запись        -> дедлайн:", deadline, "| late:", late)
assert deadline == "2026-08-20 05:00" and late is False

# ГЛАВНЫЙ КРАЕВОЙ СЛУЧАЙ: записались в тот же день ПОСЛЕ 5:00.
# Дедлайн был бы в прошлом -> окно открыто до начала визита + предупреждение.
a_late = mkappt("2026-08-20 14:00", created="2026-08-20 09:00")
deadline, late = refs.window(a_late)
print("запись в тот же день  -> дедлайн:", deadline, "| late:", late)
assert late is True, "должен включиться режим предупреждения"
assert deadline == "2026-08-20 14:00", "окно до начала визита"

# ранний слот: дедлайн 5:00 позже визита -> окно не должно пережить начало визита
a_night = mkappt("2026-08-20 02:00", created="2026-08-18 12:00")
deadline, late = refs.window(a_night)
print("ранний слот 02:00     -> дедлайн:", deadline, "| late:", late)
assert deadline == "2026-08-20 02:00", "нельзя принимать референс после начала визита"
assert late is False

# === is_open(): относительно текущего времени ===
a = mkappt(f"{tomorrow} 14:00", created=f"{today} 12:00")
assert refs.is_open(a), "визит завтра — окно открыто"

yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
closed = mkappt(f"{yesterday} 14:00", created=f"{yesterday} 01:00")
assert not refs.is_open(closed), "дедлайн 5:00 вчерашнего дня прошёл"

# --- отменённая запись не принимает референс
cancelled = mkappt(f"{tomorrow} 16:00", created=f"{today} 12:00")
db.set_appointment_status(cancelled["id"], "cancelled")
assert not refs.is_open(db.get_appointment(cancelled["id"]))

# --- лимит медиа соблюдается (в т.ч. при параллельном альбоме)
aid = a["id"]
for i in range(5):
    assert db.add_ref(aid, f"file_{i}", "photo", 5), f"фото {i} должно добавиться"
assert not db.add_ref(aid, "file_6", "photo", 5), "шестое фото не должно пролезть"
assert db.refs_count(aid) == 5
print("лимит медиа:", db.refs_count(aid), "/ 5 ✅")

# --- комментарий и сводка
db.set_ref_comment(aid, "френч молочный")
a = db.get_appointment(aid)
assert refs.has_ref(a)
print("сводка:", refs.summary(a))
assert refs.summary(a) == "5 медиа · «френч молочный»"

# --- отложенное уведомление мастеру не дублируется
db.schedule_ref_notify(aid)
db.schedule_ref_notify(aid)
n = db.conn().execute(
    "SELECT COUNT(*) c FROM notifications WHERE appointment_id=? AND type='refnew' AND sent=0",
    (aid,)).fetchone()["c"]
assert n == 1, f"должно быть ровно одно ожидающее уведомление, а не {n}"
db.mark_ref_notified(aid)
n = db.conn().execute(
    "SELECT COUNT(*) c FROM notifications WHERE appointment_id=? AND type='refnew' AND sent=0",
    (aid,)).fetchone()["c"]
assert n == 0, "после отправки не должно остаться ожидающих"
print("дедупликация уведомлений OK ✅")

# --- статусы
db.set_ref_status(aid, "ok")
assert db.get_appointment(aid)["ref_status"] == "ok"
db.set_ref_status(aid, "no_materials")
assert db.get_appointment(aid)["ref_status"] == "no_materials"

# --- удаление сбрасывает и медиа, и комментарий, и статус
db.clear_refs(aid)
a = db.get_appointment(aid)
assert db.refs_count(aid) == 0 and not a["ref_comment"] and not a["ref_status"]
assert not refs.has_ref(a)
print("удаление референса OK ✅")

# --- напоминание про референс не шлётся, если референс уже прислан
a2 = mkappt(f"{tomorrow} 18:00", created=f"{today} 10:00")
assert scheduler._build_message("refremind", a2) is not None, "без референса — напоминаем"
db.add_ref(a2["id"], "f", "photo", 5)
a2 = db.get_appointment(a2["id"])
assert scheduler._build_message("refremind", a2) is None, "референс есть — не напоминаем"
print("напоминание про референс OK ✅")

# --- переписка
db.add_message(a2["id"], "client", "опоздаю на 10 минут")
db.add_message(a2["id"], "master", "хорошо")
assert db.messages_count(a2["id"]) == 2

print()
print("=== МИГРАЦИЯ СТАРОЙ БАЗЫ ===")
# Старая база без колонок ref_comment/ref_status: данные должны уцелеть
OLD = DB + ".old"
if os.path.exists(OLD):
    os.remove(OLD)
c = sqlite3.connect(OLD)
c.executescript("""
CREATE TABLE appointments(
    id INTEGER PRIMARY KEY AUTOINCREMENT, client_id INTEGER NOT NULL,
    service_id INTEGER NOT NULL, master_id INTEGER NOT NULL, group_session_id INTEGER,
    starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, status TEXT DEFAULT 'created',
    created_by TEXT DEFAULT 'client', created_at TEXT DEFAULT (datetime('now','localtime')));
INSERT INTO appointments(client_id,service_id,master_id,starts_at,ends_at)
VALUES(1,1,1,'2026-07-20 10:00','2026-07-20 12:00');
""")
c.commit()
c.close()

import importlib

import config

os.environ["DB_PATH"] = OLD
importlib.reload(config)   # db.conn() читает путь именно отсюда
assert config.DB_PATH == OLD
db._conn = None
db.init_db()
row = db.conn().execute("SELECT * FROM appointments WHERE id=1").fetchone()
print("старая запись после миграции:", dict(row))
assert row["starts_at"] == "2026-07-20 10:00", "данные не должны потеряться"
assert row["ref_comment"] == "" and row["ref_status"] == "", "новые колонки должны появиться"
db.init_db()  # повторный запуск не должен падать
print("миграция OK ✅")

print()
print("ВСЕ ПРОВЕРКИ РЕФЕРЕНСОВ ПРОШЛИ ✅")
