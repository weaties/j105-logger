# J105 Logger

NMEA 2000 data logger for J105 sailboat racing performance analysis. Runs on a
Raspberry Pi with a CAN bus HAT connected to the B&G instrument network. Signal K
Server decodes the NMEA 2000 bus and feeds both InfluxDB → Grafana (real-time
dashboards) and j105-logger (SQLite → CSV/GPX/JSON for regatta analysis tools).

Two Signal K plugins are required:
- **signalk-to-influxdb2** — forwards all SK data to InfluxDB for Grafana dashboards
- **@signalk/derived-data** — computes true wind (TWS/TWA/TWD) from apparent wind + boatspeed + heading; without this, true wind fields will be empty in the logger and exports

---

## Architecture

```
CAN Bus (can0)
    │
    ▼
Signal K Server          ← owns can0, decodes NMEA 2000 via canboatjs
    ├──► InfluxDB 2.7.11 ← via signalk-to-influxdb2 plugin
    │        └──► Grafana  ← real-time dashboards, port 3001
    └──► WebSocket ws://localhost:3000/signalk/v1/stream
              │
              ▼
         j105-logger (sk_reader.py)
              │
              ▼
         SQLite (storage.py)
              │
              ▼
         export.py  →  CSV / GPX / JSON  →  Sailmon, regatta tools
```

Service dependency chain:
```
can-interface.service  →  signalk.service  →  j105-logger.service
influxd.service        (independent, starts at boot)
grafana-server.service (independent, starts at boot)
```

---

## Table of Contents

1. [Daily use](#daily-use)
2. [Web interfaces](#web-interfaces)
3. [Race marking](#race-marking)
4. [Linking YouTube videos](#linking-youtube-videos)
5. [External data — weather and tides](#external-data--weather-and-tides)
6. [Recording audio commentary](#recording-audio-commentary)
7. [Fresh SD card setup](#fresh-sd-card-setup)
8. [Updating](#updating)
9. [Configuration](#configuration)
10. [Troubleshooting](#troubleshooting)

---

## Daily use

> These commands assume the Pi is already set up. SSH in via Tailscale
> (`ssh weaties@corvopi`) and run from the project directory.

### Check what's in the database

```bash
j105-logger status
```

```
Table                    Rows  Last seen
-------------------------------------------------------
headings                 1823  2025-08-10T14:32:05.123+00:00
speeds                   1821  2025-08-10T14:32:05.089+00:00
winds                    1820  2025-08-10T14:32:04.997+00:00
...
```

### Start logging (manual / foreground)

```bash
j105-logger run
```

Press `Ctrl-C` to stop. All buffered data is flushed before exit.

### Export to CSV, GPX, or JSON

```bash
j105-logger export \
  --start "2025-08-10T13:00:00" \
  --end   "2025-08-10T15:30:00" \
  --out   data/race1.csv
```

The format is inferred from the file extension:

| Extension | Format | Best for |
|---|---|---|
| `.csv` | Comma-separated values | Spreadsheets, Sailmon, custom analysis |
| `.gpx` | GPX 1.1 XML track | Navigation apps, course replay tools |
| `.json` | Structured JSON | Custom scripts, programmatic analysis |

```bash
# GPX — only seconds with GPS position produce a <trkpt>
j105-logger export --start "2025-08-10T13:00:00" --end "2025-08-10T15:30:00" \
  --out data/race1.gpx

# JSON — numeric values are typed (null instead of empty string for missing data)
j105-logger export --start "2025-08-10T13:00:00" --end "2025-08-10T15:30:00" \
  --out data/race1.json
```

Timestamps are UTC ISO 8601. The output CSV has one row per second with columns:

| Column | Description |
|---|---|
| `timestamp` | UTC ISO 8601 |
| `HDG` | Heading (degrees true) |
| `BSP` | Boatspeed through water (knots) |
| `DEPTH` | Water depth (metres) |
| `LAT` / `LON` | GPS position (decimal degrees) |
| `COG` / `SOG` | Course and speed over ground |
| `TWS` / `TWA` | True wind speed (kts) and angle (°) |
| `AWA` / `AWS` | Apparent wind angle (°) and speed (kts) |
| `WTEMP` | Water temperature (°C) |
| `video_url` | YouTube deep-link for that second (empty if no video linked) |
| `WX_TWS` / `WX_TWD` | Synoptic wind speed (kts) and direction (°) from Open-Meteo |
| `AIR_TEMP` | Air temperature (°C) from Open-Meteo |
| `PRESSURE` | Surface pressure (hPa) from Open-Meteo |
| `TIDE_HT` | Tide height above MLLW (metres) from NOAA CO-OPS |

Weather and tide columns are hourly resolution — all seconds within the same
hour share the same value. They are empty if the Pi had no internet or GPS
lock when the session was logged.

### Manage the background service

The logger runs automatically as a systemd service when the Pi boots on the boat.

```bash
# Check status
sudo systemctl status j105-logger

# View live logs
sudo journalctl -fu j105-logger

# Stop / start / restart
sudo systemctl stop    j105-logger
sudo systemctl start   j105-logger
sudo systemctl restart j105-logger
```

The service depends on `signalk.service`, which in turn depends on
`can-interface.service`. All three start automatically at boot.

### CAN bus health check

```bash
# Should show frames streaming in — if blank, check the physical connection
candump can0

# Interface details (state should be ERROR-ACTIVE when bus is healthy)
ip -details link show can0
```

---

## Web interfaces

All four are available from any device on your Tailscale network:

| Interface | URL | Purpose |
|---|---|---|
| Signal K | `http://corvopi:3000` | NMEA 2000 data explorer, plugin management |
| Grafana | `http://corvopi:3001` | Real-time sailing dashboards |
| j105-logger | `http://corvopi:3002` | Race marker (mobile-optimised) |
| InfluxDB | `http://corvopi:8086` | Time-series data explorer, query UI |

Grafana is pre-provisioned with an InfluxDB datasource. The default credentials
are `admin` / `changeme123` — change these after first login.

---

## Race marking

The race-marker web page at `http://corvopi:3002` gives any crew device on
Tailscale a one-tap way to mark the start and end of each race. Race names tie
together instrument data, audio, and video for that window so exports can be
scoped to a specific race rather than a hand-entered time range.

### Opening the page

On any phone or tablet joined to your Tailscale network:
open `http://corvopi:3002` in a browser. Bookmark it for quick access at the
start line.

### Race naming

Race names follow the format `YYYYMMDD-{Event}-{N}` where N is the race number
for that UTC day (starting at 1).

| Day | Auto event |
|---|---|
| Monday | `BallardCup` |
| Wednesday | `CYC` |
| Any other | You are prompted to type an event name; it is saved and persists across logger restarts |

On Monday and Wednesday the event name is set automatically. On other days an
event name input appears above the race controls — type the event name and tap
**Save** before starting your first race.

### Starting and ending races

- **START RACE N** — opens a new race, auto-closes the previous one if it was
  still in progress, and begins the duration counter.
- **END RACE N** — closes the current race. The next Start will use N+1.

The page polls for updates every 10 seconds and ticks the duration counter
every second.

### Downloading race exports from the phone

Completed races in the "Today's races" list show **↓ CSV** and **↓ GPX**
buttons. Tapping either downloads that race's data directly to the phone.

### Security

Tailscale is the security boundary — all crew devices on the tailnet are
trusted. No login is required. An optional `WEB_PIN` env var is reserved for
future PIN-based access control (not yet implemented).

---

## Linking YouTube videos

If you record a race on video and upload it to YouTube, you can link it to
your instrument data. Once linked, every row in the exported CSV gets a
`video_url` column with a deep-link (`?t=<seconds>`) that jumps straight to
that moment in the video.

### How to find your sync point

You need one moment where you know both the **UTC time from the instrument
log** and **where that moment appears in the video** (seconds from the start).

A good sync point is the starting gun — it's visible on video and you can
find it in the log by looking for a sudden change in boatspeed or heading.

### Option A — you know when you pressed Record

If you noted the time when you started the camera, use `--start`:

```bash
j105-logger link-video \
  --url "https://youtu.be/YOUR_VIDEO_ID" \
  --start "2025-08-10T13:45:00"
```

This tells the system the video playback position at `T=0s` corresponds to
UTC `13:45:00`.

### Option B — sync on a known event (recommended)

This is more accurate. Pick any identifiable moment — the starting gun works
well — and note:

1. **Where it is in the video** — scrub to the moment in YouTube and read
   the time off the progress bar (e.g. `5:30` = 330 seconds)
2. **What UTC time it was** — look at your exported CSV for that event, or
   check `j105-logger status` to see timestamps and cross-reference with the
   log

```bash
j105-logger link-video \
  --url "https://youtu.be/YOUR_VIDEO_ID" \
  --sync-utc  "2025-08-10T14:05:30" \
  --sync-offset 330
```

The command fetches the video title and duration from YouTube and stores the
sync point. It prints a verification URL at the sync moment so you can
confirm the alignment is correct.

### List linked videos

```bash
j105-logger list-videos
```

```
Title                                      Duration  Sync UTC
--------------------------------------------------------------------------------
J105 Race — August 2025                      2:03:14  2025-08-10T14:05:30+00:00
  https://youtu.be/YOUR_VIDEO_ID
```

### Video links in the CSV export

Once a video is linked, run `export` as normal:

```bash
j105-logger export \
  --start "2025-08-10T13:00:00" \
  --end   "2025-08-10T15:30:00" \
  --out   data/race1.csv
```

The `video_url` column in the output will contain a clickable link for every
second that falls within the video's duration, and will be empty outside that
range. In Excel or Numbers, click the cell to jump directly to that moment.

---

## External data — weather and tides

When `j105-logger run` is active, two background tasks automatically fetch
external data and store it in the same SQLite database:

| Source | Data | Coverage |
|---|---|---|
| [Open-Meteo](https://open-meteo.com/) | Wind speed, wind direction, air temperature, pressure | Global, free, no API key |
| [NOAA CO-OPS](https://tidesandcurrents.noaa.gov/) | Hourly tide height predictions (MLLW datum) | US coastal waters, free, no API key |

Both tasks start as soon as the Pi has a GPS lock and internet access:

- **Weather** is fetched once per hour for the current position.
- **Tides** are fetched once per day — today's and tomorrow's full 24-hour
  prediction set — from the nearest NOAA station to the boat's position.
  Re-fetching is idempotent, so restarting the logger never creates duplicates.

The data appears automatically as extra columns in the CSV export (`WX_TWS`,
`WX_TWD`, `AIR_TEMP`, `PRESSURE`, `TIDE_HT`). No configuration is needed.

> **Offline use**: If the Pi has no internet (e.g. at anchor without Wi-Fi),
> the external fetches fail silently. The logger continues normally and those
> CSV columns will be empty for that session.

---

## Recording audio commentary

When `j105-logger run` is active, it automatically records audio from the
first available USB input device (or the one matching `AUDIO_DEVICE` in `.env`).
This is designed for the **Gordik 2T1R** wireless lavalier system, whose USB
receiver appears as a standard UAC device — no drivers needed.

Audio is saved as a WAV file per session in `data/audio/`, named with the UTC
start timestamp so it lines up directly with the instrument log.

### Initial setup

1. Plug the Gordik USB receiver into any USB port on the Pi.
2. Find its device name:

   ```bash
   j105-logger list-devices
   ```

   ```
   Idx  Name                                      Ch    Default rate
   -----------------------------------------------------------------
     0  Built-in Microphone                        2           44100
     1  Gordik 2T1R USB Audio                      1           48000
   ```

3. Set `AUDIO_DEVICE` in `.env` to a substring of the name (case-insensitive):

   ```bash
   # In ~/j105-logger/.env:
   AUDIO_DEVICE=Gordik
   ```

   Or use the integer index (`AUDIO_DEVICE=1`). If `AUDIO_DEVICE` is not set,
   the first available input device is used automatically.

4. Restart the logger service:

   ```bash
   sudo systemctl restart j105-logger
   ```

   Confirm with:
   ```bash
   sudo journalctl -fu j105-logger | grep -i audio
   # Audio recording started: data/audio/audio_20250810_140530.wav
   ```

### List recorded audio sessions

```bash
j105-logger list-audio
```

```
File                                          Duration  Start UTC
--------------------------------------------------------------------------------
data/audio/audio_20250810_140530.wav             1:23:45  2025-08-10T14:05:30+00:00
```

### WAV file naming

Files are named `audio_YYYYMMDD_HHMMSS.wav` using the UTC start time, so they
can be matched to the instrument log by timestamp.

### Graceful degradation

If no audio device is found at startup (e.g. Gordik receiver not plugged in),
the logger logs a warning and continues running normally — instrument data is
never interrupted by a missing audio device.

See `docs/audio-setup.md` for full details, including system dependency notes
and troubleshooting.

---

## Fresh SD card setup

This covers everything from a blank SD card to a fully running stack.

### 1. Flash the OS

Download and install **[Raspberry Pi Imager](https://www.raspberrypi.com/software/)**.

- **OS**: Raspberry Pi OS Lite (64-bit) — "Other → Raspberry Pi OS (other)"
- **Storage**: your SD card or SSD

Click the gear icon (⚙) before writing to pre-configure:

| Setting | Value |
|---|---|
| Hostname | `corvopi` (or whatever you like) |
| Enable SSH | Yes — "Allow public-key authentication only" |
| SSH public key | paste your Mac's `~/.ssh/id_ed25519.pub` |
| Username | `weaties` |
| Password | set one (used for sudo) |
| Wi-Fi | your home network SSID/password |
| Locale | your timezone |

Write the card, insert it into the Pi, and power on.

### 2. First SSH in

Find the Pi on your local network and connect:

```bash
ssh weaties@corvopi.local
```

If `.local` doesn't resolve, check your router's DHCP table for the IP.

### 3. System update

```bash
sudo apt-get update && sudo apt-get upgrade -y
```

### 4. Add to Tailscale

Tailscale lets you SSH into the Pi from anywhere — marina, dock, home — without
port-forwarding or a static IP.

```bash
# Install Tailscale
curl -fsSL https://tailscale.com/install.sh | sudo bash

# Connect to your Tailscale network and enable Tailscale SSH
sudo tailscale up --ssh --accept-dns=false

# Follow the URL printed in the terminal to authenticate in your browser
```

`--ssh` enables Tailscale's built-in SSH (you won't need to manage authorized_keys).
`--accept-dns=false` prevents Tailscale from overriding the Pi's DNS, which can
cause issues on some networks.

After joining, approve the machine in the [Tailscale admin console](https://login.tailscale.com/admin/machines).
From then on, SSH from anywhere with:

```bash
ssh weaties@corvopi
```

Pin the Pi's Tailscale IP if you want a stable address — check it with
`tailscale ip -4`.

#### Keep Tailscale running across reboots

```bash
sudo systemctl enable --now tailscaled
```

This is done automatically by the installer, but worth confirming:

```bash
sudo systemctl status tailscaled
```

### 5. Configure the CAN HAT

The HAT uses an MCP2515 CAN controller connected over SPI with a 16 MHz crystal
and the interrupt line on GPIO 25. Add this to the Pi's boot config:

```bash
sudo nano /boot/firmware/config.txt
```

Add at the very end of the file (before any `[pi*]` section tags, or after `[all]`):

```
dtparam=spi=on
dtoverlay=mcp2515-can0,oscillator=16000000,interrupt=25
```

> **Note**: `spi=on` may already be present. Only add it once.

Save and reboot:

```bash
sudo reboot
```

After rebooting, confirm the CAN interface appeared:

```bash
ip link show can0
# Should show: can0: <NOARP,ECHO> ...
```

### 6. Clone the repository

```bash
cd ~
git clone https://github.com/weaties/j105-logger.git
cd j105-logger
```

### 7. Run the setup script

```bash
./scripts/setup.sh
```

This script is fully idempotent — safe to re-run after updates. It installs and
configures:

1. Node.js 24 LTS (via NodeSource)
2. Signal K Server + plugins (`signalk-to-influxdb2`, `@signalk/derived-data`)
3. InfluxDB 2.7.11 (pinned; `apt-mark hold` prevents v3 auto-upgrade)
4. Grafana OSS (pre-provisioned InfluxDB datasource, port 3001)
5. `uv` and all Python dependencies
6. System audio libraries (`libportaudio2`, `libsndfile1`) for USB audio recording
7. `.env` config file from the template
8. `data/` directory for the SQLite database; `data/audio/` for WAV recordings
9. `netdev` group membership for non-root CAN bus access
10. `can-interface.service` — brings up `can0` at boot
11. `signalk.service` — starts Signal K after CAN is up
12. `j105-logger.service` — starts logger after Signal K is up

The InfluxDB admin token is saved to `~/influx-token.txt` (permissions 600).
If you ever lose it, retrieve it with:

```bash
influx auth list
```

### 8. Add `uv` to your PATH

`uv` is installed to `~/.local/bin/`. Add it permanently:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

### 9. Reboot and verify

```bash
sudo reboot
```

After rebooting:

```bash
# All five should be active
sudo systemctl status can-interface signalk influxd grafana-server j105-logger

# Logger rows accumulating
j105-logger status

# Signal K dashboard
# Open http://corvopi:3000 in a browser

# Grafana dashboards
# Open http://corvopi:3001 in a browser (admin/changeme123)
```

---

## Updating

After a `git pull`, re-run setup to pick up dependency or service changes:

```bash
cd ~/j105-logger
git pull
./scripts/setup.sh
sudo npm update -g signalk-server
sudo systemctl restart signalk j105-logger
```

---

## Configuration

Settings live in `~/j105-logger/.env`:

```bash
CAN_INTERFACE=can0      # SocketCAN interface name
CAN_BITRATE=250000      # NMEA 2000 standard bitrate
DB_PATH=data/logger.db  # SQLite database path (relative to project root)
LOG_LEVEL=INFO          # loguru log level: DEBUG, INFO, WARNING, ERROR
DATA_SOURCE=signalk     # signalk (default) or can (legacy direct CAN mode)
SK_HOST=localhost        # Signal K server hostname
SK_PORT=3000             # Signal K WebSocket port
# Audio recording (Gordik 2T1R or any USB Audio Class device)
# AUDIO_DEVICE=Gordik   # name substring or integer index; omit to auto-detect
AUDIO_DIR=data/audio    # directory for WAV files
AUDIO_SAMPLE_RATE=48000
AUDIO_CHANNELS=1
# Web interface (race marker)
WEB_HOST=0.0.0.0        # bind address
WEB_PORT=3002           # http://corvopi:3002 on Tailscale
# WEB_PIN=             # optional PIN (reserved, not yet implemented)
```

Edit with `nano ~/j105-logger/.env`. Changes take effect on the next
`sudo systemctl restart j105-logger`.

---

## Troubleshooting

### `j105-logger: command not found`

`uv` isn't in your PATH. Either:
```bash
export PATH="$HOME/.local/bin:$PATH"   # temporary
# or add it to ~/.bashrc permanently (see step 8 above)
```

Or use the full invocation:
```bash
~/.local/bin/uv run --project ~/j105-logger j105-logger status
```

### j105-logger can't connect to Signal K

The logger connects to Signal K's WebSocket at `ws://${SK_HOST}:${SK_PORT}/signalk/v1/stream`.

```bash
# Check Signal K is running
sudo systemctl status signalk

# Check Signal K logs
sudo journalctl -u signalk --no-pager -n 50

# Verify the WebSocket endpoint is up
curl -s http://localhost:3000/signalk/v1/api/ | python3 -m json.tool
```

If Signal K is running but the logger can't connect, check `SK_HOST` and
`SK_PORT` in `.env` match the Signal K server configuration.

### Signal K reports "socketcan stopped" (can-interface.service not running)

```bash
sudo systemctl status can-interface
sudo systemctl restart can-interface
sudo systemctl restart signalk
```

If `can-interface` fails, the CAN HAT likely isn't configured — see step 5
in the fresh SD card setup above.

### InfluxDB token lost

The token was saved at setup time to `~/influx-token.txt`:

```bash
cat ~/influx-token.txt
```

Or list all tokens via the CLI:

```bash
influx auth list
```

### `can0` interface missing after reboot

**Check 1 — `can-interface.service` not installed:**

If the setup script was never run (or failed partway through), the service that
brings up `can0` won't exist:

```bash
sudo systemctl status can-interface
```

If it shows "Unit can-interface.service could not be found", re-run setup:

```bash
./scripts/setup.sh
```

**Check 2 — `dtoverlay` line missing or malformed:**

The `dtoverlay` line in `/boot/firmware/config.txt` may be missing:

```bash
grep -n "mcp2515\|spi" /boot/firmware/config.txt
```

Expected output:
```
dtparam=spi=on
dtoverlay=mcp2515-can0,oscillator=16000000,interrupt=25
```

### CAN bus stays in `ERROR-PASSIVE`

The Pi is not connected to an active NMEA 2000 bus (no other nodes to
acknowledge frames). This is normal at home. On the boat, this should
clear to `ERROR-ACTIVE` within seconds of the bus powering up.

### Logger service fails to start

```bash
sudo journalctl -u j105-logger --no-pager
```

Common causes:
- Signal K not running yet (check `signalk.service` status)
- `.env` file missing (re-run `./scripts/setup.sh`)
- `netdev` group not applied (reboot required after first setup)

### Permission denied on `can0`

Signal K owns the CAN bus — j105-logger never touches it directly (it reads
from the Signal K WebSocket). If you see this error, check that `DATA_SOURCE`
in `.env` is set to `signalk` (the default), not `can`.
