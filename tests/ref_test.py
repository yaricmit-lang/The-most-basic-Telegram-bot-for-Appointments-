"""Reference logic: the upload window, the deadline, the limit, statuses, migration."""
import os
import tempfile
import sqlite3
import sys
from datetime import datetime, timedelta

DB = os.path.join(tempfile.gettempdir(), "ref.db")
os.environ["DB_PATH"] = DB
if os.path.exists(DB):
    os.remove(DB)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import config
config.ADMIN_IDS = frozenset()  # isolate tests from the developer's real .env
import refs
import scheduler

db.init_db()
mid = db.add_master("Test Master", 111111111)
sid = db.add_service("Manicure", 120, "€40", False, 1, 25)
cid = db.create_client("Test Client", "+79990001122", tg_id=222)

tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
today = datetime.now().strftime("%Y-%m-%d")


def mkappt(starts, created=None):
    aid = db.create_appointment(cid, sid, mid, starts, starts)
    if created:
        db.conn().execute("UPDATE appointments SET created_at=? WHERE id=?", (created, aid))
        db.conn().commit()
    return db.get_appointment(aid)


# === window(): pure deadline logic, independent of the current time ===

# a normal booking (created well ahead): deadline = 5:00 on the booking day
a_norm = mkappt("2026-08-20 14:00", created="2026-08-18 12:00")
deadline, late = refs.window(a_norm)
print("normal booking         -> deadline:", deadline, "| late:", late)
assert deadline == "2026-08-20 05:00" and late is False

# KEY EDGE CASE: booked the same day, AFTER 5:00.
# The deadline would be in the past -> the window stays open until the visit, with a warning.
a_late = mkappt("2026-08-20 14:00", created="2026-08-20 09:00")
deadline, late = refs.window(a_late)
print("same-day booking       -> deadline:", deadline, "| late:", late)
assert late is True, "the warning mode should turn on"
assert deadline == "2026-08-20 14:00", "window stays open until the visit"

# an early slot: 5:00 deadline is later than the visit -> the window can't outlive the visit
a_night = mkappt("2026-08-20 02:00", created="2026-08-18 12:00")
deadline, late = refs.window(a_night)
print("early 02:00 slot       -> deadline:", deadline, "| late:", late)
assert deadline == "2026-08-20 02:00", "can't accept a reference after the visit starts"
assert late is False

# === is_open(): relative to the current time ===
a = mkappt(f"{tomorrow} 14:00", created=f"{today} 12:00")
assert refs.is_open(a), "the visit is tomorrow -- the window is open"

yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
closed = mkappt(f"{yesterday} 14:00", created=f"{yesterday} 01:00")
assert not refs.is_open(closed), "yesterday's 5:00 deadline has passed"

# --- a cancelled booking doesn't accept a reference
cancelled = mkappt(f"{tomorrow} 16:00", created=f"{today} 12:00")
db.set_appointment_status(cancelled["id"], "cancelled")
assert not refs.is_open(db.get_appointment(cancelled["id"]))

# --- the media limit is enforced (including against a concurrent album)
aid = a["id"]
for i in range(5):
    assert db.add_ref(aid, f"file_{i}", "photo", 5), f"photo {i} should be added"
assert not db.add_ref(aid, "file_6", "photo", 5), "a sixth photo shouldn't get through"
assert db.refs_count(aid) == 5
print("media limit:", db.refs_count(aid), "/ 5 ✅")

# --- comment and summary
db.set_ref_comment(aid, "milky french tips")
a = db.get_appointment(aid)
assert refs.has_ref(a)
print("summary:", refs.summary(a))
assert refs.summary(a) == "5 media · «milky french tips»"

# --- the delayed master notification isn't duplicated
db.schedule_ref_notify(aid)
db.schedule_ref_notify(aid)
n = db.conn().execute(
    "SELECT COUNT(*) c FROM notifications WHERE appointment_id=? AND type='refnew' AND sent=0",
    (aid,)).fetchone()["c"]
assert n == 1, f"there should be exactly one pending notification, not {n}"
db.mark_ref_notified(aid)
n = db.conn().execute(
    "SELECT COUNT(*) c FROM notifications WHERE appointment_id=? AND type='refnew' AND sent=0",
    (aid,)).fetchone()["c"]
assert n == 0, "nothing should remain pending after it's sent"
print("notification dedup OK ✅")

# --- statuses
db.set_ref_status(aid, "ok")
assert db.get_appointment(aid)["ref_status"] == "ok"
db.set_ref_status(aid, "no_materials")
assert db.get_appointment(aid)["ref_status"] == "no_materials"

# --- deleting resets the media, the comment, and the status
db.clear_refs(aid)
a = db.get_appointment(aid)
assert db.refs_count(aid) == 0 and not a["ref_comment"] and not a["ref_status"]
assert not refs.has_ref(a)
print("reference deletion OK ✅")

# --- the reference reminder doesn't fire if a reference was already sent
a2 = mkappt(f"{tomorrow} 18:00", created=f"{today} 10:00")
assert scheduler._build_message("refremind", a2) is not None, "no reference -- remind"
db.add_ref(a2["id"], "f", "photo", 5)
a2 = db.get_appointment(a2["id"])
assert scheduler._build_message("refremind", a2) is None, "reference exists -- don't remind"
print("reference reminder OK ✅")

# --- chat messages
db.add_message(a2["id"], "client", "running 10 minutes late")
db.add_message(a2["id"], "master", "no problem")
assert db.messages_count(a2["id"]) == 2

print()
print("=== OLD DATABASE MIGRATION ===")
# An old database without the ref_comment/ref_status columns: data must survive
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
importlib.reload(config)   # db.conn() reads the path from here
assert config.DB_PATH == OLD
db._conn = None
db.init_db()
row = db.conn().execute("SELECT * FROM appointments WHERE id=1").fetchone()
print("old row after migration:", dict(row))
assert row["starts_at"] == "2026-07-20 10:00", "data must not be lost"
assert row["ref_comment"] == "" and row["ref_status"] == "", "the new columns should appear"
db.init_db()  # running it again shouldn't fail
print("migration OK ✅")

print()
print("ALL REFERENCE CHECKS PASSED ✅")
