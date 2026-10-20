import logging

import pytest

from leadbot.config import Config
from leadbot.storage import Storage
from leadbot.web import create_app, get_state
from tests.fakes import FakeTelegram, make_update

SECRET = "hook_secret-1"
HDR = "X-Telegram-Bot-Api-Secret-Token"


def make_config(tmp_path):
    return Config(
        bot_token="123:TEST",
        webhook_secret=SECRET,
        admin_password="long-enough-pass",
        secret_key="flask-secret",
        database_path=str(tmp_path / "data" / "bot.sqlite3"),
        public_base_url="https://bot.example.org",
    )


@pytest.fixture
def received():
    return []


@pytest.fixture
def app(tmp_path, received):
    cfg = make_config(tmp_path)
    storage = Storage(str(tmp_path / "t.sqlite3"))
    return create_app(cfg, storage=storage, tg=FakeTelegram(), update_handler=received.append)


@pytest.fixture
def client(app):
    return app.test_client()


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}


def test_webhook_without_secret_is_forbidden(client, received):
    resp = client.post("/tg/webhook", json=make_update(1, "hi"))
    assert resp.status_code == 403
    assert received == []


def test_webhook_with_wrong_secret_is_forbidden(client, received):
    resp = client.post("/tg/webhook", json=make_update(1, "hi"), headers={HDR: "wrong"})
    assert resp.status_code == 403
    assert received == []


def test_webhook_get_not_allowed(client):
    assert client.get("/tg/webhook").status_code == 405


def test_webhook_passes_update_to_handler(client, received):
    update = make_update(10, "/start", chat_id=55)
    resp = client.post("/tg/webhook", json=update, headers={HDR: SECRET})
    assert resp.status_code == 200
    assert received == [update]


def test_handler_error_still_returns_200(tmp_path, caplog):
    def boom(update):
        raise RuntimeError("engine exploded")

    app = create_app(make_config(tmp_path), storage=Storage(str(tmp_path / "t.db")),
                     tg=FakeTelegram(), update_handler=boom)
    with caplog.at_level(logging.ERROR):
        resp = app.test_client().post("/tg/webhook", json=make_update(3, "x"),
                                      headers={HDR: SECRET})
    assert resp.status_code == 200
    assert "engine exploded" in caplog.text


def test_invalid_body_with_valid_secret_is_ignored(client, received):
    resp = client.post("/tg/webhook", data="not json", content_type="text/plain",
                       headers={HDR: SECRET})
    assert resp.status_code == 200
    resp = client.post("/tg/webhook", json=[1, 2], headers={HDR: SECRET})
    assert resp.status_code == 200
    assert received == []


def test_handler_can_be_replaced_after_creation(app, client, received):
    other = []
    get_state(app).update_handler = other.append
    client.post("/tg/webhook", json=make_update(4, "x"), headers={HDR: SECRET})
    assert received == [] and other == [make_update(4, "x")]


def test_state_exposes_dependencies(app):
    state = get_state(app)
    assert isinstance(state.storage, Storage)
    assert isinstance(state.tg, FakeTelegram)
    assert state.config.webhook_secret == SECRET
    assert app.secret_key == "flask-secret"


def test_default_handler_logs_and_returns_200(tmp_path, caplog):
    app = create_app(make_config(tmp_path), storage=Storage(str(tmp_path / "t.db")),
                     tg=FakeTelegram())
    with caplog.at_level(logging.INFO):
        resp = app.test_client().post("/tg/webhook", json=make_update(321, "x"),
                                      headers={HDR: SECRET})
    assert resp.status_code == 200
    assert "321" in caplog.text


def test_builds_and_seeds_storage_from_config(tmp_path):
    cfg = make_config(tmp_path)
    app = create_app(cfg, tg=FakeTelegram())
    storage = get_state(app).storage
    assert storage.path == cfg.database_path
    assert (tmp_path / "data" / "bot.sqlite3").exists()
    assert storage.get_setting("contact_telegram") == "@site_it"
    assert len(storage.list_categories()) == 7


def test_secret_not_logged(client, caplog):
    with caplog.at_level(logging.DEBUG):
        client.post("/tg/webhook", json=make_update(1, "x"), headers={HDR: "wrong"})
        client.post("/tg/webhook", json=make_update(2, "x"), headers={HDR: SECRET})
    assert SECRET not in caplog.text
    assert "123:TEST" not in caplog.text


def test_wsgi_exposes_application(tmp_path, monkeypatch):
    import importlib
    import sys

    env = {"TELEGRAM_BOT_TOKEN": "123:TEST", "TELEGRAM_WEBHOOK_SECRET": SECRET,
           "ADMIN_PASSWORD": "long-enough-pass", "SECRET_KEY": "flask-secret",
           "DATABASE_PATH": str(tmp_path / "w.sqlite3"), "PUBLIC_BASE_URL": ""}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    sys.modules.pop("wsgi", None)
    wsgi = importlib.import_module("wsgi")
    try:
        assert wsgi.application is wsgi.app
        assert wsgi.application.test_client().get("/health").get_json() == {"ok": True}
        assert get_state(wsgi.application).storage.path == env["DATABASE_PATH"]
    finally:
        sys.modules.pop("wsgi", None)
