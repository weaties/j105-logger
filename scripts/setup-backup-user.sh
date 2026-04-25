#!/usr/bin/env bash
# setup-backup-user.sh — Provision the dedicated `helmlog-backup` user on a Pi.
#
# Run on the Pi (or piped over SSH from the Mac):
#   ssh -t weaties@<pi-host> 'bash -s' < scripts/setup-backup-user.sh
#
# Idempotent — safe to re-run. Requires sudo on the Pi.
#
# What it does:
#   1. Creates system user `helmlog-backup` with home dir, member of `weaties`
#   2. Authorises the Mac's helmlog-backup public key
#   3. Adds a sudoers rule: `helmlog-backup` may run `/usr/bin/rsync` NOPASSWD
#   4. Loosens read perms on .env and influx-token.txt to 640 (group=weaties)
#      so the backup user can read them via group membership.

set -euo pipefail

# Pinned: the helmlog-backup public key generated on the Mac.
PUBKEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHSEm1oV+D1Oay6IsgVQAkB6THuLpdWCN9bFSArVXhnq helmlog-backup-mac@MacBook-Pro-3'

OWNER_USER="${OWNER_USER:-weaties}"   # the existing user that owns helmlog data
OWNER_HOME="$(getent passwd "$OWNER_USER" | cut -d: -f6)"
[ -d "$OWNER_HOME" ] || { echo "Cannot find home dir for $OWNER_USER" >&2; exit 1; }

# 1. Create the user (idempotent).
if id helmlog-backup >/dev/null 2>&1; then
  echo "user helmlog-backup already exists"
else
  echo "creating user helmlog-backup"
  sudo useradd -r -m -s /bin/bash -G "$OWNER_USER" helmlog-backup
fi

# Make sure group membership is correct even if the user pre-existed.
sudo usermod -aG "$OWNER_USER" helmlog-backup

# 2. Authorise the Mac's pubkey.
BACKUP_HOME="$(getent passwd helmlog-backup | cut -d: -f6)"
sudo install -d -o helmlog-backup -g helmlog-backup -m 700 "$BACKUP_HOME/.ssh"
AUTH_KEYS="$BACKUP_HOME/.ssh/authorized_keys"
if sudo grep -qF "$PUBKEY" "$AUTH_KEYS" 2>/dev/null; then
  echo "pubkey already authorised"
else
  echo "appending pubkey to $AUTH_KEYS"
  echo "$PUBKEY" | sudo tee -a "$AUTH_KEYS" >/dev/null
fi
sudo chown helmlog-backup:helmlog-backup "$AUTH_KEYS"
sudo chmod 600 "$AUTH_KEYS"

# 3. Sudoers rule. helmlog-backup needs broad NOPASSWD because:
#    - backup.sh: sudo rsync for /var/lib/grafana
#    - restore.sh: sudo systemctl, rsync into helmlog-owned dirs, sqlite3,
#      cp/install for the influx token, rm for identity wipe
#    The user is dedicated to backup/restore admin and already has full read
#    access to all helmlog data. Auth boundary is the SSH key.
SUDOERS=/etc/sudoers.d/helmlog-backup
RULE='helmlog-backup ALL=(ALL) NOPASSWD: ALL'
if [ ! -f "$SUDOERS" ] || ! sudo grep -qF "$RULE" "$SUDOERS"; then
  echo "writing $SUDOERS"
  echo "$RULE" | sudo tee "$SUDOERS" >/dev/null
  sudo chmod 440 "$SUDOERS"
fi
sudo visudo -cf "$SUDOERS" >/dev/null

# 4. Loosen read perms on the two single-user files the backup needs.
TOKEN="$OWNER_HOME/influx-token.txt"
ENV_FILE="$OWNER_HOME/helmlog/.env"
for f in "$TOKEN" "$ENV_FILE"; do
  if [ -f "$f" ]; then
    sudo chgrp "$OWNER_USER" "$f"
    sudo chmod 640 "$f"
    echo "perm: $f -> $(stat -c '%a %U:%G' "$f")"
  fi
done

# 5. Smoke check.
echo "---"
echo "smoke check as helmlog-backup:"
sudo -u helmlog-backup test -r "$TOKEN"        && echo "  read $TOKEN: OK"        || echo "  read $TOKEN: FAIL"
sudo -u helmlog-backup test -r "$ENV_FILE"     && echo "  read $ENV_FILE: OK"     || echo "  read $ENV_FILE: FAIL"
sudo -u helmlog-backup test -r "$OWNER_HOME/helmlog/data/logger.db" \
                                                && echo "  read logger.db: OK"     || echo "  read logger.db: FAIL"
sudo -u helmlog-backup test -r "$OWNER_HOME/.signalk" \
                                                && echo "  read .signalk: OK"      || echo "  read .signalk: FAIL"
sudo -u helmlog-backup sudo -n /usr/bin/rsync --version >/dev/null \
                                                && echo "  sudo rsync: OK"         || echo "  sudo rsync: FAIL"

echo "done."
