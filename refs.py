"""Окно приёма референса.

Правило: принимаем до 5:00 утра дня записи (час настраивается) — мастеру нужно
время подготовить материалы. Но если клиент записался уже ПОСЛЕ этого момента
(запись в тот же день), дедлайн был бы в прошлом и прислать было бы нельзя вообще —
поэтому для таких записей окно открыто до начала визита, а бот предупреждает,
что мастер может не успеть подготовиться.
"""
import db

MEDIA_TYPES = ("photo", "video", "video_note", "animation")


def window(appt):
    """(дедлайн 'YYYY-MM-DD HH:MM', late) — до какого момента принимаем референс."""
    hour = db.get_int("ref_deadline_hour", 5)
    deadline = f"{appt['starts_at'][:10]} {hour:02d}:00"
    created = (appt["created_at"] or "")[:16]
    if created and created > deadline:
        # записались уже после дедлайна — иначе прислать было бы нельзя вообще
        return appt["starts_at"], True
    # визит раньше дедлайна (ранний слот) — окно не может пережить начало визита
    return min(deadline, appt["starts_at"]), False


def is_open(appt):
    if appt["status"] not in ("created", "confirmed"):
        return False
    deadline, _ = window(appt)
    return db.now_str() < deadline


def has_ref(appt):
    return db.refs_count(appt["id"]) > 0 or bool(appt["ref_comment"])


def summary(appt):
    """Короткая строка о референсе для карточки записи."""
    n = db.refs_count(appt["id"])
    bits = []
    if n:
        bits.append(f"{n} медиа")
    if appt["ref_comment"]:
        bits.append(f"«{appt['ref_comment']}»")
    return " · ".join(bits) if bits else ""


def extract_media(message):
    """(file_id, media_type) из сообщения или None."""
    if message.photo:
        return message.photo[-1].file_id, "photo"
    if message.video:
        return message.video.file_id, "video"
    if message.video_note:
        return message.video_note.file_id, "video_note"
    if message.animation:
        return message.animation.file_id, "animation"
    return None
