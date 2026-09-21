import os
import sys
import time
import shutil
import logging
import threading
import subprocess

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [CLUSTER] - %(levelname)s - %(message)s'
)
logger = logging.getLogger("cluster")

def run_update():
    logger.info("Running update check...")
    try:
        subprocess.run([sys.executable, "update.py"])
    except Exception as e:
        logger.error(f"Error running update.py: {e}")

def run_web_server():
    port = os.environ.get("PORT", "5000")
    logger.info(f"Starting web server via run.py on port {port}...")
    try:
        subprocess.run([sys.executable, "run.py"])
    except Exception as e:
        logger.error(f"Web server failed: {e}")

def run_supervisord():
    if shutil.which("supervisord"):
        logger.info("Starting supervisord daemon...")
        try:
            subprocess.run(["supervisord", "-n", "-c", "supervisord.conf"])
        except Exception as e:
            logger.error(f"Supervisord failed: {e}")
    else:
        logger.warning("supervisord is not installed in PATH. Bot supervisor service skipped.")

def run_worker():
    logger.info("Starting bot worker manager...")
    try:
        subprocess.run([sys.executable, "worker.py"])
    except Exception as e:
        logger.error(f"Worker failed: {e}")

def run_ping_server():
    try:
        subprocess.run([sys.executable, "ping_server.py"])
    except Exception as e:
        logger.error(f"Ping server failed: {e}")

if __name__ == "__main__":
    logger.info("Initializing BotClusters...")

    # Start the web server immediately so Koyeb/Render health check probes succeed instantly
    web_server_thread = threading.Thread(target=run_web_server, name="WebServerThread", daemon=True)
    web_server_thread.start()

    # Background support services
    threads = [
        web_server_thread,
        threading.Thread(target=run_update, name="UpdateThread", daemon=True),
        threading.Thread(target=run_supervisord, name="SupervisorThread", daemon=True),
        threading.Thread(target=run_worker, name="WorkerThread", daemon=True),
        threading.Thread(target=run_ping_server, name="PingServerThread", daemon=True)
    ]

    for t in threads[1:]:
        t.start()

    logger.info("All BotClusters background services started successfully.")

    try:
        while True:
            time.sleep(1)
            # If web server dies, exit
            if not threads[0].is_alive():
                logger.error("Web server thread stopped unexpectedly.")
                break
    except KeyboardInterrupt:
        logger.info("Shutting down BotClusters gracefully...")
