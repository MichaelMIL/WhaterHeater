# Water Heater Telegram Bot (PC-hosted)

A standalone Telegram bot that runs on any PC and controls the water heater
(via the WaterHeater relay board's REST API). Presents itself as **Water Heater**:
heater on/off, timers, an inline-button panel — and it **logs every power
on/off cycle and all activity** to disk. (Per-relay control and polarity
config are intentionally not exposed here; use the device's web page for
those.)

A background monitor polls the device every 5 seconds, so heating cycles
started from the web page, the physical button, the timer or the device's
built-in bot are logged too — not only the ones triggered from Telegram.

## Logs (written to `./logs`, override with `LOG_DIR`)

- **`events.jsonl`** — everything, one JSON object per line: every command
  and button press (with chat + user), every power change (with timer
  value), device offline/online transitions, bot start/stop.
- **`cycles.csv`** — completed power-on cycles per circuit (`relay1`,
  `relay2`, and `heater` = any relay on) with on/off timestamps and
  duration in seconds. Opens directly in Excel/Numbers.

`/history` in Telegram shows today's total heating time and the last 10
cycles from this log.

### Gap recovery

The device keeps its **own** history in flash — the last 32 heating cycles
and 14 daily totals — so runs are recorded even when this bot is down
(including button presses, the usual way it gets switched on). On startup
and every 10 minutes the bot pulls `/usage/get` and merges anything missing
into `cycles.csv`, matching duplicates by start time.

That covers gaps up to 32 cycles / 14 days. Beyond that the device's ring
buffer wraps, so still run the bot as a service (see below) — and
`/history` and `/graph` warn when the newest cycle is over a day old, so a
logging gap is never mistaken for an idle heater.

## Setup

1. Create a bot: message [@BotFather](https://t.me/BotFather) → `/newbot`
   (name it e.g. "Water Heater") → copy the token.

   > **Important:** Telegram allows only ONE poller per bot token. Either
   > untick **"Telegram bot enabled"** on the device's settings page, or
   > create a *separate* bot for this script. Running both on the same
   > token causes `409 Conflict` errors.

2. Configure:

   ```bash
   cd telegrambot
   cp .env.example .env
   # edit .env: BOT_TOKEN, DEVICE_URL, ALLOWED_CHAT_IDS
   ```

3. Install & run:

   ```bash
   python3 -m pip install -r requirements.txt
   python3 bot.py
   ```

   The bot prints the web panel URL on start (default
   <http://localhost:4245/>).

4. Message the bot `/start`. Send `/chatid`, put the number into
   `ALLOWED_CHAT_IDS` in `.env`, and restart the bot so only you can
   control the heater.

## Commands

| Command | Effect |
|---|---|
| `/menu` | Control panel with live inline buttons |
| `/status` | Heater state, time heating, timer left, heated-today total |
| `/history` | Today's heating total + last 10 cycles |
| `/graph` | Chart image: heating per day + by hour of day, last 14 days |
| `/on` | Heat for 30 minutes |
| `/off` | Heater off (cancels a running timer) |
| `/timer N` | Heat for N minutes (1–720) |
| `/once` | One-time run wizard: hour → minutes → duration (or direct: `/once HH:MM MIN`) |
| `/once off` | Cancel the pending one-time run |
| `/settings` | Schedule wizard: tap a slot → hour → minutes → duration |
| `/setsched` | Same wizard (or direct: `/setsched N HH:MM MIN`) |
| `/delsched N` | Disable schedule slot N |
| `/chatid` | Show this chat's ID |

The `/menu` panel: 🔥 Heater ON (30m) / Heater OFF, Heat 15m / 60m,
📊 History, 📈 Graph, 🔄 refresh — it updates in place after every press,
and the panel text shows how long the heater has been on today.

`/graph` sends one PNG (rendered with matplotlib from `cycles.csv`) with two
panels: heating time per day, and heating by hour of day — both over the
last 14 days. Cycles are split across the midnight/hour boundaries they
cross, and the currently running cycle is included. The caption names the
busiest hour.

## Local web panel

`bot.py` also starts a small web server (stdlib only, no extra packages)
with the **same buttons and settings as the Telegram menu**, on three
pages. Open <http://localhost:4245/> on the bot PC, or
`http://<bot-pc-ip>:4245/` from a phone on the same network.

- **Control** (`/`): live status (heating / timer left / heated today) and
  the panel buttons — Heater ON (30m), Heater OFF, Heat 15 min, Heat 60
  min, custom minutes (like `/timer N`) — plus a **Heat once** card: pick a
  time and duration and the heater runs one time at the next occurrence of
  that time, then the entry clears.
- **History** (`/history`): today's total and cycle count, the last 10
  cycles (the running one is marked live), and the 14-day usage graph.
- **Settings** (`/settings`): device info (timezone, device time, max
  continuous ON) and the 5 daily schedule slots, same as the `/settings`
  wizard.

Night mode follows the OS setting; the 🌙/☀️ button in the top bar
overrides it (remembered per browser).

Everything goes through the same code as the bot, so web presses count
toward the same rate limit, show up in `events.jsonl` (as `web_action` /
`web_schedule` with the client IP) and in `cycles.csv`.

| `.env` key | Default | Meaning |
|---|---|---|
| `WEB_PORT` | `4245` | Port; `0` disables the panel |
| `WEB_HOST` | `0.0.0.0` | Bind address. `0.0.0.0` = whole LAN (like the relay's own page); `127.0.0.1` = this PC only |

There is no login on the panel (same as the relay's own page) — keep it on
a trusted home network or bind it to `127.0.0.1`.

JSON API behind the page, handy for scripts / Shortcuts:

```
GET  /api/state                                  status + relay state
POST /api/action   {"action":"allon"|"alloff"|"t15"|"t60"|"timer","mins":N}
GET  /api/history                                today's total + last 10 cycles
GET  /graph.png                                  the /graph image
GET  /api/settings                               device info + schedule slots
POST /api/schedule {"slot":0-4,"en":true,"hh":6,"mm":30,"dur":45}
GET  /api/once                                   pending one-time run
POST /api/once     {"hh":17,"mm":30,"dur":45}  or  {"cancel":true}
```

## One-time scheduled run

Available everywhere: the `/once` command and the ⏰ **Heat once** menu
button in Telegram, the **Heat once** card on the web panel, the same card on
the device's own page, and `/once HH:MM N` in the device's built-in bot.
The run is stored **on the device** (`/once/set`, `/once/get`,
`/once/cancel`), so it fires even if the bot PC is off. "Next occurrence"
semantics: if the time has already passed today it is set for tomorrow. If
the device was powered off and comes back more than 15 minutes late, the
run is skipped instead of starting unexpectedly.

## Alerts & daily summary

Sent to `ALERT_CHAT_ID` (default: the first allowed chat):

- ⚠️ device unreachable for ~30 s / ✅ back online
- ⚠️ heater ON continuously longer than `ALERT_ON_HOURS` (default 3 h)
- 📅 daily summary at `SUMMARY_TIME` (default 21:00): total heating time
  and cycle count for the day

## Rate limiting

At most **10 state changes per 5 minutes** — enforced both here and in the
firmware (which also covers the web page and the physical button). Actions
that turn the heater OFF are never blocked. Event logs rotate monthly
(`events-YYYY-MM.jsonl`).

## Device REST API used

- `GET /state` → `{"relay1":bool,"relay2":bool,"timer":seconds}`
- `GET /set?relay=all&state=on|off`
- `GET /timer/set?mins=N` / `GET /timer/cancel`
- `GET /once/get`, `GET /once/set?hh=H&mm=M&dur=MIN[&day=0|1]`, `GET /once/cancel`

## Run at boot (optional)

- **Linux (systemd):**

  ```ini
  # /etc/systemd/system/waterheater-bot.service
  [Unit]
  Description=Water Heater telegram bot
  After=network-online.target

  [Service]
  WorkingDirectory=/path/to/telegrambot
  ExecStart=/usr/bin/python3 bot.py
  Restart=always
  RestartSec=5

  [Install]
  WantedBy=multi-user.target
  ```

  `sudo systemctl enable --now waterheater-bot`

- **macOS:** run it from a LaunchAgent, or simply `python3 bot.py` in a
  terminal / `tmux` session.

- **Windows:** Task Scheduler → run `pythonw bot.py` at logon.
