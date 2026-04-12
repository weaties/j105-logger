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
    storage = get_storage(request)
    body = await request.json()

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

    # Coerce keys to int (JSON object keys are always strings)
    try:
        mapping = {int(k): str(v) for k, v in mapping_raw.items()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"non-integer channel index: {exc}") from exc

    # Enforce per-position voice biometric consent acknowledgement
    missing = sorted({pos for pos in mapping.values() if pos not in consent_acks})
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"missing consent acknowledgement for: {', '.join(missing)}",
        )

    user_id = _user.get("id") if isinstance(_user, dict) else None

    # Persist the mapping (admin default — audio_session_id=None)
    await storage.set_channel_map(
        vendor_id=vendor_id,
        product_id=product_id,
        serial=serial,
        usb_port_path=usb_port_path,
        mapping=mapping,
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

    await audit(
        request,
        "audio_channel_map_saved",
        detail=(
            f"device={vendor_id:04x}:{product_id:04x} serial={serial} "
            f"port={usb_port_path} positions={sorted(set(mapping.values()))}"
        ),
        user=_user if isinstance(_user, dict) else None,
    )

    return Response(status_code=204)
