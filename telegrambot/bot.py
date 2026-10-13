#!/usr/bin/env python3
"""Water Heater telegram bot, meant to run on a PC.

Controls the WaterHeater relay board (which switches the water heater) over its
REST API and presents itself as "Water Heater". Full control set: per-relay
switches, heater on/off, timers with an inline-button panel, status,
polarity config.

Everything is logged to ./logs (override with LOG_DIR):
  events.jsonl - every command, button press, power change, timer value,
                 device outage/recovery, bot start/stop (one JSON per line)
  cycles.csv   - completed power-on cycles per circuit (relay1, relay2 and
                 "heater" = any relay on) with on/off timestamps + duration

A background monitor polls the device every 5s, so cycles started from the
web page, the physical button, the timer or the built-in bot are logged
too - not only the ones triggered from Telegram.

Configuration via environment variables or a .env file next to this script:

  BOT_TOKEN         Telegram bot token from @BotFather (required)
  DEVICE_URL        Relay base URL, e.g. http://192.168.50.207 (required)
  ALLOWED_CHAT_IDS  Comma-separated chat IDs allowed to control the heater.
                    Empty = anyone who finds the bot (not recommended).
  LOG_DIR           Where to write logs (default: ./logs next to bot.py)
  WEB_PORT          Local web panel port (default 4245; 0 disables it)
  WEB_HOST          Bind address for the web panel (default 0.0.0.0 = LAN)

NOTE: don't run this with the same token as the device's built-in bot while
that one is enabled - Telegram allows only one getUpdates consumer per token
(the loser gets HTTP 409). Untick "Telegram bot enabled" on the device's
settings page, or create a separate bot for this script.
"""

import csv
import json
import os
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests

import webserver

# ---------- configuration ----------

def load_dotenv() -> None:
    env_file = Path(__file__).with_name(".env")
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DEVICE_URL = os.environ.get("DEVICE_URL", "").rstrip("/")
ALLOWED_CHAT_IDS = {c.strip() for c in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if c.strip()}

if not BOT_TOKEN or not DEVICE_URL:
    sys.exit("Set BOT_TOKEN and DEVICE_URL (environment or .env file). See README.md")

# Proactive notifications go to this chat (default: first allowed chat).
ALERT_CHAT_ID = os.environ.get("ALERT_CHAT_ID", "") or (sorted(ALLOWED_CHAT_IDS)[0] if ALLOWED_CHAT_IDS else "")
# Alert when the heater has been on continuously for this long (0 disables).
ALERT_ON_HOURS = float(os.environ.get("ALERT_ON_HOURS", "3"))
# Daily usage summary time, "HH:MM" local ("" disables).
SUMMARY_TIME = os.environ.get("SUMMARY_TIME", "21:00")

# Local web panel with the same buttons/settings as the Telegram menu.
# Served by webserver.py; 0 disables it. 0.0.0.0 makes it reachable from
# every device on the LAN (like the relay's own page) - no login, so use
# 127.0.0.1 if the PC sits on an untrusted network.
WEB_PORT = int(os.environ.get("WEB_PORT", "4245") or 0)
WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

HELP_TEXT = (
    "\U0001F6BF Water Heater bot\n\n"
    "/menu - control panel with buttons\n"
    "/status - heater state\n"
    "/history - recent heating cycles & today's total\n"
    "/graph - daily heating time, last 14 days\n"
    "/on - heat for 30 min\n"
    "/off - heater off\n"
    "/timer N - heat for N minutes (1-720)\n"
    "/once HH:MM N - heat once at HH:MM for N min (wizard: /once)\n"
    "/once off - cancel the one-time schedule\n"
    "/settings - daily schedules & device info\n"
    "/setsched - schedule wizard (or: /setsched N HH:MM MINUTES)\n"
    "/delsched N - disable schedule slot N\n"
    "/chatid - show this chat's ID"
)

# ---------- logging ----------

LOG_DIR = Path(os.environ.get("LOG_DIR", "") or Path(__file__).with_name("logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
CYCLES_FILE = LOG_DIR / "cycles.csv"
_log_lock = threading.Lock()

def now() -> datetime:
    return datetime.now().astimezone()

def log_event(event_type: str, **fields) -> None:
    # Monthly rotation: events go to events-YYYY-MM.jsonl.
    events_file = LOG_DIR / f"events-{now():%Y-%m}.jsonl"
    record = {"ts": now().isoformat(timespec="seconds"), "type": event_type, **fields}
    with _log_lock, events_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

def log_cycle(circuit: str, on_ts: datetime, off_ts: datetime) -> None:
    write_header = not CYCLES_FILE.exists()
    with _log_lock, CYCLES_FILE.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["circuit", "on", "off", "seconds"])
        w.writerow([circuit,
                    on_ts.isoformat(timespec="seconds"),
                    off_ts.isoformat(timespec="seconds"),
                    int((off_ts - on_ts).total_seconds())])

def fmt_duration(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"

# ---------- device REST helpers ----------

class DeviceError(Exception):
    pass

# Persistent connections: a new TLS/TCP handshake per request (and a fresh
# DNS lookup, which on broken-IPv6 networks can stall for seconds) is what
# makes button presses feel slow. Sessions are per-thread since
# requests.Session isn't guaranteed thread-safe.
_local = threading.local()

def _session() -> requests.Session:
    s = getattr(_local, "session", None)
    if s is None:
        s = _local.session = requests.Session()
    return s

# mDNS names like waterheater.local resolve slowly (multicast query + an
# AAAA-timeout wait, repeated per connection), so resolve the hostname once,
# cache the IP, and talk to the device by IP. Re-resolve only when the
# device stops answering (e.g. DHCP gave it a new address), at most every
# RESOLVE_RETRY_SECONDS so an outage doesn't spam mDNS queries.
RESOLVE_RETRY_SECONDS = 30
_resolve_lock = threading.Lock()
_resolved_base = None
_last_resolve = 0.0

def _device_base(force: bool = False) -> str:
    global _resolved_base, _last_resolve
    with _resolve_lock:
        if _resolved_base and not (force and time.time() - _last_resolve > RESOLVE_RETRY_SECONDS):
            return _resolved_base
        parts = urlsplit(DEVICE_URL)
        host = parts.hostname or ""
        _last_resolve = time.time()
        try:
            socket.inet_aton(host)          # already an IP address
            _resolved_base = DEVICE_URL
            return _resolved_base
        except OSError:
            pass
        try:
            infos = socket.getaddrinfo(host, parts.port or 80,
                                       socket.AF_INET, socket.SOCK_STREAM)
            ip = infos[0][4][0]
            netloc = ip if parts.port is None else f"{ip}:{parts.port}"
            base = f"{parts.scheme}://{netloc}"
            if base != _resolved_base:
                print(f"resolved {host} -> {ip}")
                log_event("device_resolved", host=host, ip=ip)
            _resolved_base = base
        except OSError:
            _resolved_base = DEVICE_URL     # can't resolve: use the raw URL
        return _resolved_base

# Rate limit: at most 10 state changes per 5 minutes from this bot.
# OFF actions are always allowed (never lock yourself out of switching a
# heater off) but still consume quota. The firmware enforces the same limit.
RATE_MAX, RATE_WINDOW = 10, 300
_rate_lock = threading.Lock()
_rate_stamps = []

def allow_change(turns_on: bool) -> bool:
    with _rate_lock:
        cutoff = time.time() - RATE_WINDOW
        _rate_stamps[:] = [t for t in _rate_stamps if t > cutoff]
        if turns_on and len(_rate_stamps) >= RATE_MAX:
            return False
        _rate_stamps.append(time.time())
        return True

RATE_MSG = "⏳ Rate limited: max 10 changes per 5 minutes"

def device_get(path: str, **params) -> dict:
    base = _device_base()
    try:
        r = _session().get(f"{base}{path}", params=params, timeout=6)
        if r.status_code == 429:
            raise DeviceError("rate limited by the device (10 changes / 5 min)")
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, json.JSONDecodeError) as e:
        # Maybe the device got a new DHCP address - re-resolve and retry once.
        new_base = _device_base(force=True)
        if new_base != base:
            try:
                r = _session().get(f"{new_base}{path}", params=params, timeout=6)
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, json.JSONDecodeError):
                pass
        raise DeviceError(f"heater unreachable ({e.__class__.__name__})") from e

def get_state() -> dict:
    return device_get("/state")

def set_relay(relay: str, state: str) -> dict:
    return device_get("/set", relay=relay, state=state)

def set_timer(mins: int) -> dict:
    return device_get("/timer/set", mins=mins)

def backfill_from_device() -> int:
    """Merge cycles the device recorded on its own into cycles.csv.

    The device keeps its last 32 heating cycles in flash, so runs that
    happened while this bot was down (or its monitor was wedged) are not
    lost - notably button presses, which are the usual way it is switched
    on. Duplicates are matched by start time.
    """
    try:
        cycles = device_get("/usage/get").get("cycles", [])
    except DeviceError:
        return 0
    if not cycles:
        return 0

    known = set()
    if CYCLES_FILE.exists():
        with CYCLES_FILE.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["circuit"] == "heater":
                    known.add(int(datetime.fromisoformat(r["on"]).timestamp()))

    added = []
    for c in cycles:
        start, secs = int(c.get("t", 0)), int(c.get("s", 0))
        if start <= 0 or secs <= 0:
            continue
        if any(abs(start - k) <= 5 for k in known):  # already recorded locally
            continue
        known.add(start)
        added.append((datetime.fromtimestamp(start).astimezone(),
                      datetime.fromtimestamp(start + secs).astimezone()))

    for on_ts, off_ts in sorted(added):
        log_cycle("heater", on_ts, off_ts)
    if added:
        log_event("backfill", cycles=len(added),
                  oldest=min(a[0] for a in added).isoformat(timespec="seconds"))
        print(f"backfilled {len(added)} cycle(s) from the device")
    return len(added)

def get_once() -> dict:
    return device_get("/once/get")

def set_once(hh: int, mm: int, dur: int, day: int = None) -> dict:
    params = {"hh": hh, "mm": mm, "dur": dur}
    if day is not None:
        params["day"] = day
    return device_get("/once/set", **params)

def cancel_once() -> dict:
    return device_get("/once/cancel")

def once_text(once: dict) -> str:
    """'17:30 today for 45 min (in 2h 10m)' or '' when nothing is pending."""
    if not once or not once.get("at"):
        return ""
    local = once.get("local", "")
    when = local[11:]
    if local[:10] == now().strftime("%Y-%m-%d"):
        when += " today"
    else:
        when += f" on {local[8:10]}/{local[5:7]}"
    return f"{when} for {once.get('dur')} min (in {fmt_duration(int(once.get('in', 0)))})"

def get_schedules() -> list:
    return device_get("/sched/get")["slots"]

def set_schedule(slot: int, en: bool, hh: int = None, mm: int = None, dur: int = None) -> list:
    params = {f"en{slot}": "1" if en else "0"}
    if hh is not None:
        params.update({f"hh{slot}": hh, f"mm{slot}": mm, f"dur{slot}": dur})
    return device_get("/sched/set", **params)["slots"]

# ---------- power cycle monitor ----------

def alert(text: str) -> None:
    """Proactive notification to the alert chat (if configured)."""
    if ALERT_CHAT_ID:
        tg("sendMessage", chat_id=ALERT_CHAT_ID, text=text)

class Monitor(threading.Thread):
    """Polls the device, logs every power change and completed cycle,
    sends offline / on-too-long alerts and the daily summary."""

    POLL_SECONDS = 5
    OFFLINE_ALERT_AFTER = 6  # consecutive failed polls (~30s) before alerting

    def __init__(self):
        super().__init__(daemon=True, name="monitor")
        self.prev = None
        self.on_since = {}  # circuit -> datetime it turned on
        self.offline = False
        self.fail_count = 0
        self.offline_alerted = False
        self.long_on_alerted = False
        self.summary_sent_day = None
        self.last_backfill = 0.0

    def run(self):
        while True:
            # The outer guard is what keeps cycle logging alive: any
            # unexpected error here used to kill the thread silently, and
            # the bot kept answering commands while recording nothing.
            try:
                try:
                    state = get_state()
                    self.fail_count = 0
                    if self.offline:
                        self.offline = False
                        log_event("device_online")
                        if self.offline_alerted:
                            self.offline_alerted = False
                            alert("✅ Water Heater is back online")
                    self.update(state)
                    self.check_long_on()
                except DeviceError as e:
                    self.fail_count += 1
                    if not self.offline:
                        self.offline = True
                        log_event("device_offline", error=str(e))
                    if self.fail_count == self.OFFLINE_ALERT_AFTER and not self.offline_alerted:
                        self.offline_alerted = True
                        alert(f"⚠️ Water Heater unreachable ({e})")
                self.check_summary()
                if time.time() - self.last_backfill >= 600:  # every 10 min
                    self.last_backfill = time.time()
                    backfill_from_device()
            except Exception as e:  # noqa: BLE001 - deliberately broad
                log_event("monitor_error", error=f"{e.__class__.__name__}: {e}")
                print(f"monitor error (continuing): {e!r}", file=sys.stderr)
            time.sleep(self.POLL_SECONDS)

    def check_long_on(self):
        if ALERT_ON_HOURS <= 0:
            return
        on_since = self.on_since.get("heater")
        if not on_since:
            self.long_on_alerted = False
            return
        hours = (now() - on_since).total_seconds() / 3600
        if hours >= ALERT_ON_HOURS and not self.long_on_alerted:
            self.long_on_alerted = True
            alert(f"⚠️ Water Heater has been ON for {fmt_duration(hours * 3600)} — still intended?")

    def check_summary(self):
        if not SUMMARY_TIME:
            return
        t = now()
        if t.strftime("%H:%M") != SUMMARY_TIME or self.summary_sent_day == t.date():
            return
        self.summary_sent_day = t.date()
        total = daily_totals(1)[t.date()]
        cycles = 0
        if CYCLES_FILE.exists():
            with CYCLES_FILE.open(encoding="utf-8") as f:
                cycles = sum(1 for r in csv.DictReader(f)
                             if r["circuit"] == "heater"
                             and datetime.fromisoformat(r["off"]).date() == t.date())
        if self.on_since.get("heater"):
            cycles += 1
        alert(f"\U0001F4C5 Today the heater ran {fmt_duration(total)}"
              f" over {cycles} cycle{'s' if cycles != 1 else ''}")

    def update(self, state: dict) -> None:
        current = {
            "relay1": state["relay1"],
            "relay2": state["relay2"],
            "heater": state["relay1"] or state["relay2"],
        }
        t = now()

        if self.prev is None:  # first poll after startup
            self.prev = current
            for circuit, is_on in current.items():
                if is_on:
                    self.on_since[circuit] = t
            log_event("monitor_start",
                      relay1="ON" if current["relay1"] else "off",
                      relay2="ON" if current["relay2"] else "off",
                      timer=state.get("timer", 0))
            return

        for circuit, is_on in current.items():
            if is_on == self.prev[circuit]:
                continue
            log_event("power_change", circuit=circuit,
                      state="ON" if is_on else "off",
                      timer=state.get("timer", 0))
            if is_on:
                self.on_since[circuit] = t
            else:
                on_ts = self.on_since.pop(circuit, None)
                if on_ts:
                    log_cycle(circuit, on_ts, t)
        self.prev = current

MONITOR = Monitor()

# ---------- telegram helpers ----------

def tg(method: str, **params):
    # Never raises: a Telegram hiccup must not kill the caller's thread
    # (a failed alert used to take the whole monitor down with it).
    try:
        r = _session().post(f"{TG_API}/{method}", json=params, timeout=40)
        data = r.json()
    except (requests.RequestException, json.JSONDecodeError) as e:
        print(f"telegram {method} error: {e}", file=sys.stderr)
        return {"ok": False, "description": str(e)}
    if not data.get("ok"):
        # Benign 400s not worth logging:
        # - "message is not modified": Refresh pressed while nothing changed
        # - "query is too old": callback answered past Telegram's ~15s window
        desc = data.get("description", "")
        if ("message is not modified" not in desc
                and "query is too old" not in desc):
            print(f"telegram {method} failed: {data}", file=sys.stderr)
    return data

def status_text(state: dict) -> str:
    heating = state["relay1"] or state["relay2"]
    lines = [f"\U0001F525 Water Heater: {'ON — heating' if heating else 'off'}"]
    if heating and MONITOR.on_since.get("heater"):
        lines[0] += f" for {fmt_duration((now() - MONITOR.on_since['heater']).total_seconds())}"
    timer = state.get("timer", 0)
    if timer > 0:
        lines.append(f"Timer: {timer // 60}m {timer % 60}s left")
    else:
        lines.append("Timer: off")
    lines.append(f"Heated today: {fmt_duration(daily_totals(1)[now().date()])}")
    pending = once_text(state.get("once"))
    if pending:
        lines.append(f"⏰ Once: {pending}")
    return "\n".join(lines)

def menu_keyboard(state: dict) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "\U0001F525 Heater ON (30m)", "callback_data": "allon"},
             {"text": "Heater OFF", "callback_data": "alloff"}],
            [{"text": "Heat 15m", "callback_data": "t15"},
             {"text": "Heat 60m", "callback_data": "t60"}],
            [{"text": "\U0001F4CA History", "callback_data": "history"},
             {"text": "\U0001F4C8 Graph", "callback_data": "graph"},
             {"text": "\U0001F504", "callback_data": "menu"}],
            [{"text": "⏰ Heat once", "callback_data": "o"},
             {"text": "⚙️ Schedules", "callback_data": "w"}],
        ]
    }

def send_menu(chat_id):
    state = get_state()
    tg("sendMessage", chat_id=chat_id, text=status_text(state), reply_markup=menu_keyboard(state))

def edit_menu(chat_id, message_id):
    state = get_state()
    tg("editMessageText", chat_id=chat_id, message_id=message_id,
       text=status_text(state), reply_markup=menu_keyboard(state))

# ---------- history / stats ----------

def stale_note(rows: list) -> str:
    """Warn when the newest logged cycle is old: nothing is recorded while
    the bot is not running, so an old 'last cycle' means a logging gap,
    not an idle heater."""
    if MONITOR.on_since.get("heater") or not rows:
        return ""
    last = max(datetime.fromisoformat(r["off"]) for r in rows)
    days = (now() - last).days
    if days < 1:
        return ""
    return (f"⚠️ No data logged since {last.strftime('%d/%m %H:%M')} ({days}d ago) — "
            f"the bot only records while it is running.")

def history_text() -> str:
    rows = []
    if CYCLES_FILE.exists():
        with CYCLES_FILE.open(encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r["circuit"] == "heater"]
    # backfilled rows are appended out of order, so sort before slicing
    rows.sort(key=lambda r: datetime.fromisoformat(r["on"]))

    lines = ["\U0001F4CA Water Heater history"]
    note = stale_note(rows)
    if note:
        lines.append(note)

    today = now().date()
    today_secs = sum(int(r["seconds"]) for r in rows
                     if datetime.fromisoformat(r["off"]).date() == today)
    ongoing = MONITOR.on_since.get("heater")
    if ongoing:
        today_secs += int((now() - ongoing).total_seconds())
        lines.append(f"Currently heating since {ongoing.strftime('%H:%M')}")
    lines.append(f"Heating time today: {fmt_duration(today_secs)}")

    if rows:
        lines.append("\nLast cycles:")
        for r in rows[-10:]:
            on = datetime.fromisoformat(r["on"])
            lines.append(f"  {on.strftime('%d/%m %H:%M')} — {fmt_duration(int(r['seconds']))}")
    elif not ongoing:
        lines.append("No completed heating cycles logged yet.")
    return "\n".join(lines)

# ---------- usage graph ----------

def daily_totals(days: int = 14) -> dict:
    """date -> heated seconds for the last `days` days, splitting cycles
    that span midnight across the days they touch."""
    from datetime import timedelta

    today = now().date()
    totals = {today - timedelta(days=i): 0.0 for i in range(days - 1, -1, -1)}

    def add_interval(start: datetime, end: datetime):
        cur = start
        while cur < end:
            day = cur.date()
            day_end = datetime.combine(day, datetime.max.time()).astimezone()
            chunk_end = min(end, day_end)
            if day in totals:
                totals[day] += (chunk_end - cur).total_seconds()
            if chunk_end >= end:
                break
            cur = datetime.combine(day + timedelta(days=1),
                                   datetime.min.time()).astimezone()

    if CYCLES_FILE.exists():
        with CYCLES_FILE.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["circuit"] != "heater":
                    continue
                add_interval(datetime.fromisoformat(r["on"]),
                             datetime.fromisoformat(r["off"]))
    ongoing = MONITOR.on_since.get("heater")
    if ongoing:
        add_interval(ongoing, now())
    return totals

def hourly_totals(days: int = 14) -> list:
    """24 buckets: heated seconds per hour-of-day over the last `days` days,
    splitting cycles across the hour boundaries they cross."""
    from datetime import timedelta

    cutoff = (now() - timedelta(days=days)).astimezone()
    buckets = [0.0] * 24

    def add_interval(start: datetime, end: datetime):
        cur = max(start, cutoff)
        while cur < end:
            hour_end = cur.replace(minute=59, second=59, microsecond=999999)
            chunk_end = min(end, hour_end)
            buckets[cur.hour] += (chunk_end - cur).total_seconds()
            if chunk_end >= end:
                break
            cur = (chunk_end + timedelta(microseconds=1))

    if CYCLES_FILE.exists():
        with CYCLES_FILE.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["circuit"] != "heater":
                    continue
                add_interval(datetime.fromisoformat(r["on"]),
                             datetime.fromisoformat(r["off"]))
    ongoing = MONITOR.on_since.get("heater")
    if ongoing:
        add_interval(ongoing, now())
    return buckets

BAR, INK, MUTED, GRID = "#2563eb", "#374151", "#6b7280", "#e5e7eb"

def _style_axis(ax, title: str):
    ax.set_facecolor("white")
    ax.set_title(title, fontsize=11, color=INK, loc="left", pad=12)
    ax.set_ylabel("hours", fontsize=9, color=MUTED)
    ax.tick_params(axis="y", labelsize=8, colors=MUTED, length=0)
    ax.tick_params(axis="x", length=0)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8, zorder=0)
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.margins(y=0.15)

def _label_bar(ax, i: int, value_hours: float):
    if value_hours > 0:
        ax.annotate(fmt_duration(value_hours * 3600), (i, value_hours),
                    textcoords="offset points", xytext=(0, 4),
                    ha="center", fontsize=8, color=MUTED, zorder=4)

def graph_png():
    """Render daily + hour-of-day usage charts; returns PNG bytes or None
    if matplotlib is missing."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    import io

    totals = daily_totals(14)
    days = list(totals.keys())
    day_hours = [totals[d] / 3600 for d in days]
    hour_hours = [s / 3600 for s in hourly_totals(14)]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8, 7.6), dpi=150)
    fig.patch.set_facecolor("white")

    # panel 1: heating time per day
    ax1.bar(range(len(days)), day_hours, width=0.72, color=BAR, zorder=3)
    label_idx = {len(days) - 1}  # today, plus the biggest day
    if any(h > 0 for h in day_hours):
        label_idx.add(day_hours.index(max(day_hours)))
    for i in label_idx:
        _label_bar(ax1, i, day_hours[i])
    ax1.set_xticks(range(len(days)))
    ax1.set_xticklabels([d.strftime("%d/%m") for d in days],
                        fontsize=8, color=MUTED, rotation=45)
    _style_axis(ax1, "Water Heater — heating time per day")

    # panel 2: heating time by hour of day
    ax2.bar(range(24), hour_hours, width=0.72, color=BAR, zorder=3)
    if any(h > 0 for h in hour_hours):
        _label_bar(ax2, hour_hours.index(max(hour_hours)), max(hour_hours))
    ax2.set_xticks(range(24))
    ax2.set_xticklabels([f"{h:02d}" for h in range(24)], fontsize=7, color=MUTED)
    ax2.set_xlabel("hour of day", fontsize=9, color=MUTED)
    _style_axis(ax2, "Water Heater — heating by hour of day (last 14 days)")

    buf = io.BytesIO()
    fig.tight_layout(h_pad=2.5)
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.read()

def send_graph(chat_id):
    png = graph_png()
    if png is None:
        tg("sendMessage", chat_id=chat_id,
           text="Graphs need matplotlib on the bot PC:\n"
                "python3 -m pip install matplotlib")
        return
    totals = daily_totals(14)
    total = sum(totals.values())
    hourly = hourly_totals(14)
    peak = hourly.index(max(hourly)) if any(hourly) else None
    caption = (f"Last 14 days: {fmt_duration(total)} total, "
               f"today {fmt_duration(totals[now().date()])}")
    if peak is not None:
        caption += f". Busiest hour: {peak:02d}:00–{(peak + 1) % 24:02d}:00"
    rows = []
    if CYCLES_FILE.exists():
        with CYCLES_FILE.open(encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r["circuit"] == "heater"]
    note = stale_note(rows)
    if note:
        caption += "\n" + note
    r = _session().post(f"{TG_API}/sendPhoto",
                        data={"chat_id": chat_id, "caption": caption},
                        files={"photo": ("usage.png", png, "image/png")},
                        timeout=40)
    if not r.json().get("ok"):
        print(f"telegram sendPhoto failed: {r.json()}", file=sys.stderr)

def settings_text() -> str:
    lines = ["⚙️ Water Heater settings"]
    try:
        sys_info = device_get("/sys/get")
        lines.append(f"Timezone: {sys_info.get('tz', '?')}"
                     + (f" (device time {sys_info['time']})" if sys_info.get("time") else ""))
        lines.append(f"Max continuous ON: {sys_info.get('maxon', '?')} min")
    except DeviceError:
        pass
    lines.append("\nDaily schedules — tap a slot to edit:")
    return "\n".join(lines)

# ---------- schedule wizard (stateless: the step lives in callback_data) ----------
# "w"            -> slot list          "w:2"        -> hour picker for slot 2
# "w:2:6"        -> minute picker      "w:2:6:30"   -> duration picker
# "w:2:6:30:45"  -> apply & show list  "wd:2"       -> disable slot 2

def _slot_button(i: int, s: dict) -> dict:
    label = (f"{i + 1}:  {s['hh']:02d}:{s['mm']:02d} · {s['dur']} min ✅"
             if s["en"] else f"{i + 1}:  — tap to set")
    return {"text": label, "callback_data": f"w:{i}"}

def wizard_home(note: str = "") -> tuple:
    slots = get_schedules()
    kb = {"inline_keyboard": [[_slot_button(i, s)] for i, s in enumerate(slots)]
          + [[{"text": "✖ Close", "callback_data": "menu"}]]}
    text = (note + "\n\n" if note else "") + settings_text()
    return text, kb

def _grid(items, fmt, data_fmt, per_row) -> list:
    rows, row = [], []
    for v in items:
        row.append({"text": fmt(v), "callback_data": data_fmt(v)})
        if len(row) == per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows

def wizard_render(data: str) -> tuple:
    """Returns (text, keyboard) for any wizard callback."""
    if data == "w":
        return wizard_home()
    parts = data.split(":")
    if parts[0] == "wd":
        slot = int(parts[1])
        set_schedule(slot, False)
        return wizard_home(f"Schedule {slot + 1} disabled")

    slot = int(parts[1])
    if len(parts) == 2:  # pick hour
        slots = get_schedules()
        rows = _grid(range(24), lambda h: f"{h:02d}", lambda h: f"w:{slot}:{h}", 6)
        extra = [{"text": "◀ Back", "callback_data": "w"}]
        if slots[slot]["en"]:
            extra.append({"text": "🗑 Disable", "callback_data": f"wd:{slot}"})
        rows.append(extra)
        return f"Schedule {slot + 1} — pick the start hour:", {"inline_keyboard": rows}

    if len(parts) == 3:  # pick minutes
        hh = int(parts[2])
        rows = _grid([0, 15, 30, 45], lambda m: f"{hh:02d}:{m:02d}",
                     lambda m: f"w:{slot}:{hh}:{m}", 4)
        rows.append([{"text": "◀ Back", "callback_data": f"w:{slot}"}])
        return f"Schedule {slot + 1} — pick the start time:", {"inline_keyboard": rows}

    if len(parts) == 4:  # pick duration
        hh, mm = int(parts[2]), int(parts[3])
        rows = _grid([15, 30, 45, 60, 90, 120], lambda d: f"{d} min",
                     lambda d: f"w:{slot}:{hh}:{mm}:{d}", 3)
        rows.append([{"text": "◀ Back", "callback_data": f"w:{slot}:{hh}"}])
        return (f"Schedule {slot + 1} — {hh:02d}:{mm:02d}, pick the duration:",
                {"inline_keyboard": rows})

    # len 5: apply
    hh, mm, dur = int(parts[2]), int(parts[3]), int(parts[4])
    set_schedule(slot, True, hh, mm, dur)
    return wizard_home(f"✅ Schedule {slot + 1}: every day at {hh:02d}:{mm:02d} for {dur} min")

# ---------- one-time schedule wizard (stateless, like the daily one) ----------
# "o"          -> overview (pending run or hour picker)   "o:17"      -> minutes
# "o:17:30"    -> duration                                 "o:17:30:45" -> apply
# "oc"         -> cancel pending run                       "on"        -> new (hour picker)

def once_home(note: str = "") -> tuple:
    pending = once_text(get_once())
    lines = ["⏰ Heat once"]
    if note:
        lines.insert(0, note + "\n")
    if pending:
        lines.append(f"Pending: {pending}")
        kb = [[{"text": "🗑 Cancel it", "callback_data": "oc"},
               {"text": "✏️ Change", "callback_data": "on"}],
              [{"text": "◀ Menu", "callback_data": "menu"}]]
        return "\n".join(lines), {"inline_keyboard": kb}
    lines.append("Nothing pending — pick the start hour:")
    rows = _grid(range(24), lambda h: f"{h:02d}", lambda h: f"o:{h}", 6)
    rows.append([{"text": "◀ Menu", "callback_data": "menu"}])
    return "\n".join(lines), {"inline_keyboard": rows}

def once_render(data: str) -> tuple:
    if data == "o":
        return once_home()
    if data == "oc":
        cancel_once()
        return once_home("One-time schedule cancelled")
    if data == "on":
        rows = _grid(range(24), lambda h: f"{h:02d}", lambda h: f"o:{h}", 6)
        rows.append([{"text": "◀ Back", "callback_data": "o"}])
        return "⏰ Heat once — pick the start hour:", {"inline_keyboard": rows}
    parts = data.split(":")
    hh = int(parts[1])
    if len(parts) == 2:
        rows = _grid([0, 15, 30, 45], lambda m: f"{hh:02d}:{m:02d}", lambda m: f"o:{hh}:{m}", 4)
        rows.append([{"text": "◀ Back", "callback_data": "on"}])
        return "⏰ Heat once — pick the start time:", {"inline_keyboard": rows}
    mm = int(parts[2])
    if len(parts) == 3:
        rows = _grid([15, 30, 45, 60, 90, 120], lambda d: f"{d} min", lambda d: f"o:{hh}:{mm}:{d}", 3)
        rows.append([{"text": "◀ Back", "callback_data": f"o:{hh}"}])
        return f"⏰ Heat once at {hh:02d}:{mm:02d} — pick the duration:", {"inline_keyboard": rows}
    dur = int(parts[3])
    once = set_once(hh, mm, dur)
    return once_home(f"✅ Will heat once at {once_text(once)}")

# ---------- command handling ----------

def apply_action(data: str) -> str:
    """Callback-button actions; returns short feedback text."""
    if data == "menu":
        return "Refreshed"
    turns_on = data in ("allon", "t15", "t60")
    if data in ("allon", "alloff", "t15", "t60") and not allow_change(turns_on):
        return RATE_MSG
    if data == "allon":  # Heater ON always runs on a 30 min timer
        set_timer(30)
        return "Heater ON for 30 min"
    if data == "alloff":  # turns off and cancels any running timer
        set_relay("all", "off")
        return "Heater off"
    if data in ("t15", "t60"):
        mins = int(data[1:])
        set_timer(mins)
        return f"Heating for {mins} min"
    return "?"

def handle_command(chat_id, text: str):
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()  # strip @botname in groups
    arg = arg.strip()

    if cmd in ("/start", "/help"):
        tg("sendMessage", chat_id=chat_id, text=HELP_TEXT)
        send_menu(chat_id)
    elif cmd == "/menu":
        send_menu(chat_id)
    elif cmd == "/status":
        tg("sendMessage", chat_id=chat_id, text=status_text(get_state()))
    elif cmd == "/history":
        tg("sendMessage", chat_id=chat_id, text=history_text())
    elif cmd == "/graph":
        send_graph(chat_id)
    elif cmd == "/chatid":
        tg("sendMessage", chat_id=chat_id, text=f"Chat ID: {chat_id}")
    elif cmd == "/settings" or (cmd == "/setsched" and not arg):
        text, kb = wizard_home()
        tg("sendMessage", chat_id=chat_id, text=text, reply_markup=kb)
    elif cmd == "/setsched":  # power-user direct syntax: /setsched N HH:MM MIN
        parts = arg.split()
        ok = False
        if len(parts) == 3 and parts[0].isdigit() and ":" in parts[1] and parts[2].isdigit():
            slot, dur = int(parts[0]), int(parts[2])
            hh, _, mm = parts[1].partition(":")
            if (1 <= slot <= 5 and hh.isdigit() and mm.isdigit()
                    and 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59 and 1 <= dur <= 720):
                set_schedule(slot - 1, True, int(hh), int(mm), dur)
                text, kb = wizard_home(f"✅ Schedule {slot} set: {int(hh):02d}:{int(mm):02d} for {dur} min")
                tg("sendMessage", chat_id=chat_id, text=text, reply_markup=kb)
                ok = True
        if not ok:
            tg("sendMessage", chat_id=chat_id,
               text="Just send /setsched for the wizard, or: /setsched N HH:MM MINUTES")
    elif cmd == "/once":
        if not arg:
            text, kb = once_home()
            tg("sendMessage", chat_id=chat_id, text=text, reply_markup=kb)
        elif arg.lower() in ("off", "cancel"):
            cancel_once()
            tg("sendMessage", chat_id=chat_id, text="One-time schedule cancelled")
        else:
            parts = arg.split()
            ok = False
            if len(parts) == 2 and ":" in parts[0] and parts[1].isdigit():
                hh, _, mm = parts[0].partition(":")
                dur = int(parts[1])
                if hh.isdigit() and mm.isdigit() and 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59 and 1 <= dur <= 720:
                    once = set_once(int(hh), int(mm), dur)
                    tg("sendMessage", chat_id=chat_id, text=f"⏰ Will heat once at {once_text(once)}")
                    ok = True
            if not ok:
                tg("sendMessage", chat_id=chat_id,
                   text="Usage: /once HH:MM MINUTES  (or just /once for the wizard, /once off to cancel)")
    elif cmd == "/delsched":
        if arg.isdigit() and 1 <= int(arg) <= 5:
            set_schedule(int(arg) - 1, False)
            text, kb = wizard_home(f"Schedule {int(arg)} disabled")
            tg("sendMessage", chat_id=chat_id, text=text, reply_markup=kb)
        else:
            tg("sendMessage", chat_id=chat_id, text="Usage: /delsched N  (1-5)")
    elif cmd == "/on":
        if not allow_change(True):
            tg("sendMessage", chat_id=chat_id, text=RATE_MSG)
        else:
            set_timer(30)
            tg("sendMessage", chat_id=chat_id, text="\U0001F525 Heater ON for 30 min")
    elif cmd == "/off":
        allow_change(False)
        set_relay("all", "off")  # also cancels a running timer on the device
        tg("sendMessage", chat_id=chat_id, text="Heater off")
    elif cmd == "/timer":
        try:
            mins = int(arg)
        except ValueError:
            mins = 0
        if not 1 <= mins <= 720:
            tg("sendMessage", chat_id=chat_id, text="Usage: /timer N  (minutes, 1-720)")
        elif not allow_change(True):
            tg("sendMessage", chat_id=chat_id, text=RATE_MSG)
        else:
            set_timer(mins)
            tg("sendMessage", chat_id=chat_id, text=f"\U0001F525 Heating for {mins} min")
    else:
        tg("sendMessage", chat_id=chat_id, text="Unknown command. /help for the list.")

def authorized(chat_id) -> bool:
    return not ALLOWED_CHAT_IDS or str(chat_id) in ALLOWED_CHAT_IDS

def user_label(entity: dict) -> str:
    u = entity.get("from", {})
    return u.get("username") or f"{u.get('first_name', '')} {u.get('last_name', '')}".strip() or "?"

def handle_update(update: dict):
    if "callback_query" in update:
        q = update["callback_query"]
        chat_id = q["message"]["chat"]["id"]
        data = q.get("data", "")
        log_event("button", chat=chat_id, user=user_label(q), action=data,
                  authorized=authorized(chat_id))
        if not authorized(chat_id):
            tg("answerCallbackQuery", callback_query_id=q["id"], text="Not authorized")
            return
        # Answer immediately so the button spinner clears at once; the
        # edited panel below shows the actual result.
        tg("answerCallbackQuery", callback_query_id=q["id"])
        try:
            if data == "w" or data.startswith("w:") or data.startswith("wd:"):
                text, kb = wizard_render(data)
                tg("editMessageText", chat_id=chat_id,
                   message_id=q["message"]["message_id"], text=text, reply_markup=kb)
                return
            if data in ("o", "oc", "on") or data.startswith("o:"):
                text, kb = once_render(data)
                tg("editMessageText", chat_id=chat_id,
                   message_id=q["message"]["message_id"], text=text, reply_markup=kb)
                return
            if data == "history":
                tg("sendMessage", chat_id=chat_id, text=history_text())
                return
            if data == "graph":
                send_graph(chat_id)
                return
            feedback = apply_action(data)
            if feedback == RATE_MSG:
                tg("sendMessage", chat_id=chat_id, text=RATE_MSG)
            edit_menu(chat_id, q["message"]["message_id"])  # update panel in place
        except DeviceError as e:
            log_event("device_error", context=data, error=str(e))
            tg("sendMessage", chat_id=chat_id, text=f"⚠️ {e}")
        return

    msg = update.get("message")
    if not msg or "text" not in msg:
        return
    chat_id = msg["chat"]["id"]
    log_event("command", chat=chat_id, user=user_label(msg), text=msg["text"],
              authorized=authorized(chat_id))
    if not authorized(chat_id):
        tg("sendMessage", chat_id=chat_id, text=f"Not authorized. This chat ID: {chat_id}")
        return
    try:
        handle_command(chat_id, msg["text"])
    except DeviceError as e:
        log_event("device_error", context=msg["text"], error=str(e))
        tg("sendMessage", chat_id=chat_id, text=f"⚠️ {e}")

# ---------- main loop ----------

def main():
    print(f"Water Heater telegram bot: device={DEVICE_URL}, logs={LOG_DIR}, "
          f"allowed chats={sorted(ALLOWED_CHAT_IDS) or 'ANY (set ALLOWED_CHAT_IDS!)'}")
    _device_base()  # resolve .local -> IP once up front (prints the result)
    me = tg("getMe")
    if not me.get("ok"):
        sys.exit("Invalid BOT_TOKEN (getMe failed)")
    print(f"Connected as @{me['result']['username']}")
    log_event("bot_start", bot=me["result"]["username"], device=DEVICE_URL)

    MONITOR.start()
    backfill_from_device()  # recover anything logged while we were down

    if WEB_PORT:
        try:
            webserver.start(sys.modules[__name__], WEB_HOST, WEB_PORT)
            shown = "localhost" if WEB_HOST in ("0.0.0.0", "") else WEB_HOST
            print(f"Web panel: http://{shown}:{WEB_PORT}/")
            log_event("web_start", host=WEB_HOST, port=WEB_PORT)
        except OSError as e:
            print(f"web panel not started ({e}); set WEB_PORT to change the port "
                  "or 0 to disable", file=sys.stderr)
            log_event("web_error", error=str(e))

    # Drop updates that queued up while the bot was offline: acting on a
    # button pressed minutes ago would switch the heater unexpectedly, and
    # the callback IDs are expired anyway.
    offset = 0
    try:
        resp = _session().get(f"{TG_API}/getUpdates",
                              params={"offset": -1, "timeout": 0}, timeout=10).json()
        if resp.get("ok") and resp["result"]:
            offset = resp["result"][-1]["update_id"] + 1
            log_event("startup_flush", last_update_id=offset - 1)
            print("dropped updates queued while offline")
    except requests.RequestException:
        pass

    while True:
        try:
            resp = _session().get(f"{TG_API}/getUpdates",
                                  params={"offset": offset, "timeout": 30},
                                  timeout=40).json()
            if not resp.get("ok"):
                if resp.get("error_code") == 409:
                    print("409 Conflict: another process is polling this token "
                          "(disable the device's built-in bot or use a separate token)",
                          file=sys.stderr)
                time.sleep(5)
                continue
            for update in resp.get("result", []):
                offset = update["update_id"] + 1
                handle_update(update)
        except KeyboardInterrupt:
            log_event("bot_stop")
            print("\nbye")
            return
        except requests.RequestException as e:
            print(f"telegram connection error: {e}", file=sys.stderr)
            time.sleep(5)
        except Exception as e:  # noqa: BLE001 - keep the bot alive
            log_event("loop_error", error=f"{e.__class__.__name__}: {e}")
            print(f"loop error (continuing): {e!r}", file=sys.stderr)
            time.sleep(5)

if __name__ == "__main__":
    main()
