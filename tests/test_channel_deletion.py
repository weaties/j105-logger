"""Per-channel atomic deletion (#462 pt.7 / #499).

Exercise the full data-licensing deletion right for a single position in a
multi-channel recording: zero the channel in the WAV, drop its transcript
segments, drop its channel_map row, and write an audit entry — all atomic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import numpy as np
import pytest
import soundfile as sf

from helmlog.audio import AudioSession

if TYPE_CHECKING:
    from pathlib import Path

    from helmlog.storage import Storage


def _write_four_channel_wav(path: Path, *, sample_rate: int = 48000, seconds: float = 0.25) -> None:
    n = int(sample_rate * seconds)
    t = np.arange(n, dtype=np.float32) / sample_rate
    data = np.stack(
        [
            0.5 * np.sin(2 * np.pi * 220.0 * t),
            0.5 * np.sin(2 * np.pi * 330.0 * t),
            0.5 * np.sin(2 * np.pi * 440.0 * t),
            0.5 * np.sin(2 * np.pi * 550.0 * t),
        ],
        axis=1,
    ).astype(np.float32)
    sf.write(str(path), data, sample_rate, subtype="PCM_16")


async def _seed_multichannel(storage: Storage, wav_path: Path) -> tuple[int, int]:
    """Write audio session, device identity, channel_map, transcript, and
    4 segments (one per channel). Return (audio_session_id, transcript_id)."""
    session = AudioSession(
        file_path=str(wav_path),
        device_name="Gordik 4ch",
        start_utc=datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC),
        end_utc=datetime(2024, 6, 15, 12, 0, 1, tzinfo=UTC),
        sample_rate=48000,
        channels=4,
    )
    audio_session_id = await storage.write_audio_session(session)
    await storage.set_audio_session_device(
        audio_session_id,
        vendor_id=0x1234,
        product_id=0x5678,
        serial="ABC",
        usb_port_path="1-1.2",
    )
    await storage.set_channel_map(
        vendor_id=0x1234,
        product_id=0x5678,
        serial="ABC",
        usb_port_path="1-1.2",
        mapping={0: "helm", 1: "tactician", 2: "trim", 3: "bow"},
        audio_session_id=audio_session_id,
    )
    transcript_id = await storage.create_transcript_job(audio_session_id, model="tiny.en")
    segments = [
        {
            "segment_index": i,
            "start_time": float(i) * 0.05,
            "end_time": float(i) * 0.05 + 0.04,
            "text": f"channel {i} test",
            "speaker": pos,
            "channel_index": i,
            "position_name": pos,
        }
        for i, pos in enumerate(["helm", "tactician", "trim", "bow"])
    ]
    await storage.insert_transcript_segments(transcript_id, segments)
    return audio_session_id, transcript_id


@pytest.mark.asyncio
async def test_delete_audio_channel_zeros_and_cascades(tmp_path: Path, storage: Storage) -> None:
    wav = tmp_path / "rec.wav"
    _write_four_channel_wav(wav)
    audio_session_id, transcript_id = await _seed_multichannel(storage, wav)

    original, _ = sf.read(str(wav), dtype="float32", always_2d=True)
    assert original.shape[1] == 4

    await storage.delete_audio_channel(
        audio_session_id,
        channel_index=1,
        user_id=None,
        reason="crew member deletion request",
    )

    # WAV: still 4 channels, channel 1 is silent, others untouched.
    after, sr = sf.read(str(wav), dtype="float32", always_2d=True)
    assert after.shape == original.shape
    assert sr == 48000
    np.testing.assert_array_equal(after[:, 1], np.zeros_like(after[:, 1]))
    for ch in (0, 2, 3):
        np.testing.assert_allclose(after[:, ch], original[:, ch], atol=1e-3)

    # transcript_segments: channel 1 gone, others remain
    remaining = await storage.list_transcript_segments(transcript_id)
    indexes = sorted(s["channel_index"] for s in remaining)
    assert indexes == [0, 2, 3]

    # channel_map: channel 1 row gone, others remain
    cmap = await storage.get_channel_map_for_audio_session(audio_session_id)
    assert 1 not in cmap
    assert set(cmap.keys()) == {0, 2, 3}

    # Audit log
    entries = await storage.list_audit_log(limit=5)
    assert any(
        e["action"] == "audio_channel_delete"
        and f'"audio_session_id": {audio_session_id}' in (e["detail"] or "")
        and '"channel_index": 1' in (e["detail"] or "")
        for e in entries
    )


@pytest.mark.asyncio
async def test_delete_audio_channel_rolls_back_on_missing_file(
    tmp_path: Path, storage: Storage
) -> None:
    wav = tmp_path / "rec.wav"
    _write_four_channel_wav(wav)
    audio_session_id, transcript_id = await _seed_multichannel(storage, wav)

    wav.unlink()  # simulate missing file

    with pytest.raises(FileNotFoundError):
        await storage.delete_audio_channel(audio_session_id, channel_index=2, user_id=None)

    # DB state must be untouched
    remaining = await storage.list_transcript_segments(transcript_id)
    assert sorted(s["channel_index"] for s in remaining) == [0, 1, 2, 3]
    cmap = await storage.get_channel_map_for_audio_session(audio_session_id)
    assert set(cmap.keys()) == {0, 1, 2, 3}


@pytest.mark.asyncio
async def test_delete_audio_channel_rolls_back_on_db_error(
    tmp_path: Path, storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    wav = tmp_path / "rec.wav"
    _write_four_channel_wav(wav)
    audio_session_id, transcript_id = await _seed_multichannel(storage, wav)
    original, _ = sf.read(str(wav), dtype="float32", always_2d=True)

    # Force the audit insert (last DB step) to raise.
    real_log = storage.log_action

    async def boom(*args: object, **kwargs: object) -> int:
        raise RuntimeError("audit log exploded")

    monkeypatch.setattr(storage, "log_action", boom)

    with pytest.raises(RuntimeError, match="audit log exploded"):
        await storage.delete_audio_channel(audio_session_id, channel_index=0, user_id=None)

    # WAV unchanged
    after, _ = sf.read(str(wav), dtype="float32", always_2d=True)
    np.testing.assert_array_equal(after, original)

    # DB state unchanged
    remaining = await storage.list_transcript_segments(transcript_id)
    assert sorted(s["channel_index"] for s in remaining) == [0, 1, 2, 3]
    cmap = await storage.get_channel_map_for_audio_session(audio_session_id)
    assert set(cmap.keys()) == {0, 1, 2, 3}

    # No stray tmp files
    strays = list(tmp_path.glob("*.tmp*"))
    assert strays == []
    _ = real_log  # keep ref


@pytest.mark.asyncio
async def test_delete_audio_channel_rejects_out_of_range(tmp_path: Path, storage: Storage) -> None:
    wav = tmp_path / "rec.wav"
    _write_four_channel_wav(wav)
    audio_session_id, _ = await _seed_multichannel(storage, wav)

    with pytest.raises(ValueError):
        await storage.delete_audio_channel(audio_session_id, channel_index=9, user_id=None)


@pytest.mark.asyncio
async def test_delete_audio_channel_unknown_session(tmp_path: Path, storage: Storage) -> None:
    with pytest.raises(ValueError):
        await storage.delete_audio_channel(99999, channel_index=0, user_id=None)
