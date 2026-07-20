# Appointment Booking Bot

A Telegram bot for booking clients: nail care, haircuts, training sessions — anything.
Python + aiogram 3 + SQLite, everything stored locally in a single database file.

## What it does

**For the client**
- Booking: service → master → day → time. Name and phone are asked **only once**
  (phone via a "Share my number" button), the Telegram profile is picked up automatically.
- **"Book again"** — the core flow: a returning client sees a "Repeat: service, master"
  card right at /start and only needs to pick a date and time.
  The full menu is tucked under "Different service".
- **Nail extension**: after picking a style, the bot asks how many nails to extend
  (0–10). Each nail adds to the price and duration per the style's rate (e.g.
  medium — +5 € and +15 min, long — +10 € and +20 min). Scheduling, buffers, and
  price are computed from the extended duration; rates are set on the service card.
- **Design reference** — photo/video (up to 5) + a comment, sent right after booking or
  later from "My bookings". Accepted **until 5:00 AM on the day of the appointment**
  (hour is configurable) — so the master has time to prepare materials. If the client
  booked the same day after the deadline, the window stays open until the visit starts,
  but the bot warns that the master may not have time to prepare.
- **Messaging the master** through the bot: the client can write first ("running
  10 minutes late") or reply to the master's message.
- Cancel a booking from "My bookings".
- Waitlist: if a day is full — "Notify me if it opens up". When someone cancels,
  the bot notifies whoever's waiting.
- Group sessions with an automatic capacity limit.

**Automated notifications** (survive bot restarts — stored in the database)
- Booking created → confirmation to the client + notification to the master.
- Booking created by the owner manually → the client **still** gets notified.
- 24 hours before → confirmation request (buttons "Confirm" / "Can't make it").
- 2 hours before → reminder.
- Cancellation → notification to the other side + the waitlist kicks in.
- Client sent a reference → the master gets the media + a client card with buttons
  "✅ Got it" / "❌ No materials" / "✍️ Message client" / "⏰ Reply later".
  The album is batched into a single send (~1 minute delay), so 5 photos don't turn
  into 5 notifications.
- **At the end of the work day** (time comes from today's schedule; if today is a
  day off — 20:00) → a reminder to the master to check materials if any of
  tomorrow's bookings still have an unanswered reference. References postponed
  with "⏰ Reply later" are included in this reminder too.
- The evening before at 20:00 → a reminder to the client to send a reference, if
  they haven't already.
- Every morning at 8:00 → a digest for the master: today's bookings, references,
  and what to prepare.
- After the visit → a request to rate (1–5) and review, forwarded to the master.
- After N days (default 21) → a "time to refresh" nudge with a "Book again" button.

**For the owner** (`/admin`)
- Bookings for today/tomorrow/the week, cancel any booking.
- Manual booking for a client (search by name/phone, or add a new client). If that
  person later opens the bot themselves, their history merges by phone number.
- Services (manicure styles): name, duration, price, **example gallery** (up to 10
  photos), days until the "time to refresh" nudge. The first photo is the cover;
  the client sees it when picking a style and can open all examples with
  "🖼 Show examples" — so it's clear why different styles have different
  durations and prices.
- Masters: name + Telegram ID for personal notifications.
- Weekly schedule as plain text: `10:00-19:00 lunch 14:00-15:00` or `day off`.
- Exceptions for specific dates (vacation, a short day).
- Group sessions: create, see the participant list, cancel with everyone notified.
- Reply to the client right from the notification: "❌ No materials" pre-fills a
  ready-made message that can be sent as-is or extended in your own words.
- Settings: break between sessions (15 min), time grid step (30 min), minimum
  lead time before booking, booking horizon, reference deadline hour (5:00), photo
  limit (5), digest hour (8:00), reference-reminder hour (20:00), the master's
  name as shown to clients.

## Setup

```bash
cd appointment-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Open `.env` and fill in:

- `BOT_TOKEN` — the token from @BotFather. **Never share it with anyone**; if it
  ever leaks (e.g. shows up in a screenshot), hit Revoke in BotFather and paste a
  new one.
- `ADMIN_IDS` — your Telegram ID (find it via @userinfobot). Multiple IDs can be
  comma-separated.

Run it:

```bash
.venv/bin/python bot.py
```

## First-time setup (2 minutes)

1. Open the bot, send `/admin`.
2. "🧾 Services" → add your services.
3. "📅 Schedule" → set working hours for each day.
4. Done — clients can start booking.

## Notes

- Time is the local time of the machine running the bot.
- The database is a single file, `booking.db`, next to the bot; backing up means
  copying that file.
- Reference photos and videos aren't stored on disk: the database only holds a
  `file_id`, the actual files live on Telegram. As a result, if you ever rotate the
  bot's token, old references will stop opening (bookings and all other data are
  unaffected).
- To keep the bot running continuously, run it on a server/mini PC and set up
  autostart (launchd on macOS, systemd on Linux). Example systemd unit:

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
