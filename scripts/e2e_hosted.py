"""End-to-end check of the deployed bot on its public HTTPS address.

    python scripts/e2e_hosted.py --chat-id <ВАШ chat_id>

Run it only with the chat owner's consent: the bot really answers that chat in Telegram
(and, if this chat is the lead recipient, the test lead arrives there too).

What it does, step by step (✅/❌ for each):

1. ``/health`` answers ``{"ok": true}``; ``getWebhookInfo`` shows our URL and no
   ``last_error_message``.
2. The panel: the session cookie is ``Secure`` (and ``HttpOnly``) on HTTPS; one failed login
   with a forged ``X-Real-IP: 1.2.3.4`` header is just a failed login (no crash, no lock);
   the real login works (``ADMIN_PASSWORD`` from ``.env``, CSRF token from the form).
3. In the panel the short title of a question (``pr_current.title``) gets a marker.
4. Signed updates (``X-Telegram-Bot-Api-Secret-Token``) are posted to ``/tg/webhook`` from
   ``--chat-id``: /start → «Начать» → (если был начатый диалог — «Начать заново») →
   «Продвижение» → «Не знаю, нужна рекомендация» ×2 → «Никогда не продвигали» → имя и
   телефон (демонстрационные) → «Звонки» → «Без разницы» → «Отправить заявку». Callback data are
   built from ``leadbot.scenario`` the way the engine builds them.
5. The new lead is found in the panel; its card shows the changed title. The lead is marked
   «Обработана». The title is restored (customer's own text, or the default).

How the message_id of the active keyboard is known: the engine accepts a button press only
for the message that carries the current keyboard. The script cannot read the chat, so
before each press it sends (and immediately deletes) a silent probe message with the bot
token: in a private chat bot and user messages share one counter, so if the bot has sent
something since the previous probe, its last message — the one with the keyboard — is
``probe_id - 1``; if it has sent nothing (a ✅ toggle only edits the keyboard), the active
message is unchanged. Do not write to the bot while the check runs.

No secret value is printed (all output goes through ``Redactor``).
"""
from __future__ import annotations

import argparse
import html
import os
import re
import secrets
import sys
import time
from typing import Any, Callable, Dict, List, Optional, TextIO, Tuple
from urllib.parse import urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from leadbot import scenario  # noqa: E402
from leadbot.telegram import TelegramClient, TelegramError  # noqa: E402
from leadbot.texts import TEXTS  # noqa: E402
from scripts.common import (DEFAULT_ENV_PATH, HTTP_TIMEOUT, SECRET_NAMES,  # noqa: E402
                            WEBHOOK_PATH, Console, PythonAnywhere, ScriptError, check_health,
                            check_pa_settings, pa_domain, read_env_file, server_env)

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
TITLE_KEY = "pr_current.title"
TEST_NAME = "Тест Автопроверка"  # the name validator accepts letters only, so no "E2E"
TEST_PHONE = "+7 900 000-00-00"
TEST_PHONE_NORMALIZED = "+79000000000"
FORGED_IP = "1.2.3.4"
PROBE_TEXT = "🔧 E2E: проверка связи (сообщение сейчас удалится)"
UPDATE_ID_BASE = 10 ** 13  # far above real Telegram update ids: never collides with them

_CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
_LEAD_LINK_RE = re.compile(r'href="/admin/leads/(\d+)"')


# -- the bot side ------------------------------------------------------------------------

class BotApi(TelegramClient):
    """The project's Bot API client plus the two calls the probe needs."""

    def probe(self, chat_id: int) -> int:
        message = self._call("sendMessage", {"chat_id": chat_id, "text": PROBE_TEXT,
                                             "disable_notification": True}) or {}
        message_id = int(message["message_id"])
        try:
            self._call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        except TelegramError:
            pass  # a leftover probe message is harmless
        return message_id


class Step:
    """One update of the walk: a text message or a press of ``node|action|arg``."""

    def __init__(self, title: str, text: Optional[str] = None,
                 callback: Optional[Tuple[str, str, str]] = None, edits_only: bool = False):
        self.title, self.text, self.callback, self.edits_only = title, text, callback, edits_only


def _cb(node_id: str, action: str, option_id: str = "") -> Tuple[str, str, str]:
    """Callback for an option that must exist in the real scenario graph."""
    node = scenario.NODES[node_id]
    if option_id and action in ("pick", "toggle"):
        node.option(option_id)  # KeyError if the graph changed: the walk must be updated
    return node_id, action, option_id


def _label(key: str) -> str:
    return TEXTS[key].default


def promo_walk() -> List[Step]:
    """The shortest branch to a lead: promotion → «never promoted» → contacts → submit."""
    assert scenario.BRANCHES["promo"][:3] == ("pr_what", "pr_types", "pr_current")
    assert scenario.NODES["pr_current"].option("never").final_jump
    assert scenario.BRANCHES["final"] == ("fin_name", "fin_phone", "fin_messenger", "fin_time")
    return [
        Step("/start", text="/start"),
        Step(f"блок конфиденциальности → «{_label('privacy.start')}» (если показан)",
             callback=_cb("privacy", "pick", "start")),
        Step(f"«{_label('resume.opt.restart')}» (если был начатый диалог)",
             callback=_cb("resume", "pick", "restart")),
        Step(f"меню → «{_label('menu.opt.promo')}»", callback=_cb("menu", "pick", "promo")),
        Step(f"что продвигать → «{_label('pr_what.opt.unknown')}»",
             callback=_cb("pr_what", "toggle", "unknown"), edits_only=True),
        Step("«Готово»", callback=_cb("pr_what", "done")),
        Step(f"виды продвижения → «{_label('pr_types.opt.unknown')}»",
             callback=_cb("pr_types", "toggle", "unknown"), edits_only=True),
        Step("«Готово»", callback=_cb("pr_types", "done")),
        Step(f"текущее продвижение → «{_label('pr_current.opt.never')}»",
             callback=_cb("pr_current", "toggle", "never"), edits_only=True),
        Step("«Готово» → переход к контактам", callback=_cb("pr_current", "done")),
        Step(f"имя «{TEST_NAME}»", text=TEST_NAME),
        Step(f"телефон {TEST_PHONE}", text=TEST_PHONE),
        Step(f"мессенджер → «{_label('fin_messenger.opt.calls')}»",
             callback=_cb("fin_messenger", "toggle", "calls"), edits_only=True),
        Step(f"«{_label('fin_messenger.done')}»", callback=_cb("fin_messenger", "done")),
        Step(f"время → «{_label('fin_time.opt.any')}» → сводка",
             callback=_cb("fin_time", "pick", "any")),
        Step(f"сводка → «{_label('summary.opt.submit')}»",
             callback=_cb("summary", "submit")),
    ]


class Walker:
    def __init__(self, session: Any, base_url: str, secret: str, bot: BotApi, chat_id: int):
        self.session = session
        self.url = base_url + WEBHOOK_PATH
        self._secret = secret
        self.bot = bot
        self.chat_id = chat_id
        self.user = {"id": chat_id, "is_bot": False, "first_name": "Тест"}
        self.chat = {"id": chat_id, "type": "private"}
        self.counter = 0
        self.base = UPDATE_ID_BASE + int(time.time()) * 1000
        self.last_probe: Optional[int] = None
        self.active: Optional[int] = None

    def _next_id(self) -> int:
        self.counter += 1
        return self.base + self.counter

    def _post(self, update: dict) -> Optional[str]:
        try:
            resp = self.session.post(self.url, json=update, timeout=HTTP_TIMEOUT,
                                     headers={SECRET_HEADER: self._secret})
        except Exception as exc:
            return f"нет связи ({type(exc).__name__})"
        if resp.status_code == 403:
            return "HTTP 403 — сервер не принял секрет webhook (TELEGRAM_WEBHOOK_SECRET " \
                   "в локальном и серверном .env различаются?)"
        if resp.status_code != 200:
            return f"HTTP {resp.status_code}"
        return None

    def sync(self) -> bool:
        """Probe the chat; True if the bot sent something since the previous probe."""
        probe = self.bot.probe(self.chat_id)
        sent = self.last_probe is None or probe > self.last_probe + 1
        if sent:
            self.active = probe - 1
        self.last_probe = probe
        return sent

    def run(self, step: Step) -> Tuple[Optional[str], str]:
        """(problem or None, note) for one step."""
        update_id = self._next_id()
        if step.text is not None:
            update = {"update_id": update_id, "message": {
                "message_id": update_id % 10 ** 9, "date": int(time.time()),
                "chat": self.chat, "from": self.user, "text": step.text}}
        else:
            node_id, action, arg = step.callback
            update = {"update_id": update_id, "callback_query": {
                "id": f"e2e{update_id}", "from": self.user, "chat_instance": "e2e",
                "data": f"{node_id}|{action}|{arg}",
                "message": {"message_id": self.active or 0, "date": int(time.time()),
                            "chat": self.chat}}}
        problem = self._post(update)
        if problem:
            return problem, ""
        sent = self.sync()
        if step.edits_only:
            return None, ("бот отметил вариант" if not sent else
                          "бот прислал новое сообщение вместо отметки — проверьте чат")
        return None, "бот ответил" if sent else "бот промолчал"


# -- the panel side ----------------------------------------------------------------------

class Panel:
    def __init__(self, session: Any, base_url: str):
        self.session = session
        self.base = base_url
        self.csrf = ""

    def get(self, path: str):
        resp = self.session.get(self.base + path, timeout=HTTP_TIMEOUT, allow_redirects=False)
        self._remember_csrf(resp)
        return resp

    def post(self, path: str, data: Dict[str, str], headers: Optional[dict] = None):
        form = {"csrf_token": self.csrf, **data}
        return self.session.post(self.base + path, data=form, headers=headers or {},
                                 timeout=HTTP_TIMEOUT, allow_redirects=False)

    def _remember_csrf(self, resp) -> None:
        match = _CSRF_RE.search(resp.text or "")
        if match:
            self.csrf = html.unescape(match.group(1))

    def lead_ids(self) -> List[int]:
        resp = self.get("/admin/leads")
        if resp.status_code != 200:
            raise ScriptError(f"список заявок не открылся (HTTP {resp.status_code})")
        return sorted({int(x) for x in _LEAD_LINK_RE.findall(resp.text)})

    def text_value(self, key: str) -> Tuple[str, bool]:
        """(current value, changed in the panel?) of a bot text."""
        group = TEXTS[key].group
        resp = self.get(f"/admin/texts?group={group}")
        if resp.status_code != 200:
            raise ScriptError(f"раздел «Тексты» не открылся (HTTP {resp.status_code})")
        form = re.search(r'<form class="([^"]*)"\s+id="t-' + re.escape(key) + r'"(.*?)</form>',
                         resp.text, re.S)
        if not form:
            raise ScriptError(f"в разделе «Тексты» не найден текст {key}")
        body = form.group(2)
        area = re.search(r"<textarea[^>]*>(.*?)</textarea>", body, re.S)
        field = area.group(1) if area else (
            re.search(r'name="value" value="([^"]*)"', body) or [None, ""])[1]
        return html.unescape(field), "is-changed" in form.group(1)

    def save_text(self, key: str, value: str):
        return self.post(f"/admin/texts/{key}", {"value": value, "section": "texts",
                                                  "group": TEXTS[key].group})

    def reset_text(self, key: str):
        return self.post(f"/admin/texts/{key}/reset", {"section": "texts",
                                                        "group": TEXTS[key].group})


def _cookie_flags(resp) -> Tuple[bool, bool]:
    header = resp.headers.get("Set-Cookie", "") or ""
    lowered = header.lower()
    return "secure" in lowered, "httponly" in lowered


# -- main --------------------------------------------------------------------------------

def parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Проверить бота на хостинге: ответы, заявка, правка текста из панели.")
    parser.add_argument("--chat-id", type=int,
                        help="chat_id чата владельца, от имени которого идут тестовые апдейты "
                             "(бот реально ответит в этот чат)")
    parser.add_argument("--env", default=DEFAULT_ENV_PATH, help="путь к локальному .env")
    parser.add_argument("--base-url", default="",
                        help="адрес бота, по умолчанию https://<PA_USERNAME>.pythonanywhere.com")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None, session: Any = None,
         sleep: Callable[[float], None] = time.sleep, out: Optional[TextIO] = None) -> int:
    args = parse_args(argv)
    console = Console(out)
    if not args.chat_id:
        console.fail("Нужен --chat-id: chat_id чата, куда бот будет отвечать во время проверки "
                     "(например, ваш личный чат с ботом). Без него проверка не запускается.")
        return 2
    try:
        return _run(args, console, session, sleep)
    except ScriptError as exc:
        console.fail(str(exc))
        console.say("Проверка остановлена.")
        return 1


def _settings(args, console: Console, session: Any, sleep) -> Tuple[str, str, str, str]:
    """(base_url, bot token, webhook secret, admin password) from .env (or the server)."""
    local = read_env_file(args.env)
    console.redact.add(*(local.get(name) for name in SECRET_NAMES))
    values = dict(local)
    needed = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "ADMIN_PASSWORD")
    base_url = args.base_url.rstrip("/")
    if any(not (values.get(n) or "").strip() for n in needed) or not base_url:
        username, token, host = check_pa_settings(local)
        base_url = base_url or f"https://{pa_domain(username, host)}"
        if any(not (values.get(n) or "").strip() for n in needed):
            remote = server_env(PythonAnywhere(username, token, host, session, sleep=sleep))
            console.redact.add(*(remote.get(name) for name in SECRET_NAMES))
            for name in needed:
                if not (values.get(name) or "").strip() and remote.get(name):
                    values[name] = remote[name]
                    console.info(f"{name} взят из серверного .env (значение не показывается)")
    missing = [n for n in needed if not (values.get(n) or "").strip()]
    if missing:
        raise ScriptError("Не заданы ни локально, ни на сервере: " + ", ".join(missing) + ".")
    if urlsplit(base_url).scheme != "https":
        raise ScriptError("--base-url должен начинаться с https://")
    return base_url, values["TELEGRAM_BOT_TOKEN"].strip(), \
        values["TELEGRAM_WEBHOOK_SECRET"].strip(), values["ADMIN_PASSWORD"].strip()


def _run(args, console: Console, session: Any, sleep) -> int:
    if session is None:
        import requests
        session = requests.Session()
    base_url, token, secret, password = _settings(args, console, session, sleep)
    chat_id = args.chat_id
    console.say(f"Проверка бота на {base_url} от имени чата {chat_id}")
    number = iter(range(1, 1000))

    def ok(text: str) -> None:
        console.ok(f"Шаг {next(number)}. {text}")

    def fail(text: str) -> None:
        console.fail(f"Шаг {next(number)}. {text}")

    # 1. the service is up and Telegram reaches it
    problem = check_health(session, base_url)
    if problem:
        fail(f"{base_url}/health не отвечает ({problem})")
        return 1
    ok(f"{base_url}/health отвечает {{\"ok\": true}}")
    bot = BotApi(token, session=session)
    try:
        info = bot.get_webhook_info() or {}
    except TelegramError as exc:
        raise ScriptError(f"getWebhookInfo: {exc.description}") from None
    expected = base_url + WEBHOOK_PATH
    if info.get("url") != expected:
        fail(f"webhook указывает на {info.get('url')!r}, а должен на {expected} — "
             f"запустите деплой ещё раз")
    elif info.get("last_error_message"):
        fail(f"getWebhookInfo: последняя ошибка доставки — {info['last_error_message']}")
    else:
        ok(f"getWebhookInfo: webhook {expected}, ошибок доставки нет "
           f"(в очереди: {info.get('pending_update_count', 0)})")

    # 2. the panel: cookie flags, forged X-Real-IP, login
    panel = Panel(session, base_url)
    resp = panel.get("/admin/login")
    secure, http_only = _cookie_flags(resp)
    if resp.status_code != 200 or not panel.csrf:
        fail(f"страница входа в панель не открылась (HTTP {resp.status_code})")
        return 1
    if secure and http_only:
        ok("cookie сессии панели — Secure и HttpOnly (передаётся только по HTTPS)")
    else:
        fail(f"cookie сессии панели без флагов: Secure={secure}, HttpOnly={http_only}")
    resp = panel.post("/admin/login", {"password": "wrong-" + secrets.token_hex(6)},
                      headers={"X-Real-IP": FORGED_IP})
    if resp.status_code == 401:
        ok(f"неверный пароль с поддельным X-Real-IP: {FORGED_IP} — обычный отказ (HTTP 401), "
           f"сервер работает, вход не заблокирован")
    else:
        fail(f"неверный пароль с поддельным X-Real-IP: ожидался HTTP 401, пришёл "
             f"{resp.status_code}")
    panel.get("/admin/login")
    resp = panel.post("/admin/login", {"password": password})
    location = resp.headers.get("Location", "")
    if resp.status_code not in (302, 303) or "/admin/login" in location:
        fail(f"вход в панель с ADMIN_PASSWORD не удался (HTTP {resp.status_code}) — "
             f"пароль в локальном и серверном .env совпадает?")
        return 1
    secure, http_only = _cookie_flags(resp)
    if secure and http_only:
        ok("вход в панель выполнен; cookie сессии Secure и HttpOnly")
    else:
        fail(f"вход выполнен, но cookie без флагов: Secure={secure}, HttpOnly={http_only}")

    # 3. change a question title in the panel
    leads_before = panel.lead_ids()
    original, was_changed = panel.text_value(TITLE_KEY)
    marked = f"{original} [проверка {int(time.time()) % 1000000}]"
    resp = panel.save_text(TITLE_KEY, marked)
    if resp.status_code not in (302, 303):
        fail(f"не удалось изменить текст {TITLE_KEY} в панели (HTTP {resp.status_code})")
        return 1
    ok(f"в панели изменён краткий заголовок вопроса ({TITLE_KEY}): «{marked}»")

    try:
        walked = _walk(console, ok, fail, session, base_url, secret, bot, chat_id)
        if walked:
            _check_lead(ok, fail, panel, leads_before, marked)
    finally:
        _restore(ok, fail, panel, original, was_changed)

    console.say("")
    if console.failed:
        console.say(f"Итог: есть ошибки ({console.failed}). Подробности — в строках с ❌.")
        return 1
    console.say("Итог: всё работает — бот отвечает, заявка сохранена, правка из панели "
                "применилась. Тестовая заявка отмечена обработанной.")
    return 0


def _walk(console, ok, fail, session, base_url, secret, bot, chat_id) -> bool:
    walker = Walker(session, base_url, secret, bot, chat_id)
    try:
        walker.sync()
        for step in promo_walk():
            problem, note = walker.run(step)
            if problem:
                fail(f"бот: {step.title} — {problem}")
                return False
            ok(f"бот: {step.title} — {note}")
    except TelegramError as exc:
        fail(f"Telegram не принял служебное сообщение-пробу: {exc.description}")
        return False
    return True


def _check_lead(ok, fail, panel: Panel, before: List[int], marked: str) -> None:
    new_ids = [i for i in panel.lead_ids() if i not in before]
    for lead_id in sorted(new_ids, reverse=True):
        card = panel.get(f"/admin/leads/{lead_id}")
        if card.status_code != 200 or html.escape(TEST_NAME) not in card.text:
            continue
        ok(f"заявка №{lead_id} сохранена и видна в панели (имя «{TEST_NAME}», "
           f"телефон {TEST_PHONE_NORMALIZED if TEST_PHONE_NORMALIZED in card.text else '?'})")
        if html.escape(marked, quote=False) in card.text:
            ok("в карточке заявки — новый заголовок из панели: правка применилась")
        else:
            fail("в карточке заявки нет нового заголовка — бот не подхватил правку из панели")
        resp = panel.post(f"/admin/leads/{lead_id}/status", {"status": "processed"})
        if resp.status_code in (302, 303):
            ok(f"тестовая заявка №{lead_id} отмечена «Обработана»")
        else:
            fail(f"не удалось отметить заявку №{lead_id} обработанной (HTTP {resp.status_code})")
        return
    fail("новая заявка с тестовым именем в панели не найдена")


def _restore(ok, fail, panel: Panel, original: str, was_changed: bool) -> None:
    resp = panel.save_text(TITLE_KEY, original) if was_changed else panel.reset_text(TITLE_KEY)
    try:
        value, changed = panel.text_value(TITLE_KEY)
    except ScriptError as exc:
        fail(f"текст {TITLE_KEY} не удалось проверить после возврата: {exc}")
        return
    if resp.status_code in (302, 303) and value == original and changed == was_changed:
        ok(f"текст {TITLE_KEY} возвращён: «{original}»"
           + ("" if was_changed else " (по умолчанию)"))
    else:
        fail(f"текст {TITLE_KEY} не вернулся к «{original}» — верните его в панели вручную "
             f"(Тексты → кнопка «Вернуть по умолчанию»)")


if __name__ == "__main__":
    sys.exit(main())
