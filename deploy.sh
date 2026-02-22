#!/usr/bin/env bash
# ============================================================================
#  Hysteria 2 VPN Server — One-Click Deploy Script for Ubuntu 24.04
#  Features: Self-signed TLS, Salamander obfuscation, PSK auth,
#            systemd service, traffic stats API, auto-update cron,
#            management commands (status/restart/uninstall/change-key/etc.)
# ============================================================================
set -euo pipefail

# ── Colours & helpers ───────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }
fatal() { err "$@"; exit 1; }

# ── Constants ───────────────────────────────────────────────────────────────
HYSTERIA_DIR="/etc/hysteria"
HYSTERIA_BIN="/usr/local/bin/hysteria"
CONFIG_FILE="${HYSTERIA_DIR}/config.yaml"
CERT_DIR="${HYSTERIA_DIR}/certs"
CERT_FILE="${CERT_DIR}/server.crt"
KEY_FILE="${CERT_DIR}/server.key"
SERVICE_NAME="hysteria-server"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
CRON_SCRIPT="/etc/cron.daily/hysteria-update"
MANAGEMENT_SCRIPT="/usr/local/bin/hysteria-manage"
LISTEN_PORT=443
STATS_PORT=9090
STATS_SECRET=""
PSK=""
OBFS_PASSWORD=""
MASQ_URL="https://www.google.com"  # fallback, not used with Salamander

# ── Pre-flight checks ──────────────────────────────────────────────────────
preflight() {
    if [[ $EUID -ne 0 ]]; then
        fatal "This script must be run as root."
    fi

    # Check Ubuntu 24.04
    if [[ -f /etc/os-release ]]; then
        . /etc/os-release
        if [[ "${ID}" != "ubuntu" ]]; then
            warn "This script is designed for Ubuntu. Detected: ${ID}. Continuing anyway..."
        fi
        if [[ "${VERSION_ID}" != "24.04" ]]; then
            warn "Designed for Ubuntu 24.04. Detected: ${VERSION_ID}. Continuing anyway..."
        fi
    else
        warn "Cannot determine OS. Continuing anyway..."
    fi

    info "Pre-flight checks passed."
}

# ── Install dependencies ───────────────────────────────────────────────────
install_deps() {
    info "Updating package lists and installing dependencies..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq curl wget openssl iptables jq cron > /dev/null 2>&1
    info "Dependencies installed."
}

# ── Download Hysteria binary ───────────────────────────────────────────────
install_hysteria() {
    if [[ -f "${HYSTERIA_BIN}" ]]; then
        local current_version
        current_version=$("${HYSTERIA_BIN}" version 2>/dev/null | head -1 || echo "unknown")
        info "Hysteria already installed: ${current_version}"
        info "Re-downloading latest version..."
    fi

    info "Downloading Hysteria 2 via official installer..."
    bash <(curl -fsSL https://get.hy2.sh/) > /dev/null 2>&1 || {
        # Fallback: direct download
        warn "Official installer failed. Trying direct download..."
        local ARCH
        ARCH=$(uname -m)
        case "${ARCH}" in
            x86_64)  ARCH="amd64" ;;
            aarch64) ARCH="arm64" ;;
            armv7l)  ARCH="arm"   ;;
            *)       fatal "Unsupported architecture: ${ARCH}" ;;
        esac
        local DOWNLOAD_URL="https://download.hysteria.network/app/latest/hysteria-linux-${ARCH}"
        curl -fsSL -o "${HYSTERIA_BIN}" "${DOWNLOAD_URL}"
        chmod +x "${HYSTERIA_BIN}"
    }

    # Verify binary
    if [[ ! -x "${HYSTERIA_BIN}" ]]; then
        # The official installer might place the binary elsewhere
        local alt_bin
        alt_bin=$(which hysteria 2>/dev/null || true)
        if [[ -n "${alt_bin}" && -x "${alt_bin}" ]]; then
            HYSTERIA_BIN="${alt_bin}"
        else
            fatal "Hysteria binary not found after installation."
        fi
    fi

    info "Hysteria installed: $("${HYSTERIA_BIN}" version 2>/dev/null | head -1 || echo 'OK')"
}

# ── Generate secrets ───────────────────────────────────────────────────────
generate_secrets() {
    PSK=$(openssl rand -hex 16)
    OBFS_PASSWORD=$(openssl rand -hex 16)
    STATS_SECRET=$(openssl rand -hex 16)
    info "Generated PSK auth key, obfuscation password, and stats secret."
}

# ── Generate self-signed certificate ───────────────────────────────────────
generate_cert() {
    mkdir -p "${CERT_DIR}"

    if [[ -f "${CERT_FILE}" && -f "${KEY_FILE}" ]]; then
        info "Existing certificates found. Regenerating..."
    fi

    info "Generating self-signed TLS certificate (10 years)..."
    local SERVER_IP
    SERVER_IP=$(curl -4 -fsSL ifconfig.me 2>/dev/null || curl -4 -fsSL icanhazip.com 2>/dev/null || echo "127.0.0.1")

    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
        -days 3650 -nodes \
        -keyout "${KEY_FILE}" \
        -out "${CERT_FILE}" \
        -subj "/CN=hysteria-server" \
        -addext "subjectAltName=IP:${SERVER_IP}" \
        2>/dev/null

    chmod 600 "${KEY_FILE}"
    chmod 644 "${CERT_FILE}"
    info "Certificate generated for IP: ${SERVER_IP}"
}

# ── Write Hysteria config ──────────────────────────────────────────────────
write_config() {
    mkdir -p "${HYSTERIA_DIR}"

    cat > "${CONFIG_FILE}" <<YAML
# Hysteria 2 Server Configuration
# Auto-generated by deploy.sh

listen: :${LISTEN_PORT}

tls:
  cert: ${CERT_FILE}
  key: ${KEY_FILE}

auth:
  type: password
  password: ${PSK}

obfs:
  type: salamander
  salamander:
    password: ${OBFS_PASSWORD}

# QUIC performance tuning
quic:
  initStreamReceiveWindow: 16777216
  maxStreamReceiveWindow: 16777216
  initConnReceiveWindow: 33554432
  maxConnReceiveWindow: 33554432
  maxIdleTimeout: 90s
  maxIncomingStreams: 2048
  disablePathMTUDiscovery: false

# No bandwidth limits — unlimited speed
# bandwidth:
#   up: 0
#   down: 0
ignoreClientBandwidth: false

# Traffic Stats API
trafficStats:
  listen: 127.0.0.1:${STATS_PORT}
  secret: ${STATS_SECRET}

# Outbound — direct with BBR-like behavior
outbounds:
  - name: default
    type: direct
    direct:
      mode: auto
YAML

    chmod 600 "${CONFIG_FILE}"
    info "Configuration written to ${CONFIG_FILE}"
}

# ── Firewall rules ─────────────────────────────────────────────────────────
setup_firewall() {
    info "Configuring firewall rules..."

    # Enable IP forwarding
    sysctl -w net.ipv4.ip_forward=1 > /dev/null 2>&1
    sysctl -w net.ipv6.conf.all.forwarding=1 > /dev/null 2>&1

    # Persist IP forwarding
    if ! grep -q "^net.ipv4.ip_forward=1" /etc/sysctl.conf 2>/dev/null; then
        echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf
    fi
    if ! grep -q "^net.ipv6.conf.all.forwarding=1" /etc/sysctl.conf 2>/dev/null; then
        echo "net.ipv6.conf.all.forwarding=1" >> /etc/sysctl.conf
    fi

    # Allow Hysteria port (UDP)
    iptables -C INPUT -p udp --dport "${LISTEN_PORT}" -j ACCEPT 2>/dev/null || \
        iptables -I INPUT 1 -p udp --dport "${LISTEN_PORT}" -j ACCEPT

    # Allow established connections
    iptables -C INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || \
        iptables -I INPUT 1 -m state --state ESTABLISHED,RELATED -j ACCEPT

    # IPv6 rules
    ip6tables -C INPUT -p udp --dport "${LISTEN_PORT}" -j ACCEPT 2>/dev/null || \
        ip6tables -I INPUT 1 -p udp --dport "${LISTEN_PORT}" -j ACCEPT 2>/dev/null || true

    # Persist iptables rules if iptables-persistent is available
    if command -v netfilter-persistent &>/dev/null; then
        netfilter-persistent save 2>/dev/null || true
    fi

    # Optimize network buffers for QUIC
    sysctl -w net.core.rmem_max=16777216 > /dev/null 2>&1
    sysctl -w net.core.wmem_max=16777216 > /dev/null 2>&1
    sysctl -w net.core.rmem_default=1048576 > /dev/null 2>&1
    sysctl -w net.core.wmem_default=1048576 > /dev/null 2>&1

    # Persist buffer settings
    for param in "net.core.rmem_max=16777216" "net.core.wmem_max=16777216" \
                 "net.core.rmem_default=1048576" "net.core.wmem_default=1048576"; do
        local key="${param%%=*}"
        if ! grep -q "^${key}" /etc/sysctl.conf 2>/dev/null; then
            echo "${param}" >> /etc/sysctl.conf
        fi
    done

    info "Firewall and network optimizations applied."
}

# ── Systemd service ────────────────────────────────────────────────────────
setup_service() {
    info "Creating systemd service..."

    # Stop any existing official hysteria services to avoid port conflicts
    systemctl stop hysteria-server.service 2>/dev/null || true
    systemctl disable hysteria-server.service 2>/dev/null || true

    cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=Hysteria 2 VPN Server
Documentation=https://hysteria.network/
After=network.target network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=${HYSTERIA_BIN} server --config ${CONFIG_FILE}
Restart=always
RestartSec=5
StartLimitIntervalSec=60
StartLimitBurst=10
LimitNOFILE=65535
LimitNPROC=65535

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${HYSTERIA_DIR}
PrivateTmp=true

# Watchdog
WatchdogSec=30

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hysteria

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}" > /dev/null 2>&1
    systemctl restart "${SERVICE_NAME}"

    # Wait and verify
    sleep 2
    if systemctl is-active --quiet "${SERVICE_NAME}"; then
        info "Hysteria service started successfully."
    else
        err "Service failed to start. Checking logs..."
        journalctl -u "${SERVICE_NAME}" --no-pager -n 20
        fatal "Service startup failed. See logs above."
    fi
}

# ── Auto-update cron job ───────────────────────────────────────────────────
setup_autoupdate() {
    info "Setting up auto-update cron job..."

    cat > "${CRON_SCRIPT}" <<'CRONEOF'
#!/usr/bin/env bash
# Auto-update Hysteria 2 binary (runs daily)
set -euo pipefail
LOG="/var/log/hysteria-update.log"

exec >> "${LOG}" 2>&1
echo "=== Update check: $(date) ==="

CURRENT=$(/usr/local/bin/hysteria version 2>/dev/null | head -1 || echo "unknown")

# Download latest
TMPBIN=$(mktemp)
bash <(curl -fsSL https://get.hy2.sh/) > /dev/null 2>&1 || {
    echo "Update download failed, keeping current version."
    rm -f "${TMPBIN}"
    exit 0
}

NEW=$(/usr/local/bin/hysteria version 2>/dev/null | head -1 || echo "unknown")

if [[ "${CURRENT}" != "${NEW}" ]]; then
    echo "Updated: ${CURRENT} -> ${NEW}"
    systemctl restart hysteria-server 2>/dev/null || true
    echo "Service restarted."
else
    echo "Already up to date: ${CURRENT}"
fi
CRONEOF

    chmod +x "${CRON_SCRIPT}"
    info "Auto-update cron job installed at ${CRON_SCRIPT}"
}

# ── Management script ──────────────────────────────────────────────────────
create_management_script() {
    info "Creating management script..."

    cat > "${MANAGEMENT_SCRIPT}" <<'MGMTEOF'
#!/usr/bin/env bash
# Hysteria 2 VPN Management Script
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

CONFIG="/etc/hysteria/config.yaml"
SERVICE="hysteria-server"
CERT_DIR="/etc/hysteria/certs"

usage() {
    echo -e "${BOLD}Hysteria 2 VPN Management${NC}"
    echo ""
    echo "Usage: hysteria-manage <command>"
    echo ""
    echo "Commands:"
    echo "  status          Show service status"
    echo "  start           Start the service"
    echo "  stop            Stop the service"
    echo "  restart         Restart the service"
    echo "  logs [N]        Show last N log lines (default 50)"
    echo "  change-key      Generate and set a new auth key"
    echo "  change-obfs     Generate and set a new obfuscation password"
    echo "  show-config     Display current server config"
    echo "  show-client     Show client connection config"
    echo "  stats           Show traffic stats (active connections)"
    echo "  update          Update Hysteria to latest version"
    echo "  uninstall       Remove Hysteria completely"
    echo ""
}

cmd_status() {
    echo -e "${BOLD}=== Hysteria Service Status ===${NC}"
    systemctl status "${SERVICE}" --no-pager 2>/dev/null || echo -e "${RED}Service not found${NC}"
    echo ""
    echo -e "${BOLD}=== Port Listening ===${NC}"
    ss -ulnp | grep -E ":(443|${LISTEN_PORT:-443})\b" || echo "No listening ports found."
}

cmd_logs() {
    local lines="${1:-50}"
    journalctl -u "${SERVICE}" --no-pager -n "${lines}"
}

cmd_change_key() {
    local new_key
    new_key=$(openssl rand -hex 16)
    sed -i "s/^  password: .*/  password: ${new_key}/" "${CONFIG}"
    echo -e "${GREEN}New auth key: ${BOLD}${new_key}${NC}"
    echo -e "${YELLOW}Restarting service...${NC}"
    systemctl restart "${SERVICE}"
    echo -e "${GREEN}Done. Update this key on all clients.${NC}"
}

cmd_change_obfs() {
    local new_obfs
    new_obfs=$(openssl rand -hex 16)
    # Update the salamander password (the second 'password:' under obfs section)
    python3 -c "
import re, sys
with open('${CONFIG}', 'r') as f:
    content = f.read()
# Replace salamander password
content = re.sub(
    r'(salamander:\s*\n\s*password:\s*)(\S+)',
    r'\g<1>${new_obfs}',
    content
)
with open('${CONFIG}', 'w') as f:
    f.write(content)
" 2>/dev/null || {
    # Fallback if python3 not available
    sed -i '/salamander:/,/password:/{s/password: .*/password: '"${new_obfs}"'/}' "${CONFIG}"
    }
    echo -e "${GREEN}New obfuscation password: ${BOLD}${new_obfs}${NC}"
    echo -e "${YELLOW}Restarting service...${NC}"
    systemctl restart "${SERVICE}"
    echo -e "${GREEN}Done. Update this password on all clients.${NC}"
}

cmd_show_config() {
    echo -e "${BOLD}=== Server Configuration ===${NC}"
    cat "${CONFIG}"
}

cmd_show_client() {
    local server_ip
    server_ip=$(curl -4 -fsSL ifconfig.me 2>/dev/null || curl -4 -fsSL icanhazip.com 2>/dev/null || echo "YOUR_SERVER_IP")

    local auth_key obfs_pass port
    auth_key=$(grep -A1 "^auth:" "${CONFIG}" | grep "password:" | head -1 | awk '{print $2}')
    obfs_pass=$(grep -A2 "salamander:" "${CONFIG}" | grep "password:" | tail -1 | awk '{print $2}')
    port=$(grep "^listen:" "${CONFIG}" | sed 's/listen: ://' | tr -d '[:space:]')
    [[ -z "${port}" ]] && port="443"

    echo -e "${BOLD}=== Client Configuration ===${NC}"
    echo ""
    echo -e "${CYAN}Server IP:${NC}       ${server_ip}"
    echo -e "${CYAN}Port:${NC}            ${port} (UDP)"
    echo -e "${CYAN}Auth key:${NC}        ${auth_key}"
    echo -e "${CYAN}Obfs type:${NC}       salamander"
    echo -e "${CYAN}Obfs password:${NC}   ${obfs_pass}"
    echo -e "${CYAN}TLS:${NC}             insecure (self-signed cert)"
    echo ""
    echo -e "${BOLD}=== Client config.yaml ===${NC}"
    cat <<CLIENT_YAML

server: ${server_ip}:${port}

auth: ${auth_key}

tls:
  insecure: true

obfs:
  type: salamander
  salamander:
    password: ${obfs_pass}

# Optional: set bandwidth for Brutal congestion control
# bandwidth:
#   up: 100 mbps
#   down: 100 mbps

socks5:
  listen: 127.0.0.1:1080

http:
  listen: 127.0.0.1:8080

CLIENT_YAML
    echo ""
    echo -e "${BOLD}=== Connection URI (for mobile apps) ===${NC}"
    local uri="hy2://${auth_key}@${server_ip}:${port}?obfs=salamander&obfs-password=${obfs_pass}&insecure=1#Hysteria2-VPN"
    echo -e "${GREEN}${uri}${NC}"
    echo ""
    echo -e "${YELLOW}Copy the URI above into Hysteria Android/iOS app.${NC}"
}

cmd_stats() {
    local secret
    secret=$(grep -A1 "trafficStats:" "${CONFIG}" | grep "secret:" | awk '{print $2}')
    local stats_port
    stats_port=$(grep -A1 "trafficStats:" "${CONFIG}" | grep "listen:" | sed 's/.*://' | tr -d '[:space:]')

    if [[ -z "${secret}" ]]; then
        echo -e "${RED}Traffic stats API not configured.${NC}"
        return 1
    fi

    echo -e "${BOLD}=== Active Connections ===${NC}"
    curl -s "http://127.0.0.1:${stats_port}/traffic?secret=${secret}" 2>/dev/null | jq . 2>/dev/null || \
        echo -e "${YELLOW}No data available or service not running.${NC}"

    echo ""
    echo -e "${BOLD}=== Online Users ===${NC}"
    curl -s "http://127.0.0.1:${stats_port}/online?secret=${secret}" 2>/dev/null | jq . 2>/dev/null || \
        echo -e "${YELLOW}No data available or service not running.${NC}"
}

cmd_update() {
    echo -e "${YELLOW}Updating Hysteria...${NC}"
    local current
    current=$(/usr/local/bin/hysteria version 2>/dev/null | head -1 || echo "unknown")
    bash <(curl -fsSL https://get.hy2.sh/) || {
        echo -e "${RED}Update failed.${NC}"
        return 1
    }
    local new_ver
    new_ver=$(/usr/local/bin/hysteria version 2>/dev/null | head -1 || echo "unknown")
    echo -e "${GREEN}Updated: ${current} -> ${new_ver}${NC}"
    echo -e "${YELLOW}Restarting service...${NC}"
    systemctl restart "${SERVICE}"
    echo -e "${GREEN}Done.${NC}"
}

cmd_uninstall() {
    echo -e "${RED}${BOLD}WARNING: This will completely remove Hysteria 2 VPN.${NC}"
    read -rp "Are you sure? (yes/no): " confirm
    if [[ "${confirm}" != "yes" ]]; then
        echo "Aborted."
        return
    fi

    echo "Stopping service..."
    systemctl stop "${SERVICE}" 2>/dev/null || true
    systemctl disable "${SERVICE}" 2>/dev/null || true

    echo "Removing files..."
    rm -f "/etc/systemd/system/${SERVICE}.service"
    rm -rf /etc/hysteria
    rm -f /usr/local/bin/hysteria
    rm -f /usr/local/bin/hysteria-manage
    rm -f /etc/cron.daily/hysteria-update

    systemctl daemon-reload

    echo -e "${GREEN}Hysteria 2 has been completely removed.${NC}"
}

# ── Main ────────────────────────────────────────────────────────────────────
case "${1:-}" in
    status)      cmd_status ;;
    start)       systemctl start "${SERVICE}" && echo -e "${GREEN}Started.${NC}" ;;
    stop)        systemctl stop "${SERVICE}" && echo -e "${YELLOW}Stopped.${NC}" ;;
    restart)     systemctl restart "${SERVICE}" && echo -e "${GREEN}Restarted.${NC}" ;;
    logs)        cmd_logs "${2:-50}" ;;
    change-key)  cmd_change_key ;;
    change-obfs) cmd_change_obfs ;;
    show-config) cmd_show_config ;;
    show-client) cmd_show_client ;;
    stats)       cmd_stats ;;
    update)      cmd_update ;;
    uninstall)   cmd_uninstall ;;
    *)           usage ;;
esac
MGMTEOF

    chmod +x "${MANAGEMENT_SCRIPT}"
    info "Management script installed: ${MANAGEMENT_SCRIPT}"
}

# ── Print summary ──────────────────────────────────────────────────────────
print_summary() {
    local server_ip
    server_ip=$(curl -4 -fsSL ifconfig.me 2>/dev/null || curl -4 -fsSL icanhazip.com 2>/dev/null || echo "YOUR_SERVER_IP")

    echo ""
    echo -e "${BOLD}╔══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}║           Hysteria 2 VPN — Deployment Complete!             ║${NC}"
    echo -e "${BOLD}╚══════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "${CYAN}Server IP:${NC}            ${server_ip}"
    echo -e "${CYAN}Port:${NC}                 ${LISTEN_PORT} (UDP)"
    echo -e "${CYAN}Auth Key (PSK):${NC}       ${PSK}"
    echo -e "${CYAN}Obfs Type:${NC}            Salamander"
    echo -e "${CYAN}Obfs Password:${NC}        ${OBFS_PASSWORD}"
    echo -e "${CYAN}TLS:${NC}                  Self-signed (insecure: true on client)"
    echo -e "${CYAN}Stats API:${NC}            http://127.0.0.1:${STATS_PORT}"
    echo -e "${CYAN}Stats Secret:${NC}         ${STATS_SECRET}"
    echo ""
    echo -e "${BOLD}── Connection URI (for mobile apps) ──${NC}"
    local uri="hy2://${PSK}@${server_ip}:${LISTEN_PORT}?obfs=salamander&obfs-password=${OBFS_PASSWORD}&insecure=1#Hysteria2-VPN"
    echo -e "${GREEN}${uri}${NC}"
    echo ""
    echo -e "${BOLD}── Client config.yaml ──${NC}"
    echo ""
    echo "server: ${server_ip}:${LISTEN_PORT}"
    echo ""
    echo "auth: ${PSK}"
    echo ""
    echo "tls:"
    echo "  insecure: true"
    echo ""
    echo "obfs:"
    echo "  type: salamander"
    echo "  salamander:"
    echo "    password: ${OBFS_PASSWORD}"
    echo ""
    echo "socks5:"
    echo "  listen: 127.0.0.1:1080"
    echo ""
    echo "http:"
    echo "  listen: 127.0.0.1:8080"
    echo ""
    echo -e "${BOLD}── Management ──${NC}"
    echo -e "  ${GREEN}hysteria-manage status${NC}       — service status"
    echo -e "  ${GREEN}hysteria-manage show-client${NC}  — show client config & URI"
    echo -e "  ${GREEN}hysteria-manage logs${NC}         — view logs"
    echo -e "  ${GREEN}hysteria-manage stats${NC}        — traffic statistics"
    echo -e "  ${GREEN}hysteria-manage change-key${NC}   — rotate auth key"
    echo -e "  ${GREEN}hysteria-manage change-obfs${NC}  — rotate obfs password"
    echo -e "  ${GREEN}hysteria-manage update${NC}       — update Hysteria"
    echo -e "  ${GREEN}hysteria-manage restart${NC}      — restart service"
    echo -e "  ${GREEN}hysteria-manage uninstall${NC}    — remove everything"
    echo ""
    echo -e "${BOLD}── Important ──${NC}"
    echo -e "  ${YELLOW}• Save the Auth Key and Obfs Password — you need them for clients${NC}"
    echo -e "  ${YELLOW}• One key = unlimited devices${NC}"
    echo -e "  ${YELLOW}• No traffic limits applied${NC}"
    echo -e "  ${YELLOW}• Service auto-restarts on crash (systemd + watchdog)${NC}"
    echo -e "  ${YELLOW}• Auto-updates daily via cron${NC}"
    echo ""

    # Save credentials to file
    cat > "${HYSTERIA_DIR}/credentials.txt" <<CREDS
# Hysteria 2 VPN Credentials
# Generated: $(date)

Server IP:       ${server_ip}
Port:            ${LISTEN_PORT} (UDP)
Auth Key:        ${PSK}
Obfs Type:       Salamander
Obfs Password:   ${OBFS_PASSWORD}
Stats API:       http://127.0.0.1:${STATS_PORT}
Stats Secret:    ${STATS_SECRET}

Connection URI:
${uri}
CREDS
    chmod 600 "${HYSTERIA_DIR}/credentials.txt"
    info "Credentials saved to ${HYSTERIA_DIR}/credentials.txt"
}

# ── Main ────────────────────────────────────────────────────────────────────
main() {
    echo ""
    echo -e "${BOLD}╔══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}║        Hysteria 2 VPN Server — One-Click Deployment         ║${NC}"
    echo -e "${BOLD}╚══════════════════════════════════════════════════════════════╝${NC}"
    echo ""

    preflight
    install_deps
    install_hysteria
    generate_secrets
    generate_cert
    write_config
    setup_firewall
    setup_service
    setup_autoupdate
    create_management_script
    print_summary
}

main "$@"
