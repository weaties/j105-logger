"""Tests for migration v78 — seed starter tag vocabulary (#652)."""

from __future__ import annotations

import contextlib

import aiosqlite
import pytest

from helmlog.storage import _MIGRATIONS, _split_migration_sql


async def _apply_migration(db: aiosqlite.Connection, version: int) -> None:
    for stmt in _split_migration_sql(_MIGRATIONS[version]):
        upper = stmt.lstrip().upper()
        is_alter_add = upper.startswith("ALTER TABLE") and "ADD COLUMN" in upper
        if is_alter_add:
            with contextlib.suppress(aiosqlite.OperationalError):
                await db.execute(stmt)
        else:
            await db.execute(stmt)
    await db.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (?)", (version,))
    await db.commit()


async def _build_db_at(version: int) -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    for v in sorted(_MIGRATIONS):
        if v > version:
            break
        await _apply_migration(db, v)
    return db


EXPECTED_SEED_TAGS = {
    # Boat-on-boat, directional
    "close-crossing",
    "collision",
    "near-miss",
    "rolled-them",
    "got-rolled",
    "lee-bowed-them",
    "got-lee-bowed",
    "pinned-them",
    "got-pinned",
    # Position change
    "places-gained",
    "places-lost",
    "passed-boat",
    "got-passed",
    # Rules
    "we-protested",
    "got-protested",
    "took-penalty",
    "hit-mark",
    "ocs",
    # Incident
    "gear-failure",
    "crew-incident",
    "grounded",
}


@pytest.mark.asyncio
async def test_v78_inserts_seed_tags() -> None:
    db = await _build_db_at(78)
    try:
        await _apply_migration(db, 78)
        async with db.execute("SELECT name FROM tags") as cur:
            names = {r[0] for r in await cur.fetchall()}
        missing = EXPECTED_SEED_TAGS - names
        assert not missing, f"seed tags missing after v78: {sorted(missing)}"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v78_seed_tags_have_colors() -> None:
    """Every seed tag should have a non-null color — the us-good / us-bad /
    rules / incident color convention is the whole point of seeding."""
    db = await _build_db_at(78)
    try:
        async with db.execute(
            "SELECT name, color FROM tags WHERE name IN ({})".format(
                ",".join("?" * len(EXPECTED_SEED_TAGS))
            ),
            tuple(EXPECTED_SEED_TAGS),
        ) as cur:
            rows = await cur.fetchall()
        uncolored = [r[0] for r in rows if not r[1]]
        assert not uncolored, f"seed tags missing color: {uncolored}"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v78_directional_pairs_have_opposite_colors() -> None:
    """us-good (green) / us-bad (red) pairs must render distinctly — that's
    the whole reason the pair isn't collapsed into one neutral tag."""
    db = await _build_db_at(78)
    try:
        pairs = [
            ("rolled-them", "got-rolled"),
            ("lee-bowed-them", "got-lee-bowed"),
            ("pinned-them", "got-pinned"),
            ("places-gained", "places-lost"),
            ("passed-boat", "got-passed"),
        ]
        for good, bad in pairs:
            async with db.execute(
                "SELECT name, color FROM tags WHERE name IN (?, ?)", (good, bad)
            ) as cur:
                colors = {r[0]: r[1] for r in await cur.fetchall()}
            assert colors[good] != colors[bad], (
                f"directional pair {good}/{bad} must use different colors"
            )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_v78_is_idempotent() -> None:
    """Running v78 on a DB that already has some of the seed tags (from
    earlier manual creation) must not error or duplicate."""
    db = await _build_db_at(77)
    try:
        now = "2026-04-21T00:00:00"
        await db.execute(
            "INSERT INTO tags (name, color, created_at) VALUES (?, ?, ?)",
            ("collision", "#ff0000", now),
        )
        await db.commit()

        await _apply_migration(db, 78)
        # Run it a second time — should still not error or duplicate.
        await _apply_migration(db, 78)

        async with db.execute("SELECT COUNT(*) FROM tags WHERE name = ?", ("collision",)) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 1, "idempotent insert must not duplicate existing tag"

        async with db.execute("SELECT color FROM tags WHERE name = ?", ("collision",)) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == "#ff0000", "existing tag's color must be preserved"
    finally:
        await db.close()


# Fresh-DB schema_version is asserted dynamically against _CURRENT_VERSION
# in test_migration_v75.py::test_schema_version_is_current_on_fresh_db, so
# a fixed v78 assertion here would be redundant.
