#!/usr/bin/env bash
# ============================================================================
#  Hysteria 2 VPN Server — One-Click Deploy Script for Ubuntu 24.04
#  Features: Self-signed TLS, Salamander obfuscation, HTTP auth backend,
#            multi-key via Telegram bot, systemd services, traffic stats API,
#            auto-update cron, management commands
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
BOT_DIR="${HYSTERIA_DIR}/bot"
BOT_SERVICE_NAME="hysteria-bot"
BOT_SERVICE_FILE="/etc/systemd/system/${BOT_SERVICE_NAME}.service"
BOT_CONFIG_FILE="${BOT_DIR}/bot.json"
CRON_SCRIPT="/etc/cron.daily/hysteria-update"
MANAGEMENT_SCRIPT="/usr/local/bin/hysteria-manage"
LISTEN_PORT=443
STATS_PORT=9090
AUTH_BACKEND_PORT=8787
STATS_SECRET=""
OBFS_PASSWORD=""
BOT_TOKEN=""
ADMIN_PASSWORD=""
SERVER_IP=""

# ── Pre-flight checks ──────────────────────────────────────────────────────
preflight() {
    if [[ $EUID -ne 0 ]]; then
        fatal "This script must be run as root."
    fi

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

# ── Ask for Telegram bot config ────────────────────────────────────────────
ask_bot_config() {
    echo ""
    echo -e "${BOLD}── Telegram Bot Configuration ──${NC}"
    echo ""

    while [[ -z "${BOT_TOKEN}" ]]; do
        read -rp "$(echo -e "${CYAN}Telegram Bot Token${NC} (from @BotFather): ")" BOT_TOKEN
        if [[ -z "${BOT_TOKEN}" ]]; then
            err "Bot token cannot be empty."
        fi
    done

    while [[ -z "${ADMIN_PASSWORD}" ]]; do
        read -rp "$(echo -e "${CYAN}Admin password${NC} (for bot access): ")" ADMIN_PASSWORD
        if [[ -z "${ADMIN_PASSWORD}" ]]; then
            err "Admin password cannot be empty."
        fi
    done

    info "Bot configuration received."
}

# ── Install dependencies ───────────────────────────────────────────────────
install_deps() {
    info "Updating package lists and installing dependencies..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq curl wget openssl iptables jq cron \
        python3 python3-pip python3-venv > /dev/null 2>&1
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

    if [[ ! -x "${HYSTERIA_BIN}" ]]; then
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
    OBFS_PASSWORD=$(openssl rand -hex 16)
    STATS_SECRET=$(openssl rand -hex 16)
    info "Generated obfuscation password and stats secret."
}

# ── Detect server IP ──────────────────────────────────────────────────────
detect_server_ip() {
    SERVER_IP=$(curl -4 -fsSL ifconfig.me 2>/dev/null || curl -4 -fsSL icanhazip.com 2>/dev/null || echo "127.0.0.1")
    info "Server IP detected: ${SERVER_IP}"
}

# ── Generate self-signed certificate ───────────────────────────────────────
generate_cert() {
    mkdir -p "${CERT_DIR}"

    if [[ -f "${CERT_FILE}" && -f "${KEY_FILE}" ]]; then
        info "Existing certificates found. Regenerating..."
    fi

    info "Generating self-signed TLS certificate (10 years)..."

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

# ── Write Hysteria config (HTTP auth backend) ─────────────────────────────
write_config() {
    mkdir -p "${HYSTERIA_DIR}"

    cat > "${CONFIG_FILE}" <<YAML
# Hysteria 2 Server Configuration
# Auth via HTTP backend (Telegram bot manages keys)

listen: :${LISTEN_PORT}

tls:
  cert: ${CERT_FILE}
  key: ${KEY_FILE}

auth:
  type: http
  http:
    url: http://127.0.0.1:${AUTH_BACKEND_PORT}/auth
    insecure: false

obfs:
  type: salamander
  salamander:
    password: ${OBFS_PASSWORD}

# QUIC tuning — optimized for low latency
quic:
  initStreamReceiveWindow: 2097152
  maxStreamReceiveWindow: 4194304
  initConnReceiveWindow: 4194304
  maxConnReceiveWindow: 8388608
  maxIdleTimeout: 30s
  maxIncomingStreams: 1024
  disablePathMTUDiscovery: false

# Let server decide bandwidth — avoids Brutal without client bandwidth set
ignoreClientBandwidth: true

# Traffic Stats API
trafficStats:
  listen: 127.0.0.1:${STATS_PORT}
  secret: ${STATS_SECRET}

# Outbound — direct
outbounds:
  - name: default
    type: direct
    direct:
      mode: auto
YAML

    chmod 600 "${CONFIG_FILE}"
    info "Configuration written to ${CONFIG_FILE}"
}

# ── Install Telegram bot ──────────────────────────────────────────────────
install_bot() {
    info "Installing Telegram bot..."

    mkdir -p "${BOT_DIR}"

    # Copy bot source files
    local SCRIPT_DIR
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

    if [[ -d "${SCRIPT_DIR}/bot" ]]; then
        cp "${SCRIPT_DIR}/bot/"*.py "${BOT_DIR}/"
        cp "${SCRIPT_DIR}/bot/requirements.txt" "${BOT_DIR}/"
        info "Bot files copied from ${SCRIPT_DIR}/bot/"
    else
        fatal "Bot source directory not found at ${SCRIPT_DIR}/bot/. Ensure bot/ folder is alongside deploy.sh"
    fi

    # Create Python virtual environment
    info "Setting up Python virtual environment..."
    python3 -m venv "${BOT_DIR}/venv"
    "${BOT_DIR}/venv/bin/pip" install --upgrade pip > /dev/null 2>&1
    "${BOT_DIR}/venv/bin/pip" install -r "${BOT_DIR}/requirements.txt" > /dev/null 2>&1
    info "Python dependencies installed."

    # Write bot config
    cat > "${BOT_CONFIG_FILE}" <<BOTJSON
{
  "bot_token": "${BOT_TOKEN}",
  "admin_password": "${ADMIN_PASSWORD}",
  "auth_backend_port": ${AUTH_BACKEND_PORT},
  "hysteria_config": "${CONFIG_FILE}",
  "hysteria_service": "${SERVICE_NAME}",
  "stats_listen": "127.0.0.1:${STATS_PORT}",
  "stats_secret": "${STATS_SECRET}",
  "server_ip": "${SERVER_IP}",
  "server_port": ${LISTEN_PORT},
  "obfs_password": "${OBFS_PASSWORD}",
  "log_lines": 50
}
BOTJSON

    chmod 600 "${BOT_CONFIG_FILE}"
    info "Bot configuration saved to ${BOT_CONFIG_FILE}"
}

# ── Firewall rules ─────────────────────────────────────────────────────────
setup_firewall() {
    info "Configuring firewall rules..."

    sysctl -w net.ipv4.ip_forward=1 > /dev/null 2>&1
    sysctl -w net.ipv6.conf.all.forwarding=1 > /dev/null 2>&1

    if ! grep -q "^net.ipv4.ip_forward=1" /etc/sysctl.conf 2>/dev/null; then
        echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf
    fi
    if ! grep -q "^net.ipv6.conf.all.forwarding=1" /etc/sysctl.conf 2>/dev/null; then
        echo "net.ipv6.conf.all.forwarding=1" >> /etc/sysctl.conf
    fi

    iptables -C INPUT -p udp --dport "${LISTEN_PORT}" -j ACCEPT 2>/dev/null || \
        iptables -I INPUT 1 -p udp --dport "${LISTEN_PORT}" -j ACCEPT

    iptables -C INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || \
        iptables -I INPUT 1 -m state --state ESTABLISHED,RELATED -j ACCEPT

    ip6tables -C INPUT -p udp --dport "${LISTEN_PORT}" -j ACCEPT 2>/dev/null || \
        ip6tables -I INPUT 1 -p udp --dport "${LISTEN_PORT}" -j ACCEPT 2>/dev/null || true

    if command -v netfilter-persistent &>/dev/null; then
        netfilter-persistent save 2>/dev/null || true
    fi

    # ── Kernel-level network optimizations for QUIC/UDP ──

    # Enable BBR congestion control (reduces bufferbloat, lowers latency)
    modprobe tcp_bbr 2>/dev/null || true

    # Create optimized sysctl config
    cat > /etc/sysctl.d/99-hysteria.conf <<'SYSCTL'
# === Hysteria 2 QUIC/UDP Optimizations ===

# BBR congestion control — critical for low latency
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr

# UDP/QUIC buffer sizes (moderate — avoids bufferbloat)
net.core.rmem_max = 8388608
net.core.wmem_max = 8388608
net.core.rmem_default = 524288
net.core.wmem_default = 524288

# TCP buffer auto-tuning (also helps QUIC stack)
net.ipv4.tcp_rmem = 4096 524288 8388608
net.ipv4.tcp_wmem = 4096 524288 8388608

# Increase network backlog for high throughput
net.core.netdev_max_backlog = 4096
net.core.somaxconn = 4096

# Conntrack optimization — prevent drops under load
net.netfilter.nf_conntrack_max = 131072
net.netfilter.nf_conntrack_udp_timeout = 60
net.netfilter.nf_conntrack_udp_timeout_stream = 120

# IP forwarding
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1

# Reduce TIME_WAIT sockets
net.ipv4.tcp_fin_timeout = 15
net.ipv4.tcp_tw_reuse = 1

# Faster keepalive for dead connection cleanup
net.ipv4.tcp_keepalive_time = 300
net.ipv4.tcp_keepalive_intvl = 15
net.ipv4.tcp_keepalive_probes = 5

# Disable slow start after idle — keeps connections fast
net.ipv4.tcp_slow_start_after_idle = 0

# Enable MTU probing — helps on paths with non-standard MTU
net.ipv4.tcp_mtu_probing = 1

# Increase file descriptors
fs.file-max = 1048576
SYSCTL

    sysctl --system > /dev/null 2>&1

    info "Firewall and kernel optimizations applied (BBR, UDP buffers, conntrack)."
}

# ── Systemd: Hysteria service ─────────────────────────────────────────────
setup_hysteria_service() {
    info "Creating Hysteria systemd service..."

    systemctl stop hysteria-server.service 2>/dev/null || true
    systemctl disable hysteria-server.service 2>/dev/null || true

    cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=Hysteria 2 VPN Server
Documentation=https://hysteria.network/
After=network.target network-online.target ${BOT_SERVICE_NAME}.service
Wants=network-online.target
Requires=${BOT_SERVICE_NAME}.service

[Service]
Type=simple
ExecStartPre=/bin/sleep 2
ExecStart=${HYSTERIA_BIN} server --config ${CONFIG_FILE}
Restart=always
RestartSec=5
StartLimitIntervalSec=60
StartLimitBurst=10
LimitNOFILE=65535
LimitNPROC=65535

NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${HYSTERIA_DIR}
PrivateTmp=true

WatchdogSec=30

StandardOutput=journal
StandardError=journal
SyslogIdentifier=hysteria

[Install]
WantedBy=multi-user.target
EOF

    info "Hysteria service file created."
}

# ── Systemd: Bot service ──────────────────────────────────────────────────
setup_bot_service() {
    info "Creating Telegram bot systemd service..."

    cat > "${BOT_SERVICE_FILE}" <<EOF
[Unit]
Description=Hysteria 2 Telegram Bot & Auth Backend
After=network.target network-online.target
Wants=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=15

[Service]
Type=simple
WorkingDirectory=${BOT_DIR}
Environment=HYSTERIA_BOT_CONFIG=${BOT_CONFIG_FILE}
Environment=HYSTERIA_BOT_DATA=${BOT_DIR}
ExecStart=${BOT_DIR}/venv/bin/python3 ${BOT_DIR}/bot.py
Restart=always
RestartSec=3
LimitNOFILE=65535

NoNewPrivileges=true
ProtectHome=true
ReadWritePaths=${HYSTERIA_DIR}
PrivateTmp=true

StandardOutput=journal
StandardError=journal
SyslogIdentifier=hysteria-bot

[Install]
WantedBy=multi-user.target
EOF

    info "Bot service file created."
}

# ── Start services ────────────────────────────────────────────────────────
start_services() {
    info "Starting services..."

    systemctl daemon-reload

    # Start bot first (auth backend must be up before Hysteria)
    systemctl enable "${BOT_SERVICE_NAME}" > /dev/null 2>&1
    systemctl restart "${BOT_SERVICE_NAME}"

    sleep 3

    if systemctl is-active --quiet "${BOT_SERVICE_NAME}"; then
        info "Bot service started successfully."
    else
        err "Bot service failed to start. Checking logs..."
        journalctl -u "${BOT_SERVICE_NAME}" --no-pager -n 20
        fatal "Bot service startup failed. Check your BOT_TOKEN."
    fi

    # Start Hysteria
    systemctl enable "${SERVICE_NAME}" > /dev/null 2>&1
    systemctl restart "${SERVICE_NAME}"

    sleep 2

    if systemctl is-active --quiet "${SERVICE_NAME}"; then
        info "Hysteria service started successfully."
    else
        err "Hysteria service failed to start. Checking logs..."
        journalctl -u "${SERVICE_NAME}" --no-pager -n 20
        fatal "Hysteria service startup failed. See logs above."
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

bash <(curl -fsSL https://get.hy2.sh/) > /dev/null 2>&1 || {
    echo "Update download failed, keeping current version."
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
BOT_SERVICE="hysteria-bot"
BOT_DIR="/etc/hysteria/bot"

usage() {
    echo -e "${BOLD}Hysteria 2 VPN Management${NC}"
    echo ""
    echo "Usage: hysteria-manage <command>"
    echo ""
    echo "Commands:"
    echo "  status          Show all services status"
    echo "  start           Start all services"
    echo "  stop            Stop all services"
    echo "  restart         Restart all services"
    echo "  logs [N]        Show last N Hysteria log lines (default 50)"
    echo "  bot-logs [N]    Show last N bot log lines (default 50)"
    echo "  show-config     Display server config"
    echo "  stats           Show traffic stats"
    echo "  update          Update Hysteria to latest version"
    echo "  uninstall       Remove everything"
    echo ""
}

cmd_status() {
    echo -e "${BOLD}=== Hysteria Service ===${NC}"
    systemctl status "${SERVICE}" --no-pager 2>/dev/null || echo -e "${RED}Not found${NC}"
    echo ""
    echo -e "${BOLD}=== Bot Service ===${NC}"
    systemctl status "${BOT_SERVICE}" --no-pager 2>/dev/null || echo -e "${RED}Not found${NC}"
    echo ""
    echo -e "${BOLD}=== Port Listening ===${NC}"
    ss -ulnp | grep ":443\b" 2>/dev/null || echo "No UDP listeners on 443"
    ss -tlnp | grep ":8787\b" 2>/dev/null || echo "No TCP listeners on 8787 (auth backend)"
}

cmd_logs() {
    local lines="${1:-50}"
    journalctl -u "${SERVICE}" --no-pager -n "${lines}"
}

cmd_bot_logs() {
    local lines="${1:-50}"
    journalctl -u "${BOT_SERVICE}" --no-pager -n "${lines}"
}

cmd_show_config() {
    echo -e "${BOLD}=== Server Configuration ===${NC}"
    cat "${CONFIG}"
}

cmd_stats() {
    local secret stats_port
    secret=$(python3 -c "import json; print(json.load(open('${BOT_DIR}/bot.json'))['stats_secret'])" 2>/dev/null || echo "")
    stats_port=$(python3 -c "import json; d=json.load(open('${BOT_DIR}/bot.json')); print(d.get('stats_listen','127.0.0.1:9090').split(':')[1])" 2>/dev/null || echo "9090")

    if [[ -z "${secret}" ]]; then
        echo -e "${RED}Stats secret not found.${NC}"
        return 1
    fi

    echo -e "${BOLD}=== Active Connections ===${NC}"
    curl -s "http://127.0.0.1:${stats_port}/traffic?secret=${secret}" 2>/dev/null | jq . 2>/dev/null || \
        echo -e "${YELLOW}No data or service not running.${NC}"

    echo ""
    echo -e "${BOLD}=== Online Users ===${NC}"
    curl -s "http://127.0.0.1:${stats_port}/online?secret=${secret}" 2>/dev/null | jq . 2>/dev/null || \
        echo -e "${YELLOW}No data or service not running.${NC}"
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
    systemctl restart "${SERVICE}"
    echo -e "${GREEN}Done.${NC}"
}

cmd_uninstall() {
    echo -e "${RED}${BOLD}WARNING: This will completely remove Hysteria 2 VPN + Bot.${NC}"
    read -rp "Are you sure? (yes/no): " confirm
    if [[ "${confirm}" != "yes" ]]; then
        echo "Aborted."
        return
    fi

    echo "Stopping services..."
    systemctl stop "${SERVICE}" 2>/dev/null || true
    systemctl stop "${BOT_SERVICE}" 2>/dev/null || true
    systemctl disable "${SERVICE}" 2>/dev/null || true
    systemctl disable "${BOT_SERVICE}" 2>/dev/null || true

    echo "Removing files..."
    rm -f "/etc/systemd/system/${SERVICE}.service"
    rm -f "/etc/systemd/system/${BOT_SERVICE}.service"
    rm -rf /etc/hysteria
    rm -f /usr/local/bin/hysteria
    rm -f /usr/local/bin/hysteria-manage
    rm -f /etc/cron.daily/hysteria-update

    systemctl daemon-reload

    echo -e "${GREEN}Hysteria 2 + Bot completely removed.${NC}"
}

case "${1:-}" in
    status)      cmd_status ;;
    start)       systemctl start "${BOT_SERVICE}" && sleep 2 && systemctl start "${SERVICE}" && echo -e "${GREEN}Started.${NC}" ;;
    stop)        systemctl stop "${SERVICE}" && systemctl stop "${BOT_SERVICE}" && echo -e "${YELLOW}Stopped.${NC}" ;;
    restart)     systemctl restart "${BOT_SERVICE}" && sleep 2 && systemctl restart "${SERVICE}" && echo -e "${GREEN}Restarted.${NC}" ;;
    logs)        cmd_logs "${2:-50}" ;;
    bot-logs)    cmd_bot_logs "${2:-50}" ;;
    show-config) cmd_show_config ;;
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
    echo ""
    echo -e "${BOLD}╔══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}║        Hysteria 2 VPN — Deployment Complete!                ║${NC}"
    echo -e "${BOLD}╚══════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "${CYAN}Server IP:${NC}            ${SERVER_IP}"
    echo -e "${CYAN}Port:${NC}                 ${LISTEN_PORT} (UDP)"
    echo -e "${CYAN}Auth:${NC}                 Multi-key (via Telegram bot)"
    echo -e "${CYAN}Obfs Type:${NC}            Salamander"
    echo -e "${CYAN}Obfs Password:${NC}        ${OBFS_PASSWORD}"
    echo -e "${CYAN}TLS:${NC}                  Self-signed (insecure: true on client)"
    echo -e "${CYAN}Stats API:${NC}            http://127.0.0.1:${STATS_PORT}"
    echo -e "${CYAN}Auth Backend:${NC}         http://127.0.0.1:${AUTH_BACKEND_PORT}"
    echo ""
    echo -e "${BOLD}── Telegram Bot ──${NC}"
    echo -e "  ${CYAN}Bot Token:${NC}          ${BOT_TOKEN:0:10}...${BOT_TOKEN: -5}"
    echo -e "  ${CYAN}Admin Password:${NC}     ${ADMIN_PASSWORD}"
    echo -e "  ${YELLOW}Open your bot in Telegram, send the password to get access.${NC}"
    echo -e "  ${YELLOW}Then use the bot to create VPN keys for clients.${NC}"
    echo ""
    echo -e "${BOLD}── Management ──${NC}"
    echo -e "  ${GREEN}hysteria-manage status${NC}       — all services status"
    echo -e "  ${GREEN}hysteria-manage logs${NC}         — Hysteria logs"
    echo -e "  ${GREEN}hysteria-manage bot-logs${NC}     — Bot logs"
    echo -e "  ${GREEN}hysteria-manage stats${NC}        — traffic statistics"
    echo -e "  ${GREEN}hysteria-manage restart${NC}      — restart all services"
    echo -e "  ${GREEN}hysteria-manage update${NC}       — update Hysteria"
    echo -e "  ${GREEN}hysteria-manage uninstall${NC}    — remove everything"
    echo ""
    echo -e "${BOLD}── How It Works ──${NC}"
    echo -e "  ${YELLOW}1. Open your Telegram bot${NC}"
    echo -e "  ${YELLOW}2. Enter the admin password${NC}"
    echo -e "  ${YELLOW}3. Create keys via the bot menu${NC}"
    echo -e "  ${YELLOW}4. Bot gives you client config + URI for each key${NC}"
    echo -e "  ${YELLOW}5. One key = unlimited devices, multiple keys supported${NC}"
    echo ""

    # Save credentials
    cat > "${HYSTERIA_DIR}/credentials.txt" <<CREDS
# Hysteria 2 VPN Credentials
# Generated: $(date)

Server IP:         ${SERVER_IP}
Port:              ${LISTEN_PORT} (UDP)
Auth:              Multi-key (via Telegram bot)
Obfs Type:         Salamander
Obfs Password:     ${OBFS_PASSWORD}
Stats API:         http://127.0.0.1:${STATS_PORT}
Stats Secret:      ${STATS_SECRET}
Auth Backend:      http://127.0.0.1:${AUTH_BACKEND_PORT}
Bot Token:         ${BOT_TOKEN}
Admin Password:    ${ADMIN_PASSWORD}
CREDS
    chmod 600 "${HYSTERIA_DIR}/credentials.txt"
    info "Credentials saved to ${HYSTERIA_DIR}/credentials.txt"
}

# ── Main ────────────────────────────────────────────────────────────────────
main() {
    echo ""
    echo -e "${BOLD}╔══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}║     Hysteria 2 VPN + Telegram Bot — One-Click Deployment    ║${NC}"
    echo -e "${BOLD}╚══════════════════════════════════════════════════════════════╝${NC}"
    echo ""

    preflight
    ask_bot_config
    install_deps
    install_hysteria
    generate_secrets
    detect_server_ip
    generate_cert
    write_config
    install_bot
    setup_firewall
    setup_hysteria_service
    setup_bot_service
    start_services
    setup_autoupdate
    create_management_script
    print_summary
}

main "$@"
