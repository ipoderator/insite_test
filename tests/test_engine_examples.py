"""Examples of works (from the database), ordering from them, contacts screen."""
import pytest

from leadbot.telegram import TelegramError
from tests.sim import Env, assert_question, label, open_menu, walk


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


@pytest.fixture
def sim(env):
    s = env.sim()
    open_menu(s)
    s.click(label("menu.opt.examples"))
    return s


def test_spheres_come_from_database_in_its_order(sim, env):
    cats = env.storage.list_categories(active_only=True)
    assert sim.buttons == [c["button"] for c in cats] + [label("ex_sphere.opt.other"),
                                                         label("common.back")]
    sim.click(label("common.back"))
    assert sim.last_text == label("menu.text")


def test_panel_changes_to_spheres_apply_immediately(sim, env):
    sport = env.storage.get_category("sport")
    env.storage.save_category({"id": sport["id"], "active": False})
    env.storage.save_category({"slug": "edu", "button": "🎓 Образование", "topic": "тематике образования",
                               "sort": 1})
    sim.click(label("common.back"))
    sim.click(label("menu.opt.examples"))
    assert sim.buttons[0] == "🎓 Образование"
    assert sport["button"] not in sim.buttons


def test_card_lists_examples_with_demo_mark_and_links(sim, env):
    auto = env.storage.get_category("auto")
    items = env.storage.list_items(auto["id"])
    env.storage.save_item({"id": items[0]["id"], "url": "https://auto.example.ru/?a=1&b=2",
                           "is_demo": False, "note": "сеть из 3 <сервисов>"})
    sim.click(auto["button"])
    text = sim.current_text
    assert text.startswith(label("ex_card.header", topic="автомобильной тематике"))
    assert "Сайт №1 – Автосервис — сеть из 3 <сервисов>" in text
    assert f"Сайт №2 – Шиномонтаж и ТО {label('ex_card.demo')}" in text
    assert label("ex_card.note") in text
    assert '<a href="https://auto.example.ru/?a=1&amp;b=2">Сайт №1 – Автосервис</a>' in sim.last_html
    assert sim.buttons == [label("ex_card.opt.order"), label("common.back_to_spheres")]
    sim.click(label("common.back_to_spheres"))
    assert_question(sim, "ex_sphere")


def test_long_card_is_split_into_messages_with_buttons_under_the_last(sim, env):
    real_send = env.tg.send_message

    def send_like_telegram(chat_id, text, **kwargs):
        if len(text) > 4096:  # what the Bot API answers to a longer text
            raise TelegramError("Bad Request: message is too long", 400)
        return real_send(chat_id, text, **kwargs)

    env.tg.send_message = send_like_telegram
    cid = env.storage.save_category({"slug": "big", "button": "Много примеров",
                                     "topic": "большой тематике", "sort": 0})
    titles = [f"Пример {n:02d} " + "а" * 180 for n in range(1, 31)]  # panel limit: 200
    for n, title in enumerate(titles):
        env.storage.save_item({"category_id": cid, "title": title, "sort": n,
                               "url": f"https://example.ru/work/{n}?a=1&b=2",
                               "note": "описание & детали " * 27, "is_demo": False})
    sim.click(label("common.back"))
    sim.click(label("menu.opt.examples"))
    mark = sim.mark()
    sim.click("Много примеров")

    sent = [kw for name, kw in env.tg.calls[mark:] if name == "send_message"]
    texts = sim.texts_since(mark)
    assert label("common.error") not in texts
    assert len(sent) > 1, "a 20 000-character card does not fit one message"
    assert all(len(kw["text"]) <= 4096 for kw in sent)
    joined = "\n".join(texts)
    assert texts[0].startswith(label("ex_card.header", topic="большой тематике"))
    assert [t for t in titles if t in joined] == titles, "every example, in order, once"
    assert all(joined.count(t) == 1 for t in titles)
    assert texts[-1].endswith(label("ex_card.note"))
    assert all(not kw["reply_markup"] for kw in sent[:-1])
    assert sim.buttons == [label("ex_card.opt.order"), label("common.back_to_spheres")]
    sim.click(label("ex_card.opt.order"))
    assert label("order.question") in sim.current_text


def test_empty_category_says_so(sim, env):
    env.storage.save_category({"slug": "empty", "button": "Пусто", "topic": "пустой тематике",
                               "sort": 0})
    sim.click(label("common.back"))
    sim.click(label("menu.opt.examples"))
    sim.click("Пусто")
    assert label("ex_card.empty") in sim.current_text


@pytest.mark.parametrize("option,node", [("new", "ns_what"), ("redesign", "rd_platform"),
                                         ("promo", "pr_what")])
def test_order_from_examples_leads_to_branch(sim, option, node):
    sim.click("🔨 Ремонтные работы")
    sim.click(label("ex_card.opt.order"))
    assert label("order.question") in sim.current_text
    assert sim.buttons == [label("order.opt.new"), label("order.opt.redesign"),
                           label("order.opt.promo"), label("common.to_menu"), label("common.back")]
    sim.click(label(f"order.opt.{option}"))
    assert_question(sim, node)


def test_sphere_of_viewed_examples_goes_into_the_lead(sim, env):
    env.bind_owner()
    sim.click("🔨 Ремонтные работы")
    sim.click(label("ex_card.opt.order"))
    sim.click(label("order.opt.redesign"))
    walk(sim, "redesign")
    assert f"{label('summary.viewed_examples')}: 🔨 Ремонтные работы" in sim.current_text
    sim.click(label("summary.opt.submit"))
    [lead] = env.leads
    assert {"title": label("summary.viewed_examples"), "answer": "🔨 Ремонтные работы"} in lead["items"]
    assert "🔨 Ремонтные работы" in "\n".join(env.owner_texts())


def test_order_menu_button_returns_to_menu(sim):
    sim.click("🔨 Ремонтные работы")
    sim.click(label("ex_card.opt.order"))
    sim.click(label("common.to_menu"))
    assert sim.last_text == label("menu.text")


def test_other_request_becomes_examples_lead_for_owner(sim, env):
    env.bind_owner()
    sim.click(label("ex_sphere.opt.other"))
    assert_question(sim, "ex_other")
    assert sim.buttons == [label("ex_card.opt.order"), label("common.back_to_spheres")]
    sim.send("x")
    assert label("err.text_short") in sim.texts[-2]
    sim.send("Сайты для ветклиник")
    assert sim.current_text == label("ex_other.done")
    assert sim.buttons == [label("ex_card.opt.order"), label("common.back_to_spheres")]
    [lead] = env.leads
    assert lead["kind"] == "examples" and lead["delivered"]
    assert lead["items"] == [{"title": label("lead.request"), "answer": "Сайты для ветклиник"}]
    assert lead["username"] == "anna_client"
    owner = "\n".join(env.owner_texts())
    assert label("lead.kind.examples") in owner and "Сайты для ветклиник" in owner
    assert "@anna_client" in owner
    sim.click(label("ex_card.opt.order"))
    assert label("order.question") in sim.current_text


def test_contacts_from_settings_with_four_buttons(env):
    env.storage.set_setting("contact_phone", "+7 900 <000>")
    s = env.sim()
    open_menu(s)
    s.click(label("menu.opt.contacts"))
    text = s.current_text
    assert label("contacts.text") in text
    assert label("contacts.telegram", value="@site_it") in text
    assert label("contacts.phone", value="+7 900 <000>") in text
    assert label("contacts.email", value="site-it@mail.ru") in text
    assert s.buttons == [label("common.to_menu"), label("contacts.opt.order"),
                         label("contacts.opt.promo"), label("contacts.opt.redesign")]
    s.click(label("contacts.opt.promo"))
    assert_question(s, "pr_what")
    s.click(label("common.back"))
    assert s.last_text == label("menu.text")


@pytest.mark.parametrize("option,node", [("order", "ns_what"), ("redesign", "rd_platform")])
def test_contacts_buttons_start_branches(env, option, node):
    s = env.sim()
    open_menu(s)
    s.click(label("menu.opt.contacts"))
    s.click(label(f"contacts.opt.{option}"))
    assert_question(s, node)
