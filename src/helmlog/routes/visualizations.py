"""Route handlers for visualizations."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from helmlog.auth import require_auth
from helmlog.routes._helpers import audit, get_storage

router = APIRouter()


@router.get("/api/visualizations/catalog")
async def api_viz_catalog(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """List available visualization plugins."""
    get_storage(request)
    from helmlog.visualization.discovery import discover_viz_plugins  # noqa: PLC0415

    plugins = discover_viz_plugins()
    result = []
    for _name, plugin in plugins.items():
        meta = plugin.meta()
        result.append(
            {
                "name": meta.name,
                "display_name": meta.display_name,
                "description": meta.description,
                "version": meta.version,
                "required_analysis": meta.required_analysis,
            }
        )
    return JSONResponse(result)


@router.post("/api/visualizations/render/{session_id}")
async def api_viz_render(
    request: Request,
    session_id: int,
    viz: str | None = None,
    model: str | None = None,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Run a visualization plugin on a session, return Plotly JSON."""
    storage = get_storage(request)
    from helmlog.analysis.cache import AnalysisCache, _compute_data_hash  # noqa: PLC0415
    from helmlog.analysis.discovery import discover_plugins, load_session_data  # noqa: PLC0415
    from helmlog.analysis.preferences import resolve_preference  # noqa: PLC0415
    from helmlog.analysis.protocol import AnalysisContext  # noqa: PLC0415
    from helmlog.visualization.discovery import discover_viz_plugins  # noqa: PLC0415
    from helmlog.visualization.protocol import VizContext  # noqa: PLC0415

    if not viz:
        raise HTTPException(422, "viz query parameter is required")

    viz_plugins = discover_viz_plugins()
    viz_plugin = viz_plugins.get(viz)
    if viz_plugin is None:
        raise HTTPException(404, f"Visualization plugin {viz!r} not found")

    # Check co-op data status
    db = storage._conn()
    race_cur = await db.execute(
        "SELECT source, peer_fingerprint FROM races WHERE id = ?", (session_id,)
    )
    race_row = await race_cur.fetchone()
    if race_row is None:
        raise HTTPException(404, "Session not found")
    is_co_op = bool(race_row["peer_fingerprint"])

    # Load session data
    session_data = await load_session_data(storage, session_id)
    if session_data is None:
        raise HTTPException(404, "Session not found or not completed")

    # Run required analysis if needed
    analysis_result: dict[str, Any] = {}
    required = viz_plugin.meta().required_analysis
    if required:
        analysis_plugin_name = model
        if not analysis_plugin_name:
            analysis_plugin_name = await resolve_preference(storage, user["id"])
        if not analysis_plugin_name and required:
            analysis_plugin_name = required[0]

        a_plugins = discover_plugins()
        a_plugin = a_plugins.get(analysis_plugin_name or "")
        if a_plugin is not None:
            cache = AnalysisCache(storage)
            data_hash = _compute_data_hash(
                {
                    "speeds": len(session_data.speeds),
                    "winds": len(session_data.winds),
                    "session_id": session_id,
                }
            )
            cached = await cache.get(session_id, analysis_plugin_name or "", data_hash=data_hash)
            if cached is not None:
                analysis_result = cached
            else:
                ctx = AnalysisContext(user_id=user["id"], is_co_op_data=is_co_op)
                result = await a_plugin.analyze(session_data, ctx)
                analysis_result = result.to_dict(include_raw=not is_co_op)
                await cache.put(
                    session_id,
                    analysis_plugin_name or "",
                    result.plugin_version,
                    data_hash,
                    result.to_dict(include_raw=True),
                )

    # Build session data dict for the viz plugin
    sd_dict: dict[str, Any] = {
        "session_id": session_data.session_id,
        "start_utc": session_data.start_utc,
        "end_utc": session_data.end_utc,
        "speeds": session_data.speeds,
        "winds": session_data.winds,
        "headings": session_data.headings,
        "positions": session_data.positions,
    }

    viz_ctx = VizContext(user_id=user["id"], is_co_op_data=is_co_op)
    plotly_spec = await viz_plugin.render(sd_dict, analysis_result, viz_ctx)

    # Audit co-op data access
    if is_co_op:
        await audit(
            request,
            "visualization.render_coop",
            detail=f"session={session_id} viz={viz}",
            user=user,
        )
    else:
        await audit(
            request,
            "visualization.render",
            detail=f"session={session_id} viz={viz}",
            user=user,
        )

    return JSONResponse(plotly_spec)


@router.get("/api/visualizations/preferences")
async def api_viz_preferences(
    request: Request,
    user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Return the resolved visualization preference for the current user."""
    storage = get_storage(request)
    from helmlog.visualization.preferences import resolve_viz_preference  # noqa: PLC0415

    plugins = await resolve_viz_preference(storage, user["id"])
    return JSONResponse({"plugin_names": plugins})


@router.put("/api/visualizations/preferences")
async def api_set_viz_preference(
    request: Request,
    user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Set visualization preference at a scope."""
    storage = get_storage(request)
    from helmlog.visualization.preferences import set_viz_preference  # noqa: PLC0415

    body = await request.json()
    scope: str = body.get("scope", "user")
    scope_id: str | None = body.get("scope_id")
    plugin_names: list[str] = body.get("plugin_names", [])
    if not plugin_names:
        raise HTTPException(422, "plugin_names is required")

    # Only admin can set platform/co_op/boat scope
    if scope != "user" and user.get("role") != "admin":
        raise HTTPException(403, "Only admin can set non-user preferences")

    if scope == "user":
        scope_id = str(user["id"])

    await set_viz_preference(storage, scope, scope_id, plugin_names)
    await audit(
        request,
        "visualization.preference",
        detail=f"scope={scope} plugins={plugin_names}",
        user=user,
    )
    return JSONResponse({"ok": True})


@router.get("/api/visualizations/shared")
async def api_viz_shared(
    request: Request,
    viz: str | None = None,
    model: str | None = None,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Resolve shared link parameters (viz + model query params).

    Returns the resolved plugin info or a fallback message if the
    requested visualization plugin is not available.
    """
    get_storage(request)
    from helmlog.visualization.discovery import discover_viz_plugins  # noqa: PLC0415

    if not viz:
        return JSONResponse({"error": "viz parameter required", "fallback": True}, status_code=422)

    plugins = discover_viz_plugins()
    plugin = plugins.get(viz)
    if plugin is None:
        available = [
            {"name": m.name, "display_name": m.display_name}
            for m in (p.meta() for p in plugins.values())
        ]
        return JSONResponse(
            {
                "error": f"Visualization {viz!r} not available",
                "fallback": True,
                "available": available,
                "requested_viz": viz,
                "requested_model": model,
            }
        )

    meta = plugin.meta()
    return JSONResponse(
        {
            "fallback": False,
            "viz": {
                "name": meta.name,
                "display_name": meta.display_name,
                "description": meta.description,
                "version": meta.version,
                "required_analysis": meta.required_analysis,
            },
            "model": model,
        }
    )
