"""Tests for scripts/: deploy to PythonAnywhere, the hosted e2e check, the local console chat.

The network is never touched: ``FakeNet`` stands in for ``requests.Session`` and routes
calls to a fake PythonAnywhere API, a fake Telegram Bot API and the site itself (for the
e2e check — the real Flask app through its test client, with ``FakeTelegram`` behind it).
All secrets below are made-up test values; every test checks none of them is printed.
"""
from __future__ import annotations

import io
import itertools
import json
import re
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

import pytest
from requests.structures import CaseInsensitiveDict

from leadbot.config import Config
from leadbot.storage import Storage
from leadbot.texts import TEXTS
from leadbot.web import create_app
from scripts import deploy_pythonanywhere as deploy
from scripts import e2e_hosted as e2e
from scripts import run_local
from tests.fakes import FakeTelegram

PA_USER = "demo"
PA_TOKEN = "pa-test-token-0123456789abcdef"
BOT_TOKEN = "123456789:TEST-bot-token-abcdefghijklmnopqrstu"
ADMIN_PASSWORD = "test-admin-password-42"
WEBHOOK_SECRET = "test_webhook_secret_ABCdef-123"
SECRET_KEY = "test-secret-key-0123456789abcdef0123456789"
DOMAIN = "demo.pythonanywhere.com"
APP_DIR = "/home/demo/leadbot-app"
WSGI_PATH = "/var/www/demo_pythonanywhere_com_wsgi.py"
OWNER_CHAT = 777000111


# -- fake network ------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code: int, body: Any = b"", headers: Optional[dict] = None):
        self.status_code = status_code
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            headers = {"Content-Type": "application/json", **(headers or {})}
        elif isinstance(body, str):
            body = body.encode()
        self.content = body
        self.headers = CaseInsensitiveDict(headers or {})

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self):
        return json.loads(self.content.decode())


Handler = Callable[[str, str, dict], FakeResponse]


class FakeNet:
    """``requests.Session`` look-alike: ``handlers[host](method, url, kwargs)``."""

    def __init__(self, handlers: Optional[Dict[str, Handler]] = None):
        self.handlers = dict(handlers or {})
        self.calls: List[tuple] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        method = method.upper()
        self.calls.append((method, url, kwargs))
        host = urlsplit(url).hostname
        if host not in self.handlers:
            raise AssertionError(f"unexpected network call: {method} {url}")
        return self.handlers[host](method, url, kwargs)

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def patch(self, url, **kw):
        return self.request("PATCH", url, **kw)


class FakePythonAnywhere:
    def __init__(self, user: str = PA_USER, token: str = PA_TOKEN):
        self.user, self.token = user, token
        self.files: Dict[str, bytes] = {}
        self.webapps: Dict[str, dict] = {}
        self.created: List[dict] = []
        self.patches: List[dict] = []
        self.reloads = 0
        self.rate_limited = 0  # answer 429 to this many uploads first

    def __call__(self, method: str, url: str, kw: dict) -> FakeResponse:
        if (kw.get("headers") or {}).get("Authorization") != f"Token {self.token}":
            return FakeResponse(401, {"detail": "Invalid token."})
        path = urlsplit(url).path
        base = f"/api/v0/user/{self.user}/"
        assert path.startswith(base), path
        rest = path[len(base):]
        if rest.startswith("files/path/"):
            target = "/" + rest[len("files/path/"):]
            if method == "GET":
                if target in self.files:
                    return FakeResponse(200, self.files[target])
                return FakeResponse(404, {"detail": "No such file or directory"})
            if method == "POST":
                if self.rate_limited:
                    self.rate_limited -= 1
                    return FakeResponse(429, {"detail": "Request was throttled."})
                _, content = kw["files"]["content"][:2]
                content = content.read() if hasattr(content, "read") else content
                existed = target in self.files
                self.files[target] = content if isinstance(content, bytes) else content.encode()
                return FakeResponse(200 if existed else 201, b"")
        if rest == "webapps/":
            if method == "GET":
                return FakeResponse(200, list(self.webapps.values()))
            if method == "POST":
                data = dict(kw.get("data") or {})
                self.created.append(data)
                self.webapps[data["domain_name"]] = {"domain_name": data["domain_name"],
                                                     "python_version": data["python_version"]}
                # creating a webapp writes a default WSGI file that must be overwritten
                self.files["/var/www/" + data["domain_name"].replace(".", "_") + "_wsgi.py"] = \
                    b"# default PythonAnywhere WSGI file\n"
                return FakeResponse(201, self.webapps[data["domain_name"]])
        match = re.fullmatch(r"webapps/([^/]+)/(reload/)?", rest)
        if match and match.group(1) in self.webapps:
            if match.group(2) and method == "POST":
                self.reloads += 1
                return FakeResponse(200, {"status": "OK"})
            if not match.group(2) and method == "PATCH":
                data = dict(kw.get("data") or {})
                if "python_version" in data and not re.fullmatch(r"\d\.\d+",
                                                                 data["python_version"]):
                    # the real API: PATCH does not take the "python311" form that POST takes
                    return FakeResponse(400, {"python_version": [
                        f'"{data["python_version"]}" is not a valid choice.']})
                self.patches.append(data)
                return FakeResponse(200, self.webapps[match.group(1)])
        return FakeResponse(404, {"detail": "Not found."})


class FakeBotApi:
    """api.telegram.org: records methods; message ids come from ``tg`` when given."""

    def __init__(self, token: str = BOT_TOKEN, tg: Optional[FakeTelegram] = None):
        self.token = token
        self.tg = tg
        self.calls: List[tuple] = []
        self.webhook: Dict[str, Any] = {"url": "", "pending_update_count": 0}
        # Telegram keeps the last delivery error across setWebhook; when set, getWebhookInfo
        # reports one dated this many seconds before the moment of the call
        self.delivery_error_age: Optional[int] = None
        self._ids = itertools.count(1000)

    def methods(self) -> List[str]:
        return [m for m, _ in self.calls]

    def __call__(self, method: str, url: str, kw: dict) -> FakeResponse:
        path = urlsplit(url).path
        prefix = f"/bot{self.token}/"
        if not path.startswith(prefix):
            return FakeResponse(401, {"ok": False, "error_code": 401,
                                      "description": "Unauthorized"})
        api_method = path[len(prefix):]
        body = kw.get("json") or {}
        self.calls.append((api_method, body))
        if api_method == "getMe":
            return self._ok({"id": 1, "is_bot": True, "username": "demo_test_bot"})
        if api_method == "setWebhook":
            self.webhook = {"url": body["url"], "pending_update_count": 0,
                            "allowed_updates": body.get("allowed_updates")}
            return self._ok(True)
        if api_method == "getWebhookInfo":
            info = dict(self.webhook)
            if self.delivery_error_age is not None:
                info.update(last_error_date=int(time.time()) - self.delivery_error_age,
                            last_error_message="Wrong response from the webhook: 502 Bad Gateway")
            return self._ok(info)
        if api_method == "setMyCommands":
            return self._ok(True)
        if api_method == "sendMessage":
            if self.tg is not None:
                message = self.tg.send_message(body["chat_id"], body["text"])
            else:
                message = {"message_id": next(self._ids), "chat": {"id": body["chat_id"]}}
            return self._ok(message)
        if api_method == "deleteMessage":
            if self.tg is not None:
                self.tg.messages.pop((body["chat_id"], body["message_id"]), None)
            return self._ok(True)
        return FakeResponse(404, {"ok": False, "error_code": 404, "description": "Not Found"})

    @staticmethod
    def _ok(result: Any) -> FakeResponse:
        return FakeResponse(200, {"ok": True, "result": result})


def health_site(method: str, url: str, kw: dict) -> FakeResponse:
    if urlsplit(url).path == "/health":
        return FakeResponse(200, {"ok": True})
    return FakeResponse(404, "not found")


SECRETS = (PA_TOKEN, BOT_TOKEN, ADMIN_PASSWORD, WEBHOOK_SECRET, SECRET_KEY)


def assert_no_secrets(text: str, extra: tuple = ()) -> None:
    for value in SECRETS + tuple(extra):
        assert value not in text, "a secret value was printed"


def write_env(tmp_path, **values: str):
    base = {"TELEGRAM_BOT_TOKEN": BOT_TOKEN, "TELEGRAM_WEBHOOK_SECRET": WEBHOOK_SECRET,
            "ADMIN_PASSWORD": ADMIN_PASSWORD, "SECRET_KEY": SECRET_KEY, "DATABASE_PATH": "",
            "PUBLIC_BASE_URL": "", "PA_USERNAME": PA_USER, "PA_API_TOKEN": PA_TOKEN,
            "PA_HOST": "www.pythonanywhere.com"}
    base.update(values)
    path = tmp_path / "local.env"
    path.write_text("# test env\n" + "".join(f"{k}={v}\n" for k, v in base.items()),
                    encoding="utf-8")
    return str(path)


def parse_env(data: bytes) -> Dict[str, str]:
    result = {}
    for line in data.decode().splitlines():
        if line.strip() and not line.startswith("#"):
            key, _, value = line.partition("=")
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            result[key] = value
    return result


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Real environment variables must not leak into the scripts under test."""
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "ADMIN_PASSWORD",
                 "SECRET_KEY", "DATABASE_PATH", "PUBLIC_BASE_URL", "PA_USERNAME",
                 "PA_API_TOKEN", "PA_HOST"):
        monkeypatch.delenv(name, raising=False)


def run_deploy(env_path: str, net: FakeNet, *extra: str):
    out = io.StringIO()
    code = deploy.main(["--env", env_path, *extra], session=net, sleep=lambda s: None, out=out)
    return code, out.getvalue()


def deploy_world(pa: Optional[FakePythonAnywhere] = None, bot: Optional[FakeBotApi] = None,
                 host: str = "www.pythonanywhere.com", domain: str = DOMAIN):
    pa = pa or FakePythonAnywhere()
    bot = bot or FakeBotApi()
    return pa, bot, FakeNet({host: pa, "api.telegram.org": bot, domain: health_site})


# -- deploy: dry run -----------------------------------------------------------------------

def test_dry_run_makes_no_network_calls_and_lists_only_app_files(tmp_path):
    net = FakeNet()  # any call fails the test
    code, out = run_deploy(write_env(tmp_path), net, "--dry-run")
    assert code == 0, out
    assert net.calls == []
    assert f"{APP_DIR}/wsgi.py" in out
    assert f"{APP_DIR}/leadbot/engine.py" in out
    assert f"{APP_DIR}/leadbot/templates/admin/login.html" in out
    assert WSGI_PATH in out
    assert f"https://{DOMAIN}/tg/webhook" in out
    for excluded in ("/tests/", "/.autopilot/", "/.venv/", "/.git/", "/scripts/", "/data/",
                     "__pycache__", ".pyc", "/docs/"):
        assert excluded not in out, excluded
    assert_no_secrets(out)


def test_dry_run_with_missing_generated_secrets_still_plans_and_prints_no_values(tmp_path):
    env = write_env(tmp_path, TELEGRAM_WEBHOOK_SECRET="", SECRET_KEY="")
    code, out = run_deploy(env, FakeNet(), "--dry-run")
    assert code == 0, out
    assert "TELEGRAM_WEBHOOK_SECRET" in out and "SECRET_KEY" in out
    assert_no_secrets(out)


def test_missing_required_values_stop_with_a_clear_message(tmp_path):
    env = write_env(tmp_path, TELEGRAM_BOT_TOKEN="", PA_API_TOKEN="")
    net = FakeNet()
    code, out = run_deploy(env, net, "--dry-run")
    assert code != 0
    assert "TELEGRAM_BOT_TOKEN" in out and "PA_API_TOKEN" in out
    assert net.calls == []
    assert_no_secrets(out)


def test_short_admin_password_is_refused_before_any_upload(tmp_path):
    env = write_env(tmp_path, ADMIN_PASSWORD="short")
    pa, bot, net = deploy_world()
    code, out = run_deploy(env, net)
    assert code != 0
    assert "ADMIN_PASSWORD" in out
    assert not any(path.startswith(APP_DIR) for path in pa.files)
    assert_no_secrets(out, ("short",))


def test_unknown_pa_host_is_refused_so_the_token_goes_nowhere_else(tmp_path):
    env = write_env(tmp_path, PA_HOST="evil.example.com")
    net = FakeNet()
    code, out = run_deploy(env, net)
    assert code != 0
    assert "PA_HOST" in out
    assert net.calls == []


# -- deploy: full run against fakes --------------------------------------------------------

def test_deploy_uploads_code_and_env_creates_webapp_and_sets_webhook(tmp_path):
    pa, bot, net = deploy_world()
    code, out = run_deploy(write_env(tmp_path), net)
    assert code == 0, out

    assert f"{APP_DIR}/wsgi.py" in pa.files
    assert f"{APP_DIR}/leadbot/engine.py" in pa.files
    assert f"{APP_DIR}/leadbot/static/admin.css" in pa.files
    assert f"{APP_DIR}/requirements.txt" in pa.files
    assert not any("/tests/" in p or "/.autopilot/" in p or "__pycache__" in p
                   or "/scripts/" in p or p.endswith("/local.env") for p in pa.files)

    server_env = parse_env(pa.files[f"{APP_DIR}/.env"])
    assert server_env["TELEGRAM_BOT_TOKEN"] == BOT_TOKEN
    assert server_env["TELEGRAM_WEBHOOK_SECRET"] == WEBHOOK_SECRET
    assert server_env["ADMIN_PASSWORD"] == ADMIN_PASSWORD
    assert server_env["SECRET_KEY"] == SECRET_KEY
    assert server_env["PUBLIC_BASE_URL"] == f"https://{DOMAIN}"
    assert not any(k.startswith("PA_") for k in server_env), "deploy credentials stay local"

    assert pa.created == [{"domain_name": DOMAIN, "python_version": "python311"}]
    wsgi = pa.files[WSGI_PATH].decode()
    assert APP_DIR in wsgi and "from wsgi import application" in wsgi
    assert pa.patches and pa.patches[-1]["source_directory"] == APP_DIR
    assert str(pa.patches[-1]["force_https"]).lower() == "true"
    assert pa.reloads == 1

    webhook = [body for m, body in bot.calls if m == "setWebhook"]
    assert webhook == [{"url": f"https://{DOMAIN}/tg/webhook", "secret_token": WEBHOOK_SECRET,
                        "allowed_updates": ["message", "callback_query"]}]
    commands = [body for m, body in bot.calls if m == "setMyCommands"][0]["commands"]
    assert [c["command"] for c in commands] == ["start", "menu"]
    assert "getWebhookInfo" in bot.methods()
    assert "✅" in out
    assert_no_secrets(out)


def test_deploy_generates_missing_secrets_and_reuses_them_on_the_next_deploy(tmp_path):
    env = write_env(tmp_path, TELEGRAM_WEBHOOK_SECRET="", SECRET_KEY="")
    pa, bot, net = deploy_world()
    code, out = run_deploy(env, net)
    assert code == 0, out
    first = parse_env(pa.files[f"{APP_DIR}/.env"])
    generated = (first["TELEGRAM_WEBHOOK_SECRET"], first["SECRET_KEY"])
    assert all(len(v) >= 32 for v in generated)
    Config.from_env(first)  # the server config is valid
    assert [b["secret_token"] for m, b in bot.calls if m == "setWebhook"] == [generated[0]]
    assert_no_secrets(out, generated)

    code, out = run_deploy(env, net)
    assert code == 0, out
    second = parse_env(pa.files[f"{APP_DIR}/.env"])
    assert (second["TELEGRAM_WEBHOOK_SECRET"], second["SECRET_KEY"]) == generated
    assert len(pa.created) == 1, "an existing webapp is not created again"
    assert pa.reloads == 2
    assert_no_secrets(out, generated)


def test_deploy_eu_host_uses_eu_domain_and_python_flag(tmp_path):
    env = write_env(tmp_path, PA_HOST="eu.pythonanywhere.com")
    domain = "demo.eu.pythonanywhere.com"
    pa, bot, net = deploy_world(host="eu.pythonanywhere.com", domain=domain)
    code, out = run_deploy(env, net, "--python", "python310")
    assert code == 0, out
    assert pa.created == [{"domain_name": domain, "python_version": "python310"}]
    assert "/var/www/demo_eu_pythonanywhere_com_wsgi.py" in pa.files
    assert [b["url"] for m, b in bot.calls if m == "setWebhook"] == [
        f"https://{domain}/tg/webhook"]


def test_deploy_waits_and_retries_when_pythonanywhere_throttles(tmp_path):
    pa, bot, net = deploy_world()
    pa.rate_limited = 2
    code, out = run_deploy(write_env(tmp_path), net)
    assert code == 0, out
    assert f"{APP_DIR}/.env" in pa.files


def test_deploy_reports_pythonanywhere_errors_without_secrets(tmp_path):
    pa, bot, net = deploy_world(pa=FakePythonAnywhere(token="another-token-zzzzzzzz"))
    code, out = run_deploy(write_env(tmp_path), net)
    assert code != 0
    assert "401" in out
    assert "❌" in out
    assert_no_secrets(out)


def test_deploy_fails_when_health_check_never_passes(tmp_path):
    pa, bot = FakePythonAnywhere(), FakeBotApi()
    net = FakeNet({"www.pythonanywhere.com": pa, "api.telegram.org": bot,
                   DOMAIN: lambda m, u, k: FakeResponse(502, "bad gateway")})
    code, out = run_deploy(write_env(tmp_path), net)
    assert code != 0
    assert "/health" in out
    assert "setWebhook" not in bot.methods(), "no webhook to an app that does not start"
    assert_no_secrets(out)


def test_deploy_ignores_a_webhook_delivery_error_older_than_this_deploy(tmp_path):
    pa, bot, net = deploy_world()
    bot.delivery_error_age = 3600  # left from before: the app was down an hour ago
    code, out = run_deploy(write_env(tmp_path), net)
    assert code == 0, out
    assert "ошибок доставки нет" in out
    assert_no_secrets(out)


def test_deploy_fails_on_a_webhook_delivery_error_after_set_webhook(tmp_path):
    pa, bot, net = deploy_world()
    bot.delivery_error_age = 0  # Telegram could not deliver to the new webhook just now
    code, out = run_deploy(write_env(tmp_path), net)
    assert code == 1, out
    assert "502 Bad Gateway" in out
    assert "ошибок доставки нет" not in out
    assert_no_secrets(out)


# -- e2e on the hosting: the real app behind a fake network ----------------------------------

class SiteViaTestClient:
    """https://<domain>/… → the Flask app's test client (it keeps the cookies like a browser)."""

    def __init__(self, app, base_url: str = f"https://{DOMAIN}"):
        self.client = app.test_client()
        self.base_url = base_url
        self.requests: List[tuple] = []

    def __call__(self, method: str, url: str, kw: dict) -> FakeResponse:
        parts = urlsplit(url)
        path = parts.path + ("?" + parts.query if parts.query else "")
        headers = dict(kw.get("headers") or {})
        self.requests.append((method, parts.path, headers))
        options: Dict[str, Any] = {"method": method, "headers": headers,
                                   "base_url": self.base_url}
        if kw.get("json") is not None:
            options["json"] = kw["json"]
        elif kw.get("data") is not None:
            options["data"] = kw["data"]
        resp = self.client.open(path, **options)
        out_headers = {k: v for k, v in resp.headers.items() if k.lower() != "set-cookie"}
        cookies = resp.headers.getlist("Set-Cookie")
        if cookies:
            out_headers["Set-Cookie"] = ", ".join(cookies)
        return FakeResponse(resp.status_code, resp.get_data(), out_headers)


def hosted_world(tmp_path, server_secret: str = WEBHOOK_SECRET):
    storage = Storage(str(tmp_path / "server.sqlite3"))
    tg = FakeTelegram()
    config = Config.from_env({"TELEGRAM_BOT_TOKEN": BOT_TOKEN,
                              "TELEGRAM_WEBHOOK_SECRET": server_secret,
                              "ADMIN_PASSWORD": ADMIN_PASSWORD, "SECRET_KEY": SECRET_KEY,
                              "PUBLIC_BASE_URL": f"https://{DOMAIN}"})
    app = create_app(config, storage=storage, tg=tg)
    storage.set_setting("owner_chat_id", str(OWNER_CHAT))
    bot = FakeBotApi(tg=tg)
    bot.webhook = {"url": f"https://{DOMAIN}/tg/webhook", "pending_update_count": 0}
    site = SiteViaTestClient(app)
    net = FakeNet({DOMAIN: site, "api.telegram.org": bot,
                   "www.pythonanywhere.com": FakePythonAnywhere()})
    return storage, tg, bot, site, net


def run_e2e(env_path: str, net: FakeNet, *extra: str):
    out = io.StringIO()
    code = e2e.main(["--env", env_path, *extra], session=net, sleep=lambda s: None, out=out)
    return code, out.getvalue()


TITLE_KEY = "pr_current.title"
MARKED_TITLE_RE = re.compile(r"^(.+) \[проверка \d+\]$")


def test_e2e_refuses_to_run_without_chat_id(tmp_path):
    net = FakeNet()
    code, out = run_e2e(write_env(tmp_path), net)
    assert code != 0
    assert "--chat-id" in out
    assert net.calls == []


def test_e2e_walks_the_promo_branch_finds_the_lead_with_the_new_title_and_restores_it(tmp_path):
    storage, tg, bot, site, net = hosted_world(tmp_path)
    code, out = run_e2e(write_env(tmp_path), net, "--chat-id", str(OWNER_CHAT))
    assert code == 0, out
    assert "❌" not in out

    leads = storage.list_leads()
    assert len(leads) == 1
    lead = leads[0]
    assert (lead["kind"], lead["chat_id"], lead["name"], lead["phone"]) == (
        "promo", OWNER_CHAT, "Тест Автопроверка", "+79000000000")
    titles = [item["title"] for item in lead["items"]]
    marked = [t for t in titles if MARKED_TITLE_RE.match(t)]
    assert len(marked) == 1 and MARKED_TITLE_RE.match(marked[0]).group(1) == \
        TEXTS[TITLE_KEY].default, titles
    assert lead["status"] == "processed", "the test lead is marked as handled"
    assert lead["delivered"], "the lead reached the owner's chat"

    assert storage.get_text_override(TITLE_KEY) is None, "the default title is back"
    assert not [k for k in tg.messages if k[0] == OWNER_CHAT
                and "проверка связи" in (tg.messages[k].get("text") or "")], \
        "probe messages are deleted"
    webhook_calls = [r for r in site.requests if r[1] == "/tg/webhook"]
    assert webhook_calls and all(
        r[2].get("X-Telegram-Bot-Api-Secret-Token") == WEBHOOK_SECRET for r in webhook_calls)
    assert any(r[2].get("X-Real-IP") == "1.2.3.4" for r in site.requests)
    assert "Secure" in out or "secure" in out
    assert_no_secrets(out)


def test_e2e_restores_a_title_the_customer_had_already_changed(tmp_path):
    storage, tg, bot, site, net = hosted_world(tmp_path)
    storage.set_text_override(TITLE_KEY, "Как продвигаете сейчас")
    code, out = run_e2e(write_env(tmp_path), net, "--chat-id", str(OWNER_CHAT))
    assert code == 0, out
    assert storage.get_text_override(TITLE_KEY) == "Как продвигаете сейчас"
    titles = [item["title"] for item in storage.list_leads()[0]["items"]]
    assert any(MARKED_TITLE_RE.match(t) and t.startswith("Как продвигаете сейчас ")
               for t in titles), titles


def test_e2e_reports_failure_when_the_webhook_secret_does_not_match(tmp_path):
    storage, tg, bot, site, net = hosted_world(tmp_path, server_secret="other_secret_value_42")
    code, out = run_e2e(write_env(tmp_path), net, "--chat-id", str(OWNER_CHAT))
    assert code != 0
    assert "❌" in out and "403" in out
    assert storage.list_leads() == []
    assert storage.get_text_override(TITLE_KEY) is None, "restored even after a failure"
    assert_no_secrets(out, ("other_secret_value_42",))


def test_e2e_reports_a_webhook_delivery_error_from_telegram(tmp_path):
    storage, tg, bot, site, net = hosted_world(tmp_path)
    bot.webhook["last_error_message"] = "Wrong response from the webhook: 502 Bad Gateway"
    code, out = run_e2e(write_env(tmp_path), net, "--chat-id", str(OWNER_CHAT))
    assert code != 0
    assert "502 Bad Gateway" in out


def test_e2e_takes_the_webhook_secret_from_the_server_env_when_local_is_empty(tmp_path):
    storage, tg, bot, site, net = hosted_world(tmp_path)
    pa = net.handlers["www.pythonanywhere.com"]
    pa.files[f"{APP_DIR}/.env"] = f'TELEGRAM_WEBHOOK_SECRET="{WEBHOOK_SECRET}"\n'.encode()
    code, out = run_e2e(write_env(tmp_path, TELEGRAM_WEBHOOK_SECRET=""), net,
                        "--chat-id", str(OWNER_CHAT))
    assert code == 0, out
    assert len(storage.list_leads()) == 1
    assert_no_secrets(out)


# -- local console chat -------------------------------------------------------------------

def run_console(tmp_path, *lines: str):
    feed = iter(lines)

    def fake_input(prompt: str = "") -> str:
        try:
            return next(feed)
        except StopIteration:
            raise EOFError from None

    out = io.StringIO()
    code = run_local.main(["--db", str(tmp_path / "local.sqlite3")], input_fn=fake_input,
                          out=out)
    return code, out.getvalue()


def test_console_starts_with_the_privacy_screen_and_numbers_the_buttons(tmp_path):
    code, out = run_console(tmp_path)
    assert code == 0
    assert "Мы не продаём" in out  # the default privacy block
    assert f"1. {TEXTS['privacy.start'].default}" in out


def test_console_presses_buttons_by_number_and_sends_typed_text(tmp_path):
    menu_promo = TEXTS["menu.opt.promo"].default
    code, out = run_console(tmp_path, "1", "")
    assert f"3. {menu_promo}" in out, out  # menu: new, redesign, promo, examples, contacts
    code, out = run_console(tmp_path / "fresh", "1", "3", "ерунда")
    assert TEXTS["pr_what.question"].default in out
    assert TEXTS["err.need_buttons"].default in out, "typed text goes to the bot as a message"


def test_console_explains_a_wrong_number(tmp_path):
    code, out = run_console(tmp_path, "42")
    assert code == 0
    assert "Нет кнопки с номером 42" in out


def test_console_shows_the_lead_sent_to_the_owner(tmp_path):
    steps = ["1", "3",                                  # Начать, Продвижение
             "8", "10", "8", "10",                       # pr_what/pr_types: «Не знаю», Готово
             "6", "8",                                   # pr_current: «Никогда», Готово
             "Анна", "+7 900 000-00-00",
             "3", "4", "4",                              # Звонки, Продолжить, Без разницы
             "1"]                                        # Отправить заявку
    code, out = run_console(tmp_path, *steps)
    assert code == 0
    assert "[владельцу]" in out, out[-3000:]
    assert "+79000000000" in out


def test_deploy_patch_does_not_send_python_version_so_create_and_redeploy_pass(tmp_path):
    pa, bot, net = deploy_world()
    env = write_env(tmp_path)
    code, out = run_deploy(env, net)
    assert code == 0, out
    code, out = run_deploy(env, net)  # the web app already exists now
    assert code == 0, out
    assert len(pa.patches) == 2
    assert all("python_version" not in p for p in pa.patches)
    assert pa.created == [{"domain_name": DOMAIN, "python_version": "python311"}]
