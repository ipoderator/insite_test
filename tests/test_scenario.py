import pytest

from leadbot import scenario
from leadbot.scenario import BRANCHES, HUB_VARIANTS, NODES, next_node
from leadbot.texts import TEXTS

# Branch order from spec.md, "Сценарий — граф диалога".
EXPECTED_BRANCHES = {
    "goods": ("pay", "delivery", "stock", "crm", "features", "variations", "count",
              "product_info", "sections", "design", "example"),
    "services": ("sphere", "structure", "content", "pay", "crm", "popup", "sections", "example"),
    "both": ("priority", "sphere", "pay", "delivery", "stock", "crm", "features", "variations",
             "count", "product_info", "sections", "design", "flows", "example"),
    "other": ("ns_other_text", "structure", "sections", "design", "example"),
    "promo": ("pr_what", "pr_types", "pr_current", "pr_helpers", "pr_support", "pr_geo",
              "pr_goals", "pr_link"),
    "redesign": ("rd_platform", "rd_what", "rd_link", "rd_rebuild", "rd_promoted", "rd_access"),
    "final": ("fin_name", "fin_phone", "fin_messenger", "fin_time"),
}

QUESTION_BRANCHES = ("goods", "services", "both", "other", "promo", "redesign")


def all_question_nodes():
    ids = {n for seq in BRANCHES.values() for n in seq}
    ids |= {c for cats in HUB_VARIANTS.values() for c in cats}
    ids.add("ns_what")
    return sorted(ids)


def test_branches_follow_spec_order():
    assert BRANCHES == EXPECTED_BRANCHES


def test_every_text_key_of_the_graph_exists():
    missing = []
    for node in NODES.values():
        keys = [node.question_key, node.title_key, node.hint_key, node.footer_key,
                node.done_key, node.intro_key]
        for opt in node.options:
            keys.append(opt.text_key)
            keys.extend(spec.prompt_key for spec in opt.inputs)
        missing += [(node.id, k) for k in keys if k is not None and k not in TEXTS]
    assert not missing


def test_every_transition_points_to_an_existing_node():
    for seq in BRANCHES.values():
        for node_id in seq:
            assert node_id in NODES, node_id
    for cats in HUB_VARIANTS.values():
        for cat in cats:
            assert NODES[cat].kind == "multi", cat
    for node in NODES.values():
        for opt in node.options:
            assert opt.next is None or opt.next in NODES, (node.id, opt.id, opt.next)


def test_every_question_has_a_short_title():
    for node_id in all_question_nodes():
        node = NODES[node_id]
        assert node.title_key and node.title_key in TEXTS, node_id
        assert TEXTS[node.title_key].kind == "title"


def test_ids_are_unique_and_short_enough_for_callback_data():
    # callback-data "<node_id>|<action>|<option_id>" <= 64 bytes; budget 10 bytes for the action;
    # the summary "edit" button carries a node id in the option slot
    longest_node = max(len(n.encode()) for n in NODES)
    for node in NODES.values():
        ids = [o.id for o in node.options]
        assert len(ids) == len(set(ids)), node.id
        for opt_id in ids + [longest_node * "x"]:
            data = f"{node.id}|{'a' * 10}|{opt_id}"
            assert len(data.encode()) <= 64, data
        assert "|" not in node.id and all("|" not in i for i in ids)


def walk(branch, start, answers):
    node, seen = start, []
    while node is not None and node != scenario.FINAL:
        seen.append(node)
        assert len(seen) < 50, seen
        node = next_node(branch, node, answers)
    return seen, node


@pytest.mark.parametrize("branch", QUESTION_BRANCHES)
def test_final_is_reachable_from_every_node_of_every_branch(branch):
    for node_id in BRANCHES[branch]:
        _, end = walk(branch, node_id, {})
        assert end == scenario.FINAL, (branch, node_id)
    path, end = walk(branch, BRANCHES[branch][0], {})
    assert tuple(path) == EXPECTED_BRANCHES[branch]


def test_final_block_ends_the_dialogue():
    node, path = "fin_name", []
    while node is not None and len(path) < 10:
        path.append(node)
        node = next_node("final", node, {})
    assert path == list(EXPECTED_BRANCHES["final"])
    # the final nodes are found whatever branch the client came from
    assert next_node("goods", "fin_name", {}) == "fin_phone"
    assert next_node("promo", "fin_time", {}) is None


@pytest.mark.parametrize("option,branch", [
    ("goods", "goods"), ("services", "services"), ("both", "both"), ("other", "other")])
def test_ns_what_starts_the_chosen_branch(option, branch):
    node = NODES["ns_what"]
    assert node.branching
    assert [o.id for o in node.options] == ["goods", "services", "both", "other"]
    # before a branch is chosen the answer of ns_what lives under the pseudo-branch "new"
    assert scenario.NEW_SITE == "new"
    assert next_node("new", "ns_what", {"new/ns_what": option}) == BRANCHES[branch][0]
    assert next_node("new", "ns_what", {"new/ns_what": {"option": option}}) == BRANCHES[branch][0]
    assert scenario.option_branch("ns_what", option) == branch
    opt = next(o for o in node.options if o.id == option)
    assert opt.next == BRANCHES[branch][0]


def test_menu_entries_lead_to_their_blocks():
    menu = NODES["menu"]
    assert [(o.id, o.next) for o in menu.options] == [
        ("new", "ns_what"), ("redesign", "rd_platform"), ("promo", "pr_what"),
        ("examples", "ex_sphere"), ("contacts", "contacts")]
    assert scenario.option_branch("menu", "redesign") == "redesign"
    assert scenario.option_branch("menu", "promo") == "promo"
    assert scenario.option_branch("menu", "new") is None
    assert scenario.option_branch("menu", "examples") is None


@pytest.mark.parametrize("answer_shape", [
    lambda ids: ids,
    lambda ids: {"selected": ids, "inputs": {}},
])
@pytest.mark.parametrize("picked", ["paid", "seo", "social", "unsure", "past"])
def test_pr_current_first_five_lead_to_helpers(picked, answer_shape):
    answers = {"promo/pr_current": answer_shape([picked])}
    assert next_node("promo", "pr_current", answers) == "pr_helpers"


@pytest.mark.parametrize("answer_shape", [
    lambda ids: ids,
    lambda ids: {"selected": ids, "inputs": {}},
])
@pytest.mark.parametrize("picked", ["never", "hard"])
def test_pr_current_never_or_hard_jumps_to_final(picked, answer_shape):
    answers = {"promo/pr_current": answer_shape([picked])}
    assert next_node("promo", "pr_current", answers) == scenario.FINAL
    path, end = walk("promo", "pr_what", answers)
    assert path == ["pr_what", "pr_types", "pr_current"] and end == scenario.FINAL


def test_single_answer_as_plain_option_id_is_understood():
    assert scenario.selected_option_ids("yes") == ["yes"]
    assert scenario.selected_option_ids(["a", "b"]) == ["a", "b"]
    assert scenario.selected_option_ids({"option": "yes"}) == ["yes"]
    assert scenario.selected_option_ids({"selected": ["a"]}) == ["a"]
    assert scenario.selected_option_ids(None) == []


def test_question_path_rebuilds_after_branch_answers():
    assert scenario.question_path("goods", {}) == ("ns_what",) + EXPECTED_BRANCHES["goods"] + \
        EXPECTED_BRANCHES["final"]
    assert scenario.question_path("redesign", {}) == EXPECTED_BRANCHES["redesign"] + \
        EXPECTED_BRANCHES["final"]
    short = scenario.question_path("promo", {"promo/pr_current": ["never"]})
    assert short == ("pr_what", "pr_types", "pr_current") + EXPECTED_BRANCHES["final"]


# -- hub of sections ----------------------------------------------------------


def option_texts(node_id):
    return [TEXTS[o.text_key].default for o in NODES[node_id].options]


def plain_options(node_id):
    from tests.test_texts import _plain
    return [_plain(t) for t in option_texts(node_id)]


def test_hub_variants_goods_and_services():
    assert NODES["sections"].kind == "hub"
    assert set(HUB_VARIANTS) == {"goods", "services"}
    goods, services = HUB_VARIANTS["goods"], HUB_VARIANTS["services"]
    from tests.test_texts import _plain
    hub_buttons = {o.next: _plain(TEXTS[o.text_key].default) for o in NODES["sections"].options
                   if o.next}
    assert [hub_buttons[c] for c in goods] == [
        "Основные страницы", "Услуги и товары", "Доверие и отзывы", "Портфолио и проекты",
        "Правовая информация"]
    assert [hub_buttons[c] for c in services] == [
        "Основные страницы", "Услуги", "Доверие и отзывы", "Портфолио и проекты",
        "Правовая информация"]
    catalog_goods = plain_options(goods[1])
    catalog_services = plain_options(services[1])
    assert "Товары (если есть каталог)" in catalog_goods
    assert "Товары (если есть каталог)" not in catalog_services
    assert catalog_services == [t for t in catalog_goods if t != "Товары (если есть каталог)"]


@pytest.mark.parametrize("branch,variant", [
    ("goods", "goods"), ("both", "goods"), ("other", "goods"), ("services", "services")])
def test_hub_variant_per_branch(branch, variant):
    assert scenario.hub_categories(branch) == HUB_VARIANTS[variant]
    shown = [o.next for o in scenario.hub_options(branch) if o.next]
    assert tuple(shown) == HUB_VARIANTS[variant]
    other = [o for o in scenario.hub_options(branch) if o.input]
    assert len(other) == 1 and other[0].input.kind == "text"
    assert TEXTS[other[0].input.prompt_key].default.endswith(
        "Напишите, какие страницы ещё нужны:")


def test_hub_categories_have_add_option_except_legal():
    from tests.test_texts import _plain
    for cat in set(HUB_VARIANTS["goods"]) | set(HUB_VARIANTS["services"]):
        node = NODES[cat]
        adds = [o for o in node.options if o.input is not None]
        title = _plain(TEXTS[node.question_key].default)
        if title == "Правовая информация:":
            assert adds == []
        else:
            assert len(adds) == 1, cat
            assert _plain(TEXTS[adds[0].text_key].default) == "Добавить свой вариант"
            assert adds[0].input.kind == "text"
        assert node.done_key == "common.back_to_sections"
    assert TEXTS[scenario.HUB_DONE_MESSAGE_KEY].default.endswith(
        "Спасибо! Переходим к следующему вопросу.")


# -- inputs ("→ поле"), exclusive and final_jump ------------------------------

# Every option that opens a field in the PDF, with the field type.
EXPECTED_INPUTS = {
    ("features", "other"): ("text",),
    ("variations", "other"): ("text",),
    ("example", "yes"): ("url", "text"),
    ("example", "tz"): ("file",),
    ("example", "other"): ("text",),
    ("sphere", "other"): ("text",),
    ("content", "other"): ("text",),
    ("popup", "texts"): ("text",),
    ("sections", "other"): ("text",),
    ("pr_what", "site"): ("url",),
    ("pr_what", "social"): ("urls",),
    ("pr_what", "gis"): ("url",),
    ("pr_what", "ybiz"): ("url",),
    ("pr_what", "ymaps"): ("url",),
    ("pr_what", "yserv"): ("url",),
    ("pr_what", "other"): ("text",),
    ("pr_types", "other"): ("text",),
    ("pr_current", "paid"): ("text",),
    ("pr_current", "unsure"): ("text",),
    ("pr_current", "past"): ("text",),
    ("pr_helpers", "other"): ("text",),
    ("pr_support", "other"): ("text",),
    ("rd_platform", "other"): ("text",),
    ("rd_what", "other"): ("text",),
    ("rd_promoted", "other"): ("text",),
    ("rd_access", "other"): ("text",),
}


def test_every_pdf_field_has_an_input_spec_of_the_right_kind():
    found = {}
    for node_id in all_question_nodes():
        if node_id.startswith("sec_"):
            continue  # "Добавить свой вариант" — checked with the hub
        for opt in NODES[node_id].options:
            if opt.inputs:
                found[(node_id, opt.id)] = tuple(s.kind for s in opt.inputs)
    assert found == EXPECTED_INPUTS


def test_example_comment_is_optional_and_link_is_not():
    yes = next(o for o in NODES["example"].options if o.id == "yes")
    assert [s.optional for s in yes.inputs] == [False, True]


def test_exclusive_and_final_jump_options():
    exclusive, jumps = set(), set()
    for node in NODES.values():
        for opt in node.options:
            if opt.exclusive:
                exclusive.add((node.id, opt.id))
            if opt.final_jump:
                jumps.add((node.id, opt.id))
    assert exclusive == {
        ("variations", "no"), ("pr_what", "none"), ("pr_what", "unknown"),
        ("pr_types", "unknown"), ("pr_current", "never"), ("pr_current", "hard"),
        ("pr_helpers", "no"), ("rd_promoted", "no")}
    assert jumps == {("pr_current", "never"), ("pr_current", "hard")}
    from tests.test_texts import _plain
    assert {_plain(TEXTS[o.text_key].default) for o in NODES["pr_current"].options
            if o.final_jump} == {"Никогда не продвигали, нужна помощь",
                                 "Затрудняюсь ответить, нужна консультация"}


def test_node_kinds_follow_spec():
    kinds = {
        "ns_what": "single", "ns_other_text": "text", "pay": "single", "delivery": "single",
        "stock": "single", "crm": "single", "features": "multi", "variations": "multi",
        "count": "single", "product_info": "single", "sections": "hub", "design": "single",
        "example": "single", "sphere": "single", "structure": "single", "content": "multi",
        "popup": "single", "priority": "single", "flows": "single",
        "pr_what": "multi", "pr_types": "multi", "pr_current": "multi", "pr_helpers": "multi",
        "pr_support": "single", "pr_geo": "single", "pr_goals": "single", "pr_link": "url",
        "rd_platform": "single", "rd_what": "multi", "rd_link": "url", "rd_rebuild": "single",
        "rd_promoted": "multi", "rd_access": "single",
        "fin_name": "name", "fin_phone": "phone", "fin_messenger": "multi", "fin_time": "single",
        "privacy": "info", "menu": "single", "ex_sphere": "single", "ex_other": "text",
        "order": "single", "contacts": "info", "summary": "info", "thanks": "info",
    }
    assert {k: NODES[k].kind for k in kinds} == kinds
    assert NODES["pr_current"].branching
    assert NODES["fin_messenger"].done_key == "fin_messenger.done"
    assert NODES["pr_what"].intro_key == "pr_what.intro"
    assert NODES["fin_name"].intro_key == "fin_name.intro"
    assert NODES["rd_promoted"].footer_key == "rd_promoted.footer"


def test_option_counts_match_pdf():
    counts = {
        "design": 6, "sphere": 8, "structure": 4, "content": 6, "popup": 4, "flows": 3,
        "pr_what": 9, "pr_types": 9, "pr_current": 7, "pr_helpers": 4, "pr_support": 4,
        "pr_geo": 4, "pr_goals": 5, "rd_platform": 7, "rd_what": 8, "rd_rebuild": 3,
        "rd_promoted": 5, "rd_access": 6, "features": 10, "variations": 7, "count": 4,
        "product_info": 4, "priority": 4, "example": 4, "fin_messenger": 3, "fin_time": 4,
        "rd_link": 2, "menu": 5, "order": 4, "thanks": 3, "contacts": 4,
    }
    assert {k: len(NODES[k].options) for k in counts} == counts


def test_screens_outside_branches_link_back_correctly():
    def targets(node_id):
        return {o.id: o.next for o in NODES[node_id].options}
    assert targets("privacy") == {"start": "menu"}
    assert targets("order") == {"new": "ns_what", "redesign": "rd_platform", "promo": "pr_what",
                                "menu": "menu"}
    assert targets("contacts") == {"menu": "menu", "order": "ns_what", "promo": "pr_what",
                                   "redesign": "rd_platform"}
    assert targets("thanks") == {"menu": "menu", "examples": "ex_sphere", "contacts": "contacts"}
    assert targets("ex_card") == {"order": "order", "back": "ex_sphere"}
    assert targets("ex_other") == {"order": "order", "back": "ex_sphere"}
    assert targets("ex_sphere") == {"other": "ex_other"}
    assert set(targets("summary")) == {"submit", "edit"}
    assert set(targets("resume")) == {"continue", "restart"}
    assert "privacy" in scenario.NO_BACK_NODES and "menu" in scenario.NO_BACK_NODES
    for node_id in all_question_nodes():
        assert node_id not in scenario.NO_BACK_NODES


def test_lead_kind_per_branch():
    assert scenario.LEAD_KIND_BY_BRANCH == {
        "goods": "new", "services": "new", "both": "new", "other": "new",
        "promo": "promo", "redesign": "redesign"}
    for kind in set(scenario.LEAD_KIND_BY_BRANCH.values()) | {"examples"}:
        assert f"lead.kind.{kind}" in TEXTS
