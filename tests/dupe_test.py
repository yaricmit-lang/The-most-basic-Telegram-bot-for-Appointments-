"""Дубли уведомлений: блокировка второго экземпляра + атомарный захват."""
import os
import sqlite3
import subprocess
import sys

DB = "/private/tmp/claude-501/-Users-admin-Claude/f8db589b-7a41-413d-a71d-7e652b630980/scratchpad/dupe.db"
BOT_DIR = "/Users/admin/Claude/appointment-bot"
os.environ["DB_PATH"] = DB
if os.path.exists(DB):
    os.remove(DB)
sys.path.insert(0, BOT_DIR)

import db
import config
config.ADMIN_IDS = frozenset()  # isolate tests from the developer's real .env

db.init_db()
mid = db.add_master("Test Master", 111111111)
sid = db.add_service("Маникюр", 120, "40 евро", False, 1, 25)
cid = db.create_client("Test Client", "+79995550001", tg_id=999)
aid = db.create_appointment(cid, sid, mid, "2026-08-20 11:00", "2026-08-20 13:00")

print("=== 1. Атомарный захват уведомления ===")
db.add_notification(aid, "refnew", "2026-01-01 00:00")
nid = db.due_notifications()[0]["id"]

# Два ПРОЦЕССА со своими соединениями видят одну и ту же неотправленную строку —
# ровно та ситуация, что слала мастеру дубль.
c2 = sqlite3.connect(DB)
c2.row_factory = sqlite3.Row
both_see = c2.execute(
    "SELECT COUNT(*) n FROM notifications WHERE id=? AND sent=0", (nid,)
).fetchone()["n"]
assert both_see == 1, "второй процесс тоже видит уведомление неотправленным"
print("  оба процесса видят строку неотправленной — как и было при баге")

first = db.claim_notification(nid)          # процесс A забирает
cur = c2.execute("UPDATE notifications SET sent=1 WHERE id=? AND sent=0", (nid,))
c2.commit()
second = cur.rowcount == 1                  # процесс B пытается забрать
print(f"  процесс A забрал: {first} | процесс B забрал: {second}")
assert first is True, "первый должен забрать уведомление"
assert second is False, "второй НЕ должен забрать — иначе мастеру уйдёт дубль"
c2.close()
print("  дубль невозможен ✅")

print()
print("=== 2. Блокировка второго экземпляра ===")
import time

# отдельный файл блокировки, чтобы не мешать живому боту
LOCK = "/private/tmp/claude-501/-Users-admin-Claude/f8db589b-7a41-413d-a71d-7e652b630980/scratchpad/test.lock"
if os.path.exists(LOCK):
    os.remove(LOCK)
env = dict(os.environ, DB_PATH=DB)
PY = f"{BOT_DIR}/.venv/bin/python"
grab = f"import bot; bot.LOCK_PATH = {LOCK!r}; print('LOCK:', bot.acquire_single_instance())"

# первый процесс держит блокировку, пока живёт
holder = subprocess.Popen([PY, "-c", grab + "; import time; time.sleep(4)"],
                          cwd=BOT_DIR, env=env, stdout=subprocess.PIPE, text=True)
time.sleep(2)

second = subprocess.run([PY, "-c", grab], cwd=BOT_DIR, env=env,
                        capture_output=True, text=True)
print("  второй экземпляр ->", second.stdout.strip())
assert "LOCK: False" in second.stdout, "второй экземпляр не должен получить блокировку"

# PID живого бота не должен быть стёрт неудачной попыткой
lock_content = open(LOCK).read().strip()
print(f"  PID в блокировке после неудачной попытки: '{lock_content}'")
assert lock_content == str(holder.pid), "в файле должен остаться PID живого бота"
print("  блокировка держит, PID цел ✅")

holder.wait()
# после смерти держателя блокировка обязана освободиться сама
after = subprocess.run([PY, "-c", grab], cwd=BOT_DIR, env=env,
                       capture_output=True, text=True)
print("  после остановки бота ->", after.stdout.strip())
assert "LOCK: True" in after.stdout, "после смерти процесса блокировка должна сняться"
print("  блокировка снимается сама ✅")

print()
print("ВСЕ ПРОВЕРКИ ДУБЛЕЙ ПРОШЛИ ✅")
