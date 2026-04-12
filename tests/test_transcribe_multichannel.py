"""Tests for the multi-channel transcribe path (#462 pt.3 / #495).

faster-whisper, pyannote, and the actual WAV split are mocked so the tests
run on any machine without hardware or models.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if TYPE_CHECKING:
    from helmlog.storage import Storage

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers — build a real audio_session + transcript row
# ---------------------------------------------------------------------------


async def _make_session_and_transcript(storage: Storage, *, channels: int) -> tuple[int, int]:
    db = storage._conn()
    cur = await db.execute(
        "INSERT INTO audio_sessions"
        " (file_path, device_name, start_utc, sample_rate, channels)"
        " VALUES (?, ?, ?, ?, ?)",
        ("/tmp/x.wav", "Lavalier4", datetime.now(UTC).isoformat(), 48000, channels),
    )
    await db.commit()
    audio_session_id = cur.lastrowid
    assert audio_session_id is not None
    transcript_id = await storage.create_transcript_job(audio_session_id, "tiny")
    return audio_session_id, transcript_id


# ---------------------------------------------------------------------------
# _transcribe_multi_channel — pure function
# ---------------------------------------------------------------------------


class TestTranscribeMultiChannel:
    async def test_merges_and_sorts_by_start(self) -> None:
        from helmlog import transcribe

        # Channel 0 (helm) speaks at t=0 and t=5; channel 1 (trim) at t=2.
        async def fake_one_channel(
            tmp_path: str,
            model_size: str,
            diarize: bool,
            *,
            transcribe_url: str = "",
        ) -> tuple[str, list[dict[str, object]]]:
            # Decide what to return based on which call this is. Track via list.
            calls.append(tmp_path)
            if len(calls) == 1:
                return "helm one helm two", [
                    {"start": 0.0, "end": 1.0, "text": "helm one"},
                    {"start": 5.0, "end": 6.0, "text": "helm two"},
                ]
            return "trim one", [{"start": 2.0, "end": 3.0, "text": "trim one"}]

        calls: list[str] = []

        fake_sf = MagicMock()
        fake_sf.read.return_value = (MagicMock(ndim=2), 48000)

        with (
            patch("helmlog.transcribe._transcribe_one_channel", side_effect=fake_one_channel),
            patch("helmlog.transcribe.tempfile.NamedTemporaryFile") as mock_tmp,
            patch("os.path.exists", return_value=False),
            patch.dict("sys.modules", {"soundfile": fake_sf}),
        ):
            mock_tmp.return_value.__enter__.return_value.name = "/tmp/ch.wav"
            segments = await transcribe._transcribe_multi_channel(
                "/tmp/x.wav",
                channels=2,
                channel_map={0: "helm", 1: "trim"},
                model_size="tiny",
            )

        assert [s["text"] for s in segments] == ["helm one", "trim one", "helm two"]
        assert [s["channel_index"] for s in segments] == [0, 1, 0]
        assert [s["position_name"] for s in segments] == ["helm", "trim", "helm"]
        assert [s["speaker"] for s in segments] == ["helm", "trim", "helm"]

    async def test_falls_back_to_chN_when_position_unmapped(self) -> None:
        from helmlog import transcribe

        async def fake_one_channel(
            tmp_path: str, model_size: str, diarize: bool, *, transcribe_url: str = ""
        ) -> tuple[str, list[dict[str, object]]]:
            return "x", [{"start": 0.0, "end": 1.0, "text": "x"}]

        fake_sf = MagicMock()
        fake_sf.read.return_value = (MagicMock(ndim=2), 48000)

        with (
            patch("helmlog.transcribe._transcribe_one_channel", side_effect=fake_one_channel),
            patch("helmlog.transcribe.tempfile.NamedTemporaryFile") as mock_tmp,
            patch("os.path.exists", return_value=False),
            patch.dict("sys.modules", {"soundfile": fake_sf}),
        ):
            mock_tmp.return_value.__enter__.return_value.name = "/tmp/ch.wav"
            segments = await transcribe._transcribe_multi_channel(
                "/tmp/x.wav",
                channels=2,
                channel_map={0: "helm"},
                model_size="tiny",
            )

        positions = [s["position_name"] for s in segments]
        assert "helm" in positions
        assert "CH1" in positions


# ---------------------------------------------------------------------------
# Full transcribe_session multi-channel path — relational persistence
# ---------------------------------------------------------------------------


class TestTranscribeSessionMultiChannel:
    async def test_persists_relational_segments(self, storage: Storage) -> None:
        from helmlog import transcribe

        audio_session_id, transcript_id = await _make_session_and_transcript(storage, channels=2)
        # Configure a channel map for the device
        await storage.set_channel_map(
            vendor_id=0x1234,
            product_id=0x5678,
            serial="ABC",
            usb_port_path="1-1.2",
            mapping={0: "helm", 1: "trim"},
        )
        await storage.set_audio_session_device(
            audio_session_id,
            vendor_id=0x1234,
            product_id=0x5678,
            serial="ABC",
            usb_port_path="1-1.2",
        )

        async def fake_multi(
            file_path: str,
            *,
            channels: int,
            channel_map: dict[int, str],
            model_size: str,
            transcribe_url: str = "",
        ) -> list[dict[str, object]]:
            assert channel_map == {0: "helm", 1: "trim"}
            return [
                {
                    "start": 0.0,
                    "end": 1.0,
                    "text": "ready about",
                    "channel_index": 0,
                    "position_name": "helm",
                    "speaker": "helm",
                },
                {
                    "start": 1.5,
                    "end": 2.0,
                    "text": "trim on",
                    "channel_index": 1,
                    "position_name": "trim",
                    "speaker": "trim",
                },
            ]

        with (
            patch("helmlog.transcribe._transcribe_multi_channel", side_effect=fake_multi),
            patch("helmlog.transcribe._run_trigger_scan", new=AsyncMock(return_value=None)),
        ):
            await transcribe.transcribe_session(
                storage,
                audio_session_id,
                transcript_id,
                model_size="tiny",
                diarize=True,
            )

        # Relational table populated with channel tags
        rows = await storage.list_transcript_segments(transcript_id)
        assert len(rows) == 2
        assert rows[0]["text"] == "ready about"
        assert rows[0]["channel_index"] == 0
        assert rows[0]["position_name"] == "helm"
        assert rows[1]["channel_index"] == 1
        assert rows[1]["position_name"] == "trim"

        # Transcript JSON column also populated for backward compat
        t = await storage.get_transcript(audio_session_id)
        assert t is not None
        assert t["status"] == "done"
        assert "ready about" in t["text"]
        assert "trim on" in t["text"]

    async def test_falls_back_to_empty_map_when_no_device_set(self, storage: Storage) -> None:
        from helmlog import transcribe

        audio_session_id, transcript_id = await _make_session_and_transcript(storage, channels=2)

        captured: dict[str, object] = {}

        async def fake_multi(
            file_path: str,
            *,
            channels: int,
            channel_map: dict[int, str],
            model_size: str,
            transcribe_url: str = "",
        ) -> list[dict[str, object]]:
            captured["channel_map"] = channel_map
            return []

        with (
            patch("helmlog.transcribe._transcribe_multi_channel", side_effect=fake_multi),
            patch("helmlog.transcribe._run_trigger_scan", new=AsyncMock(return_value=None)),
        ):
            await transcribe.transcribe_session(
                storage, audio_session_id, transcript_id, model_size="tiny"
            )

        assert captured["channel_map"] == {}


# ---------------------------------------------------------------------------
# Single-channel path is unchanged — regression check
# ---------------------------------------------------------------------------


class TestSingleChannelRegression:
    async def test_single_channel_uses_pyannote_path(self, storage: Storage) -> None:
        """A 1-channel session should NOT touch _transcribe_multi_channel."""
        from helmlog import transcribe

        audio_session_id, transcript_id = await _make_session_and_transcript(storage, channels=1)

        multi_called = MagicMock()

        async def fake_multi(*args: object, **kwargs: object) -> list[dict[str, object]]:
            multi_called()
            return []

        # Whisper produces a single segment
        def fake_run_whisper_segments(
            *, file_path: str, model_size: str
        ) -> list[tuple[float, float, str]]:
            return [(0.0, 1.0, "tack")]

        with (
            patch("helmlog.transcribe._transcribe_multi_channel", side_effect=fake_multi),
            patch(
                "helmlog.transcribe._run_whisper_segments",
                side_effect=fake_run_whisper_segments,
            ),
            patch("helmlog.transcribe._pyannote_available", return_value=False),
            patch("helmlog.transcribe._run_trigger_scan", new=AsyncMock(return_value=None)),
        ):
            await transcribe.transcribe_session(
                storage,
                audio_session_id,
                transcript_id,
                model_size="tiny",
                diarize=False,
            )

        multi_called.assert_not_called()
        t = await storage.get_transcript(audio_session_id)
        assert t is not None
        assert t["status"] == "done"
        assert "tack" in t["text"]
