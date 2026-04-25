"""Race-start simulator (#690).

Offline validation harness for #644: time skew, synthetic boat state, and
scenario presets. Mounted only when ``RACE_START_SIMULATOR=true`` so it
never appears in production.

The simulator exercises the *real* race-start routes — we don't fake the
FSM, the flag resolver, or the geometry. We only fake (a) the wall clock
that the FSM ticks against, and (b) the boat-state inserts (positions,
cogsog, winds) that the line-metrics endpoint reads from. Every path
through the real code stays in the test loop.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from helmlog.auth import require_auth
from helmlog.routes._helpers import audit, get_storage, templates, tpl_ctx

router = APIRouter()


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------


def is_simulator_enabled() -> bool:
    """Whether the simulator is enabled for this process."""
    return os.environ.get("RACE_START_SIMULATOR", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _require_sim() -> None:
    if not is_simulator_enabled():
        raise HTTPException(
            status_code=404,
            detail="race-start simulator not enabled (set RACE_START_SIMULATOR=true)",
        )


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


@router.get(
    "/race-start/simulate",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def sim_page(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> Response:
    _require_sim()
    return templates.TemplateResponse(
        request,
        "race_start_sim.html",
        tpl_ctx(request, "/race-start/simulate"),
    )


# ---------------------------------------------------------------------------
# Virtual clock
# ---------------------------------------------------------------------------


class ClockRequest(BaseModel):
    offset_s: float = Field(
        ...,
        description=(
            "Seconds to add to wall-clock UTC. Negative jumps the FSM into the "
            "future relative to the user; positive into the past. Set to 0 to "
            "return to real time."
        ),
    )


@router.get("/api/race-start/sim/clock")
async def sim_get_clock(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    _require_sim()
    offset = float(getattr(request.app.state, "race_start_sim_offset_s", 0.0))
    return JSONResponse(
        {
            "offset_s": offset,
            "real_now_utc": datetime.now(UTC).isoformat(),
            "virtual_now_utc": (
                datetime.now(UTC).timestamp() + offset
            ),  # epoch seconds; client formats
        }
    )


@router.post("/api/race-start/sim/clock")
async def sim_set_clock(
    request: Request,
    body: ClockRequest,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    _require_sim()
    request.app.state.race_start_sim_offset_s = float(body.offset_s)
    await audit(request, "race_start_sim.clock", detail=str(body.offset_s), user=user)
    return JSONResponse({"offset_s": body.offset_s})


# ---------------------------------------------------------------------------
# Synthetic boat state — writes to positions / cogsog / winds tables
# ---------------------------------------------------------------------------


class BoatStateRequest(BaseModel):
    latitude_deg: float | None = None
    longitude_deg: float | None = None
    sog_kn: float | None = None
    cog_deg: float | None = None
    twd_deg: float | None = None
    tws_kn: float | None = None


async def _write_synthetic_state(request: Request, body: BoatStateRequest) -> dict[str, Any]:
    """Insert a synthetic boat-state row into positions / cogsog / winds.

    Reuses the same SQL the real readers use so any downstream consumer
    (line-metrics, latest_position, etc.) sees identical rows.
    """
    storage = get_storage(request)
    # Use the simulator's virtual-now so the rows fall in the simulated
    # timeline (relevant for time-windowed queries).
    real = datetime.now(UTC)
    offset = float(getattr(request.app.state, "race_start_sim_offset_s", 0.0))
    ts = real.isoformat() if offset == 0.0 else (real.timestamp() + offset)
    if isinstance(ts, float):
        ts = datetime.fromtimestamp(ts, tz=UTC).isoformat()

    db = storage._conn()  # noqa: SLF001 — direct write is the simulator's job
    written: dict[str, Any] = {}
    if body.latitude_deg is not None and body.longitude_deg is not None:
        await db.execute(
            "INSERT INTO positions (ts, source_addr, latitude_deg, longitude_deg)"
            " VALUES (?, ?, ?, ?)",
            (ts, 0, body.latitude_deg, body.longitude_deg),
        )
        written["position"] = (body.latitude_deg, body.longitude_deg)
    if body.sog_kn is not None and body.cog_deg is not None:
        await db.execute(
            "INSERT INTO cogsog (ts, source_addr, cog_deg, sog_kts) VALUES (?, ?, ?, ?)",
            (ts, 0, body.cog_deg, body.sog_kn),
        )
        written["cogsog"] = (body.cog_deg, body.sog_kn)
    if body.twd_deg is not None:
        # Reference 0 = ground/true wind in helmlog's wind schema.
        await db.execute(
            "INSERT INTO winds (ts, source_addr, wind_speed_kts, wind_angle_deg, reference)"
            " VALUES (?, ?, ?, ?, ?)",
            (ts, 0, body.tws_kn or 0.0, body.twd_deg, 0),
        )
        written["wind"] = (body.twd_deg, body.tws_kn)
    await db.commit()
    return written


@router.post("/api/race-start/sim/boat")
async def sim_set_boat(
    request: Request,
    body: BoatStateRequest,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    _require_sim()
    written = await _write_synthetic_state(request, body)
    await audit(request, "race_start_sim.boat", detail=str(written), user=user)
    return JSONResponse({"written": list(written.keys())})


# ---------------------------------------------------------------------------
# Scenario presets
# ---------------------------------------------------------------------------


# Each preset is a list of (offset_s, BoatStateRequest | None, label) — the
# user clicks "step" to advance, or "play" to fire them all in sequence on
# a real-time interval.
SCENARIOS: dict[str, list[dict[str, Any]]] = {
    "boat-favoured-square-line": [
        {
            "offset_s": -300,
            "label": "Warning signal (5 min to start). Square line, 10 kn south wind.",
            "boat": {
                "latitude_deg": 47.6500,
                "longitude_deg": -122.4000,
                "sog_kn": 5.0,
                "cog_deg": 0.0,
                "twd_deg": 180.0,
                "tws_kn": 10.0,
            },
        },
        {"offset_s": -60, "label": "1-min signal", "boat": None},
        {"offset_s": 0, "label": "Start gun (square line, neutral bias)", "boat": None},
    ],
    "pin-favoured-3-2-1-0": [
        {
            "offset_s": -180,
            "label": "Warning signal for 3-2-1-0 sequence. East wind, line east-west.",
            "boat": {
                "latitude_deg": 47.6500,
                "longitude_deg": -122.4000,
                "sog_kn": 5.0,
                "cog_deg": 90.0,
                "twd_deg": 90.0,  # wind from east → pin end favoured
                "tws_kn": 12.0,
            },
        },
        {"offset_s": -60, "label": "1-min signal", "boat": None},
        {"offset_s": 0, "label": "Start gun (pin favoured)", "boat": None},
    ],
    "general-recall-at-minus-30": [
        {
            "offset_s": -60,
            "label": "1-min signal in counting_down phase",
            "boat": {
                "latitude_deg": 47.65,
                "longitude_deg": -122.40,
                "sog_kn": 5.0,
                "cog_deg": 0.0,
                "twd_deg": 180.0,
                "tws_kn": 10.0,
            },
        },
        {"offset_s": -30, "label": "30s — RC raises First Sub for general recall", "boat": None},
    ],
    "ap-then-resume": [
        {
            "offset_s": -120,
            "label": "Counting down → AP postpones",
            "boat": {
                "latitude_deg": 47.65,
                "longitude_deg": -122.40,
                "sog_kn": 5.0,
                "cog_deg": 0.0,
                "twd_deg": 180.0,
                "tws_kn": 10.0,
            },
        },
    ],
    "ocs-at-plus-2": [
        {
            "offset_s": 2,
            "label": "FSM has fired the gun. Boat is across the line — flag X expected.",
            "boat": {
                "latitude_deg": 47.6505,  # north of the line
                "longitude_deg": -122.4000,
                "sog_kn": 6.0,
                "cog_deg": 0.0,
                "twd_deg": 180.0,
                "tws_kn": 10.0,
            },
        },
    ],
    "multi-class-J70-then-PHRF": [
        {
            "offset_s": -360,
            "label": "PHRF-A start in 60s; J/70 (ours) in 360s",
            "boat": {
                "latitude_deg": 47.65,
                "longitude_deg": -122.40,
                "sog_kn": 5.0,
                "cog_deg": 0.0,
                "twd_deg": 180.0,
                "tws_kn": 10.0,
            },
        },
        {"offset_s": -300, "label": "Our class flag goes up", "boat": None},
        {"offset_s": -240, "label": "Our prep flag (I) goes up", "boat": None},
        {"offset_s": -60, "label": "1-min signal", "boat": None},
        {"offset_s": 0, "label": "Our gun", "boat": None},
    ],
}


@router.get("/api/race-start/sim/scenarios")
async def sim_list_scenarios(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    _require_sim()
    return JSONResponse(
        {"scenarios": [{"name": name, "steps": len(steps)} for name, steps in SCENARIOS.items()]}
    )


class StepRequest(BaseModel):
    scenario: str
    step_index: int = Field(0, ge=0)


@router.post("/api/race-start/sim/step")
async def sim_step(
    request: Request,
    body: StepRequest,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Apply one step of a scenario.

    Sets the virtual clock offset and writes any synthetic boat state.
    Does NOT arm or mutate the FSM — callers do that via the real
    race-start endpoints (the simulator UI typically arms first, then
    steps through the timeline)."""
    _require_sim()
    if body.scenario not in SCENARIOS:
        raise HTTPException(status_code=404, detail=f"unknown scenario: {body.scenario!r}")
    steps = SCENARIOS[body.scenario]
    if body.step_index >= len(steps):
        raise HTTPException(status_code=400, detail="step_index out of range")
    step = steps[body.step_index]
    request.app.state.race_start_sim_offset_s = float(step["offset_s"])
    written: dict[str, Any] = {}
    if step.get("boat"):
        written = await _write_synthetic_state(request, BoatStateRequest(**step["boat"]))
    await audit(
        request,
        "race_start_sim.step",
        detail=f"{body.scenario}#{body.step_index}",
        user=user,
    )
    return JSONResponse(
        {
            "scenario": body.scenario,
            "step_index": body.step_index,
            "label": step.get("label", ""),
            "offset_s": step["offset_s"],
            "boat_written": list(written.keys()),
            "is_last": body.step_index == len(steps) - 1,
        }
    )


@router.post("/api/race-start/sim/reset")
async def sim_reset(
    request: Request,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Reset clock offset to 0 and clear race-start state.

    Does NOT delete synthetic positions/cogsog/winds rows — those live
    on as part of the test history and are useful for inspecting what
    the simulator wrote.
    """
    _require_sim()
    request.app.state.race_start_sim_offset_s = 0.0
    storage = get_storage(request)
    await storage.clear_race_start_state()
    await audit(request, "race_start_sim.reset", user=user)
    return JSONResponse({"offset_s": 0.0, "fsm_cleared": True})
