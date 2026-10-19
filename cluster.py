import os
import sys
import time
import shutil
import signal
import logging
import threading
import subprocess

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [CLUSTER] - %(levelname)s - %(message)s'
)
logger = logging.getLogger("cluster")

CHILDREN = {}          # name -> Popen
_stopping = False


def spawn(name, cmd):
    """Start a child process and remember it so it can be stopped on shutdown."""
    try:
        CHILDREN[name] = subprocess.Popen(cmd)
        return CHILDREN[name]
    except Exception as e:
        logger.error(f"{name} failed to start: {e}")
        return None


def run_blocking(name, cmd):
    proc = spawn(name, cmd)
    if proc:
        proc.wait()
        if not _stopping and proc.returncode not in (0, None):
            logger.error(f"{name} exited with code {proc.returncode}")


def run_update_then_worker():
    """Order matters: update first (it may replace files), then set up bots from the NEW code."""
    run_blocking("update", [sys.executable, "update.py"])
    run_blocking("worker", [sys.executable, "worker.py"])


def run_supervisord():
    if shutil.which("supervisord"):
        logger.info("Starting supervisord daemon...")
        run_blocking("supervisord", ["supervisord", "-n", "-c", "supervisord.conf"])
    else:
        logger.warning("supervisord is not installed in PATH. Bot supervisor service skipped "
                       "(dashboard falls back to its built-in process runner).")


def stop_children():
    global _stopping
    if _stopping:
        return
    _stopping = True
    logger.info("Shutting down BotClusters gracefully...")
    for name, proc in CHILDREN.items():
        if proc.poll() is None:
            proc.terminate()
    deadline = time.time() + 15
    for proc in CHILDREN.values():
        try:
            proc.wait(timeout=max(0.1, deadline - time.time()))
        except Exception:
            proc.kill()


def shutdown(signum=None, frame=None):
    stop_children()
    sys.exit(0)


if __name__ == "__main__":
    logger.info("Initializing BotClusters...")
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Web server first so Koyeb/Render health checks pass immediately
    web = spawn("web", [sys.executable, "run.py"])
    port = os.environ.get("PORT", "5000")
    logger.info(f"Web server started on port {port}")

    for name, target in (("supervisord", run_supervisord),
                         ("setup", run_update_then_worker),
                         ("ping", lambda: run_blocking("ping", [sys.executable, "ping_server.py"]))):
        threading.Thread(target=target, name=name, daemon=True).start()

    logger.info("All BotClusters background services started.")

    while True:
        time.sleep(1)
        if web is None or web.poll() is not None:
            logger.error("Web server stopped unexpectedly - exiting so the platform restarts the container.")
            stop_children()
            sys.exit(1)
