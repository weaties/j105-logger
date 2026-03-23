"""Tests for WebSocket live push (/ws/live)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from helmlog.nmea2000 import HeadingRecord
from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage


@pytest.mark.asyncio
async def test_ws_connect_and_receive_snapshot(storage: Storage) -> None:
    """WebSocket client receives an initial instrument snapshot on connect."""
    app = create_app(storage)
    with TestClient(app) as client, client.websocket_connect("/ws/live") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "instruments"
        assert "data" in msg


@pytest.mark.asyncio
async def test_ws_disconnect_cleanup(storage: Storage) -> None:
    """Disconnected clients are removed from the broadcast set."""
    app = create_app(storage)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/live") as ws:
            ws.receive_json()  # snapshot
        # After disconnect, broadcast set should be empty
        assert len(app.state.ws_clients) == 0


@pytest.mark.asyncio
async def test_live_callback_is_registered(storage: Storage) -> None:
    """create_app() registers a live update callback on storage."""
    create_app(storage)
    assert storage._on_live_update is not None


@pytest.mark.asyncio
async def test_live_callback_fires_on_update(storage: Storage) -> None:
    """Storage.update_live() invokes the registered callback with instrument data."""
    received: list[dict] = []  # type: ignore[type-arg]

    def cb(data: dict) -> None:  # type: ignore[type-arg]
        received.append(data)

    storage.set_live_callback(cb)
    record = HeadingRecord(
        pgn=127250,
        source_addr=0,
        timestamp=datetime.now(UTC),
        heading_deg=245.3,
        deviation_deg=None,
        variation_deg=None,
    )
    storage.update_live(record)

    assert len(received) == 1
    assert received[0]["heading_deg"] == 245.3


@pytest.mark.asyncio
async def test_polling_endpoints_still_work(storage: Storage) -> None:
    """Existing HTTP polling endpoints continue to work alongside WebSocket."""
    import httpx

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/instruments")
        assert resp.status_code == 200
        data = resp.json()
        assert "heading_deg" in data
