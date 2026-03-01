# Release Notes

## Unreleased (main, 2026-03-01)

### Public internet access via Tailscale Funnel (#82–#89)

The logger, Grafana, and Signal K are now accessible over the public internet
via Tailscale Funnel — no separate domain, certificate, or firewall changes
needed. All three routes are configured automatically by `setup.sh` and
`deploy.sh`.

| Public path | Local service |
|---|---|
| `https://corvopi.<tailnet>.ts.net/` | j105-logger (port 3002) |
| `https://corvopi.<tailnet>.ts.net/grafana/` | Grafana (port 3001) |
| `https://corvopi.<tailnet>.ts.net/signalk/` | Signal K (port 3000) |

Changes across PRs #82–#89:

- **PR #82** — initial Tailscale path-based routing added to `setup.sh`
- **PR #83** — made Signal K npm install non-fatal (was aborting setup); added
  Tailscale route application to `deploy.sh` so deploys keep routes current
- **PR #84** — updated to current Tailscale CLI syntax (`tailscale funnel --bg`)
- **PR #85** — fixed Signal K plugin name from `@signalk/derived-data` (404 on
  npm) to the correct unscoped name `signalk-derived-data`; required for true
  wind (TWS/TWA/TWD) computation
- **PR #86** — used `tailscale funnel --bg --set-path` for sub-path routing
- **PR #87** — added `sudo tailscale set --operator=$USER` prerequisite; without
  it `tailscale funnel` returns "Access denied"
- **PR #88** — set Grafana `GF_SERVER_ROOT_URL` to the actual Tailscale hostname
  so Grafana deep-links resolve to the correct public URL
- **PR #89** — removed `GF_SERVER_SERVE_FROM_SUB_PATH=true`; with Tailscale
  Funnel stripping path prefixes, the `SERVE_FROM_SUB_PATH` flag caused an
  infinite redirect loop

### Audio transcription (#42, PR #63)

Completed recordings can now be transcribed to text directly from the History
page. Transcription runs on the Pi via
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) — no cloud service
required.

- **📝 Transcript** button on every audio-enabled race card in History
- Jobs run in the background; status polling shows a spinner until complete
- Transcripts are stored in SQLite (`transcripts` table, schema v15)
- Model is configurable via `WHISPER_MODEL` env var (default: `base`)
- Speaker diarisation deferred — pyannote.audio is too heavy for Pi CPU

### System health monitoring (#39, PR #62)

The logger now watches the Pi's vitals automatically.

- `monitor_loop` background task collects CPU/memory/disk/temperature via
  `psutil` every 60 s and writes a `system_health` measurement to InfluxDB
- Home page polls `/api/system-health` every 30 s; a red warning banner
  appears when disk > 85 % or CPU temp > 75 °C
- `GET /api/system-health` endpoint available for external monitoring

### Audio playback and download (#21, PR #61)

WAV recordings can now be played or downloaded directly from the web UI
without needing to SSH into the Pi.

- Inline `<audio>` player on every History race card with an associated recording
- `GET /api/audio/{id}/stream` — browser-range-request compatible streaming
- `GET /api/audio/{id}/download` — downloads the WAV with `Content-Disposition: attachment`

### Photo caching (#44, PR #61)

Photo notes no longer reload on every page refresh, which was noticeably slow
over the boat's Wi-Fi hotspot.

- `serve_note_photo` now returns `ETag` + `Cache-Control: public, max-age=31536000, immutable`
- `304 Not Modified` responses on repeat loads (effectively free after first load)
- `loading="lazy"` on all photo `<img>` tags

---

## Earlier features (main, 2026-02-27)

### Sail tracking (#57, PR #60)

- Sail inventory management on the Boats page (add / delete sails by type and name)
- Per-race sail selection on History race cards (main, jib, kite)
- Sail choices stored in `race_sails` table (schema v14)

### Guest crew position, WAV player on home page, debrief audio (#38, #49, #31, PR #59)

- Crew member positions include a **Guest** option
- The home page shows a live audio player when a recording is in progress
- Debrief sessions can be added with a named crew list

### YouTube video linking with Grafana deep links (#22, PR #51 / #58)

- Link any YouTube video to instrument data via a UTC/offset sync point
- Every CSV export row gets a `video_url` deep-link (`?t=<seconds>`)
- Grafana annotation popups link directly to the matching video timestamp
- History page **Add Video** form with optional sync calibration

### Grafana annotations endpoint (#52, PR #54)

- `POST /api/grafana/annotations` — creates Grafana annotations from race/practice events
- Enables click-through from Grafana time-series panels to race timestamps

### Git version in web UI footer (#55, PR #56)

- Branch name and short commit SHA shown in the footer of every web page
- Makes it easy to confirm which version is running on the Pi

### Grafana InfluxDB datasource and dashboards (#earlier)

- Boatspeed, wind, heading, depth, position — all provisioned at setup time
- `can-interface.service` → `signalk.service` → `j105-logger.service` dependency chain

### External data: weather and tides

- Open-Meteo hourly weather (wind, air temp, pressure) fetched once per hour
- NOAA CO-OPS hourly tide predictions fetched once per day
- Both written to SQLite and included as extra columns in CSV exports

### Audio recording

- Automatic WAV recording from USB Audio Class devices (Gordik 2T1R tested)
- One file per session in `data/audio/`, named by UTC start timestamp
- Graceful degradation — no device means instrument logging continues unaffected
