try:
    import eventlet
    eventlet.monkey_patch()
except (ImportError, Exception):
    pass

from flask import Flask

app = Flask(__name__)

from app import routes
