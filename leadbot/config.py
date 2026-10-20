"""Configuration from environment variables and an optional .env file.

Secrets live only in the environment (or a git-ignored .env next to wsgi.py).
Error messages name variables, never their values.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Mapping, Optional

DEFAULT_DATABASE_PATH = "data/bot.sqlite3"
MIN_ADMIN_PASSWORD_LENGTH = 10

_REQUIRED = (
    ("TELEGRAM_BOT_TOKEN", "токен бота от @BotFather"),
    ("TELEGRAM_WEBHOOK_SECRET", "секрет webhook"),
    ("ADMIN_PASSWORD", "пароль веб-панели"),
    ("SECRET_KEY", "ключ сессий Flask"),
)
# Telegram's secret_token: 1-256 chars, A-Z a-z 0-9 _ -
_WEBHOOK_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_ENV_LINE_RE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


class ConfigError(Exception):
    """Raised when the environment is incomplete or invalid (message is Russian)."""


@dataclass(frozen=True)
class Config:
    bot_token: str
    webhook_secret: str
    admin_password: str
    secret_key: str
    database_path: str
    public_base_url: str

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Config":
        if env is None:
            env = os.environ

        def get(name: str) -> str:
            return (env.get(name) or "").strip()

        problems = []
        missing = [f"{name} ({what})" for name, what in _REQUIRED if not get(name)]
        if missing:
            problems.append("не заданы переменные окружения: " + ", ".join(missing))

        password = get("ADMIN_PASSWORD")
        if password and len(password) < MIN_ADMIN_PASSWORD_LENGTH:
            problems.append(
                f"ADMIN_PASSWORD слишком короткий: нужно не меньше "
                f"{MIN_ADMIN_PASSWORD_LENGTH} символов"
            )

        secret = get("TELEGRAM_WEBHOOK_SECRET")
        if secret and not _WEBHOOK_SECRET_RE.match(secret):
            problems.append(
                "TELEGRAM_WEBHOOK_SECRET может содержать только латинские буквы, "
                "цифры, «_» и «-» (до 256 символов)"
            )

        if problems:
            raise ConfigError(
                "Ошибка конфигурации: " + "; ".join(problems)
                + ". Заполните .env по образцу .env.example."
            )

        return cls(
            bot_token=get("TELEGRAM_BOT_TOKEN"),
            webhook_secret=secret,
            admin_password=password,
            secret_key=get("SECRET_KEY"),
            database_path=get("DATABASE_PATH") or DEFAULT_DATABASE_PATH,
            public_base_url=get("PUBLIC_BASE_URL").rstrip("/"),
        )


def load_dotenv(path: str) -> None:
    """Load KEY=VALUE lines into os.environ without overriding variables already set.

    A missing file is silently ignored. Comments (#), blank lines, an optional
    ``export`` prefix and single/double quotes around the value are supported.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_LINE_RE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key not in os.environ:
            os.environ[key] = value
