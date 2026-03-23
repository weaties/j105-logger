"""Route handlers for analysis."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from helmlog.auth import require_auth
from helmlog.routes._helpers import audit, get_storage

router = APIRouter()


@router.get("/api/analysis/models")
async def api_analysis_models(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """List available analysis plugins."""
    get_storage(request)
    from helmlog.analysis.discovery import discover_plugins  # noqa: PLC0415

    plugins = discover_plugins()
    result = []
    for _name, plugin in plugins.items():
        meta = plugin.meta()
        result.append(
            {
                "name": meta.name,
                "display_name": meta.display_name,
                "description": meta.description,
                "version": meta.version,
            }
        )
    return JSONResponse(result)


@router.post("/api/analysis/run/{session_id}")
async def api_analysis_run(
    request: Request,
    session_id: int,
    model: str | None = None,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Run an analysis plugin on a session."""
    storage = get_storage(request)
    from helmlog.analysis.cache import AnalysisCache, _compute_data_hash  # noqa: PLC0415
    from helmlog.analysis.discovery import discover_plugins, load_session_data  # noqa: PLC0415
    from helmlog.analysis.preferences import resolve_preference  # noqa: PLC0415
    from helmlog.analysis.protocol import AnalysisContext  # noqa: PLC0415

    # Determine which plugin to use
    plugin_name = model
    if not plugin_name:
        plugin_name = await resolve_preference(storage, user["id"])
    if not plugin_name:
        # Default to first available plugin
        plugins = discover_plugins()
        if not plugins:
            raise HTTPException(404, "No analysis plugins available")
        plugin_name = next(iter(plugins))

    plugins = discover_plugins()
    plugin = plugins.get(plugin_name)
    if plugin is None:
        raise HTTPException(404, f"Plugin {plugin_name!r} not found")

    session_data = await load_session_data(storage, session_id)
    if session_data is None:
        raise HTTPException(404, "Session not found or not completed")

    # Check co-op data status
    db = storage._conn()
    race_cur = await db.execute(
        "SELECT source, peer_fingerprint FROM races WHERE id = ?", (session_id,)
    )
    race_row = await race_cur.fetchone()
    is_co_op = bool(race_row and race_row["peer_fingerprint"])

    ctx = AnalysisContext(
        user_id=user["id"],
        is_co_op_data=is_co_op,
    )

    # Check cache
    cache = AnalysisCache(storage)
    data_hash = _compute_data_hash(
        {
            "speeds": len(session_data.speeds),
            "winds": len(session_data.winds),
            "session_id": session_id,
        }
    )
    cached = await cache.get(session_id, plugin_name, data_hash=data_hash)
    if cached is not None:
        if is_co_op:
            cached.pop("raw", None)
        return JSONResponse(cached)

    result = await plugin.analyze(session_data, ctx)
    result_dict = result.to_dict(include_raw=True)

    await cache.put(session_id, plugin_name, result.plugin_version, data_hash, result_dict)

    if is_co_op:
        await audit(
            request,
            "analysis.run_coop",
            detail=f"session={session_id} plugin={plugin_name}",
            user=user,
        )
        result_dict.pop("raw", None)
    else:
        await audit(
            request,
            "analysis.run",
            detail=f"session={session_id} plugin={plugin_name}",
            user=user,
        )

    return JSONResponse(result_dict)


@router.get("/api/analysis/results/{session_id}")
async def api_analysis_results(
    request: Request,
    session_id: int,
    model: str | None = None,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return cached analysis result for a session."""
    storage = get_storage(request)
    from helmlog.analysis.discovery import discover_plugins  # noqa: PLC0415
    from helmlog.analysis.preferences import resolve_preference  # noqa: PLC0415

    plugin_name = model
    if not plugin_name:
        plugin_name = await resolve_preference(storage, user["id"])
    if not plugin_name:
        plugins = discover_plugins()
        if not plugins:
            raise HTTPException(404, "No analysis plugins available")
        plugin_name = next(iter(plugins))

    cached = await storage.get_analysis_cache(session_id, plugin_name)
    if cached is None:
        raise HTTPException(404, "No cached result")

    import json as _json  # noqa: PLC0415

    result = _json.loads(cached["result_json"])

    # Strip raw from co-op data
    db = storage._conn()
    race_cur = await db.execute("SELECT peer_fingerprint FROM races WHERE id = ?", (session_id,))
    race_row = await race_cur.fetchone()
    if race_row and race_row["peer_fingerprint"]:
        result.pop("raw", None)

    return JSONResponse(result)


@router.get("/api/analysis/preferences")
async def api_analysis_preferences(
    request: Request,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return the resolved analysis preference for the current user."""
    storage = get_storage(request)
    from helmlog.analysis.preferences import resolve_preference  # noqa: PLC0415

    model = await resolve_preference(storage, user["id"])
    return JSONResponse({"model_name": model})


@router.put("/api/analysis/preferences")
async def api_set_analysis_preference(
    request: Request,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Set analysis preference at a scope."""
    storage = get_storage(request)
    from helmlog.analysis.preferences import set_preference  # noqa: PLC0415

    body = await request.json()
    scope: str = body.get("scope", "user")
    scope_id: str | None = body.get("scope_id")
    model_name: str = body.get("model_name", "")
    if not model_name:
        raise HTTPException(422, "model_name is required")

    # Only admin can set platform/co_op/boat scope
    if scope != "user" and user.get("role") != "admin":
        raise HTTPException(403, "Only admin can set non-user preferences")

    if scope == "user":
        scope_id = str(user["id"])

    await set_preference(storage, scope, scope_id, model_name)
    await audit(
        request, "analysis.preference", detail=f"scope={scope} model={model_name}", user=user
    )
    return JSONResponse({"ok": True})
