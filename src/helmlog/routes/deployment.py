"""Route handlers for deployment."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from helmlog.auth import require_auth
from helmlog.routes._helpers import STARTUP_SHA, audit, get_storage, limiter

router = APIRouter()


@router.get("/api/deployment/status")
@limiter.limit("30/minute")
async def api_deployment_status(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    from helmlog.deploy import DeployConfig, commits_behind, fetch_latest, get_running_version

    config = await DeployConfig.from_storage(storage)
    running = get_running_version()
    await fetch_latest(config)  # update origin refs before comparing
    behind = commits_behind(config)
    last = await storage.last_deployment()
    # Detect if on-disk code differs from what the running process loaded
    restart_needed = bool(STARTUP_SHA and running["sha"] and running["sha"] != STARTUP_SHA)
    # Detect if checked-out branch differs from tracked branch
    branch_mismatch = bool(running["branch"] and running["branch"] != config.branch)
    return JSONResponse(
        {
            "running": {**running, "startup_sha": STARTUP_SHA},
            "branch": config.branch,
            "mode": config.mode,
            "poll_interval": config.poll_interval,
            "deploy_window": {
                "start": config.window_start,
                "end": config.window_end,
            },
            "commits_behind": behind,
            "update_available": behind > 0 or restart_needed or branch_mismatch,
            "restart_needed": restart_needed,
            "branch_mismatch": branch_mismatch,
            "last_deploy": last,
        }
    )


@router.get("/api/deployment/changelog")
@limiter.limit("10/minute")
async def api_deployment_changelog(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    from helmlog.deploy import DeployConfig, get_changelog

    config = await DeployConfig.from_storage(storage)
    commits = await get_changelog(config)
    return JSONResponse({"commits": commits, "count": len(commits)})


@router.post("/api/deployment/deploy")
@limiter.limit("3/minute")
async def api_deployment_deploy(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    from helmlog.deploy import DeployConfig, execute_deploy

    config = await DeployConfig.from_storage(storage)
    result = await execute_deploy(config)
    await storage.log_deployment(
        from_sha=result.get("from_sha", ""),
        to_sha=result.get("to_sha", ""),
        trigger="manual",
        status=result["status"],
        error=result.get("error"),
        user_id=_user.get("id"),
    )
    await audit(
        request,
        "deployment.manual",
        detail=f"{result.get('from_sha', '')[:7]}→{result.get('to_sha', '')[:7]}",
        user=_user,
    )
    if result["status"] == "failed":
        raise HTTPException(status_code=500, detail=result.get("error", "Deploy failed"))
    return JSONResponse(result)


@router.get("/api/deployment/branches")
@limiter.limit("10/minute")
async def api_deployment_branches(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    get_storage(request)
    from helmlog.deploy import list_remote_branches

    branches = await list_remote_branches()
    return JSONResponse({"branches": branches})


@router.put("/api/deployment/config")
@limiter.limit("10/minute")
async def api_deployment_config(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    body = await request.json()
    changed: list[str] = []
    if "mode" in body:
        mode = body["mode"]
        if mode not in ("explicit", "evergreen"):
            raise HTTPException(status_code=400, detail="Mode must be 'explicit' or 'evergreen'")
        await storage.set_setting("DEPLOY_MODE", mode)
        changed.append(f"mode={mode}")
    if "branch" in body:
        branch = str(body["branch"]).strip()
        if not branch:
            raise HTTPException(status_code=400, detail="Branch cannot be empty")
        await storage.set_setting("DEPLOY_BRANCH", branch)
        changed.append(f"branch={branch}")
    if "poll_interval" in body:
        poll = int(body["poll_interval"])
        if poll < 60:
            raise HTTPException(status_code=400, detail="Poll interval must be >= 60 seconds")
        await storage.set_setting("DEPLOY_POLL_INTERVAL", str(poll))
        changed.append(f"poll_interval={poll}")
    if "window_start" in body:
        val = body["window_start"]
        if val is None or val == "":
            await storage.delete_setting("DEPLOY_WINDOW_START")
        else:
            await storage.set_setting("DEPLOY_WINDOW_START", str(int(val)))
        changed.append(f"window_start={val}")
    if "window_end" in body:
        val = body["window_end"]
        if val is None or val == "":
            await storage.delete_setting("DEPLOY_WINDOW_END")
        else:
            await storage.set_setting("DEPLOY_WINDOW_END", str(int(val)))
        changed.append(f"window_end={val}")
    await audit(request, "deployment.config", detail=", ".join(changed), user=_user)
    return JSONResponse({"status": "ok", "changed": changed})


@router.get("/api/deployment/history")
@limiter.limit("30/minute")
async def api_deployment_history(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    deployments = await storage.list_deployments()
    return JSONResponse({"deployments": deployments})


@router.get("/api/deployment/pipeline")
@limiter.limit("10/minute")
async def api_deployment_pipeline(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    get_storage(request)
    from helmlog.deploy import get_pipeline_status

    pipeline = await get_pipeline_status()
    return JSONResponse(pipeline)


@router.get("/api/deployment/promotions")
@limiter.limit("10/minute")
async def api_deployment_promotions(
    request: Request,
    tier: str | None = None,
    limit: int = 20,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    get_storage(request)
    from helmlog.deploy import get_promotion_history

    promotions = await get_promotion_history(tier=tier, limit=limit)
    return JSONResponse({"promotions": promotions})


@router.get("/api/deployment/pending")
@limiter.limit("10/minute")
async def api_deployment_pending(
    request: Request,
    from_tier: str = "stage",
    to_tier: str = "main",
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    get_storage(request)
    from helmlog.deploy import get_pending_changes

    valid_tiers = {"main", "stage", "live"}
    if from_tier not in valid_tiers or to_tier not in valid_tiers:
        raise HTTPException(status_code=400, detail="Invalid tier — must be main, stage, or live")
    commits = await get_pending_changes(from_tier=from_tier, to_tier=to_tier)
    return JSONResponse({"commits": commits, "count": len(commits)})
