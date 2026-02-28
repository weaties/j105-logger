#!/usr/bin/env bash
# provision-grafana.sh — Deploy sailing dashboard and enable anonymous access.
# Idempotent: safe to run multiple times.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DASHBOARD_SRC="$SCRIPT_DIR/grafana/sailing-data.json"
PI_HEALTH_SRC="$SCRIPT_DIR/grafana/pi-health.json"
PROVISION_SRC="$SCRIPT_DIR/grafana/dashboards.yaml"
DATASOURCE_SRC="$SCRIPT_DIR/grafana/datasources.yaml"

DASHBOARD_DEST_DIR="/var/lib/grafana/dashboards"
PROVISION_DEST="/etc/grafana/provisioning/dashboards/j105-logger.yaml"
DATASOURCE_DEST="/etc/grafana/provisioning/datasources/j105-logger.yaml"
GRAFANA_INI="/etc/grafana/grafana.ini"

echo "==> Creating dashboard directory: $DASHBOARD_DEST_DIR"
sudo mkdir -p "$DASHBOARD_DEST_DIR"

echo "==> Copying dashboard JSONs"
sudo cp "$DASHBOARD_SRC" "$DASHBOARD_DEST_DIR/sailing-data.json"
sudo cp "$PI_HEALTH_SRC" "$DASHBOARD_DEST_DIR/pi-health.json"

echo "==> Copying dashboard provisioning config"
sudo cp "$PROVISION_SRC" "$PROVISION_DEST"

echo "==> Copying datasource provisioning config"
sudo cp "$DATASOURCE_SRC" "$DATASOURCE_DEST"

echo "==> Enabling anonymous access in $GRAFANA_INI"
# Insert/update the [auth.anonymous] section.
# If the section already exists, update enabled and org_role.
# If not, append the section at the end.
if sudo grep -q '^\[auth\.anonymous\]' "$GRAFANA_INI"; then
  # Section exists — update or insert the two keys inside it
  sudo python3 - "$GRAFANA_INI" <<'PYEOF'
import sys, re

path = sys.argv[1]
with open(path) as f:
    text = f.read()

# Split into sections
sections = re.split(r'(?=^\[)', text, flags=re.MULTILINE)
out = []
for sec in sections:
    if sec.startswith('[auth.anonymous]'):
        # Ensure enabled = true
        if re.search(r'^enabled\s*=', sec, re.MULTILINE):
            sec = re.sub(r'^(enabled\s*=\s*).*$', r'\1true', sec, flags=re.MULTILINE)
        else:
            sec = sec.rstrip('\n') + '\nenabled = true\n'
        # Ensure org_role = Viewer
        if re.search(r'^org_role\s*=', sec, re.MULTILINE):
            sec = re.sub(r'^(org_role\s*=\s*).*$', r'\1Viewer', sec, flags=re.MULTILINE)
        else:
            sec = sec.rstrip('\n') + '\norg_role = Viewer\n'
    out.append(sec)

with open(path, 'w') as f:
    f.write(''.join(out))
print("  grafana.ini updated (section existed)")
PYEOF
else
  # Section does not exist — append it
  printf '\n[auth.anonymous]\nenabled = true\norg_role = Viewer\n' | sudo tee -a "$GRAFANA_INI" > /dev/null
  echo "  grafana.ini updated (section appended)"
fi

echo "==> Restarting grafana-server"
sudo systemctl restart grafana-server

echo ""
echo "Done."
echo "  Sailing data: http://$(hostname):3001/d/j105-sailing/sailing-data"
echo "  Pi health:    http://$(hostname):3001/d/pi-health/pi-health"
