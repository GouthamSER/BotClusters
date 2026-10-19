import os

if not os.environ.get("BOTCLUSTERS_NO_WEB"):
    try:
        import eventlet
        eventlet.monkey_patch()
    except (ImportError, Exception):
        pass

from flask import Flask

app = Flask(__name__)

# worker.py imports app.utils.* only - it must not start the web routes/background threads
if not os.environ.get("BOTCLUSTERS_NO_WEB"):
    from app import routes
