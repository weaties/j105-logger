"""Entry point — wires modules together and runs the async logging loop.

Business logic lives in the other modules; this module only orchestrates them.

Subcommands:
  run     Start the logging loop (default behaviour).
  export  Export a time range to CSV.
  status  Show DB row counts and last-seen timestamps per PGN table.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from datetime import UTC, datetime

from loguru import logger


def _load_env() -> None:
    """Load .env file if present (best-effort)."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # pragma: no cover
        pass


def _setup_logging() -> None:
    import os

    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logger.remove()
    logger.add(sys.stderr, level=log_level)


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


async def _weather_loop(storage: object, fetcher: object) -> None:
    """Background task: fetch weather from Open-Meteo every hour and persist it.

    Best-effort — logs a warning and continues if the fetch fails or if no
    position data is available yet.
    """
    from logger.external import ExternalFetcher
    from logger.storage import Storage

    assert isinstance(storage, Storage)
    assert isinstance(fetcher, ExternalFetcher)

    while True:
        try:
            pos = await storage.latest_position()
            if pos is not None:
                now = datetime.now(UTC)
                reading = await fetcher.fetch_weather(
                    float(pos["latitude_deg"]),
                    float(pos["longitude_deg"]),
                    now,
                )
                if reading is not None:
                    await storage.write_weather(reading)
            else:
                logger.debug("No position data yet; skipping weather fetch")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Weather loop error (will retry next hour): {}", exc)

        await asyncio.sleep(3600)


async def _run() -> None:
    """Main async loop: read CAN frames, decode, persist."""
    from logger.can_reader import CANReader, CANReaderConfig, extract_pgn
    from logger.external import ExternalFetcher
    from logger.nmea2000 import decode
    from logger.storage import Storage, StorageConfig

    # Cancel this task on SIGTERM so finally blocks run and storage is flushed.
    loop = asyncio.get_running_loop()
    current = asyncio.current_task()
    assert current is not None
    loop.add_signal_handler(signal.SIGTERM, current.cancel)

    reader_config = CANReaderConfig()
    storage_config = StorageConfig()

    storage = Storage(storage_config)
    await storage.connect()

    logger.info(
        "Logger starting: interface={} db={}",
        reader_config.interface,
        storage_config.db_path,
    )

    reader = CANReader(reader_config)
    async with ExternalFetcher() as fetcher:
        weather_task = asyncio.create_task(_weather_loop(storage, fetcher))
        try:
            async for frame in reader:
                pgn = extract_pgn(frame.arbitration_id)
                src = frame.arbitration_id & 0xFF
                record = decode(pgn, frame.data, src, frame.timestamp)
                if record is not None:
                    await storage.write(record)
        except asyncio.CancelledError:
            logger.info("Shutdown signal received — flushing and stopping")
        finally:
            weather_task.cancel()
            await asyncio.gather(weather_task, return_exceptions=True)
            await storage.close()
            logger.info("Logger stopped")


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


async def _export(start_iso: str, end_iso: str, out: str) -> None:
    """Export a time range from the DB to CSV."""
    from logger.export import export_csv
    from logger.storage import Storage, StorageConfig

    try:
        start = datetime.fromisoformat(start_iso).replace(tzinfo=UTC)
        end = datetime.fromisoformat(end_iso).replace(tzinfo=UTC)
    except ValueError as exc:
        logger.error("Invalid datetime: {}", exc)
        sys.exit(1)

    if end <= start:
        logger.error("--end must be after --start")
        sys.exit(1)

    storage = Storage(StorageConfig())
    await storage.connect()
    try:
        rows = await export_csv(storage, start, end, out)
        logger.info("Wrote {} rows to {}", rows, out)
    finally:
        await storage.close()


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


async def _status() -> None:
    """Print row counts and last-seen timestamps for each data table."""
    from logger.storage import Storage, StorageConfig

    storage = Storage(StorageConfig())
    await storage.connect()
    try:
        summary = await storage.status_summary()
    finally:
        await storage.close()

    print(f"{'Table':<20} {'Rows':>8}  {'Last seen'}")
    print("-" * 55)
    for table, info in summary.items():
        print(f"{table:<20} {info['count']:>8}  {info['last_seen']}")


# ---------------------------------------------------------------------------
# link-video
# ---------------------------------------------------------------------------


async def _link_video(url: str, sync_utc_iso: str, sync_offset_s: float) -> None:
    """Fetch YouTube metadata and store a VideoSession sync point."""
    from logger.storage import Storage, StorageConfig
    from logger.video import VideoLinker

    try:
        sync_utc = datetime.fromisoformat(sync_utc_iso).replace(tzinfo=UTC)
    except ValueError as exc:
        logger.error("Invalid datetime: {}", exc)
        sys.exit(1)

    linker = VideoLinker()
    session = await linker.create_session(url, sync_utc, sync_offset_s)

    storage = Storage(StorageConfig())
    await storage.connect()
    try:
        await storage.write_video_session(session)
    finally:
        await storage.close()

    # Print a quick sanity-check URL at the sync point itself
    check = session.url_at(sync_utc)
    logger.info("Linked. Verify sync point: {}", check)


# ---------------------------------------------------------------------------
# list-videos
# ---------------------------------------------------------------------------


async def _list_videos() -> None:
    """Print all linked YouTube video sessions."""
    from logger.storage import Storage, StorageConfig

    storage = Storage(StorageConfig())
    await storage.connect()
    try:
        sessions = await storage.list_video_sessions()
    finally:
        await storage.close()

    if not sessions:
        print("No videos linked.")
        return

    print(f"{'Title':<42} {'Duration':>8}  {'Sync UTC'}")
    print("-" * 80)
    for s in sessions:
        h, rem = divmod(int(s.duration_s), 3600)
        m, sec = divmod(rem, 60)
        dur = f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
        print(f"{s.title[:42]:<42} {dur:>8}  {s.sync_utc.isoformat()}")
        print(f"  {s.url}")


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="j105-logger",
        description="J105 NMEA 2000 Data Logger",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="Start the CAN logging loop")

    exp = sub.add_parser("export", help="Export a time range to CSV")
    exp.add_argument("--start", required=True, metavar="ISO", help="Start time (UTC ISO 8601)")
    exp.add_argument("--end", required=True, metavar="ISO", help="End time (UTC ISO 8601)")
    exp.add_argument("--out", default="data/export.csv", metavar="FILE", help="Output CSV path")

    sub.add_parser("status", help="Show DB row counts and last-seen timestamps")

    lv = sub.add_parser(
        "link-video",
        help="Link a YouTube video to logged data via a time sync point",
        description=(
            "Associates a YouTube video with your instrument log by providing a sync point.\n\n"
            "Option A — you know when you pressed Record:\n"
            "  j105-logger link-video --url URL --start 2025-08-10T13:45:00\n\n"
            "Option B — you know a specific moment in both the video and the log\n"
            "  (e.g. starting gun at video t=5:30, UTC 14:05:30):\n"
            "  j105-logger link-video --url URL --sync-utc 2025-08-10T14:05:30 --sync-offset 330"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    lv.add_argument("--url", required=True, metavar="URL", help="YouTube video URL")
    lv_sync = lv.add_mutually_exclusive_group(required=True)
    lv_sync.add_argument(
        "--start",
        metavar="ISO",
        help="UTC time when you pressed Record (sets sync offset to 0)",
    )
    lv_sync.add_argument(
        "--sync-utc",
        metavar="ISO",
        help="UTC time of a known sync point (pair with --sync-offset)",
    )
    lv.add_argument(
        "--sync-offset",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="Seconds into the video at --sync-utc (default: 0)",
    )

    sub.add_parser("list-videos", help="List linked YouTube videos")

    return parser


def main() -> None:
    """CLI entry point."""
    _load_env()
    _setup_logging()

    args = _build_parser().parse_args()

    logger.info("J105 Logger — command={}", args.command)

    try:
        match args.command:
            case "run":
                asyncio.run(_run())
            case "export":
                asyncio.run(_export(args.start, args.end, args.out))
            case "status":
                asyncio.run(_status())
            case "link-video":
                sync_utc_iso = args.start if args.start else args.sync_utc
                sync_offset = 0.0 if args.start else args.sync_offset
                asyncio.run(_link_video(args.url, sync_utc_iso, sync_offset))
            case "list-videos":
                asyncio.run(_list_videos())
    except KeyboardInterrupt:
        logger.info("Interrupted by user — shutting down")
    except Exception as exc:
        logger.exception("Fatal error: {}", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
