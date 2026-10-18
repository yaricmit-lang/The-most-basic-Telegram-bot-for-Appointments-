"""Smoke test of the core logic without Telegram: db, slots, buffer, lunch,
groups, notifications."""
import os
import tempfile
import sys
from datetime import datetime, timedelta

os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "test.db")
if os.path.exists(os.environ["DB_PATH"]):
    os.remove(os.environ["DB_PATH"])
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import config
config.ADMIN_IDS = frozenset()  # isolate tests from the developer's real .env
import slots
import scheduler
from handlers.admin import parse_schedule_text, parse_date, parse_datetime, parse_time

db.init_db()

# --- setup: a master, a service, Mon-Sun 10:00-19:00 with lunch 14:00-15:00
mid = db.add_master("Test Master", tg_id=111)
sid = db.add_service("Manicure + coating", 120, "€30", False, 1, 21)
for wd in range(7):
    db.set_template(mid, wd, "10:00", "19:00", "14:00", "15:00")

tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

free = slots.free_slots(mid, tomorrow, 120)
print("free slots (empty day):", free)
assert "10:00" in free
assert "13:00" not in free, "13:00-15:00 overlaps lunch"
assert "14:00" not in free and "14:30" not in free, "lunch is busy"
assert "15:00" in free
assert "17:00" in free and "17:30" not in free, "17:30+120 > 19:00"

# --- a 10:00-12:00 booking: the 15-min buffer should remove 12:00; 12:30 can't
# work either (lunch at 14:00) -> 12:15 isn't on the 30-min grid anyway
cid = db.create_client("Test Client", "+79990001122", tg_id=222)
aid = db.create_appointment(cid, sid, mid, f"{tomorrow} 10:00", f"{tomorrow} 12:00")
free2 = slots.free_slots(mid, tomorrow, 120)
print("free slots (after the 10-12 booking):", free2)
assert "10:00" not in free2 and "11:00" not in free2
assert "12:00" not in free2, "15-min buffer after the session"
assert "15:00" in free2

# conflict check for an admin's arbitrary time
assert slots.conflict_exists(mid, tomorrow, "11:30", 120)
assert slots.conflict_exists(mid, tomorrow, "12:10", 120), "inside the buffer"
assert not slots.conflict_exists(mid, tomorrow, "12:15", 120)

# --- 24h, 2h, and the reference reminder the night before
far = (datetime.now() + timedelta(hours=30)).strftime("%Y-%m-%d %H:%M")
scheduler.schedule_for_appointment(aid, far)
rows = db.conn().execute("SELECT type FROM notifications WHERE appointment_id=?", (aid,)).fetchall()
types = sorted(r["type"] for r in rows)
print("notifications:", types)
assert types == ["confirm24", "refremind", "remind2"]

# cancelling clears pending notifications
db.drop_pending_notifications(aid)
assert not db.conn().execute("SELECT 1 FROM notifications WHERE appointment_id=?", (aid,)).fetchone()

# --- override: tomorrow is a day off
db.set_override(mid, tomorrow, True, None, None, None, None)
assert slots.free_slots(mid, tomorrow, 120) == []
db.delete_override(mid, tomorrow)
assert slots.free_slots(mid, tomorrow, 120)

# --- group session: spot limit
gsid = db.create_group_session(sid, mid, f"{tomorrow} 18:00", 2)
g = db.get_group_session(gsid)
assert g["booked"] == 0
db.create_appointment(cid, sid, mid, f"{tomorrow} 18:00", f"{tomorrow} 20:00", group_session_id=gsid)
cid2 = db.create_client("Second Client", "+79990003344")
db.create_appointment(cid2, sid, mid, f"{tomorrow} 18:00", f"{tomorrow} 20:00", group_session_id=gsid)
g = db.get_group_session(gsid)
print("group:", g["booked"], "/", g["capacity"])
assert g["booked"] == 2 and g["booked"] >= g["capacity"], "no spots left"
assert db.client_in_session(cid, gsid)

# --- waitlist
assert db.add_waitlist(cid2, mid, sid, tomorrow)
assert not db.add_waitlist(cid2, mid, sid, tomorrow), "a duplicate isn't created"
wl = db.waitlist_for(mid, tomorrow)
assert len(wl) == 1
db.mark_waitlist_notified(wl[0]["id"])
assert not db.waitlist_for(mid, tomorrow)

# --- merging an offline client (created by the admin) with the Telegram account that arrives
off_id = db.create_client("Offline Client", "+79995556677")
db.create_appointment(off_id, sid, mid, f"{tomorrow} 15:00", f"{tomorrow} 17:00", created_by="admin")
tg_cl = db.upsert_client(333, "katya")
db.set_client_phone(tg_cl["id"], "+79995556677")
db.merge_offline_client(tg_cl["id"], "+79995556677")
assert db.get_client(off_id) is None, "the offline duplicate is deleted"
assert db.upcoming_for_client(tg_cl["id"]), "the booking moved to the Telegram account"
assert db.get_client(tg_cl["id"])["name"] == "Offline Client"

# --- "repeat"
last = db.last_for_repeat(cid)
assert last is not None and last["service_id"] == sid

# --- admin-panel parsers
assert parse_schedule_text("10:00-19:00 lunch 14:00-15:00") == {
    "day_off": False, "ws": "10:00", "we": "19:00", "bs": "14:00", "be": "15:00"}
assert parse_schedule_text("9:30-18.00")["ws"] == "09:30"
assert parse_schedule_text("day off")["day_off"]
assert parse_schedule_text("nonsense") is None
assert parse_schedule_text("19:00-10:00") is None, "end before start"
d = parse_date("25.07")
assert d and d.endswith("-07-25")
assert parse_date("31.02") is None
dt = parse_datetime("25.07 18:00")
assert dt and dt.endswith(" 18:00")
assert parse_time("9.05") == "09:05"
assert parse_time("25:00") is None

# --- completing a visit -> feedback + comeback
past = (datetime.now() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")
past_end = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
aid2 = db.create_appointment(cid, sid, mid, past, past_end)
scheduler._complete_finished()
a2 = db.get_appointment(aid2)
assert a2["status"] == "completed"
nt = sorted(r["type"] for r in db.conn().execute(
    "SELECT type FROM notifications WHERE appointment_id=?", (aid2,)).fetchall())
print("after completion:", nt)
assert nt == ["comeback", "feedback"]
cb_due = db.conn().execute(
    "SELECT due_at FROM notifications WHERE appointment_id=? AND type='comeback'", (aid2,)).fetchone()
expected = (datetime.now() + timedelta(days=21)).strftime("%Y-%m-%d")
assert cb_due["due_at"] == f"{expected} 12:00"

print("\nALL CHECKS PASSED ✅")
