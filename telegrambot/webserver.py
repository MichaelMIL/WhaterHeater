#!/usr/bin/env python3
"""Local web panel for the Water Heater bot.

Started by bot.py in a background thread. Mirrors the Telegram bot one to
one on three pages: "/" has the same buttons as the /menu panel (Heater ON
30m / OFF / 15m / 60m / custom timer) and live status, "/history" has the
history + graph, "/settings" the device info and schedule editor from the
/settings wizard. Night mode follows the OS, with a manual toggle. Every action goes through the bot's own helpers, so
the rate limit, event log and cycle log are shared with Telegram.

Only the Python standard library is used (http.server), so there are no
extra dependencies. Bind address / port come from WEB_HOST / WEB_PORT.

JSON API (used by the page, usable from scripts too):

  GET  /api/state      -> status + raw relay state
  POST /api/action     {"action": "allon"|"alloff"|"t15"|"t60"|"timer", "mins": N}
  GET  /api/history    -> history text + last cycles
  GET  /api/usage?days=N -> per-day + per-hour totals for the charts (1-90)
  GET  /graph.png      -> the same PNG /graph sends in Telegram
  GET  /api/settings   -> device info (tz, time, max on) + schedule slots
  POST /api/schedule   {"slot": 0-4, "en": bool, "hh": H, "mm": M, "dur": MIN}
"""

import csv
import json
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

# Filled in by start(): the running bot module (passed in rather than
# imported, since bot.py runs as __main__ and importing it again would
# execute the whole script a second time).
bot = None

# ---------- pages ----------
# Three pages sharing one stylesheet + script: "/" (control), "/history"
# (history + graph) and "/settings" (device info + schedules). Night mode
# follows the OS by default; the moon button overrides it (saved in
# localStorage).

STYLE = r"""
  :root {
    --bg:#f1f5f9; --card:#ffffff; --ink:#0f172a; --muted:#64748b; --line:#e2e8f0;
    --btn:#e2e8f0; --btn-ink:#0f172a; --btn-hover:#cbd5e1;
    --accent:#2563eb; --bar:#2563eb; --hot:#ea580c; --hot2:#f97316; --danger:#dc2626; --ok:#16a34a;
    --nav:#ffffff; --shadow:0 1px 2px rgba(15,23,42,.06), 0 6px 20px rgba(15,23,42,.06);
  }
  html[data-theme="dark"] {
    --bg:#0b1220; --card:#131c2e; --ink:#e5edf7; --muted:#8b9bb4; --line:#223047;
    --btn:#1f2b40; --btn-ink:#e5edf7; --btn-hover:#2a3851;
    --accent:#60a5fa; --bar:#3b82f6; --hot:#f97316; --hot2:#fb923c; --danger:#ef4444; --ok:#22c55e;
    --nav:#0f172a; --shadow:0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.35);
  }
  * { box-sizing:border-box; }
  html { color-scheme: light dark; }
  body { margin:0; background:var(--bg); color:var(--ink); -webkit-font-smoothing:antialiased;
         font:15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif; }
  a { color:inherit; text-decoration:none; }

  nav { position:sticky; top:0; z-index:5; background:var(--nav); border-bottom:1px solid var(--line); }
  nav .in { max-width:680px; margin:0 auto; padding:0 12px; display:flex; align-items:center; gap:4px; height:56px; }
  nav .brand { font-weight:700; font-size:17px; margin-right:auto; display:flex; align-items:center; gap:8px; }
  nav .tab { padding:8px 12px; border-radius:10px; color:var(--muted); font-weight:600; font-size:14px; }
  nav .tab.active { background:var(--btn); color:var(--ink); }
  nav .tab:hover { color:var(--ink); }
  #theme { background:none; border:0; font-size:18px; cursor:pointer; padding:6px 8px; border-radius:10px; min-width:0; flex:none; color:var(--ink); }
  #theme:hover { background:var(--btn); }

  main { max-width:680px; margin:0 auto; padding:20px 16px 40px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:16px; padding:20px; margin-bottom:16px; box-shadow:var(--shadow); }
  h2 { font-size:13px; color:var(--muted); margin:0 0 14px; text-transform:uppercase; letter-spacing:.06em; font-weight:700; }
  .muted { color:var(--muted); }

  /* status hero */
  .hero { display:flex; align-items:center; gap:16px; }
  .dot { width:56px; height:56px; border-radius:50%; background:var(--btn); display:grid; place-items:center; font-size:26px; flex:none; transition:box-shadow .4s; }
  .hero.on .dot { background:linear-gradient(135deg,var(--hot),var(--hot2)); box-shadow:0 0 0 6px rgba(249,115,22,.18), 0 0 28px rgba(249,115,22,.45); }
  .hero .big { font-size:22px; font-weight:700; line-height:1.2; }
  .hero .sub { color:var(--muted); margin-top:4px; }
  .stats { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-top:18px; }
  .stat { background:var(--bg); border-radius:12px; padding:12px 14px; }
  .stat .l { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.05em; }
  .stat .v { font-size:18px; font-weight:700; margin-top:2px; font-variant-numeric:tabular-nums; }

  .grid { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-top:16px; }
  button { border:0; border-radius:12px; padding:14px 16px; font-size:15px; font-weight:600; cursor:pointer;
           background:var(--btn); color:var(--btn-ink); transition:transform .06s, background .15s, filter .15s; }
  button:hover { background:var(--btn-hover); }
  button:active { transform:scale(.98); }
  button:disabled { opacity:.5; cursor:default; transform:none; }
  button.hot { background:linear-gradient(135deg,var(--hot),var(--hot2)); color:#fff; }
  button.hot:hover { filter:brightness(1.08); }
  button.danger { background:var(--danger); color:#fff; }
  button.danger:hover { filter:brightness(1.1); }
  button.sm { padding:8px 14px; font-size:14px; border-radius:10px; }
  .custom { display:flex; gap:10px; margin-top:10px; }
  .custom input { flex:1; min-width:0; font-size:15px; padding:12px 14px; border:1px solid var(--line); border-radius:12px; background:var(--bg); color:var(--ink); }
  .custom input:focus, select:focus { outline:2px solid var(--accent); outline-offset:1px; }
  .warn { background:#fef3c7; color:#92400e; padding:10px 14px; border-radius:10px; margin-top:12px; font-size:14px; }
  html[data-theme="dark"] .warn { background:#3b2a08; color:#fcd34d; }

  /* history */
  .cycles { list-style:none; margin:0; padding:0; }
  .cycles li { display:flex; align-items:center; gap:12px; padding:11px 0; border-top:1px solid var(--line); }
  .cycles li:first-child { border-top:0; padding-top:0; }
  .cycles .d { color:var(--muted); font-size:13px; width:52px; font-variant-numeric:tabular-nums; }
  .cycles .t { font-weight:600; width:58px; font-variant-numeric:tabular-nums; }
  .cycles .bar { flex:1; height:8px; background:var(--btn); border-radius:4px; overflow:hidden; }
  .cycles .bar i { display:block; height:100%; background:linear-gradient(90deg,var(--hot),var(--hot2)); border-radius:4px; }
  .cycles .len { width:68px; text-align:right; font-variant-numeric:tabular-nums; }
  .cycles li.live .t::after { content:"●"; color:var(--hot2); margin-left:6px; font-size:10px; vertical-align:middle; }
  /* charts */
  .range { display:flex; gap:6px; margin:-4px 0 14px; }
  .range button { padding:6px 12px; font-size:13px; border-radius:999px; background:transparent; color:var(--muted); border:1px solid var(--line); }
  .range button.active { background:var(--btn); color:var(--ink); border-color:transparent; }
  .chart-title { font-weight:600; margin:0 0 2px; }
  .chart-sub { color:var(--muted); font-size:13px; margin:0 0 8px; }
  .chart { position:relative; }
  .chart svg { width:100%; height:auto; display:block; overflow:visible; }
  .chart text { font-family:inherit; fill:var(--muted); font-size:11px; }
  .chart text.val { fill:var(--ink); font-weight:600; }
  .chart .grid { stroke:var(--line); stroke-width:1; }
  .chart .bar { fill:var(--bar); transition:opacity .12s; }
  .chart .bar.dim { opacity:.35; }
  .chart .hit { fill:transparent; }
  .tip { position:absolute; pointer-events:none; background:var(--ink); color:var(--card); font-size:12px; line-height:1.35;
         padding:6px 9px; border-radius:8px; white-space:nowrap; opacity:0; transform:translate(-50%,-100%); transition:opacity .1s; box-shadow:var(--shadow); }
  .tip b { font-weight:700; }
  .tip.show { opacity:1; }
  .chart + .chart { margin-top:22px; }

  /* settings */
  .kv { display:grid; grid-template-columns:auto 1fr; gap:6px 16px; font-size:14px; }
  .kv dt { color:var(--muted); } .kv dd { margin:0; font-weight:600; }
  .slot { display:flex; align-items:center; gap:10px; padding:12px 0; border-top:1px solid var(--line); flex-wrap:wrap; }
  .slot:first-child { border-top:0; padding-top:0; }
  .slot .n { width:26px; height:26px; border-radius:50%; background:var(--btn); color:var(--muted); font-size:12px; font-weight:700; display:grid; place-items:center; flex:none; }
  .slot.on .n { background:var(--ok); color:#fff; }
  .slot select { font-size:15px; padding:8px 10px; border:1px solid var(--line); border-radius:10px; background:var(--bg); color:var(--ink); }
  .slot .act { margin-left:auto; display:flex; gap:6px; }
  .slot .state { font-size:13px; color:var(--muted); width:100%; padding-left:36px; margin-top:-4px; }

  .empty { color:var(--muted); text-align:center; padding:16px 0; }
  #toast { position:fixed; left:50%; bottom:24px; transform:translateX(-50%) translateY(10px); background:#0f172a; color:#fff;
           padding:11px 18px; border-radius:12px; opacity:0; transition:opacity .2s, transform .2s; pointer-events:none; max-width:90vw; box-shadow:var(--shadow); }
  html[data-theme="dark"] #toast { background:#e5edf7; color:#0f172a; }
  #toast.show { opacity:1; transform:translateX(-50%) translateY(0); }
  #toast.err { background:var(--danger); color:#fff; }
  @media (max-width:420px) { nav .tab { padding:8px 9px; font-size:13px; } .cycles .len { width:58px; } }
"""

SCRIPT = r"""
const $ = (s) => document.querySelector(s);
let toastTimer;
function toast(msg, err) {
  const t = $('#toast'); t.textContent = msg; t.className = 'show' + (err ? ' err' : '');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.className = '', 2500);
}
async function api(path, body) {
  const r = await fetch(path, body ? {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)} : {});
  const data = await r.json().catch(() => ({error: 'bad response'}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}
// theme
function setTheme(t) { document.documentElement.dataset.theme = t; localStorage.theme = t; $('#theme').textContent = t === 'dark' ? '☀️' : '🌙'; }
$('#theme').onclick = () => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
$('#theme').textContent = document.documentElement.dataset.theme === 'dark' ? '☀️' : '🌙';

// ----- control page -----
function renderState(s) {
  $('#hero').className = 'hero' + (s.heating ? ' on' : '');
  $('#big').textContent = s.heating ? 'Heating' : 'Off';
  $('#sub').textContent = s.heating ? (s.on_for ? 'on for ' + s.on_for : 'heater is on') : 'heater is idle';
  $('#timer').textContent = s.timer > 0 ? Math.floor(s.timer / 60) + 'm ' + String(s.timer % 60).padStart(2, '0') + 's' : 'off';
  $('#today').textContent = s.today;
  $('#offline').hidden = true;
}
async function loadState() {
  try { renderState(await api('/api/state')); }
  catch (e) { $('#offline').textContent = '⚠️ ' + e.message; $('#offline').hidden = false; }
}
async function doAction(action) {
  const body = {action};
  if (action === 'timer') {
    body.mins = parseInt($('#mins').value, 10);
    if (!(body.mins >= 1 && body.mins <= 720)) return toast('Minutes must be 1–720', true);
  }
  const btns = document.querySelectorAll('button[data-action]');
  btns.forEach(b => b.disabled = true);
  try { const d = await api('/api/action', body); toast(d.feedback, d.rate_limited); renderState(d.state); }
  catch (e) { toast(e.message, true); }
  finally { btns.forEach(b => b.disabled = false); }
}
if ($('#hero')) {
  document.querySelectorAll('button[data-action]').forEach(b => b.onclick = () => doAction(b.dataset.action));
  $('#mins').addEventListener('keydown', e => { if (e.key === 'Enter') doAction('timer'); });
  loadState(); setInterval(loadState, 5000);
}

// ----- history page -----
function renderHistory(h) {
  $('#today').textContent = h.today;
  $('#count').textContent = h.today_cycles;
  const warn = $('#offline'); warn.hidden = !h.note; warn.textContent = h.note || '';
  const ul = $('#cycles'); ul.innerHTML = '';
  const max = Math.max(1, ...h.cycles.map(c => c.seconds));
  if (!h.cycles.length) { ul.innerHTML = '<li class="empty">No heating cycles logged yet.</li>'; return; }
  for (const c of [...h.cycles].reverse()) {
    const li = document.createElement('li'); if (c.live) li.className = 'live';
    li.innerHTML = `<span class="d">${c.date}</span><span class="t">${c.time}</span>` +
                   `<span class="bar"><i style="width:${Math.round(100 * c.seconds / max)}%"></i></span>` +
                   `<span class="len">${c.duration}</span>`;
    ul.appendChild(li);
  }
}
// Bar chart as inline SVG: hairline grid, <=24px bars with a 4px rounded cap,
// per-slot hover tooltip, labels only on the biggest and the last bar.
function fmtH(sec) { if (sec <= 0) return '0'; const h = sec / 3600; return h >= 1 ? (Math.round(h * 10) / 10) + 'h' : Math.round(sec / 60) + 'm'; }
function fmtDur(sec) { const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60); return h ? `${h}h ${String(m).padStart(2,'0')}m` : `${m}m`; }
function niceStep(maxH) { const raw = maxH / 3, p = Math.pow(10, Math.floor(Math.log10(raw || 1))); for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= raw) return m * p; return p * 10; }
function barChart(el, items, opts) {
  const W = 600, H = 190, padL = 30, padR = 6, padT = 18, padB = 24;
  const innerW = W - padL - padR, innerH = H - padT - padB;
  const maxSec = Math.max(...items.map(i => i.value), 60);
  const step = niceStep(maxSec / 3600), top = Math.max(step, Math.ceil(maxSec / 3600 / step) * step);
  const slot = innerW / items.length, bw = Math.min(24, slot * 0.68);
  const y = sec => padT + innerH - (sec / 3600 / top) * innerH;
  let g = '';
  for (let v = 0; v <= top + 1e-9; v += step) {
    const yy = y(v * 3600);
    g += `<line class="grid" x1="${padL}" x2="${W - padR}" y1="${yy}" y2="${yy}"/>` +
         `<text x="${padL - 6}" y="${yy + 3.5}" text-anchor="end">${fmtH(v * 3600).replace(/^0$/, '0')}</text>`;
  }
  const maxIdx = items.reduce((b, it, i) => it.value > items[b].value ? i : b, 0);
  const labelIdx = new Set(items[maxIdx].value > 0 ? [maxIdx] : []);
  if (opts.labelLast && items[items.length - 1].value > 0) labelIdx.add(items.length - 1);
  let bars = '', hits = '', xs = '';
  items.forEach((it, i) => {
    const x = padL + slot * i + (slot - bw) / 2, yTop = y(it.value), h = padT + innerH - yTop, r = Math.min(4, h);
    const d = h > 0 ? `M${x},${padT + innerH} V${yTop + r} Q${x},${yTop} ${x + r},${yTop} H${x + bw - r} Q${x + bw},${yTop} ${x + bw},${yTop + r} V${padT + innerH} Z` : '';
    if (d) bars += `<path class="bar" data-i="${i}" d="${d}"/>`;
    if (labelIdx.has(i)) bars += `<text class="val" x="${x + bw / 2}" y="${yTop - 5}" text-anchor="middle">${fmtDur(it.value)}</text>`;
    hits += `<rect class="hit" data-i="${i}" x="${padL + slot * i}" y="${padT}" width="${slot}" height="${innerH}"/>`;
    if (i % opts.every === 0 || i === items.length - 1 && opts.every === 1)
      xs += `<text x="${padL + slot * i + slot / 2}" y="${H - 7}" text-anchor="middle">${it.label}</text>`;
  });
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${opts.aria}">${g}${bars}${xs}${hits}</svg><div class="tip"></div>`;
  const tip = el.querySelector('.tip'), allBars = el.querySelectorAll('.bar');
  el.querySelectorAll('.hit').forEach(hit => {
    hit.onmouseenter = () => {
      const i = +hit.dataset.i, it = items[i];
      tip.innerHTML = `<b>${it.tip}</b><br>${it.value ? fmtDur(it.value) : 'no heating'}`;
      const box = el.getBoundingClientRect(), hb = hit.getBoundingClientRect();
      tip.style.left = (hb.left - box.left + hb.width / 2) + 'px';
      tip.style.top = (y(it.value) / H * box.height - 8) + 'px';
      tip.classList.add('show');
      allBars.forEach(b => b.classList.toggle('dim', +b.dataset.i !== i));
    };
    hit.onmouseleave = () => { tip.classList.remove('show'); allBars.forEach(b => b.classList.remove('dim')); };
  });
}
let usageDays = +(localStorage.usageDays || 14);
async function loadUsage() {
  let u; try { u = await api('/api/usage?days=' + usageDays); } catch (e) { return toast(e.message, true); }
  $('#u-total').textContent = fmtDur(u.total);
  $('#u-avg').textContent = fmtDur(Math.round(u.total / u.days.length));
  $('#u-peak').textContent = u.peak_hour === null ? '–' : `${String(u.peak_hour).padStart(2,'0')}:00`;
  $('#days-sub').textContent = `Last ${u.days.length} days · today ${fmtDur(u.days[u.days.length - 1].seconds)}`;
  $('#hours-sub').textContent = `When the heater runs, summed over the last ${u.days.length} days`;
  const DOW = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  barChart($('#chart-days'), u.days.map(d => ({value: d.seconds, label: d.label, tip: `${DOW[d.dow]} ${d.label}`})),
           {every: u.days.length > 14 ? 3 : 1, labelLast: true, aria: 'heating time per day'});
  barChart($('#chart-hours'), u.hours.map((s, h) => ({value: s, label: String(h).padStart(2,'0'), tip: `${String(h).padStart(2,'0')}:00–${String((h + 1) % 24).padStart(2,'0')}:00`})),
           {every: 3, labelLast: false, aria: 'heating time by hour of day'});
}
async function loadHistory() {
  try { renderHistory(await api('/api/history')); } catch (e) { toast(e.message, true); }
  loadUsage();
}
if ($('#cycles')) {
  document.querySelectorAll('#range button').forEach(b => {
    b.classList.toggle('active', +b.dataset.days === usageDays);
    b.onclick = () => { usageDays = +b.dataset.days; localStorage.usageDays = usageDays;
      document.querySelectorAll('#range button').forEach(x => x.classList.toggle('active', x === b)); loadUsage(); };
  });
  loadHistory(); setInterval(loadHistory, 60000);
}

// ----- settings page -----
const HOURS = [...Array(24).keys()], MINS = [0, 15, 30, 45], DURS = [15, 30, 45, 60, 90, 120];
function sel(opts, fmt, value) {
  const s = document.createElement('select');
  for (const o of opts) { const op = document.createElement('option'); op.value = o; op.textContent = fmt(o); s.appendChild(op); }
  if (!opts.includes(value)) { const op = document.createElement('option'); op.value = value; op.textContent = fmt(value); s.appendChild(op); }
  s.value = value; return s;
}
function renderSettings(d) {
  const kv = $('#sysinfo'); kv.innerHTML = '';
  for (const [k, v] of d.info) { kv.insertAdjacentHTML('beforeend', `<dt>${k}</dt><dd>${v}</dd>`); }
  const box = $('#slots'); box.innerHTML = '';
  d.slots.forEach((slot, i) => {
    const row = document.createElement('div'); row.className = 'slot' + (slot.en ? ' on' : '');
    const n = document.createElement('span'); n.className = 'n'; n.textContent = i + 1;
    const hh = sel(HOURS, h => String(h).padStart(2, '0'), slot.hh);
    const mm = sel(MINS, m => ':' + String(m).padStart(2, '0'), slot.mm);
    const dur = sel(DURS, x => x + ' min', slot.dur);
    const act = document.createElement('span'); act.className = 'act';
    const save = document.createElement('button'); save.className = 'sm hot'; save.textContent = slot.en ? 'Save' : 'Enable';
    save.onclick = () => saveSlot(i, true, +hh.value, +mm.value, +dur.value);
    act.appendChild(save);
    if (slot.en) {
      const del = document.createElement('button'); del.className = 'sm'; del.textContent = '🗑'; del.title = 'Disable';
      del.onclick = () => saveSlot(i, false); act.appendChild(del);
    }
    row.append(n, hh, mm, dur, act);
    const st = document.createElement('span'); st.className = 'state';
    st.textContent = slot.en ? `Every day at ${String(slot.hh).padStart(2,'0')}:${String(slot.mm).padStart(2,'0')} for ${slot.dur} min` : 'Not set';
    row.appendChild(st);
    box.appendChild(row);
  });
}
async function loadSettings() { try { renderSettings(await api('/api/settings')); } catch (e) { toast(e.message, true); } }
async function saveSlot(slot, en, hh, mm, dur) {
  try { const d = await api('/api/schedule', {slot, en, hh, mm, dur}); toast(d.feedback); renderSettings(d); }
  catch (e) { toast(e.message, true); }
}
if ($('#slots')) loadSettings();
"""

BODIES = {
    "/": """
  <div class="card">
    <div id="hero" class="hero">
      <div class="dot">🔥</div>
      <div><div id="big" class="big">Loading…</div><div id="sub" class="sub"></div></div>
    </div>
    <div class="stats">
      <div class="stat"><div class="l">Timer</div><div id="timer" class="v">–</div></div>
      <div class="stat"><div class="l">Heated today</div><div id="today" class="v">–</div></div>
    </div>
    <div id="offline" class="warn" hidden></div>
  </div>
  <div class="card">
    <h2>Control</h2>
    <div class="grid" style="margin-top:0">
      <button class="hot" data-action="allon">🔥 Heater ON · 30m</button>
      <button class="danger" data-action="alloff">Heater OFF</button>
      <button data-action="t15">Heat 15 min</button>
      <button data-action="t60">Heat 60 min</button>
    </div>
    <div class="custom">
      <input id="mins" type="number" inputmode="numeric" min="1" max="720" placeholder="Custom minutes (1–720)">
      <button data-action="timer">Heat</button>
    </div>
  </div>
""",
    "/history": """
  <div class="card">
    <h2>Today</h2>
    <div class="stats" style="margin-top:0">
      <div class="stat"><div class="l">Heated today</div><div id="today" class="v">–</div></div>
      <div class="stat"><div class="l">Cycles today</div><div id="count" class="v">–</div></div>
    </div>
    <div id="offline" class="warn" hidden></div>
  </div>
  <div class="card">
    <h2>Last cycles</h2>
    <ul id="cycles" class="cycles"></ul>
  </div>
  <div class="card">
    <h2>Usage</h2>
    <div class="range" id="range">
      <button data-days="7">7 days</button>
      <button data-days="14" class="active">14 days</button>
      <button data-days="30">30 days</button>
    </div>
    <div class="stats" style="margin:0 0 18px; grid-template-columns:1fr 1fr 1fr">
      <div class="stat"><div class="l">Total</div><div id="u-total" class="v">–</div></div>
      <div class="stat"><div class="l">Per day</div><div id="u-avg" class="v">–</div></div>
      <div class="stat"><div class="l">Busiest hour</div><div id="u-peak" class="v">–</div></div>
    </div>
    <p class="chart-title">Heating time per day</p>
    <p class="chart-sub" id="days-sub"></p>
    <div class="chart" id="chart-days"></div>
    <p class="chart-title">By hour of day</p>
    <p class="chart-sub" id="hours-sub"></p>
    <div class="chart" id="chart-hours"></div>
  </div>
""",
    "/settings": """
  <div class="card">
    <h2>Device</h2>
    <dl id="sysinfo" class="kv"></dl>
  </div>
  <div class="card">
    <h2>Daily schedules</h2>
    <div id="slots"></div>
  </div>
""",
}

TABS = [("/", "🔥 Control"), ("/history", "📊 History"), ("/settings", "⚙️ Settings")]

def render_page(path: str) -> str:
    tabs = "".join(f'<a class="tab{" active" if p == path else ""}" href="{p}">{label}</a>'
                   for p, label in TABS)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0f172a" media="(prefers-color-scheme: dark)">
<meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">
<title>Water Heater</title>
<script>document.documentElement.dataset.theme = localStorage.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');</script>
<style>{STYLE}</style>
</head>
<body>
<nav><div class="in"><a class="brand" href="/">🚿 Water Heater</a>{tabs}<button id="theme" title="Toggle night mode">🌙</button></div></nav>
<main>{BODIES[path]}</main>
<div id="toast"></div>
<script>{SCRIPT}</script>
</body>
</html>
"""

# ---------- API helpers ----------

def state_payload() -> dict:
    """Same facts as the Telegram panel text (status_text), as fields."""
    state = bot.get_state()
    heating = bool(state["relay1"] or state["relay2"])
    on_since = bot.MONITOR.on_since.get("heater") if heating else None
    return {
        "relay1": state["relay1"],
        "relay2": state["relay2"],
        "timer": state.get("timer", 0),
        "heating": heating,
        "on_for": bot.fmt_duration((bot.now() - on_since).total_seconds()) if on_since else None,
        "today": bot.fmt_duration(bot.daily_totals(1)[bot.now().date()]),
        "status_text": bot.status_text(state),
    }

def history_payload() -> dict:
    rows = []
    if bot.CYCLES_FILE.exists():
        with bot.CYCLES_FILE.open(encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r["circuit"] == "heater"]
    rows.sort(key=lambda r: datetime.fromisoformat(r["on"]))
    today = bot.now().date()
    today_rows = [r for r in rows if datetime.fromisoformat(r["off"]).date() == today]
    today_secs = sum(int(r["seconds"]) for r in today_rows)
    cycles = []
    for r in rows[-10:]:
        on = datetime.fromisoformat(r["on"])
        cycles.append({"on": r["on"], "off": r["off"], "seconds": int(r["seconds"]),
                       "date": on.strftime("%d/%m"), "time": on.strftime("%H:%M"),
                       "duration": bot.fmt_duration(int(r["seconds"])), "live": False})
    ongoing = bot.MONITOR.on_since.get("heater")
    if ongoing:
        secs = int((bot.now() - ongoing).total_seconds())
        today_secs += secs
        cycles.append({"on": ongoing.isoformat(timespec="seconds"), "off": None, "seconds": secs,
                       "date": ongoing.strftime("%d/%m"), "time": ongoing.strftime("%H:%M"),
                       "duration": bot.fmt_duration(secs), "live": True})
    return {
        "text": bot.history_text(),
        "today": bot.fmt_duration(today_secs),
        "today_cycles": len(today_rows) + (1 if ongoing else 0),
        "note": bot.stale_note(rows),
        "cycles": cycles,
    }

def graph_caption() -> dict:
    totals = bot.daily_totals(14)
    hourly = bot.hourly_totals(14)
    peak = hourly.index(max(hourly)) if any(hourly) else None
    caption = (f"Last 14 days: {bot.fmt_duration(sum(totals.values()))} total, "
               f"today {bot.fmt_duration(totals[bot.now().date()])}")
    if peak is not None:
        caption += f". Busiest hour: {peak:02d}:00–{(peak + 1) % 24:02d}:00"
    rows = []
    if bot.CYCLES_FILE.exists():
        with bot.CYCLES_FILE.open(encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r["circuit"] == "heater"]
    note = bot.stale_note(rows)
    if note:
        caption += "\n" + note
    return {"caption": caption}

def usage_payload(days: int) -> dict:
    """Daily + hour-of-day totals for the charts (same maths as /graph)."""
    days = max(1, min(days, 90))
    totals = bot.daily_totals(days)
    hourly = bot.hourly_totals(days)
    return {
        "days": [{"date": d.isoformat(), "label": d.strftime("%d/%m"),
                  "dow": (d.weekday() + 1) % 7, "seconds": int(secs)}
                 for d, secs in totals.items()],
        "hours": [int(s) for s in hourly],
        "total": int(sum(totals.values())),
        "peak_hour": hourly.index(max(hourly)) if any(hourly) else None,
    }

def settings_payload(feedback: str = "") -> dict:
    """Same device info the /settings wizard header shows, as key/value pairs."""
    info = []
    try:
        sys_info = bot.device_get("/sys/get")
        info.append(["Timezone", str(sys_info.get("tz", "?"))])
        if sys_info.get("time"):
            info.append(["Device time", str(sys_info["time"])])
        info.append(["Max continuous ON", f"{sys_info.get('maxon', '?')} min"])
    except bot.DeviceError:
        info.append(["Device", "unreachable"])
    info.append(["Device URL", bot.DEVICE_URL])
    return {"info": info, "slots": bot.get_schedules(), "feedback": feedback}

def do_action(body: dict, client: str) -> tuple:
    """Returns (http_status, payload). Mirrors apply_action()/handle_command()."""
    action = body.get("action", "")
    if action == "timer":
        try:
            mins = int(body.get("mins", 0))
        except (TypeError, ValueError):
            mins = 0
        if not 1 <= mins <= 720:
            return 400, {"error": "minutes must be 1-720"}
        if not bot.allow_change(True):
            feedback, limited = bot.RATE_MSG, True
        else:
            bot.set_timer(mins)
            feedback, limited = f"\U0001F525 Heating for {mins} min", False
    elif action in ("allon", "alloff", "t15", "t60"):
        feedback = bot.apply_action(action)
        limited = feedback == bot.RATE_MSG
    else:
        return 400, {"error": "unknown action"}
    bot.log_event("web_action", client=client, action=action,
                  mins=body.get("mins"), result=feedback)
    return 200, {"feedback": feedback, "rate_limited": limited, "state": state_payload()}

def do_schedule(body: dict, client: str) -> tuple:
    try:
        slot = int(body.get("slot"))
        en = bool(body.get("en"))
    except (TypeError, ValueError):
        return 400, {"error": "slot must be 0-4"}
    if not 0 <= slot <= 4:
        return 400, {"error": "slot must be 0-4"}
    if not en:
        bot.set_schedule(slot, False)
        feedback = f"Schedule {slot + 1} disabled"
    else:
        try:
            hh, mm, dur = int(body.get("hh")), int(body.get("mm")), int(body.get("dur"))
        except (TypeError, ValueError):
            return 400, {"error": "hh, mm and dur are required"}
        if not (0 <= hh <= 23 and 0 <= mm <= 59 and 1 <= dur <= 720):
            return 400, {"error": "hh 0-23, mm 0-59, dur 1-720"}
        bot.set_schedule(slot, True, hh, mm, dur)
        feedback = f"✅ Schedule {slot + 1}: every day at {hh:02d}:{mm:02d} for {dur} min"
    bot.log_event("web_schedule", client=client, slot=slot + 1, en=en,
                  hh=body.get("hh"), mm=body.get("mm"), dur=body.get("dur"))
    return 200, settings_payload(feedback)

# ---------- HTTP handler ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "WaterHeaterBot/1.0"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    def _guard(self, fn):
        """Run fn() -> (status, payload) and translate device failures."""
        try:
            status, payload = fn()
            self._json(status, payload)
        except bot.DeviceError as e:
            bot.log_event("device_error", context=f"web {self.path}", error=str(e))
            self._json(502, {"error": str(e)})
        except Exception as e:  # noqa: BLE001 - never kill the handler thread
            bot.log_event("web_error", path=self.path, error=f"{e.__class__.__name__}: {e}")
            print(f"web error on {self.path}: {e!r}", file=sys.stderr)
            self._json(500, {"error": f"{e.__class__.__name__}: {e}"})

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/index.html":
            path = "/"
        if path in BODIES:
            self._send(200, render_page(path).encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._guard(lambda: (200, state_payload()))
        elif path == "/api/history":
            self._guard(lambda: (200, history_payload()))
        elif path == "/api/graph_caption":
            self._guard(lambda: (200, graph_caption()))
        elif path == "/api/settings":
            self._guard(lambda: (200, settings_payload()))
        elif path == "/api/usage":
            q = parse_qs(urlsplit(self.path).query)
            try:
                days = int(q.get("days", ["14"])[0])
            except ValueError:
                days = 14
            self._guard(lambda: (200, usage_payload(days)))
        elif path == "/graph.png":
            try:
                png = bot.graph_png()
            except Exception as e:  # noqa: BLE001
                print(f"web graph error: {e!r}", file=sys.stderr)
                png = None
            if png is None:
                self._json(503, {"error": "graphs need matplotlib: python3 -m pip install matplotlib"})
            else:
                self._send(200, png, "image/png")
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        client = self.client_address[0]
        body = self._read_json()
        if path == "/api/action":
            self._guard(lambda: do_action(body, client))
        elif path == "/api/schedule":
            self._guard(lambda: do_schedule(body, client))
        else:
            self._json(404, {"error": "not found"})

# ---------- startup ----------

def start(bot_module, host: str, port: int) -> ThreadingHTTPServer:
    """Start the web panel in a daemon thread; returns the server."""
    global bot
    bot = bot_module
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True, name="web").start()
    return server
