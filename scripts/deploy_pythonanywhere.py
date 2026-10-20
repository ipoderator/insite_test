"""Deploy the bot to PythonAnywhere and point the Telegram webhook at it.

    python scripts/deploy_pythonanywhere.py            # deploy
    python scripts/deploy_pythonanywhere.py --dry-run  # show the plan, no network at all
    python scripts/deploy_pythonanywhere.py --python python310

Reads the local ``.env`` (``PA_USERNAME``, ``PA_API_TOKEN``, ``PA_HOST`` and the bot
settings), then through the PythonAnywhere API:

1. uploads the application code (``wsgi.py``, ``requirements.txt``, ``leadbot/``) to
   ``/home/<user>/leadbot-app/`` and a server ``.env`` (without the ``PA_*`` values);
   ``TELEGRAM_WEBHOOK_SECRET`` and ``SECRET_KEY`` are taken from the local ``.env``, else
   kept from the server ``.env``, else generated;
2. creates the web app ``<user>.pythonanywhere.com`` if there is none;
3. writes its WSGI file, sets the source directory and force HTTPS;
4. reloads it and waits for ``/health``;
5. calls ``setWebhook`` (with the secret) and ``setMyCommands``;
6. checks ``getWebhookInfo``.

No secret value is ever printed: every output line goes through ``Redactor``.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
import time
from typing import Any, Callable, Dict, List, Optional, TextIO, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from leadbot.config import Config, ConfigError  # noqa: E402
from leadbot.telegram import TelegramClient, TelegramError  # noqa: E402
from leadbot.texts import TEXTS  # noqa: E402
from scripts.common import (ALLOWED_UPDATES, DEFAULT_ENV_PATH, SECRET_NAMES,  # noqa: E402
                            WEBHOOK_PATH, Console, PythonAnywhere, ScriptError,
                            app_dir, check_health, check_pa_settings, format_env_file,
                            pa_domain, read_env_file, server_env, wsgi_file_path)

DEFAULT_PYTHON = "python311"
CODE_FILES = ("wsgi.py", "requirements.txt")
CODE_DIRS = ("leadbot",)
SKIP_DIRS = {"__pycache__"}
SKIP_SUFFIXES = (".pyc", ".pyo")
REQUIRED_FROM_USER = ("TELEGRAM_BOT_TOKEN", "ADMIN_PASSWORD")
GENERATED = {
    "TELEGRAM_WEBHOOK_SECRET": lambda: secrets.token_urlsafe(32),
    "SECRET_KEY": lambda: secrets.token_hex(32),
}
SERVER_ENV_ORDER = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "ADMIN_PASSWORD",
                    "SECRET_KEY", "DATABASE_PATH", "PUBLIC_BASE_URL")
HEALTH_ATTEMPTS = 10
HEALTH_WAIT = 3
BOT_COMMANDS = (("start", "cmd.start"), ("menu", "cmd.menu"))

WSGI_TEMPLATE = '''"""PythonAnywhere WSGI file, written by scripts/deploy_pythonanywhere.py."""
import sys

APP_DIR = {app_dir!r}
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from wsgi import application  # noqa: E402,F401  (loads {app_dir}/.env)
'''


def collect_files(root: str = ROOT) -> List[Tuple[str, str]]:
    """``(relative path, absolute local path)`` of the application files, sorted."""
    result = []
    for name in CODE_FILES:
        result.append((name, os.path.join(root, name)))
    for top in CODE_DIRS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(root, top)):
            dirnames[:] = sorted(d for d in dirnames
                                 if d not in SKIP_DIRS and not d.startswith("."))
            for filename in sorted(filenames):
                if filename.startswith(".") or filename.endswith(SKIP_SUFFIXES):
                    continue
                full = os.path.join(dirpath, filename)
                result.append((os.path.relpath(full, root).replace(os.sep, "/"), full))
    return result


def build_server_env(local: Dict[str, str], existing: Optional[Dict[str, str]],
                     domain: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    """(server .env values, {name: where the value came from}). ``existing`` None = unknown."""
    values: Dict[str, str] = {}
    sources: Dict[str, str] = {}
    existing_known = existing is not None
    existing = existing or {}
    for name in REQUIRED_FROM_USER + tuple(GENERATED):
        if (local.get(name) or "").strip():
            values[name], sources[name] = local[name].strip(), "из локального .env"
        elif (existing.get(name) or "").strip():
            values[name], sources[name] = existing[name].strip(), "оставлен с сервера"
        elif name in GENERATED:
            values[name] = GENERATED[name]()
            sources[name] = ("сгенерирован" if existing_known else
                             "возьмётся с сервера, если он там есть, иначе будет сгенерирован")
        else:
            values[name], sources[name] = "", "НЕ ЗАДАН"
    # local DATABASE_PATH is a path on this computer; on the server keep its own or default
    values["DATABASE_PATH"] = (existing.get("DATABASE_PATH") or "").strip()
    sources["DATABASE_PATH"] = "с сервера" if values["DATABASE_PATH"] else \
        "по умолчанию (data/bot.sqlite3 в папке приложения)"
    values["PUBLIC_BASE_URL"] = f"https://{domain}"
    sources["PUBLIC_BASE_URL"] = values["PUBLIC_BASE_URL"]
    return {k: values[k] for k in SERVER_ENV_ORDER}, sources


def _validate(values: Dict[str, str]) -> None:
    missing = [n for n in REQUIRED_FROM_USER if not values.get(n)]
    if missing:
        raise ScriptError("В .env не заполнены: " + ", ".join(missing) + ".")
    try:
        Config.from_env(values)
    except ConfigError as exc:  # names variables, never values
        raise ScriptError(str(exc)) from None


def _bot_commands() -> List[Tuple[str, str]]:
    return [(command, TEXTS[key].default) for command, key in BOT_COMMANDS]


def parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Развернуть бота на PythonAnywhere и поставить webhook Telegram.")
    parser.add_argument("--env", default=DEFAULT_ENV_PATH,
                        help="путь к локальному .env (по умолчанию .env в корне проекта)")
    parser.add_argument("--python", default=DEFAULT_PYTHON,
                        help="версия Python веб-приложения, например python311 (по умолчанию)")
    parser.add_argument("--dry-run", action="store_true",
                        help="только показать, что будет сделано; в сеть не ходить")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"python3\d{1,2}", args.python):
        parser.error("--python: ожидается вид python311")
    return args


def main(argv: Optional[List[str]] = None, session: Any = None,
         sleep: Callable[[float], None] = time.sleep, out: Optional[TextIO] = None) -> int:
    args = parse_args(argv)
    console = Console(out)
    try:
        return _run(args, console, session, sleep)
    except ScriptError as exc:
        console.fail(str(exc))
        console.say("Деплой остановлен. Исправьте причину и запустите команду снова.")
        return 1


def _run(args: argparse.Namespace, console: Console, session: Any,
         sleep: Callable[[float], None]) -> int:
    local = read_env_file(args.env)
    console.redact.add(*(local.get(name) for name in SECRET_NAMES))
    problems = []
    try:
        username, token, host = check_pa_settings(local)
    except ScriptError as exc:
        problems.append(str(exc))
        username = token = host = ""
    missing = [n for n in REQUIRED_FROM_USER if not (local.get(n) or "").strip()]
    if missing:
        problems.append("В .env не заполнены: " + ", ".join(missing) + ".")
    if problems:
        raise ScriptError(" ".join(problems))

    domain = pa_domain(username, host)
    base_url = f"https://{domain}"
    target_dir = app_dir(username)
    wsgi_path = wsgi_file_path(domain)
    files = collect_files()
    wsgi_source = WSGI_TEMPLATE.format(app_dir=target_dir)

    console.say(f"Деплой на PythonAnywhere: {base_url} (Python: {args.python})")
    if args.dry_run:
        return _dry_run(console, local, domain, target_dir, wsgi_path, wsgi_source, files)

    if session is None:
        import requests
        session = requests.Session()
    pa = PythonAnywhere(username, token, host, session, sleep=sleep)

    # 1. server .env
    existing = server_env(pa)
    values, sources = build_server_env(local, existing, domain)
    console.redact.add(*(values.get(name) for name in SECRET_NAMES))
    _validate(values)
    console.ok("Настройки для сервера проверены: " + "; ".join(
        f"{name} — {sources[name]}" for name in SERVER_ENV_ORDER))

    # 2. code
    for relative, local_path in files:
        with open(local_path, "rb") as fh:
            pa.upload(f"{target_dir}/{relative}", fh.read())
    console.ok(f"Код загружен: {len(files)} файлов в {target_dir}")
    pa.upload(f"{target_dir}/.env", format_env_file(
        values, "Written by scripts/deploy_pythonanywhere.py. Secrets: do not share.").encode())
    console.ok(f"Серверный .env записан ({target_dir}/.env, значения не показываются)")

    # 3. web app
    apps = {app.get("domain_name"): app for app in pa.list_webapps() if isinstance(app, dict)}
    if domain in apps:
        console.ok(f"Веб-приложение {domain} уже есть")
        current = str(apps[domain].get("python_version") or "")
        if current and current.replace("python", "").replace(".", "") != \
                args.python.replace("python", ""):
            console.info(f"У него Python {current}, а запрошен {args.python}: версия "
                         f"существующего приложения не меняется — при необходимости смените "
                         f"её на вкладке Web на PythonAnywhere.")
    else:
        pa.create_webapp(domain, args.python)
        console.ok(f"Веб-приложение {domain} создано ({args.python})")
    pa.upload(wsgi_path, wsgi_source.encode())
    console.ok(f"WSGI-файл записан: {wsgi_path}")
    # python_version is set only on creation: PATCH rejects the "python311" form POST takes
    pa.patch_webapp(domain, source_directory=target_dir, force_https="true")
    console.ok("Включён принудительный HTTPS, указана папка кода")
    pa.reload(domain)
    console.ok("Веб-приложение перезапущено")

    problem = check_health(session, base_url, attempts=HEALTH_ATTEMPTS, wait=HEALTH_WAIT,
                           sleep=sleep)
    if problem:
        raise ScriptError(f"{base_url}/health не отвечает ({problem}). Откройте вкладку Web "
                          f"на PythonAnywhere → Error log: там причина (например, ошибка "
                          f"конфигурации). Webhook не менялся.")
    console.ok(f"{base_url}/health отвечает {{\"ok\": true}}")

    # 4. Telegram
    tg = TelegramClient(values["TELEGRAM_BOT_TOKEN"], session=session)
    webhook_url = base_url + WEBHOOK_PATH
    try:
        me = tg.get_me() or {}
        console.ok(f"Токен бота действителен: @{me.get('username', '?')}")
        # Telegram keeps the last delivery error across setWebhook: only one dated from this
        # moment on is about the new deploy (dates are whole seconds, hence the floor)
        webhook_set_at = int(time.time())
        tg.set_webhook(webhook_url, values["TELEGRAM_WEBHOOK_SECRET"], ALLOWED_UPDATES)
        console.ok(f"Webhook установлен: {webhook_url} (с секретом)")
        tg.set_my_commands(_bot_commands())
        console.ok("Команды бота заданы: " + ", ".join("/" + c for c, _ in BOT_COMMANDS))
        info = tg.get_webhook_info() or {}
    except TelegramError as exc:
        raise ScriptError(f"Telegram: {exc.description}") from None
    if info.get("url") != webhook_url:
        raise ScriptError(f"getWebhookInfo показывает другой адрес: {info.get('url')!r}")
    if info.get("last_error_message"):
        error_date = info.get("last_error_date")
        if not isinstance(error_date, int) or error_date >= webhook_set_at:
            console.fail(f"getWebhookInfo: последняя ошибка доставки — "
                         f"{info['last_error_message']}")
            return 1
        console.info(f"getWebhookInfo помнит ошибку доставки до этого деплоя — "
                     f"{info['last_error_message']}; после установки webhook новых нет.")
    console.ok(f"getWebhookInfo: адрес верный, ошибок доставки нет "
               f"(в очереди: {info.get('pending_update_count', 0)})")

    console.say("")
    console.say(f"Готово. Бот работает через {webhook_url}; панель — {base_url}/admin")
    console.say("Дальше: привяжите получателя заявок (/owner КОД из раздела «Получатель "
                "заявок») и пройдите docs/CHECKLIST.md.")
    return 0


def _dry_run(console: Console, local: Dict[str, str], domain: str, target_dir: str,
             wsgi_path: str, wsgi_source: str, files: List[Tuple[str, str]]) -> int:
    values, sources = build_server_env(local, None, domain)
    console.redact.add(*(values.get(name) for name in SECRET_NAMES))
    _validate(values)
    console.say("Пробный запуск (--dry-run): в сеть ничего не отправляется.")
    console.say(f"1. Загрузить {len(files)} файлов кода:")
    for relative, _ in files:
        console.info(f"{target_dir}/{relative}")
    console.say(f"2. Записать серверный {target_dir}/.env (значения не показываются):")
    for name in SERVER_ENV_ORDER:
        console.info(f"{name} — {sources[name]}")
    console.say(f"3. Создать веб-приложение {domain}, если его нет.")
    console.say(f"4. Записать WSGI-файл {wsgi_path}:")
    for line in wsgi_source.splitlines():
        console.info(line)
    console.say("5. Включить принудительный HTTPS, перезапустить, дождаться "
                f"https://{domain}/health.")
    console.say(f"6. setWebhook → https://{domain}{WEBHOOK_PATH} (с секретом), "
                "setMyCommands, проверить getWebhookInfo.")
    console.ok("План готов, настройки проверены.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
