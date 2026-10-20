"""Shared helpers for the deploy and e2e scripts.

* ``read_env_file`` / ``format_env_file`` — the ``.env`` format understood by
  ``leadbot.config.load_dotenv``.
* ``Redactor`` — every line a script prints goes through it, so a secret value can never
  reach the terminal, even inside an error text returned by a server.
* ``PythonAnywhere`` — a minimal client of the PythonAnywhere API v0 (files, webapps).
  The API token travels only in the ``Authorization`` header and only to an allow-listed
  PythonAnywhere host.
"""
from __future__ import annotations

import os
import re
import sys
import time
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, TextIO

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ENV_PATH = os.path.join(ROOT, ".env")

PA_HOSTS = ("www.pythonanywhere.com", "eu.pythonanywhere.com")
APP_DIR_NAME = "leadbot-app"
WEBHOOK_PATH = "/tg/webhook"
ALLOWED_UPDATES = ("message", "callback_query")

# names whose values are secrets (never printed)
SECRET_NAMES = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "ADMIN_PASSWORD",
                "SECRET_KEY", "PA_API_TOKEN")
MIN_REDACT_LENGTH = 4
HTTP_TIMEOUT = 30
RATE_LIMIT_RETRIES = 4
RATE_LIMIT_WAIT = 20  # seconds; PythonAnywhere allows 40 requests a minute per endpoint

_ENV_LINE_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


class ScriptError(Exception):
    """A step failed; the message is Russian and contains no secret values."""


# -- .env ----------------------------------------------------------------------------

def parse_env_text(text: str) -> Dict[str, str]:
    """Same rules as ``leadbot.config.load_dotenv``, but into a dict."""
    values: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_LINE_RE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def read_env_file(path: str, environ: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Values from ``path``; a variable already set in the environment wins (as in wsgi)."""
    try:
        with open(path, encoding="utf-8") as fh:
            values = parse_env_text(fh.read())
    except FileNotFoundError:
        raise ScriptError(f"Файл настроек не найден: {path}. Создайте его по образцу "
                          f".env.example.") from None
    environ = os.environ if environ is None else environ
    for key in list(values) + list(SECRET_NAMES) + ["PA_USERNAME", "PA_HOST"]:
        if environ.get(key):
            values[key] = environ[key]
    return values


def format_env_file(values: Mapping[str, str], header: str = "") -> str:
    lines = [f"# {line}" for line in header.splitlines()]
    for key, value in values.items():
        if "\n" in value or "\r" in value:
            raise ScriptError(f"Значение {key} содержит перенос строки — так нельзя.")
        # load_dotenv strips one pair of surrounding quotes and nothing else
        lines.append(f'{key}="{value}"')
    return "\n".join(lines) + "\n"


# -- output ----------------------------------------------------------------------------

class Redactor:
    def __init__(self, values: Iterable[Optional[str]] = ()):
        self._values: set = set()
        self.add(*values)

    def add(self, *values: Optional[str]) -> None:
        for value in values:
            if value and len(value) >= MIN_REDACT_LENGTH:
                self._values.add(value)

    def __call__(self, text: Any) -> str:
        text = str(text)
        for value in sorted(self._values, key=len, reverse=True):
            text = text.replace(value, "***")
        return text


class Console:
    """Prints Russian step lines through the redactor."""

    def __init__(self, out: Optional[TextIO] = None, redact: Optional[Redactor] = None):
        self.out = out if out is not None else sys.stdout
        self.redact = redact or Redactor()
        self.failed = 0

    def say(self, text: str = "") -> None:
        self.out.write(self.redact(text) + "\n")
        self.out.flush()

    def ok(self, text: str) -> None:
        self.say("✅ " + text)

    def fail(self, text: str) -> None:
        self.failed += 1
        self.say("❌ " + text)

    def info(self, text: str) -> None:
        self.say("   " + text)


# -- PythonAnywhere ----------------------------------------------------------------------

def pa_domain(username: str, host: str) -> str:
    """``<user>.pythonanywhere.com`` or ``<user>.eu.pythonanywhere.com``."""
    user = username.strip().lower()
    return f"{user}.eu.pythonanywhere.com" if host.startswith("eu.") else \
        f"{user}.pythonanywhere.com"


def wsgi_file_path(domain: str) -> str:
    return "/var/www/" + domain.replace(".", "_") + "_wsgi.py"


def app_dir(username: str) -> str:
    return f"/home/{username}/{APP_DIR_NAME}"


def check_pa_settings(values: Mapping[str, str]) -> tuple:
    """(username, token, host) or ScriptError naming what is missing."""
    username = (values.get("PA_USERNAME") or "").strip()
    token = (values.get("PA_API_TOKEN") or "").strip()
    host = (values.get("PA_HOST") or "").strip().lower() or PA_HOSTS[0]
    missing = [name for name, value in (("PA_USERNAME", username), ("PA_API_TOKEN", token))
               if not value]
    if missing:
        raise ScriptError("В .env не заполнены: " + ", ".join(missing) + ".")
    if host not in PA_HOSTS:
        raise ScriptError("PA_HOST должен быть www.pythonanywhere.com или "
                          "eu.pythonanywhere.com.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", username):
        raise ScriptError("PA_USERNAME выглядит неверно: только латиница, цифры, «_» и «-».")
    return username, token, host


class PythonAnywhere:
    def __init__(self, username: str, token: str, host: str, session: Any,
                 sleep: Callable[[float], None] = time.sleep):
        self.username = username
        self._token = token
        self.base = f"https://{host}/api/v0/user/{username}/"
        self.session = session
        self.sleep = sleep

    def __repr__(self) -> str:
        return f"PythonAnywhere({self.username!r}, token=***)"

    def _request(self, method: str, path: str, ok: Iterable[int] = (200, 201),
                 what: str = "", **kwargs: Any):
        url = self.base + path
        headers = {"Authorization": f"Token {self._token}"}
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            error = None
            try:
                resp = self.session.request(method, url, headers=headers,
                                            timeout=HTTP_TIMEOUT, **kwargs)
            except Exception as exc:  # requests.RequestException and friends
                error = type(exc).__name__
            if error is not None:
                raise ScriptError(f"{what or path}: нет связи с PythonAnywhere ({error}).")
            if resp.status_code == 429 and attempt < RATE_LIMIT_RETRIES:
                self.sleep(RATE_LIMIT_WAIT)
                continue
            break
        if resp.status_code not in tuple(ok):
            detail = (resp.text or "").strip().replace("\n", " ")[:300]
            hint = ""
            if resp.status_code in (401, 403):
                hint = " Проверьте PA_USERNAME, PA_API_TOKEN и PA_HOST."
            raise ScriptError(f"{what or path}: PythonAnywhere ответил HTTP "
                              f"{resp.status_code} {detail}.{hint}")
        return resp

    # files
    def read_file(self, abs_path: str) -> Optional[bytes]:
        resp = self._request("GET", "files/path" + abs_path, ok=(200, 404),
                             what=f"чтение {abs_path}")
        return None if resp.status_code == 404 else resp.content

    def upload(self, abs_path: str, content: bytes) -> None:
        self._request("POST", "files/path" + abs_path, ok=(200, 201),
                      what=f"загрузка {abs_path}",
                      files={"content": (os.path.basename(abs_path), content)})

    # webapps
    def list_webapps(self) -> list:
        resp = self._request("GET", "webapps/", ok=(200,), what="список веб-приложений")
        data = resp.json()
        return data if isinstance(data, list) else []

    def create_webapp(self, domain: str, python_version: str) -> None:
        self._request("POST", "webapps/", ok=(200, 201), what="создание веб-приложения",
                      data={"domain_name": domain, "python_version": python_version})

    def patch_webapp(self, domain: str, **fields: Any) -> None:
        self._request("PATCH", f"webapps/{domain}/", ok=(200,),
                      what="настройка веб-приложения", data=fields)

    def reload(self, domain: str) -> None:
        self._request("POST", f"webapps/{domain}/reload/", ok=(200,),
                      what="перезапуск веб-приложения")


def server_env(pa: PythonAnywhere) -> Dict[str, str]:
    """The ``.env`` already on the server (empty dict if there is none yet)."""
    data = pa.read_file(app_dir(pa.username) + "/.env")
    return parse_env_text(data.decode("utf-8", "replace")) if data else {}


def check_health(session: Any, base_url: str, attempts: int = 1, wait: float = 0,
                 sleep: Callable[[float], None] = time.sleep) -> Optional[str]:
    """None when ``/health`` answers ``{"ok": true}``, else the last problem (Russian)."""
    problem = "нет ответа"
    for attempt in range(max(1, attempts)):
        if attempt:
            sleep(wait)
        try:
            resp = session.get(base_url + "/health", timeout=HTTP_TIMEOUT)
        except Exception as exc:
            problem = f"нет связи ({type(exc).__name__})"
            continue
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code == 200 and isinstance(data, dict) and data.get("ok") is True:
            return None
        problem = f"HTTP {resp.status_code}"
    return problem
