"""Video pipeline orchestration — fetch sessions, match, upload, link.

Coordinates the steps between discovering Insta360 recordings and having
them appear as linked YouTube videos in HelmLog sessions.  Each
function is independently testable; the shell script
(``scripts/process-videos.sh``) calls these as the top-level driver.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from helmlog.insta360 import InstaRecording, match_sessions, recording_start_utc
from helmlog.youtube import build_description, build_title, upload_video

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PipelineConfig:
    """Runtime configuration for the video pipeline.

    Attributes:
        pi_api_url: HelmLog Pi base URL.
        pi_session_cookie: Auth cookie for Pi API calls (enables linking).
        privacy: YouTube privacy status for uploads.
        timezone: Camera local timezone for filename → UTC conversion.
        camera_label: Per-camera identifier (e.g. ``"bow"``, ``"stern"``).
            Used for output subdirectory, YouTube title suffix, and the
            link label posted to the Pi. Empty string disables the
            per-camera suffix (single-camera mode).
        youtube_account: YouTube channel handle to upload to. Drives
            which OAuth token file is loaded — see
            :func:`helmlog.youtube.upload_video`.
    """

    pi_api_url: str = "http://corvopi:3002"
    pi_session_cookie: str = ""
    privacy: str = "unlisted"
    timezone: str = "America/Los_Angeles"
    camera_label: str = ""
    youtube_account: str = ""


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class ProcessResult:
    """Outcome of processing a single recording."""

    uploaded: bool = False
    video_id: str | None = None
    youtube_url: str | None = None
    session_id: int | None = None
    linked: bool = False
    error: str | None = None


# ---------------------------------------------------------------------------
# Pi API helpers
# ---------------------------------------------------------------------------


async def fetch_sessions_from_pi(
    pi_api_url: str,
    *,
    session_cookie: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Fetch recent sessions from the HelmLog API.

    Returns an empty list on any failure (network error, non-200, etc.)
    so the caller can still proceed with generic metadata.
    """
    try:
        cookies = {"session": session_cookie} if session_cookie else {}
        async with httpx.AsyncClient(timeout=15, cookies=cookies) as client:
            resp = await client.get(f"{pi_api_url}/api/sessions", params={"limit": limit})
            if resp.status_code != 200:
                logger.warning("Could not fetch sessions (HTTP {})", resp.status_code)
                return []
            data = resp.json()
            sessions: list[dict[str, Any]] = (
                data if isinstance(data, list) else data.get("sessions", [])
            )
            logger.info("Fetched {} session(s) from Pi", len(sessions))
            return sessions
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not reach Pi API at {}: {}", pi_api_url, exc)
        return []


def build_link_label(camera_label: str = "") -> str:
    """Build the per-video label posted to the Pi when linking.

    Examples:
        >>> build_link_label("")
        '360 cam'
        >>> build_link_label("bow")
        '360 cam — bow'
    """
    if camera_label:
        return f"360 cam — {camera_label}"
    return "360 cam"


async def _link_video_on_pi(
    *,
    pi_api_url: str,
    session_id: int,
    youtube_url: str,
    sync_utc: str,
    session_cookie: str,
    label: str = "360 cam",
) -> httpx.Response:
    """POST to the Pi API to link a YouTube video to a session.

    Raises on network errors so the caller can handle gracefully.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        return await client.post(
            f"{pi_api_url}/api/sessions/{session_id}/videos",
            json={
                "youtube_url": youtube_url,
                "label": label,
                "sync_utc": sync_utc,
                "sync_offset_s": 0.0,
            },
            cookies={"session": session_cookie},
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def process_recording(
    *,
    rec: InstaRecording,
    video_path: str | Path,
    sessions: list[dict[str, Any]],
    config: PipelineConfig,
) -> ProcessResult:
    """Process a single recording: match session, upload to YouTube, link on Pi.

    Args:
        rec: The Insta360 recording (needs ``timestamp_str`` for UTC conversion).
        video_path: Path to the stitched MP4 file.
        sessions: Pre-fetched session list from :func:`fetch_sessions_from_pi`.
        config: Pipeline configuration.

    Returns:
        :class:`ProcessResult` describing what happened.
    """
    result = ProcessResult()
    video_path = Path(video_path)

    # Convert timestamp to UTC
    start_utc = recording_start_utc(rec, config.timezone)
    end_utc = start_utc + timedelta(hours=2)

    # Match to a session
    matched = match_sessions(start_utc, end_utc, sessions)
    # The /api/sessions list mixes races, debriefs, and practice sessions, but
    # the link endpoint only accepts race IDs. When the matcher picks a child
    # session (e.g. a debrief), follow ``parent_race_id`` to the parent race
    # and use *its* metadata for both linking and title-building so the
    # YouTube title says "Race 4", not "Debrief 4".
    session = matched
    if matched is not None:
        parent_race_id = matched.get("parent_race_id")
        if parent_race_id is not None:
            parent = next(
                (
                    s
                    for s in sessions
                    if s.get("id") == parent_race_id
                    and (s.get("type") == "race" or s.get("session_type") == "race")
                ),
                None,
            )
            if parent is not None:
                session = parent
                logger.info(
                    "[{}] Walked from {} → parent race {} for link + title",
                    rec.timestamp_str,
                    matched.get("name", matched.get("id")),
                    parent.get("name", parent.get("id")),
                )
    session_id: int | None = session.get("id") if session else None
    result.session_id = session_id

    # Build metadata
    title_suffix = f" — {config.camera_label} cam" if config.camera_label else ""
    # The /api/sessions endpoint returns the session-type field as ``type``,
    # not ``session_type`` — fall through to keep older callers working.
    sess_type = (
        session.get("type") or session.get("session_type") or "sailing" if session else "sailing"
    )
    title_date = start_utc.strftime("%Y-%m-%d")
    title_time = start_utc.strftime("%H:%MZ")
    if session:
        title = build_title(
            event=session.get("event"),
            session_type=sess_type,
            race_num=session.get("race_num"),
            date=title_date,
            time=title_time,
        )
        title = f"{title}{title_suffix}"
        base = f"{config.pi_api_url}/history"
        session_slug = session.get("slug")
        if session_id and session_slug:
            session_url = f"{config.pi_api_url}/session/{session_id}/{session_slug}"
        elif session_id:
            session_url = f"{config.pi_api_url}/session/{session_id}"
        else:
            session_url = base
        s_end = session.get("end_utc", "")
        desc = build_description(
            session_url=session_url,
            start_utc=start_utc.isoformat(),
            end_utc=s_end or end_utc.isoformat(),
        )
        session_name = session.get("name", session_id)
        logger.info("[{}] Matched to session: {}", rec.timestamp_str, session_name)
    else:
        title = build_title(
            event=None,
            session_type="sailing",
            race_num=None,
            date=title_date,
            time=title_time,
        )
        title = f"{title}{title_suffix}"
        desc = build_description(
            session_url=f"{config.pi_api_url}/history",
            start_utc=start_utc.isoformat(),
            end_utc=end_utc.isoformat(),
        )
        logger.info("[{}] No matching session found", rec.timestamp_str)

    # Upload to YouTube
    try:
        upload_result = await upload_video(
            file_path=video_path,
            title=title,
            description=desc,
            privacy=config.privacy,
            youtube_account=config.youtube_account or None,
        )
        result.uploaded = True
        result.video_id = upload_result.video_id
        result.youtube_url = upload_result.youtube_url
        logger.info("[{}] Uploaded → {}", rec.timestamp_str, upload_result.youtube_url)
    except Exception as exc:  # noqa: BLE001
        result.error = str(exc)
        logger.error("[{}] Upload failed: {}", rec.timestamp_str, exc)
        return result

    # Link video to session on the Pi
    if session_id and config.pi_session_cookie:
        try:
            link_resp = await _link_video_on_pi(
                pi_api_url=config.pi_api_url,
                session_id=session_id,
                youtube_url=upload_result.youtube_url,
                sync_utc=start_utc.isoformat(),
                session_cookie=config.pi_session_cookie,
                label=build_link_label(config.camera_label),
            )
            if link_resp.status_code == 201:
                result.linked = True
                logger.info("[{}] Linked to session {} on Pi", rec.timestamp_str, session_id)
            else:
                logger.warning(
                    "[{}] Link failed (HTTP {})", rec.timestamp_str, link_resp.status_code
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[{}] Could not link video: {}", rec.timestamp_str, exc)
    elif session_id and not config.pi_session_cookie:
        logger.warning("[{}] Skipping link — set PI_SESSION_COOKIE to enable", rec.timestamp_str)

    return result
