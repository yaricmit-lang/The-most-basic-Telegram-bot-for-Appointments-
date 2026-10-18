"""Test for the "check materials" reminder at the end of the working day."""
import asyncio
import os
import tempfile
import sys
from datetime import datetime, timedelta

DB = os.path.join(tempfile.gettempdir(), "eod.db")
os.environ["DB_PATH"] = DB
if os.path.exists(DB):
    os.remove(DB)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import config
config.ADMIN_IDS = frozenset()  # isolate tests from the developer's real .env
import scheduler
from i18n import t

db.init_db()
mid = db.add_master("Test Master", 111111111)
sid = db.add_service("Manicure", 120, "€40", False, 1, 25)
cid = db.create_client("Test Client", "+79995550001", tg_id=999)

today = datetime.now().strftime("%Y-%m-%d")
tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
weekday = datetime.now().weekday()


class FakeBot:
    """Captures what the bot sent and to whom."""
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text))


def reset(work_end=None, day_off=False):
    db.conn().execute("DELETE FROM settings WHERE key='last_eod_date'")
    db.conn().commit()
    if day_off:
        db.set_template(mid, weekday, None, None, None, None)
    else:
        db.set_template(mid, weekday, "10:00", work_end, None, None)


def mk(starts, with_ref=True, status=""):
    aid = db.create_appointment(cid, sid, mid, starts, starts)
    if with_ref:
        db.add_ref(aid, "file_x", "photo", 5)
    if status:
        db.set_ref_status(aid, status)
    return aid


now = datetime.now()
# the working day just ended -> we're inside the send window.
# Just a minute ago (not an hour), so the subtraction doesn't roll over to
# yesterday around midnight and produce a false "end of day" from the future.
ended = (now - timedelta(minutes=1)).strftime("%H:%M")
# the working day is still going
not_ended = (now + timedelta(hours=2)).strftime("%H:%M")


async def main():
    # 1) there's an unanswered reference for tomorrow -> remind
    reset(work_end=ended)
    a1 = mk(f"{tomorrow} 11:00")
    bot = FakeBot()
    await scheduler._maybe_eod_check(bot)
    assert bot.sent, "a materials reminder should have been sent"
    assert t("materials_check_title") in bot.sent[0][1]
    assert bot.sent[0][0] == 111111111, "the reminder should go to the master"
    print("1. unanswered reference ->", bot.sent[0][1].splitlines()[0], "✅")

    # 2) no duplicate reminder the same day
    bot = FakeBot()
    await scheduler._maybe_eod_check(bot)
    assert not bot.sent, "must not remind a second time the same day"
    print("2. no duplicates the same day ✅")

    # 3) the working day isn't over yet -> stay quiet
    reset(work_end=not_ended)
    bot = FakeBot()
    await scheduler._maybe_eod_check(bot)
    assert not bot.sent, "too early to remind before the working day ends"
    print("3. working day still going -- quiet ✅")

    # 4) the master answered every reference -> nothing to remind about
    reset(work_end=ended)
    db.set_ref_status(a1, "ok")
    bot = FakeBot()
    await scheduler._maybe_eod_check(bot)
    assert not bot.sent, "everything answered -- no reminder needed"
    print("4. everything answered -- quiet ✅")

    # 5) "Answer later" doesn't set a status -> the reference reappears in the reminder
    reset(work_end=ended)
    db.set_ref_status(a1, "")     # this is exactly what the "Answer later" button does
    bot = FakeBot()
    await scheduler._maybe_eod_check(bot)
    assert bot.sent, "a postponed reference should reappear in the reminder"
    print("5. postponed reference reappears in the reminder ✅")

    # 6) tomorrow's booking has no reference at all -> nothing to remind (nothing to check)
    reset(work_end=ended)
    db.set_ref_status(a1, "ok")
    mk(f"{tomorrow} 15:00", with_ref=False)
    bot = FakeBot()
    await scheduler._maybe_eod_check(bot)
    assert not bot.sent, "no references means nothing to check"
    print("6. bookings without a reference -- quiet ✅")

    # 7) a reference for the day after tomorrow doesn't count -- only tomorrow's
    reset(work_end=ended)
    later = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
    mk(f"{later} 11:00")
    bot = FakeBot()
    await scheduler._maybe_eod_check(bot)
    assert not bot.sent, "references for the day after tomorrow aren't urgent yet"
    print("7. only tomorrow's bookings ✅")

    # 8) a cancelled booking doesn't trigger a reminder
    reset(work_end=ended)
    a_c = mk(f"{tomorrow} 17:00")
    db.set_appointment_status(a_c, "cancelled")
    bot = FakeBot()
    await scheduler._maybe_eod_check(bot)
    assert not bot.sent, "a cancelled booking shouldn't remind about materials"
    print("8. cancelled booking ignored ✅")

    # 9) a day off: no work_end in the schedule -> fall back to 20:00
    reset(day_off=True)
    mk(f"{tomorrow} 11:00")
    bot = FakeBot()
    await scheduler._maybe_eod_check(bot)
    expected = datetime.now().hour >= 20 and datetime.now().hour < 23
    assert bool(bot.sent) == expected, (
        f"on a day off we only expect a send after 20:00 (it's currently {datetime.now().hour}:00)")
    print(f"9. day off -> 20:00 fallback, it's {datetime.now().hour}:00, "
          f"sent={'yes' if bot.sent else 'no'} ✅")

    # 10) the bot was down overnight: the working day ended 6 hours ago -> don't wake anyone at night
    reset(work_end=(now - timedelta(hours=6)).strftime("%H:%M"))
    bot = FakeBot()
    await scheduler._maybe_eod_check(bot)
    assert not bot.sent, "the 3-hour window means a stale reminder shouldn't fire overnight"
    print("10. 6 hours overdue -- stays quiet ✅")

    print()
    print("ALL END-OF-DAY CHECKS PASSED ✅")


asyncio.run(main())
