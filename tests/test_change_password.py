"""Tests for change-password from profile page (#340)."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest
from httpx import ASGITransport

from helmlog.auth import generate_token, hash_password, session_expires_at, verify_password
from helmlog.web import create_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from helmlog.storage import Storage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

OLD_PASSWORD = "oldpassword12"
NEW_PASSWORD = "newpassword12"


async def _create_password_user(
    storage: Storage, email: str = "user@test.com", password: str = OLD_PASSWORD
) -> tuple[int, str]:
    """Create a user with a password credential and an active session."""
    user_id = await storage.create_user(email, "Test User", "crew")
    pw_hash = hash_password(password)
    await storage.create_credential(user_id, "password", email, pw_hash)
    session_id = generate_token()
    await storage.create_session(session_id, user_id, session_expires_at())
    return user_id, session_id


async def _create_oauth_user(storage: Storage) -> tuple[int, str]:
    """Create a user with only an OAuth credential (no password)."""
    user_id = await storage.create_user("oauth@test.com", "OAuth User", "crew")
    await storage.create_credential(user_id, "google", "oauth@test.com", None)
    session_id = generate_token()
    await storage.create_session(session_id, user_id, session_expires_at())
    return user_id, session_id


@asynccontextmanager
async def _client(storage: Storage, session_id: str) -> AsyncIterator[httpx.AsyncClient]:
    with patch.dict(os.environ, {"AUTH_DISABLED": "false"}):
        app = create_app(storage)
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            cookies={"session": session_id},
        ) as client:
            yield client


# ---------------------------------------------------------------------------
# T1: Success — change password
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_change_password_success(storage: Storage) -> None:
    """PATCH /api/me/password with valid inputs returns 204 and updates the hash."""
    user_id, session_id = await _create_password_user(storage)

    async with _client(storage, session_id) as client:
        resp = await client.patch(
            "/api/me/password",
            json={
                "current_password": OLD_PASSWORD,
                "new_password": NEW_PASSWORD,
                "confirm_password": NEW_PASSWORD,
            },
        )
    assert resp.status_code == 204

    # Verify the new password works
    cred = await storage.get_credential(user_id, "password")
    assert cred is not None
    assert verify_password(NEW_PASSWORD, cred["password_hash"]) is True
    assert verify_password(OLD_PASSWORD, cred["password_hash"]) is False


# ---------------------------------------------------------------------------
# T2: No credential — OAuth-only user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_change_password_no_credential(storage: Storage) -> None:
    """PATCH /api/me/password for OAuth-only user returns 422."""
    _, session_id = await _create_oauth_user(storage)

    async with _client(storage, session_id) as client:
        resp = await client.patch(
            "/api/me/password",
            json={
                "current_password": "anything",
                "new_password": NEW_PASSWORD,
                "confirm_password": NEW_PASSWORD,
            },
        )
    assert resp.status_code == 422
    assert "no password credential" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# T3: Wrong current password
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_change_password_wrong_current(storage: Storage) -> None:
    """PATCH /api/me/password with wrong current password returns 403."""
    _, session_id = await _create_password_user(storage)

    async with _client(storage, session_id) as client:
        resp = await client.patch(
            "/api/me/password",
            json={
                "current_password": "wrongpassword1",
                "new_password": NEW_PASSWORD,
                "confirm_password": NEW_PASSWORD,
            },
        )
    assert resp.status_code == 403
    assert "incorrect" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# T5: Confirm mismatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_change_password_confirm_mismatch(storage: Storage) -> None:
    """PATCH /api/me/password with mismatched confirm returns 422."""
    _, session_id = await _create_password_user(storage)

    async with _client(storage, session_id) as client:
        resp = await client.patch(
            "/api/me/password",
            json={
                "current_password": OLD_PASSWORD,
                "new_password": NEW_PASSWORD,
                "confirm_password": "differentpass1",
            },
        )
    assert resp.status_code == 422
    assert "do not match" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# T6: Profile page shows form for password user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_profile_shows_password_form(storage: Storage) -> None:
    """GET /profile for a password user includes the change-password form."""
    _, session_id = await _create_password_user(storage)

    async with _client(storage, session_id) as client:
        resp = await client.get("/profile")
    assert resp.status_code == 200
    assert 'id="current-password"' in resp.text


# ---------------------------------------------------------------------------
# T7: Profile page hides form for OAuth-only user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_profile_hides_password_form_for_oauth(storage: Storage) -> None:
    """GET /profile for an OAuth-only user does NOT show the change-password form."""
    _, session_id = await _create_oauth_user(storage)

    async with _client(storage, session_id) as client:
        resp = await client.get("/profile")
    assert resp.status_code == 200
    assert 'id="current-password"' not in resp.text
    assert "Change Password</button>" not in resp.text


# ---------------------------------------------------------------------------
# T8: Audit log entry on success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_change_password_audit_log(storage: Storage) -> None:
    """Successful password change creates an audit log entry."""
    user_id, session_id = await _create_password_user(storage)

    async with _client(storage, session_id) as client:
        resp = await client.patch(
            "/api/me/password",
            json={
                "current_password": OLD_PASSWORD,
                "new_password": NEW_PASSWORD,
                "confirm_password": NEW_PASSWORD,
            },
        )
    assert resp.status_code == 204

    # Check audit log
    db = storage._read_conn()
    cur = await db.execute(
        "SELECT action FROM audit_log WHERE user_id = ? ORDER BY id DESC LIMIT 1",
        (user_id,),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row["action"] == "password.change"
