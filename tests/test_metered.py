"""Tests for metered-connection mode and bandwidth logging (#403)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio

from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> httpx.AsyncClient:  # type: ignore[misc]
    """Authenticated admin client (auth disabled via env)."""
    monkeypatch.setenv("AUTH_DISABLED", "true")
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# METERED flag tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metered_false_enables_external_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    """METERED=false (default) → external_data_should_fetch returns True."""
    monkeypatch.delenv("METERED", raising=False)
    from helmlog.external import external_data_should_fetch

    assert external_data_should_fetch() is True


@pytest.mark.asyncio
async def test_metered_true_disables_external_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    """METERED=true → external_data_should_fetch returns False."""
    monkeypatch.setenv("METERED", "true")
    from helmlog.external import external_data_should_fetch

    assert external_data_should_fetch() is False


@pytest.mark.asyncio
async def test_metered_true_still_allows_explicit_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    """EXTERNAL_DATA_ENABLED=false takes precedence regardless of METERED."""
    monkeypatch.setenv("EXTERNAL_DATA_ENABLED", "false")
    monkeypatch.delenv("METERED", raising=False)
    from helmlog.external import external_data_should_fetch

    assert external_data_should_fetch() is False


# ---------------------------------------------------------------------------
# bandwidth_log table tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bandwidth_log_table_exists(storage: Storage) -> None:
    """Schema v51 creates bandwidth_log table."""
    rows = await storage._db.execute_fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='bandwidth_log'"
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_write_bandwidth_sample(storage: Storage) -> None:
    """write_bandwidth_sample persists a row."""
    ts = datetime(2026, 3, 22, 12, 0, 0, tzinfo=UTC)
    await storage.write_bandwidth_sample(
        timestamp=ts,
        interface="wlan0",
        bytes_sent=1000,
        bytes_recv=5000,
    )
    rows = await storage._db.execute_fetchall("SELECT * FROM bandwidth_log")
    assert len(rows) == 1
    assert rows[0]["interface"] == "wlan0"
    assert rows[0]["bytes_sent"] == 1000
    assert rows[0]["bytes_recv"] == 5000


@pytest.mark.asyncio
async def test_get_bandwidth_summary_empty(storage: Storage) -> None:
    """get_bandwidth_summary returns zeros when no data."""
    result = await storage.get_bandwidth_summary(hours=24)
    assert result == []


@pytest.mark.asyncio
async def test_get_bandwidth_summary_with_data(storage: Storage) -> None:
    """get_bandwidth_summary returns per-interface totals."""
    ts = datetime(2026, 3, 22, 12, 0, 0, tzinfo=UTC)
    await storage.write_bandwidth_sample(
        timestamp=ts, interface="wlan0", bytes_sent=1000, bytes_recv=5000
    )
    await storage.write_bandwidth_sample(
        timestamp=ts, interface="wlan0", bytes_sent=2000, bytes_recv=8000
    )
    await storage.write_bandwidth_sample(
        timestamp=ts, interface="eth0", bytes_sent=500, bytes_recv=200
    )
    result = await storage.get_bandwidth_summary(hours=24)
    by_iface = {r["interface"]: r for r in result}
    assert by_iface["wlan0"]["total_sent"] == 3000
    assert by_iface["wlan0"]["total_recv"] == 13000
    assert by_iface["eth0"]["total_sent"] == 500


# ---------------------------------------------------------------------------
# /api/bandwidth endpoint tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_bandwidth_empty(client: httpx.AsyncClient) -> None:
    """GET /api/bandwidth returns empty list when no data."""
    resp = await client.get("/api/bandwidth")
    assert resp.status_code == 200
    data = resp.json()
    assert data["interfaces"] == []


@pytest.mark.asyncio
async def test_api_bandwidth_with_data(client: httpx.AsyncClient, storage: Storage) -> None:
    """GET /api/bandwidth returns per-interface totals."""
    ts = datetime(2026, 3, 22, 12, 0, 0, tzinfo=UTC)
    await storage.write_bandwidth_sample(
        timestamp=ts, interface="wlan0", bytes_sent=1024, bytes_recv=4096
    )
    resp = await client.get("/api/bandwidth")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["interfaces"]) == 1
    assert data["interfaces"][0]["interface"] == "wlan0"
    assert data["interfaces"][0]["total_sent"] == 1024
    assert data["interfaces"][0]["total_recv"] == 4096
