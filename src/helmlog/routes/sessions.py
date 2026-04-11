"""Route handlers for sessions."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from loguru import logger

from helmlog.auth import require_auth, require_developer
from helmlog.routes._helpers import audit, get_storage, limiter

router = APIRouter()


async def _resolve_session(request: Request, session_id: int) -> tuple[int | None, int | None]:
    """Return (race_id, audio_session_id) for the given session_id, or raise 404."""
    storage = get_storage(request)
    cur = await storage._conn().execute("SELECT id FROM races WHERE id = ?", (session_id,))
    if await cur.fetchone() is not None:
        return session_id, None
    cur2 = await storage._conn().execute(
        "SELECT id FROM audio_sessions WHERE id = ?", (session_id,)
    )
    if await cur2.fetchone() is not None:
        return None, session_id
    raise HTTPException(status_code=404, detail="Session not found")


@router.post("/api/sessions/synthesize")
async def api_synthesize_session(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
    _dev: dict[str, Any] = Depends(require_developer),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    from helmlog.courses import (
        CourseMark,
        build_custom_course,
        build_triangle_course,
        build_wl_course,
        validate_course_marks,
    )
    from helmlog.races import build_race_name, local_today
    from helmlog.synthesize import (
        CollisionAvoidanceConfig,
        HeaderResponseConfig,
        SynthConfig,
        generate_boat_settings,
        simulate,
    )

    body = await request.json()
    course_type = body.get("course_type", "windward_leeward")
    wind_dir = float(body.get("wind_direction", 0.0))
    tws_low = float(body.get("wind_speed_low", 8.0))
    tws_high = float(body.get("wind_speed_high", 14.0))
    shift_mag_lo = float(body.get("shift_magnitude_low", 5.0))
    shift_mag_hi = float(body.get("shift_magnitude_high", 14.0))
    start_lat = float(body.get("start_lat", 47.63))
    start_lon = float(body.get("start_lon", -122.40))
    leg_nm = float(body.get("leg_distance_nm", 1.0))
    laps = int(body.get("laps", 2))
    seed = int(body.get("seed", 42))
    raw_wind_seed = body.get("wind_seed")
    wind_seed: int | None = int(raw_wind_seed) if raw_wind_seed is not None else None
    mark_sequence = body.get("mark_sequence", "")
    peer_fingerprint: str | None = body.get("peer_fingerprint") or None
    peer_co_op_id: str | None = body.get("peer_co_op_id") or None
    raw_start_utc: str | None = body.get("start_utc")  # imported source session start

    # Collision avoidance — other boats' tracks to avoid (#246)
    raw_other_tracks: list[list[dict[str, Any]]] | None = body.get("other_tracks")
    min_separation_m = float(body.get("min_separation_m", 30.0))
    collision_avoidance = CollisionAvoidanceConfig(min_separation_m=min_separation_m)

    # Header response model — probabilistic tacking on wind shifts (#247)
    hr_raw = body.get("header_response")
    if isinstance(hr_raw, dict):
        header_response = HeaderResponseConfig(
            reaction_probability=float(hr_raw.get("reaction_probability", 0.70)),
            min_shift_threshold=(
                float(hr_raw.get("min_shift_threshold_low", 3.0)),
                float(hr_raw.get("min_shift_threshold_high", 8.0)),
            ),
            reaction_delay=(
                float(hr_raw.get("reaction_delay_low", 10.0)),
                float(hr_raw.get("reaction_delay_high", 45.0)),
            ),
            fatigue_start_frac=float(hr_raw.get("fatigue_start_frac", 0.70)),
            fatigue_floor=float(hr_raw.get("fatigue_floor", 0.40)),
        )
    else:
        header_response = HeaderResponseConfig()

    # Parse optional mark position overrides from user-dragged map markers
    raw_overrides = body.get("mark_overrides")
    mark_overrides: dict[str, tuple[float, float]] | None = None
    if isinstance(raw_overrides, dict):
        mark_overrides = {
            k: (float(v["lat"]), float(v["lon"]))
            for k, v in raw_overrides.items()
            if isinstance(v, dict) and "lat" in v and "lon" in v
        }

    if course_type == "windward_leeward":
        legs = build_wl_course(start_lat, start_lon, wind_dir, leg_nm, laps, mark_overrides)
    elif course_type == "triangle":
        legs = build_triangle_course(start_lat, start_lon, wind_dir, leg_nm, mark_overrides)
    elif course_type == "custom":
        if not mark_sequence:
            raise HTTPException(status_code=422, detail="mark_sequence required for custom course")
        try:
            legs = build_custom_course(mark_sequence, start_lat, start_lon, wind_dir, leg_nm)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    else:
        raise HTTPException(status_code=422, detail=f"Unknown course_type: {course_type}")

    # Validate all course marks are in navigable water (>6 ft deep)
    # Build marks from legs only — they already have correct overridden positions
    # and only include marks actually used in the course (#264)
    all_marks: dict[str, CourseMark] = {}
    for leg in legs:
        key = leg.target.name.split()[-1][0]
        if key not in all_marks:
            all_marks[key] = leg.target
    mark_warnings = validate_course_marks(all_marks)

    if raw_start_utc:
        start_time = datetime.fromisoformat(raw_start_utc.replace("Z", "+00:00"))
    else:
        start_time = datetime.now(UTC)
    config = SynthConfig(
        start_lat=start_lat,
        start_lon=start_lon,
        base_twd=wind_dir,
        tws_low=tws_low,
        tws_high=tws_high,
        shift_interval=(600.0, 1200.0),
        shift_magnitude=(shift_mag_lo, shift_mag_hi),
        legs=legs,
        seed=seed,
        start_time=start_time,
        wind_seed=wind_seed,
        header_response=header_response,
        collision_avoidance=collision_avoidance,
    )

    rows = await asyncio.to_thread(simulate, config, raw_other_tracks)
    if not rows:
        raise HTTPException(status_code=500, detail="Simulation produced no data points")

    today = local_today()
    date_str = today.isoformat()
    race_num = await storage.count_sessions_for_date(date_str, "synthesized") + 1
    source_id = str(uuid.uuid4())

    rules = {r["weekday"]: r["event_name"] for r in await storage.list_event_rules()}
    from helmlog.races import default_event_for_date

    custom_event = await storage.get_daily_event(date_str)
    default_event = default_event_for_date(today, rules)
    event = custom_event or default_event or "Synthesized"

    name = build_race_name(event, today, race_num, "synthesized")

    start_utc = rows[0].ts
    end_utc = rows[-1].ts

    race_id = await storage.import_race(
        name=name,
        event=event,
        race_num=race_num,
        date_str=date_str,
        start_utc=start_utc,
        end_utc=end_utc,
        session_type="synthesized",
        source="synthesized",
        source_id=source_id,
        peer_fingerprint=peer_fingerprint,
        peer_co_op_id=peer_co_op_id,
    )
    await storage.import_synthesized_data(rows, race_id=race_id)

    duration_s = (end_utc - start_utc).total_seconds()

    # Persist wind field params and course marks for later visualization
    await storage.save_synth_wind_params(
        race_id,
        {
            "seed": wind_seed if wind_seed is not None else seed,
            "base_twd": wind_dir,
            "tws_low": tws_low,
            "tws_high": tws_high,
            "shift_interval_lo": 600.0,
            "shift_interval_hi": 1200.0,
            "shift_magnitude_lo": shift_mag_lo,
            "shift_magnitude_hi": shift_mag_hi,
            "ref_lat": start_lat,
            "ref_lon": start_lon,
            "duration_s": duration_s,
            "course_type": course_type,
            "leg_distance_nm": leg_nm,
            "laps": laps if course_type == "windward_leeward" else None,
            "mark_sequence": mark_sequence if course_type == "custom" else None,
        },
    )
    marks_to_save = [
        {"mark_key": k, "mark_name": m.name, "lat": m.lat, "lon": m.lon}
        for k, m in all_marks.items()
    ]
    await storage.save_synth_course_marks(race_id, marks_to_save)

    # Generate and persist synthesized boat settings
    synth_settings = generate_boat_settings(rows, config)
    boat_level = [s for s in synth_settings if s.race_id_is_null]
    race_level = [s for s in synth_settings if not s.race_id_is_null]
    if boat_level:
        await storage.create_boat_settings(
            None,
            [{"ts": s.ts, "parameter": s.parameter, "value": s.value} for s in boat_level],
            source="synthesized",
        )
    if race_level:
        await storage.create_boat_settings(
            race_id,
            [{"ts": s.ts, "parameter": s.parameter, "value": s.value} for s in race_level],
            source="synthesized",
        )

    # Auto-apply sail defaults for synthesized race (#311)
    try:
        sail_defaults = await storage.get_sail_defaults()
        has_any = any(sail_defaults[t] is not None for t in ("main", "jib", "spinnaker"))
        if has_any:
            await storage.insert_sail_change(
                race_id,
                start_utc.isoformat(),
                main_id=sail_defaults["main"]["id"] if sail_defaults["main"] else None,
                jib_id=sail_defaults["jib"]["id"] if sail_defaults["jib"] else None,
                spinnaker_id=(
                    sail_defaults["spinnaker"]["id"] if sail_defaults["spinnaker"] else None
                ),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to auto-apply sail defaults for synth {}: {}", name, exc)

    # Auto-detect maneuvers for synthesized race
    async def _auto_detect_synth(rid: int) -> None:
        try:
            from helmlog.maneuver_detector import detect_maneuvers

            maneuvers = await detect_maneuvers(storage, rid)
            tacks = sum(1 for m in maneuvers if m.type == "tack")
            gybes = sum(1 for m in maneuvers if m.type == "gybe")
            logger.info("Synth auto-detected {} tacks, {} gybes for race {}", tacks, gybes, rid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Synth auto maneuver detection failed for race {}: {}", rid, exc)

    asyncio.ensure_future(_auto_detect_synth(race_id))

    detail = name + (f" [peer={peer_fingerprint}]" if peer_fingerprint else "")
    await audit(request, "session.synthesize", detail=detail, user=_user)

    resp: dict[str, Any] = {
        "id": race_id,
        "name": name,
        "points": len(rows),
        "duration_s": round(duration_s, 1),
    }
    if mark_warnings:
        resp["mark_warnings"] = mark_warnings
    return JSONResponse(resp, status_code=201)


@router.get("/api/sessions/{session_id}/track")
@limiter.limit("30/minute")
async def api_session_track(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return GPS track as GeoJSON for map display."""
    storage = get_storage(request)
    db = storage._conn()
    cur = await db.execute("SELECT start_utc, end_utc FROM races WHERE id = ?", (session_id,))
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Race not found")
    start_utc = row["start_utc"]
    end_utc = row["end_utc"] or start_utc

    # Prefer race_id filter (exact match for synthesized sessions);
    # fall back to time-range query for real instrument data.
    rid_cur = await db.execute(
        "SELECT COUNT(*) as cnt FROM positions WHERE race_id = ?", (session_id,)
    )
    rid_row = await rid_cur.fetchone()
    has_race_id = rid_row["cnt"] > 0 if rid_row else False

    if has_race_id:
        pos_cur = await db.execute(
            "SELECT latitude_deg, longitude_deg, ts FROM positions WHERE race_id = ? ORDER BY ts",
            (session_id,),
        )
    else:
        pos_cur = await db.execute(
            "SELECT latitude_deg, longitude_deg, ts FROM positions"
            " WHERE ts >= ? AND ts <= ? ORDER BY ts",
            (start_utc, end_utc),
        )
    positions = await pos_cur.fetchall()
    if not positions:
        return JSONResponse({"type": "FeatureCollection", "features": []})

    coords = [[r["longitude_deg"], r["latitude_deg"]] for r in positions]
    timestamps = [
        t if "+" in t or t.endswith("Z") else t + "Z" for r in positions if (t := r["ts"])
    ]
    feature = {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": coords},
        "properties": {
            "session_id": session_id,
            "points": len(coords),
            "timestamps": timestamps,
        },
    }
    return JSONResponse({"type": "FeatureCollection", "features": [feature]})


@router.get("/api/sessions/{session_id}/vakaros-overlay")
@limiter.limit("30/minute")
async def api_session_vakaros_overlay(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return Vakaros overlay data (track, start line, race events) for a race (#458).

    Used by the session detail page to augment the SK track with Vakaros-native
    overlays when a matched Vakaros session exists. Returns ``matched: false``
    with empty collections when the race is valid but has no matched session,
    and 404 when the race itself does not exist.
    """
    storage = get_storage(request)
    db = storage._conn()
    cur = await db.execute("SELECT id FROM races WHERE id = ?", (session_id,))
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="Race not found")

    overlay = await storage.get_vakaros_overlay_for_race(session_id)
    if overlay is None:
        return JSONResponse(
            {
                "matched": False,
                "vakaros_session_id": None,
                "track": None,
                "line_positions": [],
                "race_events": [],
                "line": None,
            }
        )
    return JSONResponse({"matched": True, **overlay})


@router.get("/api/sessions/{session_id}/detail")
async def api_session_detail(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return full metadata for a single session."""
    storage = get_storage(request)
    db = storage._conn()
    cur = await db.execute(
        "SELECT r.id, r.name, r.event, r.race_num, r.date,"
        " r.start_utc, r.end_utc, r.session_type,"
        " r.peer_fingerprint, r.peer_co_op_id,"
        " r.shared_name, r.match_group_id, r.match_confirmed,"
        " (SELECT COUNT(*) > 0 FROM positions p"
        "   WHERE p.ts >= r.start_utc AND p.ts <= COALESCE(r.end_utc, r.start_utc)"
        " ) AS has_track,"
        " (SELECT rv.youtube_url FROM race_videos rv"
        "   WHERE rv.race_id = r.id LIMIT 1) AS first_video_url"
        " FROM races r WHERE r.id = ?",
        (session_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")

    start_utc = datetime.fromisoformat(row["start_utc"])
    end_utc = datetime.fromisoformat(row["end_utc"]) if row["end_utc"] else None
    duration_s = (end_utc - start_utc).total_seconds() if end_utc else None

    # Check for audio
    acur = await db.execute(
        "SELECT id, start_utc FROM audio_sessions"
        " WHERE race_id = ? AND session_type IN ('race','practice')",
        (session_id,),
    )
    arow = await acur.fetchone()

    # Check for wind field params (synthesized sessions)
    wf_cur = await db.execute(
        "SELECT 1 FROM synth_wind_params WHERE session_id = ?",
        (session_id,),
    )
    has_wind_field = await wf_cur.fetchone() is not None

    return JSONResponse(
        {
            "id": row["id"],
            "type": row["session_type"],
            "name": row["name"],
            "event": row["event"],
            "race_num": row["race_num"],
            "date": row["date"],
            "start_utc": start_utc.isoformat(),
            "end_utc": end_utc.isoformat() if end_utc else None,
            "duration_s": round(duration_s, 1) if duration_s is not None else None,
            "has_track": bool(row["has_track"]),
            "first_video_url": row["first_video_url"],
            "has_audio": arow is not None,
            "audio_session_id": arow["id"] if arow else None,
            "audio_start_utc": (
                datetime.fromisoformat(arow["start_utc"]).isoformat() if arow else None
            ),
            "peer_fingerprint": row["peer_fingerprint"],
            "has_wind_field": has_wind_field,
            "shared_name": row["shared_name"],
            "match_group_id": row["match_group_id"],
            "match_status": "confirmed"
            if row["match_confirmed"]
            else ("candidate" if row["match_group_id"] else "unmatched"),
        }
    )


@router.get("/api/sessions/{session_id}/wind-field")
async def api_session_wind_field(
    request: Request,
    session_id: int,
    elapsed_s: float = 0.0,
    grid_size: int = 20,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return a spatial grid of TWD/TWS values and course marks."""
    storage = get_storage(request)
    from helmlog.wind_field import WindField

    grid_size = min(max(grid_size, 5), 40)
    params = await storage.get_synth_wind_params(session_id)
    if params is None:
        raise HTTPException(status_code=404, detail="No wind field for this session")

    marks = await storage.get_synth_course_marks(session_id)
    elapsed_s = max(0.0, min(elapsed_s, params["duration_s"]))

    # Compute bounding box from marks + 0.5 nm padding on all sides
    import math

    pad_nm = 0.5
    if marks:
        mark_lats = [m["lat"] for m in marks]
        mark_lons = [m["lon"] for m in marks]
        center_lat = (min(mark_lats) + max(mark_lats)) / 2
        cos_ref = math.cos(math.radians(center_lat))
        lat_min = min(mark_lats) - pad_nm / 60.0
        lat_max = max(mark_lats) + pad_nm / 60.0
        lon_min = min(mark_lons) - pad_nm / 60.0 / cos_ref
        lon_max = max(mark_lons) + pad_nm / 60.0 / cos_ref
    else:
        cos_ref = math.cos(math.radians(params["ref_lat"]))
        lat_min = params["ref_lat"] - pad_nm / 60.0
        lat_max = params["ref_lat"] + pad_nm / 60.0
        lon_min = params["ref_lon"] - pad_nm / 60.0 / cos_ref
        lon_max = params["ref_lon"] + pad_nm / 60.0 / cos_ref

    # Capture bounds for the thread
    bounds = (lat_min, lat_max, lon_min, lon_max)

    def _compute() -> list[dict[str, float]]:
        wf = WindField(
            base_twd=params["base_twd"],
            tws_low=params["tws_low"],
            tws_high=params["tws_high"],
            duration_s=params["duration_s"],
            shift_interval=(params["shift_interval_lo"], params["shift_interval_hi"]),
            shift_magnitude=(params["shift_magnitude_lo"], params["shift_magnitude_hi"]),
            ref_lat=params["ref_lat"],
            ref_lon=params["ref_lon"],
            seed=params["seed"],
        )
        b_lat_min, b_lat_max, b_lon_min, b_lon_max = bounds

        cells: list[dict[str, float]] = []
        for r in range(grid_size):
            lat = b_lat_min + (b_lat_max - b_lat_min) * r / (grid_size - 1)
            for c in range(grid_size):
                lon = b_lon_min + (b_lon_max - b_lon_min) * c / (grid_size - 1)
                twd, tws = wf.at(elapsed_s, lat, lon)
                cells.append(
                    {
                        "lat": round(lat, 6),
                        "lon": round(lon, 6),
                        "twd": round(twd, 1),
                        "tws": round(tws, 2),
                    }
                )
        return cells

    cells = await asyncio.to_thread(_compute)

    return JSONResponse(
        {
            "elapsed_s": elapsed_s,
            "duration_s": params["duration_s"],
            "base_twd": params["base_twd"],
            "tws_low": params["tws_low"],
            "tws_high": params["tws_high"],
            "grid": {
                "rows": grid_size,
                "cols": grid_size,
                "lat_min": round(lat_min, 6),
                "lat_max": round(lat_max, 6),
                "lon_min": round(lon_min, 6),
                "lon_max": round(lon_max, 6),
                "cells": cells,
            },
            "marks": marks,
        }
    )


@router.get("/api/sessions/{session_id}/wind-timeseries")
async def api_session_wind_timeseries(
    request: Request,
    session_id: int,
    step_s: int = 10,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return TWD/TWS time series at port, center, and starboard positions."""
    storage = get_storage(request)
    from helmlog.wind_field import WindField

    step_s = min(max(step_s, 5), 60)
    params = await storage.get_synth_wind_params(session_id)
    if params is None:
        raise HTTPException(status_code=404, detail="No wind field for this session")

    def _compute() -> dict[str, Any]:
        import math

        wf = WindField(
            base_twd=params["base_twd"],
            tws_low=params["tws_low"],
            tws_high=params["tws_high"],
            duration_s=params["duration_s"],
            shift_interval=(params["shift_interval_lo"], params["shift_interval_hi"]),
            shift_magnitude=(params["shift_magnitude_lo"], params["shift_magnitude_hi"]),
            ref_lat=params["ref_lat"],
            ref_lon=params["ref_lon"],
            seed=params["seed"],
        )
        cos_ref = math.cos(math.radians(params["ref_lat"]))
        offset_lon = 0.3 / 60.0 / cos_ref  # 0.3 nm cross-course

        positions = [
            {
                "label": "Port side",
                "lat": params["ref_lat"],
                "lon": round(params["ref_lon"] - offset_lon, 6),
            },
            {"label": "Center", "lat": params["ref_lat"], "lon": params["ref_lon"]},
            {
                "label": "Starboard side",
                "lat": params["ref_lat"],
                "lon": round(params["ref_lon"] + offset_lon, 6),
            },
        ]

        series: list[dict[str, Any]] = []
        t = 0.0
        while t <= params["duration_s"]:
            twd_vals = []
            tws_vals = []
            for p in positions:
                twd, tws = wf.at(t, p["lat"], p["lon"])
                twd_vals.append(round(twd, 1))
                tws_vals.append(round(tws, 2))
            series.append({"t": round(t, 1), "twd": twd_vals, "tws": tws_vals})
            t += step_s

        return {"positions": positions, "series": series}

    result = await asyncio.to_thread(_compute)

    return JSONResponse(
        {
            "duration_s": params["duration_s"],
            "step_s": step_s,
            "base_twd": params["base_twd"],
            "positions": result["positions"],
            "series": result["series"],
        }
    )


@router.get("/api/sessions/{session_id}/polar")
async def api_session_polar(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    import helmlog.polar as _polar

    data = await _polar.session_polar_comparison(storage, session_id)
    if data is None:
        return JSONResponse(
            {"cells": [], "tws_bins": [], "twa_bins": [], "session_sample_count": 0}
        )
    return JSONResponse(
        {
            "cells": [
                {
                    "tws": c.tws_bin,
                    "twa": c.twa_bin,
                    "baseline_mean": c.baseline_mean_bsp,
                    "baseline_p90": c.baseline_p90_bsp,
                    "session_mean": c.session_mean_bsp,
                    "samples": c.session_sample_count,
                    "delta": c.delta,
                }
                for c in data.cells
            ],
            "tws_bins": data.tws_bins,
            "twa_bins": data.twa_bins,
            "session_sample_count": data.session_sample_count,
        }
    )


@router.get("/api/sessions/{session_id}/maneuvers")
async def api_session_maneuvers(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return detected maneuvers for a session with full per-maneuver metrics.

    Each entry carries entry/exit state (BSP, TWA, TWS, HDG), min BSP during
    the maneuver, turn angle and rate, distance-loss against an entry-vector
    reference, nearest GPS position, a quartile rank (good/avg/bad), and a
    ``youtube_url`` deep-link to the linked session video at the maneuver
    timestamp when a video is linked.
    """
    storage = get_storage(request)
    from helmlog.analysis.maneuvers import enrich_session_maneuvers

    enriched, _video_sync = await enrich_session_maneuvers(storage, session_id)
    return JSONResponse(enriched)


_MANEUVER_CSV_COLUMNS = [
    "ts",
    "type",
    "rank",
    "duration_sec",
    "entry_hdg",
    "exit_hdg",
    "turn_angle_deg",
    "turn_rate_deg_s",
    "entry_bsp",
    "exit_bsp",
    "min_bsp",
    "loss_kts",
    "distance_loss_m",
    "entry_twa",
    "exit_twa",
    "entry_tws",
    "exit_tws",
    "time_to_recover_s",
    "lat",
    "lon",
    "youtube_url",
]


@router.get("/api/sessions/{session_id}/maneuvers.csv")
async def api_session_maneuvers_csv(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> Response:
    """CSV export of the enriched maneuver list for a session."""
    from helmlog.analysis.maneuvers import enrich_session_maneuvers

    storage = get_storage(request)
    enriched, _ = await enrich_session_maneuvers(storage, session_id)

    import csv
    import io

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_MANEUVER_CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for m in enriched:
        writer.writerow(
            {k: m.get(k, "") if m.get(k) is not None else "" for k in _MANEUVER_CSV_COLUMNS}
        )

    filename = f"session_{session_id}_maneuvers.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/api/sessions/{session_id}/detect-maneuvers", status_code=202)
async def api_detect_maneuvers(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Trigger maneuver detection (or re-detection) for a session.

    Returns immediately with the count of detected maneuvers.
    """
    storage = get_storage(request)
    from helmlog.maneuver_detector import detect_maneuvers

    # Verify session exists
    db = storage._conn()
    cur = await db.execute("SELECT id FROM races WHERE id = ?", (session_id,))
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail="Session not found")

    maneuvers = await detect_maneuvers(storage, session_id)
    return JSONResponse(
        {
            "session_id": session_id,
            "detected": len(maneuvers),
            "tacks": sum(1 for m in maneuvers if m.type == "tack"),
            "gybes": sum(1 for m in maneuvers if m.type == "gybe"),
            "roundings": sum(1 for m in maneuvers if m.type == "rounding"),
        },
        status_code=202,
    )


@router.get("/api/sessions")
async def api_sessions(
    request: Request,
    q: str | None = None,
    type: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 25,
    offset: int = 0,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    if type is not None and type not in ("race", "practice", "debrief", "synthesized"):
        raise HTTPException(
            status_code=422,
            detail="type must be 'race', 'practice', 'debrief', or 'synthesized'",
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


@router.get("/api/grafana/annotations")
async def api_grafana_annotations(
    request: Request,
    from_: int | None = Query(default=None, alias="from"),
    to: int | None = None,
    sessionId: int | None = None,  # noqa: N803
) -> JSONResponse:
    """Grafana SimpleJSON annotation feed.

    Grafana passes epoch milliseconds as ``from`` and ``to``.
    Optional ``sessionId`` scopes results to a single race or practice.
    """
    storage = get_storage(request)
    if from_ is None or to is None:
        return JSONResponse([])
    start = datetime.fromtimestamp(from_ / 1000.0, tz=UTC)
    end = datetime.fromtimestamp(to / 1000.0, tz=UTC)
    race_id: int | None = None
    audio_session_id: int | None = None
    if sessionId is not None:
        race_id, audio_session_id = await _resolve_session(request, sessionId)
    notes = await storage.list_notes_range(
        start, end, race_id=race_id, audio_session_id=audio_session_id
    )
    result = []
    for n in notes:
        ts_ms = int(datetime.fromisoformat(n["ts"]).timestamp() * 1000)
        text = n["body"] or ""
        if n["note_type"] == "photo" and n.get("photo_path"):
            photo_url = f"/notes/{n['photo_path']}"
            text = f'<img src="{photo_url}" style="max-width:300px"/>'
            if n["body"]:
                text = n["body"] + "<br/>" + text
        result.append(
            {
                "time": ts_ms,
                "timeEnd": ts_ms,
                "title": n["note_type"].capitalize(),
                "text": text,
                "tags": [n["note_type"]],
            }
        )
    return JSONResponse(result, headers={"Access-Control-Allow-Origin": "*"})


# ------------------------------------------------------------------
# /api/boat-settings
# ------------------------------------------------------------------


@router.delete("/api/sessions/{session_id}", status_code=204)
async def api_delete_session(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> None:
    """Delete a session and all related data (admin only)."""
    storage = get_storage(request)
    cur = await storage._conn().execute(
        "SELECT name, end_utc FROM races WHERE id = ?", (session_id,)
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if row["end_utc"] is None:
        raise HTTPException(status_code=409, detail="Cannot delete an active session")
    files = await storage.delete_race_session(session_id)
    # Clean up physical files
    for f in files:
        p = Path(f)
        if p.exists():
            await asyncio.to_thread(p.unlink)
            logger.info("Deleted file: {}", p)
    await audit(request, "session.delete", detail=row["name"], user=_user)


@router.patch("/api/sessions/{session_id}")
async def api_rename_session(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Rename a race session and regenerate its slug (#449).

    Body: ``{"name": str?, "event": str?, "race_num": int?}`` — at least one
    field is required. When *name* is omitted but *event* or *race_num*
    changes, the name is regenerated from the standard ``build_race_name``
    template. Admin only.
    """
    storage = get_storage(request)
    body = await request.json()
    raw_name = body.get("name")
    raw_event = body.get("event")
    raw_race_num = body.get("race_num")
    if raw_name is None and raw_event is None and raw_race_num is None:
        raise HTTPException(status_code=422, detail="name, event, or race_num required")

    new_name = str(raw_name) if raw_name is not None else None
    new_event = str(raw_event).strip() if raw_event is not None else None
    if new_event == "":
        raise HTTPException(status_code=422, detail="event must not be blank")
    try:
        new_race_num = int(raw_race_num) if raw_race_num is not None else None
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="race_num must be an integer") from exc

    try:
        updated, retired_slug = await storage.rename_race(
            session_id,
            new_name=new_name,
            new_event=new_event,
            new_race_num=new_race_num,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc
    except ValueError as exc:
        if str(exc) == "name_taken":
            raise HTTPException(status_code=409, detail={"error": "name_taken"}) from exc
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    changed = retired_slug is not None or updated.renamed_at is not None
    if changed:
        detail = f"{session_id}: {retired_slug!r} → {updated.slug!r} ({updated.name!r})"
        await audit(request, "race.rename", detail=detail, user=_user)
    return JSONResponse(
        {
            "id": updated.id,
            "name": updated.name,
            "event": updated.event,
            "race_num": updated.race_num,
            "slug": updated.slug,
            "renamed_at": updated.renamed_at.isoformat() if updated.renamed_at else None,
            "retired_slug": retired_slug,
            "url": (
                f"/session/{updated.id}/{updated.slug}"
                if updated.slug
                else f"/session/{updated.id}"
            ),
        }
    )
