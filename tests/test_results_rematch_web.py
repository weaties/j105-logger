"""Test the /api/results/regattas/{id}/rematch endpoint (#520)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio

from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage


@pytest_asyncio.fixture
async def admin_client(  # type: ignore[misc]
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> httpx.AsyncClient:
    monkeypatch.setenv("AUTH_DISABLED", "true")
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_rematch_links_imported_race_to_new_local_session(
    storage: Storage, admin_client: httpx.AsyncClient
) -> None:
    """Rematch backfills local_session_id for imported races created before the local session."""
    db = storage._conn()
    now = datetime.now(UTC).isoformat()

    # Insert a regatta + an imported race with no local link.
    cur = await db.execute(
        "INSERT INTO regattas (source, source_id, name, created_at)"
        " VALUES ('clubspot', 'rg1', 'Test Regatta', ?)",
        (now,),
    )
    regatta_id = cur.lastrowid

    race_date = "2026-04-09"
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, "
        "session_type, regatta_id, source, source_id) "
        "VALUES (?, ?, ?, ?, ?, 'race', ?, 'clubspot', 'rc1')",
        ("Race 1", "J/105", 1, race_date, race_date, regatta_id),
    )
    cur = await db.execute("SELECT last_insert_rowid()")
    (imported_id,) = await cur.fetchone()  # type: ignore[misc]

    # Now create a local session on that date.
    local_start = datetime.fromisoformat(race_date + "T18:00:00+00:00")
    await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, session_type)"
        " VALUES (?, ?, ?, ?, ?, 'race')",
        ("Local Wed", "Local", 1, race_date, local_start.isoformat()),
    )
    cur = await db.execute("SELECT last_insert_rowid()")
    (local_id,) = await cur.fetchone()  # type: ignore[misc]
    await db.commit()

    # Pre-condition: imported race has no link.
    cur = await db.execute("SELECT local_session_id FROM races WHERE id = ?", (imported_id,))
    assert (await cur.fetchone())[0] is None

    # Rematch.
    resp = await admin_client.post(f"/api/results/regattas/{regatta_id}/rematch")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["races_checked"] == 1
    assert body["linked"] == 1

    # Post-condition: link populated.
    cur = await db.execute("SELECT local_session_id FROM races WHERE id = ?", (imported_id,))
    assert (await cur.fetchone())[0] == local_id


@pytest.mark.asyncio
async def test_rematch_unknown_regatta_returns_404(
    storage: Storage, admin_client: httpx.AsyncClient
) -> None:
    resp = await admin_client.post("/api/results/regattas/9999/rematch")
    assert resp.status_code == 404
