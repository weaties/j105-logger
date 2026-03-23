"""WebSocket live push endpoint for real-time instrument data."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

router = APIRouter()


async def broadcast(clients: set[WebSocket], message: dict[str, Any]) -> None:
    """Send a JSON message to all connected WebSocket clients.

    Removes dead clients silently — never blocks the write path.
    """
    if not clients:
        return
    data = json.dumps(message)
    dead: list[WebSocket] = []
    for ws in set(clients):
        try:
            await ws.send_text(data)
        except Exception:  # noqa: BLE001
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)
        logger.debug("Removed dead WebSocket client")


@router.websocket("/ws/live")
async def ws_live(websocket: WebSocket) -> None:
    """WebSocket endpoint for live instrument, state, and health updates.

    On connect: sends an initial instrument snapshot.
    While connected: receives broadcasts from update_live() callback.
    Auth: uses same cookie-based auth as HTTP routes (skipped when AUTH_DISABLED).
    """
    await websocket.accept()
    clients: set[WebSocket] = websocket.app.state.ws_clients
    clients.add(websocket)

    # Send initial instrument snapshot
    storage = websocket.app.state.storage
    data = await storage.latest_instruments()
    try:
        await websocket.send_json({"type": "instruments", "data": data})
    except Exception:  # noqa: BLE001
        clients.discard(websocket)
        return

    try:
        while True:
            # Keep connection alive — wait for client messages (ping/close)
            msg = await asyncio.wait_for(websocket.receive_text(), timeout=300)
            if msg == "ping":
                await websocket.send_json({"type": "pong"})
    except (TimeoutError, WebSocketDisconnect, Exception):  # noqa: BLE001
        pass
    finally:
        clients.discard(websocket)
