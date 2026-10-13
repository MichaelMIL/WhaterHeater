#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <ESPmDNS.h>
#include <Preferences.h>
#include <WiFiClientSecure.h>
#include <UniversalTelegramBot.h>
#include <time.h>
#include <Update.h>
#include <ArduinoOTA.h>
#include "tz_lookup.h"

// ---------- Configuration ----------
static const char *FW_VERSION = "v1.4 built " __DATE__ " " __TIME__;
static const char *AP_SSID = "WaterHeater";
static const char *AP_PASSWORD = "12345678"; // min 8 chars, or "" for an open network
static const char *DEFAULT_HOSTNAME = "waterheater"; // http://<hostname>.local, settable in settings

static const uint8_t RELAY1_PIN = 7;  // IO7
static const uint8_t RELAY2_PIN = 10; // IO10

// Status LED: lit while any relay is ON.
// Flip LED_ACTIVE_LOW if yours lights up inverted.
static const uint8_t LED_PIN = 3;
static const bool LED_ACTIVE_LOW = false;

// Push button (to GND, internal pull-up).
// What a press does is configured on the settings page.
static const uint8_t BUTTON_PIN = 4;

static const byte DNS_PORT = 53;

// ---------- Globals ----------
WebServer server(80);
DNSServer dnsServer;
Preferences prefs;

bool relay1On = false;
bool relay2On = false;

unsigned long rebootAtMs = 0; // != 0: restart once millis() passes it (post-OTA)

// Per-pin polarity, configurable on the settings page and stored in NVS.
// activeLow=false: ON drives the pin HIGH (relay closed on HIGH).
// activeLow=true:  ON drives the pin LOW  (most standalone relay modules).
bool relay1ActiveLow = false;
bool relay2ActiveLow = false;

String staSsid;
String staPass;
String hostname; // mDNS/DHCP/OTA name, settable on the settings page

// Timezone for the daily schedule: an IANA name like "Asia/Jerusalem",
// resolved via the embedded tz_lookup.h table. A raw POSIX TZ string is
// also accepted as a fallback for zones not in the table.
String tzString = "Asia/Jerusalem";
// Safety cap: no relay stays on longer than this many minutes (0 = disabled)
uint16_t maxOnMinutes = 180;
// Optional: drop the AP while home WiFi is connected (auto-restores on loss)
bool apOffWhenSta = false;
bool apActive = true;
unsigned long relay1OnSinceMs = 0;
unsigned long relay2OnSinceMs = 0;

static bool tzKnown() {
  return tzNameToPosix(tzString.c_str()) != nullptr;
}

static void applyTimezone() {
  const char *posix = tzNameToPosix(tzString.c_str());
  setenv("TZ", posix ? posix : tzString.c_str(), 1);
  tzset();
}

static String sanitizeHostname(String h) {
  h.toLowerCase();
  String out;
  for (size_t i = 0; i < h.length() && out.length() < 32; i++) {
    char c = h[i];
    if ((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-') out += c;
  }
  while (out.startsWith("-")) out.remove(0, 1);
  while (out.endsWith("-")) out.remove(out.length() - 1);
  return out.length() ? out : String(DEFAULT_HOSTNAME);
}

static void loadSysConfig() {
  prefs.begin("sys", false);
  hostname = sanitizeHostname(prefs.getString("host", DEFAULT_HOSTNAME));
  tzString = prefs.getString("tz", tzString);
  // migrate the pre-v1.4 default, which was stored as a raw POSIX string
  if (tzString == "IST-2IDT,M3.4.4/26,M10.5.0") tzString = "Asia/Jerusalem";
  maxOnMinutes = prefs.getUShort("maxon", 180);
  apOffWhenSta = prefs.getBool("apoff", false);
  prefs.end();
}

static void saveSysConfig() {
  prefs.begin("sys", false);
  prefs.putString("host", hostname);
  prefs.putString("tz", tzString);
  prefs.putUShort("maxon", maxOnMinutes);
  prefs.putBool("apoff", apOffWhenSta);
  prefs.end();
}

static void setApActive(bool on) {
  if (on == apActive) return;
  apActive = on;
  if (on) {
    WiFi.mode(WIFI_AP_STA);
    WiFi.softAP(AP_SSID, AP_PASSWORD);
    dnsServer.start(DNS_PORT, "*", WiFi.softAPIP());
    Serial.println("AP re-enabled");
  } else {
    dnsServer.stop();
    WiFi.softAPdisconnect(true);
    WiFi.mode(WIFI_STA);
    Serial.println("AP disabled (home WiFi connected)");
  }
}

static void applyRelay(uint8_t pin, bool on, bool activeLow) {
  digitalWrite(pin, (on != activeLow) ? HIGH : LOW);
}

// Status LED: blink or steady while any relay is ON (configurable on the
// settings page), with PWM brightness via the LEDC peripheral.
bool ledBlink = true;       // false = steady while on
uint8_t ledBrightness = 100; // percent
bool ledLit = false;

static void loadLedConfig() {
  prefs.begin("led", false);
  ledBlink = prefs.getBool("blink", true);
  ledBrightness = prefs.getUChar("bri", 100);
  prefs.end();
}

static void saveLedConfig() {
  prefs.begin("led", false);
  prefs.putBool("blink", ledBlink);
  prefs.putUChar("bri", ledBrightness);
  prefs.end();
}

static void ledSet(bool on) {
  ledLit = on;
  uint32_t duty = on ? (255u * ledBrightness) / 100u : 0;
  if (LED_ACTIVE_LOW) duty = 255 - duty;
  ledcWrite(LED_PIN, duty);
}

static void ledTick() {
  static unsigned long lastToggle = 0;
  bool active = relay1On || relay2On;
  if (!active) {
    if (ledLit) ledSet(false);
    return;
  }
  if (!ledBlink) {
    if (!ledLit) ledSet(true);
    return;
  }
  if (millis() - lastToggle >= 500) {
    lastToggle = millis();
    ledSet(!ledLit);
  }
}

extern unsigned long relay1OnSinceMs; // defined below with the sys config
extern unsigned long relay2OnSinceMs;

static void setRelay1(bool on) {
  if (on && !relay1On) relay1OnSinceMs = millis();
  relay1On = on;
  applyRelay(RELAY1_PIN, on, relay1ActiveLow);
}

static void setRelay2(bool on) {
  if (on && !relay2On) relay2OnSinceMs = millis();
  relay2On = on;
  applyRelay(RELAY2_PIN, on, relay2ActiveLow);
}

// ---------- Telegram bot config (token set on the settings page) ----------
String tgToken;        // empty = bot disabled
String tgChat;         // optional: only this chat ID may control the relay
String tgLastChatId;   // chat ID of the last message the bot received
bool tgEnabled = true; // settings-page checkbox: bot on/off
SemaphoreHandle_t tgMutex;

static void loadTelegramConfig() {
  prefs.begin("tg", false);
  tgToken = prefs.getString("token", "");
  tgChat = prefs.getString("chat", "");
  tgEnabled = prefs.getBool("en", true);
  prefs.end();
}

static void saveTelegramConfig(const String &token, const String &chat, bool enabled) {
  prefs.begin("tg", false);
  prefs.putString("token", token);
  prefs.putString("chat", chat);
  prefs.putBool("en", enabled);
  prefs.end();
  xSemaphoreTake(tgMutex, portMAX_DELAY);
  tgToken = token;
  tgChat = chat;
  tgEnabled = enabled;
  xSemaphoreGive(tgMutex);
}

// ---------- Timer (powers both relays, persisted across reboots) ----------
bool timerActive = false;
unsigned long timerEndMs = 0;
unsigned long lastTimerSave = 0;

static void persistTimer(uint32_t remainingSecs) {
  prefs.begin("timer", false);
  prefs.putUInt("rem", remainingSecs);
  prefs.end();
}

static uint32_t timerRemainingSecs() {
  if (!timerActive) return 0;
  long diff = (long)(timerEndMs - millis());
  return diff > 0 ? (uint32_t)diff / 1000 : 0;
}

static void startTimer(uint32_t mins) {
  timerActive = true;
  timerEndMs = millis() + mins * 60000UL;
  lastTimerSave = millis();
  setRelay1(true);
  setRelay2(true);
  persistTimer(mins * 60);
}

static void stopTimer(bool relaysOff) {
  timerActive = false;
  timerEndMs = 0;
  persistTimer(0);
  if (relaysOff) {
    setRelay1(false);
    setRelay2(false);
  }
}

static void resumeTimerAfterBoot() {
  prefs.begin("timer", false);
  uint32_t rem = prefs.getUInt("rem", 0);
  prefs.end();
  if (rem > 0) {
    timerActive = true;
    timerEndMs = millis() + rem * 1000UL;
    lastTimerSave = millis();
    setRelay1(true);
    setRelay2(true);
  }
}

// ---------- Daily schedules (up to 5 slots) ----------
static const uint8_t SCHED_SLOTS = 5;
struct SchedSlot {
  bool en;
  uint8_t hh, mm;
  uint16_t dur; // minutes
};
SchedSlot sched[SCHED_SLOTS];
int schedLastFired[SCHED_SLOTS] = {-1, -1, -1, -1, -1}; // tm_yday, RAM only

static void loadSchedConfig() {
  prefs.begin("sched", false);
  bool hasNewFormat = prefs.isKey("en0");
  char key[8];
  for (uint8_t i = 0; i < SCHED_SLOTS; i++) {
    snprintf(key, sizeof(key), "en%u", i);
    sched[i].en = prefs.getBool(key, false);
    snprintf(key, sizeof(key), "hh%u", i);
    sched[i].hh = prefs.getUChar(key, 6);
    snprintf(key, sizeof(key), "mm%u", i);
    sched[i].mm = prefs.getUChar(key, 30);
    snprintf(key, sizeof(key), "dur%u", i);
    sched[i].dur = prefs.getUShort(key, 30);
  }
  if (!hasNewFormat && prefs.isKey("en")) { // migrate pre-v1.4 single slot
    sched[0].en = prefs.getBool("en", false);
    sched[0].hh = prefs.getUChar("hh", 6);
    sched[0].mm = prefs.getUChar("mm", 30);
    sched[0].dur = prefs.getUShort("dur", 30);
  }
  prefs.end();
}

static void saveSchedConfig() {
  prefs.begin("sched", false);
  char key[8];
  for (uint8_t i = 0; i < SCHED_SLOTS; i++) {
    snprintf(key, sizeof(key), "en%u", i);
    prefs.putBool(key, sched[i].en);
    snprintf(key, sizeof(key), "hh%u", i);
    prefs.putUChar(key, sched[i].hh);
    snprintf(key, sizeof(key), "mm%u", i);
    prefs.putUChar(key, sched[i].mm);
    snprintf(key, sizeof(key), "dur%u", i);
    prefs.putUShort(key, sched[i].dur);
  }
  prefs.end();
}

// ---------- Rate limiting (user-initiated state changes) ----------
// At most 10 changes per 5 minutes across web / Telegram / button. Actions
// that turn things OFF are always allowed (never lock yourself out of
// switching a heater off); they still consume quota. The daily schedule and
// the max-on cap are internal and bypass this.
static unsigned long changeStamps[10];
static uint8_t changeStored = 0, changeHead = 0;

static bool tryChange(bool turnsOn) {
  unsigned long nowMs = millis();
  if (turnsOn) {
    uint8_t recent = 0;
    for (uint8_t i = 0; i < changeStored; i++) {
      if (nowMs - changeStamps[i] < 300000UL) recent++;
    }
    if (recent >= 10) return false;
  }
  changeStamps[changeHead] = nowMs;
  changeHead = (changeHead + 1) % 10;
  if (changeStored < 10) changeStored++;
  return true;
}

// ---------- On-device usage log ----------
// The PC bot only records while it is running, and the physical button is
// the most common way this heater gets switched on - so the device keeps
// its own history (14 daily totals + the last 32 cycles) in NVS. The bot
// backfills from here after any downtime.
static const uint8_t USAGE_DAYS = 14;
static const uint8_t USAGE_CYCLES = 32;

struct DayEntry { uint32_t date; uint32_t secs; };    // date as YYYYMMDD
struct CycleEntry { uint32_t start; uint32_t secs; }; // start = unix epoch

DayEntry usageDays[USAGE_DAYS];
CycleEntry usageCycles[USAGE_CYCLES];
uint8_t usageHead = 0;    // next ring slot to write
time_t usageCurStart = 0; // open cycle start (0 = heater off)
time_t usageCurCheck = 0; // last checkpoint of the open cycle

static bool clockReady() { return time(nullptr) > 1600000000; }

static uint32_t dateKey(time_t t) {
  struct tm lt;
  localtime_r(&t, &lt);
  return (uint32_t)(lt.tm_year + 1900) * 10000 + (uint32_t)(lt.tm_mon + 1) * 100 + lt.tm_mday;
}

static void usageAddToDay(time_t when, uint32_t secs) {
  if (!secs) return;
  uint32_t key = dateKey(when);
  uint8_t oldest = 0;
  for (uint8_t i = 0; i < USAGE_DAYS; i++) {
    if (usageDays[i].date == key) { usageDays[i].secs += secs; return; }
    if (usageDays[i].date < usageDays[oldest].date) oldest = i;
  }
  usageDays[oldest].date = key; // reuse the oldest slot
  usageDays[oldest].secs = secs;
}

// Attribute a span to each local day it covers (cycles can cross midnight).
static void usageAddSpan(time_t start, time_t end) {
  while (start < end) {
    struct tm lt;
    localtime_r(&start, &lt);
    lt.tm_hour = 23; lt.tm_min = 59; lt.tm_sec = 59;
    time_t nextMidnight = mktime(&lt) + 1;
    time_t chunk = (end < nextMidnight) ? end : nextMidnight;
    usageAddToDay(start, (uint32_t)(chunk - start));
    start = chunk;
  }
}

static void usageRecord(time_t start, time_t end) {
  if (end <= start) return;
  usageAddSpan(start, end);
  usageCycles[usageHead].start = (uint32_t)start;
  usageCycles[usageHead].secs = (uint32_t)(end - start);
  usageHead = (usageHead + 1) % USAGE_CYCLES;
}

static void usageSaveCurrent() { // small, frequent write
  prefs.begin("usage", false);
  prefs.putULong("cur", (uint32_t)usageCurStart);
  prefs.putULong("chk", (uint32_t)usageCurCheck);
  prefs.end();
}

static void usageSaveAll() { // on cycle completion only
  prefs.begin("usage", false);
  prefs.putBytes("days", usageDays, sizeof(usageDays));
  prefs.putBytes("cycles", usageCycles, sizeof(usageCycles));
  prefs.putUChar("head", usageHead);
  prefs.putULong("cur", (uint32_t)usageCurStart);
  prefs.putULong("chk", (uint32_t)usageCurCheck);
  prefs.end();
}

static void usageLoad() {
  prefs.begin("usage", false);
  if (prefs.getBytesLength("days") == sizeof(usageDays))
    prefs.getBytes("days", usageDays, sizeof(usageDays));
  if (prefs.getBytesLength("cycles") == sizeof(usageCycles))
    prefs.getBytes("cycles", usageCycles, sizeof(usageCycles));
  usageHead = prefs.getUChar("head", 0) % USAGE_CYCLES;
  usageCurStart = (time_t)prefs.getULong("cur", 0);
  usageCurCheck = (time_t)prefs.getULong("chk", 0);
  prefs.end();
  // Power was lost mid-cycle: close it at the last checkpoint so the run
  // still counts (up to 5 min of it may be missing).
  if (usageCurStart && usageCurCheck > usageCurStart) {
    usageRecord(usageCurStart, usageCurCheck);
    usageCurStart = usageCurCheck = 0;
    usageSaveAll();
  }
}

static uint32_t usageTodaySecs() {
  if (!clockReady()) return 0;
  time_t nowT = time(nullptr);
  uint32_t key = dateKey(nowT), secs = 0;
  for (uint8_t i = 0; i < USAGE_DAYS; i++)
    if (usageDays[i].date == key) secs = usageDays[i].secs;
  if (usageCurStart) { // add the part of the open cycle that falls today
    struct tm lt;
    localtime_r(&nowT, &lt);
    lt.tm_hour = 0; lt.tm_min = 0; lt.tm_sec = 0;
    time_t midnight = mktime(&lt);
    time_t from = (usageCurStart > midnight) ? usageCurStart : midnight;
    if (nowT > from) secs += (uint32_t)(nowT - from);
  }
  return secs;
}

static void usageTick() {
  if (!clockReady()) return; // can't timestamp anything without NTP
  bool on = relay1On || relay2On;
  time_t nowT = time(nullptr);
  if (on && !usageCurStart) {
    usageCurStart = usageCurCheck = nowT;
    usageSaveCurrent();
  } else if (on) {
    if (nowT - usageCurCheck >= 300) { // checkpoint every 5 min
      usageCurCheck = nowT;
      usageSaveCurrent();
    }
  } else if (usageCurStart) {
    usageRecord(usageCurStart, nowT);
    usageCurStart = usageCurCheck = 0;
    usageSaveAll();
  }
}

// ---------- Physical button ----------
// Actions: "t15"/"t30"/"t60" = start timer, "on" = both ON (no timer),
// "off" = everything off (cancels timer), "none" = ignore the press.
String btnActionOff = "t30"; // pressed while everything is off
String btnActionOn = "off";  // pressed while something is on

static void loadButtonConfig() {
  prefs.begin("btn", false);
  btnActionOff = prefs.getString("aoff", "t30");
  btnActionOn = prefs.getString("aon", "off");
  prefs.end();
}

static void saveButtonConfig() {
  prefs.begin("btn", false);
  prefs.putString("aoff", btnActionOff);
  prefs.putString("aon", btnActionOn);
  prefs.end();
}

static void runButtonAction(const String &a) {
  // The physical button is exempt from the rate limit: someone standing at
  // the device must always be able to control it.
  if (a == "t15") startTimer(15);
  else if (a == "t30") startTimer(30);
  else if (a == "t60") startTimer(60);
  else if (a == "on") { setRelay1(true); setRelay2(true); }
  else if (a == "off") {
    if (timerActive) stopTimer(true);
    else { setRelay1(false); setRelay2(false); }
  }
  // "none": do nothing
}

static void handleButton() {
  static bool lastStable = HIGH;
  static bool lastRead = HIGH;
  static unsigned long lastChangeMs = 0;
  bool r = digitalRead(BUTTON_PIN);
  if (r != lastRead) {
    lastRead = r;
    lastChangeMs = millis();
  }
  if (millis() - lastChangeMs > 40 && r != lastStable) {
    lastStable = r;
    if (r == LOW) { // pressed (button pulls the pin to GND)
      runButtonAction((relay1On || relay2On) ? btnActionOn : btnActionOff);
    }
  }
}

// ---------- Relay polarity persistence ----------
static void loadRelayConfig() {
  prefs.begin("relay", false);
  relay1ActiveLow = prefs.getBool("inv1", false);
  relay2ActiveLow = prefs.getBool("inv2", false);
  prefs.end();
}

static void saveRelayConfig() {
  prefs.begin("relay", false);
  prefs.putBool("inv1", relay1ActiveLow);
  prefs.putBool("inv2", relay2ActiveLow);
  prefs.end();
}

// ---------- Telegram bot ----------
static String tgStatusText() {
  String t = "Relay 1 (IO7): ";
  t += relay1On ? "ON" : "off";
  t += "\nRelay 2 (IO10): ";
  t += relay2On ? "ON" : "off";
  uint32_t rem = timerRemainingSecs();
  if (rem > 0) {
    t += "\nTimer: " + String(rem / 60) + "m " + String(rem % 60) + "s left (both relays ON)";
  } else {
    t += "\nTimer: off";
  }
  if (WiFi.status() == WL_CONNECTED) {
    t += "\nWiFi: " + WiFi.SSID() + " (" + WiFi.localIP().toString() + ")";
  }
  return t;
}

static String tgMenuKeyboard() {
  String r1 = relay1On ? "Relay 1: ON \xE2\x9C\x85" : "Relay 1: off";
  String r2 = relay2On ? "Relay 2: ON \xE2\x9C\x85" : "Relay 2: off";
  String kb = "[";
  kb += "[{\"text\":\"" + r1 + "\",\"callback_data\":\"r1\"},";
  kb += "{\"text\":\"" + r2 + "\",\"callback_data\":\"r2\"}],";
  kb += "[{\"text\":\"Both ON\",\"callback_data\":\"allon\"},";
  kb += "{\"text\":\"Both OFF\",\"callback_data\":\"alloff\"}],";
  kb += "[{\"text\":\"Timer 15m\",\"callback_data\":\"t15\"},";
  kb += "{\"text\":\"Timer 30m\",\"callback_data\":\"t30\"},";
  kb += "{\"text\":\"Timer 60m\",\"callback_data\":\"t60\"}],";
  kb += "[{\"text\":\"Cancel timer\",\"callback_data\":\"tcancel\"},";
  kb += "{\"text\":\"\xF0\x9F\x94\x84 Refresh\",\"callback_data\":\"menu\"}]";
  kb += "]";
  return kb;
}

static const char TG_HELP[] =
    "Water Heater bot\n\n"
    "/menu - control panel with buttons\n"
    "/status - current state\n"
    "/r1on /r1off - relay 1\n"
    "/r2on /r2off - relay 2\n"
    "/allon /alloff - both relays\n"
    "/timer N - both relays ON for N minutes (1-720)\n"
    "/cancel - cancel timer (relays off)\n"
    "/polarity R high|low - what ON drives on pin (R = 1 or 2)\n"
    "/chatid - show this chat's ID";

static void tgAfterManualChange() {
  if (timerActive && !relay1On && !relay2On) stopTimer(false);
}

static void tgSendMenu(UniversalTelegramBot *b, const String &chat) {
  b->sendMessageWithInlineKeyboard(chat, tgStatusText(), "", tgMenuKeyboard());
}

static const char *TG_RATE_MSG = "Rate limited: max 10 changes per 5 min";

// Returns short feedback for callback buttons, or "" if data was unknown.
static String tgApplyAction(const String &data) {
  if (data == "menu") return "Refreshed";
  bool known = (data == "r1" || data == "r2" || data == "allon" || data == "alloff" ||
                data == "t15" || data == "t30" || data == "t60" || data == "tcancel");
  if (!known) return "";
  bool turnsOn = (data == "allon" || data == "t15" || data == "t30" || data == "t60"
                  || (data == "r1" && !relay1On) || (data == "r2" && !relay2On));
  if (!tryChange(turnsOn)) return TG_RATE_MSG;
  if (data == "r1") { setRelay1(!relay1On); tgAfterManualChange(); return relay1On ? "Relay 1 ON" : "Relay 1 off"; }
  if (data == "r2") { setRelay2(!relay2On); tgAfterManualChange(); return relay2On ? "Relay 2 ON" : "Relay 2 off"; }
  if (data == "allon") { setRelay1(true); setRelay2(true); return "Both relays ON"; }
  if (data == "alloff") { setRelay1(false); setRelay2(false); tgAfterManualChange(); return "Both relays off"; }
  if (data == "t15") { startTimer(15); return "Timer set: 15 min"; }
  if (data == "t30") { startTimer(30); return "Timer set: 30 min"; }
  if (data == "t60") { startTimer(60); return "Timer set: 60 min"; }
  if (data == "tcancel") { stopTimer(true); return "Timer cancelled, relays off"; }
  return "";
}

static void tgHandleMessages(UniversalTelegramBot *b, int count, const String &allowedChat) {
  for (int i = 0; i < count; i++) {
    telegramMessage &m = b->messages[i];
    String chat = m.chat_id;

    // Remember who talked to us last so the settings page's
    // "Get chat ID" button can pick it up (even unauthorized chats,
    // so the button works during first-time setup).
    xSemaphoreTake(tgMutex, portMAX_DELAY);
    tgLastChatId = chat;
    xSemaphoreGive(tgMutex);

    if (allowedChat.length() && chat != allowedChat) {
      if (m.type != "callback_query") {
        b->sendMessage(chat, "Not authorized. This chat ID: " + chat, "");
      }
      continue;
    }

    if (m.type == "callback_query") {
      String feedback = tgApplyAction(m.text);
      b->answerCallbackQuery(m.query_id, feedback.length() ? feedback : "?");
      tgSendMenu(b, chat); // fresh panel reflecting the new state
      continue;
    }

    String cmd = m.text;
    int at = cmd.indexOf('@'); // strip "@botname" suffixes in groups
    int sp = cmd.indexOf(' ');
    if (at > 0 && (sp < 0 || at < sp)) cmd = cmd.substring(0, at) + (sp > 0 ? m.text.substring(sp) : "");

    if (cmd == "/start" || cmd == "/help") {
      b->sendMessage(chat, TG_HELP, "");
      tgSendMenu(b, chat);
    } else if (cmd == "/menu") {
      tgSendMenu(b, chat);
    } else if (cmd == "/status") {
      b->sendMessage(chat, tgStatusText(), "");
    } else if (cmd == "/chatid") {
      b->sendMessage(chat, "Chat ID: " + chat, "");
    } else if (cmd == "/r1on") {
      b->sendMessage(chat, tryChange(true) ? (setRelay1(true), "Relay 1 ON") : TG_RATE_MSG, "");
    } else if (cmd == "/r1off") {
      tryChange(false); setRelay1(false); tgAfterManualChange(); b->sendMessage(chat, "Relay 1 off", "");
    } else if (cmd == "/r2on") {
      b->sendMessage(chat, tryChange(true) ? (setRelay2(true), "Relay 2 ON") : TG_RATE_MSG, "");
    } else if (cmd == "/r2off") {
      tryChange(false); setRelay2(false); tgAfterManualChange(); b->sendMessage(chat, "Relay 2 off", "");
    } else if (cmd == "/allon") {
      if (tryChange(true)) { setRelay1(true); setRelay2(true); b->sendMessage(chat, "Both relays ON", ""); }
      else b->sendMessage(chat, TG_RATE_MSG, "");
    } else if (cmd == "/alloff") {
      tryChange(false); setRelay1(false); setRelay2(false); tgAfterManualChange();
      b->sendMessage(chat, "Both relays off", "");
    } else if (cmd == "/cancel") {
      tryChange(false); stopTimer(true); b->sendMessage(chat, "Timer cancelled, relays off", "");
    } else if (cmd.startsWith("/timer")) {
      long mins = cmd.substring(6).toInt();
      if (mins >= 1 && mins <= 720) {
        if (tryChange(true)) {
          startTimer((uint32_t)mins);
          b->sendMessage(chat, "Timer set: both relays ON for " + String(mins) + " min", "");
        } else {
          b->sendMessage(chat, TG_RATE_MSG, "");
        }
      } else {
        b->sendMessage(chat, "Usage: /timer N  (minutes, 1-720)", "");
      }
    } else if (cmd.startsWith("/polarity")) {
      String args = cmd.substring(9);
      args.trim();
      int relay = args.startsWith("1") ? 1 : (args.startsWith("2") ? 2 : 0);
      bool low = args.endsWith("low");
      bool high = args.endsWith("high");
      if (relay && (low || high)) {
        if (relay == 1) relay1ActiveLow = low; else relay2ActiveLow = low;
        saveRelayConfig();
        setRelay1(relay1On);
        setRelay2(relay2On);
        b->sendMessage(chat, "Relay " + String(relay) + ": ON = " + (low ? "LOW" : "HIGH"), "");
      } else {
        b->sendMessage(chat, "Usage: /polarity 1 high  or  /polarity 2 low", "");
      }
    } else {
      b->sendMessage(chat, "Unknown command. /help for the list.", "");
    }
  }
}

static void telegramTask(void *) {
  static WiFiClientSecure client;
  // Unverified (but still encrypted) TLS: the CA that api.telegram.org uses
  // rotates over the years (the one bundled with the library is already
  // outdated -> X509 verify errors), and full chain verification needs extra
  // heap the C2 can't spare during handshakes.
  client.setInsecure();
  UniversalTelegramBot *b = nullptr;
  String curToken;

  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(b ? 1000 : 3000));
    if (WiFi.status() != WL_CONNECTED) continue;

    xSemaphoreTake(tgMutex, portMAX_DELAY);
    String token = tgEnabled ? tgToken : ""; // disabled -> same as no token
    String allowedChat = tgChat;
    xSemaphoreGive(tgMutex);

    if (token != curToken) {
      if (b) {
        delete b;
        b = nullptr;
        Serial.println("Telegram bot stopped");
      }
      b = token.length() ? new UniversalTelegramBot(token, client) : nullptr;
      curToken = token;
      if (b) {
        // Long polling: one HTTPS request waits up to 20s for a message
        // instead of a fresh TLS handshake every couple of seconds. Kinder
        // to the C2's heap and reacts to commands instantly.
        b->longPoll = 20;
        Serial.printf("Telegram bot started (free heap: %lu)\n",
                      (unsigned long)ESP.getFreeHeap());
      }
    }
    if (!b) continue;

    // A TLS handshake needs a big chunk of heap; skip this round instead of
    // failing allocations when memory is momentarily tight. (The C2 idles
    // around 50KB free with AP+STA up, so this can't be much higher.)
    if (ESP.getFreeHeap() < 40000) continue;

    int n = b->getUpdates(b->last_message_received + 1);
    while (n) {
      // Re-check the settings-page checkbox: a message that arrived while
      // the long poll was in flight must NOT be handled after a disable.
      xSemaphoreTake(tgMutex, portMAX_DELAY);
      bool stillEnabled = tgEnabled;
      xSemaphoreGive(tgMutex);
      if (!stillEnabled) break;
      tgHandleMessages(b, n, allowedChat);
      n = b->getUpdates(b->last_message_received + 1);
    }
  }
}

// ---------- WiFi (station) persistence ----------
static void loadWifiConfig() {
  prefs.begin("wifi", false);
  staSsid = prefs.getString("ssid", "");
  staPass = prefs.getString("pass", "");
  prefs.end();
}

static void saveWifiConfig(const String &ssid, const String &pass) {
  prefs.begin("wifi", false);
  prefs.putString("ssid", ssid);
  prefs.putString("pass", pass);
  prefs.end();
  staSsid = ssid;
  staPass = pass;
}

static void clearWifiConfig() {
  prefs.begin("wifi", false);
  prefs.clear();
  prefs.end();
  staSsid = "";
  staPass = "";
}

static void connectSta() {
  if (staSsid.length() == 0) return;
  WiFi.setAutoReconnect(true);
  WiFi.begin(staSsid.c_str(), staPass.c_str());
}

// ---------- Web UI: main page (relay controls) ----------
static const char INDEX_HTML[] PROGMEM = R"rawliteral(
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Water Heater</title>
<style>
  :root { color-scheme: light dark; }
  body {
    font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
    background: #111827; color: #f9fafb;
    margin: 0; padding: 24px;
    display: flex; flex-direction: column; align-items: center; gap: 16px;
  }
  header { width: 100%; max-width: 360px; display: flex; align-items: center; justify-content: space-between; }
  h1 { margin: 0; font-size: 1.5rem; }
  a.gear { color: #9ca3af; text-decoration: none; font-size: 1.5rem; }
  .card {
    background: #1f2937; border-radius: 16px; padding: 20px;
    width: 100%; max-width: 360px; box-sizing: border-box;
    display: flex; align-items: center; justify-content: space-between;
    box-shadow: 0 2px 8px rgba(0,0,0,.4);
  }
  .label { font-size: 1.1rem; font-weight: 600; }
  .state { font-size: .85rem; color: #9ca3af; margin-top: 4px; }
  .on .state { color: #34d399; }
  button {
    border: 0; border-radius: 12px; padding: 12px 20px;
    font-size: 1rem; font-weight: 600; cursor: pointer;
    background: #374151; color: #f9fafb;
    transition: background .15s;
  }
  .on button.toggle { background: #059669; }
  .both { display: flex; gap: 12px; width: 100%; max-width: 360px; }
  .both button { flex: 1; padding: 14px 0; }
  .both .allon  { background: #059669; }
  .both .alloff { background: #b91c1c; }
  #conn { font-size: .8rem; color: #9ca3af; }
  .card.block { display: block; }
  .card.block h2 { font-size: 1rem; margin: 0; }
  .theader { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
  #tstate { font-size: .8rem; color: #34d399; }
  .quick { display: flex; gap: 10px; margin-bottom: 14px; }
  .quick button { flex: 1; padding: 12px 0; }
  input[type=range] { width: 100%; accent-color: #2563eb; margin: 2px 0 10px; }
  .sliderrow { display: flex; align-items: center; justify-content: space-between; }
  .sliderrow span { color: #9ca3af; font-size: .95rem; }
  .start { background: #2563eb; padding: 12px 28px; }
  .cancel { background: #b91c1c; width: 100%; margin-top: 4px; }
  .countdown {
    font-size: 2.4rem; font-weight: 700; text-align: center;
    margin: 6px 0 12px; font-variant-numeric: tabular-nums; color: #34d399;
  }
  .hidden { display: none; }
</style>
</head>
<body>
<header>
  <h1>Water Heater</h1>
  <a class="gear" href="/settings" title="Settings">&#9881;</a>
</header>

<div class="card" id="card1">
  <div>
    <div class="label">Relay 1 <small>(IO7)</small></div>
    <div class="state" id="state1">off</div>
  </div>
  <button class="toggle" onclick="send('1','toggle')">Toggle</button>
</div>

<div class="card" id="card2">
  <div>
    <div class="label">Relay 2 <small>(IO10)</small></div>
    <div class="state" id="state2">off</div>
  </div>
  <button class="toggle" onclick="send('2','toggle')">Toggle</button>
</div>

<div class="both">
  <button class="allon"  onclick="send('all','on')">Both ON</button>
  <button class="alloff" onclick="send('all','off')">Both OFF</button>
</div>

<div class="card block">
  <div class="theader">
    <h2>Timer</h2>
    <span id="tstate"></span>
  </div>
  <div id="countdown" class="countdown hidden">--:--</div>
  <div id="tcontrols">
    <div class="quick">
      <button onclick="startTimer(15)">15 min</button>
      <button onclick="startTimer(30)">30 min</button>
    </div>
    <input type="range" id="tslider" min="15" max="120" step="5" value="45"
           oninput="document.getElementById('tlabel').textContent = this.value + ' min'">
    <div class="sliderrow">
      <span id="tlabel">45 min</span>
      <button class="start" onclick="startTimer(+document.getElementById('tslider').value)">Start</button>
    </div>
  </div>
  <button id="tcancel" class="cancel hidden" onclick="cancelTimer()">Cancel timer</button>
</div>

<div id="conn"></div>

<script>
let timerEnd = 0; // local ms timestamp when the timer ends; 0 = no timer

function render(s) {
  document.getElementById('card1').classList.toggle('on', s.relay1);
  document.getElementById('card2').classList.toggle('on', s.relay2);
  document.getElementById('state1').textContent = s.relay1 ? 'ON' : 'off';
  document.getElementById('state2').textContent = s.relay2 ? 'ON' : 'off';
  timerEnd = s.timer > 0 ? Date.now() + s.timer * 1000 : 0;
  renderTimer();
}

function renderTimer() {
  const active = timerEnd > Date.now();
  document.getElementById('countdown').classList.toggle('hidden', !active);
  document.getElementById('tcancel').classList.toggle('hidden', !active);
  document.getElementById('tcontrols').classList.toggle('hidden', active);
  document.getElementById('tstate').textContent = active ? 'both relays ON' : '';
  if (active) {
    const s = Math.max(0, Math.round((timerEnd - Date.now()) / 1000));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
    document.getElementById('countdown').textContent =
      (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(ss).padStart(2, '0');
  }
}
setInterval(renderTimer, 500);

async function startTimer(mins) {
  try {
    const r = await fetch(`/timer/set?mins=${mins}`);
    const j = await r.json();
    if (j.error) { document.getElementById('conn').textContent = j.error; return; }
    render(j);
    document.getElementById('conn').textContent = '';
  } catch (e) {}
}

async function cancelTimer() {
  try {
    const r = await fetch('/timer/cancel');
    render(await r.json());
  } catch (e) {}
}
async function send(relay, state) {
  try {
    const r = await fetch(`/set?relay=${relay}&state=${state}`);
    const j = await r.json();
    if (j.error) { document.getElementById('conn').textContent = j.error; return; }
    render(j);
    document.getElementById('conn').textContent = '';
  } catch (e) {
    document.getElementById('conn').textContent = 'connection lost';
  }
}
async function refresh() {
  try {
    const r = await fetch('/state');
    render(await r.json());
    document.getElementById('conn').textContent = '';
  } catch (e) {
    document.getElementById('conn').textContent = 'connection lost';
  }
}
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
)rawliteral";

// ---------- Web UI: settings page (WiFi) ----------
static const char SETTINGS_HTML[] PROGMEM = R"rawliteral(
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Water Heater Settings</title>
<style>
  :root { color-scheme: light dark; }
  body {
    font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
    background: #111827; color: #f9fafb;
    margin: 0; padding: 24px;
    display: flex; flex-direction: column; align-items: center; gap: 16px;
  }
  header { width: 100%; max-width: 360px; display: flex; align-items: center; gap: 12px; }
  h1 { margin: 0; font-size: 1.3rem; }
  a.back { color: #9ca3af; text-decoration: none; font-size: 1.4rem; }
  .card {
    background: #1f2937; border-radius: 16px; padding: 20px;
    width: 100%; max-width: 360px; box-sizing: border-box;
    box-shadow: 0 2px 8px rgba(0,0,0,.4);
  }
  h2 { font-size: 1rem; margin: 0 0 10px; }
  #wifistate { font-size: .85rem; color: #9ca3af; margin-bottom: 12px; }
  #wifistate.ok { color: #34d399; }
  .nethead { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
  .nethead span { font-size: .85rem; color: #9ca3af; }
  #networks { margin-bottom: 12px; max-height: 260px; overflow-y: auto; }
  .net {
    padding: 10px 12px; border-radius: 10px; cursor: pointer; font-size: .95rem;
    background: #111827; margin-bottom: 6px;
    display: flex; justify-content: space-between; align-items: center;
    border: 1px solid transparent;
  }
  .net.sel { border-color: #2563eb; }
  .net .meta { color: #9ca3af; font-size: .8rem; display: flex; gap: 8px; align-items: center; }
  .bars { letter-spacing: 1px; }
  input {
    width: 100%; box-sizing: border-box; margin: 4px 0 12px;
    padding: 10px; border-radius: 10px; border: 1px solid #374151;
    background: #111827; color: #f9fafb; font-size: 1rem;
  }
  button {
    border: 0; border-radius: 12px; padding: 12px 0;
    font-size: .95rem; font-weight: 600; cursor: pointer;
    background: #374151; color: #f9fafb; width: 100%;
  }
  .row { display: flex; gap: 10px; }
  .save { background: #2563eb; }
  .forget { background: #7f1d1d; }
  .hidden { display: none; }
  .spin { color: #9ca3af; font-size: .85rem; padding: 8px 0; }
  .hint { font-size: .8rem; color: #9ca3af; margin-bottom: 12px; }
  .checkrow {
    display: flex; align-items: center; gap: 10px;
    margin-bottom: 12px; font-size: .95rem; cursor: pointer;
  }
  .checkrow input { width: 18px; height: 18px; margin: 0; accent-color: #2563eb; }
  .polrow {
    display: flex; align-items: center; justify-content: space-between;
    gap: 10px; margin-bottom: 10px; font-size: .95rem;
  }
  select {
    padding: 8px 10px; border-radius: 10px; border: 1px solid #374151;
    background: #111827; color: #f9fafb; font-size: .9rem;
  }
</style>
</head>
<body>
<header>
  <a class="back" href="/" title="Back">&#8592;</a>
  <h1>Settings</h1>
</header>

<div class="card">
  <h2>Home WiFi</h2>
  <div id="wifistate">not configured</div>
  <label class="checkrow"><input type="checkbox" id="apoff" onchange="saveApOff()">
    Turn off the WaterHeater AP while connected to home WiFi</label>
  <div class="hint">AP shuts down ~60s after WiFi connects and comes back
    automatically if WiFi drops. If you are connected via the AP right now,
    enabling this will disconnect you.</div>

  <div class="nethead">
    <span>Nearby networks</span>
    <span id="scanstate"></span>
  </div>
  <div id="networks"><div class="spin">scanning&hellip;</div></div>
  <button onclick="scan(true)">Rescan</button>
</div>

<div class="card hidden" id="joincard">
  <h2 id="jointitle">Connect</h2>
  <input id="pass" placeholder="Password" type="password" autocomplete="off">
  <div class="row">
    <button class="save" onclick="saveWifi()">Save &amp; Connect</button>
  </div>
</div>

<div class="card">
  <div class="row">
    <button class="forget" onclick="forgetWifi()">Forget saved network</button>
  </div>
</div>

<div class="card">
  <h2>Telegram bot</h2>
  <div class="hint">Create a bot with @BotFather and paste its token here.
    The optional chat ID limits who can control the relay (send /chatid to the
    bot to find yours). Works while connected to home WiFi.</div>
  <label class="checkrow"><input type="checkbox" id="tgen" checked onchange="saveTg()"> Telegram bot enabled</label>
  <input id="tgtoken" placeholder="Bot token" autocomplete="off">
  <input id="tgchat" placeholder="Allowed chat ID (optional)" autocomplete="off">
  <div class="row">
    <button onclick="getChatId()">Get chat ID</button>
    <button class="save" onclick="saveTg()">Save</button>
  </div>
  <div class="hint" id="tgsaved" style="margin-top:10px"></div>
</div>

<div class="card">
  <h2>Daily schedules</h2>
  <div class="hint">Up to 5 daily heating times (device local time).
    Each enabled row turns both relays on at its time, for its duration.</div>
  <div id="schedrows"></div>
  <div class="row">
    <button class="save" onclick="saveSched()">Save schedules</button>
  </div>
  <div class="hint" id="schstate" style="margin-top:10px"></div>
</div>

<div class="card">
  <h2>Device</h2>
  <div class="hint">Name used for http://&lt;name&gt;.local, the network
    (DHCP) name and OTA. Letters, digits and dashes only.</div>
  <input id="host" placeholder="Hostname" autocomplete="off">
  <div class="polrow">
    <span>Max continuous ON (min, 0 = off)</span>
    <input type="number" id="maxon" min="0" max="1440" step="5"
           style="width:90px;margin:0">
  </div>
  <input id="tz" placeholder="Timezone (e.g. Asia/Jerusalem)" autocomplete="off">
  <div class="hint" id="devtime"></div>
  <div class="row">
    <button class="save" onclick="saveSys()">Save</button>
    <button class="forget" onclick="rebootDev()">Reboot</button>
  </div>
  <div class="hint" id="sysstate" style="margin-top:10px"></div>
</div>

<div class="card">
  <h2>Firmware</h2>
  <div class="hint" id="fwver">version: &hellip;</div>
  <input type="file" id="fwfile" accept=".bin">
  <div class="row">
    <button class="save" onclick="doOta()">Update firmware</button>
  </div>
  <div class="hint" id="otastate" style="margin-top:10px"></div>
</div>

<div class="card">
  <h2>Button</h2>
  <div class="hint">What a press of the physical button (IO4) does,
    depending on whether the relays are currently off or on.
    The LED (IO3) is lit while anything is on.</div>
  <div class="polrow">
    <span>When off, press:</span>
    <select id="aoff" onchange="saveBtn()">
      <option value="t30">Starts 30 min timer</option>
      <option value="t15">Starts 15 min timer</option>
      <option value="t60">Starts 60 min timer</option>
      <option value="on">Turns both ON</option>
      <option value="none">Does nothing</option>
    </select>
  </div>
  <div class="polrow">
    <span>When on, press:</span>
    <select id="aon" onchange="saveBtn()">
      <option value="off">Turns everything off</option>
      <option value="t30">Restarts 30 min timer</option>
      <option value="t15">Restarts 15 min timer</option>
      <option value="t60">Restarts 60 min timer</option>
      <option value="none">Does nothing</option>
    </select>
  </div>
</div>

<div class="card">
  <h2>Status LED</h2>
  <div class="polrow">
    <span>When heating</span>
    <select id="ledmode" onchange="saveLed()">
      <option value="blink">Blink</option>
      <option value="still">Steady</option>
    </select>
  </div>
  <div class="polrow">
    <span>Brightness</span>
    <span id="brival">100%</span>
  </div>
  <input type="range" id="ledbri" min="5" max="100" step="5" value="100"
         oninput="document.getElementById('brival').textContent = this.value + '%'"
         onchange="saveLed()">
</div>

<div class="card">
  <h2>Relay outputs</h2>
  <div class="hint">What the pin does when a relay is switched ON.
    Pick LOW if your relay module closes on a low signal (active-low).</div>
  <div class="polrow">
    <span>Relay 1 (IO7) &mdash; ON =</span>
    <select id="pol1" onchange="savePolarity()">
      <option value="high">HIGH (closed)</option>
      <option value="low">LOW (closed)</option>
    </select>
  </div>
  <div class="polrow">
    <span>Relay 2 (IO10) &mdash; ON =</span>
    <select id="pol2" onchange="savePolarity()">
      <option value="high">HIGH (closed)</option>
      <option value="low">LOW (closed)</option>
    </select>
  </div>
</div>

<script>
let selected = '';
let selectedOpen = false;
let scanTimer = null;
let current = {ssid: '', connected: false};
let lastList = [];

function bars(rssi) {
  if (rssi >= -55) return '&#9601;&#9603;&#9605;&#9607;';
  if (rssi >= -67) return '&#9601;&#9603;&#9605;';
  if (rssi >= -78) return '&#9601;&#9603;';
  return '&#9601;';
}

function renderWifi(w) {
  current = w;
  const el = document.getElementById('wifistate');
  if (!w.ssid) { el.textContent = 'not configured'; el.classList.remove('ok'); }
  else if (w.connected) {
    el.textContent = `connected to ${w.ssid} — http://${w.ip} (${w.host}.local)`;
    el.classList.add('ok');
  } else {
    el.textContent = `trying to connect to ${w.ssid}…`;
    el.classList.remove('ok');
  }
}

function renderNetworks(list) {
  const box = document.getElementById('networks');
  box.innerHTML = '';
  if (!list.length) {
    box.innerHTML = '<div class="spin">no networks found</div>';
    return;
  }
  list.sort((a, b) => b.rssi - a.rssi);
  list.forEach(n => {
    if (!n.ssid) return; // skip hidden networks
    const d = document.createElement('div');
    d.className = 'net' + (n.ssid === selected ? ' sel' : '');
    const name = document.createElement('div');
    name.textContent = n.ssid + (n.ssid === current.ssid && current.connected ? ' ✓' : '');
    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.innerHTML = `${n.open ? 'open' : '&#128274;'} <span class="bars">${bars(n.rssi)}</span>`;
    d.appendChild(name);
    d.appendChild(meta);
    d.onclick = () => select(n);
    box.appendChild(d);
  });
}

function select(n) {
  selected = n.ssid;
  selectedOpen = n.open;
  document.getElementById('joincard').classList.remove('hidden');
  document.getElementById('jointitle').textContent = `Connect to ${n.ssid}`;
  const pass = document.getElementById('pass');
  pass.classList.toggle('hidden', n.open);
  pass.value = '';
  if (!n.open) pass.focus();
  renderNetworks(lastList); // refresh selection highlight
}

async function scan(fresh) {
  if (fresh) {
    document.getElementById('networks').innerHTML = '<div class="spin">scanning&hellip;</div>';
  }
  document.getElementById('scanstate').textContent = 'scanning…';
  try {
    const r = await fetch('/wifi/scan');
    const j = await r.json();
    if (j.pending) {
      clearTimeout(scanTimer);
      scanTimer = setTimeout(() => scan(false), 1500);
      return;
    }
    document.getElementById('scanstate').textContent = '';
    lastList = j.networks;
    renderNetworks(lastList);
  } catch (e) {
    document.getElementById('scanstate').textContent = 'scan failed';
  }
}

async function saveWifi() {
  if (!selected) return;
  const pass = selectedOpen ? '' : document.getElementById('pass').value;
  const body = new URLSearchParams({ssid: selected, pass});
  await fetch('/wifi/save', {method: 'POST', body});
  document.getElementById('wifistate').textContent = `connecting to ${selected}…`;
  document.getElementById('joincard').classList.add('hidden');
}

async function forgetWifi() {
  await fetch('/wifi/forget', {method: 'POST'});
  refreshStatus();
}

async function refreshStatus() {
  try {
    const r = await fetch('/wifi/status');
    renderWifi(await r.json());
  } catch (e) {}
}

async function loadPolarity() {
  try {
    const r = await fetch('/relaycfg/get');
    const j = await r.json();
    document.getElementById('pol1').value = j.activelow1 ? 'low' : 'high';
    document.getElementById('pol2').value = j.activelow2 ? 'low' : 'high';
  } catch (e) {}
}

async function savePolarity() {
  const body = new URLSearchParams({
    activelow1: document.getElementById('pol1').value === 'low' ? '1' : '0',
    activelow2: document.getElementById('pol2').value === 'low' ? '1' : '0'
  });
  await fetch('/relaycfg/set', {method: 'POST', body});
}

async function loadTg() {
  try {
    const r = await fetch('/tg/get');
    const j = await r.json();
    document.getElementById('tgtoken').value = j.token;
    document.getElementById('tgchat').value = j.chat;
    document.getElementById('tgen').checked = j.enabled;
  } catch (e) {}
}

async function loadSys() {
  try {
    const r = await fetch('/sys/get');
    const j = await r.json();
    document.getElementById('host').value = j.host;
    document.getElementById('tz').value = j.tz;
    document.getElementById('maxon').value = j.maxon;
    document.getElementById('apoff').checked = j.apoff;
    renderDevTime(j);
  } catch (e) {}
}

async function saveApOff() {
  const body = new URLSearchParams({
    apoff: document.getElementById('apoff').checked ? '1' : '0'
  });
  await fetch('/sys/set', {method: 'POST', body});
}

function renderDevTime(j) {
  const el = document.getElementById('devtime');
  if (!j.tzok && j.tz.indexOf(',') < 0) {
    el.textContent = 'unknown timezone name — using it as a raw POSIX TZ';
  } else if (j.time) {
    el.textContent = 'device local time: ' + j.time;
  } else {
    el.textContent = 'device clock not synced yet (needs internet/NTP)';
  }
}

async function saveSys() {
  const body = new URLSearchParams({
    host: document.getElementById('host').value.trim(),
    tz: document.getElementById('tz').value.trim(),
    maxon: document.getElementById('maxon').value
  });
  const r = await fetch('/sys/set', {method: 'POST', body});
  const j = await r.json();
  document.getElementById('host').value = j.host;
  renderDevTime(j);
  const el = document.getElementById('sysstate');
  el.textContent = `saved — http://${j.host}.local (network & OTA name apply after reboot)`;
  setTimeout(() => { el.textContent = ''; }, 6000);
}

const SCHED_SLOTS = 5;

function buildSchedRows() {
  const box = document.getElementById('schedrows');
  let html = '';
  for (let i = 0; i < SCHED_SLOTS; i++) {
    html += `<div class="polrow">
      <label class="checkrow" style="margin:0"><input type="checkbox" id="schen${i}"> ${i + 1}</label>
      <input type="time" id="schtime${i}" value="06:30" style="width:auto;margin:0">
      <span style="display:flex;align-items:center;gap:6px">
        <input type="number" id="schdur${i}" min="5" max="720" step="5" value="30"
               style="width:80px;margin:0"> min</span>
    </div>`;
  }
  box.innerHTML = html;
}

async function loadSched() {
  try {
    const r = await fetch('/sched/get');
    const j = await r.json();
    j.slots.forEach((s, i) => {
      document.getElementById(`schen${i}`).checked = s.en;
      document.getElementById(`schtime${i}`).value =
        String(s.hh).padStart(2, '0') + ':' + String(s.mm).padStart(2, '0');
      document.getElementById(`schdur${i}`).value = s.dur;
    });
  } catch (e) {}
}

async function saveSched() {
  const params = {};
  for (let i = 0; i < SCHED_SLOTS; i++) {
    const [hh, mm] = document.getElementById(`schtime${i}`).value.split(':');
    params[`en${i}`] = document.getElementById(`schen${i}`).checked ? '1' : '0';
    params[`hh${i}`] = hh;
    params[`mm${i}`] = mm;
    params[`dur${i}`] = document.getElementById(`schdur${i}`).value;
  }
  const r = await fetch('/sched/set', {method: 'POST', body: new URLSearchParams(params)});
  const j = await r.json();
  const active = j.slots.filter(s => s.en).length;
  const el = document.getElementById('schstate');
  el.textContent = active ? `saved — ${active} schedule${active > 1 ? 's' : ''} active`
                          : 'saved — no schedules active';
  setTimeout(() => { el.textContent = ''; }, 6000);
}

async function rebootDev() {
  if (!confirm('Reboot the device?')) return;
  const el = document.getElementById('sysstate');
  try {
    await fetch('/reboot', {method: 'POST'});
    el.textContent = 'rebooting — page reloads in a few seconds';
    setTimeout(() => location.reload(), 8000);
  } catch (e) {
    el.textContent = 'reboot request failed';
  }
}

async function loadFw() {
  try {
    const r = await fetch('/fw/info');
    const j = await r.json();
    document.getElementById('fwver').textContent = 'version: ' + j.version;
  } catch (e) {}
}

async function doOta() {
  const f = document.getElementById('fwfile').files[0];
  const el = document.getElementById('otastate');
  if (!f) { el.textContent = 'pick a firmware.bin first'; return; }
  el.textContent = 'uploading & flashing… do not power off';
  const fd = new FormData();
  fd.append('firmware', f);
  try {
    const r = await fetch('/ota', {method: 'POST', body: fd});
    const j = await r.json();
    if (j.ok) {
      el.textContent = 'success — rebooting, page reloads in a few seconds';
      setTimeout(() => location.reload(), 10000);
    } else {
      el.textContent = 'failed: ' + (j.error || 'unknown error');
    }
  } catch (e) {
    el.textContent = 'upload failed (connection lost?)';
  }
}

async function loadLed() {
  try {
    const r = await fetch('/led/get');
    const j = await r.json();
    document.getElementById('ledmode').value = j.blink ? 'blink' : 'still';
    document.getElementById('ledbri').value = j.bri;
    document.getElementById('brival').textContent = j.bri + '%';
  } catch (e) {}
}

async function saveLed() {
  const body = new URLSearchParams({
    blink: document.getElementById('ledmode').value === 'blink' ? '1' : '0',
    bri: document.getElementById('ledbri').value
  });
  await fetch('/led/set', {method: 'POST', body});
}

async function loadBtn() {
  try {
    const r = await fetch('/btncfg/get');
    const j = await r.json();
    document.getElementById('aoff').value = j.aoff;
    document.getElementById('aon').value = j.aon;
  } catch (e) {}
}

async function saveBtn() {
  const body = new URLSearchParams({
    aoff: document.getElementById('aoff').value,
    aon: document.getElementById('aon').value
  });
  await fetch('/btncfg/set', {method: 'POST', body});
}

async function getChatId() {
  const el = document.getElementById('tgsaved');
  try {
    const r = await fetch('/tg/lastchat');
    const j = await r.json();
    if (j.chat) {
      document.getElementById('tgchat').value = j.chat;
      el.textContent = 'chat ID filled in — press Save to lock the bot to it';
    } else {
      el.textContent = 'no chats seen yet — send any message to the bot, then try again';
    }
  } catch (e) {
    el.textContent = 'failed to fetch chat ID';
  }
  setTimeout(() => { el.textContent = ''; }, 6000);
}

async function saveTg() {
  const enabled = document.getElementById('tgen').checked;
  const body = new URLSearchParams({
    token: document.getElementById('tgtoken').value.trim(),
    chat: document.getElementById('tgchat').value.trim(),
    enabled: enabled ? '1' : '0'
  });
  await fetch('/tg/set', {method: 'POST', body});
  const el = document.getElementById('tgsaved');
  el.textContent = enabled ? 'saved — the bot restarts within a few seconds'
                           : 'saved — bot disabled';
  setTimeout(() => { el.textContent = ''; }, 4000);
}

refreshStatus();
setInterval(refreshStatus, 2000);
loadPolarity();
loadLed();
loadBtn();
loadTg();
loadSys();
buildSchedRows();
loadSched();
loadFw();
scan(true); // auto-scan on page load
</script>
</body>
</html>
)rawliteral";

// ---------- HTTP handlers ----------
static String jsonEscape(const String &s) {
  String out;
  for (size_t i = 0; i < s.length(); i++) {
    char c = s[i];
    if (c == '"' || c == '\\') { out += '\\'; out += c; }
    else if ((uint8_t)c >= 0x20) out += c;
  }
  return out;
}

static String stateJson() {
  String json = "{\"relay1\":";
  json += relay1On ? "true" : "false";
  json += ",\"relay2\":";
  json += relay2On ? "true" : "false";
  json += ",\"timer\":" + String(timerRemainingSecs());
  json += ",\"today\":" + String(usageTodaySecs());
  json += "}";
  return json;
}

static void handleUsageGet() {
  String json = "{\"today\":" + String(usageTodaySecs());
  json += ",\"clock\":";
  json += clockReady() ? "true" : "false";
  json += ",\"days\":[";
  bool first = true;
  for (uint8_t i = 0; i < USAGE_DAYS; i++) {
    if (!usageDays[i].date) continue;
    if (!first) json += ",";
    first = false;
    json += "{\"d\":" + String(usageDays[i].date) + ",\"s\":" + String(usageDays[i].secs) + "}";
  }
  json += "],\"cycles\":[";
  first = true;
  for (uint8_t n = 0; n < USAGE_CYCLES; n++) { // oldest first
    const CycleEntry &c = usageCycles[(usageHead + n) % USAGE_CYCLES];
    if (!c.start) continue;
    if (!first) json += ",";
    first = false;
    json += "{\"t\":" + String(c.start) + ",\"s\":" + String(c.secs) + "}";
  }
  json += "]";
  if (usageCurStart) json += ",\"open\":" + String((uint32_t)usageCurStart);
  json += "}";
  server.send(200, "application/json", json);
}

static void handleRoot() {
  server.send_P(200, "text/html", INDEX_HTML);
}

static void handleSettings() {
  server.send_P(200, "text/html", SETTINGS_HTML);
}

static void handleState() {
  server.send(200, "application/json", stateJson());
}

static void handleSet() {
  String relay = server.arg("relay");
  String state = server.arg("state");

  bool targetKnown = (state == "on" || state == "off");
  bool target = (state == "on");

  bool turnsOn;
  if (relay == "1") turnsOn = targetKnown ? target : !relay1On;
  else if (relay == "2") turnsOn = targetKnown ? target : !relay2On;
  else if (relay == "all") turnsOn = targetKnown ? target : !(relay1On && relay2On);
  else {
    server.send(400, "application/json", "{\"error\":\"unknown relay\"}");
    return;
  }
  if (!tryChange(turnsOn)) {
    server.send(429, "application/json", "{\"error\":\"rate limited: max 10 changes per 5 min\"}");
    return;
  }

  if (relay == "1") {
    setRelay1(targetKnown ? target : !relay1On);
  } else if (relay == "2") {
    setRelay2(targetKnown ? target : !relay2On);
  } else if (relay == "all") {
    if (!targetKnown) target = !(relay1On && relay2On); // toggle: on unless both already on
    setRelay1(target);
    setRelay2(target);
  }
  // Manually switching everything off while a timer runs cancels the timer.
  if (timerActive && !relay1On && !relay2On) stopTimer(false);
  server.send(200, "application/json", stateJson());
}

static void handleTimerSet() {
  long mins = server.arg("mins").toInt();
  if (mins < 1 || mins > 720) {
    server.send(400, "application/json", "{\"error\":\"mins must be 1-720\"}");
    return;
  }
  if (!tryChange(true)) {
    server.send(429, "application/json", "{\"error\":\"rate limited: max 10 changes per 5 min\"}");
    return;
  }
  startTimer((uint32_t)mins);
  server.send(200, "application/json", stateJson());
}

static void handleTimerCancel() {
  tryChange(false);
  stopTimer(true);
  server.send(200, "application/json", stateJson());
}

static void handleWifiStatus() {
  bool connected = (WiFi.status() == WL_CONNECTED);
  String json = "{\"ssid\":\"" + jsonEscape(staSsid) + "\"";
  json += ",\"connected\":";
  json += connected ? "true" : "false";
  json += ",\"ip\":\"" + (connected ? WiFi.localIP().toString() : String("")) + "\"";
  json += ",\"rssi\":" + String(connected ? WiFi.RSSI() : 0);
  json += ",\"host\":\"" + hostname + "\"";
  json += "}";
  server.send(200, "application/json", json);
}

static void handleSysGet() {
  String json = "{\"host\":\"" + hostname + "\"";
  json += ",\"tz\":\"" + jsonEscape(tzString) + "\"";
  json += ",\"tzok\":";
  json += tzKnown() ? "true" : "false";
  json += ",\"maxon\":" + String(maxOnMinutes);
  json += ",\"apoff\":";
  json += apOffWhenSta ? "true" : "false";
  time_t nowT = time(nullptr);
  if (nowT > 1600000000) {
    struct tm lt;
    localtime_r(&nowT, &lt);
    char buf[24];
    strftime(buf, sizeof(buf), "%Y-%m-%d %H:%M", &lt);
    json += ",\"time\":\"" + String(buf) + "\"";
  } else {
    json += ",\"time\":\"\"";
  }
  json += "}";
  server.send(200, "application/json", json);
}

static void handleSysSet() {
  if (server.hasArg("host")) {
    String newHost = sanitizeHostname(server.arg("host"));
    if (newHost != hostname) {
      hostname = newHost;
      // Re-announce mDNS under the new name right away; the DHCP and OTA
      // names refresh on the next reboot.
      MDNS.end();
      MDNS.begin(hostname.c_str());
      MDNS.addService("http", "tcp", 80);
    }
  }
  if (server.hasArg("tz") && server.arg("tz").length()) {
    tzString = server.arg("tz");
    applyTimezone();
  }
  if (server.hasArg("maxon")) {
    long m = server.arg("maxon").toInt();
    if (m >= 0 && m <= 1440) maxOnMinutes = (uint16_t)m;
  }
  if (server.hasArg("apoff")) apOffWhenSta = (server.arg("apoff") == "1");
  saveSysConfig();
  handleSysGet();
}

static void handleSchedGet() {
  String json = "{\"slots\":[";
  for (uint8_t i = 0; i < SCHED_SLOTS; i++) {
    if (i) json += ",";
    json += "{\"en\":";
    json += sched[i].en ? "true" : "false";
    json += ",\"hh\":" + String(sched[i].hh);
    json += ",\"mm\":" + String(sched[i].mm);
    json += ",\"dur\":" + String(sched[i].dur);
    json += "}";
  }
  json += "]}";
  server.send(200, "application/json", json);
}

static void handleSchedSet() {
  char key[8];
  for (uint8_t i = 0; i < SCHED_SLOTS; i++) {
    snprintf(key, sizeof(key), "en%u", i);
    if (server.hasArg(key)) sched[i].en = (server.arg(key) == "1");
    snprintf(key, sizeof(key), "hh%u", i);
    if (server.hasArg(key)) {
      long v = server.arg(key).toInt();
      if (v >= 0 && v <= 23) sched[i].hh = (uint8_t)v;
    }
    snprintf(key, sizeof(key), "mm%u", i);
    if (server.hasArg(key)) {
      long v = server.arg(key).toInt();
      if (v >= 0 && v <= 59) sched[i].mm = (uint8_t)v;
    }
    snprintf(key, sizeof(key), "dur%u", i);
    if (server.hasArg(key)) {
      long v = server.arg(key).toInt();
      if (v >= 1 && v <= 720) sched[i].dur = (uint16_t)v;
    }
  }
  saveSchedConfig();
  handleSchedGet();
}

static void handleReboot() {
  server.send(200, "application/json", "{\"ok\":true}");
  rebootAtMs = millis() + 600; // let the response reach the browser first
}

static void handleWifiSave() {
  String ssid = server.arg("ssid");
  String pass = server.arg("pass");
  if (ssid.length() == 0) {
    server.send(400, "application/json", "{\"error\":\"ssid required\"}");
    return;
  }
  saveWifiConfig(ssid, pass);
  server.send(200, "application/json", "{\"ok\":true}");
  WiFi.disconnect();
  connectSta();
}

static void handleWifiForget() {
  clearWifiConfig();
  WiFi.disconnect();
  server.send(200, "application/json", "{\"ok\":true}");
}

static String relayCfgJson() {
  String json = "{\"activelow1\":";
  json += relay1ActiveLow ? "true" : "false";
  json += ",\"activelow2\":";
  json += relay2ActiveLow ? "true" : "false";
  json += "}";
  return json;
}

static void handleLedGet() {
  String json = "{\"blink\":";
  json += ledBlink ? "true" : "false";
  json += ",\"bri\":" + String(ledBrightness) + "}";
  server.send(200, "application/json", json);
}

static void handleLedSet() {
  if (server.hasArg("blink")) ledBlink = (server.arg("blink") == "1");
  if (server.hasArg("bri")) {
    long v = server.arg("bri").toInt();
    if (v >= 5 && v <= 100) ledBrightness = (uint8_t)v;
  }
  saveLedConfig();
  if (ledLit) ledSet(true); // apply new brightness immediately
  handleLedGet();
}

static void handleRelayCfgGet() {
  server.send(200, "application/json", relayCfgJson());
}

static void handleRelayCfgSet() {
  if (server.hasArg("activelow1")) relay1ActiveLow = (server.arg("activelow1") == "1");
  if (server.hasArg("activelow2")) relay2ActiveLow = (server.arg("activelow2") == "1");
  saveRelayConfig();
  // Re-drive the pins so the logical ON/OFF state is kept with the new polarity.
  setRelay1(relay1On);
  setRelay2(relay2On);
  server.send(200, "application/json", relayCfgJson());
}

static void handleTgGet() {
  xSemaphoreTake(tgMutex, portMAX_DELAY);
  String json = "{\"token\":\"" + jsonEscape(tgToken) + "\",\"chat\":\"" + jsonEscape(tgChat) + "\"";
  json += ",\"enabled\":";
  json += tgEnabled ? "true" : "false";
  json += "}";
  xSemaphoreGive(tgMutex);
  server.send(200, "application/json", json);
}

static bool validButtonAction(const String &a) {
  return a == "t15" || a == "t30" || a == "t60" || a == "on" || a == "off" || a == "none";
}

static void handleBtnCfgGet() {
  String json = "{\"aoff\":\"" + btnActionOff + "\",\"aon\":\"" + btnActionOn + "\"}";
  server.send(200, "application/json", json);
}

static void handleBtnCfgSet() {
  String aoff = server.arg("aoff");
  String aon = server.arg("aon");
  if (validButtonAction(aoff)) btnActionOff = aoff;
  if (validButtonAction(aon)) btnActionOn = aon;
  saveButtonConfig();
  handleBtnCfgGet();
}

static void handleTgLastChat() {
  xSemaphoreTake(tgMutex, portMAX_DELAY);
  String json = "{\"chat\":\"" + jsonEscape(tgLastChatId) + "\"}";
  xSemaphoreGive(tgMutex);
  server.send(200, "application/json", json);
}

static void handleTgSet() {
  String token = server.arg("token");
  String chat = server.arg("chat");
  token.trim();
  chat.trim();
  bool enabled = (server.arg("enabled") != "0");
  saveTelegramConfig(token, chat, enabled);
  server.send(200, "application/json", "{\"ok\":true}");
}

static void handleWifiScan() {
  int n = WiFi.scanComplete();
  if (n == WIFI_SCAN_FAILED) { // no scan yet -> start an async one
    WiFi.scanNetworks(true);
    server.send(200, "application/json", "{\"pending\":true}");
    return;
  }
  if (n == WIFI_SCAN_RUNNING) {
    server.send(200, "application/json", "{\"pending\":true}");
    return;
  }
  String json = "{\"pending\":false,\"networks\":[";
  for (int i = 0; i < n; i++) {
    if (i) json += ",";
    json += "{\"ssid\":\"" + jsonEscape(WiFi.SSID(i)) + "\"";
    json += ",\"rssi\":" + String(WiFi.RSSI(i));
    json += ",\"open\":";
    json += (WiFi.encryptionType(i) == WIFI_AUTH_OPEN) ? "true" : "false";
    json += "}";
  }
  json += "]}";
  WiFi.scanDelete(); // free results; next /wifi/scan starts a fresh scan
  server.send(200, "application/json", json);
}

// ---------- OTA firmware update ----------
static void handleFwInfo() {
  String json = "{\"version\":\"" + String(FW_VERSION) + "\"";
  json += ",\"free\":" + String(ESP.getFreeHeap());
  json += "}";
  server.send(200, "application/json", json);
}

static void handleOtaDone() {
  bool ok = !Update.hasError();
  String json = ok ? "{\"ok\":true}"
                   : "{\"ok\":false,\"error\":\"" + String(Update.errorString()) + "\"}";
  server.send(200, "application/json", json);
  if (ok) rebootAtMs = millis() + 800; // let the response reach the browser first
}

static void handleOtaUpload() {
  HTTPUpload &up = server.upload();
  if (up.status == UPLOAD_FILE_START) {
    Serial.printf("OTA start: %s\n", up.filename.c_str());
    Update.begin(UPDATE_SIZE_UNKNOWN);
  } else if (up.status == UPLOAD_FILE_WRITE) {
    if (!Update.hasError()) Update.write(up.buf, up.currentSize);
  } else if (up.status == UPLOAD_FILE_END) {
    if (Update.end(true)) {
      Serial.printf("OTA done: %u bytes\n", up.totalSize);
    } else {
      Serial.printf("OTA failed: %s\n", Update.errorString());
    }
  } else if (up.status == UPLOAD_FILE_ABORTED) {
    Update.abort();
    Serial.println("OTA aborted");
  }
}

// Captive portal: redirect every unknown URL to our page so the phone's
// "sign in to network" popup opens the relay controls automatically.
static void handleNotFound() {
  server.sendHeader("Location", "http://" + WiFi.softAPIP().toString() + "/", true);
  server.send(302, "text/plain", "");
}

void setup() {
  Serial.begin(115200);

  loadRelayConfig(); // polarity must be known before driving the pins
  loadButtonConfig();
  loadSchedConfig();
  loadLedConfig();
  pinMode(RELAY1_PIN, OUTPUT);
  pinMode(RELAY2_PIN, OUTPUT);
  ledcAttach(LED_PIN, 5000, 8); // PWM for brightness control
  ledSet(false);
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  setRelay1(false); // start with everything off
  setRelay2(false);

  usageLoad();            // recover any cycle left open by a power cut
  resumeTimerAfterBoot(); // continue a timer that was running before reboot

  tgMutex = xSemaphoreCreateMutex();
  loadTelegramConfig();
  loadSysConfig();
  applyTimezone();
  loadWifiConfig();

  // AP + STA: the access point is always available, and if home WiFi
  // credentials are stored the device also joins that network.
  WiFi.setHostname(hostname.c_str()); // DHCP name (must precede WiFi.mode)
  WiFi.mode(WIFI_AP_STA);
  WiFi.softAP(AP_SSID, AP_PASSWORD);
  IPAddress ip = WiFi.softAPIP();
  connectSta();

  dnsServer.start(DNS_PORT, "*", ip);  // captive portal DNS (AP side)
  MDNS.begin(hostname.c_str());        // http://<hostname>.local (home network side)
  MDNS.addService("http", "tcp", 80);

  configTime(0, 0, "pool.ntp.org", "time.google.com"); // clock for TLS cert checks
  applyTimezone(); // configTime() resets TZ to UTC, so apply ours after it

  // Network OTA for PlatformIO (upload_protocol = espota,
  // upload_port = <hostname>.local) — works over home WiFi or the AP.
  ArduinoOTA.setHostname(hostname.c_str());
  ArduinoOTA.begin();

  // Telegram polling runs in its own task so HTTPS round-trips
  // never block the web server / captive portal in loop().
  xTaskCreate(telegramTask, "telegram", 8192, nullptr, 1, nullptr);

  server.on("/", handleRoot);
  server.on("/settings", handleSettings);
  server.on("/state", handleState);
  server.on("/set", handleSet);
  server.on("/wifi/status", handleWifiStatus);
  server.on("/wifi/save", handleWifiSave);
  server.on("/wifi/forget", handleWifiForget);
  server.on("/wifi/scan", handleWifiScan);
  server.on("/relaycfg/get", handleRelayCfgGet);
  server.on("/relaycfg/set", handleRelayCfgSet);
  server.on("/timer/set", handleTimerSet);
  server.on("/timer/cancel", handleTimerCancel);
  server.on("/tg/get", handleTgGet);
  server.on("/tg/set", handleTgSet);
  server.on("/tg/lastchat", handleTgLastChat);
  server.on("/btncfg/get", handleBtnCfgGet);
  server.on("/btncfg/set", handleBtnCfgSet);
  server.on("/led/get", handleLedGet);
  server.on("/led/set", handleLedSet);
  server.on("/usage/get", handleUsageGet);
  server.on("/fw/info", handleFwInfo);
  server.on("/ota", HTTP_POST, handleOtaDone, handleOtaUpload);
  server.on("/sys/get", handleSysGet);
  server.on("/sys/set", handleSysSet);
  server.on("/sched/get", handleSchedGet);
  server.on("/sched/set", handleSchedSet);
  server.on("/reboot", HTTP_POST, handleReboot);
  server.onNotFound(handleNotFound);
  server.begin();

  Serial.println();
  Serial.print("AP started: ");
  Serial.println(AP_SSID);
  Serial.print("Open http://");
  Serial.println(ip);
  if (staSsid.length()) {
    Serial.print("Connecting to saved WiFi: ");
    Serial.println(staSsid);
  }
}

void loop() {
  ArduinoOTA.handle();
  if (rebootAtMs && (long)(millis() - rebootAtMs) > 0) ESP.restart();

  handleButton();
  ledTick();

  static unsigned long lastSecondly = 0;
  if (millis() - lastSecondly >= 1000) {
    lastSecondly = millis();

    // Optional AP shutdown while home WiFi is up. Grace periods avoid
    // cutting off someone configuring via the AP (60s) and AP/STA flapping
    // during brief WiFi drops (10s).
    static uint16_t staUpSecs = 0, staDownSecs = 0;
    if (WiFi.status() == WL_CONNECTED) {
      if (staUpSecs < 60000) staUpSecs++;
      staDownSecs = 0;
    } else {
      staUpSecs = 0;
      if (staDownSecs < 60000) staDownSecs++;
    }
    if (apOffWhenSta) {
      if (apActive && staUpSecs >= 60) setApActive(false);
      if (!apActive && staDownSecs >= 10) setApActive(true);
    } else if (!apActive) {
      setApActive(true); // option was just turned off
    }

    usageTick(); // record heating cycles on the device itself

    // Daily schedules: fire each enabled slot once when local time hits it.
    time_t nowT = time(nullptr);
    if (nowT > 1600000000) { // NTP has synced
      struct tm lt;
      localtime_r(&nowT, &lt);
      for (uint8_t i = 0; i < SCHED_SLOTS; i++) {
        if (sched[i].en && lt.tm_hour == sched[i].hh && lt.tm_min == sched[i].mm &&
            schedLastFired[i] != lt.tm_yday) {
          schedLastFired[i] = lt.tm_yday;
          startTimer(sched[i].dur);
          Serial.printf("Schedule %u fired: ON for %u min\n", i + 1, sched[i].dur);
        }
      }
    }

    // Safety cap: no relay stays on longer than maxOnMinutes.
    if (maxOnMinutes > 0) {
      unsigned long capMs = (unsigned long)maxOnMinutes * 60000UL;
      if (relay1On && millis() - relay1OnSinceMs > capMs) {
        setRelay1(false);
        Serial.println("Max-on cap reached: relay 1 off");
      }
      if (relay2On && millis() - relay2OnSinceMs > capMs) {
        setRelay2(false);
        Serial.println("Max-on cap reached: relay 2 off");
      }
      if (timerActive && !relay1On && !relay2On) stopTimer(false);
    }
  }

  if (timerActive) {
    if ((long)(timerEndMs - millis()) <= 0) {
      stopTimer(true); // time's up -> both relays off
    } else if (millis() - lastTimerSave >= 60000UL) {
      persistTimer(timerRemainingSecs()); // keep NVS fresh for reboot resume
      lastTimerSave = millis();
    }
  }

  static wl_status_t lastStatus = WL_IDLE_STATUS;
  wl_status_t st = WiFi.status();
  if (st != lastStatus) {
    lastStatus = st;
    if (st == WL_CONNECTED) {
      Serial.print("WiFi connected, IP: ");
      Serial.println(WiFi.localIP());
    }
  }
  dnsServer.processNextRequest();
  server.handleClient();
}
