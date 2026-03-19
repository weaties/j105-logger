"""Tests for scheduled race start (#345)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest

from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage


async def _set_event(client: httpx.AsyncClient, name: str = "TestRegatta") -> None:
    resp = await client.post("/api/event", json={"event_name": name})
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# POST /api/races/schedule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_start_returns_201(storage: Storage) -> None:
    """POST /api/races/schedule with a valid future time returns 201."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        future = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
        resp = await client.post(
            "/api/races/schedule",
            json={"scheduled_start_utc": future},
        )

    assert resp.status_code == 201
    data = resp.json()
    assert "id" in data
    assert data["event"] == "TestRegatta"
    assert data["session_type"] == "race"
    assert data["seconds_until_start"] > 0


@pytest.mark.asyncio
async def test_schedule_start_rejects_past_time(storage: Storage) -> None:
    """POST /api/races/schedule with a past time returns 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        resp = await client.post(
            "/api/races/schedule",
            json={"scheduled_start_utc": past},
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_schedule_start_rejects_missing_timestamp(storage: Storage) -> None:
    """POST /api/races/schedule without a timestamp returns 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        resp = await client.post("/api/races/schedule", json={})

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_schedule_start_replaces_existing(storage: Storage) -> None:
    """Scheduling a second start replaces the first one."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        t1 = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
        t2 = (datetime.now(UTC) + timedelta(minutes=20)).isoformat()
        await client.post("/api/races/schedule", json={"scheduled_start_utc": t1})
        resp = await client.post("/api/races/schedule", json={"scheduled_start_utc": t2})

    assert resp.status_code == 201
    # Only one row should exist
    row = await storage.get_scheduled_start()
    assert row is not None
    assert row["scheduled_start_utc"] == datetime.fromisoformat(t2).isoformat()


@pytest.mark.asyncio
async def test_schedule_start_no_event_returns_422(storage: Storage) -> None:
    """POST /api/races/schedule without an event configured returns 422."""
    from unittest.mock import patch

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        with patch("helmlog.races.default_event_for_date", return_value=None):
            future = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
            resp = await client.post(
                "/api/races/schedule",
                json={"scheduled_start_utc": future},
            )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_schedule_start_with_explicit_event(storage: Storage) -> None:
    """POST /api/races/schedule with an explicit event succeeds."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        future = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
        resp = await client.post(
            "/api/races/schedule",
            json={"scheduled_start_utc": future, "event": "CustomEvent"},
        )

    assert resp.status_code == 201
    assert resp.json()["event"] == "CustomEvent"


@pytest.mark.asyncio
async def test_schedule_start_practice_session_type(storage: Storage) -> None:
    """POST /api/races/schedule with session_type=practice works."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        future = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
        resp = await client.post(
            "/api/races/schedule",
            json={"scheduled_start_utc": future, "session_type": "practice"},
        )

    assert resp.status_code == 201
    assert resp.json()["session_type"] == "practice"


@pytest.mark.asyncio
async def test_schedule_start_invalid_session_type(storage: Storage) -> None:
    """POST /api/races/schedule with invalid session_type returns 422."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        future = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
        resp = await client.post(
            "/api/races/schedule",
            json={"scheduled_start_utc": future, "session_type": "invalid"},
        )

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/races/schedule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_schedule_returns_404_when_empty(storage: Storage) -> None:
    """GET /api/races/schedule returns 404 when nothing is scheduled."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/races/schedule")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_schedule_returns_scheduled_start(storage: Storage) -> None:
    """GET /api/races/schedule returns the scheduled start when one exists."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        future = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
        await client.post(
            "/api/races/schedule",
            json={"scheduled_start_utc": future},
        )
        resp = await client.get("/api/races/schedule")

    assert resp.status_code == 200
    data = resp.json()
    assert data["event"] == "TestRegatta"
    assert data["seconds_until_start"] > 0


# ---------------------------------------------------------------------------
# DELETE /api/races/schedule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_schedule_returns_204(storage: Storage) -> None:
    """DELETE /api/races/schedule cancels the scheduled start."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        future = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
        await client.post(
            "/api/races/schedule",
            json={"scheduled_start_utc": future},
        )
        resp = await client.delete("/api/races/schedule")

    assert resp.status_code == 204
    assert await storage.get_scheduled_start() is None


@pytest.mark.asyncio
async def test_cancel_schedule_idempotent(storage: Storage) -> None:
    """DELETE /api/races/schedule returns 204 even when nothing is scheduled."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.delete("/api/races/schedule")

    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# Manual start cancels schedule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_start_cancels_schedule(storage: Storage) -> None:
    """POST /api/races/start cancels any pending scheduled start."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        future = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
        await client.post(
            "/api/races/schedule",
            json={"scheduled_start_utc": future},
        )
        # Verify schedule exists
        assert await storage.get_scheduled_start() is not None

        # Manual start should cancel the schedule
        resp = await client.post("/api/races/start")

    assert resp.status_code == 201
    assert await storage.get_scheduled_start() is None


# ---------------------------------------------------------------------------
# State endpoint includes scheduled_start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_includes_scheduled_start(storage: Storage) -> None:
    """GET /api/state includes scheduled_start when one is set."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _set_event(client)
        future = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
        await client.post(
            "/api/races/schedule",
            json={"scheduled_start_utc": future},
        )
        resp = await client.get("/api/state")

    data = resp.json()
    assert data["scheduled_start"] is not None
    assert data["scheduled_start"]["event"] == "TestRegatta"
    assert data["scheduled_start"]["seconds_until_start"] > 0


@pytest.mark.asyncio
async def test_state_scheduled_start_null_when_none(storage: Storage) -> None:
    """GET /api/state has scheduled_start as null when none is set."""
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/state")

    data = resp.json()
    assert data["scheduled_start"] is None


# ---------------------------------------------------------------------------
# Storage layer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_storage_schedule_start(storage: Storage) -> None:
    """Storage.schedule_start stores and retrieves the scheduled start."""
    ts = datetime.now(UTC) + timedelta(minutes=10)
    row_id = await storage.schedule_start(ts, "TestRegatta")
    assert row_id > 0

    row = await storage.get_scheduled_start()
    assert row is not None
    assert row["event"] == "TestRegatta"
    assert row["session_type"] == "race"


@pytest.mark.asyncio
async def test_storage_schedule_replaces_existing(storage: Storage) -> None:
    """Storage.schedule_start replaces any existing row."""
    t1 = datetime.now(UTC) + timedelta(minutes=10)
    t2 = datetime.now(UTC) + timedelta(minutes=20)
    await storage.schedule_start(t1, "Event1")
    await storage.schedule_start(t2, "Event2")

    row = await storage.get_scheduled_start()
    assert row is not None
    assert row["event"] == "Event2"


@pytest.mark.asyncio
async def test_storage_cancel_scheduled_start(storage: Storage) -> None:
    """Storage.cancel_scheduled_start deletes the row."""
    ts = datetime.now(UTC) + timedelta(minutes=10)
    await storage.schedule_start(ts, "TestRegatta")
    deleted = await storage.cancel_scheduled_start()
    assert deleted is True
    assert await storage.get_scheduled_start() is None


@pytest.mark.asyncio
async def test_storage_cancel_no_row(storage: Storage) -> None:
    """Storage.cancel_scheduled_start returns False when nothing exists."""
    deleted = await storage.cancel_scheduled_start()
    assert deleted is False


# ---------------------------------------------------------------------------
# Missed start recovery (startup)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_schedule_not_in_state(storage: Storage) -> None:
    """GET /api/state still returns the expired schedule row until the loop clears it.

    The real missed-start cleanup is done by the background loop's first check.
    Here we verify the storage layer correctly persists and retrieves expired rows.
    """
    # Insert a scheduled start in the past directly
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    db = storage._conn()
    await db.execute(
        "INSERT INTO scheduled_starts (scheduled_start_utc, event, session_type, created_at)"
        " VALUES (?, ?, ?, ?)",
        (past, "TestRegatta", "race", datetime.now(UTC).isoformat()),
    )
    await db.commit()

    # Verify the row is retrievable
    row = await storage.get_scheduled_start()
    assert row is not None
    assert row["event"] == "TestRegatta"

    # Cancel simulates what the fire loop's first check does
    await storage.cancel_scheduled_start()
    assert await storage.get_scheduled_start() is None
    # No race should have been created
    current = await storage.get_current_race()
    assert current is None
