"""Route handlers for boat_settings."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from helmlog.auth import require_auth
from helmlog.boat_settings import (
    CATEGORY_ORDER,
    PARAMETER_NAMES,
    WEIGHT_DISTRIBUTION_PRESETS,
    parameters_by_category,
)
from helmlog.routes._helpers import audit, get_storage
from helmlog.tuning_extraction import (
    accept_item as _te_accept,
)
from helmlog.tuning_extraction import (
    compare_runs as _te_compare,
)
from helmlog.tuning_extraction import (
    create_extraction_run as _te_create_run,
)
from helmlog.tuning_extraction import (
    delete_run as _te_delete_run,
)
from helmlog.tuning_extraction import (
    dismiss_item as _te_dismiss,
)
from helmlog.tuning_extraction import (
    get_run_with_items as _te_get_run,
)
from helmlog.tuning_extraction import (
    run_extraction as _te_run,
)

router = APIRouter()


@router.get("/api/boat-settings/parameters")
async def api_boat_settings_parameters(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return the canonical parameter definitions grouped by category."""
    get_storage(request)
    grouped = parameters_by_category()
    result = []
    for cat, label in CATEGORY_ORDER:
        params = [
            {"name": p.name, "label": p.label, "unit": p.unit, "input_type": p.input_type}
            for p in grouped[cat]
        ]
        result.append({"category": cat, "label": label, "parameters": params})
    return JSONResponse(
        {"categories": result, "weight_distribution_presets": list(WEIGHT_DISTRIBUTION_PRESETS)}
    )


@router.post("/api/boat-settings", status_code=201)
async def api_create_boat_settings(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Create one or more boat setting entries.

    Body: ``{"race_id": int|null, "source": str, "entries": [{"ts": str, "parameter": str, "value": str}, ...]}``
    """
    storage = get_storage(request)
    body = await request.json()
    race_id: int | None = body.get("race_id")
    source: str = body.get("source", "manual")
    extraction_run_id: int | None = body.get("extraction_run_id")
    entries: list[dict[str, str]] = body.get("entries", [])
    if not entries:
        raise HTTPException(status_code=400, detail="entries is required and must be non-empty")
    for e in entries:
        if not all(k in e for k in ("ts", "parameter", "value")):
            raise HTTPException(
                status_code=400, detail="Each entry must have ts, parameter, and value"
            )
        if e["parameter"] not in PARAMETER_NAMES:
            raise HTTPException(status_code=400, detail=f"Unknown parameter: {e['parameter']!r}")
    try:
        ids = await storage.create_boat_settings(race_id, entries, source, extraction_run_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await audit(request, "boat_settings.create", detail=f"{len(ids)} entries", user=_user)
    return JSONResponse({"ids": ids}, status_code=201)


@router.get("/api/boat-settings")
async def api_list_boat_settings(
    request: Request,
    race_id: int | None = Query(None),
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """List all boat settings for a race, ordered by timestamp."""
    storage = get_storage(request)
    rows = await storage.list_boat_settings(race_id)
    return JSONResponse(rows)


@router.get("/api/boat-settings/current")
async def api_current_boat_settings(
    request: Request,
    race_id: int | None = Query(None),
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return the latest value for each parameter in a race."""
    storage = get_storage(request)
    rows = await storage.current_boat_settings(race_id)
    return JSONResponse(rows)


@router.get("/api/boat-settings/resolve")
async def api_resolve_boat_settings(
    request: Request,
    race_id: int = Query(...),
    as_of: str = Query(...),
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Resolve boat settings at a specific timestamp for a race.

    Merges race-specific settings over boat-level defaults.  Each entry
    includes ``supersedes_value`` / ``supersedes_source`` when a race-level
    value overrides a boat-level default.
    """
    storage = get_storage(request)
    rows = await storage.resolve_boat_settings(race_id, as_of)
    return JSONResponse(rows)


@router.delete("/api/boat-settings/extraction-run/{extraction_run_id}", status_code=200)
async def api_delete_boat_settings_extraction_run(
    request: Request,
    extraction_run_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Delete all settings from a specific extraction run."""
    storage = get_storage(request)
    count = await storage.delete_boat_settings_extraction_run(extraction_run_id)
    await audit(
        request,
        "boat_settings.delete_run",
        detail=f"run={extraction_run_id} deleted={count}",
        user=_user,
    )
    return JSONResponse({"deleted": count})


# ------------------------------------------------------------------
# /api/tuning — transcript extraction (#276)
# ------------------------------------------------------------------


@router.post("/api/tuning/extract/{transcript_id}", status_code=201)
async def api_tuning_extract(
    request: Request,
    transcript_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Create and run a tuning extraction on a transcript."""
    storage = get_storage(request)
    body = await request.json()
    method: str = body.get("method", "regex")
    run_id = await _te_create_run(storage, transcript_id, method)
    items = await _te_run(storage, run_id)
    await audit(request, "tuning.extract", detail=f"run={run_id} items={len(items)}", user=_user)
    run = await _te_get_run(storage, run_id)
    return JSONResponse(
        {"run_id": run_id, "status": run.status if run else "error", "item_count": len(items)},
        status_code=201,
    )


@router.get("/api/tuning/runs")
async def api_tuning_runs(
    request: Request,
    transcript_id: int | None = Query(None),
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """List extraction runs, optionally filtered by transcript_id."""
    storage = get_storage(request)
    db = storage._conn()
    if transcript_id is not None:
        cur = await db.execute(
            "SELECT id, transcript_id, method, created_at, status, item_count, accepted_count"
            " FROM extraction_runs WHERE transcript_id = ? ORDER BY created_at DESC",
            (transcript_id,),
        )
    else:
        cur = await db.execute(
            "SELECT id, transcript_id, method, created_at, status, item_count, accepted_count"
            " FROM extraction_runs ORDER BY created_at DESC"
        )
    rows = await cur.fetchall()
    return JSONResponse([dict(r) for r in rows])


@router.get("/api/tuning/runs/{run_id}")
async def api_tuning_run_detail(
    request: Request,
    run_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Get an extraction run with all its items."""
    storage = get_storage(request)
    from helmlog.tuning_extraction import _item_to_dict

    run = await _te_get_run(storage, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return JSONResponse(
        {
            "id": run.id,
            "transcript_id": run.transcript_id,
            "method": run.method,
            "created_at": run.created_at,
            "status": run.status,
            "item_count": run.item_count,
            "accepted_count": run.accepted_count,
            "items": [_item_to_dict(i) for i in run.items],
        }
    )


@router.post("/api/tuning/items/{item_id}/accept")
async def api_tuning_accept_item(
    request: Request,
    item_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Accept an extraction item — adds to boat settings timeline."""
    storage = get_storage(request)
    user_id: int = _user.get("id", 0)
    await _te_accept(storage, item_id, user_id)
    await audit(request, "tuning.accept", detail=f"item={item_id}", user=_user)
    return JSONResponse({"status": "accepted"})


@router.post("/api/tuning/items/{item_id}/dismiss")
async def api_tuning_dismiss_item(
    request: Request,
    item_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Dismiss an extraction item — excluded from timeline."""
    storage = get_storage(request)
    user_id: int = _user.get("id", 0)
    await _te_dismiss(storage, item_id, user_id)
    await audit(request, "tuning.dismiss", detail=f"item={item_id}", user=_user)
    return JSONResponse({"status": "dismissed"})


@router.delete("/api/tuning/runs/{run_id}")
async def api_tuning_delete_run(
    request: Request,
    run_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Delete an extraction run and its items."""
    storage = get_storage(request)
    await _te_delete_run(storage, run_id)
    await audit(request, "tuning.delete_run", detail=f"run={run_id}", user=_user)
    return JSONResponse({"status": "deleted"})


@router.get("/api/tuning/compare")
async def api_tuning_compare(
    request: Request,
    run1: int = Query(...),
    run2: int | None = Query(None),
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Compare one or two extraction runs side by side."""
    storage = get_storage(request)
    result = await _te_compare(storage, run1, run2)
    return JSONResponse(result)
