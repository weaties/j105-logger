"""Tests for storage-boundary parsing of race start_utc / end_utc (#532).

Covers the _parse_utc helper decision table and ensures that races with
date-only or naive datetimes hydrate to tz-aware UTC datetimes, so that
downstream subtraction against datetime.now(UTC) never raises
``TypeError: can't subtract offset-naive and offset-aware datetimes``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from helmlog.storage import _parse_utc

if TYPE_CHECKING:
    from helmlog.storage import Storage


class TestParseUtc:
    """Decision table for _parse_utc (one case per row of the spec table)."""

    def test_none_returns_none(self) -> None:
        assert _parse_utc(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert _parse_utc("") is None

    def test_date_only_coerces_to_utc_midnight(self) -> None:
        """Date-only strings (10 chars) parse to midnight UTC, tz-aware."""
        got = _parse_utc("2026-04-13")
        assert got == datetime(2026, 4, 13, 0, 0, 0, tzinfo=UTC)
        assert got is not None and got.tzinfo is not None

    def test_naive_iso_datetime_coerced_to_utc(self) -> None:
        got = _parse_utc("2026-04-13T18:05:00")
        assert got == datetime(2026, 4, 13, 18, 5, 0, tzinfo=UTC)
        assert got is not None and got.tzinfo is not None

    def test_aware_utc_iso_datetime(self) -> None:
        got = _parse_utc("2026-04-13T18:05:00+00:00")
        assert got == datetime(2026, 4, 13, 18, 5, 0, tzinfo=UTC)

    def test_aware_offset_iso_datetime_converted_to_utc(self) -> None:
        got = _parse_utc("2026-04-13T11:05:00-07:00")
        assert got is not None
        assert got.tzinfo is not None
        # 11:05 PDT == 18:05 UTC
        assert got.astimezone(UTC) == datetime(2026, 4, 13, 18, 5, 0, tzinfo=UTC)

    def test_malformed_returns_none(self) -> None:
        assert _parse_utc("not a date") is None
        assert _parse_utc("2026-13-99") is None


class TestRowToRaceHydration:
    """_row_to_race must always produce a tz-aware start_utc."""

    @pytest.mark.asyncio
    async def test_date_only_start_utc_hydrates_aware(self, storage: Storage) -> None:
        """Regression for #532: bad row with date-only start_utc must not cause
        a naive-vs-aware TypeError when consumers do arithmetic."""
        db = storage._conn()
        await db.execute(
            "INSERT INTO races"
            " (name, event, race_num, date, start_utc, end_utc, session_type)"
            " VALUES"
            " ('Race 1 - Flying Sails', 'Flying Sails', 1, '2026-04-13',"
            "  '2026-04-13', '', 'race')"
        )
        await db.commit()

        races = await storage.list_races_for_date("2026-04-13")
        assert len(races) == 1
        r = races[0]
        # Must be tz-aware so arithmetic against datetime.now(UTC) works.
        assert r.start_utc.tzinfo is not None
        # Subtraction must not raise.
        (datetime.now(UTC) - r.start_utc).total_seconds()

    @pytest.mark.asyncio
    async def test_empty_end_utc_hydrates_none(self, storage: Storage) -> None:
        """Empty-string end_utc from the importer must become None."""
        db = storage._conn()
        await db.execute(
            "INSERT INTO races"
            " (name, event, race_num, date, start_utc, end_utc, session_type)"
            " VALUES"
            " ('Race 2', 'Flying Sails', 2, '2026-04-13',"
            "  '2026-04-13T12:00:00+00:00', '', 'race')"
        )
        await db.commit()

        races = await storage.list_races_for_date("2026-04-13")
        assert len(races) == 1
        assert races[0].end_utc is None


class TestMigrationV67Backfill:
    """v68 must rewrite date-only start_utc values to full ISO timestamps."""

    @pytest.mark.asyncio
    async def test_v68_rewrites_date_only_start_utc(self, storage: Storage) -> None:
        """Simulate a pre-fix dirty row (date-only start_utc, empty end_utc)
        and re-run the v68 UPDATEs to confirm they clean it up."""
        db = storage._conn()
        # Migrations already ran against the fresh schema. Insert a dirty row
        # as if the pre-fix importer had written it, then re-run the v68 SQL.
        await db.execute(
            "INSERT INTO races"
            " (name, event, race_num, date, start_utc, end_utc, session_type)"
            " VALUES ('Dirty Row', 'Flying Sails', 1, '2026-04-13',"
            "         '2026-04-13', '', 'race')"
        )
        await db.commit()

        await db.execute(
            "UPDATE races SET start_utc = start_utc || 'T00:00:00+00:00'"
            " WHERE length(start_utc) = 10"
        )
        await db.execute("UPDATE races SET end_utc = NULL WHERE end_utc = ''")
        await db.commit()

        cur = await db.execute("SELECT start_utc, end_utc FROM races WHERE name = 'Dirty Row'")
        row = await cur.fetchone()
        assert row is not None
        assert row["start_utc"] == "2026-04-13T00:00:00+00:00"
        assert row["end_utc"] is None


class TestGetCurrentRace:
    """get_current_race must only return genuinely open recording sessions."""

    @pytest.mark.asyncio
    async def test_skips_imported_race_with_end_utc(self, storage: Storage) -> None:
        """Imported results rows carry end_utc at insert time so they are
        never returned by get_current_race. Without this guarantee every
        imported row would show up as an open session on the home page."""
        db = storage._conn()
        await db.execute(
            "INSERT INTO races"
            " (name, event, race_num, date, start_utc, end_utc, session_type)"
            " VALUES"
            " ('Imported Race', 'Flying Sails', 1, '2026-04-13',"
            "  '2026-04-13T00:00:00+00:00', '2026-04-13T00:00:00+00:00', 'race')"
        )
        await db.commit()

        current = await storage.get_current_race()
        assert current is None

    @pytest.mark.asyncio
    async def test_returns_real_open_race(self, storage: Storage) -> None:
        """A race with a real start_utc and NULL end_utc is still returned."""
        db = storage._conn()
        await db.execute(
            "INSERT INTO races"
            " (name, event, race_num, date, start_utc, end_utc, session_type)"
            " VALUES"
            " ('Live Race', 'BC', 1, '2026-04-13',"
            "  '2026-04-13T18:05:00+00:00', NULL, 'race')"
        )
        await db.commit()

        current = await storage.get_current_race()
        assert current is not None
        assert current.name == "Live Race"
        assert current.start_utc.tzinfo is not None
