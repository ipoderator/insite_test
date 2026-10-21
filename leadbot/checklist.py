"""Checklist for the client after a lead is saved: what to prepare for the specialist's call.

Items are chosen by the answers of the current path (``RULES``); their wording is the
``checklist.<item>`` texts, editable in the panel. A rule is a list of ``(node, options)``
conditions: the item is shown when any of them holds — the node is on the client's path and
one of the options is selected. Answers the path no longer goes through (another branch
chosen in «Исправить», questions skipped by ``final_jump``) never count.

The checklist only suggests what to prepare: no deadlines, prices or promised results, and
it does not repeat the summary. Nothing matched (too few answers, no branch) → the neutral
list ``NEUTRAL``.
"""
from __future__ import annotations

from typing import Any, List, Mapping, Sequence, Tuple

from .leads import Block, _bold, _esc, answer_key, session_path, split_blocks
from .scenario import selected_option_ids

Condition = Tuple[str, Tuple[str, ...]]  # (node id, option ids)

# item id → conditions; display order, the most useful first
RULES: Tuple[Tuple[str, Tuple[Condition, ...]], ...] = (
    # new site
    ("pay", (("pay", ("yes",)),)),
    ("delivery", (("delivery", ("yes",)),)),
    ("stock", (("stock", ("yes",)),)),
    ("crm", (("crm", ("yes",)),)),
    ("products_file", (("product_info", ("excel", "file")),)),
    ("products_parse", (("product_info", ("parse",)),)),
    ("content", (("content", ("all", "partial")),)),
    ("logo", (("design", ("has_logo",)),)),
    ("features", (("features", ("subscribe", "coupons", "account", "one_click", "search",
                                "quiz", "consultant", "calculator", "city", "other")),)),
    # redesign
    ("access", (("rd_access", ("all", "domain", "hosting")),)),
    ("access_lost", (("rd_access", ("lost", "none")),)),
    ("site_forgot", (("rd_link", ("forgot",)),)),
    ("site_broken", (("rd_link", ("broken",)),)),
    ("bugs", (("rd_what", ("bugs", "layout")),)),
    ("changes", (("rd_what", ("pages", "features")),)),
    ("migrate", (("rd_what", ("migrate",)),)),
    # promotion
    ("ad_history", (("pr_current", ("paid", "past")),)),
    ("cards", (("pr_what", ("gis", "ybiz", "ymaps", "yserv")),)),
    ("geo", (("pr_geo", ("city", "regions")),)),
    ("analytics", (("pr_what", ("site",)),
                   ("rd_promoted", ("direct", "ybiz", "seo", "other")))),
    # both
    ("examples", (("example", ("no",)), ("rd_what", ("visual",)))),
)
NEUTRAL = ("neutral_goal", "neutral_refs")
CLOSING = "questions"  # last item of every checklist
MAX_ITEMS = 6  # chosen items shown at most, before the closing one


def checklist_items(session: Mapping[str, Any]) -> List[str]:
    """Item ids for the session's branch and answers, closing item included."""
    branch = session.get("branch")
    answers = session.get("answers") or {}
    path = set(session_path(session))
    chosen = [item for item, conditions in RULES
              if any(node in path
                     and set(options) & set(selected_option_ids(
                         answers.get(answer_key(branch, node))))
                     for node, options in conditions)]
    return (chosen[:MAX_ITEMS] or list(NEUTRAL)) + [CLOSING]


def format_checklist(items: Sequence[str], texts) -> List[str]:
    """Header and one «•» line per item; parts ≤ 4096 characters."""
    blocks: List[Block] = [(_bold(texts.get("checklist.header")), "")]
    blocks += [("• ", _esc(texts.get(f"checklist.{item}"))) for item in items]
    return split_blocks(blocks)
