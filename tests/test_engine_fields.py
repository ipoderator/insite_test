"""Every option with a "→ поле" field asks for its input and keeps it up to the lead."""
import pytest

from leadbot import scenario
from leadbot.scenario import NODES
from tests.sim import Env, enter_branch, label, open_menu, path_of, walk

CATEGORY_NODES = {cat for cats in scenario.HUB_VARIANTS.values() for cat in cats}
FIELD_CASES = [(node_id, opt.id) for node_id, node in NODES.items()
               for opt in node.options if opt.inputs]


def branch_of(node_id):
    if node_id == "sec_services":
        return "services"
    if node_id in CATEGORY_NODES or node_id == "sections":
        return "goods"
    return next(b for b, seq in scenario.BRANCHES.items() if node_id in seq)


def typed_value(spec):
    if spec.kind == "url":
        return "field-example.ru", "https://field-example.ru"
    if spec.kind == "urls":
        return "vk.com/studio t.me/studio", "https://vk.com/studio"
    value = f"Ответ на {spec.prompt_key}"
    return value, value


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_field_cases_cover_the_pdf_fields():
    # sanity check of the parametrization itself: 5 hub categories with "add", hub "Иное",
    # and the fields of the other questions
    assert len([c for c in FIELD_CASES if c[0] in CATEGORY_NODES]) == 5
    assert ("sections", "other") in FIELD_CASES and ("example", "tz") in FIELD_CASES
    assert len(FIELD_CASES) >= 30


@pytest.mark.parametrize("node_id,option_id", FIELD_CASES)
def test_option_field_is_asked_and_kept_in_summary_and_lead(env, node_id, option_id):
    branch = branch_of(node_id)
    node, opt = NODES[node_id], NODES[node_id].option(option_id)
    sim = env.sim()
    open_menu(sim)
    enter_branch(sim, branch)
    target = "sections" if node_id in CATEGORY_NODES else node_id
    walk(sim, branch, stop=target)

    if node_id in CATEGORY_NODES:
        hub_button = next(o for o in scenario.hub_options(branch) if o.next == node_id)
        sim.click(label(hub_button.text_key))
        sim.click(label(opt.text_key))
        sim.click(label("common.back_to_sections"))
    elif node_id == "sections":
        sim.click(label(opt.text_key))
    else:
        sim.click(label(opt.text_key))
        if node.kind == "multi":
            sim.click(label(node.done_key or "common.done"))

    expected = []
    for spec in opt.inputs:
        assert label(spec.prompt_key) in sim.last_text, (spec.prompt_key, sim.last_text)
        if spec.kind == "file":
            sim.send_document("brief.docx", mime="application/msword", size=5000,
                              file_id="FILE-1")
            assert "brief.docx" in sim.last_text
            sim.click(label("common.done"))
            expected.append("brief.docx")
        else:
            sent, shown = typed_value(spec)
            sim.send(sent)
            expected.append(shown)

    if target == "sections":
        sim.click(label("common.done"))
    nodes = path_of(branch)
    walk(sim, branch, start=nodes[nodes.index(target) + 1])

    summary = "\n".join(sim.texts[-3:])
    assert label("summary.header") in summary
    for value in expected:
        assert value in summary, (value, summary)
    sim.click(label("summary.opt.submit"))
    [lead] = env.leads
    lead_answers = "\n".join(i["answer"] for i in lead["items"])
    for value in expected:
        assert value in lead_answers
    if any(spec.kind == "file" for spec in opt.inputs):
        assert [f["file_id"] for f in lead["files"]] == ["FILE-1"]
        assert lead["files"][0]["file_name"] == "brief.docx"


def test_optional_comment_can_be_skipped(env):
    sim = env.sim()
    open_menu(sim)
    enter_branch(sim, "other")
    walk(sim, "other", stop="example")
    sim.click(label("example.opt.yes"))
    sim.send("liked.example.ru")
    assert label("example.yes.comment") in sim.last_text
    sim.click(label("common.skip"))
    assert "Ваше имя:" in sim.current_text
    walk(sim, "other", start="fin_name")
    summary = sim.current_text
    assert "https://liked.example.ru" in summary and label("summary.skipped") in summary


def test_several_tz_files_and_a_photo_go_to_the_owner(env):
    env.bind_owner()
    sim = env.sim()
    open_menu(sim)
    enter_branch(sim, "other")
    walk(sim, "other", stop="example")
    sim.click(label("example.opt.tz"))
    sim.click(label("common.done"))
    assert sim.last_popup == label("err.no_files")
    sim.send_document("tz.pdf", file_id="D1")
    sim.send_photo(file_id="P1")
    sim.click(label("common.done"))
    walk(sim, "other", start="fin_name")
    assert label("summary.files_value", count=2) in sim.current_text
    sim.click(label("summary.opt.submit"))
    [lead] = env.leads
    assert [(f["file_id"], f["kind"]) for f in lead["files"]] == [("D1", "document"),
                                                                ("P1", "photo")]
    docs = env.tg.calls_of("send_document")
    photos = env.tg.calls_of("send_photo")
    assert [d["file_id"] for d in docs] == ["D1"] and [p["file_id"] for p in photos] == ["P1"]
    caption = label("lead.file_caption", id=lead["id"])
    assert docs[0]["caption"] == caption and docs[0]["chat_id"] == photos[0]["chat_id"] < 0
