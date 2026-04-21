#!/usr/bin/env bash
# upload-stitched.sh — Upload one already-stitched Insta360 export to YouTube
# and link it to the matching HelmLog session.
#
# Usage:
#   ./scripts/upload-stitched.sh /path/to/VID_YYYYMMDD_HHMMSS_00_NNN.mp4
#
# Designed to be called by a folder watcher (see scripts/watch-exports.sh) but
# safe to run by hand. Single-file in, single result out — no batching here.
#
# Skips files that:
#   - aren't named in the X4 VID_*.mp4 / .insv pattern
#   - are still being written (size still growing, held open, or unparseable)
#   - are already in the upload ledger
#
# Readiness check is defensive: the previous 30 s capped poll uploaded
# half-rendered files whenever Insta360 Studio took longer than that to
# finish exporting, which it always does for multi-GB 360° videos. The
# current check waits until ALL of:
#   1. no process holds the file open (lsof)
#   2. size has been stable for $STABLE_WINDOW_S consecutive seconds
#   3. ffprobe parses the container and reports a positive duration
# before proceeding, with a generous $STABLE_MAX_WAIT_S ceiling so pathological
# exports eventually fail loudly rather than silently uploading garbage.
#
# Required environment (same names as process-videos.sh):
#   PI_API_URL              HelmLog API base URL, e.g. http://<pi-hostname>:3002
#   YOUTUBE_ACCOUNT         YouTube channel handle (selects the OAuth token file)
#
# Optional environment overrides:
#   PI_SESSION_COOKIE       required for linking (no link if empty)
#   HELMLOG_CAMERA_LABEL    default derived from parent dir name
#   TIMEZONE                default America/Los_Angeles
#   VIDEO_PRIVACY           default unlisted
#   HELMLOG_IMPORT_DIR      default ~/Insta360 Imports
#   HELMLOG_BACKUP_DIR      default /Volumes/Insta360 Backups/helmlog
#                           After upload+link succeeds, the stitched export
#                           and any matching source segments in the import
#                           dir are moved here. Set to "" to disable moves.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

FILE="${1:-}"
if [ -z "$FILE" ] || [ ! -f "$FILE" ]; then
  echo "Usage: $0 /path/to/VID_*.mp4" >&2
  exit 1
fi

# Sanity-check the filename pattern; the timestamp comes from the name.
BASE=$(basename "$FILE")
if ! [[ "$BASE" =~ ^(PRO_)?VID_([0-9]{8}_[0-9]{6}) ]]; then
  echo "Skipping (not an Insta360 VID file): $BASE" >&2
  exit 0
fi
TS="${BASH_REMATCH[2]}"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] upload-stitched: $*"; }

# ── Per-file lock ──────────────────────────────────────────────────────────
#
# fswatch may fire several events (Created + N × Updated + MovedTo) for one
# export. Each fired event invokes this script, so without a lock a slow
# readiness gate can be holding up one instance while follow-on events
# queue up more. The lock turns those into no-ops.
LOCKKEY=$(basename "$FILE" | tr -c 'A-Za-z0-9._-' '_')
LOCKDIR="${TMPDIR:-/tmp}/helmlog-upload.${LOCKKEY}.lock"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  log "another upload-stitched.sh is already processing ${BASE} — exiting"
  exit 0
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null || true' EXIT

# ── Wait for the export to be fully written ────────────────────────────────
#
# Studio writes in place and can take tens of minutes on a big 360° file.
# Uploading before Studio finishes produces corrupted videos on YouTube and
# — worse — once the upload "succeeds" the script moves the broken file to
# the backup volume, so the only recovery is to re-export from scratch. The
# pre-flight gate below is deliberately paranoid to avoid that class of bug.

STABLE_WINDOW_S="${HELMLOG_STABLE_WINDOW_S:-120}"   # required quiet interval
STABLE_POLL_S="${HELMLOG_STABLE_POLL_S:-10}"        # poll cadence
STABLE_MAX_WAIT_S="${HELMLOG_STABLE_MAX_WAIT_S:-14400}"  # 4 h hard ceiling

file_has_writer() {
  # Check whether any process holds the file open for writing. Read-only
  # openers (Finder preview, Spotlight) do NOT indicate an in-progress
  # export, so we filter on access mode rather than any open handle.
  #
  # lsof's ``-Fan`` emits one field per line — `a` = access mode (r/w/u),
  # `n` = name. We only need the `a` records: if any one of them reports
  # a writer mode the file is still being written.
  lsof -w -Fan -- "$FILE" 2>/dev/null | awk '
    /^a/ {
      mode = substr($0, 2)
      if (mode ~ /[wu]/) { found = 1; exit }
    }
    END { exit found ? 0 : 1 }
  '
}

ffprobe_ok() {
  # Require ffprobe to (a) parse without error AND (b) return a positive
  # container duration. A half-written MP4 often has a readable header but
  # no moov atom, which makes `-show_format` fail with "Invalid data found
  # when processing input". A fully-written file will not.
  local dur
  dur=$(ffprobe -v error -show_entries format=duration -of default=nk=1:nw=1 \
        -- "$FILE" 2>/dev/null || true)
  [ -n "$dur" ] && awk -v d="$dur" 'BEGIN { exit (d+0 > 0) ? 0 : 1 }'
}

if ! command -v ffprobe >/dev/null 2>&1; then
  log "ERROR: ffprobe is required for readiness validation (brew install ffmpeg)" >&2
  exit 1
fi

log "readiness gate: file=$FILE window=${STABLE_WINDOW_S}s max=${STABLE_MAX_WAIT_S}s"

started_at=$(date +%s)
prev_size=-1
stable_since=0

while true; do
  now=$(date +%s)
  elapsed=$((now - started_at))

  if [ "$elapsed" -ge "$STABLE_MAX_WAIT_S" ]; then
    log "ERROR: file did not stabilize within ${STABLE_MAX_WAIT_S}s — aborting to avoid uploading a half-written export" >&2
    exit 1
  fi

  if file_has_writer; then
    [ "$((elapsed % 60))" -lt "$STABLE_POLL_S" ] && \
      log "still being written (lsof shows a writer) — waiting… elapsed=${elapsed}s"
    prev_size=-1
    stable_since=0
    sleep "$STABLE_POLL_S"
    continue
  fi

  cur=$(stat -f%z "$FILE" 2>/dev/null || echo 0)
  if [ "$cur" -le 0 ]; then
    sleep "$STABLE_POLL_S"
    continue
  fi

  if [ "$cur" = "$prev_size" ]; then
    [ "$stable_since" = 0 ] && stable_since=$now
    stable_for=$((now - stable_since))
    if [ "$stable_for" -ge "$STABLE_WINDOW_S" ]; then
      if ffprobe_ok; then
        log "readiness gate: passed (size=${cur} stable_for=${stable_for}s)"
        break
      else
        log "size stable but ffprobe rejected the container — resetting window" >&2
        prev_size=-1
        stable_since=0
      fi
    fi
  else
    prev_size=$cur
    stable_since=0
  fi

  sleep "$STABLE_POLL_S"
done

# Default the camera label from the parent directory if not set explicitly,
# so files dropped into ~/Insta360 Exports/stern/ get tagged "stern".
if [ -z "${HELMLOG_CAMERA_LABEL:-}" ]; then
  parent=$(basename "$(dirname "$FILE")")
  if [ "$parent" != "Insta360 Exports" ] && [ "$parent" != "." ]; then
    export HELMLOG_CAMERA_LABEL="$parent"
  fi
fi

: "${PI_API_URL:?PI_API_URL must be set — e.g. export PI_API_URL=http://<pi-hostname>:3002}"
: "${YOUTUBE_ACCOUNT:?YOUTUBE_ACCOUNT must be set — selects ~/.config/helmlog/youtube/<account>.json}"
export PI_API_URL
export YOUTUBE_ACCOUNT
export PI_SESSION_COOKIE="${PI_SESSION_COOKIE:-}"
export TIMEZONE="${TIMEZONE:-America/Los_Angeles}"
export VIDEO_PRIVACY="${VIDEO_PRIVACY:-unlisted}"
export HELMLOG_IMPORT_DIR="${HELMLOG_IMPORT_DIR:-$HOME/Insta360 Imports}"
export HELMLOG_BACKUP_DIR="${HELMLOG_BACKUP_DIR:-/Volumes/Insta360 Backups/helmlog}"

cd "$PROJECT_DIR"

uv run --no-sync python - "$FILE" "$TS" << 'PY'
import asyncio
import os
import sys
from pathlib import Path

from helmlog.insta360 import InstaRecording
from helmlog.pipeline import PipelineConfig, fetch_sessions_from_pi, process_recording
from helmlog.video_ledger import LedgerEntry, LedgerKey, VideoLedger


async def main() -> int:
    src = Path(sys.argv[1])
    ts = sys.argv[2]

    cfg = PipelineConfig(
        pi_api_url=os.environ["PI_API_URL"],
        pi_session_cookie=os.environ.get("PI_SESSION_COOKIE", ""),
        privacy=os.environ.get("VIDEO_PRIVACY", "unlisted"),
        timezone=os.environ.get("TIMEZONE", "America/Los_Angeles"),
        camera_label=os.environ.get("HELMLOG_CAMERA_LABEL", ""),
        youtube_account=os.environ.get("YOUTUBE_ACCOUNT", ""),
    )

    # Skip if we've already uploaded this exact file (size + name).
    ledger = VideoLedger()
    key = LedgerKey(
        volume_uuid=cfg.camera_label or "unknown",
        source_filename=src.name,
        size_bytes=src.stat().st_size,
    )
    if ledger.has(key):
        prev = ledger.get(key)
        print(f"already uploaded: {prev.youtube_url if prev else 'unknown'} — skipping")
        return 0

    sessions = await fetch_sessions_from_pi(
        cfg.pi_api_url, session_cookie=cfg.pi_session_cookie
    )
    print(f"fetched {len(sessions)} session(s) from Pi")

    rec = InstaRecording(
        timestamp_str=ts,
        segments=[src],
        total_size_bytes=src.stat().st_size,
        needs_stitching=False,
    )
    result = await process_recording(
        rec=rec, video_path=src, sessions=sessions, config=cfg
    )

    print(
        f"uploaded={result.uploaded} video_id={result.video_id} "
        f"session_id={result.session_id} linked={result.linked} "
        f"error={result.error}"
    )

    if result.uploaded and result.video_id and result.youtube_url:
        ledger.record(
            LedgerEntry(
                volume_uuid=cfg.camera_label or "unknown",
                source_filename=src.name,
                size_bytes=src.stat().st_size,
                video_id=result.video_id,
                youtube_url=result.youtube_url,
                camera_label=cfg.camera_label,
                session_id=result.session_id,
                linked=result.linked,
            )
        )

        # Move the stitched export and any matching source segments from the
        # import dir to the long-term backup volume. Only run when linking
        # succeeded too — keeps unlinked uploads in place for manual
        # troubleshooting.
        if result.linked:
            backup_dir = Path(os.environ.get("HELMLOG_BACKUP_DIR", "")).expanduser()
            import_dir = Path(
                os.environ.get("HELMLOG_IMPORT_DIR", "~/Insta360 Imports")
            ).expanduser()
            if backup_dir and str(backup_dir):
                _move_to_backup(src, ts, backup_dir, import_dir)

        # macOS notification — fire-and-forget, no error if osascript missing
        try:
            import subprocess

            subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'display notification "{result.youtube_url}" '
                    f'with title "HelmLog video uploaded" '
                    f'subtitle "{src.name}"',
                ],
                check=False,
                timeout=5,
            )
        except Exception:  # noqa: BLE001
            pass
        return 0

    return 1


def _move_to_backup(
    stitched: Path, timestamp: str, backup_dir: Path, import_dir: Path
) -> None:
    """Move the uploaded stitched MP4 + matching source segments to backups.

    Source segments are identified by the recording timestamp, e.g. all files
    in ``import_dir`` whose name matches ``VID_<timestamp>_00_*`` (and the
    ``LRV_..._01_*`` previews) get archived together.

    Cross-volume moves on macOS use ``shutil.move`` which copy-then-deletes,
    so this can take a few minutes for a 13 GB file. The function logs each
    move and continues on individual failures rather than aborting the run —
    a partial backup is better than none.
    """
    import shutil

    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"backup: cannot create {backup_dir}: {exc}")
        return

    moved: list[Path] = []

    # 1. The stitched MP4 we just uploaded.
    if stitched.exists():
        try:
            dest = backup_dir / stitched.name
            print(f"backup: moving stitched export → {dest}")
            shutil.move(str(stitched), str(dest))
            moved.append(dest)
        except OSError as exc:
            print(f"backup: failed to move {stitched.name}: {exc}")

    # 2. All source segments in the import dir for this timestamp.
    if import_dir.is_dir():
        for src_file in sorted(import_dir.iterdir()):
            name = src_file.name
            # Match VID_<ts>_00_* (main lens) and LRV_<ts>_01_* (preview)
            if not (
                name.startswith(f"VID_{timestamp}_00_")
                or name.startswith(f"LRV_{timestamp}_01_")
            ):
                continue
            try:
                dest = backup_dir / name
                print(f"backup: moving source → {dest}")
                shutil.move(str(src_file), str(dest))
                moved.append(dest)
            except OSError as exc:
                print(f"backup: failed to move {name}: {exc}")

    if moved:
        print(f"backup: archived {len(moved)} file(s) to {backup_dir}")
    else:
        print(f"backup: nothing to move from {import_dir}")


sys.exit(asyncio.run(main()))
PY
