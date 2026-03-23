"""Bandwidth attribution — per-component, per-user traffic tracking (#403).

Writes tagged ``bandwidth`` points to InfluxDB so Grafana can show exactly
where every byte goes. Components tracked:

  web       — HTTP requests served to users (tagged by user + route)
  weather   — Open-Meteo API calls
  tides     — NOAA CO-OPS API calls
  deploy    — GitHub API calls for update checking
  signalk   — Signal K WebSocket frames (inbound from localhost)

Each point is tagged with ``network`` (hotspot / local / loopback / unknown)
based on which network interface the traffic would traverse, so Grafana can
separate cellular-metered traffic from free local traffic.

All writes are best-effort: if InfluxDB is unreachable, the error is logged
and the caller continues normally.
"""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    import httpx
    from starlette.requests import Request
    from starlette.responses import Response

# Interface name → network label. Set HOTSPOT_INTERFACE to match your setup.
# Default: wlan0 is the hotspot/cellular interface.
_HOTSPOT_IFACE = os.environ.get("HOTSPOT_INTERFACE", "wlan0")


def _classify_interface(iface: str) -> str:
    """Classify a network interface as hotspot, local, or loopback."""
    if iface == "lo" or iface.startswith("lo"):
        return "loopback"
    if iface == _HOTSPOT_IFACE:
        return "hotspot"
    return "local"


@lru_cache(maxsize=64)
def _resolve_route_interface(host: str) -> str:
    """Determine which interface a destination IP would route through.

    Uses ``ip route get <host>`` on Linux. Falls back to "unknown" on macOS
    or on error. Results are cached since routes rarely change mid-session.
    """
    try:
        result = subprocess.run(
            ["ip", "route", "get", host],
            capture_output=True,
            text=True,
            timeout=2.0,
        )  # noqa: S603
        if result.returncode == 0:
            # Output like: "8.8.8.8 via 192.168.1.1 dev wlan0 src 192.168.1.50"
            for token_idx, token in enumerate(result.stdout.split()):
                if token == "dev" and token_idx + 1 < len(result.stdout.split()):
                    return _classify_interface(result.stdout.split()[token_idx + 1])
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return "unknown"


def _client_network(client_host: str | None) -> str:
    """Classify an inbound client IP as hotspot, local, or loopback."""
    if not client_host:
        return "unknown"
    if client_host in ("127.0.0.1", "::1", "localhost"):
        return "loopback"
    # For inbound requests, we can check which interface the client's subnet
    # matches, but the simplest approach: use ip route get
    return _resolve_route_interface(client_host)


def write_bandwidth(
    *,
    component: str,
    direction: str,
    bytes_count: int,
    network: str = "unknown",
    user: str | None = None,
    route: str | None = None,
    detail: str | None = None,
) -> None:
    """Write a single bandwidth attribution point to InfluxDB.

    Args:
        component: Source component (web, weather, tides, deploy, signalk).
        direction: "in" (received by helmlog) or "out" (sent by helmlog).
        bytes_count: Number of bytes transferred.
        network: Network path — "hotspot", "local", "loopback", or "unknown".
        user: Authenticated user name (for web component).
        route: HTTP route path (for web component).
        detail: Extra detail (e.g., API endpoint URL).
    """
    if bytes_count <= 0:
        return

    try:
        from helmlog.influx import _client
    except ImportError:
        return

    client, write_api = _client()
    if write_api is None:
        return

    bucket = os.environ.get("INFLUX_BUCKET", "signalk")
    org = os.environ.get("INFLUX_ORG", "helmlog")

    try:
        from influxdb_client import Point  # type: ignore[attr-defined]

        p: Any = (
            Point("bandwidth")  # type: ignore[no-untyped-call]
            .tag("component", component)
            .tag("direction", direction)
            .tag("network", network)
            .field("bytes", bytes_count)
        )
        if user:
            p = p.tag("user", user)
        if route:
            p = p.tag("route", route)
        if detail:
            p = p.tag("detail", detail)
        write_api.write(bucket=bucket, org=org, record=p)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Bandwidth write failed (non-fatal): {}", exc)
    finally:
        if client:
            client.close()


# ---------------------------------------------------------------------------
# ASGI middleware for web request tracking
# ---------------------------------------------------------------------------


async def bandwidth_middleware(request: Request, call_next: Any) -> Response:  # noqa: ANN401
    """ASGI middleware that tracks bytes in/out per request.

    Must be added AFTER auth_middleware so request.state.user is populated.
    """
    import asyncio

    # Read request body size (content-length header or 0)
    req_size = int(request.headers.get("content-length", "0"))

    response: Response = await call_next(request)

    # Read response body size from content-length header
    resp_size = int(response.headers.get("content-length", "0"))

    # Extract user, route, and network path
    user_obj: dict[str, Any] | None = getattr(request.state, "user", None)
    user_name: str | None = user_obj.get("name") if user_obj else None
    route = request.url.path
    client_host = request.client.host if request.client else None
    net = _client_network(client_host)

    # Skip static files and healthz to reduce noise
    if route.startswith("/static/") or route == "/healthz":
        return response

    # Write both directions in background to avoid slowing the response
    def _write() -> None:
        if req_size > 0:
            write_bandwidth(
                component="web",
                direction="in",
                bytes_count=req_size,
                network=net,
                user=user_name,
                route=route,
            )
        if resp_size > 0:
            write_bandwidth(
                component="web",
                direction="out",
                bytes_count=resp_size,
                network=net,
                user=user_name,
                route=route,
            )

    asyncio.get_event_loop().run_in_executor(None, _write)
    return response


# ---------------------------------------------------------------------------
# httpx response hook for outbound API tracking
# ---------------------------------------------------------------------------


def track_httpx_response(component: str, response: httpx.Response) -> None:
    """Record bytes for an httpx response. Call after receiving a response.

    Args:
        component: Component name (weather, tides, deploy).
        response: The httpx Response object.
    """
    # Request bytes (approximate: URL + headers + body)
    req = response.request
    req_size = len(req.url.raw_path)
    if req.content:
        req_size += len(req.content)
    # Add approximate header overhead
    req_size += sum(len(k) + len(v) + 4 for k, v in req.headers.raw)

    # Response bytes
    resp_size = len(response.content) if response.is_stream_consumed else 0
    resp_size += int(response.headers.get("content-length", "0"))

    detail = str(req.url.host)
    net = _resolve_route_interface(detail) if detail else "unknown"

    if req_size > 0:
        write_bandwidth(
            component=component,
            direction="out",
            bytes_count=req_size,
            network=net,
            detail=detail,
        )
    if resp_size > 0:
        write_bandwidth(
            component=component,
            direction="in",
            bytes_count=resp_size,
            network=net,
            detail=detail,
        )
