"""Routes of the admin panel (``/admin/...``). Every page needs a login; every POST, CSRF.

Changes go straight to ``Storage``; the bot reads it on every update, so they apply from
the next message. Templates are autoescaped Jinja (``leadbot/templates/admin``).
"""
from __future__ import annotations

import html
import logging
import math
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests
from flask import (Blueprint, Response, abort, current_app, flash, redirect, render_template,
                   request, url_for)

from .. import leads
from ..scenario import NODES
from ..storage import HISTORY_SETTING, HISTORY_TEXT
from ..texts import GROUPS, TEXTS, TextStore
from . import auth, content, preview

log = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

PUBLIC_ENDPOINTS = frozenset({"admin.login"})
GROUP_NAMES = dict(GROUPS)
INFO_GROUP = "info"
SEARCH_LIMIT = 100
HISTORY_SHOWN = 5   # versions listed under a text
MAX_PAGE = 100000
DOWNLOAD_TIMEOUT = (10, 60)
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")


def _state():
    from ..web import get_state  # late import: web imports this package

    return get_state(current_app)


def _storage():
    return _state().storage


def _texts() -> TextStore:
    return TextStore(_storage())


# -- access control ----------------------------------------------------------------

@admin_bp.before_request
def _guard():
    state = _state()
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if not auth.is_logged_in(state.config, state.storage):
        target = auth.ADMIN_HOME
        if request.method == "GET":
            target = auth.safe_next(request.full_path.rstrip("?"))
        if target == auth.ADMIN_HOME:
            return redirect(url_for("admin.login"))
        return redirect(url_for("admin.login", next=target))
    if request.method == "POST":
        auth.require_csrf()
    return None


@admin_bp.after_request
def _headers(resp: Response) -> Response:
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
    return resp


@admin_bp.context_processor
def _template_globals() -> Dict[str, Any]:
    return {"csrf_token": auth.csrf_token, "nav_endpoint": request.endpoint or ""}


@admin_bp.errorhandler(400)
def _bad_request(exc):
    if getattr(exc, "description", "") == "csrf":
        message = ("Страница устарела или открыта слишком давно. Вернитесь назад, обновите "
                   "страницу и повторите действие.")
    else:
        message = "Запрос не удалось обработать. Вернитесь назад и попробуйте ещё раз."
    return render_template("admin/error.html", title="Не получилось", message=message), 400


@admin_bp.errorhandler(404)
def _not_found(exc):
    return render_template("admin/error.html", title="Не найдено",
                           message="Такой страницы или записи нет — возможно, её удалили."), 404


# -- login / logout ----------------------------------------------------------------

@admin_bp.route("/login", methods=["GET", "POST"])
def login():
    state = _state()
    next_path = auth.safe_next(request.args.get("next"))
    if request.method == "GET":
        if auth.is_logged_in(state.config, state.storage):
            return redirect(next_path)
        return render_template("admin/login.html", next_path=next_path)

    if not auth.csrf_valid():
        return render_template("admin/login.html", next_path=next_path,
                               error="Страница входа устарела — попробуйте ещё раз."), 400
    ip = auth.client_ip()
    if auth.is_locked(state.storage, ip):
        log.warning("Admin login refused: too many failures from %s", ip)
        return render_template("admin/login.html", next_path=next_path, locked=True), 429
    if auth.password_matches(request.form.get("password", ""), state.config.admin_password):
        state.storage.clear_login_failures(ip)
        auth.start_session(state.config, state.storage)
        log.info("Admin logged in from %s", ip)
        return redirect(next_path)
    state.storage.record_login_failure(ip)
    log.warning("Admin login failed from %s", ip)
    locked = auth.is_locked(state.storage, ip)
    return render_template("admin/login.html", next_path=next_path,
                           error="Неверный пароль.", locked=locked), 401


@admin_bp.post("/logout")
def logout():
    auth.end_all_sessions(_storage())
    flash("Вы вышли из панели.", "ok")
    return redirect(url_for("admin.login"))


# -- home and help ------------------------------------------------------------------

@admin_bp.get("")
@admin_bp.get("/")
def home():
    storage = _storage()
    categories = storage.list_categories()
    for cat in categories:
        cat["items"] = storage.list_items(cat["id"])
    return render_template(
        "admin/home.html",
        new_count=storage.count_leads(status="new"),
        undelivered_count=storage.count_leads(delivered=False),
        recipient=(storage.get_setting("owner_chat_id") or "").strip(),
        changed_texts=len(storage.all_text_overrides()),
        gaps=content.handover_gaps(storage.all_settings(), categories),
    )


@admin_bp.get("/help")
def help_page():
    return render_template("admin/help.html")


# -- leads ---------------------------------------------------------------------------

def _kind_label(texts: TextStore, kind: str) -> str:
    try:
        return texts.get(f"lead.kind.{kind}")
    except KeyError:
        return kind


@admin_bp.get("/leads")
def leads_list():
    storage, texts = _storage(), _texts()
    args = request.args
    kind = args.get("kind", "")
    kind = kind if kind in content.LEAD_KINDS else ""
    status = args.get("status", "")
    status = status if status in dict(content.LEAD_STATUSES) else ""
    delivery = args.get("delivery", "")
    delivery = delivery if delivery in dict(content.LEAD_DELIVERY) else ""
    query = " ".join((args.get("q") or "").split())[:content.LEAD_QUERY_LIMIT]
    day_from, day_to = content.parse_day(args.get("from")), content.parse_day(args.get("to"))
    if day_from and day_to and day_from > day_to:
        day_from, day_to = day_to, day_from
    page_arg = args.get("page", "")
    page = min(max(int(page_arg), 1), MAX_PAGE) if page_arg.isascii() and page_arg.isdigit() \
        else 1

    filters = {
        "kind": kind or None, "status": status or None,
        "delivered": {"yes": True, "no": False}.get(delivery), "query": query,
        "created_from": content.moscow_day_start(day_from) if day_from else None,
        "created_to": content.moscow_day_after(day_to) if day_to else None,
    }
    size = content.LEADS_PAGE_SIZE
    rows, total = storage.find_leads(**filters, limit=size, offset=(page - 1) * size)
    pages = max(1, math.ceil(total / size))
    if page > pages:
        page = pages
        rows, total = storage.find_leads(**filters, limit=size, offset=(page - 1) * size)
    link_args = {name: value for name, value in (
        ("q", query), ("kind", kind), ("status", status), ("delivery", delivery),
        ("from", day_from.isoformat() if day_from else ""),
        ("to", day_to.isoformat() if day_to else "")) if value}
    return render_template(
        "admin/leads.html", leads=rows, kind=kind, status=status, delivery=delivery,
        query=query, day_from=link_args.get("from", ""), day_to=link_args.get("to", ""),
        kinds=[(k, _kind_label(texts, k)) for k in content.LEAD_KINDS],
        kind_labels={k: _kind_label(texts, k) for k in content.LEAD_KINDS},
        statuses=content.LEAD_STATUSES, status_labels=dict(content.LEAD_STATUSES),
        deliveries=content.LEAD_DELIVERY, when=content.moscow_time,
        filtered=bool(link_args), link_args=link_args, page=page, pages=pages, total=total)


def _lead_or_404(lead_id: int) -> dict:
    lead = _storage().get_lead(lead_id)
    if lead is None:
        abort(404)
    return lead


@admin_bp.get("/leads/<int:lead_id>")
def lead_card(lead_id: int):
    lead = _lead_or_404(lead_id)
    texts = _texts()
    username = lead.get("username") or ""
    meta = lead.get("meta") or {}
    return render_template(
        "admin/lead.html", lead=lead, meta=meta,
        kind_label=_kind_label(texts, lead["kind"]),
        status_label=dict(content.LEAD_STATUSES).get(lead["status"], lead["status"]),
        username_link=("https://t.me/" + username) if _USERNAME_RE.match(username) else "",
        when=content.moscow_time, size=content.file_size)


@admin_bp.post("/leads/<int:lead_id>/status")
def lead_status(lead_id: int):
    _lead_or_404(lead_id)
    status = request.form.get("status", "")
    if status not in dict(content.LEAD_STATUSES):
        abort(400)
    _storage().set_lead_status(lead_id, status)
    flash("Заявка отмечена обработанной." if status == "processed"
          else "Заявка снова в новых.", "ok")
    return redirect(url_for("admin.lead_card", lead_id=lead_id))


@admin_bp.post("/leads/<int:lead_id>/resend")
def lead_resend(lead_id: int):
    _lead_or_404(lead_id)
    state = _state()
    if leads.deliver_lead(lead_id, state.storage, state.tg, TextStore(state.storage)):
        flash("Заявка отправлена владельцу в Telegram.", "ok")
    else:
        error = (state.storage.get_lead(lead_id) or {}).get("delivery_error") or ""
        flash("Не удалось отправить заявку. " + error, "error")
    return redirect(url_for("admin.lead_card", lead_id=lead_id))


def _download_name(name: Optional[str]) -> str:
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ord(ch) >= 32 and ch not in '"').strip(" .")
    return name or "file"


def _content_disposition(name: str) -> str:
    ascii_name = "".join(ch if 32 <= ord(ch) < 127 else "_" for ch in name)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


@admin_bp.get("/leads/<int:lead_id>/files/<int:index>")
def lead_file(lead_id: int, index: int):
    """Fetch the file from Telegram on the server: the URL with the token never leaves it."""
    lead = _lead_or_404(lead_id)
    files = lead.get("files") or []
    if not 0 <= index < len(files):
        abort(404)
    info = files[index]
    try:
        url = _state().tg.get_file_url(info["file_id"])
        upstream = requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT)
    except Exception as exc:  # TelegramError, network errors: their text may hold the URL
        log.warning("Lead %s file %s download failed: %s", lead_id, index, type(exc).__name__)
        upstream = None
    if upstream is None or upstream.status_code != 200:
        if upstream is not None:
            log.warning("Lead %s file %s download failed: HTTP %s", lead_id, index,
                        upstream.status_code)
            upstream.close()
        flash("Не удалось скачать файл из Telegram. Попробуйте позже; если не выйдет — файл "
              "можно найти в чате получателя заявок.", "error")
        return redirect(url_for("admin.lead_card", lead_id=lead_id))
    def stream():
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                yield chunk
        finally:
            upstream.close()

    resp = Response(stream(), mimetype="application/octet-stream")
    resp.headers["Content-Disposition"] = _content_disposition(_download_name(info.get("file_name")))
    length = upstream.headers.get("Content-Length")
    if length and length.isdigit():
        resp.headers["Content-Length"] = length
    resp.call_on_close(upstream.close)  # also when the client goes away before the end
    return resp


# -- texts and info blocks -----------------------------------------------------------

def _text_rows(keys: List[str], texts: TextStore, overrides: Dict[str, str],
               errors: Dict[str, str], submitted: Dict[str, str]) -> List[Dict[str, Any]]:
    storage = _storage()
    history_counts = storage.history_counts(HISTORY_TEXT)
    rows = []
    for key in keys:
        text_def = TEXTS[key]
        rows.append({
            "key": key,
            "label": text_def.label,
            "kind": "button" if content.is_button(text_def) else text_def.kind,
            "group": GROUP_NAMES.get(text_def.group, text_def.group),
            "important": text_def.group == INFO_GROUP,
            "default": text_def.default,
            "value": submitted.get(key, overrides.get(key, text_def.default)),
            "changed": key in overrides,
            "limit": content.text_limit(text_def),
            "placeholders": [(p, content.PLACEHOLDER_HINTS.get(p, ""))
                             for p in content.placeholders(text_def.default)],
            "error": errors.get(key),
            "history_count": history_counts.get(key, 0),
            "history": (storage.history(HISTORY_TEXT, [key], HISTORY_SHOWN)
                        if history_counts.get(key) else []),
        })
    return rows


def _matches(key: str, query: str, overrides: Dict[str, str]) -> bool:
    text_def = TEXTS[key]
    haystack = " ".join([key, text_def.label, text_def.default, overrides.get(key, "")])
    return query.casefold() in haystack.casefold()


def _text_view(key: Optional[str] = None) -> Dict[str, Any]:
    """The texts page to show: section, group, step, search, filters; for a text form — the
    page the form is on (its hidden fields), moved to the text's own group and step."""
    source = request.form if request.method == "POST" else request.args
    view: Dict[str, Any] = {
        "section": INFO_GROUP if source.get("section") == INFO_GROUP else "texts",
        "q": " ".join((source.get("q") or "").split())[:SEARCH_LIMIT],
        "changed": source.get("changed") == "1",
        "show_keys": source.get("keys") == "1",
    }
    group = source.get("group", "")
    group = group if group in GROUP_NAMES else GROUPS[0][0]
    step = source.get("step", "")
    if key is not None:
        if view["section"] == INFO_GROUP and TEXTS[key].group != INFO_GROUP:
            view["section"] = "texts"
        if view["section"] != INFO_GROUP and not (view["q"] or view["changed"]):
            group, step = TEXTS[key].group, content.step_of(key)
    steps = content.group_steps(group) if content.uses_steps(group) else []
    view["group"] = group
    view["step"] = step if step in steps else (steps[0] if steps else "")
    return view


def _texts_url(view: Dict[str, Any], key: Optional[str] = None) -> str:
    args: Dict[str, str] = {}
    if view["section"] != INFO_GROUP:
        if view["q"] or view["changed"]:
            if view["q"]:
                args["q"] = view["q"]
            if view["changed"]:
                args["changed"] = "1"
        else:
            args["group"] = view["group"]
            if view["step"]:
                args["step"] = view["step"]
    if view["show_keys"]:
        args["keys"] = "1"
    endpoint = "admin.info_blocks" if view["section"] == INFO_GROUP else "admin.texts_page"
    return url_for(endpoint, _anchor=("t-" + key) if key else None, **args)


def _render_texts(view: Dict[str, Any], errors: Optional[Dict[str, str]] = None,
                  submitted: Optional[Dict[str, str]] = None, status: int = 200):
    storage = _storage()
    texts = TextStore(storage)
    overrides = storage.all_text_overrides()
    group, query = view["group"], view["q"]
    steps: List[Dict[str, Any]] = []
    screen: List[preview.Message] = []
    if view["section"] == INFO_GROUP:
        keys = [k for k, d in TEXTS.items() if d.group == INFO_GROUP]
    elif query or view["changed"]:
        keys = [k for k in TEXTS if (not query or _matches(k, query, overrides))
                and (not view["changed"] or k in overrides)]
    elif view["step"]:
        keys = content.step_keys(group, view["step"])
        for number, step in enumerate(content.group_steps(group), start=1):
            step_texts = content.step_keys(group, step)
            steps.append({"id": step, "number": number,
                          "name": content.step_name(step, texts), "count": len(step_texts),
                          "changed": sum(1 for k in step_texts if k in overrides)})
        if view["step"] in NODES:
            screen = preview.screen(texts, view["step"])
    else:
        keys = [k for k, d in TEXTS.items() if d.group == group]
    groups = [{"id": gid, "name": name,
               "count": sum(1 for d in TEXTS.values() if d.group == gid),
               "changed": sum(1 for k in overrides if k in TEXTS and TEXTS[k].group == gid)}
              for gid, name in GROUPS]
    current = next((st for st in steps if st["id"] == view["step"]), None)
    return render_template(
        "admin/texts.html", view=view, section=view["section"], group=group, query=query,
        groups=groups, group_name=GROUP_NAMES.get(group, ""), steps=steps, current_step=current,
        prev_step=steps[current["number"] - 2] if current and current["number"] > 1 else None,
        next_step=steps[current["number"]] if current and current["number"] < len(steps)
        else None,
        screen=screen, changed_total=sum(1 for k in overrides if k in TEXTS),
        rows=_text_rows(keys, texts, overrides, errors or {}, submitted or {}),
        when=content.moscow_time), status


@admin_bp.get("/texts")
def texts_page():
    view = _text_view()
    view["section"] = "texts"
    return _render_texts(view)


@admin_bp.get("/info")
def info_blocks():
    view = _text_view()
    view.update(section=INFO_GROUP, group=INFO_GROUP, step="", q="", changed=False)
    return _render_texts(view)


def _text_or_404(key: str) -> None:
    if key not in TEXTS:
        abort(404)


@admin_bp.post("/texts/<key>")
def text_save(key: str):
    _text_or_404(key)
    view = _text_view(key)
    value, error = content.check_text(key, request.form.get("value"))
    if error:
        return _render_texts(view, errors={key: error},
                             submitted={key: request.form.get("value", "")}, status=400)
    storage = _storage()
    if value == TEXTS[key].default:
        storage.delete_text_override(key)
    else:
        storage.set_text_override(key, value)
    log.info("Text %s changed in the panel", key)
    flash("Сохранено, бот уже использует новый текст. Прежний вариант — в истории "
          "изменений под текстом.", "ok")
    return redirect(_texts_url(view, key))


@admin_bp.post("/texts/<key>/preview")
def text_preview(key: str):
    """How the client will see the edited text, before it reaches the bot."""
    _text_or_404(key)
    view = _text_view(key)
    value, error = content.check_text(key, request.form.get("value"))
    if error:
        return _render_texts(view, errors={key: error},
                             submitted={key: request.form.get("value", "")}, status=400)
    storage = _storage()
    current = TextStore(storage)
    text_def = TEXTS[key]
    return render_template(
        "admin/text_preview.html", key=key, label=text_def.label, value=value, view=view,
        kind="button" if content.is_button(text_def) else text_def.kind,
        limit=content.text_limit(text_def), unchanged=value == current.get(key),
        before=preview.text_screen(current, key),
        after=preview.text_screen(preview.PreviewTexts(storage, {key: value}), key),
        back=_texts_url(view, key))


@admin_bp.post("/texts/<key>/reset")
def text_reset(key: str):
    _text_or_404(key)
    view = _text_view(key)
    _storage().delete_text_override(key)
    flash("Возвращён текст по умолчанию — бот уже использует его.", "ok")
    return redirect(_texts_url(view, key))


def _history_entry_or_404(entry_id: int, scope: str, keys) -> dict:
    entry = _storage().get_history_entry(entry_id)
    if entry is None or entry["scope"] != scope or entry["key"] not in keys:
        abort(404)
    return entry


@admin_bp.post("/texts/<key>/restore/<int:entry_id>")
def text_restore(key: str, entry_id: int):
    _text_or_404(key)
    entry = _history_entry_or_404(entry_id, HISTORY_TEXT, {key})
    view = _text_view(key)
    storage = _storage()
    if entry["value"] is None or entry["value"] == TEXTS[key].default:
        storage.delete_text_override(key)
    else:
        value, error = content.check_text(key, entry["value"])
        if error:
            flash("Эту версию нельзя вернуть: " + error, "error")
            return redirect(_texts_url(view, key))
        storage.set_text_override(key, value)
    log.info("Text %s restored to version %s in the panel", key, entry_id)
    flash(f"Возвращена версия, действовавшая до {content.moscow_time(entry['created_at'])} — "
          f"бот уже использует её.", "ok")
    return redirect(_texts_url(view, key))


# -- contacts, links and the demo policy -----------------------------------------------

def _policy_link_in_bot(storage) -> str:
    url = (storage.get_setting("privacy_url") or "").strip()
    if url:
        return url
    base = _state().config.public_base_url
    return (base + "/privacy") if base else url_for("privacy_page", _external=True)


def _render_contacts(values: Dict[str, str], errors: Dict[str, str], status: int = 200):
    storage = _storage()
    return render_template(
        "admin/contacts.html", values=values, errors=errors,
        policy_link=_policy_link_in_bot(storage),
        history=storage.history(HISTORY_SETTING, content.CONTACT_FIELDS, HISTORY_SHOWN * 2),
        field_labels=content.CONTACT_LABELS, when=content.moscow_time,
        screen=preview.screen(TextStore(storage), "contacts")), status


def _saved_contacts() -> Dict[str, str]:
    storage = _storage()
    return {f: storage.get_setting(f) or "" for f in content.CONTACT_FIELDS}


@admin_bp.route("/contacts", methods=["GET", "POST"])
def contacts():
    storage = _storage()
    if request.method == "POST":
        values, errors = content.check_contacts(request.form)
        if errors:
            return _render_contacts(values, errors, 400)
        for field in content.CONTACT_FIELDS:
            storage.set_setting(field, values[field], keep_history=True)
        flash("Сохранено. Бот уже показывает новые контакты и ссылку. Прежние значения — "
              "в истории изменений внизу страницы.", "ok")
        return redirect(url_for("admin.contacts"))
    return _render_contacts(_saved_contacts(), {})


@admin_bp.post("/contacts/restore/<int:entry_id>")
def contacts_restore(entry_id: int):
    entry = _history_entry_or_404(entry_id, HISTORY_SETTING, content.CONTACT_FIELDS)
    values, errors = content.check_contacts({**_saved_contacts(),
                                             entry["key"]: entry["value"] or ""})
    if errors.get(entry["key"]):
        flash("Это значение нельзя вернуть: " + errors[entry["key"]], "error")
    else:
        _storage().set_setting(entry["key"], values[entry["key"]], keep_history=True)
        flash(f"«{content.CONTACT_LABELS[entry['key']]}»: возвращено прежнее значение — бот "
              f"уже показывает его.", "ok")
    return redirect(url_for("admin.contacts"))


@admin_bp.route("/policy", methods=["GET", "POST"])
def policy():
    storage = _storage()
    if request.method == "POST":
        text = content.normalize_text(request.form.get("text"))
        error = None
        if not text:
            error = "Текст не может быть пустым."
        elif len(text) > content.POLICY_LIMIT:
            error = f"Слишком длинно: {len(text)} символов, можно не больше {content.POLICY_LIMIT}."
        if error:
            return render_template("admin/policy.html", text=request.form.get("text", ""),
                                   error=error, changed=True), 400
        storage.set_setting(content.PRIVACY_PAGE_SETTING, text, keep_history=True)
        flash("Сохранено. Страница политики уже показывает новый текст.", "ok")
        return redirect(url_for("admin.policy"))
    saved = storage.get_setting(content.PRIVACY_PAGE_SETTING)
    return render_template("admin/policy.html", text=saved or content.DEFAULT_PRIVACY_PAGE,
                           error=None, changed=bool(saved),
                           external=(storage.get_setting("privacy_url") or "").strip(),
                           history=storage.history(HISTORY_SETTING,
                                                   [content.PRIVACY_PAGE_SETTING], HISTORY_SHOWN),
                           when=content.moscow_time)


@admin_bp.post("/policy/reset")
def policy_reset():
    _storage().set_setting(content.PRIVACY_PAGE_SETTING, "", keep_history=True)
    flash("Возвращён демонстрационный текст.", "ok")
    return redirect(url_for("admin.policy"))


@admin_bp.post("/policy/restore/<int:entry_id>")
def policy_restore(entry_id: int):
    entry = _history_entry_or_404(entry_id, HISTORY_SETTING, {content.PRIVACY_PAGE_SETTING})
    _storage().set_setting(content.PRIVACY_PAGE_SETTING, entry["value"] or "",
                           keep_history=True)
    flash("Возвращена прежняя версия текста политики.", "ok")
    return redirect(url_for("admin.policy"))


# -- examples --------------------------------------------------------------------------

def _category_or_404(category_id: int) -> dict:
    category = _storage().get_category(category_id)
    if category is None:
        abort(404)
    return category


def _item_or_404(category_id: int, item_id: int) -> dict:
    for item in _storage().list_items(category_id):
        if item["id"] == item_id:
            return item
    abort(404)


def _move(rows: List[dict], row_id: int, direction: str, save) -> None:
    ids = [r["id"] for r in rows]
    if row_id not in ids or direction not in ("up", "down"):
        abort(400)
    i = ids.index(row_id)
    j = i - 1 if direction == "up" else i + 1
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
    for pos, rid in enumerate(ids, start=1):
        save({"id": rid, "sort": pos * 10})


@admin_bp.get("/examples")
def examples():
    storage = _storage()
    categories = storage.list_categories()
    for cat in categories:
        cat["items_count"] = len(storage.list_items(cat["id"]))
    return render_template("admin/examples.html", categories=categories,
                           values={}, errors={})


@admin_bp.post("/examples/new")
def category_create():
    values, errors = content.check_category(request.form)
    storage = _storage()
    if errors:
        categories = storage.list_categories()
        for cat in categories:
            cat["items_count"] = len(storage.list_items(cat["id"]))
        return render_template("admin/examples.html", categories=categories,
                               values=values, errors=errors), 400
    category_id = storage.save_category({**values, "active": True})
    flash("Сфера добавлена. Теперь добавьте в неё примеры.", "ok")
    return redirect(url_for("admin.category", category_id=category_id))


def _render_category(category: dict, cat_values=None, cat_errors=None, item_errors=None,
                     item_values=None, new_values=None, new_errors=None, status=200):
    return render_template(
        "admin/category.html", category=category, items=_storage().list_items(category["id"]),
        cat_values=cat_values or category, cat_errors=cat_errors or {},
        item_errors=item_errors or {}, item_values=item_values or {},
        new_values=new_values or {"is_demo": False}, new_errors=new_errors or {}), status


@admin_bp.get("/examples/<int:category_id>")
def category(category_id: int):
    return _render_category(_category_or_404(category_id))


@admin_bp.post("/examples/<int:category_id>")
def category_save(category_id: int):
    category = _category_or_404(category_id)
    values, errors = content.check_category(request.form)
    values["active"] = bool(request.form.get("active"))
    if errors:
        return _render_category(category, cat_values={**category, **values},
                                cat_errors=errors, status=400)
    _storage().save_category({"id": category_id, **values})
    flash("Сфера сохранена — бот уже показывает изменения.", "ok")
    return redirect(url_for("admin.category", category_id=category_id))


@admin_bp.post("/examples/<int:category_id>/toggle")
def category_toggle(category_id: int):
    category = _category_or_404(category_id)
    _storage().save_category({"id": category_id, "active": not category["active"]})
    flash("Сфера скрыта из бота." if category["active"] else "Сфера снова видна в боте.", "ok")
    return redirect(url_for("admin.examples"))


@admin_bp.post("/examples/<int:category_id>/move")
def category_move(category_id: int):
    _category_or_404(category_id)
    storage = _storage()
    _move(storage.list_categories(), category_id, request.form.get("direction", ""),
          storage.save_category)
    flash("Порядок сфер изменён.", "ok")
    return redirect(url_for("admin.examples", _anchor=f"c-{category_id}"))


@admin_bp.route("/examples/<int:category_id>/delete", methods=["GET", "POST"])
def category_delete(category_id: int):
    category = _category_or_404(category_id)
    storage = _storage()
    if request.method == "GET":
        count = len(storage.list_items(category_id))
        return render_template(
            "admin/confirm_delete.html", title="Удалить сферу?",
            what=f"Сфера «{category['button']}» и все её примеры ({count}) будут удалены "
                 f"из бота без возможности восстановления.",
            action=url_for("admin.category_delete", category_id=category_id),
            cancel=url_for("admin.category", category_id=category_id))
    storage.delete_category(category_id)
    flash(f"Сфера «{category['button']}» удалена.", "ok")
    return redirect(url_for("admin.examples"))


@admin_bp.post("/examples/<int:category_id>/items/new")
def item_create(category_id: int):
    category = _category_or_404(category_id)
    values, errors = content.check_item(request.form)
    if errors:
        return _render_category(category, new_values=values, new_errors=errors, status=400)
    item_id = _storage().save_item({**values, "category_id": category_id})
    flash("Пример добавлен — бот уже показывает его.", "ok")
    return redirect(url_for("admin.category", category_id=category_id, _anchor=f"i-{item_id}"))


@admin_bp.post("/examples/<int:category_id>/items/<int:item_id>")
def item_save(category_id: int, item_id: int):
    category = _category_or_404(category_id)
    _item_or_404(category_id, item_id)
    values, errors = content.check_item(request.form)
    if errors:
        return _render_category(category, item_errors={item_id: errors},
                                item_values={item_id: values}, status=400)
    _storage().save_item({"id": item_id, **values})
    flash("Пример сохранён — бот уже показывает изменения.", "ok")
    return redirect(url_for("admin.category", category_id=category_id, _anchor=f"i-{item_id}"))


@admin_bp.post("/examples/<int:category_id>/items/<int:item_id>/move")
def item_move(category_id: int, item_id: int):
    _category_or_404(category_id)
    _item_or_404(category_id, item_id)
    storage = _storage()
    _move(storage.list_items(category_id), item_id, request.form.get("direction", ""),
          storage.save_item)
    flash("Порядок примеров изменён.", "ok")
    return redirect(url_for("admin.category", category_id=category_id, _anchor=f"i-{item_id}"))


@admin_bp.route("/examples/<int:category_id>/items/<int:item_id>/delete",
                methods=["GET", "POST"])
def item_delete(category_id: int, item_id: int):
    _category_or_404(category_id)
    item = _item_or_404(category_id, item_id)
    if request.method == "GET":
        return render_template(
            "admin/confirm_delete.html", title="Удалить пример?",
            what=f"Пример «{item['title']}» будет удалён из бота без возможности "
                 f"восстановления.",
            action=url_for("admin.item_delete", category_id=category_id, item_id=item_id),
            cancel=url_for("admin.category", category_id=category_id))
    _storage().delete_item(item_id)
    flash(f"Пример «{item['title']}» удалён.", "ok")
    return redirect(url_for("admin.category", category_id=category_id))


# -- lead recipient ----------------------------------------------------------------------

def _owner_chat(value: str) -> Any:
    return int(value) if value.lstrip("-").isdigit() else value


@admin_bp.get("/recipient")
def recipient():
    storage = _storage()
    chat = (storage.get_setting("owner_chat_id") or "").strip()
    return render_template("admin/recipient.html", chat=chat,
                           is_group=chat.startswith("-"),
                           code=storage.get_setting("owner_bind_code") or "")


@admin_bp.post("/recipient/test")
def recipient_test():
    state = _state()
    chat = (state.storage.get_setting("owner_chat_id") or "").strip()
    if not chat:
        flash("Получатель ещё не привязан — сначала отправьте боту команду с кодом.", "error")
        return redirect(url_for("admin.recipient"))
    try:
        state.tg.send_message(_owner_chat(chat), html.escape(TextStore(state.storage)
                                                              .get("owner.test")))
    except Exception as exc:
        reason = getattr(exc, "description", None) or type(exc).__name__
        log.warning("Test message to the recipient failed: %s", reason)
        flash("Telegram не принял сообщение: " + str(reason), "error")
    else:
        flash("Тестовое сообщение отправлено — проверьте чат получателя.", "ok")
    return redirect(url_for("admin.recipient"))


@admin_bp.post("/recipient/unbind")
def recipient_unbind():
    _storage().set_setting("owner_chat_id", "")
    log.info("Lead recipient unbound in the panel")
    flash("Получатель отвязан. Новые заявки сохраняются в панели, но в Telegram не "
          "приходят, пока вы не привяжете чат снова.", "ok")
    return redirect(url_for("admin.recipient"))


@admin_bp.post("/recipient/code")
def recipient_code():
    _storage().rotate_owner_bind_code()
    flash("Код привязки изменён. Старый код больше не работает.", "ok")
    return redirect(url_for("admin.recipient"))
