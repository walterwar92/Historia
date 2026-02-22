"""
Hysteria 2 Telegram Bot — Configuration
"""
import os
import json

# ── Paths ──────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("HYSTERIA_BOT_DATA", "/etc/hysteria/bot")
DB_PATH = os.path.join(DATA_DIR, "keys.db")
CONFIG_PATH = os.environ.get("HYSTERIA_BOT_CONFIG", os.path.join(DATA_DIR, "bot.json"))

# ── Defaults ───────────────────────────────────────────────────────────────
DEFAULTS = {
    "bot_token": "",
    "admin_password": "",
    "auth_backend_port": 8787,
    "hysteria_config": "/etc/hysteria/config.yaml",
    "hysteria_service": "hysteria-server",
    "stats_listen": "127.0.0.1:9090",
    "stats_secret": "",
    "server_ip": "",
    "server_port": 443,
    "obfs_password": "",
    "log_lines": 50,
    "tt_credentials_path": "/etc/hysteria/bot/tt_credentials.txt",
    "tt_service": "trusttunnel",
    "tt_server_port": 443,
}


def load_config() -> dict:
    """Load config from JSON, falling back to env vars and defaults."""
    cfg = dict(DEFAULTS)

    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            cfg.update(json.load(f))

    # Environment overrides
    env_map = {
        "BOT_TOKEN": "bot_token",
        "ADMIN_PASSWORD": "admin_password",
        "AUTH_BACKEND_PORT": "auth_backend_port",
        "HYSTERIA_CONFIG": "hysteria_config",
        "HYSTERIA_SERVICE": "hysteria_service",
        "STATS_LISTEN": "stats_listen",
        "STATS_SECRET": "stats_secret",
        "SERVER_IP": "server_ip",
        "SERVER_PORT": "server_port",
        "OBFS_PASSWORD": "obfs_password",
        "TT_CREDENTIALS_PATH": "tt_credentials_path",
        "TT_SERVICE": "tt_service",
        "TT_SERVER_PORT": "tt_server_port",
    }
    for env_key, cfg_key in env_map.items():
        val = os.environ.get(env_key)
        if val is not None:
            # Cast int fields
            if cfg_key in ("auth_backend_port", "server_port", "log_lines", "tt_server_port"):
                val = int(val)
            cfg[cfg_key] = val

    return cfg


def save_config(cfg: dict):
    """Persist config to JSON."""
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.chmod(CONFIG_PATH, 0o600)
