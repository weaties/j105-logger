# AGENTS.md — HelmLog

This file provides conventions for any AI coding agent working on HelmLog.
Claude Code users: see `CLAUDE.md` for additional Claude-specific skills and workflows.

---

## Project Overview

A Raspberry Pi-based sailing data logger that reads from a B&G instrument system via Signal K,
stores time-series data in SQLite, and provides a web interface for race marking, history,
debrief audio, and performance exports.

**Stack:** Python 3.12+, FastAPI, SQLite (aiosqlite), uv (dependency management), Ruff (lint/format), mypy (types), pytest (tests).

---

## Essential Commands

```bash
uv sync                          # install dependencies (always run first)
uv run pytest                    # run all tests
uv run ruff check .              # lint
uv run ruff format --check .     # format check
uv run mypy src/                 # type check
uv run pytest tests/integration/ -v  # federation integration tests
```

All four checks (tests, ruff check, ruff format, mypy) must pass before submitting a PR.

---

## Project Structure

```
src/helmlog/           # all source code
  main.py              # CLI entry point (wiring only, no business logic)
  web.py               # FastAPI routes and API endpoints
  storage.py           # SQLite schema, migrations, read/write
  sk_reader.py         # Signal K WebSocket reader (primary data source)
  can_reader.py        # CAN bus reader (legacy path)
  auth.py              # Magic-link authentication
  federation.py        # Boat identity (Ed25519), co-op membership
  peer_api.py          # Inter-boat peer API endpoints
  peer_auth.py         # Ed25519 request signing/verification
  peer_client.py       # Async HTTP client for peer boats
  export.py            # CSV / GPX / JSON export
  audio.py             # USB audio recording
  transcribe.py        # Whisper transcription + diarization
  polar.py             # Polar performance baseline
  templates/           # Jinja2 HTML templates
  static/              # CSS and JS

tests/                 # pytest suite (no hardware required)
tests/integration/     # federation integration tests
```

---

## Coding Conventions

- **Python 3.12+** — use modern syntax (`match`, `X | Y` unions, `tomllib`)
- **Full type annotations** on all function signatures — mypy must pass
- **Ruff** is the only linter/formatter — line length 100, `E501` suppressed only in `web.py`
- **Loguru** for all logging — never use `print()`
- **Dataclasses or TypedDict** for structured data — no raw dicts with unknown shapes
- Modules stay under ~200 lines; split when they grow
- Hardware-dependent code is isolated to its own module

---

## Architecture Rules

- **Signal K is the primary data source** — `sk_reader.py` connects to Signal K WebSocket
- **Hardware isolation** — hardware modules are only imported by `main.py`; other modules receive decoded data structures
- **Decode early, store clean** — raw data is decoded to named dataclasses immediately on arrival
- **SQLite is the single source of truth** — all data goes to SQLite; exports read from SQLite
- **Timestamps are always UTC** — convert to local time only at display/export boundaries
- **No business logic in `main.py`** — it only wires modules together

---

## Testing Requirements

- Follow TDD: write a failing test first, then implement
- Tests run without hardware — mock `audio.py`, `can_reader.py`, `sk_reader.py`, `cameras.py`
- Use the `storage` fixture from `conftest.py` for in-memory SQLite
- Use `httpx.AsyncClient` with `ASGITransport` for web route tests
- Federation/co-op/peer API changes require integration tests (`tests/integration/`)

---

## Do NOT

- Push directly to `main` — all changes go through PRs
- Use `print()` — use loguru
- Add raw dicts with string keys for structured data
- Put business logic in `main.py`
- Import hardware modules outside their isolated module
- Use `uv pip install` — use `uv add` (to add deps) or `uv sync` (to install)
- Commit `data/` directory or `.db` files
- Hardcode device paths — use config/environment variables

---

## Risk Tiers

Changes to these modules require extra care:

| Tier | Modules | Extra requirements |
|---|---|---|
| **Critical** | `auth.py`, `peer_auth.py`, `federation.py`, `storage.py` (migrations), `can_reader.py` | Integration tests + data-license review |
| **High** | `sk_reader.py`, `peer_api.py`, `peer_client.py`, `export.py`, `transcribe.py` | Integration tests where applicable |

---

## Data Licensing

HelmLog has a data licensing policy (`docs/data-licensing.md`). Code handling user data,
PII (audio, photos, emails, biometrics), co-op data sharing, or exports must comply.
Key rules: boat owns its data, PII has deletion rights, co-op data is view-only,
no gambling or protest committee exports.
