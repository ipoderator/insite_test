import inspect
import logging

import pytest
import requests

from leadbot.telegram import TelegramClient, TelegramError
from tests.fakes import FakeTelegram

TOKEN = "123:TEST"
API = "https://api.telegram.org/bot123:TEST/"


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    """Stands in for requests.Session and records every POST."""

    def __init__(self, responses=None):
        self.requests = []
        self.responses = list(responses or [])

    def post(self, url, json=None, timeout=None, **kwargs):
        self.requests.append({"url": url, "json": json, "timeout": timeout})
        if not self.responses:
            return FakeResponse({"ok": True, "result": True})
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def ok(result):
    return FakeResponse({"ok": True, "result": result})


def make(*responses, timeout=15):
    session = FakeSession(responses)
    return TelegramClient(TOKEN, timeout=timeout, session=session), session


def test_send_message_request_shape():
    tg, s = make(ok({"message_id": 5, "chat": {"id": 42}}), timeout=7)
    kb = {"inline_keyboard": [[{"text": "Да", "callback_data": "n|pick|yes"}]]}
    msg = tg.send_message(42, "<b>Привет</b>", reply_markup=kb)
    assert msg == {"message_id": 5, "chat": {"id": 42}}
    req = s.requests[0]
    assert req["url"] == API + "sendMessage"
    assert req["timeout"] == 7
    assert req["json"] == {
        "chat_id": 42,
        "text": "<b>Привет</b>",
        "parse_mode": "HTML",
        "reply_markup": kb,
        "link_preview_options": {"is_disabled": True},
    }


def test_send_message_preview_enabled_and_no_markup():
    tg, s = make(ok({"message_id": 1}))
    tg.send_message(1, "x", disable_preview=False)
    body = s.requests[0]["json"]
    assert "reply_markup" not in body
    assert "link_preview_options" not in body


def test_edit_message_text_shape():
    tg, s = make(ok({"message_id": 9}))
    assert tg.edit_message_text(1, 9, "новый", reply_markup={"inline_keyboard": []}) is None
    req = s.requests[0]
    assert req["url"] == API + "editMessageText"
    assert req["json"] == {
        "chat_id": 1, "message_id": 9, "text": "новый", "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": []},
        "link_preview_options": {"is_disabled": True},
    }


def test_edit_reply_markup_shape():
    tg, s = make(ok(True), ok(True))
    tg.edit_reply_markup(1, 9, {"inline_keyboard": [[]]})
    tg.edit_reply_markup(1, 9)
    assert s.requests[0]["url"] == API + "editMessageReplyMarkup"
    assert s.requests[0]["json"] == {"chat_id": 1, "message_id": 9,
                                     "reply_markup": {"inline_keyboard": [[]]}}
    assert s.requests[1]["json"] == {"chat_id": 1, "message_id": 9}


def test_answer_callback_shape():
    tg, s = make(ok(True), ok(True))
    tg.answer_callback("cb1")
    tg.answer_callback("cb2", text="Выберите хотя бы один вариант", alert=True)
    assert s.requests[0]["url"] == API + "answerCallbackQuery"
    assert s.requests[0]["json"] == {"callback_query_id": "cb1", "show_alert": False}
    assert s.requests[1]["json"] == {"callback_query_id": "cb2", "show_alert": True,
                                     "text": "Выберите хотя бы один вариант"}


def test_send_document_and_photo_shape():
    tg, s = make(ok({"message_id": 1}), ok({"message_id": 2}))
    assert tg.send_document(5, "FILE1", caption="ТЗ")["message_id"] == 1
    tg.send_photo(5, "PHOTO1")
    assert s.requests[0]["url"] == API + "sendDocument"
    assert s.requests[0]["json"] == {"chat_id": 5, "document": "FILE1", "caption": "ТЗ",
                                     "parse_mode": "HTML"}
    assert s.requests[1]["url"] == API + "sendPhoto"
    assert s.requests[1]["json"] == {"chat_id": 5, "photo": "PHOTO1"}


def test_get_file_url():
    tg, s = make(ok({"file_id": "F", "file_path": "documents/file_1.pdf"}))
    url = tg.get_file_url("F")
    assert s.requests[0]["url"] == API + "getFile"
    assert s.requests[0]["json"] == {"file_id": "F"}
    assert url == "https://api.telegram.org/file/bot123:TEST/documents/file_1.pdf"


def test_get_file_url_without_path_is_error():
    tg, _ = make(ok({"file_id": "F"}))
    with pytest.raises(TelegramError):
        tg.get_file_url("F")


def test_set_webhook_info_commands_me():
    tg, s = make(ok(True), ok({"url": "https://x/tg/webhook"}), ok(True), ok({"id": 1, "username": "b"}))
    tg.set_webhook("https://x/tg/webhook", "sec_1", ["message", "callback_query"])
    assert s.requests[0]["url"] == API + "setWebhook"
    assert s.requests[0]["json"] == {"url": "https://x/tg/webhook", "secret_token": "sec_1",
                                     "allowed_updates": ["message", "callback_query"]}
    assert tg.get_webhook_info() == {"url": "https://x/tg/webhook"}
    assert s.requests[1]["url"] == API + "getWebhookInfo"
    tg.set_my_commands([("start", "Начать"), {"command": "help", "description": "Помощь"}])
    assert s.requests[2]["url"] == API + "setMyCommands"
    assert s.requests[2]["json"] == {"commands": [
        {"command": "start", "description": "Начать"},
        {"command": "help", "description": "Помощь"},
    ]}
    assert tg.get_me() == {"id": 1, "username": "b"}
    assert s.requests[3]["url"] == API + "getMe"


def test_not_modified_is_swallowed():
    resp = FakeResponse({"ok": False, "error_code": 400,
                         "description": "Bad Request: message is not modified: specified new "
                                        "message content and reply markup are exactly the same"},
                        status=400)
    tg, _ = make(resp, FakeResponse(resp._payload, 400))
    assert tg.edit_message_text(1, 2, "same") is None
    assert tg.edit_reply_markup(1, 2, None) is None


def test_api_error_raises_telegram_error():
    tg, _ = make(FakeResponse({"ok": False, "error_code": 403,
                               "description": "Forbidden: bot was blocked by the user"}, 403))
    with pytest.raises(TelegramError) as exc:
        tg.send_message(1, "x")
    assert exc.value.description == "Forbidden: bot was blocked by the user"
    assert exc.value.error_code == 403


def test_non_json_response_raises_telegram_error():
    tg, _ = make(FakeResponse(ValueError("no json"), 502))
    with pytest.raises(TelegramError) as exc:
        tg.get_me()
    assert "502" in exc.value.description


def test_network_error_does_not_leak_token(caplog):
    boom = requests.ConnectionError(f"Max retries exceeded with url: /bot{TOKEN}/sendMessage")
    tg, _ = make(boom)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(TelegramError) as exc:
        tg.send_message(1, "x")
    assert TOKEN not in str(exc.value)
    assert TOKEN not in exc.value.description
    # no chained exception carrying the URL with the token
    assert exc.value.__cause__ is None and exc.value.__context__ is None
    assert TOKEN not in caplog.text


def test_token_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    tg, _ = make(ok({"message_id": 1}),
                 FakeResponse({"ok": False, "error_code": 400, "description": "Bad Request: x"}, 400),
                 ok({"file_path": "a/b.pdf"}))
    tg.send_message(1, "x")
    with pytest.raises(TelegramError):
        tg.send_message(1, "y")
    tg.get_file_url("F")
    assert TOKEN not in caplog.text
    assert "bot123" not in caplog.text
    assert TOKEN not in repr(tg)


def test_default_session_is_requests_session():
    tg = TelegramClient(TOKEN)
    assert isinstance(tg.session, requests.Session)


# --- FakeTelegram -----------------------------------------------------------

def _public_methods(cls):
    return {
        name: inspect.signature(fn)
        for name, fn in inspect.getmembers(cls, inspect.isfunction)
        if not name.startswith("_")
    }


def test_fake_implements_every_client_method_with_same_signature():
    real = _public_methods(TelegramClient)
    fake = _public_methods(FakeTelegram)
    for name, sig in real.items():
        assert name in fake, f"FakeTelegram lacks {name}"
        assert list(fake[name].parameters) == list(sig.parameters), name


def test_fake_records_calls_and_grows_message_ids():
    fake = FakeTelegram()
    m1 = fake.send_message(10, "a")
    m2 = fake.send_document(10, "F", caption="c")
    m3 = fake.send_photo(10, "P")
    assert m1["message_id"] < m2["message_id"] < m3["message_id"]
    fake.edit_message_text(10, m1["message_id"], "b", reply_markup={"inline_keyboard": []})
    fake.answer_callback("cb", text="t", alert=True)
    assert [c[0] for c in fake.calls] == [
        "send_message", "send_document", "send_photo", "edit_message_text", "answer_callback"]
    assert fake.calls[0][1]["text"] == "a"
    assert fake.calls[4][1] == {"callback_id": "cb", "text": "t", "alert": True}
    assert fake.sent_texts(10) == ["a"]


def test_fake_can_simulate_errors():
    fake = FakeTelegram()
    fake.fail["send_message"] = TelegramError("Forbidden: bot was blocked by the user", 403)
    with pytest.raises(TelegramError):
        fake.send_message(1, "x")
    assert fake.calls[-1][0] == "send_message"


def test_fake_file_url():
    assert FakeTelegram().get_file_url("F1").endswith("F1")
