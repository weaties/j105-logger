# Backup Guide

The logger stores all data on a microSD card inside the Raspberry Pi.
This guide covers how to pull a complete backup to your Mac automatically,
and how to restore from it if the card fails.

> **Identity:** Backups run as a dedicated `helmlog-backup` user on the Pi
> (provisioned by `scripts/setup-backup-user.sh`), authenticated with a
> dedicated key (`~/.ssh/helmlog-backup`) — not as the boat owner's
> account. This decouples the backup path from per-boat usernames and lets a
> narrow Tailscale ACL rule grant the daily cron headless SSH without
> tripping `check`-mode re-auth.
>
> **Multiple Pis?** Pass the `PI` env var
> (e.g. `PI=helmlog-backup@testpi ./scripts/backup.sh`). Examples below use
> the placeholder `<pi-host>`.

---

## What gets backed up

| Source | Destination in snapshot |
|---|---|
| `~/helmlog/data/logger.db` — consistent copy via `sqlite3 .backup` | `<snapshot>/data/logger.db` |
| `~/helmlog/data/audio/` — race + debrief WAV files | `<snapshot>/data/audio/` |
| `~/helmlog/data/notes/` — photo attachments (camera + phone) | `<snapshot>/data/notes/` |
| `~/helmlog/data/avatars/` — user profile photos | `<snapshot>/data/avatars/` |
| `~/helmlog/data/vakaros-inbox/` — pending race session uploads | `<snapshot>/data/vakaros-inbox/` |
| `~/helmlog/data/exports/` — CSV / GPX / JSON exports | `<snapshot>/data/exports/` |
| `~/helmlog/.env` | `<snapshot>/config/helmlog.env` |
| InfluxDB (system health metrics) | `<snapshot>/influxdb/` |
| Grafana (dashboards, datasources) | `<snapshot>/grafana/` |

Snapshots land in `~/backups/helmlog/<timestamp>/`.
The 10 most recent are kept; older ones are deleted automatically.

### Snapshot integrity (#676)

`backup.sh` runs `scripts/validate_snapshot.py` against every freshly
written snapshot. The validator cross-checks each `moment_attachments.path`,
`audio_sessions.file_path`, and `users.avatar_path` row against the files
actually present under `<snapshot>/data/` and reports orphans in the
backup-report email. `restore.sh` runs the same validator against the
snapshot before wiping the target, and again against the restored tree on
the Pi — so a restore that would land broken attachments is visible
immediately, not days later as broken image placeholders in the UI.

Run it manually against any snapshot:

```bash
python3 scripts/validate_snapshot.py ~/backups/helmlog/20260228T030000Z/data
```

Exit code `0` means every referenced file is present; `1` means one or more
orphans were found (sample paths are printed).

---

## First-time setup

### 1. Prerequisites on the Mac

```bash
# Dedicated SSH keypair for the backup user (separate from your interactive key)
ssh-keygen -t ed25519 -f ~/.ssh/helmlog-backup -N "" -C "helmlog-backup-mac"

# InfluxDB CLI (used during restore)
brew install influxdb-cli
```

### 2. Pi: provision the helmlog-backup user

From the project root, copy the setup script to the Pi and run it (sudo will
prompt for the boat owner's password):

```bash
scp scripts/setup-backup-user.sh <owner>@<pi-host>:/tmp/
ssh -t <owner>@<pi-host> 'bash /tmp/setup-backup-user.sh && rm /tmp/setup-backup-user.sh'
```

The script is idempotent. It:
- Creates the `helmlog-backup` system user, member of the boat owner's group
- Authorises `~/.ssh/helmlog-backup.pub` for SSH login
- Adds `/etc/sudoers.d/helmlog-backup` with `NOPASSWD: ALL` (needed for
  restore writes into helmlog-owned paths and service control)
- Loosens `~/helmlog/.env` and `~/influx-token.txt` to mode 640 so the new
  user can read them via group membership

If the boat owner is not `weaties`, set `OWNER_USER=<name>` before running.

### 3. Tailscale ACL: bypass check mode for the backup user

Add an `ssh` rule that **accepts** (rather than `check`s) connections from
this Mac to the Pis when the SSH login user is `helmlog-backup`. Without
this, the cron-driven backup at 03:00 will fail any time Tailscale's
periodic re-auth window expires (this is what caused the 2026-04-23 and
2026-04-24 missed backups).

Minimal addition to the tailnet policy file:

```hujson
"ssh": [
  // place this BEFORE the existing check-mode rule for autogroup:nonroot
  // so it matches first when the SSH login user is helmlog-backup
  {
    "action": "accept",
    "src":    ["autogroup:member"],
    "dst":    ["autogroup:self"],
    "users":  ["helmlog-backup"],
  },
  // existing check-mode rule stays for interactive humans
  // ...
],
```

`sessionDuration` is only valid on `check`-action rules — `accept` has no
expiry, which is exactly what the headless cron needs.

Apply via the admin UI at <https://login.tailscale.com/admin/acls/file>.

### 4. Install the daily launchd agent on the Mac

From the project root:

```bash
PI=helmlog-backup@<pi-host> REPORT_TO=you@example.com ./scripts/setup-backup-mac.sh
```

This:
- Checks SSH connectivity to the Pi using `~/.ssh/helmlog-backup`
- Creates `~/backups/helmlog/`
- Installs `~/Library/LaunchAgents/com.helmlog.backup.plist` with
  PI / SSH_KEY / REPORT_TO baked into `EnvironmentVariables`
- Schedules the backup to run every day at **03:00 local time**

### 5. Run a test backup now

```bash
PI=helmlog-backup@<pi-host> REPORT_TO=you@example.com ./scripts/backup.sh
```

Verify the snapshot contains:

```
~/backups/helmlog/20260228T030000Z/
  data/
    logger.db
    audio/
    notes/
    exports/
  influxdb/
  grafana/
```

---

## Monitoring backups

Logs are written to `~/backups/helmlog/backup.log`:

```bash
tail -f ~/backups/helmlog/backup.log
```

To check that the launchd agent is active:

```bash
launchctl list com.helmlog.backup
```

To trigger an immediate run:

```bash
launchctl start com.helmlog.backup
```

---

## Restoring from a backup

`scripts/restore.sh` does the full sequence — stop services, restore SQLite
+ file data, .env, Signal K, Grafana, InfluxDB (`--full`), wipe boat
identity, restart services. Run it as the dedicated backup user; sudo on the
Pi handles the privileged writes.

```bash
PI=helmlog-backup@<pi-host> ./scripts/restore.sh                                # most recent snapshot
PI=helmlog-backup@<pi-host> ./scripts/restore.sh ~/backups/helmlog/20260228T030000Z
```

After restore, run `helmlog identity init …` on the target so it federates
as itself rather than impersonating the source. Set `KEEP_IDENTITY=1` only
when the source Pi is offline during testing.

---

## Configuration

All settings are overridable via environment variables:

| Variable | Default | Description |
|---|---|---|
| `PI` | _required_ | SSH target, e.g. `helmlog-backup@<pi-host>` |
| `SSH_KEY` | `~/.ssh/helmlog-backup` | Local SSH private key for the backup user |
| `REPORT_TO` | _required_ | Email address for the daily report (or set `SKIP_EMAIL=1`) |
| `BACKUP_DEST` | `~/backups/helmlog` | Local snapshot root |
| `KEEP_SNAPSHOTS` | `10` | Number of snapshots to retain |
| `PI_HELMLOG_DIR` | `/home/weaties/helmlog` | Pi-side helmlog directory |
| `PI_SIGNALK_DIR` | `/home/weaties/.signalk` | Pi-side Signal K directory |
| `PI_OWNER_USER` (restore) | `weaties` | Pi-side file owner used for `--chown` during restore |
| `INFLUX_TOKEN_FILE` | `/home/weaties/influx-token.txt` | Pi-side InfluxDB token path |

Example — keep 30 days of snapshots:

```bash
KEEP_SNAPSHOTS=30 ./scripts/backup.sh
```

---

## Changing the backup schedule

Edit `~/Library/LaunchAgents/com.helmlog.backup.plist` and change the
`StartCalendarInterval` hour/minute, then reload:

```bash
launchctl unload ~/Library/LaunchAgents/com.helmlog.backup.plist
launchctl load   ~/Library/LaunchAgents/com.helmlog.backup.plist
```

---

## Disabling the backup

```bash
launchctl unload ~/Library/LaunchAgents/com.helmlog.backup.plist
rm ~/Library/LaunchAgents/com.helmlog.backup.plist
```
