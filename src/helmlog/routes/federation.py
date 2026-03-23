"""Route handlers for federation."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from loguru import logger

from helmlog.auth import require_auth
from helmlog.routes._helpers import audit, get_storage, limiter

router = APIRouter()


@router.get("/api/federation/identity")
async def api_federation_identity(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    identity = await storage.get_boat_identity()
    boat_card_json: str | None = None

    # The filesystem identity is authoritative. If the DB row is missing
    # (e.g. DB was recreated), rebuild it from the filesystem.
    try:
        from helmlog.federation import load_identity

        _, card = load_identity()
        boat_card_json = card.to_json()
        if identity:
            identity["owner_email"] = card.owner_email
        else:
            # Re-sync DB from filesystem identity
            db = storage._conn()
            from datetime import UTC, datetime

            now_iso = datetime.now(UTC).isoformat()
            await db.execute(
                "INSERT OR REPLACE INTO boat_identity"
                " (id, pub_key, fingerprint, sail_number, boat_name, created_at)"
                " VALUES (1, ?, ?, ?, ?, ?)",
                (
                    card.pub_key,
                    card.fingerprint,
                    card.sail_number,
                    card.boat_name,
                    now_iso,
                ),
            )
            await db.commit()
            identity = {
                "pub_key": card.pub_key,
                "fingerprint": card.fingerprint,
                "sail_number": card.sail_number,
                "boat_name": card.boat_name,
                "owner_email": card.owner_email,
                "created_at": now_iso,
            }
    except FileNotFoundError:
        pass

    return JSONResponse(
        {
            "identity": identity,
            "boat_card_json": boat_card_json,
        }
    )


@router.post("/api/federation/identity", status_code=201)
async def api_federation_identity_init(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    body = await request.json()
    sail = body.get("sail_number", "").strip()
    name = body.get("boat_name", "").strip()
    email = body.get("owner_email") or None
    if not sail or not name:
        raise HTTPException(422, "sail_number and boat_name are required")

    from helmlog.federation import identity_exists, init_identity

    if identity_exists():
        raise HTTPException(409, "Identity already exists")

    card = init_identity(
        sail_number=sail,
        boat_name=name,
        owner_email=email,
    )
    await storage.save_boat_identity(
        pub_key=card.pub_key,
        fingerprint=card.fingerprint,
        sail_number=card.sail_number,
        boat_name=card.boat_name,
    )
    await audit(
        request,
        "federation.identity.init",
        detail=f"{card.boat_name} ({card.fingerprint})",
        user=_user,
    )
    return JSONResponse(
        {
            "pub_key": card.pub_key,
            "fingerprint": card.fingerprint,
            "sail_number": card.sail_number,
            "boat_name": card.boat_name,
        },
        status_code=201,
    )


@router.get("/api/federation/co-ops")
async def api_federation_coops(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    memberships = await storage.list_co_op_memberships()
    result = []
    for m in memberships:
        peers = await storage.list_co_op_peers(m["co_op_id"])
        result.append({**m, "peers": peers})
    return JSONResponse({"co_ops": result})


@router.post("/api/federation/co-ops", status_code=201)
async def api_federation_coop_create(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    from helmlog.federation import create_co_op, load_identity

    try:
        private_key, card = load_identity()
    except FileNotFoundError:
        raise HTTPException(409, "Initialize identity first")  # noqa: B904

    if not card.owner_email:
        raise HTTPException(
            422,
            "Co-op requires an owner email. Re-initialize identity with an email address.",
        )

    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(422, "Co-op name is required")
    areas = body.get("areas") or []

    charter = create_co_op(private_key, card, name=name, areas=areas)

    from helmlog.federation import list_co_op_members

    members = list_co_op_members(charter.co_op_id)
    if members:
        await storage.save_co_op_membership(
            co_op_id=charter.co_op_id,
            co_op_name=charter.name,
            co_op_pub=card.pub_key,
            membership_json=members[0].to_json(),
            role="admin",
            joined_at=members[0].joined_at,
        )
        # Also save the creating boat as a peer so it appears in the member list
        await storage.save_co_op_peer(
            co_op_id=charter.co_op_id,
            boat_pub=card.pub_key,
            fingerprint=card.fingerprint,
            membership_json=members[0].to_json(),
            sail_number=card.sail_number,
            boat_name=card.boat_name,
        )
    await audit(
        request,
        "federation.co_op.create",
        detail=f"{charter.name} ({charter.co_op_id})",
        user=_user,
    )
    return JSONResponse(charter.to_dict(), status_code=201)


@router.post("/api/federation/co-ops/{co_op_id}/invite", status_code=201)
async def api_federation_invite(
    request: Request,
    co_op_id: str,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    from helmlog.federation import BoatCard, load_identity, sign_membership

    try:
        private_key, admin_card = load_identity()
    except FileNotFoundError:
        raise HTTPException(409, "Initialize identity first")  # noqa: B904

    membership = await storage.get_co_op_membership(co_op_id)
    if not membership or membership["role"] != "admin":
        raise HTTPException(403, "You are not admin of this co-op")

    body = await request.json()
    required = ["pub", "fingerprint", "sail_number", "name"]
    missing = [f for f in required if not body.get(f)]
    if missing:
        raise HTTPException(
            422,
            f"Boat card missing required fields: {', '.join(missing)}",
        )

    invitee = BoatCard(
        pub_key=body["pub"],
        fingerprint=body["fingerprint"],
        sail_number=body["sail_number"],
        boat_name=body["name"],
        owner_email=body.get("owner_email"),
    )

    record = sign_membership(
        private_key,
        co_op_id=co_op_id,
        boat_card=invitee,
    )

    # Persist to filesystem
    from pathlib import Path

    identity_dir = Path.home() / ".helmlog" / "identity"
    members_dir = identity_dir.parent / "co-ops" / co_op_id / "members"
    members_dir.mkdir(parents=True, exist_ok=True)
    member_file = members_dir / f"{invitee.fingerprint}.json"
    member_file.write_text(record.to_json())

    # Persist to SQLite as peer
    await storage.save_co_op_peer(
        co_op_id=co_op_id,
        boat_pub=invitee.pub_key,
        fingerprint=invitee.fingerprint,
        membership_json=record.to_json(),
        sail_number=invitee.sail_number,
        boat_name=invitee.boat_name,
        tailscale_ip=body.get("tailscale_ip"),
    )
    await audit(
        request,
        "federation.invite",
        detail=f"{invitee.boat_name} ({invitee.fingerprint}) → {co_op_id}",
        user=_user,
    )
    # Build invite bundle — the invitee imports this to join
    membership = await storage.get_co_op_membership(co_op_id)
    invite_bundle = {
        "co_op_id": co_op_id,
        "co_op_name": membership["co_op_name"] if membership else "",
        "admin_pub": admin_card.pub_key,
        "admin_fingerprint": admin_card.fingerprint,
        "admin_boat_name": admin_card.boat_name,
        "admin_sail_number": admin_card.sail_number,
        "admin_tailscale_ip": admin_card.tailscale_ip or "",
        "membership": record.to_dict(),
    }
    return JSONResponse(
        {
            "boat_name": invitee.boat_name,
            "fingerprint": invitee.fingerprint,
            "membership": record.to_dict(),
            "invite_bundle": invite_bundle,
        },
        status_code=201,
    )


@router.post("/api/federation/join", status_code=201)
async def api_federation_join(
    request: Request,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Join a co-op using an invite bundle from the admin boat."""
    storage = get_storage(request)
    body = await request.json()
    co_op_id = body.get("co_op_id", "").strip()
    co_op_name = body.get("co_op_name", "").strip()
    admin_pub = body.get("admin_pub", "").strip()
    admin_fingerprint = body.get("admin_fingerprint", "").strip()
    membership_json = body.get("membership")

    if not all([co_op_id, co_op_name, admin_pub]):
        raise HTTPException(422, "Missing required fields in invite bundle")

    # Verify the membership signature before accepting the bundle
    if isinstance(membership_json, dict) and membership_json.get("admin_sig"):
        from helmlog.federation import MembershipRecord, verify_membership

        try:
            m = membership_json
            record = MembershipRecord(
                co_op_id=m.get("co_op_id", ""),
                boat_pub=m.get("boat_pub", ""),
                sail_number=m.get("sail_number", ""),
                boat_name=m.get("boat_name", ""),
                role=m.get("role", "member"),
                joined_at=m.get("joined_at", ""),
                owner_email=m.get("owner_email"),
                admin_sig=m.get("admin_sig", ""),
            )
            if not verify_membership(admin_pub, record):
                raise HTTPException(
                    422,
                    "Invite bundle has invalid signature — bundle may be tampered",
                )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(422, f"Invalid invite bundle: {exc}") from exc

    import json as _json

    membership_str = (
        _json.dumps(membership_json)
        if isinstance(membership_json, dict)
        else str(membership_json or "{}")
    )

    # Save co-op membership (this boat is a member, not admin)
    await storage.save_co_op_membership(
        co_op_id=co_op_id,
        co_op_name=co_op_name,
        co_op_pub=admin_pub,
        membership_json=membership_str,
        role="member",
    )

    # Save ourselves as a peer (so we show in the members list)
    try:
        from helmlog.federation import load_identity

        _, my_card = load_identity()
        await storage.save_co_op_peer(
            co_op_id=co_op_id,
            boat_pub=my_card.pub_key,
            fingerprint=my_card.fingerprint,
            membership_json=membership_str,
            sail_number=my_card.sail_number,
            boat_name=my_card.boat_name,
            tailscale_ip=my_card.tailscale_ip,
        )
    except FileNotFoundError:
        pass

    # Save the admin as a peer so we can query them
    admin_tailscale_ip = body.get("admin_tailscale_ip", "").strip() or None
    admin_boat_name = body.get("admin_boat_name", "").strip()
    admin_sail_number = body.get("admin_sail_number", "").strip()
    await storage.save_co_op_peer(
        co_op_id=co_op_id,
        boat_pub=admin_pub,
        fingerprint=admin_fingerprint,
        membership_json="{}",
        sail_number=admin_sail_number,
        boat_name=admin_boat_name,
        tailscale_ip=admin_tailscale_ip,
    )

    await audit(
        request,
        "federation.join",
        detail=f"Joined {co_op_name} ({co_op_id})",
        user=_user,
    )
    return JSONResponse(
        {"status": "joined", "co_op_id": co_op_id, "co_op_name": co_op_name},
        status_code=201,
    )


# ── Session sharing ──────────────────────────────────────────────────


@router.get("/api/sessions/{session_id}/sharing")
async def api_session_sharing(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    memberships = await storage.list_co_op_memberships()
    sharing = await storage.get_session_sharing(session_id)
    shared_ids = {s["co_op_id"] for s in sharing}
    return JSONResponse(
        {
            "sharing": sharing,
            "co_ops": [
                {
                    "co_op_id": m["co_op_id"],
                    "co_op_name": m["co_op_name"],
                    "shared": m["co_op_id"] in shared_ids,
                }
                for m in memberships
                if m["status"] == "active"
            ],
        }
    )


@router.post("/api/sessions/{session_id}/share", status_code=201)
async def api_session_share(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    body = await request.json()
    co_op_id = body.get("co_op_id", "").strip()
    if not co_op_id:
        raise HTTPException(422, "co_op_id is required")
    membership = await storage.get_co_op_membership(co_op_id)
    if not membership:
        raise HTTPException(404, "Not a member of this co-op")
    embargo_until = body.get("embargo_until") or None
    await storage.share_session(
        session_id,
        co_op_id,
        user_id=_user.get("id"),
        embargo_until=embargo_until,
    )
    await audit(
        request,
        "federation.session.share",
        detail=f"session {session_id} → {membership['co_op_name']}",
        user=_user,
    )
    return JSONResponse({"status": "shared", "co_op_id": co_op_id}, status_code=201)


@router.delete("/api/sessions/{session_id}/share/{co_op_id}")
async def api_session_unshare(
    request: Request,
    session_id: int,
    co_op_id: str,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    storage = get_storage(request)
    removed = await storage.unshare_session(session_id, co_op_id)
    if not removed:
        raise HTTPException(404, "Session was not shared with this co-op")
    await audit(
        request,
        "federation.session.unshare",
        detail=f"session {session_id} ✕ {co_op_id}",
        user=_user,
    )
    return JSONResponse({"status": "unshared", "co_op_id": co_op_id})


# ── Session matching (local UI endpoints) (#281) ─────────────────


@router.get("/api/sessions/{session_id}/match")
async def api_session_match_status(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Get match status for a session."""
    storage = get_storage(request)
    db = storage._conn()
    cur = await db.execute(
        "SELECT match_group_id, match_confirmed, shared_name FROM races WHERE id = ?",
        (session_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(404, "Session not found")

    match_group_id = row["match_group_id"]
    if not match_group_id:
        return JSONResponse({"status": "unmatched", "match_group_id": None, "shared_name": None})

    # Look up proposal status + peer info
    cur2 = await db.execute(
        "SELECT p.status, p.proposer_fingerprint, p.peer_session_id,"
        "       cp.boat_name AS peer_boat_name"
        " FROM session_match_proposals p"
        " LEFT JOIN co_op_peers cp ON p.proposer_fingerprint = cp.fingerprint"
        " WHERE p.match_group_id = ?"
        " LIMIT 1",
        (match_group_id,),
    )
    proposal = await cur2.fetchone()
    status = dict(proposal)["status"] if proposal else "unmatched"

    peer_boat_name: str | None = None
    peer_session_name: str | None = None
    if proposal:
        peer_boat_name = proposal["peer_boat_name"]
        peer_sid = proposal["peer_session_id"]
        # Look up the peer's session name on this boat (if it was the proposer,
        # peer_session_id is the remote session — we won't have the name locally).
        # If this boat received the proposal, local_session_id == session_id and
        # peer_session_id is the remote one. Check if we have a local race with that ID.
        if peer_sid:
            cur3 = await db.execute("SELECT name FROM races WHERE id = ?", (peer_sid,))
            peer_race = await cur3.fetchone()
            if peer_race:
                peer_session_name = peer_race["name"]

    return JSONResponse(
        {
            "status": status,
            "match_group_id": match_group_id,
            "shared_name": row["shared_name"],
            "match_confirmed": bool(row["match_confirmed"]),
            "peer_boat_name": peer_boat_name,
            "peer_session_name": peer_session_name,
        }
    )


@router.post("/api/sessions/{session_id}/match/confirm")
async def api_session_match_confirm(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Confirm a pending session match."""
    storage = get_storage(request)
    db = storage._conn()
    cur = await db.execute(
        "SELECT match_group_id FROM races WHERE id = ?",
        (session_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(404, "Session not found")
    match_group_id = row["match_group_id"]
    if not match_group_id:
        raise HTTPException(404, "No match proposal for this session")

    from helmlog.session_matching import confirm_match

    identity = await storage.get_boat_identity()
    fp = identity["fingerprint"] if identity else "local"
    # Each boat confirms independently on its own DB, so quorum=1.
    # Federation-wide quorum is tracked via peer API confirmations.
    ok = await confirm_match(storage, match_group_id, fp, quorum=1)
    if not ok:
        raise HTTPException(404, "Match not found or cannot be confirmed")

    # Get updated status
    cur2 = await db.execute(
        "SELECT status FROM session_match_proposals WHERE match_group_id = ?",
        (match_group_id,),
    )
    proposal = await cur2.fetchone()
    status = dict(proposal)["status"] if proposal else "candidate"

    await audit(request, "session.match.confirm", detail=f"match={match_group_id}", user=_user)
    return JSONResponse({"match_group_id": match_group_id, "status": status})


@router.post("/api/sessions/{session_id}/match/reject")
async def api_session_match_reject(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Reject a pending session match."""
    storage = get_storage(request)
    db = storage._conn()
    cur = await db.execute(
        "SELECT match_group_id FROM races WHERE id = ?",
        (session_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(404, "Session not found")
    match_group_id = row["match_group_id"]
    if not match_group_id:
        raise HTTPException(404, "No match proposal for this session")

    from helmlog.session_matching import reject_match

    identity = await storage.get_boat_identity()
    fp = identity["fingerprint"] if identity else "local"
    ok = await reject_match(storage, match_group_id, fp)
    if not ok:
        raise HTTPException(404, "Match not found or cannot be rejected")

    await audit(request, "session.match.reject", detail=f"match={match_group_id}", user=_user)
    return JSONResponse({"match_group_id": match_group_id, "status": "rejected"})


@router.put("/api/sessions/{session_id}/match/name")
async def api_session_match_name(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Set or update the shared name for a confirmed match."""
    storage = get_storage(request)
    db = storage._conn()
    cur = await db.execute(
        "SELECT match_group_id, match_confirmed FROM races WHERE id = ?",
        (session_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(404, "Session not found")
    match_group_id = row["match_group_id"]
    if not match_group_id:
        raise HTTPException(400, "No match proposal for this session")
    if not row["match_confirmed"]:
        raise HTTPException(400, "Match must be confirmed before setting a shared name")

    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(422, "name is required")

    from helmlog.session_matching import set_shared_name

    identity = await storage.get_boat_identity()
    fp = identity["fingerprint"] if identity else "local"
    ok = await set_shared_name(storage, match_group_id, name, fp)
    if not ok:
        raise HTTPException(400, "Failed to set shared name")

    # Push name to co-op peers
    co_op_cur = await db.execute(
        "SELECT co_op_id FROM session_sharing WHERE session_id = ?",
        (session_id,),
    )
    co_op_rows = await co_op_cur.fetchall()
    if co_op_rows:
        from helmlog.federation import load_identity
        from helmlog.peer_client import set_match_name as peer_set_match_name

        try:
            private_key, card = load_identity()
            coros: list[Any] = []
            for co_op_row in co_op_rows:
                co_op_id = co_op_row["co_op_id"]
                peers = await storage.list_co_op_peers(co_op_id)
                for peer in peers:
                    pip = peer.get("tailscale_ip")
                    if not pip or peer.get("fingerprint") == card.fingerprint:
                        continue
                    coros.append(
                        peer_set_match_name(
                            pip,
                            co_op_id,
                            match_group_id,
                            name,
                            private_key,
                            card.fingerprint,
                        )
                    )
            results = await asyncio.gather(*coros, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    logger.warning(f"Peer name push failed: {r}")
        except Exception:
            logger.warning("Failed to push shared name to peers (local save succeeded)")

    await audit(
        request, "session.match.name", detail=f"match={match_group_id} name={name}", user=_user
    )
    return JSONResponse({"match_group_id": match_group_id, "shared_name": name})


@router.post("/api/sessions/{session_id}/match/scan")
@limiter.limit("5/minute")
async def api_session_match_scan(
    request: Request,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("admin")),  # noqa: B008
) -> JSONResponse:
    """Trigger a proximity scan for this session against co-op peers."""
    storage = get_storage(request)
    db = storage._conn()

    # Find co-ops this session is shared with
    cur = await db.execute(
        "SELECT co_op_id FROM session_sharing WHERE session_id = ?",
        (session_id,),
    )
    rows = await cur.fetchall()
    co_op_ids = [r["co_op_id"] for r in rows]
    if not co_op_ids:
        raise HTTPException(400, "Session is not shared with any co-op")

    # Get session centroid and time range
    from helmlog.session_matching import compute_session_centroid

    centroid_lat, centroid_lon = await compute_session_centroid(storage, session_id)
    cur2 = await db.execute(
        "SELECT start_utc, end_utc FROM races WHERE id = ?",
        (session_id,),
    )
    race = await cur2.fetchone()
    if not race or not race["start_utc"] or not race["end_utc"]:
        raise HTTPException(400, "Session has no time range")

    from helmlog.federation import load_identity
    from helmlog.peer_client import propose_session_match

    try:
        private_key, card = load_identity()
    except FileNotFoundError:
        raise HTTPException(409, "Initialize identity first")  # noqa: B904

    # For each co-op, propose matches to all peers in parallel
    coros: list[Any] = []
    for co_op_id in co_op_ids:
        peers = await storage.list_co_op_peers(co_op_id)
        for peer in peers:
            ip = peer.get("tailscale_ip")
            if not ip or peer.get("fingerprint") == card.fingerprint:
                continue
            coros.append(
                propose_session_match(
                    ip,
                    co_op_id,
                    private_key,
                    card.fingerprint,
                    local_session_id=session_id,
                    centroid_lat=centroid_lat,
                    centroid_lon=centroid_lon,
                    start_utc=race["start_utc"],
                    end_utc=race["end_utc"],
                )
            )
    results = await asyncio.gather(*coros, return_exceptions=True)
    proposals: list[dict[str, Any]] = [r for r in results if isinstance(r, dict)]

    # Mirror the match on the initiating boat: set match_group_id on the
    # local session and create a local proposal so the status query works.
    if proposals:
        from datetime import UTC, datetime, timedelta

        mgid = proposals[0].get("match_group_id")
        matched_peer_sid = proposals[0].get("matched_session_id")
        if mgid:
            await db.execute(
                "UPDATE races SET match_group_id = ? WHERE id = ? AND match_group_id IS NULL",
                (mgid, session_id),
            )
            now = datetime.now(UTC)
            await db.execute(
                "INSERT OR IGNORE INTO session_match_proposals"
                " (match_group_id, proposer_fingerprint, local_session_id,"
                "  peer_session_id, centroid_lat, centroid_lon,"
                "  start_utc, end_utc, status, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?)",
                (
                    mgid,
                    card.fingerprint,
                    session_id,
                    matched_peer_sid,
                    centroid_lat,
                    centroid_lon,
                    race["start_utc"],
                    race["end_utc"],
                    now.isoformat(),
                    (now + timedelta(hours=48)).isoformat(),
                ),
            )
            await db.commit()

    await audit(
        request,
        "session.match.scan",
        detail=f"session={session_id} proposals={len(proposals)}",
        user=_user,
    )
    return JSONResponse({"proposals": proposals})


# ── Peer data proxies (local UI → remote peers) ────────────────────


@router.get("/api/federation/co-ops/{co_op_id}/peer-sessions")
@limiter.limit("10/minute")
async def api_peer_sessions(
    request: Request,
    co_op_id: str,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Query all online peers in a co-op for their shared sessions."""
    storage = get_storage(request)
    from helmlog.federation import load_identity
    from helmlog.peer_client import fetch_all_peer_sessions

    try:
        private_key, card = load_identity()
    except FileNotFoundError:
        raise HTTPException(409, "Initialize identity first")  # noqa: B904

    peers = await fetch_all_peer_sessions(
        storage,
        co_op_id,
        private_key,
        card.fingerprint,
    )
    await audit(
        request,
        "coop.proxy.peer_sessions",
        detail=f"co_op={co_op_id} peers={len(peers)}",
        user=_user,
    )
    return JSONResponse({"peers": peers})


@router.get(
    "/api/federation/co-ops/{co_op_id}/peers/{fingerprint}/sessions/{session_id}/track",
)
@limiter.limit("10/minute")
async def api_peer_session_track(
    request: Request,
    co_op_id: str,
    fingerprint: str,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Proxy track data from a specific remote peer."""
    storage = get_storage(request)
    from helmlog.federation import load_identity
    from helmlog.peer_client import fetch_session_track

    try:
        private_key, card = load_identity()
    except FileNotFoundError:
        raise HTTPException(409, "Initialize identity first")  # noqa: B904

    # Look up peer's Tailscale IP
    peer = await storage.get_co_op_peer(co_op_id, fingerprint)
    if not peer or not peer.get("tailscale_ip"):
        raise HTTPException(404, "Peer not found or no Tailscale IP")

    track = await fetch_session_track(
        peer["tailscale_ip"],
        co_op_id,
        session_id,
        private_key,
        card.fingerprint,
    )
    await audit(
        request,
        "coop.proxy.peer_track",
        detail=f"co_op={co_op_id} peer={fingerprint} session={session_id} points={len(track)}",
        user=_user,
    )
    return JSONResponse({"track": track, "count": len(track)})


@router.get(
    "/api/federation/co-ops/{co_op_id}/peers/{fingerprint}/sessions/{session_id}/wind-field",
)
@limiter.limit("10/minute")
async def api_peer_session_wind_field(
    request: Request,
    co_op_id: str,
    fingerprint: str,
    session_id: int,
    _user: dict[str, Any] = Depends(require_auth("viewer")),  # noqa: B008
) -> JSONResponse:
    """Proxy wind-field data from a specific remote peer (#246)."""
    storage = get_storage(request)
    from helmlog.federation import load_identity
    from helmlog.peer_client import fetch_session_wind_field

    try:
        private_key, card = load_identity()
    except FileNotFoundError:
        raise HTTPException(409, "Initialize identity first")  # noqa: B904

    peer = await storage.get_co_op_peer(co_op_id, fingerprint)
    if not peer or not peer.get("tailscale_ip"):
        raise HTTPException(404, "Peer not found or no Tailscale IP")

    data = await fetch_session_wind_field(
        peer["tailscale_ip"],
        co_op_id,
        session_id,
        private_key,
        card.fingerprint,
    )
    if data is None:
        raise HTTPException(502, "Failed to fetch wind-field from peer")

    await audit(
        request,
        "coop.proxy.peer_wind_field",
        detail=f"co_op={co_op_id} peer={fingerprint} session={session_id}",
        user=_user,
    )
    return JSONResponse(data)
