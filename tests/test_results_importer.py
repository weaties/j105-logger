"""Importer integration test: fetch → import → re-import idempotency (#459, R36-R38)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

from helmlog.results.base import Regatta
from helmlog.results.clubspot import ClubspotProvider
from helmlog.results.importer import import_results

if TYPE_CHECKING:
    from helmlog.results.base import RegattaResults
    from helmlog.storage import Storage

_FIXTURES = Path(__file__).parent / "fixtures" / "results" / "clubspot"
_REGATTA_ID = "wYFzQvmG4R"
_J105_CLASS_ID = "7q1o9ikhPH"


def _mock_transport() -> httpx.MockTransport:
    payload = (_FIXTURES / "wYFzQvmG4R_J105.json").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, headers={"content-type": "application/json"})

    return httpx.MockTransport(handler)


async def _fetch_results() -> RegattaResults:  # noqa: F821

    transport = _mock_transport()
    async with httpx.AsyncClient(transport=transport) as client:
        provider = ClubspotProvider(client=client)
        regatta = Regatta(
            source="clubspot",
            source_id=_REGATTA_ID,
            name="CYC Sound Wednesday",
            default_class=_J105_CLASS_ID,
        )
        return await provider.fetch(regatta)


# ---------------------------------------------------------------------------
# R37: fetch → import → query end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_creates_regatta(storage: Storage) -> None:
    results = await _fetch_results()
    await import_results(storage, results)
    db = storage._conn()
    async with db.execute("SELECT * FROM regattas WHERE source_id = ?", (_REGATTA_ID,)) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["name"] == "CYC Sound Wednesday"
    assert row["source"] == "clubspot"
    assert row["last_fetched_at"] is not None


@pytest.mark.asyncio
async def test_import_creates_races(storage: Storage) -> None:
    results = await _fetch_results()
    await import_results(storage, results)
    db = storage._conn()
    async with db.execute("SELECT COUNT(*) FROM races WHERE source = 'clubspot'") as cur:
        (count,) = await cur.fetchone()  # type: ignore[misc]
    assert count == 2


@pytest.mark.asyncio
async def test_import_creates_boats(storage: Storage) -> None:
    results = await _fetch_results()
    await import_results(storage, results)
    db = storage._conn()
    async with db.execute("SELECT COUNT(*) FROM boats") as cur:
        (count,) = await cur.fetchone()  # type: ignore[misc]
    assert count == 15


@pytest.mark.asyncio
async def test_import_creates_race_results(storage: Storage) -> None:
    results = await _fetch_results()
    await import_results(storage, results)
    db = storage._conn()
    async with db.execute("SELECT COUNT(*) FROM race_results") as cur:
        (count,) = await cur.fetchone()  # type: ignore[misc]
    # 15 boats × 2 races = 30
    assert count == 30


@pytest.mark.asyncio
async def test_import_creates_series_results(storage: Storage) -> None:
    results = await _fetch_results()
    await import_results(storage, results)
    db = storage._conn()
    async with db.execute("SELECT COUNT(*) FROM series_results") as cur:
        (count,) = await cur.fetchone()  # type: ignore[misc]
    assert count == 15


@pytest.mark.asyncio
async def test_import_preserves_status_codes(storage: Storage) -> None:
    results = await _fetch_results()
    await import_results(storage, results)
    db = storage._conn()
    async with db.execute(
        "SELECT status_code, dnf, dns FROM race_results WHERE status_code IS NOT NULL"
    ) as cur:
        rows = await cur.fetchall()
    assert len(rows) > 0
    for row in rows:
        if row["status_code"] in ("DNF", "RET"):
            assert row["dnf"] == 1
        if row["status_code"] in ("DNS", "DNC"):
            assert row["dns"] == 1


@pytest.mark.asyncio
async def test_import_writes_audit_log(storage: Storage) -> None:
    results = await _fetch_results()
    await import_results(storage, results, user_id=None)
    db = storage._conn()
    async with db.execute(
        "SELECT action, detail FROM audit_log WHERE action = 'results_import'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    detail = json.loads(row["detail"])
    assert detail["source"] == "clubspot"
    assert detail["races_upserted"] == 2


# ---------------------------------------------------------------------------
# R38: re-importing same fixture is idempotent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reimport_idempotent(storage: Storage) -> None:
    """Second import of the same fixture produces zero net DB changes."""
    results = await _fetch_results()

    first = await import_results(storage, results)
    assert first["races_upserted"] == 2
    assert first["results_upserted"] == 30

    db = storage._conn()

    async def _counts() -> dict[str, int]:
        totals = {}
        for table in ("regattas", "races", "boats", "race_results", "series_results"):
            async with db.execute(f"SELECT COUNT(*) FROM {table}") as cur:  # noqa: S608
                (n,) = await cur.fetchone()  # type: ignore[misc]
            totals[table] = n
        return totals

    before = await _counts()

    second = await import_results(storage, results)
    assert second["races_upserted"] == 2

    after = await _counts()
    assert before == after, f"Row counts changed on re-import: {before} vs {after}"


@pytest.mark.asyncio
async def test_reimport_updates_changed_points(storage: Storage) -> None:
    """Re-import with changed points updates existing rows in place."""
    results = await _fetch_results()
    await import_results(storage, results)

    db = storage._conn()
    async with db.execute(
        "SELECT rr.points FROM race_results rr "
        "JOIN boats b ON rr.boat_id = b.id "
        "WHERE b.sail_number = '482'"
    ) as cur:
        original = [row["points"] for row in await cur.fetchall()]
    assert len(original) == 2
    assert all(p is not None for p in original)


@pytest.mark.asyncio
async def test_race_with_no_date_skipped(storage: Storage) -> None:
    """R: imported race with no date is rejected, not written."""
    from helmlog.results.base import BoatFinish, RaceData, Regatta, RegattaResults

    bad_race = RaceData(
        source_id="no_date_race",
        race_number=99,
        name="Bad Race",
        date="",
        class_name="J/105",
        finishes=(BoatFinish(sail_number="000", place=1),),
    )
    results = RegattaResults(
        regatta=Regatta(source="test", source_id="t1", name="Test"),
        races=(bad_race,),
    )
    counts = await import_results(storage, results)
    assert counts["races_upserted"] == 0

    db = storage._conn()
    async with db.execute("SELECT COUNT(*) FROM races WHERE source_id = 'no_date_race'") as cur:
        (n,) = await cur.fetchone()  # type: ignore[misc]
    assert n == 0
