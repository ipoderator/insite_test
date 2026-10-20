"""Admin web panel for the customer (Russian UI): ``register_admin(app)`` mounts ``/admin``."""
from __future__ import annotations

from flask import Flask

from .auth import SESSION_LIFETIME, AdminSessionInterface
from .views import admin_bp


def register_admin(app: Flask) -> None:
    app.session_interface = AdminSessionInterface()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=SESSION_LIFETIME,
        MAX_CONTENT_LENGTH=1024 * 1024,  # panel forms are small
    )
    app.register_blueprint(admin_bp)


__all__ = ["register_admin", "admin_bp"]
