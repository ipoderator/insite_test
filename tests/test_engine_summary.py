"""Summary before sending (R79): contents, «Исправить», path rebuild, single submit."""
import copy

import pytest

from leadbot import scenario
from tests.sim import Env, answer_default, assert_question, enter_branch, label, open_menu, walk


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


@pytest.fixture
def sim(env):
    s = env.sim()
    open_menu(s)
    return s


def edit(sim, node_id):
    sim.click(label("summary.opt.edit"))
    assert label("summary.edit_question") in sim.current_text
    sim.click(label(scenario.NODES[node_id].title_key))


def test_summary_lists_kind_every_answer_and_contacts(sim):
    enter_branch(sim, "goods")
    walk(sim, "goods")
    text = sim.current_text
    assert text.startswith(label("summary.header"))
    assert f"{label('summary.kind')}: {label('lead.kind.new')}" in text
    for node_id in ("ns_what",) + scenario.BRANCHES["goods"] + scenario.BRANCHES["final"]:
        assert label(scenario.NODES[node_id].title_key) + ":" in text, node_id
    assert "Анна" in text and "+79991234567" in text
    assert sim.buttons == [label("summary.opt.submit"), label("summary.opt.edit")]


def test_edit_list_has_every_question_of_the_path_and_back_to_summary(sim):
    enter_branch(sim, "redesign")
    walk(sim, "redesign")
    sim.click(label("summary.opt.edit"))
    expected = [label(scenario.NODES[n].title_key)
                for n in scenario.BRANCHES["redesign"] + scenario.BRANCHES["final"]]
    assert sim.buttons == expected + [label("summary.back")]
    sim.click(label("summary.back"))
    assert sim.current_text.startswith(label("summary.header"))


def test_edit_one_answer_returns_to_summary(sim, env):
    enter_branch(sim, "redesign")
    walk(sim, "redesign")
    edit(sim, "rd_platform")
    assert_question(sim, "rd_platform")
    assert [b for b in sim.buttons if b.startswith("✅")] == ["✅ " + label("rd_platform.opt.wordpress")]
    sim.click(label("rd_platform.opt.other"))
    sim.send("Самописная CMS")
    text = sim.current_text
    assert text.startswith(label("summary.header"))
    assert f"{label('rd_platform.opt.other')}: Самописная CMS" in text
    assert label("rd_platform.opt.wordpress") not in text
    sim.click(label("summary.opt.submit"))
    [lead] = env.leads
    assert any(i["answer"] == f"{label('rd_platform.opt.other')}: Самописная CMS"
               for i in lead["items"])


def test_back_from_edited_question_returns_to_edit_list(sim):
    enter_branch(sim, "redesign")
    walk(sim, "redesign")
    edit(sim, "fin_name")
    assert_question(sim, "fin_name")
    sim.click(label("common.back"))
    assert label("summary.edit_question") in sim.current_text


def test_edit_contacts_name_and_phone(sim, env):
    enter_branch(sim, "promo")
    walk(sim, "promo")
    edit(sim, "fin_name")
    sim.send("Борис")
    edit(sim, "fin_phone")
    sim.send("+7 912 000-11-22")
    text = sim.current_text
    assert "Борис" in text and "+79120001122" in text and "Анна" not in text
    sim.click(label("summary.opt.submit"))
    [lead] = env.leads
    assert lead["name"] == "Борис" and lead["phone"] == "+79120001122"
    assert sim.last_text == label("thanks.text", name="Борис")


def test_changing_ns_what_rebuilds_the_path(sim, env):
    enter_branch(sim, "goods")
    walk(sim, "goods")
    edit(sim, "ns_what")
    assert [b for b in sim.buttons if b.startswith("✅")] == ["✅ " + label("ns_what.opt.goods")]
    sim.click(label("ns_what.opt.services"))
    # new questions of the services branch, in order; contacts are already known
    for node_id in scenario.BRANCHES["services"]:
        assert_question(sim, node_id, "services")
        answer_default(sim, node_id, "services")
    text = sim.current_text
    assert text.startswith(label("summary.header"))
    assert label("sphere.title") + ":" in text
    assert label("delivery.title") + ":" not in text   # goods-only question left the path
    sim.click(label("summary.opt.submit"))
    [lead] = env.leads
    titles = [i["title"] for i in lead["items"]]
    path = ("ns_what",) + scenario.BRANCHES["services"] + scenario.BRANCHES["final"]
    assert titles == [label(scenario.NODES[n].title_key) for n in path]


def test_changing_ns_what_back_to_old_branch_reuses_its_answers(sim):
    enter_branch(sim, "goods")
    walk(sim, "goods")
    edit(sim, "ns_what")
    sim.click(label("ns_what.opt.other"))
    assert_question(sim, "ns_other_text")
    sim.send("Каталог проектов домов")
    assert_question(sim, "structure")
    answer_default(sim, "structure", "other")
    # sections, design, example are answered in the goods branch only -> asked again
    assert_question(sim, "sections", "other")
    for node_id in ("sections", "design", "example"):
        answer_default(sim, node_id, "other")
    assert sim.current_text.startswith(label("summary.header"))
    edit(sim, "ns_what")
    sim.click(label("ns_what.opt.goods"))
    assert sim.current_text.startswith(label("summary.header"))   # goods fully answered
    assert label("pay.title") + ":" in sim.current_text


def test_changing_pr_current_rebuilds_promo_path(sim, env):
    enter_branch(sim, "promo")
    walk(sim, "promo", stop="pr_current")
    sim.click(label("pr_current.opt.never"))
    sim.click(label("common.done"))
    walk(sim, "promo", start="fin_name")
    assert label("pr_support.title") + ":" not in sim.current_text
    edit(sim, "pr_current")
    sim.click(label("pr_current.opt.seo"))
    sim.click(label("common.done"))
    for node_id in ("pr_helpers", "pr_support", "pr_geo", "pr_goals", "pr_link"):
        assert_question(sim, node_id, "promo")
        answer_default(sim, node_id, "promo")
    assert sim.current_text.startswith(label("summary.header"))
    assert label("pr_support.title") + ":" in sim.current_text
    edit(sim, "pr_current")
    sim.click(label("pr_current.opt.hard"))
    sim.click(label("common.done"))
    text = sim.current_text
    assert text.startswith(label("summary.header")) and label("pr_support.title") not in text


def test_edit_hub_sections_from_summary(sim):
    enter_branch(sim, "goods")
    walk(sim, "goods")
    edit(sim, "sections")
    sim.click(label("sections.cat.trust"))
    sim.click(label("sec_trust.opt.reviews"))
    sim.click(label("common.back_to_sections"))
    sim.click(label("common.done"))
    text = sim.current_text
    assert text.startswith(label("summary.header"))
    assert "⭐ Доверие и отзывы: 💬 Отзывы" in text


def test_submit_creates_exactly_one_lead(sim, env):
    enter_branch(sim, "redesign")
    walk(sim, "redesign")
    summary_id = sim.current_message_id
    [data] = [b["callback_data"] for row in sim.tg.messages[(sim.chat_id, summary_id)]
              ["reply_markup"]["inline_keyboard"] for b in row
              if b["text"] == label("summary.opt.submit")]
    update = sim.click(label("summary.opt.submit"))
    sim.replay(update)                                   # webhook retry
    sim.press(data, message_id=summary_id)               # second tap on the old button
    assert sim.last_popup == label("common.already_sent")
    assert len(env.leads) == 1
    sim.start()
    assert sim.last_text == label("menu.text")           # answers were cleared, no resume


def test_answers_are_cleared_after_submit(sim, env):
    enter_branch(sim, "goods")
    walk(sim, "goods")
    sim.click(label("summary.opt.submit"))
    sim.click(label("thanks.opt.menu"))
    sim.click(label("menu.opt.new"))
    assert not any(b.startswith("✅") for b in sim.buttons)


def test_long_summary_is_split_into_messages(sim):
    enter_branch(sim, "promo")
    sim.click(label("pr_what.opt.social"))
    sim.click(label("common.done"))
    links = " ".join(f"https://social{i}.example.ru/" + "x" * 470 for i in range(20))
    sim.send(links)
    walk(sim, "promo", start="pr_types")
    parts = [t for t in sim.texts if t.startswith(label("summary.header"))]
    assert parts, "summary not shown"
    idx = sim.texts.index(parts[-1])
    summary_parts = sim.texts[idx:]
    assert len(summary_parts) >= 3
    assert all(len(p) <= 4096 for p in summary_parts)
    joined = "".join(summary_parts)
    assert all(f"https://social{i}.example.ru/" in joined for i in range(20))
    assert sim.buttons == [label("summary.opt.submit"), label("summary.opt.edit")]


def test_failed_thanks_message_does_not_allow_a_second_lead(sim, env):
    enter_branch(sim, "redesign")
    walk(sim, "redesign")
    summary_id = sim.current_message_id
    original = env.tg.send_message
    thanks = label("thanks.text", name="Анна")

    def failing(chat_id, text, reply_markup=None, disable_preview=True):
        if text == thanks:
            raise RuntimeError("network down")
        return original(chat_id, text, reply_markup=reply_markup, disable_preview=disable_preview)

    env.tg.send_message = failing
    sim.click(label("summary.opt.submit"), message_id=summary_id)
    env.tg.send_message = original
    sim.press("summary|submit|", message_id=summary_id)
    assert len(env.leads) == 1
    assert sim.last_popup == label("common.already_sent")


def test_new_branch_left_half_way_is_finished_before_the_summary(sim, env):
    enter_branch(sim, "goods")
    walk(sim, "goods")
    edit(sim, "ns_what")
    sim.click(label("ns_what.opt.services"))
    assert_question(sim, "sphere", "services")
    sim.click(label("common.back"))                      # back to ns_what
    sim.click(label("common.back"))                      # back to the edit list
    sim.click(label("summary.back"))                     # «К сводке» with services unanswered
    assert label("summary.unanswered") in sim.texts[-2]
    assert_question(sim, "sphere", "services")
    assert label("summary.opt.submit") not in sim.buttons
    for node_id in scenario.BRANCHES["services"]:
        assert_question(sim, node_id, "services")
        answer_default(sim, node_id, "services")
    assert sim.current_text.startswith(label("summary.header"))
    sim.click(label("summary.opt.submit"))
    [lead] = env.leads
    path = ("ns_what",) + scenario.BRANCHES["services"] + scenario.BRANCHES["final"]
    assert [i["title"] for i in lead["items"]] == [label(scenario.NODES[n].title_key)
                                                  for n in path]


def test_edit_left_without_the_required_file_is_asked_again(sim, env):
    enter_branch(sim, "goods")
    walk(sim, "goods")
    edit(sim, "example")
    sim.click(label("example.opt.tz"))
    assert label("example.tz.prompt") in sim.current_text
    sim.click(label("common.back"))                      # no file sent: back to the question
    sim.click(label("common.back"))                      # back to the edit list
    sim.click(label("summary.back"))
    assert label("summary.unanswered") in sim.texts[-2]
    assert_question(sim, "example", "goods")
    sim.click(label("example.opt.tz"))
    sim.send_document("tz.pdf")
    sim.click(label("common.done"))
    text = sim.current_text
    assert text.startswith(label("summary.header"))
    assert f"{label('example.opt.tz')}: tz.pdf" in text
    assert label("summary.files_value", count=1) in text
    sim.click(label("summary.opt.submit"))
    [lead] = env.leads
    assert [f["file_name"] for f in lead["files"]] == ["tz.pdf"]


def _promo_with_site(sim, url):
    enter_branch(sim, "promo")
    sim.click(label("pr_what.opt.site"))
    sim.click(label("common.done"))
    sim.send(url)
    walk(sim, "promo", start="pr_types", stop="pr_link")
    sim.click(label("pr_link.opt.use", url=url))
    walk(sim, "promo", start="fin_name")


def test_edited_site_link_replaces_the_link_for_analysis(sim, env):
    _promo_with_site(sim, "https://old.example.ru")
    edit(sim, "pr_what")
    sim.click(label("common.done"))
    sim.send("https://new.example.ru")
    # the answer «Использовать: old link» is outdated: the question comes again
    assert_question(sim, "pr_link", "promo")
    assert label("pr_link.opt.use", url="https://new.example.ru") in sim.buttons
    sim.click(label("pr_link.opt.use", url="https://new.example.ru"))
    text = sim.current_text
    assert text.startswith(label("summary.header")) and "old.example.ru" not in text
    sim.click(label("summary.opt.submit"))
    [lead] = env.leads
    assert {"title": label("pr_link.title"), "answer": "https://new.example.ru"} in lead["items"]
    assert not any("old.example.ru" in i["answer"] for i in lead["items"])


def test_removed_site_link_is_not_kept_for_analysis(sim, env):
    _promo_with_site(sim, "https://old.example.ru")
    edit(sim, "pr_what")
    sim.click(label("pr_what.opt.site"))                 # unmark «Сайт»
    sim.click(label("pr_what.opt.unknown"))
    sim.click(label("common.done"))
    assert_question(sim, "pr_link", "promo")
    assert not any("old.example.ru" in b for b in sim.buttons)
    sim.click(label("pr_link.opt.none"))
    sim.click(label("summary.opt.submit"))
    [lead] = env.leads
    assert not any("old.example.ru" in i["answer"] for i in lead["items"])


def test_parallel_taps_on_submit_create_one_lead(sim, env):
    """The second webhook request read the summary session before the first one saved it."""
    enter_branch(sim, "redesign")
    walk(sim, "redesign")
    summary_id = sim.current_message_id
    before = copy.deepcopy(env.storage.load_session(sim.chat_id))
    sim.press("summary|submit|", message_id=summary_id)
    assert len(env.leads) == 1
    after = env.storage.load_session(sim.chat_id)
    load = env.storage.load_session
    env.storage.load_session = lambda chat_id: copy.deepcopy(before)
    try:
        sim.press("summary|submit|", message_id=summary_id)
    finally:
        env.storage.load_session = load
    assert len(env.leads) == 1
    assert sim.last_popup == label("common.already_sent")
    assert env.storage.load_session(sim.chat_id) == after   # the winner's session is kept


def test_summary_buttons_and_titles_follow_the_panel(sim, env):
    env.storage.set_text_override("summary.opt.submit", "Отправить <сейчас> & всё")
    env.storage.set_text_override("summary.opt.edit", "Поменять ответ")
    env.storage.set_text_override("summary.back", "Обратно")
    env.storage.set_text_override("summary.edit_question", "Что поменять?")
    env.storage.set_text_override("rd_platform.title", "CMS & движок")
    enter_branch(sim, "redesign")
    walk(sim, "redesign")
    assert sim.buttons == ["Отправить <сейчас> & всё", "Поменять ответ"]
    assert "CMS & движок: " in sim.current_text          # escaped in HTML, shown as typed
    sim.click("Поменять ответ")
    assert "Что поменять?" in sim.current_text
    assert sim.buttons[0] == "CMS & движок" and sim.buttons[-1] == "Обратно"
    sim.click("Обратно")
    sim.click("Отправить <сейчас> & всё")
    assert len(env.leads) == 1
