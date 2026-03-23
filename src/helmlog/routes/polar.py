"""Route handlers for polar."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from helmlog.auth import require_auth
from helmlog.routes._helpers import audit, get_storage

router = APIRouter()


@router.get("/api/polar/current")
async def api_polar_current(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    import helmlog.polar as _polar

    inst = await storage.latest_instruments()
    tws = inst.get("tws_kts")
    twa = inst.get("twa_deg")
    bsp = inst.get("bsp_kts")
    point = None
    if tws is not None and twa is not None:
        point = await _polar.lookup_polar(storage, float(tws), float(twa))
    baseline_bsp = point["mean_bsp"] if point else None
    delta = (
        round(float(bsp) - float(baseline_bsp), 2)
        if (bsp is not None and baseline_bsp is not None)
        else None
    )
    return JSONResponse(
        {
            "bsp": bsp,
            "tws": tws,
            "twa": twa,
            "baseline_bsp": baseline_bsp,
            "baseline_p90": point["p90_bsp"] if point else None,
            "delta": delta,
            "sufficient_data": point is not None,
        }
    )


@router.post("/api/polar/rebuild", status_code=200)
async def api_polar_rebuild(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    import helmlog.polar as _polar

    count = await _polar.build_polar_baseline(storage)
    await audit(request, "polar.rebuild", detail=f"{count} bins", user=_user)
    return JSONResponse({"bins": count})
