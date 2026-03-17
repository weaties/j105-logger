"""Tests for session match local web API endpoints (#281).

Exercises the local API that the UI calls: get match status, confirm, reject,
set shared name. The scan endpoint requires peer communication so is tested
with mocked peer_client calls.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

os.environ["AUTH_DISABLED"] = "true"


@pytest.fixture
def _seed_co_op_and_session() -> Callable[..., Coroutine[Any, Any, dict[str, Any]]]:
    """Returns an async helper that seeds a co-op + shared session."""

    async def _seed(storage: object) -> dict[str, Any]:
        from helmlog.storage import Storage

        assert isinstance(storage, Storage)
        db = storage._conn()

        co_op_id = "test-coop-123"
        await db.execute(
            "INSERT INTO co_op_memberships"
            " (co_op_id, co_op_name, co_op_pub, membership_json,"
            "  role, joined_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                co_op_id,
                "Test Co-op",
                "fake-pub-key",
                "{}",
                "admin",
                datetime.now(UTC).isoformat(),
            ),
        )

        start = datetime(2024, 6, 15, 18, 0, 0, tzinfo=UTC)
        end = start + timedelta(minutes=45)
        await db.execute(
            "INSERT INTO races"
            " (name, event, race_num, date, start_utc, end_utc,"
            "  session_type)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "Wed Night Race 3",
                "CYC Wednesday",
                3,
                "2024-06-15",
                start.isoformat(),
                end.isoformat(),
                "race",
            ),
        )
        cur = await db.execute("SELECT last_insert_rowid()")
        row = await cur.fetchone()
        session_id = row[0]

        await db.execute(
            "INSERT INTO session_sharing (session_id, co_op_id, shared_at) VALUES (?, ?, ?)",
            (session_id, co_op_id, datetime.now(UTC).isoformat()),
        )

        for i in range(20):
            ts = start + timedelta(seconds=i * 30)
            await db.execute(
                "INSERT INTO positions"
                " (ts, source_addr, latitude_deg, longitude_deg)"
                " VALUES (?, ?, ?, ?)",
                (
                    ts.isoformat(),
                    0,
                    37.8044 + i * 0.0001,
                    -122.2712 + i * 0.0001,
                ),
            )

        await db.execute(
            "INSERT INTO co_op_peers"
            " (co_op_id, boat_pub, fingerprint, sail_number,"
            "  boat_name, tailscale_ip, membership_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                co_op_id,
                "peer-pub-key",
                "peer-fp-123",
                "456",
                "Peer Boat",
                "100.64.0.2",
                '{"role":"member"}',
            ),
        )

        await db.commit()
        return {
            "co_op_id": co_op_id,
            "session_id": session_id,
            "start_utc": start.isoformat(),
            "end_utc": end.isoformat(),
        }

    return _seed


SeedFn = Callable[..., Coroutine[Any, Any, dict[str, Any]]]


def _make_app(storage: object) -> httpx.ASGITransport:
    from helmlog.web import create_app

    app = create_app(storage)  # type: ignore[arg-type]
    return httpx.ASGITransport(app=app)


async def _propose(
    storage: object,
    seed: dict[str, Any],
) -> str:
    """Helper: create a match proposal and return its ID."""
    from helmlog.session_matching import propose_match

    return await propose_match(
        storage,  # type: ignore[arg-type]
        local_session_id=seed["session_id"],
        peer_fingerprint="peer-fp-123",
        peer_session_id=99,
        centroid_lat=37.8044,
        centroid_lon=-122.2712,
        start_utc=seed["start_utc"],
        end_utc=seed["end_utc"],
    )


# ------------------------------------------------------------------
# GET /api/sessions/{id}/match — match status
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_match_status_no_match(
    storage: object,
    _seed_co_op_and_session: SeedFn,
) -> None:
    """Session with no match returns unmatched status."""
    seed = await _seed_co_op_and_session(storage)
    transport = _make_app(storage)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        resp = await client.get(
            f"/api/sessions/{seed['session_id']}/match",
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "unmatched"
        assert data["shared_name"] is None


@pytest.mark.asyncio
async def test_get_match_status_with_candidate(
    storage: object,
    _seed_co_op_and_session: SeedFn,
) -> None:
    """Candidate match returns candidate status and proposal details."""
    seed = await _seed_co_op_and_session(storage)
    match_id = await _propose(storage, seed)

    transport = _make_app(storage)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        resp = await client.get(
            f"/api/sessions/{seed['session_id']}/match",
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "candidate"
        assert data["match_group_id"] == match_id
        assert data["shared_name"] is None


@pytest.mark.asyncio
async def test_get_match_status_with_shared_name(
    storage: object,
    _seed_co_op_and_session: SeedFn,
) -> None:
    """Confirmed match with shared name returns it."""
    seed = await _seed_co_op_and_session(storage)

    from helmlog.session_matching import (
        confirm_match,
        set_shared_name,
    )

    match_id = await _propose(storage, seed)
    await confirm_match(
        storage,
        match_id,
        "self-fp",
        quorum=1,  # type: ignore[arg-type]
    )
    await set_shared_name(
        storage,
        match_id,  # type: ignore[arg-type]
        "CYC Wednesday Night Race 3",
        "self-fp",
    )

    transport = _make_app(storage)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        resp = await client.get(
            f"/api/sessions/{seed['session_id']}/match",
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "confirmed"
        assert data["shared_name"] == "CYC Wednesday Night Race 3"


# ------------------------------------------------------------------
# POST /api/sessions/{id}/match/confirm
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_match(
    storage: object,
    _seed_co_op_and_session: SeedFn,
) -> None:
    """Confirming a candidate match succeeds."""
    seed = await _seed_co_op_and_session(storage)
    match_id = await _propose(storage, seed)

    transport = _make_app(storage)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/api/sessions/{seed['session_id']}/match/confirm",
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["match_group_id"] == match_id
        assert data["status"] in ("candidate", "confirmed")


@pytest.mark.asyncio
async def test_confirm_match_no_proposal(
    storage: object,
    _seed_co_op_and_session: SeedFn,
) -> None:
    """Confirming when no proposal exists returns 404."""
    seed = await _seed_co_op_and_session(storage)
    transport = _make_app(storage)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/api/sessions/{seed['session_id']}/match/confirm",
        )
        assert resp.status_code == 404


# ------------------------------------------------------------------
# POST /api/sessions/{id}/match/reject
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reject_match(
    storage: object,
    _seed_co_op_and_session: SeedFn,
) -> None:
    """Rejecting a candidate match clears match_group_id."""
    seed = await _seed_co_op_and_session(storage)
    await _propose(storage, seed)

    transport = _make_app(storage)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/api/sessions/{seed['session_id']}/match/reject",
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "rejected"

        # Verify match is cleared
        resp2 = await client.get(
            f"/api/sessions/{seed['session_id']}/match",
        )
        assert resp2.json()["status"] == "unmatched"


# ------------------------------------------------------------------
# PUT /api/sessions/{id}/match/name
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_shared_name(
    storage: object,
    _seed_co_op_and_session: SeedFn,
) -> None:
    """Setting shared name on a confirmed match succeeds."""
    seed = await _seed_co_op_and_session(storage)

    from helmlog.session_matching import confirm_match

    match_id = await _propose(storage, seed)
    await confirm_match(
        storage,
        match_id,
        "self-fp",
        quorum=1,  # type: ignore[arg-type]
    )

    transport = _make_app(storage)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        resp = await client.put(
            f"/api/sessions/{seed['session_id']}/match/name",
            json={"name": "CYC Wednesday Night Race 3"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["shared_name"] == "CYC Wednesday Night Race 3"


@pytest.mark.asyncio
async def test_set_shared_name_unconfirmed(
    storage: object,
    _seed_co_op_and_session: SeedFn,
) -> None:
    """Setting shared name on unconfirmed match returns 400."""
    seed = await _seed_co_op_and_session(storage)
    await _propose(storage, seed)

    transport = _make_app(storage)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        resp = await client.put(
            f"/api/sessions/{seed['session_id']}/match/name",
            json={"name": "Test Name"},
        )
        assert resp.status_code == 400


# ------------------------------------------------------------------
# POST /api/sessions/{id}/match/scan — proximity scan
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_for_matches(
    storage: object,
    _seed_co_op_and_session: SeedFn,
) -> None:
    """Scan calls peer_client and proposes matches."""
    seed = await _seed_co_op_and_session(storage)

    mock_propose = AsyncMock(
        return_value={
            "match_group_id": "mock-match-id",
            "matched_session_id": seed["session_id"],
            "status": "candidate",
        }
    )

    mock_card = MagicMock()
    mock_card.fingerprint = "self-fp-789"

    with (
        patch(
            "helmlog.peer_client.propose_session_match",
            mock_propose,
        ),
        patch("helmlog.federation.load_identity") as mock_id,
    ):
        mock_id.return_value = (MagicMock(), mock_card)

        transport = _make_app(storage)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            resp = await client.post(
                f"/api/sessions/{seed['session_id']}/match/scan",
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "proposals" in data


@pytest.mark.asyncio
async def test_scan_no_co_op(storage: object) -> None:
    """Scan on session not shared with any co-op returns 400."""
    from helmlog.storage import Storage

    assert isinstance(storage, Storage)
    db = storage._conn()

    start = datetime(2024, 6, 15, 18, 0, 0, tzinfo=UTC)
    end = start + timedelta(minutes=45)
    await db.execute(
        "INSERT INTO races"
        " (name, event, race_num, date, start_utc, end_utc,"
        "  session_type)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "Solo Race",
            "Solo",
            1,
            "2024-06-15",
            start.isoformat(),
            end.isoformat(),
            "race",
        ),
    )
    cur = await db.execute("SELECT last_insert_rowid()")
    row = await cur.fetchone()
    session_id = row[0]
    await db.commit()

    transport = _make_app(storage)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        resp = await client.post(
            f"/api/sessions/{session_id}/match/scan",
        )
        assert resp.status_code == 400


# ------------------------------------------------------------------
# /api/sessions/{id}/detail includes match info
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_detail_includes_match_info(
    storage: object,
    _seed_co_op_and_session: SeedFn,
) -> None:
    """Session detail endpoint includes shared_name and match_status."""
    seed = await _seed_co_op_and_session(storage)

    from helmlog.session_matching import (
        confirm_match,
        set_shared_name,
    )

    match_id = await _propose(storage, seed)
    await confirm_match(
        storage,
        match_id,
        "self-fp",
        quorum=1,  # type: ignore[arg-type]
    )
    await set_shared_name(
        storage,
        match_id,  # type: ignore[arg-type]
        "CYC Wed Night 3",
        "self-fp",
    )

    transport = _make_app(storage)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        resp = await client.get(
            f"/api/sessions/{seed['session_id']}/detail",
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["shared_name"] == "CYC Wed Night 3"
        assert data["match_status"] == "confirmed"
