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
from collections import defaultdict
import io

import psutil
from dotenv import load_dotenv

load_dotenv('cluster.env', override=True)

from app import app
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
    get_git_info
)
from app.utils.process_manager import (
    get_system_metrics, get_process_resource_usage,
    has_supervisorctl, prepare_bot_environment,
    write_supervisor_config_file,
    _fallback_start, _fallback_stop, _fallback_status,
    _DEV_PROCESSES
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    filename='app.log'
)
logger = logging.getLogger(__name__)
logging.getLogger('socketio').setLevel(logging.INFO)
logging.getLogger('engineio').setLevel(logging.INFO)

# Configurable secret key for persistent sessions
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'botclusters-secret-key-salt-2025')

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
TEMP_SUPERVISOR_CONFIGS = {}

# Track consecutive failures per process for auto-pause
FAILURE_COUNTS = defaultdict(int)
MAX_FAILURES_BEFORE_PAUSE = 5
PAUSED_BY_SYSTEM = set()

# Cronjob restart interval (in hours), 0 = disabled
CRON_RESTART_INTERVAL = int(os.environ.get('CRON_RESTART_HOURS', 0))
_cron_thread = None

def get_auth_users():
    load_dotenv('cluster.env', override=True)
    admin_user = os.environ.get("ADMIN_USERNAME", "admin")
    admin_pass = os.environ.get("ADMIN_PASSWORD", "password123")
    return {admin_user: admin_pass}

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            if request.is_json or request.path.startswith('/supervisor') or request.path.startswith('/api'):
                return jsonify({"status": "error", "message": "Authentication required"}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def is_process_paused(pid):
    try:
        pid_int = int(pid)
        p = psutil.Process(pid_int)
        return p.status() == psutil.STATUS_STOPPED
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
            name = parts[0]
            status = parts[1]
            pid_match = re.search(r'pid (\d+)', status_line)
            uptime_match = re.search(r'uptime ([\d:]+)', status_line)
            pid = pid_match.group(1) if pid_match else None
            paused = False

            if pid and is_process_paused(pid):
                paused = True

            return {
                "name": name,
                "status": status,
                "pid": pid,
                "uptime": uptime_match.group(1) if uptime_match else "0:00:00",
                "paused": paused
            }
    except Exception as e:
        logger.error(f"Error parsing supervisor status line: {e}")
    return None

def run_supervisor_command(command, process_name=None, timeout=30):
    if not has_supervisorctl():
        # Cross-platform fallback process runner for Windows/local environments without supervisorctl
        if command == "status":
            return {"status": "success", "message": _fallback_status()}
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

        logger.info(f"Executing supervisor command: {' '.join(cmd)}")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )

        if result.stdout:
            logger.info(f"Command output: {result.stdout.strip()}")
        if result.stderr:
            logger.error(f"Command error: {result.stderr.strip()}")

        if result.returncode == 0:
            return {"status": "success", "message": result.stdout.strip()}
        else:
            return {"status": "error", "message": result.stderr.strip() or result.stdout.strip()}

    except subprocess.TimeoutExpired:
        logger.error(f"Command timed out after {timeout} seconds")
        return {"status": "error", "message": f"Command timed out after {timeout} seconds"}
    except Exception as e:
        logger.error(f"Error executing supervisor command: {str(e)}")
        return {"status": "error", "message": str(e)}

def pause_process(process_name):
    result = run_supervisor_command("status", process_name)
    if result["status"] == "success":
        proc = parse_supervisor_status(result["message"])
        if proc and proc["pid"]:
            try:
                pid = int(proc["pid"])
                if hasattr(signal, 'SIGSTOP'):
                    os.kill(pid, signal.SIGSTOP)
                else:
                    psutil.Process(pid).suspend()
                return {"status": "success", "message": f"Paused process {process_name}"}
            except Exception as e:
                logger.error(f"Error pausing process {process_name}: {e}")
                return {"status": "error", "message": str(e)}
    return {"status": "error", "message": "Process not running or PID not found"}

def resume_process(process_name):
    result = run_supervisor_command("status", process_name)
    if result["status"] == "success":
        proc = parse_supervisor_status(result["message"])
        if proc and proc["pid"]:
            try:
                pid = int(proc["pid"])
                if hasattr(signal, 'SIGCONT'):
                    os.kill(pid, signal.SIGCONT)
                else:
                    psutil.Process(pid).resume()
                return {"status": "success", "message": f"Resumed process {process_name}"}
            except Exception as e:
                logger.error(f"Error resuming process {process_name}: {e}")
                return {"status": "error", "message": str(e)}
    return {"status": "error", "message": "Process not running or PID not found"}

def verify_process_status(process_name, expected_status=None):
    try:
        result = run_supervisor_command("status", process_name)
        if result["status"] == "success":
            if expected_status:
                return expected_status in result["message"]
            return result["message"]
        return None
    except Exception as e:
        logger.error(f"Error verifying process status: {str(e)}")
        return None

def build_process_status_payload():
    """Build complete process list with metrics, git status, and system metrics."""
    status = run_supervisor_command("status")
    processes = []
    seen_names = set()
    configured = get_configured_clusters()
    config_by_name = {c['safe_name']: c for c in configured}
    config_by_name.update({c['bot_name']: c for c in configured})

    if status.get("status") == "success" and status.get("message"):
        for proc_line in status["message"].splitlines():
            parsed = parse_supervisor_status(proc_line)
            if parsed:
                pname = parsed["name"]
                seen_names.add(pname)
                if parsed["status"] in ("FATAL", "BACKOFF", "EXITED"):
                    FAILURE_COUNTS[pname] += 1
                    if FAILURE_COUNTS[pname] >= MAX_FAILURES_BEFORE_PAUSE and pname not in PAUSED_BY_SYSTEM:
                        logger.warning(f"Process {pname} failed {FAILURE_COUNTS[pname]} times, auto-pausing")
                        PAUSED_BY_SYSTEM.add(pname)
                        parsed["auto_paused"] = True
                        send_telegram_alert(f"⚠️ <b>BotClusters Alert</b>\nBot <code>{pname}</code> failed repeatedly ({FAILURE_COUNTS[pname]} times)!\nStatus: <b>{parsed['status']}</b>. Auto-pause activated.")
                    elif pname in PAUSED_BY_SYSTEM:
                        parsed["auto_paused"] = True
                    else:
                        parsed["auto_paused"] = False
                else:
                    if parsed["status"] == "RUNNING":
                        FAILURE_COUNTS[pname] = 0
                        if pname in PAUSED_BY_SYSTEM:
                            PAUSED_BY_SYSTEM.discard(pname)
                    parsed["auto_paused"] = pname in PAUSED_BY_SYSTEM

                # Add resource consumption & metadata
                pid = parsed.get("pid")
                res = get_process_resource_usage(pid)
                parsed["memory_mb"] = res["memory_mb"]
                parsed["cpu_percent"] = res["cpu_percent"]

                cfg = config_by_name.get(pname, {})
                parsed["slot"] = cfg.get("slot", "")
                parsed["git_url"] = cfg.get("git_url", "")
                parsed["branch"] = cfg.get("branch", "main")
                parsed["run_command"] = cfg.get("run_command", "")
                parsed["git_info"] = cfg.get("git_info", {})
                parsed["python_version"] = cfg.get("python_version", "")

                processes.append(parsed)

    # Ensure all configured clusters appear on the dashboard
    for c in configured:
        sname = c['safe_name']
        if sname not in seen_names and c['bot_name'] not in seen_names:
            processes.append({
                "name": sname,
                "status": "STOPPED",
                "pid": None,
                "uptime": "0:00:00",
                "paused": False,
                "auto_paused": False,
                "memory_mb": 0,
                "cpu_percent": 0,
                "slot": c.get("slot", ""),
                "git_url": c.get("git_url", ""),
                "branch": c.get("branch", "main"),
                "run_command": c.get("run_command", ""),
                "git_info": c.get("git_info", {}),
                "python_version": c.get("python_version", "")
            })
            seen_names.add(sname)

    system_metrics = get_system_metrics()
    return {
        "status": "success",
        "processes": processes,
        "system": system_metrics,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

def broadcast_status_update():
    try:
        with app.app_context():
            payload = build_process_status_payload()
            socketio.emit('status_update', payload, broadcast=True)
            return True
    except Exception as e:
        logger.error(f"Error broadcasting status update: {str(e)}")
    return False

def update_process_code(process_name, config_content=None):
    try:
        directory = None
        if config_content:
            config = configparser.ConfigParser()
            config.read_string(config_content)
            section = 'program:' + process_name
            if section in config:
                directory = config[section].get('directory')
        else:
            config_path = Path(SUPERVISORD_CONF_DIR) / f"{process_name.replace(' ', '_')}.conf"
            if config_path.exists():
                config = configparser.ConfigParser()
                config.read(config_path)
                section = 'program:' + process_name
                if section in config:
                    directory = config[section].get('directory')

        if not directory:
            directory = (APP_BASE_DIR / process_name.replace(' ', '_')).as_posix()

        if directory and Path(directory).exists():
            subprocess.run(['git', 'pull'], cwd=directory, check=True)
            logger.info(f"Updated code for {process_name} in {directory}")
        else:
            logger.warning(f"No valid directory found for {process_name}")
    except Exception as e:
        logger.error(f"Error updating code for {process_name}: {str(e)}")

def thoroughly_cleanup(process_name):
    try:
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                cmdline = ' '.join(proc.info.get('cmdline') or [])
                name = proc.info.get('name') or ''
                if process_name in cmdline or process_name in name:
                    proc.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception as e:
        logger.warning(f"Process cleanup notice: {e}")

    directory = (APP_BASE_DIR / process_name.replace(' ', '_'))
    if directory.exists():
        for root, dirs, files in os.walk(directory):
            for d in dirs:
                if d == '__pycache__':
                    pycache_dir = Path(root) / d
                    for file in pycache_dir.glob('*.pyc'):
                        try:
                            file.unlink()
                        except Exception:
                            pass
                    try:
                        pycache_dir.rmdir()
                    except Exception:
                        pass
            for f in files:
                if f.endswith('.pyc'):
                    try:
                        (Path(root) / f).unlink()
                    except Exception:
                        pass

def delete_supervisor_logs(process_name):
    try:
        stdout_log = Path(SUPERVISOR_LOG_DIR) / f"{process_name}_out.log"
        stderr_log = Path(SUPERVISOR_LOG_DIR) / f"{process_name}_err.log"
        combined_log = Path(SUPERVISOR_LOG_DIR) / f"{process_name}_combined.log"
        for log_file in [stdout_log, stderr_log, combined_log]:
            if log_file.exists():
                log_file.unlink()
                logger.info(f"Deleted log file: {log_file}")
    except Exception as e:
        logger.error(f"Error deleting logs for {process_name}: {e}")

# ── Health Check Endpoints ─────────────────────────────────────
@app.route('/health')
@app.route('/healthz')
@app.route('/ping')
def health_check():
    return jsonify({
        "status": "healthy",
        "service": "BotClusters",
        "version": "8.0",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }), 200

# ── Authentication & Main Routes ────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        users = get_auth_users()

        if username in users and users[username] == password:
            session['logged_in'] = True
            session['user'] = username
            return redirect(url_for('cluster'))
        else:
            flash('Invalid username or password. Please try again.')

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
    payload = build_process_status_payload()
    return jsonify(payload), 200

@app.route('/supervisor/pause/<process_name>', methods=['POST'])
@login_required
def pause_supervisor_process(process_name):
    logger.info(f"Received pause request for process: {process_name}")
    result = pause_process(process_name)
    if result["status"] == "success":
        broadcast_status_update()
        return jsonify(result), 200
    return jsonify(result), 500

@app.route('/supervisor/resume/<process_name>', methods=['POST'])
@login_required
def resume_supervisor_process(process_name):
    logger.info(f"Received resume request for process: {process_name}")
    result = resume_process(process_name)
    if result["status"] == "success":
        broadcast_status_update()
        return jsonify(result), 200
    return jsonify(result), 500

@app.route('/supervisor/<action>/<process_name>', methods=['POST'])
@login_required
def manage_supervisor_process(action, process_name):
    logger.info(f"Received {action} request for process: {process_name}")

    if action not in ["start", "stop", "restart"]:
        return jsonify({"status": "error", "message": "Invalid action"}), 400

    if not re.match(r'^[a-zA-Z0-9_\- ]+$', process_name):
        return jsonify({"status": "error", "message": "Invalid process name"}), 400

    safe_name = process_name.replace(' ', '_')
    config_path = Path(SUPERVISORD_CONF_DIR) / f"{safe_name}.conf"

    try:
        if action == "stop":
            result = run_supervisor_command("stop", process_name)
            expected_status = "STOPPED"
        elif action == "start":
            if not has_supervisorctl():
                result = run_supervisor_command("start", process_name)
            else:
                if process_name in TEMP_SUPERVISOR_CONFIGS:
                    config_content = TEMP_SUPERVISOR_CONFIGS[process_name]
                    update_process_code(process_name, config_content)
                    with open(config_path, 'w', encoding='utf-8') as f:
                        f.write(config_content)
                    subprocess.run(["supervisorctl", "reread"], check=False)
                    subprocess.run(["supervisorctl", "update"], check=False)
                    del TEMP_SUPERVISOR_CONFIGS[process_name]
                elif not config_path.exists():
                    configured = get_configured_clusters()
                    match = next((c for c in configured if c['safe_name'] == safe_name or c['bot_name'] == process_name), None)
                    if match:
                        _, cmd = prepare_bot_environment(match)
                        write_supervisor_config_file(match, cmd)
                        subprocess.run(["supervisorctl", "reread"], check=False)
                        subprocess.run(["supervisorctl", "update"], check=False)

                result = run_supervisor_command("start", process_name)
            expected_status = "RUNNING"
        elif action == "restart":
            thoroughly_cleanup(process_name)
            delete_supervisor_logs(process_name)
            result = run_supervisor_command("restart", process_name)
            expected_status = "RUNNING"

        broadcast_status_update()
        if result.get("status") == "success":
            return jsonify({"status": "success", "message": f"Successfully {action}ed {process_name}"}), 200
        else:
            return jsonify(result), 500

    except Exception as e:
        logger.error(f"Error managing process {process_name}: {str(e)}")
        return jsonify({"status": "error", "message": f"Error managing process: {str(e)}"}), 500

# ── Batch Controls (Start All, Stop All, Restart All) ───────────
@app.route('/supervisor/batch/<action>', methods=['POST'])
@login_required
def batch_manage_processes(action):
    if action not in ["start_all", "stop_all", "restart_all"]:
        return jsonify({"status": "error", "message": "Invalid batch action"}), 400

    logger.info(f"Received batch action: {action}")
    configured = get_configured_clusters()
    results = {}

    for cluster in configured:
        safe_name = cluster['safe_name']
        try:
            if action == "start_all":
                res = run_supervisor_command("start", safe_name)
            elif action == "stop_all":
                res = run_supervisor_command("stop", safe_name)
            elif action == "restart_all":
                res = run_supervisor_command("restart", safe_name)
            results[safe_name] = res.get("status", "unknown")
        except Exception as e:
            results[safe_name] = f"error: {str(e)}"

    broadcast_status_update()
    return jsonify({
        "status": "success",
        "action": action,
        "results": results,
        "message": f"Batch {action} executed for {len(configured)} bots."
    }), 200

@app.route('/supervisor/log/<process_name>', methods=['GET'])
@login_required
def download_supervisor_log(process_name):
    try:
        if not re.match(r'^[a-zA-Z0-9_\- ]+$', process_name):
            return jsonify({"status": "error", "message": "Invalid process name"}), 400

        safe_name = process_name.replace(' ', '_')
        stdout_log = Path(SUPERVISOR_LOG_DIR) / f"{safe_name}_out.log"
        stderr_log = Path(SUPERVISOR_LOG_DIR) / f"{safe_name}_err.log"
        combined_log = Path(SUPERVISOR_LOG_DIR) / f"{safe_name}_combined.log"

        if stdout_log.exists() or stderr_log.exists():
            with combined_log.open('w', encoding='utf-8') as outfile:
                outfile.write(f"=== Combined logs for {process_name} ===\n")
                outfile.write(f"Generated at: {datetime.now(timezone.utc).isoformat()}\n\n")

                if stdout_log.exists():
                    outfile.write("=== STDOUT LOG ===\n")
                    with stdout_log.open('r', errors='replace', encoding='utf-8') as f:
                        outfile.write(f.read())
                    outfile.write("\n\n")

                if stderr_log.exists():
                    outfile.write("=== STDERR LOG ===\n")
                    with stderr_log.open('r', errors='replace', encoding='utf-8') as f:
                        outfile.write(f.read())

            return send_file(
                str(combined_log),
                mimetype='text/plain',
                as_attachment=True,
                download_name=f"{safe_name}_combined.log"
            )
        else:
            return jsonify({
                "status": "error",
                "message": "No log files found for this process"
            }), 404

    except Exception as e:
        logger.error(f"Error accessing log files for {process_name}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/supervisor/clear_failure/<process_name>', methods=['POST'])
@login_required
def clear_failure(process_name):
    FAILURE_COUNTS[process_name] = 0
    PAUSED_BY_SYSTEM.discard(process_name)
    run_supervisor_command("start", process_name)
    broadcast_status_update()
    return jsonify({"status": "success", "message": f"Cleared failure state for {process_name}"})

# ── Dynamic Bot Management API (Add, Edit, Delete, Pull, Exec) ─
@app.route('/api/system/metrics', methods=['GET'])
@login_required
def api_system_metrics():
    return jsonify(get_system_metrics()), 200

@app.route('/api/bots', methods=['GET'])
@login_required
def api_list_bots():
    clusters = get_configured_clusters()
    return jsonify({"status": "success", "bots": clusters}), 200

@app.route('/api/bots/add', methods=['POST'])
@login_required
def api_add_bot():
    try:
        data = request.get_json(force=True) or {}
        bot_name = (data.get('bot_name') or '').strip()
        git_url = (data.get('git_url') or '').strip()
        branch = (data.get('branch') or 'main').strip()
        run_command = (data.get('run_command') or 'bot.py').strip()
        env_vars = data.get('env', {})
        python_version = data.get('python_version')
        cron = data.get('cron')
        auto_start = data.get('auto_start', True)

        if not bot_name or not re.match(r'^[a-zA-Z0-9_\-]+$', bot_name):
            return jsonify({"status": "error", "message": "Bot name must contain only letters, numbers, hyphens or underscores"}), 400

        if not git_url or not git_url.startswith(('http://', 'https://', 'git@')):
            return jsonify({"status": "error", "message": "A valid Git URL is required"}), 400

        slot = data.get('slot') or get_next_available_slot()
        bot_data = {
            "slot": slot,
            "bot_name": bot_name,
            "safe_name": bot_name.replace(" ", "_"),
            "git_url": git_url,
            "branch": branch,
            "run_command": run_command,
            "env": env_vars,
            "python_version": python_version,
            "cron": cron
        }

        save_cluster_to_env(slot, bot_data)

        # Clone and write configuration in background or sync
        try:
            bot_dir, final_cmd = prepare_bot_environment(bot_data)
            write_supervisor_config_file(bot_data, final_cmd)
            if has_supervisorctl():
                subprocess.run(["supervisorctl", "reread"], check=False)
                subprocess.run(["supervisorctl", "update"], check=False)
        except Exception as e:
            logger.error(f"Error setting up bot environment: {e}")

        if auto_start:
            run_supervisor_command("start", bot_name)

        broadcast_status_update()
        return jsonify({
            "status": "success",
            "message": f"Bot {bot_name} successfully added to {slot}!",
            "slot": slot
        }), 200

    except Exception as e:
        logger.error(f"Error adding bot: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/bots/<slot>', methods=['GET'])
@login_required
def api_get_bot(slot):
    clusters = get_configured_clusters()
    match = next((c for c in clusters if c['slot'].upper() == slot.upper()), None)
    if not match:
        return jsonify({"status": "error", "message": f"Cluster {slot} not found"}), 404
    return jsonify({"status": "success", "bot": match}), 200

@app.route('/api/bots/update/<slot>', methods=['POST'])
@login_required
def api_update_bot(slot):
    try:
        data = request.get_json(force=True) or {}
        clusters = get_configured_clusters()
        existing = next((c for c in clusters if c['slot'].upper() == slot.upper()), None)
        if not existing:
            return jsonify({"status": "error", "message": f"Cluster {slot} not found"}), 404

        bot_name = data.get('bot_name') or existing['bot_name']
        git_url = data.get('git_url') or existing['git_url']
        branch = data.get('branch') or existing['branch']
        run_command = data.get('run_command') or existing['run_command']
        env_vars = data.get('env', existing['env'])
        python_version = data.get('python_version', existing.get('python_version'))
        cron = data.get('cron', existing.get('cron'))

        updated_data = {
            "slot": slot,
            "bot_name": bot_name,
            "safe_name": bot_name.replace(" ", "_"),
            "git_url": git_url,
            "branch": branch,
            "run_command": run_command,
            "env": env_vars,
            "python_version": python_version,
            "cron": cron
        }

        save_cluster_to_env(slot, updated_data)

        # Update supervisor config
        try:
            bot_dir, final_cmd = prepare_bot_environment(updated_data)
            write_supervisor_config_file(updated_data, final_cmd)
            if has_supervisorctl():
                subprocess.run(["supervisorctl", "reread"], check=False)
                subprocess.run(["supervisorctl", "update"], check=False)
        except Exception as e:
            logger.error(f"Error updating supervisor config: {e}")

        broadcast_status_update()
        return jsonify({
            "status": "success",
            "message": f"Bot {bot_name} ({slot}) updated successfully!"
        }), 200

    except Exception as e:
        logger.error(f"Error updating bot {slot}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/bots/delete/<slot>', methods=['POST', 'DELETE'])
@login_required
def api_delete_bot(slot):
    try:
        data = request.get_json(silent=True) or {}
        clean_files = data.get('clean_files', False)

        clusters = get_configured_clusters()
        existing = next((c for c in clusters if c['slot'].upper() == slot.upper()), None)
        if not existing:
            return jsonify({"status": "error", "message": f"Cluster {slot} not found"}), 404

        safe_name = existing['safe_name']
        run_supervisor_command("stop", safe_name)

        # Remove config file
        conf_file = Path(SUPERVISORD_CONF_DIR) / f"{safe_name}.conf"
        if conf_file.exists():
            try:
                conf_file.unlink()
            except Exception:
                pass

        if has_supervisorctl():
            subprocess.run(["supervisorctl", "reread"], check=False)
            subprocess.run(["supervisorctl", "update"], check=False)

        delete_cluster_from_env(slot)
        delete_supervisor_logs(safe_name)

        if clean_files:
            bot_dir = APP_BASE_DIR / safe_name
            if bot_dir.exists():
                shutil.rmtree(bot_dir, ignore_errors=True)

        broadcast_status_update()
        return jsonify({
            "status": "success",
            "message": f"Bot {existing['bot_name']} ({slot}) removed successfully!"
        }), 200

    except Exception as e:
        logger.error(f"Error deleting bot {slot}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/bots/pull/<process_name>', methods=['POST'])
@login_required
def api_bot_git_pull(process_name):
    try:
        safe_name = process_name.replace(' ', '_')
        bot_dir = APP_BASE_DIR / safe_name
        if not bot_dir.exists():
            return jsonify({"status": "error", "message": f"Bot directory {bot_dir} does not exist"}), 404

        res = subprocess.run(["git", "pull"], cwd=bot_dir, capture_output=True, text=True, timeout=30)
        output = (res.stdout + "\n" + res.stderr).strip()

        # Restart bot to take changes
        restart_res = run_supervisor_command("restart", process_name)
        broadcast_status_update()

        return jsonify({
            "status": "success",
            "git_output": output,
            "restarted": restart_res.get("status") == "success",
            "message": f"Pulled latest code for {process_name}."
        }), 200
    except Exception as e:
        logger.error(f"Error in git pull for {process_name}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/bots/rebuild_venv/<process_name>', methods=['POST'])
@login_required
def api_bot_rebuild_venv(process_name):
    try:
        safe_name = process_name.replace(' ', '_')
        bot_dir = APP_BASE_DIR / safe_name
        req_file = bot_dir / "requirements.txt"
        if not req_file.exists():
            return jsonify({"status": "error", "message": "No requirements.txt found in bot directory"}), 400

        from app.utils.process_manager import get_venv_bin
        pip_bin = str(get_venv_bin(bot_dir / "venv", "pip"))
        res = subprocess.run([pip_bin, "install", "--no-cache-dir", "-r", str(req_file)],
                             cwd=bot_dir, capture_output=True, text=True, timeout=120)
        output = (res.stdout + "\n" + res.stderr).strip()
        run_supervisor_command("restart", process_name)
        broadcast_status_update()

        return jsonify({
            "status": "success",
            "pip_output": output,
            "message": f"Requirements reinstalled for {process_name}."
        }), 200
    except Exception as e:
        logger.error(f"Error rebuilding venv for {process_name}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/bots/exec/<process_name>', methods=['POST'])
@login_required
def api_bot_exec(process_name):
    try:
        data = request.get_json(force=True) or {}
        command = (data.get("command") or "").strip()
        if not command:
            return jsonify({"status": "error", "message": "Command is required"}), 400

        # Safety filter
        forbidden = ["rm -rf /", ":(){ :|:& };:", "dd if=", "mkfs"]
        if any(fb in command for fb in forbidden):
            return jsonify({"status": "error", "message": "Command not allowed for safety"}), 400

        safe_name = process_name.replace(' ', '_')
        bot_dir = APP_BASE_DIR / safe_name
        if not bot_dir.exists():
            bot_dir = Path.cwd()

        res = subprocess.run(command, shell=True, cwd=str(bot_dir), capture_output=True, text=True, timeout=15)
        return jsonify({
            "status": "success",
            "stdout": res.stdout,
            "stderr": res.stderr,
            "exit_code": res.returncode
        }), 200
    except subprocess.TimeoutExpired:
        return jsonify({"status": "error", "message": "Command execution timed out after 15 seconds"}), 408
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# ── General Settings & Telegram Alert API ─────────────────────
@app.route('/api/settings', methods=['GET', 'POST'])
@login_required
def api_settings():
    load_dotenv('cluster.env', override=True)
    if request.method == 'POST':
        data = request.get_json(force=True) or {}

        if data.get('admin_username'):
            update_general_setting("ADMIN_USERNAME", data['admin_username'])
        if data.get('admin_password'):
            update_general_setting("ADMIN_PASSWORD", data['admin_password'])
        if 'app_url' in data:
            update_general_setting("APP_URL", data['app_url'])
        if 'ping_interval' in data:
            update_general_setting("PING_INTERVAL", str(data['ping_interval']))
        if 'cron_restart_hours' in data:
            update_general_setting("CRON_RESTART_HOURS", str(data['cron_restart_hours']))
        if 'telegram_token' in data:
            update_general_setting("TELEGRAM_NOTIFY_TOKEN", data['telegram_token'])
        if 'telegram_chat_id' in data:
            update_general_setting("TELEGRAM_NOTIFY_CHAT_ID", data['telegram_chat_id'])

        return jsonify({"status": "success", "message": "Settings saved successfully!"}), 200

    return jsonify({
        "status": "success",
        "settings": {
            "admin_username": os.environ.get("ADMIN_USERNAME", "admin"),
            "app_url": os.environ.get("APP_URL", ""),
            "ping_interval": int(os.environ.get("PING_INTERVAL", 240)),
            "cron_restart_hours": int(os.environ.get("CRON_RESTART_HOURS", 0)),
            "telegram_token_configured": bool(os.environ.get("TELEGRAM_NOTIFY_TOKEN")),
            "telegram_chat_id": os.environ.get("TELEGRAM_NOTIFY_CHAT_ID", "")
        }
    }), 200

@app.route('/api/notifications/test', methods=['POST'])
@login_required
def api_test_telegram():
    success = send_telegram_alert("🔔 <b>BotClusters Test Notification</b>\nYour Telegram alert integration is configured and working perfectly!")
    if success:
        return jsonify({"status": "success", "message": "Test notification delivered successfully to Telegram!"}), 200
    else:
        return jsonify({"status": "error", "message": "Failed to send notification. Check token & chat ID."}), 400

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
            file = request.files['file']
            data = json.load(file)
        else:
            data = request.get_json(force=True) or {}

        imported, errors = import_clusters_from_dict(data)
        broadcast_status_update()
        return jsonify({
            "status": "success",
            "imported_count": imported,
            "errors": errors,
            "message": f"Successfully imported {imported} bot cluster(s)!"
        }), 200
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
        while True:
            if log_dir.exists():
                for log_file in sorted(log_dir.glob("*.log")):
                    if '_combined' in log_file.name:
                        continue
                    try:
                        pos = positions.get(log_file.name, 0)
                        size = log_file.stat().st_size
                        if size < pos:
                            pos = 0
                        if size > pos:
                            with log_file.open('r', errors='replace', encoding='utf-8') as fh:
                                fh.seek(pos)
                                new_data = fh.read()
                                positions[log_file.name] = fh.tell()
                            if new_data.strip():
                                payload = json.dumps({
                                    "file": log_file.name,
                                    "data": new_data
                                })
                                yield f"data: {payload}\n\n"
                    except Exception:
                        pass
            async_sleep(1)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        }
    )

# ── Cronjob Restart Config ──────────────────────────────────────
@app.route('/config/cron', methods=['GET', 'POST'])
@login_required
def config_cron():
    global CRON_RESTART_INTERVAL, _cron_thread
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        hours = int(data.get('hours', 0))
        CRON_RESTART_INTERVAL = max(0, hours)
        update_general_setting('CRON_RESTART_HOURS', str(CRON_RESTART_INTERVAL))
        _restart_cron_thread()
        return jsonify({"status": "success", "hours": CRON_RESTART_INTERVAL})
    return jsonify({"status": "success", "hours": CRON_RESTART_INTERVAL})

def _cron_restart_loop():
    while True:
        interval = CRON_RESTART_INTERVAL
        if interval <= 0:
            async_sleep(30)
            continue
        logger.info(f"Cron restart: scheduled in {interval} hours")
        async_sleep(interval * 3600)
        if CRON_RESTART_INTERVAL <= 0:
            continue
        logger.info("Cron restart: restarting all processes...")
        try:
            run_supervisor_command("restart", "all")
            broadcast_status_update()
        except Exception as e:
            logger.error(f"Cron restart error: {e}")

def _start_cron_thread():
    global _cron_thread
    if _cron_thread is None:
        _cron_thread = spawn_background_task(_cron_restart_loop)

def _restart_cron_thread():
    global _cron_thread
    if _cron_thread is not None:
        kill_background_task(_cron_thread)
        _cron_thread = None
    _start_cron_thread()

def _auto_delete_logs_loop():
    while True:
        async_sleep(24 * 3600)
        logger.info("Auto log cleanup: deleting all supervisor logs")
        try:
            log_dir = Path(SUPERVISOR_LOG_DIR)
            deleted = 0
            if log_dir.exists():
                for log_file in log_dir.glob("*.log"):
                    try:
                        log_file.unlink()
                        deleted += 1
                    except Exception as e:
                        logger.error(f"Failed to delete {log_file}: {e}")
            logger.info(f"Auto log cleanup: deleted {deleted} log files")
        except Exception as e:
            logger.error(f"Auto log cleanup error: {e}")

_log_cleanup_thread = None

def _start_log_cleanup_thread():
    global _log_cleanup_thread
    if _log_cleanup_thread is None:
        _log_cleanup_thread = spawn_background_task(_auto_delete_logs_loop)

# ── SocketIO Events ─────────────────────────────────────────────
@socketio.on('connect')
def handle_connect():
    if 'logged_in' not in session:
        return False
    logger.info("Authenticated client connected via SocketIO")
    emit('connected', {'data': 'Connected'})
    broadcast_status_update()

@socketio.on('disconnect')
def handle_disconnect():
    logger.info("Client disconnected from SocketIO")

@socketio.on('request_status')
def handle_status_request():
    if 'logged_in' not in session:
        emit('status_update', {"status": "error", "message": "Unauthorized", "processes": []})
        return

    try:
        payload = build_process_status_payload()
        emit('status_update', payload)
    except Exception as e:
        logger.error(f"Error in handle_status_request: {str(e)}")
        emit('status_update', {
            "status": "error",
            "message": str(e),
            "processes": []
        })

@app.errorhandler(Exception)
def handle_error(e):
    logger.error(f"Unhandled server error: {str(e)}", exc_info=True)
    return jsonify({
        "status": "error",
        "message": "An internal server error occurred"
    }), 500

# Start background loops
_start_cron_thread()
_start_log_cleanup_thread()
