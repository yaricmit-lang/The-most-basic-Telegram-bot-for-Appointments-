"""Reference-photo acceptance window.

Rule: accepted until 5:00 AM on the day of the appointment (hour is configurable) —
the master needs time to prepare materials. But if the client booked AFTER that
moment (same-day booking), the deadline would already be in the past and nothing
could ever be sent — so for such appointments the window stays open until the visit
starts, and the bot warns that the master may not have time to prepare.
"""
import db

MEDIA_TYPES = ("photo", "video", "video_note", "animation")


def window(appt):
    """(deadline 'YYYY-MM-DD HH:MM', late) — cutoff for accepting a reference."""
    hour = db.get_int("ref_deadline_hour", 5)
    deadline = f"{appt['starts_at'][:10]} {hour:02d}:00"
    created = (appt["created_at"] or "")[:16]
    if created and created > deadline:
        # booked after the deadline already — otherwise sending would be impossible
        return appt["starts_at"], True
    # visit is earlier than the deadline (early slot) — window can't outlive the visit
    return min(deadline, appt["starts_at"]), False


def is_open(appt):
    if appt["status"] not in ("created", "confirmed"):
        return False
    deadline, _ = window(appt)
    return db.now_str() < deadline


def has_ref(appt):
    return db.refs_count(appt["id"]) > 0 or bool(appt["ref_comment"])


def summary(appt):
    """Short reference summary line for the appointment card."""
    n = db.refs_count(appt["id"])
    bits = []
    if n:
        bits.append(f"{n} media")
    if appt["ref_comment"]:
        bits.append(f"«{appt['ref_comment']}»")
    return " · ".join(bits) if bits else ""


def extract_media(message):
    """(file_id, media_type) from a message, or None."""
    if message.photo:
        return message.photo[-1].file_id, "photo"
    if message.video:
        return message.video.file_id, "video"
    if message.video_note:
        return message.video_note.file_id, "video_note"
    if message.animation:
        return message.animation.file_id, "animation"
    return None
