---
name: diagnose
description: Systematic Pi troubleshooting runbook — checks all subsystems (systemd, nginx, Signal K, SQLite, audio, InfluxDB, Tailscale) and reports health. TRIGGER when the user reports a Pi problem ("helmlog is down", "not recording", "web interface broken", "service won't start") or asks for a health check. DO NOT trigger for development issues on Mac, test failures, code questions, or deployment instructions (use /deploy-pi for those).
---

# /diagnose — Pi Troubleshooting Runbook

Systematically check the health of a HelmLog Pi deployment. Run this when the
Pi is misbehaving (no data, CAN errors, audio issues, web UI down) instead of
ad-hoc debugging.

## Usage

- `/diagnose` — run all subsystem checks
- `/diagnose system` — system health only
- `/diagnose services` — systemd services only
- `/diagnose can` — CAN bus only
- `/diagnose signalk` — Signal K only
- `/diagnose audio` — audio subsystem only
- `/diagnose database` — SQLite database only
- `/diagnose network` — network and connectivity only
- `/diagnose aihat` — AI HAT (Hailo) only

The argument is available as `$ARGUMENTS`. If empty, run all subsystems.

## Output Format

For each check, report a status line:

```
[OK]   Check name — detail
[WARN] Check name — detail → suggested fix
[FAIL] Check name — detail → suggested fix
```

After all checks, print a summary: total checks, pass/warn/fail counts, and
the most likely root cause if any failures were found.

## Diagnostic Sequence

Checks are ordered by dependency — earlier failures make later checks moot.
If a dependency fails, skip dependent checks and note why.

### 1. System Health

```bash
# Disk space (WARN >80%, FAIL >95%)
df -h /

# Memory (WARN >80% used, FAIL >95%)
free -m

# CPU temperature (WARN >70°C, FAIL >80°C)
cat /sys/class/thermal/thermal_zone0/temp
# Divide by 1000 for °C

# SD card health — check for read-only filesystem
touch /tmp/helmlog-health-check && rm /tmp/helmlog-health-check

# Uptime and load average
uptime
```

### 2. Services

**Dependency:** System health must not be FAIL (read-only filesystem blocks
everything).

```bash
# HelmLog service
# Note: is-active doesn't need sudo; status needs sudo for full output.
systemctl is-active helmlog
sudo systemctl status helmlog --no-pager -l
# If failed: sudo journalctl -u helmlog -n 30 --no-pager

# Signal K server (setup.sh names the service "signalk", not "signalk-server")
systemctl is-active signalk
# If failed: sudo journalctl -u signalk -n 30 --no-pager

# nginx (reverse proxy)
systemctl is-active nginx
# If failed: sudo nginx -t
```

**Known failure patterns:**

| Log signature | Meaning | Fix |
|---|---|---|
| `ModuleNotFoundError` | Stale venv | `cd ~/helmlog && uv sync` then restart |
| `Address already in use` | Port conflict | `sudo lsof -i :3002` to find conflict |
| `Permission denied: data/` | Ownership wrong | `sudo chown -R helmlog:helmlog data/` |

### 3. CAN Bus

**Dependency:** HelmLog service must be active.

```bash
# Interface present and UP
ip link show can0

# Message rate (should be >0 frames/sec when instruments on)
# Read for 2 seconds and count frames
timeout 2 candump can0 2>/dev/null | wc -l

# Error frames and bus-off state
ip -details -statistics link show can0
# Look for: "bus-off", "error-warning", "error-passive"
# tx_errors and rx_errors should be low (<100)

# CAN state
cat /sys/class/net/can0/operstate
# Should be "up"; "stopped" or missing = interface down
```

**Known failure patterns:**

| Log signature | Meaning | Fix |
|---|---|---|
| `Network is down` on can0 | Interface not brought up | `sudo ip link set can0 up type can bitrate 250000` |
| `No buffer space available` | CAN TX queue full | `sudo ip link set can0 txqueuelen 1000` then restart interface |
| High `rx_errors` / `tx_errors` | Bus noise or termination issue | Check CAN wiring and 120Ω termination resistors |
| `bus-off` state | Severe bus errors, controller shut down | `sudo ip link set can0 down && sudo ip link set can0 up type can bitrate 250000` |

### 4. Signal K

**Dependency:** Signal K service must be active.

```bash
# WebSocket connectivity — attempt connection to Signal K
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:3000/signalk/v1/api/

# Delta message flow — check if deltas are arriving
# Look in helmlog logs for recent SK messages
sudo journalctl -u helmlog --since "2 minutes ago" --no-pager | grep -c "sk_reader"

# Self endpoint (API health check)
curl -s http://127.0.0.1:3000/signalk/v1/api/self
```

**Known failure patterns:**

| Log signature | Meaning | Fix |
|---|---|---|
| `WebSocket connection closed` repeating | SK server dropping connections | Restart signalk: `sudo systemctl restart signalk` |
| `connection refused` on :3000 | SK not listening | Check `systemctl status signalk` |
| No deltas but SK is up | No CAN data reaching SK | Check CAN bus (subsystem 3) |
| `401 Unauthorized` | Auth token expired | Check SK admin password in `~/.signalk-admin-pass.txt` |

### 5. Audio

**Dependency:** HelmLog service must be active.

```bash
# USB audio device presence
arecord -l
# Should list at least one capture device (e.g., Gordik USB Audio)

# ALSA state — check for errors
cat /proc/asound/cards

# Quick recording test (1 second)
timeout 1 arecord -D default -f S16_LE -r 16000 -c 1 /tmp/helmlog-audio-test.wav 2>&1
# Check exit code: 0 = success
rm -f /tmp/helmlog-audio-test.wav
```

**Known failure patterns:**

| Log signature | Meaning | Fix |
|---|---|---|
| `no soundcards found` | USB device disconnected | Re-seat USB audio device, check `lsusb` |
| `Device or resource busy` | Another process using device | `sudo lsof /dev/snd/*` to find conflicting process |
| `Input/output error` on recording | Device error | Unplug and replug USB audio device |

### 6. Database

**Dependency:** None — can always check.

```bash
# SQLite integrity check
sqlite3 data/logger.db "PRAGMA integrity_check;" 2>&1
# Must return "ok"

# WAL file size (WARN >100MB, FAIL >500MB)
ls -lh data/logger.db-wal 2>/dev/null

# Recent write timestamp — check last insert
sqlite3 data/logger.db "SELECT MAX(ts) FROM headings;" 2>&1
# If significantly old, data pipeline is stalled

# Database file size
ls -lh data/logger.db
```

**Known failure patterns:**

| Log signature | Meaning | Fix |
|---|---|---|
| `database is locked` | Long-running transaction or WAL checkpoint stuck | Restart helmlog service |
| `disk I/O error` | SD card failing | **Critical:** back up data immediately, replace SD card |
| Integrity check ≠ "ok" | Corruption | Restore from backup; investigate SD card health |
| WAL > 500MB | Checkpointing not running | `sqlite3 data/logger.db "PRAGMA wal_checkpoint(TRUNCATE);"` |

### 7. Network

**Dependency:** None — can always check.

```bash
# Tailscale status
tailscale status

# Peer connectivity (if peers configured)
tailscale ping <peer-hostname> --timeout 5s 2>/dev/null

# nginx upstream health
curl -s -o /dev/null -w "%{http_code}" http://localhost/
# Should return 200

# DNS resolution
host github.com

# Internet connectivity
curl -s -o /dev/null -w "%{http_code}" --max-time 5 https://api.github.com
```

**Known failure patterns:**

| Log signature | Meaning | Fix |
|---|---|---|
| `Tailscale is stopped` | Tailscale not running | `sudo tailscale up` |
| `502 Bad Gateway` from nginx | Upstream service down | Restart helmlog: `sudo systemctl restart helmlog` |
| `connection timed out` | Network unreachable | Check ethernet/WiFi: `ip addr`, `nmcli` |
| `DERP` in tailscale ping | No direct connection, relayed | Check firewall/NAT; may be acceptable for remote debugging |

### 8. AI HAT (Future)

**Dependency:** HelmLog service must be active.

```bash
# Hailo device presence
ls /dev/hailo*

# Hailo runtime status
hailortcli fw-control identify 2>/dev/null

# Inference test (if available)
# This section will be expanded when the AI HAT is integrated
```

**Note:** This subsystem is not yet deployed. Skip checks gracefully if Hailo
device is not present — report `[OK] AI HAT — not installed (expected)`.

## Dependency Graph

```
System Health
  └── Services (skip if filesystem read-only)
        ├── CAN Bus (skip if helmlog service down)
        ├── Signal K (skip if signalk down)
        └── Audio (skip if helmlog service down)
Database (independent)
Network (independent)
AI HAT (skip if helmlog service down)
```

If a parent check fails, skip its children and report:
```
[SKIP] CAN Bus — skipped because helmlog service is not running
```

## Root Cause Heuristics

After running all checks, if failures exist, suggest the most likely root cause
based on the failure pattern:

| Failure pattern | Likely root cause |
|---|---|
| System FAIL + all services down | SD card failure or power issue |
| Services FAIL + everything else OK | Bad deploy or stale venv — run `uv sync` and restart |
| CAN FAIL + Signal K no deltas | CAN bus wiring or interface not up |
| Signal K FAIL + CAN OK | Signal K server issue — restart signalk |
| Audio FAIL only | USB device disconnected or conflict |
| Database FAIL only | SD card degradation or WAL bloat |
| Network FAIL only | Tailscale or connectivity issue |
| No failures but "no data" symptom | Instruments not powered on or CAN bus silent |
