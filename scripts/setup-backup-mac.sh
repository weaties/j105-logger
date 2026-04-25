#!/usr/bin/env bash
# setup-backup-mac.sh — Install the daily Pi-to-Mac backup launchd agent.
#
# Run once on your Mac from the project root:
#   PI=helmlog-backup@pi-hostname REPORT_TO=you@example.com ./scripts/setup-backup-mac.sh
#
# What it does:
#   1. Verifies SSH connectivity to $PI as helmlog-backup
#   2. Creates ~/backups/helmlog/
#   3. Copies com.helmlog.backup.plist → ~/Library/LaunchAgents/ (with real values)
#   4. Loads (enables) the agent — runs daily at 03:00
#
# Prerequisites (one-time per Pi):
#   1. ./scripts/setup-backup-user.sh has been run on the Pi (creates the
#      `helmlog-backup` user and authorises this Mac's helmlog-backup pubkey).
#   2. The dedicated SSH keypair exists at ~/.ssh/helmlog-backup{,.pub}
#      (generate with: ssh-keygen -t ed25519 -f ~/.ssh/helmlog-backup -N "")
#
# To uninstall:
#   launchctl unload ~/Library/LaunchAgents/com.helmlog.backup.plist
#   rm ~/Library/LaunchAgents/com.helmlog.backup.plist

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_SCRIPT="$SCRIPT_DIR/backup.sh"
PLIST_SRC="$SCRIPT_DIR/com.helmlog.backup.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/com.helmlog.backup.plist"
BACKUP_DEST="${BACKUP_DEST:-$HOME/backups/helmlog}"
LOG_FILE="$BACKUP_DEST/backup.log"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/helmlog-backup}"
: "${PI:?PI must be set — e.g. export PI=helmlog-backup@pi-hostname}"
: "${REPORT_TO:?REPORT_TO must be set — e.g. export REPORT_TO=you@example.com}"

log() { echo "==> $*"; }
warn() { echo "    WARNING: $*" >&2; }

# ── 1. Pre-flight checks ──────────────────────────────────────────────────────
log "Checking dependencies..."

if ! command -v rsync &>/dev/null; then
  echo "ERROR: rsync not found. Install via Xcode CLT: xcode-select --install" >&2
  exit 1
fi

if ! command -v influx &>/dev/null; then
  warn "influx CLI not found — InfluxDB backup will be skipped."
  warn "Install with: brew install influxdb-cli"
fi

if [ ! -f "$SSH_KEY" ]; then
  echo "ERROR: SSH key not found at $SSH_KEY" >&2
  echo "Generate with: ssh-keygen -t ed25519 -f $SSH_KEY -N \"\"" >&2
  exit 1
fi

log "Testing SSH connectivity to $PI (key: $SSH_KEY)..."
if ! ssh -i "$SSH_KEY" -o ConnectTimeout=10 -o BatchMode=yes -o IdentitiesOnly=yes \
    -o StrictHostKeyChecking=accept-new "$PI" "echo ok" &>/dev/null; then
  echo "" >&2
  echo "ERROR: Cannot SSH to $PI as $(echo "$PI" | cut -d@ -f1)." >&2
  echo "Make sure:" >&2
  echo "  1. setup-backup-user.sh has been run on the Pi" >&2
  echo "  2. Tailscale ACL allows ssh user '$(echo "$PI" | cut -d@ -f1)' with action: accept" >&2
  echo "  3. You can reach the Pi via Tailscale ('tailscale status')" >&2
  exit 1
fi
log "  SSH OK"

# ── 2. Create backup destination ─────────────────────────────────────────────
mkdir -p "$BACKUP_DEST"
log "Backup destination: $BACKUP_DEST"

# ── 3. Install plist ──────────────────────────────────────────────────────────
log "Installing launchd agent..."

# Substitute real values into the plist template.
sed \
  -e "s|BACKUP_SCRIPT_PATH|$BACKUP_SCRIPT|g" \
  -e "s|BACKUP_LOG_PATH|$LOG_FILE|g" \
  -e "s|HOME_PATH|$HOME|g" \
  -e "s|PI_TARGET|$PI|g" \
  -e "s|SSH_KEY_PATH|$SSH_KEY|g" \
  -e "s|REPORT_TO_ADDR|$REPORT_TO|g" \
  "$PLIST_SRC" > "$PLIST_DEST"

log "  Plist written → $PLIST_DEST"

# ── 4. Load the agent ────────────────────────────────────────────────────────
# Unload first in case it was previously installed with a different path
launchctl unload "$PLIST_DEST" 2>/dev/null || true
launchctl load "$PLIST_DEST"
log "  Agent loaded (runs daily at 03:00)"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "Setup complete."
echo ""
echo "  Schedule : daily at 03:00 local time"
echo "  Log      : $LOG_FILE"
echo "  Snapshots: $BACKUP_DEST/<timestamp>/"
echo "  Retention: 10 snapshots (override with KEEP_SNAPSHOTS=N ./scripts/backup.sh)"
echo ""
echo "Run a backup now:"
echo "  $BACKUP_SCRIPT"
echo ""
echo "Or trigger via launchd:"
echo "  launchctl start com.helmlog.backup"
