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
#   - are still being written (size still growing)
#   - are already in the upload ledger
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

# Wait for the file to stop growing — Studio writes incrementally and we
# don't want to upload a half-rendered file.
prev_size=-1
for _ in 1 2 3 4 5 6 7 8 9 10; do
  cur=$(stat -f%z "$FILE" 2>/dev/null || echo 0)
  if [ "$cur" -gt 0 ] && [ "$cur" -eq "$prev_size" ]; then
    break
  fi
  prev_size=$cur
  sleep 3
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
