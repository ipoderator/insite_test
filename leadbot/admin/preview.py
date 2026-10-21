"""«Как это увидит клиент»: a screen of the scenario as Telegram messages, for the panel.

Built from the same scenario nodes and texts as ``Engine._render``, but without a chat:
answers, marks and placeholders get sample values, so the result is close to what the bot
sends, not a byte-exact copy. Pure (reads the database only through ``TextStore.storage``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..scenario import NO_BACK_NODES, NODES, Node
from ..texts import TEXTS, TextStore
from . import content

# Placeholders the bot fills from the chat; ``{policy}`` is drawn as a link instead.
SAMPLE = {"name": "Мария", "topic": "строительной тематике", "label": "📄 Основные страницы",
          "count": "2", "url": "https://example.ru", "file": "ТЗ.pdf", "id": "128",
          "value": "@site_it"}

Part = Tuple[str, str]  # (text, style): "" plain, "b" bold, "a" link


@dataclass
class Message:
    parts: List[Part]
    buttons: List[str] = field(default_factory=list)        # under the message
    reply_buttons: List[str] = field(default_factory=list)  # instead of the keyboard
    popup: bool = False                                     # a pop-up over the buttons


class PreviewTexts(TextStore):
    """Saved texts with unsaved ``changes`` on top."""

    def __init__(self, storage: Any, changes: Dict[str, str]):
        super().__init__(storage)
        self.changes = dict(changes)

    def _override(self, key: str) -> Optional[str]:
        if key in self.changes:
            return self.changes[key]
        return super()._override(key)


def _node_keys(node: Node) -> List[str]:
    keys = [node.question_key, node.hint_key, node.footer_key, node.intro_key, node.done_key]
    keys += [o.text_key for o in node.options]
    return [k for k in keys if k]


def _prompt_keys(node: Node) -> List[str]:
    return [spec.prompt_key for o in node.options for spec in o.inputs]


def node_of(key: str) -> Optional[str]:
    """The screen that shows ``key``: its own step first, else the first one that uses it."""
    own = NODES.get(content.step_of(key))
    if own is not None and key in _node_keys(own):
        return own.id
    for node in NODES.values():
        if key in _node_keys(node):
            return node.id
    return None


def _is_prompt(key: str) -> bool:
    return any(key in _prompt_keys(node) for node in NODES.values())


def _has_back(node: Node) -> bool:
    return (node.id not in NO_BACK_NODES and node.done_key != "common.back_to_sections"
            and all(o.text_key != "common.back_to_spheres" for o in node.options))


def _settings(texts: TextStore) -> Dict[str, str]:
    return texts.storage.all_settings() if texts.storage is not None else {}


def _categories(texts: TextStore) -> List[dict]:
    if texts.storage is None:
        return []
    return texts.storage.list_categories(active_only=True)


def _privacy_parts(texts: TextStore) -> List[Part]:
    text, link = texts.get("privacy.text", **SAMPLE), texts.get("privacy.link")
    if "{policy}" not in text:
        return [(text + "\n", ""), (link, "a")]
    before, after = text.split("{policy}", 1)
    return [(before, ""), (link, "a"), (after.replace("{policy}", link), "")]


def _contacts_parts(texts: TextStore) -> List[Part]:
    settings = _settings(texts)
    lines = [texts.get("contacts.text"), "", texts.get("contacts.header")]
    for key, setting in (("contacts.telegram", "contact_telegram"),
                         ("contacts.phone", "contact_phone"),
                         ("contacts.email", "contact_email")):
        value = (settings.get(setting) or "").strip()
        if value:
            lines.append(texts.get(key, value=value))
    return [("\n".join(lines), "")]


def _card_parts(texts: TextStore) -> List[Part]:
    categories = _categories(texts)
    category = categories[0] if categories else None
    topic = category["topic"] if category else SAMPLE["topic"]
    lines = [texts.get("ex_card.header", topic=topic), ""]
    items = texts.storage.list_items(category["id"]) if category else []
    for item in items:
        line = "• " + item["title"]
        if item.get("is_demo"):
            line += " " + texts.get("ex_card.demo")
        if (item.get("note") or "").strip():
            line += " — " + item["note"].strip()
        lines.append(line)
    if not items:
        lines.append(texts.get("ex_card.empty"))
    lines += ["", texts.get("ex_card.note")]
    return [("\n".join(lines), "")]


def _question_parts(texts: TextStore, node: Node) -> List[Part]:
    parts: List[Part] = [(texts.get(node.question_key, **SAMPLE), "b")]
    hint = node.hint_key or ("common.multi_hint" if node.kind == "multi" else None)
    if hint:
        parts.append(("\n" + texts.get(hint, **SAMPLE), ""))
    if node.footer_key:
        parts.append(("\n\n" + texts.get(node.footer_key, **SAMPLE), ""))
    return parts


def _body(texts: TextStore, node: Node) -> List[Part]:
    if node.id == "privacy":
        return _privacy_parts(texts)
    if node.id == "contacts":
        return _contacts_parts(texts)
    if node.id == "ex_card":
        return _card_parts(texts)
    if node.kind == "info" or node.id == "menu":
        return [(texts.get(node.question_key, **SAMPLE), "")]
    return _question_parts(texts, node)


def _buttons(texts: TextStore, node: Node) -> List[str]:
    buttons: List[str] = []
    if node.id == "ex_sphere":
        buttons += [c["button"] for c in _categories(texts)]
    buttons += [texts.get(o.text_key, **SAMPLE) for o in node.options]
    if node.kind in ("hub", "multi"):
        buttons.append(texts.get(node.done_key or "common.done"))
    if _has_back(node):
        buttons.append(texts.get("common.back"))
    return buttons


def screen(texts: TextStore, node_id: str) -> List[Message]:
    """Messages the bot sends when the client reaches ``node_id``."""
    node = NODES[node_id]
    messages: List[Message] = []
    if node.intro_key:
        messages.append(Message([(texts.get(node.intro_key, **SAMPLE), "")]))
    if node.kind == "phone":
        share = [texts.get(o.text_key) for o in node.options]
        messages.append(Message(_question_parts(texts, node), reply_buttons=share))
        messages.append(Message([(texts.get("fin_phone.share_hint"), "")],
                                buttons=[texts.get("common.back")]))
        return messages
    messages.append(Message(_body(texts, node), buttons=_buttons(texts, node)))
    return messages


def text_screen(texts: TextStore, key: str) -> List[Message]:
    """Where the client meets ``key``: its screen, or the text alone (a prompt, an error)."""
    if key in content.POPUP_KEYS:
        return [Message([(texts.get(key, **SAMPLE), "")], popup=True)]
    node_id = node_of(key)
    if node_id is not None:
        return screen(texts, node_id)
    if content.is_button(TEXTS[key]) and not _is_prompt(key):
        return [Message([], buttons=[texts.get(key, **SAMPLE)])]
    return [Message([(texts.get(key, **SAMPLE), "")])]
