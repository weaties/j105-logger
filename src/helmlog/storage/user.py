"""User and authentication repository."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .base import BaseRepository


class UserRepository(BaseRepository):
    """Manages users, invitations, credentials, sessions, audit logs, and tags."""

    _USER_COLS = (
        "id, email, name, role, created_at, last_seen,"
        " avatar_path, is_developer, is_active, weight_lbs, color_scheme"
    )

    async def create_user(
        self,
        email: str,
        name: str | None = None,
        role: str = "viewer",
        is_developer: bool = False,
        is_active: bool = True,
    ) -> int:
        """Insert a new user and return their id."""
        now = datetime.now(UTC).isoformat()
        db = self._conn()
        cur = await db.execute(
            "INSERT INTO users (email, name, role, created_at, is_developer, is_active)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (email.lower().strip(), name, role, now, int(is_developer), int(is_active)),
        )
        await db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        cur = await self._read_conn().execute(
            f"SELECT {self._USER_COLS} FROM users WHERE id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        cur = await self._read_conn().execute(
            f"SELECT {self._USER_COLS} FROM users WHERE email = ?",
            (email.lower().strip(),),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def update_user_role(self, user_id: int, role: str) -> None:
        db = self._conn()
        await db.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        await db.commit()

    async def update_user_last_seen(self, user_id: int) -> None:
        now = datetime.now(UTC).isoformat()
        db = self._conn()
        await db.execute("UPDATE users SET last_seen = ? WHERE id = ?", (now, user_id))
        await db.commit()

    async def update_user_developer(self, user_id: int, is_developer: bool) -> None:
        db = self._conn()
        await db.execute(
            "UPDATE users SET is_developer = ? WHERE id = ?", (int(is_developer), user_id)
        )
        await db.commit()

    async def update_user_profile(self, user_id: int, name: str | None, email: str | None) -> None:
        """Update a user's name and/or email."""
        db = self._conn()
        if email is not None:
            await db.execute(
                "UPDATE users SET email = ? WHERE id = ?",
                (email.lower().strip(), user_id),
            )
        if name is not None:
            await db.execute("UPDATE users SET name = ? WHERE id = ?", (name, user_id))
        await db.commit()

    async def list_users(self) -> list[dict[str, Any]]:
        cur = await self._read_conn().execute(
            "SELECT id, email, name, role, created_at, last_seen, is_developer,"
            " weight_lbs"
            " FROM users WHERE email NOT LIKE 'deleted_%@redacted'"
            " ORDER BY created_at"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # -- Invitations --

    async def create_invitation(
        self,
        token: str,
        email: str,
        role: str,
        name: str | None,
        is_developer: bool,
        invited_by: int | None,
        expires_at: str,
    ) -> int:
        now = datetime.now(UTC).isoformat()
        db = self._conn()
        cur = await db.execute(
            "INSERT INTO invitations"
            " (token, email, role, name, is_developer, invited_by, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                token,
                email.lower().strip(),
                role,
                name,
                int(is_developer),
                invited_by,
                now,
                expires_at,
            ),
        )
        await db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def get_invitation(self, token: str) -> dict[str, Any] | None:
        cur = await self._read_conn().execute(
            "SELECT id, token, email, role, name, is_developer, invited_by,"
            " created_at, expires_at, accepted_at, revoked_at"
            " FROM invitations WHERE token = ?",
            (token,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def accept_invitation(self, token: str) -> None:
        now = datetime.now(UTC).isoformat()
        db = self._conn()
        await db.execute("UPDATE invitations SET accepted_at = ? WHERE token = ?", (now, token))
        await db.commit()

    async def revoke_invitation(self, invitation_id: int) -> None:
        now = datetime.now(UTC).isoformat()
        db = self._conn()
        await db.execute("UPDATE invitations SET revoked_at = ? WHERE id = ?", (now, invitation_id))
        await db.commit()

    async def list_pending_invitations(self) -> list[dict[str, Any]]:
        now = datetime.now(UTC).isoformat()
        cur = await self._read_conn().execute(
            "SELECT id, token, email, role, name, is_developer, invited_by,"
            " created_at, expires_at"
            " FROM invitations"
            " WHERE accepted_at IS NULL AND revoked_at IS NULL AND expires_at > ?"
            " ORDER BY created_at DESC",
            (now,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def list_pending_invitation_emails(self) -> set[str]:
        now = datetime.now(UTC).isoformat()
        cur = await self._read_conn().execute(
            "SELECT DISTINCT email FROM invitations"
            " WHERE accepted_at IS NULL AND revoked_at IS NULL AND expires_at > ?",
            (now,),
        )
        rows = await cur.fetchall()
        return {r["email"] for r in rows}

    # -- User credentials --

    async def create_credential(
        self,
        user_id: int,
        provider: str,
        provider_uid: str | None,
        password_hash: str | None,
    ) -> int:
        now = datetime.now(UTC).isoformat()
        db = self._conn()
        cur = await db.execute(
            "INSERT INTO user_credentials"
            " (user_id, provider, provider_uid, password_hash, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (user_id, provider, provider_uid, password_hash, now),
        )
        await db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def get_credential(self, user_id: int, provider: str) -> dict[str, Any] | None:
        cur = await self._read_conn().execute(
            "SELECT id, user_id, provider, provider_uid, password_hash, created_at"
            " FROM user_credentials WHERE user_id = ? AND provider = ?",
            (user_id, provider),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_credential_by_provider_uid(
        self, provider: str, provider_uid: str
    ) -> dict[str, Any] | None:
        cur = await self._read_conn().execute(
            "SELECT id, user_id, provider, provider_uid, password_hash, created_at"
            " FROM user_credentials WHERE provider = ? AND provider_uid = ?",
            (provider, provider_uid),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def update_password_hash(self, user_id: int, password_hash: str) -> None:
        db = self._conn()
        await db.execute(
            "UPDATE user_credentials SET password_hash = ?"
            " WHERE user_id = ? AND provider = 'password'",
            (password_hash, user_id),
        )
        await db.commit()

    # -- Password reset tokens --

    async def create_password_reset_token(self, token: str, user_id: int, expires_at: str) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO password_reset_tokens (token, user_id, expires_at) VALUES (?, ?, ?)",
            (token, user_id, expires_at),
        )
        await db.commit()

    async def get_password_reset_token(self, token: str) -> dict[str, Any] | None:
        cur = await self._read_conn().execute(
            "SELECT id, token, user_id, expires_at, used_at"
            " FROM password_reset_tokens WHERE token = ?",
            (token,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def use_password_reset_token(self, token: str) -> None:
        now = datetime.now(UTC).isoformat()
        db = self._conn()
        await db.execute(
            "UPDATE password_reset_tokens SET used_at = ? WHERE token = ?", (now, token)
        )
        await db.commit()

    # -- User activation --

    async def deactivate_user(self, user_id: int) -> None:
        db = self._conn()
        await db.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user_id,))
        await db.commit()

    async def activate_user(self, user_id: int) -> None:
        db = self._conn()
        await db.execute("UPDATE users SET is_active = 1 WHERE id = ?", (user_id,))
        await db.commit()

    # -- Sessions --

    async def create_session(
        self,
        session_id: str,
        user_id: int,
        expires_at: str,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        db = self._conn()
        await db.execute(
            "INSERT INTO auth_sessions"
            " (session_id, user_id, created_at, expires_at, ip, user_agent)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, user_id, now, expires_at, ip, user_agent),
        )
        await db.commit()

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        cur = await self._read_conn().execute(
            "SELECT session_id, user_id, created_at, expires_at, ip, user_agent"
            " FROM auth_sessions WHERE session_id = ?",
            (session_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def delete_session(self, session_id: str) -> None:
        db = self._conn()
        await db.execute("DELETE FROM auth_sessions WHERE session_id = ?", (session_id,))
        await db.commit()

    async def list_auth_sessions(self, user_id: int | None = None) -> list[dict[str, Any]]:
        if user_id is not None:
            cur = await self._read_conn().execute(
                "SELECT session_id, user_id, created_at, expires_at, ip, user_agent"
                " FROM auth_sessions WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            )
        else:
            cur = await self._read_conn().execute(
                "SELECT s.session_id, s.user_id, s.created_at, s.expires_at, s.ip, s.user_agent,"
                " u.email, u.name, u.role"
                " FROM auth_sessions s JOIN users u ON s.user_id = u.id"
                " ORDER BY s.created_at DESC"
            )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def delete_expired_sessions(self) -> None:
        now = datetime.now(UTC).isoformat()
        db = self._conn()
        await db.execute("DELETE FROM auth_sessions WHERE expires_at < ?", (now,))
        await db.commit()

    # -- Audit log --

    async def log_action(
        self,
        action: str,
        *,
        detail: str | None = None,
        user_id: int | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> int:
        ts = datetime.now(UTC).isoformat()
        db = self._conn()
        cur = await db.execute(
            "INSERT INTO audit_log (ts, user_id, action, detail, ip_address, user_agent)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (ts, user_id, action, detail, ip_address, user_agent),
        )
        await db.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def list_audit_log(self, *, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        cur = await self._read_conn().execute(
            "SELECT a.id, a.ts, a.action, a.detail, a.ip_address, a.user_agent,"
            " a.user_id, u.name AS user_name, u.email AS user_email"
            " FROM audit_log a LEFT JOIN users u ON a.user_id = u.id"
            " ORDER BY a.ts DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # -- Tags --

    async def create_tag(self, name: str, color: str | None = None) -> int:
        name = name.strip().lower()
        ts = datetime.now(UTC).isoformat()
        db = self._conn()
        cur = await db.execute(
            "INSERT INTO tags (name, color, created_at) VALUES (?, ?, ?)",
            (name, color, ts),
        )
        await db.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def get_tag_by_name(self, name: str) -> dict[str, Any] | None:
        cur = await self._read_conn().execute(
            "SELECT id, name, color, created_at FROM tags WHERE name = ?",
            (name.strip().lower(),),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_tags(self) -> list[dict[str, Any]]:
        cur = await self._read_conn().execute(
            "SELECT t.id, t.name, t.color, t.created_at,"
            " (SELECT COUNT(*) FROM session_tags st WHERE st.tag_id = t.id) AS session_count,"
            " (SELECT COUNT(*) FROM note_tags nt WHERE nt.tag_id = t.id) AS note_count"
            " FROM tags t ORDER BY t.name"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def delete_tag(self, tag_id: int) -> bool:
        db = self._conn()
        cur = await db.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
        await db.commit()
        return cur.rowcount > 0

    async def delete_user(self, user_id: int) -> None:
        db = self._conn()
        # Credentials, sessions, and reset tokens will cascade if FKs are enabled.
        # However, invitations.invited_by is a FK but we usually want to KEEP 
        # the invitation and just null out who invited them.
        await db.execute("UPDATE invitations SET invited_by = NULL WHERE invited_by = ?", (user_id,))
        await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        await db.commit()
