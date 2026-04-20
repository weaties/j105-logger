# Bootstrap HelmLog on a new Pi

This guide takes a sophisticated user (shell + git fluent) from "I have a Pi, a
CAN HAT, and a boat" to "I'm logging my own sailing data." It complements the
mechanical step-through in [README → Fresh SD card setup](../README.md#fresh-sd-card-setup)
by focusing on the decisions, prerequisites, and verification a new owner needs
that the README doesn't call out.

> **If you just want the step list**, go straight to the README's Fresh SD card
> setup. This doc is the "what am I getting into, what do I need, did it work"
> wrapper around those steps.

---

## 1. What you're building

A single Raspberry Pi that:

- Listens to your B&G (or other NMEA 2000) instrument system via a CAN HAT
- Runs **Signal K Server** (Node) as the decoder and publisher
- Runs **HelmLog** (Python/FastAPI) as the logger + web UI
- Stores everything in **SQLite** on the Pi
- Optionally uploads time-series metrics to an on-device **InfluxDB + Grafana**
  for dashboards
- Optionally joins a **co-op** of other boats (federation) for shared tracks

Out of the box, every optional feature (cameras, audio/transcription, YouTube
pipeline, co-op federation, public HTTPS via Cloudflare Tunnel) is **off**.
The minimum-viable install is: Pi → Signal K → HelmLog → local web UI.

---

## 2. Prerequisites checklist

### Hardware

- [ ] Raspberry Pi 4 or 5 (4 GB+ recommended; 8 GB if you plan to run
      transcription on-device)
- [ ] SD card or SSD (32 GB minimum; SSD strongly recommended for wear)
- [ ] **MCP2515-based CAN HAT** with a **16 MHz crystal** wired to
      **GPIO 25 interrupt**. The setup scripts assume exactly this hardware —
      if your HAT uses a different crystal or interrupt pin, you'll need to
      edit the `dtoverlay` line in step 5 of the README accordingly.
- [ ] NMEA 2000 drop cable from the HAT into your boat's backbone (with
      terminators already present on the backbone — don't add new ones)
- [ ] Way to power the Pi on the boat (12 V → 5 V DC-DC is typical; USB-C
      from a house battery works for bench testing)

### Accounts (only the ones you want to use)

All of these are **optional**. None are required to log data.

| Feature | What you need |
|---|---|
| Public HTTPS access (from anywhere) | Tailscale account, or Cloudflare account with a domain |
| Remote SSH without port-forwarding | Tailscale account |
| Email notifications (invites, new-device alerts) | SMTP credentials — Gmail app password is easiest |
| Google / Apple / GitHub sign-in for crew | OAuth app in the respective console |
| YouTube video linking + uploads | Google Cloud project with YouTube Data API v3 enabled |
| Speaker diarization in audio transcripts | Hugging Face account + accepted pyannote model licenses |
| Co-op (share tracks with other boats) | Nothing external — boats exchange keys directly |

### Network

- [ ] Home Wi-Fi credentials for bench setup
- [ ] Boat Wi-Fi (phone hotspot, boat router, or marina AP) for deployment —
      you can add these later via the web UI

### Decide before you start

1. **Fork or upstream?** The setup scripts clone from
   `https://github.com/weaties/helmlog.git` (the upstream project owner's
   fork). For your own boat you probably want to **fork the repo to your own
   GitHub account** and clone that instead — then you can commit your boat's
   local tweaks (polar files, boat-settings snapshots, custom triggers)
   without having to merge them into the shared codebase. The clone URL in
   README §6 is the only place to substitute.

2. **Hostname.** You'll use this name constantly (`ssh user@hostname`,
   `http://hostname/` in a browser). Pick something short and boat-specific
   that won't collide with the other Pis you own. Examples: `oryxpi`,
   `wavedancer-pi`, `s42-pi`.

3. **Pi username.** The service account (`helmlog`) is created for you. The
   login user is whatever you set in Pi Imager — `pi`, your name, or a
   boat-mascot handle are all fine. The scripts don't assume any particular
   username.

4. **Race data start time.** If you want weather/tides/history from before
   the Pi existed, you can't — SQLite is only populated from the moment the
   logger starts. Plan your first test sails accordingly.

---

## 3. Install

Follow [README → Fresh SD card setup](../README.md#fresh-sd-card-setup) steps
1 through 10.

A few clarifications for a first-time owner:

- **Step 4 (Tailscale)** is optional. Skip it if you only plan to access the
  Pi on your boat's local network. You can always add it later.
- **Step 5 (CAN HAT dtoverlay)** is where a mismatch between your HAT and the
  default settings will bite you. After rebooting, `ip link show can0` must
  show the interface. If it doesn't, your HAT likely uses a different
  crystal (8 MHz is also common) or interrupt pin — check the HAT's datasheet
  and adjust `oscillator=` and `interrupt=` in `/boot/firmware/config.txt`.
- **Step 7 (`./scripts/setup.sh`)** is idempotent and verbose. Read the
  output — it tells you what it's installing, what's already set up, and
  where it writes secrets (`~/influx-token.txt`, `~/.signalk-admin-pass.txt`).
  Both files are `chmod 600`. Back them up off the Pi.
- **Step 9 (admin user)** uses the CLI (`helmlog add-user`). Use the email
  address you actually check — that's how you'll log in later.

After a successful reboot (step 10), `sudo systemctl status can-interface
signalk influxd grafana-server helmlog` should show all five **active
(running)**. If any are failing, jump to §6 Troubleshooting below.

---

## 4. Make it yours (post-install)

### Claim a boat identity (optional, but do this before joining any co-op)

The federation system uses an Ed25519 keypair to identify your boat. Generate
it once:

```bash
helmlog identity init --sail-number <your-sail-number> --boat-name "<Your Boat>"
helmlog identity show    # prints fingerprint to share with co-op members
```

The keys land in `~/.helmlog/identity/`. **Back these up off the Pi** — losing
them means losing your co-op membership, and there's no recovery path.

### Set your timezone

The logger stores everything in UTC (by design — don't change this), but
display defaults to UTC too. Override via `.env`:

```bash
# in /home/<user>/helmlog/.env
TIMEZONE=America/Los_Angeles
```

Then `sudo systemctl restart helmlog`.

### Configure the boat (web UI)

- Go to `http://<pi-hostname>/admin/settings` — fill in sail number, boat
  length, displacement, and any polar data you have. These feed the race
  classifier and performance analytics.
- Go to `http://<pi-hostname>/admin/settings` (boat profile section) to pick
  your primary race venue(s) — this scopes weather/tide lookups.

### Turn on the features you want

Edit `/home/<user>/helmlog/.env` and restart the service after each change:

```bash
sudo systemctl restart helmlog
```

A minimum to get email invites working (Gmail example — requires 2FA + an
app password at <https://myaccount.google.com/apppasswords>):

```env
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASSWORD=<16-char-app-password>
SMTP_FROM=you@gmail.com
```

The full set of optional toggles is in `.env.example` — read it top-to-bottom
once; it's the canonical reference.

### Co-op (optional)

Only relevant if you're sharing with other HelmLog boats. Two flows:

- **You're creating a new co-op**: `helmlog co-op create --name "Fleet Name"`
  → you're the moderator.
- **You're joining an existing co-op**: send your `~/.helmlog/identity/boat.json`
  to the moderator; they run `helmlog co-op invite ./your-boat.json` and
  send you back an invite bundle.

See [`docs/guide-federation.md`](guide-federation.md) for the full lifecycle.

---

## 5. Verify it's working

### Data is flowing

```bash
# Signal K is seeing the bus
curl -s http://localhost:3000/signalk/v1/api/vessels/self | jq 'keys'
# Should include 'navigation', 'environment', 'performance', etc.

# HelmLog is logging
helmlog status
# Row counts should be non-zero and growing
```

Drive a minute-long test session from the web UI (Home page → "Start Race"
or "Start Practice"). After stopping:

```bash
helmlog status    # session count should have incremented
```

Open `http://<pi-hostname>/history/` — your session should be there, with a
track on the map.

### Exports work

From the session detail page, download CSV and GPX. Load the GPX into any
chartplotter or Sailmon to confirm the track looks right. The CSV should
include `twd`, `tws`, `twa` columns — if they're all empty, the Signal K
derived-data plugin didn't install correctly; re-run `./scripts/setup.sh`.

### System health is green

```bash
# Web UI → /admin/system — all green checks
# Or CLI:
sudo systemctl status helmlog signalk can-interface influxd grafana-server
```

---

## 6. Troubleshooting first boot

| Symptom | Likely cause | Fix |
|---|---|---|
| `ip link show can0` says "does not exist" | `dtoverlay` not applied or wrong HAT settings | Edit `/boot/firmware/config.txt`, verify `dtparam=spi=on` and the `dtoverlay=mcp2515-can0,...` line; reboot |
| `can0` is up but `cansniffer can0` is silent | HAT not wired to the N2K backbone, or backbone not powered | Check cabling; N2K bus needs 12 V and terminators at both ends |
| `systemctl status signalk` fails to start | Node not installed or port 3000 in use | `node --version` should report v24+; `sudo ss -tlnp | grep :3000` to find the squatter |
| `systemctl status helmlog` fails with `ModuleNotFoundError` | `uv sync` didn't run or ran as the wrong user | `cd ~/helmlog && uv sync`, then `sudo systemctl restart helmlog` |
| `helmlog status` row counts stay at zero | SK is running but not decoding — derived-data plugin missing, or CAN interface is up but not receiving | Signal K admin UI at `http://<pi-hostname>:3000` → Plugins; Data Browser should show live PGNs |
| Web UI unreachable at `http://<pi-hostname>/` | nginx didn't start or hostname not resolving | `sudo systemctl status nginx`; try the IP directly; check router DHCP table |
| Login email never arrives | SMTP not configured (that's fine) or wrong app password | Check `journalctl -u helmlog -e` for SMTP errors; magic-link URL is also printed to the journal if SMTP fails |

When in doubt: `journalctl -u helmlog -n 200 --no-pager` is the single best
log to read. `loguru` writes structured warnings for every decode failure,
storage write, and HTTP handler error.

---

## 7. Staying current

```bash
cd ~/helmlog
git pull
./scripts/deploy.sh     # pulls, syncs uv, restarts helmlog
```

`deploy.sh` is idempotent and safe to re-run. If you've forked the repo,
pull from your fork and periodically rebase on upstream:

```bash
git remote add upstream https://github.com/weaties/helmlog.git
git fetch upstream
git rebase upstream/main
```

If `DEPLOY_MODE=evergreen` is set in `.env`, the Pi will auto-pull and
restart on a schedule — leave it off until you're comfortable with the
update cadence.

---

## 8. What to back up

The Pi is cattle, not pets — but these files are not reproducible:

| Path | What it is |
|---|---|
| `~/.helmlog/identity/` | Your boat's Ed25519 keypair and boat card |
| `~/helmlog/data/logger.db` | The SQLite database — all your races, tracks, notes |
| `~/helmlog/data/audio/` | Raw WAV recordings (large; optional) |
| `~/helmlog/.env` | Your configuration (secrets, tokens) |
| `~/influx-token.txt` | InfluxDB admin token |
| `~/.signalk-admin-pass.txt` | Signal K admin password |

`scripts/backup.sh` (run from a Mac or another machine with SSH access to
the Pi) pulls all of these to a local archive. See
[`docs/backup.md`](backup.md).

---

## 9. Next reading

- [`docs/operators-guide.md`](operators-guide.md) — race-day workflow on the
  boat
- [`docs/guide-federation.md`](guide-federation.md) — co-op setup end-to-end
- [`docs/https-deployment.md`](https-deployment.md) — public HTTPS via
  Tailscale Funnel or Cloudflare
- [`docs/data-licensing.md`](data-licensing.md) — what data is yours, what
  you share when you join a co-op, and what the code guarantees on your
  behalf
- [`docs/audio-setup.md`](audio-setup.md), [`docs/camera-setup.md`](camera-setup.md)
  — optional capture devices
