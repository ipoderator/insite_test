"""Pure lead formatting and delivery (leads.format_*, deliver_lead)."""
import pytest

from leadbot.leads import deliver_lead, format_lead_for_owner, format_summary
from leadbot.storage import Storage
from leadbot.telegram import TelegramError
from leadbot.texts import TextStore
from tests.fakes import FakeTelegram
from tests.sim import check_html, label, plain


@pytest.fixture
def storage(tmp_path):
    st = Storage(str(tmp_path / "l.sqlite3"))
    st.seed_defaults()
    return st


@pytest.fixture
def texts(storage):
    return TextStore(storage)


def make_lead(storage, items=None, files=None, username="ivan", kind="new"):
    lead_id = storage.create_lead(
        kind, 4242, username, "Иван", "+79990000000",
        items if items is not None else [{"title": "Имя", "answer": "Иван"}],
        files or [], {"user_id": 4242})
    return lead_id


def test_summary_escapes_titles_and_answers(texts):
    parts = format_summary([{"title": "Сайт <b>", "answer": "a & b <i>"}], texts, kind="promo")
    assert len(parts) == 1
    check_html(parts[0])
    assert "Сайт &lt;b&gt;" in parts[0] and "a &amp; b &lt;i&gt;" in parts[0]
    shown = plain(parts[0])
    assert shown.startswith(label("summary.header"))
    assert f"{label('summary.kind')}: {label('lead.kind.promo')}" in shown


def test_summary_mentions_files(texts):
    [part] = format_summary([], texts, kind="new", files_count=2)
    assert f"{label('summary.files')}: {label('summary.files_value', count=2)}" in plain(part)


def test_long_summary_parts_fit_telegram_and_keep_everything(texts):
    items = [{"title": f"Вопрос {i}", "answer": ("слово & " * 150).strip()} for i in range(10)]
    items.append({"title": "Ссылки", "answer": "&" * 9000})   # one oversize answer
    parts = format_summary(items, texts)
    assert len(parts) > 2
    for part in parts:
        check_html(part)
    joined = plain("".join(parts))
    assert joined.count("&") == 10 * 150 + 9000
    for i in range(10):
        assert f"Вопрос {i}:" in joined


def test_owner_message_has_number_moscow_date_kind_client_and_answers(storage, texts):
    lead_id = make_lead(storage, items=[{"title": "Платформа", "answer": "<Tilda>"}])
    lead = storage.get_lead(lead_id)
    lead["created_at"] = "2026-10-20T21:30:00+00:00"
    parts = format_lead_for_owner(lead, texts)
    for part in parts:
        check_html(part)
    text = plain("\n".join(parts))
    assert label("lead.header", id=lead_id) in text
    assert "21.10.2026 00:30" in text                      # Europe/Moscow = UTC+3
    assert f"{label('lead.kind')}: {label('lead.kind.new')}" in text
    assert "@ivan" in text and "Платформа: <Tilda>" in text
    assert 'href="tg://user?id=4242"' in "\n".join(parts)


def test_owner_message_without_username(storage, texts):
    lead = storage.get_lead(make_lead(storage, username=None))
    text = plain("\n".join(format_lead_for_owner(lead, texts)))
    assert label("lead.no_username") in text


def test_deliver_sends_text_then_files_and_marks_delivered(storage, texts):
    files = [{"file_id": "D1", "file_name": "tz.pdf", "mime": "application/pdf", "size": 1,
              "kind": "document"},
             {"file_id": "P1", "file_name": "photo.jpg", "mime": "image/jpeg", "size": 1,
              "kind": "photo"}]
    lead_id = make_lead(storage, files=files)
    storage.set_setting("owner_chat_id", "-1001")
    tg = FakeTelegram()
    assert deliver_lead(lead_id, storage, tg, texts) is True
    methods = [m for m, _ in tg.calls]
    assert methods == ["send_message", "send_document", "send_photo"]
    assert all(kw["chat_id"] == -1001 for _, kw in tg.calls)
    assert label("lead.files", count=2) in plain(tg.calls[0][1]["text"])
    assert tg.calls_of("send_document")[0]["caption"] == label("lead.file_caption", id=lead_id)
    lead = storage.get_lead(lead_id)
    assert lead["delivered"] is True and lead["delivered_at"]


def test_deliver_without_recipient(storage, texts):
    lead_id = make_lead(storage)
    tg = FakeTelegram()
    assert deliver_lead(lead_id, storage, tg, texts) is False
    assert tg.calls == []
    lead = storage.get_lead(lead_id)
    assert lead["delivered"] is False and lead["delivery_error"]


def test_deliver_records_telegram_error(storage, texts):
    lead_id = make_lead(storage)
    storage.set_setting("owner_chat_id", "12345")
    tg = FakeTelegram()
    tg.fail["send_message"] = TelegramError("Bad Request: chat not found", 400)
    assert deliver_lead(lead_id, storage, tg, texts) is False
    lead = storage.get_lead(lead_id)
    assert lead["delivered"] is False and lead["delivery_error"] == "Bad Request: chat not found"


def test_deliver_again_after_failure_succeeds(storage, texts):
    lead_id = make_lead(storage)
    storage.set_setting("owner_chat_id", "12345")
    tg = FakeTelegram()
    tg.fail["send_message"] = TelegramError("timeout")
    deliver_lead(lead_id, storage, tg, texts)
    del tg.fail["send_message"]
    assert deliver_lead(lead_id, storage, tg, texts) is True
    assert storage.get_lead(lead_id)["delivery_error"] is None
