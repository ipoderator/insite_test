"""Dialogue graph as data: nodes, options, branches, transitions.

The engine executes only this data and the texts of ``leadbot.texts``; every key below is a
``TEXTS`` key.

Conventions the engine relies on:

* Answers are stored by ``f"{branch}/{node_id}"``. ``ns_what`` is answered before a branch
  exists, under the pseudo-branch ``NEW_SITE`` ("new"). An answer is an option id (single),
  a list of option ids (multi) or a dict with ``"option"`` / ``"selected"`` plus the
  engine's own fields (``selected_option_ids`` reads all three shapes).
* ``Option.next`` — where the option leads outside the question flow (menus, examples,
  contacts, hub categories, the first node of a branch). Inside a branch the order is
  ``BRANCHES`` + ``next_node``; an option of a question node has ``next=None``.
* ``Node.branching`` — the answer changes the path after this node (``ns_what``: option id
  is the new branch; ``pr_current``: ``final_jump`` options skip to ``FINAL``). Editing such
  a node from the summary rebuilds the path (``question_path``).
* ``Option.inputs`` — the "→ поле" fields asked after the choice (for a multi node: after
  "Готово", one by one, in option order). Several fields per option are possible
  (``example``/``yes``: link, then an optional comment with a "Пропустить" button).
* ``Option.exclusive`` — selecting it clears the other marks of the node, and selecting any
  other option clears it. ``Option.final_jump`` — selecting it ends the branch: next is
  ``FINAL``.
* ``Node.done_key`` — label of the button that finishes a multi / file / hub node
  (``None`` → ``common.done``); hub categories finish with ``common.back_to_sections``.
* ``Node.intro_key`` — info block sent before the question; ``Node.footer_key`` — comment
  under the question; ``Node.hint_key`` — hint under the question.
* Hub ``sections``: ``hub_options(branch)`` gives the category buttons of the branch's
  variant (``Option.next`` = category node) plus "Иное" (text field). A category is a multi
  node whose marks are saved on every toggle; it closes back to the hub. Hub "Готово"
  answers with ``HUB_DONE_MESSAGE_KEY`` and continues the branch.
* ``NO_BACK_NODES`` — screens that get no generic "⬅️ Назад" (they have own navigation).
* ``url`` / ``phone`` nodes may have options: buttons that answer instead of typing
  (``pr_link``/``use`` shows only when a site link is known and its text takes ``{url}``;
  ``fin_phone``/``share`` is a reply-keyboard "request_contact" button).
* ``ex_sphere`` lists the example categories from the database before its own options.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Literal, Mapping, Optional, Tuple

InputKind = Literal["text", "url", "urls", "file"]
NodeKind = Literal["single", "multi", "text", "url", "file", "hub", "info", "phone", "name"]


@dataclass(frozen=True)
class InputSpec:
    kind: InputKind
    prompt_key: str  # unique per field: also the key of the field's value in the answer
    optional: bool = False


@dataclass(frozen=True)
class Option:
    id: str
    text_key: str
    next: Optional[str] = None
    input: Optional[InputSpec] = None
    exclusive: bool = False
    final_jump: bool = False
    extra_inputs: Tuple[InputSpec, ...] = ()

    @property
    def inputs(self) -> Tuple[InputSpec, ...]:
        """All "→ поле" fields of the option, in the order they are asked."""
        return ((self.input,) if self.input else ()) + self.extra_inputs


@dataclass(frozen=True)
class Node:
    id: str
    kind: NodeKind
    question_key: str
    title_key: Optional[str]
    options: Tuple[Option, ...] = ()
    hint_key: Optional[str] = None
    footer_key: Optional[str] = None
    done_key: Optional[str] = None
    branching: bool = False
    intro_key: Optional[str] = None

    def option(self, option_id: str) -> Option:
        for opt in self.options:
            if opt.id == option_id:
                return opt
        raise KeyError(f"{self.id}/{option_id}")


NEW_SITE = "new"
FINAL = "fin_name"
HUB_DONE_MESSAGE_KEY = "sections.done_message"

NODES: Dict[str, Node] = {}


def _add(node: Node) -> None:
    if node.id in NODES:
        raise ValueError(f"duplicate node {node.id}")
    NODES[node.id] = node


def _text(node: str, opt: str) -> InputSpec:
    return InputSpec("text", f"{node}.{opt}.prompt")


def _options(node: str, ids: Iterable[str], inputs: Optional[Mapping[str, InputSpec]] = None,
             exclusive: Iterable[str] = (), final_jump: Iterable[str] = ()) -> Tuple[Option, ...]:
    inputs = inputs or {}
    exclusive, final_jump = set(exclusive), set(final_jump)
    return tuple(Option(id=i, text_key=f"{node}.opt.{i}", input=inputs.get(i),
                        exclusive=i in exclusive, final_jump=i in final_jump) for i in ids)


def _question(node_id: str, kind: NodeKind, ids: Iterable[str] = (), *,
              inputs: Optional[Mapping[str, InputSpec]] = None, exclusive: Iterable[str] = (),
              final_jump: Iterable[str] = (), hint: bool = False, **extra: Any) -> None:
    _add(Node(id=node_id, kind=kind, question_key=f"{node_id}.question",
              title_key=f"{node_id}.title",
              options=_options(node_id, ids, inputs, exclusive, final_jump),
              hint_key=f"{node_id}.hint" if hint else None, **extra))


YES_NO = ("yes", "no")

# -- entry, menu ------------------------------------------------------------------------

_add(Node("privacy", "info", "privacy.text", None,
          options=(Option("start", "privacy.start", next="menu"),)))
_add(Node("menu", "single", "menu.text", None, options=(
    Option("new", "menu.opt.new", next="ns_what"),
    Option("redesign", "menu.opt.redesign", next="rd_platform"),
    Option("promo", "menu.opt.promo", next="pr_what"),
    Option("examples", "menu.opt.examples", next="ex_sphere"),
    Option("contacts", "menu.opt.contacts", next="contacts"),
)))
_add(Node("resume", "single", "resume.question", None, options=(
    Option("continue", "resume.opt.continue"),
    Option("restart", "resume.opt.restart"),
)))

# -- new site ---------------------------------------------------------------------------

_add(Node("ns_what", "single", "ns_what.question", "ns_what.title", branching=True, options=(
    Option("goods", "ns_what.opt.goods", next="pay"),
    Option("services", "ns_what.opt.services", next="sphere"),
    Option("both", "ns_what.opt.both", next="priority"),
    Option("other", "ns_what.opt.other", next="ns_other_text"),
)))
_question("ns_other_text", "text")
_question("pay", "single", YES_NO, hint=True)
_question("delivery", "single", YES_NO, hint=True)
_question("stock", "single", YES_NO)
_question("crm", "single", YES_NO)
_question("features", "multi", ["subscribe", "coupons", "account", "one_click", "search", "quiz",
                                "consultant", "calculator", "city", "other"],
          inputs={"other": _text("features", "other")}, hint=True)
_question("variations", "multi", ["no", "color", "size", "weight", "volume", "material", "other"],
          inputs={"other": _text("variations", "other")}, exclusive=["no"], hint=True)
_question("count", "single", ["lt100", "to1k", "to10k", "gt10k"])
_question("product_info", "single", ["excel", "file", "parse", "unknown"])

# hub of sections: categories (multi nodes) + "Иное"
_HUB_CATEGORIES = [
    # (hub option id, category node, item option ids)
    ("main", "sec_main", ["about", "contacts", "prices", "sales", "delivery", "faq"]),
    ("catalog", "sec_catalog", ["services", "goods", "certificates", "brands", "vacancies"]),
    ("services", "sec_services", ["services", "certificates", "brands", "vacancies"]),
    ("trust", "sec_trust", ["advantages", "reviews", "partners", "news"]),
    ("portfolio", "sec_portfolio", ["projects", "works", "gallery"]),
    ("legal", "sec_legal", ["privacy", "terms", "offer"]),
]
_NO_ADD_CATEGORIES = {"sec_legal"}  # the PDF shows no "Добавить свой вариант" there

_add(Node("sections", "hub", "sections.question", "sections.title", hint_key="sections.hint",
          options=tuple(Option(hub_id, f"sections.cat.{hub_id}", next=cat)
                        for hub_id, cat, _ in _HUB_CATEGORIES)
          + (Option("other", "sections.opt.other", input=_text("sections", "other")),)))
for _hub_id, _cat, _items in _HUB_CATEGORIES:
    _cat_options = _options(_cat, _items)
    if _cat not in _NO_ADD_CATEGORIES:
        _cat_options += (Option("add", "sections.add",
                                input=InputSpec("text", "sections.add.prompt")),)
    _add(Node(_cat, "multi", f"{_cat}.question", f"{_cat}.title", options=_cat_options,
              hint_key="sections.cat_hint", done_key="common.back_to_sections"))

HUB_VARIANTS: Dict[str, Tuple[str, ...]] = {
    "goods": ("sec_main", "sec_catalog", "sec_trust", "sec_portfolio", "sec_legal"),
    "services": ("sec_main", "sec_services", "sec_trust", "sec_portfolio", "sec_legal"),
}
HUB_VARIANT_BY_BRANCH: Dict[str, str] = {
    "goods": "goods", "both": "goods", "other": "goods", "services": "services"}

_question("design", "single", ["full", "logo_only", "has_logo", "modern", "template", "consult"])
_add(Node("example", "single", "example.question", "example.title", options=(
    Option("yes", "example.opt.yes", input=InputSpec("url", "example.yes.prompt"),
           extra_inputs=(InputSpec("text", "example.yes.comment", optional=True),)),
    Option("no", "example.opt.no"),
    Option("tz", "example.opt.tz", input=InputSpec("file", "example.tz.prompt")),
    Option("other", "example.opt.other", input=_text("example", "other")),
)))
_question("sphere", "single", ["construction", "repair", "industry", "auto", "specialists",
                               "food", "sport", "other"],
          inputs={"other": _text("sphere", "other")}, hint=True)
_question("structure", "single", ["landing", "multipage", "budget", "consult"], hint=True)
_question("content", "multi", ["all", "partial", "texts", "images", "discuss", "other"],
          inputs={"other": _text("content", "other")}, hint=True)
_question("popup", "single", ["callback", "texts", "consult", "unsure"],
          inputs={"texts": _text("popup", "texts")}, hint=True)
_question("priority", "single", ["goods", "services", "equal", "unknown"])
_question("flows", "single", ["yes", "no", "unsure"])

# -- promotion --------------------------------------------------------------------------

_question("pr_what", "multi", ["site", "social", "gis", "ybiz", "ymaps", "yserv", "none",
                               "unknown", "other"],
          inputs={"site": InputSpec("url", "pr_what.site.prompt"),
                  "social": InputSpec("urls", "pr_what.social.prompt"),
                  "gis": InputSpec("url", "pr_what.gis.prompt"),
                  "ybiz": InputSpec("url", "pr_what.ybiz.prompt"),
                  "ymaps": InputSpec("url", "pr_what.ymaps.prompt"),
                  "yserv": InputSpec("url", "pr_what.yserv.prompt"),
                  "other": _text("pr_what", "other")},
          exclusive=["none", "unknown"], hint=True, intro_key="pr_what.intro")
_question("pr_types", "multi", ["direct", "gis", "ybiz", "ymaps", "yserv", "social", "seo",
                                "unknown", "other"],
          inputs={"other": _text("pr_types", "other")}, exclusive=["unknown"], hint=True)
_question("pr_current", "multi", ["paid", "seo", "social", "unsure", "past", "never", "hard"],
          inputs={"paid": _text("pr_current", "paid"), "unsure": _text("pr_current", "unsure"),
                  "past": _text("pr_current", "past")},
          exclusive=["never", "hard"], final_jump=["never", "hard"], hint=True,
          branching=True)
_question("pr_helpers", "multi", ["no", "yandex", "marketers", "other"],
          inputs={"other": _text("pr_helpers", "other")}, exclusive=["no"], hint=True)
_question("pr_support", "single", ["both", "once", "consult", "other"],
          inputs={"other": _text("pr_support", "other")})
_question("pr_geo", "single", ["city", "regions", "russia", "world"])
_question("pr_goals", "single", ["leads", "calls", "sales", "traffic", "all"])
_question("pr_link", "url", ["use", "none"])

# -- redesign ---------------------------------------------------------------------------

_question("rd_platform", "single", ["wordpress", "tilda", "opencart", "joomla", "bitrix",
                                    "other", "unknown"],
          inputs={"other": _text("rd_platform", "other")})
_question("rd_what", "multi", ["visual", "layout", "bugs", "pages", "features", "advice",
                               "migrate", "other"],
          inputs={"other": _text("rd_what", "other")}, hint=True)
_question("rd_link", "url", ["forgot", "broken"], hint=True)
_question("rd_rebuild", "single", ["yes", "no", "consult"])
_question("rd_promoted", "multi", ["direct", "ybiz", "seo", "other", "no"],
          inputs={"other": _text("rd_promoted", "other")}, exclusive=["no"], hint=True,
          footer_key="rd_promoted.footer")
_question("rd_access", "single", ["all", "domain", "hosting", "lost", "none", "other"],
          inputs={"other": _text("rd_access", "other")})

# -- final block: contacts --------------------------------------------------------------

_question("fin_name", "name", intro_key="fin_name.intro")
_question("fin_phone", "phone", ["share"], hint=True)
_question("fin_messenger", "multi", ["whatsapp", "telegram", "calls"], hint=True,
          done_key="fin_messenger.done")
_question("fin_time", "single", ["morning", "day", "evening", "any"])

_add(Node("summary", "info", "summary.header", None, options=(
    Option("submit", "summary.opt.submit"),
    Option("edit", "summary.opt.edit"),
)))
_add(Node("thanks", "info", "thanks.text", None, options=(
    Option("menu", "thanks.opt.menu", next="menu"),
    Option("examples", "thanks.opt.examples", next="ex_sphere"),
    Option("contacts", "thanks.opt.contacts", next="contacts"),
)))

# -- examples of works ------------------------------------------------------------------

_add(Node("ex_sphere", "single", "ex_sphere.question", "ex_sphere.title", options=(
    Option("other", "ex_sphere.opt.other", next="ex_other"),
)))
_EX_NAV = (
    Option("order", "ex_card.opt.order", next="order"),
    Option("back", "common.back_to_spheres", next="ex_sphere"),
)
_add(Node("ex_card", "info", "ex_card.header", None, options=_EX_NAV,
          footer_key="ex_card.note"))
_add(Node("ex_other", "text", "ex_other.question", "ex_other.title", options=_EX_NAV))
_add(Node("ex_other_done", "info", "ex_other.done", None, options=_EX_NAV))
_add(Node("order", "single", "order.question", None, options=(
    Option("new", "order.opt.new", next="ns_what"),
    Option("redesign", "order.opt.redesign", next="rd_platform"),
    Option("promo", "order.opt.promo", next="pr_what"),
    Option("menu", "common.to_menu", next="menu"),
)))

# -- contact us -------------------------------------------------------------------------

_add(Node("contacts", "info", "contacts.text", None, options=(
    Option("menu", "common.to_menu", next="menu"),
    Option("order", "contacts.opt.order", next="ns_what"),
    Option("promo", "contacts.opt.promo", next="pr_what"),
    Option("redesign", "contacts.opt.redesign", next="rd_platform"),
)))

NO_BACK_NODES = frozenset({"privacy", "menu", "resume", "summary", "thanks", "ex_card",
                           "ex_other_done", "contacts"})

# -- branches ---------------------------------------------------------------------------

BRANCHES: Dict[str, Tuple[str, ...]] = {
    "goods": ("pay", "delivery", "stock", "crm", "features", "variations", "count",
              "product_info", "sections", "design", "example"),
    "services": ("sphere", "structure", "content", "pay", "crm", "popup", "sections",
                 "example"),
    "both": ("priority", "sphere", "pay", "delivery", "stock", "crm", "features", "variations",
             "count", "product_info", "sections", "design", "flows", "example"),
    "other": ("ns_other_text", "structure", "sections", "design", "example"),
    "promo": ("pr_what", "pr_types", "pr_current", "pr_helpers", "pr_support", "pr_geo",
              "pr_goals", "pr_link"),
    "redesign": ("rd_platform", "rd_what", "rd_link", "rd_rebuild", "rd_promoted",
                 "rd_access"),
    "final": ("fin_name", "fin_phone", "fin_messenger", "fin_time"),
}
NEW_SITE_BRANCHES = ("goods", "services", "both", "other")
LEAD_KIND_BY_BRANCH: Dict[str, str] = {
    "goods": "new", "services": "new", "both": "new", "other": "new",
    "promo": "promo", "redesign": "redesign"}
_BRANCH_BY_FIRST_NODE = {seq[0]: name for name, seq in BRANCHES.items()
                         if name not in NEW_SITE_BRANCHES and name != "final"}


# -- functions --------------------------------------------------------------------------


def selected_option_ids(answer: Any) -> List[str]:
    """Option ids of a stored answer: ``"id"``, ``[ids]`` or ``{"option"|"selected": ...}``."""
    if answer is None:
        return []
    if isinstance(answer, str):
        return [answer]
    if isinstance(answer, Mapping):
        if answer.get("selected") is not None:
            return list(answer["selected"])
        return [answer["option"]] if answer.get("option") else []
    return list(answer)


def option_branch(node_id: str, option_id: str) -> Optional[str]:
    """Branch that the option starts, if any (``ns_what`` options, menu → redesign/promo)."""
    node = NODES[node_id]
    if node.branching and option_id in BRANCHES:
        return option_id
    return _BRANCH_BY_FIRST_NODE.get(node.option(option_id).next or "")


def hub_categories(branch: str) -> Tuple[str, ...]:
    """Category nodes of the sections hub for the branch, in display order."""
    return HUB_VARIANTS[HUB_VARIANT_BY_BRANCH[branch]]


def hub_options(branch: str) -> Tuple[Option, ...]:
    """Hub buttons for the branch: its categories, then "Иное"."""
    categories = hub_categories(branch)
    hub = NODES["sections"]
    by_target = {o.next: o for o in hub.options if o.next}
    return tuple(by_target[c] for c in categories) + tuple(o for o in hub.options if o.input)


def next_node(branch: Optional[str], node_id: str, answers: Mapping[str, Any]) -> Optional[str]:
    """Next question after ``node_id``; ``FINAL`` after a branch; ``None`` after the final block.

    Also ``None`` for screens outside the question flow (menus, hub categories, examples).
    """
    node = NODES[node_id]
    selected = selected_option_ids(answers.get(f"{branch}/{node_id}"))
    if node.branching and node_id == "ns_what":
        chosen = next((s for s in selected if s in BRANCHES), None)
        return BRANCHES[chosen][0] if chosen else None
    final = BRANCHES["final"]
    if node_id in final:
        pos = final.index(node_id)
        return final[pos + 1] if pos + 1 < len(final) else None
    if any(o.final_jump and o.id in selected for o in node.options):
        return FINAL
    sequence = BRANCHES.get(branch or "", ())
    if node_id not in sequence:
        return None
    pos = sequence.index(node_id)
    return sequence[pos + 1] if pos + 1 < len(sequence) else FINAL


def question_path(branch: str, answers: Mapping[str, Any]) -> Tuple[str, ...]:
    """All questions of the branch as the client goes through them now, plus the final block.

    New-site branches start with ``ns_what``. Used for the summary order and to rebuild the
    path after an edit of a branching question.
    """
    path: List[str] = ["ns_what"] if branch in NEW_SITE_BRANCHES else []
    node: Optional[str] = BRANCHES[branch][0]
    while node is not None:
        path.append(node)
        node = next_node(branch, node, answers)
    return tuple(path)
