"""SQLite persistence: the only module that knows SQL.

Every public method opens its own short-lived connection, so a ``Storage`` can be
shared between threads of a WSGI server. Times are stored as UTC ISO-8601.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple, Union

PROCESSED_UPDATES_TTL = timedelta(days=7)
LOGIN_FAILURES_TTL = timedelta(days=1)
LEAD_STATUS_NEW = "new"
HISTORY_KEEP = 20  # versions kept per text or setting
HISTORY_TEXT = "text"
HISTORY_SETTING = "setting"

_BIND_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I
_BIND_CODE_LENGTH = 8
_SEED_EXAMPLES_FLAG = "seeded_examples"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS texts (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    chat_id INTEGER PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_updates (
    update_id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_processed_updates_created ON processed_updates(created_at);
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    username TEXT,
    name TEXT,
    phone TEXT,
    items TEXT NOT NULL,
    files TEXT NOT NULL,
    meta TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    delivered INTEGER NOT NULL DEFAULT 0,
    delivery_error TEXT,
    delivered_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_kind_status ON leads(kind, status);
CREATE TABLE IF NOT EXISTS example_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    button TEXT NOT NULL,
    topic TEXT NOT NULL DEFAULT '',
    sort INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS example_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL REFERENCES example_categories(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    is_demo INTEGER NOT NULL DEFAULT 0,
    sort INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_example_items_category ON example_items(category_id);
CREATE TABLE IF NOT EXISTS login_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_failures_ip ON login_failures(ip, created_at);
CREATE TABLE IF NOT EXISTS content_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_content_history_key ON content_history(scope, key, id);
"""

# Seed data from the PDF, pages 24-28. Emojis in the source were lost; chosen by meaning.
# topic = the phrase after "по" in "Вот несколько примеров сайтов по … :".
DEFAULT_CATEGORIES = [
    ("construction", "🏗 Строительство / благоустройство", "строительной тематике",
     ["Сайт №1 – Дома под ключ", "Сайт №2 – Укладка брусчатки",
      "Сайт №3 – Утепление фасадов"]),
    ("repair", "🔨 Ремонтные работы", "ремонтной тематике",
     ["Сайт №1 – Ремонт квартир", "Сайт №2 – Натяжные потолки",
      "Сайт №3 – Отделка балконов"]),
    ("industry", "⚙️ Производство / промышленность", "тематике производства / промышленности",
     ["Сайт №1 – Производство металлоконструкций", "Сайт №2 – Инженерные системы"]),
    ("auto", "🚗 Автомобили", "автомобильной тематике",
     ["Сайт №1 – Автосервис", "Сайт №2 – Шиномонтаж и ТО"]),
    ("specialists", "💼 Специалисты / услуги (бьюти, консультации, юристы и т.д.)",
     "тематике “специалисты”",
     ["Сайт №1 – Консультант-юрист", "Сайт №2 – Бьюти", "Сайт №3 – Коуч/психолог"]),
    ("food", "🍽 Еда / доставка / кулинария", "тематике еды и кулинарии",
     ["Сайт №1 – Кафе / меню", "Сайт №2 – Доставка обедов"]),
    # The PDF repeats "еды и кулинарии" here by mistake.
    ("sport", "🏋️ Спорт / отдых / тренеры", "тематике спорта и отдыха",
     ["Сайт №1 – Персональный тренер", "Сайт №2 – Фитнес-клуб"]),
]

DEFAULT_SETTINGS = {
    "contact_telegram": "@site_it",
    "contact_phone": "+7 999 155-10-43",
    "contact_email": "site-it@mail.ru",
    "privacy_url": "",  # empty: the bot links to the demo page /privacy
}

_CATEGORY_FIELDS = ("slug", "button", "topic", "sort", "active")
_ITEM_FIELDS = ("category_id", "title", "url", "note", "is_demo", "sort")
_LEAD_JSON_FIELDS = ("items", "files", "meta")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _casefold(value: Optional[str]) -> str:
    return (value or "").casefold()


def _digits(value: Optional[str]) -> str:
    return "".join(ch for ch in (value or "") if ch in "0123456789")


def _like(value: str) -> str:
    """``%value%`` for LIKE with the user's own % and _ taken literally (ESCAPE '\\')."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + escaped + "%"


def new_bind_code() -> str:
    return "".join(secrets.choice(_BIND_CODE_ALPHABET) for _ in range(_BIND_CODE_LENGTH))


class Storage:
    def __init__(self, path: str, clock: Optional[Callable[[], datetime]] = None):
        self.path = path
        self._clock = clock or _utc_now
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        self.init_schema()

    # -- infrastructure ------------------------------------------------------

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # SQLite's own LOWER() knows only ASCII: the panel searches Russian names
        conn.create_function("py_casefold", 1, _casefold, deterministic=True)
        conn.create_function("py_digits", 1, _digits, deterministic=True)
        try:
            with conn:  # commit on success, rollback on error
                yield conn
        finally:
            conn.close()

    def _now(self) -> str:
        return self._clock().astimezone(timezone.utc).isoformat(timespec="seconds")

    def _ago(self, delta: timedelta) -> str:
        return (self._clock() - delta).astimezone(timezone.utc).isoformat(timespec="seconds")

    def init_schema(self) -> None:
        conn = sqlite3.connect(self.path, timeout=15)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def seed_defaults(self) -> None:
        """Insert PDF defaults once; never overwrites or re-adds what the owner changed."""
        with self._tx() as conn:
            for key, value in DEFAULT_SETTINGS.items():
                conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                             (key, value))
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                         ("owner_bind_code", new_bind_code()))
            seeded = conn.execute("SELECT 1 FROM settings WHERE key = ?",
                                  (_SEED_EXAMPLES_FLAG,)).fetchone()
            if seeded:
                return
            for pos, (slug, button, topic, titles) in enumerate(DEFAULT_CATEGORIES, start=1):
                cur = conn.execute(
                    "INSERT OR IGNORE INTO example_categories(slug, button, topic, sort, active) "
                    "VALUES (?, ?, ?, ?, 1)", (slug, button, topic, pos * 10))
                if not cur.rowcount:
                    continue
                for i, title in enumerate(titles, start=1):
                    conn.execute(
                        "INSERT INTO example_items(category_id, title, url, note, is_demo, sort) "
                        "VALUES (?, ?, '', '', 1, ?)", (cur.lastrowid, title, i * 10))
            conn.execute("INSERT INTO settings(key, value) VALUES (?, '1')",
                         (_SEED_EXAMPLES_FLAG,))

    # -- settings ------------------------------------------------------------

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._tx() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str, keep_history: bool = False) -> None:
        """Save a setting; ``keep_history`` — remember the previous value for a rollback."""
        with self._tx() as conn:
            if keep_history:
                row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
                old = row["value"] if row else None
                if old != str(value):
                    self._remember(conn, HISTORY_SETTING, key, old)
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))

    def all_settings(self) -> Dict[str, str]:
        with self._tx() as conn:
            return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}

    def rotate_owner_bind_code(self) -> str:
        code = new_bind_code()
        self.set_setting("owner_bind_code", code)
        return code

    # -- text overrides ------------------------------------------------------

    def get_text_override(self, key: str) -> Optional[str]:
        with self._tx() as conn:
            row = conn.execute("SELECT value FROM texts WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_text_override(self, key: str, value: str) -> None:
        """Save the panel's text; the value it replaces goes to the history."""
        with self._tx() as conn:
            row = conn.execute("SELECT value FROM texts WHERE key = ?", (key,)).fetchone()
            old = row["value"] if row else None
            if old == value:
                return
            self._remember(conn, HISTORY_TEXT, key, old)
            conn.execute(
                "INSERT INTO texts(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at", (key, value, self._now()))

    def delete_text_override(self, key: str) -> None:
        with self._tx() as conn:
            row = conn.execute("SELECT value FROM texts WHERE key = ?", (key,)).fetchone()
            if row is None:
                return
            self._remember(conn, HISTORY_TEXT, key, row["value"])
            conn.execute("DELETE FROM texts WHERE key = ?", (key,))

    def all_text_overrides(self) -> Dict[str, str]:
        with self._tx() as conn:
            return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM texts")}

    # -- history of texts and settings ---------------------------------------

    def _remember(self, conn: sqlite3.Connection, scope: str, key: str,
                  value: Optional[str]) -> None:
        """Keep ``value`` — what was there before a change; ``None`` = the default / unset."""
        conn.execute("INSERT INTO content_history(scope, key, value, created_at) "
                     "VALUES (?, ?, ?, ?)", (scope, key, value, self._now()))
        conn.execute(
            "DELETE FROM content_history WHERE scope = ? AND key = ? AND id NOT IN ("
            "SELECT id FROM content_history WHERE scope = ? AND key = ? "
            "ORDER BY id DESC LIMIT ?)", (scope, key, scope, key, HISTORY_KEEP))

    def history(self, scope: str, keys: Sequence[str], limit: int = HISTORY_KEEP) -> List[dict]:
        """Previous values of ``keys``, newest first."""
        if not keys:
            return []
        marks = ", ".join("?" for _ in keys)
        with self._tx() as conn:
            rows = conn.execute(
                f"SELECT id, scope, key, value, created_at FROM content_history "
                f"WHERE scope = ? AND key IN ({marks}) ORDER BY id DESC LIMIT ?",
                (scope, *keys, int(limit)))
            return [dict(r) for r in rows]

    def history_counts(self, scope: str) -> Dict[str, int]:
        with self._tx() as conn:
            rows = conn.execute("SELECT key, COUNT(*) AS n FROM content_history "
                                "WHERE scope = ? GROUP BY key", (scope,))
            return {r["key"]: r["n"] for r in rows}

    def get_history_entry(self, entry_id: int) -> Optional[dict]:
        with self._tx() as conn:
            row = conn.execute("SELECT id, scope, key, value, created_at FROM content_history "
                               "WHERE id = ?", (entry_id,)).fetchone()
        return dict(row) if row else None

    # -- sessions ------------------------------------------------------------

    def load_session(self, chat_id: int) -> Optional[dict]:
        with self._tx() as conn:
            row = conn.execute("SELECT data FROM sessions WHERE chat_id = ?",
                               (chat_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def save_session(self, chat_id: int, data: dict) -> None:
        with self._tx() as conn:
            self._upsert_session(conn, chat_id, data)

    def _upsert_session(self, conn: sqlite3.Connection, chat_id: int, data: dict) -> None:
        conn.execute(
            "INSERT INTO sessions(chat_id, data, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET data = excluded.data, "
            "updated_at = excluded.updated_at",
            (chat_id, json.dumps(data, ensure_ascii=False), self._now()))

    def delete_session(self, chat_id: int) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM sessions WHERE chat_id = ?", (chat_id,))

    # -- processed updates ---------------------------------------------------

    def mark_update_processed(self, update_id: int) -> bool:
        """Remember ``update_id``; False if it was already seen (a Telegram retry)."""
        with self._tx() as conn:
            conn.execute("DELETE FROM processed_updates WHERE created_at < ?",
                         (self._ago(PROCESSED_UPDATES_TTL),))
            cur = conn.execute(
                "INSERT OR IGNORE INTO processed_updates(update_id, created_at) VALUES (?, ?)",
                (update_id, self._now()))
            return cur.rowcount == 1

    # -- leads ---------------------------------------------------------------

    def create_lead(self, kind: str, chat_id: int, username: Optional[str],
                    name: Optional[str], phone: Optional[str], items: List[dict],
                    files: List[dict], meta: dict) -> int:
        with self._tx() as conn:
            return self._insert_lead(conn, kind, chat_id, username, name, phone, items, files,
                                     meta)

    def create_lead_for_session(self, chat_id: int, expected: Dict[str, Any], session: dict,
                                kind: str, username: Optional[str], name: Optional[str],
                                phone: Optional[str], items: List[dict], files: List[dict],
                                meta: dict) -> Optional[int]:
        """Save the lead and replace the chat's session with ``session`` in one transaction.

        Only while the stored session still holds the ``expected`` values: when a parallel
        request has changed it first (a double tap on «Отправить»), nothing is written and
        the result is ``None``.
        """
        with self._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")  # no other writer between the check and the insert
            row = conn.execute("SELECT data FROM sessions WHERE chat_id = ?",
                               (chat_id,)).fetchone()
            stored = json.loads(row["data"]) if row else {}
            if any(stored.get(key) != value for key, value in expected.items()):
                return None
            lead_id = self._insert_lead(conn, kind, chat_id, username, name, phone, items,
                                        files, meta)
            self._upsert_session(conn, chat_id, session)
            return lead_id

    def _insert_lead(self, conn: sqlite3.Connection, kind: str, chat_id: int,
                     username: Optional[str], name: Optional[str], phone: Optional[str],
                     items: List[dict], files: List[dict], meta: dict) -> int:
        now = self._now()
        cur = conn.execute(
            "INSERT INTO leads(kind, chat_id, username, name, phone, items, files, meta, "
            "status, delivered, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (kind, chat_id, username, name, phone,
             json.dumps(items, ensure_ascii=False), json.dumps(files, ensure_ascii=False),
             json.dumps(meta, ensure_ascii=False), LEAD_STATUS_NEW, now, now))
        return cur.lastrowid

    @staticmethod
    def _lead_from_row(row: sqlite3.Row) -> dict:
        lead = dict(row)
        for field in _LEAD_JSON_FIELDS:
            lead[field] = json.loads(lead[field])
        lead["delivered"] = bool(lead["delivered"])
        return lead

    def get_lead(self, lead_id: int) -> Optional[dict]:
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return self._lead_from_row(row) if row else None

    def list_leads(self, kind: Optional[str] = None, status: Optional[str] = None,
                   limit: int = 200) -> List[dict]:
        sql, args = "SELECT * FROM leads WHERE 1 = 1", []
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        with self._tx() as conn:
            return [self._lead_from_row(r) for r in conn.execute(sql, args)]

    def find_leads(self, kind: Optional[str] = None, status: Optional[str] = None,
                   delivered: Optional[bool] = None, query: str = "",
                   created_from: Optional[str] = None, created_to: Optional[str] = None,
                   limit: int = 50, offset: int = 0) -> Tuple[List[dict], int]:
        """A page of leads, newest first, and how many match in all.

        ``query``: «№12» / «#12» — the lead number; only digits (spaces, «+», «-», brackets
        allowed) — the number or a part of the phone; anything else — a part of the name or
        the Telegram username, case-insensitive. ``created_from`` (inclusive) and
        ``created_to`` (exclusive) are UTC ISO-8601 bounds.
        """
        where: List[str] = ["1 = 1"]
        args: List[Any] = []
        if kind:
            where.append("kind = ?")
            args.append(kind)
        if status:
            where.append("status = ?")
            args.append(status)
        if delivered is not None:
            where.append("delivered = ?")
            args.append(1 if delivered else 0)
        if created_from:
            where.append("created_at >= ?")
            args.append(created_from)
        if created_to:
            where.append("created_at < ?")
            args.append(created_to)
        text = " ".join((query or "").split())
        if text:
            digits = _digits(text)
            if text[0] in "№#":
                number = text[1:].strip()
                where.append("id = ?")
                args.append(int(number) if number == _digits(number) and 0 < len(number) < 10
                            else -1)
            elif digits and all(ch in "0123456789 +-()" for ch in text):
                # a Russian number is typed both as 8 999… and +7 999…
                alt = "7" + digits[1:] if digits.startswith("8") and len(digits) > 1 else digits
                where.append("(id = ? OR py_digits(phone) LIKE ? ESCAPE '\\' "
                             "OR py_digits(phone) LIKE ? ESCAPE '\\')")
                args += [int(digits) if len(digits) < 10 else -1, _like(digits), _like(alt)]
            else:
                needle = _like(text.lstrip("@").casefold())
                where.append("(py_casefold(name) LIKE ? ESCAPE '\\' "
                             "OR py_casefold(username) LIKE ? ESCAPE '\\')")
                args += [needle, needle]
        condition = " AND ".join(where)
        with self._tx() as conn:
            total = conn.execute(f"SELECT COUNT(*) AS n FROM leads WHERE {condition}",
                                 args).fetchone()["n"]
            rows = conn.execute(f"SELECT * FROM leads WHERE {condition} "
                                f"ORDER BY id DESC LIMIT ? OFFSET ?",
                                [*args, int(limit), int(offset)])
            return [self._lead_from_row(r) for r in rows], total

    def count_leads(self, status: Optional[str] = None,
                    delivered: Optional[bool] = None) -> int:
        return self.find_leads(status=status, delivered=delivered, limit=0)[1]

    def set_lead_delivery(self, lead_id: int, delivered: bool, error: Optional[str]) -> None:
        now = self._now()
        with self._tx() as conn:
            conn.execute(
                "UPDATE leads SET delivered = ?, delivery_error = ?, "
                "delivered_at = CASE WHEN ? THEN ? ELSE delivered_at END, updated_at = ? "
                "WHERE id = ?",
                (1 if delivered else 0, None if delivered else error,
                 1 if delivered else 0, now, now, lead_id))

    def set_lead_status(self, lead_id: int, status: str) -> None:
        with self._tx() as conn:
            conn.execute("UPDATE leads SET status = ?, updated_at = ? WHERE id = ?",
                         (status, self._now(), lead_id))

    # -- example categories and items ----------------------------------------

    @staticmethod
    def _category_from_row(row: sqlite3.Row) -> dict:
        cat = dict(row)
        cat["active"] = bool(cat["active"])
        return cat

    def list_categories(self, active_only: bool = False) -> List[dict]:
        sql = "SELECT id, slug, button, topic, sort, active FROM example_categories"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY sort, id"
        with self._tx() as conn:
            return [self._category_from_row(r) for r in conn.execute(sql)]

    def get_category(self, id_or_slug: Union[int, str]) -> Optional[dict]:
        column = "id" if isinstance(id_or_slug, int) else "slug"
        with self._tx() as conn:
            row = conn.execute(
                f"SELECT id, slug, button, topic, sort, active FROM example_categories "
                f"WHERE {column} = ?", (id_or_slug,)).fetchone()
        return self._category_from_row(row) if row else None

    def save_category(self, data: Dict[str, Any]) -> int:
        """Insert (no ``id``) or partially update (with ``id``); returns the id."""
        values = {k: data[k] for k in _CATEGORY_FIELDS if k in data}
        if "active" in values:
            values["active"] = 1 if values["active"] else 0
        with self._tx() as conn:
            if data.get("id"):
                if not values.get("slug", True):
                    values.pop("slug")
                return self._update(conn, "example_categories", int(data["id"]), values)
            if not values.get("slug"):
                values["slug"] = "c" + secrets.token_hex(4)
            if values.get("sort") is None:
                values["sort"] = self._next_sort(conn, "example_categories")
            values.setdefault("topic", "")
            values.setdefault("active", 1)
            return self._insert(conn, "example_categories", values)

    def delete_category(self, category_id: int) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM example_items WHERE category_id = ?", (category_id,))
            conn.execute("DELETE FROM example_categories WHERE id = ?", (category_id,))

    @staticmethod
    def _item_from_row(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["is_demo"] = bool(item["is_demo"])
        return item

    def list_items(self, category_id: int) -> List[dict]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT id, category_id, title, url, note, is_demo, sort FROM example_items "
                "WHERE category_id = ? ORDER BY sort, id", (category_id,))
            return [self._item_from_row(r) for r in rows]

    def save_item(self, data: Dict[str, Any]) -> int:
        """Insert (no ``id``) or partially update (with ``id``); returns the id."""
        values = {k: data[k] for k in _ITEM_FIELDS if k in data}
        if "is_demo" in values:
            values["is_demo"] = 1 if values["is_demo"] else 0
        for text_field in ("url", "note"):
            if text_field in values and values[text_field] is None:
                values[text_field] = ""
        with self._tx() as conn:
            if data.get("id"):
                return self._update(conn, "example_items", int(data["id"]), values)
            if values.get("sort") is None:
                values["sort"] = self._next_sort(conn, "example_items",
                                                 "category_id = ?", (values["category_id"],))
            return self._insert(conn, "example_items", values)

    def delete_item(self, item_id: int) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM example_items WHERE id = ?", (item_id,))

    # table/column names below come only from the module's own constants
    @staticmethod
    def _insert(conn: sqlite3.Connection, table: str, values: Dict[str, Any]) -> int:
        cols = list(values)
        cur = conn.execute(
            f"INSERT INTO {table}({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            [values[c] for c in cols])
        return cur.lastrowid

    @staticmethod
    def _update(conn: sqlite3.Connection, table: str, row_id: int, values: Dict[str, Any]) -> int:
        if values:
            assignments = ", ".join(f"{c} = ?" for c in values)
            conn.execute(f"UPDATE {table} SET {assignments} WHERE id = ?",
                         [*values.values(), row_id])
        return row_id

    @staticmethod
    def _next_sort(conn: sqlite3.Connection, table: str, where: str = "1 = 1",
                   args: tuple = ()) -> int:
        row = conn.execute(f"SELECT COALESCE(MAX(sort), 0) AS m FROM {table} WHERE {where}",
                           args).fetchone()
        return row["m"] + 10

    # -- admin login failures ------------------------------------------------

    def record_login_failure(self, ip: str) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM login_failures WHERE created_at < ?",
                         (self._ago(LOGIN_FAILURES_TTL),))
            conn.execute("INSERT INTO login_failures(ip, created_at) VALUES (?, ?)",
                         (ip, self._now()))

    def recent_login_failures(self, ip: str, minutes: int = 15) -> int:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM login_failures WHERE ip = ? AND created_at >= ?",
                (ip, self._ago(timedelta(minutes=minutes)))).fetchone()
        return row["n"]

    def clear_login_failures(self, ip: str) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM login_failures WHERE ip = ?", (ip,))
