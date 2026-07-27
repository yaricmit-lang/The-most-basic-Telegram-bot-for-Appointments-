"""Фоновый цикл: завершение визитов и отправка отложенных уведомлений.

Все уведомления лежат в таблице notifications и переживают перезапуск бота.
Типы: confirm24 (за 24 ч, требует ответа), remind2 (за 2 ч),
feedback (после визита), comeback (приглашение вернуться через N дней).
"""
import asyncio
import logging
from datetime import datetime, timedelta

import db
import notify
import refs
import slots
from ui import btn, esc, fmt_dt, kb

log = logging.getLogger(__name__)

# Если сегодня выходной, «конца рабочего дня» в расписании нет — проверяем вечером.
EOD_FALLBACK = "20:00"


def schedule_for_appointment(appt_id, starts_at):
    st = datetime.strptime(starts_at, "%Y-%m-%d %H:%M")
    now = datetime.now()
    for typ, hours in (("confirm24", 24), ("remind2", 2)):
        due = st - timedelta(hours=hours)
        if due > now:
            db.add_notification(appt_id, typ, due.strftime("%Y-%m-%d %H:%M"))
    # напоминание прислать референс — накануне вечером, до дедлайна
    ref_due = (st - timedelta(days=1)).replace(
        hour=db.get_int("ref_remind_hour", 20), minute=0
    )
    if ref_due > now:
        db.add_notification(appt_id, "refremind", ref_due.strftime("%Y-%m-%d %H:%M"))


def _complete_finished():
    for a in db.to_complete():
        db.set_appointment_status(a["id"], "completed")
        db.add_notification(a["id"], "feedback", db.now_str())
        svc = db.get_service(a["service_id"])
        if svc and svc["comeback_days"]:
            due_date = (datetime.now() + timedelta(days=svc["comeback_days"])).strftime("%Y-%m-%d")
            db.add_notification(a["id"], "comeback", f"{due_date} 12:00")


def _build_message(typ, appt):
    """(text, markup) или None, если уведомление уже неактуально."""
    now = db.now_str()
    if typ == "confirm24":
        if appt["status"] != "created" or appt["starts_at"] <= now:
            return None
        text = (
            f"⏰ Напоминаем: <b>{fmt_dt(appt['starts_at'])}</b> — "
            f"{esc(appt['service_title'])} у мастера {esc(appt['master_name'])}.\n\n"
            f"Пожалуйста, подтвердите запись:"
        )
        markup = kb([
            [btn("✅ Подтверждаю", f"cfm:{appt['id']}")],
            [btn("❌ Не смогу прийти", f"ccl:{appt['id']}")],
        ])
        return text, markup
    if typ == "remind2":
        if appt["status"] not in ("created", "confirmed") or appt["starts_at"] <= now:
            return None
        text = (
            f"🔔 Через 2 часа ждём вас: {esc(appt['service_title'])}, "
            f"{appt['starts_at'][11:16]}. До встречи!"
        )
        return text, None
    if typ == "feedback":
        if appt["status"] != "completed":
            return None
        text = (
            f"💬 Как всё прошло? Оцените визит\n"
            f"«{esc(appt['service_title'])}» от {fmt_dt(appt['starts_at'])}:"
        )
        markup = kb([[btn(str(n), f"fb:{appt['id']}:{n}") for n in range(1, 6)]])
        return text, markup
    if typ == "refremind":
        if appt["status"] not in ("created", "confirmed") or appt["starts_at"] <= now:
            return None
        if refs.has_ref(appt) or not refs.is_open(appt):
            return None  # уже прислал или окно закрылось
        deadline, _ = refs.window(appt)
        text = (
            f"💅 Завтра ваш визит: {esc(appt['service_title'])}, "
            f"{fmt_dt(appt['starts_at'])}.\n\n"
            f"Хотите прислать пример дизайна? Мастер успеет подготовить материалы.\n"
            f"Принимаю до {fmt_dt(deadline)}."
        )
        markup = kb([
            [btn("📎 Прислать референс", f"ref:{appt['id']}")],
            [btn("Не нужно", "refskip")],
        ])
        return text, markup
    if typ == "comeback":
        if appt["status"] != "completed" or db.client_has_upcoming(appt["client_id"]):
            return None
        svc = db.get_service(appt["service_id"])
        days = svc["comeback_days"] if svc else 21
        period = f"{days // 7} недели" if days % 7 == 0 else f"{days} дней"
        text = (
            f"✨ Прошло уже {period} после «{esc(appt['service_title'])}» — "
            f"пора освежить!\n\nЗаписаться снова?"
        )
        markup = kb([
            [btn("🔁 Записаться снова", f"rpt:{appt['service_id']}:{appt['master_id']}")],
            [btn("🗂 Другая услуга", "bk")],
        ])
        return text, markup
    return None


async def _send_due(bot):
    for n in db.due_notifications():
        if not db.claim_notification(n["id"]):
            continue  # уведомление уже забрал другой процесс
        appt = db.get_appointment(n["appointment_id"])
        if not appt:
            continue
        # референс уходит мастеру, а не клиенту: пауза в минуту склеивает альбом
        if n["type"] == "refnew":
            if appt["status"] in ("created", "confirmed") and refs.has_ref(appt):
                await notify.master_reference(bot, appt)
            continue
        if not appt["client_tg"]:
            continue
        built = _build_message(n["type"], appt)
        if not built:
            continue
        text, markup = built
        try:
            await bot.send_message(appt["client_tg"], text, reply_markup=markup)
            log.info("Отправлено уведомление id=%s тип=%s запись=%s клиенту=%s",
                     n["id"], n["type"], appt["id"], appt["client_tg"])
        except Exception as e:
            log.warning("Уведомление %s клиенту %s не доставлено: %s",
                        n["type"], appt["client_tg"], e)


async def _maybe_digest(bot):
    """Сводка мастеру раз в день. Окно в 3 часа — чтобы после долгого простоя
    бота не прилетела «утренняя» сводка вечером."""
    hour = db.get_int("digest_hour", 8)
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if not (hour <= now.hour < hour + 3):
        return
    if db.get_setting("last_digest_date") == today:
        return
    db.set_setting("last_digest_date", today)
    await notify.morning_digest(bot, today)


async def _maybe_eod_check(bot):
    """В конце рабочего дня (по расписанию) напомнить проверить материалы на завтра —
    если по завтрашним записям остались референсы без ответа."""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if db.get_setting("last_eod_date") == today:
        return
    owner = db.owner_master()
    if not owner:
        return
    win = slots.get_work_window(owner["id"], today)
    end_dt = datetime.strptime(f"{today} {win[1] if win else EOD_FALLBACK}", "%Y-%m-%d %H:%M")
    # окно в 3 часа: после долгого простоя бота напоминание не прилетит среди ночи
    if not (end_dt <= now < end_dt + timedelta(hours=3)):
        return
    db.set_setting("last_eod_date", today)
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    await notify.materials_check(bot, tomorrow)


async def worker(bot):
    await asyncio.sleep(5)
    while True:
        try:
            _complete_finished()
            await _send_due(bot)
            await _maybe_digest(bot)
            await _maybe_eod_check(bot)
        except Exception:
            log.exception("Ошибка фонового цикла уведомлений")
        await asyncio.sleep(60)
