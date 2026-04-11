#!/usr/bin/env bash
# restore.sh — Restore a helmlog snapshot onto a target Pi.
#
# Usage:
#   PI=weaties@corvopi-tst1 ./scripts/restore.sh [SNAPSHOT_DIR]
#
# If SNAPSHOT_DIR is omitted, the most recent snapshot in $BACKUP_DEST is used.
#
# This is destructive on the target. It will:
#   - Stop helmlog, signalk, grafana-server
#   - Overwrite ~/helmlog/data/, ~/helmlog/.env, ~/.signalk/, /var/lib/grafana/
#   - Run `influx restore --full` (wipes and replaces ALL InfluxDB data)
#   - Wipe the boat identity (~/.helmlog/identity/ + boat_identity table)
#   - Restart services
#
# Identity wipe: by default the target's boat identity is removed so it does
# not impersonate the source for federation/peer auth. After restore, run
# `helmlog identity init ...` on the target. Set KEEP_IDENTITY=1 to skip the
# wipe (only safe if the source Pi will be offline during testing).
#
# Environment:
#   PI                 SSH target              (required)
#   BACKUP_DEST        local snapshot root     (default: ~/backups/helmlog)
#   KEEP_IDENTITY      skip identity wipe      (default: 0)
#   INFLUX_TOKEN_FILE  path on target          (default: ~/influx-token.txt)
#   FORCE              skip confirmation prompt (default: 0)

set -euo pipefail

PI="${PI:?set PI to the target SSH host, e.g. weaties@corvopi-tst1}"
BACKUP_DEST="${BACKUP_DEST:-$HOME/backups/helmlog}"
KEEP_IDENTITY="${KEEP_IDENTITY:-0}"
INFLUX_TOKEN_FILE="${INFLUX_TOKEN_FILE:-~/influx-token.txt}"
FORCE="${FORCE:-0}"

SNAP="${1:-}"
if [ -z "$SNAP" ]; then
  SNAP=$(ls -1dt "$BACKUP_DEST"/20* 2>/dev/null | head -1 || true)
  [ -n "$SNAP" ] || { echo "No snapshots found in $BACKUP_DEST" >&2; exit 1; }
fi
[ -d "$SNAP" ] || { echo "Snapshot directory not found: $SNAP" >&2; exit 1; }

log() { echo "[$(date -u +%H:%M:%SZ)] $*"; }

# rsync 3.1+ has --info=progress2; macOS ships with 2.6.9.
if rsync --info=progress2 --version >/dev/null 2>&1; then
  RSYNC_PROGRESS="--info=progress2"
else
  RSYNC_PROGRESS="--progress"
fi

log "Target:   $PI"
log "Snapshot: $SNAP"
if [ "$KEEP_IDENTITY" = "1" ]; then
  log "Identity wipe: SKIPPED (KEEP_IDENTITY=1)"
else
  log "Identity wipe: ENABLED"
fi

if [ "$FORCE" != "1" ]; then
  echo
  printf 'This will WIPE existing helmlog data on %s. Continue? [y/N] ' "$PI"
  read -r ans
  case "$ans" in
    y|Y|yes|YES) ;;
    *) echo "Aborted."; exit 1 ;;
  esac
fi

# ── 1. Stop services ──────────────────────────────────────────────────────────
log "Step 1/7: Stopping services on $PI"
ssh "$PI" "sudo systemctl stop helmlog signalk grafana-server" || \
  log "  WARNING: one or more services failed to stop"

# ── 2. SQLite + file data ─────────────────────────────────────────────────────
log "Step 2/7: Restoring SQLite + file data → ~/helmlog/data/"
# --omit-dir-times: ~/helmlog/data/ subdirs are owned by the `helmlog` service
# user (with `weaties` in the group via setgid), so rsync as `weaties` can write
# files inside them but cannot update the directory mtimes.
# shellcheck disable=SC2086
rsync -az --delete --omit-dir-times $RSYNC_PROGRESS \
  "$SNAP/data/" \
  "$PI:helmlog/data/"
log "  data/ done"

# ── 3. .env config ────────────────────────────────────────────────────────────
log "Step 3/7: Restoring helmlog .env"
if [ -f "$SNAP/config/helmlog.env" ]; then
  rsync -az "$SNAP/config/helmlog.env" "$PI:helmlog/.env"
  ssh "$PI" "chmod 600 ~/helmlog/.env"
  log "  .env done"
else
  log "  WARNING: $SNAP/config/helmlog.env not present; skipping"
fi

# ── 4. Signal K config + data ─────────────────────────────────────────────────
log "Step 4/7: Restoring Signal K → ~/.signalk/"
if [ -d "$SNAP/signalk" ]; then
  # No --delete: preserve target's node_modules and package-lock.json
  # (they were excluded from the snapshot intentionally).
  # shellcheck disable=SC2086
  rsync -az $RSYNC_PROGRESS \
    "$SNAP/signalk/" \
    "$PI:.signalk/"
  log "  signalk/ done (target node_modules preserved)"
else
  log "  WARNING: $SNAP/signalk not present; skipping"
fi

# ── 5. Grafana ────────────────────────────────────────────────────────────────
log "Step 5/7: Restoring Grafana → /var/lib/grafana/"
if [ -d "$SNAP/grafana" ]; then
  # Two-step: push to a staging dir owned by the SSH user, then `sudo rsync`
  # locally on the Pi to copy into /var/lib/grafana with the right ownership.
  # macOS ships Apple's openrsync, which mishandles --chown via --rsync-path,
  # so the chown must happen on the Pi (GNU rsync 3.x).
  # shellcheck disable=SC2086
  rsync -az --delete $RSYNC_PROGRESS \
    "$SNAP/grafana/" \
    "$PI:/tmp/grafana-restore/"
  ssh "$PI" "sudo rsync -a --delete --chown=grafana:grafana \
    /tmp/grafana-restore/ /var/lib/grafana/ && rm -rf /tmp/grafana-restore"
  log "  grafana done"
else
  log "  WARNING: $SNAP/grafana not present; skipping"
fi

# ── 6. InfluxDB restore (full) ────────────────────────────────────────────────
log "Step 6/7: Restoring InfluxDB (--full)"
if [ -d "$SNAP/influxdb" ] && ssh "$PI" "test -f $INFLUX_TOKEN_FILE" 2>/dev/null; then
  ssh "$PI" "rm -rf /tmp/influx-restore && mkdir -p /tmp/influx-restore"
  rsync -az "$SNAP/influxdb/" "$PI:/tmp/influx-restore/"
  if ssh "$PI" "influx restore /tmp/influx-restore \
      --host http://localhost:8086 \
      --token \$(cat $INFLUX_TOKEN_FILE) \
      --full"; then
    log "  InfluxDB restore done"
    log "  NOTE: $INFLUX_TOKEN_FILE on $PI may now be stale (auth was replaced)"
  else
    log "  WARNING: InfluxDB restore failed"
  fi
  ssh "$PI" "rm -rf /tmp/influx-restore"
else
  log "  WARNING: $SNAP/influxdb missing or $INFLUX_TOKEN_FILE not on target; skipping"
fi

# ── 7. Identity wipe + service restart ────────────────────────────────────────
log "Step 7/7: Identity wipe + restart services"
if [ "$KEEP_IDENTITY" != "1" ]; then
  ssh "$PI" "rm -rf ~/.helmlog/identity/ && \
    sqlite3 ~/helmlog/data/logger.db 'DELETE FROM boat_identity;' 2>/dev/null || true"
  log "  Identity wiped on $PI"
fi
ssh "$PI" "sudo systemctl start signalk grafana-server helmlog" || \
  log "  WARNING: one or more services failed to start"
log "  Services restarted"

echo
log "Restore complete: $SNAP → $PI"

if [ "$KEEP_IDENTITY" != "1" ]; then
  cat <<EOF

NEXT STEPS on $PI:
  ssh $PI
  helmlog identity init --boat-name <name> --sail-number <num> --email <email>
  sudo systemctl restart helmlog

EOF
fi
