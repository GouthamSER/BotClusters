import os
import re
import json
import shutil
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

ENV_FILE = Path("cluster.env")
CONFIG_JSON = Path("config.json")

def get_base_app_dir() -> Path:
    custom_dir = os.environ.get("APP_DIR")
    if custom_dir:
        p = Path(custom_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p
    p = Path("/app")
    try:
        p.mkdir(parents=True, exist_ok=True)
        return p
    except (PermissionError, OSError):
        local_p = Path.cwd() / "bots"
        local_p.mkdir(parents=True, exist_ok=True)
        return local_p

APP_BASE_DIR = get_base_app_dir()

def get_supervisor_dirs():
    conf_dir = os.environ.get("SUPERVISORD_CONF_DIR")
    log_dir = os.environ.get("SUPERVISOR_LOG_DIR")

    if conf_dir:
        p_conf = Path(conf_dir)
    else:
        p_conf = Path("/etc/supervisor/conf.d")
        try:
            p_conf.mkdir(parents=True, exist_ok=True)
        except (PermissionError, OSError):
            p_conf = Path.cwd() / "supervisor_conf"

    if log_dir:
        p_log = Path(log_dir)
    else:
        p_log = Path("/var/log/supervisor")
        try:
            p_log.mkdir(parents=True, exist_ok=True)
        except (PermissionError, OSError):
            p_log = Path.cwd() / "logs" / "supervisor"

    p_conf.mkdir(parents=True, exist_ok=True)
    p_log.mkdir(parents=True, exist_ok=True)
    return p_conf, p_log

SUPERVISORD_CONF_DIR, SUPERVISOR_LOG_DIR = get_supervisor_dirs()

def parse_cluster_env() -> dict:
    """Read cluster.env and return a dict of key -> raw string value."""
    if not ENV_FILE.exists():
        return {}

    env_data = {}
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" in stripped:
                key, val = stripped.split("=", 1)
                key = key.strip()
                val = val.strip()
                # Remove surrounding quotes if present
                if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                    val = val[1:-1]
                env_data[key] = val
    return env_data

def get_configured_clusters() -> list:
    """Return all configured bot clusters with structured metadata."""
    load_dotenv(ENV_FILE, override=True)
    env_data = parse_cluster_env()
    clusters = []

    # Sort keys by CLUSTER_XX number
    cluster_keys = [k for k in env_data if re.match(r"^CLUSTER_\d+$", k, re.IGNORECASE)]
    cluster_keys.sort(key=lambda x: int(re.search(r"\d+", x).group()) if re.search(r"\d+", x) else 999)

    for slot in cluster_keys:
        raw_val = env_data.get(slot, "")
        try:
            parsed = json.loads(raw_val)
            if isinstance(parsed, list) and len(parsed) >= 4:
                bot_name = parsed[0]
                git_url = parsed[1]
                branch = parsed[2] if len(parsed) > 2 else "main"
                run_command = parsed[3] if len(parsed) > 3 else "bot.py"
                env_dict = parsed[4] if len(parsed) > 4 and isinstance(parsed[4], dict) else {}
                python_version = parsed[5] if len(parsed) > 5 else None
                cron = parsed[6] if len(parsed) > 6 else None

                bot_dir = APP_BASE_DIR / bot_name.replace(" ", "_")
                git_info = get_git_info(bot_dir)

                clusters.append({
                    "slot": slot,
                    "bot_name": bot_name,
                    "safe_name": bot_name.replace(" ", "_"),
                    "git_url": git_url,
                    "branch": branch,
                    "run_command": run_command,
                    "env": env_dict,
                    "python_version": python_version,
                    "cron": cron,
                    "dir_exists": bot_dir.exists(),
                    "git_info": git_info
                })
        except Exception as e:
            logger.warning(f"Failed to parse {slot}: {e}")

    return clusters

def get_git_info(bot_dir: Path) -> dict:
    """Retrieve git branch and latest commit info from bot repo if cloned."""
    if not (bot_dir / ".git").exists():
        return {"cloned": False}
    try:
        cmd = ["git", "log", "-1", "--format=%h||%s||%cr"]
        res = subprocess.run(cmd, cwd=bot_dir, capture_output=True, text=True, timeout=5)
        if res.returncode == 0 and res.stdout.strip():
            parts = res.stdout.strip().split("||")
            return {
                "cloned": True,
                "commit_hash": parts[0] if len(parts) > 0 else "",
                "commit_message": parts[1] if len(parts) > 1 else "",
                "commit_time": parts[2] if len(parts) > 2 else ""
            }
    except Exception:
        pass
    return {"cloned": True, "commit_hash": "", "commit_message": "", "commit_time": ""}

def get_next_available_slot() -> str:
    """Find the next available CLUSTER_XX slot."""
    env_data = parse_cluster_env()
    existing_nums = set()
    for k in env_data:
        m = re.match(r"^CLUSTER_(\d+)$", k, re.IGNORECASE)
        if m:
            existing_nums.add(int(m.group(1)))

    for i in range(1, 100):
        if i not in existing_nums:
            return f"CLUSTER_{i:02d}"
    return "CLUSTER_99"

def save_cluster_to_env(slot: str, bot_data: dict) -> bool:
    """Write or update a CLUSTER_XX entry in cluster.env."""
    raw_array = [
        bot_data.get("bot_name", "bot01"),
        bot_data.get("git_url", ""),
        bot_data.get("branch", "main"),
        bot_data.get("run_command", "bot.py"),
        bot_data.get("env", {}),
    ]
    if bot_data.get("python_version"):
        raw_array.append(bot_data.get("python_version"))
    if bot_data.get("cron"):
        # if python_version was skipped, put None in index 5
        while len(raw_array) < 6:
            raw_array.append(None)
        raw_array.append(bot_data.get("cron"))

    json_str = json.dumps(raw_array)
    new_line = f'{slot}={json_str}\n'

    lines = []
    found = False
    if ENV_FILE.exists():
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()

    new_lines = []
    slot_pattern = re.compile(rf'^\s*{re.escape(slot)}\s*=', re.IGNORECASE)
    for line in lines:
        if slot_pattern.match(line):
            new_lines.append(new_line)
            found = True
        else:
            new_lines.append(line)

    if not found:
        if new_lines and not new_lines[-1].endswith('\n'):
            new_lines[-1] += '\n'
        new_lines.append(new_line)

    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    os.environ[slot] = json_str
    return True

def delete_cluster_from_env(slot: str) -> bool:
    """Remove a CLUSTER_XX from cluster.env."""
    if not ENV_FILE.exists():
        return False

    with open(ENV_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()

    slot_pattern = re.compile(rf'^\s*{re.escape(slot)}\s*=', re.IGNORECASE)
    new_lines = [l for l in lines if not slot_pattern.match(l)]

    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    if slot in os.environ:
        del os.environ[slot]
    return True

def update_general_setting(key: str, value: str) -> bool:
    """Update general key in cluster.env (e.g. ADMIN_PASSWORD, APP_URL, etc.)."""
    lines = []
    found = False
    if ENV_FILE.exists():
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()

    new_line = f'{key}={value}\n'
    new_lines = []
    key_pattern = re.compile(rf'^\s*{re.escape(key)}\s*=', re.IGNORECASE)

    for line in lines:
        if key_pattern.match(line):
            new_lines.append(new_line)
            found = True
        else:
            new_lines.append(line)

    if not found:
        if new_lines and not new_lines[-1].endswith('\n'):
            new_lines[-1] += '\n'
        new_lines.append(new_line)

    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    os.environ[key] = str(value)
    return True

def export_all_clusters() -> dict:
    """Export all clusters and settings to a portable dictionary."""
    clusters = get_configured_clusters()
    settings = {
        "ADMIN_USERNAME": os.environ.get("ADMIN_USERNAME", "admin"),
        "APP_URL": os.environ.get("APP_URL", ""),
        "PING_INTERVAL": os.environ.get("PING_INTERVAL", "240"),
        "CRON_RESTART_HOURS": os.environ.get("CRON_RESTART_HOURS", "0"),
        "TELEGRAM_NOTIFY_TOKEN": os.environ.get("TELEGRAM_NOTIFY_TOKEN", ""),
        "TELEGRAM_NOTIFY_CHAT_ID": os.environ.get("TELEGRAM_NOTIFY_CHAT_ID", ""),
    }
    return {
        "version": "8.0",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "settings": settings,
        "clusters": clusters
    }

def import_clusters_from_dict(data: dict) -> tuple[int, list]:
    """Import clusters from exported dictionary."""
    imported = 0
    errors = []
    clusters = data.get("clusters", [])

    for c in clusters:
        slot = c.get("slot") or get_next_available_slot()
        try:
            save_cluster_to_env(slot, c)
            imported += 1
        except Exception as e:
            errors.append(f"Failed to import {slot}: {e}")

    # Optionally import settings
    settings = data.get("settings", {})
    for k, v in settings.items():
        if k in ("APP_URL", "PING_INTERVAL", "CRON_RESTART_HOURS", "TELEGRAM_NOTIFY_TOKEN", "TELEGRAM_NOTIFY_CHAT_ID"):
            update_general_setting(k, str(v))

    return imported, errors

def send_telegram_alert(message: str) -> bool:
    """Send an alert message via Telegram Bot if token and chat ID are configured."""
    token = os.environ.get("TELEGRAM_NOTIFY_TOKEN")
    chat_id = os.environ.get("TELEGRAM_NOTIFY_CHAT_ID")
    if not token or not chat_id:
        return False

    try:
        import requests
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML"
        }
        res = requests.post(url, json=payload, timeout=8)
        return res.status_code == 200
    except Exception as e:
        logger.error(f"Error sending Telegram alert: {e}")
        return False
