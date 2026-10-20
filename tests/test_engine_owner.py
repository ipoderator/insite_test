"""Owner binding with /owner CODE and delivery of the lead (R59, R95i)."""
import pytest

from leadbot.telegram import TelegramError
from tests.sim import OWNER_CHAT, Env, label, open_menu, enter_branch, walk


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def submit_lead(env, branch="redesign", **sim_kwargs):
    sim = env.sim(**sim_kwargs)
    open_menu(sim)
    enter_branch(sim, branch)
    walk(sim, branch)
    sim.click(label("summary.opt.submit"))
    return sim


def test_owner_binds_group_with_valid_code_and_code_rotates(env):
    code = env.storage.get_setting("owner_bind_code")
    owner = env.sim(chat_id=OWNER_CHAT, chat_type="group")
    owner.send(f"/owner@test_bot {code}")
    assert env.storage.get_setting("owner_chat_id") == str(OWNER_CHAT)
    assert owner.last_text == label("owner.bound")
    new_code = env.storage.get_setting("owner_bind_code")
    assert new_code and new_code != code
    other = env.sim(chat_id=777, chat_type="private")
    other.send(f"/owner {code}")                # old code no longer works
    assert other.last_text == label("owner.bad_code")
    assert env.storage.get_setting("owner_chat_id") == str(OWNER_CHAT)


def test_owner_binds_private_chat_case_insensitive(env):
    code = env.storage.get_setting("owner_bind_code")
    owner = env.sim(chat_id=555)
    owner.send(f"/owner {code.lower()}")
    assert env.storage.get_setting("owner_chat_id") == "555"


@pytest.mark.parametrize("command", ["/owner", "/owner WRONGCOD", "/owner  "])
def test_wrong_or_missing_code_is_refused(env, command):
    owner = env.sim(chat_id=OWNER_CHAT, chat_type="group")
    owner.send(command)
    assert owner.last_text == label("owner.bad_code")
    assert env.storage.get_setting("owner_chat_id") is None


def test_owner_receives_full_lead_text(env):
    env.bind_owner()
    sim = submit_lead(env)
    [lead] = env.leads
    texts = env.owner_texts()
    full = "\n".join(texts)
    assert label("lead.header", id=lead["id"]) in full
    assert label("lead.kind.redesign") in full
    assert "@anna_client" in full
    assert f'href="tg://user?id={sim.chat_id}"' in "\n".join(env.tg.sent_texts(OWNER_CHAT))
    for item in lead["items"]:
        assert f"{item['title']}: {item['answer']}" in full
    assert lead["delivered"] is True and lead["delivery_error"] is None
    assert sim.last_text == label("thanks.text", name="Анна")


def test_client_without_username_is_linked_by_id(env):
    env.bind_owner()
    sim = submit_lead(env, username=None, chat_id=3003)
    full = "\n".join(env.owner_texts())
    assert label("lead.no_username") in full
    assert 'href="tg://user?id=3003"' in "\n".join(env.tg.sent_texts(OWNER_CHAT))
    assert sim.last_text == label("thanks.text", name="Анна")


def test_without_owner_lead_is_saved_as_not_delivered(env):
    sim = submit_lead(env)
    [lead] = env.leads
    assert lead["delivered"] is False and lead["delivery_error"]
    assert sim.last_text == label("thanks.text", name="Анна")
    assert len(sim.buttons) == 3


def test_telegram_error_marks_lead_not_delivered_and_client_is_thanked(env):
    env.bind_owner()
    sim = env.sim()
    open_menu(sim)
    enter_branch(sim, "redesign")
    walk(sim, "redesign")
    original = env.tg.send_message

    def failing(chat_id, text, reply_markup=None, disable_preview=True):
        if chat_id == OWNER_CHAT:   # only the owner's chat fails
            raise TelegramError("Forbidden: bot was kicked from the group chat", 403)
        return original(chat_id, text, reply_markup=reply_markup, disable_preview=disable_preview)

    env.tg.send_message = failing
    sim.click(label("summary.opt.submit"))
    [lead] = env.leads
    assert lead["delivered"] is False
    assert "bot was kicked" in lead["delivery_error"]
    assert sim.last_text == label("thanks.text", name="Анна")


def test_lead_is_saved_before_delivery(env):
    env.bind_owner()
    seen = {}
    original = env.tg.send_message

    def spy(chat_id, text, reply_markup=None, disable_preview=True):
        if chat_id == OWNER_CHAT:
            seen["leads_at_delivery"] = len(env.storage.list_leads())
        return original(chat_id, text, reply_markup=reply_markup, disable_preview=disable_preview)

    env.tg.send_message = spy
    submit_lead(env)
    assert seen["leads_at_delivery"] == 1
