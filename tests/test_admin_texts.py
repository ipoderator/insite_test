"""Admin panel: «Тексты» and «Информационные блоки» — list, search, save, reset, checks."""
import html
import re

import pytest

from leadbot.texts import GROUPS, TEXTS
from tests.sim import label, plain
from tests.test_admin_support import Panel, path_of

SAVED = "Сохранено, бот уже использует новый текст"


@pytest.fixture
def panel(tmp_path):
    p = Panel(tmp_path)
    p.login()
    return p


def shown_keys(page: str):
    return set(re.findall(r'id="t-([^"]+)"', page))


def test_every_text_is_listed_in_its_group_with_label_and_default(panel):
    seen = set()
    for group_id, group_name in GROUPS:
        page = panel.page(f"/admin/texts?group={group_id}")
        assert html.escape(group_name) in page
        keys = shown_keys(page)
        assert keys == {k for k, d in TEXTS.items() if d.group == group_id}
        for key in keys:
            assert html.escape(TEXTS[key].label) in page
            assert html.escape(TEXTS[key].default) in page
        seen |= keys
    assert seen == set(TEXTS)


def test_info_blocks_section_lists_the_info_texts(panel):
    page = panel.page("/admin/info")
    expected = {k for k, d in TEXTS.items() if d.group == "info"}
    assert shown_keys(page) == expected
    assert {"privacy.text", "menu.text", "pr_what.intro", "fin_name.intro", "thanks.text",
            "ex_card.note", "contacts.text", "rd_promoted.footer"} <= expected


def test_saved_text_is_used_by_the_bot_at_once(panel):
    bot = panel.bot()
    resp = panel.post("/admin/texts/menu.text",
                      {"value": "Добрый день! Выберите, что нужно:", "section": "info"})
    assert resp.status_code == 302
    assert path_of(resp.location).startswith("/admin/info")
    assert SAVED in panel.page(path_of(resp.location))
    bot.start()
    bot.click(label("privacy.start"))
    assert "Добрый день! Выберите, что нужно:" in bot.current_text
    assert label("menu.text") not in bot.current_text


def test_changed_text_is_marked_and_can_be_reset(panel):
    panel.post("/admin/texts/privacy.start", {"value": "Поехали", "group": "start"})
    page = panel.page("/admin/texts?group=start")
    block = page.split('id="t-privacy.start"')[1].split("</form>")[0]
    assert "изменён" in block and "Вернуть по умолчанию" in block
    assert html.escape(label("privacy.start")) in block  # the default is shown
    bot = panel.bot()
    bot.start()
    assert "Поехали" in bot.buttons

    resp = panel.post("/admin/texts/privacy.start/reset", {"group": "start"})
    assert resp.status_code == 302
    assert path_of(resp.location).startswith("/admin/texts?group=start")
    assert panel.storage.get_text_override("privacy.start") is None
    block = panel.page("/admin/texts?group=start").split('id="t-privacy.start"')[1] \
        .split("</form>")[0]
    assert "изменён" not in block
    other = panel.bot(chat_id=5002)
    other.start()
    assert label("privacy.start") in other.buttons


def test_saving_the_default_text_is_not_a_change(panel):
    panel.post("/admin/texts/menu.text", {"value": "Другое"})
    panel.post("/admin/texts/menu.text", {"value": "  " + label("menu.text") + "\r\n"})
    assert panel.storage.get_text_override("menu.text") is None


def test_privacy_block_change_reaches_the_bot_with_the_link(panel):
    panel.post("/admin/texts/privacy.text",
               {"value": "Мы бережём ваши данные. Подробнее — {policy}", "section": "info"})
    bot = panel.bot()
    bot.start()
    assert plain(bot.current_text).startswith("Мы бережём ваши данные. Подробнее — "
                                              + label("privacy.link"))
    assert 'href="https://bot.example.org/privacy"' in bot.last_html


@pytest.mark.parametrize("value", ["", "   ", "\n\n"])
def test_empty_text_is_refused(panel, value):
    resp = panel.post("/admin/texts/menu.text", {"value": value})
    assert resp.status_code == 400
    assert "не может быть пустым" in resp.get_data(as_text=True)
    assert panel.storage.get_text_override("menu.text") is None


def test_message_limit_is_3500(panel):
    ok = panel.post("/admin/texts/menu.text", {"value": "я" * 3500})
    assert ok.status_code == 302
    too_long = panel.post("/admin/texts/menu.text", {"value": "ю" * 3501})
    assert too_long.status_code == 400
    assert "3500" in too_long.get_data(as_text=True)
    assert panel.storage.get_text_override("menu.text") == "я" * 3500


def test_button_limit_is_60(panel):
    assert panel.post("/admin/texts/menu.opt.new", {"value": "к" * 61}).status_code == 400
    assert panel.storage.get_text_override("menu.opt.new") is None
    assert panel.post("/admin/texts/menu.opt.new", {"value": "к" * 60}).status_code == 302


def test_long_pdf_buttons_keep_their_default_length(panel):
    for key, length in (("popup.opt.texts", 63), ("rd_what.opt.features", 70)):
        assert len(TEXTS[key].default) == length
        assert panel.post(f"/admin/texts/{key}", {"value": "д" * length}).status_code == 302
        assert panel.post(f"/admin/texts/{key}",
                          {"value": "д" * (length + 1)}).status_code == 400


def test_button_must_be_one_line(panel):
    resp = panel.post("/admin/texts/menu.opt.new", {"value": "Новый\nсайт"})
    assert resp.status_code == 400


def test_popup_text_is_limited_to_200(panel):
    assert panel.post("/admin/texts/err.empty_multi", {"value": "в" * 201}).status_code == 400
    assert panel.post("/admin/texts/err.empty_multi", {"value": "в" * 200}).status_code == 302


def test_error_keeps_what_was_typed_and_explains(panel):
    resp = panel.post("/admin/texts/contacts.phone", {"value": "Телефон студии", "group": "contacts"})
    page = resp.get_data(as_text=True)
    assert resp.status_code == 400
    assert "Телефон студии" in page
    assert "{value}" in page and "Оставьте в тексте" in page
    assert panel.storage.get_text_override("contacts.phone") is None


def test_search_finds_texts_by_words_across_groups(panel):
    page = panel.page("/admin/texts?q=бюджет")
    keys = shown_keys(page)
    expected = {k for k, d in TEXTS.items()
                if "бюджет" in (d.default + d.label + k).casefold()}
    assert keys == expected and len({TEXTS[k].group for k in keys}) > 1
    panel.post("/admin/texts/menu.text", {"value": "Уникальное слово кукушка"})
    assert shown_keys(panel.page("/admin/texts?q=КУКУШКА")) == {"menu.text"}
    assert "Ничего не нашлось" in panel.page("/admin/texts?q=zzzнетслова")


def test_save_from_search_returns_to_the_search(panel):
    resp = panel.post("/admin/texts/menu.text", {"value": "Привет", "q": "привет"})
    assert path_of(resp.location).startswith("/admin/texts?q=")


def test_markup_from_the_panel_is_plain_text_everywhere(panel):
    evil = '<script>alert(1)</script> <b>жирный</b> & "кавычки"'
    panel.post("/admin/texts/menu.text", {"value": evil, "section": "info"})
    page = panel.page("/admin/info")
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    bot = panel.bot()
    bot.start()
    bot.click(label("privacy.start"))  # ChatSim also checks the HTML is valid for Telegram
    assert "&lt;script&gt;" in bot.last_html and "<script>" not in bot.last_html
    assert bot.current_text.startswith(evil)


def test_unknown_text_key_is_404(panel):
    assert panel.post("/admin/texts/no.such.key", {"value": "x"}).status_code == 404


def test_redirect_after_save_ignores_foreign_group(panel):
    resp = panel.post("/admin/texts/menu.text", {"value": "Привет", "group": "//evil.example"})
    assert path_of(resp.location).startswith("/admin/texts?group=")
    assert "evil" not in resp.location


def test_question_titles_are_edited_as_edit_list_buttons(panel):
    block = panel.page("/admin/texts?group=new").split('id="t-pay.title"')[1].split("</form>")[0]
    assert ">кнопка<" in block and '<input type="text"' in block and "<textarea" not in block
    assert panel.post("/admin/texts/pay.title", {"value": "Оплата\nонлайн"}).status_code == 400
    assert panel.post("/admin/texts/pay.title", {"value": "о" * 61}).status_code == 400
    assert panel.storage.get_text_override("pay.title") is None
    assert panel.post("/admin/texts/pay.title", {"value": "Онлайн-оплата"}).status_code == 302
    # a title the summary does not show stays a plain text
    block = panel.page("/admin/texts?group=examples").split('id="t-ex_sphere.title"')[1]
    assert ">кнопка<" not in block.split("</form>")[0]
