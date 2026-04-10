#!/usr/bin/env bash
# watch-exports.sh — Watch ~/Insta360 Exports/ for newly stitched MP4s and
# auto-upload + link each one to its matching HelmLog session.
#
# Designed to run as a background launchd agent (see
# launchd/com.helmlog.video-watch.plist) but safe to run by hand for testing.
#
# Usage:
#   ./scripts/watch-exports.sh                # watch default dir
#   ./scripts/watch-exports.sh /custom/path   # watch a different dir
#
# Companion to upload-stitched.sh — this script just spots new files and
# delegates the actual upload to upload-stitched.sh per file.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WATCH_DIR="${1:-$HOME/Insta360 Exports}"
UPLOAD_SCRIPT="$SCRIPT_DIR/upload-stitched.sh"

if ! command -v fswatch >/dev/null 2>&1; then
  echo "ERROR: fswatch is required (brew install fswatch)" >&2
  exit 1
fi

if [ ! -d "$WATCH_DIR" ]; then
  echo "ERROR: watch dir does not exist: $WATCH_DIR" >&2
  exit 1
fi

if [ ! -x "$UPLOAD_SCRIPT" ]; then
  echo "ERROR: upload script not executable: $UPLOAD_SCRIPT" >&2
  exit 1
fi

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

log "Watching: $WATCH_DIR"
log "Upload script: $UPLOAD_SCRIPT"

# fswatch flags:
#   -0          NUL-separated output (handles spaces in paths)
#   -r          recurse so per-camera subdirs are covered
#   -E          extended regex — {n}, [0-9], etc. in the include pattern
#               only work under extended syntax; without -E the include
#               filter silently never matches and -e ".*" swallows every
#               event, which is exactly what happens if you forget -E.
#   -e          exclude regex (skip everything by default …)
#   -i          … then include only VID_*.mp4 / .insv
#   --event     only fire on Created / MovedTo (file appearance)
#   --latency   small debounce
fswatch \
  -0 \
  -r \
  -E \
  --latency 1 \
  --event=Created \
  --event=Updated \
  --event=MovedTo \
  -e ".*" \
  -i 'VID_[0-9]{8}_[0-9]{6}_[0-9]{2}_[0-9]+.*\.(mp4|insv)$' \
  "$WATCH_DIR" |
  while IFS= read -r -d '' f; do
    log "event: $f"
    # Run uploads sequentially — one big upload at a time keeps quota,
    # bandwidth, and YouTube rate-limits sane.
    "$UPLOAD_SCRIPT" "$f" || log "upload failed for $f"
  done
