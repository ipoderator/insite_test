"""Shared helpers for the admin panel tests (no tests here).

``Panel`` builds the app on a temporary database with ``FakeTelegram`` and drives it like a
browser: it reads the CSRF token from the rendered form, logs in, posts forms. ``bot()``
gives a chat simulator on the same database, to check what the bot sends after a change.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from leadbot.config import Config
from leadbot.engine import Engine
from leadbot.storage import Storage
from leadbot.web import create_app
from tests.fakes import FakeTelegram
from tests.sim import ChatSim

PASSWORD = "correct-horse-battery"
BASE_URL = "https://bot.example.org"
_CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def make_config(tmp_path) -> Config:
    return Config(bot_token="123:TEST", webhook_secret="hook_secret-1",
                  admin_password=PASSWORD, secret_key="test-secret-key",
                  database_path=str(tmp_path / "unused.sqlite3"), public_base_url=BASE_URL)


def path_of(location: Optional[str]) -> str:
    """Path (with query) of a Location header, whether relative or absolute."""
    parts = urlsplit(location or "")
    return parts.path + ("?" + parts.query if parts.query else "")


def csrf_from(html: str) -> str:
    match = _CSRF_RE.search(html)
    assert match, "no CSRF token in the page"
    return match.group(1)


class Panel:
    def __init__(self, tmp_path, clock=None, remote_addr: str = "198.51.100.7"):
        self.storage = Storage(str(tmp_path / "panel.sqlite3"), clock=clock)
        self.tg = FakeTelegram()
        self.app = create_app(make_config(tmp_path), storage=self.storage, tg=self.tg)
        self.app.config["TESTING"] = True
        self.remote_addr = remote_addr
        self.client = self.new_client()
        self._token: Optional[str] = None

    def new_client(self, **kwargs: Any):
        client = self.app.test_client(**kwargs)
        client.environ_base["REMOTE_ADDR"] = self.remote_addr
        return client

    # -- browsing ------------------------------------------------------------

    def get(self, path: str, **kwargs: Any):
        return self.client.get(path, **kwargs)

    def page(self, path: str) -> str:
        resp = self.client.get(path)
        assert resp.status_code == 200, (path, resp.status_code, path_of(resp.location))
        html = resp.get_data(as_text=True)
        found = _CSRF_RE.search(html)
        if found:
            self._token = found.group(1)
        return html

    def token(self) -> str:
        if self._token is None:
            self.page("/admin/help")
        return self._token

    def post(self, path: str, data: Optional[Dict[str, Any]] = None, csrf: bool = True,
             **kwargs: Any):
        form = dict(data or {})
        if csrf:
            form.setdefault("csrf_token", self.token())
        return self.client.post(path, data=form, **kwargs)

    def login(self, password: str = PASSWORD, next_path: Optional[str] = None, client=None):
        client = client or self.client
        html = client.get("/admin/login").get_data(as_text=True)
        url = "/admin/login" + (f"?next={next_path}" if next_path is not None else "")
        resp = client.post(url, data={"password": password, "csrf_token": csrf_from(html)})
        if client is self.client:
            self._token = None
        return resp

    # -- the bot on the same database ---------------------------------------

    def bot(self, chat_id: int = 5001, **kwargs: Any) -> ChatSim:
        engine = Engine(self.storage, self.tg, public_base_url=BASE_URL)
        return ChatSim(self.storage, self.tg, engine=engine, chat_id=chat_id, **kwargs)
