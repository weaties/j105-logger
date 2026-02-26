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


async def _tide_loop(storage: object, fetcher: object) -> None:
    """Background task: fetch NOAA tide predictions daily and persist them.

    Fetches today's and tomorrow's hourly predictions at startup, then every
    24 hours. Using two days ensures full coverage when logging spans midnight.
    INSERT OR IGNORE makes re-fetching idempotent.
    """
    from datetime import UTC, timedelta
    from datetime import datetime as _datetime

    from logger.external import ExternalFetcher
    from logger.storage import Storage

    assert isinstance(storage, Storage)
    assert isinstance(fetcher, ExternalFetcher)

    while True:
        try:
            pos = await storage.latest_position()
            if pos is not None:
                lat = float(pos["latitude_deg"])
                lon = float(pos["longitude_deg"])
                now = _datetime.now(UTC)
                for delta in (0, 1):  # today and tomorrow
                    target_date = (now + timedelta(days=delta)).date()
                    readings = await fetcher.fetch_tide_predictions(lat, lon, target_date)
                    for reading in readings:
                        await storage.write_tide(reading)
            else:
                logger.debug("No position data yet; skipping tide fetch")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Tide loop error (will retry in 24h): {}", exc)

        await asyncio.sleep(86400)  # re-fetch once per day


async def _audio_loop(storage: object) -> None:
    """Background task: record audio for the duration of the session.

    Gracefully handles the case where no audio input device is available
    (logs a warning and returns without failing the run).
    """
    from logger.audio import AudioConfig, AudioDeviceNotFoundError, AudioRecorder
    from logger.storage import Storage

    assert isinstance(storage, Storage)

    config = AudioConfig()
    recorder = AudioRecorder()
    session_id: int | None = None
    try:
        session = await recorder.start(config)
        session_id = await storage.write_audio_session(session)
        logger.info("Audio recording started: {}", session.file_path)
        await asyncio.Event().wait()  # wait until cancelled
    except AudioDeviceNotFoundError as exc:
        logger.warning("No audio input device found — audio recording disabled: {}", exc)
    except asyncio.CancelledError:
        if session_id is not None:
            completed = await recorder.stop()
            assert completed.end_utc is not None
            await storage.update_audio_session_end(session_id, completed.end_utc)
            logger.info("Audio recording saved: {}", completed.file_path)
        raise


async def _run() -> None:
    """Main async loop: read instrument data, decode, persist.

    Data source is selected by the DATA_SOURCE environment variable:
      signalk (default) — consume the Signal K WebSocket feed (SK owns can0)
      can               — read raw CAN frames directly (legacy mode)
    """
    import os

    from logger.external import ExternalFetcher
    from logger.storage import Storage, StorageConfig

    # Cancel this task on SIGTERM so finally blocks run and storage is flushed.
    loop = asyncio.get_running_loop()
    current = asyncio.current_task()
    assert current is not None
    loop.add_signal_handler(signal.SIGTERM, current.cancel)

    data_source = os.environ.get("DATA_SOURCE", "signalk").lower()
    storage_config = StorageConfig()
    storage = Storage(storage_config)
    await storage.connect()

    async with ExternalFetcher() as fetcher:
        weather_task = asyncio.create_task(_weather_loop(storage, fetcher))
        tide_task = asyncio.create_task(_tide_loop(storage, fetcher))
        audio_task = asyncio.create_task(_audio_loop(storage))
        try:
            if data_source == "signalk":
                from logger.sk_reader import SKReader, SKReaderConfig

                sk_config = SKReaderConfig()
                logger.info(
                    "Logger starting: source=signalk host={}:{} db={}",
                    sk_config.host,
                    sk_config.port,
                    storage_config.db_path,
                )
                async for record in SKReader(sk_config):
                    await storage.write(record)
            else:
                from logger.can_reader import CANReader, CANReaderConfig, extract_pgn
                from logger.nmea2000 import decode

                can_config = CANReaderConfig()
                logger.info(
                    "Logger starting: source=can interface={} db={}",
                    can_config.interface,
                    storage_config.db_path,
                )
                async for frame in CANReader(can_config):
                    pgn = extract_pgn(frame.arbitration_id)
                    src = frame.arbitration_id & 0xFF
                    decoded = decode(pgn, frame.data, src, frame.timestamp)
                    if decoded is not None:
                        await storage.write(decoded)
        except asyncio.CancelledError:
            logger.info("Shutdown signal received — flushing and stopping")
        finally:
            weather_task.cancel()
            tide_task.cancel()
            audio_task.cancel()
            await asyncio.gather(weather_task, tide_task, audio_task, return_exceptions=True)
            await storage.close()
            logger.info("Logger stopped")


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


async def _export(start_iso: str, end_iso: str, out: str) -> None:
    """Export a time range from the DB to CSV."""
    from logger.export import export_to_file
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
        rows = await export_to_file(storage, start, end, out)
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
# list-audio
# ---------------------------------------------------------------------------


async def _list_audio() -> None:
    """Print all recorded audio sessions."""
    from logger.storage import Storage, StorageConfig

    storage = Storage(StorageConfig())
    await storage.connect()
    try:
        sessions = await storage.list_audio_sessions()
    finally:
        await storage.close()

    if not sessions:
        print("No audio sessions recorded.")
        return

    print(f"{'File':<45} {'Duration':>9}  {'Start UTC'}")
    print("-" * 80)
    for s in sessions:
        if s.end_utc is not None:
            dur_s = int((s.end_utc - s.start_utc).total_seconds())
            h, rem = divmod(dur_s, 3600)
            m, sec = divmod(rem, 60)
            dur = f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
        else:
            dur = "in progress"
        short_path = s.file_path[-45:] if len(s.file_path) > 45 else s.file_path
        print(f"{short_path:<45} {dur:>9}  {s.start_utc.isoformat()}")


# ---------------------------------------------------------------------------
# list-devices
# ---------------------------------------------------------------------------


async def _list_devices() -> None:
    """Print available audio input devices."""
    from logger.audio import AudioRecorder

    devices = AudioRecorder.list_devices()
    if not devices:
        print("No audio input devices found.")
        return

    print(f"{'Idx':>3}  {'Name':<40}  {'Ch':>3}  {'Default rate':>12}")
    print("-" * 65)
    for dev in devices:
        print(
            f"{dev['index']:>3}  {str(dev['name']):<40}  "
            f"{dev['max_input_channels']:>3}  {dev['default_samplerate']:>12.0f}"
        )


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
    exp.add_argument(
        "--out",
        default="data/export.csv",
        metavar="FILE",
        help="Output file path; format inferred from extension (.csv, .gpx, .json)",
    )

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

    sub.add_parser("list-audio", help="List recorded audio sessions")
    sub.add_parser("list-devices", help="List available audio input devices")

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
            case "list-audio":
                asyncio.run(_list_audio())
            case "list-devices":
                asyncio.run(_list_devices())
    except KeyboardInterrupt:
        logger.info("Interrupted by user — shutting down")
    except Exception as exc:
        logger.exception("Fatal error: {}", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
