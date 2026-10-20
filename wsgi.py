"""WSGI entry point (PythonAnywhere, ``flask --app wsgi run``).

Loads ``.env`` next to this file (without overriding real environment variables),
builds the config and exposes ``application`` (and ``app`` for the Flask CLI).
"""
import logging
import os
import sys
from dataclasses import replace

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from leadbot.config import Config, ConfigError, load_dotenv  # noqa: E402
from leadbot.web import create_app  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("leadbot.wsgi")


def build_app():
    load_dotenv(os.path.join(BASE_DIR, ".env"))
    try:
        config = Config.from_env()
    except ConfigError as exc:
        log.error("%s", exc)
        raise
    if not os.path.isabs(config.database_path):
        # the WSGI server's working directory is not the project directory
        config = replace(config, database_path=os.path.join(BASE_DIR, config.database_path))
    return create_app(config)


application = build_app()
app = application
