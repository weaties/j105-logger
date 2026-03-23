"""Route handlers for sails."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from helmlog.auth import require_auth
from helmlog.routes._helpers import RaceSailsSet, SailCreate, SailUpdate, audit, get_storage
from helmlog.storage import _SAIL_TYPES

_POINT_OF_SAIL_VALUES = ("upwind", "downwind", "both")

router = APIRouter()


@router.get("/api/sails")
async def api_list_sails(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return active sails grouped by type."""
    storage = get_storage(request)
    all_sails = await storage.list_sails(include_inactive=False)
    grouped: dict[str, list[dict[str, Any]]] = {t: [] for t in _SAIL_TYPES}
    for s in all_sails:
        if s["type"] in grouped:
            grouped[s["type"]].append(s)
    return JSONResponse(grouped)


@router.post("/api/sails", status_code=201)
async def api_add_sail(
    request: Request,
    body: SailCreate,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Add a sail to the inventory."""
    storage = get_storage(request)
    if body.type not in _SAIL_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown sail type {body.type!r}. Must be one of {list(_SAIL_TYPES)}",
        )
    if not body.name.strip():
        raise HTTPException(status_code=422, detail="name must not be blank")
    if body.point_of_sail is not None and body.point_of_sail not in _POINT_OF_SAIL_VALUES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid point_of_sail {body.point_of_sail!r}. Must be one of {list(_POINT_OF_SAIL_VALUES)}",
        )
    try:
        sail_id = await storage.add_sail(
            body.type, body.name, body.notes, point_of_sail=body.point_of_sail
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"Sail already exists: type={body.type!r} name={body.name!r}",
        ) from exc
    await audit(request, "sail.add", detail=f"{body.type}/{body.name}", user=_user)
    return JSONResponse(
        {"id": sail_id, "type": body.type, "name": body.name.strip()}, status_code=201
    )


@router.get("/api/sails/defaults")
async def api_get_sail_defaults(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return the boat-level default sail selection."""
    storage = get_storage(request)
    defaults = await storage.get_sail_defaults()
    return JSONResponse(defaults)


@router.put("/api/sails/defaults", status_code=200)
async def api_set_sail_defaults(
    request: Request,
    body: RaceSailsSet,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Set the boat-level default sail selection."""
    storage = get_storage(request)
    # Validate that each supplied sail_id references a sail of the correct type
    slot_map = {"main": body.main_id, "jib": body.jib_id, "spinnaker": body.spinnaker_id}
    for slot_type, sail_id in slot_map.items():
        if sail_id is None:
            continue
        all_sails = await storage.list_sails(include_inactive=True)
        matched = next((s for s in all_sails if s["id"] == sail_id), None)
        if matched is None:
            raise HTTPException(status_code=422, detail=f"Sail id={sail_id} not found")
        if matched["type"] != slot_type:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Sail id={sail_id} has type {matched['type']!r},"
                    f" expected {slot_type!r} for the {slot_type} slot"
                ),
            )

    await storage.set_sail_defaults(
        main_id=body.main_id,
        jib_id=body.jib_id,
        spinnaker_id=body.spinnaker_id,
    )
    defaults = await storage.get_sail_defaults()
    await audit(request, "sails.defaults.set", user=_user)
    return JSONResponse(defaults)


@router.get("/api/sails/stats")
async def api_sail_stats(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return all sails with accumulated tack/gybe counts and session totals."""
    storage = get_storage(request)
    stats = await storage.get_sail_stats()
    return JSONResponse(stats)


@router.get("/api/sails/{sail_id}/sessions")
async def api_sail_sessions(
    request: Request,
    sail_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return session history for a specific sail."""
    storage = get_storage(request)
    history = await storage.get_sail_session_history(sail_id)
    return JSONResponse(history)


@router.patch("/api/sails/{sail_id}", status_code=200)
async def api_update_sail(
    request: Request,
    sail_id: int,
    body: SailUpdate,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Update sail name/notes, point-of-sail, or retire it."""
    storage = get_storage(request)
    if body.point_of_sail is not None and body.point_of_sail not in _POINT_OF_SAIL_VALUES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid point_of_sail {body.point_of_sail!r}. Must be one of {list(_POINT_OF_SAIL_VALUES)}",
        )
    found = await storage.update_sail(
        sail_id,
        name=body.name,
        notes=body.notes,
        active=body.active,
        point_of_sail=body.point_of_sail,
    )
    if not found:
        raise HTTPException(status_code=404, detail="Sail not found")
    await audit(request, "sail.update", detail=str(sail_id), user=_user)
    return JSONResponse({"id": sail_id, "updated": True})


@router.get("/api/sessions/{session_id}/sails")
async def api_get_session_sails(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return the sail selection for a race/practice session."""
    storage = get_storage(request)
    race = await storage.get_race(session_id)
    if race is None:
        raise HTTPException(status_code=404, detail="Session not found")
    sails = await storage.get_race_sails(session_id)
    return JSONResponse(sails)


@router.put("/api/sessions/{session_id}/sails", status_code=200)
async def api_set_session_sails(
    request: Request,
    session_id: int,
    body: RaceSailsSet,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Set the sail selection for a race/practice session."""
    storage = get_storage(request)
    race = await storage.get_race(session_id)
    if race is None:
        raise HTTPException(status_code=404, detail="Session not found")

    # Validate that each supplied sail_id references a sail of the correct type
    slot_map = {"main": body.main_id, "jib": body.jib_id, "spinnaker": body.spinnaker_id}
    for slot_type, sail_id in slot_map.items():
        if sail_id is None:
            continue
        all_sails = await storage.list_sails(include_inactive=True)
        matched = next((s for s in all_sails if s["id"] == sail_id), None)
        if matched is None:
            raise HTTPException(status_code=422, detail=f"Sail id={sail_id} not found")
        if matched["type"] != slot_type:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Sail id={sail_id} has type {matched['type']!r},"
                    f" expected {slot_type!r} for the {slot_type} slot"
                ),
            )

    ts = datetime.now(UTC).isoformat()
    await storage.insert_sail_change(
        session_id,
        ts,
        main_id=body.main_id,
        jib_id=body.jib_id,
        spinnaker_id=body.spinnaker_id,
    )
    sails = await storage.get_race_sails(session_id)
    await audit(request, "sails.set", detail=str(session_id), user=_user)
    return JSONResponse(sails)


@router.get("/api/sessions/{session_id}/sail-changes")
async def api_get_sail_changes(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return the full sail change history for a session."""
    storage = get_storage(request)
    race = await storage.get_race(session_id)
    if race is None:
        raise HTTPException(status_code=404, detail="Session not found")
    changes = await storage.get_sail_change_history(session_id)
    return JSONResponse({"changes": changes})


@router.get("/api/sails/performance")
async def api_sails_performance(
    request: Request,
    sail_type: str | None = None,
    sail_id: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Aggregate VMG across all sessions by sail."""
    storage = get_storage(request)
    from collections import defaultdict  # noqa: PLC0415

    from helmlog.analysis.plugins.sail_vmg import (  # noqa: PLC0415, E501
        compute_downwind_vmg,
        compute_upwind_vmg,
        wind_band_for,
        wind_band_label,
    )
    from helmlog.polar import _compute_twa  # noqa: PLC0415

    ranges = await storage.get_sail_active_ranges(
        sail_id=sail_id,
        sail_type=sail_type,
        start_date=start_date,
        end_date=end_date,
    )

    # Group ranges by session to batch-load instrument data
    sessions_sails: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in ranges:
        sessions_sails[r["session_id"]].append(r)

    # sail_id → wind_band → direction → [vmg values]
    SailStats = dict[str, dict[str, list[float]]]
    sail_vmgs: dict[int, SailStats] = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    sail_info: dict[int, dict[str, str]] = {}

    from datetime import UTC, datetime  # noqa: PLC0415

    for sid, sail_ranges in sessions_sails.items():
        db = storage._conn()
        cur = await db.execute("SELECT start_utc, end_utc FROM races WHERE id = ?", (sid,))
        row = await cur.fetchone()
        if not row or not row["end_utc"]:
            continue

        try:
            start = datetime.fromisoformat(str(row["start_utc"])).replace(tzinfo=UTC)
            end = datetime.fromisoformat(str(row["end_utc"])).replace(tzinfo=UTC)
        except ValueError:
            continue

        speeds = await storage.query_range("speeds", start, end, race_id=sid)
        winds = await storage.query_range("winds", start, end, race_id=sid)
        headings = await storage.query_range("headings", start, end, race_id=sid)

        spd_by_s: dict[str, dict[str, Any]] = {}
        for s in speeds:
            spd_by_s.setdefault(str(s["ts"])[:19], s)
        hdg_by_s: dict[str, dict[str, Any]] = {}
        for h in headings:
            hdg_by_s.setdefault(str(h["ts"])[:19], h)
        tw_by_s: dict[str, dict[str, Any]] = {}
        for w in winds:
            ref = int(w.get("reference", -1))
            if ref not in (0, 4):
                continue
            tw_by_s.setdefault(str(w["ts"])[:19], w)

        for sr in sail_ranges:
            s_id = sr["sail_id"]
            sail_info[s_id] = {"name": sr["sail_name"], "type": sr["sail_type"]}

            for sk, spd_row in spd_by_s.items():
                wind_row = tw_by_s.get(sk)
                if wind_row is None:
                    continue
                bsp = float(spd_row["speed_kts"])
                if bsp <= 0:
                    continue
                tws = float(wind_row["wind_speed_kts"])
                ref = int(wind_row.get("reference", -1))
                wa = float(wind_row["wind_angle_deg"])
                hdg_row = hdg_by_s.get(sk)
                heading = float(hdg_row["heading_deg"]) if hdg_row else None
                twa = _compute_twa(wa, ref, heading)
                if twa is None:
                    continue

                band = wind_band_for(tws)
                if band is None:
                    continue
                bl = wind_band_label(band[0], band[1])

                if twa < 90:
                    vmg = compute_upwind_vmg(bsp, twa)
                    sail_vmgs[s_id][bl]["upwind"].append(vmg)
                else:
                    vmg = compute_downwind_vmg(bsp, twa)
                    sail_vmgs[s_id][bl]["downwind"].append(vmg)

    # Build response
    sails_out: list[dict[str, Any]] = []
    for s_id, bands in sail_vmgs.items():
        info = sail_info.get(s_id, {"name": "", "type": ""})
        wind_bands_out: dict[str, Any] = {}
        for bl_label, dirs in bands.items():
            wb: dict[str, Any] = {}
            for direction in ("upwind", "downwind"):
                vals = dirs.get(direction, [])
                if vals:
                    n = len(vals)
                    sorted_v = sorted(vals)
                    wb[f"{direction}_vmg"] = {
                        "mean": round(sum(vals) / n, 4),
                        "median": round(sorted_v[n // 2], 4),
                        "n": n,
                    }
                else:
                    wb[f"{direction}_vmg"] = {"mean": 0, "median": 0, "n": 0}
            wind_bands_out[bl_label] = wb
        sails_out.append(
            {
                "sail_id": s_id,
                "sail_name": info["name"],
                "sail_type": info["type"],
                "wind_bands": wind_bands_out,
            }
        )

    return JSONResponse({"sails": sails_out})
