# WaterHeater

Smart water-heater controller: an ESP32-C2 relay board (firmware in `src/`)
plus a PC-hosted Telegram bot with a local web panel (`telegrambot/`).

- **Firmware** (`src/main.cpp`, PlatformIO): two relays, physical button,
  timers, daily schedules, usage history in flash, a web page at
  `http://waterheater.local/` and a REST API (`/state`, `/set`, `/timer/set`,
  `/sched/*`, `/usage/get`, `/sys/get`). Build with `pio run -t upload`
  (see `platformio.ini`; the `-march` link fix is in `fix_link_march.py`).
- **Telegram bot + web panel** (`telegrambot/`): heater on/off, timers,
  schedules, history, graphs, alerts and a daily summary. Logs every heating
  cycle locally to `telegrambot/logs/`. See `telegrambot/README.md`.

Local-only files that are never committed: `telegrambot/.env` (bot token,
device URL, chat IDs) and `telegrambot/logs/` (cycle history and event log).
