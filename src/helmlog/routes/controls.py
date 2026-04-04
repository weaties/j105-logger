"""Route handlers for unified boat controls."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from helmlog.auth import require_auth
from helmlog.boat_settings import CATEGORY_ORDER
from helmlog.routes._helpers import audit, get_storage

router = APIRouter()


# ---------------------------------------------------------------------------
# List (grouped by category)
# ---------------------------------------------------------------------------


@router.get("/api/controls")
async def api_list_controls(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth()),  # noqa: B008
) -> JSONResponse:
    """Return all controls grouped by category, with ArUco config and trigger words."""
    storage = get_storage(request)
    controls = await storage.list_controls()

    # Group by category in display order
    cat_order = [cat for cat, _label in CATEGORY_ORDER]
    cat_labels = dict(CATEGORY_ORDER)
    grouped: dict[str, list[dict[str, Any]]] = {cat: [] for cat in cat_order}

    for ctrl in controls:
        cat = ctrl["category"]
        if cat not in grouped:
            grouped[cat] = []
        grouped[cat].append(ctrl)

    categories = []
    for cat in cat_order:
        if grouped.get(cat):
            categories.append(
                {
                    "category": cat,
                    "label": cat_labels.get(cat, cat.replace("_", " ").title()),
                    "controls": grouped[cat],
                }
            )
    # Add any categories not in CATEGORY_ORDER
    for cat, ctrls in grouped.items():
        if cat not in cat_order and ctrls:
            categories.append(
                {
                    "category": cat,
                    "label": cat.replace("_", " ").title(),
                    "controls": ctrls,
                }
            )

    return JSONResponse({"categories": categories})


# ---------------------------------------------------------------------------
# Control CRUD
# ---------------------------------------------------------------------------


@router.post("/api/controls")
async def api_add_control(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Create a new control."""
    storage = get_storage(request)
    body = await request.json()
    name = body.get("name", "").strip()
    label = body.get("label", "").strip()
    if not name or not label:
        raise HTTPException(400, "name and label are required")
    control_id = await storage.add_control(
        name=name,
        label=label,
        unit=body.get("unit", ""),
        input_type=body.get("input_type", "number"),
        category=body.get("category", "sail_controls"),
        sort_order=body.get("sort_order", 0),
        preset_values=body.get("preset_values"),
    )
    await audit(request, "control_add", f"name={name}", _user)
    return JSONResponse({"id": control_id}, status_code=201)


@router.put("/api/controls/{control_id}")
async def api_update_control(
    request: Request,
    control_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Update a control."""
    storage = get_storage(request)
    body = await request.json()
    ok = await storage.update_control(
        control_id,
        name=body.get("name"),
        label=body.get("label"),
        unit=body.get("unit"),
        input_type=body.get("input_type"),
        category=body.get("category"),
        sort_order=body.get("sort_order"),
    )
    if not ok:
        raise HTTPException(404, "Control not found")
    await audit(request, "control_update", f"id={control_id}", _user)
    return JSONResponse({"ok": True})


@router.delete("/api/controls/{control_id}")
async def api_delete_control(
    request: Request,
    control_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Delete a control (cascades to ArUco config, trigger words)."""
    storage = get_storage(request)
    ok = await storage.delete_control(control_id)
    if not ok:
        raise HTTPException(404, "Control not found")
    await audit(request, "control_delete", f"id={control_id}", _user)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# ArUco marker config
# ---------------------------------------------------------------------------


@router.put("/api/controls/{control_id}/aruco")
async def api_set_aruco(
    request: Request,
    control_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Attach or update ArUco marker pair for a control."""
    storage = get_storage(request)
    body = await request.json()
    camera_id = body.get("camera_id")
    marker_id_a = body.get("marker_id_a")
    marker_id_b = body.get("marker_id_b")
    if camera_id is None or marker_id_a is None or marker_id_b is None:
        raise HTTPException(400, "camera_id, marker_id_a, and marker_id_b are required")
    await storage.set_control_aruco(
        control_id,
        int(camera_id),
        int(marker_id_a),
        int(marker_id_b),
        tolerance_mm=body.get("tolerance_mm"),
    )
    await audit(request, "control_aruco_set", f"control={control_id}", _user)
    return JSONResponse({"ok": True})


@router.delete("/api/controls/{control_id}/aruco")
async def api_delete_aruco(
    request: Request,
    control_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Remove ArUco marker pair from a control."""
    storage = get_storage(request)
    ok = await storage.delete_control_aruco(control_id)
    if not ok:
        raise HTTPException(404, "No ArUco config for this control")
    await audit(request, "control_aruco_delete", f"control={control_id}", _user)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Trigger words
# ---------------------------------------------------------------------------


@router.post("/api/controls/{control_id}/trigger-words")
async def api_add_trigger_word(
    request: Request,
    control_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Add a trigger word to a control."""
    storage = get_storage(request)
    body = await request.json()
    phrase = body.get("phrase", "").strip().lower()
    if not phrase:
        raise HTTPException(400, "phrase is required")
    tw_id = await storage.add_control_trigger_word(control_id, phrase)
    await audit(request, "control_trigger_add", f"phrase={phrase}", _user)
    return JSONResponse({"id": tw_id}, status_code=201)


@router.delete("/api/controls/trigger-words/{trigger_id}")
async def api_delete_trigger_word(
    request: Request,
    trigger_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Delete a trigger word."""
    storage = get_storage(request)
    ok = await storage.delete_control_trigger_word(trigger_id)
    if not ok:
        raise HTTPException(404, "Trigger word not found")
    await audit(request, "control_trigger_delete", f"id={trigger_id}", _user)
    return JSONResponse({"ok": True})
