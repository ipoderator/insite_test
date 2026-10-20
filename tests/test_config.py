import os

import pytest

from leadbot.config import Config, ConfigError, load_dotenv

FAKE_TOKEN = "123:TEST"


def full_env(**overrides):
    env = {
        "TELEGRAM_BOT_TOKEN": FAKE_TOKEN,
        "TELEGRAM_WEBHOOK_SECRET": "hook_secret-1",
        "ADMIN_PASSWORD": "long-enough-pass",
        "SECRET_KEY": "flask-secret",
        "DATABASE_PATH": "/tmp/x/bot.sqlite3",
        "PUBLIC_BASE_URL": "https://bot.example.org/",
    }
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


def test_from_env_reads_all_fields():
    cfg = Config.from_env(full_env())
    assert cfg.bot_token == FAKE_TOKEN
    assert cfg.webhook_secret == "hook_secret-1"
    assert cfg.admin_password == "long-enough-pass"
    assert cfg.secret_key == "flask-secret"
    assert cfg.database_path == "/tmp/x/bot.sqlite3"
    # trailing slash is dropped so links can be built as base + "/privacy"
    assert cfg.public_base_url == "https://bot.example.org"


def test_database_path_has_default_and_public_url_is_optional():
    cfg = Config.from_env(full_env(DATABASE_PATH=None, PUBLIC_BASE_URL=None))
    assert cfg.database_path == "data/bot.sqlite3"
    assert cfg.public_base_url == ""


@pytest.mark.parametrize(
    "missing",
    ["TELEGRAM_BOT_TOKEN", "TELEGRAM_WEBHOOK_SECRET", "ADMIN_PASSWORD", "SECRET_KEY"],
)
def test_missing_required_variable_names_it(missing):
    with pytest.raises(ConfigError) as exc:
        Config.from_env(full_env(**{missing: None}))
    assert missing in str(exc.value)


def test_blank_value_counts_as_missing():
    with pytest.raises(ConfigError) as exc:
        Config.from_env(full_env(SECRET_KEY="   "))
    assert "SECRET_KEY" in str(exc.value)


def test_short_admin_password_rejected_without_leaking_it():
    with pytest.raises(ConfigError) as exc:
        Config.from_env(full_env(ADMIN_PASSWORD="short9chr"))
    msg = str(exc.value)
    assert "ADMIN_PASSWORD" in msg and "10" in msg
    assert "short9chr" not in msg


def test_ten_char_password_is_enough():
    assert Config.from_env(full_env(ADMIN_PASSWORD="0123456789")).admin_password == "0123456789"


def test_webhook_secret_with_forbidden_chars_rejected():
    # Telegram allows only A-Z, a-z, 0-9, _ and - in secret_token
    with pytest.raises(ConfigError) as exc:
        Config.from_env(full_env(TELEGRAM_WEBHOOK_SECRET="bad secret!"))
    assert "TELEGRAM_WEBHOOK_SECRET" in str(exc.value)


def test_error_never_contains_token_value():
    with pytest.raises(ConfigError) as exc:
        Config.from_env(full_env(SECRET_KEY=None))
    assert FAKE_TOKEN not in str(exc.value)


def test_from_env_defaults_to_os_environ(monkeypatch):
    for k, v in full_env().items():
        monkeypatch.setenv(k, v)
    assert Config.from_env().bot_token == FAKE_TOKEN


def test_load_dotenv_sets_missing_but_keeps_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADBOT_T_EXISTING", "from-env")
    monkeypatch.delenv("LEADBOT_T_NEW", raising=False)
    monkeypatch.delenv("LEADBOT_T_QUOTED", raising=False)
    monkeypatch.delenv("LEADBOT_T_EXPORTED", raising=False)
    monkeypatch.delenv("LEADBOT_T_EMPTY", raising=False)
    path = tmp_path / ".env"
    path.write_text(
        "# comment line\n"
        "\n"
        "LEADBOT_T_EXISTING=from-file\n"
        "LEADBOT_T_NEW=value=with=equals\n"
        'LEADBOT_T_QUOTED="quoted value"\n'
        "export LEADBOT_T_EXPORTED=yes\n"
        "LEADBOT_T_EMPTY=\n"
        "not a valid line\n",
        encoding="utf-8",
    )
    load_dotenv(str(path))
    assert os.environ["LEADBOT_T_EXISTING"] == "from-env"
    assert os.environ["LEADBOT_T_NEW"] == "value=with=equals"
    assert os.environ["LEADBOT_T_QUOTED"] == "quoted value"
    assert os.environ["LEADBOT_T_EXPORTED"] == "yes"
    assert os.environ["LEADBOT_T_EMPTY"] == ""


def test_load_dotenv_missing_file_is_noop(tmp_path):
    load_dotenv(str(tmp_path / "nope.env"))
