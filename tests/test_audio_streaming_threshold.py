"""Session detail returns ``use_streaming_audio`` hint for long sibling sessions (#648).

Session pages with two or more sibling WAVs longer than
``AUDIO_STREAM_THRESHOLD_MINUTES`` (default 45) must stream via
``<audio>`` + ``MediaElementAudioSourceNode`` instead of ``decodeAudioData``;
the detail endpoint surfaces the per-session verdict so the frontend can
branch without knowing the threshold.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING

import httpx
import pytest

from helmlog.audio import AudioSession
from helmlog.web import create_app

if TYPE_CHECKING:
    from helmlog.storage import Storage

pytestmark = pytest.mark.asyncio


async def _seed_race_with_siblings(
    storage: Storage, tmp_path: Path, *, duration_minutes: int, n_siblings: int = 2
) -> int:
    """Create a race with N sibling audio sessions of the given duration."""
    start = datetime(2026, 4, 21, 10, 0, 0, tzinfo=UTC)
    end = start + timedelta(minutes=duration_minutes)

    db = storage._conn()
    cur = await db.execute(
        "INSERT INTO races (name, event, race_num, date, session_type, start_utc, end_utc)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            f"LongRace-{duration_minutes}min",
            "Bench",
            1,
            start.date().isoformat(),
            "race",
            start.isoformat(),
            end.isoformat(),
        ),
    )
    await db.commit()
    race_id = cur.lastrowid
    assert race_id is not None

    for ordinal in range(n_siblings):
        wav = tmp_path / f"sib{ordinal}.wav"
        wav.write_bytes(b"RIFF0000WAVEfmt ")
        await storage.write_audio_session(
            AudioSession(
                file_path=str(wav),
                device_name=f"Fake sib{ordinal}",
                start_utc=start,
                end_utc=end,
                sample_rate=48000,
                channels=1,
                vendor_id=0x1234,
                product_id=0x5678,
                serial=f"sn{ordinal}",
                usb_port_path=f"1-{ordinal + 1}",
                capture_group_id="grp-648",
                capture_ordinal=ordinal,
            ),
            race_id=race_id,
            session_type="race",
            name=f"LongRace-{duration_minutes}min",
        )
    return race_id


async def _seed_single_audio_race(
    storage: Storage, tmp_path: Path, *, duration_minutes: int
) -> int:
    """Race with a single (non-sibling) audio session — long but single-device."""
    start = datetime(2026, 4, 21, 10, 0, 0, tzinfo=UTC)
    end = start + timedelta(minutes=duration_minutes)
    db = storage._conn()
    cur = await db.execute(
        "INSERT INTO races (name, event, race_num, date, session_type, start_utc, end_utc)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            f"SingleLong-{duration_minutes}min",
            "Bench",
            2,
            start.date().isoformat(),
            "race",
            start.isoformat(),
            end.isoformat(),
        ),
    )
    await db.commit()
    race_id = cur.lastrowid
    assert race_id is not None

    wav = tmp_path / "single.wav"
    wav.write_bytes(b"RIFF0000WAVEfmt ")
    await storage.write_audio_session(
        AudioSession(
            file_path=str(wav),
            device_name="Single multichannel",
            start_utc=start,
            end_utc=end,
            sample_rate=48000,
            channels=4,  # multi-channel on one device; no sibling group
        ),
        race_id=race_id,
        session_type="race",
        name=f"SingleLong-{duration_minutes}min",
    )
    return race_id


async def _get_detail(storage: Storage, race_id: int) -> dict:
    app = create_app(storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/sessions/{race_id}/detail")
    assert resp.status_code == 200
    return resp.json()


async def test_short_sibling_session_uses_buffer_mode(storage: Storage, tmp_path: Path) -> None:
    """30-min session < 45-min default → use_streaming_audio is False."""
    race_id = await _seed_race_with_siblings(storage, tmp_path, duration_minutes=30)
    data = await _get_detail(storage, race_id)
    assert data["audio_channels"] == 2
    assert data["use_streaming_audio"] is False


async def test_long_sibling_session_triggers_streaming(storage: Storage, tmp_path: Path) -> None:
    """82-min session (like the #648 repro race) ≥ 45-min default → streaming mode."""
    race_id = await _seed_race_with_siblings(storage, tmp_path, duration_minutes=82)
    data = await _get_detail(storage, race_id)
    assert data["audio_channels"] == 2
    assert data["use_streaming_audio"] is True


async def test_threshold_override_via_env(
    storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Custom AUDIO_STREAM_THRESHOLD_MINUTES=20 → 25-min session flips to streaming."""
    monkeypatch.setenv("AUDIO_STREAM_THRESHOLD_MINUTES", "20")
    race_id = await _seed_race_with_siblings(storage, tmp_path, duration_minutes=25)
    data = await _get_detail(storage, race_id)
    assert data["use_streaming_audio"] is True


async def test_invalid_threshold_falls_back_to_default(
    storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-integer env var → fall back to 45-min default, 30-min stays buffer."""
    monkeypatch.setenv("AUDIO_STREAM_THRESHOLD_MINUTES", "not-a-number")
    race_id = await _seed_race_with_siblings(storage, tmp_path, duration_minutes=30)
    data = await _get_detail(storage, race_id)
    assert data["use_streaming_audio"] is False


async def test_single_device_long_session_does_not_stream(storage: Storage, tmp_path: Path) -> None:
    """Streaming path only applies to sibling captures; single-device multi-channel
    sessions stay on the decodeAudioData path regardless of duration because the
    channel splitter that drives isolation only works with a decoded AudioBuffer."""
    race_id = await _seed_single_audio_race(storage, tmp_path, duration_minutes=90)
    data = await _get_detail(storage, race_id)
    assert data["use_streaming_audio"] is False


async def test_no_audio_session_use_streaming_false(storage: Storage) -> None:
    """Race with no audio rows at all → use_streaming_audio is False."""
    db = storage._conn()
    start = datetime(2026, 4, 21, 10, 0, 0, tzinfo=UTC)
    end = start + timedelta(minutes=90)
    cur = await db.execute(
        "INSERT INTO races (name, event, race_num, date, session_type, start_utc, end_utc)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "NoAudio",
            "Bench",
            3,
            start.date().isoformat(),
            "race",
            start.isoformat(),
            end.isoformat(),
        ),
    )
    await db.commit()
    race_id = cur.lastrowid
    assert race_id is not None
    data = await _get_detail(storage, race_id)
    assert data["has_audio"] is False
    assert data["use_streaming_audio"] is False
