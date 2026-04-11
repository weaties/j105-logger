"""Database engine — manages shared aiosqlite connections."""

from __future__ import annotations

import aiosqlite
from loguru import logger


class DatabaseEngine:
    """Manages read/write aiosqlite connections with optional WAL mode."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._write_conn: aiosqlite.Connection | None = None
        self._read_conn: aiosqlite.Connection | None = None
        self._owns_write: bool = False
        self._owns_read: bool = False

    async def connect(self) -> None:
        """Establish connections and configure WAL mode."""
        if self._write_conn:
            return

        logger.debug("Database: connecting to {}", self.db_path)
        
        if self.db_path == ":memory:":
            # In-memory must use a single connection for absolute visibility.
            # We set _read_conn to None because test_storage_wal expects it to be None
            # for memory databases (it wants explicit single-connection behavior).
            self._write_conn = await aiosqlite.connect(":memory:")
            self._write_conn.row_factory = aiosqlite.Row
            await self._write_conn.execute("PRAGMA foreign_keys = ON")
            self._read_conn = None
            self._owns_write = True
            self._owns_read = False
            return

        # File-backed database
        self._write_conn = await aiosqlite.connect(self.db_path)
        self._write_conn.row_factory = aiosqlite.Row
        await self._write_conn.execute("PRAGMA journal_mode=WAL")
        await self._write_conn.execute("PRAGMA synchronous=NORMAL")
        await self._write_conn.execute("PRAGMA foreign_keys = ON")
        self._owns_write = True

        self._read_conn = await aiosqlite.connect(f"file:{self.db_path}?mode=ro", uri=True)
        self._read_conn.row_factory = aiosqlite.Row
        self._owns_read = True

    async def close(self) -> None:
        """Close connections ONLY if we own them."""
        if self._owns_write and self._write_conn:
            await self._write_conn.close()
            self._write_conn = None
            self._owns_write = False
        
        if self._owns_read and self._read_conn:
            await self._read_conn.close()
            self._read_conn = None
            self._owns_read = False

    def write_conn(self) -> aiosqlite.Connection:
        if not self._write_conn:
            raise RuntimeError("DatabaseEngine not connected")
        return self._write_conn

    def read_conn(self) -> aiosqlite.Connection:
        # Fallback to write connection if read connection is None (e.g. :memory:)
        return self._read_conn or self.write_conn()
