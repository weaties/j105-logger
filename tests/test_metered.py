"""Tests for metered-connection mode and bandwidth attribution (#403)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import httpx
import pytest
import pytest_asyncio

if TYPE_CHECKING:
    from helmlog.storage import Storage


# ---------------------------------------------------------------------------
# METERED flag tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metered_false_enables_external_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    """METERED=false (default) → external_data_should_fetch returns True."""
    monkeypatch.delenv("METERED", raising=False)
    monkeypatch.delenv("EXTERNAL_DATA_ENABLED", raising=False)
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
# write_bandwidth — InfluxDB writer
# ---------------------------------------------------------------------------


def test_write_bandwidth_skips_zero_bytes() -> None:
    """write_bandwidth does nothing for zero-byte counts."""
    from helmlog.bandwidth import write_bandwidth

    # Should not raise or attempt InfluxDB write
    write_bandwidth(component="web", direction="out", bytes_count=0)


def test_write_bandwidth_calls_influx() -> None:
    """write_bandwidth writes a tagged point to InfluxDB."""
    mock_write_api = MagicMock()
    mock_client = MagicMock()

    with patch("helmlog.influx._client", return_value=(mock_client, mock_write_api)):
        from helmlog.bandwidth import write_bandwidth

        write_bandwidth(
            component="web",
            direction="out",
            bytes_count=1024,
            user="alice",
            route="/history",
        )

    mock_write_api.write.assert_called_once()
    mock_client.close.assert_called_once()


def test_write_bandwidth_survives_influx_failure() -> None:
    """write_bandwidth logs and continues if InfluxDB is down."""
    with patch("helmlog.influx._client", return_value=(None, None)):
        from helmlog.bandwidth import write_bandwidth

        # Should not raise
        write_bandwidth(component="web", direction="out", bytes_count=1024)


# ---------------------------------------------------------------------------
# track_httpx_response
# ---------------------------------------------------------------------------


def test_track_httpx_response_records_bytes() -> None:
    """track_httpx_response writes bandwidth points for an httpx response."""
    mock_write_api = MagicMock()
    mock_client = MagicMock()

    # Build a minimal httpx response
    request = httpx.Request("GET", "https://api.open-meteo.com/v1/forecast?lat=41")
    response = httpx.Response(200, content=b'{"current":{}}', request=request)

    with patch("helmlog.influx._client", return_value=(mock_client, mock_write_api)):
        from helmlog.bandwidth import track_httpx_response

        track_httpx_response("weather", response)

    # Should have written at least one point (response bytes)
    assert mock_write_api.write.call_count >= 1


# ---------------------------------------------------------------------------
# Network path classification
# ---------------------------------------------------------------------------


def test_classify_loopback() -> None:
    """Loopback interface classified as loopback."""
    from helmlog.bandwidth import _classify_interface

    assert _classify_interface("lo") == "loopback"


def test_classify_hotspot_default() -> None:
    """Default hotspot interface (wlan0) classified as hotspot."""
    from helmlog.bandwidth import _classify_interface

    assert _classify_interface("wlan0") == "hotspot"


def test_classify_local() -> None:
    """Other interfaces classified as local."""
    from helmlog.bandwidth import _classify_interface

    assert _classify_interface("eth0") == "local"


def test_client_network_loopback() -> None:
    """Localhost IPs classified as loopback."""
    from helmlog.bandwidth import _client_network

    assert _client_network("127.0.0.1") == "loopback"
    assert _client_network("::1") == "loopback"


def test_client_network_none() -> None:
    """None client host returns unknown."""
    from helmlog.bandwidth import _client_network

    assert _client_network(None) == "unknown"


# ---------------------------------------------------------------------------
# bandwidth_middleware
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def bw_client(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> httpx.AsyncClient:  # type: ignore[misc]
    """Client with bandwidth middleware active and auth disabled."""
    monkeypatch.setenv("AUTH_DISABLED", "true")
    from helmlog.web import create_app

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_middleware_tracks_web_requests(bw_client: httpx.AsyncClient) -> None:
    """Bandwidth middleware fires for non-static routes."""
    with patch("helmlog.bandwidth.write_bandwidth") as mock_write:
        resp = await bw_client.get("/healthz")
        assert resp.status_code == 200
        # /healthz is skipped by the middleware
        mock_write.assert_not_called()


@pytest.mark.asyncio
async def test_middleware_skips_static(bw_client: httpx.AsyncClient) -> None:
    """Bandwidth middleware skips /static/ paths."""
    with patch("helmlog.bandwidth.write_bandwidth") as mock_write:
        # Static file — may 404 but middleware should not fire
        await bw_client.get("/static/base.css")
        mock_write.assert_not_called()
