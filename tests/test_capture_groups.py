"""Sibling-card capture storage path (#509 / #462 follow-up)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from helmlog.audio import AudioSession

if TYPE_CHECKING:
    from helmlog.storage import Storage


def _sess(
    file_path: str,
    *,
    capture_group_id: str | None,
    ordinal: int,
    device_name: str = "Jieli card",
) -> AudioSession:
    return AudioSession(
        file_path=file_path,
        device_name=device_name,
        start_utc=datetime(2026, 4, 12, 16, 30, 0, tzinfo=UTC),
        end_utc=datetime(2026, 4, 12, 16, 30, 30, tzinfo=UTC),
        sample_rate=48000,
        channels=1,
        capture_group_id=capture_group_id,
        capture_ordinal=ordinal,
    )


@pytest.mark.asyncio
async def test_capture_group_roundtrip(storage: Storage) -> None:
    a = await storage.write_audio_session(
        _sess("/tmp/a.wav", capture_group_id="grp-abc", ordinal=0)
    )
    b = await storage.write_audio_session(
        _sess("/tmp/b.wav", capture_group_id="grp-abc", ordinal=1, device_name="Jieli card 2")
    )
    # Unrelated legacy session must not be returned.
    await storage.write_audio_session(
        _sess("/tmp/c.wav", capture_group_id=None, ordinal=0, device_name="built-in")
    )

    siblings = await storage.list_capture_group_siblings("grp-abc")
    assert [s["id"] for s in siblings] == [a, b]
    assert [s["capture_ordinal"] for s in siblings] == [0, 1]
    assert all(s["capture_group_id"] == "grp-abc" for s in siblings)
    assert [s["file_path"] for s in siblings] == ["/tmp/a.wav", "/tmp/b.wav"]


@pytest.mark.asyncio
async def test_capture_group_unknown_returns_empty(storage: Storage) -> None:
    assert await storage.list_capture_group_siblings("nope") == []


@pytest.mark.asyncio
async def test_legacy_session_has_null_group(storage: Storage) -> None:
    sid = await storage.write_audio_session(
        _sess("/tmp/legacy.wav", capture_group_id=None, ordinal=0)
    )
    row = await storage.get_audio_session_row(sid)
    assert row is not None
    assert row["capture_group_id"] is None
    assert row["capture_ordinal"] == 0


# ---------------------------------------------------------------------------
# capture_start / capture_stop helpers
# ---------------------------------------------------------------------------


class _FakeSingleRecorder:
    """Quacks like AudioRecorder.start()/stop() for capture_start/stop tests."""

    def __init__(self, file_path: str) -> None:
        self._file_path = file_path
        self._started = False

    async def start(self, config: object, *, name: str | None = None) -> AudioSession:
        self._started = True
        return AudioSession(
            file_path=self._file_path,
            device_name="Fake",
            start_utc=datetime(2026, 4, 12, 16, 30, 0, tzinfo=UTC),
            end_utc=None,
            sample_rate=48000,
            channels=1,
        )

    async def stop(self) -> AudioSession:
        self._started = False
        return AudioSession(
            file_path=self._file_path,
            device_name="Fake",
            start_utc=datetime(2026, 4, 12, 16, 30, 0, tzinfo=UTC),
            end_utc=datetime(2026, 4, 12, 16, 30, 30, tzinfo=UTC),
            sample_rate=48000,
            channels=1,
        )


class _FakeGroupRecorder:
    """Quacks like AudioRecorderGroup for sibling-mode helper tests."""

    def __init__(self, paths: list[str]) -> None:
        self._paths = paths
        self._group_id = "grp-fake"

    async def start(
        self, config: object, *, devices: list[object], name: str | None = None
    ) -> list[AudioSession]:
        sessions = []
        for ordinal, _dev in enumerate(self._paths):
            sessions.append(
                AudioSession(
                    file_path=self._paths[ordinal],
                    device_name=f"Fake card {ordinal}",
                    start_utc=datetime(2026, 4, 12, 16, 30, 0, tzinfo=UTC),
                    end_utc=None,
                    sample_rate=48000,
                    channels=1,
                    capture_group_id=self._group_id,
                    capture_ordinal=ordinal,
                )
            )
        return sessions

    async def stop(self) -> list[AudioSession]:
        return [
            AudioSession(
                file_path=p,
                device_name=f"Fake card {i}",
                start_utc=datetime(2026, 4, 12, 16, 30, 0, tzinfo=UTC),
                end_utc=datetime(2026, 4, 12, 16, 30, 30, tzinfo=UTC),
                sample_rate=48000,
                channels=1,
                capture_group_id=self._group_id,
                capture_ordinal=i,
            )
            for i, p in enumerate(self._paths)
        ]


@pytest.mark.asyncio
async def test_capture_start_single_mode_persists_one_row(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from helmlog.audio import AudioConfig, AudioRecorder, capture_start

    rec = _FakeSingleRecorder("/tmp/single.wav")
    # Bypass isinstance check so we can use the duck-typed fake.
    monkeypatch.setattr(
        "helmlog.audio.AudioRecorderGroup",
        type("Never", (), {}),
        raising=True,
    )

    # Re-import to pick up monkeypatched class reference used by capture_start
    # We'll just call capture_start directly which uses isinstance — since
    # _FakeSingleRecorder is not an AudioRecorderGroup, it takes the single path.
    _ = AudioRecorder  # keep import

    sid = await capture_start(
        rec,  # type: ignore[arg-type]
        AudioConfig(device=None, sample_rate=48000, channels=1, output_dir="/tmp"),
        storage,
        name="test-race",
        race_id=None,
        session_type="race",
    )
    row = await storage.get_audio_session_row(sid)
    assert row is not None
    assert row["file_path"] == "/tmp/single.wav"
    assert row["capture_group_id"] is None
    assert row["capture_ordinal"] == 0


@pytest.mark.asyncio
async def test_capture_start_and_stop_sibling_mode_roundtrip(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from helmlog.audio import (
        AudioConfig,
        capture_start,
        capture_stop,
    )

    paths = ["/tmp/sib0.wav", "/tmp/sib1.wav"]
    fake = _FakeGroupRecorder(paths)
    # Make the isinstance check in capture_start/stop treat our fake as a group.
    monkeypatch.setattr(
        "helmlog.audio.AudioRecorderGroup",
        _FakeGroupRecorder,
        raising=True,
    )
    # Stub detect_all_capture_devices so capture_start doesn't hit real sounddevice.
    monkeypatch.setattr(
        "helmlog.usb_audio.detect_all_capture_devices",
        lambda *, min_channels=1: [object(), object()],
    )

    primary_id = await capture_start(
        fake,  # type: ignore[arg-type]
        AudioConfig(device=None, sample_rate=48000, channels=1, output_dir="/tmp"),
        storage,
        name="sibling-race",
        race_id=None,
        session_type="race",
    )

    # Two rows exist, same capture_group_id, ordinals 0 and 1.
    row0 = await storage.get_audio_session_row(primary_id)
    assert row0 is not None
    group_id = row0["capture_group_id"]
    assert group_id == "grp-fake"
    assert row0["capture_ordinal"] == 0

    siblings = await storage.list_capture_group_siblings(group_id)
    assert len(siblings) == 2
    assert [s["file_path"] for s in siblings] == paths
    # Only the primary's id is the one returned; second sibling has a different id.
    assert siblings[0]["id"] == primary_id
    assert siblings[1]["id"] != primary_id

    # end_utc must be NULL before stop.
    for s in siblings:
        assert s["end_utc"] is None

    await capture_stop(
        fake,  # type: ignore[arg-type]
        storage,
        primary_session_id=primary_id,
    )

    siblings_after = await storage.list_capture_group_siblings(group_id)
    for s in siblings_after:
        assert s["end_utc"] is not None
