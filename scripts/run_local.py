"""Local console chat with the bot — no Telegram, no network, no .env needed.

    python scripts/run_local.py                 # fresh temporary database
    python scripts/run_local.py --db data/local.sqlite3   # keep the dialogue between runs

The real ``Engine`` runs on a SQLite database with ``FakeTelegram`` (from ``tests/fakes.py``)
instead of the Bot API. The console prints every message the bot sends and numbers the
buttons of the active keyboard:

* a number — press that button;
* any other line — send it to the bot as a text message (``/start``, ``/menu`` work too);
* ``/файл`` — send a demo document ``tz.pdf`` (for the «Загрузить ТЗ» step);
* ``/выход`` or Ctrl+D — quit.

A submitted lead goes to a separate demo owner chat; its messages are shown with the
``[владельцу]`` prefix.
"""
from __future__ import annotations

import argparse
import html
import itertools
import os
import re
import sys
import tempfile
import time
from typing import Any, Callable, List, Optional, TextIO, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from leadbot.engine import Engine  # noqa: E402
from leadbot.storage import Storage  # noqa: E402
from tests.fakes import FakeTelegram  # noqa: E402

CLIENT_CHAT = 1001
OWNER_CHAT = 1
DEMO_PHONE = "+79000000000"
QUIT_COMMANDS = {"/выход", "/exit", "/quit"}
FILE_COMMAND = "/файл"

Button = Tuple[str, Optional[str]]  # (caption, callback data | None for "share contact")


def plain(text: str) -> str:
    """What Telegram shows for an HTML message: tags removed, entities decoded."""
    return html.unescape(re.sub(r"<[^>]+>", "", text or ""))


class ConsoleChat:
    def __init__(self, storage: Storage, out: TextIO):
        self.storage = storage
        self.tg = FakeTelegram()
        self.engine = Engine(storage, self.tg, public_base_url="http://localhost:5000")
        self.out = out
        self.user = {"id": CLIENT_CHAT, "is_bot": False, "first_name": "Клиент",
                     "username": "local_client"}
        self.chat = {"id": CLIENT_CHAT, "type": "private"}
        self._seen = 0
        self._ids = itertools.count(int(time.time()) * 1000)
        self.share_contact = False

    # -- sending ---------------------------------------------------------------------

    def _dispatch(self, body: dict) -> None:
        self.engine.handle_update({"update_id": next(self._ids), **body})
        self.show_new()

    def _message(self, **fields: Any) -> None:
        msg = {"message_id": next(self._ids), "date": int(time.time()), "from": self.user,
               "chat": self.chat, **fields}
        self._dispatch({"message": msg})

    def send_text(self, text: str) -> None:
        self._message(text=text)

    def send_document(self) -> None:
        self._message(document={"file_id": "LOCALDOC", "file_unique_id": "localdoc",
                                "file_name": "tz.pdf", "mime_type": "application/pdf",
                                "file_size": 2048})

    def press(self, button: Button) -> None:
        caption, data = button
        if data is None:
            self._message(contact={"phone_number": DEMO_PHONE, "first_name": "Клиент",
                                   "user_id": CLIENT_CHAT})
            return
        self._dispatch({"callback_query": {
            "id": f"cb{next(self._ids)}", "from": self.user, "chat_instance": "local",
            "data": data, "message": {"message_id": self.active_message_id() or 0,
                                      "date": int(time.time()), "chat": self.chat}}})

    # -- reading ---------------------------------------------------------------------

    def active_message_id(self) -> Optional[int]:
        ids = [mid for (chat, mid), msg in self.tg.messages.items()
               if chat == CLIENT_CHAT and (msg.get("reply_markup") or {}).get("inline_keyboard")]
        return max(ids) if ids else None

    def buttons(self) -> List[Button]:
        result: List[Button] = []
        mid = self.active_message_id()
        if mid is not None:
            markup = self.tg.messages[(CLIENT_CHAT, mid)]["reply_markup"]
            result = [(b["text"], b["callback_data"]) for row in markup["inline_keyboard"]
                      for b in row]
        if self.share_contact:
            result.append(("📱 Отправить мой контакт (демо-номер)", None))
        return result

    def say(self, text: str = "") -> None:
        self.out.write(text + "\n")

    def show_new(self) -> None:
        for method, kw in self.tg.calls[self._seen:]:
            prefix = "[владельцу] " if kw.get("chat_id") == OWNER_CHAT else ""
            if method == "send_message":
                markup = kw.get("reply_markup") or {}
                if kw["chat_id"] == CLIENT_CHAT and "keyboard" in markup:
                    self.share_contact = True
                if kw["chat_id"] == CLIENT_CHAT and "remove_keyboard" in markup:
                    self.share_contact = False
                self.say("")
                self.say(prefix + "🤖 " + plain(kw["text"]).replace("\n", "\n   "))
            elif method == "edit_message_text":
                self.say("")
                self.say(prefix + "✏️ (бот изменил сообщение) "
                         + plain(kw["text"]).replace("\n", "\n   "))
            elif method == "answer_callback" and kw.get("text"):
                self.say(f"💬 [всплывающее сообщение] {kw['text']}")
            elif method in ("send_document", "send_photo"):
                self.say(prefix + f"📎 файл {kw.get('file_id')} — {plain(kw.get('caption') or '')}")
        self._seen = len(self.tg.calls)
        buttons = self.buttons()
        if buttons:
            self.say("")
            for number, (caption, _) in enumerate(buttons, 1):
                self.say(f"   {number}. {caption}")


def main(argv: Optional[List[str]] = None, input_fn: Callable[[str], str] = input,
         out: Optional[TextIO] = None) -> int:
    parser = argparse.ArgumentParser(description="Консольный чат с ботом без Telegram.")
    parser.add_argument("--db", default="",
                        help="файл базы SQLite (по умолчанию — временный, удаляется)")
    args = parser.parse_args(argv)
    out = out if out is not None else sys.stdout

    temp_dir = None
    db_path = args.db
    if not db_path:
        temp_dir = tempfile.TemporaryDirectory(prefix="leadbot-local-")
        db_path = os.path.join(temp_dir.name, "bot.sqlite3")
    try:
        storage = Storage(db_path)
        storage.seed_defaults()
        if not (storage.get_setting("owner_chat_id") or "").strip():
            storage.set_setting("owner_chat_id", str(OWNER_CHAT))
        chat = ConsoleChat(storage, out)
        chat.say("Локальный чат с ботом. Номер — нажать кнопку, текст — отправить сообщение, "
                 f"{FILE_COMMAND} — отправить демо-файл ТЗ, /выход — выйти.")
        chat.send_text("/start")
        while True:
            try:
                line = input_fn("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            if line.lower() in QUIT_COMMANDS:
                break
            if line == FILE_COMMAND:
                chat.send_document()
            elif line.isdigit():
                buttons = chat.buttons()
                number = int(line)
                if 1 <= number <= len(buttons):
                    chat.press(buttons[number - 1])
                else:
                    chat.say(f"Нет кнопки с номером {number}. Доступно: 1–{len(buttons)}."
                             if buttons else f"Нет кнопки с номером {number}: кнопок сейчас нет.")
            else:
                chat.send_text(line)
        chat.say("Пока!")
        return 0
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()


if __name__ == "__main__":
    sys.exit(main())
