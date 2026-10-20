"""Admin panel: «Примеры работ» — spheres and examples; changes are visible in the bot."""
import html
import re

import pytest

from tests.sim import label
from tests.test_admin_support import Panel, path_of


@pytest.fixture
def panel(tmp_path):
    p = Panel(tmp_path)
    p.login()
    return p


def spheres_in_bot(panel, chat_id=5001):
    bot = panel.bot(chat_id=chat_id)
    bot.start()
    bot.click(label("privacy.start"))
    bot.click(label("menu.opt.examples"))
    return bot


def sphere_buttons(bot):
    return bot.buttons[:-2]  # without «Иное» and «Назад»


def test_list_shows_spheres_in_order_with_counts(panel):
    page = panel.page("/admin/examples")
    cats = panel.storage.list_categories()
    positions = [page.index(html.escape(c["button"])) for c in cats]
    assert positions == sorted(positions)
    assert "примеров: 3" in page
    assert "Добавить сферу" in page


def test_new_sphere_with_example_is_shown_in_the_bot(panel):
    resp = panel.post("/admin/examples/new", {"button": "🎓 Образование",
                                               "topic": "тематике образования"})
    assert resp.status_code == 302
    category_id = int(re.search(r"/admin/examples/(\d+)", path_of(resp.location)).group(1))
    resp = panel.post(f"/admin/examples/{category_id}/items/new",
                      {"title": "Онлайн-школа", "url": "school.example.org", "note": "курсы",
                       "is_demo": ""})
    assert resp.status_code == 302
    [item] = panel.storage.list_items(category_id)
    assert item["url"] == "https://school.example.org" and item["is_demo"] is False

    bot = spheres_in_bot(panel)
    assert sphere_buttons(bot)[-1] == "🎓 Образование"
    bot.click("🎓 Образование")
    assert bot.current_text.startswith(label("ex_card.header", topic="тематике образования"))
    assert "Онлайн-школа — курсы" in bot.current_text
    assert label("ex_card.demo") not in bot.current_text
    assert '<a href="https://school.example.org">Онлайн-школа</a>' in bot.last_html


def test_sphere_edit_and_hide(panel):
    sport = panel.storage.get_category("sport")
    resp = panel.post(f"/admin/examples/{sport['id']}",
                      {"button": "🏋️ Фитнес", "topic": "тематике фитнеса", "active": "1"})
    assert resp.status_code == 302
    assert "🏋️ Фитнес" in sphere_buttons(spheres_in_bot(panel))
    panel.post(f"/admin/examples/{sport['id']}/toggle")
    assert panel.storage.get_category("sport")["active"] is False
    assert "🏋️ Фитнес" not in sphere_buttons(spheres_in_bot(panel, chat_id=5002))
    assert "скрыта в боте" in panel.page("/admin/examples")
    panel.post(f"/admin/examples/{sport['id']}/toggle")
    assert "🏋️ Фитнес" in sphere_buttons(spheres_in_bot(panel, chat_id=5003))


def test_unchecked_active_box_hides_the_sphere(panel):
    auto = panel.storage.get_category("auto")
    panel.post(f"/admin/examples/{auto['id']}", {"button": auto["button"], "topic": auto["topic"]})
    assert panel.storage.get_category("auto")["active"] is False


def test_sphere_order_is_changed_with_arrows(panel):
    before = [c["button"] for c in panel.storage.list_categories()]
    second = panel.storage.list_categories()[1]
    assert panel.post(f"/admin/examples/{second['id']}/move",
                      {"direction": "up"}).status_code == 302
    after = [c["button"] for c in panel.storage.list_categories()]
    assert after == [before[1], before[0]] + before[2:]
    assert sphere_buttons(spheres_in_bot(panel)) == after
    last = panel.storage.list_categories()[-1]
    panel.post(f"/admin/examples/{last['id']}/move", {"direction": "down"})  # stays last
    assert [c["button"] for c in panel.storage.list_categories()] == after


def test_sphere_delete_asks_for_confirmation(panel):
    food = panel.storage.get_category("food")
    confirm = panel.page(f"/admin/examples/{food['id']}/delete")
    assert "Удалить сферу?" in confirm and html.escape(food["button"]) in confirm
    assert "Да, удалить" in confirm and "Отмена" in confirm
    assert panel.storage.get_category("food") is not None  # GET deletes nothing
    resp = panel.post(f"/admin/examples/{food['id']}/delete")
    assert resp.status_code == 302
    assert panel.storage.get_category("food") is None
    assert food["button"] not in sphere_buttons(spheres_in_bot(panel))


def test_example_edit_demo_flag_order_and_delete(panel):
    auto = panel.storage.get_category("auto")
    first, second = panel.storage.list_items(auto["id"])
    assert first["is_demo"] and second["is_demo"]
    resp = panel.post(f"/admin/examples/{auto['id']}/items/{first['id']}",
                      {"title": "Автосервис «Пример»", "url": "https://auto.example.org",
                       "note": "", "is_demo": ""})
    assert resp.status_code == 302
    panel.post(f"/admin/examples/{auto['id']}/items/{second['id']}/move", {"direction": "up"})
    bot = spheres_in_bot(panel)
    bot.click(auto["button"])
    text = bot.current_text
    assert text.index("Шиномонтаж") < text.index("Автосервис «Пример»")
    assert f"Сайт №2 – Шиномонтаж и ТО {label('ex_card.demo')}" in text
    assert f"Автосервис «Пример» {label('ex_card.demo')}" not in text

    confirm = panel.page(f"/admin/examples/{auto['id']}/items/{second['id']}/delete")
    assert "Удалить пример?" in confirm
    panel.post(f"/admin/examples/{auto['id']}/items/{second['id']}/delete")
    assert [i["id"] for i in panel.storage.list_items(auto["id"])] == [first["id"]]


def test_category_page_shows_examples_and_demo_badge(panel):
    auto = panel.storage.get_category("auto")
    page = panel.page(f"/admin/examples/{auto['id']}")
    assert "Сайт №1 – Автосервис" in page and "демо" in page
    assert "Добавить пример" in page


@pytest.mark.parametrize("data,field_error", [
    ({"button": "", "topic": "тематике"}, "Впишите название кнопки"),
    ({"button": "х" * 61, "topic": "тематике"}, "не больше 60"),
    ({"button": "Кнопка", "topic": ""}, "Впишите тематику"),
])
def test_invalid_sphere_is_refused(panel, data, field_error):
    count = len(panel.storage.list_categories())
    resp = panel.post("/admin/examples/new", data)
    assert resp.status_code == 400
    assert field_error in resp.get_data(as_text=True)
    assert len(panel.storage.list_categories()) == count


@pytest.mark.parametrize("data,field_error", [
    ({"title": "", "url": ""}, "Впишите название примера"),
    ({"title": "Сайт", "url": "javascript:alert(1)"}, "не похоже на ссылку"),
    ({"title": "Сайт", "url": "", "note": "я" * 501}, "не больше 500"),
])
def test_invalid_example_is_refused(panel, data, field_error):
    auto = panel.storage.get_category("auto")
    resp = panel.post(f"/admin/examples/{auto['id']}/items/new", data)
    assert resp.status_code == 400
    assert field_error in resp.get_data(as_text=True)
    assert len(panel.storage.list_items(auto["id"])) == 2


def test_item_of_another_sphere_is_404(panel):
    auto = panel.storage.get_category("auto")
    food = panel.storage.get_category("food")
    item = panel.storage.list_items(food["id"])[0]
    resp = panel.post(f"/admin/examples/{auto['id']}/items/{item['id']}/delete")
    assert resp.status_code == 404
    assert len(panel.storage.list_items(food["id"])) == 2


def test_example_markup_is_escaped_in_the_panel(panel):
    auto = panel.storage.get_category("auto")
    panel.post(f"/admin/examples/{auto['id']}/items/new",
               {"title": "<script>alert(1)</script>", "url": "", "note": "<b>x</b>",
                "is_demo": "1"})
    page = panel.page(f"/admin/examples/{auto['id']}")
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    assert "<b>x</b>" not in page
