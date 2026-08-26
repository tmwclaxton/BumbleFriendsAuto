# Bumble Friends ADB auto-swiper

Mac-side Python loop that drives your Android phone over ADB and swipes Bumble Friends cards with human-like delays.

**Warning:** Automating Bumble violates their terms of service and can get your account banned. Use only on a phone and account you own. This project does **not** solve verification, paywalls, or anti-bot checks — it stops when those appear.

## Requirements

- macOS (or Linux) with Python 3.10+
- Android phone with **USB debugging** enabled
  - On Xiaomi / HyperOS also enable **USB debugging (Security settings)**
- [`adb`](https://developer.android.com/tools/adb): `brew install android-platform-tools`
- Bumble installed, logged in, **Friends / BFF** tab showing a profile card
- Phone unlocked, screen on (stay-awake while charging recommended)

> This project targets the separate **Bumble For Friends** package `com.bumblebff.app`
> (not the dating app `com.bumble.app`). If you start on Chats, the swiper taps **People** first.

## Setup

```bash
cd BumbleFriendsAuto
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml   # optional local overrides
```

Plug in the phone and accept the debugging prompt:

```bash
adb devices
```

You should see your device listed as `device` (not `unauthorized`).

## Workflow

1. Open Bumble → Friends and leave a swipeable card on screen.
2. Dump the UI once so you can tune swipe coordinates if needed:

```bash
python -m src.dump_ui
```

Artifacts land in `dumps/` (XML hierarchy + screenshot).

3. Run a short session:

```bash
python -m src.swiper
python -m src.swiper --like-ratio 0.8 --max-swipes 25
```

Ctrl+C stops immediately.

### Message New friends

Opens only the **New friends** circles on the Chats tab (people who haven’t started a chat). Skips if the chat isn’t empty. Sends:

> Hi {name}, I'm putting together a wee group for hiking / board games / sports. Does that sound like something you would be interested in?

```bash
python -m src.messenger --dry-run    # list only — does not send
python -m src.messenger              # actually send
python -m src.messenger --max-messages 5
```

### Chat inbox (SQLite)

People and where each chat is up to live in `data/friends.db` (gitignored). Sync from the Chats list, then query:

```bash
python -m src.sync_chats --full   # open every chat, save full transcripts
python -m src.store               # everyone + status
python -m src.store needs-reply   # Your turn / last message from them
```

Status is `needs_reply`, `waiting`, `expired`, or `unknown`. Sending an opener with `src.messenger` also records that person as waiting.

### Inbox dashboard

Local browser UI over `friends.db`. Pick a chat, read the thread, type a reply. **Send** opens that chat on the phone and actually sends.

```bash
python -m src.dashboard
# http://127.0.0.1:8765
python -m src.dashboard --stop
```

It detaches from the terminal, restarts if it crashes, and reloads Python after `src/` changes. HTML/JS updates apply on refresh without a restart. Logs: `data/dashboard.log`.

Keep the phone unlocked and on USB. Send and Refresh go on a queue so you can stack several replies (same person or others) while the phone works through them one at a time.

### Useful flags

| Flag | Meaning |
|------|---------|
| `--max-swipes N` | Session cap (default 30) |
| `--like-ratio 0.0–1.0` | Chance of liking each card (default 1.0 = always like) |
| `--delay-min` / `--delay-max` | Seconds between swipes |
| `--serial` | ADB serial if multiple devices |
| `--no-foreground` | Do not launch/resume Bumble at start |
| `--config path` | Custom YAML config |
| `--dry-run` | Messenger only: list targets, send nothing |
| `--max-messages N` | Messenger only: cap openers |

## Behavior

- Default: always **like** (swipe right), with jittered path and 2.5–7s delays
- **Before each decision:** looks at the main photo, taps through gallery photos, scrolls the bio/prompts with reading pauses, then swipes
- Match popup: try to dismiss and continue; stop if it cannot
- **Hard stop** on: paywall / out of likes, photo verification, empty stack, permission dialog, not Bumble, or unknown UI
- Messenger: New friends circles only; skips non-empty chats; no overnight unattended runs

## Project layout

```
src/config.py      # YAML + defaults
src/device.py      # ADB / uiautomator2 connect + dump
src/screen.py      # Screen classification from hierarchy text
src/browse.py      # Look through photos + scroll bio before deciding
src/gestures.py    # Jittered like/pass swipes
src/swiper.py      # Main swipe loop
src/chats.py       # New friends parsing + empty-chat checks
src/messenger.py   # Send template openers to New friends
src/store.py       # SQLite people + chat progress
src/sync_chats.py  # Scan Chats list into the db
src/dashboard.py   # Local web inbox + send-on-phone
src/dump_ui.py     # One-shot UI dump
config.example.yaml
```

## Troubleshooting

- **`unauthorized` in `adb devices`**: unlock phone and re-accept the RSA prompt.
- **Swipes miss the card**: edit `swipe:` fractions in `config.yaml` (they are relative to screen size). Use `dump_ui` screenshots to eyeball the card center.
- **Always `stop:unknown`**: Bumble’s copy may differ by locale; dump the XML and extend the patterns in `src/screen.py`, or keep a Friends card fully visible before starting.
- **uiautomator2 first run**: it may install a helper APK on the phone; keep the screen unlocked until that finishes.
