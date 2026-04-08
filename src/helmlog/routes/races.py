"""Route handlers for races."""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from loguru import logger

from helmlog.auth import require_auth
from helmlog.routes._helpers import EventRequest, audit, get_storage, limiter, load_cameras

router = APIRouter()


@router.post("/api/event", status_code=204)
async def api_set_event(
    request: Request,
    body: EventRequest,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> None:
    storage = get_storage(request)
    event_name = body.event_name.strip()
    if not event_name:
        raise HTTPException(status_code=422, detail="event_name must not be blank")
    from helmlog.races import local_today

    date_str = local_today().isoformat()
    await storage.set_daily_event(date_str, event_name)
    await audit(request, "event.set", detail=event_name, user=_user)


# ------------------------------------------------------------------
# /api/event-rules (day-of-week → event name)
# ------------------------------------------------------------------

_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


@router.get("/api/event-rules")
async def api_list_event_rules(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    rules = await storage.list_event_rules()
    for r in rules:
        r["weekday_name"] = _WEEKDAY_NAMES[r["weekday"]]
    return JSONResponse(rules)


@router.post("/api/event-rules", status_code=201)
async def api_set_event_rule(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    body = await request.json()
    weekday = body.get("weekday")
    event_name = str(body.get("event_name", "")).strip()
    if weekday is None or not isinstance(weekday, int) or not (0 <= weekday <= 6):
        raise HTTPException(400, detail="weekday must be an integer 0 (Mon) – 6 (Sun)")
    if not event_name:
        raise HTTPException(400, detail="event_name is required")
    await storage.set_event_rule(weekday, event_name)
    await audit(
        request, "event_rule.set", detail=f"{_WEEKDAY_NAMES[weekday]}={event_name}", user=_user
    )
    return JSONResponse({"weekday": weekday, "event_name": event_name})


@router.delete("/api/event-rules/{weekday}", status_code=204)
async def api_delete_event_rule(
    weekday: int,
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> None:
    storage = get_storage(request)
    if not (0 <= weekday <= 6):
        raise HTTPException(400, detail="weekday must be 0–6")
    ok = await storage.delete_event_rule(weekday)
    if not ok:
        raise HTTPException(404, detail="No rule for that weekday")
    await audit(request, "event_rule.delete", detail=_WEEKDAY_NAMES[weekday], user=_user)


# ------------------------------------------------------------------
# /api/races/schedule  (#345 — scheduled race starts)
# ------------------------------------------------------------------


async def _schedule_fire_loop(app: FastAPI) -> None:
    """Background task: poll for a pending scheduled start and fire when due."""
    storage = app.state.storage
    ss = app.state.session_state
    while True:
        try:
            row = await storage.get_scheduled_start()
            if row is not None:
                fire_at = datetime.fromisoformat(row["scheduled_start_utc"])
                if fire_at.tzinfo is None:
                    fire_at = fire_at.replace(tzinfo=UTC)
                now = datetime.now(UTC)
                if now >= fire_at:
                    if not ss.schedule_first_check_done:
                        # First check after startup — missed start, don't fire
                        logger.warning(
                            "Scheduled start at {} was missed — system was not running",
                            row["scheduled_start_utc"],
                        )
                        await storage.cancel_scheduled_start()
                    else:
                        # Normal operation — fire the scheduled start
                        current = await storage.get_current_race()
                        if current is not None:
                            await storage.cancel_scheduled_start()
                            logger.warning(
                                "Scheduled start cancelled — race {} already active",
                                current.name,
                            )
                        else:
                            await storage.cancel_scheduled_start()
                            await _do_scheduled_start(app, row["event"], row["session_type"])
            ss.schedule_first_check_done = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Schedule fire loop error: {}", exc)
        await asyncio.sleep(1)


async def _do_scheduled_start(app: FastAPI, event: str, session_type: str) -> None:
    """Fire a scheduled start — equivalent to pressing Start."""
    storage = app.state.storage
    ss = app.state.session_state
    from helmlog.races import build_race_name, local_today

    now = datetime.now(UTC)
    today = local_today()
    date_str = today.isoformat()
    race_num = await storage.count_sessions_for_date(date_str, session_type) + 1
    name = build_race_name(event, today, race_num, session_type)
    try:
        race = await storage.start_race(event, now, date_str, race_num, name, session_type)
        logger.info("Scheduled start fired: {} (id={})", race.name, race.id)

        # Auto-apply sail defaults
        try:
            sail_defaults = await storage.get_sail_defaults()
            has_any = any(sail_defaults[t] is not None for t in ("main", "jib", "spinnaker"))
            if has_any:
                await storage.insert_sail_change(
                    race.id,
                    race.start_utc.isoformat(),
                    main_id=sail_defaults["main"]["id"] if sail_defaults["main"] else None,
                    jib_id=sail_defaults["jib"]["id"] if sail_defaults["jib"] else None,
                    spinnaker_id=(
                        sail_defaults["spinnaker"]["id"] if sail_defaults["spinnaker"] else None
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Sail defaults failed for scheduled race {}: {}", name, exc)

        # Start audio if request.app.state.recorder is available
        if app.state.recorder is not None and app.state.audio_config is not None:
            from helmlog.audio import AudioDeviceNotFoundError

            try:
                session = await app.state.recorder.start(app.state.audio_config, name=race.name)
                ss.audio_session_id = await storage.write_audio_session(
                    session,
                    race_id=race.id,
                    session_type=session_type,
                    name=race.name,
                )
            except AudioDeviceNotFoundError as exc:
                logger.warning("Audio unavailable for scheduled race {}: {}", name, exc)

        await storage.log_action("race.scheduled_start", detail=race.name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Scheduled start failed: {}", exc)


@router.on_event("startup")
async def _start_schedule_loop() -> None:
    # Note: at startup time, router doesn't have direct app access.
    # This is handled by the app including the router; FastAPI injects app context.
    # We defer the actual task creation to the first request or use a workaround.
    pass


@router.on_event("shutdown")
async def _stop_schedule_loop() -> None:
    pass


def start_schedule_loop(app: FastAPI) -> None:
    """Called from create_app() after the router is included to start the background task."""
    ss = app.state.session_state
    ss.schedule_task = asyncio.create_task(_schedule_fire_loop(app))


def stop_schedule_loop(app: FastAPI) -> None:
    """Called on shutdown to cancel the background task."""
    ss = app.state.session_state
    if ss.schedule_task is not None:
        ss.schedule_task.cancel()


@router.post("/api/races/schedule", status_code=201)
async def api_schedule_start(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    body = await request.json()
    raw_ts = body.get("scheduled_start_utc")
    event = body.get("event")
    session_type = body.get("session_type", "race")
    if not raw_ts:
        raise HTTPException(422, detail="scheduled_start_utc is required")

    try:
        fire_at = datetime.fromisoformat(raw_ts)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, detail=f"Invalid timestamp: {exc}") from exc
    if fire_at.tzinfo is None:
        fire_at = fire_at.replace(tzinfo=UTC)

    now = datetime.now(UTC)
    if fire_at <= now + timedelta(seconds=5):
        raise HTTPException(422, detail="scheduled_start_utc must be in the future (> now + 5s)")

    if session_type not in ("race", "practice"):
        raise HTTPException(422, detail="session_type must be 'race' or 'practice'")

    # If no event provided, resolve from rules / daily override
    if not event:
        from helmlog.races import default_event_for_date, local_today

        today = local_today()
        date_str = today.isoformat()
        rules = {r["weekday"]: r["event_name"] for r in await storage.list_event_rules()}
        default_ev = default_event_for_date(today, rules)
        custom_ev = await storage.get_daily_event(date_str)
        event = custom_ev or default_ev
        if event is None:
            raise HTTPException(422, detail="No event set for today. POST /api/event first.")

    row_id = await storage.schedule_start(fire_at, event, session_type)
    await audit(request, "race.schedule", detail=fire_at.isoformat(), user=_user)
    seconds_until = max(0, int((fire_at - now).total_seconds()))
    return JSONResponse(
        {
            "id": row_id,
            "scheduled_start_utc": fire_at.isoformat(),
            "event": event,
            "session_type": session_type,
            "seconds_until_start": seconds_until,
        },
        status_code=201,
    )


@router.get("/api/races/schedule")
async def api_get_schedule(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    row = await storage.get_scheduled_start()
    if row is None:
        raise HTTPException(404, detail="No scheduled start")
    fire_at = datetime.fromisoformat(row["scheduled_start_utc"])
    if fire_at.tzinfo is None:
        fire_at = fire_at.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    seconds_until = max(0, int((fire_at - now).total_seconds()))
    return JSONResponse(
        {
            "id": row["id"],
            "scheduled_start_utc": row["scheduled_start_utc"],
            "event": row["event"],
            "session_type": row["session_type"],
            "seconds_until_start": seconds_until,
        }
    )


@router.delete("/api/races/schedule", status_code=204)
async def api_cancel_schedule(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> None:
    storage = get_storage(request)
    await storage.cancel_scheduled_start()
    await audit(request, "race.schedule_cancel", user=_user)


@router.post("/api/races/start", status_code=201)
async def api_start_race(
    request: Request,
    session_type: str = Query(default="race"),
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    ss = request.app.state.session_state
    from helmlog.races import build_race_name, default_event_for_date

    if session_type not in ("race", "practice"):
        raise HTTPException(
            status_code=422,
            detail="session_type must be 'race' or 'practice'",
        )

    from helmlog.races import local_today

    today = local_today()
    date_str = today.isoformat()

    rules = {r["weekday"]: r["event_name"] for r in await storage.list_event_rules()}
    default_event = default_event_for_date(today, rules)
    custom_event = await storage.get_daily_event(date_str)
    event = custom_event or default_event
    if event is None:
        raise HTTPException(
            status_code=422,
            detail="No event set for today. POST /api/event first.",
        )

    # Cancel any pending scheduled start (#345)
    await storage.cancel_scheduled_start()

    # Auto-stop any active debrief before starting a new session
    if ss.debrief_audio_session_id is not None:
        completed = await request.app.state.recorder.stop()
        assert completed.end_utc is not None
        await storage.update_audio_session_end(ss.debrief_audio_session_id, completed.end_utc)
        logger.info("Debrief auto-stopped to start new {}", session_type)
        ss.debrief_audio_session_id = None
        ss.debrief_race_id = None
        ss.debrief_race_name = None
        ss.debrief_start_utc = None

    race_num = await storage.count_sessions_for_date(date_str, session_type) + 1
    name = build_race_name(event, today, race_num, session_type)

    now = datetime.now(UTC)
    race = await storage.start_race(event, now, date_str, race_num, name, session_type)

    # Boat-level crew defaults auto-apply via resolve_crew() —
    # no explicit copy-forward needed (#305)

    # Auto-apply sail defaults as initial sail_changes row (#311)
    try:
        sail_defaults = await storage.get_sail_defaults()
        has_any = any(sail_defaults[t] is not None for t in ("main", "jib", "spinnaker"))
        if has_any:
            await storage.insert_sail_change(
                race.id,
                race.start_utc.isoformat(),
                main_id=sail_defaults["main"]["id"] if sail_defaults["main"] else None,
                jib_id=sail_defaults["jib"]["id"] if sail_defaults["jib"] else None,
                spinnaker_id=(
                    sail_defaults["spinnaker"]["id"] if sail_defaults["spinnaker"] else None
                ),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to auto-apply sail defaults for race {}: {}", race.name, exc)

    if request.app.state.recorder is not None and request.app.state.audio_config is not None:
        from helmlog.audio import AudioDeviceNotFoundError

        try:
            session = await request.app.state.recorder.start(
                request.app.state.audio_config, name=race.name
            )
            ss.audio_session_id = await storage.write_audio_session(
                session,
                race_id=race.id,
                session_type=session_type,
                name=race.name,
            )
            logger.info("Audio recording started: {}", session.file_path)
        except AudioDeviceNotFoundError as exc:
            logger.warning("Audio unavailable for race {}: {}", race.name, exc)

    async def _start_cameras(rid: int) -> None:
        cams = await load_cameras(request)
        if not cams:
            return
        import helmlog.cameras as cameras_mod

        try:
            statuses = await cameras_mod.start_all(cams, rid, storage)
            for s in statuses:
                if s.error:
                    logger.warning("Camera {} failed to start: {}", s.name, s.error)
                else:
                    logger.info("Camera {} recording started", s.name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Camera start_all failed: {}", exc)

    async def _network_auto_switch_start() -> None:
        import helmlog.network as net_mod

        try:
            result = await net_mod.auto_switch_for_race_start(storage)
            if result and not result.success:
                logger.warning("WLAN auto-switch failed: {}", result.error)
        except Exception as exc:  # noqa: BLE001
            logger.warning("WLAN auto-switch error: {}", exc)

    asyncio.ensure_future(_start_cameras(race.id))
    asyncio.ensure_future(_network_auto_switch_start())
    await audit(request, "race.start", detail=race.name, user=_user)
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


@router.post("/api/races/{race_id}/end", status_code=204)
async def api_end_race(
    request: Request,
    race_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> None:
    storage = get_storage(request)
    ss = request.app.state.session_state
    now = datetime.now(UTC)
    await storage.end_race(race_id, now)
    await audit(request, "race.end", detail=str(race_id), user=_user)

    async def _stop_cameras(rid: int) -> None:
        cams = await load_cameras(request)
        if not cams:
            return
        import helmlog.cameras as cameras_mod

        try:
            statuses = await cameras_mod.stop_all(cams, rid, storage)
            for s in statuses:
                if s.error:
                    logger.warning("Camera {} failed to stop: {}", s.name, s.error)
                else:
                    logger.info("Camera {} recording stopped", s.name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Camera stop_all failed: {}", exc)

    async def _network_auto_switch_end() -> None:
        import helmlog.network as net_mod

        try:
            result = await net_mod.auto_switch_for_race_end(storage)
            if result and not result.success:
                logger.warning("WLAN auto-revert failed: {}", result.error)
        except Exception as exc:  # noqa: BLE001
            logger.warning("WLAN auto-revert error: {}", exc)

    async def _auto_detect_maneuvers(rid: int) -> None:
        try:
            from helmlog.maneuver_detector import detect_maneuvers

            maneuvers = await detect_maneuvers(storage, rid)
            tacks = sum(1 for m in maneuvers if m.type == "tack")
            gybes = sum(1 for m in maneuvers if m.type == "gybe")
            logger.info("Auto-detected {} tacks, {} gybes for race {}", tacks, gybes, rid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Auto maneuver detection failed for race {}: {}", rid, exc)

    asyncio.ensure_future(_stop_cameras(race_id))
    asyncio.ensure_future(_network_auto_switch_end())
    asyncio.ensure_future(_auto_detect_maneuvers(race_id))

    if request.app.state.recorder is not None and ss.audio_session_id is not None:
        completed = await request.app.state.recorder.stop()
        assert completed.end_utc is not None
        await storage.update_audio_session_end(ss.audio_session_id, completed.end_utc)
        logger.info("Audio recording saved: {}", completed.file_path)
        ss.audio_session_id = None


@router.post("/api/races/{race_id}/debrief/start", status_code=201)
async def api_start_debrief(
    request: Request,
    race_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    ss = request.app.state.session_state

    if request.app.state.recorder is None or request.app.state.audio_config is None:
        raise HTTPException(status_code=409, detail="No audio recorder configured")

    cur = await storage._conn().execute(
        "SELECT id, name, end_utc FROM races WHERE id = ?", (race_id,)
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Race not found")

    # Defensive: if the race is still in progress, auto-end it first
    if row["end_utc"] is None:
        now_end = datetime.now(UTC)
        await storage.end_race(race_id, now_end)
        if ss.audio_session_id is not None:
            completed = await request.app.state.recorder.stop()
            assert completed.end_utc is not None
            await storage.update_audio_session_end(ss.audio_session_id, completed.end_utc)
            ss.audio_session_id = None
        logger.info("Race {} auto-ended to start debrief", race_id)

    if ss.debrief_audio_session_id is not None:
        completed = await request.app.state.recorder.stop()
        assert completed.end_utc is not None
        await storage.update_audio_session_end(ss.debrief_audio_session_id, completed.end_utc)
        ss.debrief_audio_session_id = None

    debrief_name = f"{row['name']}-debrief"
    now = datetime.now(UTC)
    session = await request.app.state.recorder.start(
        request.app.state.audio_config, name=debrief_name
    )
    ss.debrief_audio_session_id = await storage.write_audio_session(
        session,
        race_id=race_id,
        session_type="debrief",
        name=debrief_name,
    )
    ss.debrief_race_id = race_id
    ss.debrief_race_name = row["name"]
    ss.debrief_start_utc = now
    logger.info("Debrief recording started: {}", session.file_path)

    await audit(request, "debrief.start", detail=row["name"], user=_user)
    return JSONResponse(
        {"race_id": race_id, "race_name": row["name"], "start_utc": now.isoformat()},
        status_code=201,
    )


@router.post("/api/debrief/stop", status_code=204)
async def api_stop_debrief(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> None:
    storage = get_storage(request)
    ss = request.app.state.session_state

    if ss.debrief_audio_session_id is None:
        raise HTTPException(status_code=409, detail="No debrief in progress")

    completed = await request.app.state.recorder.stop()
    assert completed.end_utc is not None
    await storage.update_audio_session_end(ss.debrief_audio_session_id, completed.end_utc)
    logger.info("Debrief recording saved: {}", completed.file_path)

    await audit(request, "debrief.stop", user=_user)
    ss.debrief_audio_session_id = None
    ss.debrief_race_id = None
    ss.debrief_race_name = None
    ss.debrief_start_utc = None


@router.get("/api/races/{race_id}/export.{fmt}")
@limiter.limit("20/minute")
async def api_export_race(
    request: Request,
    race_id: int,
    fmt: str,
    gps_precision: int | None = Query(default=None, ge=0, le=8),
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> FileResponse:
    """Export race data. Optional gps_precision (0-8 decimal places) reduces GPS accuracy (#203)."""
    storage = get_storage(request)
    if fmt not in ("csv", "gpx", "json"):
        raise HTTPException(status_code=400, detail="fmt must be csv, gpx, or json")

    from helmlog.races import local_today

    races = await storage.list_races_for_date(local_today().isoformat())
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

        from helmlog.races import Race

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

    from helmlog.export import export_to_file

    suffix = f".{fmt}"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        out_path = f.name

    await export_to_file(
        storage,
        race.start_utc,
        race.end_utc,
        out_path,
        gps_precision=gps_precision,
    )

    filename = f"{race.name}.{fmt}"
    media = {
        "csv": "text/csv",
        "gpx": "application/gpx+xml",
        "json": "application/json",
    }[fmt]
    await audit(request, "export.download", detail=f"{race.name}.{fmt}", user=_user)
    return FileResponse(
        out_path,
        media_type=media,
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/courses/marks")
async def api_course_marks(
    request: Request,
    wind_dir: float = 0.0,
    start_lat: float = 47.63,
    start_lon: float = -122.40,
    leg_nm: float = 1.0,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    from helmlog.courses import CYC_MARKS, compute_buoy_marks

    buoy = compute_buoy_marks(start_lat, start_lon, wind_dir, leg_nm)
    buoy_json = {k: {"name": m.name, "lat": m.lat, "lon": m.lon} for k, m in buoy.items()}
    cyc_json = {k: {"name": m.name, "lat": m.lat, "lon": m.lon} for k, m in CYC_MARKS.items()}
    return JSONResponse({"buoy_marks": buoy_json, "cyc_marks": cyc_json})


@router.get("/api/races")
async def api_list_races(
    request: Request,
    date: str | None = None,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    if date is None:
        from helmlog.races import local_today

        date = local_today().isoformat()
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
