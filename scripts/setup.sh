#!/usr/bin/env bash
# setup.sh — Full automated setup for j105-logger + Signal K + InfluxDB + Grafana.
#
# Usage:
#   ./scripts/setup.sh
#
# Fully idempotent — safe to re-run after a git pull. No browser steps required.
# Requires sudo for package installation and systemd service management.

set -euo pipefail

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CURRENT_USER="$(id -un)"
ENV_FILE="$PROJECT_DIR/.env"
INFLUX_TOKEN_FILE="$HOME/influx-token.txt"

INFLUX_VERSION="2.7.11"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

step()  { echo -e "\n${GREEN}==> $*${NC}"; }
warn()  { echo -e "${YELLOW}WARN:${NC} $*"; }
info()  { echo -e "${CYAN}    $*${NC}"; }

# ---------------------------------------------------------------------------
# a) System prerequisites
# ---------------------------------------------------------------------------

step "Installing system prerequisites..."
sudo apt-get update -qq
sudo apt-get install -y \
    git can-utils curl gnupg2 apt-transport-https \
    software-properties-common ca-certificates lsb-release jq

# ---------------------------------------------------------------------------
# b) Node.js 24 LTS
# ---------------------------------------------------------------------------

step "Checking Node.js..."
if ! node --version 2>/dev/null | grep -q "^v24"; then
    info "Installing Node.js 24 LTS via NodeSource..."
    curl -fsSL https://deb.nodesource.com/setup_24.x | sudo -E bash -
    sudo apt-get install -y nodejs
else
    info "Node.js $(node --version) already installed."
fi

# ---------------------------------------------------------------------------
# c) Signal K Server
# ---------------------------------------------------------------------------

step "Installing Signal K Server..."
sudo npm install -g signalk-server

SK_BIN="$(command -v signalk-server || echo '/usr/lib/node_modules/signalk-server/bin/signalk-server')"
info "signalk-server: $SK_BIN"

# Signal K config directory
mkdir -p "$HOME/.signalk"

# Write settings.json (CAN bus pipedProvider pre-configured)
if [[ ! -f "$HOME/.signalk/settings.json" ]]; then
    info "Writing ~/.signalk/settings.json..."
    cat > "$HOME/.signalk/settings.json" << 'EOF'
{
  "vessel": { "name": "corvopi" },
  "pipedProviders": [{
    "id": "n2k-canbus",
    "pipeElements": [
      { "type": "providers/rawsocketcan", "options": { "interface": "can0" } },
      { "type": "providers/canboatjs", "options": {} }
    ],
    "enabled": true
  }],
  "interfaces": { "plugins": true }
}
EOF
else
    info "~/.signalk/settings.json already exists — skipping."
fi

# Install signalk-to-influxdb2 plugin
info "Installing signalk-to-influxdb2 plugin..."
cat > "$HOME/.signalk/package.json" << 'EOF'
{
  "name": "signalk-server-config",
  "version": "1.0.0",
  "dependencies": { "signalk-to-influxdb2": "*" }
}
EOF
(cd "$HOME/.signalk" && npm install --silent)

# systemd service for Signal K
sudo tee /etc/systemd/system/signalk.service > /dev/null << EOF
[Unit]
Description=Signal K Server
After=can-interface.service network.target
Wants=can-interface.service

[Service]
User=${CURRENT_USER}
WorkingDirectory=/home/${CURRENT_USER}/.signalk
ExecStart=/usr/bin/node ${SK_BIN} -c /home/${CURRENT_USER}/.signalk
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable signalk.service
info "signalk.service installed and enabled."

# ---------------------------------------------------------------------------
# d) InfluxDB 2.7.11 (pinned; apt-mark hold prevents v3 auto-upgrade)
# ---------------------------------------------------------------------------

step "Checking InfluxDB..."
if ! influx version 2>/dev/null | grep -q "${INFLUX_VERSION}"; then
    info "Installing InfluxDB ${INFLUX_VERSION}..."
    INFLUX_DEB="influxdb2_${INFLUX_VERSION}_arm64.deb"
    wget -q "https://dl.influxdata.com/influxdb/releases/${INFLUX_DEB}"
    sudo dpkg -i "${INFLUX_DEB}"
    rm "${INFLUX_DEB}"
    sudo systemctl enable --now influxdb
    sudo apt-mark hold influxdb2
    # Wait for influxd to be ready
    info "Waiting for influxd to start..."
    for i in {1..15}; do
        influx ping 2>/dev/null && break
        sleep 2
    done
else
    info "InfluxDB ${INFLUX_VERSION} already installed."
fi

# Initial setup (idempotent — exits 0 if already configured)
influx setup \
    --username admin \
    --password changeme123 \
    --org j105 \
    --bucket signalk \
    --retention 0 \
    --force 2>/dev/null || true

# Capture token for downstream configs
INFLUX_TOKEN="$(influx auth list --json 2>/dev/null | jq -r '.[0].token' || echo '')"
if [[ -z "$INFLUX_TOKEN" || "$INFLUX_TOKEN" == "null" ]]; then
    warn "Could not retrieve InfluxDB token. Plugin config will need manual token update."
    INFLUX_TOKEN="REPLACE_WITH_INFLUX_TOKEN"
else
    echo "$INFLUX_TOKEN" > "$INFLUX_TOKEN_FILE"
    chmod 600 "$INFLUX_TOKEN_FILE"
    info "InfluxDB token saved to: $INFLUX_TOKEN_FILE"
fi

# ---------------------------------------------------------------------------
# e) Grafana OSS (latest via apt repo, port 3001 to avoid clash with Signal K)
# ---------------------------------------------------------------------------

step "Installing Grafana..."
sudo mkdir -p /etc/apt/keyrings
wget -q -O - https://apt.grafana.com/gpg.key | \
    gpg --dearmor | sudo tee /etc/apt/keyrings/grafana.gpg > /dev/null
echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" | \
    sudo tee /etc/apt/sources.list.d/grafana.list > /dev/null
sudo apt-get update -qq && sudo apt-get install -y grafana

# Override Grafana's default port (3000 is taken by Signal K)
sudo mkdir -p /etc/systemd/system/grafana-server.service.d
sudo tee /etc/systemd/system/grafana-server.service.d/port.conf > /dev/null << 'EOF'
[Service]
Environment=GF_SERVER_HTTP_PORT=3001
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now grafana-server

# Pre-provision InfluxDB datasource via Grafana provisioning
sudo mkdir -p /etc/grafana/provisioning/datasources
sudo tee /etc/grafana/provisioning/datasources/influxdb.yaml > /dev/null << EOF
apiVersion: 1
datasources:
  - name: InfluxDB
    type: influxdb
    access: proxy
    url: http://localhost:8086
    jsonData:
      version: Flux
      organization: j105
      defaultBucket: signalk
      httpMode: POST
    secureJsonData:
      token: ${INFLUX_TOKEN}
    isDefault: true
EOF
sudo systemctl restart grafana-server
info "Grafana installed and provisioned on port 3001."

# ---------------------------------------------------------------------------
# f) Signal K → InfluxDB plugin config (with captured token)
# ---------------------------------------------------------------------------

step "Configuring signalk-to-influxdb2 plugin..."
mkdir -p "$HOME/.signalk/plugin-config-data"
cat > "$HOME/.signalk/plugin-config-data/signalk-to-influxdb2.json" << EOF
{
  "configuration": {
    "url": "http://localhost:8086",
    "token": "${INFLUX_TOKEN}",
    "org": "j105",
    "bucket": "signalk",
    "enabled": true
  },
  "enabled": true
}
EOF
info "Plugin config written."

# ---------------------------------------------------------------------------
# g) uv + Python dependencies
# ---------------------------------------------------------------------------

step "Checking uv..."
UV_BIN=""
if command -v uv &>/dev/null; then
    UV_BIN="$(command -v uv)"
elif [[ -x "$HOME/.local/bin/uv" ]]; then
    UV_BIN="$HOME/.local/bin/uv"
fi

if [[ -z "$UV_BIN" ]]; then
    info "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    UV_BIN="$HOME/.local/bin/uv"
fi
info "uv: $UV_BIN ($("$UV_BIN" --version))"

step "Syncing Python dependencies..."
"$UV_BIN" sync --project "$PROJECT_DIR"

# ---------------------------------------------------------------------------
# h) .env file and data directory
# ---------------------------------------------------------------------------

step "Configuring environment..."
if [[ ! -f "$ENV_FILE" ]]; then
    cp "$PROJECT_DIR/.env.example" "$ENV_FILE"
    info "Created .env from template."
    info "Review $ENV_FILE and adjust if needed."
else
    info ".env already exists — leaving untouched."
fi

# Load env vars for service generation
set -a
# shellcheck disable=SC1090
source <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_FILE" | grep -v '^#')
set +a

CAN_INTERFACE="${CAN_INTERFACE:-can0}"
CAN_BITRATE="${CAN_BITRATE:-250000}"

step "Ensuring data directory exists..."
mkdir -p "$PROJECT_DIR/data"
info "$PROJECT_DIR/data"

# ---------------------------------------------------------------------------
# i) netdev group (allows non-root SocketCAN access)
# ---------------------------------------------------------------------------

step "Checking group membership..."
if ! id -nG "$CURRENT_USER" | grep -qw netdev; then
    sudo usermod -aG netdev "$CURRENT_USER"
    warn "Added $CURRENT_USER to the 'netdev' group — reboot required."
else
    info "$CURRENT_USER is already in the 'netdev' group."
fi

# ---------------------------------------------------------------------------
# j) CAN interface systemd service
# ---------------------------------------------------------------------------

step "Installing CAN interface service (interface=$CAN_INTERFACE bitrate=$CAN_BITRATE)..."
sudo tee /etc/systemd/system/can-interface.service > /dev/null << EOF
[Unit]
Description=SocketCAN interface ${CAN_INTERFACE}
After=sys-subsystem-net-devices-${CAN_INTERFACE}.device
BindsTo=sys-subsystem-net-devices-${CAN_INTERFACE}.device

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=-/sbin/ip link set ${CAN_INTERFACE} down
ExecStart=/sbin/ip link set ${CAN_INTERFACE} up type can bitrate ${CAN_BITRATE}
ExecStop=/sbin/ip link set ${CAN_INTERFACE} down

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable can-interface.service

if ip link show "$CAN_INTERFACE" &>/dev/null; then
    sudo systemctl restart can-interface.service
    info "CAN interface ${CAN_INTERFACE} is up."
else
    warn "Network device '${CAN_INTERFACE}' not found — service enabled but not started."
    warn "Add dtoverlay=mcp2515-can0,oscillator=16000000,interrupt=25 to /boot/firmware/config.txt"
    warn "and reboot, then re-run this script."
fi

# ---------------------------------------------------------------------------
# k) j105-logger systemd service (depends on Signal K, not directly on CAN)
# ---------------------------------------------------------------------------

step "Installing j105-logger service..."
sudo tee /etc/systemd/system/j105-logger.service > /dev/null << EOF
[Unit]
Description=J105 NMEA 2000 Data Logger
After=signalk.service
Wants=signalk.service

[Service]
User=${CURRENT_USER}
WorkingDirectory=${PROJECT_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${UV_BIN} run --project ${PROJECT_DIR} j105-logger run
Restart=on-failure
RestartSec=5
SupplementaryGroups=netdev

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable j105-logger.service

if sudo systemctl is-active --quiet signalk.service 2>/dev/null; then
    sudo systemctl restart j105-logger.service
    info "Logger service started."
else
    info "Logger service enabled (will start once Signal K is running)."
fi

# ---------------------------------------------------------------------------
# l) Done — summary
# ---------------------------------------------------------------------------

echo ""
echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Setup complete. Reboot, then verify:${NC}"
echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
echo ""
echo "  Signal K:  http://corvopi:3000"
echo "  Grafana:   http://corvopi:3001"
echo "  InfluxDB:  http://corvopi:8086"
echo ""
if [[ -f "$INFLUX_TOKEN_FILE" ]]; then
    echo "  InfluxDB token saved to: $INFLUX_TOKEN_FILE"
fi
echo ""
echo "  sudo reboot"
echo ""
echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
echo ""
echo "  After reboot, check service status:"
echo "    sudo systemctl status can-interface signalk influxd grafana-server j105-logger"
echo ""
echo "  View logger output:"
echo "    sudo journalctl -fu j105-logger"
echo ""
echo "  To update after a git pull:"
echo "    git pull && ./scripts/setup.sh"
echo "    sudo npm update -g signalk-server"
