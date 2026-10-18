# Appointment Booking Bot

[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![aiogram](https://img.shields.io/badge/aiogram-3.x-2CA5E0)](https://docs.aiogram.dev/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A Telegram bot for booking appointments — nails, haircuts, personal training,
or any other service that runs on a schedule. Built with Python, [aiogram
3](https://docs.aiogram.dev/) and SQLite; everything lives in a single local
database file, with no external services required.

## Features

**For clients**
- Booking flow: service → master → day → time. Name and phone are asked
  **once** (phone via a "Share my number" button); the Telegram profile is
  picked up automatically.
- **"Book again"** — the primary flow for returning clients: on `/start`
  they immediately see a "Repeat: service, master" card and only need to
  pick a new date and time. The full menu is one tap away, under "Other
  service".
- **Nail extensions**: after choosing a style, the bot asks how many nails
  to extend (0–10). Each nail adds to the price and duration according to
  that style's rate (e.g. medium length: +€5 and +15 min; long: +€10 and
  +20 min). The schedule, buffer, and price are all computed from the
  stretched duration; rates are configurable per service.
- **Design references** — photo/video (up to 5) plus a comment, sent either
  right when booking or later from "My bookings". Accepted until **5:00 AM
  on the day of the visit** (the hour is configurable) so the master has
  time to prepare. A same-day booking made after that deadline keeps the
  window open until the visit starts, with a warning that the master might
  not have time to prepare.
- **Chat with the master** through the bot: the client can write freely
  ("running 10 minutes late") or reply to a message from the master.
- Cancel a booking from "My bookings".
- Waitlist: if a day is fully booked, "Notify me if it opens up." When
  someone cancels, the bot automatically pings everyone waiting for that day.
- Group sessions with an automatically enforced capacity limit.

**Automatic notifications** (survive a bot restart — they live in the database)
- A booking is created → confirmation to the client + notification to the master.
- A booking created manually by the admin → the client is notified too.
- 24 hours ahead → a confirmation request ("I'll be there" / "Can't make it").
- 2 hours ahead → a reminder.
- Cancellation → the other side is notified + the waitlist is triggered.
- A client sends a reference → the master gets the media plus a client card
  with "All set" / "No materials" / "Message client" / "Answer later"
  buttons. An album is batched into a single send (with a ~1-minute delay),
  so 5 photos don't turn into 5 separate notifications.
- **End of the working day** (time taken from today's schedule; 20:00 if
  today is a day off) → a reminder to the master to check materials, if any
  of tomorrow's bookings still have an unanswered reference. References
  postponed with "Answer later" are included too.
- The evening before, at 20:00 → a reminder to the client to send a
  reference, if they haven't yet.
- Every morning at 8:00 → a digest for the master: today's bookings,
  references, and what to prepare.
- After the visit → a rating request (1–5) and a review, both forwarded to
  the master.
- After N days (21 by default) → a "time for a refresh" nudge with a "Book
  again" button.

**For the owner** (`/admin`)
- Bookings for today/tomorrow/the week, cancel any booking.
- Manual booking (search an existing client by name/phone, or create a new
  one). If that person later opens the bot themselves, their history
  merges by phone number.
- Services: title, duration, price, an **example gallery** (up to 10
  photos), days until the "time for a refresh" nudge. The first photo is
  the cover; the client sees it when choosing a style and can open the full
  gallery with "Show examples" — a clear way to show why different options
  have different durations and prices.
- Masters: name + Telegram ID for personal notifications.
- Weekly schedule as plain text per day: `10:00-19:00 lunch 14:00-15:00` or
  `day off`.
- Date-specific exceptions (vacation, a short day).
- Group sessions: create, list participants, cancel with a notification to
  everyone.
- Reply to a client directly from a notification: "No materials" prefills a
  ready-made message that can be sent as-is or edited.
- Settings: buffer between sessions (15 min), time-grid step (30 min),
  minimum lead time before a booking, booking horizon, the reference
  deadline hour (5:00), the photo/video limit (5), the digest hour (8:00),
  the reference-reminder hour (20:00), and the name clients see for the
  master.

## Setup

```bash
cd appointment-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Open `.env` and fill in:

- `BOT_TOKEN` — the token from @BotFather. **Never share it** — if it leaks
  (e.g. in a screenshot), hit Revoke in BotFather and paste in a new one.
- `ADMIN_IDS` — your Telegram ID (find it via @userinfobot). Multiple IDs
  can be comma-separated.
- `BOT_LANG` — `en` or `ru`. Every message the bot sends to clients and the
  master is routed through `i18n.py`; this setting picks which language is
  used. Defaults to `en`.

Run it:

```bash
.venv/bin/python bot.py
```

## First-time setup (2 minutes)

1. Open the bot and send `/admin`.
2. "Services" → add your services.
3. "Schedule" → set your working hours per day.
4. Done — clients can now book.

## Notes

- Time is the local time of the machine running the bot.
- The database is a single file, `booking.db`, next to the bot — back it up
  by copying that file.
- Reference photos and videos aren't stored on disk: only the Telegram
  `file_id` is kept in the database, and the actual files live on
  Telegram's servers. As a consequence, if the bot's token is ever changed,
  old references will stop opening (bookings and all other data are
  unaffected).
- To keep the bot running continuously, deploy it on a server or a small
  always-on machine and set it up to start automatically (`launchd` on
  macOS, `systemd` on Linux). Example systemd unit:

```ini
[Unit]
Description=Appointment bot
After=network.target

[Service]
WorkingDirectory=/opt/appointment-bot
ExecStart=/opt/appointment-bot/.venv/bin/python bot.py
Restart=always

[Install]
WantedBy=multi-user.target
```

## Internationalization

Every string a client or the master sees in Telegram is defined once per
language in `i18n.py` and looked up through `t("key", **kwargs)`. The active
language comes from `BOT_LANG` in `.env` (`en` or `ru`). Code, comments, and
this document are always in English, regardless of `BOT_LANG` — that setting
only affects what end users read in the chat.

To add a new language, add a third dict to `STRINGS` in `i18n.py` with the
same keys as `"en"`, then allow that language code wherever `config.BOT_LANG`
is checked.

## Testing

The test suite exercises the booking logic, scheduling, notifications, and
admin-input parsing directly, without a real Telegram connection:

```bash
./run_tests.sh
```

Each file in `tests/` can also be run on its own, e.g. `.venv/bin/python
tests/smoke_test.py`.

## Project layout

```
bot.py              Entry point: starts the bot and the notification worker
config.py           Environment configuration (token, admin IDs, DB path, language)
db.py               SQLite schema and all queries
i18n.py             Bot-facing text in every supported language
ui.py               Formatting and keyboard helpers
slots.py            Free-slot computation (working hours, lunch, buffer)
refs.py             The reference-upload window
notify.py           Outgoing notifications to clients and the master
scheduler.py        Background loop for scheduled notifications
handlers/client.py  Client-facing conversation flows
handlers/admin.py   Owner-panel conversation flows
tests/              Standalone test scripts (see Testing above)
```

## License

[MIT](LICENSE)
