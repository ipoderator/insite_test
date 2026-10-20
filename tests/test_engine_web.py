"""The webhook of create_app runs the dialogue engine by default."""
from leadbot.storage import Storage
from leadbot.web import create_app
from tests.fakes import FakeTelegram, make_update
from tests.test_web_skeleton import HDR, SECRET, make_config


def test_webhook_runs_engine_by_default(tmp_path):
    tg = FakeTelegram()
    app = create_app(make_config(tmp_path), storage=Storage(str(tmp_path / "w.db")), tg=tg)
    client = app.test_client()
    resp = client.post("/tg/webhook", json=make_update(1, "/start", chat_id=77),
                       headers={HDR: SECRET})
    assert resp.status_code == 200
    [sent] = tg.calls_of("send_message")
    assert sent["chat_id"] == 77 and "Перед началом" in sent["text"]
    # the privacy link uses PUBLIC_BASE_URL from the config
    assert 'href="https://bot.example.org/privacy"' in sent["text"]


def test_webhook_retry_is_not_processed_twice(tmp_path):
    tg = FakeTelegram()
    app = create_app(make_config(tmp_path), storage=Storage(str(tmp_path / "w.db")), tg=tg)
    client = app.test_client()
    for _ in range(2):
        client.post("/tg/webhook", json=make_update(5, "/start", chat_id=77),
                    headers={HDR: SECRET})
    assert len(tg.calls_of("send_message")) == 1
