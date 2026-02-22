"""YouTube video timestamping — correlate recorded video with logged data.

Uses yt-dlp to fetch video metadata (title, upload date, duration).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from loguru import logger

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VideoMetadata:
    """Metadata for a YouTube video used during a logging session."""

    url: str
    title: str
    start_time: datetime  # UTC start of the video recording
    duration_seconds: float


# ---------------------------------------------------------------------------
# VideoLogger
# ---------------------------------------------------------------------------


class VideoLogger:
    """Fetch and correlate YouTube video metadata with logged sailing data."""

    async def fetch_metadata(self, url: str) -> VideoMetadata:
        """Fetch video metadata from YouTube via yt-dlp.

        Runs yt-dlp in a subprocess to avoid blocking the event loop.

        Args:
            url: A YouTube video URL.

        Returns:
            A VideoMetadata instance.

        Raises:
            RuntimeError: If yt-dlp cannot retrieve the metadata.
        """
        logger.info("Fetching video metadata for: {}", url)

        # Run yt-dlp in a thread to avoid blocking
        loop = asyncio.get_event_loop()
        metadata = await loop.run_in_executor(None, self._fetch_sync, url)
        return metadata

    def _fetch_sync(self, url: str) -> VideoMetadata:
        """Synchronous yt-dlp metadata fetch (called from executor)."""
        try:
            import yt_dlp  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("yt-dlp is not installed") from exc

        ydl_opts: dict[str, object] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if info is None:
            raise RuntimeError(f"yt-dlp returned no info for {url!r}")

        title: str = str(info.get("title", ""))
        duration: float = float(info.get("duration", 0.0))

        # yt-dlp provides upload_date as YYYYMMDD; we use it as a best-effort
        # start time. For live streams, 'release_timestamp' may be available.
        release_ts: int | None = info.get("release_timestamp")
        upload_date: str | None = info.get("upload_date")

        if release_ts is not None:
            start_time = datetime.fromtimestamp(release_ts, tz=UTC)
        elif upload_date is not None:
            start_time = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=UTC)
        else:
            logger.warning("Video {!r}: no release timestamp or upload_date found", url)
            start_time = datetime.now(tz=UTC)

        logger.info(
            "Video metadata: title={!r} duration={}s start_time={}",
            title,
            duration,
            start_time,
        )
        return VideoMetadata(url=url, title=title, start_time=start_time, duration_seconds=duration)

    def correlate_timestamp(
        self,
        video_meta: VideoMetadata,
        elapsed_seconds: float,
    ) -> datetime:
        """Convert an elapsed video time to a UTC wall-clock timestamp.

        Args:
            video_meta:      Metadata for the video, including its UTC start time.
            elapsed_seconds: Seconds from the start of the video.

        Returns:
            The corresponding UTC datetime.
        """
        return video_meta.start_time + timedelta(seconds=elapsed_seconds)
