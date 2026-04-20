"""YouTube upload via the Data API v3.

Handles OAuth2 credential management, resumable uploads, and metadata
templating for 360° sailing video.

Environment variables:
    YOUTUBE_CLIENT_SECRETS  Path to client_secrets.json from Google Cloud Console.
    YOUTUBE_TOKEN_FILE      Path to stored OAuth2 token (default ~/.helmlog-youtube-token.json).
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]
from googleapiclient.discovery import build as _build_service  # type: ignore[import-untyped]
from googleapiclient.http import MediaFileUpload  # type: ignore[import-untyped]
from loguru import logger

_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


def account_token_path(account: str) -> Path:
    """Return the OAuth token cache path for a YouTube account.

    Tokens live under ``~/.config/helmlog/youtube/<account>.json`` so
    multiple channels can be used without clobbering each other.
    """
    base = Path.home() / ".config" / "helmlog" / "youtube"
    return base / f"{account}.json"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UploadResult:
    """Result of a successful YouTube upload."""

    video_id: str
    youtube_url: str
    title: str


# ---------------------------------------------------------------------------
# Credential management
# ---------------------------------------------------------------------------


def load_credentials(client_secrets: Path, token_file: Path) -> Credentials:
    """Load or create OAuth2 credentials for the YouTube Data API.

    On first run (no token file), opens a browser for interactive consent.
    Subsequent runs use the stored refresh token for unattended access.

    Args:
        client_secrets: Path to ``client_secrets.json`` from Google Cloud Console.
        token_file: Path where the OAuth2 token is cached.

    Returns:
        Valid Credentials object.
    """
    import contextlib

    creds: Credentials | None = None

    with contextlib.suppress(FileNotFoundError, ValueError):
        creds = Credentials.from_authorized_user_file(str(token_file), _SCOPES)  # type: ignore[no-untyped-call]

    if creds and creds.expired and creds.refresh_token:
        logger.debug("Refreshing expired YouTube credentials")
        creds.refresh(Request())
        token_file.write_text(creds.to_json())  # type: ignore[no-untyped-call]
    elif creds is None or not creds.valid:
        logger.info("No valid YouTube credentials — starting OAuth flow")
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), _SCOPES)
        creds = flow.run_local_server(port=0)
        token_file.write_text(creds.to_json())
        logger.info("YouTube credentials saved to {}", token_file)

    assert creds is not None, "Failed to obtain YouTube credentials"
    return creds


def build_service(creds: Credentials) -> object:
    """Build the YouTube API service object."""
    return _build_service("youtube", "v3", credentials=creds)


class ChannelMismatchError(RuntimeError):
    """Raised when the authenticated channel does not match the expected account."""


def verify_channel(service: object, expected_account: str) -> str:
    """Confirm the loaded credentials authenticate to the expected channel.

    Calls ``channels.list(mine=true)`` and compares the returned channel
    title and custom URL/handle against ``expected_account``. Match is
    case-insensitive and tolerates a leading ``@`` on the handle.

    Args:
        service: The YouTube API service from :func:`build_service`.
        expected_account: Channel handle or title (e.g. ``"my-sailing-channel"``).

    Returns:
        The actual channel title returned by the API.

    Raises:
        ChannelMismatchError: If neither title nor handle matches.
    """
    expected = expected_account.lstrip("@").lower()
    response = (
        service.channels()  # type: ignore[attr-defined]
        .list(part="snippet", mine=True)
        .execute()
    )
    items = response.get("items") or []
    if not items:
        raise ChannelMismatchError(
            f"YouTube credentials returned no channel; expected {expected_account!r}"
        )
    snippet = items[0].get("snippet", {})
    title = str(snippet.get("title", ""))
    custom_url = str(snippet.get("customUrl", "")).lstrip("@")
    candidates = {title.lower(), custom_url.lower()}
    if expected not in candidates:
        raise ChannelMismatchError(
            f"YouTube credentials authenticate {title!r}/{custom_url!r}, "
            f"expected {expected_account!r}. Re-run OAuth for this account."
        )
    logger.info("YouTube channel verified: {} ({})", title, custom_url or "no handle")
    return title


# ---------------------------------------------------------------------------
# Metadata templating
# ---------------------------------------------------------------------------


def build_title(
    *,
    event: str | None,
    session_type: str,
    race_num: int | None,
    date: str,
    time: str | None = None,
) -> str:
    """Build a YouTube video title from session metadata.

    Titles lead with date (and optional time) so they sort correctly in
    chronological order under any alphabetic listing — that's how we
    keep race videos lined up on the YouTube channel page.

    Examples:
        >>> build_title(event="Ballard Cup", session_type="race", race_num=2,
        ...             date="2026-08-10", time="14:05")
        '2026-08-10 14:05 — Ballard Cup Race 2'
        >>> build_title(event=None, session_type="practice", race_num=None,
        ...             date="2026-08-10")
        '2026-08-10 — Practice'
    """
    label = session_type.capitalize()
    if race_num is not None:
        label = f"{label} {race_num}"
    if event:
        label = f"{event} {label}"
    prefix = f"{date} {time}" if time else date
    return f"{prefix} — {label}"


def build_description(
    *,
    session_url: str,
    start_utc: str,
    end_utc: str,
) -> str:
    """Build a YouTube video description with session metadata.

    Args:
        session_url: Deep link to the HelmLog session page.
        start_utc: Session start time (ISO 8601).
        end_utc: Session end time (ISO 8601).

    Returns:
        Multi-line description string.
    """
    return (
        f"360° sailing video — HelmLog\n"
        f"\n"
        f"Session: {session_url}\n"
        f"Start: {start_utc}\n"
        f"End: {end_utc}\n"
        f"\n"
        f"Recorded with Insta360 X4. Auto-uploaded by helmlog."
    )


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


async def upload_video(
    *,
    file_path: Path,
    title: str,
    description: str,
    privacy: str = "unlisted",
    tags: list[str] | None = None,
    client_secrets: Path | None = None,
    token_file: Path | None = None,
    youtube_account: str | None = None,
) -> UploadResult:
    """Upload a video to YouTube via the Data API v3.

    Uses resumable upload for large files. Credentials are loaded from
    ``YOUTUBE_CLIENT_SECRETS`` and ``YOUTUBE_TOKEN_FILE`` env vars, or
    from the explicit parameters.

    Args:
        file_path: Path to the MP4 file to upload.
        title: Video title.
        description: Video description.
        privacy: Privacy status (``"private"``, ``"unlisted"``, ``"public"``).
        tags: Optional list of tags.
        client_secrets: Override for client secrets path.
        token_file: Override for token file path.

    Returns:
        :class:`UploadResult` with the YouTube video ID and URL.
    """
    secrets = client_secrets or Path(
        os.environ.get(
            "YOUTUBE_CLIENT_SECRETS",
            str(Path.home() / ".helmlog-youtube-client-secrets.json"),
        )
    )
    if token_file is not None:
        token = token_file
    elif youtube_account:
        token = account_token_path(youtube_account)
        token.parent.mkdir(parents=True, exist_ok=True)
    else:
        token = Path(
            os.environ.get(
                "YOUTUBE_TOKEN_FILE",
                str(Path.home() / ".helmlog-youtube-token.json"),
            )
        )

    creds = load_credentials(secrets, token)
    service = build_service(creds)

    # Verify we're talking to the expected channel before uploading. Network
    # errors are downgraded to a warning so transient failures don't block
    # uploads (the cached token is still trusted in that case).
    if youtube_account:
        try:
            verify_channel(service, youtube_account)
        except ChannelMismatchError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not verify YouTube channel for {!r}: {} — proceeding",
                youtube_account,
                exc,
            )

    body: dict[str, object] = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags or ["sailing", "helmlog", "360"],
            "categoryId": "17",  # Sports
        },
        "status": {
            "privacyStatus": privacy,
        },
    }

    media = MediaFileUpload(str(file_path), resumable=True, chunksize=25 * 1024 * 1024)

    logger.info("Uploading {} to YouTube as {!r} ({})", file_path.name, title, privacy)

    # Run the upload in a thread to avoid blocking the event loop. Each
    # ``next_chunk`` call can fail transiently on flaky connections (timeouts,
    # connection resets, 5xx). Retry the call (not the whole upload) with
    # exponential backoff so the resumable upload picks up where it left off
    # instead of starting over from byte zero.
    def _do_upload() -> str:
        import socket
        import time

        request = service.videos().insert(  # type: ignore[attr-defined]
            part="snippet,status",
            body=body,
            media_body=media,
        )
        response: Any = None
        retryable: tuple[type[BaseException], ...] = (
            TimeoutError,
            ConnectionError,
            socket.timeout,
        )
        max_attempts = 6
        while response is None:
            for attempt in range(1, max_attempts + 1):
                try:
                    status, response = request.next_chunk()
                except retryable as exc:
                    if attempt == max_attempts:
                        raise
                    delay = 2**attempt
                    logger.warning(
                        "Upload chunk failed ({}); retry {}/{} in {}s",
                        exc,
                        attempt,
                        max_attempts - 1,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                if status:
                    logger.debug("Upload progress: {:.0%}", status.progress())
                break
        return str(response["id"])

    video_id: str = await asyncio.to_thread(_do_upload)

    youtube_url = f"https://youtu.be/{video_id}"
    logger.info("Upload complete: {} → {}", title, youtube_url)
    return UploadResult(video_id=video_id, youtube_url=youtube_url, title=title)
