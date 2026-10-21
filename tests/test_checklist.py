"""Checklist for the call after a lead: choice of items by branch and answers, sending."""
import re

import pytest

from leadbot import scenario
from leadbot.checklist import (CLOSING, MAX_ITEMS, NEUTRAL, RULES, checklist_items,
                               format_checklist)
from leadbot.leads import answer_key
from leadbot.texts import TEXTS, TextStore
from tests.sim import Env, answer_default, enter_branch, label, open_menu, plain, walk
from tests.test_admin_support import Panel

RULE_ITEMS = [item for item, _ in RULES]
ALL_BRANCHES = scenario.NEW_SITE_BRANCHES + ("promo", "redesign")


def answer(node_id, *option_ids):
    if scenario.NODES[node_id].kind == "multi":
        return {"selected": list(option_ids), "fields": {}}
    return {"option": option_ids[0], "fields": {}}


def session(branch, **answers):
    """Session with the given answers; ``node=(option, …)`` or ``node=None`` (no answer)."""
    stored = {answer_key(branch, node): answer(node, *opts)
              for node, opts in answers.items() if opts is not None}
    return {"branch": branch, "answers": stored}


def branch_of(node_id):
    return next(b for b in ALL_BRANCHES if node_id in scenario.BRANCHES[b])


def conditions():
    return [(item, node, opt) for item, conds in RULES for node, opts in conds for opt in opts]


def other_options(item, node):
    """Options of the node that do not trigger this item through this node."""
    triggering = {o for n, opts in dict(RULES)[item] if n == node for o in opts}
    return [o.id for o in scenario.NODES[node].options if o.id not in triggering]


# -- choice of items ----------------------------------------------------------------------


def test_rules_refer_to_real_nodes_options_and_texts():
    for item, conds in RULES:
        assert f"checklist.{item}" in TEXTS, item
        for node, opts in conds:
            assert any(node in seq for seq in scenario.BRANCHES.values()), node
            for opt in opts:
                scenario.NODES[node].option(opt)  # KeyError on a typo
    used = set(RULE_ITEMS) | set(NEUTRAL) | {CLOSING, "header"}
    assert {k.split(".", 1)[1] for k, d in TEXTS.items() if d.group == "checklist"} == used


@pytest.mark.parametrize("item,node,option", conditions())
def test_each_option_of_a_rule_adds_its_item(item, node, option):
    items = checklist_items(session(branch_of(node), **{node: (option,)}))
    assert item in items
    assert not set(NEUTRAL) & set(items)
    assert items[-1] == CLOSING


@pytest.mark.parametrize("item,node", sorted({(i, n) for i, n, _ in conditions()}))
def test_other_options_of_the_node_do_not_add_the_item(item, node):
    for option in other_options(item, node):
        items = checklist_items(session(branch_of(node), **{node: (option,)}))
        assert item not in items, (node, option)


@pytest.mark.parametrize("branch,node", [(b, n) for b in scenario.NEW_SITE_BRANCHES
                                         for n in ("pay", "delivery", "stock", "crm")])
def test_payment_and_delivery_items_only_on_branches_that_ask_them(branch, node):
    items = checklist_items(session(branch, **{node: ("yes",)}))
    assert (node in items) == (node in scenario.BRANCHES[branch])


def test_payment_only_when_chosen_delivery_only_when_chosen():
    both = checklist_items(session("goods", pay=("yes",), delivery=("yes",)))
    assert "pay" in both and "delivery" in both
    pay_only = checklist_items(session("goods", pay=("yes",), delivery=("no",)))
    assert "pay" in pay_only and "delivery" not in pay_only
    delivery_only = checklist_items(session("goods", pay=("no",), delivery=("yes",)))
    assert "pay" not in delivery_only and "delivery" in delivery_only
    assert checklist_items(session("goods", pay=("no",), delivery=("no",))) == \
        list(NEUTRAL) + [CLOSING]


def test_branch_specific_items():
    assert checklist_items(session("services", pay=("yes",), content=("partial",),
                                   example=("no",))) == ["pay", "content", "examples", CLOSING]
    assert checklist_items(session("other", design=("has_logo",))) == ["logo", CLOSING]
    assert checklist_items(session("promo", pr_what=("site", "gis"), pr_current=("past",),
                                   pr_geo=("regions",))) == \
        ["ad_history", "cards", "geo", "analytics", CLOSING]
    assert checklist_items(session("redesign", rd_what=("bugs", "visual", "migrate"),
                                   rd_link=("broken",), rd_access=("lost",),
                                   rd_promoted=("seo",))) == \
        ["access_lost", "site_broken", "bugs", "migrate", "analytics", "examples", CLOSING]


def test_answers_off_the_current_path_do_not_count():
    # «Никогда не продвигали» ends the branch: the later answers stay in the session unused
    s = session("promo", pr_current=("never",), pr_geo=("city",))
    assert checklist_items(s) == list(NEUTRAL) + [CLOSING]
    # answers of the goods branch left after switching to services in «Исправить»
    s = session("services", sphere=("auto",))
    s["answers"].update(session("goods", delivery=("yes",), stock=("yes",))["answers"])
    assert checklist_items(s) == list(NEUTRAL) + [CLOSING]


@pytest.mark.parametrize("data", [
    {},
    {"branch": None, "answers": {}},
    {"branch": "goods"},
    {"branch": "goods", "answers": None},
    {"branch": "nonexistent", "answers": {}},
    session("goods", pay=None, delivery=None),
    {"branch": "goods", "answers": {"goods/pay": {}, "goods/delivery": {"selected": None},
                                    "goods/stock": {"option": "maybe"}, "goods/crm": {}}},
    {"branch": "redesign", "answers": {"redesign/rd_link": {"text": "https://example.ru"}}},
])
def test_missing_or_unusable_answers_give_the_neutral_list(data):
    assert checklist_items(data) == list(NEUTRAL) + [CLOSING]


def test_checklist_is_short():
    s = session("both", pay=("yes",), delivery=("yes",), stock=("yes",), crm=("yes",),
                features=("quiz",), product_info=("file",), design=("has_logo",),
                example=("no",))
    items = checklist_items(s)
    assert items == RULE_ITEMS[:4] + ["products_file", "logo", CLOSING]
    assert len(items) == MAX_ITEMS + 1


def test_default_texts_promise_no_deadlines_prices_or_results():
    banned = re.compile(r"₽|руб|\bдн(я|ей)\b|недел|месяц|гарант|обеща|срок|результат|"
                        r"бесплатн|скидк|\d+\s*%", re.IGNORECASE)
    for key, text in TEXTS.items():
        if text.group == "checklist":
            assert not banned.search(text.default), (key, text.default)
            assert text.kind == "message", key


# -- message ------------------------------------------------------------------------------


def test_format_escapes_panel_texts_and_splits_long_lists(tmp_path):
    env = Env(tmp_path)
    env.storage.set_text_override("checklist.pay", "Оплата <карта> & счёт")
    parts = format_checklist(["pay", CLOSING], TextStore(env.storage))
    assert parts == ["<b>" + label("checklist.header") + "</b>\n"
                     "• Оплата &lt;карта&gt; &amp; счёт\n• " + label("checklist.questions")]
    for item in RULE_ITEMS:
        env.storage.set_text_override(f"checklist.{item}", f"{item} " + "х" * 3400)
    parts = format_checklist(RULE_ITEMS, TextStore(env.storage))
    assert len(parts) > 1 and all(len(p) <= 4096 for p in parts)
    assert all(f"{item} х" in "".join(parts) for item in RULE_ITEMS)


# -- in the chat --------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def submit(sim):
    mark = sim.mark()
    sim.click(label("summary.opt.submit"))
    return [plain(t) for t in sim.texts_since(mark)]


def checklist_text(texts):
    return [t for t in texts if t.startswith(label("checklist.header"))]


THANKS_BUTTONS = [label("thanks.opt.menu"), label("thanks.opt.examples"),
                  label("thanks.opt.contacts")]


@pytest.mark.parametrize("branch", ALL_BRANCHES)
def test_checklist_comes_before_the_unchanged_final_message(env, branch):
    env.bind_owner()
    sim = env.sim()
    open_menu(sim)
    enter_branch(sim, branch)
    walk(sim, branch)
    sent = submit(sim)
    assert len(env.leads) == 1
    [checklist] = checklist_text(sent)
    assert sent.index(checklist) == len(sent) - 2
    assert checklist.endswith("• " + label("checklist.questions"))
    assert sim.last_text == label("thanks.text", name="Анна")
    assert sim.buttons == THANKS_BUTTONS
    assert not checklist_text(env.owner_texts())  # only the client gets it
    sim.click(label("thanks.opt.menu"))
    assert sim.current_text == label("menu.text")


def test_chosen_answers_in_the_chat_shape_the_checklist(env):
    sim = env.sim()
    open_menu(sim)
    enter_branch(sim, "goods")
    walk(sim, "goods", stop="pay")
    sim.click(label("pay.opt.no"))
    sim.click(label("delivery.opt.yes"))
    walk(sim, "goods", start="stock")
    [checklist] = checklist_text(submit(sim))
    assert label("checklist.delivery") in checklist
    assert label("checklist.pay") not in checklist


def test_promotion_ended_early_gets_the_neutral_list(env):
    sim = env.sim()
    open_menu(sim)
    enter_branch(sim, "promo")
    sim.click(label("pr_what.opt.none"))
    sim.click(label("common.done"))
    sim.click(label("pr_types.opt.unknown"))
    sim.click(label("common.done"))
    sim.click(label("pr_current.opt.never"))
    sim.click(label("common.done"))
    walk(sim, "promo", start="fin_name")
    [checklist] = checklist_text(submit(sim))
    assert checklist == "\n".join([label("checklist.header")] + [
        "• " + label(f"checklist.{i}") for i in NEUTRAL + (CLOSING,)])


def test_branch_changed_in_edit_uses_the_new_branch(env):
    sim = env.sim()
    open_menu(sim)
    enter_branch(sim, "goods")
    walk(sim, "goods")
    sim.click(label("summary.opt.edit"))
    sim.click(label("ns_what.title"))
    sim.click(label("ns_what.opt.services"))
    for node_id in scenario.BRANCHES["services"]:
        answer_default(sim, node_id, "services")
    assert sim.current_text.startswith(label("summary.header"))
    [checklist] = checklist_text(submit(sim))
    assert label("checklist.pay") in checklist            # services also ask about payment
    assert label("checklist.delivery") not in checklist  # goods-only answers are dropped
    assert label("checklist.stock") not in checklist


def test_repeated_submit_sends_neither_a_second_lead_nor_a_second_checklist(env):
    sim = env.sim()
    open_menu(sim)
    enter_branch(sim, "redesign")
    walk(sim, "redesign")
    summary_id = sim.current_message_id
    mark = sim.mark()
    update = sim.click(label("summary.opt.submit"))
    sim.replay(update)                                    # webhook retry
    sim.press("summary|submit|", message_id=summary_id)   # second tap
    assert sim.last_popup == label("common.already_sent")
    sim.send("а что дальше?")                             # text on the final screen
    assert len(env.leads) == 1
    assert len(checklist_text([plain(t) for t in sim.texts_since(mark)])) == 1


def test_failed_checklist_does_not_stop_the_final_message(env):
    sim = env.sim()
    open_menu(sim)
    enter_branch(sim, "goods")
    walk(sim, "goods")
    summary_id = sim.current_message_id
    original = env.tg.send_message

    def failing(chat_id, text, reply_markup=None, disable_preview=True):
        if plain(text).startswith(label("checklist.header")):
            raise RuntimeError("network down")
        return original(chat_id, text, reply_markup=reply_markup, disable_preview=disable_preview)

    env.tg.send_message = failing
    sim.click(label("summary.opt.submit"), message_id=summary_id)
    env.tg.send_message = original
    assert sim.last_text == label("thanks.text", name="Анна")
    assert sim.buttons == THANKS_BUTTONS
    sim.press("summary|submit|", message_id=summary_id)
    assert len(env.leads) == 1


def test_long_checklist_from_the_panel_is_split_and_thanks_stays_last(env):
    for item in RULE_ITEMS:
        env.storage.set_text_override(f"checklist.{item}", f"{item} " + "х" * 3400)
    sim = env.sim()
    open_menu(sim)
    enter_branch(sim, "goods")
    walk(sim, "goods")
    sent = submit(sim)
    assert len(sent) >= 3
    assert sim.last_text == label("thanks.text", name="Анна")
    assert all(f"{item} х" in "".join(sent) for item in ("pay", "delivery", "features"))


def test_checklist_wording_is_edited_in_the_panel(tmp_path):
    panel = Panel(tmp_path)
    panel.login()
    page = panel.page("/admin/texts?group=checklist")
    assert 'id="t-checklist.pay"' in page and 'id="t-checklist.questions"' in page
    resp = panel.post("/admin/texts/checklist.pay",
                      {"value": "💳 Узнайте у бухгалтера реквизиты для приёма оплаты.",
                       "group": "checklist"})
    assert resp.status_code == 302
    bot = panel.bot()
    open_menu(bot)
    enter_branch(bot, "goods")
    walk(bot, "goods")
    [checklist] = checklist_text(submit(bot))
    assert "• 💳 Узнайте у бухгалтера реквизиты для приёма оплаты." in checklist
    assert label("checklist.pay") not in checklist
