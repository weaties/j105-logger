"""Per-segment speaker override (#648).

Label-wide assignments via ``speaker_map`` re-label every segment that
shares a label. Overrides let a single misattributed segment point at a
different crew member without touching the rest. Covers:

- Storage: set/clear override, per-transcript lookup, users FK ON DELETE SET NULL
- API: POST /transcripts/segments/{idx}/speaker-override + merged transcript
       response carries override_user_id / override_name per segment
- Integration: race → debrief sibling captures round-trip
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import pytest

from helmlog.audio import AudioSession
from helmlog.web import create_app

if TYPE_CHECKING:
    from pathlib import Path

    from helmlog.storage import Storage

pytestmark = pytest.mark.asyncio


_START_UTC = datetime(2026, 4, 21, 10, 0, 0, tzinfo=UTC)
_END_UTC = datetime(2026, 4, 21, 10, 5, 0, tzinfo=UTC)


async def _seed_sibling_pair(storage: Storage, tmp_path: Path) -> tuple[int, int]:
    """Two mono sibling audio_sessions in a capture group + relational segments."""
    group_id = "grp-override"
    paths = [tmp_path / f"sib{i}.wav" for i in range(2)]
    for p in paths:
        p.write_bytes(b"RIFF0000WAVEfmt ")

    ids: list[int] = []
    for ordinal, p in enumerate(paths):
        sess = AudioSession(
            file_path=str(p),
            device_name=f"Sib {ordinal}",
            start_utc=_START_UTC,
            end_utc=_END_UTC,
            sample_rate=48000,
            channels=1,
            capture_group_id=group_id,
            capture_ordinal=ordinal,
        )
        ids.append(await storage.write_audio_session(sess, session_type="race", name="test"))

    # Seed transcripts + segments for both siblings.
    for ordinal, asid in enumerate(ids):
        tid = await storage.create_transcript_job(asid, "base")
        segments = [
            {
                "start": 0.0 + ordinal,
                "end": 2.0 + ordinal,
                "speaker": f"sib{ordinal}:SPEAKER_00",
                "text": f"hello from sib{ordinal}",
                "channel_index": ordinal,
                "position_name": f"sib{ordinal}",
            },
            {
                "start": 3.0 + ordinal,
                "end": 5.0 + ordinal,
                "speaker": f"sib{ordinal}:SPEAKER_01",
                "text": f"second speaker on sib{ordinal}",
                "channel_index": ordinal,
                "position_name": f"sib{ordinal}",
            },
        ]
        await storage.update_transcript(tid, status="done", segments_json=json.dumps(segments))
        relational = [
            {
                "segment_index": idx,
                "start_time": seg["start"],
                "end_time": seg["end"],
                "text": seg["text"],
                "speaker": seg["speaker"],
                "channel_index": seg["channel_index"],
                "position_name": seg["position_name"],
            }
            for idx, seg in enumerate(segments)
        ]
        await storage.insert_transcript_segments(tid, relational)
    return ids[0], ids[1]


async def _create_user(storage: Storage, email: str, name: str) -> int:
    db = storage._conn()
    now = datetime.now(UTC).isoformat()
    cur = await db.execute(
        "INSERT INTO users (email, name, role, created_at) VALUES (?, ?, 'crew', ?)",
        (email, name, now),
    )
    await db.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


async def test_migration_77_adds_override_column(storage: Storage) -> None:
    """transcript_segments.override_user_id must exist after migration 77."""
    db = storage._conn()
    cur = await db.execute("PRAGMA table_info(transcript_segments)")
    cols = {row["name"] for row in await cur.fetchall()}
    assert "override_user_id" in cols


# ---------------------------------------------------------------------------
# Storage roundtrip
# ---------------------------------------------------------------------------


async def test_set_segment_override_roundtrip(storage: Storage, tmp_path: Path) -> None:
    sib0_id, _ = await _seed_sibling_pair(storage, tmp_path)
    t = await storage.get_transcript(sib0_id)
    assert t is not None
    alex = await _create_user(storage, "alex@boat.com", "Alex")

    # Override segment 1 on sib0 to Alex
    found, name = await storage.set_segment_speaker_override(int(t["id"]), 1, alex)
    assert found is True
    assert name == "Alex"

    overrides = await storage.get_segment_overrides(int(t["id"]))
    assert overrides == {1: {"user_id": alex, "name": "Alex"}}


async def test_override_clears_on_null_user(storage: Storage, tmp_path: Path) -> None:
    sib0_id, _ = await _seed_sibling_pair(storage, tmp_path)
    t = await storage.get_transcript(sib0_id)
    assert t is not None
    alex = await _create_user(storage, "alex@boat.com", "Alex")

    await storage.set_segment_speaker_override(int(t["id"]), 1, alex)
    cleared, name = await storage.set_segment_speaker_override(int(t["id"]), 1, None)
    assert cleared is True
    assert name is None
    assert await storage.get_segment_overrides(int(t["id"])) == {}


async def test_override_unknown_segment_returns_not_found(storage: Storage, tmp_path: Path) -> None:
    sib0_id, _ = await _seed_sibling_pair(storage, tmp_path)
    t = await storage.get_transcript(sib0_id)
    assert t is not None
    alex = await _create_user(storage, "alex@boat.com", "Alex")
    found, _ = await storage.set_segment_speaker_override(int(t["id"]), 99, alex)
    assert found is False


async def test_override_unknown_user_returns_not_found(storage: Storage, tmp_path: Path) -> None:
    sib0_id, _ = await _seed_sibling_pair(storage, tmp_path)
    t = await storage.get_transcript(sib0_id)
    assert t is not None
    found, _ = await storage.set_segment_speaker_override(int(t["id"]), 0, 99999)
    assert found is False


# ---------------------------------------------------------------------------
# API: /transcript response carries override info; endpoint writes it
# ---------------------------------------------------------------------------


async def test_transcript_response_exposes_override_per_segment(
    storage: Storage, tmp_path: Path
) -> None:
    sib0_id, sib1_id = await _seed_sibling_pair(storage, tmp_path)
    alex = await _create_user(storage, "alex@boat.com", "Alex")

    # Override segment 1 on sib0 (where speaker is "sib0:SPEAKER_01") to Alex
    t0 = await storage.get_transcript(sib0_id)
    assert t0 is not None
    await storage.set_segment_speaker_override(int(t0["id"]), 1, alex)

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/audio/{sib0_id}/transcript")
    assert resp.status_code == 200
    data = resp.json()
    segs = data["segments"]
    # Merged response has 4 segments (2 from sib0 + 2 from sib1), sorted by start.
    assert len(segs) == 4
    # Every segment tagged with its source audio_session_id + segment_index
    # so the client can address them for override POSTs.
    for s in segs:
        assert "audio_session_id" in s
        assert "segment_index" in s
    # Exactly one segment carries the override we set.
    with_ov = [s for s in segs if s.get("override_user_id")]
    assert len(with_ov) == 1
    assert with_ov[0]["override_user_id"] == alex
    assert with_ov[0]["override_name"] == "Alex"
    assert with_ov[0]["audio_session_id"] == sib0_id
    assert with_ov[0]["segment_index"] == 1
    # Other sib0 segment + both sib1 segments carry no override.
    no_ov = [s for s in segs if not s.get("override_user_id")]
    assert len(no_ov) == 3


async def test_post_override_endpoint_writes_and_returns(storage: Storage, tmp_path: Path) -> None:
    sib0_id, _ = await _seed_sibling_pair(storage, tmp_path)
    alex = await _create_user(storage, "alex@boat.com", "Alex")

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/audio/{sib0_id}/transcript/segments/0/speaker-override",
            json={"user_id": alex},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["audio_session_id"] == sib0_id
    assert data["segment_index"] == 0
    assert data["user_id"] == alex
    assert data["name"] == "Alex"

    # Verify it's persisted
    t = await storage.get_transcript(sib0_id)
    assert t is not None
    overrides = await storage.get_segment_overrides(int(t["id"]))
    assert overrides == {0: {"user_id": alex, "name": "Alex"}}


async def test_post_override_endpoint_clears_on_null(storage: Storage, tmp_path: Path) -> None:
    sib0_id, _ = await _seed_sibling_pair(storage, tmp_path)
    alex = await _create_user(storage, "alex@boat.com", "Alex")
    t = await storage.get_transcript(sib0_id)
    assert t is not None
    await storage.set_segment_speaker_override(int(t["id"]), 0, alex)

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/audio/{sib0_id}/transcript/segments/0/speaker-override",
            json={"user_id": None},
        )
    assert resp.status_code == 200
    assert resp.json()["name"] is None
    assert await storage.get_segment_overrides(int(t["id"])) == {}


async def test_post_override_rejects_non_int_user_id(storage: Storage, tmp_path: Path) -> None:
    sib0_id, _ = await _seed_sibling_pair(storage, tmp_path)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/audio/{sib0_id}/transcript/segments/0/speaker-override",
            json={"user_id": "alex"},
        )
    assert resp.status_code == 422


async def test_post_override_unknown_user_returns_404(storage: Storage, tmp_path: Path) -> None:
    sib0_id, _ = await _seed_sibling_pair(storage, tmp_path)
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/audio/{sib0_id}/transcript/segments/0/speaker-override",
            json={"user_id": 99999},
        )
    assert resp.status_code == 404


async def test_override_does_not_affect_speaker_map(storage: Storage, tmp_path: Path) -> None:
    """Overriding one segment must not mutate speaker_map (label-wide state)."""
    sib0_id, _ = await _seed_sibling_pair(storage, tmp_path)
    alex = await _create_user(storage, "alex@boat.com", "Alex")
    t = await storage.get_transcript(sib0_id)
    assert t is not None

    await storage.set_segment_speaker_override(int(t["id"]), 0, alex)
    sm = await storage.get_speaker_map(int(t["id"]))
    assert sm == {}
