"""Panel-side rules for the content the customer edits: limits, checks, labels, defaults.

Everything here is pure (no Flask, no database), so the views stay thin and the rules are
in one place. Messages are Russian: they are shown to the customer as is.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from ..leads import MOSCOW
from ..scenario import BRANCHES, NODES
from ..texts import TEXTS, TextDef
from ..validators import validate_url

# -- settings keys owned by the panel ------------------------------------------------

PRIVACY_PAGE_SETTING = "privacy_page_text"   # text of the demo page /privacy
SESSION_EPOCH_SETTING = "admin_session_epoch"  # changes on "Выйти": old cookies stop working
CONTACT_FIELDS = ("contact_telegram", "contact_phone", "contact_email", "privacy_url")

# -- texts -------------------------------------------------------------------------

MESSAGE_LIMIT = 3500
BUTTON_LIMIT = 60
POPUP_LIMIT = 200  # Telegram answerCallbackQuery text limit
# texts the bot shows as a pop-up over the buttons (answerCallbackQuery)
POPUP_KEYS = frozenset({"common.already_sent", "common.button_inactive", "err.empty_multi",
                        "err.no_files"})
# placeholders the bot copes without: the policy link is appended, the name is optional
OPTIONAL_PLACEHOLDERS = {"privacy.text": {"policy"}, "thanks.text": {"name"}}
# question titles: bold in the summary and also the buttons of the «Исправить» list
EDIT_BUTTON_KEYS = frozenset(NODES[n].title_key for n in {"ns_what"}.union(*BRANCHES.values()))
_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")

PLACEHOLDER_HINTS = {
    "name": "имя клиента",
    "policy": "ссылка на политику конфиденциальности",
    "topic": "тематика сферы из раздела «Примеры работ»",
    "value": "значение из раздела «Контакты и ссылки»",
    "label": "название категории",
    "count": "число",
    "url": "ссылка",
    "file": "имя файла",
    "id": "номер заявки",
}

LEAD_STATUSES = (("new", "Новая"), ("processed", "Обработана"))
LEAD_KINDS = ("new", "redesign", "promo", "examples")


def is_button(text_def: TextDef) -> bool:
    """Shown on a button: one line, a button length limit, «кнопка» in the panel."""
    return text_def.kind == "button" or text_def.key in EDIT_BUTTON_KEYS


def text_limit(text_def: TextDef) -> int:
    """Longest allowed value; two PDF buttons are longer than 60 and stay allowed."""
    if text_def.key in POPUP_KEYS:
        return POPUP_LIMIT
    if is_button(text_def):
        return max(BUTTON_LIMIT, len(text_def.default))
    return MESSAGE_LIMIT


def placeholders(text: str) -> List[str]:
    found: List[str] = []
    for name in _PLACEHOLDER_RE.findall(text):
        if name not in found:
            found.append(name)
    return found


def required_placeholders(text_def: TextDef) -> List[str]:
    optional = OPTIONAL_PLACEHOLDERS.get(text_def.key, set())
    return [p for p in placeholders(text_def.default) if p not in optional]


def normalize_text(value: Optional[str]) -> str:
    return (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def check_text(key: str, value: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """(normalized value, None) or (None, Russian error message)."""
    text_def = TEXTS[key]
    text = normalize_text(value)
    if not text:
        return None, "Текст не может быть пустым."
    limit = text_limit(text_def)
    if len(text) > limit:
        return None, f"Слишком длинно: {len(text)} символов, можно не больше {limit}."
    if is_button(text_def) and "\n" in text:
        return None, "Подпись кнопки должна быть в одну строку."
    missing = [p for p in required_placeholders(text_def) if "{" + p + "}" not in text]
    if missing:
        marks = ", ".join("{" + p + "}" for p in missing)
        return None, f"Оставьте в тексте {marks} — на это место бот подставляет данные."
    return text, None


# -- contacts and links ------------------------------------------------------------

_TG_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_TG_LINK_RE = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{3,31})/?$",
                         re.IGNORECASE)
_PHONE_RE = re.compile(r"^\+?[0-9 ()\-]{5,30}$")
_EMAIL_RE = re.compile(r"^[^@\s<>\"]+@[^@\s<>\"]+\.[^@\s<>\"]{2,}$")


def check_contacts(form: Dict[str, str]) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Normalized values of CONTACT_FIELDS and errors by field (empty value = hidden)."""
    values = {f: " ".join((form.get(f) or "").split()) for f in CONTACT_FIELDS}
    errors: Dict[str, str] = {}

    tg = values["contact_telegram"]
    if tg:
        link = _TG_LINK_RE.match(tg)
        name = tg[1:] if tg.startswith("@") else tg
        if link:
            values["contact_telegram"] = "@" + link.group(1)
        elif _TG_USERNAME_RE.match(name):
            values["contact_telegram"] = "@" + name
        else:
            errors["contact_telegram"] = ("Укажите имя пользователя Telegram (например, "
                                          "@site_it) или ссылку вида t.me/site_it.")

    phone = values["contact_phone"]
    if phone and (not _PHONE_RE.match(phone) or sum(ch.isdigit() for ch in phone) < 5):
        errors["contact_phone"] = ("Телефон — цифры, пробелы, «+», «-» и скобки. "
                                   "Пример: +7 999 155-10-43")

    email = values["contact_email"]
    if email and (len(email) > 254 or not _EMAIL_RE.match(email)):
        errors["contact_email"] = "Это не похоже на email. Пример: site-it@mail.ru"

    url = values["privacy_url"]
    if url:
        ok, normalized, _ = validate_url(url)
        if ok:
            values["privacy_url"] = normalized
        else:
            errors["privacy_url"] = ("Это не похоже на ссылку. Пример: "
                                     "https://example.ru/privacy")
    return values, errors


# -- examples ----------------------------------------------------------------------

CATEGORY_BUTTON_LIMIT = BUTTON_LIMIT
CATEGORY_TOPIC_LIMIT = 200
ITEM_TITLE_LIMIT = 200
ITEM_NOTE_LIMIT = 500


def _one_line(value: Optional[str]) -> str:
    return " ".join((value or "").split())


def check_category(form: Dict[str, str]) -> Tuple[Dict[str, object], Dict[str, str]]:
    values: Dict[str, object] = {"button": _one_line(form.get("button")),
                                 "topic": _one_line(form.get("topic"))}
    errors: Dict[str, str] = {}
    if not values["button"]:
        errors["button"] = "Впишите название кнопки."
    elif len(values["button"]) > CATEGORY_BUTTON_LIMIT:
        errors["button"] = f"Название кнопки — не больше {CATEGORY_BUTTON_LIMIT} символов."
    if not values["topic"]:
        errors["topic"] = "Впишите тематику — продолжение фразы «Вот несколько примеров сайтов по …»."
    elif len(values["topic"]) > CATEGORY_TOPIC_LIMIT:
        errors["topic"] = f"Тематика — не больше {CATEGORY_TOPIC_LIMIT} символов."
    return values, errors


def check_item(form: Dict[str, str]) -> Tuple[Dict[str, object], Dict[str, str]]:
    values: Dict[str, object] = {
        "title": _one_line(form.get("title")),
        "url": (form.get("url") or "").strip(),
        "note": normalize_text(form.get("note")),
        "is_demo": bool(form.get("is_demo")),
    }
    errors: Dict[str, str] = {}
    if not values["title"]:
        errors["title"] = "Впишите название примера."
    elif len(values["title"]) > ITEM_TITLE_LIMIT:
        errors["title"] = f"Название — не больше {ITEM_TITLE_LIMIT} символов."
    if values["url"]:
        ok, normalized, _ = validate_url(str(values["url"]))
        if ok:
            values["url"] = normalized
        else:
            errors["url"] = "Это не похоже на ссылку. Пример: https://example.ru"
    if len(str(values["note"])) > ITEM_NOTE_LIMIT:
        errors["note"] = f"Описание — не больше {ITEM_NOTE_LIMIT} символов."
    return values, errors


# -- demo privacy page -------------------------------------------------------------

POLICY_LIMIT = 20000

DEFAULT_PRIVACY_PAGE = """\
Это демонстрационный текст. Замените его на политику конфиденциальности вашей компании \
или укажите в панели ссылку на готовую страницу политики.

1. Кто обрабатывает данные
[НАЗВАНИЕ КОМПАНИИ ИЛИ ИП, ИНН, АДРЕС — впишите]

2. Какие данные мы получаем
Ответы на вопросы анкеты в Telegram-боте, имя, номер телефона, удобный способ и время \
связи, ссылки и файлы, которые вы отправили, а также ваш профиль Telegram (имя \
пользователя и идентификатор).

3. Зачем
Чтобы связаться с вами и подготовить предложение по вашему запросу. Мы не продаём \
ваши данные и не передаём их для рекламы.

4. Где хранятся данные
Сообщения проходят через серверы Telegram. Заявка хранится на сервере, где работает \
бот: [ХОСТИНГ И СТРАНА ХРАНЕНИЯ — впишите].

5. Сколько хранятся
[СРОК ХРАНЕНИЯ — впишите]

6. Как отозвать согласие или удалить данные
Напишите нам: [КОНТАКТ ДЛЯ ОБРАЩЕНИЙ — впишите].
"""


# -- formatting --------------------------------------------------------------------

def moscow_time(value: Optional[str]) -> str:
    """UTC ISO-8601 from the database as «20.10.2026 12:00» Moscow time."""
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(MOSCOW).strftime("%d.%m.%Y %H:%M")


def file_size(size: Optional[int]) -> str:
    if not size:
        return ""
    if size < 1024:
        return f"{size} Б"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} КБ"
    return f"{size / (1024 * 1024):.1f} МБ".replace(".", ",")
