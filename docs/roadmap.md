# Roadmap & TODO

Checked items are complete. Work through these roughly in order.

---

## Blocking — needed before first real logging session

- [x] **CAN HAT hardware setup** — MCP2515 HAT configured via `dtoverlay`, `can0` is UP at
      250000 bps. Verified in loopback mode (`ip link set can0 type can loopback on`) with
      all 7 supported PGNs decoded and persisted end-to-end.

- [x] **CLI subcommands** — `j105-logger run / export / status` all implemented and working.

- [x] **Batch SQLite writes** — buffered with 1 s / 200-record flush threshold in `Storage`.
      Non-blocking via `asyncio.to_thread` in the CAN reader. Graceful SIGTERM shutdown
      flushes remaining records before exit.

- [x] **Timestamp indexes** — schema migration v2 adds `idx_<table>_ts` on all 7 tables.

---

## Important — needed for reliable operation

- [ ] **CAN HAT verified with live traffic** — confirm each of the 7 supported PGNs is
      being decoded correctly against real B&G output. Use `candump` + the decoder to spot-check
      values against the chart plotter display.

- [ ] **B&G proprietary PGNs** — capture live CAN traffic and reverse-engineer any B&G-specific
      PGN payloads that carry data not covered by the standard PGNs. Document in `docs/pgn-notes.md`.

---

## Useful — add when the basics are working

- [ ] **Video correlation CLI** — `j105-logger run --video-url <youtube-url>` to associate a
      recording with a session. Store `VideoMetadata` in a new `video_metadata` DB table.
      Include video-relative timestamps in the CSV export.

- [ ] **External weather data** — implement `fetch_weather()` in `external.py` using the
      [Open-Meteo API](https://open-meteo.com/) (free, no key required). Add background async
      task in the run loop. Add `weather` table to storage schema (migration v3).

- [ ] **External tide data** — implement `fetch_tides()` using NOAA CO-OPS or WorldTides API.
      Add `tides` table to storage schema.

---

## Polish — low priority

- [ ] **Regatta export formats** — export to JSON and/or GPX in addition to CSV.
      Investigate column name conventions expected by Sailmon and other regatta analysis tools.

- [ ] **CAN frame filtering** — filter `CANReader` to only pass supported PGNs to the decoder,
      reducing CPU overhead from unsupported frames.

- [ ] **FastPacket reassembly** — support multi-frame NMEA 2000 messages (e.g. PGN 129029
      GNSS Position Data) if needed.

- [ ] **Integration tests** — add tests that replay a recorded `.log` file from `candump` through
      the full stack (reader → decoder → storage → export) to catch regressions with real data.

---

## Completed

- [x] NMEA 2000 PGN decoders for 7 standard PGNs (127250, 128259, 128267, 129025, 129026, 130306, 130310)
- [x] SQLite storage with async writes and integer-versioned migrations
- [x] CSV export joining all tables by second
- [x] External data stubs (tide, weather)
- [x] YouTube video metadata fetching via yt-dlp
- [x] Raspberry Pi setup script (`scripts/setup.sh`) — installs deps, CAN service, systemd logger service
- [x] Full test suite (56 tests — nmea2000, storage, export)
- [x] ruff + mypy clean
- [x] CAN HAT hardware setup & loopback testing on Pi (all 7 PGNs verified)
- [x] CLI subcommands: `run`, `export --start/--end/--out`, `status`
- [x] Batch SQLite writes (flush every 1 s / 200 records)
- [x] Timestamp indexes (schema migration v2)
- [x] Non-blocking CAN recv via `asyncio.to_thread`
- [x] Graceful SIGTERM/SIGINT shutdown with buffer flush
