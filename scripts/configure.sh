#!/usr/bin/env bash
# configure.sh — Interactive configuration wizard for HelmLog.
#
# Guides the operator through external service settings and persists them
# to ~/.helmlog/config.env so they survive reset-pi.sh (which wipes the repo).
#
# Usage:
#   ./scripts/configure.sh                     # interactive prompts
#   ./scripts/configure.sh --non-interactive   # use env vars only, no prompts
#   ./scripts/configure.sh --clear KEY         # clear a single value
#   ./scripts/configure.sh --clear-all         # clear all values
#   ./scripts/configure.sh --show              # print current config
#
# Environment variables override prompts (for CI / scripted installs):
#   ADMIN_EMAIL=me@example.com ./scripts/configure.sh --non-interactive
#
# Integration:
#   - setup.sh calls this early (step 0) before creating .env
#   - bootstrap.sh calls this after clone, before setup.sh
#   - deploy.sh reads ~/.helmlog/config.env for service restart decisions
#   - reset-pi.sh does NOT touch ~/.helmlog/

set -euo pipefail

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

CONFIG_DIR="$HOME/.helmlog"
CONFIG_FILE="$CONFIG_DIR/config.env"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}    $*${NC}"; }
warn()  { echo -e "${YELLOW}WARN:${NC} $*"; }

# ---------------------------------------------------------------------------
# Config fields — order matters for the interactive wizard
# ---------------------------------------------------------------------------

# Each entry: VAR_NAME|prompt_label|is_secret (true/false)
CONFIG_FIELDS=(
    "ADMIN_EMAIL|Admin email|false"
    "SMTP_HOST|SMTP host|false"
    "SMTP_PORT|SMTP port|false"
    "SMTP_USER|SMTP user|false"
    "SMTP_PASSWORD|SMTP password|true"
    "SMTP_FROM|SMTP from address|false"
    "CLOUDFLARE_TUNNEL_TOKEN|Cloudflare tunnel token|true"
    "CLOUDFLARE_HOSTNAME|Cloudflare hostname (e.g. boat.helmlog.org)|false"
    "INFLUX_URL|InfluxDB URL (e.g. http://localhost:8086)|false"
    "INFLUX_TOKEN|InfluxDB token|true"
    "INFLUX_ORG|InfluxDB org|false"
    "INFLUX_BUCKET|InfluxDB bucket|false"
    "CAMERAS|Cameras (e.g. Stern:192.168.42.1)|false"
    "CAMERA_WIFI_SSID|Camera WiFi SSID|false"
    "CAMERA_WIFI_PASSWORD|Camera WiFi password|true"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ensure_config_dir() {
    mkdir -p "$CONFIG_DIR"
}

# Read a value from config.env (returns empty string if not found)
read_config() {
    local key="$1"
    if [[ -f "$CONFIG_FILE" ]]; then
        grep -E "^${key}=" "$CONFIG_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true
    fi
}

# Write a key=value to config.env (creates backup, updates in place or appends)
write_config() {
    local key="$1"
    local value="$2"

    ensure_config_dir

    # Backup before first write in this session
    if [[ -f "$CONFIG_FILE" && ! -f "${CONFIG_FILE}.bak.$$" ]]; then
        cp "$CONFIG_FILE" "${CONFIG_FILE}.bak"
        touch "${CONFIG_FILE}.bak.$$"  # sentinel to avoid repeated backups
    fi

    if [[ -f "$CONFIG_FILE" ]] && grep -qE "^${key}=" "$CONFIG_FILE" 2>/dev/null; then
        # Update existing line — use | as delimiter to avoid issues with / in values
        sed -i "s|^${key}=.*|${key}=${value}|" "$CONFIG_FILE"
    else
        echo "${key}=${value}" >> "$CONFIG_FILE"
    fi
}

# Remove a key from config.env
remove_config() {
    local key="$1"
    if [[ -f "$CONFIG_FILE" ]]; then
        sed -i "/^${key}=/d" "$CONFIG_FILE"
    fi
}

# Prompt for a single config value with current default
prompt_field() {
    local var_name="$1"
    local label="$2"
    local is_secret="$3"
    local current_value
    current_value="$(read_config "$var_name")"

    # If env var is set, use it as override (for unattended mode)
    local env_value="${!var_name:-}"
    if [[ -n "$env_value" ]]; then
        write_config "$var_name" "$env_value"
        return
    fi

    # Build the default display
    local default_display=""
    if [[ -n "$current_value" ]]; then
        if [[ "$is_secret" == "true" ]]; then
            default_display="[****]"
        else
            default_display="[${current_value}]"
        fi
    fi

    # Prompt
    local prompt_text="${label} ${default_display}: "
    local input
    if [[ "$is_secret" == "true" ]]; then
        read -rsp "$prompt_text" input
        echo ""  # newline after hidden input
    else
        read -rp "$prompt_text" input
    fi

    # If user pressed Enter with no input, keep current value
    if [[ -z "$input" ]]; then
        return
    fi

    write_config "$var_name" "$input"
}

# Validate email format (basic check)
validate_email() {
    local email="$1"
    if [[ -n "$email" ]] && ! [[ "$email" =~ ^[^@]+@[^@]+\.[^@]+$ ]]; then
        warn "\"${email}\" doesn't look like a valid email address."
        return 1
    fi
    return 0
}

# Validate hostname format (basic check)
validate_hostname() {
    local hostname="$1"
    if [[ -n "$hostname" ]] && ! [[ "$hostname" =~ ^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$ ]]; then
        warn "\"${hostname}\" doesn't look like a valid hostname."
        return 1
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

cmd_clear() {
    local key="$1"
    if [[ -f "$CONFIG_FILE" ]] && grep -qE "^${key}=" "$CONFIG_FILE" 2>/dev/null; then
        remove_config "$key"
        info "Cleared ${key} from config.env"
    else
        info "${key} is not set."
    fi
}

cmd_clear_all() {
    if [[ -f "$CONFIG_FILE" ]]; then
        cp "$CONFIG_FILE" "${CONFIG_FILE}.bak"
        rm "$CONFIG_FILE"
        ensure_config_dir
        touch "$CONFIG_FILE"
        info "All values cleared. Backup saved to config.env.bak"
    else
        info "No config.env to clear."
    fi
}

cmd_show() {
    if [[ ! -f "$CONFIG_FILE" ]]; then
        echo "No configuration found at ${CONFIG_FILE}"
        return
    fi
    echo ""
    echo "Current configuration (${CONFIG_FILE}):"
    echo ""
    for entry in "${CONFIG_FIELDS[@]}"; do
        IFS='|' read -r var_name label is_secret <<< "$entry"
        local value
        value="$(read_config "$var_name")"
        if [[ -z "$value" ]]; then
            echo "  ${var_name}=(not set)"
        elif [[ "$is_secret" == "true" ]]; then
            echo "  ${var_name}=****"
        else
            echo "  ${var_name}=${value}"
        fi
    done
    echo ""
}

cmd_interactive() {
    ensure_config_dir

    echo ""
    echo -e "${GREEN}═══════════════════════════════════════${NC}"
    echo -e "${GREEN}  HelmLog Configuration${NC}"
    echo -e "${GREEN}═══════════════════════════════════════${NC}"
    echo ""

    for entry in "${CONFIG_FIELDS[@]}"; do
        IFS='|' read -r var_name label is_secret <<< "$entry"
        prompt_field "$var_name" "$label" "$is_secret"
    done

    # Post-prompting validation
    local admin_email
    admin_email="$(read_config "ADMIN_EMAIL")"
    validate_email "$admin_email" || true

    local cf_hostname
    cf_hostname="$(read_config "CLOUDFLARE_HOSTNAME")"
    validate_hostname "$cf_hostname" || true

    # Auth safety check: warn if tunnel is configured but AUTH_DISABLED might be true
    local cf_token
    cf_token="$(read_config "CLOUDFLARE_TUNNEL_TOKEN")"
    if [[ -n "$cf_token" ]]; then
        echo ""
        warn "Cloudflare Tunnel is configured — the Pi will be on the public internet."
        warn "Make sure AUTH_DISABLED is NOT set to true in .env."
    fi

    echo ""
    info "Configuration saved to ${CONFIG_FILE}"
    echo ""

    # Clean up backup sentinel
    rm -f "${CONFIG_FILE}.bak.$$"
}

cmd_non_interactive() {
    ensure_config_dir

    local any_set=false
    for entry in "${CONFIG_FIELDS[@]}"; do
        IFS='|' read -r var_name label is_secret <<< "$entry"
        local env_value="${!var_name:-}"
        if [[ -n "$env_value" ]]; then
            write_config "$var_name" "$env_value"
            info "Set ${var_name} from environment"
            any_set=true
        fi
    done

    if ! $any_set; then
        info "No environment variables set — config.env unchanged."
    fi

    # Clean up backup sentinel
    rm -f "${CONFIG_FILE}.bak.$$"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

case "${1:-}" in
    --clear)
        if [[ -z "${2:-}" ]]; then
            echo "Usage: configure.sh --clear KEY" >&2
            exit 1
        fi
        cmd_clear "$2"
        ;;
    --clear-all)
        cmd_clear_all
        ;;
    --show)
        cmd_show
        ;;
    --non-interactive)
        cmd_non_interactive
        ;;
    --help|-h)
        echo "Usage: configure.sh [--non-interactive] [--clear KEY] [--clear-all] [--show]"
        echo ""
        echo "Interactive configuration wizard for HelmLog operator settings."
        echo "Settings are stored in ~/.helmlog/config.env and persist across reset-pi.sh."
        echo ""
        echo "Options:"
        echo "  (no args)          Interactive prompts for all settings"
        echo "  --non-interactive  Use environment variables only, skip prompts"
        echo "  --clear KEY        Remove a single value from config.env"
        echo "  --clear-all        Remove all values from config.env"
        echo "  --show             Display current configuration"
        ;;
    *)
        cmd_interactive
        ;;
esac
