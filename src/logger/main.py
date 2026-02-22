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


async def _run() -> None:
    """Main async loop: read CAN frames, decode, persist."""
    from logger.can_reader import CANReader, CANReaderConfig, extract_pgn
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
    except KeyboardInterrupt:
        logger.info("Interrupted by user — shutting down")
    except Exception as exc:
        logger.exception("Fatal error: {}", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
