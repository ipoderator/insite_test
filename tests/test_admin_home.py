"""Admin panel home: the checklist of data still missing before the bot goes to work."""
import pytest

from leadbot.admin import content
from leadbot.storage import DEFAULT_CATEGORIES
from tests.test_admin_support import Panel

DEMO_ITEMS = sum(len(titles) for *_, titles in DEFAULT_CATEGORIES)


@pytest.fixture
def panel(tmp_path):
    p = Panel(tmp_path)
    p.login()
    return p


def gaps_of(page: str) -> str:
    return page.split('class="card gaps"')[1].split("</section>")[0] \
        if 'class="card gaps"' in page else ""


def fill_everything(panel):
    st = panel.storage
    st.set_setting("owner_chat_id", "-100777")
    st.set_setting("privacy_url", "https://example.ru/privacy")
    for cat in st.list_categories():
        for item in st.list_items(cat["id"]):
            st.save_item({"id": item["id"], "is_demo": False})


def test_fresh_bot_lists_what_is_still_demo(panel):
    gaps = gaps_of(panel.page("/admin"))
    assert "Перед запуском осталось заполнить: 4" in gaps
    for text in ("Получатель заявок не привязан",
                 "Ссылка на политику конфиденциальности ведёт на демо-страницу",
                 "Демо-текст политики не заполнен",
                 f"Примеры работ с пометкой «демо»: {DEMO_ITEMS}"):
        assert text in gaps
    for href in ("/admin/recipient", "/admin/contacts", "/admin/policy", "/admin/examples"):
        assert f'href="{href}"' in gaps


def test_filled_bot_has_no_gaps(panel):
    fill_everything(panel)
    page = panel.page("/admin")
    assert gaps_of(page) == "" and "Демо-данных не осталось" in page


def test_own_policy_text_closes_its_gap_and_a_link_closes_both(panel):
    panel.post("/admin/policy", {"text": "[НАЗВАНИЕ КОМПАНИИ — впишите]"})
    assert "Демо-текст политики не заполнен" in gaps_of(panel.page("/admin"))
    panel.post("/admin/policy", {"text": "ООО «Ромашка», ИНН 7700000000. Храним 1 год."})
    gaps = gaps_of(panel.page("/admin"))
    assert "Демо-текст политики" not in gaps and "ведёт на демо-страницу" in gaps
    panel.storage.set_setting("privacy_url", "https://example.ru/privacy")
    assert "политик" not in gaps_of(panel.page("/admin"))


def test_demo_examples_and_empty_spheres_are_named(panel):
    fill_everything(panel)
    st = panel.storage
    auto = st.get_category("auto")
    [first, *_] = st.list_items(auto["id"])
    st.save_item({"id": first["id"], "is_demo": True})
    food = st.get_category("food")
    for item in st.list_items(food["id"]):
        st.delete_item(item["id"])
    gaps = gaps_of(panel.page("/admin"))
    assert "Примеры работ с пометкой «демо»: 1" in gaps and auto["button"] in gaps
    assert "Сферы без примеров: 1" in gaps and food["button"] in gaps
    # a hidden sphere is not shown to clients, so it is not a gap
    st.save_category({"id": food["id"], "active": False})
    st.save_category({"id": auto["id"], "active": False})
    assert gaps_of(panel.page("/admin")) == ""


def test_policy_is_filled_rules():
    assert not content.policy_is_filled(None)
    assert not content.policy_is_filled(content.DEFAULT_PRIVACY_PAGE)
    assert not content.policy_is_filled("Срок: [СРОК ХРАНЕНИЯ — впишите]")
    assert content.policy_is_filled("Политика ООО «Ромашка»")
