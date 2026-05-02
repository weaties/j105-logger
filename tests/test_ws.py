"""Tests for WebSocket live push (/ws/live)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from helmlog.nmea2000 import HeadingRecord, PositionRecord
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
async def test_position_callback_fires_for_position_record(storage: Storage) -> None:
    """update_live(PositionRecord) routes to the position callback, not the
    instrument callback. Wire format is {ts, lat, lon, race_id}."""
    received: list[dict] = []  # type: ignore[type-arg]

    def cb(payload: dict) -> None:  # type: ignore[type-arg]
        received.append(payload)

    storage.set_position_callback(cb)
    record = PositionRecord(
        pgn=129025,
        source_addr=0,
        timestamp=datetime(2026, 5, 2, 16, 30, 0, tzinfo=UTC),
        latitude_deg=47.65,
        longitude_deg=-122.40,
    )
    storage.update_live(record)

    assert len(received) == 1
    assert received[0]["lat"] == 47.65
    assert received[0]["lon"] == -122.40
    assert received[0]["ts"].startswith("2026-05-02T16:30:00")


@pytest.mark.asyncio
async def test_position_broadcasts_throttled_to_1hz(storage: Storage) -> None:
    """Multiple position records inside the same second collapse to one
    broadcast — GPS at 5–10 Hz must not flood the wire."""
    received: list[dict] = []  # type: ignore[type-arg]

    def cb(payload: dict) -> None:  # type: ignore[type-arg]
        received.append(payload)

    storage.set_position_callback(cb)
    base = datetime(2026, 5, 2, 16, 30, 0, tzinfo=UTC)
    for i in range(5):
        storage.update_live(
            PositionRecord(
                pgn=129025,
                source_addr=0,
                timestamp=base.replace(microsecond=i * 100_000),
                latitude_deg=47.65 + i * 0.0001,
                longitude_deg=-122.40,
            )
        )
    # Five fixes within the same monotonic second → only the first reaches
    # the wire. The throttle is monotonic-clock-based so this assertion is
    # tight and not flaky.
    assert len(received) == 1


@pytest.mark.asyncio
async def test_position_record_does_not_fire_instrument_callback(storage: Storage) -> None:
    """A PositionRecord must not also trigger the instruments broadcast — it
    has no instrument fields and would just spam an unchanged snapshot."""
    inst_calls: list[dict] = []  # type: ignore[type-arg]
    pos_calls: list[dict] = []  # type: ignore[type-arg]

    storage.set_live_callback(lambda d: inst_calls.append(d))
    storage.set_position_callback(lambda p: pos_calls.append(p))

    storage.update_live(
        PositionRecord(
            pgn=129025,
            source_addr=0,
            timestamp=datetime(2026, 5, 2, 16, 31, 0, tzinfo=UTC),
            latitude_deg=47.65,
            longitude_deg=-122.40,
        )
    )

    assert len(pos_calls) == 1
    assert len(inst_calls) == 0


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
