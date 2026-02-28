# CLAUDE.md — J105 Sailboat Data Logger

## Project Overview

A Raspberry Pi-based NMEA 2000 data logger that reads from a B&G instrument system via a CAN bus HAT, stores time-series sailing data (boatspeed, wind, heading, etc.), correlates it with external data sources and YouTube video streams, and exports it in formats compatible with regatta performance analysis tools (e.g., Sailmon, Regatta tools).

---

## Stack & Tooling

| Concern | Tool |
|---|---|
| Dependency management | `uv` |
| NMEA 2000 / CAN bus | `python-can`, `canboat` (via subprocess for PGN decoding) |
| Storage | SQLite via `sqlite3` (stdlib) or `aiosqlite` for async |
| Linting + formatting | `ruff` |
| Type checking | `mypy` |
| Testing | `pytest` |
| Logging | `loguru` |
| External data / YouTube | `yt-dlp`, `httpx` for API calls |

---

## Project Structure

```
j105-logger/
├── CLAUDE.md
├── README.md
├── pyproject.toml          # uv-managed, single source of truth for deps & config
├── .python-version         # pin Python version (e.g. 3.12)
│
├── src/
│   └── logger/
│       ├── __init__.py
│       ├── main.py             # Entry point / CLI
│       ├── can_reader.py       # CAN bus interface & raw frame reading
│       ├── nmea2000.py         # PGN decoding, data extraction
│       ├── storage.py          # SQLite read/write, schema management
│       ├── external.py         # Non-instrument data sources (weather, tides, etc.)
│       ├── video.py            # YouTube stream timestamping / metadata
│       └── export.py           # Export to CSV / formats for regatta tools
│
├── tests/
│   ├── conftest.py
│   ├── test_nmea2000.py
│   ├── test_storage.py
│   └── test_export.py
│
├── data/                   # Local SQLite DB and exported files (gitignored)
├── scripts/                # One-off utility scripts (not part of the package)
└── docs/                   # Notes on PGN mappings, B&G specifics, etc.
```

---

## Common Commands

```bash
# Install dependencies
uv sync

# Run the logger
uv run python -m logger.main

# Run tests
uv run pytest

# Run tests with coverage
uv run pytest --cov=src/logger

# Lint & format check
uv run ruff check .
uv run ruff format --check .

# Auto-fix lint + format
uv run ruff check --fix .
uv run ruff format .

# Type check
uv run mypy src/
```

---

## Coding Conventions

- **Python 3.12+** — use modern syntax: `match`, `X | Y` unions, `tomllib`, etc.
- **Type hints everywhere** — all functions must have fully annotated signatures. Run mypy clean.
- **Ruff is the single formatter and linter** — do not introduce black, isort, or flake8.
- **Modules are small and single-purpose** — if a module is growing beyond ~200 lines, split it.
- **Use `loguru` for all logging** — never use `print()` for operational output.
- **Dataclasses or `typing.TypedDict`** for structured data (e.g., decoded PGN records) — avoid raw dicts with unknown shapes.
- **Keep hardware-dependent code isolated** — CAN bus reads should only happen in `can_reader.py` so the rest of the codebase can be tested without physical hardware.

---

## Architecture Principles

- **Hardware isolation**: `can_reader.py` is the only module that touches the CAN bus. All other modules receive decoded data structures, not raw frames. This allows full unit testing on non-Pi hardware.
- **Decode early, store clean**: Raw CAN frames are decoded to named PGN records as soon as they're read. Nothing downstream handles raw bytes.
- **SQLite is the single source of truth**: All data — instrument, external, video metadata — is written to SQLite with a UTC timestamp. Export functions read from SQLite, never from live data.
- **Timestamps are always UTC**: Store and compute in UTC. Convert to local time only at display/export boundaries.
- **External data is async-friendly**: Use `httpx` with async if fetching external sources during logging runs.

---

## Key Data: Important NMEA 2000 PGNs (B&G)

| PGN | Description |
|---|---|
| 127250 | Vessel Heading |
| 128259 | Speed Through Water (boatspeed) |
| 128267 | Water Depth |
| 129025 | Position (Lat/Lon rapid update) |
| 129026 | COG & SOG (GPS) |
| 130306 | Wind Data (speed + angle) |
| 130310 | Environmental (water temp) |

Document any B&G-specific proprietary PGNs in `docs/` as they are discovered.

---

## Dos and Don'ts

**Do:**
- Write tests for all decoding and export logic
- Use `uv add <package>` to add dependencies — never edit `pyproject.toml` manually for deps
- Keep the SQLite schema versioned with simple integer migrations in `storage.py`
- Log every CAN read error and decode failure with `loguru` at `WARNING` or above
- Export data in standard formats first (CSV with standard column names) before custom formats

**Don't:**
- Don't parse NMEA 2000 PGNs manually from scratch — use `canboat` or a library; only write custom decoders when necessary
- Don't store data in memory across long runs — flush to SQLite frequently to survive crashes/reboots
- Don't hardcode device paths (e.g., `/dev/can0`) — use config or environment variables
- Don't mix business logic into `main.py` — it should only wire things together and start the loop
- Don't commit the `data/` directory or any `.db` files

---

## Environment & Configuration

Configuration is via environment variables or a `.env` file (loaded with `python-dotenv`):

```
CAN_INTERFACE=can0          # CAN bus interface name
CAN_BITRATE=250000          # NMEA 2000 standard bitrate
DB_PATH=data/logger.db      # SQLite database path
LOG_LEVEL=INFO              # loguru log level
AUDIO_DEVICE=Gordik         # name substring to match (or integer index); omit to auto-detect
AUDIO_DIR=data/audio        # where WAV files are saved
AUDIO_SAMPLE_RATE=48000     # sample rate in Hz
AUDIO_CHANNELS=1            # 1=mono, 2=stereo
NOTES_DIR=data/notes        # where photo notes are saved
```

---

## Testing Strategy

- Unit tests live in `tests/` and should run on any machine (no Pi hardware required)
- Use `pytest` fixtures in `conftest.py` to provide in-memory SQLite DBs and sample decoded PGN data
- Mock `can_reader.py` with pre-recorded CAN frame fixtures for integration tests
- Aim for high coverage on `nmea2000.py`, `storage.py`, and `export.py` — these are the most critical paths
