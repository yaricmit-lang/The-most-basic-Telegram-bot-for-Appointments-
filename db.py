"""SQLite: схема и все запросы."""
import sqlite3
from datetime import datetime, timedelta

import config

_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER UNIQUE,
    username TEXT,
    name TEXT,
    phone TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS masters(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    tg_id INTEGER,
    active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS services(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    duration_min INTEGER NOT NULL,
    price TEXT DEFAULT '',
    is_group INTEGER DEFAULT 0,
    capacity INTEGER DEFAULT 1,
    comeback_days INTEGER DEFAULT 21,
    photo_id TEXT DEFAULT '',
    ext_price_per_nail INTEGER DEFAULT 0,
    ext_min_per_nail INTEGER DEFAULT 0,
    active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS schedule_templates(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    master_id INTEGER NOT NULL,
    weekday INTEGER NOT NULL,
    work_start TEXT, work_end TEXT,
    break_start TEXT, break_end TEXT,
    UNIQUE(master_id, weekday)
);
CREATE TABLE IF NOT EXISTS schedule_overrides(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    master_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    day_off INTEGER DEFAULT 0,
    work_start TEXT, work_end TEXT,
    break_start TEXT, break_end TEXT,
    UNIQUE(master_id, date)
);
CREATE TABLE IF NOT EXISTS group_sessions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id INTEGER NOT NULL,
    master_id INTEGER NOT NULL,
    starts_at TEXT NOT NULL,
    capacity INTEGER NOT NULL,
    status TEXT DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS appointments(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    service_id INTEGER NOT NULL,
    master_id INTEGER NOT NULL,
    group_session_id INTEGER,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    status TEXT DEFAULT 'created',
    created_by TEXT DEFAULT 'client',
    ext_nails INTEGER DEFAULT 0,
    ext_price INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS waitlist(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    master_id INTEGER NOT NULL,
    service_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    notified INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS notifications(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    due_at TEXT NOT NULL,
    sent INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS feedback(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id INTEGER UNIQUE,
    rating INTEGER,
    comment TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS refs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id INTEGER NOT NULL,
    file_id TEXT NOT NULL,
    media_type TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id INTEGER NOT NULL,
    sender TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS service_photos(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id INTEGER NOT NULL,
    file_id TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_refs_appt ON refs(appointment_id);
CREATE INDEX IF NOT EXISTS idx_msg_appt ON messages(appointment_id);
CREATE INDEX IF NOT EXISTS idx_svcph_service ON service_photos(service_id);
CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

DEFAULT_SETTINGS = {
    "buffer_min": "15",       # перерыв между сеансами
    "slot_step": "30",        # шаг сетки слотов
    "min_lead_min": "60",     # за сколько минут до слота ещё можно записаться
    "horizon_days": "14",     # на сколько дней вперёд открыта запись
    "ref_deadline_hour": "5",  # референс принимается до 5:00 дня записи
    "ref_max": "5",           # сколько фото/видео можно прислать
    "digest_hour": "8",       # во сколько мастеру приходит сводка на день
    "ref_remind_hour": "20",  # во сколько накануне напомнить про референс
}


def conn():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


def _migrate():
    """Досоздаём колонки в уже существующей базе (данные не теряются)."""
    c = conn()
    cols = {r["name"] for r in c.execute("PRAGMA table_info(appointments)")}
    for col in ("ref_comment", "ref_status"):
        if col not in cols:
            c.execute(f"ALTER TABLE appointments ADD COLUMN {col} TEXT DEFAULT ''")
    for col in ("ext_nails", "ext_price"):
        if col not in cols:
            c.execute(f"ALTER TABLE appointments ADD COLUMN {col} INTEGER DEFAULT 0")
    cols = {r["name"] for r in c.execute("PRAGMA table_info(services)")}
    if "photo_id" not in cols:
        c.execute("ALTER TABLE services ADD COLUMN photo_id TEXT DEFAULT ''")
    if "ext_price_per_nail" not in cols:
        c.execute("ALTER TABLE services ADD COLUMN ext_price_per_nail INTEGER DEFAULT 0")
        c.execute("ALTER TABLE services ADD COLUMN ext_min_per_nail INTEGER DEFAULT 0")
        # первичные тарифы наращивания по названию вида (мастер меняет в панели):
        # средняя длина — 5 €/15 мин за ноготь, длинная — 10 €/20 мин.
        # Сопоставление в Python: lower() в SQLite не понижает кириллицу.
        for r in c.execute("SELECT id, title FROM services").fetchall():
            t = (r["title"] or "").lower()
            if "средн" in t or "medium" in t:
                c.execute("UPDATE services SET ext_price_per_nail=5, ext_min_per_nail=15 "
                          "WHERE id=?", (r["id"],))
            elif "длинн" in t or "long" in t:
                c.execute("UPDATE services SET ext_price_per_nail=10, ext_min_per_nail=20 "
                          "WHERE id=?", (r["id"],))
    # одиночное фото услуги переехало в галерею примеров: переносим и гасим колонку
    for r in c.execute("SELECT id, photo_id FROM services WHERE photo_id != ''").fetchall():
        dup = c.execute(
            "SELECT 1 FROM service_photos WHERE service_id=? AND file_id=?",
            (r["id"], r["photo_id"]),
        ).fetchone()
        if not dup:
            c.execute("INSERT INTO service_photos(service_id, file_id) VALUES(?,?)",
                      (r["id"], r["photo_id"]))
    c.execute("UPDATE services SET photo_id='' WHERE photo_id != ''")
    # Защита от повторов: на одну запись — не более одного уведомления каждого
    # типа (кроме refnew, который законно повторяется, когда клиент дошлёт референс).
    # Сначала чистим уже накопившиеся дубли, оставляя самую раннюю строку.
    c.execute("""
        DELETE FROM notifications WHERE type != 'refnew' AND id NOT IN (
            SELECT MIN(id) FROM notifications WHERE type != 'refnew'
            GROUP BY appointment_id, type
        )
    """)
    c.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_notif_once
        ON notifications(appointment_id, type) WHERE type != 'refnew'
    """)
    c.commit()


def init_db():
    c = conn()
    c.executescript(SCHEMA)
    _migrate()
    for k, v in DEFAULT_SETTINGS.items():
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
    c.commit()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ---------- settings ----------

def get_setting(key, default=""):
    r = conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def get_int(key, default):
    try:
        return int(get_setting(key, str(default)))
    except ValueError:
        return default


def set_setting(key, value):
    conn().execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn().commit()


# ---------- clients ----------

def norm_phone(raw):
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    return "+" + digits if digits else ""


def upsert_client(tg_id, username):
    c = conn()
    r = c.execute("SELECT * FROM clients WHERE tg_id=?", (tg_id,)).fetchone()
    if r:
        if username != r["username"]:
            c.execute("UPDATE clients SET username=? WHERE id=?", (username, r["id"]))
            c.commit()
    else:
        c.execute("INSERT INTO clients(tg_id, username) VALUES(?,?)", (tg_id, username))
        c.commit()
    return get_client_by_tg(tg_id)


def get_client_by_tg(tg_id):
    return conn().execute("SELECT * FROM clients WHERE tg_id=?", (tg_id,)).fetchone()


def get_client(cid):
    return conn().execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()


def set_client_name(cid, name):
    conn().execute("UPDATE clients SET name=? WHERE id=?", (name, cid))
    conn().commit()


def set_client_phone(cid, phone):
    conn().execute("UPDATE clients SET phone=? WHERE id=?", (phone, cid))
    conn().commit()


def create_client(name, phone, tg_id=None):
    cur = conn().execute(
        "INSERT INTO clients(name, phone, tg_id) VALUES(?,?,?)", (name, phone, tg_id)
    )
    conn().commit()
    return cur.lastrowid


def merge_offline_client(client_id, phone):
    """Если админ раньше завёл клиента вручную (без Telegram) с тем же номером —
    переносим его историю на пришедший Telegram-аккаунт."""
    c = conn()
    row = c.execute(
        "SELECT * FROM clients WHERE phone=? AND tg_id IS NULL AND id != ?",
        (phone, client_id),
    ).fetchone()
    if not row:
        return
    c.execute("UPDATE appointments SET client_id=? WHERE client_id=?", (client_id, row["id"]))
    c.execute("UPDATE waitlist SET client_id=? WHERE client_id=?", (client_id, row["id"]))
    cur = c.execute("SELECT name FROM clients WHERE id=?", (client_id,)).fetchone()
    if not cur["name"] and row["name"]:
        c.execute("UPDATE clients SET name=? WHERE id=?", (row["name"], client_id))
    c.execute("DELETE FROM clients WHERE id=?", (row["id"],))
    c.commit()


def search_clients(query, limit=10):
    q = query.strip()
    digits = "".join(ch for ch in q if ch.isdigit())
    if len(digits) >= 5:
        pat = "%" + "%".join(digits[-10:]) + "%"
        rows = conn().execute(
            "SELECT * FROM clients WHERE phone LIKE ? ORDER BY id DESC LIMIT ?",
            (pat, limit),
        ).fetchall()
    else:
        rows = conn().execute(
            "SELECT * FROM clients WHERE name LIKE ? OR username LIKE ? ORDER BY id DESC LIMIT ?",
            (f"%{q}%", f"%{q}%", limit),
        ).fetchall()
    return rows


# ---------- services ----------

def list_services(only_active=True):
    sql = "SELECT * FROM services"
    if only_active:
        sql += " WHERE active=1"
    return conn().execute(sql + " ORDER BY id").fetchall()


def get_service(sid):
    return conn().execute("SELECT * FROM services WHERE id=?", (sid,)).fetchone()


def add_service(title, duration_min, price, is_group, capacity, comeback_days):
    cur = conn().execute(
        "INSERT INTO services(title,duration_min,price,is_group,capacity,comeback_days) "
        "VALUES(?,?,?,?,?,?)",
        (title, duration_min, price, int(is_group), capacity, comeback_days),
    )
    conn().commit()
    return cur.lastrowid


def update_service_field(sid, field, value):
    assert field in ("title", "duration_min", "price", "capacity", "comeback_days",
                     "photo_id", "ext_price_per_nail", "ext_min_per_nail")
    conn().execute(f"UPDATE services SET {field}=? WHERE id=?", (value, sid))
    conn().commit()


def effective_duration(svc, nails):
    """Длительность сеанса с учётом наращивания."""
    return svc["duration_min"] + (nails or 0) * (svc["ext_min_per_nail"] or 0)


def ext_extra_price(svc, nails):
    """Доплата за наращивание, €."""
    return (nails or 0) * (svc["ext_price_per_nail"] or 0)


def toggle_service(sid):
    conn().execute("UPDATE services SET active=1-active WHERE id=?", (sid,))
    conn().commit()


# ---------- примеры работ (галерея услуги) ----------

SVC_PHOTO_MAX = 10  # предел альбома в Telegram


def add_service_photo(sid, file_id):
    """False — если галерея уже заполнена. Без await внутри, поэтому альбом
    от мастера не может пробить лимит."""
    c = conn()
    n = c.execute(
        "SELECT COUNT(*) AS n FROM service_photos WHERE service_id=?", (sid,)
    ).fetchone()["n"]
    if n >= SVC_PHOTO_MAX:
        return False
    c.execute("INSERT INTO service_photos(service_id, file_id) VALUES(?,?)", (sid, file_id))
    c.commit()
    return True


def service_photos(sid):
    return conn().execute(
        "SELECT * FROM service_photos WHERE service_id=? ORDER BY id", (sid,)
    ).fetchall()


def get_service_photo(pid):
    return conn().execute("SELECT * FROM service_photos WHERE id=?", (pid,)).fetchone()


def service_photos_count(sid):
    return conn().execute(
        "SELECT COUNT(*) AS n FROM service_photos WHERE service_id=?", (sid,)
    ).fetchone()["n"]


def service_cover(sid):
    """Первое фото галереи — обложка вида."""
    r = conn().execute(
        "SELECT file_id FROM service_photos WHERE service_id=? ORDER BY id LIMIT 1", (sid,)
    ).fetchone()
    return r["file_id"] if r else ""


def delete_service_photo(pid):
    conn().execute("DELETE FROM service_photos WHERE id=?", (pid,))
    conn().commit()


def services_with_photos():
    """id услуг, у которых есть примеры — одним запросом, без цикла по услугам."""
    return {r["service_id"]
            for r in conn().execute("SELECT DISTINCT service_id FROM service_photos")}


# ---------- masters ----------

def list_masters(only_active=True):
    sql = "SELECT * FROM masters"
    if only_active:
        sql += " WHERE active=1"
    return conn().execute(sql + " ORDER BY id").fetchall()


def owner_master():
    """Единственный мастер = владелец."""
    return conn().execute("SELECT * FROM masters ORDER BY id LIMIT 1").fetchone()


def ensure_owner_master(admin_ids):
    """Гарантируем ровно одного мастера, привязанного к владельцу-админу."""
    admin_id = min(admin_ids) if admin_ids else None
    masters = list_masters(only_active=False)
    if not masters:
        add_master("Мастер", admin_id)
    elif admin_id and masters[0]["tg_id"] is None:
        update_master_field(masters[0]["id"], "tg_id", admin_id)


def get_master(mid):
    return conn().execute("SELECT * FROM masters WHERE id=?", (mid,)).fetchone()


def add_master(name, tg_id=None):
    cur = conn().execute("INSERT INTO masters(name, tg_id) VALUES(?,?)", (name, tg_id))
    conn().commit()
    return cur.lastrowid


def update_master_field(mid, field, value):
    assert field in ("name", "tg_id")
    conn().execute(f"UPDATE masters SET {field}=? WHERE id=?", (value, mid))
    conn().commit()


def toggle_master(mid):
    conn().execute("UPDATE masters SET active=1-active WHERE id=?", (mid,))
    conn().commit()


# ---------- schedule ----------

def get_template(mid, weekday):
    return conn().execute(
        "SELECT * FROM schedule_templates WHERE master_id=? AND weekday=?", (mid, weekday)
    ).fetchone()


def set_template(mid, weekday, ws, we, bs, be):
    conn().execute(
        "INSERT INTO schedule_templates(master_id,weekday,work_start,work_end,break_start,break_end) "
        "VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(master_id,weekday) DO UPDATE SET "
        "work_start=excluded.work_start, work_end=excluded.work_end, "
        "break_start=excluded.break_start, break_end=excluded.break_end",
        (mid, weekday, ws, we, bs, be),
    )
    conn().commit()


def get_override(mid, date):
    return conn().execute(
        "SELECT * FROM schedule_overrides WHERE master_id=? AND date=?", (mid, date)
    ).fetchone()


def set_override(mid, date, day_off, ws, we, bs, be):
    conn().execute(
        "INSERT INTO schedule_overrides(master_id,date,day_off,work_start,work_end,break_start,break_end) "
        "VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(master_id,date) DO UPDATE SET "
        "day_off=excluded.day_off, work_start=excluded.work_start, work_end=excluded.work_end, "
        "break_start=excluded.break_start, break_end=excluded.break_end",
        (mid, date, int(day_off), ws, we, bs, be),
    )
    conn().commit()


def delete_override(mid, date):
    conn().execute("DELETE FROM schedule_overrides WHERE master_id=? AND date=?", (mid, date))
    conn().commit()


def list_overrides(mid, from_date):
    return conn().execute(
        "SELECT * FROM schedule_overrides WHERE master_id=? AND date>=? ORDER BY date",
        (mid, from_date),
    ).fetchall()


# ---------- appointments ----------

_APPT_SQL = """
SELECT a.*,
       c.name AS client_name, c.phone AS client_phone,
       c.tg_id AS client_tg, c.username AS client_username,
       s.title AS service_title, s.duration_min, s.price, s.is_group,
       m.name AS master_name, m.tg_id AS master_tg
FROM appointments a
JOIN clients c ON c.id = a.client_id
JOIN services s ON s.id = a.service_id
JOIN masters m ON m.id = a.master_id
"""


def create_appointment(client_id, service_id, master_id, starts_at, ends_at,
                       created_by="client", group_session_id=None,
                       ext_nails=0, ext_price=0):
    cur = conn().execute(
        "INSERT INTO appointments(client_id,service_id,master_id,starts_at,ends_at,"
        "created_by,group_session_id,ext_nails,ext_price) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (client_id, service_id, master_id, starts_at, ends_at, created_by,
         group_session_id, ext_nails, ext_price),
    )
    conn().commit()
    return cur.lastrowid


def get_appointment(aid):
    return conn().execute(_APPT_SQL + " WHERE a.id=?", (aid,)).fetchone()


def set_appointment_status(aid, status):
    conn().execute("UPDATE appointments SET status=? WHERE id=?", (status, aid))
    conn().commit()


def transition_status(aid, from_statuses, to_status):
    """Атомарный переход статуса. True — если перевели именно мы.

    aiogram обрабатывает апдейты параллельно, поэтому два быстрых тапа по «Отменить»
    или «Подтвердить» могут гонкой пройти проверку статуса. Один UPDATE ... WHERE
    status IN (...) гарантирует, что уведомление уйдёт ровно один раз.
    """
    placeholders = ",".join("?" * len(from_statuses))
    c = conn()
    cur = c.execute(
        f"UPDATE appointments SET status=? WHERE id=? AND status IN ({placeholders})",
        (to_status, aid, *from_statuses),
    )
    c.commit()
    return cur.rowcount == 1


def appointments_for_master_date(mid, date):
    return conn().execute(
        _APPT_SQL + " WHERE a.master_id=? AND a.starts_at LIKE ? "
        "AND a.status IN ('created','confirmed') ORDER BY a.starts_at",
        (mid, date + "%"),
    ).fetchall()


def appointments_between(from_s, to_s):
    return conn().execute(
        _APPT_SQL + " WHERE a.starts_at >= ? AND a.starts_at < ? "
        "AND a.status IN ('created','confirmed') ORDER BY a.starts_at",
        (from_s, to_s),
    ).fetchall()


def upcoming_for_client(client_id):
    return conn().execute(
        _APPT_SQL + " WHERE a.client_id=? AND a.status IN ('created','confirmed') "
        "AND a.starts_at >= ? ORDER BY a.starts_at",
        (client_id, now_str()),
    ).fetchall()


def client_has_upcoming(client_id):
    r = conn().execute(
        "SELECT 1 FROM appointments WHERE client_id=? AND status IN ('created','confirmed') "
        "AND starts_at >= ? LIMIT 1",
        (client_id, now_str()),
    ).fetchone()
    return r is not None


def last_for_repeat(client_id):
    """Последняя не отменённая запись — основа кнопки «Записаться снова»."""
    return conn().execute(
        _APPT_SQL + " WHERE a.client_id=? AND a.status != 'cancelled' "
        "ORDER BY a.starts_at DESC LIMIT 1",
        (client_id,),
    ).fetchone()


def to_complete():
    return conn().execute(
        _APPT_SQL + " WHERE a.status IN ('created','confirmed') AND a.ends_at <= ?",
        (now_str(),),
    ).fetchall()


def client_in_session(client_id, gs_id):
    r = conn().execute(
        "SELECT 1 FROM appointments WHERE client_id=? AND group_session_id=? "
        "AND status IN ('created','confirmed') LIMIT 1",
        (client_id, gs_id),
    ).fetchone()
    return r is not None


def appointments_of_session(gs_id):
    return conn().execute(
        _APPT_SQL + " WHERE a.group_session_id=? AND a.status IN ('created','confirmed') "
        "ORDER BY a.created_at",
        (gs_id,),
    ).fetchall()


# ---------- group sessions ----------

_GS_SQL = """
SELECT g.*, s.title AS service_title, s.duration_min, s.price,
       m.name AS master_name, m.tg_id AS master_tg,
       (SELECT COUNT(*) FROM appointments a
        WHERE a.group_session_id = g.id AND a.status IN ('created','confirmed')) AS booked
FROM group_sessions g
JOIN services s ON s.id = g.service_id
JOIN masters m ON m.id = g.master_id
"""


def create_group_session(service_id, master_id, starts_at, capacity):
    cur = conn().execute(
        "INSERT INTO group_sessions(service_id,master_id,starts_at,capacity) VALUES(?,?,?,?)",
        (service_id, master_id, starts_at, capacity),
    )
    conn().commit()
    return cur.lastrowid


def get_group_session(gid):
    return conn().execute(_GS_SQL + " WHERE g.id=?", (gid,)).fetchone()


def upcoming_group_sessions(service_id=None):
    sql = _GS_SQL + " WHERE g.status='active' AND g.starts_at >= ?"
    args = [now_str()]
    if service_id:
        sql += " AND g.service_id=?"
        args.append(service_id)
    return conn().execute(sql + " ORDER BY g.starts_at", args).fetchall()


def group_sessions_for_master_date(mid, date):
    return conn().execute(
        _GS_SQL + " WHERE g.master_id=? AND g.starts_at LIKE ? AND g.status='active'",
        (mid, date + "%"),
    ).fetchall()


def set_group_session_status(gid, status):
    conn().execute("UPDATE group_sessions SET status=? WHERE id=?", (status, gid))
    conn().commit()


# ---------- waitlist ----------

def add_waitlist(client_id, master_id, service_id, date):
    c = conn()
    r = c.execute(
        "SELECT 1 FROM waitlist WHERE client_id=? AND master_id=? AND date=? AND notified=0",
        (client_id, master_id, date),
    ).fetchone()
    if r:
        return False
    c.execute(
        "INSERT INTO waitlist(client_id,master_id,service_id,date) VALUES(?,?,?,?)",
        (client_id, master_id, service_id, date),
    )
    c.commit()
    return True


def waitlist_for(master_id, date):
    return conn().execute(
        "SELECT w.*, c.tg_id, c.name AS client_name FROM waitlist w "
        "JOIN clients c ON c.id = w.client_id "
        "WHERE w.master_id=? AND w.date=? AND w.notified=0",
        (master_id, date),
    ).fetchall()


def mark_waitlist_notified(wid):
    conn().execute("UPDATE waitlist SET notified=1 WHERE id=?", (wid,))
    conn().commit()


# ---------- notifications ----------

def add_notification(appt_id, typ, due_at):
    # OR IGNORE + уникальный индекс idx_notif_once: повторный вызов для той же
    # записи и типа (двойной тап, переотправка апдейта) не создаёт второй строки.
    conn().execute(
        "INSERT OR IGNORE INTO notifications(appointment_id,type,due_at) VALUES(?,?,?)",
        (appt_id, typ, due_at),
    )
    conn().commit()


def due_notifications():
    return conn().execute(
        "SELECT * FROM notifications WHERE sent=0 AND due_at <= ? ORDER BY due_at",
        (now_str(),),
    ).fetchall()


def mark_notification_sent(nid):
    conn().execute("UPDATE notifications SET sent=1 WHERE id=?", (nid,))
    conn().commit()


def claim_notification(nid):
    """True — если уведомление забрали именно мы.

    Пометка и проверка идут одним UPDATE ... WHERE sent=0, поэтому даже два
    процесса не смогут отправить одно уведомление дважды.
    """
    c = conn()
    cur = c.execute("UPDATE notifications SET sent=1 WHERE id=? AND sent=0", (nid,))
    c.commit()
    return cur.rowcount == 1


def drop_pending_notifications(appt_id):
    conn().execute("DELETE FROM notifications WHERE appointment_id=? AND sent=0", (appt_id,))
    conn().commit()


# ---------- референсы ----------

def add_ref(appt_id, file_id, media_type, limit):
    """Кладём медиа в референс. False — если лимит уже выбран.

    Проверка и вставка идут без await, поэтому альбом из нескольких фото
    (aiogram обрабатывает их параллельно) не может пробить лимит.
    """
    c = conn()
    n = c.execute(
        "SELECT COUNT(*) AS n FROM refs WHERE appointment_id=?", (appt_id,)
    ).fetchone()["n"]
    if n >= limit:
        return False
    c.execute(
        "INSERT INTO refs(appointment_id, file_id, media_type) VALUES(?,?,?)",
        (appt_id, file_id, media_type),
    )
    c.commit()
    return True


def refs_for(appt_id):
    return conn().execute(
        "SELECT * FROM refs WHERE appointment_id=? ORDER BY id", (appt_id,)
    ).fetchall()


def refs_count(appt_id):
    return conn().execute(
        "SELECT COUNT(*) AS n FROM refs WHERE appointment_id=?", (appt_id,)
    ).fetchone()["n"]


def clear_refs(appt_id):
    c = conn()
    c.execute("DELETE FROM refs WHERE appointment_id=?", (appt_id,))
    c.execute("UPDATE appointments SET ref_comment='', ref_status='' WHERE id=?", (appt_id,))
    c.commit()


def set_ref_comment(appt_id, text):
    conn().execute("UPDATE appointments SET ref_comment=? WHERE id=?", (text, appt_id))
    conn().commit()


def set_ref_status(appt_id, status):
    conn().execute("UPDATE appointments SET ref_status=? WHERE id=?", (status, appt_id))
    conn().commit()


def set_ref_status_from(appt_id, status, from_statuses):
    """Атомарно меняет статус референса. True — если поменяли именно мы.

    Клиенту «мастер подтвердил» шлём только на реальном переходе, поэтому два
    быстрых тапа по «Всё есть» не отправят клиенту два одинаковых сообщения.
    """
    placeholders = ",".join("?" * len(from_statuses))
    c = conn()
    cur = c.execute(
        f"UPDATE appointments SET ref_status=? WHERE id=? AND ref_status IN ({placeholders})",
        (status, appt_id, *from_statuses),
    )
    c.commit()
    return cur.rowcount == 1


def schedule_ref_notify(appt_id):
    """Отложенное уведомление мастеру — чтобы альбом ушёл одним пакетом."""
    c = conn()
    r = c.execute(
        "SELECT 1 FROM notifications WHERE appointment_id=? AND type='refnew' AND sent=0",
        (appt_id,),
    ).fetchone()
    if r:
        return
    due = (datetime.now() + timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M")
    c.execute(
        "INSERT INTO notifications(appointment_id,type,due_at) VALUES(?,'refnew',?)",
        (appt_id, due),
    )
    c.commit()


def mark_ref_notified(appt_id):
    conn().execute(
        "UPDATE notifications SET sent=1 WHERE appointment_id=? AND type='refnew' AND sent=0",
        (appt_id,),
    )
    conn().commit()


# ---------- переписка ----------

def add_message(appt_id, sender, text):
    conn().execute(
        "INSERT INTO messages(appointment_id, sender, text) VALUES(?,?,?)",
        (appt_id, sender, text),
    )
    conn().commit()


def messages_count(appt_id):
    return conn().execute(
        "SELECT COUNT(*) AS n FROM messages WHERE appointment_id=?", (appt_id,)
    ).fetchone()["n"]


# ---------- feedback ----------

def save_rating(appt_id, rating):
    """True — если оценку поставили впервые (мастеру шлём только тогда).

    appointment_id в feedback уникален, поэтому первый INSERT проходит, а повторный
    тап (кнопки-звёзды остаются на старом сообщении) обновляет оценку, но возвращает
    False — второго уведомления мастеру не будет.
    """
    c = conn()
    existed = c.execute(
        "SELECT rating FROM feedback WHERE appointment_id=?", (appt_id,)
    ).fetchone()
    c.execute(
        "INSERT INTO feedback(appointment_id,rating) VALUES(?,?) "
        "ON CONFLICT(appointment_id) DO UPDATE SET rating=excluded.rating",
        (appt_id, rating),
    )
    c.commit()
    return existed is None or existed["rating"] is None


def save_comment(appt_id, comment):
    conn().execute(
        "UPDATE feedback SET comment=? WHERE appointment_id=?", (comment, appt_id)
    )
    conn().commit()
