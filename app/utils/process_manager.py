import os
import re
import sys
import time
import shutil
import signal
import logging
import psutil
import glob
import shlex
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional

from app.utils.cluster_config import (
    APP_BASE_DIR, SUPERVISORD_CONF_DIR, SUPERVISOR_LOG_DIR,
    get_configured_clusters, mask_url, invalidate_git_cache, parse_runtime
)

logger = logging.getLogger(__name__)

# Fallback in-memory process tracking for environments without supervisorctl
_DEV_PROCESSES: Dict[str, Dict[str, Any]] = {}

def has_supervisorctl() -> bool:
    return shutil.which("supervisorctl") is not None

def _read_int(path: str) -> Optional[int]:
    try:
        raw = Path(path).read_text().strip()
        if raw and raw != "max":
            return int(raw)
    except Exception:
        pass
    return None


def _cgroup_memory() -> Optional[Dict[str, int]]:
    """Container memory limit/usage in bytes (Koyeb/Render/Docker), else None."""
    total = psutil.virtual_memory().total
    limit = _read_int("/sys/fs/cgroup/memory.max") or _read_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    used = _read_int("/sys/fs/cgroup/memory.current") or _read_int("/sys/fs/cgroup/memory/memory.usage_in_bytes")
    if not limit or not used or limit >= total:
        return None
    # page cache is reclaimable -> do not count it as "used"
    for stat_file, key in (("/sys/fs/cgroup/memory.stat", "inactive_file"),
                           ("/sys/fs/cgroup/memory/memory.stat", "total_inactive_file")):
        try:
            for line in Path(stat_file).read_text().splitlines():
                k, _, v = line.partition(" ")
                if k == key:
                    used = max(0, used - int(v))
                    break
            break
        except Exception:
            continue
    return {"limit": limit, "used": min(used, limit)}


def _format_uptime(seconds: int) -> str:
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return f"{d}d {h}h {m}m" if d else f"{h}h {m}m"


def get_system_metrics() -> Dict[str, Any]:
    """Live CPU, memory (container-aware), disk and uptime."""
    try:
        cpu_percent = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()
        cg = _cgroup_memory()
        if cg:
            total_ram_mb = round(cg["limit"] / (1024 * 1024))
            used_ram_mb = round(cg["used"] / (1024 * 1024))
            ram_percent = round(cg["used"] * 100 / cg["limit"], 1)
        else:
            total_ram_mb = round(vm.total / (1024 * 1024))
            used_ram_mb = round((vm.total - vm.available) / (1024 * 1024))
            ram_percent = vm.percent
        free_ram_mb = max(0, total_ram_mb - used_ram_mb)

        try:
            disk = psutil.disk_usage(str(Path.cwd()))
            total_disk_gb = round(disk.total / (1024 ** 3), 1)
            used_disk_gb = round(disk.used / (1024 ** 3), 1)
            disk_percent = disk.percent
        except Exception:
            total_disk_gb = used_disk_gb = disk_percent = 0

        try:
            # PID 1 start = container start (host boot time is misleading in Docker)
            started = psutil.Process(1).create_time()
        except Exception:
            started = psutil.boot_time()
        uptime_str = _format_uptime(max(0, int(time.time() - started)))

        return {
            "cpu_percent": cpu_percent,
            "ram_total_mb": total_ram_mb,
            "ram_used_mb": used_ram_mb,
            "ram_free_mb": free_ram_mb,
            "ram_percent": ram_percent,
            "ram_is_container_limit": bool(cg),
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
            "cpu_percent": 0, "ram_total_mb": 0, "ram_used_mb": 0, "ram_free_mb": 0,
            "ram_percent": 0, "disk_total_gb": 0, "disk_used_gb": 0, "disk_percent": 0,
            "uptime": "N/A", "python_version": sys.version.split()[0], "platform": sys.platform
        }


# psutil needs the SAME Process object between calls, otherwise cpu_percent() is always 0.0
_PROC_CACHE: Dict[int, "psutil.Process"] = {}


def _get_proc(pid: int) -> "psutil.Process":
    proc = _PROC_CACHE.get(pid)
    if proc is None:
        proc = psutil.Process(pid)
        proc.cpu_percent(interval=None)  # prime
        _PROC_CACHE[pid] = proc
        if len(_PROC_CACHE) > 200:
            for dead in [k for k, v in _PROC_CACHE.items() if not v.is_running()]:
                _PROC_CACHE.pop(dead, None)
    return proc


def get_process_resource_usage(pid: Optional[int]) -> Dict[str, Any]:
    """RSS memory (MB) and CPU % for a PID, including child processes."""
    if not pid:
        return {"memory_mb": 0, "cpu_percent": 0}
    try:
        proc = _get_proc(int(pid))
        mem = proc.memory_info().rss
        cpu = proc.cpu_percent(interval=None)
        try:
            for child in proc.children(recursive=True):
                try:
                    c = _get_proc(child.pid)
                    mem += c.memory_info().rss
                    cpu += c.cpu_percent(interval=None)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except Exception:
            pass
        return {"memory_mb": round(mem / (1024 * 1024), 1), "cpu_percent": round(cpu, 1)}
    except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
        _PROC_CACHE.pop(int(pid), None) if str(pid).isdigit() else None
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

def get_pyenv_python(version: Optional[str]) -> str:
    """Interpreter for a requested python version (e.g. '3.11'), else current one."""
    default = sys.executable or shutil.which("python3") or "python3"
    if not version:
        return default
    major_minor = '.'.join(str(version).split('.')[:2])

    for name in (f"python{major_minor}", f"python{major_minor}.exe", f"python-{major_minor}"):
        shim = shutil.which(name)
        if shim:
            return shim

    try:
        res = subprocess.run(["uv", "python", "find", major_minor], capture_output=True, text=True, timeout=5)
        if res.returncode == 0 and res.stdout.strip():
            cand = res.stdout.strip()
            if Path(cand).exists():
                return cand
    except Exception:
        pass

    candidates = [
        Path(f"/usr/local/bin/python{major_minor}"),
        Path(f"/usr/bin/python{major_minor}"),
    ] + list(Path("/opt/python").glob(f"cpython-{major_minor}*/bin/python3"))
    for cand in candidates:
        if cand.exists():
            return cand.as_posix()

    logger.warning(f"python{major_minor} not found in PATH, falling back to {default}")
    return default


def get_node_bin(version: Optional[str] = "22", bin_name: str = "node") -> str:
    """
    Find binary (node, npm, npx) for a given version ('20', '22', 'latest').
    Checks versioned shims, 'n' directories, and system PATH.
    """
    v_str = str(version or "").strip().lower()
    clean_ver = "20" if "20" in v_str else ("22" if "22" in v_str else "")

    # 1. Versioned shims in PATH (e.g. node20, npm20, node22, npm22)
    if clean_ver:
        shim = shutil.which(f"{bin_name}{clean_ver}") or shutil.which(f"{bin_name}{clean_ver}.cmd") or shutil.which(f"{bin_name}{clean_ver}.exe")
        if shim:
            return shim

    # 2. 'n' version manager directory structure (/usr/local/n/versions/node/...)
    if clean_ver:
        pattern = f"/usr/local/n/versions/node/{clean_ver}*/bin/{bin_name}"
        matches = glob.glob(pattern)
        if matches:
            return matches[-1]

    # 3. Dedicated installation folders
    if clean_ver:
        cand = Path(f"/usr/local/node-v{clean_ver}/bin/{bin_name}")
        if cand.exists():
            return cand.as_posix()

    # 4. Global system PATH
    sys_bin = shutil.which(bin_name)
    if sys_bin:
        return sys_bin

    return bin_name


def _run(cmd, **kw):
    """subprocess.run that raises RuntimeError with the real stderr (not just 'exit status 1')."""
    res = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if res.returncode != 0:
        tail = (res.stderr or res.stdout or "").strip()[-600:]
        raise RuntimeError(f"{mask_url(' '.join(map(str, cmd[:4])))} failed: {mask_url(tail)}")
    return res


def install_requirements(bot_dir: Path, python_version: Optional[str] = None) -> str:
    """Create venv if missing and pip install requirements. Returns pip output."""
    venv_dir = bot_dir / 'venv'
    req = bot_dir / 'requirements.txt'
    if not req.exists():
        return "No requirements.txt"
    env = os.environ.copy()
    if python_version:
        env["PYENV_VERSION"] = str(python_version)
    try:
        if not venv_dir.exists():
            _run([get_pyenv_python(python_version), '-m', 'venv', str(venv_dir)], env=env, timeout=300)
        res = _run([str(get_venv_bin(venv_dir, 'pip')), 'install', '--no-cache-dir', '-r', str(req)],
                   cwd=str(bot_dir), env=env, timeout=1800)
        return (res.stdout or "")[-2000:]
    except Exception:
        # half-built venv would be skipped forever on the next start -> remove it
        shutil.rmtree(venv_dir, ignore_errors=True)
        raise


def install_node_dependencies(bot_dir: Path, node_version: Optional[str] = None) -> str:
    """Run npm install for Node.js projects if package.json exists."""
    pkg = bot_dir / 'package.json'
    if not pkg.exists():
        return "No package.json"
    npm_bin = get_node_bin(node_version, 'npm')
    node_bin = get_node_bin(node_version, 'node')
    node_dir = Path(node_bin).parent.as_posix()

    env = os.environ.copy()
    is_windows = os.name == 'nt' or sys.platform == 'win32'
    path_sep = ';' if is_windows else ':'
    env["PATH"] = f"{node_dir}{path_sep}{str(bot_dir / 'node_modules' / '.bin')}{path_sep}{env.get('PATH', '')}"

    logger.info(f"Running npm install in {bot_dir.name} using {npm_bin}...")
    try:
        cmd = [npm_bin, "install", "--prefer-offline", "--no-audit", "--no-fund"]
        res = _run(cmd, cwd=str(bot_dir), env=env, timeout=1800)
        return (res.stdout or "")[-2000:]
    except Exception as e:
        logger.error(f"npm install failed for {bot_dir.name}: {e}")
        raise


def build_run_command(run_cmd: str, bot_dir: Path, exec_bin: str, runtime: str = "python", node_version: Optional[str] = None) -> str:
    """Turn the configured run_command into a shell command line for Python or Node.js."""
    q = shlex.quote
    cmd_str = (run_cmd or "").strip()
    if not cmd_str:
        cmd_str = "index.js" if runtime == "node" else "bot.py"

    parts = shlex.split(cmd_str, posix=True)
    first, rest = parts[0], parts[1:]

    # Shell script execution is universal
    if first.endswith(".sh"):
        return " ".join(["bash", q((bot_dir / first).as_posix())] + [q(x) for x in rest])

    if runtime == "node":
        node_bin = exec_bin or get_node_bin(node_version, 'node')
        npm_bin = get_node_bin(node_version, 'npm')

        if first in ("npm", "npx"):
            actual_npm = npm_bin if first == "npm" else get_node_bin(node_version, 'npx')
            return " ".join([q(actual_npm)] + [q(x) for x in rest])

        if first in ("node", "nodejs"):
            return " ".join([q(node_bin)] + [q(x) for x in rest])

        if first in ("yarn", "pnpm"):
            bin_tool = shutil.which(first) or first
            return " ".join([q(bin_tool)] + [q(x) for x in rest])

        if first.endswith((".js", ".mjs", ".cjs", ".ts")):
            script_path = (bot_dir / first).as_posix() if (bot_dir / first).exists() else first
            return " ".join([q(node_bin), q(script_path)] + [q(x) for x in rest])

        if (bot_dir / f"{first}.js").exists():
            return " ".join([q(node_bin), q((bot_dir / f"{first}.js").as_posix())] + [q(x) for x in rest])

        return " ".join([q(node_bin), q(first)] + [q(x) for x in rest])

    # Python runtime
    py = exec_bin
    if first in ("python", "python3", "py"):
        return " ".join([q(py)] + [q(x) for x in rest])
    if first.endswith(".py"):
        return " ".join([q(py), q((bot_dir / first).as_posix())] + [q(x) for x in rest])
    if first == "-m" and rest:
        return " ".join([q(py), "-m"] + [q(x) for x in rest])
    module = first[:-3] if first.endswith(".py") else first   # keep dotted module paths (pkg.sub)
    return " ".join([q(py), "-m", q(module)] + [q(x) for x in rest])


def _file_stamp(path: Path) -> str:
    try:
        import hashlib
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return ""


def _deps_stale(bot_dir: Path, manifest: str, stamp_dir: Path) -> bool:
    """True when manifest differs from the one last installed (stamp saved by _save_stamp)."""
    stamp = stamp_dir / ".deps_stamp"
    try:
        return stamp.read_text().strip() != _file_stamp(bot_dir / manifest)
    except Exception:
        return True


def _save_stamp(bot_dir: Path, manifest: str, stamp_dir: Path):
    try:
        (stamp_dir / ".deps_stamp").write_text(_file_stamp(bot_dir / manifest))
    except Exception:
        pass


def prepare_bot_environment(cluster: dict, force_reclone: bool = False) -> tuple[Path, str]:
    """Clone repo + install dependencies (Python or Node.js). Returns (bot_dir, run_command_str)."""
    bot_name = cluster['bot_name']
    safe_name = cluster.get('safe_name', bot_name.replace(" ", "_"))
    bot_dir = APP_BASE_DIR / safe_name
    venv_dir = bot_dir / 'venv'
    branch = cluster.get('branch') or 'main'
    git_url = cluster.get('git_url', '')

    bot_dir.parent.mkdir(parents=True, exist_ok=True)

    if force_reclone and bot_dir.exists():
        shutil.rmtree(bot_dir, ignore_errors=True)
        invalidate_git_cache(bot_dir)

    if not (bot_dir / '.git').exists() and git_url:
        logger.info(f"Cloning {bot_name} from {mask_url(git_url)} branch {branch} into {bot_dir}")
        if bot_dir.exists():
            shutil.rmtree(bot_dir, ignore_errors=True)
        _run(['git', 'clone', '-b', branch, '--single-branch', git_url, str(bot_dir)], timeout=600)
        invalidate_git_cache(bot_dir)

    # Determine runtime
    runtime = cluster.get("runtime")
    run_cmd = cluster.get('run_command') or ''
    if not runtime:
        runtime, r_ver, _ = parse_runtime(cluster.get("python_version") or cluster.get("runtime_version"), bot_dir, run_cmd)
    else:
        r_ver = cluster.get("runtime_version") or cluster.get("node_version") or cluster.get("python_version")

    if runtime == "node":
        node_version = r_ver or cluster.get("node_version") or "22"
        nm_dir = bot_dir / 'node_modules'
        if (bot_dir / 'package.json').exists() and (not nm_dir.exists() or _deps_stale(bot_dir, 'package.json', nm_dir)):
            logger.info(f"Installing Node.js dependencies for {bot_name} (Node {node_version})...")
            install_node_dependencies(bot_dir, node_version)
            _save_stamp(bot_dir, 'package.json', nm_dir)
        node_bin = get_node_bin(node_version, 'node')
        return bot_dir, build_run_command(run_cmd or 'index.js', bot_dir, node_bin, runtime="node", node_version=node_version)

    # Default: Python
    python_version = r_ver or cluster.get("python_version")
    if (bot_dir / 'requirements.txt').exists() and (not venv_dir.exists() or _deps_stale(bot_dir, 'requirements.txt', venv_dir)):
        logger.info(f"Installing requirements for {bot_name} (Python {python_version or 'default'})...")
        install_requirements(bot_dir, python_version)
        _save_stamp(bot_dir, 'requirements.txt', venv_dir)

    py = get_venv_bin(venv_dir, 'python').as_posix() if venv_dir.exists() else get_pyenv_python(python_version)
    return bot_dir, build_run_command(run_cmd or 'bot.py', bot_dir, py, runtime="python")


_ENV_KEY_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _supervisor_env_string(env: dict) -> str:
    """Build a supervisor `environment=` value. Handles % (supervisor interpolation) and quotes."""
    pairs = []
    for k, v in (env or {}).items():
        k, v = str(k).strip(), str(v)
        if not _ENV_KEY_RE.match(k):
            raise ValueError(f"Bad env variable name: '{k}'")
        if "\n" in v or "\r" in v:
            raise ValueError(f"Env variable {k} contains a line break")
        v = v.replace('%', '%%')
        if '"' not in v:
            pairs.append(f'{k}="{v}"')
        elif "'" not in v:
            pairs.append(f"{k}='{v}'")
        else:
            raise ValueError(f"Env variable {k} contains both ' and \" - not supported")
    return ','.join(pairs)


def write_supervisor_config_file(cluster: dict, command: str, autostart: bool = True) -> Path:
    safe_name = cluster.get('safe_name', cluster['bot_name'].replace(" ", "_"))
    SUPERVISORD_CONF_DIR.mkdir(parents=True, exist_ok=True)
    SUPERVISOR_LOG_DIR.mkdir(parents=True, exist_ok=True)

    config_path = SUPERVISORD_CONF_DIR / f"{safe_name}.conf"
    bot_dir = APP_BASE_DIR / safe_name

    env_copy = dict(cluster.get('env') or {})
    runtime = cluster.get("runtime")
    if not runtime:
        runtime, _, _ = parse_runtime(cluster.get("python_version"), bot_dir, cluster.get("run_command"))

    if runtime == "node":
        node_bin = get_node_bin(cluster.get("node_version") or cluster.get("runtime_version"), 'node')
        node_dir = Path(node_bin).parent.as_posix()
        nm_bin = (bot_dir / "node_modules" / ".bin").as_posix()
        curr_path = env_copy.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        env_copy["PATH"] = f"{nm_bin}:{node_dir}:{curr_path}"

    env_vars = _supervisor_env_string(env_copy)

    stdout_path = (SUPERVISOR_LOG_DIR / f"{safe_name}_out.log").as_posix()
    stderr_path = (SUPERVISOR_LOG_DIR / f"{safe_name}_err.log").as_posix()

    lines = [
        f"[program:{safe_name}]",
        f"command={command}",
        f"directory={bot_dir.as_posix()}",
        f"autostart={'true' if autostart else 'false'}",
        "autorestart=true",
        "startretries=12",
        "stopasgroup=true",      # kill bot's child processes too (no orphans on restart)
        "killasgroup=true",
        "stopwaitsecs=10",
        "stdout_logfile_maxbytes=10MB",
        "stdout_logfile_backups=1",
        "stderr_logfile_maxbytes=10MB",
        "stderr_logfile_backups=1",
        f"stderr_logfile={stderr_path}",
        f"stdout_logfile={stdout_path}",
    ]
    if env_vars:
        lines.append(f"environment={env_vars}")
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config_path


def remove_supervisor_config(safe_name: str):
    conf = SUPERVISORD_CONF_DIR / f"{safe_name}.conf"
    try:
        conf.unlink()
    except FileNotFoundError:
        pass


# Fallback runner for systems without supervisorctl (Windows / local dev)
def _fallback_start(process_name: str) -> Dict[str, Any]:
    clusters = get_configured_clusters()
    match = next((c for c in clusters if c['safe_name'] == process_name or c['bot_name'] == process_name), None)
    if not match:
        return {"status": "error", "message": f"Cluster not configured: {process_name}"}
    name = match['safe_name']

    old = _DEV_PROCESSES.get(name)
    if old and old["proc"].poll() is None:
        return {"status": "success", "message": f"{name} already running (PID: {old['pid']})"}

    try:
        bot_dir, command = prepare_bot_environment(match)
    except Exception as e:
        return {"status": "error", "message": str(e)}

    SUPERVISOR_LOG_DIR.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    for k, v in (match.get('env') or {}).items():
        env[str(k)] = str(v)

    with open(SUPERVISOR_LOG_DIR / f"{name}_out.log", "ab") as out_f, \
         open(SUPERVISOR_LOG_DIR / f"{name}_err.log", "ab") as err_f:
        proc = subprocess.Popen(command, shell=True, cwd=str(bot_dir), stdout=out_f, stderr=err_f, env=env)
    _DEV_PROCESSES[name] = {"proc": proc, "pid": proc.pid, "start_time": time.time(), "command": command}
    return {"status": "success", "message": f"Started {name} (PID: {proc.pid})"}


def _fallback_stop(process_name: str) -> Dict[str, Any]:
    name = process_name.replace(' ', '_')
    info = _DEV_PROCESSES.pop(name, None)
    if info:
        proc = info["proc"]
        try:
            parent = psutil.Process(proc.pid)
            for child in parent.children(recursive=True):
                child.terminate()
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        return {"status": "success", "message": f"Stopped {name}"}
    return {"status": "success", "message": f"{name} already stopped"}


def _fallback_status() -> str:
    lines = []
    for c in get_configured_clusters():
        name = c['safe_name']
        info = _DEV_PROCESSES.get(name)
        if not info:
            lines.append(f"{name:<20} STOPPED   Not started")
            continue
        proc = info["proc"]
        code = proc.poll()
        if code is None:
            up = int(time.time() - info["start_time"])
            h, r = divmod(up, 3600)
            m, s = divmod(r, 60)
            lines.append(f"{name:<20} RUNNING   pid {proc.pid}, uptime {h}:{m:02d}:{s:02d}")
        else:
            lines.append(f"{name:<20} EXITED    exit status {code}")
    return "\n".join(lines)
