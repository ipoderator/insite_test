"""Admin panel: «Заявки» (list, filters, card, status, resend, file download) and
«Получатель заявок» (bind code, test message, unbind, new code)."""
import logging

import pytest
import requests

from leadbot.leads import NO_RECIPIENT_ERROR
from leadbot.telegram import TelegramError
from tests.sim import label
from tests.test_admin_support import Panel, path_of

OWNER = -100777


@pytest.fixture
def panel(tmp_path):
    p = Panel(tmp_path)
    p.login()
    return p


def add_lead(panel, kind="new", name="Анна", phone="+79991234567", files=None, items=None,
             username="anna_client"):
    return panel.storage.create_lead(
        kind, 1001, username, name, phone,
        items if items is not None else [{"title": "Тип сайта", "answer": "Интернет-магазин"},
                                         {"title": "Бюджет", "answer": "до 100 000 ₽"}],
        files or [], {"user_id": 1001, "first_name": "Анна", "last_name": "К.", "branch": "goods"})


TZ = {"file_id": "BQACAgIAAxkB-tz", "file_name": "ТЗ проекта.pdf", "mime": "application/pdf",
      "size": 2048, "kind": "document"}


# -- list -------------------------------------------------------------------------------

def test_empty_list_invites_to_try_the_bot(panel):
    assert "Заявок пока нет — пройдите анкету в боте" in panel.page("/admin/leads")


def test_list_shows_columns_and_filters(panel):
    first = add_lead(panel, "new", name="Анна")
    second = add_lead(panel, "promo", name="Борис", phone="+79990000000")
    panel.storage.set_lead_delivery(second, True, None)
    page = panel.page("/admin/leads")
    for text in (f"№{first}", f"№{second}", "Анна", "Борис", "+79990000000",
                 label("lead.kind.new"), label("lead.kind.promo"), "доставлена",
                 "не доставлена", "Новая"):
        assert text in page
    only_promo = panel.page("/admin/leads?kind=promo")
    assert "Борис" in only_promo and "Анна" not in only_promo
    panel.storage.set_lead_status(first, "processed")
    processed = panel.page("/admin/leads?status=processed")
    assert "Анна" in processed and "Борис" not in processed
    assert "По этому фильтру заявок нет" in panel.page("/admin/leads?kind=redesign")
    # unknown filter values are ignored
    assert "Борис" in panel.page("/admin/leads?kind=<x>&status=zzz")


def test_lead_from_the_bot_appears_in_the_panel(panel):
    bot = panel.bot()
    bot.start()
    bot.click(label("privacy.start"))
    bot.click(label("menu.opt.examples"))
    bot.click(label("ex_sphere.opt.other"))
    bot.send("Нужны сайты стоматологий")
    page = panel.page("/admin/leads")
    assert label("lead.kind.examples") in page and "не доставлена" in page
    [lead] = panel.storage.list_leads()
    card = panel.page(f"/admin/leads/{lead['id']}")
    assert "Нужны сайты стоматологий" in card and "@anna_client" in card


# -- card -------------------------------------------------------------------------------

def test_card_shows_everything(panel):
    lead_id = add_lead(panel, files=[TZ])
    page = panel.page(f"/admin/leads/{lead_id}")
    for text in ("Заявка №", "Анна", "+79991234567", "Тип сайта", "Интернет-магазин",
                 "Бюджет", "до 100 000 ₽", "ТЗ проекта.pdf", "Скачать", "@anna_client",
                 'href="https://t.me/anna_client"', "Анна К.", "не доставлена"):
        assert text in page, text
    assert f"/admin/leads/{lead_id}/files/0" in page
    assert "BQACAgIAAxkB-tz" not in page  # the Telegram file id is not exposed


def test_card_escapes_client_answers(panel):
    lead_id = add_lead(panel, name="<script>alert(1)</script>",
                       items=[{"title": "Идея", "answer": "<img src=x onerror=alert(1)>"}],
                       username="bad\"user")
    page = panel.page(f"/admin/leads/{lead_id}")
    assert "<script>alert(1)</script>" not in page and "<img src=x" not in page
    assert "&lt;img src=x onerror=alert(1)&gt;" in page
    assert "https://t.me/bad" not in page  # a username that is not a username is not a link


def test_missing_lead_is_404(panel):
    assert panel.get("/admin/leads/999").status_code == 404


def test_mark_processed_and_back(panel):
    lead_id = add_lead(panel)
    resp = panel.post(f"/admin/leads/{lead_id}/status", {"status": "processed"})
    assert resp.status_code == 302 and path_of(resp.location) == f"/admin/leads/{lead_id}"
    assert panel.storage.get_lead(lead_id)["status"] == "processed"
    assert "Вернуть в новые" in panel.page(f"/admin/leads/{lead_id}")
    panel.post(f"/admin/leads/{lead_id}/status", {"status": "new"})
    assert panel.storage.get_lead(lead_id)["status"] == "new"
    assert panel.post(f"/admin/leads/{lead_id}/status", {"status": "hacked"}).status_code == 400


def test_resend_without_recipient_explains_why(panel):
    lead_id = add_lead(panel)
    resp = panel.post(f"/admin/leads/{lead_id}/resend")
    page = panel.page(path_of(resp.location))
    assert "Не удалось отправить заявку" in page and NO_RECIPIENT_ERROR in page
    assert panel.tg.calls_of("send_message") == []


def test_resend_delivers_text_and_files_to_the_recipient(panel):
    lead_id = add_lead(panel, files=[TZ])
    panel.storage.set_setting("owner_chat_id", str(OWNER))
    resp = panel.post(f"/admin/leads/{lead_id}/resend")
    assert "Заявка отправлена владельцу" in panel.page(path_of(resp.location))
    assert any("Интернет-магазин" in t for t in panel.tg.sent_texts(OWNER))
    assert panel.tg.calls_of("send_document")[0]["file_id"] == TZ["file_id"]
    assert panel.storage.get_lead(lead_id)["delivered"] is True


# -- file download ----------------------------------------------------------------------

class FakeDownload:
    def __init__(self, body=b"%PDF-1.4 test", status_code=200):
        self.body = body
        self.status_code = status_code
        self.headers = {"Content-Length": str(len(body))}
        self.closed = False

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.body), 4):
            yield self.body[i:i + 4]

    def close(self):
        self.closed = True


def test_file_is_downloaded_through_the_server(panel, monkeypatch):
    lead_id = add_lead(panel, files=[TZ])
    fetched = []
    download = FakeDownload()

    def fake_get(url, **kwargs):
        fetched.append((url, kwargs))
        return download

    monkeypatch.setattr(requests, "get", fake_get)
    resp = panel.get(f"/admin/leads/{lead_id}/files/0")
    assert resp.status_code == 200
    assert resp.data == b"%PDF-1.4 test"
    resp.close()
    assert download.closed
    assert fetched[0][0] == panel.tg.file_url_base + TZ["file_id"]
    assert fetched[0][1].get("timeout")
    disposition = resp.headers["Content-Disposition"]
    assert disposition.startswith("attachment;")
    assert "filename*=UTF-8''%D0%A2%D0%97%20%D0%BF%D1%80%D0%BE%D0%B5%D0%BA%D1%82%D0%B0.pdf" \
        in disposition
    assert resp.headers["Content-Type"] == "application/octet-stream"
    assert panel.tg.file_url_base not in str(resp.headers)


def test_failed_download_does_not_leak_the_telegram_url(panel, monkeypatch, caplog):
    lead_id = add_lead(panel, files=[TZ])
    secret_url = panel.tg.file_url_base + TZ["file_id"]

    def broken_get(url, **kwargs):
        raise requests.ConnectionError(f"Max retries exceeded with url: {url}")

    monkeypatch.setattr(requests, "get", broken_get)
    with caplog.at_level(logging.DEBUG):
        resp = panel.get(f"/admin/leads/{lead_id}/files/0")
    assert resp.status_code == 302 and path_of(resp.location) == f"/admin/leads/{lead_id}"
    page = panel.page(path_of(resp.location))
    assert "Не удалось скачать файл" in page
    assert secret_url not in page and secret_url not in caplog.text
    assert "123:TEST" not in caplog.text


def test_telegram_refusing_the_file_is_reported(panel, monkeypatch):
    lead_id = add_lead(panel, files=[TZ])
    panel.tg.fail["get_file_url"] = TelegramError("Bad Request: file is too big")
    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("must not download"))
    resp = panel.get(f"/admin/leads/{lead_id}/files/0")
    assert "Не удалось скачать файл" in panel.page(path_of(resp.location))
    panel.tg.fail.clear()
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeDownload(status_code=404))
    resp = panel.get(f"/admin/leads/{lead_id}/files/0")
    assert resp.status_code == 302


def test_wrong_file_index_is_404(panel):
    lead_id = add_lead(panel, files=[TZ])
    assert panel.get(f"/admin/leads/{lead_id}/files/1").status_code == 404
    assert panel.get(f"/admin/leads/{lead_id + 1}/files/0").status_code == 404


def test_download_requires_login(panel):
    lead_id = add_lead(panel, files=[TZ])
    resp = panel.new_client().get(f"/admin/leads/{lead_id}/files/0")
    assert resp.status_code == 302 and path_of(resp.location).startswith("/admin/login")


# -- recipient --------------------------------------------------------------------------

def test_recipient_page_shows_the_bind_code(panel):
    code = panel.storage.get_setting("owner_bind_code")
    page = panel.page("/admin/recipient")
    assert f"/owner {code}" in page and "не привязан" in page
    assert "Тестовое сообщение" not in page


def test_binding_in_the_bot_shows_the_chat(panel):
    code = panel.storage.get_setting("owner_bind_code")
    owner = panel.bot(chat_id=OWNER, chat_type="group", username="owner")
    owner.send(f"/owner {code}")
    page = panel.page("/admin/recipient")
    assert "привязан" in page and str(OWNER) in page and "Рабочая группа" in page
    assert f"/owner {code}" not in page  # the code changed after binding


def test_test_message_goes_to_the_recipient(panel):
    panel.storage.set_setting("owner_chat_id", str(OWNER))
    resp = panel.post("/admin/recipient/test")
    assert "Тестовое сообщение отправлено" in panel.page(path_of(resp.location))
    assert panel.tg.sent_texts(OWNER) == [label("owner.test")]


def test_test_message_failure_is_shown(panel):
    panel.storage.set_setting("owner_chat_id", str(OWNER))
    panel.tg.fail["send_message"] = TelegramError("Forbidden: bot was kicked from the group chat")
    resp = panel.post("/admin/recipient/test")
    assert "bot was kicked" in panel.page(path_of(resp.location))


def test_test_message_without_recipient(panel):
    resp = panel.post("/admin/recipient/test")
    assert "ещё не привязан" in panel.page(path_of(resp.location))
    assert panel.tg.calls_of("send_message") == []


def test_unbind_stops_delivery(panel):
    panel.storage.set_setting("owner_chat_id", str(OWNER))
    panel.post("/admin/recipient/unbind")
    assert (panel.storage.get_setting("owner_chat_id") or "") == ""
    assert "не привязан" in panel.page("/admin/recipient")
    lead_id = add_lead(panel)
    panel.post(f"/admin/leads/{lead_id}/resend")
    assert panel.storage.get_lead(lead_id)["delivery_error"] == NO_RECIPIENT_ERROR


def test_new_code_replaces_the_old_one(panel):
    old = panel.storage.get_setting("owner_bind_code")
    resp = panel.post("/admin/recipient/code")
    new = panel.storage.get_setting("owner_bind_code")
    assert new != old and f"/owner {new}" in panel.page(path_of(resp.location))
    owner = panel.bot(chat_id=OWNER, chat_type="group", username="owner")
    owner.send(f"/owner {old}")
    assert (panel.storage.get_setting("owner_chat_id") or "") == ""
