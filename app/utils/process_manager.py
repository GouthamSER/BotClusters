import os
import re
import sys
import time
import shutil
import signal
import logging
import psutil
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional

from app.utils.cluster_config import (
    APP_BASE_DIR, SUPERVISORD_CONF_DIR, SUPERVISOR_LOG_DIR,
    get_configured_clusters
)

logger = logging.getLogger(__name__)

# Fallback in-memory process tracking for environments without supervisorctl
_DEV_PROCESSES: Dict[str, Dict[str, Any]] = {}

def has_supervisorctl() -> bool:
    return shutil.which("supervisorctl") is not None

def get_system_metrics() -> Dict[str, Any]:
    """Retrieve live CPU, Memory, Disk and Host performance metrics."""
    try:
        cpu_percent = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()
        total_ram_mb = round(vm.total / (1024 * 1024))
        used_ram_mb = round(vm.used / (1024 * 1024))
        free_ram_mb = round(vm.available / (1024 * 1024))
        ram_percent = vm.percent

        try:
            disk = psutil.disk_usage(str(Path.cwd()))
            total_disk_gb = round(disk.total / (1024 ** 3), 1)
            used_disk_gb = round(disk.used / (1024 ** 3), 1)
            disk_percent = disk.percent
        except Exception:
            total_disk_gb = 0
            used_disk_gb = 0
            disk_percent = 0

        boot_time = psutil.boot_time()
        uptime_seconds = int(time.time() - boot_time)
        hours, remainder = divmod(uptime_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f"{hours}h {minutes}m"

        return {
            "cpu_percent": cpu_percent,
            "ram_total_mb": total_ram_mb,
            "ram_used_mb": used_ram_mb,
            "ram_free_mb": free_ram_mb,
            "ram_percent": ram_percent,
            "disk_total_gb": total_disk_gb,
            "disk_used_gb": used_disk_gb,
            "disk_percent": disk_percent,
            "uptime": uptime_str,
            "python_version": sys.version.split()[0],
            "platform": sys.platform
        }
    except Exception as e:
        logger.error(f"Error reading system metrics: {e}")
        return {
            "cpu_percent": 0,
            "ram_total_mb": 0,
            "ram_used_mb": 0,
            "ram_percent": 0,
            "disk_total_gb": 0,
            "disk_used_gb": 0,
            "disk_percent": 0,
            "uptime": "N/A",
            "python_version": sys.version.split()[0],
            "platform": sys.platform
        }

def get_process_resource_usage(pid: Optional[int]) -> Dict[str, Any]:
    """Retrieve memory in MB (RSS) and CPU percent for a given PID."""
    if not pid:
        return {"memory_mb": 0, "cpu_percent": 0}
    try:
        proc = psutil.Process(int(pid))
        mem_info = proc.memory_info()
        mem_mb = round(mem_info.rss / (1024 * 1024), 1)

        # Include child processes memory (e.g. if bot forks)
        try:
            for child in proc.children(recursive=True):
                mem_mb += round(child.memory_info().rss / (1024 * 1024), 1)
        except Exception:
            pass

        return {
            "memory_mb": mem_mb,
            "cpu_percent": proc.cpu_percent(interval=None)
        }
    except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
        return {"memory_mb": 0, "cpu_percent": 0}

def get_venv_bin(venv_dir: Path, bin_name: str) -> Path:
    is_windows = os.name == 'nt' or sys.platform == 'win32'
    dirs = [venv_dir / ('Scripts' if is_windows else 'bin'), venv_dir / 'bin', venv_dir / 'Scripts']
    exts = ['.exe', ''] if is_windows else ['', '.exe']
    for d in dirs:
        for ext in exts:
            target = d / f"{bin_name}{ext}"
            if target.exists():
                return target
    primary = venv_dir / ('Scripts' if is_windows else 'bin')
    return primary / (f"{bin_name}.exe" if is_windows else bin_name)

def prepare_bot_environment(cluster: dict) -> tuple[Path, str]:
    """Clone repository and setup virtualenv if needed, returns (bot_dir, run_command_str)."""
    bot_name = cluster['bot_name']
    safe_name = cluster.get('safe_name', bot_name.replace(" ", "_"))
    bot_dir = APP_BASE_DIR / safe_name
    venv_dir = bot_dir / 'venv'
    branch = cluster.get('branch', 'main')
    git_url = cluster.get('git_url', '')

    bot_dir.parent.mkdir(parents=True, exist_ok=True)

    if not (bot_dir / '.git').exists() and git_url:
        logger.info(f"Cloning {bot_name} from {git_url} branch {branch} into {bot_dir}")
        if bot_dir.exists():
            shutil.rmtree(bot_dir, ignore_errors=True)
        subprocess.run(['git', 'clone', '-b', branch, '--single-branch', git_url, str(bot_dir)], check=True)

    requirements_file = bot_dir / 'requirements.txt'
    python_version = cluster.get("python_version")
    python_exec = sys.executable or shutil.which("python3") or "python3"

    if requirements_file.exists() and not venv_dir.exists():
        logger.info(f"Creating venv for {bot_name}...")
        subprocess.run([python_exec, '-m', 'venv', str(venv_dir)], check=True)
        pip_bin = str(get_venv_bin(venv_dir, 'pip'))
        subprocess.run([pip_bin, 'install', '--no-cache-dir', '-r', str(requirements_file)], check=True)

    # Determine command
    venv_python = get_venv_bin(venv_dir, 'python')
    py_to_run = venv_python.as_posix() if venv_dir.exists() else python_exec

    run_cmd = cluster.get('run_command', 'bot.py')
    bot_file = bot_dir / run_cmd

    if run_cmd.endswith(".sh"):
        final_cmd = f"bash {bot_file.as_posix()}"
    elif run_cmd.endswith(".py"):
        final_cmd = f"{py_to_run} {bot_file.as_posix()}"
    else:
        # e.g. python -m something
        final_cmd = f"{py_to_run} -m {Path(run_cmd).stem}"

    return bot_dir, final_cmd

def write_supervisor_config_file(cluster: dict, command: str) -> Path:
    safe_name = cluster.get('safe_name', cluster['bot_name'].replace(" ", "_"))
    SUPERVISORD_CONF_DIR.mkdir(parents=True, exist_ok=True)
    SUPERVISOR_LOG_DIR.mkdir(parents=True, exist_ok=True)

    config_path = SUPERVISORD_CONF_DIR / f"{safe_name}.conf"
    bot_dir = APP_BASE_DIR / safe_name
    env_vars = ','.join([f'{k}="{v}"' for k, v in cluster.get('env', {}).items()]) if cluster.get('env') else ""

    stdout_path = (SUPERVISOR_LOG_DIR / f"{safe_name}_out.log").as_posix()
    stderr_path = (SUPERVISOR_LOG_DIR / f"{safe_name}_err.log").as_posix()

    content = f"""[program:{safe_name}]
command={command}
directory={bot_dir.as_posix()}
autostart=true
autorestart=true
startretries=12
stderr_logfile={stderr_path}
stdout_logfile={stdout_path}
{f"environment={env_vars}" if env_vars else ""}
"""
    config_path.write_text(content.strip(), encoding="utf-8")
    return config_path

# Fallback runner implementation for systems without supervisorctl
def _fallback_start(process_name: str) -> Dict[str, Any]:
    global _DEV_PROCESSES
    clusters = get_configured_clusters()
    match = next((c for c in clusters if c['safe_name'] == process_name or c['bot_name'] == process_name), None)
    if not match:
        return {"status": "error", "message": f"Cluster not configured: {process_name}"}

    bot_dir, command = prepare_bot_environment(match)
    SUPERVISOR_LOG_DIR.mkdir(parents=True, exist_ok=True)
    stdout_f = open(SUPERVISOR_LOG_DIR / f"{process_name}_out.log", "a", encoding="utf-8", errors="replace")
    stderr_f = open(SUPERVISOR_LOG_DIR / f"{process_name}_err.log", "a", encoding="utf-8", errors="replace")

    env = os.environ.copy()
    for k, v in match.get('env', {}).items():
        env[str(k)] = str(v)

    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=str(bot_dir),
        stdout=stdout_f,
        stderr=stderr_f,
        env=env
    )
    _DEV_PROCESSES[process_name] = {
        "proc": proc,
        "pid": proc.pid,
        "start_time": time.time(),
        "status": "RUNNING",
        "command": command
    }
    return {"status": "success", "message": f"Started {process_name} (PID: {proc.pid})"}

def _fallback_stop(process_name: str) -> Dict[str, Any]:
    global _DEV_PROCESSES
    if process_name in _DEV_PROCESSES:
        info = _DEV_PROCESSES[process_name]
        proc = info.get("proc")
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        del _DEV_PROCESSES[process_name]
        return {"status": "success", "message": f"Stopped {process_name}"}
    return {"status": "success", "message": f"{process_name} already stopped"}

def _fallback_status() -> str:
    global _DEV_PROCESSES
    lines = []
    clusters = get_configured_clusters()
    configured_names = {c['safe_name'] for c in clusters}

    for name in configured_names:
        if name in _DEV_PROCESSES:
            info = _DEV_PROCESSES[name]
            proc = info.get("proc")
            if proc and proc.poll() is None:
                uptime = int(time.time() - info.get("start_time", time.time()))
                h, r = divmod(uptime, 3600)
                m, s = divmod(r, 60)
                lines.append(f"{name:<20} RUNNING   pid {proc.pid}, uptime {h}:{m:02d}:{s:02d}")
            else:
                lines.append(f"{name:<20} STOPPED")
        else:
            lines.append(f"{name:<20} STOPPED")
    return "\n".join(lines)
