"""Dialogue mechanics: multi choice, hub, back, validation, stale buttons, /start, restarts."""
import pytest

from leadbot import scenario
from tests.sim import (ChatSim, Env, answer_default, assert_question, enter_branch, label,
                       open_menu, walk)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


@pytest.fixture
def sim(env):
    s = env.sim()
    open_menu(s)
    return s


def marked(sim):
    return [b[2:] for b in sim.buttons if b.startswith("✅ ") and b != label("common.done")]


# -- entry ---------------------------------------------------------------------------------


def test_first_start_shows_privacy_with_policy_link_then_menu(env):
    sim = env.sim()
    sim.start()
    assert "Перед началом" in sim.last_text
    assert "Мы не продаём и не передаём их для рекламы" in sim.last_text
    assert '<a href="https://bot.example.org/privacy">Политика конфиденциальности</a>' in sim.last_html
    assert sim.buttons == [label("privacy.start")]
    sim.click(label("privacy.start"))
    assert sim.last_text == label("menu.text")
    assert sim.buttons == [label(f"menu.opt.{o}")
                           for o in ("new", "redesign", "promo", "examples", "contacts")]
    sim.start()
    assert sim.last_text == label("menu.text")  # privacy is shown only once


def test_privacy_link_comes_from_settings(env):
    env.storage.set_setting("privacy_url", "https://studio.example.ru/policy?a=1&b=2")
    sim = env.sim()
    sim.start()
    assert 'href="https://studio.example.ru/policy?a=1&amp;b=2"' in sim.last_html


def test_any_text_before_privacy_shows_privacy(env):
    sim = env.sim()
    sim.send("привет")
    assert "Перед началом" in sim.last_text


@pytest.mark.parametrize("option,node", [("new", "ns_what"), ("redesign", "rd_platform"),
                                         ("promo", "pr_what"), ("examples", "ex_sphere")])
def test_menu_buttons_lead_to_their_blocks(sim, option, node):
    sim.click(label(f"menu.opt.{option}"))
    assert_question(sim, node)


# -- single / multi ------------------------------------------------------------------------


def test_single_answer_is_written_under_the_question(sim):
    sim.click(label("menu.opt.new"))
    question_id = sim.current_message_id
    sim.click(label("ns_what.opt.goods"))
    text = sim.message_text(question_id)
    assert text.endswith(label("common.your_answer") + " " + label("ns_what.opt.goods"))
    assert sim.keyboard_of(question_id) == []
    assert sim.current_message_id != question_id


def test_multi_toggles_edit_the_same_message_and_exclusive_clears_others(sim, env):
    enter_branch(sim, "goods")
    walk(sim, "goods", stop="variations")
    mid = sim.current_message_id
    sends = len(env.tg.calls_of("send_message"))
    sim.click(label("variations.opt.color"))
    sim.click(label("variations.opt.size"))
    assert sim.current_message_id == mid
    assert len(env.tg.calls_of("send_message")) == sends
    assert marked(sim) == [label("variations.opt.color"), label("variations.opt.size")]
    sim.click(label("variations.opt.no"))
    assert marked(sim) == [label("variations.opt.no")]
    sim.click(label("variations.opt.weight"))
    assert marked(sim) == [label("variations.opt.weight")]
    sim.click(label("variations.opt.weight"))
    assert marked(sim) == []
    sim.click(label("common.done"))
    assert sim.last_popup == label("err.empty_multi")
    assert sim.current_message_id == mid


def test_multi_fields_are_asked_one_by_one_after_done(sim):
    enter_branch(sim, "promo")
    for opt in ("yserv", "site", "other"):
        sim.click(label(f"pr_what.opt.{opt}"))
    sim.click(label("common.done"))
    assert label("pr_what.site.prompt") in sim.last_text   # option order, not click order
    sim.send("site.ru")
    assert label("pr_what.yserv.prompt") in sim.last_text
    sim.send("https://uslugi.yandex.ru/profile/x")
    assert label("pr_what.other.prompt") in sim.last_text
    sim.send("Группа в Одноклассниках")
    assert_question(sim, "pr_types")


def test_hub_categories_marks_counter_other_and_done_phrase(sim):
    enter_branch(sim, "goods")
    walk(sim, "goods", stop="sections")
    assert label("sections.hint") in sim.current_text
    sim.click(label("common.done"))
    assert sim.last_popup == label("err.empty_multi")
    sim.click(label("sections.cat.main"))
    assert_question(sim, "sec_main")
    assert label("common.back") not in sim.buttons   # own "Назад к разделам" only
    sim.click(label("sec_main.opt.about"))
    sim.click(label("sec_main.opt.faq"))
    sim.click(label("common.back_to_sections"))
    assert label("sections.cat_selected", label=label("sections.cat.main"), count=2) in sim.buttons
    sim.click(label("sections.cat.main"))
    assert marked(sim) == [label("sec_main.opt.about"), label("sec_main.opt.faq")]
    sim.click(label("common.back_to_sections"))
    sim.click(label("sections.cat.legal"))
    assert label("sections.add") not in sim.buttons
    sim.click(label("sec_legal.opt.offer"))
    sim.click(label("common.back_to_sections"))
    sim.click(label("sections.opt.other"))
    assert label("sections.other.prompt") in sim.last_text
    sim.send("Блог и калькулятор")
    assert_question(sim, "sections", "goods")
    sim.click(label("common.done"))
    assert label("sections.done_message") in sim.texts[-2]
    assert_question(sim, "design")
    walk(sim, "goods", start="design")
    summary = sim.current_text
    assert "📄 Основные страницы: 🏢 О компании / Обо мне, ❓ FAQ — Часто задаваемые вопросы" in summary
    assert "⚖️ Правовая информация: 📝 Договор-оферта" in summary
    assert "➕ Иное: Блог и калькулятор" in summary


# -- back and saved answers ----------------------------------------------------------------


def test_back_returns_to_previous_question_with_marks(sim):
    enter_branch(sim, "goods")
    walk(sim, "goods", stop="features")
    sim.click(label("features.opt.search"))
    sim.click(label("features.opt.quiz"))
    sim.click(label("common.done"))
    assert_question(sim, "variations")
    sim.click(label("common.back"))
    assert_question(sim, "features")
    assert marked(sim) == [label("features.opt.search"), label("features.opt.quiz")]
    sim.click(label("common.back"))
    assert_question(sim, "crm")
    assert marked(sim) == ["Да"]


def test_back_from_every_question_of_a_branch(sim):
    enter_branch(sim, "both")
    previous = "ns_what"
    for node_id in scenario.BRANCHES["both"]:
        assert_question(sim, node_id, "both")
        sim.click(label("common.back"))
        assert_question(sim, previous, "both")
        if previous == "ns_what":
            sim.click(label("ns_what.opt.both"))
        elif scenario.NODES[previous].kind in ("multi", "hub"):
            sim.click(label(scenario.NODES[previous].done_key or "common.done"))  # marks kept
        else:
            answer_default(sim, previous, "both")
        assert_question(sim, node_id, "both")
        answer_default(sim, node_id, "both")
        previous = node_id


def test_back_from_first_questions_leads_to_menu_or_ns_what(sim):
    sim.click(label("menu.opt.new"))
    sim.click(label("common.back"))
    assert sim.last_text == label("menu.text")
    sim.click(label("menu.opt.promo"))
    sim.click(label("common.back"))
    assert sim.last_text == label("menu.text")
    enter_branch(sim, "services")
    sim.click(label("common.back"))
    assert_question(sim, "ns_what")
    assert marked(sim) == [label("ns_what.opt.services")]


def test_back_from_field_prompt_returns_to_its_question(sim):
    enter_branch(sim, "services")
    sim.click(label("sphere.opt.other"))
    assert label("sphere.other.prompt") in sim.last_text
    sim.click(label("common.back"))
    assert_question(sim, "sphere")
    assert marked(sim) == [label("sphere.opt.other")]


def test_reentering_branch_from_menu_shows_previous_answers(sim):
    enter_branch(sim, "goods")
    sim.click("Нет")   # pay
    sim.send("/menu")
    assert sim.last_text == label("menu.text")
    sim.click(label("menu.opt.new"))
    assert marked(sim) == [label("ns_what.opt.goods")]
    sim.click(label("ns_what.opt.goods"))
    assert_question(sim, "pay")
    assert marked(sim) == ["Нет"]


# -- validation ----------------------------------------------------------------------------


def at_name(env):
    s = env.sim(chat_id=2002)
    open_menu(s)
    enter_branch(s, "redesign")
    walk(s, "redesign", stop="fin_name")
    return s


def test_invalid_name_and_phone_show_errors_and_repeat_question(env):
    s = at_name(env)
    s.send("12345")
    assert label("err.name") in s.texts[-2]
    assert_question(s, "fin_name")
    s.send("Анна-Мария")
    assert_question(s, "fin_phone")
    s.send("12-34")
    assert label("err.phone") in s.texts
    assert_question(s, "fin_phone")
    s.send_sticker()
    assert label("err.need_phone") in s.texts
    s.send_contact("79991112233")
    assert label("fin_phone.received") in s.texts
    assert s.reply_keyboard == {"remove_keyboard": True}
    assert_question(s, "fin_messenger")
    answer_default(s, "fin_messenger")
    answer_default(s, "fin_time")
    assert "+79991112233" in s.current_text and "Анна-Мария" in s.current_text


def test_invalid_url_text_and_wrong_input_types(env):
    s = env.sim(chat_id=2003)
    open_menu(s)
    enter_branch(s, "redesign")
    s.click(label("rd_platform.opt.other"))
    s.send("x")
    assert label("err.text_short") in s.texts[-2]
    assert label("rd_platform.other.prompt") in s.last_text
    s.send("я" * 1001)
    assert label("err.text_long") in s.texts[-2]
    s.send_sticker()
    assert label("err.need_text") in s.texts[-2]
    s.send("Самописный движок")
    assert_question(s, "rd_what")
    s.send("просто текст")
    assert label("err.need_buttons") in s.texts[-2]
    assert_question(s, "rd_what")
    answer_default(s, "rd_what")
    s.send("не ссылка")
    assert label("err.url") in s.texts[-2]
    assert_question(s, "rd_link")
    s.send("old-site.ru")
    assert_question(s, "rd_rebuild")


def test_bad_tz_files_are_rejected(env):
    s = env.sim(chat_id=2004)
    open_menu(s)
    enter_branch(s, "other")
    walk(s, "other", stop="example")
    s.click(label("example.opt.tz"))
    s.send_document("virus.exe", mime="application/octet-stream")
    assert label("err.file_type") in s.texts[-2]
    s.send_document("big.pdf", size=21 * 1024 * 1024)
    assert label("err.file_size") in s.texts[-2]
    s.send("вот ТЗ")
    assert label("err.need_file") in s.texts[-2]
    assert label("example.tz.prompt") in s.last_text
    s.send_document("ok.pdf")
    s.click(label("common.done"))
    assert_question(s, "fin_name")


def test_phone_typed_in_russian_forms(env):
    s = at_name(env)
    s.send("Олег")
    s.send("8 (999) 765-43-21")
    assert_question(s, "fin_messenger")


# -- stale buttons, /start, /menu ----------------------------------------------------------


def test_old_button_is_inactive_and_current_question_is_repeated(sim):
    enter_branch(sim, "goods")
    old = sim.current_message_id
    pay_yes = next(b for b in sim.tg.messages[(sim.chat_id, old)]["reply_markup"]
                   ["inline_keyboard"] if b[0]["text"] == "Да")[0]["callback_data"]
    sim.click("Да")
    assert_question(sim, "delivery")
    sim.press(pay_yes, message_id=old)
    assert sim.last_popup == label("common.button_inactive")
    assert_question(sim, "delivery")


def test_start_in_the_middle_offers_continue_or_restart(sim):
    enter_branch(sim, "goods")
    sim.click("Да")
    sim.start()
    assert sim.last_text == label("resume.question")
    assert sim.buttons == [label("resume.opt.continue"), label("resume.opt.restart")]
    sim.click(label("resume.opt.continue"))
    assert_question(sim, "delivery")
    sim.start()
    sim.click(label("resume.opt.restart"))
    assert sim.last_text == label("menu.text")
    sim.click(label("menu.opt.new"))
    assert marked(sim) == []
    sim.click(label("ns_what.opt.goods"))
    assert marked(sim) == []


def test_start_while_typing_a_field_continues_the_field(sim):
    enter_branch(sim, "services")
    sim.click(label("sphere.opt.other"))
    sim.start()
    sim.send("текст вместо кнопки")
    assert label("err.need_buttons") in sim.texts[-2]
    sim.click(label("resume.opt.continue"))
    assert label("sphere.other.prompt") in sim.last_text
    sim.send("Ветеринарная клиника")
    assert_question(sim, "structure")


def test_repeated_update_is_processed_once(sim, env):
    enter_branch(sim, "goods")
    update = sim.click("Да")
    calls = len(env.tg.calls)
    sim.replay(update)
    assert len(env.tg.calls) == calls
    assert_question(sim, "delivery")


def test_new_engine_on_same_database_continues_dialogue(env, tmp_path):
    s = env.sim()
    open_menu(s)
    enter_branch(s, "goods")
    s.click("Да")
    s.click("Нет")
    from leadbot.storage import Storage
    restarted = ChatSim(Storage(env.path), env.tg, engine=env.new_engine(Storage(env.path)))
    restarted.click("Да")   # stock
    assert_question(restarted, "crm")
    restarted.click(label("common.back"))
    restarted.click(label("common.back"))
    assert marked(restarted) == ["Нет"]


def test_text_override_changes_the_next_reply(sim, env):
    env.storage.set_text_override("pay.question", "Оплата онлайн нужна?")
    enter_branch(sim, "goods")
    assert "Оплата онлайн нужна?" in sim.current_text
    env.storage.set_text_override("pay.opt.yes", "Конечно <да>")
    sim.click(label("common.back"))
    sim.click(label("ns_what.opt.goods"))
    assert "Конечно <да>" in sim.buttons


def test_html_in_overrides_and_user_input_is_escaped(sim, env):
    env.storage.set_text_override("ns_other_text.question", "Опишите <b>идею</b> & цели")
    sim.click(label("menu.opt.new"))
    sim.click(label("ns_what.opt.other"))
    assert "Опишите &lt;b&gt;идею&lt;/b&gt; &amp; цели" in sim.last_html
    sim.send("<script>alert(1)</script> & co")
    assert "&lt;script&gt;" in "".join(t["text"] for t in env.tg.calls_of("edit_message_text"))


def test_internal_error_is_reported_and_not_raised(sim, env):
    env.tg.fail["send_message"] = RuntimeError("boom")
    sim.click(label("menu.opt.new"))   # must not raise
    del env.tg.fail["send_message"]
    texts = [kw["text"] for kw in env.tg.calls_of("send_message")]
    assert texts[-1] == label("common.error")


def test_group_messages_other_than_owner_command_are_ignored(env):
    group = env.sim(chat_id=-4242, chat_type="group")
    group.start()
    group.send("привет")
    assert group.texts == []


def test_every_button_of_a_whole_branch_fits_callback_limit(sim):
    # ChatSim checks every keyboard; walking the longest branch exercises them all
    enter_branch(sim, "both")
    walk(sim, "both")
    sim.click(label("summary.opt.edit"))
    assert label("summary.back") in sim.buttons


@pytest.mark.parametrize("data", ["pay|pick|maybe", "pay|toggle|yes", "pay|cat|x", "pay"])
def test_forged_callback_on_current_message_is_inactive(sim, data):
    enter_branch(sim, "goods")
    sim.press(data)
    assert sim.last_popup == label("common.button_inactive")
    assert_question(sim, "pay")


def test_start_twice_keeps_the_resume_question(sim):
    enter_branch(sim, "goods")
    sim.click("Да")
    sim.start()
    sim.start()
    assert sim.last_text == label("resume.question")
    sim.click(label("resume.opt.continue"))
    assert_question(sim, "delivery")


def test_phone_keyboard_left_after_menu_is_removed_on_next_error(env):
    s = at_name(env)
    s.send("Анна")
    assert "keyboard" in s.reply_keyboard
    s.send("/menu")
    s.send_contact("79990001122")          # the leftover button pressed in the menu
    assert label("err.need_buttons") in s.texts[-2]
    assert s.reply_keyboard == {"remove_keyboard": True}
    assert s.last_text == label("menu.text")


def test_forged_pick_of_hub_category_is_inactive(sim):
    enter_branch(sim, "goods")
    walk(sim, "goods", stop="sections")
    category = scenario.hub_options("goods")[0]
    sim.press(f"sections|pick|{category.id}")
    assert sim.last_popup == label("common.button_inactive")
    assert label("common.error") not in sim.texts
    assert_question(sim, "sections", "goods")
