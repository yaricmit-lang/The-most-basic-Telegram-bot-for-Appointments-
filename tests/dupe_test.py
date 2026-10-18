"""Duplicate notifications: single-instance locking + atomic claiming."""
import os
import tempfile
import sqlite3
import subprocess
import sys

DB = os.path.join(tempfile.gettempdir(), "dupe.db")
BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["DB_PATH"] = DB
if os.path.exists(DB):
    os.remove(DB)
sys.path.insert(0, BOT_DIR)

import db
import config
config.ADMIN_IDS = frozenset()  # isolate tests from the developer's real .env

db.init_db()
mid = db.add_master("Test Master", 111111111)
sid = db.add_service("Manicure", 120, "€40", False, 1, 25)
cid = db.create_client("Test Client", "+79995550001", tg_id=999)
aid = db.create_appointment(cid, sid, mid, "2026-08-20 11:00", "2026-08-20 13:00")

print("=== 1. Atomic notification claiming ===")
db.add_notification(aid, "refnew", "2026-01-01 00:00")
nid = db.due_notifications()[0]["id"]

# Two PROCESSES with their own connections see the same unsent row -- exactly
# the situation that used to send the master a duplicate.
c2 = sqlite3.connect(DB)
c2.row_factory = sqlite3.Row
both_see = c2.execute(
    "SELECT COUNT(*) n FROM notifications WHERE id=? AND sent=0", (nid,)
).fetchone()["n"]
assert both_see == 1, "the second process also sees the notification as unsent"
print("  both processes see the row as unsent -- just like the original bug")

first = db.claim_notification(nid)          # process A claims it
cur = c2.execute("UPDATE notifications SET sent=1 WHERE id=? AND sent=0", (nid,))
c2.commit()
second = cur.rowcount == 1                  # process B tries to claim it
print(f"  process A claimed it: {first} | process B claimed it: {second}")
assert first is True, "the first one should claim the notification"
assert second is False, "the second one must NOT claim it -- or the master gets a duplicate"
c2.close()
print("  a duplicate is impossible ✅")

print()
print("=== 2. Single-instance locking ===")
import time

# a separate lock file, so this doesn't interfere with a live bot
LOCK = os.path.join(tempfile.gettempdir(), "test.lock")
if os.path.exists(LOCK):
    os.remove(LOCK)
env = dict(os.environ, DB_PATH=DB)
PY = f"{BOT_DIR}/.venv/bin/python"
grab = f"import bot; bot.LOCK_PATH = {LOCK!r}; print('LOCK:', bot.acquire_single_instance())"

# the first process holds the lock while it's alive
holder = subprocess.Popen([PY, "-c", grab + "; import time; time.sleep(4)"],
                          cwd=BOT_DIR, env=env, stdout=subprocess.PIPE, text=True)
time.sleep(2)

second = subprocess.run([PY, "-c", grab], cwd=BOT_DIR, env=env,
                        capture_output=True, text=True)
print("  second instance ->", second.stdout.strip())
assert "LOCK: False" in second.stdout, "a second instance must not get the lock"

# the live bot's PID must not get wiped by a failed attempt
lock_content = open(LOCK).read().strip()
print(f"  PID in the lock file after the failed attempt: '{lock_content}'")
assert lock_content == str(holder.pid), "the live bot's PID should remain in the file"
print("  lock holds, PID intact ✅")

holder.wait()
# once the holder dies, the lock must release itself
after = subprocess.run([PY, "-c", grab], cwd=BOT_DIR, env=env,
                       capture_output=True, text=True)
print("  after the bot stops ->", after.stdout.strip())
assert "LOCK: True" in after.stdout, "the lock should release once the process dies"
print("  the lock releases itself ✅")

print()
print("ALL DUPLICATE-NOTIFICATION CHECKS PASSED ✅")
