"""Tests for the Vakaros admin web routes (#458 cycle 5)."""

from __future__ import annotations

import math
import struct
from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from helmlog.storage import Storage
else:
    from pathlib import Path  # noqa: TC003  # runtime needed by pytest fixture types


def _build_minimal_vkx_bytes(ts_ms: int = 1_700_000_000_000) -> bytes:
    header = bytes([0xFF, 0x05, 0, 0, 0, 0, 0, 0])
    payload = struct.pack(
        "<Qiifffffff",
        ts_ms,
        round(47.68 / 1e-7),
        round(-122.41 / 1e-7),
        1.0,
        math.radians(0.0),
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
    )
    row = bytes([0x02]) + payload
    terminator = bytes([0xFE]) + struct.pack("<H", len(row))
    return header + row + terminator


@pytest.fixture
def inbox_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    inbox = tmp_path / "vakaros-inbox"
    inbox.mkdir()
    monkeypatch.setenv("AUTH_DISABLED", "true")
    monkeypatch.setenv("VAKAROS_INBOX_DIR", str(inbox))
    return inbox


@pytest.mark.asyncio
async def test_admin_vakaros_page_lists_inbox_files_and_sessions(
    storage: Storage, inbox_path: Path
) -> None:
    from helmlog.web import create_app

    (inbox_path / "alpha.vkx").write_bytes(b"x")
    (inbox_path / "bravo.vkx").write_bytes(b"x")
    (inbox_path / "readme.txt").write_bytes(b"ignored")

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/admin/vakaros")

    assert resp.status_code == 200
    body = resp.text
    assert "alpha.vkx" in body
    assert "bravo.vkx" in body
    assert "readme.txt" not in body
    # Empty state for the sessions list
    assert "No Vakaros sessions" in body or "vakaros_sessions" in body.lower()


@pytest.mark.asyncio
async def test_admin_vakaros_ingest_processes_valid_file(
    storage: Storage, inbox_path: Path
) -> None:
    from helmlog.web import create_app

    (inbox_path / "good.vkx").write_bytes(_build_minimal_vkx_bytes())

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/admin/vakaros/ingest",
            data={"filename": "good.vkx"},
            follow_redirects=False,
        )

    # Expect a redirect back to the admin page (PRG pattern).
    assert resp.status_code in (302, 303)
    assert resp.headers["location"].startswith("/admin/vakaros")

    # Original is gone, archived in processed/, DB has one row.
    assert not (inbox_path / "good.vkx").exists()
    assert (inbox_path / "processed" / "good.vkx").exists()

    db = storage._conn()
    cur = await db.execute("SELECT COUNT(*) AS n FROM vakaros_sessions")
    row = await cur.fetchone()
    assert row["n"] == 1


@pytest.mark.asyncio
async def test_admin_vakaros_ingest_moves_malformed_file_to_failed(
    storage: Storage, inbox_path: Path
) -> None:
    from helmlog.web import create_app

    (inbox_path / "junk.vkx").write_bytes(b"\x00\x00\x00")

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/admin/vakaros/ingest",
            data={"filename": "junk.vkx"},
            follow_redirects=False,
        )

    assert resp.status_code in (302, 303)
    assert (inbox_path / "failed" / "junk.vkx").exists()
    assert (inbox_path / "failed" / "junk.vkx.err").exists()

    db = storage._conn()
    cur = await db.execute("SELECT COUNT(*) AS n FROM vakaros_sessions")
    row = await cur.fetchone()
    assert row["n"] == 0


@pytest.mark.asyncio
async def test_vakaros_overlay_returns_404_for_unknown_race(
    storage: Storage, inbox_path: Path
) -> None:
    from helmlog.web import create_app

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/sessions/99999/vakaros-overlay")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_vakaros_overlay_returns_empty_when_no_match(
    storage: Storage, inbox_path: Path
) -> None:
    """A race that has no matched Vakaros session should return an empty payload."""
    from datetime import UTC, datetime

    from helmlog.web import create_app

    # Insert a race with no Vakaros session nearby.
    db = storage._conn()
    cur = await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc, session_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "Solo race",
            "evt",
            1,
            "2026-04-09",
            datetime(2026, 4, 9, 12, 0, tzinfo=UTC).isoformat(),
            datetime(2026, 4, 9, 13, 0, tzinfo=UTC).isoformat(),
            "race",
        ),
    )
    await db.commit()
    race_id = cur.lastrowid

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{race_id}/vakaros-overlay")
    assert resp.status_code == 200
    data = resp.json()
    assert data["matched"] is False
    assert data["line_positions"] == []
    assert data["race_events"] == []
    assert data["track"] is None


@pytest.mark.asyncio
async def test_vakaros_overlay_returns_full_payload_when_matched(
    storage: Storage, inbox_path: Path
) -> None:
    """When a race is matched to a Vakaros session, overlay returns track, line, events."""
    from datetime import UTC, datetime, timedelta

    from helmlog.vakaros import (
        LinePosition,
        LinePositionType,
        PositionRow,
        RaceTimerEvent,
        RaceTimerEventType,
        VakarosSession,
    )
    from helmlog.web import create_app

    t0 = datetime(2026, 4, 9, 12, 0, 0, tzinfo=UTC)
    session = VakarosSession(
        source_hash="f" * 64,
        source_file="test.vkx",
        start_utc=t0,
        end_utc=t0 + timedelta(minutes=30),
        positions=(
            PositionRow(
                timestamp=t0,
                latitude_deg=47.68,
                longitude_deg=-122.41,
                sog_mps=1.0,
                cog_deg=0.0,
                altitude_m=0.0,
                quat_w=1.0,
                quat_x=0.0,
                quat_y=0.0,
                quat_z=0.0,
            ),
            PositionRow(
                timestamp=t0 + timedelta(minutes=15),
                latitude_deg=47.681,
                longitude_deg=-122.411,
                sog_mps=2.0,
                cog_deg=45.0,
                altitude_m=0.0,
                quat_w=1.0,
                quat_x=0.0,
                quat_y=0.0,
                quat_z=0.0,
            ),
            PositionRow(
                timestamp=t0 + timedelta(minutes=30),
                latitude_deg=47.682,
                longitude_deg=-122.412,
                sog_mps=1.5,
                cog_deg=90.0,
                altitude_m=0.0,
                quat_w=1.0,
                quat_x=0.0,
                quat_y=0.0,
                quat_z=0.0,
            ),
        ),
        line_positions=(
            LinePosition(
                timestamp=t0,
                line_type=LinePositionType.PIN,
                latitude_deg=47.687,
                longitude_deg=-122.420,
            ),
            LinePosition(
                timestamp=t0,
                line_type=LinePositionType.BOAT,
                latitude_deg=47.687,
                longitude_deg=-122.416,
            ),
        ),
        race_events=(
            RaceTimerEvent(
                timestamp=t0 + timedelta(minutes=5),
                event_type=RaceTimerEventType.RACE_START,
                timer_value_s=0,
            ),
            RaceTimerEvent(
                timestamp=t0 + timedelta(minutes=28),
                event_type=RaceTimerEventType.RACE_END,
                timer_value_s=1380,
            ),
        ),
        winds=(),
    )
    vakaros_id = await storage.store_vakaros_session(session)

    # Create an overlapping race and match it.
    db = storage._conn()
    cur = await db.execute(
        "INSERT INTO races (name, event, race_num, date, start_utc, end_utc, session_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "Shilshole race",
            "evt",
            1,
            "2026-04-09",
            (t0 + timedelta(minutes=3)).isoformat(),
            (t0 + timedelta(minutes=27)).isoformat(),
            "race",
        ),
    )
    await db.commit()
    race_id = cur.lastrowid
    assert race_id is not None
    matched = await storage.match_vakaros_session_to_race(vakaros_id)
    assert matched == race_id

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{race_id}/vakaros-overlay")

    assert resp.status_code == 200
    data = resp.json()
    assert data["matched"] is True
    assert data["vakaros_session_id"] == vakaros_id

    # Track is a GeoJSON-ish LineString with 3 points.
    assert data["track"] is not None
    assert data["track"]["type"] == "Feature"
    assert data["track"]["geometry"]["type"] == "LineString"
    assert len(data["track"]["geometry"]["coordinates"]) == 3
    # [lon, lat] GeoJSON order
    assert data["track"]["geometry"]["coordinates"][0] == [-122.41, 47.68]

    # Line positions: one pin, one boat.
    lines_by_type = {lp["line_type"]: lp for lp in data["line_positions"]}
    assert set(lines_by_type.keys()) == {"pin", "boat"}
    assert lines_by_type["pin"]["latitude_deg"] == 47.687
    assert lines_by_type["boat"]["longitude_deg"] == -122.416

    # Race events: race_start + race_end.
    event_types = {e["event_type"] for e in data["race_events"]}
    assert "race_start" in event_types
    assert "race_end" in event_types


@pytest.mark.asyncio
async def test_admin_vakaros_ingest_rejects_path_traversal(
    storage: Storage, inbox_path: Path
) -> None:
    from helmlog.web import create_app

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/admin/vakaros/ingest",
            data={"filename": "../escape.vkx"},
            follow_redirects=False,
        )

    assert resp.status_code == 400
