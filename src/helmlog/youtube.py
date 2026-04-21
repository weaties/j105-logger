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
from typing import TYPE_CHECKING, Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]
from googleapiclient.discovery import build as _build_service  # type: ignore[import-untyped]
from googleapiclient.http import MediaFileUpload  # type: ignore[import-untyped]
from loguru import logger

if TYPE_CHECKING:
    from collections.abc import Callable

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


class UploadVerificationError(RuntimeError):
    """Raised when YouTube rejects or fails to process an uploaded video.

    The pipeline treats this as a "do not archive the local file" signal —
    the source MP4 stays in the exports dir so the user can investigate or
    retry without having to re-export from Insta360 Studio.
    """


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
# Post-upload verification
# ---------------------------------------------------------------------------


# Terminal states reported by the YouTube Data API. A video that lands in one
# of these has either been accepted (``processed``) or unambiguously refused
# (``rejected`` / ``failed``). Anything else — notably ``uploaded`` —
# means YouTube is still ingesting the bytes and may yet decide either way.
_UPLOAD_STATUS_PROCESSED = "processed"
_UPLOAD_STATUS_REJECTED = "rejected"
_UPLOAD_STATUS_FAILED = "failed"
_TERMINAL_UPLOAD_STATUSES = frozenset(
    {_UPLOAD_STATUS_PROCESSED, _UPLOAD_STATUS_REJECTED, _UPLOAD_STATUS_FAILED}
)


def wait_for_upload_acceptance(
    service: object,
    video_id: str,
    *,
    timeout_s: float = 1800.0,
    poll_interval_s: float = 20.0,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Poll YouTube until ``uploadStatus`` transitions out of ``uploaded``.

    YouTube's initial response to ``videos.insert`` returns as soon as the
    last byte is sent; the server still has to open the container,
    remux / validate it, and mark the video as ``processed`` (success) or
    ``rejected`` / ``failed`` (bad upload — typically truncated or corrupt).
    This helper blocks until one of those terminal states is reported so the
    caller can decide whether to archive the source file or keep it for a
    retry.

    Note this waits only for *upload acceptance* — deep transcoding (the
    ``processingDetails.processingStatus`` field) can take many hours for
    a multi-GB 360° video and isn't a reliable signal for "was the bitstream
    coherent". If YouTube accepted the bitstream, it will eventually finish
    transcoding on its own.

    Args:
        service: YouTube API service from :func:`build_service`.
        video_id: The id returned by ``videos.insert``.
        timeout_s: Hard ceiling — raises ``UploadVerificationError`` if we
            never see a terminal status within this window.
        poll_interval_s: Seconds between ``videos.list`` calls.
        sleep: Optional injectable sleeper (tests use a no-op).

    Returns:
        The raw ``status`` dict for the video once it's in a terminal state.

    Raises:
        UploadVerificationError: If YouTube rejects the upload, marks it as
            failed, or fails to return a terminal status inside ``timeout_s``.
    """
    import time

    if sleep is None:
        sleep = time.sleep

    deadline = time.monotonic() + timeout_s
    last_status: str | None = None

    while True:
        response = (
            service.videos()  # type: ignore[attr-defined]
            .list(part="status", id=video_id)
            .execute()
        )
        items = response.get("items") or []
        if not items:
            # Fresh uploads occasionally return an empty list before the
            # server fully registers the id — don't treat that as fatal
            # on its own, just keep polling until the deadline.
            logger.debug("videos.list returned no items yet for {}", video_id)
        else:
            status = items[0].get("status") or {}
            upload_status = str(status.get("uploadStatus") or "")
            if upload_status != last_status:
                logger.info("YouTube uploadStatus for {}: {}", video_id, upload_status)
                last_status = upload_status

            if upload_status == _UPLOAD_STATUS_PROCESSED:
                return status
            if upload_status == _UPLOAD_STATUS_REJECTED:
                reason = status.get("rejectionReason") or "unknown"
                raise UploadVerificationError(f"YouTube rejected video {video_id}: {reason}")
            if upload_status == _UPLOAD_STATUS_FAILED:
                reason = status.get("failureReason") or "unknown"
                raise UploadVerificationError(
                    f"YouTube failed to process video {video_id}: {reason}"
                )

        if time.monotonic() >= deadline:
            raise UploadVerificationError(
                f"YouTube upload for {video_id} did not reach a terminal state "
                f"within {timeout_s:.0f}s (last uploadStatus={last_status!r})"
            )
        sleep(poll_interval_s)


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
    logger.info("Upload bytes complete, verifying YouTube accepted: {} → {}", title, youtube_url)

    # Block until YouTube decides whether it could actually open the file.
    # A truncated / corrupt source (the exact failure mode we used to hit when
    # the readiness gate was too short) lands as ``rejected`` / ``failed``
    # here; raising lets ``upload-stitched.sh`` skip the move-to-backup step
    # and keep the source file in the exports dir for a retry.
    verify_timeout = float(os.environ.get("HELMLOG_YT_VERIFY_TIMEOUT_S", "1800"))
    await asyncio.to_thread(
        wait_for_upload_acceptance,
        service,
        video_id,
        timeout_s=verify_timeout,
    )

    logger.info("Upload accepted: {} → {}", title, youtube_url)
    return UploadResult(video_id=video_id, youtube_url=youtube_url, title=title)
