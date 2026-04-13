"""Sibling-card per-receiver deletion dispatch (#509 chunk 4).

When ``delete_audio_channel`` is called on a session that belongs to a
capture group, the ``channel_index`` parameter identifies a *sibling*
(by ``capture_ordinal``) rather than a channel within a multi-channel
WAV. The dispatch deletes that whole sibling — its audio_sessions row,
its transcript, its channel_map entries, and its WAV file — while
leaving every other sibling in the group intact.
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


def _write_mono_wav(path: Path, *, frequency: float = 440.0, seconds: float = 0.1) -> None:
    n = int(48000 * seconds)
    t = np.arange(n, dtype=np.float32) / 48000
    data = 0.5 * np.sin(2 * np.pi * frequency * t)
    sf.write(str(path), data.astype(np.float32), 48000, subtype="PCM_16")


async def _seed_sibling_pair(storage: Storage, tmp_path: Path) -> tuple[int, int, str]:
    """Create a 2-sibling capture group with channel maps + per-sibling transcripts."""
    wav_a = tmp_path / "sib0.wav"
    wav_b = tmp_path / "sib1.wav"
    _write_mono_wav(wav_a, frequency=220.0)
    _write_mono_wav(wav_b, frequency=440.0)

    group_id = "grp-delete-test"

    def _sess(path: Path, ordinal: int, serial: str) -> AudioSession:
        return AudioSession(
            file_path=str(path),
            device_name=f"Jieli {ordinal}",
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

    await storage.set_audio_session_device(
        primary_id, vendor_id=0x3634, product_id=0x4155, serial="AAA", usb_port_path="1-1"
    )
    await storage.set_audio_session_device(
        secondary_id, vendor_id=0x3634, product_id=0x4155, serial="BBB", usb_port_path="1-2"
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

    # Seed one transcript + one segment per sibling so we can observe cascade.
    for sid, label in ((primary_id, "Helm pair"), (secondary_id, "Bow pair")):
        tid = await storage.create_transcript_job(sid, "base")
        await storage.insert_transcript_segments(
            tid,
            [
                {
                    "segment_index": 0,
                    "start_time": 0.0,
                    "end_time": 0.1,
                    "text": f"{label} test",
                    "speaker": label,
                    "channel_index": 0,
                    "position_name": label,
                }
            ],
        )

    return primary_id, secondary_id, group_id


@pytest.mark.asyncio
async def test_delete_audio_channel_on_sibling_deletes_only_that_receiver(
    tmp_path: Path, storage: Storage
) -> None:
    primary_id, secondary_id, group_id = await _seed_sibling_pair(storage, tmp_path)
    primary_wav = (await storage.get_audio_session_row(primary_id))["file_path"]
    secondary_wav = (await storage.get_audio_session_row(secondary_id))["file_path"]

    # Delete the Bow pair (ordinal 1) via the pt.7 API.
    await storage.delete_audio_channel(
        primary_id, channel_index=1, user_id=None, reason="speaker request"
    )

    # Secondary sibling row + file + channel_map + transcript must be gone.
    assert await storage.get_audio_session_row(secondary_id) is None
    from pathlib import Path as _P

    assert not _P(secondary_wav).exists()
    # Primary sibling is untouched.
    primary_row = await storage.get_audio_session_row(primary_id)
    assert primary_row is not None
    assert _P(primary_wav).exists()
    # Primary still in the capture group alone.
    remaining = await storage.list_capture_group_siblings(group_id)
    assert [r["id"] for r in remaining] == [primary_id]
    assert [r["capture_ordinal"] for r in remaining] == [0]
    # Primary's channel map entry still present.
    cmap = await storage.get_channel_map_for_audio_session(primary_id)
    assert cmap == {0: "Helm pair"}

    # Audit log records the sibling-mode deletion.
    audit = await storage.list_audit_log(limit=5)
    assert any(
        e["action"] == "audio_channel_delete"
        and '"channel_index": 1' in (e["detail"] or "")
        and f'"deleted_audio_session_id": {secondary_id}' in (e["detail"] or "")
        for e in audit
    )


@pytest.mark.asyncio
async def test_delete_audio_channel_sibling_ordinal_out_of_range(
    tmp_path: Path, storage: Storage
) -> None:
    primary_id, _, _ = await _seed_sibling_pair(storage, tmp_path)
    with pytest.raises(ValueError, match="no sibling"):
        await storage.delete_audio_channel(primary_id, channel_index=9, user_id=None)


@pytest.mark.asyncio
async def test_delete_audio_channel_sibling_from_secondary_still_targets_correct_row(
    tmp_path: Path, storage: Storage
) -> None:
    """Calling through the secondary session id still resolves siblings correctly."""
    primary_id, secondary_id, _ = await _seed_sibling_pair(storage, tmp_path)

    # Delete ordinal 0 (primary) via a request rooted at the secondary id.
    await storage.delete_audio_channel(
        secondary_id, channel_index=0, user_id=None, reason="speaker request"
    )
    assert await storage.get_audio_session_row(primary_id) is None
    assert await storage.get_audio_session_row(secondary_id) is not None
