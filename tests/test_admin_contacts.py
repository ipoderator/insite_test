"""Admin panel: «Контакты и ссылки», «Политика (демо)» and the public page /privacy."""
import pytest

from tests.sim import label
from tests.test_admin_support import Panel, path_of

CONTACTS = {"contact_telegram": "@studio_bot_test", "contact_phone": "+7 900 000-00-00",
            "contact_email": "hello@example.org", "privacy_url": ""}


@pytest.fixture
def panel(tmp_path):
    p = Panel(tmp_path)
    p.login()
    return p


def open_contacts(bot):
    bot.start()
    bot.click(label("privacy.start"))
    bot.click(label("menu.opt.contacts"))
    return bot.current_text


def test_form_shows_current_values(panel):
    page = panel.page("/admin/contacts")
    for value in ("@site_it", "+7 999 155-10-43", "site-it@mail.ru"):
        assert f'value="{value}"' in page
    assert "Ссылка на политику конфиденциальности" in page


def test_changed_contacts_appear_on_the_contacts_screen(panel):
    resp = panel.post("/admin/contacts", CONTACTS)
    assert resp.status_code == 302
    assert "Сохранено" in panel.page(path_of(resp.location))
    text = open_contacts(panel.bot())
    for value in ("@studio_bot_test", "+7 900 000-00-00", "hello@example.org"):
        assert value in text
    assert "@site_it" not in text


def test_empty_contact_is_hidden_in_the_bot(panel):
    panel.post("/admin/contacts", {**CONTACTS, "contact_phone": ""})
    text = open_contacts(panel.bot())
    assert label("contacts.phone", value="").strip() not in text
    assert "@studio_bot_test" in text


def test_telegram_link_is_normalized_to_username(panel):
    panel.post("/admin/contacts", {**CONTACTS, "contact_telegram": "https://t.me/site_it"})
    assert panel.storage.get_setting("contact_telegram") == "@site_it"
    panel.post("/admin/contacts", {**CONTACTS, "contact_telegram": "site_it"})
    assert panel.storage.get_setting("contact_telegram") == "@site_it"


def test_policy_url_appears_in_the_privacy_block(panel):
    resp = panel.post("/admin/contacts", {**CONTACTS, "privacy_url": "example.org/policy?a=1&b=2"})
    assert resp.status_code == 302
    assert panel.storage.get_setting("privacy_url") == "https://example.org/policy?a=1&b=2"
    bot = panel.bot()
    bot.start()
    assert 'href="https://example.org/policy?a=1&amp;b=2"' in bot.last_html


def test_empty_policy_url_links_the_demo_page(panel):
    panel.post("/admin/contacts", {**CONTACTS, "privacy_url": ""})
    bot = panel.bot()
    bot.start()
    assert 'href="https://bot.example.org/privacy"' in bot.last_html


@pytest.mark.parametrize("field,value", [
    ("privacy_url", "javascript:alert(1)"),
    ("privacy_url", "не ссылка"),
    ("contact_email", "почта"),
    ("contact_phone", "позвоните нам"),
    ("contact_telegram", "@a b"),
])
def test_invalid_values_are_refused_and_nothing_is_saved(panel, field, value):
    resp = panel.post("/admin/contacts", {**CONTACTS, field: value})
    assert resp.status_code == 400
    assert "исправьте" in resp.get_data(as_text=True)
    assert panel.storage.get_setting("contact_telegram") == "@site_it"
    assert panel.storage.get_setting(field) != value


def test_contact_markup_is_escaped_in_the_panel(panel):
    resp = panel.post("/admin/contacts", {**CONTACTS, "contact_email": '"><script>x</script>'})
    page = resp.get_data(as_text=True)
    assert "<script>x</script>" not in page and "&lt;script&gt;" in page


# -- the demo policy page --------------------------------------------------------------

def test_public_privacy_page_is_marked_as_demo(panel):
    resp = panel.new_client().get("/privacy")  # no login needed
    page = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "ДЕМОНСТРАЦИОННЫЙ ТЕКСТ" in page and "замените на реальную политику" in page
    assert 'name="viewport"' in page


def test_policy_text_is_edited_in_the_panel(panel):
    page = panel.page("/admin/policy")
    assert "Это демонстрационный текст" in page
    resp = panel.post("/admin/policy", {"text": "Наша политика.\n\nВторой абзац."})
    assert resp.status_code == 302
    public = panel.new_client().get("/privacy").get_data(as_text=True)
    assert "Наша политика." in public and "Второй абзац." in public
    assert "ДЕМОНСТРАЦИОННЫЙ ТЕКСТ" in public


def test_policy_reset_returns_the_demo_text(panel):
    default = panel.new_client().get("/privacy").get_data(as_text=True)
    panel.post("/admin/policy", {"text": "Временный текст"})
    panel.post("/admin/policy/reset")
    assert panel.new_client().get("/privacy").get_data(as_text=True) == default


def test_empty_policy_is_refused(panel):
    assert panel.post("/admin/policy", {"text": "  "}).status_code == 400


def test_policy_text_is_escaped_on_the_public_page(panel):
    panel.post("/admin/policy", {"text": '<script>alert("x")</script><img src=x onerror=1>'})
    public = panel.new_client().get("/privacy")
    page = public.get_data(as_text=True)
    assert "<script>" not in page and "<img" not in page
    assert "&lt;script&gt;" in page
    assert "script-src 'none'" in public.headers["Content-Security-Policy"]
