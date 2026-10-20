"""Lead content: answers of the path as items, the summary, the owner's message, delivery.

Answers live in the session under ``answer_key(branch, node_id)``:

* single question: ``{"option": id, "fields": {prompt_key: value}}``;
* multi question / hub category: ``{"selected": [ids], "fields": {...}}``;
* typed answer (text, name, phone, url): ``{"text": value}`` (``pr_link`` "use" button keeps
  ``"option": "use"`` next to the link);
* hub ``sections``: ``{"fields": {"sections.other.prompt": text}, "done": bool}``, the marks
  are in the answers of its categories.

A field value is a string, ``None`` for a skipped optional field, or a list of file dicts
for a file field. Every text the owner or client sees comes from ``TextStore``; everything
inserted into HTML is escaped here.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .scenario import (BRANCHES, NEW_SITE, NODES, hub_categories, question_path,
                       selected_option_ids)

log = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 4096
FINAL_PREFIX = "final"
# shown in the admin panel as the delivery error
NO_RECIPIENT_ERROR = "Получатель заявок не привязан: отправьте боту /owner КОД из панели"

try:
    from zoneinfo import ZoneInfo

    MOSCOW = ZoneInfo("Europe/Moscow")
except Exception:  # no tz database on the host: Moscow has no DST since 2014
    MOSCOW = timezone(timedelta(hours=3), "MSK")

Block = Tuple[str, str]  # (head HTML that is never split, escaped body that may be split)


def answer_key(branch: Optional[str], node_id: str) -> str:
    """Session key of a node's answer: ``new/ns_what``, ``final/<node>`` or ``<branch>/<node>``."""
    if node_id == "ns_what":
        return f"{NEW_SITE}/ns_what"
    if node_id in BRANCHES["final"]:
        return f"{FINAL_PREFIX}/{node_id}"
    return f"{branch}/{node_id}"


def _field_text(spec, value: Any, texts) -> Optional[str]:
    if spec.kind == "file":
        names = [f.get("file_name") or f.get("file_id", "") for f in (value or [])]
        return ", ".join(names) or None
    if value is None:
        return texts.get("summary.skipped") if spec.optional else None
    return str(value).replace("\n", ", ")


def answer_text(node_id: str, answer: Any, texts) -> str:
    """Readable answer of a question node (not the hub): options, their fields, typed text."""
    if answer is None:
        return ""
    if isinstance(answer, Mapping) and answer.get("text") is not None:
        return str(answer["text"])
    node = NODES[node_id]
    fields = answer.get("fields", {}) if isinstance(answer, Mapping) else {}
    parts = []
    for option_id in selected_option_ids(answer):
        try:
            opt = node.option(option_id)
        except KeyError:
            continue
        caption = texts.get(opt.text_key)
        values = [v for v in (_field_text(spec, fields.get(spec.prompt_key), texts)
                              for spec in opt.inputs if spec.prompt_key in fields) if v]
        parts.append(caption + (": " + " — ".join(values) if values else ""))
    return "; ".join(parts)


def hub_answer_text(branch: str, answers: Mapping[str, Any], texts) -> str:
    """Sections hub: one line per category with marks, then "Иное"."""
    lines = []
    for category in hub_categories(branch):
        text = answer_text(category, answers.get(answer_key(branch, category)), texts)
        if text:
            lines.append(texts.get(NODES[category].question_key) + " " + text.replace("; ", ", "))
    hub = answers.get(answer_key(branch, "sections")) or {}
    other = (hub.get("fields") or {}).get("sections.other.prompt")
    if other:
        lines.append(texts.get("sections.opt.other") + ": " + other)
    return "\n".join(lines)


def node_answer_text(session: Mapping[str, Any], node_id: str, texts) -> str:
    branch = session.get("branch")
    answers = session.get("answers") or {}
    if NODES[node_id].kind == "hub":
        return hub_answer_text(branch, answers, texts)
    return answer_text(node_id, answers.get(answer_key(branch, node_id)), texts)


def session_path(session: Mapping[str, Any]) -> Tuple[str, ...]:
    branch = session.get("branch")
    if not branch or branch not in BRANCHES:
        return ()
    return question_path(branch, session.get("answers") or {})


def build_items(session: Mapping[str, Any], texts) -> List[Dict[str, str]]:
    """Questions of the current path in order (+ «Смотрел примеры»), then the contacts."""
    questions, contacts = [], []
    for node_id in session_path(session):
        text = node_answer_text(session, node_id, texts)
        if not text:
            continue
        item = {"title": texts.get(NODES[node_id].title_key), "answer": text}
        (contacts if node_id in BRANCHES["final"] else questions).append(item)
    viewed = (session.get("context") or {}).get("viewed_examples")
    if viewed:
        questions.append({"title": texts.get("summary.viewed_examples"), "answer": viewed})
    return questions + contacts


def lead_files(session: Mapping[str, Any]) -> List[dict]:
    """TZ files attached on the current path, in order."""
    branch = session.get("branch")
    answers = session.get("answers") or {}
    files: List[dict] = []
    for node_id in session_path(session):
        answer = answers.get(answer_key(branch, node_id))
        if not isinstance(answer, Mapping):
            continue
        fields = answer.get("fields") or {}
        for option_id in selected_option_ids(answer):
            try:
                opt = NODES[node_id].option(option_id)
            except KeyError:
                continue
            for spec in opt.inputs:
                if spec.kind == "file":
                    files.extend(fields.get(spec.prompt_key) or [])
    return files


# -- HTML messages ------------------------------------------------------------------------


def _esc(text: str) -> str:
    return html.escape(text)


def _bold(text: str) -> str:
    return "<b>" + _esc(text) + "</b>"


def _cut(body: str, room: int) -> Tuple[str, str]:
    """Split escaped text at a space/new line, never inside an HTML entity."""
    if len(body) <= room:
        return body, ""
    cut = body.rfind("\n", 0, room)
    if cut <= 0:
        cut = body.rfind(" ", 0, room)
    if cut <= 0:
        cut = room
    amp = body.rfind("&", 0, cut)
    if amp > 0 and ";" not in body[amp:cut]:
        cut = amp
    return body[:cut], body[cut:].lstrip(" \n")


def split_blocks(blocks: Sequence[Block], limit: int = MAX_MESSAGE_LENGTH) -> List[str]:
    """Join blocks with new lines into messages of at most ``limit`` characters."""
    parts: List[str] = []
    current = ""

    def push(piece: str) -> None:
        nonlocal current
        if not current:
            current = piece
        elif len(current) + 1 + len(piece) <= limit:
            current += "\n" + piece
        else:
            parts.append(current)
            current = piece

    for head, body in blocks:
        if len(head) + len(body) <= limit:
            push(head + body)
            continue
        first = True
        while body:
            chunk, body = _cut(body, limit - (len(head) if first else 0))
            push((head if first else "") + chunk)
            first = False
    if current:
        parts.append(current)
    return parts


def _item_blocks(items: Sequence[Mapping[str, str]]) -> List[Block]:
    return [(_bold(str(i["title"])) + ": ", _esc(str(i["answer"]))) for i in items]


def _kind_text(kind: str, texts) -> str:
    try:
        return texts.get(f"lead.kind.{kind}")
    except KeyError:
        return kind


def format_summary(items: Sequence[Mapping[str, str]], texts, kind: Optional[str] = None,
                   files_count: int = 0) -> List[str]:
    """«Проверьте заявку»: kind, answers, contacts, files mark; parts ≤ 4096 characters."""
    blocks: List[Block] = [(_bold(texts.get("summary.header")), "")]
    if kind:
        blocks.append((_bold(texts.get("summary.kind")) + ": ", _esc(_kind_text(kind, texts))))
    blocks += _item_blocks(items)
    if files_count:
        blocks.append((_bold(texts.get("summary.files")) + ": ",
                       _esc(texts.get("summary.files_value", count=files_count))))
    return split_blocks(blocks)


def _moscow_time(created_at: Optional[str]) -> str:
    try:
        moment = datetime.fromisoformat(created_at or "")
    except ValueError:
        return created_at or ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(MOSCOW).strftime("%d.%m.%Y %H:%M")


def _client_html(lead: Mapping[str, Any], texts) -> str:
    meta = lead.get("meta") or {}
    try:
        user_id = int(meta.get("user_id") or lead.get("chat_id"))
    except (TypeError, ValueError):
        user_id = None
    shown = " ".join(x for x in (meta.get("first_name"), meta.get("last_name")) if x) \
        or lead.get("name") or (str(user_id) if user_id is not None else "")
    link = f'<a href="tg://user?id={user_id}">{_esc(shown)}</a>' if user_id is not None \
        else _esc(shown)
    username = lead.get("username")
    handle = "@" + username if username else texts.get("lead.no_username")
    return (link + " " if shown else "") + "(" + _esc(handle) + ")"


def format_lead_for_owner(lead: Mapping[str, Any], texts) -> List[str]:
    """Full lead for the owner's chat: number, Moscow date, kind, client, answers, files."""
    blocks: List[Block] = [
        (_bold(texts.get("lead.header", id=lead["id"])), ""),
        (_bold(texts.get("lead.date")) + ": ", _esc(_moscow_time(lead.get("created_at")))),
        (_bold(texts.get("lead.kind")) + ": ", _esc(_kind_text(lead.get("kind", ""), texts))),
        (_bold(texts.get("lead.client")) + ": " + _client_html(lead, texts), ""),
        (_bold(texts.get("lead.answers")), ""),
    ]
    blocks += _item_blocks(lead.get("items") or [])
    files = lead.get("files") or []
    if files:
        blocks.append((_esc(texts.get("lead.files", count=len(files))), ""))
    return split_blocks(blocks)


def _owner_chat(value: str) -> Any:
    value = value.strip()
    return int(value) if value.lstrip("-").isdigit() else value


def deliver_lead(lead_id: int, storage, tg, texts) -> bool:
    """Send the lead and its files to the bound owner chat; record the result on the lead."""
    lead = storage.get_lead(lead_id)
    if lead is None:
        return False
    owner = (storage.get_setting("owner_chat_id") or "").strip()
    if not owner:
        log.warning("Lead %s not delivered: no recipient bound", lead_id)
        storage.set_lead_delivery(lead_id, False, NO_RECIPIENT_ERROR)
        return False
    chat_id = _owner_chat(owner)
    try:
        for part in format_lead_for_owner(lead, texts):
            tg.send_message(chat_id, part)
        caption = _esc(texts.get("lead.file_caption", id=lead_id))
        for f in lead.get("files") or []:
            if f.get("kind") == "photo":
                tg.send_photo(chat_id, f["file_id"], caption=caption)
            else:
                tg.send_document(chat_id, f["file_id"], caption=caption)
    except Exception as exc:  # TelegramError or a network failure: keep the lead, report
        reason = getattr(exc, "description", None) or type(exc).__name__
        log.warning("Lead %s not delivered: %s", lead_id, reason)
        storage.set_lead_delivery(lead_id, False, reason)
        return False
    storage.set_lead_delivery(lead_id, True, None)
    log.info("Lead %s delivered", lead_id)
    return True
