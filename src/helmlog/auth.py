"""Authentication and authorisation helpers for the HelmLog web interface.

Invitation + flexible authentication flow (#268):
  1. Admin creates an invitation via POST /admin/users/invite.
  2. Invitation link is sent or copied: GET /auth/accept-invite?token=<token>.
  3. Recipient registers (password or OAuth), creating a user + credential.
  4. Subsequent logins use email+password or OAuth provider.
  5. ``require_auth`` validates the session cookie and attaches the user.

Device API key auth (#423):
  Headless devices (ESP32-CAM, IoT sensors) authenticate via
  ``Authorization: Bearer <key>``.  Keys are stored hashed (SHA-256) in the
  ``devices`` table.  Each device has a fixed role and optional scope.

Auth can be disabled entirely (e.g. in tests) by setting the env var
``AUTH_DISABLED=true``.  In that mode every request is treated as an admin.
"""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import os
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Cookie, HTTPException, Request, status

if TYPE_CHECKING:
    from collections.abc import Callable

    from helmlog.storage import Storage

# ---------------------------------------------------------------------------
# Role ordering
# ---------------------------------------------------------------------------

_ROLE_RANK: dict[str, int] = {"viewer": 0, "crew": 1, "admin": 2}

SESSION_TTL_DAYS = int(os.getenv("AUTH_SESSION_TTL_DAYS", "90"))


def _is_auth_disabled() -> bool:
    return os.getenv("AUTH_DISABLED", "false").lower() in {"1", "true", "yes"}


# ---------------------------------------------------------------------------
# Mock admin user returned when auth is disabled
# ---------------------------------------------------------------------------

_MOCK_ADMIN: dict[str, Any] = {
    "id": None,
    "email": "admin@local",
    "name": "Local Admin",
    "role": "admin",
    "is_developer": 1,
    "created_at": "1970-01-01T00:00:00+00:00",
    "last_seen": None,
    "is_active": 1,
}


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------


def _get_storage(request: Request) -> Storage:
    return request.app.state.storage  # type: ignore[no-any-return]


async def _resolve_user(
    request: Request,
    session: Annotated[str | None, Cookie()] = None,
) -> dict[str, Any] | None:
    """Return the authenticated user dict, or None if not authenticated."""
    if _is_auth_disabled():
        return _MOCK_ADMIN

    if not session:
        return None

    storage: Storage = _get_storage(request)
    session_row = await storage.get_session(session)
    if not session_row:
        return None

    # Check expiry
    expires_at = datetime.fromisoformat(session_row["expires_at"])
    if datetime.now(UTC) > expires_at:
        await storage.delete_session(session)
        return None

    user = await storage.get_user_by_id(session_row["user_id"])
    if not user:
        return None

    # Reject deactivated users (#268)
    if not user.get("is_active", 1):
        return None

    # Fire-and-forget last_seen update (best effort)
    with contextlib.suppress(Exception):
        await storage.update_user_last_seen(user["id"])

    return user


def require_auth(min_role: str = "viewer") -> Callable[..., Any]:
    """FastAPI dependency factory. Returns a dependency that enforces *min_role*.

    Usage::

        @app.get("/admin/users")
        async def admin_users(user=Depends(require_auth("admin"))):
            ...
    """
    rank = _ROLE_RANK.get(min_role)
    if rank is None:
        raise ValueError(f"Unknown role: {min_role!r}")

    async def _dep(
        request: Request,
        session: Annotated[str | None, Cookie()] = None,
    ) -> dict[str, Any]:
        # Check if the auth middleware already resolved a user (e.g. device bearer token)
        user: dict[str, Any] | None = getattr(request.state, "user", None)
        if user is None:
            user = await _resolve_user(request, session)
        if user is None:
            # Redirect browsers to /login; return 401 for API clients
            accept = request.headers.get("accept", "")
            if "text/html" in accept:
                raise HTTPException(
                    status_code=status.HTTP_307_TEMPORARY_REDIRECT,
                    headers={"Location": f"/login?next={request.url.path}"},
                )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated",
            )

        user_rank = _ROLE_RANK.get(user["role"], -1)
        if user_rank < rank:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")

        return user

    return _dep


async def require_developer(
    request: Request,
    session: Annotated[str | None, Cookie()] = None,
) -> dict[str, Any]:
    """FastAPI dependency that enforces the ``is_developer`` flag.

    Must be used alongside ``require_auth()`` — this only checks the flag,
    not the role rank.  Use as an additional ``Depends()`` on routes that
    need developer access.
    """
    user: dict[str, Any] | None = getattr(request.state, "user", None)
    if user is None:
        user = await _resolve_user(request, session)
    if user is None:
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            raise HTTPException(
                status_code=status.HTTP_307_TEMPORARY_REDIRECT,
                headers={"Location": f"/login?next={request.url.path}"},
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    if not user.get("is_developer"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Developer access required"
        )

    return user


# ---------------------------------------------------------------------------
# Token / session helpers (called from web.py routes)
# ---------------------------------------------------------------------------


def generate_token(nbytes: int = 32) -> str:
    """Return a URL-safe random token string."""
    return secrets.token_urlsafe(nbytes)


def session_expires_at() -> str:
    """Return the ISO-8601 expiry datetime for a new session."""
    return (datetime.now(UTC) + timedelta(days=SESSION_TTL_DAYS)).isoformat()


def invite_expires_at(days: int = 7) -> str:
    """Return the ISO-8601 expiry datetime for a new invite token (default 7 days)."""
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def reset_token_expires_at(hours: int = 1) -> str:
    """Return the ISO-8601 expiry datetime for a password reset token."""
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat()


# ---------------------------------------------------------------------------
# Password hashing (argon2)
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Hash a password using Argon2id."""
    from argon2 import PasswordHasher

    ph = PasswordHasher()
    return ph.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against an Argon2 hash. Returns False on mismatch."""
    from argon2 import PasswordHasher
    from argon2.exceptions import VerificationError

    ph = PasswordHasher()
    try:
        return ph.verify(password_hash, password)
    except VerificationError:
        return False


# ---------------------------------------------------------------------------
# Device API key helpers (#423)
# ---------------------------------------------------------------------------


def hash_api_key(key: str) -> str:
    """Return the SHA-256 hex digest of a plaintext API key."""
    return hashlib.sha256(key.encode()).hexdigest()


async def resolve_device(request: Request) -> dict[str, Any] | None:
    """Check for ``Authorization: Bearer <key>`` and return a device dict.

    Returns a user-shaped dict (with ``id``, ``role``, ``name``, etc.) so the
    rest of the auth stack can treat devices like users.  Returns None if no
    bearer token or the token is invalid/revoked.
    """
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return None

    token = auth_header[7:]  # strip "Bearer "
    key_hash = hash_api_key(token)
    storage: Storage = _get_storage(request)
    device = await storage.get_device_by_key_hash(key_hash)
    if device is None:
        return None

    # Fire-and-forget last_used update
    with contextlib.suppress(Exception):
        await storage.update_device_last_used(device["id"])

    return {
        "id": None,
        "email": f"device:{device['name']}",
        "name": device["name"],
        "role": device["role"],
        "is_developer": 0,
        "is_active": 1,
        "device_id": device["id"],
        "device_scope": device["scope"],
    }


def check_device_scope(scope: str | None, method: str, path: str) -> bool:
    """Return True if the request method+path is allowed by the device scope.

    *scope* is a comma-separated list of ``METHOD /path/pattern`` entries.
    Patterns support ``*`` wildcards (matched per path segment via fnmatch).
    If *scope* is None, all paths are allowed.
    """
    if scope is None:
        return True

    for entry in scope.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(None, 1)
        if len(parts) != 2:  # noqa: PLR2004
            continue
        allowed_method, allowed_path = parts
        if allowed_method.upper() != method.upper():
            continue
        if fnmatch.fnmatch(path, allowed_path):
            return True

    return False
