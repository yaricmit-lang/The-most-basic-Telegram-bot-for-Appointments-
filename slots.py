"""Free-slot calculation: working hours, lunch break, buffer between sessions."""
from datetime import datetime

import db


def to_min(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def to_hhmm(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def get_work_window(master_id, date_str):
    """(work_start, work_end, break_start, break_end) or None if the day is off."""
    ovr = db.get_override(master_id, date_str)
    if ovr:
        if ovr["day_off"] or not ovr["work_start"]:
            return None
        return ovr["work_start"], ovr["work_end"], ovr["break_start"], ovr["break_end"]
    weekday = datetime.strptime(date_str, "%Y-%m-%d").weekday()
    tpl = db.get_template(master_id, weekday)
    if not tpl or not tpl["work_start"]:
        return None
    return tpl["work_start"], tpl["work_end"], tpl["break_start"], tpl["break_end"]


def busy_intervals(master_id, date_str):
    """Busy intervals in minutes since midnight (appointments + group sessions)."""
    res = []
    for a in db.appointments_for_master_date(master_id, date_str):
        res.append((to_min(a["starts_at"][11:16]), to_min(a["ends_at"][11:16])))
    for g in db.group_sessions_for_master_date(master_id, date_str):
        start = to_min(g["starts_at"][11:16])
        res.append((start, start + g["duration_min"]))
    return res


def free_slots(master_id, date_str, duration_min):
    win = get_work_window(master_id, date_str)
    if not win:
        return []
    ws, we, bs, be = win
    buffer_min = db.get_int("buffer_min", 15)
    step = db.get_int("slot_step", 30)
    lead = db.get_int("min_lead_min", 60)

    start_min, end_min = to_min(ws), to_min(we)
    lunch = (to_min(bs), to_min(be)) if bs and be else None
    busy = busy_intervals(master_id, date_str)

    now = datetime.now()
    min_start = -1
    if now.strftime("%Y-%m-%d") == date_str:
        min_start = now.hour * 60 + now.minute + lead

    slots = []
    t = start_min
    while t + duration_min <= end_min:
        slot_end = t + duration_min
        ok = t >= min_start
        if ok and lunch and t < lunch[1] and lunch[0] < slot_end:
            ok = False
        if ok:
            for b0, b1 in busy:
                if t < b1 + buffer_min and b0 < slot_end + buffer_min:
                    ok = False
                    break
        if ok:
            slots.append(to_hhmm(t))
        t += step
    return slots


def conflict_exists(master_id, date_str, time_hhmm, duration_min):
    """Whether an arbitrary time (entered by the owner) overlaps busy intervals."""
    buffer_min = db.get_int("buffer_min", 15)
    t = to_min(time_hhmm)
    e = t + duration_min
    for b0, b1 in busy_intervals(master_id, date_str):
        if t < b1 + buffer_min and b0 < e + buffer_min:
            return True
    return False
