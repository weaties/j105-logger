"""Tests for multi-camera video pipeline pieces (issue #445).

Covers the additions that extend the existing pipeline:

* per-camera link label & title suffix
* account-scoped YouTube token paths
* YouTube channel verification
* camera label resolution + persistence
* the JSON-backed video upload ledger
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from helmlog.insta360 import (
    InstaRecording,
    _placeholder_label,
    load_camera_labels,
    resolve_camera_label,
    save_camera_label,
)
from helmlog.pipeline import PipelineConfig, build_link_label, process_recording
from helmlog.video_ledger import LedgerEntry, LedgerKey, VideoLedger
from helmlog.youtube import (
    ChannelMismatchError,
    UploadResult,
    account_token_path,
    verify_channel,
)

# ---------------------------------------------------------------------------
# Link label
# ---------------------------------------------------------------------------


class TestBuildLinkLabel:
    def test_default_label(self) -> None:
        assert build_link_label("") == "360 cam"

    def test_per_camera_label(self) -> None:
        assert build_link_label("bow") == "360 cam — bow"
        assert build_link_label("stern") == "360 cam — stern"


# ---------------------------------------------------------------------------
# process_recording — camera label propagation
# ---------------------------------------------------------------------------


def _sessions() -> list[dict[str, Any]]:
    return [
        {
            "id": 10,
            "name": "Race 1",
            "start_utc": "2026-08-10T14:00:00+00:00",
            "end_utc": "2026-08-10T15:00:00+00:00",
            "event": "Ballard Cup",
            "race_num": 1,
            "session_type": "race",
        },
    ]


def _upload_result() -> UploadResult:
    return UploadResult(
        video_id="abc",
        youtube_url="https://youtu.be/abc",
        title="Ballard Cup Race 1 — 2026-08-10 — bow cam",
    )


@pytest.mark.asyncio
async def test_camera_label_in_title_and_link() -> None:
    rec = InstaRecording(timestamp_str="20260810_070500", segments=[], total_size_bytes=0)
    cfg = PipelineConfig(
        pi_api_url="http://corvopi:3002",
        pi_session_cookie="cookie",
        timezone="America/Los_Angeles",
        camera_label="bow",
        youtube_account="corvo105",
    )

    mock_upload = AsyncMock(return_value=_upload_result())
    mock_link = AsyncMock(return_value=httpx.Response(201))

    with (
        patch("helmlog.pipeline.upload_video", mock_upload),
        patch("helmlog.pipeline._link_video_on_pi", mock_link),
    ):
        result = await process_recording(
            rec=rec, video_path="/tmp/test.mp4", sessions=_sessions(), config=cfg
        )

    assert result.linked is True
    assert mock_upload.call_args.kwargs["title"].endswith("— bow cam")
    assert mock_upload.call_args.kwargs["youtube_account"] == "corvo105"
    assert mock_link.call_args.kwargs["label"] == "360 cam — bow"


@pytest.mark.asyncio
async def test_no_camera_label_keeps_legacy_label() -> None:
    rec = InstaRecording(timestamp_str="20260810_070500", segments=[], total_size_bytes=0)
    cfg = PipelineConfig(pi_session_cookie="cookie")

    mock_upload = AsyncMock(return_value=_upload_result())
    mock_link = AsyncMock(return_value=httpx.Response(201))

    with (
        patch("helmlog.pipeline.upload_video", mock_upload),
        patch("helmlog.pipeline._link_video_on_pi", mock_link),
    ):
        await process_recording(
            rec=rec, video_path="/tmp/test.mp4", sessions=_sessions(), config=cfg
        )

    assert "cam" not in mock_upload.call_args.kwargs["title"].split("—")[-1].lower() or (
        mock_upload.call_args.kwargs["title"].endswith("2026-08-10")
    )
    assert mock_link.call_args.kwargs["label"] == "360 cam"


# ---------------------------------------------------------------------------
# YouTube account-scoped tokens & channel verification
# ---------------------------------------------------------------------------


class TestAccountTokenPath:
    def test_path_under_config_dir(self) -> None:
        p = account_token_path("corvo105")
        assert p.name == "corvo105.json"
        assert "helmlog" in p.parts
        assert "youtube" in p.parts


class TestVerifyChannel:
    def _service(self, items: list[dict[str, Any]]) -> MagicMock:
        svc = MagicMock()
        svc.channels.return_value.list.return_value.execute.return_value = {"items": items}
        return svc

    def test_match_by_title(self) -> None:
        svc = self._service([{"snippet": {"title": "corvo105", "customUrl": "@corvo105"}}])
        assert verify_channel(svc, "corvo105") == "corvo105"

    def test_match_by_handle_with_at_sign(self) -> None:
        svc = self._service([{"snippet": {"title": "Corvo Sailing", "customUrl": "@corvo105"}}])
        assert verify_channel(svc, "@corvo105") == "Corvo Sailing"

    def test_mismatch_raises(self) -> None:
        svc = self._service([{"snippet": {"title": "Other", "customUrl": "@other"}}])
        with pytest.raises(ChannelMismatchError):
            verify_channel(svc, "corvo105")

    def test_no_items_raises(self) -> None:
        svc = self._service([])
        with pytest.raises(ChannelMismatchError):
            verify_channel(svc, "corvo105")


# ---------------------------------------------------------------------------
# Camera label resolution
# ---------------------------------------------------------------------------


class TestCameraLabels:
    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        cfg = tmp_path / "cameras.toml"
        save_camera_label("ABCD-1234", "bow", cfg)
        save_camera_label("EFGH-5678", "stern", cfg)
        labels = load_camera_labels(cfg)
        assert labels == {"abcd-1234": "bow", "efgh-5678": "stern"}

    def test_load_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert load_camera_labels(tmp_path / "nope.toml") == {}

    def test_resolve_known_camera(self, tmp_path: Path) -> None:
        cfg = tmp_path / "cameras.toml"
        save_camera_label("AAAA-BBBB", "bow", cfg)

        with patch("helmlog.insta360.volume_uuid_for", return_value="AAAA-BBBB"):
            label, known = resolve_camera_label(Path("/Volumes/X4"), labels_path=cfg)

        assert label == "bow"
        assert known is True

    def test_resolve_unknown_creates_placeholder(self, tmp_path: Path) -> None:
        cfg = tmp_path / "cameras.toml"

        with patch(
            "helmlog.insta360.volume_uuid_for", return_value="11112222-3333-4444-5555-666677778888"
        ):
            label, known = resolve_camera_label(Path("/Volumes/X4"), labels_path=cfg)

        assert known is False
        assert label.startswith("camera-")
        # placeholder must have been persisted so subsequent runs see it
        labels = load_camera_labels(cfg)
        assert any(v.startswith("camera-") for v in labels.values())

    def test_resolve_uuid_lookup_failure_falls_back_to_basename(self, tmp_path: Path) -> None:
        with patch("helmlog.insta360.volume_uuid_for", return_value=None):
            label, known = resolve_camera_label(
                Path("/Volumes/Insta360-A"), labels_path=tmp_path / "c.toml"
            )
        assert label == "Insta360-A"
        assert known is False

    def test_placeholder_label_format(self) -> None:
        assert _placeholder_label("ABCD-1234-EF56-7890") == "camera-abcd1234"


# ---------------------------------------------------------------------------
# Video ledger
# ---------------------------------------------------------------------------


class TestVideoLedger:
    def test_record_and_lookup(self, tmp_path: Path) -> None:
        ledger = VideoLedger(tmp_path / "ledger.json")
        key = LedgerKey("UUID-1", "VID_20260810_140000_00_000.insv", 1234)
        assert not ledger.has(key)

        ledger.record(
            LedgerEntry(
                volume_uuid="UUID-1",
                source_filename="VID_20260810_140000_00_000.insv",
                size_bytes=1234,
                video_id="abc",
                youtube_url="https://youtu.be/abc",
                camera_label="bow",
                session_id=10,
                linked=True,
            )
        )
        assert ledger.has(key)
        entry = ledger.get(key)
        assert entry is not None
        assert entry.video_id == "abc"

    def test_persistence_across_instances(self, tmp_path: Path) -> None:
        path = tmp_path / "ledger.json"
        a = VideoLedger(path)
        a.record(
            LedgerEntry(
                volume_uuid="UUID-1",
                source_filename="VID.insv",
                size_bytes=99,
                video_id="vid",
                youtube_url="https://y.t/vid",
            )
        )
        b = VideoLedger(path)
        assert b.has(LedgerKey("UUID-1", "VID.insv", 99))
        assert len(b) == 1

    def test_corrupt_file_does_not_raise(self, tmp_path: Path) -> None:
        path = tmp_path / "ledger.json"
        path.write_text("not json {")
        ledger = VideoLedger(path)
        assert len(ledger) == 0
