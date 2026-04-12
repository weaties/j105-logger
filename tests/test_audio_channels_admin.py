"""Tests for the admin audio-channels UI / API (#462 pt.4 / #496)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio

from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def admin_client(  # type: ignore[misc]
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> httpx.AsyncClient:
    """An auth-bypassed client (every request is admin)."""
    monkeypatch.setenv("AUTH_DISABLED", "true")
    # Stop usb_audio detection from leaking real hardware into the test
    monkeypatch.setattr(
        "helmlog.routes.audio_channels.detect_multi_channel_device",
        lambda *, min_channels=2: None,
    )
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest_asyncio.fixture
async def viewer_client(  # type: ignore[misc]
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> httpx.AsyncClient:
    """An auth-bypassed client downgraded to viewer role to exercise denial."""
    monkeypatch.setenv("AUTH_DISABLED", "false")
    # Patch require_auth to return a fixed viewer-role user. This is the
    # cleanest way to assert role-based denial without standing up the full
    # magic-link flow.
    from helmlog.routes import audio_channels as ac_routes

    async def fake_require(role: str, request: object) -> object:  # noqa: ARG001
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="forbidden")

    def fake_factory(role: str):  # noqa: ANN001, ANN202
        async def dep(request: object) -> object:  # noqa: ARG001
            return await fake_require(role, request)

        return dep

    monkeypatch.setattr(ac_routes, "require_auth", fake_factory)
    monkeypatch.setattr(
        "helmlog.routes.audio_channels.detect_multi_channel_device",
        lambda *, min_channels=2: None,
    )
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Page renders
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_page_renders(admin_client: httpx.AsyncClient) -> None:
    resp = await admin_client.get("/admin/audio-channels")
    assert resp.status_code == 200
    assert "Audio Channels" in resp.text
    assert "voice biometric consent" in resp.text.lower()


# ---------------------------------------------------------------------------
# GET devices
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_devices_returns_saved_mappings(
    admin_client: httpx.AsyncClient, storage: Storage
) -> None:
    await storage.set_channel_map(
        vendor_id=0x1234,
        product_id=0x5678,
        serial="ABC",
        usb_port_path="1-1.2",
        mapping={0: "helm", 1: "trim"},
    )
    resp = await admin_client.get("/api/audio-channels/devices")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    d = body[0]
    assert d["vendor_id"] == 0x1234
    assert d["product_id"] == 0x5678
    assert d["serial"] == "ABC"
    assert d["usb_port_path"] == "1-1.2"
    # JSON object keys are strings
    assert d["mapping"] == {"0": "helm", "1": "trim"}


# ---------------------------------------------------------------------------
# POST save — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_writes_mapping_and_consent_audit(
    admin_client: httpx.AsyncClient, storage: Storage
) -> None:
    payload = {
        "vendor_id": 0x1234,
        "product_id": 0x5678,
        "serial": "ABC",
        "usb_port_path": "1-1.2",
        "mapping": {"0": "helm", "1": "tactician"},
        "consent_acks": ["helm", "tactician"],
    }
    resp = await admin_client.post("/api/audio-channels/save", json=payload)
    assert resp.status_code == 204

    saved = await storage.get_channel_map(
        vendor_id=0x1234, product_id=0x5678, serial="ABC", usb_port_path="1-1.2"
    )
    assert saved == {0: "helm", 1: "tactician"}

    # Two voice_consent_ack entries (one per position) plus an
    # audio_channel_map_saved umbrella entry.
    entries = await storage.list_audit_log(limit=20)
    actions = [e["action"] for e in entries]
    assert actions.count("voice_consent_ack") == 2
    assert "audio_channel_map_saved" in actions


# ---------------------------------------------------------------------------
# POST save — missing consent rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_rejected_when_consent_missing(
    admin_client: httpx.AsyncClient, storage: Storage
) -> None:
    payload = {
        "vendor_id": 0x1234,
        "product_id": 0x5678,
        "serial": "ABC",
        "usb_port_path": "1-1.2",
        "mapping": {"0": "helm", "1": "tactician"},
        "consent_acks": ["helm"],  # tactician missing
    }
    resp = await admin_client.post("/api/audio-channels/save", json=payload)
    assert resp.status_code == 400
    assert "tactician" in resp.text

    saved = await storage.get_channel_map(
        vendor_id=0x1234, product_id=0x5678, serial="ABC", usb_port_path="1-1.2"
    )
    assert saved == {}

    entries = await storage.list_audit_log(limit=20)
    assert all(e["action"] != "voice_consent_ack" for e in entries)


@pytest.mark.asyncio
async def test_save_rejects_empty_mapping(admin_client: httpx.AsyncClient) -> None:
    resp = await admin_client.post(
        "/api/audio-channels/save",
        json={
            "vendor_id": 1,
            "product_id": 2,
            "serial": "",
            "usb_port_path": "x",
            "mapping": {},
            "consent_acks": [],
        },
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST save — non-admin denied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_denied_for_non_admin(viewer_client: httpx.AsyncClient) -> None:
    resp = await viewer_client.post(
        "/api/audio-channels/save",
        json={
            "vendor_id": 1,
            "product_id": 2,
            "serial": "",
            "usb_port_path": "x",
            "mapping": {"0": "helm"},
            "consent_acks": ["helm"],
        },
    )
    # Auth middleware short-circuits unauthenticated requests at 401 before
    # the route's require_auth("admin") dep returns 403; both prove the
    # non-admin denial path is wired up.
    assert resp.status_code in (401, 403)
