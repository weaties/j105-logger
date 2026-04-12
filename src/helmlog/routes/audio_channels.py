"""Admin UI for the multi-channel audio default channel→position map (#496).

Built on the storage primitives from #493 / #494: ``list_channel_map_devices``
returns the device list, ``set_channel_map`` writes the default mapping, and
``log_voice_consent_ack`` records the per-position biometric consent
acknowledgement that the data licensing policy requires before audio PII can
be attributed to a named individual.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from helmlog.auth import require_auth
from helmlog.routes._helpers import audit, get_storage, templates, tpl_ctx
from helmlog.usb_audio import detect_multi_channel_device

router = APIRouter()


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


@router.get("/admin/audio-channels", response_class=HTMLResponse, include_in_schema=False)
async def admin_audio_channels_page(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> Response:
    get_storage(request)
    return templates.TemplateResponse(
        request,
        "admin/audio_channels.html",
        tpl_ctx(request, "/admin/audio-channels"),
    )


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------


@router.get("/api/audio-channels/devices")
async def api_list_audio_channel_devices(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Return known + currently-attached USB multi-channel devices.

    The list is the union of:
      * devices that already have a saved admin default in the v63 channel_map
      * a freshly-detected device, if any (via ``usb_audio.detect_*``).

    The freshly-detected device is included even if it has no saved mapping
    yet so the admin can configure it on first connection. On darwin the
    detection path returns zero/empty identity which is fine for the dev UI.
    """
    storage = get_storage(request)
    saved = await storage.list_channel_map_devices()

    detected = detect_multi_channel_device(min_channels=2)
    if detected is not None:
        identity = detected.identity()
        if not any(
            (d["vendor_id"], d["product_id"], d["serial"], d["usb_port_path"]) == identity
            for d in saved
        ):
            saved.append(
                {
                    "vendor_id": detected.vendor_id,
                    "product_id": detected.product_id,
                    "serial": detected.serial,
                    "usb_port_path": detected.usb_port_path,
                    "mapping": {},
                    "last_updated_utc": None,
                    "name": detected.name,
                    "max_channels": detected.max_channels,
                    "attached": True,
                }
            )

    return JSONResponse(saved)


@router.post("/api/audio-channels/save", status_code=204)
async def api_save_audio_channel_map(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> Response:
    """Persist a default channel→position map for a USB device.

    Body schema (JSON)::

        {
          "vendor_id":     0x1234,
          "product_id":    0x5678,
          "serial":        "ABC123",
          "usb_port_path": "1-1.2",
          "mapping": {"0": "helm", "1": "tactician", ...},
          "consent_acks": ["helm", "tactician", ...]
        }

    Every position in ``mapping`` MUST also appear in ``consent_acks`` —
    voice biometric consent has to be acknowledged before any per-channel
    name attribution can be saved (data licensing policy). On success the
    mapping is written and one structured ``voice_consent_ack`` audit entry
    is recorded per position.
    """
    body = await request.json()
    await _persist_channel_map(request, _user, body, audio_session_id=None)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Per-session override (#497 / pt.5)
# ---------------------------------------------------------------------------


@router.get("/api/audio-channels/sessions/{audio_session_id}")
async def api_get_session_channel_map(
    request: Request,
    audio_session_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> JSONResponse:
    """Return the active channel→position map for an audio session.

    Chains override → admin default. Empty dict if neither is set or if
    the session has no recorded device identity yet.
    """
    storage = get_storage(request)
    row = await storage.get_audio_session_row(audio_session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="audio session not found")
    mapping = await storage.get_channel_map_for_audio_session(audio_session_id)
    return JSONResponse(
        {
            "audio_session_id": audio_session_id,
            "vendor_id": row.get("vendor_id"),
            "product_id": row.get("product_id"),
            "serial": row.get("serial"),
            "usb_port_path": row.get("usb_port_path"),
            "mapping": mapping,
        }
    )


@router.post("/api/audio-channels/sessions/{audio_session_id}/override", status_code=204)
async def api_set_session_override(
    request: Request,
    audio_session_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> Response:
    """Persist a per-session override of the admin default channel map.

    Body schema is identical to ``/api/audio-channels/save`` but the
    ``audio_session_id`` is taken from the URL — the rows are written into
    the per-session scope of the v63 channel_map table so the admin default
    is left untouched. Subsequent sessions on the same device revert to the
    default automatically.
    """
    storage = get_storage(request)
    if await storage.get_audio_session_row(audio_session_id) is None:
        raise HTTPException(status_code=404, detail="audio session not found")
    body = await request.json()
    await _persist_channel_map(request, _user, body, audio_session_id=audio_session_id)
    return Response(status_code=204)


@router.delete("/api/audio-channels/sessions/{audio_session_id}/override", status_code=204)
async def api_clear_session_override(
    request: Request,
    audio_session_id: int,
    _user: dict[str, Any] = Depends(require_auth("crew")),  # noqa: B008
) -> Response:
    """Clear the per-session override; reverts to the admin default."""
    storage = get_storage(request)
    row = await storage.get_audio_session_row(audio_session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="audio session not found")
    vendor_id = row.get("vendor_id")
    product_id = row.get("product_id")
    if vendor_id is None or product_id is None:
        # No identity → nothing to clear; treat as no-op success
        return Response(status_code=204)
    await storage.set_channel_map(
        vendor_id=int(vendor_id),
        product_id=int(product_id),
        serial=row.get("serial") or "",
        usb_port_path=row.get("usb_port_path") or "",
        mapping={},
        audio_session_id=audio_session_id,
    )
    await audit(
        request,
        "audio_channel_session_override_cleared",
        detail=f"audio_session_id={audio_session_id}",
        user=_user if isinstance(_user, dict) else None,
    )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Shared persistence helper
# ---------------------------------------------------------------------------


async def _persist_channel_map(
    request: Request,
    user: dict[str, Any] | None,
    body: dict[str, Any],
    *,
    audio_session_id: int | None,
) -> None:
    """Validate, consent-gate, and persist a channel map at admin or session scope."""
    storage = get_storage(request)
    try:
        vendor_id = int(body["vendor_id"])
        product_id = int(body["product_id"])
        serial = str(body.get("serial", ""))
        usb_port_path = str(body["usb_port_path"])
        mapping_raw: dict[str, str] = body["mapping"] or {}
        consent_acks: list[str] = list(body.get("consent_acks", []))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid payload: {exc}") from exc

    if not mapping_raw:
        raise HTTPException(status_code=400, detail="mapping is empty")

    try:
        mapping = {int(k): str(v) for k, v in mapping_raw.items()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"non-integer channel index: {exc}") from exc

    missing = sorted({pos for pos in mapping.values() if pos not in consent_acks})
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"missing consent acknowledgement for: {', '.join(missing)}",
        )

    user_id = user.get("id") if isinstance(user, dict) else None
    await storage.set_channel_map(
        vendor_id=vendor_id,
        product_id=product_id,
        serial=serial,
        usb_port_path=usb_port_path,
        mapping=mapping,
        audio_session_id=audio_session_id,
        created_by=user_id if isinstance(user_id, int) else None,
    )

    device_identity = {
        "vendor_id": vendor_id,
        "product_id": product_id,
        "serial": serial,
        "usb_port_path": usb_port_path,
    }
    for position in sorted(set(mapping.values())):
        await storage.log_voice_consent_ack(
            user_id=user_id if isinstance(user_id, int) else None,
            position_name=position,
            device=device_identity,
        )

    scope = "default" if audio_session_id is None else f"session={audio_session_id}"
    await audit(
        request,
        "audio_channel_map_saved",
        detail=(
            f"scope={scope} device={vendor_id:04x}:{product_id:04x} "
            f"serial={serial} port={usb_port_path} "
            f"positions={sorted(set(mapping.values()))}"
        ),
        user=user if isinstance(user, dict) else None,
    )
