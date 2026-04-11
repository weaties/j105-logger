"""Base repository class for domain-specific storage logic."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import DatabaseEngine


class BaseRepository:
    """Provides a shared DatabaseEngine to domain-specific repositories."""

    def __init__(self, engine: DatabaseEngine) -> None:
        self._engine = engine

    def _conn(self):
        """Returns the primary write connection."""
        return self._engine.write_conn()

    def _read_conn(self):
        """Returns the read-only connection."""
        return self._engine.read_conn()
