"""Chat simulator: drives ``Engine.handle_update`` like a Telegram client would.

``ChatSim`` sends messages and presses inline buttons by their caption; it reads what the
bot sent from ``FakeTelegram``. Every text the bot sends or edits is checked to be valid
Telegram HTML (only allowed tags, balanced, no raw ``<``/``&``) and at most 4096 chars;
every callback_data is checked to be at most 64 bytes.
"""
from __future__ import annotations

import html
import itertools
import re
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional

from leadbot.engine import Engine
from leadbot.texts import TEXTS
from tests.fakes import FakeTelegram

_update_ids = itertools.count(1)
_file_ids = itertools.count(1)
ALLOWED_TAGS = {"b", "i", "u", "s", "a", "code", "pre"}
_ENTITY_RE = re.compile(r"&(#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);")


def plain(text: str) -> str:
    """What the user sees in a Telegram HTML message: tags removed, entities decoded."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def label(key: str, **vars: Any) -> str:
    """Default text of a ``TEXTS`` key with ``{name}`` substitution, as the bot shows it."""
    value = TEXTS[key].default
    for name, replacement in vars.items():
        value = value.replace("{" + name + "}", str(replacement))
    return value


class _TagChecker(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: List[str] = []
        self.errors: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self.errors.append(f"tag <{tag}> is not allowed")
        if tag == "a" and [k for k, _ in attrs] != ["href"]:
            self.errors.append("<a> must have only href")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unbalanced </{tag}>")
        else:
            self.stack.pop()


def check_html(text: str) -> None:
    assert isinstance(text, str) and text, "empty message"
    assert len(text) <= 4096, f"message is {len(text)} chars"
    checker = _TagChecker()
    checker.feed(text)
    checker.close()
    assert not checker.errors, (checker.errors, text)
    assert not checker.stack, ("unclosed tags", checker.stack, text)
    bare = re.sub(r"<(/?)(b|i|u|s|code|pre)>|</a>|<a href=\"[^\"<>]*\">", "", text)
    assert "<" not in bare and ">" not in bare, ("unescaped < or >", text)
    assert all(_ENTITY_RE.match(bare, m.start()) for m in re.finditer("&", bare)), \
        ("unescaped &", text)


def check_markup(markup: Optional[dict]) -> None:
    if not markup or "inline_keyboard" not in markup:
        return
    for row in markup["inline_keyboard"]:
        for button in row:
            data = button["callback_data"]
            assert len(data.encode("utf-8")) <= 64, data
            assert button["text"].strip(), button


def inline_labels(markup: Optional[dict]) -> List[str]:
    if not markup or "inline_keyboard" not in markup:
        return []
    return [b["text"] for row in markup["inline_keyboard"] for b in row]


class ChatSim:
    def __init__(self, storage, tg: Optional[FakeTelegram] = None, engine: Optional[Engine] = None,
                 chat_id: int = 1001, username: Optional[str] = "anna_client",
                 first_name: str = "Анна", chat_type: str = "private"):
        self.storage = storage
        self.tg = tg if tg is not None else FakeTelegram()
        self.engine = engine if engine is not None else Engine(storage, self.tg)
        self.chat_id = chat_id
        self.user = {"id": chat_id, "is_bot": False, "first_name": first_name}
        if username:
            self.user["username"] = username
        self.chat_type = chat_type
        self.last_update: Optional[dict] = None
        self._checked = 0

    # -- sending -------------------------------------------------------------

    def _dispatch(self, body: Dict[str, Any]) -> dict:
        update = {"update_id": next(_update_ids), **body}
        return self.replay(update)

    def replay(self, update: dict) -> dict:
        """Hand an update to the engine (also used to re-send an already sent update)."""
        self.last_update = update
        self.engine.handle_update(update)
        self._check_new_calls()
        return update

    def _message(self, **fields: Any) -> dict:
        msg = {"message_id": next(_update_ids), "date": 0, "from": dict(self.user),
               "chat": {"id": self.chat_id, "type": self.chat_type}}
        msg.update(fields)
        return self._dispatch({"message": msg})

    def start(self) -> dict:
        return self.send("/start")

    def send(self, text: str) -> dict:
        return self._message(text=text)

    def send_contact(self, phone: str, user_id: Optional[int] = None) -> dict:
        return self._message(contact={"phone_number": phone, "first_name": "Анна",
                                      "user_id": user_id or self.chat_id})

    def send_document(self, file_name: Optional[str] = "tz.pdf", mime: str = "application/pdf",
                      size: int = 1024, file_id: Optional[str] = None) -> dict:
        file_id = file_id or f"DOC{next(_file_ids)}"
        doc = {"file_id": file_id, "file_unique_id": "u" + file_id, "mime_type": mime,
               "file_size": size}
        if file_name is not None:
            doc["file_name"] = file_name
        return self._message(document=doc)

    def send_photo(self, size: int = 2048, file_id: Optional[str] = None) -> dict:
        file_id = file_id or f"PHO{next(_file_ids)}"
        return self._message(photo=[
            {"file_id": file_id + "s", "file_unique_id": "s" + file_id, "width": 90,
             "height": 90, "file_size": 100},
            {"file_id": file_id, "file_unique_id": "u" + file_id, "width": 800, "height": 800,
             "file_size": size},
        ])

    def send_sticker(self) -> dict:
        return self._message(sticker={"file_id": "STK", "file_unique_id": "stk", "width": 1,
                                      "height": 1, "is_animated": False, "type": "regular"})

    def press(self, data: str, message_id: Optional[int] = None) -> dict:
        if message_id is None:
            message_id = self.current_message_id
        return self._dispatch({"callback_query": {
            "id": f"cb{next(_update_ids)}", "from": dict(self.user), "chat_instance": "ci",
            "message": {"message_id": message_id, "date": 0,
                        "chat": {"id": self.chat_id, "type": self.chat_type}},
            "data": data,
        }})

    def click(self, caption: str, message_id: Optional[int] = None) -> dict:
        """Press the button of the current keyboard (or of ``message_id``) by its caption.

        Exact caption first, then the caption without the "✅ " mark, then a unique substring.
        """
        if message_id is None:
            message_id = self.current_message_id
        assert message_id is not None, "no message with buttons"
        buttons = [b for row in self.tg.messages[(self.chat_id, message_id)]["reply_markup"]
                   ["inline_keyboard"] for b in row]
        mark = TEXTS["common.mark"].default + " "
        found = [b for b in buttons if b["text"] == caption]
        if not found:
            found = [b for b in buttons if b["text"].startswith(mark)
                     and b["text"][len(mark):] == caption]
        if not found:
            found = [b for b in buttons if caption in b["text"]]
        assert len(found) == 1, f"button {caption!r} not found once in {[b['text'] for b in buttons]}"
        return self.press(found[0]["callback_data"], message_id)

    # -- reading -------------------------------------------------------------

    def _check_new_calls(self) -> None:
        for method, kwargs in self.tg.calls[self._checked:]:
            if method in ("send_message", "edit_message_text"):
                check_html(kwargs["text"])
            if method in ("send_message", "edit_message_text", "edit_reply_markup"):
                check_markup(kwargs.get("reply_markup"))
        self._checked = len(self.tg.calls)

    def mark(self) -> int:
        """Position in the call log, for ``texts_since``."""
        return len(self.tg.calls)

    def texts_since(self, mark: int) -> List[str]:
        return [plain(kw["text"]) for name, kw in self.tg.calls[mark:]
                if name == "send_message" and kw["chat_id"] == self.chat_id]

    @property
    def texts(self) -> List[str]:
        """Plain texts (HTML unescaped) of every message sent to this chat, in order."""
        return [plain(t) for t in self.tg.sent_texts(self.chat_id)]

    @property
    def last_text(self) -> str:
        texts = self.texts
        return texts[-1] if texts else ""

    @property
    def last_html(self) -> str:
        texts = self.tg.sent_texts(self.chat_id)
        return texts[-1] if texts else ""

    @property
    def current_message_id(self) -> Optional[int]:
        """Latest message of this chat that carries an inline keyboard."""
        ids = sorted((mid for (chat, mid), msg in self.tg.messages.items()
                      if chat == self.chat_id and inline_labels(msg.get("reply_markup"))),
                     reverse=True)
        return ids[0] if ids else None

    @property
    def current_text(self) -> str:
        mid = self.current_message_id
        return plain(self.tg.messages[(self.chat_id, mid)]["text"]) if mid else ""

    @property
    def buttons(self) -> List[str]:
        mid = self.current_message_id
        if mid is None:
            return []
        return inline_labels(self.tg.messages[(self.chat_id, mid)]["reply_markup"])

    def message_text(self, message_id: int) -> str:
        return plain(self.tg.messages[(self.chat_id, message_id)]["text"])

    def keyboard_of(self, message_id: int) -> List[str]:
        return inline_labels(self.tg.messages[(self.chat_id, message_id)].get("reply_markup"))

    @property
    def reply_keyboard(self) -> Optional[dict]:
        """Reply keyboard of the last message that set or removed one."""
        for kw in reversed(self.tg.calls_of("send_message")):
            markup = kw.get("reply_markup") or {}
            if kw["chat_id"] == self.chat_id and ("keyboard" in markup or "remove_keyboard" in markup):
                return markup
        return None

    @property
    def popups(self) -> List[Optional[str]]:
        return [kw["text"] for kw in self.tg.calls_of("answer_callback")]

    @property
    def last_popup(self) -> Optional[str]:
        popups = self.popups
        return popups[-1] if popups else None


# -- scenario drivers shared by the engine tests ------------------------------------------

from leadbot import scenario  # noqa: E402
from leadbot.storage import Storage  # noqa: E402

OWNER_CHAT = -100500  # a group chat
DEFAULT_TYPED = {"text": "Описание для теста", "name": "Анна", "phone": "+7 999 123-45-67",
                 "url": "https://example.ru"}


class Env:
    """One database file + one fake Telegram shared by several chats and engine instances."""

    def __init__(self, tmp_path, public_base_url: str = "https://bot.example.org"):
        self.path = str(tmp_path / "bot.sqlite3")
        self.storage = Storage(self.path)
        self.storage.seed_defaults()
        self.tg = FakeTelegram()
        self.public_base_url = public_base_url
        self.engine = self.new_engine()

    def new_engine(self, storage=None) -> Engine:
        return Engine(storage or self.storage, self.tg, public_base_url=self.public_base_url)

    def sim(self, **kwargs: Any) -> ChatSim:
        kwargs.setdefault("engine", self.engine)
        return ChatSim(self.storage, self.tg, **kwargs)

    def bind_owner(self, chat_id: int = OWNER_CHAT) -> ChatSim:
        owner = self.sim(chat_id=chat_id, username="owner", first_name="Владелец",
                         chat_type="group" if chat_id < 0 else "private")
        owner.send("/owner " + self.storage.get_setting("owner_bind_code"))
        return owner

    def owner_texts(self, chat_id: int = OWNER_CHAT) -> List[str]:
        return [plain(t) for t in self.tg.sent_texts(chat_id)]

    @property
    def leads(self) -> List[dict]:
        return self.storage.list_leads()


def strip_mark(text: str) -> str:
    mark = TEXTS["common.mark"].default + " "
    return text[len(mark):] if text.startswith(mark) else text


def open_menu(sim: ChatSim) -> None:
    sim.start()
    sim.click(label("privacy.start"))


def screen_options(node_id: str, branch: Optional[str] = None) -> List[str]:
    node = scenario.NODES[node_id]
    if node.kind == "hub":
        return [label(o.text_key) for o in scenario.hub_options(branch)]
    return [label(o.text_key) for o in node.options
            if not (node_id == "pr_link" and o.id == "use") and node.kind != "phone"]


def assert_question(sim: ChatSim, node_id: str, branch: Optional[str] = None) -> None:
    """The bot shows the node's question text and all its option captions (defaults)."""
    node = scenario.NODES[node_id]
    question = label(node.question_key)
    if node.kind == "phone":
        assert question in "\n".join(sim.texts[-2:]), (question, sim.texts[-2:])
        keyboard = sim.reply_keyboard
        assert keyboard and keyboard["keyboard"][0][0]["text"] == label("fin_phone.opt.share")
        assert keyboard["keyboard"][0][0]["request_contact"] is True
        return
    assert question in sim.current_text, (node_id, question, sim.current_text)
    raw = sim.buttons
    shown = set(raw) | {strip_mark(b) for b in raw} | {strip_mark(b).rsplit(" (", 1)[0] for b in raw}
    for caption in screen_options(node_id, branch):
        assert caption in shown, (node_id, caption, raw)


def first_plain_option(node_id: str) -> scenario.Option:
    """First option without a field that keeps the branch going (non-exclusive if possible)."""
    candidates = [o for o in scenario.NODES[node_id].options
                  if not o.inputs and not o.final_jump and not o.next]
    return next((o for o in candidates if not o.exclusive), None) or candidates[0]


def answer_default(sim: ChatSim, node_id: str, branch: Optional[str] = None) -> None:
    """Answer a question in the simplest valid way (first plain option / typical input)."""
    node = scenario.NODES[node_id]
    assert_question(sim, node_id, branch)
    if node.kind == "hub":
        category = scenario.hub_options(branch)[0]
        sim.click(label(category.text_key))
        sim.click(label(scenario.NODES[category.next].options[0].text_key))
        sim.click(label("common.back_to_sections"))
        sim.click(label("common.done"))
    elif node.kind == "single":
        sim.click(label(first_plain_option(node_id).text_key))
    elif node.kind == "multi":
        sim.click(label(first_plain_option(node_id).text_key))
        sim.click(label(node.done_key or "common.done"))
    else:
        sim.send(DEFAULT_TYPED[node.kind])


def path_of(branch: str) -> List[str]:
    """Nodes the default answers go through: the branch, then the final block."""
    return list(scenario.BRANCHES[branch]) + list(scenario.BRANCHES["final"])


def enter_branch(sim: ChatSim, branch: str) -> None:
    """From the main menu to the first question of ``branch``."""
    if branch in scenario.NEW_SITE_BRANCHES:
        sim.click(label("menu.opt.new"))
        assert_question(sim, "ns_what")
        sim.click(label(f"ns_what.opt.{branch}"))
    else:
        sim.click(label(f"menu.opt.{branch}"))


def walk(sim: ChatSim, branch: str, start: Optional[str] = None,
         stop: Optional[str] = None) -> None:
    """Answer the default way every node of the path from ``start`` up to (not incl.) ``stop``."""
    nodes = path_of(branch)
    begin = nodes.index(start) if start else 0
    for node_id in nodes[begin:]:
        if node_id == stop:
            return
        answer_default(sim, node_id, branch)
