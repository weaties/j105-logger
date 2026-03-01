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
    ca-certificates lsb-release jq \
    libportaudio2 libsndfile1

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
      { "type": "providers/canbus", "options": { "canDevice": "can0" } },
      { "type": "providers/canboatjs", "options": {} },
      { "type": "providers/n2k-signalk", "options": {} }
    ],
    "enabled": true
  }],
  "interfaces": { "plugins": true }
}
EOF
else
    info "~/.signalk/settings.json already exists — skipping."
fi

# Install Signal K plugins
info "Installing Signal K plugins..."
cat > "$HOME/.signalk/package.json" << 'EOF'
{
  "name": "signalk-server-config",
  "version": "1.0.0",
  "dependencies": {
    "signalk-to-influxdb2": "*",
    "signalk-derived-data": "*"
  }
}
EOF
(cd "$HOME/.signalk" && npm install --no-fund --no-audit --legacy-peer-deps) || \
    warn "Signal K plugin install had errors — run 'cd ~/.signalk && npm install --legacy-peer-deps' to retry."

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
    info "Installing InfluxDB ${INFLUX_VERSION} via apt repo..."
    # Add InfluxData apt repo (arm64 debs are only available via their repo, not direct download)
    curl -fsSL https://repos.influxdata.com/influxdata-archive.key | \
        gpg --dearmor | sudo tee /etc/apt/trusted.gpg.d/influxdata-archive.gpg > /dev/null
    echo "deb [signed-by=/etc/apt/trusted.gpg.d/influxdata-archive.gpg] https://repos.influxdata.com/debian stable main" | \
        sudo tee /etc/apt/sources.list.d/influxdata.list > /dev/null
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "influxdb2=${INFLUX_VERSION}-1"
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
Environment=GF_SERVER_ROOT_URL=%(protocol)s://%(domain)s/grafana/
Environment=GF_SERVER_SERVE_FROM_SUB_PATH=true
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
    "influxes": [
      {
        "url": "http://localhost:8086",
        "token": "${INFLUX_TOKEN}",
        "org": "j105",
        "bucket": "signalk",
        "onlySelf": true
      }
    ]
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

# Install j105-logger wrapper script so the command works directly in the shell
# (uv console scripts live in the venv, not in ~/.local/bin)
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/j105-logger" << WRAPPER
#!/usr/bin/env bash
exec "${UV_BIN}" run --project "${PROJECT_DIR}" j105-logger "\$@"
WRAPPER
chmod +x "$HOME/.local/bin/j105-logger"
info "j105-logger wrapper installed to ~/.local/bin/j105-logger"

# Ensure ~/.local/bin is in PATH permanently
if ! grep -q 'local/bin' "$HOME/.bashrc" 2>/dev/null; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
    info "Added ~/.local/bin to PATH in ~/.bashrc"
else
    info "~/.local/bin already in ~/.bashrc PATH"
fi

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

step "Ensuring data directories exist..."
mkdir -p "$PROJECT_DIR/data"
mkdir -p "$PROJECT_DIR/data/audio"
info "$PROJECT_DIR/data (SQLite DB)"
info "$PROJECT_DIR/data/audio (WAV recordings)"

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
# l) Tailscale Funnel — expose webapp, Grafana, and Signal K on port 443
# ---------------------------------------------------------------------------

step "Configuring Tailscale Funnel..."
if command -v tailscale &>/dev/null; then
    TS_HOSTNAME="$(tailscale status --json 2>/dev/null | jq -r '.Self.DNSName // empty' | sed 's/\.$//' || echo '')"
    if [[ -n "$TS_HOSTNAME" ]]; then
        PUBLIC_URL_VALUE="https://${TS_HOSTNAME}"
        # Grant current user permission to configure Tailscale serve/funnel (idempotent)
        sudo tailscale set --operator="$CURRENT_USER"
        # Path-based funnel rules (idempotent — safe to re-run)
        tailscale funnel --bg 3002
        tailscale funnel --bg --set-path /grafana/ 3001
        tailscale funnel --bg --set-path /signalk/ 3000
        info "Tailscale Funnel enabled: ${PUBLIC_URL_VALUE}"
        # Persist PUBLIC_URL in .env so the webapp generates correct links
        if grep -q '^PUBLIC_URL=' "$ENV_FILE" 2>/dev/null; then
            sed -i "s|^PUBLIC_URL=.*|PUBLIC_URL=${PUBLIC_URL_VALUE}|" "$ENV_FILE"
            info "Updated PUBLIC_URL in .env"
        else
            printf '\nPUBLIC_URL=%s\n' "${PUBLIC_URL_VALUE}" >> "$ENV_FILE"
            info "Added PUBLIC_URL to .env — restart j105-logger to pick it up"
        fi
    else
        warn "Tailscale not connected — Funnel not configured."
        warn "Run 'tailscale up', then re-run setup.sh to enable public access."
    fi
else
    warn "tailscale CLI not found — Funnel not configured."
    warn "Install Tailscale (https://tailscale.com/download/linux), then re-run setup.sh."
fi

# ---------------------------------------------------------------------------
# m) Done — summary
# ---------------------------------------------------------------------------

echo ""
echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Setup complete. Reboot, then verify:${NC}"
echo -e "${GREEN}══════════════════════════════════════════════════${NC}"
echo ""
echo "  Signal K:    http://corvopi:3000"
echo "  Grafana:     http://corvopi:3001"
echo "  InfluxDB:    http://corvopi:8086"
echo "  Race marker: http://corvopi:3002"
if [[ -n "${TS_HOSTNAME:-}" ]]; then
    echo ""
    echo "  Public (Tailscale Funnel):"
    echo "    Race marker: https://${TS_HOSTNAME}/"
    echo "    Grafana:     https://${TS_HOSTNAME}/grafana/"
    echo "    Signal K:    https://${TS_HOSTNAME}/signalk/"
fi
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
echo "  To list available audio input devices (e.g. Gordik USB receiver):"
echo "    j105-logger list-devices"
echo "  Then set AUDIO_DEVICE in .env to match the device name or index."
echo ""
echo "  To update after a git pull:"
echo "    git pull && ./scripts/setup.sh"
echo "    sudo npm update -g signalk-server"
