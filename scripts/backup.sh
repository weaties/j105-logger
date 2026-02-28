#!/usr/bin/env bash
# backup.sh — Pull all persistent data from corvopi to this Mac.
#
# Run manually:  ./scripts/backup.sh
# Or let setup-backup-mac.sh schedule it daily via launchd.
#
# Creates a timestamped snapshot under $BACKUP_DEST (default ~/backups/j105-logger/).
# Keeps the most recent $KEEP_SNAPSHOTS snapshots; older ones are deleted.
#
# Prerequisites on the Mac:
#   - SSH access to corvopi (via Tailscale; key-based auth recommended)
#   - influx CLI for InfluxDB restore: brew install influxdb-cli
#   - sudo rsync allowed on the Pi for the SSH user (for Grafana dir)
#
# Environment overrides:
#   PI                 SSH target           (default: weaties@corvopi)
#   BACKUP_DEST        local snapshot root  (default: ~/backups/j105-logger)
#   KEEP_SNAPSHOTS     how many to retain   (default: 10)
#   INFLUX_TOKEN_FILE  path on the Pi       (default: ~/influx-token.txt)

set -euo pipefail

PI="${PI:-weaties@corvopi}"
BACKUP_DEST="${BACKUP_DEST:-$HOME/backups/j105-logger}"
KEEP_SNAPSHOTS="${KEEP_SNAPSHOTS:-10}"
INFLUX_TOKEN_FILE="${INFLUX_TOKEN_FILE:-~/influx-token.txt}"

DATE=$(date -u +%Y%m%dT%H%M%SZ)
SNAP="$BACKUP_DEST/$DATE"

log() { echo "[$(date -u +%H:%M:%SZ)] $*"; }

# Find the most recent previous snapshot to use as --link-dest base.
# rsync --link-dest creates hardlinks for files that are byte-for-byte identical
# to the previous snapshot, so unchanged files (photos, WAVs, etc.) cost zero
# extra disk space across snapshots.
PREV=$(ls -1dt "$BACKUP_DEST"/20* 2>/dev/null | head -1)

link_dest_args() {
  # Usage: link_dest_args <subdir>
  # Emits --link-dest=<prev>/<subdir> if a previous snapshot exists.
  local subdir="$1"
  if [ -n "$PREV" ] && [ -d "$PREV/$subdir" ]; then
    echo "--link-dest=$PREV/$subdir"
  fi
}

mkdir -p "$SNAP"
log "Starting backup → $SNAP"
log "Source: $PI"
[ -n "$PREV" ] && log "Link-dest (hardlink base): $PREV"

# ── 1. SQLite — WAL checkpoint then rsync ────────────────────────────────────
log "Step 1/4: SQLite WAL checkpoint + rsync"
ssh "$PI" "sqlite3 ~/j105-logger/data/logger.db 'PRAGMA wal_checkpoint(TRUNCATE);'" 2>/dev/null || \
  log "  WARNING: WAL checkpoint failed (DB may not exist yet); continuing"

# shellcheck disable=SC2046
rsync -az --info=progress2 $(link_dest_args data) \
  "$PI:~/j105-logger/data/" \
  "$SNAP/data/"
log "  SQLite + file data done"

# ── 2. InfluxDB — remote backup then rsync ───────────────────────────────────
log "Step 2/4: InfluxDB backup"
if ssh "$PI" "test -f $INFLUX_TOKEN_FILE" 2>/dev/null; then
  ssh "$PI" "influx backup /tmp/influx-backup \
    --host http://localhost:8086 \
    --token \$(cat $INFLUX_TOKEN_FILE)" 2>/dev/null && \
  # shellcheck disable=SC2046
  rsync -az --info=progress2 $(link_dest_args influxdb) \
    "$PI:/tmp/influx-backup/" \
    "$SNAP/influxdb/" && \
  ssh "$PI" "rm -rf /tmp/influx-backup" && \
  log "  InfluxDB backup done" || \
  log "  WARNING: InfluxDB backup failed; skipping (data dir intact on Pi)"
else
  log "  WARNING: $INFLUX_TOKEN_FILE not found on Pi; skipping InfluxDB backup"
fi

# ── 3. Grafana — rsync with sudo ─────────────────────────────────────────────
log "Step 3/4: Grafana data dir"
# shellcheck disable=SC2046
if rsync -az --info=progress2 \
    --rsync-path='sudo rsync' \
    $(link_dest_args grafana) \
    "$PI:/var/lib/grafana/" \
    "$SNAP/grafana/" 2>/dev/null; then
  log "  Grafana backup done"
else
  log "  WARNING: Grafana rsync failed (sudo rsync not configured?); skipping"
fi

# ── 4. Rotate old snapshots ───────────────────────────────────────────────────
log "Step 4/4: Rotating snapshots (keeping $KEEP_SNAPSHOTS)"
# shellcheck disable=SC2012
STALE=$(ls -1dt "$BACKUP_DEST"/20* 2>/dev/null | tail -n +"$((KEEP_SNAPSHOTS + 1))")
if [ -n "$STALE" ]; then
  echo "$STALE" | xargs rm -rf
  log "  Removed $(echo "$STALE" | wc -l | tr -d ' ') old snapshot(s)"
else
  log "  Nothing to rotate"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
SNAP_SIZE=$(du -sh "$SNAP" 2>/dev/null | cut -f1)
log "Backup complete — $SNAP  ($SNAP_SIZE)"
echo ""
echo "Snapshots in $BACKUP_DEST:"
ls -1t "$BACKUP_DEST" | head -"$KEEP_SNAPSHOTS" | sed 's/^/  /'
