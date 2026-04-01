"""Tests for device API key authentication (#423).

Covers:
- Storage CRUD for devices (create, list, get by key hash, revoke, rotate)
- Bearer token authentication via auth middleware
- Scope enforcement (device limited to specific paths)
- Admin endpoints for device management
- Audit logging of device requests
"""

from __future__ import annotations

import hashlib
import os
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest

from helmlog.auth import generate_token, session_expires_at
from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage

_AUTH_ENV = {"AUTH_DISABLED": "false"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_admin_user(storage: Storage) -> tuple[int, str]:
    """Create an admin user and return (user_id, session_id)."""
    user_id = await storage.create_user("admin@test.com", "Test Admin", "admin")
    session_id = generate_token()
    await storage.create_session(session_id, user_id, session_expires_at())
    return user_id, session_id


def _hash_key(key: str) -> str:
    """SHA-256 hash of a plaintext API key — must match storage implementation."""
    return hashlib.sha256(key.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Storage CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_device(storage: Storage) -> None:
    """create_device stores a device and returns its id."""
    key = generate_token()
    device_id = await storage.create_device(
        name="mast-cam-1",
        key_hash=_hash_key(key),
        role="crew",
    )
    assert isinstance(device_id, int)

    device = await storage.get_device(device_id)
    assert device is not None
    assert device["name"] == "mast-cam-1"
    assert device["role"] == "crew"
    assert device["is_active"] == 1
    assert device["scope"] is None


@pytest.mark.asyncio
async def test_create_device_with_scope(storage: Storage) -> None:
    """Device can be created with an optional scope."""
    key = generate_token()
    device_id = await storage.create_device(
        name="bow-cam",
        key_hash=_hash_key(key),
        role="crew",
        scope="POST /api/sessions/*/notes, GET /api/sessions",
    )
    device = await storage.get_device(device_id)
    assert device is not None
    assert device["scope"] == "POST /api/sessions/*/notes, GET /api/sessions"


@pytest.mark.asyncio
async def test_get_device_by_key_hash(storage: Storage) -> None:
    """Look up an active device by its hashed API key."""
    key = generate_token()
    key_hash = _hash_key(key)
    await storage.create_device(name="cam-1", key_hash=key_hash, role="crew")

    device = await storage.get_device_by_key_hash(key_hash)
    assert device is not None
    assert device["name"] == "cam-1"


@pytest.mark.asyncio
async def test_get_device_by_key_hash_revoked(storage: Storage) -> None:
    """Revoked device is not returned by key hash lookup."""
    key = generate_token()
    key_hash = _hash_key(key)
    device_id = await storage.create_device(name="cam-1", key_hash=key_hash, role="crew")
    await storage.revoke_device(device_id)

    device = await storage.get_device_by_key_hash(key_hash)
    assert device is None


@pytest.mark.asyncio
async def test_list_devices(storage: Storage) -> None:
    """list_devices returns all devices."""
    await storage.create_device(name="cam-1", key_hash=_hash_key("a"), role="crew")
    await storage.create_device(name="cam-2", key_hash=_hash_key("b"), role="crew")

    devices = await storage.list_devices()
    assert len(devices) == 2
    names = {d["name"] for d in devices}
    assert names == {"cam-1", "cam-2"}


@pytest.mark.asyncio
async def test_revoke_device(storage: Storage) -> None:
    """Revoking a device sets is_active to 0."""
    device_id = await storage.create_device(name="cam-1", key_hash=_hash_key("a"), role="crew")
    await storage.revoke_device(device_id)

    device = await storage.get_device(device_id)
    assert device is not None
    assert device["is_active"] == 0


@pytest.mark.asyncio
async def test_rotate_device_key(storage: Storage) -> None:
    """Rotating replaces the key hash."""
    old_hash = _hash_key("old")
    new_hash = _hash_key("new")
    device_id = await storage.create_device(name="cam-1", key_hash=old_hash, role="crew")

    await storage.rotate_device_key(device_id, new_hash)

    # Old hash no longer works
    assert await storage.get_device_by_key_hash(old_hash) is None
    # New hash works
    device = await storage.get_device_by_key_hash(new_hash)
    assert device is not None
    assert device["name"] == "cam-1"


@pytest.mark.asyncio
async def test_update_device_last_used(storage: Storage) -> None:
    """update_device_last_used sets the last_used timestamp."""
    device_id = await storage.create_device(name="cam-1", key_hash=_hash_key("a"), role="crew")

    device = await storage.get_device(device_id)
    assert device is not None
    assert device["last_used"] is None

    await storage.update_device_last_used(device_id)
    device = await storage.get_device(device_id)
    assert device is not None
    assert device["last_used"] is not None


@pytest.mark.asyncio
async def test_duplicate_device_name_rejected(storage: Storage) -> None:
    """Device names must be unique."""
    await storage.create_device(name="cam-1", key_hash=_hash_key("a"), role="crew")
    with pytest.raises(Exception):  # noqa: B017
        await storage.create_device(name="cam-1", key_hash=_hash_key("b"), role="crew")


# ---------------------------------------------------------------------------
# Bearer token auth (web integration)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bearer_auth_valid(storage: Storage) -> None:
    """A valid bearer token authenticates the device."""
    key = generate_token()
    await storage.create_device(name="cam-1", key_hash=_hash_key(key), role="crew")
    with patch.dict(os.environ, _AUTH_ENV):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/sessions",
                headers={"Authorization": f"Bearer {key}"},
            )
    # Should not be 401
    assert resp.status_code != 401


@pytest.mark.asyncio
async def test_bearer_auth_invalid_key(storage: Storage) -> None:
    """An invalid bearer token returns 401."""
    with patch.dict(os.environ, _AUTH_ENV):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/sessions",
                headers={"Authorization": "Bearer totally-bogus-key"},
            )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_bearer_auth_revoked_key(storage: Storage) -> None:
    """A revoked device's bearer token returns 401."""
    key = generate_token()
    device_id = await storage.create_device(name="cam-1", key_hash=_hash_key(key), role="crew")
    await storage.revoke_device(device_id)
    with patch.dict(os.environ, _AUTH_ENV):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/sessions",
                headers={"Authorization": f"Bearer {key}"},
            )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_bearer_auth_role_enforcement(storage: Storage) -> None:
    """Device with crew role cannot access admin endpoints."""
    key = generate_token()
    await storage.create_device(name="cam-1", key_hash=_hash_key(key), role="crew")
    with patch.dict(os.environ, _AUTH_ENV):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/admin/users",
                headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_bearer_auth_scope_allowed(storage: Storage) -> None:
    """Device with scope can access allowed paths."""
    key = generate_token()
    await storage.create_device(
        name="cam-1",
        key_hash=_hash_key(key),
        role="crew",
        scope="GET /api/sessions",
    )
    with patch.dict(os.environ, _AUTH_ENV):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/sessions",
                headers={"Authorization": f"Bearer {key}"},
            )
    assert resp.status_code != 403


@pytest.mark.asyncio
async def test_bearer_auth_scope_denied(storage: Storage) -> None:
    """Device with scope is denied access to paths outside its scope."""
    key = generate_token()
    await storage.create_device(
        name="cam-1",
        key_hash=_hash_key(key),
        role="crew",
        scope="GET /api/sessions",
    )
    with patch.dict(os.environ, _AUTH_ENV):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/me",
                headers={"Authorization": f"Bearer {key}"},
            )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_bearer_auth_scope_wildcard(storage: Storage) -> None:
    """Scope with wildcard matches path segments."""
    key = generate_token()
    await storage.create_device(
        name="cam-1",
        key_hash=_hash_key(key),
        role="crew",
        scope="POST /api/sessions/*/notes",
    )
    with patch.dict(os.environ, _AUTH_ENV):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # POST to a specific session's notes — should be allowed (not 403)
            resp = await client.post(
                "/api/sessions/42/notes",
                headers={"Authorization": f"Bearer {key}"},
                json={"text": "test note"},
            )
    assert resp.status_code != 403


@pytest.mark.asyncio
async def test_device_cannot_get_admin_role(storage: Storage) -> None:
    """Devices cannot be created with admin role."""
    with pytest.raises(ValueError, match="admin"):
        await storage.create_device(name="hacker", key_hash=_hash_key("x"), role="admin")


# ---------------------------------------------------------------------------
# Admin device management endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_create_device(storage: Storage) -> None:
    """Admin can create a device via POST /admin/devices."""
    _, session_id = await _create_admin_user(storage)
    with patch.dict(os.environ, _AUTH_ENV):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/admin/devices",
                data={"name": "mast-cam-1", "role": "crew"},
                cookies={"session": session_id},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert "api_key" in body
    assert body["name"] == "mast-cam-1"
    assert len(body["api_key"]) >= 32


@pytest.mark.asyncio
async def test_admin_list_devices(storage: Storage) -> None:
    """Admin can list devices via GET /api/devices."""
    _, session_id = await _create_admin_user(storage)
    await storage.create_device(name="cam-1", key_hash=_hash_key("a"), role="crew")
    await storage.create_device(name="cam-2", key_hash=_hash_key("b"), role="crew")
    with patch.dict(os.environ, _AUTH_ENV):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/devices",
                cookies={"session": session_id},
            )
    assert resp.status_code == 200
    devices = resp.json()
    assert len(devices) == 2


@pytest.mark.asyncio
async def test_admin_revoke_device(storage: Storage) -> None:
    """Admin can revoke a device via DELETE /admin/devices/{id}."""
    _, session_id = await _create_admin_user(storage)
    device_id = await storage.create_device(name="cam-1", key_hash=_hash_key("a"), role="crew")
    with patch.dict(os.environ, _AUTH_ENV):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.delete(
                f"/admin/devices/{device_id}",
                cookies={"session": session_id},
            )
    assert resp.status_code == 200
    device = await storage.get_device(device_id)
    assert device is not None
    assert device["is_active"] == 0


@pytest.mark.asyncio
async def test_admin_rotate_device_key(storage: Storage) -> None:
    """Admin can rotate a device key via POST /admin/devices/{id}/rotate."""
    _, session_id = await _create_admin_user(storage)
    old_key = generate_token()
    device_id = await storage.create_device(name="cam-1", key_hash=_hash_key(old_key), role="crew")
    with patch.dict(os.environ, _AUTH_ENV):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/admin/devices/{device_id}/rotate",
                cookies={"session": session_id},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert "api_key" in body
    assert body["api_key"] != old_key
    assert await storage.get_device_by_key_hash(_hash_key(old_key)) is None
    new_device = await storage.get_device_by_key_hash(_hash_key(body["api_key"]))
    assert new_device is not None


@pytest.mark.asyncio
async def test_non_admin_cannot_manage_devices(storage: Storage) -> None:
    """Crew users cannot access device management endpoints."""
    user_id = await storage.create_user("crew@test.com", "Crew", "crew")
    session_id = generate_token()
    await storage.create_session(session_id, user_id, session_expires_at())
    with patch.dict(os.environ, _AUTH_ENV):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/admin/devices",
                data={"name": "hacker-cam", "role": "crew"},
                cookies={"session": session_id},
            )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_device_auth_audit_trail(storage: Storage) -> None:
    """Device requests update last_used timestamp."""
    key = generate_token()
    await storage.create_device(name="mast-cam-1", key_hash=_hash_key(key), role="crew")
    with patch.dict(os.environ, _AUTH_ENV):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.get(
                "/api/sessions",
                headers={"Authorization": f"Bearer {key}"},
            )

    devices = await storage.list_devices()
    cam = [d for d in devices if d["name"] == "mast-cam-1"][0]
    assert cam["last_used"] is not None
