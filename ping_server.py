import os
import sys
import time
import logging
import requests
from dotenv import load_dotenv

load_dotenv('cluster.env', override=True)

DEFAULT_PING_INTERVAL = 240
MAX_FAILURES = 5

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def get_app_url():
    return os.getenv("APP_URL") or (sys.argv[1] if len(sys.argv) > 1 else None)

def get_ping_interval():
    try:
        return int(os.getenv("PING_INTERVAL", DEFAULT_PING_INTERVAL))
    except ValueError:
        logger.warning("Invalid PING_INTERVAL value; using default 240s.")
        return DEFAULT_PING_INTERVAL

def get_delay():
    try:
        return int(os.getenv("DELAY", 300))
    except ValueError:
        logger.warning("Invalid DELAY value; using default 300 seconds.")
        return 300

def should_delay_ping():
    return os.getenv("DELAY_PING", "False").lower() in ("true", "1", "yes")

def ping_url(session, url):
    try:
        response = session.get(url, timeout=15)
        if response.status_code == 200:
            logger.info(f"Ping successful: {response.status_code} - {url}")
            return True
        else:
            logger.warning(f"Ping responded with status: {response.status_code} - {url}")
            return False
    except requests.RequestException as e:
        logger.error(f"Error pinging URL: {e}")
        return False

def main():
    logger.info("Ping service started (reads APP_URL / PING_INTERVAL from cluster.env on every cycle).")
    if should_delay_ping():
        delay_seconds = get_delay()
        logger.info(f"Delaying start of pinging by {delay_seconds} seconds as per DELAY_PING setting.")
        time.sleep(delay_seconds)

    failure_count = 0
    warned_no_url = False

    with requests.Session() as session:
        try:
            while True:
                # Re-read settings each cycle so URL / interval changed in the dashboard apply without restart
                load_dotenv('cluster.env', override=True)
                app_url = get_app_url()
                if not app_url:
                    if not warned_no_url:
                        logger.info("No APP_URL set - ping disabled until you set it in Settings.")
                        warned_no_url = True
                    time.sleep(30)
                    continue
                warned_no_url = False

                if ping_url(session, app_url):
                    failure_count = 0
                else:
                    failure_count += 1
                    if failure_count >= MAX_FAILURES:
                        logger.error(f"Maximum failure count reached ({MAX_FAILURES}). Pausing for 10 minutes before retrying...")
                        time.sleep(600)
                        failure_count = 0
                        continue
                time.sleep(max(30, get_ping_interval()))
        except KeyboardInterrupt:
            logger.info("Ping process interrupted by user.")


if __name__ == "__main__":
    main()
