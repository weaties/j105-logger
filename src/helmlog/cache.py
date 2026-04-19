"""Web response cache (#594).

Two tiers sharing one process-scoped owner:

* **T1** — in-process dict with a monotonic-clock TTL. Cheap reads for small
  hot metadata (session list page, crew, boat settings). Lives for the life
  of the process; cleared by explicit invalidation or TTL.
* **T2** — a SQLite blob table (``web_cache``). Large immutable results
  (session summary, track GeoJSON, wind field). Content-addressed by a
  ``data_hash`` derived from the source race row so writes to the race
  naturally invalidate the entry via the storage mutation hook.

The cache is best-effort: any failure to read or write is logged and
swallowed — a broken cache must never fail a user request (EARS Req. 3).
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from loguru import logger

if TYPE_CHECKING:
    import aiosqlite

    from helmlog.storage import Storage


MAX_CACHE_ROWS_DEFAULT: int = 1000


class RaceCache(Protocol):
    """Protocol the storage layer sees when a cache is bound."""

    async def invalidate(self, race_id: int) -> None: ...


def compute_race_data_hash(
    *,
    race_id: int,
    start_utc: datetime,
    end_utc: datetime | None,
    row_count: int,
) -> str:
    """Stable 16-char hex digest of the race's cache-relevant inputs.

    A cache entry is valid exactly as long as the hash matches the current
    race row. No TTL is needed: any mutation to the underlying race flows
    through the storage invalidation hook, which drops all entries for the
    race id.
    """
    payload = {
        "race_id": race_id,
        "start_utc": start_utc.isoformat(),
        "end_utc": end_utc.isoformat() if end_utc is not None else None,
        "row_count": row_count,
    }
    raw = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


async def resolve_race_data_hash(storage: Storage, race_id: int) -> str | None:
    """Read the race row + instrument row counts and return its data_hash.

    Returns ``None`` when the race doesn't exist. Row count comes from
    ``positions`` (the dominant data source for summary/track/wind-field)
    filtered by ``race_id`` if tagged, else by the race time window. A
    completed race's hash is stable; an open race's hash changes as data
    streams in, which keeps ETag revalidation meaningful for the home page.
    """
    db = storage._conn()
    cur = await db.execute("SELECT start_utc, end_utc FROM races WHERE id = ?", (race_id,))
    race = await cur.fetchone()
    if race is None:
        return None
    start_iso = str(race["start_utc"])
    end_iso = str(race["end_utc"]) if race["end_utc"] is not None else None

    # Row count: prefer race_id tagging, fall back to time window.
    rid_cur = await db.execute(
        "SELECT COUNT(*) AS cnt FROM positions WHERE race_id = ?", (race_id,)
    )
    rid_row = await rid_cur.fetchone()
    n_tagged = int(rid_row["cnt"]) if rid_row else 0
    if n_tagged > 0:
        row_count = n_tagged
    else:
        window_end = end_iso or start_iso
        win_cur = await db.execute(
            "SELECT COUNT(*) AS cnt FROM positions WHERE ts >= ? AND ts <= ?",
            (start_iso, window_end),
        )
        win_row = await win_cur.fetchone()
        row_count = int(win_row["cnt"]) if win_row else 0

    start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00")) if end_iso else None
    return compute_race_data_hash(
        race_id=race_id, start_utc=start_dt, end_utc=end_dt, row_count=row_count
    )


class _T1Entry:
    __slots__ = ("expires_at", "value")

    def __init__(self, value: object, expires_at: float) -> None:
        self.value = value
        self.expires_at = expires_at


def _t1_race_key(family: str, race_id: int) -> str:
    return f"{family}::race={race_id}"


class WebCache:
    """Two-tier process-scoped cache shared by web routes.

    Storage binds a single instance via ``Storage.bind_race_cache`` so that
    ``races`` INSERT/UPDATE/DELETE paths can call ``invalidate(race_id)``
    in-transaction (EARS Req. 2).
    """

    def __init__(self, storage: Storage, *, max_rows: int = MAX_CACHE_ROWS_DEFAULT) -> None:
        self._storage = storage
        self._max_rows = max_rows
        self._t1: dict[str, _T1Entry] = {}

    # ------------------------------------------------------------------
    # T1 — process dict with TTL
    # ------------------------------------------------------------------

    def t1_get(self, key: str) -> object | None:
        entry = self._t1.get(key)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            self._t1.pop(key, None)
            return None
        return entry.value

    def t1_put(self, key: str, value: object, *, ttl_seconds: float) -> None:
        self._t1[key] = _T1Entry(value, time.monotonic() + ttl_seconds)

    def t1_get_for_race(self, family: str, *, race_id: int) -> object | None:
        return self.t1_get(_t1_race_key(family, race_id))

    def t1_put_for_race(
        self, family: str, *, race_id: int, value: object, ttl_seconds: float
    ) -> None:
        self.t1_put(_t1_race_key(family, race_id), value, ttl_seconds=ttl_seconds)

    def t1_invalidate_family(self, family: str) -> None:
        """Drop every T1 entry whose key starts with ``family:`` or is race-keyed under it."""
        prefix_a = f"{family}:"
        prefix_b = f"{family}::race="
        for key in [k for k in self._t1 if k.startswith(prefix_a) or k.startswith(prefix_b)]:
            self._t1.pop(key, None)

    # ------------------------------------------------------------------
    # T2 — SQLite blob cache
    # ------------------------------------------------------------------

    async def t2_get(self, key_family: str, *, race_id: int, data_hash: str) -> object | None:
        try:
            row = await self._read_row(key_family, race_id)
        except Exception as exc:  # pragma: no cover - exercised via monkeypatch
            logger.warning("web cache read failed ({}): {}", key_family, exc)
            return None
        if row is None:
            return None
        if row["data_hash"] != data_hash:
            return None
        try:
            decoded: object = json.loads(row["blob"])
            return decoded
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning(
                "web cache blob decode failed for {} race={}: {} — evicting",
                key_family,
                race_id,
                exc,
            )
            await self._delete_row(key_family, race_id)
            return None

    async def t2_put(
        self,
        key_family: str,
        *,
        race_id: int,
        data_hash: str,
        value: object,
        _now: datetime | None = None,
    ) -> None:
        try:
            blob = json.dumps(value, default=str)
        except (TypeError, ValueError) as exc:
            logger.warning("web cache encode failed for {}: {}", key_family, exc)
            return
        created = (_now or datetime.now(UTC)).isoformat()
        try:
            await self._write_row(key_family, race_id, data_hash, blob, created)
            await self._evict_over_cap()
        except Exception as exc:
            logger.warning("web cache write failed ({}): {}", key_family, exc)

    # ------------------------------------------------------------------
    # Invalidation — called by storage race-mutation paths
    # ------------------------------------------------------------------

    async def invalidate(self, race_id: int) -> None:
        # T1 — drop any race-keyed entries for this id
        suffix = f"::race={race_id}"
        for key in [k for k in self._t1 if k.endswith(suffix)]:
            self._t1.pop(key, None)
        # T2 — drop every row for this race
        try:
            db = self._storage._conn()  # noqa: SLF001
            await db.execute("DELETE FROM web_cache WHERE race_id = ?", (race_id,))
            await db.commit()
        except Exception as exc:
            logger.warning("web cache invalidate failed race={}: {}", race_id, exc)

    # ------------------------------------------------------------------
    # Internal DB helpers (patched in tests to simulate failures)
    # ------------------------------------------------------------------

    async def _read_row(self, key_family: str, race_id: int) -> aiosqlite.Row | None:
        db = self._storage._conn()  # noqa: SLF001
        cur = await db.execute(
            "SELECT data_hash, blob FROM web_cache WHERE key_family = ? AND race_id = ?",
            (key_family, race_id),
        )
        return await cur.fetchone()

    async def _write_row(
        self,
        key_family: str,
        race_id: int,
        data_hash: str,
        blob: str,
        created_utc: str,
    ) -> None:
        db = self._storage._conn()  # noqa: SLF001
        await db.execute(
            "INSERT INTO web_cache (key_family, race_id, data_hash, blob, created_utc)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(key_family, race_id) DO UPDATE SET"
            "   data_hash = excluded.data_hash,"
            "   blob = excluded.blob,"
            "   created_utc = excluded.created_utc",
            (key_family, race_id, data_hash, blob, created_utc),
        )
        await db.commit()

    async def _delete_row(self, key_family: str, race_id: int) -> None:
        db = self._storage._conn()  # noqa: SLF001
        await db.execute(
            "DELETE FROM web_cache WHERE key_family = ? AND race_id = ?",
            (key_family, race_id),
        )
        await db.commit()

    async def _evict_over_cap(self) -> None:
        db = self._storage._conn()  # noqa: SLF001
        cur = await db.execute("SELECT COUNT(*) AS n FROM web_cache")
        row = await cur.fetchone()
        if row is None:
            return
        total = int(row["n"])
        excess = total - self._max_rows
        if excess <= 0:
            return
        await db.execute(
            "DELETE FROM web_cache WHERE rowid IN ("
            "  SELECT rowid FROM web_cache ORDER BY created_utc ASC LIMIT ?"
            ")",
            (excess,),
        )
        await db.commit()


__all__ = [
    "MAX_CACHE_ROWS_DEFAULT",
    "RaceCache",
    "WebCache",
    "compute_race_data_hash",
]
