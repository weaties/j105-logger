#!/usr/bin/env bash
# restore.sh — Restore a helmlog snapshot onto a target Pi.
#
# Usage:
#   PI=user@pi-hostname ./scripts/restore.sh [SNAPSHOT_DIR]
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

PI="${PI:?set PI to the target SSH host, e.g. user@pi-hostname}"
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
# user (with the SSH login user in the group via setgid), so rsync can write
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
  log "  grafana data dir done"
else
  log "  WARNING: $SNAP/grafana not present; skipping data dir"
fi

# /etc/grafana/provisioning holds the InfluxDB datasource yaml (with the
# token). Without restoring this, Grafana keeps the target's old token —
# which is invalidated by the InfluxDB --full restore in step 6 — and
# dashboards show "No data".
if [ -d "$SNAP/grafana-provisioning" ]; then
  # shellcheck disable=SC2086
  rsync -az --delete $RSYNC_PROGRESS \
    "$SNAP/grafana-provisioning/" \
    "$PI:/tmp/grafana-provisioning-restore/"
  ssh "$PI" "sudo rsync -a --delete --chown=root:grafana \
    /tmp/grafana-provisioning-restore/ /etc/grafana/provisioning/ && \
    rm -rf /tmp/grafana-provisioning-restore"
  log "  grafana provisioning done"
else
  log "  WARNING: $SNAP/grafana-provisioning not present; skipping (dashboards may show 'No data' until you re-run backup.sh against the source)"
fi

# ── 6. InfluxDB restore (full) ────────────────────────────────────────────────
log "Step 6/7: Restoring InfluxDB (--full)"
if [ -d "$SNAP/influxdb" ]; then
  # Token chicken-and-egg: `influx restore --full` replaces all auth on the
  # target, so after the first restore the target's $INFLUX_TOKEN_FILE is
  # stale (it has the original target token, but the InfluxDB instance now
  # holds the source's tokens). Try the target's local token first; if that
  # 401s, fall back to the snapshot's captured source token.
  ssh "$PI" "rm -rf /tmp/influx-restore && mkdir -p /tmp/influx-restore"
  rsync -az "$SNAP/influxdb/" "$PI:/tmp/influx-restore/"

  # Stage candidate tokens on the target (avoid embedding secrets in ssh args).
  ssh "$PI" "rm -f /tmp/influx-token-target /tmp/influx-token-snap"
  ssh "$PI" "test -f $INFLUX_TOKEN_FILE && cp $INFLUX_TOKEN_FILE /tmp/influx-token-target" \
    2>/dev/null || true
  if [ -f "$SNAP/config/influx-token.txt" ]; then
    rsync -az "$SNAP/config/influx-token.txt" "$PI:/tmp/influx-token-snap"
  fi

  RESTORE_OK=0
  WORKING_TOKEN_FILE=""
  for candidate in /tmp/influx-token-target /tmp/influx-token-snap; do
    if ssh "$PI" "test -s $candidate" 2>/dev/null; then
      if ssh "$PI" "influx restore /tmp/influx-restore \
          --host http://localhost:8086 \
          --token \$(cat $candidate) \
          --full"; then
        RESTORE_OK=1
        WORKING_TOKEN_FILE="$candidate"
        break
      fi
      log "  token at $candidate did not work; trying next"
    fi
  done

  if [ "$RESTORE_OK" = "1" ]; then
    log "  InfluxDB restore done (used $WORKING_TOKEN_FILE)"
    # If we used the snapshot token, write it back to the canonical location
    # so subsequent restores authenticate without falling back.
    if [ "$WORKING_TOKEN_FILE" = "/tmp/influx-token-snap" ]; then
      ssh "$PI" "cp /tmp/influx-token-snap $INFLUX_TOKEN_FILE && chmod 600 $INFLUX_TOKEN_FILE"
      log "  Updated $INFLUX_TOKEN_FILE on $PI to match restored auth"
    fi
  else
    log "  WARNING: InfluxDB restore failed (no working token)"
  fi
  ssh "$PI" "rm -rf /tmp/influx-restore /tmp/influx-token-target /tmp/influx-token-snap"
else
  log "  WARNING: $SNAP/influxdb missing; skipping"
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
