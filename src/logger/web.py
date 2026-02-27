"""FastAPI web interface for race marking.

Provides a mobile-optimised single-page app at http://corvopi:3002 that lets
crew tap a button to start/end races. The app factory pattern (create_app)
keeps this testable without running a live server.

Security:
  Layer 1 (current) — Tailscale is the security boundary. All tailnet devices
    are trusted; no additional auth code.
  Layer 2 (TODO) — Optional WEB_PIN env var. If set, POST /login accepts the
    PIN, sets a signed session cookie (HMAC-SHA256(pin, WEB_SECRET_KEY) using
    stdlib hmac + hashlib only). GET / checks for cookie; redirect to /login
    if missing or invalid.
  Layer 3 (TODO) — Tailscale Whois API (GET http://100.100.100.100/v0/whois
    ?addr=<client_ip>) returns the caller's Tailscale identity for audit logs
    and per-device permissions — zero login UI, no extra dependencies.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from loguru import logger
from pydantic import BaseModel

if TYPE_CHECKING:
    from logger.audio import AudioConfig, AudioRecorder
    from logger.storage import Storage

# ---------------------------------------------------------------------------
# HTML — inline mobile-first single-page app
# ---------------------------------------------------------------------------

_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>J105 Logger</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,sans-serif;background:#0a1628;color:#e8eaf0;
padding:16px;max-width:480px;margin:0 auto}
h1{font-size:1.3rem;font-weight:700;color:#7eb8f7;margin-bottom:2px}
.sub{font-size:.9rem;color:#8892a4;margin-bottom:20px}
.card{background:#131f35;border-radius:12px;padding:16px;margin-bottom:16px}
.race-name{font-size:1rem;font-weight:600;color:#e8eaf0;margin-bottom:4px}
.race-meta{font-size:.8rem;color:#8892a4}
.status-dot{display:inline-block;width:10px;height:10px;border-radius:50%;
background:#22c55e;margin-right:6px;animation:pulse 1.4s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.label{font-size:.75rem;text-transform:uppercase;letter-spacing:.08em;color:#8892a4;margin-bottom:8px}
.duration{font-size:1.6rem;font-weight:700;color:#22c55e;font-variant-numeric:tabular-nums}
.btn{display:block;width:100%;padding:18px;border:none;border-radius:10px;font-size:1.1rem;font-weight:700;cursor:pointer;margin-bottom:10px;letter-spacing:.02em}
.btn-primary{background:#2563eb;color:#fff}
.btn-primary:active{background:#1d4ed8}
.btn-secondary{background:#1e3a5f;color:#7eb8f7;border:1px solid #2563eb}
.btn-secondary:active{background:#163252}
.btn-danger{background:#7f1d1d;color:#fca5a5;border:1px solid #dc2626}
.btn-practice{background:#1a3a2a;color:#4ade80;border:1px solid #16a34a}
.btn-practice:active{background:#14532d}
.btn-debrief{background:#2d1b4e;color:#c084fc;border:1px solid #7c3aed}
.btn-debrief:active{background:#1e1236}
.badge{font-size:.7rem;padding:1px 6px;border-radius:3px;margin-left:4px;vertical-align:middle}
.badge-race{background:#1e3a5f;color:#7eb8f7}
.badge-practice{background:#14532d;color:#4ade80}
.event-row{display:flex;gap:8px;margin-bottom:16px}
.event-input{flex:1;background:#0a1628;border:1px solid #2563eb;
border-radius:8px;padding:12px;color:#e8eaf0;font-size:1rem}
.btn-save{padding:12px 18px;border:none;border-radius:8px;background:#2563eb;
color:#fff;font-weight:700;cursor:pointer;font-size:1rem}
.race-list{margin-top:8px}
.race-item{padding:10px 0;border-bottom:1px solid #1e3a5f}
.race-item:last-child{border-bottom:none}
.race-item-name{font-weight:600;font-size:.9rem;margin-bottom:4px}
.race-item-time{font-size:.8rem;color:#8892a4}
.race-exports{margin-top:6px;display:flex;gap:8px}
.btn-export{padding:5px 12px;border:1px solid #2563eb;border-radius:6px;
background:#131f35;color:#7eb8f7;font-size:.8rem;cursor:pointer;text-decoration:none}
.btn-grafana{border-color:#b45309;color:#fbbf24}
.hidden{display:none}
.instruments-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px 12px;margin-top:8px}
.inst-item{display:flex;flex-direction:column}
.inst-label{font-size:.7rem;text-transform:uppercase;letter-spacing:.07em;color:#8892a4}
.inst-value{font-size:1.3rem;font-weight:700;color:#7eb8f7;font-variant-numeric:tabular-nums}
.inst-unit{font-size:.75rem;color:#8892a4;margin-left:2px}
.inst-time{font-size:1rem;font-weight:600;color:#e8eaf0;font-variant-numeric:tabular-nums;margin-bottom:8px}
</style>
</head>
<body>
<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:2px">
  <h1>J105 Logger</h1>
  <div style="display:flex;gap:6px;margin-top:2px">
    <a class="btn-export" href="/history">📋 History</a>
    <a class="btn-export btn-grafana" href="__GRAFANA_URL__/d/__GRAFANA_UID__/sailing-data" target="_blank">📊 Grafana</a>
  </div>
</div>
<div class="sub" id="header-sub">Loading…</div>

<div id="event-section" class="hidden">
  <div class="label">Event name</div>
  <div class="event-row">
    <input id="event-input" class="event-input" placeholder="e.g. Regatta" maxlength="40"/>
    <button class="btn-save" onclick="saveEvent()">Save</button>
  </div>
</div>

<div id="current-card" class="card hidden">
  <div class="label"><span class="status-dot"></span>Race in progress</div>
  <div class="race-name" id="cur-name">—</div>
  <div class="race-meta" id="cur-meta">—</div>
  <div class="label" style="margin-top:12px">Duration</div>
  <div class="duration" id="cur-duration">—</div>
</div>

<div id="debrief-card" class="card hidden">
  <div class="label">
    <span class="status-dot" style="background:#c084fc"></span>Debrief in progress
  </div>
  <div class="race-name" id="debrief-name">—</div>
  <div class="label" style="margin-top:12px">Duration</div>
  <div class="duration" id="debrief-duration" style="color:#c084fc">—</div>
  <button class="btn btn-danger" style="margin-top:12px" onclick="stopDebrief()">⏹ STOP DEBRIEF</button>
</div>

<div class="card" id="instruments-card">
  <div class="label">Instruments</div>
  <div class="inst-time" id="inst-time">--:--:-- UTC</div>
  <div class="instruments-grid">
    <div class="inst-item"><span class="inst-label">SOG</span>
      <span><span class="inst-value" id="iv-sog">—</span><span class="inst-unit">kts</span></span></div>
    <div class="inst-item"><span class="inst-label">COG</span>
      <span><span class="inst-value" id="iv-cog">—</span><span class="inst-unit">°</span></span></div>
    <div class="inst-item"><span class="inst-label">HDG</span>
      <span><span class="inst-value" id="iv-hdg">—</span><span class="inst-unit">°</span></span></div>
    <div class="inst-item"><span class="inst-label">BSP</span>
      <span><span class="inst-value" id="iv-bsp">—</span><span class="inst-unit">kts</span></span></div>
    <div class="inst-item"><span class="inst-label">AWS</span>
      <span><span class="inst-value" id="iv-aws">—</span><span class="inst-unit">kts</span></span></div>
    <div class="inst-item"><span class="inst-label">AWA</span>
      <span><span class="inst-value" id="iv-awa">—</span><span class="inst-unit">°</span></span></div>
    <div class="inst-item"><span class="inst-label">TWS</span>
      <span><span class="inst-value" id="iv-tws">—</span><span class="inst-unit">kts</span></span></div>
    <div class="inst-item"><span class="inst-label">TWA</span>
      <span><span class="inst-value" id="iv-twa">—</span><span class="inst-unit">°</span></span></div>
    <div class="inst-item"><span class="inst-label">TWD</span>
      <span><span class="inst-value" id="iv-twd">—</span><span class="inst-unit">°</span></span></div>
  </div>
</div>

<div id="controls">
  <button class="btn btn-primary"  id="btn-start-race"     onclick="startSession('race')">▶ START RACE 1</button>
  <button class="btn btn-practice" id="btn-start-practice" onclick="startSession('practice')">▶ START PRACTICE</button>
  <button class="btn btn-secondary hidden" id="btn-end" onclick="endRace()">■ END RACE</button>
</div>

<div class="card" id="history-card" style="display:none">
  <div class="label">Today's races</div>
  <div class="race-list" id="race-list"></div>
</div>

<script>
let state = null;
let tickInterval = null;
let curRaceStartMs = null;
let debriefStartMs = null;

async function loadState() {
  try {
    const r = await fetch('/api/state');
    state = await r.json();
    render(state);
  } catch(e) { console.error('state error', e); }
}

function fmt(s) {
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), ss = s%60;
  if(h) return `${h}:${String(m).padStart(2,'0')}:${String(ss).padStart(2,'0')}`;
  return `${m}:${String(ss).padStart(2,'0')}`;
}

function fmtTime(iso) {
  if(!iso) return '—';
  return new Date(iso).toISOString().substring(11,19) + ' UTC';
}

function render(s) {
  document.getElementById('header-sub').textContent =
    `${s.weekday} · ${s.event || '(no event)'}`;

  const evSec = document.getElementById('event-section');
  if(!s.event_is_default) {
    evSec.classList.remove('hidden');
    if(!document.getElementById('event-input').value && s.event) {
      document.getElementById('event-input').value = s.event;
    }
  } else {
    evSec.classList.add('hidden');
  }

  const cur = s.current_race;
  const curCard = document.getElementById('current-card');
  const btnEnd = document.getElementById('btn-end');
  const btnStartRace = document.getElementById('btn-start-race');
  const btnStartPractice = document.getElementById('btn-start-practice');

  if(cur) {
    curCard.classList.remove('hidden');
    btnEnd.classList.remove('hidden');
    btnStartRace.classList.add('hidden');
    btnStartPractice.classList.add('hidden');
    document.getElementById('cur-name').textContent = cur.name;
    document.getElementById('cur-meta').textContent =
      'Started ' + fmtTime(cur.start_utc);
    curRaceStartMs = new Date(cur.start_utc).getTime();
    btnEnd.textContent = '■ END ' + cur.name;
  } else {
    curCard.classList.add('hidden');
    btnEnd.classList.add('hidden');
    btnStartRace.classList.remove('hidden');
    btnStartPractice.classList.remove('hidden');
    curRaceStartMs = null;
    clearInterval(tickInterval);
  }

  const debriefCard = document.getElementById('debrief-card');
  if(s.current_debrief) {
    debriefCard.classList.remove('hidden');
    document.getElementById('debrief-name').textContent = s.current_debrief.race_name + ' — debrief';
    debriefStartMs = new Date(s.current_debrief.start_utc).getTime();
  } else {
    debriefCard.classList.add('hidden');
    debriefStartMs = null;
  }

  btnStartRace.textContent = `▶ START RACE ${s.next_race_num}`;

  const hist = document.getElementById('history-card');
  const list = document.getElementById('race-list');
  if(s.today_races && s.today_races.length) {
    hist.style.display = '';
    list.innerHTML = s.today_races.slice().reverse().map(r => {
      const start = fmtTime(r.start_utc);
      const end = r.end_utc ? fmtTime(r.end_utc) : 'in progress';
      const dur = (r.end_utc && r.duration_s != null)
        ? ` (${fmt(Math.round(r.duration_s))})` : '';
      const badge = r.session_type === 'practice'
        ? '<span class="badge badge-practice">PRACTICE</span>'
        : '<span class="badge badge-race">RACE</span>';
      const from = new Date(r.start_utc).getTime();
      const to   = r.end_utc ? new Date(r.end_utc).getTime() : 'now';
      const grafanaBtn = `<a class="btn-export btn-grafana" href="__GRAFANA_URL__/d/__GRAFANA_UID__/sailing-data?from=${from}&to=${to}&orgId=1" target="_blank">📊 ${r.end_utc ? 'Grafana' : 'Live'}</a>`;
      const debriefBtn = (r.end_utc && s.has_recorder && !s.current_debrief && !s.current_race)
        ? `<button class="btn-export btn-debrief" onclick="startDebrief(${r.id})">🎙 Debrief</button>`
        : '';
      const exports = r.end_utc
        ? `<div class="race-exports">
             <a class="btn-export" href="/api/races/${r.id}/export.csv">↓ CSV</a>
             <a class="btn-export" href="/api/races/${r.id}/export.gpx">↓ GPX</a>
             ${grafanaBtn}
             ${debriefBtn}
           </div>`
        : `<div class="race-exports">${grafanaBtn}</div>`;
      return `<div class="race-item">
        <div class="race-item-name">${r.name}${badge}</div>
        <div class="race-item-time">${start} → ${end}${dur}</div>
        ${exports}
      </div>`;
    }).join('');
  } else {
    hist.style.display = 'none';
  }
}

function tick() {
  const now = new Date();
  document.getElementById('inst-time').textContent =
    now.toISOString().substring(11,19) + ' UTC';
  if(curRaceStartMs) {
    const elapsed = Math.floor((Date.now() - curRaceStartMs) / 1000);
    document.getElementById('cur-duration').textContent = fmt(elapsed);
  }
  if(debriefStartMs) {
    const elapsed = Math.floor((Date.now() - debriefStartMs) / 1000);
    document.getElementById('debrief-duration').textContent = fmt(elapsed);
  }
}

async function loadInstruments() {
  try {
    const r = await fetch('/api/instruments');
    const d = await r.json();
    const set = (id, val, decimals=1) => {
      const el = document.getElementById(id);
      el.textContent = val != null ? Number(val).toFixed(decimals) : '—';
    };
    set('iv-sog', d.sog_kts, 1);
    set('iv-cog', d.cog_deg, 0);
    set('iv-hdg', d.heading_deg, 0);
    set('iv-bsp', d.bsp_kts, 1);
    set('iv-aws', d.aws_kts, 1);
    set('iv-awa', d.awa_deg, 0);
    set('iv-tws', d.tws_kts, 1);
    set('iv-twa', d.twa_deg, 0);
    set('iv-twd', d.twd_deg, 0);
  } catch(e) { console.error('instruments error', e); }
}

async function startSession(type) {
  await fetch(`/api/races/start?session_type=${type}`, {method:'POST'});
  await loadState();
  clearInterval(tickInterval);
  if(curRaceStartMs) tickInterval = setInterval(tick, 1000);
}

async function endRace() {
  if(!state || !state.current_race) return;
  await fetch(`/api/races/${state.current_race.id}/end`, {method:'POST'});
  await loadState();
}

async function startDebrief(raceId) {
  await fetch(`/api/races/${raceId}/debrief/start`, {method: 'POST'});
  await loadState();
}

async function stopDebrief() {
  await fetch('/api/debrief/stop', {method: 'POST'});
  await loadState();
}

async function saveEvent() {
  const name = document.getElementById('event-input').value.trim();
  if(!name) return;
  await fetch('/api/event', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({event_name: name})
  });
  await loadState();
}

loadState();
setInterval(loadState, 10000);
setInterval(tick, 1000);
loadInstruments();
setInterval(loadInstruments, 2000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# History page HTML
# ---------------------------------------------------------------------------

_HISTORY_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Session History — J105 Logger</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,sans-serif;background:#0a1628;color:#e8eaf0;
padding:16px;max-width:600px;margin:0 auto}
h1{font-size:1.3rem;font-weight:700;color:#7eb8f7}
.card{background:#131f35;border-radius:12px;padding:16px;margin-bottom:12px}
.label{font-size:.75rem;text-transform:uppercase;letter-spacing:.08em;color:#8892a4;margin-bottom:6px}
.btn{display:inline-block;padding:10px 18px;border:none;border-radius:8px;
font-size:.95rem;font-weight:700;cursor:pointer;letter-spacing:.02em}
.btn-secondary{background:#1e3a5f;color:#7eb8f7;border:1px solid #2563eb}
.btn-export{padding:5px 12px;border:1px solid #2563eb;border-radius:6px;
background:#131f35;color:#7eb8f7;font-size:.8rem;cursor:pointer;text-decoration:none;display:inline-block}
.btn-grafana{border-color:#b45309;color:#fbbf24}
.event-input{background:#0a1628;border:1px solid #2563eb;border-radius:8px;
padding:10px 12px;color:#e8eaf0;font-size:.95rem;width:100%}
.filter-btn{padding:7px 14px;border:1px solid #2563eb;border-radius:20px;
background:#0a1628;color:#7eb8f7;font-size:.8rem;cursor:pointer}
.filter-btn.active{background:#2563eb;color:#fff}
.badge{font-size:.7rem;padding:1px 6px;border-radius:3px;margin-left:4px;vertical-align:middle}
.badge-race{background:#1e3a5f;color:#7eb8f7}
.badge-practice{background:#14532d;color:#4ade80}
.badge-debrief{background:#2d1b4e;color:#c084fc}
.session-name{font-weight:600;font-size:.95rem;margin-bottom:3px}
.session-meta{font-size:.8rem;color:#8892a4}
.session-exports{margin-top:8px;display:flex;gap:6px;flex-wrap:wrap}
.empty{color:#8892a4;text-align:center;padding:24px 0}
.pager{display:flex;gap:8px;justify-content:center;align-items:center;margin-top:8px}
.pager-info{color:#8892a4;font-size:.85rem}
</style>
</head>
<body>
<div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">
  <a href="/" class="btn-export" style="padding:7px 14px">← Back</a>
  <h1>Session History</h1>
</div>

<div class="card">
  <input id="q" class="event-input" placeholder="Search by name or event…"
    oninput="scheduleLoad()" style="margin-bottom:10px"/>
  <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px">
    <button class="filter-btn active" onclick="setType(this,'')">All</button>
    <button class="filter-btn" onclick="setType(this,'race')">Race</button>
    <button class="filter-btn" onclick="setType(this,'practice')">Practice</button>
    <button class="filter-btn" onclick="setType(this,'debrief')">Debrief</button>
  </div>
  <div style="display:flex;gap:8px">
    <div style="flex:1">
      <div class="label">From</div>
      <input id="from-date" type="date" class="event-input" onchange="load()"/>
    </div>
    <div style="flex:1">
      <div class="label">To</div>
      <input id="to-date" type="date" class="event-input" onchange="load()"/>
    </div>
  </div>
</div>

<div id="results"></div>
<div id="pager" class="pager"></div>

<script>
const GRAFANA_URL = '__GRAFANA_URL__';
const GRAFANA_UID = '__GRAFANA_UID__';
let currentType = '';
let currentOffset = 0;
const LIMIT = 25;
let loadTimer = null;

function setType(btn, t) {
  currentType = t;
  currentOffset = 0;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  load();
}

function scheduleLoad() {
  clearTimeout(loadTimer);
  loadTimer = setTimeout(load, 300);
}

async function load() {
  const params = new URLSearchParams();
  const q = document.getElementById('q').value.trim();
  if (q) params.set('q', q);
  if (currentType) params.set('type', currentType);
  const from = document.getElementById('from-date').value;
  const to = document.getElementById('to-date').value;
  if (from) params.set('from_date', from);
  if (to) params.set('to_date', to);
  params.set('limit', LIMIT);
  params.set('offset', currentOffset);
  const r = await fetch('/api/sessions?' + params);
  const data = await r.json();
  render(data);
}

function fmtTime(iso) {
  if (!iso) return '—';
  return new Date(iso).toISOString().substring(11,16) + ' UTC';
}

function fmtDur(s) {
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), ss = Math.floor(s%60);
  if (h) return h + ':' + String(m).padStart(2,'0') + ':' + String(ss).padStart(2,'0');
  return m + ':' + String(ss).padStart(2,'0');
}

function render(data) {
  const el = document.getElementById('results');
  if (!data.sessions.length) {
    el.innerHTML = '<div class="empty">No sessions found</div>';
    document.getElementById('pager').innerHTML = '';
    return;
  }
  el.innerHTML = data.sessions.map(s => {
    const start = fmtTime(s.start_utc);
    const end = s.end_utc ? fmtTime(s.end_utc) : 'in progress';
    const dur = (s.end_utc && s.duration_s != null) ? ' (' + fmtDur(Math.round(s.duration_s)) + ')' : '';
    const typeClass = s.type === 'race' ? 'badge-race' : s.type === 'practice' ? 'badge-practice' : 'badge-debrief';
    const badge = '<span class="badge ' + typeClass + '">' + s.type.toUpperCase() + '</span>';
    const parent = s.parent_race_name ? '<div class="session-meta">Debrief of ' + s.parent_race_name + '</div>' : '';

    let exports = '';
    if (s.type !== 'debrief' && s.end_utc) {
      const from = new Date(s.start_utc).getTime();
      const to = new Date(s.end_utc).getTime();
      exports += '<a class="btn-export" href="/api/races/' + s.id + '/export.csv">&#8595; CSV</a>';
      exports += '<a class="btn-export" href="/api/races/' + s.id + '/export.gpx">&#8595; GPX</a>';
      exports += '<a class="btn-export btn-grafana" href="' + GRAFANA_URL + '/d/' + GRAFANA_UID + '/sailing-data?from=' + from + '&to=' + to + '&orgId=1" target="_blank">&#128202; Grafana</a>';
    }
    if (s.has_audio && s.audio_session_id) {
      exports += '<a class="btn-export" href="/api/audio/' + s.audio_session_id + '/download">&#8595; WAV</a>';
    }
    const exportsHtml = exports ? '<div class="session-exports">' + exports + '</div>' : '';

    return '<div class="card"><div class="session-name">' + s.name + badge + '</div>'
      + '<div class="session-meta">' + s.date + ' &nbsp;·&nbsp; ' + start + ' → ' + end + dur + '</div>'
      + parent + exportsHtml + '</div>';
  }).join('');

  const total = data.total;
  const page = Math.floor(currentOffset / LIMIT);
  const totalPages = Math.ceil(total / LIMIT);
  const pager = document.getElementById('pager');
  if (totalPages <= 1) {
    pager.innerHTML = '<span class="pager-info">' + total + ' session' + (total !== 1 ? 's' : '') + '</span>';
  } else {
    pager.innerHTML =
      '<button class="btn btn-secondary" style="padding:8px 14px" onclick="go(' + (page-1) + ')"' + (page===0?' disabled':'') + '>&#8592; Prev</button>'
      + '<span class="pager-info">Page ' + (page+1) + ' of ' + totalPages + ' (' + total + ' total)</span>'
      + '<button class="btn btn-secondary" style="padding:8px 14px" onclick="go(' + (page+1) + ')"' + (page>=totalPages-1?' disabled':'') + '>Next &#8594;</button>';
  }
}

function go(page) {
  currentOffset = page * LIMIT;
  load();
  window.scrollTo(0, 0);
}

// Default: last 30 days
const now = new Date();
const past = new Date(now - 30 * 86400000);
document.getElementById('to-date').value = now.toISOString().substring(0,10);
document.getElementById('from-date').value = past.toISOString().substring(0,10);
load();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------


class EventRequest(BaseModel):
    event_name: str


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    storage: Storage,
    recorder: AudioRecorder | None = None,
    audio_config: AudioConfig | None = None,
) -> FastAPI:
    """Create and return the FastAPI application bound to the given Storage.

    If *recorder* and *audio_config* are provided, recording starts when a race
    starts and stops when the race ends.
    """
    app = FastAPI(title="J105 Logger", docs_url=None, redoc_url=None)
    _audio_session_id: int | None = None
    _debrief_audio_session_id: int | None = None
    _debrief_race_id: int | None = None
    _debrief_race_name: str | None = None
    _debrief_start_utc: datetime | None = None

    from logger.races import RaceConfig

    cfg = RaceConfig()
    _page = _HTML.replace("__GRAFANA_URL__", cfg.grafana_url).replace(
        "__GRAFANA_UID__", cfg.grafana_uid
    )
    _history_page = _HISTORY_HTML.replace("__GRAFANA_URL__", cfg.grafana_url).replace(
        "__GRAFANA_UID__", cfg.grafana_uid
    )

    # ------------------------------------------------------------------
    # HTML UI
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> HTMLResponse:
        return HTMLResponse(_page)

    @app.get("/history", response_class=HTMLResponse, include_in_schema=False)
    async def history_page() -> HTMLResponse:
        return HTMLResponse(_history_page)

    # ------------------------------------------------------------------
    # /api/state
    # ------------------------------------------------------------------

    @app.get("/api/state")
    async def api_state() -> JSONResponse:
        from logger.races import Race as _Race
        from logger.races import default_event_for_date

        now = datetime.now(UTC)
        today = now.date()
        date_str = today.isoformat()
        weekday = today.strftime("%A")

        default_event = default_event_for_date(today)
        custom_event = await storage.get_daily_event(date_str)

        if default_event is not None:
            event: str | None = default_event
            event_is_default = True
        elif custom_event is not None:
            event = custom_event
            event_is_default = False
        else:
            event = None
            event_is_default = False

        current = await storage.get_current_race()
        today_races = await storage.list_races_for_date(date_str)

        next_race_num = await storage.count_sessions_for_date(date_str, "race") + 1
        next_practice_num = await storage.count_sessions_for_date(date_str, "practice") + 1

        def _race_dict(r: _Race) -> dict[str, Any]:
            duration_s: float | None = None
            if r.end_utc is not None:
                duration_s = (r.end_utc - r.start_utc).total_seconds()
            else:
                elapsed = (now - r.start_utc).total_seconds()
                duration_s = elapsed
            return {
                "id": r.id,
                "name": r.name,
                "event": r.event,
                "race_num": r.race_num,
                "date": r.date,
                "start_utc": r.start_utc.isoformat(),
                "end_utc": r.end_utc.isoformat() if r.end_utc else None,
                "duration_s": round(duration_s, 1) if duration_s is not None else None,
                "session_type": r.session_type,
            }

        return JSONResponse(
            {
                "date": date_str,
                "weekday": weekday,
                "event": event,
                "event_is_default": event_is_default,
                "current_race": _race_dict(current) if current else None,
                "next_race_num": next_race_num,
                "next_practice_num": next_practice_num,
                "today_races": [_race_dict(r) for r in today_races],
                "has_recorder": recorder is not None,
                "current_debrief": {
                    "race_id": _debrief_race_id,
                    "race_name": _debrief_race_name,
                    "start_utc": _debrief_start_utc.isoformat(),
                }
                if _debrief_race_id is not None
                else None,
            }
        )

    # ------------------------------------------------------------------
    # /api/instruments
    # ------------------------------------------------------------------

    @app.get("/api/instruments")
    async def api_instruments() -> JSONResponse:
        data = await storage.latest_instruments()
        return JSONResponse(data)

    # ------------------------------------------------------------------
    # /api/event
    # ------------------------------------------------------------------

    @app.post("/api/event", status_code=204)
    async def api_set_event(body: EventRequest) -> None:
        event_name = body.event_name.strip()
        if not event_name:
            raise HTTPException(status_code=422, detail="event_name must not be blank")
        date_str = datetime.now(UTC).date().isoformat()
        await storage.set_daily_event(date_str, event_name)

    # ------------------------------------------------------------------
    # /api/races/start
    # ------------------------------------------------------------------

    @app.post("/api/races/start", status_code=201)
    async def api_start_race(
        session_type: str = Query(default="race"),
    ) -> JSONResponse:
        nonlocal _audio_session_id
        from logger.races import build_race_name, default_event_for_date

        if session_type not in ("race", "practice"):
            raise HTTPException(
                status_code=422,
                detail="session_type must be 'race' or 'practice'",
            )

        now = datetime.now(UTC)
        today = now.date()
        date_str = today.isoformat()

        default_event = default_event_for_date(today)
        custom_event = await storage.get_daily_event(date_str)
        event = default_event or custom_event
        if event is None:
            raise HTTPException(
                status_code=422,
                detail="No event set for today. POST /api/event first.",
            )

        race_num = await storage.count_sessions_for_date(date_str, session_type) + 1
        name = build_race_name(event, today, race_num, session_type)

        race = await storage.start_race(event, now, date_str, race_num, name, session_type)

        if recorder is not None and audio_config is not None:
            from logger.audio import AudioDeviceNotFoundError

            try:
                session = await recorder.start(audio_config, name=race.name)
                _audio_session_id = await storage.write_audio_session(
                    session,
                    race_id=race.id,
                    session_type=session_type,
                    name=race.name,
                )
                logger.info("Audio recording started: {}", session.file_path)
            except AudioDeviceNotFoundError as exc:
                logger.warning("Audio unavailable for race {}: {}", race.name, exc)

        return JSONResponse(
            {
                "id": race.id,
                "name": race.name,
                "event": race.event,
                "race_num": race.race_num,
                "start_utc": race.start_utc.isoformat(),
                "session_type": race.session_type,
            },
            status_code=201,
        )

    # ------------------------------------------------------------------
    # /api/races/{id}/end
    # ------------------------------------------------------------------

    @app.post("/api/races/{race_id}/end", status_code=204)
    async def api_end_race(race_id: int) -> None:
        nonlocal _audio_session_id
        now = datetime.now(UTC)
        await storage.end_race(race_id, now)

        if recorder is not None and _audio_session_id is not None:
            completed = await recorder.stop()
            assert completed.end_utc is not None
            await storage.update_audio_session_end(_audio_session_id, completed.end_utc)
            logger.info("Audio recording saved: {}", completed.file_path)
            _audio_session_id = None

    # ------------------------------------------------------------------
    # /api/races/{id}/debrief/start
    # ------------------------------------------------------------------

    @app.post("/api/races/{race_id}/debrief/start", status_code=201)
    async def api_start_debrief(race_id: int) -> JSONResponse:
        nonlocal _debrief_audio_session_id, _debrief_race_id, _debrief_race_name, _debrief_start_utc

        if recorder is None or audio_config is None:
            raise HTTPException(status_code=409, detail="No audio recorder configured")

        cur = await storage._conn().execute(
            "SELECT id, name, end_utc FROM races WHERE id = ?", (race_id,)
        )
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Race not found")
        if row["end_utc"] is None:
            raise HTTPException(status_code=409, detail="Race is still in progress")

        if _debrief_audio_session_id is not None:
            completed = await recorder.stop()
            assert completed.end_utc is not None
            await storage.update_audio_session_end(_debrief_audio_session_id, completed.end_utc)
            _debrief_audio_session_id = None

        debrief_name = f"{row['name']}-debrief"
        now = datetime.now(UTC)
        session = await recorder.start(audio_config, name=debrief_name)
        _debrief_audio_session_id = await storage.write_audio_session(
            session,
            race_id=race_id,
            session_type="debrief",
            name=debrief_name,
        )
        _debrief_race_id = race_id
        _debrief_race_name = row["name"]
        _debrief_start_utc = now
        logger.info("Debrief recording started: {}", session.file_path)

        return JSONResponse(
            {"race_id": race_id, "race_name": row["name"], "start_utc": now.isoformat()},
            status_code=201,
        )

    # ------------------------------------------------------------------
    # /api/debrief/stop
    # ------------------------------------------------------------------

    @app.post("/api/debrief/stop", status_code=204)
    async def api_stop_debrief() -> None:
        nonlocal _debrief_audio_session_id, _debrief_race_id, _debrief_race_name, _debrief_start_utc

        if _debrief_audio_session_id is None:
            raise HTTPException(status_code=409, detail="No debrief in progress")

        completed = await recorder.stop()
        assert completed.end_utc is not None
        await storage.update_audio_session_end(_debrief_audio_session_id, completed.end_utc)
        logger.info("Debrief recording saved: {}", completed.file_path)

        _debrief_audio_session_id = None
        _debrief_race_id = None
        _debrief_race_name = None
        _debrief_start_utc = None

    # ------------------------------------------------------------------
    # /api/races/{id}/export.{fmt}
    # ------------------------------------------------------------------

    @app.get("/api/races/{race_id}/export.{fmt}")
    async def api_export_race(race_id: int, fmt: str) -> FileResponse:
        if fmt not in ("csv", "gpx", "json"):
            raise HTTPException(status_code=400, detail="fmt must be csv, gpx, or json")

        races = await storage.list_races_for_date(datetime.now(UTC).date().isoformat())
        # Also search across all dates by fetching by id directly
        race = None
        for r in races:
            if r.id == race_id:
                race = r
                break

        if race is None:
            # Fallback: search all races (no date filter)
            cur = await storage._conn().execute(
                "SELECT id, name, event, race_num, date, start_utc, end_utc, session_type"
                " FROM races WHERE id = ?",
                (race_id,),
            )
            row = await cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Race not found")
            from datetime import datetime as _dt

            from logger.races import Race

            race = Race(
                id=row["id"],
                name=row["name"],
                event=row["event"],
                race_num=row["race_num"],
                date=row["date"],
                start_utc=_dt.fromisoformat(row["start_utc"]),
                end_utc=_dt.fromisoformat(row["end_utc"]) if row["end_utc"] else None,
                session_type=row["session_type"],
            )

        if race.end_utc is None:
            raise HTTPException(status_code=409, detail="Race is still in progress")

        from logger.export import export_to_file

        suffix = f".{fmt}"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            out_path = f.name

        await export_to_file(storage, race.start_utc, race.end_utc, out_path)

        filename = f"{race.name}.{fmt}"
        media = {
            "csv": "text/csv",
            "gpx": "application/gpx+xml",
            "json": "application/json",
        }[fmt]
        return FileResponse(
            out_path,
            media_type=media,
            filename=filename,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # ------------------------------------------------------------------
    # /api/sessions  (history browser)
    # ------------------------------------------------------------------

    @app.get("/api/sessions")
    async def api_sessions(
        q: str | None = None,
        type: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> JSONResponse:
        if type is not None and type not in ("race", "practice", "debrief"):
            raise HTTPException(
                status_code=422,
                detail="type must be 'race', 'practice', or 'debrief'",
            )
        limit = max(1, min(limit, 200))
        total, sessions = await storage.list_sessions(
            q=q or None,
            session_type=type,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
            offset=offset,
        )
        return JSONResponse({"total": total, "sessions": sessions})

    # ------------------------------------------------------------------
    # /api/races
    # ------------------------------------------------------------------

    @app.get("/api/races")
    async def api_list_races(date: str | None = None) -> JSONResponse:
        if date is None:
            date = datetime.now(UTC).date().isoformat()
        races = await storage.list_races_for_date(date)
        result = []
        for r in races:
            duration_s: float | None = None
            if r.end_utc is not None:
                duration_s = (r.end_utc - r.start_utc).total_seconds()
            result.append(
                {
                    "id": r.id,
                    "name": r.name,
                    "event": r.event,
                    "race_num": r.race_num,
                    "date": r.date,
                    "start_utc": r.start_utc.isoformat(),
                    "end_utc": r.end_utc.isoformat() if r.end_utc else None,
                    "duration_s": round(duration_s, 1) if duration_s is not None else None,
                }
            )
        return JSONResponse(result)

    return app
