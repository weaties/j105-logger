"""Entry point — wires modules together and runs the async logging loop.

Business logic lives in the other modules; this module only orchestrates them.
"""

from __future__ import annotations

import asyncio
import sys

from loguru import logger


def _load_env() -> None:
    """Load .env file if present (best-effort)."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # pragma: no cover
        pass


async def run_logger() -> None:
    """Main async loop: read CAN frames, decode, persist."""
    from logger.can_reader import CANReader, CANReaderConfig, extract_pgn
    from logger.nmea2000 import decode
    from logger.storage import Storage, StorageConfig

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
    finally:
        await storage.close()
        logger.info("Logger stopped")


def main() -> None:
    """CLI entry point."""
    _load_env()

    # Configure loguru from environment
    import os

    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logger.remove()
    logger.add(sys.stderr, level=log_level)

    logger.info("J105 Logger starting up")

    try:
        asyncio.run(run_logger())
    except KeyboardInterrupt:
        logger.info("Interrupted by user — shutting down")
    except Exception as exc:
        logger.exception("Fatal error: {}", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
