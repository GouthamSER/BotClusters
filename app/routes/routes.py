try:
    import eventlet
    eventlet.monkey_patch()
    HAS_EVENTLET = True
except (ImportError, Exception):
    HAS_EVENTLET = False

import os
import json
import signal
import subprocess
import re
import shutil
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
import logging
import time
import threading
import configparser
from collections import defaultdict, deque
import io
import hmac
import shlex

import psutil
from dotenv import load_dotenv

load_dotenv('cluster.env', override=True)

from app import app
from werkzeug.exceptions import HTTPException
from flask import (
    Flask, render_template, request, jsonify, Response,
    send_file, abort, redirect, url_for, session, flash, stream_with_context
)
from flask_socketio import SocketIO, emit

from app.utils.cluster_config import (
    SUPERVISORD_CONF_DIR, SUPERVISOR_LOG_DIR, APP_BASE_DIR,
    get_configured_clusters, get_next_available_slot,
    save_cluster_to_env, delete_cluster_from_env,
    update_general_setting, export_all_clusters,
    import_clusters_from_dict, send_telegram_alert,
    get_git_info, invalidate_git_cache, mask_url, validate_bot_name,
    validate_git_url, get_secret_key, SLOT_RE, DEFAULT_PASSWORDS,
    read_worker_state, set_worker_state
)
from app.utils.process_manager import (
    get_system_metrics, get_process_resource_usage,
    has_supervisorctl, prepare_bot_environment,
    write_supervisor_config_file, remove_supervisor_config,
    install_requirements, install_node_dependencies, get_venv_bin, _supervisor_env_string,
    _fallback_start, _fallback_stop, _fallback_status,
    _DEV_PROCESSES
)

try:
    from croniter import croniter
except Exception:          # croniter optional: per-bot cron disabled without it
    croniter = None

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    filename='app.log'
)
logger = logging.getLogger(__name__)
logging.getLogger('socketio').setLevel(logging.INFO)
logging.getLogger('engineio').setLevel(logging.INFO)

# Random per-install secret (env SECRET_KEY wins). Never a hardcoded default.
app.config['SECRET_KEY'] = get_secret_key()
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

ASYNC_MODE = 'eventlet' if HAS_EVENTLET else 'threading'
socketio = SocketIO(
    app,
    async_mode=ASYNC_MODE,
    cors_allowed_origins="*",
    ping_timeout=60,
    ping_interval=25
)

def async_sleep(seconds):
    if HAS_EVENTLET:
        eventlet.sleep(seconds)
    else:
        time.sleep(seconds)

def spawn_background_task(func, *args, **kwargs):
    if HAS_EVENTLET:
        return eventlet.spawn(func, *args, **kwargs)
    else:
        t = threading.Thread(target=func, args=args, kwargs=kwargs, daemon=True)
        t.start()
        return t

def kill_background_task(task):
    if HAS_EVENTLET and hasattr(task, 'kill'):
        try:
            task.kill()
        except Exception:
            pass

STATUS_CHECK_INTERVAL = 2
MAX_STATUS_CHECK_ATTEMPTS = 10

# ── Crash tracking (driven by a background monitor, so alerts work with no browser open) ──
FAIL_SINCE = {}                 # name -> first time seen in a failing state
PAUSED_BY_SYSTEM = set()        # names flagged as "crashing" (key kept for UI compat)
FAIL_GRACE_SECONDS = 45         # BACKOFF/EXITED longer than this = crash loop. FATAL = immediate
_HEALTH_LOCK = threading.Lock()

# ── Background setup jobs (clone / pip install can take minutes) ──
# name -> {"state": "INSTALLING"|"SETUP_FAILED", "message": str, "since": float}
SETUP_STATE = {}

# Cronjob restart interval (in hours), 0 = disabled
try:
    CRON_RESTART_INTERVAL = max(0, int(os.environ.get('CRON_RESTART_HOURS', 0) or 0))
except ValueError:
    CRON_RESTART_INTERVAL = 0

PROCESS_NAME_RE = re.compile(r'^[a-zA-Z0-9_\- ]+$')


def valid_process_name(name):
    return bool(name) and len(name) <= 80 and bool(PROCESS_NAME_RE.match(name))


def bad_name_response():
    return jsonify({"status": "error", "message": "Invalid process name"}), 400


# ── Auth ─────────────────────────────────────────────────────────
_LOGIN_FAILS = {}               # ip -> [count, first_fail_ts, locked_until]
LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SECONDS = 300


def get_auth_users():
    load_dotenv('cluster.env', override=True)
    admin_user = os.environ.get("ADMIN_USERNAME", "admin")
    admin_pass = os.environ.get("ADMIN_PASSWORD", "password123")
    return {admin_user: admin_pass}


def using_default_password():
    load_dotenv('cluster.env', override=True)
    return os.environ.get("ADMIN_PASSWORD", "password123") in DEFAULT_PASSWORDS


def client_ip():
    # rightmost X-Forwarded-For entry = the one added by the platform proxy (client cannot forge it)
    xff = request.headers.get('X-Forwarded-For', '')
    if xff:
        return xff.split(',')[-1].strip()
    return request.remote_addr or 'unknown'


def login_locked_seconds(ip):
    rec = _LOGIN_FAILS.get(ip)
    if rec and rec[2] > time.time():
        return int(rec[2] - time.time()) + 1
    return 0


def register_login_failure(ip):
    now = time.time()
    rec = _LOGIN_FAILS.get(ip)
    if not rec or now - rec[1] > 600 or rec[2] and rec[2] <= now:
        rec = [0, now, 0]
    rec[0] += 1
    if rec[0] >= LOGIN_MAX_FAILS:
        rec[2] = now + LOGIN_LOCK_SECONDS
    _LOGIN_FAILS[ip] = rec
    if len(_LOGIN_FAILS) > 500:
        for k in [k for k, v in _LOGIN_FAILS.items() if now - v[1] > 600 and v[2] <= now]:
            _LOGIN_FAILS.pop(k, None)


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            if request.is_json or request.path.startswith('/supervisor') or request.path.startswith('/api') \
                    or request.path.startswith('/config') or request.path.startswith('/logstream/stream'):
                return jsonify({"status": "error", "message": "Authentication required"}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


# ── Supervisor helpers ──────────────────────────────────────────
def is_process_paused(pid):
    try:
        return psutil.Process(int(pid)).status() == psutil.STATUS_STOPPED
    except Exception:
        pass
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("State:") and "\tT" in line:
                    return True
    except Exception:
        pass
    return False


def parse_supervisor_status(status_line):
    try:
        parts = status_line.strip().split()
        if len(parts) >= 2:
            pid_match = re.search(r'pid (\d+)', status_line)
            uptime_match = re.search(r'uptime ([\d:]+)', status_line)
            pid = pid_match.group(1) if pid_match else None
            return {
                "name": parts[0],
                "status": parts[1],
                "pid": pid,
                "uptime": uptime_match.group(1) if uptime_match else "0:00:00",
                "paused": bool(pid and is_process_paused(pid)),
            }
    except Exception as e:
        logger.error(f"Error parsing supervisor status line: {e}")
    return None


def run_supervisor_command(command, process_name=None, timeout=30):
    if not has_supervisorctl():
        # Fallback runner for Windows / local environments without supervisorctl
        if command == "status":
            lines = _fallback_status().splitlines()
            if process_name:
                lines = [l for l in lines if l.split() and l.split()[0] == process_name.replace(' ', '_')]
            return {"status": "success", "message": "\n".join(lines)}
        elif command == "start" and process_name:
            return _fallback_start(process_name)
        elif command == "stop" and process_name:
            return _fallback_stop(process_name)
        elif command == "restart" and process_name:
            _fallback_stop(process_name)
            time.sleep(1)
            return _fallback_start(process_name)
        elif command in ("reread", "update"):
            return {"status": "success", "message": "Configuration updated"}
        return {"status": "error", "message": f"Unsupported command '{command}' on standalone mode"}

    try:
        cmd = ["supervisorctl"]
        if command:
            cmd.append(command)
        if process_name:
            cmd.append(process_name)

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out, err = result.stdout.strip(), result.stderr.strip()
        logger.debug(f"supervisorctl {command} {process_name or ''} -> rc={result.returncode} {out[:200]}")

        # `supervisorctl status` exits 3 when ANY program is not RUNNING - the output is still valid.
        # Treating that as an error made every bot look STOPPED as soon as one bot was down.
        if result.returncode == 0 or (command == "status" and result.returncode == 3 and out):
            return {"status": "success", "message": out}
        return {"status": "error", "message": err or out or f"supervisorctl exited {result.returncode}"}

    except subprocess.TimeoutExpired:
        logger.error(f"supervisorctl {command} timed out after {timeout}s")
        return {"status": "error", "message": f"Command timed out after {timeout} seconds"}
    except Exception as e:
        logger.error(f"Error executing supervisor command: {e}")
        return {"status": "error", "message": str(e)}


def get_process_state(name):
    """Return supervisor status word (RUNNING/STOPPED/...) for a program, or None if unknown."""
    res = run_supervisor_command("status", name)
    if res["status"] == "success":
        for line in res["message"].splitlines():
            parsed = parse_supervisor_status(line)
            if parsed and parsed["name"] == name:
                return parsed["status"]
    return None


def _signal_process(process_name, sig_name, psutil_method, verb):
    result = run_supervisor_command("status", process_name)
    if result["status"] == "success":
        for line in result["message"].splitlines():
            proc = parse_supervisor_status(line)
            if proc and proc["name"] == process_name.replace(' ', '_') and proc["pid"]:
                try:
                    pid = int(proc["pid"])
                    sig = getattr(signal, sig_name, None)
                    parent = psutil.Process(pid)
                    # pause the whole tree (bot may spawn workers)
                    targets = [parent] + parent.children(recursive=True)
                    for p in targets:
                        try:
                            if sig is not None:
                                os.kill(p.pid, sig)
                            else:
                                getattr(p, psutil_method)()
                        except (psutil.NoSuchProcess, ProcessLookupError):
                            pass
                    return {"status": "success", "message": f"{verb} process {process_name}"}
                except Exception as e:
                    logger.error(f"Error {verb.lower()} {process_name}: {e}")
                    return {"status": "error", "message": str(e)}
    return {"status": "error", "message": "Process is not running"}


def pause_process(process_name):
    return _signal_process(process_name, 'SIGSTOP', 'suspend', 'Paused')


def resume_process(process_name):
    return _signal_process(process_name, 'SIGCONT', 'resume', 'Resumed')


def reload_supervisor():
    if has_supervisorctl():
        run_supervisor_command("reread")
        run_supervisor_command("update", timeout=60)


# ── Health monitoring / status payload ──────────────────────────
def _track_health(pname, status, now):
    """Update crash tracking. Returns True if process is flagged as crashing."""
    with _HEALTH_LOCK:
        failing = status in ("FATAL", "BACKOFF", "EXITED")
        if failing:
            first = FAIL_SINCE.setdefault(pname, now)
            tripped = status == "FATAL" or (now - first) >= FAIL_GRACE_SECONDS
            if tripped and pname not in PAUSED_BY_SYSTEM:
                PAUSED_BY_SYSTEM.add(pname)
                logger.warning(f"Process {pname} is crashing ({status})")
                send_telegram_alert(
                    f"⚠️ <b>BotClusters Alert</b>\nBot <code>{pname}</code> is crashing.\n"
                    f"Status: <b>{status}</b>. Check its logs from the dashboard."
                )
            return pname in PAUSED_BY_SYSTEM

        FAIL_SINCE.pop(pname, None)
        if status == "RUNNING" and pname in PAUSED_BY_SYSTEM:
            PAUSED_BY_SYSTEM.discard(pname)
            send_telegram_alert(f"✅ <b>BotClusters</b>\nBot <code>{pname}</code> is running again.")
        elif status in ("STOPPED", "STOPPING"):
            PAUSED_BY_SYSTEM.discard(pname)          # stopped on purpose
        return pname in PAUSED_BY_SYSTEM


def _cfg_fields(cfg):
    return {
        "slot": cfg.get("slot", ""),
        "source": cfg.get("source", "file"),
        "git_url": mask_url(cfg.get("git_url", "")),
        "branch": cfg.get("branch", "main"),
        "run_command": cfg.get("run_command", ""),
        "git_info": cfg.get("git_info", {}),
        "runtime": cfg.get("runtime", "python"),
        "runtime_version": cfg.get("runtime_version", ""),
        "runtime_display": cfg.get("runtime_display", ""),
        "python_version": cfg.get("python_version", ""),
        "node_version": cfg.get("node_version", ""),
        "cron": cfg.get("cron") or "",
    }


def build_process_status_payload():
    """Complete process list with metrics, git status and system metrics."""
    status = run_supervisor_command("status")
    now = time.time()
    processes = []
    seen_names = set()
    configured = get_configured_clusters()
    config_by_name = {c['safe_name']: c for c in configured}
    config_by_name.update({c['bot_name']: c for c in configured})
    supervisor_error = None

    if status.get("status") == "success" and status.get("message"):
        for proc_line in status["message"].splitlines():
            parsed = parse_supervisor_status(proc_line)
            if not parsed:
                continue
            pname = parsed["name"]
            seen_names.add(pname)
            parsed["auto_paused"] = _track_health(pname, parsed["status"], now)

            res = get_process_resource_usage(parsed.get("pid"))
            parsed["memory_mb"] = res["memory_mb"]
            parsed["cpu_percent"] = res["cpu_percent"]
            parsed.update(_cfg_fields(config_by_name.get(pname, {})))
            processes.append(parsed)
    elif status.get("status") == "error" and has_supervisorctl():
        supervisor_error = status.get("message")

    # Every configured bot must appear, even if supervisor doesn't know it yet
    for c in configured:
        sname = c['safe_name']
        if sname in seen_names or c['bot_name'] in seen_names:
            continue
        row = {"name": sname, "status": "STOPPED", "pid": None, "uptime": "0:00:00",
               "paused": False, "auto_paused": False, "memory_mb": 0, "cpu_percent": 0}
        row.update(_cfg_fields(c))
        processes.append(row)
        seen_names.add(sname)

    for p in processes:
        h = HISTORY.get(p["name"])
        p["hist_mem"] = [round(x[1], 1) for x in h][-60:] if h else []
        p["hist_cpu"] = [round(x[2], 1) for x in h][-60:] if h else []
        p["restarts"] = RESTARTS.get(p["name"], 0)

    # Overlay background-setup state (installing / failed)
    worker_state = read_worker_state()
    for p in processes:
        st = SETUP_STATE.get(p["name"]) or worker_state.get(p["name"])
        if st:
            p["status"] = st["state"]
            p["setup_message"] = st.get("message", "")

    return {
        "status": "success",
        "processes": processes,
        "system": get_system_metrics(),
        "supervisor_error": supervisor_error,
        "default_password": using_default_password(),
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


def broadcast_status_update():
    try:
        with app.app_context():
            payload = build_process_status_payload()
            # emit() from outside a handler already reaches every client
            # (the old broadcast=True kwarg raised TypeError on this socketio version)
            socketio.emit('status_update', payload)
            return True
    except Exception as e:
        logger.error(f"Error broadcasting status update: {e}")
    return False


# ── Metrics history (in memory, ~30 min @ 15s) + restart counter ──
HISTORY = defaultdict(lambda: deque(maxlen=120))     # name -> deque[(ts, mem_mb, cpu)]
RESTARTS = defaultdict(int)
_LAST_PID = {}


def record_metrics(processes):
    now = time.time()
    for p in processes:
        name = p["name"]
        if p["status"] == "RUNNING" and p.get("pid"):
            HISTORY[name].append((now, p.get("memory_mb", 0), p.get("cpu_percent", 0)))
            last = _LAST_PID.get(name)
            if last and last != p["pid"]:
                RESTARTS[name] += 1                   # new PID = process was restarted/crashed
            _LAST_PID[name] = p["pid"]
        elif p["status"] in ("BACKOFF", "EXITED", "FATAL"):
            _LAST_PID.pop(name, None)


def _monitor_loop():
    """Poll health every 15s so crash alerts fire even when nobody has the dashboard open."""
    while True:
        async_sleep(15)
        try:
            record_metrics(build_process_status_payload()["processes"])
        except Exception as e:
            logger.error(f"Monitor error: {e}")


# ── Bot provisioning (background) ───────────────────────────────
def provision_bot(bot, autostart=True):
    """Clone + venv + supervisor conf (blocking). Raises on failure."""
    _supervisor_env_string(bot.get('env') or {})        # validate env early with clear message
    _, cmd = prepare_bot_environment(bot)
    write_supervisor_config_file(bot, cmd, autostart=autostart)
    reload_supervisor()
    return cmd


def run_setup_job(name, label, fn):
    """Run fn() in background; expose INSTALLING / SETUP_FAILED state to the dashboard."""
    cur = SETUP_STATE.get(name)
    if cur and cur["state"] == "INSTALLING":
        return False
    SETUP_STATE[name] = {"state": "INSTALLING", "message": label, "since": time.time()}
    set_worker_state(name, None)              # a newer job replaces any stale boot-time flag

    def job():
        try:
            broadcast_status_update()
            fn()
            SETUP_STATE.pop(name, None)
        except Exception as e:
            msg = mask_url(str(e))[-500:]
            logger.error(f"Setup of {name} failed: {msg}")
            SETUP_STATE[name] = {"state": "SETUP_FAILED", "message": msg, "since": time.time()}
            send_telegram_alert(f"❌ <b>BotClusters</b>\nSetup of <code>{name}</code> failed:\n<code>{msg[-300:]}</code>")
        finally:
            invalidate_git_cache()
            broadcast_status_update()

    spawn_background_task(job)
    return True


def setup_bot_async(bot, start=True, reclone=False, drop_venv=False):
    name = bot['safe_name']

    def work():
        bot_dir = APP_BASE_DIR / name
        if reclone and bot_dir.exists():
            shutil.rmtree(bot_dir, ignore_errors=True)
        elif drop_venv:
            shutil.rmtree(bot_dir / 'venv', ignore_errors=True)
        provision_bot(bot, autostart=start)
        if start:
            run_supervisor_command("start", name)          # "already started" error is fine

    return run_setup_job(name, "Cloning repository and installing dependencies…", work)


def is_provisioned(bot):
    bot_dir = APP_BASE_DIR / bot['safe_name']
    if not (bot_dir / '.git').exists():
        return False
    if has_supervisorctl():
        return (Path(SUPERVISORD_CONF_DIR) / f"{bot['safe_name']}.conf").exists()
    return True


def find_cluster(name_or_slot):
    for c in get_configured_clusters():
        if name_or_slot in (c['safe_name'], c['bot_name'], c['slot']) or name_or_slot.upper() == c['slot'].upper():
            return c
    return None


def clean_pycache(process_name):
    """Remove stale .pyc files of a bot (skips venv/.git - walking them is very slow)."""
    directory = APP_BASE_DIR / process_name.replace(' ', '_')
    if not directory.exists():
        return
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in ('venv', '.git', 'node_modules')]
        for d in list(dirs):
            if d == '__pycache__':
                shutil.rmtree(Path(root) / d, ignore_errors=True)
                dirs.remove(d)


def truncate_supervisor_logs(process_name):
    """Empty log files IN PLACE. (Unlinking a file supervisor still has open makes logs vanish.)"""
    for suffix in ("_out.log", "_err.log"):
        f = Path(SUPERVISOR_LOG_DIR) / f"{process_name}{suffix}"
        try:
            if f.exists():
                with open(f, 'w'):
                    pass
        except Exception as e:
            logger.error(f"Could not clear {f}: {e}")


def delete_supervisor_logs(process_name):
    for suffix in ("_out.log", "_err.log", "_combined.log"):
        f = Path(SUPERVISOR_LOG_DIR) / f"{process_name}{suffix}"
        try:
            if f.exists():
                f.unlink()
        except Exception as e:
            logger.error(f"Error deleting {f}: {e}")


def restart_running_bots():
    """Restart only bots that are currently RUNNING (never wake up bots the user stopped)."""
    payload = build_process_status_payload()
    count = 0
    for p in payload["processes"]:
        if p["status"] == "RUNNING":
            run_supervisor_command("restart", p["name"])
            count += 1
    return count


def tail_bytes(path, limit):
    """Last `limit` bytes of file decoded safely."""
    size = path.stat().st_size
    with path.open('rb') as fh:
        if size > limit:
            fh.seek(size - limit)
            fh.readline()          # drop partial first line
        return fh.read().decode('utf-8', errors='replace')


# ── Health Check Endpoints ─────────────────────────────────────
@app.route('/health')
@app.route('/healthz')
@app.route('/ping')
def health_check():
    return jsonify({
        "status": "healthy",
        "service": "BotClusters",
        "version": "8.1",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }), 200


# ── Authentication & Main Routes ────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        ip = client_ip()
        wait = login_locked_seconds(ip)
        if wait:
            flash(f'Too many failed attempts. Try again in {wait} seconds.')
            return render_template('login.html'), 429

        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        users = get_auth_users()

        ok = False
        for u, p in users.items():      # constant-time compare, no early exit leaks
            ok |= hmac.compare_digest(username.encode(), u.encode()) and hmac.compare_digest(password.encode(), p.encode())
        if ok:
            _LOGIN_FAILS.pop(ip, None)
            session['logged_in'] = True
            session['user'] = username
            return redirect(url_for('cluster'))
        register_login_failure(ip)
        flash('Invalid username or password. Please try again.')
        return render_template('login.html'), 401

    return render_template('login.html'), 200


@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    session.pop('user', None)
    return redirect(url_for('login'))


@app.route('/')
def cluster():
    if 'logged_in' in session:
        return render_template('cluster.html'), 200
    return render_template('login.html'), 200


# ── Supervisor API Routes ───────────────────────────────────────
@app.route('/supervisor/status', methods=['GET'])
@login_required
def list_supervisor_processes():
    return jsonify(build_process_status_payload()), 200


@app.route('/supervisor/pause/<process_name>', methods=['POST'])
@login_required
def pause_supervisor_process(process_name):
    if not valid_process_name(process_name):
        return bad_name_response()
    result = pause_process(process_name)
    broadcast_status_update()
    return jsonify(result), (200 if result["status"] == "success" else 500)


@app.route('/supervisor/resume/<process_name>', methods=['POST'])
@login_required
def resume_supervisor_process(process_name):
    if not valid_process_name(process_name):
        return bad_name_response()
    result = resume_process(process_name)
    broadcast_status_update()
    return jsonify(result), (200 if result["status"] == "success" else 500)


@app.route('/supervisor/<action>/<process_name>', methods=['POST'])
@login_required
def manage_supervisor_process(action, process_name):
    if action not in ("start", "stop", "restart"):
        return jsonify({"status": "error", "message": "Invalid action"}), 400
    if not valid_process_name(process_name):
        return bad_name_response()

    safe_name = process_name.replace(' ', '_')
    try:
        if action == "stop":
            result = run_supervisor_command("stop", safe_name)
        else:
            bot = find_cluster(safe_name)
            if bot and not is_provisioned(bot):
                # first start / repo not cloned yet -> do the heavy part in background
                started = setup_bot_async(bot, start=True)
                broadcast_status_update()
                msg = "Setting up bot (clone + install). It will start automatically when ready." if started \
                    else "Setup already in progress."
                return jsonify({"status": "success", "message": msg}), 200
            if action == "restart":
                clean_pycache(safe_name)
            result = run_supervisor_command(action, safe_name)

        broadcast_status_update()
        if result.get("status") == "success":
            done = {"start": "started", "stop": "stopped", "restart": "restarted"}[action]
            return jsonify({"status": "success", "message": f"Successfully {done} {safe_name}"}), 200
        return jsonify(result), 500

    except Exception as e:
        logger.error(f"Error managing process {process_name}: {e}")
        return jsonify({"status": "error", "message": f"Error managing process: {e}"}), 500


# ── Batch Controls (Start All, Stop All, Restart All) ───────────
@app.route('/supervisor/batch/<action>', methods=['POST'])
@login_required
def batch_manage_processes(action):
    if action not in ("start_all", "stop_all", "restart_all"):
        return jsonify({"status": "error", "message": "Invalid batch action"}), 400

    logger.info(f"Batch action: {action}")
    configured = get_configured_clusters()
    results = {}

    for bot in configured:
        name = bot['safe_name']
        try:
            if action == "stop_all":
                res = run_supervisor_command("stop", name)
            elif not is_provisioned(bot):
                setup_bot_async(bot, start=True)
                res = {"status": "setting up"}
            elif action == "start_all":
                res = run_supervisor_command("start", name)
            else:
                res = run_supervisor_command("restart", name)
            results[name] = res.get("status", "unknown")
        except Exception as e:
            results[name] = f"error: {e}"

    broadcast_status_update()
    failed = [n for n, r in results.items() if r not in ("success", "setting up")]
    return jsonify({
        "status": "success",
        "action": action,
        "results": results,
        "failed": failed,
        "message": f"Batch {action.replace('_', ' ')} done for {len(configured)} bot(s)"
                   + (f" - {len(failed)} failed: {', '.join(failed)}" if failed else "."),
    }), 200


@app.route('/supervisor/log/<process_name>', methods=['GET'])
@login_required
def download_supervisor_log(process_name):
    try:
        if not valid_process_name(process_name):
            return bad_name_response()

        safe_name = process_name.replace(' ', '_')
        stdout_log = Path(SUPERVISOR_LOG_DIR) / f"{safe_name}_out.log"
        stderr_log = Path(SUPERVISOR_LOG_DIR) / f"{safe_name}_err.log"
        if not (stdout_log.exists() or stderr_log.exists()):
            return jsonify({"status": "error", "message": "No log files found for this process"}), 404

        limit = 2 * 1024 * 1024            # last 2 MB per stream keeps the viewer fast
        buf = io.StringIO()
        buf.write(f"=== Combined logs for {safe_name} ===\n")
        buf.write(f"Generated at: {datetime.now(timezone.utc).isoformat()} (last {limit // 1024 // 1024} MB per stream)\n\n")
        for title, f in (("STDOUT LOG", stdout_log), ("STDERR LOG", stderr_log)):
            if f.exists():
                buf.write(f"=== {title} ===\n{tail_bytes(f, limit)}\n\n")

        return send_file(
            io.BytesIO(buf.getvalue().encode('utf-8')),
            mimetype='text/plain',
            as_attachment=True,
            download_name=f"{safe_name}_combined.log"
        )
    except Exception as e:
        logger.error(f"Error accessing log files for {process_name}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/supervisor/clear_failure/<process_name>', methods=['POST'])
@login_required
def clear_failure(process_name):
    if not valid_process_name(process_name):
        return bad_name_response()
    safe_name = process_name.replace(' ', '_')
    with _HEALTH_LOCK:
        FAIL_SINCE.pop(safe_name, None)
        PAUSED_BY_SYSTEM.discard(safe_name)
    SETUP_STATE.pop(safe_name, None)
    bot = find_cluster(safe_name)
    if bot and not is_provisioned(bot):
        setup_bot_async(bot, start=True)
        msg = f"Retrying setup for {safe_name}…"
    else:
        res = run_supervisor_command("restart", safe_name)
        msg = f"Cleared failure state for {safe_name}" if res.get("status") == "success" \
            else f"Failure state cleared, but start failed: {res.get('message')}"
    broadcast_status_update()
    return jsonify({"status": "success", "message": msg})


# ── Dynamic Bot Management API (Add, Edit, Delete, Pull, Exec) ─
@app.route('/api/system/metrics', methods=['GET'])
@login_required
def api_system_metrics():
    return jsonify(get_system_metrics()), 200


def _masked(bot):
    b = dict(bot)
    b["git_url_masked"] = mask_url(bot.get("git_url", ""))
    return b


@app.route('/api/bots', methods=['GET'])
@login_required
def api_list_bots():
    return jsonify({"status": "success", "bots": [_masked(c) for c in get_configured_clusters()]}), 200


def validate_bot_payload(data, existing_names=()):
    """Return (clean_dict, error_message)."""
    bot_name = (data.get('bot_name') or '').strip()
    git_url = (data.get('git_url') or '').strip()
    branch = (data.get('branch') or 'main').strip()
    run_command = (data.get('run_command') or '').strip()
    env_vars = data.get('env') or {}
    cron = (str(data.get('cron') or '').strip()) or None

    runtime = (data.get('runtime') or '').strip().lower()
    runtime_version = (str(data.get('runtime_version') or '').strip()) or None
    python_version = (str(data.get('python_version') or '').strip()) or None
    node_version = (str(data.get('node_version') or '').strip()) or None

    err = validate_bot_name(bot_name, existing_names) or validate_git_url(git_url)
    if err:
        return None, err
    if not re.match(r'^[\w./\-]+$', branch):
        return None, "Invalid branch name"
    if '\n' in run_command or '\r' in run_command:
        return None, "Run command cannot contain line breaks"
    if not isinstance(env_vars, dict):
        return None, "Environment variables must be key/value pairs"
    try:
        _supervisor_env_string(env_vars)
    except ValueError as e:
        return None, str(e)

    # Determine runtime
    if not runtime:
        if node_version or (python_version and str(python_version).lower().startswith("node")):
            runtime = "node"
        elif any(run_command.startswith(x) for x in ("node ", "npm ", "npx ", "yarn ", "pnpm ")) or run_command.endswith((".js", ".mjs", ".cjs", ".ts")):
            runtime = "node"
        else:
            runtime = "python"

    if not run_command:
        run_command = "index.js" if runtime == "node" else "bot.py"

    if runtime == "node":
        n_ver = node_version or runtime_version
        if not n_ver and python_version and str(python_version).lower().startswith("node"):
            n_ver = re.sub(r"^node[\s:\-_]*", "", python_version, flags=re.IGNORECASE).strip()
        n_ver = n_ver or "22"
        if n_ver not in ("20", "22", "latest") and not re.match(r"^\d+(\.\d+)?$", n_ver):
            return None, "Node.js version must be 20, 22, or latest"
        node_version = n_ver
        python_version = None
        r_version = node_version
    else:
        runtime = "python"
        py_ver = python_version or runtime_version
        if py_ver and not re.match(r'^\d+\.\d+(\.\d+)?$', py_ver):
            return None, "Python version must look like 3.10, 3.11, 3.12, or 3.13"
        python_version = py_ver
        node_version = None
        r_version = python_version or "3.10"

    if cron:
        if croniter is None:
            return None, "Cron needs the 'croniter' package (pip install croniter)"
        if not croniter.is_valid(cron):
            return None, f"Invalid cron expression: '{cron}' (example: 0 */6 * * *)"

    return {
        "bot_name": bot_name, "safe_name": bot_name.replace(" ", "_"),
        "git_url": git_url, "branch": branch, "run_command": run_command,
        "env": {str(k).strip(): str(v) for k, v in env_vars.items() if str(k).strip()},
        "runtime": runtime,
        "runtime_version": r_version,
        "python_version": python_version,
        "node_version": node_version,
        "cron": cron,
    }, None


@app.route('/api/bots/add', methods=['POST'])
@login_required
def api_add_bot():
    try:
        data = request.get_json(force=True, silent=True) or {}
        clusters = get_configured_clusters()
        bot_data, err = validate_bot_payload(data, [c['bot_name'] for c in clusters])
        if err:
            return jsonify({"status": "error", "message": err}), 400

        slot = (data.get('slot') or '').strip().upper() or get_next_available_slot()
        if not SLOT_RE.match(slot):
            return jsonify({"status": "error", "message": "Slot must look like CLUSTER_05"}), 400
        if any(c['slot'].upper() == slot for c in clusters):
            return jsonify({"status": "error", "message": f"Slot {slot} is already used"}), 400
        bot_data["slot"] = slot

        save_cluster_to_env(slot, bot_data)
        setup_bot_async(bot_data, start=bool(data.get('auto_start', True)), reclone=True)
        broadcast_status_update()
        return jsonify({
            "status": "success",
            "message": f"Bot {bot_data['bot_name']} saved to {slot}. Cloning & installing in background - watch the card status.",
            "slot": slot
        }), 200
    except Exception as e:
        logger.error(f"Error adding bot: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/bots/<slot>', methods=['GET'])
@login_required
def api_get_bot(slot):
    match = next((c for c in get_configured_clusters() if c['slot'].upper() == slot.upper()), None)
    if not match:
        return jsonify({"status": "error", "message": f"Cluster {slot} not found"}), 404
    return jsonify({"status": "success", "bot": match}), 200


@app.route('/api/bots/update/<slot>', methods=['POST'])
@login_required
def api_update_bot(slot):
    try:
        data = request.get_json(force=True, silent=True) or {}
        clusters = get_configured_clusters()
        existing = next((c for c in clusters if c['slot'].upper() == slot.upper()), None)
        if not existing:
            return jsonify({"status": "error", "message": f"Cluster {slot} not found"}), 404

        merged = {
            "bot_name": data.get('bot_name') or existing['bot_name'],
            "git_url": data.get('git_url') or existing['git_url'],
            "branch": data.get('branch') or existing['branch'],
            "run_command": data.get('run_command') or existing['run_command'],
            "env": data.get('env', existing['env']),
            "runtime": data.get('runtime', existing.get('runtime')),
            "runtime_version": data.get('runtime_version', existing.get('runtime_version')),
            "python_version": data.get('python_version', existing.get('python_version')),
            "node_version": data.get('node_version', existing.get('node_version')),
            "cron": data.get('cron', existing.get('cron')),
        }
        others = [c['bot_name'] for c in clusters if c['slot'].upper() != slot.upper()]
        new, err = validate_bot_payload(merged, others)
        if err:
            return jsonify({"status": "error", "message": err}), 400
        new["slot"] = existing['slot']

        old_name = existing['safe_name']
        renamed = new['safe_name'] != old_name
        repo_changed = (new['git_url'] != existing['git_url']) or (new['branch'] != existing['branch'])
        runtime_changed = (new.get('runtime') != existing.get('runtime')) or \
                          (new.get('runtime_version') != existing.get('runtime_version')) or \
                          ((new.get('python_version') or '') != (existing.get('python_version') or '')) or \
                          ((new.get('node_version') or '') != (existing.get('node_version') or ''))

        was_running = get_process_state(old_name) == "RUNNING"

        if renamed:
            # old program + folder would be orphaned: remove them completely
            run_supervisor_command("stop", old_name)
            remove_supervisor_config(old_name)
            reload_supervisor()
            shutil.rmtree(APP_BASE_DIR / old_name, ignore_errors=True)
            delete_supervisor_logs(old_name)
            SETUP_STATE.pop(old_name, None)

        save_cluster_to_env(existing['slot'], new)

        setup_bot_async(new, start=was_running,
                        reclone=repo_changed or renamed, drop_venv=runtime_changed and not (repo_changed or renamed))
        broadcast_status_update()
        return jsonify({
            "status": "success",
            "message": f"Bot {new['bot_name']} ({existing['slot']}) updated. "
                       + ("Re-cloning repository in background…" if (repo_changed or renamed)
                          else "Applying changes…")
        }), 200
    except Exception as e:
        logger.error(f"Error updating bot {slot}: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/bots/delete/<slot>', methods=['POST', 'DELETE'])
@login_required
def api_delete_bot(slot):
    try:
        data = request.get_json(silent=True) or {}
        clean_files = data.get('clean_files', False)

        existing = next((c for c in get_configured_clusters() if c['slot'].upper() == slot.upper()), None)
        if not existing:
            return jsonify({"status": "error", "message": f"Cluster {slot} not found"}), 404

        safe_name = existing['safe_name']
        run_supervisor_command("stop", safe_name)
        remove_supervisor_config(safe_name)
        reload_supervisor()

        delete_cluster_from_env(existing['slot'])
        delete_supervisor_logs(safe_name)
        SETUP_STATE.pop(safe_name, None)
        with _HEALTH_LOCK:
            FAIL_SINCE.pop(safe_name, None)
            PAUSED_BY_SYSTEM.discard(safe_name)

        if clean_files:
            shutil.rmtree(APP_BASE_DIR / safe_name, ignore_errors=True)
            invalidate_git_cache(APP_BASE_DIR / safe_name)

        note = ""
        if existing.get("source") == "env":
            note = " NOTE: it is also defined as a platform environment variable - remove it there too or it returns after restart."
        broadcast_status_update()
        return jsonify({
            "status": "success",
            "message": f"Bot {existing['bot_name']} ({existing['slot']}) removed.{note}"
        }), 200
    except Exception as e:
        logger.error(f"Error deleting bot {slot}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


def _git(bot_dir, *args, timeout=120):
    return subprocess.run(["git", *args], cwd=bot_dir, capture_output=True, text=True, timeout=timeout)


def _file_hash(path):
    try:
        return hash(Path(path).read_bytes())
    except Exception:
        return None


def _deps_job(safe_name, bot_dir, label="requirements.txt changed - installing dependencies…"):
    bot = find_cluster(safe_name) or {}
    was_running = get_process_state(safe_name) == "RUNNING"

    def work():
        if bot.get("runtime") == "node":
            install_node_dependencies(bot_dir, bot.get("node_version"))
        elif (bot_dir / 'venv').exists():
            _pip_install(bot_dir, bot.get("python_version"))
        else:
            install_requirements(bot_dir, bot.get("python_version"))
        if was_running:
            run_supervisor_command("restart", safe_name)

    return run_setup_job(safe_name, label, work)


def restart_if_running(safe_name):
    """Restart only when currently RUNNING. Webhook/auto-pull/rollback must not start a bot the user stopped."""
    if get_process_state(safe_name) == "RUNNING":
        return run_supervisor_command("restart", safe_name).get("status") == "success"
    return False


def pull_core(safe_name):
    """git pull + restart (+ deps if requirements changed). Returns (payload, http_code)."""
    bot_dir = APP_BASE_DIR / safe_name
    if not (bot_dir / '.git').exists():
        return {"status": "error", "message": "Bot repository is not cloned yet - start the bot once first"}, 404
    try:
        before = _git(bot_dir, "rev-parse", "HEAD").stdout.strip()
        req_before = (_file_hash(bot_dir / "requirements.txt"), _file_hash(bot_dir / "package.json"))
        res = _git(bot_dir, "pull", "--ff-only")
        output = mask_url((res.stdout + "\n" + res.stderr).strip())
        if res.returncode != 0:
            return {"status": "error", "git_output": output,
                    "message": "git pull failed (local changes or diverged history?). See details in console."}, 500

        invalidate_git_cache(bot_dir)
        after = _git(bot_dir, "rev-parse", "HEAD").stdout.strip()
        if before == after:
            return {"status": "success", "git_output": output, "restarted": False,
                    "message": f"{safe_name} is already up to date."}, 200

        deps_changed = (_file_hash(bot_dir / "requirements.txt"), _file_hash(bot_dir / "package.json")) != req_before
        if deps_changed:
            _deps_job(safe_name, bot_dir)
            restarted = "after dependency install"
        else:
            restarted = restart_if_running(safe_name)
        broadcast_status_update()
        return {"status": "success", "git_output": output, "restarted": restarted,
                "message": f"Pulled new code for {safe_name}" + (" (dependencies updating)." if deps_changed else ".")}, 200
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "git pull timed out"}, 504
    except Exception as e:
        logger.error(f"Error in git pull for {safe_name}: {e}")
        return {"status": "error", "message": mask_url(str(e))}, 500


@app.route('/api/bots/pull/<process_name>', methods=['POST'])
@login_required
def api_bot_git_pull(process_name):
    if not valid_process_name(process_name):
        return bad_name_response()
    payload, code = pull_core(process_name.replace(' ', '_'))
    return jsonify(payload), code


# ── Versions: commit list + rollback ────────────────────────────
@app.route('/api/bots/commits/<process_name>', methods=['GET'])
@login_required
def api_bot_commits(process_name):
    if not valid_process_name(process_name):
        return bad_name_response()
    bot_dir = APP_BASE_DIR / process_name.replace(' ', '_')
    if not (bot_dir / '.git').exists():
        return jsonify({"status": "error", "message": "Bot repository is not cloned yet"}), 404
    try:
        res = _git(bot_dir, "log", "-20", "--branches", "--remotes", "--format=%H||%h||%s||%cr||%an", timeout=15)  # incl. newer remote commits so you can roll forward
        head = _git(bot_dir, "rev-parse", "HEAD", timeout=10).stdout.strip()
        commits = []
        for line in res.stdout.strip().splitlines():
            full, short, subject, when, author = (line.split("||") + ["", "", "", "", ""])[:5]
            commits.append({"hash": full, "short": short, "subject": subject, "when": when,
                            "author": author, "current": full == head})
        return jsonify({"status": "success", "commits": commits}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": mask_url(str(e))}), 500


@app.route('/api/bots/rollback/<process_name>', methods=['POST'])
@login_required
def api_bot_rollback(process_name):
    if not valid_process_name(process_name):
        return bad_name_response()
    commit = ((request.get_json(silent=True) or {}).get("commit") or "").strip()
    if not re.match(r'^[0-9a-f]{7,40}$', commit):
        return jsonify({"status": "error", "message": "Invalid commit id"}), 400
    safe_name = process_name.replace(' ', '_')
    bot_dir = APP_BASE_DIR / safe_name
    if not (bot_dir / '.git').exists():
        return jsonify({"status": "error", "message": "Bot repository is not cloned yet"}), 404
    try:
        req_before = (_file_hash(bot_dir / "requirements.txt"), _file_hash(bot_dir / "package.json"))
        res = _git(bot_dir, "reset", "--hard", commit, timeout=60)
        if res.returncode != 0:
            return jsonify({"status": "error", "message": mask_url((res.stderr or res.stdout).strip()[-300:])}), 500
        invalidate_git_cache(bot_dir)
        if (_file_hash(bot_dir / "requirements.txt"), _file_hash(bot_dir / "package.json")) != req_before:
            _deps_job(safe_name, bot_dir, "Rolled back - reinstalling dependencies…")
        else:
            restart_if_running(safe_name)
        broadcast_status_update()
        return jsonify({"status": "success",
                        "message": f"{safe_name} rolled back to {commit[:7]}. Pressing Pull later moves it forward again."}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": mask_url(str(e))}), 500


# ── Webhook: GitHub push -> auto pull + restart ─────────────────
def webhook_token(safe_name):
    return hmac.new(app.config['SECRET_KEY'].encode(), f"webhook:{safe_name}".encode(), "sha256").hexdigest()[:32]


@app.route('/api/bots/webhook/<process_name>', methods=['GET'])
@login_required
def api_bot_webhook_info(process_name):
    if not valid_process_name(process_name):
        return bad_name_response()
    safe_name = process_name.replace(' ', '_')
    return jsonify({"status": "success", "path": f"/webhook/{safe_name}/{webhook_token(safe_name)}"}), 200


@app.route('/webhook/<process_name>/<token>', methods=['POST'])
def webhook_pull(process_name, token):
    """Public on purpose (GitHub cannot log in); protected by an unguessable per-bot token."""
    if not valid_process_name(process_name) or not hmac.compare_digest(token, webhook_token(process_name.replace(' ', '_'))):
        return jsonify({"status": "error", "message": "Forbidden"}), 403
    if not find_cluster(process_name.replace(' ', '_')):
        return jsonify({"status": "error", "message": "Unknown bot"}), 404
    payload, code = pull_core(process_name.replace(' ', '_'))
    return jsonify(payload), code


def _pip_install(bot_dir, python_version):
    """Re-run pip in existing venv (surfaces pip errors; venv kept)."""
    venv = bot_dir / 'venv'
    res = subprocess.run([str(get_venv_bin(venv, 'pip')), 'install', '--no-cache-dir', '-r', str(bot_dir / 'requirements.txt')],
                         cwd=str(bot_dir), capture_output=True, text=True, timeout=1800)
    if res.returncode != 0:
        raise RuntimeError("pip install failed: " + (res.stderr or res.stdout)[-500:])


@app.route('/api/bots/rebuild_venv/<process_name>', methods=['POST'])
@login_required
def api_bot_rebuild_venv(process_name):
    try:
        if not valid_process_name(process_name):
            return bad_name_response()
        safe_name = process_name.replace(' ', '_')
        bot_dir = APP_BASE_DIR / safe_name
        if not ((bot_dir / "requirements.txt").exists() or (bot_dir / "package.json").exists()):
            return jsonify({"status": "error", "message": "No requirements.txt or package.json found in bot directory"}), 400
        started = _deps_job(safe_name, bot_dir, "Reinstalling dependencies…")
        broadcast_status_update()
        return jsonify({"status": "success",
                        "message": "Reinstalling requirements in background…" if started else "Setup already in progress."}), 200
    except Exception as e:
        logger.error(f"Error rebuilding venv for {process_name}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/bots/exec/<process_name>', methods=['POST'])
@login_required
def api_bot_exec(process_name):
    try:
        if not valid_process_name(process_name):
            return bad_name_response()
        data = request.get_json(force=True, silent=True) or {}
        command = (data.get("command") or "").strip()
        if not command:
            return jsonify({"status": "error", "message": "Command is required"}), 400

        # Light guard against accidents. This console is a full admin shell by design.
        forbidden = ["rm -rf /", ":(){ :|:& };:", "dd if=", "mkfs"]
        if any(fb in command for fb in forbidden):
            return jsonify({"status": "error", "message": "Command not allowed for safety"}), 400

        bot_dir = APP_BASE_DIR / process_name.replace(' ', '_')
        if not bot_dir.exists():
            bot_dir = Path.cwd()

        # run inside the bot's venv so `pip list` / `python` show the BOT's packages, not the dashboard's
        env = os.environ.copy()
        venv = bot_dir / 'venv'
        if venv.exists():
            env["VIRTUAL_ENV"] = str(venv)
            env["PATH"] = str(get_venv_bin(venv, 'python').parent) + os.pathsep + env.get("PATH", "")

        res = subprocess.run(command, shell=True, cwd=str(bot_dir), capture_output=True, text=True,
                             timeout=60, env=env)
        return jsonify({
            "status": "success",
            "stdout": mask_url(res.stdout),
            "stderr": mask_url(res.stderr),
            "exit_code": res.returncode
        }), 200
    except subprocess.TimeoutExpired:
        return jsonify({"status": "error", "message": "Command timed out after 60 seconds"}), 408
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ── General Settings & Telegram Alert API ─────────────────────
@app.route('/api/settings', methods=['GET', 'POST'])
@login_required
def api_settings():
    load_dotenv('cluster.env', override=True)
    if request.method == 'POST':
        global CRON_RESTART_INTERVAL
        data = request.get_json(force=True, silent=True) or {}
        try:
            if 'ping_interval' in data:
                ping = int(data['ping_interval'])
                if ping < 30:
                    return jsonify({"status": "error", "message": "Ping interval must be at least 30 seconds"}), 400
            if 'cron_restart_hours' in data:
                hours = int(data['cron_restart_hours'])
                if hours < 0 or hours > 720:
                    return jsonify({"status": "error", "message": "Restart hours must be 0-720"}), 400
            app_url = (data.get('app_url') or '').strip() if 'app_url' in data else None
            if app_url and not app_url.startswith(('http://', 'https://')):
                return jsonify({"status": "error", "message": "App URL must start with http:// or https://"}), 400
            if 'auto_pull_minutes' in data:
                ap = int(data['auto_pull_minutes'])
                if ap != 0 and ap < 5:
                    return jsonify({"status": "error", "message": "Auto-pull interval must be 0 (off) or at least 5 minutes"}), 400
            if data.get('admin_password') and len(data['admin_password']) < 6:
                return jsonify({"status": "error", "message": "Password must be at least 6 characters"}), 400

            if data.get('admin_username'):
                update_general_setting("ADMIN_USERNAME", data['admin_username'])
            if data.get('admin_password'):
                update_general_setting("ADMIN_PASSWORD", data['admin_password'])
            if app_url is not None:
                update_general_setting("APP_URL", app_url)
            if 'ping_interval' in data:
                update_general_setting("PING_INTERVAL", str(ping))
            if 'cron_restart_hours' in data:
                update_general_setting("CRON_RESTART_HOURS", str(hours))
                CRON_RESTART_INTERVAL = hours
            if 'auto_pull_minutes' in data:
                update_general_setting("AUTO_PULL_MINUTES", str(ap))
            if 'telegram_token' in data:
                update_general_setting("TELEGRAM_NOTIFY_TOKEN", data['telegram_token'])
            if 'telegram_chat_id' in data:
                update_general_setting("TELEGRAM_NOTIFY_CHAT_ID", data['telegram_chat_id'])
        except (ValueError, TypeError) as e:
            return jsonify({"status": "error", "message": str(e)}), 400

        return jsonify({"status": "success", "message": "Settings saved successfully!"}), 200

    def _int(key, default):
        try:
            return int(os.environ.get(key, default))
        except ValueError:
            return default

    return jsonify({
        "status": "success",
        "settings": {
            "admin_username": os.environ.get("ADMIN_USERNAME", "admin"),
            "app_url": os.environ.get("APP_URL", ""),
            "ping_interval": _int("PING_INTERVAL", 240),
            "cron_restart_hours": _int("CRON_RESTART_HOURS", 0),
            "auto_pull_minutes": _int("AUTO_PULL_MINUTES", 0),
            "telegram_token_configured": bool(os.environ.get("TELEGRAM_NOTIFY_TOKEN")),
            "telegram_chat_id": os.environ.get("TELEGRAM_NOTIFY_CHAT_ID", ""),
            "default_password": using_default_password(),
        }
    }), 200


@app.route('/api/notifications/test', methods=['POST'])
@login_required
def api_test_telegram():
    load_dotenv('cluster.env', override=True)
    if not os.environ.get("TELEGRAM_NOTIFY_TOKEN") or not os.environ.get("TELEGRAM_NOTIFY_CHAT_ID"):
        return jsonify({"status": "error", "message": "Save a Telegram bot token AND chat ID first."}), 400
    if send_telegram_alert("🔔 <b>BotClusters Test Notification</b>\nYour Telegram alert integration is working!"):
        return jsonify({"status": "success", "message": "Test notification delivered to Telegram!"}), 200
    return jsonify({"status": "error", "message": "Telegram rejected the message. Check token & chat ID (and press Start on your bot)."}), 400


# ── Backup & Restore / Export & Import API ─────────────────────
@app.route('/api/config/export', methods=['GET'])
@login_required
def api_export_config():
    data = export_all_clusters()
    mem_file = io.BytesIO(json.dumps(data, indent=2).encode('utf-8'))
    return send_file(
        mem_file,
        mimetype='application/json',
        as_attachment=True,
        download_name=f"botclusters_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )


@app.route('/api/config/import', methods=['POST'])
@login_required
def api_import_config():
    try:
        if 'file' in request.files:
            data = json.load(request.files['file'])
        else:
            data = request.get_json(force=True, silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"status": "error", "message": "Invalid backup file"}), 400

        imported, errors = import_clusters_from_dict(data)
        # imported bots are cloned/installed in background on first Start (or Start All)
        broadcast_status_update()
        msg = f"Imported {imported} bot cluster(s). Press Start (or Start All) to deploy them."
        if errors:
            msg += f" {len(errors)} skipped: " + "; ".join(errors[:3])
        code = 200 if (imported or not errors) else 400
        return jsonify({"status": "success" if code == 200 else "error", "imported_count": imported,
                        "errors": errors, "message": msg}), code
    except Exception as e:
        logger.error(f"Config import error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# ── Log Stream ──────────────────────────────────────────────────
@app.route('/logstream')
@login_required
def logstream_page():
    return render_template('logstream.html')


@app.route('/logstream/stream')
@login_required
def logstream_sse():
    """Stream all supervisor stdout/stderr logs as Server-Sent Events."""
    def generate():
        log_dir = Path(SUPERVISOR_LOG_DIR)
        positions = {}
        idle = 0
        while True:
            sent = False
            if log_dir.exists():
                for log_file in sorted(log_dir.glob("*.log")):
                    if '_combined' in log_file.name or log_file.name.startswith('supervisord'):
                        continue
                    try:
                        size = log_file.stat().st_size
                        pos = positions.get(log_file.name)
                        if pos is None:
                            pos = max(0, size - 16 * 1024)      # first sight: only the recent tail
                        elif size < pos:
                            pos = 0                              # file truncated / rotated
                        if size > pos:
                            with log_file.open('rb') as fh:      # binary: offsets are real byte positions
                                fh.seek(pos)
                                raw = fh.read(512 * 1024)
                                positions[log_file.name] = pos + len(raw)
                            text = raw.decode('utf-8', errors='replace')
                            if text.strip():
                                yield f"data: {json.dumps({'file': log_file.name, 'data': text})}\n\n"
                                sent = True
                        else:
                            positions[log_file.name] = pos
                    except Exception:
                        pass
            idle = 0 if sent else idle + 1
            if idle >= 15:
                yield ": keep-alive\n\n"          # lets proxies keep the connection and detects closed tabs
                idle = 0
            async_sleep(1)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )


# ── Scheduled restarts (global interval + per-bot cron expression) ──
@app.route('/config/cron', methods=['GET', 'POST'])
@login_required
def config_cron():
    global CRON_RESTART_INTERVAL
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        try:
            hours = int(data.get('hours', 0))
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "Hours must be a number"}), 400
        CRON_RESTART_INTERVAL = min(720, max(0, hours))
        update_general_setting('CRON_RESTART_HOURS', str(CRON_RESTART_INTERVAL))
        return jsonify({"status": "success", "hours": CRON_RESTART_INTERVAL})
    return jsonify({"status": "success", "hours": CRON_RESTART_INTERVAL})


_cron_next = {}


def auto_pull_all():
    """Pull every running bot whose remote has new commits (restart only when code changed)."""
    for c in get_configured_clusters():
        name = c['safe_name']
        bot_dir = APP_BASE_DIR / name
        if not (bot_dir / '.git').exists() or get_process_state(name) != "RUNNING":
            continue
        try:
            _git(bot_dir, "fetch", "-q", timeout=60)
            ahead = _git(bot_dir, "rev-list", "--count", "HEAD..@{u}", timeout=15).stdout.strip()
            if ahead.isdigit() and int(ahead) > 0:
                logger.info(f"Auto-pull: {name} has {ahead} new commit(s)")
                payload, _ = pull_core(name)
                send_telegram_alert(f"⬇️ <b>BotClusters</b>\nAuto-pulled <code>{name}</code>: {payload.get('message', '')}")
        except Exception as e:
            logger.error(f"Auto-pull {name} failed: {mask_url(str(e))}")


def _scheduler_loop():
    """ONE long-lived loop. (Old code spawned a new loop on every save in threading mode -> duplicate restarts.)"""
    next_global, last_hours = None, None
    next_pull = None
    while True:
        async_sleep(20)
        try:
            now = time.time()
            try:
                pull_min = int(os.environ.get("AUTO_PULL_MINUTES", 0) or 0)
            except ValueError:
                pull_min = 0
            if pull_min <= 0:
                next_pull = None
            elif next_pull is None:
                next_pull = now + pull_min * 60
            elif now >= next_pull:
                next_pull = now + pull_min * 60
                auto_pull_all()
            hrs = CRON_RESTART_INTERVAL
            if hrs <= 0:
                next_global = None
            elif next_global is None or hrs != last_hours:
                next_global, last_hours = now + hrs * 3600, hrs
            elif now >= next_global:
                logger.info("Scheduled restart: restarting running bots")
                restart_running_bots()
                broadcast_status_update()
                next_global = now + hrs * 3600

            if croniter:
                dt = datetime.now()
                for c in get_configured_clusters():
                    expr, name = c.get('cron'), c['safe_name']
                    if not expr or not croniter.is_valid(expr):
                        _cron_next.pop(name, None)
                        continue
                    rec = _cron_next.get(name)
                    if rec is None or rec[0] != expr:
                        _cron_next[name] = (expr, croniter(expr, dt).get_next(datetime))
                    elif dt >= rec[1]:
                        if get_process_state(name) == "RUNNING":
                            logger.info(f"Cron '{expr}': restarting {name}")
                            run_supervisor_command("restart", name)
                        _cron_next[name] = (expr, croniter(expr, dt).get_next(datetime))
        except Exception as e:
            logger.error(f"Scheduler error: {e}")


def _auto_delete_logs_loop():
    while True:
        async_sleep(24 * 3600)
        try:
            n = 0
            for log_file in Path(SUPERVISOR_LOG_DIR).glob("*.log"):
                try:
                    with open(log_file, 'w'):       # truncate in place, supervisor keeps writing fine
                        pass
                    n += 1
                except Exception as e:
                    logger.error(f"Failed to clear {log_file}: {e}")
            logger.info(f"Auto log cleanup: cleared {n} log files")
        except Exception as e:
            logger.error(f"Auto log cleanup error: {e}")


_bg_started = False


def _start_background_tasks():
    global _bg_started
    if _bg_started:
        return
    _bg_started = True
    spawn_background_task(_scheduler_loop)
    spawn_background_task(_auto_delete_logs_loop)
    spawn_background_task(_monitor_loop)


# ── SocketIO Events ─────────────────────────────────────────────
@socketio.on('connect')
def handle_connect():
    if 'logged_in' not in session:
        return False
    emit('connected', {'data': 'Connected'})
    try:
        emit('status_update', build_process_status_payload())     # only to the new client
    except Exception as e:
        logger.error(f"connect status error: {e}")


@socketio.on('disconnect')
def handle_disconnect():
    pass


@socketio.on('request_status')
def handle_status_request():
    if 'logged_in' not in session:
        emit('status_update', {"status": "error", "message": "Unauthorized", "processes": []})
        return
    try:
        emit('status_update', build_process_status_payload())
    except Exception as e:
        logger.error(f"Error in handle_status_request: {e}")
        emit('status_update', {"status": "error", "message": str(e), "processes": []})


@app.errorhandler(Exception)
def handle_error(e):
    if isinstance(e, HTTPException):           # 404 / 405 etc must keep their own code
        return jsonify({"status": "error", "message": e.description}), e.code
    logger.error(f"Unhandled server error: {e}", exc_info=True)
    return jsonify({"status": "error", "message": "An internal server error occurred"}), 500


_start_background_tasks()
