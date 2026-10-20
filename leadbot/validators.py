"""Pure input validators.

Every function returns ``Result = (ok, normalized_value, error_key)``; the error
key is a ``TextStore`` key (``err.*``) whose default text lives in ``texts.py``.
"""
from __future__ import annotations

import os
import re
from typing import Optional, Tuple

Result = Tuple[bool, Optional[str], Optional[str]]

MAX_FILE_SIZE = 20 * 1024 * 1024  # Bot API getFile download limit
MAX_URL_LENGTH = 500
MAX_URLS = 20
NAME_MIN, NAME_MAX = 2, 60

ALLOWED_FILE_EXTENSIONS = frozenset(
    ["pdf", "doc", "docx", "rtf", "odt", "txt", "xls", "xlsx", "ppt", "pptx",
     "png", "jpg", "jpeg", "zip"]
)
_MIME_TO_EXT = {
    "application/pdf": "pdf",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/rtf": "rtf",
    "text/rtf": "rtf",
    "application/vnd.oasis.opendocument.text": "odt",
    "text/plain": "txt",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "image/png": "png",
    "image/jpeg": "jpg",
    "application/zip": "zip",
    "application/x-zip-compressed": "zip",
}

_PHONE_JUNK_RE = re.compile(r"[\s()\-‐-―.]")
_NAME_EXTRA_CHARS = frozenset(" -'’")

# label: starts/ends with a letter or digit (any script), hyphens inside
_LABEL = r"[^\W_](?:[\w-]{0,61}[^\W_])?"
_TLD = r"(?:[^\W\d_]{2,63}|xn--[a-z0-9-]{1,59})"
_URL_RE = re.compile(
    r"^(?P<scheme>https?://)?"
    r"(?P<host>(?:" + _LABEL + r"\.)+" + _TLD + r")"
    r"(?::\d{1,5})?"
    r"(?:[/?#]\S*)?$",
    re.IGNORECASE,
)


def _fail(key: str) -> Result:
    return (False, None, key)


def validate_name(s: Optional[str]) -> Result:
    if not s:
        return _fail("err.name")
    name = " ".join(s.split())
    if not NAME_MIN <= len(name) <= NAME_MAX:
        return _fail("err.name")
    letters = 0
    for ch in name:
        if ch.isalpha():
            letters += 1
        elif ch not in _NAME_EXTRA_CHARS:
            return _fail("err.name")
    if letters < NAME_MIN:
        return _fail("err.name")
    return (True, name, None)


def validate_phone(s: Optional[str]) -> Result:
    if not s:
        return _fail("err.phone")
    raw = _PHONE_JUNK_RE.sub("", s.strip())
    plus = raw.startswith("+")
    digits = raw[1:] if plus else raw
    if not digits.isdigit() or not digits.isascii():
        return _fail("err.phone")
    if plus:
        if digits.startswith("7") and len(digits) != 11:
            return _fail("err.phone")
        if 10 <= len(digits) <= 15:
            return (True, "+" + digits, None)
        return _fail("err.phone")
    # bare digits: only Russian forms 8XXXXXXXXXX, 7XXXXXXXXXX, 9XXXXXXXXX
    if len(digits) == 11 and digits[0] in "78":
        return (True, "+7" + digits[1:], None)
    if len(digits) == 10 and digits[0] == "9":
        return (True, "+7" + digits, None)
    return _fail("err.phone")


def validate_url(s: Optional[str]) -> Result:
    if not s:
        return _fail("err.url")
    url = s.strip()
    if not url or len(url) > MAX_URL_LENGTH or any(ch.isspace() for ch in url):
        return _fail("err.url")
    match = _URL_RE.match(url)
    if not match:
        return _fail("err.url")
    if not match.group("scheme"):
        url = "https://" + url
        if len(url) > MAX_URL_LENGTH:
            return _fail("err.url")
    return (True, url, None)


def validate_urls(s: Optional[str]) -> Result:
    """One or several links separated by spaces or new lines; value is newline-joined."""
    if not s:
        return _fail("err.url")
    result = []
    for token in s.split():
        token = token.strip(",;")
        if not token:
            continue
        ok, value, _ = validate_url(token)
        if not ok:
            return _fail("err.url")
        if value not in result:
            result.append(value)
    if not result or len(result) > MAX_URLS:
        return _fail("err.url")
    return (True, "\n".join(result), None)


def validate_free_text(s: Optional[str], min_len: int = 2, max_len: int = 1000) -> Result:
    text = (s or "").strip()
    if len(text) < min_len:
        return _fail("err.text_short")
    if len(text) > max_len:
        return _fail("err.text_long")
    return (True, text, None)


def validate_file(
    file_name: Optional[str], mime: Optional[str], size: Optional[int], kind: str
) -> Result:
    """Check a Telegram document/photo; value is the file name to store."""
    if kind == "photo":
        name = file_name or "photo.jpg"
    elif kind == "document":
        if file_name:
            ext = os.path.splitext(file_name)[1].lstrip(".").lower()
            if ext not in ALLOWED_FILE_EXTENSIONS:
                return _fail("err.file_type")
            name = file_name
        else:
            ext = _MIME_TO_EXT.get((mime or "").lower())
            if not ext:
                return _fail("err.file_type")
            name = "file." + ext
    else:
        return _fail("err.file_type")
    if size is not None and size > MAX_FILE_SIZE:
        return _fail("err.file_size")
    return (True, name, None)
