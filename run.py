try:
    import eventlet
    eventlet.monkey_patch()
except (ImportError, Exception):
    pass

import os
import logging
from app import app
from app.routes.routes import socketio

SUPERVISOR_LOG_DIR = os.environ.get("SUPERVISOR_LOG_DIR", "/var/log/supervisor")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    try:
        try:
            os.makedirs(SUPERVISOR_LOG_DIR, exist_ok=True)
        except (PermissionError, OSError) as e:
            logger.warning(f"Could not create supervisor log dir {SUPERVISOR_LOG_DIR}: {e}")

        host = os.environ.get("HOST", "0.0.0.0")
        port = int(os.environ.get("PORT", 5000))
        logger.info(f"Running BotClusters dashboard on {host}:{port}")

        run_kwargs = {
            "host": host,
            "port": port,
            "debug": False,
            "use_reloader": False
        }
        # allow_unsafe_werkzeug is only accepted by the Werkzeug development server
        if getattr(socketio, 'async_mode', '') != 'eventlet':
            run_kwargs['allow_unsafe_werkzeug'] = True

        socketio.run(app, **run_kwargs)
    except Exception as e:
        logger.error(f"Failed to start application: {str(e)}", exc_info=True)
        raise
