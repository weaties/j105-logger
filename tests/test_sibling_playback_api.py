"""Sibling-card playback API (#509 chunk 3).

Verifies:
- ``GET /api/sessions/{id}`` includes an ``audio_siblings`` block when the
  race's audio is a sibling capture, with stream URLs and position names
  in ordinal order; ``audio_channels`` reflects the sibling count so the
  session.js pt.6 multi-channel gate trips.
- ``GET /api/audio/{id}/transcript`` returns a merged, time-sorted
  ``segments`` array combining every sibling's transcript when the
  target belongs to a capture group.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING

import httpx
import pytest

from helmlog.audio import AudioSession
from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage

pytestmark = pytest.mark.asyncio


_START_UTC = datetime(2026, 4, 12, 16, 30, 0, tzinfo=UTC)
_END_UTC = datetime(2026, 4, 12, 16, 30, 30, tzinfo=UTC)


async def _seed_race_with_sibling_audio(storage: Storage, tmp_path: Path) -> tuple[int, int, int]:
    """Create a race + two mono sibling audio sessions. Returns ids."""
    db = storage._conn()
    cur = await db.execute(
        "INSERT INTO races"
        " (name, event, race_num, date, session_type, start_utc, end_utc)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "Test Race",
            "Tuesday Night",
            1,
            _START_UTC.date().isoformat(),
            "race",
            _START_UTC.isoformat(),
            _END_UTC.isoformat(),
        ),
    )
    await db.commit()
    race_id = cur.lastrowid
    assert race_id is not None

    wav_a = tmp_path / "sib0.wav"
    wav_b = tmp_path / "sib1.wav"
    wav_a.write_bytes(b"RIFF0000WAVEfmt ")
    wav_b.write_bytes(b"RIFF0000WAVEfmt ")

    def _sess(path: Path, ordinal: int, serial: str) -> AudioSession:
        return AudioSession(
            file_path=str(path),
            device_name=f"Jieli {ordinal}",
            start_utc=_START_UTC,
            end_utc=_END_UTC,
            sample_rate=48000,
            channels=1,
            vendor_id=0x3634,
            product_id=0x4155,
            serial=serial,
            usb_port_path=f"1-{ordinal + 1}",
            capture_group_id="grp-xyz",
            capture_ordinal=ordinal,
        )

    primary_id = await storage.write_audio_session(
        _sess(wav_a, 0, "AAA"), race_id=race_id, session_type="race", name="Test Race"
    )
    secondary_id = await storage.write_audio_session(
        _sess(wav_b, 1, "BBB"), race_id=race_id, session_type="race", name="Test Race"
    )
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
    return race_id, primary_id, secondary_id


# ---------------------------------------------------------------------------
# /api/sessions/{id} — audio_siblings block
# ---------------------------------------------------------------------------


async def test_session_detail_returns_sibling_audio_group(storage: Storage, tmp_path: Path) -> None:
    race_id, primary_id, secondary_id = await _seed_race_with_sibling_audio(storage, tmp_path)

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{race_id}/detail")
    assert resp.status_code == 200
    data = resp.json()

    assert data["has_audio"] is True
    # Primary is ordinal 0 so session.js keeps the scalar audio_session_id.
    assert data["audio_session_id"] == primary_id
    # Exposed sibling count so the pt.6 multi-channel gate trips.
    assert data["audio_channels"] == 2
    # New sibling block.
    siblings = data["audio_siblings"]
    assert len(siblings) == 2
    assert [s["ordinal"] for s in siblings] == [0, 1]
    assert [s["audio_session_id"] for s in siblings] == [primary_id, secondary_id]
    assert siblings[0]["position_name"] == "Helm pair"
    assert siblings[1]["position_name"] == "Bow pair"
    assert siblings[0]["stream_url"] == f"/api/audio/{primary_id}/stream"
    assert siblings[1]["stream_url"] == f"/api/audio/{secondary_id}/stream"


async def test_session_detail_single_session_no_sibling_block(
    storage: Storage, tmp_path: Path
) -> None:
    """Non-sibling races get the legacy shape (no audio_siblings key or empty list)."""
    db = storage._conn()
    cur = await db.execute(
        "INSERT INTO races (name, event, race_num, date, session_type, start_utc, end_utc)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "Legacy Race",
            "Tuesday Night",
            2,
            _START_UTC.date().isoformat(),
            "race",
            _START_UTC.isoformat(),
            _END_UTC.isoformat(),
        ),
    )
    await db.commit()
    race_id = cur.lastrowid
    assert race_id is not None

    wav = tmp_path / "legacy.wav"
    wav.write_bytes(b"RIFF0000WAVEfmt ")
    await storage.write_audio_session(
        AudioSession(
            file_path=str(wav),
            device_name="Built-in",
            start_utc=_START_UTC,
            end_utc=_END_UTC,
            sample_rate=48000,
            channels=1,
        ),
        race_id=race_id,
        session_type="race",
        name="Legacy Race",
    )

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{race_id}/detail")
    data = resp.json()
    assert data["has_audio"] is True
    assert data["audio_channels"] == 1
    assert data.get("audio_siblings") in (None, [])


# ---------------------------------------------------------------------------
# /api/audio/{id}/transcript — sibling merge
# ---------------------------------------------------------------------------


async def test_transcript_endpoint_merges_sibling_segments(
    storage: Storage, tmp_path: Path
) -> None:
    _, primary_id, secondary_id = await _seed_race_with_sibling_audio(storage, tmp_path)

    # Primary (ordinal 0, helm pair) — two segments
    t0 = await storage.create_transcript_job(primary_id, "base")
    await storage.update_transcript(
        t0,
        status="done",
        text="helm one helm two",
        segments_json=json.dumps(
            [
                {
                    "start": 0.0,
                    "end": 1.0,
                    "text": "helm one",
                    "channel_index": 0,
                    "position_name": "Helm pair",
                    "speaker": "Helm pair",
                },
                {
                    "start": 5.0,
                    "end": 6.0,
                    "text": "helm two",
                    "channel_index": 0,
                    "position_name": "Helm pair",
                    "speaker": "Helm pair",
                },
            ]
        ),
    )
    # Secondary (ordinal 1, bow pair) — one segment that should sort in the middle
    t1 = await storage.create_transcript_job(secondary_id, "base")
    await storage.update_transcript(
        t1,
        status="done",
        text="bow one",
        segments_json=json.dumps(
            [
                {
                    "start": 2.5,
                    "end": 3.5,
                    "text": "bow one",
                    "channel_index": 1,
                    "position_name": "Bow pair",
                    "speaker": "Bow pair",
                },
            ]
        ),
    )

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/audio/{primary_id}/transcript")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "done"
    # Merged across siblings, sorted by start_time.
    assert [s["text"] for s in data["segments"]] == [
        "helm one",
        "bow one",
        "helm two",
    ]
    assert [s["channel_index"] for s in data["segments"]] == [0, 1, 0]


async def test_transcript_endpoint_sibling_single_completed(
    storage: Storage, tmp_path: Path
) -> None:
    """Only one sibling done yet — we still return its segments."""
    _, primary_id, secondary_id = await _seed_race_with_sibling_audio(storage, tmp_path)
    t0 = await storage.create_transcript_job(primary_id, "base")
    await storage.update_transcript(
        t0,
        status="done",
        text="only helm",
        segments_json=json.dumps(
            [
                {
                    "start": 0.0,
                    "end": 1.0,
                    "text": "only helm",
                    "channel_index": 0,
                    "position_name": "Helm pair",
                    "speaker": "Helm pair",
                }
            ]
        ),
    )
    # Secondary: pending, no segments yet
    await storage.create_transcript_job(secondary_id, "base")

    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/audio/{primary_id}/transcript")
    data = resp.json()
    assert len(data["segments"]) == 1
    assert data["segments"][0]["text"] == "only helm"
