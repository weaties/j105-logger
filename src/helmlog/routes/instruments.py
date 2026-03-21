"""Route handlers for instruments."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from helmlog.auth import require_auth
from helmlog.routes._helpers import get_storage

router = APIRouter()


@router.get("/api/state")
async def api_state(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    ss = request.app.state.session_state
    from helmlog.races import Race as _Race
    from helmlog.races import configured_tz, default_event_for_date, local_today, local_weekday

    now = datetime.now(UTC)
    today = local_today()
    date_str = today.isoformat()
    weekday = local_weekday()

    rules = {r["weekday"]: r["event_name"] for r in await storage.list_event_rules()}
    default_event = default_event_for_date(today, rules)
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

    async def _race_dict(r: _Race) -> dict[str, Any]:
        duration_s: float | None = None
        if r.end_utc is not None:
            duration_s = (r.end_utc - r.start_utc).total_seconds()
        else:
            elapsed = (now - r.start_utc).total_seconds()
            duration_s = elapsed
        crew = await storage.resolve_crew(r.id)
        results = await storage.list_race_results(r.id)
        sails = await storage.get_race_sails(r.id)
        cur = await storage._conn().execute(
            "SELECT id FROM audio_sessions"
            " WHERE race_id = ? AND session_type IN ('race', 'practice') LIMIT 1",
            (r.id,),
        )
        audio_row = await cur.fetchone()
        audio_session_id: int | None = audio_row["id"] if audio_row else None
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
            "crew": crew,
            "results": results,
            "sails": sails,
            "has_audio": audio_session_id is not None,
            "audio_session_id": audio_session_id,
        }

    current_dict = await _race_dict(current) if current else None
    today_race_dicts = [await _race_dict(r) for r in today_races]

    # Scheduled start info (#345)
    sched_row = await storage.get_scheduled_start()
    scheduled_start_dict: dict[str, Any] | None = None
    if sched_row is not None:
        fire_at = datetime.fromisoformat(sched_row["scheduled_start_utc"])
        if fire_at.tzinfo is None:
            fire_at = fire_at.replace(tzinfo=UTC)
        secs = max(0, int((fire_at - now).total_seconds()))
        scheduled_start_dict = {
            "scheduled_start_utc": sched_row["scheduled_start_utc"],
            "event": sched_row["event"],
            "session_type": sched_row["session_type"],
            "seconds_until_start": secs,
        }

    return JSONResponse(
        {
            "date": date_str,
            "weekday": weekday,
            "timezone": str(configured_tz()),
            "event": event,
            "event_is_default": event_is_default,
            "current_race": current_dict,
            "next_race_num": next_race_num,
            "next_practice_num": next_practice_num,
            "today_races": today_race_dicts,
            "has_recorder": request.app.state.recorder is not None,
            "scheduled_start": scheduled_start_dict,
            "current_debrief": {
                "race_id": ss.debrief_race_id,
                "race_name": ss.debrief_race_name,
                "start_utc": ss.debrief_start_utc.isoformat(),
            }
            if ss.debrief_race_id is not None
            else None,
        }
    )


@router.get("/api/instruments")
async def api_instruments(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    data = await storage.latest_instruments()
    return JSONResponse(data)


@router.get("/api/system-health")
async def api_system_health(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return current CPU, memory, and disk utilisation percentages."""
    get_storage(request)
    import psutil  # type: ignore[import-untyped]

    cpu = psutil.cpu_percent(interval=0.2)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    payload: dict[str, float | None] = {
        "cpu_pct": cpu,
        "mem_pct": mem.percent,
        "disk_pct": disk.percent,
    }
    temp_c: float | None = None
    get_temps = getattr(psutil, "sensors_temperatures", None)
    if get_temps is not None:
        temps: dict[str, list[object]] = get_temps()
        for entries in temps.values():
            if entries:
                current = getattr(entries[0], "current", None)
                if current is not None:
                    temp_c = float(current)
                break
    payload["cpu_temp_c"] = temp_c
    return JSONResponse(payload)
