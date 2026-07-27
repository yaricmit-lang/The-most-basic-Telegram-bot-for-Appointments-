"""Тест напоминания «проверь материалы» в конце рабочего дня."""
import asyncio
import os
import sys
from datetime import datetime, timedelta

DB = "/private/tmp/claude-501/-Users-admin-Claude/f8db589b-7a41-413d-a71d-7e652b630980/scratchpad/eod.db"
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
sid = db.add_service("Маникюр", 120, "40 евро", False, 1, 25)
cid = db.create_client("Test Client", "+79995550001", tg_id=999)

today = datetime.now().strftime("%Y-%m-%d")
tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
weekday = datetime.now().weekday()


class FakeBot:
    """Ловим, что и кому бот отправил."""
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
# рабочий день закончился только что -> мы внутри окна отправки.
# Всего минуту назад (а не час), чтобы вычитание не проваливалось на вчера
# около полуночи и не давало ложный "конец дня" из будущего.
ended = (now - timedelta(minutes=1)).strftime("%H:%M")
# рабочий день ещё идёт
not_ended = (now + timedelta(hours=2)).strftime("%H:%M")


async def main():
    # 1) есть референс на завтра без ответа -> напоминаем
    reset(work_end=ended)
    a1 = mk(f"{tomorrow} 11:00")
    bot = FakeBot()
    await scheduler._maybe_eod_check(bot)
    assert bot.sent, "должно прийти напоминание про материалы"
    assert "Проверьте материалы" in bot.sent[0][1]
    assert bot.sent[0][0] == 111111111, "напоминание должно уйти мастеру"
    print("1. референс без ответа ->", bot.sent[0][1].splitlines()[0], "✅")

    # 2) повторно в тот же день не дублируем
    bot = FakeBot()
    await scheduler._maybe_eod_check(bot)
    assert not bot.sent, "второй раз за день напоминать нельзя"
    print("2. без дублей в тот же день ✅")

    # 3) рабочий день ещё не кончился -> молчим
    reset(work_end=not_ended)
    bot = FakeBot()
    await scheduler._maybe_eod_check(bot)
    assert not bot.sent, "до конца рабочего дня напоминать рано"
    print("3. рабочий день идёт — молчим ✅")

    # 4) мастер ответил на все референсы -> напоминать не о чем
    reset(work_end=ended)
    db.set_ref_status(a1, "ok")
    bot = FakeBot()
    await scheduler._maybe_eod_check(bot)
    assert not bot.sent, "на все референсы отвечено — напоминание лишнее"
    print("4. на все ответили — молчим ✅")

    # 5) «Ответить позже» не ставит статус -> референс снова попадает в напоминание
    reset(work_end=ended)
    db.set_ref_status(a1, "")     # ровно это делает кнопка «Ответить позже»
    bot = FakeBot()
    await scheduler._maybe_eod_check(bot)
    assert bot.sent, "отложенный референс должен попасть в напоминание"
    print("5. отложенный референс попадает в напоминание ✅")

    # 6) записи на завтра без референса вообще -> не напоминаем (проверять нечего)
    reset(work_end=ended)
    db.set_ref_status(a1, "ok")
    mk(f"{tomorrow} 15:00", with_ref=False)
    bot = FakeBot()
    await scheduler._maybe_eod_check(bot)
    assert not bot.sent, "без референсов проверять нечего"
    print("6. записи без референса — молчим ✅")

    # 7) референс на послезавтра не считается — только завтрашние
    reset(work_end=ended)
    later = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
    mk(f"{later} 11:00")
    bot = FakeBot()
    await scheduler._maybe_eod_check(bot)
    assert not bot.sent, "послезавтрашние референсы ещё не горят"
    print("7. только завтрашние записи ✅")

    # 8) отменённая запись не попадает в напоминание
    reset(work_end=ended)
    a_c = mk(f"{tomorrow} 17:00")
    db.set_appointment_status(a_c, "cancelled")
    bot = FakeBot()
    await scheduler._maybe_eod_check(bot)
    assert not bot.sent, "отменённая запись не должна напоминать о материалах"
    print("8. отменённая запись игнорируется ✅")

    # 9) выходной: work_end в расписании нет -> резервное время 20:00
    reset(day_off=True)
    mk(f"{tomorrow} 11:00")
    bot = FakeBot()
    await scheduler._maybe_eod_check(bot)
    expected = datetime.now().hour >= 20 and datetime.now().hour < 23
    assert bool(bot.sent) == expected, (
        f"в выходной ждём отправку только после 20:00 (сейчас {datetime.now().hour}:00)")
    print(f"9. выходной -> резерв 20:00, сейчас {datetime.now().hour}:00, "
          f"отправка={'да' if bot.sent else 'нет'} ✅")

    # 10) бот простоял всю ночь: рабочий день кончился 6 часов назад -> не будим ночью
    reset(work_end=(now - timedelta(hours=6)).strftime("%H:%M"))
    bot = FakeBot()
    await scheduler._maybe_eod_check(bot)
    assert not bot.sent, "окно 3 часа: старое напоминание не должно прилетать ночью"
    print("10. просрочено на 6 ч — не будим ✅")

    print()
    print("ВСЕ ПРОВЕРКИ КОНЦА РАБОЧЕГО ДНЯ ПРОШЛИ ✅")


asyncio.run(main())
