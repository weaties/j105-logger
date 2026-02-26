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

from fastapi import FastAPI, HTTPException
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
<h1>J105 Logger</h1>
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
  <button class="btn btn-primary" id="btn-start" onclick="startRace()">▶ START RACE</button>
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
  const btnStart = document.getElementById('btn-start');

  if(cur) {
    curCard.classList.remove('hidden');
    btnEnd.classList.remove('hidden');
    document.getElementById('cur-name').textContent = cur.name;
    document.getElementById('cur-meta').textContent =
      'Started ' + fmtTime(cur.start_utc);
    curRaceStartMs = new Date(cur.start_utc).getTime();
    btnEnd.textContent = '■ END ' + cur.name;
  } else {
    curCard.classList.add('hidden');
    btnEnd.classList.add('hidden');
    curRaceStartMs = null;
    clearInterval(tickInterval);
  }

  btnStart.textContent = `▶ START RACE ${s.next_race_num}`;

  const hist = document.getElementById('history-card');
  const list = document.getElementById('race-list');
  if(s.today_races && s.today_races.length) {
    hist.style.display = '';
    list.innerHTML = s.today_races.slice().reverse().map(r => {
      const start = fmtTime(r.start_utc);
      const end = r.end_utc ? fmtTime(r.end_utc) : 'in progress';
      const dur = (r.end_utc && r.duration_s != null)
        ? ` (${fmt(Math.round(r.duration_s))})` : '';
      const exports = r.end_utc
        ? `<div class="race-exports">
             <a class="btn-export" href="/api/races/${r.id}/export.csv">↓ CSV</a>
             <a class="btn-export" href="/api/races/${r.id}/export.gpx">↓ GPX</a>
           </div>`
        : '';
      return `<div class="race-item">
        <div class="race-item-name">${r.name}</div>
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
  if(!curRaceStartMs) return;
  const elapsed = Math.floor((Date.now() - curRaceStartMs) / 1000);
  document.getElementById('cur-duration').textContent = fmt(elapsed);
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

async function startRace() {
  await fetch('/api/races/start', {method:'POST'});
  await loadState();
  clearInterval(tickInterval);
  if(curRaceStartMs) tickInterval = setInterval(tick, 1000);
}

async function endRace() {
  if(!state || !state.current_race) return;
  await fetch(`/api/races/${state.current_race.id}/end`, {method:'POST'});
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

    # ------------------------------------------------------------------
    # HTML UI
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> HTMLResponse:
        return HTMLResponse(_HTML)

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

        next_race_num = len(today_races) + 1

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
            }

        return JSONResponse(
            {
                "date": date_str,
                "weekday": weekday,
                "event": event,
                "event_is_default": event_is_default,
                "current_race": _race_dict(current) if current else None,
                "next_race_num": next_race_num,
                "today_races": [_race_dict(r) for r in today_races],
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
    async def api_start_race() -> JSONResponse:
        nonlocal _audio_session_id
        from logger.races import build_race_name, default_event_for_date

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

        today_races = await storage.list_races_for_date(date_str)
        race_num = len(today_races) + 1
        name = build_race_name(event, today, race_num)

        race = await storage.start_race(event, now, date_str, race_num, name)

        if recorder is not None and audio_config is not None:
            from logger.audio import AudioDeviceNotFoundError

            try:
                session = await recorder.start(audio_config, name=race.name)
                _audio_session_id = await storage.write_audio_session(session)
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
                "SELECT id, name, event, race_num, date, start_utc, end_utc"
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
