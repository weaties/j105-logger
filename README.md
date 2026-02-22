# J105 Logger

NMEA 2000 data logger for J105 sailboat racing performance analysis. Runs on a
Raspberry Pi with a CAN bus HAT connected to the B&G instrument network. Logs
boatspeed, wind, heading, depth, position, and water temperature to SQLite, then
exports to CSV for analysis in regatta tools.

---

## Table of Contents

1. [Daily use](#daily-use)
2. [Linking YouTube videos](#linking-youtube-videos)
3. [Fresh SD card setup](#fresh-sd-card-setup)
4. [Updating](#updating)

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

### Export to CSV

```bash
j105-logger export \
  --start "2025-08-10T13:00:00" \
  --end   "2025-08-10T15:30:00" \
  --out   data/race1.csv
```

Timestamps are UTC ISO 8601. The output CSV has one row per second with columns:
`timestamp, HDG, BSP, DEPTH, LAT, LON, COG, SOG, TWS, TWA, AWA, AWS, WTEMP`

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

The service depends on `can-interface.service`, which brings up `can0` at boot.
Both services start automatically when the Pi is powered on.

### CAN bus health check

```bash
# Should show frames streaming in — if blank, check the physical connection
candump can0

# Interface details (state should be ERROR-ACTIVE when bus is healthy)
ip -details link show can0
```

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

## Fresh SD card setup

This covers everything from a blank SD card to a fully running logger.

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
sudo apt-get install -y git can-utils
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
# From your home directory
cd ~
git clone https://github.com/weaties/j105-logger.git
cd j105-logger
```

If you prefer SSH (and have a deploy key or personal key on the Pi):

```bash
git clone git@github.com:weaties/j105-logger.git
```

### 7. Run the setup script

```bash
./scripts/setup.sh
```

This script is idempotent — safe to re-run after updates. It:

1. Installs `uv` (Python package manager) to `~/.local/bin/`
2. Creates a Python 3.12 virtual environment and installs all dependencies
3. Creates a `.env` config file from the template
4. Creates the `data/` directory for the SQLite database
5. Adds your user to the `netdev` group (required for non-root CAN bus access)
6. Installs and enables `can-interface.service` (brings up `can0` at boot)
7. Installs and enables `j105-logger.service` (starts the logger automatically)

After the script completes, **reboot once** to activate the `netdev` group
membership (or run `newgrp netdev` in your current shell):

```bash
sudo reboot
```

### 8. Add `uv` to your PATH

`uv` is installed to `~/.local/bin/`. Add it permanently:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

### 9. Verify everything works

```bash
# CAN interface should be ERROR-ACTIVE (no bus) or ERROR-ACTIVE with frames
ip -details link show can0

# Logger service should be active (it will wait for the boat's NMEA bus)
sudo systemctl status j105-logger

# Run the status command — should show 0 rows until the boat's bus is connected
j105-logger status
```

On the boat, once the NMEA 2000 bus is powered up:

```bash
# You should see a stream of CAN frames
candump can0

# Logger logs (confirm PGNs are being decoded)
sudo journalctl -fu j105-logger
```

---

## Updating

After a `git pull`, re-run setup to pick up any dependency or service changes:

```bash
cd ~/j105-logger
git pull
./scripts/setup.sh
sudo systemctl restart j105-logger
```

---

## Configuration

Settings live in `~/j105-logger/.env`:

```bash
CAN_INTERFACE=can0      # SocketCAN interface name
CAN_BITRATE=250000      # NMEA 2000 standard bitrate
DB_PATH=data/logger.db  # SQLite database path (relative to project root)
LOG_LEVEL=INFO          # loguru log level: DEBUG, INFO, WARNING, ERROR
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

### `can0` interface missing after reboot

The `dtoverlay` line in `/boot/firmware/config.txt` is missing or malformed.
Check:
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
- `can0` not up yet (check `can-interface.service` status)
- `.env` file missing (re-run `./scripts/setup.sh`)
- `netdev` group not applied (reboot required after first setup)

### Permission denied on `can0`

Your user isn't in the `netdev` group, or the group change hasn't taken effect.
Reboot the Pi. Confirm with:
```bash
groups weaties   # should include 'netdev'
```
