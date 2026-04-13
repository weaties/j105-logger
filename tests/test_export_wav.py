"""WAV export preserves all channels (#462 pt.7 / #499)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import numpy as np
import pytest
import soundfile as sf

from helmlog.audio import AudioSession
from helmlog.export import export_wav

if TYPE_CHECKING:
    from pathlib import Path

    from helmlog.storage import Storage


def _write_four_channel_wav(path: Path, *, sample_rate: int = 48000, seconds: float = 0.5) -> None:
    n = int(sample_rate * seconds)
    t = np.arange(n, dtype=np.float32) / sample_rate
    data = np.stack(
        [
            np.sin(2 * np.pi * 220.0 * t),
            np.sin(2 * np.pi * 330.0 * t),
            np.sin(2 * np.pi * 440.0 * t),
            np.sin(2 * np.pi * 550.0 * t),
        ],
        axis=1,
    ).astype(np.float32)
    sf.write(str(path), data, sample_rate, subtype="PCM_16")


async def _seed_session(storage: Storage, file_path: Path, channels: int = 4) -> int:
    session = AudioSession(
        file_path=str(file_path),
        device_name="TestDev",
        start_utc=datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC),
        end_utc=datetime(2024, 6, 15, 12, 0, 1, tzinfo=UTC),
        sample_rate=48000,
        channels=channels,
    )
    return await storage.write_audio_session(session)


@pytest.mark.asyncio
async def test_export_wav_preserves_all_four_channels(tmp_path: Path, storage: Storage) -> None:
    src = tmp_path / "recording.wav"
    _write_four_channel_wav(src)
    audio_session_id = await _seed_session(storage, src)

    dst = tmp_path / "export.wav"
    await export_wav(storage, audio_session_id, dst)

    assert dst.exists()
    info = sf.info(str(dst))
    assert info.channels == 4
    assert info.samplerate == 48000

    orig, _ = sf.read(str(src), dtype="float32", always_2d=True)
    out, _ = sf.read(str(dst), dtype="float32", always_2d=True)
    assert orig.shape == out.shape
    np.testing.assert_array_equal(orig, out)


@pytest.mark.asyncio
async def test_export_wav_missing_session_raises(tmp_path: Path, storage: Storage) -> None:
    dst = tmp_path / "nope.wav"
    with pytest.raises(ValueError):
        await export_wav(storage, 9999, dst)
    assert not dst.exists()


@pytest.mark.asyncio
async def test_export_wav_missing_source_file_raises(tmp_path: Path, storage: Storage) -> None:
    ghost = tmp_path / "ghost.wav"
    audio_session_id = await _seed_session(storage, ghost)

    dst = tmp_path / "out.wav"
    with pytest.raises(FileNotFoundError):
        await export_wav(storage, audio_session_id, dst)
    assert not dst.exists()
