"""Admin panel: history of texts, contacts and the demo policy, and rolling a change back."""
import re

import pytest

from leadbot.admin import content
from leadbot.storage import HISTORY_KEEP, HISTORY_SETTING, HISTORY_TEXT
from tests.sim import label
from tests.test_admin_support import Panel, path_of

CONTACTS = {"contact_telegram": "@site_it", "contact_phone": "+7 999 155-10-43",
            "contact_email": "site-it@mail.ru", "privacy_url": ""}


@pytest.fixture
def panel(tmp_path):
    p = Panel(tmp_path)
    p.login()
    return p


def restore_buttons(block: str):
    return re.findall(r'formaction="(/admin/texts/[^"]+/restore/\d+)"', block)


def text_block(panel, path: str, key: str) -> str:
    return panel.page(path).split(f'id="t-{key}"')[1].split("</form>")[0]


# -- storage -------------------------------------------------------------------------

def test_storage_keeps_previous_values_newest_first(panel):
    st = panel.storage
    st.set_text_override("menu.text", "Первый")
    st.set_text_override("menu.text", "Первый")  # the same value is not a new version
    st.set_text_override("menu.text", "Второй")
    st.delete_text_override("menu.text")
    st.delete_text_override("menu.text")  # nothing to delete — nothing to remember
    assert [e["value"] for e in st.history(HISTORY_TEXT, ["menu.text"])] == \
        ["Второй", "Первый", None]
    assert st.history_counts(HISTORY_TEXT) == {"menu.text": 3}
    assert st.history(HISTORY_TEXT, []) == []

    st.set_setting("contact_phone", "+7 1", keep_history=True)
    st.set_setting("contact_phone", "+7 1", keep_history=True)
    st.set_setting("owner_chat_id", "42")  # settings without history stay without it
    assert [e["value"] for e in st.history(HISTORY_SETTING, ["contact_phone"])] == \
        [CONTACTS["contact_phone"]]
    assert st.history(HISTORY_SETTING, ["owner_chat_id"]) == []


def test_history_is_capped_per_key(panel):
    for n in range(HISTORY_KEEP + 5):
        panel.storage.set_text_override("menu.text", f"Версия {n}")
    panel.storage.set_text_override("privacy.start", "Поехали")
    entries = panel.storage.history(HISTORY_TEXT, ["menu.text"], limit=100)
    assert len(entries) == HISTORY_KEEP
    assert entries[0]["value"] == f"Версия {HISTORY_KEEP + 3}"
    assert len(panel.storage.history(HISTORY_TEXT, ["privacy.start"])) == 1


# -- texts ---------------------------------------------------------------------------

def test_previous_text_can_be_restored_from_the_history(panel):
    path = "/admin/texts?group=start"
    assert "История изменений" not in text_block(panel, path, "privacy.start")
    panel.post("/admin/texts/privacy.start", {"value": "Поехали", "group": "start"})
    panel.post("/admin/texts/privacy.start", {"value": "Вперёд", "group": "start"})
    block = text_block(panel, path, "privacy.start")
    assert "История изменений (2)" in block and "Поехали" in block
    assert "текст по умолчанию" in block and "Вернуть предыдущую версию" in block
    newest, oldest = restore_buttons(block)

    resp = panel.post(newest, {"group": "start"})
    assert path_of(resp.location) == path
    assert panel.storage.get_text_override("privacy.start") == "Поехали"
    assert "Возвращена версия" in panel.page(path)
    bot = panel.bot()
    bot.start()
    assert "Поехали" in bot.buttons

    panel.post(oldest, {"group": "start"})  # the very first version — the default text
    assert panel.storage.get_text_override("privacy.start") is None
    # the rollback itself is in the history: it can be undone too
    assert "История изменений (4)" in text_block(panel, path, "privacy.start")


def test_restore_from_an_info_block_returns_to_the_info_page(panel):
    panel.post("/admin/texts/menu.text", {"value": "Привет!", "section": "info"})
    [button] = restore_buttons(text_block(panel, "/admin/info", "menu.text"))
    resp = panel.post(button, {"section": "info"})
    assert path_of(resp.location) == "/admin/info"
    assert panel.storage.get_text_override("menu.text") is None


def test_restore_refuses_a_foreign_or_unknown_version(panel):
    panel.post("/admin/texts/privacy.start", {"value": "Поехали"})
    [entry] = panel.storage.history(HISTORY_TEXT, ["privacy.start"])
    assert panel.post(f"/admin/texts/menu.text/restore/{entry['id']}").status_code == 404
    assert panel.post("/admin/texts/menu.text/restore/999999").status_code == 404
    assert panel.post(f"/admin/texts/no.such/restore/{entry['id']}").status_code == 404
    assert panel.post(f"/admin/contacts/restore/{entry['id']}").status_code == 404
    assert panel.post(f"/admin/policy/restore/{entry['id']}").status_code == 404
    assert panel.storage.get_text_override("privacy.start") == "Поехали"


def test_a_version_that_no_longer_passes_the_checks_is_not_restored(panel):
    panel.storage.set_text_override("contacts.phone", "Телефон без метки")  # bypasses checks
    panel.storage.set_text_override("contacts.phone", "📞 {value}")
    [newest, _] = panel.storage.history(HISTORY_TEXT, ["contacts.phone"])
    resp = panel.post(f"/admin/texts/contacts.phone/restore/{newest['id']}",
                      {"group": "contacts"})
    assert resp.status_code == 302
    assert "Эту версию нельзя вернуть" in panel.page(path_of(resp.location))
    assert panel.storage.get_text_override("contacts.phone") == "📞 {value}"


def test_restore_needs_csrf(panel):
    panel.post("/admin/texts/privacy.start", {"value": "Поехали"})
    [entry] = panel.storage.history(HISTORY_TEXT, ["privacy.start"])
    resp = panel.post(f"/admin/texts/privacy.start/restore/{entry['id']}", csrf=False)
    assert resp.status_code == 400
    assert panel.storage.get_text_override("privacy.start") == "Поехали"


# -- contacts and policy -----------------------------------------------------------------

def test_contacts_keep_their_history_and_restore_a_field(panel):
    assert "История изменений" not in panel.page("/admin/contacts")
    panel.post("/admin/contacts", {**CONTACTS, "contact_phone": "+7 900 000-00-00"})
    page = panel.page("/admin/contacts")
    history = page.split("История изменений")[1]
    assert "Телефон" in history and CONTACTS["contact_phone"] in history
    assert "Telegram" not in history  # unchanged fields add nothing
    [entry] = panel.storage.history(HISTORY_SETTING, content.CONTACT_FIELDS)

    resp = panel.post(f"/admin/contacts/restore/{entry['id']}")
    assert path_of(resp.location) == "/admin/contacts"
    assert panel.storage.get_setting("contact_phone") == CONTACTS["contact_phone"]
    assert "возвращено прежнее значение" in panel.page("/admin/contacts")
    bot = panel.bot()
    bot.start()
    bot.click(label("privacy.start"))
    bot.click(label("menu.opt.contacts"))
    assert CONTACTS["contact_phone"] in bot.current_text


def test_contacts_page_shows_the_contacts_screen(panel):
    page = panel.page("/admin/contacts")
    tg = page.split('class="tg"')[1]
    assert CONTACTS["contact_email"] in tg and label("contacts.opt.order") in tg


def test_policy_text_history_and_restore(panel):
    panel.post("/admin/policy", {"text": "Политика ООО «Ромашка», версия 1"})
    panel.post("/admin/policy", {"text": "Политика ООО «Ромашка», версия 2"})
    page = panel.page("/admin/policy")
    assert "История изменений" in page and "версия 1" in page.split("История изменений")[1]
    newest, oldest = panel.storage.history(HISTORY_SETTING, [content.PRIVACY_PAGE_SETTING])
    panel.post(f"/admin/policy/restore/{newest['id']}")
    assert panel.storage.get_setting(content.PRIVACY_PAGE_SETTING) == \
        "Политика ООО «Ромашка», версия 1"
    panel.post(f"/admin/policy/restore/{oldest['id']}")  # before any edit: the demo text
    assert panel.storage.get_setting(content.PRIVACY_PAGE_SETTING) == ""
    assert "демонстрационный текст" in panel.page("/admin/policy").split("История")[1]
