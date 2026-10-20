"""Promotion (with the pr_current condition) and redesign branches up to the lead."""
import pytest

from leadbot import scenario
from tests.sim import (Env, answer_default, assert_question, enter_branch, label, open_menu,
                       walk)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def titles(lead):
    return [i["title"] for i in lead["items"]]


def test_promo_through_helpers_to_lead(env):
    env.bind_owner()
    sim = env.sim()
    open_menu(sim)
    enter_branch(sim, "promo")
    assert label("pr_what.intro") in "\n".join(sim.texts[-2:])
    walk(sim, "promo")
    sim.click(label("summary.opt.submit"))
    [lead] = env.leads
    assert lead["kind"] == "promo" and lead["delivered"]
    expected = scenario.BRANCHES["promo"] + scenario.BRANCHES["final"]
    assert titles(lead) == [label(scenario.NODES[n].title_key) for n in expected]
    assert label("lead.kind.promo") in "\n".join(env.owner_texts())


@pytest.mark.parametrize("jump", ["never", "hard"])
def test_promo_never_or_hard_goes_straight_to_final(env, jump):
    sim = env.sim()
    open_menu(sim)
    enter_branch(sim, "promo")
    walk(sim, "promo", stop="pr_current")
    sim.click(label("pr_current.opt.seo"))
    sim.click(label(f"pr_current.opt.{jump}"))   # exclusive: removes "seo"
    marks = [b for b in sim.buttons if b.startswith("✅ ") and b != label("common.done")]
    assert marks == ["✅ " + label(f"pr_current.opt.{jump}")]
    sim.click(label("common.done"))
    assert_question(sim, scenario.FINAL)
    walk(sim, "promo", start="fin_name")
    sim.click(label("summary.opt.submit"))
    [lead] = env.leads
    assert label("pr_helpers.title") not in titles(lead)
    assert titles(lead)[:3] == [label("pr_what.title"), label("pr_types.title"),
                                label("pr_current.title")]
    assert titles(lead)[3] == label("fin_name.title")


@pytest.mark.parametrize("picked", ["paid", "seo", "social", "unsure", "past"])
def test_pr_helpers_shown_after_each_of_first_five(env, picked):
    sim = env.sim()
    open_menu(sim)
    enter_branch(sim, "promo")
    walk(sim, "promo", stop="pr_current")
    sim.click(label(f"pr_current.opt.{picked}"))
    sim.click(label("common.done"))
    if scenario.NODES["pr_current"].option(picked).inputs:
        sim.send("Яндекс Директ и ВК")
    assert_question(sim, "pr_helpers")


def test_pr_link_offers_site_link_from_pr_what_and_no_site_button(env):
    sim = env.sim()
    open_menu(sim)
    enter_branch(sim, "promo")
    sim.click(label("pr_what.opt.site"))
    sim.click(label("pr_what.opt.social"))
    sim.click(label("common.done"))
    assert label("pr_what.site.prompt") in sim.last_text
    sim.send("my-shop.ru")
    assert label("pr_what.social.prompt") in sim.last_text
    sim.send("vk.com/myshop\nt.me/myshop")
    walk(sim, "promo", start="pr_types", stop="pr_link")
    use = label("pr_link.opt.use", url="https://my-shop.ru")
    assert use in sim.buttons and label("pr_link.opt.none") in sim.buttons
    sim.click(use)
    assert_question(sim, scenario.FINAL)
    walk(sim, "promo", start="fin_name")
    summary = sim.current_text
    assert "https://vk.com/myshop" in summary and "https://t.me/myshop" in summary
    assert summary.count("https://my-shop.ru") == 2  # pr_what field and pr_link


def test_pr_link_without_known_site_and_no_site_option(env):
    sim = env.sim()
    open_menu(sim)
    enter_branch(sim, "promo")
    walk(sim, "promo", stop="pr_link")
    assert not any("Использовать" in b for b in sim.buttons)
    sim.click(label("pr_link.opt.none"))
    assert_question(sim, scenario.FINAL)


def test_redesign_to_lead_with_typed_link(env):
    env.bind_owner()
    sim = env.sim()
    open_menu(sim)
    enter_branch(sim, "redesign")
    walk(sim, "redesign")
    sim.click(label("summary.opt.submit"))
    [lead] = env.leads
    assert lead["kind"] == "redesign" and lead["delivered"]
    expected = scenario.BRANCHES["redesign"] + scenario.BRANCHES["final"]
    assert titles(lead) == [label(scenario.NODES[n].title_key) for n in expected]
    answers = {i["title"]: i["answer"] for i in lead["items"]}
    assert answers[label("rd_link.title")] == "https://example.ru"
    assert label("lead.kind.redesign") in "\n".join(env.owner_texts())


@pytest.mark.parametrize("option", ["forgot", "broken"])
def test_rd_link_help_buttons_answer_instead_of_link(env, option):
    sim = env.sim()
    open_menu(sim)
    enter_branch(sim, "redesign")
    walk(sim, "redesign", stop="rd_link")
    assert_question(sim, "rd_link")
    sim.click(label(f"rd_link.opt.{option}"))
    assert_question(sim, "rd_rebuild")
    walk(sim, "redesign", start="rd_rebuild")
    assert label(f"rd_link.opt.{option}") in sim.current_text


def test_rd_promoted_shows_footer_comment(env):
    sim = env.sim()
    open_menu(sim)
    enter_branch(sim, "redesign")
    walk(sim, "redesign", stop="rd_promoted")
    assert label("rd_promoted.footer") in sim.current_text
    answer_default(sim, "rd_promoted", "redesign")
    assert_question(sim, "rd_access")
