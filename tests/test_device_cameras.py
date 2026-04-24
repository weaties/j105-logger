"""Tests for ESP32-CAM push endpoints (#660).

Covers:
- GET /api/device-cameras/status — active-race check for sleepy cameras
- POST /api/device-cameras/{role}/photo — multipart photo ingest

Auth model: device bearer token via require_auth("crew"), with the
URL role path param required to match the authenticated device's name.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest

from helmlog.auth import generate_token
from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage

_AUTH_ENV = {"AUTH_DISABLED": "false"}


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def _register_camera(storage: Storage, role: str) -> str:
    """Create a crew-role device named after *role* and return its plaintext key."""
    key = generate_token()
    await storage.create_device(name=role, key_hash=_hash_key(key), role="crew")
    return key


async def _start_race(storage: Storage) -> int:
    """Start a race and return its id."""
    race = await storage.start_race(
        event="TestRegatta",
        start_utc=datetime.now(UTC),
        date_str=datetime.now(UTC).date().isoformat(),
        race_num=1,
        name="r1",
    )
    return race.id


# ---------------------------------------------------------------------------
# GET /api/device-cameras/status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_requires_auth(storage: Storage) -> None:
    """No bearer token → 401."""
    with patch.dict(os.environ, _AUTH_ENV):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/device-cameras/status")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_status_inactive_when_no_race(storage: Storage) -> None:
    """With valid bearer and no active race → active=false, session_id=null."""
    key = await _register_camera(storage, "mainsail")
    with patch.dict(os.environ, _AUTH_ENV):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/device-cameras/status",
                headers={"Authorization": f"Bearer {key}"},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"active": False, "session_id": None}


@pytest.mark.asyncio
async def test_status_active_with_race(storage: Storage) -> None:
    """With an active race → active=true, session_id=<race id>."""
    key = await _register_camera(storage, "mainsail")
    race_id = await _start_race(storage)
    with patch.dict(os.environ, _AUTH_ENV):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/device-cameras/status",
                headers={"Authorization": f"Bearer {key}"},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"active": True, "session_id": race_id}


# ---------------------------------------------------------------------------
# POST /api/device-cameras/{role}/photo
# ---------------------------------------------------------------------------


_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"


@pytest.mark.asyncio
async def test_photo_requires_auth(storage: Storage, tmp_path: Path) -> None:
    """No bearer token → 401."""
    with patch.dict(os.environ, {**_AUTH_ENV, "NOTES_DIR": str(tmp_path)}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/device-cameras/mainsail/photo",
                files={"file": ("p.jpg", _JPEG, "image/jpeg")},
            )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_photo_role_mismatch_forbidden(storage: Storage, tmp_path: Path) -> None:
    """Device auth'd as 'mainsail' posting to /headsail/photo → 403."""
    key = await _register_camera(storage, "mainsail")
    await _start_race(storage)
    with patch.dict(os.environ, {**_AUTH_ENV, "NOTES_DIR": str(tmp_path)}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/device-cameras/headsail/photo",
                headers={"Authorization": f"Bearer {key}"},
                files={"file": ("p.jpg", _JPEG, "image/jpeg")},
            )
    assert resp.status_code == 403
    # Nothing should have been written
    assert not any(tmp_path.rglob("*.jpg"))


@pytest.mark.asyncio
async def test_photo_no_active_race_returns_204(storage: Storage, tmp_path: Path) -> None:
    """No active race → 204 and no file written."""
    key = await _register_camera(storage, "mainsail")
    with patch.dict(os.environ, {**_AUTH_ENV, "NOTES_DIR": str(tmp_path)}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/device-cameras/mainsail/photo",
                headers={"Authorization": f"Bearer {key}"},
                files={"file": ("p.jpg", _JPEG, "image/jpeg")},
            )
    assert resp.status_code == 204
    assert not any(tmp_path.rglob("*.jpg"))


@pytest.mark.asyncio
async def test_photo_active_race_creates_note(storage: Storage, tmp_path: Path) -> None:
    """Active race → 201, file written with role prefix, photo note created."""
    key = await _register_camera(storage, "mainsail")
    race_id = await _start_race(storage)
    with patch.dict(os.environ, {**_AUTH_ENV, "NOTES_DIR": str(tmp_path)}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/device-cameras/mainsail/photo",
                headers={"Authorization": f"Bearer {key}"},
                files={"file": ("p.jpg", _JPEG, "image/jpeg")},
            )
    assert resp.status_code == 201
    body = resp.json()
    assert "id" in body
    assert body["photo_path"].startswith(f"{race_id}/mainsail_")
    assert body["photo_path"].endswith(".jpg")
    assert (tmp_path / body["photo_path"]).read_bytes() == _JPEG

    notes = await storage.list_notes(race_id=race_id)
    assert len(notes) == 1
    assert notes[0]["note_type"] == "photo"
    assert notes[0]["photo_path"] == body["photo_path"]


@pytest.mark.asyncio
async def test_photo_served_via_notes_route(storage: Storage, tmp_path: Path) -> None:
    """Uploaded photo is reachable via GET /notes/{path}."""
    key = await _register_camera(storage, "headsail")
    await _start_race(storage)
    with patch.dict(os.environ, {**_AUTH_ENV, "NOTES_DIR": str(tmp_path)}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            up = await client.post(
                "/api/device-cameras/headsail/photo",
                headers={"Authorization": f"Bearer {key}"},
                files={"file": ("p.jpg", _JPEG, "image/jpeg")},
            )
            assert up.status_code == 201
            path = up.json()["photo_path"]
            # Fetch with the same device bearer — crew can read /notes/ (viewer)
            resp = await client.get(
                f"/notes/{path}",
                headers={"Authorization": f"Bearer {key}"},
            )
    assert resp.status_code == 200
    assert resp.content == _JPEG
