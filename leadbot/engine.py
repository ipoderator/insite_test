"""Dialogue engine: one finite-state machine executes ``leadbot.scenario`` for every chat.

``Engine.handle_update`` is the only entry point. Each update loads the chat's session from
``Storage``, changes it, sends messages through the Telegram client and saves it back, so a
restarted process continues every dialogue where it stopped.

Session (JSON, one row per chat; missing keys get defaults on load)::

    accepted_privacy  bool — "Начать" pressed on the privacy screen
    flow              "new" | "promo" | "redesign" while a questionnaire is open, else None
    branch            current branch (goods/services/both/other/promo/redesign) or None
    node              current screen (a scenario node id; "summary" also for the edit list)
    stack             screens to return to with "Назад"
    answers           {answer_key: answer} — shapes in ``leadbot.leads``
    pending_inputs    [{"node", "option", "field"}] — "→ поле" inputs still to ask
    edit_mode         True while fixing an answer from the summary
    message_id        message whose inline keyboard is active now (older buttons are stale)
    submitted         a lead was sent from this session and nothing new started since
    context           q_msg, resume_from, reply_kb, example_category, viewed_examples,
                      thanks_name, last_lead_id

Callback data: ``"<node_id>|<action>|<arg>"`` (≤ 64 bytes); actions: pick, toggle, done,
back, cat, skip, submit, edit, fix, tosum.
"""
from __future__ import annotations

import hmac
import html
import logging
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import scenario as sc
from . import validators
from .leads import (MAX_MESSAGE_LENGTH, answer_key, build_items, deliver_lead, format_summary,
                    lead_files, node_answer_text, session_path)
from .scenario import NODES, Node, Option
from .texts import TextStore

log = logging.getLogger(__name__)

CALLBACK_LIMIT = 64
ANSWER_PREVIEW_LIMIT = 1500  # "— Ваш ответ: …" under a question stays far below 4096
FINAL_NODES = sc.BRANCHES["final"]
CATEGORY_NODES = frozenset(n for n, node in NODES.items()
                           if node.done_key == "common.back_to_sections")
QUESTION_NODES = frozenset({"ns_what", "sections"} | CATEGORY_NODES
                           | {n for seq in sc.BRANCHES.values() for n in seq})
NAV_NODES = frozenset({"menu", "thanks", "ex_card", "ex_other", "ex_other_done", "order",
                       "contacts"})
ENTRY_FLOWS = {"ns_what": "new", "rd_platform": "redesign", "pr_what": "promo"}
ROOT_SCREENS = frozenset({"menu", "thanks", "contacts", "privacy"})
COMMANDS = frozenset({"start", "menu", "owner"})
REMOVE_REPLY_KEYBOARD = {"remove_keyboard": True}

Row = List[Tuple[str, str]]


def new_session() -> Dict[str, Any]:
    return {"accepted_privacy": False, "flow": None, "branch": None, "node": None,
            "stack": [], "answers": {}, "pending_inputs": [], "edit_mode": False,
            "message_id": None, "submitted": False, "context": {}}


def _parse_command(text: Optional[str]) -> Tuple[Optional[str], str]:
    if not text or not text.startswith("/"):
        return None, ""
    head, _, rest = text.strip().partition(" ")
    name = head[1:].split("@", 1)[0].lower()
    return (name, rest.strip()) if name in COMMANDS else (None, "")


def _cb(node_id: str, action: str, arg: str = "") -> str:
    data = f"{node_id}|{action}|{arg}"
    if len(data.encode("utf-8")) > CALLBACK_LIMIT:
        raise ValueError(f"callback data too long: {data}")
    return data


def _inline(rows: List[Row]) -> dict:
    return {"inline_keyboard": [[{"text": text, "callback_data": data} for text, data in row]
                                for row in rows]}


def _received_file(msg: Mapping[str, Any]) -> Optional[dict]:
    doc = msg.get("document")
    if doc:
        return {"file_id": doc.get("file_id"), "file_name": doc.get("file_name"),
                "mime": doc.get("mime_type"), "size": doc.get("file_size"), "kind": "document"}
    photos = msg.get("photo")
    if photos:
        best = photos[-1]
        return {"file_id": best.get("file_id"), "file_name": None, "mime": "image/jpeg",
                "size": best.get("file_size"), "kind": "photo"}
    return None


class _Turn:
    """One update of one chat: its session and the callback popup to answer with."""

    def __init__(self, chat_id: int, user: Mapping[str, Any], session: Dict[str, Any]):
        self.chat_id = chat_id
        self.user = dict(user or {})
        self.s = session
        self.popup: Optional[str] = None


class Engine:
    def __init__(self, storage, tg, texts: Optional[TextStore] = None,
                 public_base_url: str = ""):
        self.storage = storage
        self.tg = tg
        self.texts = texts or TextStore(storage)
        self.public_base_url = (public_base_url or "").rstrip("/")

    # -- entry point --------------------------------------------------------------------

    def handle_update(self, update: dict) -> None:
        chat_id = None
        try:
            update_id = update.get("update_id")
            log.info("Update %s received", update_id)
            if update_id is not None and not self.storage.mark_update_processed(int(update_id)):
                log.info("Update %s was already processed", update_id)
                return
            if update.get("callback_query"):
                query = update["callback_query"]
                chat_id = ((query.get("message") or {}).get("chat") or {}).get("id")
                self._on_callback(query)
            elif update.get("message"):
                chat_id = (update["message"].get("chat") or {}).get("id")
                self._on_message(update["message"])
        except Exception:
            log.exception("Failed to handle update %s",
                          update.get("update_id") if isinstance(update, dict) else None)
            if chat_id is not None:
                try:
                    self.tg.send_message(chat_id, self._esc("common.error"))
                except Exception:
                    log.exception("Could not report the error to the chat")

    # -- helpers ------------------------------------------------------------------------

    def _t(self, key: str, **vars: Any) -> str:
        return self.texts.get(key, **vars)

    def _esc(self, key: str, **vars: Any) -> str:
        return html.escape(self.texts.get(key, **vars))

    def _load(self, chat_id: int, user: Mapping[str, Any]) -> _Turn:
        session = new_session()
        stored = self.storage.load_session(chat_id) or {}
        session.update(stored)
        for key, kind in (("stack", list), ("answers", dict), ("pending_inputs", list),
                          ("context", dict)):
            if not isinstance(session.get(key), kind):
                session[key] = kind()
        return _Turn(chat_id, user, session)

    def _save(self, c: _Turn) -> None:
        self.storage.save_session(c.chat_id, c.s)

    def _send(self, c: _Turn, text: str, markup: Optional[dict] = None) -> Optional[int]:
        message = self.tg.send_message(c.chat_id, text, reply_markup=markup)
        return (message or {}).get("message_id")

    def _edit_text(self, c: _Turn, message_id: int, text: str) -> None:
        try:
            self.tg.edit_message_text(c.chat_id, message_id, text, reply_markup=None)
        except Exception as exc:  # an old message may be impossible to edit; not fatal
            log.warning("Could not edit message: %s", getattr(exc, "description", exc))

    def _drop_keyboard(self, c: _Turn) -> None:
        message_id = c.s.get("message_id")
        c.s["message_id"] = None
        if message_id:
            try:
                self.tg.edit_reply_markup(c.chat_id, message_id, reply_markup=None)
            except Exception as exc:
                log.warning("Could not remove keyboard: %s", getattr(exc, "description", exc))

    def _key(self, c: _Turn, node_id: str) -> str:
        return answer_key(c.s.get("branch"), node_id)

    def _answer(self, c: _Turn, node_id: str) -> Any:
        return c.s["answers"].get(self._key(c, node_id))

    def _selected(self, c: _Turn, node_id: str) -> List[str]:
        return sc.selected_option_ids(self._answer(c, node_id))

    def _mark(self, caption: str) -> str:
        return self._t("common.mark") + " " + caption

    def _username(self, c: _Turn) -> Optional[str]:
        return c.user.get("username")

    def _meta(self, c: _Turn) -> dict:
        return {"user_id": c.user.get("id"), "first_name": c.user.get("first_name"),
                "last_name": c.user.get("last_name"), "branch": c.s.get("branch"),
                "viewed_examples": c.s["context"].get("viewed_examples")}

    # -- messages -----------------------------------------------------------------------

    def _on_message(self, msg: Mapping[str, Any]) -> None:
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return
        command, arg = _parse_command(msg.get("text"))
        if command == "owner":
            self._bind_owner(chat_id, arg)
            return
        if chat.get("type", "private") != "private":
            return  # in the owner's group only /owner is handled
        c = self._load(chat_id, msg.get("from") or {})
        if command == "start":
            self._cmd_start(c)
        elif not c.s["accepted_privacy"]:
            self._show_privacy(c)
        elif command == "menu":
            self._go_menu(c)
        else:
            self._on_input(c, msg)
        self._save(c)

    def _bind_owner(self, chat_id: int, arg: str) -> None:
        code = (self.storage.get_setting("owner_bind_code") or "").strip().upper()
        given = (arg or "").strip().upper()
        if code and given and hmac.compare_digest(given.encode(), code.encode()):
            self.storage.set_setting("owner_chat_id", str(chat_id))
            self.storage.rotate_owner_bind_code()
            log.info("Owner chat bound")
            self.tg.send_message(chat_id, self._esc("owner.bound"))
        else:
            log.warning("Wrong owner bind code")
            self.tg.send_message(chat_id, self._esc("owner.bad_code"))

    def _cmd_start(self, c: _Turn) -> None:
        s = c.s
        if not s["accepted_privacy"]:
            self._show_privacy(c)
        elif s["node"] == "resume":
            self._render_resume(c)
        elif s["flow"] and s["answers"] and s["node"]:
            s["context"]["resume_from"] = s["node"]
            self._render_resume(c)
        else:
            self._go_menu(c)

    def _on_input(self, c: _Turn, msg: Mapping[str, Any]) -> None:
        s = c.s
        node_id = s["node"]
        if node_id not in NODES:
            return self._go_menu(c)
        if node_id in ("resume", "summary"):
            return self._error(c, "err.need_buttons")
        if s["pending_inputs"]:
            return self._on_field_input(c, msg)
        node = NODES[node_id]
        if node.kind == "phone":
            return self._on_phone(c, node, msg)
        if node.kind not in ("text", "name", "url"):
            return self._error(c, "err.need_buttons")
        text = msg.get("text")
        if text is None:
            return self._error(c, "err.need_text")
        check = {"text": validators.validate_free_text, "name": validators.validate_name,
                 "url": validators.validate_url}[node.kind]
        ok, value, error = check(text)
        if not ok:
            return self._error(c, error)
        if node_id == "ex_other":
            return self._examples_request(c, node, value)
        s["answers"][self._key(c, node_id)] = {"text": value}
        self._close_question(c, node)
        self._complete(c, node)

    def _on_phone(self, c: _Turn, node: Node, msg: Mapping[str, Any]) -> None:
        contact = msg.get("contact")
        if contact and contact.get("phone_number"):
            raw = str(contact["phone_number"]).strip()
            raw = raw if raw.startswith("+") else "+" + raw
        elif msg.get("text") is not None:
            raw = msg["text"]
        else:
            return self._error(c, "err.need_phone")
        ok, value, error = validators.validate_phone(raw)
        if not ok:
            return self._error(c, error)
        c.s["answers"][self._key(c, node.id)] = {"text": value}
        self._drop_keyboard(c)
        c.s["context"].pop("reply_kb", None)
        self._send(c, self._esc("fin_phone.received"), REMOVE_REPLY_KEYBOARD)
        self._complete(c, node)

    def _on_field_input(self, c: _Turn, msg: Mapping[str, Any]) -> None:
        entry = c.s["pending_inputs"][0]
        spec = self._spec(entry)
        if spec.kind == "file":
            received = _received_file(msg)
            if received is None:
                return self._error(c, "err.need_file")
            ok, name, error = validators.validate_file(received["file_name"], received["mime"],
                                                       received["size"], received["kind"])
            if not ok:
                return self._error(c, error)
            received["file_name"] = name
            fields = self._fields(c, entry)
            files = fields.get(spec.prompt_key) or []
            if not any(f.get("file_id") == received["file_id"] for f in files):
                files.append(received)
            fields[spec.prompt_key] = files
            self._drop_keyboard(c)
            node_id = c.s["node"]
            c.s["message_id"] = self._send(
                c, self._esc("common.file_received", file=name),
                _inline([[(self._t("common.done"), _cb(node_id, "done"))],
                         [(self._t("common.back"), _cb(node_id, "back"))]]))
            return
        text = msg.get("text")
        if text is None:
            return self._error(c, "err.need_text")
        check = {"text": validators.validate_free_text, "url": validators.validate_url,
                 "urls": validators.validate_urls}[spec.kind]
        ok, value, error = check(text)
        if not ok:
            return self._error(c, error)
        self._fields(c, entry)[spec.prompt_key] = value
        self._next_field(c)

    def _error(self, c: _Turn, key: str) -> None:
        markup = None
        if c.s["context"].get("reply_kb") and c.s["node"] != "fin_phone":
            c.s["context"].pop("reply_kb")
            markup = REMOVE_REPLY_KEYBOARD
        self._send(c, self._esc(key), markup)
        self._show_current(c)

    # -- callbacks ----------------------------------------------------------------------

    def _on_callback(self, query: Mapping[str, Any]) -> None:
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        c = None
        try:
            if chat_id is None or chat.get("type", "private") != "private":
                return
            c = self._load(chat_id, query.get("from") or {})
            self._route_callback(c, str(query.get("data") or ""), message.get("message_id"))
            self._save(c)
        finally:
            try:
                self.tg.answer_callback(query.get("id"), text=c.popup if c else None)
            except Exception as exc:
                log.warning("Could not answer callback: %s", getattr(exc, "description", exc))

    def _route_callback(self, c: _Turn, data: str, message_id: Optional[int]) -> None:
        node_id, action, arg = (data.split("|", 2) + ["", ""])[:3]
        s = c.s
        if node_id == "summary" and action == "submit" and s["submitted"] \
                and s["node"] != "summary":
            c.popup = self._t("common.already_sent")
            return
        if node_id != s["node"] or message_id is None or message_id != s["message_id"]:
            return self._stale(c)
        try:
            self._dispatch(c, node_id, action, arg)
        except KeyError:  # unknown option of a known node: a forged or outdated button
            self._stale(c)

    def _stale(self, c: _Turn) -> None:
        c.popup = self._t("common.button_inactive")
        self._show_current(c)

    def _dispatch(self, c: _Turn, node_id: str, action: str, arg: str) -> None:
        s = c.s
        if node_id == "resume":
            return self._resume_action(c, arg)
        if s["pending_inputs"]:
            return self._field_action(c, action)
        if node_id == "privacy":
            s["accepted_privacy"] = True
            return self._go_menu(c)
        if node_id == "summary":
            return self._summary_action(c, action, arg)
        if action == "back":
            return self._back(c)
        node = NODES[node_id]
        if node.kind == "hub":
            return self._hub_action(c, node, action, arg)
        if node_id == "ex_sphere" and action == "pick" and arg.startswith("c"):
            return self._open_category(c, arg[1:])
        if node.kind == "multi" and action == "done":
            return self._multi_done(c, node)
        opt = node.option(arg)
        if node.kind == "multi" and action == "toggle":
            return self._toggle(c, node, opt)
        if action == "pick":
            if node_id in NAV_NODES or node_id == "ex_sphere":
                return self._navigate(c, opt.next)
            return self._answer_single(c, node, opt)
        self._stale(c)

    # -- navigation ---------------------------------------------------------------------

    def _show_privacy(self, c: _Turn) -> None:
        c.s["stack"] = []
        self._render(c, "privacy")

    def _go_menu(self, c: _Turn) -> None:
        s = c.s
        s.update(stack=[], pending_inputs=[], edit_mode=False, flow=None)
        self._render(c, "menu")

    def _navigate(self, c: _Turn, target: Optional[str]) -> None:
        s = c.s
        if not target or target == "menu":
            return self._go_menu(c)
        source = s["node"]
        if target in s["stack"]:
            s["stack"] = s["stack"][:s["stack"].index(target)]
        elif source in ROOT_SCREENS:
            s["stack"] = ["menu"]
        else:
            s["stack"].append(source)
        if target in ENTRY_FLOWS:
            flow = ENTRY_FLOWS[target]
            s.update(flow=flow, submitted=False, edit_mode=False)
            if flow != "new":
                s["branch"] = flow
        self._render(c, target)

    def _back(self, c: _Turn) -> None:
        s = c.s
        if not s["stack"]:
            return self._go_menu(c)
        previous = s["stack"].pop()
        if previous == "menu":
            return self._go_menu(c)
        if previous == "summary":
            return self._show_edit_list(c) if s["edit_mode"] else self._show_summary(c)
        self._render(c, previous)

    def _show_current(self, c: _Turn) -> None:
        s = c.s
        node_id = s["node"]
        if node_id not in NODES:
            return self._go_menu(c) if s["accepted_privacy"] else self._show_privacy(c)
        if node_id == "resume":
            return self._render_resume(c)
        if node_id == "summary":
            return self._show_edit_list(c) if s["edit_mode"] else self._show_summary(c)
        if s["pending_inputs"]:
            return self._ask_field(c)
        self._render(c, node_id, repeat=True)

    # -- rendering ----------------------------------------------------------------------

    def _render(self, c: _Turn, node_id: str, repeat: bool = False) -> None:
        """Send the screen as a new message; ``repeat`` — the same question again (no intro)."""
        s = c.s
        self._drop_keyboard(c)
        s["node"] = node_id
        s["pending_inputs"] = []
        if node_id not in QUESTION_NODES:
            s["flow"] = None
        node = NODES[node_id]
        if node.intro_key and not (repeat and node_id == s["context"].get("intro_shown")):
            reply = None
            if s["context"].pop("reply_kb", None):
                reply = REMOVE_REPLY_KEYBOARD
            self._send(c, self._esc(node.intro_key), reply)
            s["context"]["intro_shown"] = node_id
        if node.kind == "phone":
            return self._render_phone(c, node)
        parts = self._card_parts(c) if node_id == "ex_card" else [self._screen_text(c, node)]
        for part in parts[:-1]:  # a long card: the buttons go under its last message
            self._send(c, part)
        message_id = self._send(c, parts[-1], _inline(self._keyboard(c, node)))
        s["message_id"] = message_id
        s["context"]["q_msg"] = message_id

    def _render_phone(self, c: _Turn, node: Node) -> None:
        share = node.option("share")
        reply = {"keyboard": [[{"text": self._t(share.text_key), "request_contact": True}]],
                 "resize_keyboard": True, "one_time_keyboard": True}
        self._send(c, self._question_html(node), reply)
        c.s["context"]["reply_kb"] = True
        c.s["context"]["q_msg"] = None
        c.s["message_id"] = self._send(
            c, self._esc("fin_phone.share_hint"),
            _inline([[(self._t("common.back"), _cb(node.id, "back"))]]))

    def _render_resume(self, c: _Turn) -> None:
        self._drop_keyboard(c)
        c.s["node"] = "resume"
        node = NODES["resume"]
        rows = [[(self._t(o.text_key), _cb("resume", "pick", o.id))] for o in node.options]
        c.s["message_id"] = self._send(c, self._question_html(node), _inline(rows))

    def _question_html(self, node: Node) -> str:
        text = "<b>" + self._esc(node.question_key) + "</b>"
        hint = node.hint_key or ("common.multi_hint" if node.kind == "multi" else None)
        if hint:
            text += "\n" + self._esc(hint)
        if node.footer_key:
            text += "\n\n" + self._esc(node.footer_key)
        return text

    def _screen_text(self, c: _Turn, node: Node) -> str:
        if node.id == "privacy":
            return self._privacy_html()
        if node.id == "contacts":
            return self._contacts_html()
        if node.id == "ex_card":
            return self._card_html(c)
        if node.id == "thanks":
            return self._esc("thanks.text", name=c.s["context"].get("thanks_name") or "")
        if node.kind == "info" or node.id == "menu":
            return self._esc(node.question_key)
        return self._question_html(node)

    def _privacy_html(self) -> str:
        url = (self.storage.get_setting("privacy_url") or "").strip()
        if not url and self.public_base_url:
            url = self.public_base_url + "/privacy"
        link = self._esc("privacy.link")
        if url.lower().startswith(("https://", "http://")):
            link = f'<a href="{html.escape(url, quote=True)}">{link}</a>'
        text = self._esc("privacy.text")
        return text.replace("{policy}", link) if "{policy}" in text else text + "\n" + link

    def _contacts_html(self) -> str:
        lines = [self._esc("contacts.text"), "", self._esc("contacts.header")]
        for key, setting in (("contacts.telegram", "contact_telegram"),
                             ("contacts.phone", "contact_phone"),
                             ("contacts.email", "contact_email")):
            value = (self.storage.get_setting(setting) or "").strip()
            if value:
                lines.append(self._esc(key, value=value))
        return "\n".join(lines)

    def _card_html(self, c: _Turn) -> str:
        return "\n".join(self._card_lines(c))

    def _card_parts(self, c: _Turn) -> List[str]:
        """The card as messages of at most 4096 characters, split between examples only.

        An example line is never cut (that could break its link); a single line longer than
        the limit is still sent alone — Telegram counts the text without tags and entities,
        and the panel limits a title and a note to far below 4096 visible characters.
        """
        parts: List[str] = []
        current = ""
        for line in self._card_lines(c):
            if current and len(current) + 1 + len(line) > MAX_MESSAGE_LENGTH:
                parts.append(current)
                current = line
            else:
                current = current + "\n" + line if current else line
        return parts + [current]

    def _card_lines(self, c: _Turn) -> List[str]:
        category = self._category(c.s["context"].get("example_category"))
        topic = category["topic"] if category else ""
        lines = [self._esc("ex_card.header", topic=topic), ""]
        items = self.storage.list_items(category["id"]) if category else []
        for item in items:
            title = html.escape(item["title"])
            url = (item.get("url") or "").strip()
            if url.lower().startswith(("https://", "http://")):
                title = f'<a href="{html.escape(url, quote=True)}">{title}</a>'
            line = "• " + title
            if item.get("is_demo"):
                line += " " + self._esc("ex_card.demo")
            if (item.get("note") or "").strip():
                line += " — " + html.escape(item["note"].strip())
            lines.append(line)
        if not items:
            lines.append(self._esc("ex_card.empty"))
        lines += ["", self._esc("ex_card.note")]
        return lines

    def _category(self, category_id: Any) -> Optional[dict]:
        try:
            return self.storage.get_category(int(category_id))
        except (TypeError, ValueError):
            return None

    def _has_back(self, node: Node) -> bool:
        return (node.id not in sc.NO_BACK_NODES and node.id not in CATEGORY_NODES
                and all(o.text_key != "common.back_to_spheres" for o in node.options))

    def _known_site_url(self, c: _Turn) -> Optional[str]:
        answer = c.s["answers"].get(answer_key("promo", "pr_what"))
        if "site" not in sc.selected_option_ids(answer):
            return None
        return (answer.get("fields") or {}).get("pr_what.site.prompt")

    def _keyboard(self, c: _Turn, node: Node) -> List[Row]:
        nid = node.id
        rows: List[Row] = []
        if node.kind == "hub":
            branch = c.s["branch"]
            for opt in sc.hub_options(branch):
                caption = self._t(opt.text_key)
                if opt.next:
                    count = len(sc.selected_option_ids(
                        c.s["answers"].get(answer_key(branch, opt.next))))
                    if count:
                        caption = self._t("sections.cat_selected", label=caption, count=count)
                    rows.append([(caption, _cb(nid, "cat", opt.id))])
                else:
                    if self._hub_other(c):
                        caption = self._mark(caption)
                    rows.append([(caption, _cb(nid, "pick", opt.id))])
            rows.append([(self._t("common.done"), _cb(nid, "done"))])
        elif node.kind == "multi":
            selected = self._selected(c, nid)
            for opt in node.options:
                caption = self._t(opt.text_key)
                rows.append([(self._mark(caption) if opt.id in selected else caption,
                              _cb(nid, "toggle", opt.id))])
            rows.append([(self._t(node.done_key or "common.done"), _cb(nid, "done"))])
        else:
            if nid == "ex_sphere":
                for cat in self.storage.list_categories(active_only=True):
                    rows.append([(cat["button"], _cb(nid, "pick", f"c{cat['id']}"))])
            selected = self._selected(c, nid) if nid in QUESTION_NODES else []
            for opt in node.options:
                if nid == "pr_link" and opt.id == "use":
                    url = self._known_site_url(c)
                    if not url:
                        continue
                    caption = self._t(opt.text_key, url=url)
                else:
                    caption = self._t(opt.text_key)
                rows.append([(self._mark(caption) if opt.id in selected else caption,
                              _cb(nid, "pick", opt.id))])
        if self._has_back(node):
            rows.append([(self._t("common.back"), _cb(nid, "back"))])
        return rows

    def _close_question(self, c: _Turn, node: Node) -> None:
        """Write the answer under the answered question and remove its buttons."""
        message_id = c.s["context"].pop("q_msg", None)
        if not message_id:
            return self._drop_keyboard(c)
        answer = node_answer_text(c.s, node.id, self.texts)
        if len(answer) > ANSWER_PREVIEW_LIMIT:  # the full answer is in the summary
            answer = answer[:ANSWER_PREVIEW_LIMIT].rstrip() + "…"
        text = self._screen_text(c, node)
        if answer:
            text += "\n\n<i>" + html.escape(self._t("common.your_answer") + " " + answer) + "</i>"
        self._edit_text(c, message_id, text)
        if c.s["message_id"] == message_id:
            c.s["message_id"] = None

    # -- answering ----------------------------------------------------------------------

    def _answer_single(self, c: _Turn, node: Node, opt: Option) -> None:
        key = self._key(c, node.id)
        previous = c.s["answers"].get(key)
        keep = isinstance(previous, dict) and sc.selected_option_ids(previous) == [opt.id]
        answer: Dict[str, Any] = {"option": opt.id,
                                  "fields": dict(previous.get("fields") or {}) if keep else {}}
        if node.id == "pr_link" and opt.id == "use":
            url = self._known_site_url(c)
            if not url:
                return self._stale(c)
            answer["text"] = url
        c.s["answers"][key] = answer
        self._close_question(c, node)
        if opt.inputs:
            return self._queue_inputs(c, node, [opt])
        self._complete(c, node)

    def _toggle(self, c: _Turn, node: Node, opt: Option) -> None:
        key = self._key(c, node.id)
        previous = c.s["answers"].get(key)
        fields = dict(previous.get("fields") or {}) if isinstance(previous, dict) else {}
        selected = sc.selected_option_ids(previous)
        if opt.id in selected:
            selected.remove(opt.id)
        elif opt.exclusive:
            selected = [opt.id]
        else:
            selected = [x for x in selected if not node.option(x).exclusive] + [opt.id]
        order = [o.id for o in node.options]
        selected.sort(key=order.index)
        c.s["answers"][key] = {"selected": selected, "fields": fields}
        try:
            self.tg.edit_reply_markup(c.chat_id, c.s["message_id"],
                                      reply_markup=_inline(self._keyboard(c, node)))
        except Exception as exc:
            log.warning("Could not update marks: %s", getattr(exc, "description", exc))
            self._render(c, node.id)

    def _multi_done(self, c: _Turn, node: Node) -> None:
        selected = self._selected(c, node.id)
        if not selected and node.id not in CATEGORY_NODES:
            c.popup = self._t("err.empty_multi")
            return
        self._close_question(c, node)
        with_inputs = [node.option(x) for x in selected if node.option(x).inputs]
        if with_inputs:
            return self._queue_inputs(c, node, with_inputs)
        self._complete(c, node)

    def _hub_other(self, c: _Turn) -> Optional[str]:
        hub = c.s["answers"].get(self._key(c, "sections")) or {}
        return (hub.get("fields") or {}).get("sections.other.prompt")

    def _hub_action(self, c: _Turn, node: Node, action: str, arg: str) -> None:
        s = c.s
        if action == "cat":
            opt = node.option(arg)
            if opt.next not in sc.hub_categories(s["branch"]):
                return self._stale(c)
            s["stack"].append(node.id)
            return self._render(c, opt.next)
        if action == "pick":
            opt = node.option(arg)
            if not opt.inputs:  # only "Иное" is picked; categories open with "cat"
                return self._stale(c)
            s["answers"].setdefault(self._key(c, node.id), {"fields": {}})
            return self._queue_inputs(c, node, [opt])
        if action == "done":
            has_marks = any(sc.selected_option_ids(s["answers"].get(self._key(c, cat)))
                            for cat in sc.hub_categories(s["branch"]))
            if not has_marks and not self._hub_other(c):
                c.popup = self._t("err.empty_multi")
                return
            s["answers"].setdefault(self._key(c, node.id), {"fields": {}})["done"] = True
            self._close_question(c, node)
            self._send(c, self._esc(sc.HUB_DONE_MESSAGE_KEY))
            return self._complete(c, node)
        self._stale(c)

    def _open_category(self, c: _Turn, raw_id: str) -> None:
        category = self._category(raw_id)
        if not category or not category["active"]:
            return self._stale(c)
        c.s["context"]["example_category"] = category["id"]
        c.s["context"]["viewed_examples"] = category["button"]
        self._navigate(c, "ex_card")

    def _examples_request(self, c: _Turn, node: Node, text: str) -> None:
        name = " ".join(x for x in (c.user.get("first_name"), c.user.get("last_name")) if x)
        lead_id = self.storage.create_lead(
            "examples", c.chat_id, self._username(c), name or None, None,
            [{"title": self._t("lead.request"), "answer": text}], [], self._meta(c))
        self._close_question(c, node)
        self._deliver(lead_id)
        self._navigate(c, "ex_other_done")

    def _deliver(self, lead_id: int) -> None:
        try:
            deliver_lead(lead_id, self.storage, self.tg, self.texts)
        except Exception:  # the lead is saved; the panel shows it and can resend it
            log.exception("Delivery of lead %s failed", lead_id)

    # -- "→ поле" inputs ----------------------------------------------------------------

    def _spec(self, entry: Mapping[str, str]):
        opt = NODES[entry["node"]].option(entry["option"])
        return next(spec for spec in opt.inputs if spec.prompt_key == entry["field"])

    def _fields(self, c: _Turn, entry: Mapping[str, str]) -> dict:
        answer = c.s["answers"].setdefault(self._key(c, entry["node"]), {})
        return answer.setdefault("fields", {})

    def _queue_inputs(self, c: _Turn, node: Node, options: List[Option]) -> None:
        c.s["pending_inputs"] = [{"node": node.id, "option": o.id, "field": spec.prompt_key}
                                 for o in options for spec in o.inputs]
        self._ask_field(c)

    def _ask_field(self, c: _Turn) -> None:
        entry = c.s["pending_inputs"][0]
        spec = self._spec(entry)
        node_id = c.s["node"]
        self._drop_keyboard(c)
        rows: List[Row] = []
        if spec.optional:
            rows.append([(self._t("common.skip"), _cb(node_id, "skip"))])
        if spec.kind == "file":
            rows.append([(self._t("common.done"), _cb(node_id, "done"))])
        rows.append([(self._t("common.back"), _cb(node_id, "back"))])
        c.s["message_id"] = self._send(c, self._esc(spec.prompt_key), _inline(rows))

    def _field_action(self, c: _Turn, action: str) -> None:
        s = c.s
        entry = s["pending_inputs"][0]
        spec = self._spec(entry)
        if action == "back":
            s["pending_inputs"] = []
            return self._render(c, s["node"])
        if action == "skip" and spec.optional:
            self._fields(c, entry)[spec.prompt_key] = None
            return self._next_field(c)
        if action == "done" and spec.kind == "file":
            if not self._fields(c, entry).get(spec.prompt_key):
                c.popup = self._t("err.no_files")
                return
            return self._next_field(c)
        self._stale(c)

    def _next_field(self, c: _Turn) -> None:
        s = c.s
        self._drop_keyboard(c)
        s["pending_inputs"].pop(0)
        if s["pending_inputs"]:
            return self._ask_field(c)
        node = NODES[s["node"]]
        if node.kind == "hub":
            return self._render(c, node.id)
        self._complete(c, node)

    # -- moving on ----------------------------------------------------------------------

    def _is_answered(self, c: _Turn, node_id: str) -> bool:
        answer = self._answer(c, node_id)
        if NODES[node_id].kind == "hub":
            return bool(isinstance(answer, dict) and answer.get("done"))
        if isinstance(answer, dict) and answer.get("text"):
            return True
        return bool(sc.selected_option_ids(answer))

    def _complete(self, c: _Turn, node: Node) -> None:
        """The node and its fields are answered: go to the next screen."""
        s = c.s
        if node.id in CATEGORY_NODES:
            if s["stack"] and s["stack"][-1] == "sections":
                s["stack"].pop()
            return self._render(c, "sections")
        if node.id == "ns_what":
            chosen = next((x for x in self._selected(c, "ns_what") if x in sc.BRANCHES), None)
            s.update(branch=chosen, flow="new")
        if s["edit_mode"]:
            following = next((n for n in session_path(s) if not self._is_answered(c, n)), None)
        elif node.id == "ns_what":
            following = sc.next_node(sc.NEW_SITE, "ns_what", s["answers"])
        else:
            following = sc.next_node(s["branch"], node.id, s["answers"])
        s["stack"].append(node.id)
        if following is None:
            return self._show_summary(c)
        self._render(c, following)

    # -- summary, edit, submit ----------------------------------------------------------

    def _lead_kind(self, c: _Turn) -> str:
        return sc.LEAD_KIND_BY_BRANCH.get(c.s["branch"] or "", "new")

    def _show_summary(self, c: _Turn) -> None:
        s = c.s
        self._drop_keyboard(c)
        s.update(node="summary", edit_mode=False, pending_inputs=[])
        parts = format_summary(build_items(s, self.texts), self.texts, kind=self._lead_kind(c),
                               files_count=len(lead_files(s)))
        for part in parts[:-1]:
            self._send(c, part)
        rows = [[(self._t("summary.opt.submit"), _cb("summary", "submit"))],
                [(self._t("summary.opt.edit"), _cb("summary", "edit"))]]
        s["message_id"] = self._send(c, parts[-1], _inline(rows))

    def _show_edit_list(self, c: _Turn) -> None:
        s = c.s
        self._drop_keyboard(c)
        s.update(node="summary", edit_mode=True, pending_inputs=[], stack=[])
        rows = [[(self._t(NODES[n].title_key), _cb("summary", "fix", n))]
                for n in session_path(s)]
        rows.append([(self._t("summary.back"), _cb("summary", "tosum"))])
        s["message_id"] = self._send(c, "<b>" + self._esc("summary.edit_question") + "</b>",
                                     _inline(rows))

    def _summary_action(self, c: _Turn, action: str, arg: str) -> None:
        s = c.s
        if action == "submit" and not s["edit_mode"]:
            return self._submit(c)
        if action == "edit":
            return self._show_edit_list(c)
        if action == "tosum":
            return self._show_summary(c)
        if action == "fix" and s["edit_mode"] and arg in session_path(s):
            s["stack"] = ["summary"]
            return self._render(c, arg)
        self._stale(c)

    def _submit(self, c: _Turn) -> None:
        s = c.s
        if s["submitted"]:
            c.popup = self._t("common.already_sent")
            return
        answers = s["answers"]
        name = (answers.get(answer_key(None, "fin_name")) or {}).get("text")
        phone = (answers.get(answer_key(None, "fin_phone")) or {}).get("text")
        lead_id = self.storage.create_lead(
            self._lead_kind(c), c.chat_id, self._username(c), name, phone,
            build_items(s, self.texts), lead_files(s), self._meta(c))
        log.info("Lead %s saved", lead_id)
        old_message = s["message_id"]
        # persist "submitted" right away: a later Telegram failure must not allow a second lead
        s.update(answers={}, flow=None, branch=None, stack=[], pending_inputs=[],
                 edit_mode=False, submitted=True, node="thanks", message_id=None,
                 context={"thanks_name": name or "", "last_lead_id": lead_id})
        self._save(c)
        s["message_id"] = old_message
        self._drop_keyboard(c)
        self._deliver(lead_id)
        self._render(c, "thanks")

    def _resume_action(self, c: _Turn, arg: str) -> None:
        s = c.s
        if arg == "continue":
            s["node"] = s["context"].pop("resume_from", None)
            return self._show_current(c)
        if arg == "restart":
            s.update(answers={}, flow=None, branch=None, stack=[], pending_inputs=[],
                     edit_mode=False, submitted=False, context={})
            return self._go_menu(c)
        self._stale(c)
