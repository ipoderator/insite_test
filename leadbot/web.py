"""Flask application: Telegram webhook, health check, demo privacy page, admin panel.

The webhook hands every update to ``LeadbotState.update_handler``. By default it is
``Engine(storage, tg).handle_update``; tests pass their own handler to ``create_app`` or
assign it later (``get_state(app).update_handler = ...``).
"""
from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from flask import Flask, abort, jsonify, render_template, request

from .admin import register_admin
from .admin.content import DEFAULT_PRIVACY_PAGE, PRIVACY_PAGE_SETTING
from .config import Config
from .engine import Engine
from .storage import Storage
from .telegram import TelegramClient

log = logging.getLogger(__name__)

UpdateHandler = Callable[[dict], None]
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
EXTENSION_KEY = "leadbot"


@dataclass
class LeadbotState:
    config: Config
    storage: Storage
    tg: Any
    update_handler: UpdateHandler


def log_only_handler(update: dict) -> None:
    """Handler that only logs the update (kept for tools that must not answer users)."""
    log.info("Update %s received; dialogue engine is not connected", update.get("update_id"))


def get_state(app: Flask) -> LeadbotState:
    return app.extensions[EXTENSION_KEY]


def create_app(config: Config, storage: Optional[Storage] = None, tg: Any = None,
               update_handler: Optional[UpdateHandler] = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = config.secret_key

    if storage is None:
        storage = Storage(config.database_path)
    storage.seed_defaults()
    if tg is None:
        tg = TelegramClient(config.bot_token)

    if update_handler is None:
        update_handler = Engine(storage, tg, public_base_url=config.public_base_url).handle_update
    state = LeadbotState(config=config, storage=storage, tg=tg, update_handler=update_handler)
    app.extensions[EXTENSION_KEY] = state

    register_admin(app)

    @app.get("/privacy")
    def privacy_page():
        text = (storage.get_setting(PRIVACY_PAGE_SETTING) or "").strip() or DEFAULT_PRIVACY_PAGE
        resp = app.make_response(render_template("privacy.html", text=text))
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'none'; style-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'none'")
        return resp

    @app.get("/health")
    def health():
        return jsonify(ok=True)

    @app.post("/tg/webhook")
    def telegram_webhook():
        given = request.headers.get(SECRET_HEADER, "")
        if not hmac.compare_digest(given.encode(), config.webhook_secret.encode()):
            log.warning("Webhook call with a missing or wrong secret from %s",
                        request.remote_addr)
            abort(403)
        update = request.get_json(silent=True)
        if not isinstance(update, dict):
            log.warning("Webhook body is not a JSON object; ignored")
            return jsonify(ok=True)
        try:
            state.update_handler(update)
        except Exception:
            # Telegram retries non-2xx responses; a broken update must not loop forever.
            log.exception("Failed to handle update %s", update.get("update_id"))
        return jsonify(ok=True)

    return app
