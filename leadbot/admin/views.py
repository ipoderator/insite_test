"""Routes of the admin panel (``/admin/...``). Every page needs a login; every POST, CSRF.

Changes go straight to ``Storage``; the bot reads it on every update, so they apply from
the next message. Templates are autoescaped Jinja (``leadbot/templates/admin``).
"""
from __future__ import annotations

import html
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests
from flask import (Blueprint, Response, abort, current_app, flash, redirect, render_template,
                   request, url_for)

from .. import leads
from ..texts import GROUPS, TEXTS, TextStore
from . import auth, content

log = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

PUBLIC_ENDPOINTS = frozenset({"admin.login"})
GROUP_NAMES = dict(GROUPS)
INFO_GROUP = "info"
SEARCH_LIMIT = 100
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
    recent = storage.list_leads(limit=200)
    return render_template(
        "admin/home.html",
        new_count=sum(1 for lead in recent if lead["status"] == "new"),
        undelivered_count=sum(1 for lead in recent if not lead["delivered"]),
        recipient=(storage.get_setting("owner_chat_id") or "").strip(),
        changed_texts=len(storage.all_text_overrides()),
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
    kind = request.args.get("kind", "")
    status = request.args.get("status", "")
    kind = kind if kind in content.LEAD_KINDS else ""
    status = status if status in dict(content.LEAD_STATUSES) else ""
    rows = storage.list_leads(kind=kind or None, status=status or None, limit=200)
    return render_template(
        "admin/leads.html", leads=rows, kind=kind, status=status,
        kinds=[(k, _kind_label(texts, k)) for k in content.LEAD_KINDS],
        kind_labels={k: _kind_label(texts, k) for k in content.LEAD_KINDS},
        statuses=content.LEAD_STATUSES, status_labels=dict(content.LEAD_STATUSES),
        when=content.moscow_time, filtered=bool(kind or status))


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
    rows = []
    for key in keys:
        text_def = TEXTS[key]
        rows.append({
            "key": key,
            "label": text_def.label,
            "kind": "button" if content.is_button(text_def) else text_def.kind,
            "group": GROUP_NAMES.get(text_def.group, text_def.group),
            "default": text_def.default,
            "value": submitted.get(key, overrides.get(key, text_def.default)),
            "changed": key in overrides,
            "limit": content.text_limit(text_def),
            "placeholders": [(p, content.PLACEHOLDER_HINTS.get(p, ""))
                             for p in content.placeholders(text_def.default)],
            "error": errors.get(key),
        })
    return rows


def _matches(key: str, query: str, overrides: Dict[str, str]) -> bool:
    text_def = TEXTS[key]
    haystack = " ".join([key, text_def.label, text_def.default, overrides.get(key, "")])
    return query.casefold() in haystack.casefold()


def _render_texts(section: str, group: str, query: str, errors: Optional[Dict[str, str]] = None,
                  submitted: Optional[Dict[str, str]] = None, status: int = 200):
    storage = _storage()
    texts = TextStore(storage)
    overrides = storage.all_text_overrides()
    if section == INFO_GROUP:
        keys = [k for k, d in TEXTS.items() if d.group == INFO_GROUP]
        group = INFO_GROUP
    elif query:
        keys = [k for k in TEXTS if _matches(k, query, overrides)]
    else:
        keys = [k for k, d in TEXTS.items() if d.group == group]
    groups = [{"id": gid, "name": name,
               "count": sum(1 for d in TEXTS.values() if d.group == gid),
               "changed": sum(1 for k in overrides if k in TEXTS and TEXTS[k].group == gid)}
              for gid, name in GROUPS]
    return render_template(
        "admin/texts.html", section=section, group=group, query=query, groups=groups,
        group_name=GROUP_NAMES.get(group, ""),
        rows=_text_rows(keys, texts, overrides, errors or {}, submitted or {})), status


def _section_args(key: Optional[str] = None) -> tuple:
    """(section, group, query) of the page to show; for a text form — the page it is on."""
    source = request.form if request.method == "POST" else request.args
    section = INFO_GROUP if source.get("section") == INFO_GROUP else "texts"
    group = source.get("group", "")
    group = group if group in GROUP_NAMES else GROUPS[0][0]
    query = " ".join((source.get("q") or "").split())[:SEARCH_LIMIT]
    if key is not None:
        if section == INFO_GROUP and TEXTS[key].group != INFO_GROUP:
            section = "texts"
        if section != INFO_GROUP and not query:
            group = TEXTS[key].group
    return section, group, query


def _back_to_texts(section: str, group: str, query: str, key: str):
    if section == INFO_GROUP:
        return redirect(url_for("admin.info_blocks", _anchor="t-" + key))
    args = {"q": query} if query else {"group": group}
    return redirect(url_for("admin.texts_page", _anchor="t-" + key, **args))


@admin_bp.get("/texts")
def texts_page():
    section, group, query = _section_args()
    return _render_texts("texts", group, query)


@admin_bp.get("/info")
def info_blocks():
    return _render_texts(INFO_GROUP, INFO_GROUP, "")


@admin_bp.post("/texts/<key>")
def text_save(key: str):
    if key not in TEXTS:
        abort(404)
    section, group, query = _section_args(key)
    value, error = content.check_text(key, request.form.get("value"))
    if error:
        return _render_texts(section, group, query, errors={key: error},
                             submitted={key: request.form.get("value", "")}, status=400)
    storage = _storage()
    if value == TEXTS[key].default:
        storage.delete_text_override(key)
    else:
        storage.set_text_override(key, value)
    log.info("Text %s changed in the panel", key)
    flash("Сохранено, бот уже использует новый текст.", "ok")
    return _back_to_texts(section, group, query, key)


@admin_bp.post("/texts/<key>/reset")
def text_reset(key: str):
    if key not in TEXTS:
        abort(404)
    section, group, query = _section_args(key)
    _storage().delete_text_override(key)
    flash("Возвращён текст по умолчанию — бот уже использует его.", "ok")
    return _back_to_texts(section, group, query, key)


# -- contacts, links and the demo policy -----------------------------------------------

def _policy_link_in_bot(storage) -> str:
    url = (storage.get_setting("privacy_url") or "").strip()
    if url:
        return url
    base = _state().config.public_base_url
    return (base + "/privacy") if base else url_for("privacy_page", _external=True)


@admin_bp.route("/contacts", methods=["GET", "POST"])
def contacts():
    storage = _storage()
    if request.method == "POST":
        values, errors = content.check_contacts(request.form)
        if errors:
            return render_template("admin/contacts.html", values=values, errors=errors,
                                   policy_link=_policy_link_in_bot(storage)), 400
        for field in content.CONTACT_FIELDS:
            storage.set_setting(field, values[field])
        flash("Сохранено. Бот уже показывает новые контакты и ссылку.", "ok")
        return redirect(url_for("admin.contacts"))
    values = {f: storage.get_setting(f) or "" for f in content.CONTACT_FIELDS}
    return render_template("admin/contacts.html", values=values, errors={},
                           policy_link=_policy_link_in_bot(storage))


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
        storage.set_setting(content.PRIVACY_PAGE_SETTING, text)
        flash("Сохранено. Страница политики уже показывает новый текст.", "ok")
        return redirect(url_for("admin.policy"))
    saved = storage.get_setting(content.PRIVACY_PAGE_SETTING)
    return render_template("admin/policy.html", text=saved or content.DEFAULT_PRIVACY_PAGE,
                           error=None, changed=bool(saved),
                           external=(storage.get_setting("privacy_url") or "").strip())


@admin_bp.post("/policy/reset")
def policy_reset():
    _storage().set_setting(content.PRIVACY_PAGE_SETTING, "")
    flash("Возвращён демонстрационный текст.", "ok")
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
