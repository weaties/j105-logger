"""Sibling-card transcription (#509 chunk 2).

Verifies that when an audio_sessions row carries a ``capture_group_id``,
``transcribe_session`` runs the single-channel whisper path, tags each
segment with the sibling's ``capture_ordinal`` + configured position
name, and that the HTTP transcribe/retranscribe endpoints fan out to
every sibling in the group.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from helmlog.audio import AudioSession
from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_sibling_pair(storage: Storage, tmp_path: Path) -> tuple[int, int, str]:
    """Seed two mono sibling audio_sessions sharing a capture_group_id.

    Returns ``(primary_id, secondary_id, capture_group_id)``. Both rows
    have a matching ``channel_map`` entry mapping channel 0 to a
    position, so the sibling tag lookup resolves.
    """
    group_id = "grp-test"
    wav_a = tmp_path / "sib0.wav"
    wav_b = tmp_path / "sib1.wav"
    wav_a.write_bytes(b"RIFF0000WAVEfmt ")
    wav_b.write_bytes(b"RIFF0000WAVEfmt ")

    def _sess(path: Path, ordinal: int, serial: str) -> AudioSession:
        return AudioSession(
            file_path=str(path),
            device_name=f"Jieli card {ordinal}",
            start_utc=datetime(2026, 4, 12, 16, 30, 0, tzinfo=UTC),
            end_utc=datetime(2026, 4, 12, 16, 30, 30, tzinfo=UTC),
            sample_rate=48000,
            channels=1,
            vendor_id=0x3634,
            product_id=0x4155,
            serial=serial,
            usb_port_path=f"1-{ordinal + 1}",
            capture_group_id=group_id,
            capture_ordinal=ordinal,
        )

    primary_id = await storage.write_audio_session(_sess(wav_a, 0, "AAA"))
    secondary_id = await storage.write_audio_session(_sess(wav_b, 1, "BBB"))
    # Match the session-override write path used by the admin UI.
    await storage.set_audio_session_device(
        primary_id, vendor_id=0x3634, product_id=0x4155, serial="AAA", usb_port_path="1-1"
    )
    await storage.set_audio_session_device(
        secondary_id,
        vendor_id=0x3634,
        product_id=0x4155,
        serial="BBB",
        usb_port_path="1-2",
    )
    await storage.set_channel_map(
        vendor_id=0x3634,
        product_id=0x4155,
        serial="AAA",
        usb_port_path="1-1",
        mapping={0: "Helm pair"},
        audio_session_id=primary_id,
    )
    await storage.set_channel_map(
        vendor_id=0x3634,
        product_id=0x4155,
        serial="BBB",
        usb_port_path="1-2",
        mapping={0: "Bow pair"},
        audio_session_id=secondary_id,
    )
    return primary_id, secondary_id, group_id


# ---------------------------------------------------------------------------
# transcribe_session() — sibling tag + relational persistence
# ---------------------------------------------------------------------------


async def test_transcribe_session_tags_sibling_segments(storage: Storage, tmp_path: Path) -> None:
    """A mono sibling gets channel_index=capture_ordinal and position from channel_map."""
    primary_id, secondary_id, _ = await _make_sibling_pair(storage, tmp_path)
    transcript_id = await storage.create_transcript_job(secondary_id, "base")

    fake_segs = [(0.0, 1.5, "bowman ready"), (2.0, 3.0, "jib in")]
    with patch("helmlog.transcribe._run_whisper_segments", return_value=fake_segs):
        from helmlog.transcribe import transcribe_session

        await transcribe_session(storage, secondary_id, transcript_id, model_size="base")

    rows = await storage.list_transcript_segments(transcript_id)
    assert len(rows) == 2
    assert [r["text"] for r in rows] == ["bowman ready", "jib in"]
    # Ordinal 1 → virtual channel index 1.
    assert all(r["channel_index"] == 1 for r in rows)
    assert all(r["position_name"] == "Bow pair" for r in rows)
    assert all(r["speaker"] == "Bow pair" for r in rows)


async def test_transcribe_session_sibling_falls_back_to_sibN_when_unmapped(
    storage: Storage, tmp_path: Path
) -> None:
    """No channel_map entry → fall back to sib{ordinal} label."""
    wav = tmp_path / "loose.wav"
    wav.write_bytes(b"RIFF0000WAVEfmt ")
    session = AudioSession(
        file_path=str(wav),
        device_name="Loose card",
        start_utc=datetime(2026, 4, 12, 16, 30, 0, tzinfo=UTC),
        end_utc=datetime(2026, 4, 12, 16, 30, 10, tzinfo=UTC),
        sample_rate=48000,
        channels=1,
        capture_group_id="grp-loose",
        capture_ordinal=3,
    )
    sid = await storage.write_audio_session(session)
    tid = await storage.create_transcript_job(sid, "base")
    with patch("helmlog.transcribe._run_whisper_segments", return_value=[(0.0, 1.0, "hi")]):
        from helmlog.transcribe import transcribe_session

        await transcribe_session(storage, sid, tid, model_size="base")
    rows = await storage.list_transcript_segments(tid)
    assert rows[0]["position_name"] == "sib3"
    assert rows[0]["channel_index"] == 3


# ---------------------------------------------------------------------------
# POST /api/audio/{id}/transcribe — sibling fan-out
# ---------------------------------------------------------------------------


async def test_transcribe_endpoint_fans_out_to_all_siblings(
    storage: Storage, tmp_path: Path
) -> None:
    """Hitting /transcribe on one sibling creates jobs for every member."""
    primary_id, secondary_id, _ = await _make_sibling_pair(storage, tmp_path)
    app = create_app(storage)

    with patch("helmlog.web.asyncio.create_task") as mock_create_task:
        mock_create_task.return_value = AsyncMock()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(f"/api/audio/{primary_id}/transcribe")

    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["sibling_count"] == 2
    # Two transcribe_session coroutines should have been enqueued.
    assert mock_create_task.call_count == 2

    # Both siblings have a transcripts row now.
    primary_trans = await storage.get_transcript(primary_id)
    secondary_trans = await storage.get_transcript(secondary_id)
    assert primary_trans is not None
    assert secondary_trans is not None


async def test_transcribe_endpoint_single_session_unchanged(
    storage: Storage, tmp_path: Path
) -> None:
    """Non-sibling sessions still return a single-job response."""
    wav = tmp_path / "solo.wav"
    wav.write_bytes(b"RIFF0000WAVEfmt ")
    session = AudioSession(
        file_path=str(wav),
        device_name="Built-in",
        start_utc=datetime(2026, 4, 12, 16, 30, 0, tzinfo=UTC),
        end_utc=datetime(2026, 4, 12, 16, 30, 10, tzinfo=UTC),
        sample_rate=48000,
        channels=1,
    )
    sid = await storage.write_audio_session(session)
    app = create_app(storage)
    with patch("helmlog.web.asyncio.create_task") as mock_create_task:
        mock_create_task.return_value = AsyncMock()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(f"/api/audio/{sid}/transcribe")
    assert resp.status_code == 202
    assert resp.json()["sibling_count"] == 1
    assert mock_create_task.call_count == 1


async def test_retranscribe_endpoint_fans_out_to_all_siblings(
    storage: Storage, tmp_path: Path
) -> None:
    """Retranscribe deletes all sibling transcripts and relaunches jobs for each."""
    primary_id, secondary_id, _ = await _make_sibling_pair(storage, tmp_path)
    # Seed an existing transcript job on both siblings.
    t1 = await storage.create_transcript_job(primary_id, "base")
    t2 = await storage.create_transcript_job(secondary_id, "base")

    app = create_app(storage)
    with patch("helmlog.web.asyncio.create_task") as mock_create_task:
        mock_create_task.return_value = AsyncMock()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(f"/api/audio/{secondary_id}/retranscribe")

    assert resp.status_code == 202
    data = resp.json()
    assert data["sibling_count"] == 2
    # New jobs were created — ids should differ from the old ones.
    primary_trans = await storage.get_transcript(primary_id)
    secondary_trans = await storage.get_transcript(secondary_id)
    assert primary_trans is not None
    assert secondary_trans is not None
    assert primary_trans["id"] not in {t1, t2}
    assert secondary_trans["id"] not in {t1, t2}
    assert mock_create_task.call_count == 2
