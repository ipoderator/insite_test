"""New-site branches G, S, B, M from /start to a delivered lead (simulator seam)."""
import pytest

from leadbot import scenario
from tests.sim import (Env, answer_default, assert_question, enter_branch, label, open_menu,
                       walk)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def thanks_buttons():
    return [label("thanks.opt.menu"), label("thanks.opt.examples"), label("thanks.opt.contacts")]


@pytest.mark.parametrize("branch", scenario.NEW_SITE_BRANCHES)
def test_new_site_branch_from_start_to_submitted_lead(env, branch):
    env.bind_owner()
    sim = env.sim()
    open_menu(sim)
    enter_branch(sim, branch)
    walk(sim, branch)

    assert label("summary.header") in sim.current_text
    sim.click(label("summary.opt.submit"))

    [lead] = env.leads
    assert lead["kind"] == "new"
    path = ("ns_what",) + scenario.BRANCHES[branch] + scenario.BRANCHES["final"]
    assert [i["title"] for i in lead["items"]] == \
        [label(scenario.NODES[n].title_key) for n in path]
    assert lead["name"] == "Анна" and lead["phone"] == "+79991234567"
    assert lead["username"] == "anna_client" and lead["chat_id"] == sim.chat_id
    assert lead["delivered"] is True
    assert sim.last_text == label("thanks.text", name="Анна")
    assert sim.buttons == thanks_buttons()
    owner_text = "\n".join(env.owner_texts())
    assert label("lead.header", id=lead["id"]) in owner_text
    assert label("lead.kind.new") in owner_text


def test_goods_branch_answers_land_in_the_lead_verbatim(env):
    sim = env.sim()
    open_menu(sim)
    enter_branch(sim, "goods")
    walk(sim, "goods")
    sim.click(label("summary.opt.submit"))
    [lead] = env.leads
    answers = {i["title"]: i["answer"] for i in lead["items"]}
    assert answers == {
        "Что разместить на сайте": "🛒 Товары",
        "Оплата на сайте": "Да",
        "Расчёт доставки": "Да",
        "Учёт остатков": "Да",
        "Интеграция с сервисами": "Да",
        "Функционал": "📧 Подписка на рассылку",
        "Вариации товаров": "Цвет",
        "Количество товаров": "< 100",
        "Информация о товарах": "📊 Заполню Excel",
        "Разделы сайта": "📄 Основные страницы: 🏢 О компании / Обо мне",
        "Стилистика и дизайн": "🎨 Полный индивидуальный дизайн + логотип",
        "Пример сайта / ТЗ": "❌ Нет, нужна помощь с примерами",
        "Имя": "Анна",
        "Телефон": "+79991234567",
        "Мессенджер для связи": "💚 WhatsApp",
        "Удобное время": "🌅 Утро (09:00–12:00)",
    }


def test_services_branch_uses_services_variant_of_the_hub(env):
    sim = env.sim()
    open_menu(sim)
    enter_branch(sim, "services")
    walk(sim, "services", stop="sections")
    assert label("sections.cat.services") in sim.buttons
    assert label("sections.cat.catalog") not in sim.buttons
    sim.click(label("sections.cat.services"))
    assert label("sec_catalog.opt.goods") not in sim.buttons
    assert label("sec_services.opt.services") in sim.buttons


def test_other_branch_asks_description_first_and_keeps_it(env):
    sim = env.sim()
    open_menu(sim)
    sim.click(label("menu.opt.new"))
    sim.click(label("ns_what.opt.other"))
    assert_question(sim, "ns_other_text")
    sim.send("Сайт-визитка для фотостудии")
    assert_question(sim, "structure")
    walk(sim, "other", start="structure")
    assert "Сайт-визитка для фотостудии" in sim.current_text


def test_branch_questions_follow_pdf_order_for_both(env):
    sim = env.sim()
    open_menu(sim)
    enter_branch(sim, "both")
    seen = []
    for node_id in scenario.BRANCHES["both"]:
        assert_question(sim, node_id, "both")
        seen.append(node_id)
        answer_default(sim, node_id, "both")
    assert seen[0] == "priority" and seen[-2:] == ["flows", "example"]
    assert_question(sim, scenario.FINAL)
    assert label("fin_name.intro") in "\n".join(sim.texts[-2:])
