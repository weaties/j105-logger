"""Tests for diarized transcript crew association and voice profiles (#443).

Covers:
- Schema migration 57: speaker_map column + crew_voice_profiles table
- Storage: assign_speaker_crew, get_speaker_map, voice profile CRUD
- API: assign-speaker endpoint, voice profile consent + hard delete
- Transcript display: speaker_map applied in get_transcript_with_anon
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from helmlog.storage import Storage

_START_UTC = datetime(2026, 2, 26, 14, 0, 0, tzinfo=UTC)
_END_UTC = datetime(2026, 2, 26, 14, 30, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_audio_session(storage: Storage, tmp_path: Path) -> int:
    from helmlog.audio import AudioSession

    wav_file = tmp_path / "test.wav"
    wav_file.write_bytes(b"RIFF")
    session = AudioSession(
        file_path=str(wav_file),
        device_name="Test",
        start_utc=_START_UTC,
        end_utc=_END_UTC,
        sample_rate=48000,
        channels=1,
    )
    return await storage.write_audio_session(session)


async def _create_transcript_with_segments(storage: Storage, audio_session_id: int) -> int:
    """Create a completed transcript with diarized segments."""
    tid = await storage.create_transcript_job(audio_session_id, "base")
    segments = [
        {"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00", "text": "Ready about."},
        {"start": 2.1, "end": 4.0, "speaker": "SPEAKER_01", "text": "Ready."},
        {"start": 4.1, "end": 6.0, "speaker": "SPEAKER_00", "text": "Helm's a-lee!"},
    ]
    await storage.update_transcript(
        tid,
        status="done",
        text="SPEAKER_00: Ready about.\nSPEAKER_01: Ready.\nSPEAKER_00: Helm's a-lee!",
        segments_json=json.dumps(segments),
    )
    return tid


async def _create_user(storage: Storage, email: str, name: str, role: str = "crew") -> int:
    """Create a user and return their id."""
    db = storage._conn()
    now = datetime.now(UTC).isoformat()
    cur = await db.execute(
        "INSERT INTO users (email, name, role, created_at) VALUES (?, ?, ?, ?)",
        (email, name, role, now),
    )
    await db.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_57_adds_speaker_map_column(storage: Storage) -> None:
    """Migration 57 adds speaker_map TEXT column to transcripts."""
    db = storage._conn()
    cur = await db.execute("PRAGMA table_info(transcripts)")
    cols = {row["name"] for row in await cur.fetchall()}
    assert "speaker_map" in cols


@pytest.mark.asyncio
async def test_migration_57_creates_crew_voice_profiles(storage: Storage) -> None:
    """Migration 57 creates crew_voice_profiles table."""
    db = storage._conn()
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='crew_voice_profiles'"
    )
    row = await cur.fetchone()
    assert row is not None


# ---------------------------------------------------------------------------
# Storage: assign_speaker_crew
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_speaker_crew(storage: Storage, tmp_path: Path) -> None:
    """assign_speaker_crew stores a crew mapping in speaker_map."""
    audio_id = await _create_audio_session(storage, tmp_path)
    tid = await _create_transcript_with_segments(storage, audio_id)
    user_id = await _create_user(storage, "dave@boat.com", "Dave")

    ok = await storage.assign_speaker_crew(tid, "SPEAKER_00", user_id, "Dave")
    assert ok is True

    speaker_map = await storage.get_speaker_map(tid)
    assert speaker_map["SPEAKER_00"] == {
        "type": "crew",
        "user_id": user_id,
        "name": "Dave",
    }


@pytest.mark.asyncio
async def test_assign_speaker_crew_not_found(storage: Storage) -> None:
    """assign_speaker_crew returns False for nonexistent transcript."""
    ok = await storage.assign_speaker_crew(999, "SPEAKER_00", 1, "Dave")
    assert ok is False


@pytest.mark.asyncio
async def test_assign_speaker_crew_preserves_existing(storage: Storage, tmp_path: Path) -> None:
    """Assigning a second speaker preserves the first."""
    audio_id = await _create_audio_session(storage, tmp_path)
    tid = await _create_transcript_with_segments(storage, audio_id)
    uid1 = await _create_user(storage, "dave@boat.com", "Dave")
    uid2 = await _create_user(storage, "mike@boat.com", "Mike")

    await storage.assign_speaker_crew(tid, "SPEAKER_00", uid1, "Dave")
    await storage.assign_speaker_crew(tid, "SPEAKER_01", uid2, "Mike")

    speaker_map = await storage.get_speaker_map(tid)
    assert "SPEAKER_00" in speaker_map
    assert "SPEAKER_01" in speaker_map
    assert speaker_map["SPEAKER_00"]["name"] == "Dave"
    assert speaker_map["SPEAKER_01"]["name"] == "Mike"


@pytest.mark.asyncio
async def test_assign_speaker_crew_overwrite(storage: Storage, tmp_path: Path) -> None:
    """Reassigning a speaker overwrites the previous mapping."""
    audio_id = await _create_audio_session(storage, tmp_path)
    tid = await _create_transcript_with_segments(storage, audio_id)
    uid1 = await _create_user(storage, "dave@boat.com", "Dave")
    uid2 = await _create_user(storage, "mike@boat.com", "Mike")

    await storage.assign_speaker_crew(tid, "SPEAKER_00", uid1, "Dave")
    await storage.assign_speaker_crew(tid, "SPEAKER_00", uid2, "Mike")

    speaker_map = await storage.get_speaker_map(tid)
    assert speaker_map["SPEAKER_00"]["name"] == "Mike"


# ---------------------------------------------------------------------------
# Storage: speaker_map interop with anonymization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_speaker_map_coexists_with_anon(storage: Storage, tmp_path: Path) -> None:
    """speaker_map crew entries and speaker_anon_map redactions coexist."""
    audio_id = await _create_audio_session(storage, tmp_path)
    tid = await _create_transcript_with_segments(storage, audio_id)
    uid = await _create_user(storage, "dave@boat.com", "Dave")

    # Assign speaker 0 to crew
    await storage.assign_speaker_crew(tid, "SPEAKER_00", uid, "Dave")
    # Anonymize speaker 1
    await storage.anonymize_speaker(tid, "SPEAKER_01")

    t = await storage.get_transcript_with_anon(audio_id)
    assert t is not None
    segs = json.loads(t["segments_json"])
    # SPEAKER_00 should show as Dave (crew mapping)
    assert segs[0]["speaker"] == "Dave"
    # SPEAKER_01 should be redacted (anonymization takes priority)
    assert segs[1]["speaker"] == "REDACTED"
    assert segs[1]["text"] == "[REDACTED]"
    # SPEAKER_00 in third segment should also show as Dave
    assert segs[2]["speaker"] == "Dave"


@pytest.mark.asyncio
async def test_get_transcript_with_anon_applies_speaker_map(
    storage: Storage, tmp_path: Path
) -> None:
    """get_transcript_with_anon replaces speaker labels with crew names from speaker_map."""
    audio_id = await _create_audio_session(storage, tmp_path)
    tid = await _create_transcript_with_segments(storage, audio_id)
    uid = await _create_user(storage, "dave@boat.com", "Dave")

    await storage.assign_speaker_crew(tid, "SPEAKER_00", uid, "Dave")

    t = await storage.get_transcript_with_anon(audio_id)
    assert t is not None
    segs = json.loads(t["segments_json"])
    assert segs[0]["speaker"] == "Dave"
    assert segs[2]["speaker"] == "Dave"
    # Unmapped speaker stays as-is
    assert segs[1]["speaker"] == "SPEAKER_01"


# ---------------------------------------------------------------------------
# Storage: voice profiles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_voice_profile(storage: Storage) -> None:
    """Upserting a voice profile stores embedding + counts."""
    uid = await _create_user(storage, "dave@boat.com", "Dave")
    # Grant voice_profile consent
    await storage.set_crew_consent(uid, "voice_profile", True)

    embedding = b"\x00" * 128  # fake embedding
    await storage.upsert_voice_profile(uid, embedding, segment_count=30, session_count=2)

    profile = await storage.get_voice_profile(uid)
    assert profile is not None
    assert profile["embedding"] == embedding
    assert profile["segment_count"] == 30
    assert profile["session_count"] == 2


@pytest.mark.asyncio
async def test_upsert_voice_profile_updates(storage: Storage) -> None:
    """Upserting again updates the existing profile."""
    uid = await _create_user(storage, "dave@boat.com", "Dave")
    await storage.set_crew_consent(uid, "voice_profile", True)

    await storage.upsert_voice_profile(uid, b"\x01" * 128, segment_count=30, session_count=2)
    await storage.upsert_voice_profile(uid, b"\x02" * 128, segment_count=60, session_count=4)

    profile = await storage.get_voice_profile(uid)
    assert profile is not None
    assert profile["embedding"] == b"\x02" * 128
    assert profile["segment_count"] == 60


@pytest.mark.asyncio
async def test_delete_voice_profile(storage: Storage) -> None:
    """delete_voice_profile removes the profile."""
    uid = await _create_user(storage, "dave@boat.com", "Dave")
    await storage.set_crew_consent(uid, "voice_profile", True)
    await storage.upsert_voice_profile(uid, b"\x01" * 128, segment_count=30, session_count=2)

    deleted = await storage.delete_voice_profile(uid)
    assert deleted is True

    profile = await storage.get_voice_profile(uid)
    assert profile is None


@pytest.mark.asyncio
async def test_delete_voice_profile_not_found(storage: Storage) -> None:
    """delete_voice_profile returns False if no profile exists."""
    deleted = await storage.delete_voice_profile(999)
    assert deleted is False


@pytest.mark.asyncio
async def test_consent_revoke_deletes_voice_profile(storage: Storage) -> None:
    """Revoking voice_profile consent hard-deletes the voice profile."""
    uid = await _create_user(storage, "dave@boat.com", "Dave")
    await storage.set_crew_consent(uid, "voice_profile", True)
    await storage.upsert_voice_profile(uid, b"\x01" * 128, segment_count=30, session_count=2)

    # Revoke consent — should cascade delete
    await storage.revoke_voice_profile_consent(uid)

    profile = await storage.get_voice_profile(uid)
    assert profile is None
    # Consent should be revoked
    consents = await storage.get_crew_consents(uid)
    vp = [c for c in consents if c["consent_type"] == "voice_profile"]
    assert len(vp) == 1
    assert vp[0]["granted"] == 0


@pytest.mark.asyncio
async def test_revoke_voice_consent_clears_auto_speaker_map_entries(
    storage: Storage, tmp_path: Path
) -> None:
    """Revoking consent removes 'auto' speaker_map entries but keeps 'crew' ones."""
    audio_id = await _create_audio_session(storage, tmp_path)
    tid = await _create_transcript_with_segments(storage, audio_id)
    uid = await _create_user(storage, "dave@boat.com", "Dave")
    await storage.set_crew_consent(uid, "voice_profile", True)

    # Simulate both a manual crew assignment and an auto-match
    db = storage._conn()
    speaker_map = {
        "SPEAKER_00": {"type": "crew", "user_id": uid, "name": "Dave"},
        "SPEAKER_01": {"type": "auto", "user_id": uid, "name": "Dave", "confidence": 0.87},
    }
    await db.execute(
        "UPDATE transcripts SET speaker_map = ? WHERE id = ?",
        (json.dumps(speaker_map), tid),
    )
    await db.commit()

    await storage.revoke_voice_profile_consent(uid)

    smap = await storage.get_speaker_map(tid)
    # Manual crew assignment preserved
    assert smap["SPEAKER_00"]["type"] == "crew"
    # Auto entry removed
    assert "SPEAKER_01" not in smap


# ---------------------------------------------------------------------------
# API: assign-speaker endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_assign_speaker(storage: Storage, tmp_path: Path) -> None:
    """POST assign-speaker updates the speaker_map and returns it."""
    import httpx

    from helmlog.web import create_app

    audio_id = await _create_audio_session(storage, tmp_path)
    await _create_transcript_with_segments(storage, audio_id)
    uid = await _create_user(storage, "dave@boat.com", "Dave")

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/audio/{audio_id}/transcript/assign-speaker",
            json={"speaker_label": "SPEAKER_00", "user_id": uid},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["speaker_label"] == "SPEAKER_00"
    assert data["user_id"] == uid
    assert data["name"] == "Dave"


@pytest.mark.asyncio
async def test_api_assign_speaker_missing_label(storage: Storage, tmp_path: Path) -> None:
    """POST assign-speaker with missing speaker_label returns 422."""
    import httpx

    from helmlog.web import create_app

    audio_id = await _create_audio_session(storage, tmp_path)
    await _create_transcript_with_segments(storage, audio_id)

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/audio/{audio_id}/transcript/assign-speaker",
            json={"user_id": 1},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_api_assign_speaker_no_transcript(storage: Storage, tmp_path: Path) -> None:
    """POST assign-speaker returns 404 when no transcript exists."""
    import httpx

    from helmlog.web import create_app

    audio_id = await _create_audio_session(storage, tmp_path)

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/api/audio/{audio_id}/transcript/assign-speaker",
            json={"speaker_label": "SPEAKER_00", "user_id": 1},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_api_transcript_includes_speaker_map(storage: Storage, tmp_path: Path) -> None:
    """GET transcript includes speaker_map (without internal details) for UI rendering."""
    import httpx

    from helmlog.web import create_app

    audio_id = await _create_audio_session(storage, tmp_path)
    tid = await _create_transcript_with_segments(storage, audio_id)
    uid = await _create_user(storage, "dave@boat.com", "Dave")
    await storage.assign_speaker_crew(tid, "SPEAKER_00", uid, "Dave")

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/audio/{audio_id}/transcript")
    assert resp.status_code == 200
    data = resp.json()
    # speaker_map should be exposed for UI
    assert "speaker_map" in data
    assert data["speaker_map"]["SPEAKER_00"]["name"] == "Dave"


# ---------------------------------------------------------------------------
# API: retranscribe endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_retranscribe(storage: Storage, tmp_path: Path) -> None:
    """POST retranscribe deletes existing transcript and creates a new job."""
    from unittest.mock import AsyncMock, patch

    import httpx

    from helmlog.web import create_app

    audio_id = await _create_audio_session(storage, tmp_path)
    await _create_transcript_with_segments(storage, audio_id)

    # Verify transcript exists
    t = await storage.get_transcript(audio_id)
    assert t is not None
    assert t["status"] == "done"

    app = create_app(storage)
    with patch("helmlog.web.asyncio.create_task") as mock_create_task:
        mock_create_task.return_value = AsyncMock()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(f"/api/audio/{audio_id}/retranscribe")
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted"

    # Old transcript should be replaced with a new pending one
    t2 = await storage.get_transcript(audio_id)
    assert t2 is not None
    assert t2["status"] == "pending"


@pytest.mark.asyncio
async def test_api_retranscribe_no_session(storage: Storage, tmp_path: Path) -> None:
    """POST retranscribe returns 404 for nonexistent audio session."""
    import httpx

    from helmlog.web import create_app

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/audio/999/retranscribe")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# API: voice profile endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_delete_voice_profile(storage: Storage, tmp_path: Path) -> None:
    """DELETE voice profile removes it."""
    import httpx

    from helmlog.web import create_app

    uid = await _create_user(storage, "dave@boat.com", "Dave")
    await storage.set_crew_consent(uid, "voice_profile", True)
    await storage.upsert_voice_profile(uid, b"\x01" * 128, segment_count=30, session_count=2)

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.delete(f"/api/crew/{uid}/voice-profile")
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# Voice learning: cosine similarity + auto-match
# ---------------------------------------------------------------------------


def test_cosine_similarity_identical() -> None:
    """Identical embeddings have similarity 1.0."""
    import struct

    from helmlog.transcribe import _cosine_similarity

    emb = struct.pack("<4f", 1.0, 0.0, 0.0, 0.0)
    assert abs(_cosine_similarity(emb, emb) - 1.0) < 1e-6


def test_cosine_similarity_orthogonal() -> None:
    """Orthogonal embeddings have similarity 0.0."""
    import struct

    from helmlog.transcribe import _cosine_similarity

    a = struct.pack("<4f", 1.0, 0.0, 0.0, 0.0)
    b = struct.pack("<4f", 0.0, 1.0, 0.0, 0.0)
    assert abs(_cosine_similarity(a, b)) < 1e-6


def test_cosine_similarity_opposite() -> None:
    """Opposite embeddings have similarity -1.0."""
    import struct

    from helmlog.transcribe import _cosine_similarity

    a = struct.pack("<4f", 1.0, 0.0, 0.0, 0.0)
    b = struct.pack("<4f", -1.0, 0.0, 0.0, 0.0)
    assert abs(_cosine_similarity(a, b) + 1.0) < 1e-6


@pytest.mark.asyncio
async def test_auto_match_speakers_with_profiles(storage: Storage, tmp_path: Path) -> None:
    """Auto-match assigns speakers when profiles exist with high similarity."""
    import struct

    audio_id = await _create_audio_session(storage, tmp_path)
    tid = await _create_transcript_with_segments(storage, audio_id)
    uid = await _create_user(storage, "dave@boat.com", "Dave")

    # Grant consent and store a voice profile
    await storage.set_crew_consent(uid, "voice_profile", True)
    emb = struct.pack("<4f", 1.0, 0.0, 0.0, 0.0)
    await storage.upsert_voice_profile(uid, emb, segment_count=30, session_count=2)

    # Auto-match with identical embedding for SPEAKER_00
    from helmlog.transcribe import auto_match_speakers

    speaker_embs = {"SPEAKER_00": emb}
    matches = await auto_match_speakers(storage, tid, speaker_embs)

    assert "SPEAKER_00" in matches
    assert matches["SPEAKER_00"]["user_id"] == uid
    assert matches["SPEAKER_00"]["name"] == "Dave"
    assert matches["SPEAKER_00"]["confidence"] >= 0.7


@pytest.mark.asyncio
async def test_auto_match_no_profiles(storage: Storage, tmp_path: Path) -> None:
    """Auto-match returns empty when no voice profiles exist."""
    import struct

    audio_id = await _create_audio_session(storage, tmp_path)
    tid = await _create_transcript_with_segments(storage, audio_id)

    from helmlog.transcribe import auto_match_speakers

    emb = struct.pack("<4f", 1.0, 0.0, 0.0, 0.0)
    matches = await auto_match_speakers(storage, tid, {"SPEAKER_00": emb})
    assert matches == {}


@pytest.mark.asyncio
async def test_auto_match_low_confidence_excluded(storage: Storage, tmp_path: Path) -> None:
    """Auto-match excludes speakers below the low confidence threshold."""
    import struct

    audio_id = await _create_audio_session(storage, tmp_path)
    tid = await _create_transcript_with_segments(storage, audio_id)
    uid = await _create_user(storage, "dave@boat.com", "Dave")

    await storage.set_crew_consent(uid, "voice_profile", True)
    # Profile embedding orthogonal to speaker embedding → ~0.0 similarity
    profile_emb = struct.pack("<4f", 1.0, 0.0, 0.0, 0.0)
    speaker_emb = struct.pack("<4f", 0.0, 1.0, 0.0, 0.0)
    await storage.upsert_voice_profile(uid, profile_emb, segment_count=30, session_count=2)

    from helmlog.transcribe import auto_match_speakers

    matches = await auto_match_speakers(storage, tid, {"SPEAKER_00": speaker_emb})
    assert "SPEAKER_00" not in matches


@pytest.mark.asyncio
async def test_auto_match_does_not_overwrite_manual(storage: Storage, tmp_path: Path) -> None:
    """Auto-match does not overwrite existing manual crew assignments."""
    import struct

    audio_id = await _create_audio_session(storage, tmp_path)
    tid = await _create_transcript_with_segments(storage, audio_id)
    uid1 = await _create_user(storage, "dave@boat.com", "Dave")
    uid2 = await _create_user(storage, "mike@boat.com", "Mike")

    # Manual crew assignment for SPEAKER_00
    await storage.assign_speaker_crew(tid, "SPEAKER_00", uid1, "Dave")

    # Voice profile for Mike matches SPEAKER_00
    await storage.set_crew_consent(uid2, "voice_profile", True)
    emb = struct.pack("<4f", 1.0, 0.0, 0.0, 0.0)
    await storage.upsert_voice_profile(uid2, emb, segment_count=30, session_count=2)

    from helmlog.transcribe import auto_match_speakers

    await auto_match_speakers(storage, tid, {"SPEAKER_00": emb})
    # Auto-match should not have written over the manual assignment
    smap = await storage.get_speaker_map(tid)
    assert smap["SPEAKER_00"]["type"] == "crew"
    assert smap["SPEAKER_00"]["name"] == "Dave"


@pytest.mark.asyncio
async def test_maybe_build_profile_insufficient_data(storage: Storage) -> None:
    """maybe_build_voice_profile returns False when data is insufficient."""
    uid = await _create_user(storage, "dave@boat.com", "Dave")
    await storage.set_crew_consent(uid, "voice_profile", True)

    from helmlog.transcribe import maybe_build_voice_profile

    result = await maybe_build_voice_profile(storage, uid)
    assert result is False


@pytest.mark.asyncio
async def test_maybe_build_profile_no_consent(storage: Storage, tmp_path: Path) -> None:
    """maybe_build_voice_profile returns False without voice_profile consent."""
    uid = await _create_user(storage, "dave@boat.com", "Dave")
    # No consent granted

    from helmlog.transcribe import maybe_build_voice_profile

    result = await maybe_build_voice_profile(storage, uid)
    assert result is False


@pytest.mark.asyncio
async def test_api_revoke_voice_consent(storage: Storage, tmp_path: Path) -> None:
    """Revoking voice_profile consent via API deletes profile too."""
    import httpx

    from helmlog.web import create_app

    uid = await _create_user(storage, "dave@boat.com", "Dave")
    await storage.set_crew_consent(uid, "voice_profile", True)
    await storage.upsert_voice_profile(uid, b"\x01" * 128, segment_count=30, session_count=2)

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            f"/api/crew/{uid}/consents",
            json={"consent_type": "voice_profile", "granted": False},
        )
    assert resp.status_code == 200

    # Profile should be gone
    profile = await storage.get_voice_profile(uid)
    assert profile is None
