"""
BotClusters boot worker.

Runs once at container start (started by cluster.py):
  1. waits until supervisord is ready
  2. removes supervisor programs of bots that are no longer configured
  3. clones / updates every configured bot, builds its venv, writes its supervisor conf
  4. tells supervisor to load the confs (bots then start automatically)

It uses the SAME config reader and setup code as the web dashboard, so names, folders,
log paths and python versions always match what the dashboard shows. One broken bot
never blocks the others.
"""
import os
os.environ["BOTCLUSTERS_NO_WEB"] = "1"      # import app.utils without booting the web app

import sys
import time
import shutil
import logging
import argparse
import subprocess
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

load_dotenv('cluster.env', override=True)

from app.utils.cluster_config import (       # noqa: E402
    SUPERVISORD_CONF_DIR, APP_BASE_DIR, get_configured_clusters,
    send_telegram_alert, set_worker_state, mask_url
)
from app.utils.process_manager import (      # noqa: E402
    prepare_bot_environment, write_supervisor_config_file, _supervisor_env_string
)

LOG_FILE = 'bot_manager.log'
file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3, encoding='utf-8')
console_handler = logging.StreamHandler()
fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
for h in (file_handler, console_handler):
    h.setFormatter(fmt)
root = logging.getLogger()
root.handlers.clear()
root.addHandler(file_handler)
root.addHandler(console_handler)
root.setLevel(logging.INFO)

MAX_PARALLEL_SETUPS = 3
HAS_SUPERVISORCTL = shutil.which("supervisorctl") is not None


def supervisorctl(*args, timeout=60):
    return subprocess.run(["supervisorctl", *args], capture_output=True, text=True, timeout=timeout)


def wait_for_supervisord(timeout=90):
    """supervisord is started in parallel by cluster.py - `reread/update` fail if we beat it."""
    if not HAS_SUPERVISORCTL:
        logging.warning("supervisorctl not found - only writing configs")
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            res = supervisorctl("status", timeout=10)
            # exit 0 = all running, 3 = some not running; both mean "daemon answered"
            if res.returncode in (0, 3):
                return True
        except Exception:
            pass
        time.sleep(2)
    logging.error("supervisord did not become ready in time")
    return False


def remove_stale_programs(configured_names):
    """Delete supervisor programs for bots that are no longer configured."""
    if not SUPERVISORD_CONF_DIR.exists():
        return
    for conf in SUPERVISORD_CONF_DIR.glob("*.conf"):
        if conf.stem in configured_names:
            continue
        logging.info(f"Removing stale supervisor program: {conf.stem}")
        if HAS_SUPERVISORCTL:
            try:
                supervisorctl("stop", conf.stem, timeout=30)
            except Exception:
                pass
        try:
            conf.unlink()
        except Exception:
            pass


def refresh_repo(bot_dir):
    """Existing clone (persistent disk): fast-forward to latest code. Never fatal."""
    if (bot_dir / ".git").exists():
        try:
            subprocess.run(["git", "pull", "--ff-only", "-q"], cwd=bot_dir, capture_output=True, timeout=120)
        except Exception as e:
            logging.warning(f"git pull skipped for {bot_dir.name}: {e}")


def setup_one(bot):
    name = bot["safe_name"]
    try:
        set_worker_state(name, "INSTALLING", "Cloning repository and installing dependencies…")
        _supervisor_env_string(bot.get("env") or {})
        refresh_repo(APP_BASE_DIR / name)
        _, cmd = prepare_bot_environment(bot)
        write_supervisor_config_file(bot, cmd, autostart=True)
        set_worker_state(name, None)
        logging.info(f"[{name}] ready: {cmd}")
        return name, None
    except Exception as e:
        msg = mask_url(str(e))[-500:]
        logging.error(f"[{name}] setup failed: {msg}")
        set_worker_state(name, "SETUP_FAILED", msg)
        send_telegram_alert(f"❌ <b>BotClusters</b>\nSetup of <code>{name}</code> failed at boot:\n<code>{msg[-300:]}</code>")
        return name, msg


def main():
    parser = argparse.ArgumentParser(description='Bot Manager')
    parser.add_argument('--restart', action='store_true', help='Stop all configured bots')
    args = parser.parse_args()

    clusters = get_configured_clusters()
    logging.info(f"Found {len(clusters)} configured bot(s): {[c['safe_name'] for c in clusters]}")

    try:
        os.remove("worker_state.json")          # drop stale INSTALLING/FAILED flags from last run
    except OSError:
        pass

    supervisor_ready = wait_for_supervisord()

    if args.restart:
        for c in clusters:
            if supervisor_ready:
                supervisorctl("stop", c["safe_name"])
        return

    remove_stale_programs({c["safe_name"] for c in clusters})

    if not clusters:
        logging.info("No clusters configured. Add bots from the dashboard.")
        if supervisor_ready:
            supervisorctl("reread")
            supervisorctl("update")
        return

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_SETUPS) as pool:
        results = list(pool.map(setup_one, clusters))

    failed = [n for n, err in results if err]
    if supervisor_ready:
        supervisorctl("reread")
        supervisorctl("update", timeout=120)
        logging.info("Supervisor updated.")
    logging.info(f"Worker finished: {len(results) - len(failed)} ready, {len(failed)} failed {failed or ''}")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        logging.info("Worker interrupted")
    except Exception:
        logging.exception("Worker crashed")
        sys.exit(1)
