"""Settings repository."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .base import BaseRepository

if TYPE_CHECKING:
    from . import Storage


class SettingsRepository(BaseRepository):
    """Manages system and user-level settings."""

    async def get_setting(self, key: str) -> str | None:
        """Return the stored value for *key*, or None if not set."""
        cur = await self._read_conn().execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (key,),
        )
        row = await cur.fetchone()
        return row[0] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        """Upsert a setting value."""
        db = self._conn()
        now = datetime.now(UTC).isoformat()
        await db.execute(
            "INSERT INTO app_settings (key, value, updated_at)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
            (key, value, now),
        )
        await db.commit()

    async def delete_setting(self, key: str) -> bool:
        """Delete a setting by key. Returns True if found."""
        db = self._conn()
        cur = await db.execute("DELETE FROM app_settings WHERE key = ?", (key,))
        await db.commit()
        return cur.rowcount > 0

    async def get_control_names(self, conn: Any | None = None) -> frozenset[str]:
        """Return the set of all control names (for validation)."""
        db = conn if conn is not None else self._read_conn()
        cur = await db.execute("SELECT name FROM controls")
        rows = await cur.fetchall()
        return frozenset(r["name"] for r in rows)

    async def create_boat_settings(
        self,
        race_id: int | None,
        entries: list[dict[str, str]],
        source: str,
        extraction_run_id: int | None = None,
    ) -> list[int]:
        """Insert one or more boat setting entries."""
        db = self._conn()
        valid_names = await self.get_control_names(conn=db)
        
        now = datetime.now(UTC).isoformat()
        ids: list[int] = []
        for entry in entries:
            param = entry["parameter"]
            if param not in valid_names:
                raise ValueError(f"Unknown parameter: {param!r}")
            cur = await db.execute(
                "INSERT INTO boat_settings"
                " (race_id, ts, parameter, value, source, extraction_run_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (race_id, entry["ts"], param, entry["value"], source, extraction_run_id, now),
            )
            if cur.lastrowid is not None:
                ids.append(cur.lastrowid)
        await db.commit()
        return ids

    async def add_control(
        self,
        name: str,
        label: str,
        unit: str = "",
        input_type: str = "number",
        category: str = "sail_controls",
        sort_order: int = 0,
        preset_values: str | None = None,
    ) -> int:
        """Add a control. Returns the new row id."""
        db = self._conn()
        cur = await db.execute(
            "INSERT INTO controls"
            " (name, label, unit, input_type, category, sort_order, preset_values)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, label, unit, input_type, category, sort_order, preset_values),
        )
        await db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid


async def get_effective_setting(storage: Storage, key: str, default: str = "") -> str:
    """Return the effectively active value for *key*.

    Priority:
    1. Database (app_settings table)
    2. Environment variable (uppercase key)
    3. Provided default value
    """
    val = await storage.settings.get_setting(key)
    if val is not None:
        return val
    return os.environ.get(key.upper(), default)
