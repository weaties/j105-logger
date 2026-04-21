"""Tests for YouTube upload module."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from helmlog.youtube import (
    UploadResult,
    UploadVerificationError,
    build_description,
    build_title,
    load_credentials,
    upload_video,
    wait_for_upload_acceptance,
)

# ---------------------------------------------------------------------------
# Title templating
# ---------------------------------------------------------------------------


class TestBuildTitle:
    def test_race_with_event(self) -> None:
        title = build_title(event="Ballard Cup", session_type="race", race_num=2, date="2026-08-10")
        assert title == "2026-08-10 — Ballard Cup Race 2"

    def test_race_with_time(self) -> None:
        title = build_title(
            event="Ballard Cup",
            session_type="race",
            race_num=2,
            date="2026-08-10",
            time="14:05Z",
        )
        assert title == "2026-08-10 14:05Z — Ballard Cup Race 2"

    def test_race_without_event(self) -> None:
        title = build_title(event=None, session_type="race", race_num=1, date="2026-08-10")
        assert title == "2026-08-10 — Race 1"

    def test_practice(self) -> None:
        title = build_title(event=None, session_type="practice", race_num=None, date="2026-08-10")
        assert title == "2026-08-10 — Practice"

    def test_practice_with_event(self) -> None:
        title = build_title(
            event="Ballard Cup", session_type="practice", race_num=None, date="2026-08-10"
        )
        assert title == "2026-08-10 — Ballard Cup Practice"

    def test_unknown_session_type(self) -> None:
        title = build_title(event=None, session_type="other", race_num=None, date="2026-08-10")
        assert title == "2026-08-10 — Other"

    def test_titles_sort_chronologically(self) -> None:
        """Date-leading titles must sort to chronological order alphabetically."""
        a = build_title(event=None, session_type="race", race_num=1, date="2026-08-10")
        b = build_title(event=None, session_type="race", race_num=2, date="2026-08-11")
        c = build_title(event=None, session_type="race", race_num=3, date="2026-08-09")
        assert sorted([a, b, c]) == [c, a, b]


# ---------------------------------------------------------------------------
# Description templating
# ---------------------------------------------------------------------------


class TestBuildDescription:
    def test_basic_description(self) -> None:
        desc = build_description(
            session_url="https://corvopi:3002/history/42",
            start_utc="2026-08-10T14:05:30Z",
            end_utc="2026-08-10T15:30:00Z",
        )
        assert "https://corvopi:3002/history/42" in desc
        assert "14:05:30" in desc
        assert "360" in desc.lower() or "HelmLog" in desc


# ---------------------------------------------------------------------------
# Credential loading
# ---------------------------------------------------------------------------


class TestLoadCredentials:
    def test_loads_existing_token(self, tmp_path: Path) -> None:
        """When a valid token file exists, load and return credentials."""
        token_data = {
            "token": "ya29.test-access-token",
            "refresh_token": "1//test-refresh-token",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "test-client-id.apps.googleusercontent.com",
            "client_secret": "test-secret",
            "scopes": ["https://www.googleapis.com/auth/youtube.upload"],
        }
        token_file = tmp_path / "token.json"
        token_file.write_text(json.dumps(token_data))

        secrets_file = tmp_path / "client_secrets.json"
        secrets_file.write_text("{}")  # not used when token exists

        with patch("helmlog.youtube.Credentials") as mock_creds_cls:
            mock_creds = MagicMock()
            mock_creds.valid = True
            mock_creds.expired = False
            mock_creds_cls.from_authorized_user_file.return_value = mock_creds

            creds = load_credentials(secrets_file, token_file)

        assert creds is mock_creds
        mock_creds_cls.from_authorized_user_file.assert_called_once()

    def test_missing_token_triggers_flow(self, tmp_path: Path) -> None:
        """When no token file exists, run the interactive OAuth flow."""
        secrets_file = tmp_path / "client_secrets.json"
        secrets_file.write_text('{"installed": {}}')
        token_file = tmp_path / "token.json"

        with (
            patch("helmlog.youtube.InstalledAppFlow") as mock_flow_cls,
            patch("helmlog.youtube.Credentials") as mock_creds_cls,
        ):
            mock_creds_cls.from_authorized_user_file.side_effect = FileNotFoundError
            mock_flow = MagicMock()
            mock_creds = MagicMock()
            mock_creds.to_json.return_value = '{"token": "new"}'
            mock_flow.run_local_server.return_value = mock_creds
            mock_flow_cls.from_client_secrets_file.return_value = mock_flow

            creds = load_credentials(secrets_file, token_file)

        assert creds is mock_creds
        mock_flow.run_local_server.assert_called_once_with(port=0)
        # Token should have been saved
        assert token_file.exists()


# ---------------------------------------------------------------------------
# Upload (mocked API)
# ---------------------------------------------------------------------------


def _make_upload_service(video_id: str, upload_status: str = "processed") -> MagicMock:
    """Build a mock YouTube service that completes upload + verification."""
    insert_response = MagicMock()
    insert_response.__getitem__ = lambda _, k: {"id": video_id}[k]

    mock_insert = MagicMock()
    mock_insert.next_chunk.return_value = (None, insert_response)

    mock_list_request = MagicMock()
    mock_list_request.execute.return_value = {
        "items": [{"id": video_id, "status": {"uploadStatus": upload_status}}]
    }

    mock_videos = MagicMock()
    mock_videos.insert.return_value = mock_insert
    mock_videos.list.return_value = mock_list_request

    mock_service = MagicMock()
    mock_service.videos.return_value = mock_videos
    return mock_service


class TestUploadVideo:
    @pytest.mark.asyncio
    async def test_upload_returns_result(self, tmp_path: Path) -> None:
        video_file = tmp_path / "test.mp4"
        video_file.write_bytes(b"\x00" * 100)

        mock_service = _make_upload_service("dQw4w9WgXcQ")

        with (
            patch("helmlog.youtube.load_credentials") as mock_load,
            patch("helmlog.youtube.build_service") as mock_build,
        ):
            mock_load.return_value = MagicMock()
            mock_build.return_value = mock_service

            result = await upload_video(
                file_path=video_file,
                title="Test Race",
                description="A test upload",
                privacy="unlisted",
            )

        assert isinstance(result, UploadResult)
        assert result.video_id == "dQw4w9WgXcQ"
        assert result.youtube_url == "https://youtu.be/dQw4w9WgXcQ"
        assert result.title == "Test Race"

    @pytest.mark.asyncio
    async def test_upload_sets_privacy(self, tmp_path: Path) -> None:
        """Verify privacy setting is passed to the API."""
        video_file = tmp_path / "test.mp4"
        video_file.write_bytes(b"\x00" * 100)

        mock_service = _make_upload_service("abc123")
        mock_videos = mock_service.videos.return_value

        with (
            patch("helmlog.youtube.load_credentials") as mock_load,
            patch("helmlog.youtube.build_service") as mock_build,
        ):
            mock_load.return_value = MagicMock()
            mock_build.return_value = mock_service

            await upload_video(
                file_path=video_file,
                title="Test",
                description="Desc",
                privacy="private",
            )

        # Check the body passed to videos().insert()
        call_kwargs = mock_videos.insert.call_args
        body = call_kwargs.kwargs.get("body") or call_kwargs[1].get("body")
        assert body["status"]["privacyStatus"] == "private"

    @pytest.mark.asyncio
    async def test_upload_raises_when_youtube_rejects(self, tmp_path: Path) -> None:
        """A rejected upload must propagate so the caller leaves the file for retry."""
        video_file = tmp_path / "test.mp4"
        video_file.write_bytes(b"\x00" * 100)

        insert_response = MagicMock()
        insert_response.__getitem__ = lambda _, k: {"id": "rej123"}[k]
        mock_insert = MagicMock()
        mock_insert.next_chunk.return_value = (None, insert_response)

        mock_list_request = MagicMock()
        mock_list_request.execute.return_value = {
            "items": [
                {
                    "id": "rej123",
                    "status": {
                        "uploadStatus": "rejected",
                        "rejectionReason": "uploadAborted",
                    },
                }
            ]
        }

        mock_videos = MagicMock()
        mock_videos.insert.return_value = mock_insert
        mock_videos.list.return_value = mock_list_request
        mock_service = MagicMock()
        mock_service.videos.return_value = mock_videos

        with (
            patch("helmlog.youtube.load_credentials") as mock_load,
            patch("helmlog.youtube.build_service") as mock_build,
        ):
            mock_load.return_value = MagicMock()
            mock_build.return_value = mock_service

            with pytest.raises(UploadVerificationError, match="uploadAborted"):
                await upload_video(
                    file_path=video_file,
                    title="Broken",
                    description="Broken",
                    privacy="unlisted",
                )


# ---------------------------------------------------------------------------
# Post-upload verification
# ---------------------------------------------------------------------------


class TestWaitForUploadAcceptance:
    def test_returns_when_processed(self) -> None:
        service = MagicMock()
        service.videos.return_value.list.return_value.execute.return_value = {
            "items": [{"id": "ok1", "status": {"uploadStatus": "processed"}}]
        }

        status = wait_for_upload_acceptance(service, "ok1", sleep=lambda _s: None)
        assert status["uploadStatus"] == "processed"

    def test_raises_on_rejected_with_reason(self) -> None:
        service = MagicMock()
        service.videos.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "id": "bad1",
                    "status": {
                        "uploadStatus": "rejected",
                        "rejectionReason": "invalidFile",
                    },
                }
            ]
        }

        with pytest.raises(UploadVerificationError, match="invalidFile"):
            wait_for_upload_acceptance(service, "bad1", sleep=lambda _s: None)

    def test_raises_on_failed_with_reason(self) -> None:
        service = MagicMock()
        service.videos.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "id": "bad2",
                    "status": {
                        "uploadStatus": "failed",
                        "failureReason": "codec",
                    },
                }
            ]
        }

        with pytest.raises(UploadVerificationError, match="codec"):
            wait_for_upload_acceptance(service, "bad2", sleep=lambda _s: None)

    def test_polls_until_terminal(self) -> None:
        """Should keep polling while status remains ``uploaded``."""
        service = MagicMock()
        service.videos.return_value.list.return_value.execute.side_effect = [
            {"items": [{"id": "poll1", "status": {"uploadStatus": "uploaded"}}]},
            {"items": [{"id": "poll1", "status": {"uploadStatus": "uploaded"}}]},
            {"items": [{"id": "poll1", "status": {"uploadStatus": "processed"}}]},
        ]

        status = wait_for_upload_acceptance(service, "poll1", sleep=lambda _s: None)
        assert status["uploadStatus"] == "processed"
        assert service.videos.return_value.list.return_value.execute.call_count == 3

    def test_raises_on_timeout(self) -> None:
        service = MagicMock()
        service.videos.return_value.list.return_value.execute.return_value = {
            "items": [{"id": "slow", "status": {"uploadStatus": "uploaded"}}]
        }

        with pytest.raises(UploadVerificationError, match="did not reach a terminal"):
            # timeout_s=0 forces one poll then a timeout check.
            wait_for_upload_acceptance(service, "slow", timeout_s=0.0, sleep=lambda _s: None)

    def test_tolerates_empty_items_before_terminal(self) -> None:
        """Fresh uploads sometimes return no items before the id propagates."""
        service = MagicMock()
        service.videos.return_value.list.return_value.execute.side_effect = [
            {"items": []},
            {"items": [{"id": "new1", "status": {"uploadStatus": "processed"}}]},
        ]

        status = wait_for_upload_acceptance(service, "new1", sleep=lambda _s: None)
        assert status["uploadStatus"] == "processed"
