"""Panel access: password login with lockout, the session cookie, CSRF, safe redirects.

* The password (``Config.admin_password``) is compared in constant time (digests of equal
  length, ``hmac.compare_digest``).
* 5 failures in 15 minutes from one IP lock the login for that IP (``Storage`` keeps the
  failures). Behind the hosting's own proxy (a private address) the client address is
  taken from ``X-Real-IP``; from anywhere else that header is ignored, so it cannot be
  forged to dodge the lock.
* The session is Flask's signed cookie: HttpOnly, SameSite=Lax, Secure when the request
  came over HTTPS, 12 hours from login (checked on the server too, not only by the
  cookie's expiry). It carries a marker derived from the password and a database
  "epoch": "Выйти" changes the epoch, so a copied cookie stops working as well, and so
  does every session after the password is changed.
* Every POST must carry the session's CSRF token (``csrf_token`` form field), else 400.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import secrets
import time
from datetime import timedelta
from typing import Optional
from urllib.parse import urlsplit

from flask import abort, request, session
from flask.sessions import SecureCookieSessionInterface

from .content import SESSION_EPOCH_SETTING

log = logging.getLogger(__name__)

SESSION_LIFETIME = timedelta(hours=12)
MAX_FAILURES = 5
LOCK_MINUTES = 15
CSRF_FIELD = "csrf_token"
ADMIN_HOME = "/admin"

_TRUSTED_PROXY_NETS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fc00::/7"))


class AdminSessionInterface(SecureCookieSessionInterface):
    """Flask's cookie session with the Secure flag set per request (HTTPS only)."""

    def get_cookie_secure(self, app) -> bool:  # noqa: D401 - Flask hook
        return request_is_https()


def request_is_https() -> bool:
    if request.is_secure:
        return True
    # set by the hosting's front proxy; a forged value only makes the cookie stricter
    forwarded = request.headers.get("X-Forwarded-Proto", "")
    return forwarded.split(",")[0].strip().lower() == "https"


def _is_trusted_proxy(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in net for net in _TRUSTED_PROXY_NETS)


def client_ip() -> str:
    remote = request.remote_addr or "unknown"
    if _is_trusted_proxy(remote):
        real = request.headers.get("X-Real-IP", "").strip()
        try:
            return str(ipaddress.ip_address(real))
        except ValueError:
            pass
    return remote


# -- password and session ------------------------------------------------------------

def password_matches(given: str, expected: str) -> bool:
    given_digest = hashlib.sha256(given.encode("utf-8")).digest()
    expected_digest = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(given_digest, expected_digest) and bool(expected)


def _epoch(storage) -> str:
    return storage.get_setting(SESSION_EPOCH_SETTING) or "0"


def _marker(config, storage) -> str:
    message = f"admin|{_epoch(storage)}|{config.admin_password}".encode("utf-8")
    return hmac.new(config.secret_key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def is_logged_in(config, storage) -> bool:
    marker = session.get("admin")
    login_at = session.get("login_at")
    if not isinstance(marker, str) or not isinstance(login_at, (int, float)):
        return False
    if not 0 <= time.time() - login_at <= SESSION_LIFETIME.total_seconds():
        return False
    return hmac.compare_digest(marker, _marker(config, storage))


def start_session(config, storage) -> None:
    session.clear()
    session.permanent = True
    session["admin"] = _marker(config, storage)
    session["login_at"] = int(time.time())
    session[CSRF_FIELD] = secrets.token_urlsafe(32)


def end_all_sessions(storage) -> None:
    storage.set_setting(SESSION_EPOCH_SETTING, secrets.token_hex(8))
    session.clear()


def is_locked(storage, ip: str) -> bool:
    return storage.recent_login_failures(ip, minutes=LOCK_MINUTES) >= MAX_FAILURES


# -- CSRF ----------------------------------------------------------------------------

def csrf_token() -> str:
    token = session.get(CSRF_FIELD)
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_FIELD] = token
    return token


def csrf_valid() -> bool:
    expected = session.get(CSRF_FIELD)
    given = request.form.get(CSRF_FIELD, "")
    if not isinstance(expected, str) or not expected or not given:
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def require_csrf() -> None:
    if not csrf_valid():
        log.warning("Admin POST %s without a valid CSRF token", request.path)
        abort(400, description="csrf")


# -- redirects -----------------------------------------------------------------------

def safe_next(value: Optional[str]) -> str:
    """A panel path to return to after login; anything else becomes the panel home."""
    if not value or "\\" in value or any(ord(ch) < 32 for ch in value):
        return ADMIN_HOME
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or value.startswith("//"):
        return ADMIN_HOME
    if parts.path != ADMIN_HOME and not parts.path.startswith(ADMIN_HOME + "/"):
        return ADMIN_HOME
    if parts.path.startswith(ADMIN_HOME + "/login") or parts.path.startswith(
            ADMIN_HOME + "/logout"):
        return ADMIN_HOME
    return value
