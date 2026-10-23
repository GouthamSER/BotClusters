import os
import sys
import re
import json
import shutil
import logging
import time
import threading
import secrets
import subprocess
from typing import Optional
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

ENV_FILE = Path("cluster.env")
CONFIG_JSON = Path("config.json")

SLOT_RE = re.compile(r"^CLUSTER_\d+$", re.IGNORECASE)
NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
# Folder names that live next to bots in the base dir. A bot with one of these
# names would overwrite (rmtree) dashboard code or data.
RESERVED_NAMES = {"app", "bots", "logs", "supervisor_conf", "__pycache__", "venv", "static", "templates"}
DEFAULT_PASSWORDS = {"password123", "admin123", "admin", "password", ""}


def mask_url(url: str) -> str:
    """Hide user:token@ part of git urls so secrets never hit logs."""
    return re.sub(r"(https?://)[^/@\s]+@", r"\1***@", url or "")


def validate_bot_name(name: str, existing_names=()) -> Optional[str]:
    """Return error text or None if name ok."""
    if not name or not NAME_RE.match(name):
        return "Bot name: only letters, numbers, hyphen, underscore (no spaces)"
    if len(name) > 40:
        return "Bot name too long (max 40 chars)"
    if name.lower() in RESERVED_NAMES:
        return f"'{name}' is reserved (would overwrite dashboard files). Pick another name"
    if name.lower() in {str(n).lower() for n in existing_names}:
        return f"A bot named '{name}' already exists"
    return None


def validate_git_url(url: str) -> Optional[str]:
    if not url or not url.startswith(("http://", "https://", "git@", "ssh://")):
        return "A valid Git URL is required (https://..., git@...)"
    if any(c in url for c in " \t\r\n;|&`$<>"):
        return "Git URL contains illegal characters"
    return None


def get_secret_key() -> str:
    """Stable random secret for sessions (never a hardcoded default)."""
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    key_file = Path(".secret_key")
    try:
        if key_file.exists():
            val = key_file.read_text().strip()
            if val:
                return val
        val = secrets.token_hex(32)
        key_file.write_text(val)
        try:
            key_file.chmod(0o600)
        except Exception:
            pass
        return val
    except Exception:
        return secrets.token_hex(32)

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
    env_data = {}
    # CLUSTER_xx defined as platform env vars (Koyeb/Render/Heroku) count too.
    # File values (loaded below) win over them.
    for k, v in os.environ.items():
        if SLOT_RE.match(k) and v:
            env_data[k.upper()] = v
    if not ENV_FILE.exists():
        return env_data

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
                env_data[key.upper() if SLOT_RE.match(key) else key] = val
    return env_data

def parse_runtime(raw_version: Optional[str], bot_dir: Optional[Path] = None, run_command: Optional[str] = None):
    """
    Parse runtime details from raw_version string, repository contents, or run command.
    Returns: (runtime, version, display_label)
    e.g. ('python', '3.12', 'Python 3.12') or ('node', '22', 'Node.js 22')
    """
    raw = str(raw_version or "").strip()
    if raw.lower().startswith("node"):
        v = re.sub(r"^node[\s:\-_]*", "", raw, flags=re.IGNORECASE).strip() or "22"
        return "node", v, f"Node.js {v}"

    if raw and (raw.startswith("3.") or raw.lower().startswith("py")):
        v = re.sub(r"^python[\s:\-_]*", "", raw, flags=re.IGNORECASE).strip()
        v = v if v else "3.10"
        return "python", v, f"Python {v}"

    if raw and re.match(r"^\d+(\.\d+)?$", raw):
        try:
            num = float(raw)
            if num >= 14:
                return "node", str(int(num)), f"Node.js {int(num)}"
            elif num < 4:
                return "python", raw, f"Python {raw}"
        except Exception:
            pass

    # Auto-detect from run_command
    if run_command:
        first_token = run_command.strip().split()[0].lower() if run_command.strip() else ""
        if first_token in ("node", "npm", "npx", "yarn", "pnpm") or first_token.endswith((".js", ".mjs", ".cjs", ".ts")):
            return "node", "22", "Node.js 22"

    # Auto-detect from files in repo if present
    if bot_dir and bot_dir.exists():
        if (bot_dir / "package.json").exists() and not (bot_dir / "requirements.txt").exists():
            return "node", "22", "Node.js 22"

    default_py = sys.version.split()[0] if sys.version else "3.10"
    short_py = '.'.join(default_py.split('.')[:2])
    return "python", short_py, f"Python {short_py}"


def get_configured_clusters() -> list:
    """Return all configured bot clusters with structured metadata."""
    load_dotenv(ENV_FILE, override=True)
    env_data = parse_cluster_env()
    file_slots = _file_slots()
    clusters = []

    # Sort keys by CLUSTER_XX number
    cluster_keys = [k for k in env_data if SLOT_RE.match(k)]
    cluster_keys.sort(key=lambda x: int(re.search(r"\d+", x).group()) if re.search(r"\d+", x) else 999)

    for slot in cluster_keys:
        raw_val = env_data.get(slot, "")
        try:
            parsed = json.loads(raw_val)
            if isinstance(parsed, list) and len(parsed) >= 4 and NAME_RE.match(str(parsed[0])):
                bot_name = parsed[0]
                git_url = parsed[1]
                branch = parsed[2] if len(parsed) > 2 else "main"
                run_command = parsed[3] if len(parsed) > 3 else "bot.py"
                env_dict = parsed[4] if len(parsed) > 4 and isinstance(parsed[4], dict) else {}
                raw_version = parsed[5] if len(parsed) > 5 else None
                cron = parsed[6] if len(parsed) > 6 else None

                bot_dir = APP_BASE_DIR / bot_name.replace(" ", "_")
                git_info = get_git_info(bot_dir)
                runtime, r_ver, r_display = parse_runtime(raw_version, bot_dir, run_command)

                clusters.append({
                    "slot": slot,
                    "bot_name": bot_name,
                    "safe_name": bot_name.replace(" ", "_"),
                    "git_url": git_url,
                    "branch": branch,
                    "run_command": run_command,
                    "env": env_dict,
                    "runtime": runtime,
                    "runtime_version": r_ver,
                    "runtime_display": r_display,
                    "python_version": r_ver if runtime == "python" else None,
                    "node_version": r_ver if runtime == "node" else None,
                    "cron": cron,
                    "source": "env" if (slot not in file_slots and slot in os.environ) else "file",
                    "dir_exists": bot_dir.exists(),
                    "git_info": git_info
                })
        except Exception as e:
            logger.warning(f"Failed to parse {slot}: {e}")

    return clusters

def _file_slots() -> set:
    slots = set()
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^\s*(CLUSTER_\d+)\s*=", line, re.IGNORECASE)
            if m:
                slots.add(m.group(1).upper())
    return slots


_GIT_CACHE: dict = {}
_GIT_TTL = 20


def invalidate_git_cache(bot_dir: Optional[Path] = None):
    if bot_dir is None:
        _GIT_CACHE.clear()
    else:
        _GIT_CACHE.pop(str(bot_dir), None)


def get_git_info(bot_dir: Path) -> dict:
    """Latest commit info (cached ~20s, status is polled every few seconds)."""
    key = str(bot_dir)
    hit = _GIT_CACHE.get(key)
    if hit and time.time() - hit[0] < _GIT_TTL:
        return hit[1]
    info = _get_git_info_uncached(bot_dir)
    _GIT_CACHE[key] = (time.time(), info)
    return info


def _get_git_info_uncached(bot_dir: Path) -> dict:
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
    runtime = bot_data.get("runtime") or ("node" if str(bot_data.get("python_version") or "").startswith("node") or bot_data.get("node_version") else "python")
    run_cmd = bot_data.get("run_command") or ("index.js" if runtime == "node" else "bot.py")

    raw_array = [
        bot_data.get("bot_name", "bot01"),
        bot_data.get("git_url", ""),
        bot_data.get("branch", "main"),
        run_cmd,
        bot_data.get("env", {}),
    ]

    if runtime == "node":
        n_ver = bot_data.get("node_version") or bot_data.get("runtime_version") or "22"
        saved_ver = f"node:{n_ver}" if not str(n_ver).startswith("node:") else n_ver
        raw_array.append(saved_ver)
    elif bot_data.get("python_version") or bot_data.get("runtime_version"):
        raw_array.append(bot_data.get("python_version") or bot_data.get("runtime_version"))

    if bot_data.get("cron"):
        while len(raw_array) < 6:
            raw_array.append(None)
        raw_array.append(bot_data.get("cron"))

    slot = slot.upper()
    if not SLOT_RE.match(slot):
        raise ValueError(f"Invalid slot name: {slot}")
    # '#' escaped so dotenv never treats it as an inline comment and cuts the value
    json_str = json.dumps(raw_array).replace("#", "\\u0023")
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

    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
        raise ValueError("Invalid setting key")
    value = str(value)
    if "\n" in value or "\r" in value:
        raise ValueError("Value cannot contain line breaks")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    new_line = f'{key}="{escaped}"\n'
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
        "version": "8.1",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "settings": settings,
        "clusters": clusters
    }

def import_clusters_from_dict(data: dict) -> tuple[int, list]:
    """Import clusters from exported dictionary (validated, matched by bot name)."""
    imported = 0
    errors = []
    clusters = data.get("clusters", [])
    if not isinstance(clusters, list):
        return 0, ["Invalid backup file: 'clusters' must be a list"]

    existing = get_configured_clusters()
    by_name = {c["bot_name"].lower(): c["slot"] for c in existing}
    used_slots = {c["slot"].upper() for c in existing}

    for c in clusters:
        if not isinstance(c, dict):
            errors.append("Skipped malformed entry")
            continue
        name = str(c.get("bot_name", ""))
        try:
            err = validate_bot_name(name) or validate_git_url(str(c.get("git_url", "")))
            if err:
                raise ValueError(err)
            if name.lower() in by_name:
                slot = by_name[name.lower()]          # same bot -> update in place
            else:
                slot = str(c.get("slot") or "").upper()
                if not SLOT_RE.match(slot) or slot in used_slots:
                    slot = get_next_available_slot()
            if not isinstance(c.get("env", {}), dict):
                c["env"] = {}
            save_cluster_to_env(slot, c)
            used_slots.add(slot)
            by_name[name.lower()] = slot
            imported += 1
        except Exception as e:
            errors.append(f"Failed to import '{name or '?'}': {e}")

    settings = data.get("settings", {})
    if isinstance(settings, dict):
        for k, v in settings.items():
            if k in ("APP_URL", "PING_INTERVAL", "CRON_RESTART_HOURS", "TELEGRAM_NOTIFY_TOKEN", "TELEGRAM_NOTIFY_CHAT_ID"):
                try:
                    update_general_setting(k, str(v))
                except Exception as e:
                    errors.append(f"Setting {k}: {e}")

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


# ── State shared between worker.py (boot-time setup) and the dashboard ──
WORKER_STATE_FILE = Path("worker_state.json")


_STATE_LOCK = threading.Lock()


def set_worker_state(name: str, state: Optional[str], message: str = ""):
    """state None = remove entry. Used so the dashboard can show 'Installing...' during boot setup."""
    with _STATE_LOCK:        # setups run in parallel threads: serialize read-modify-write
        try:
            data = read_worker_state()
            if state is None:
                data.pop(name, None)
            else:
                data[name] = {"state": state, "message": message, "since": time.time()}
            tmp = WORKER_STATE_FILE.with_name(f"{WORKER_STATE_FILE.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(WORKER_STATE_FILE)
        except Exception as e:
            logger.debug(f"worker state write failed: {e}")


def read_worker_state() -> dict:
    try:
        if WORKER_STATE_FILE.exists():
            return json.loads(WORKER_STATE_FILE.read_text() or "{}")
    except Exception:
        pass
    return {}
