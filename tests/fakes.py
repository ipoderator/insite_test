"""Test doubles shared by all test modules."""
from __future__ import annotations

import itertools
from typing import Any, Dict, List, Optional, Tuple


class FakeTelegram:
    """In-memory stand-in for ``leadbot.telegram.TelegramClient``.

    * ``calls`` — every call as ``(method_name, kwargs)`` in order;
    * ``messages`` — current text/markup of each message by ``(chat_id, message_id)``;
    * ``fail`` — ``{method_name: exception}``: that method records the call, then raises.
    """

    def __init__(self, file_url_base: str = "https://files.example.test/"):
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.messages: Dict[Tuple[Any, int], Dict[str, Any]] = {}
        self.fail: Dict[str, Exception] = {}
        self.webhook: Dict[str, Any] = {"url": "", "pending_update_count": 0}
        self.commands: List[Any] = []
        self.file_url_base = file_url_base
        self._ids = itertools.count(1)

    # -- helpers -------------------------------------------------------------

    def _record(self, method: str, **kwargs: Any) -> None:
        self.calls.append((method, kwargs))
        if method in self.fail:
            raise self.fail[method]

    def _new_message(self, chat_id, **fields: Any) -> dict:
        message_id = next(self._ids)
        message = {"message_id": message_id, "chat": {"id": chat_id}, **fields}
        self.messages[(chat_id, message_id)] = message
        return message

    def calls_of(self, method: str) -> List[Dict[str, Any]]:
        return [kwargs for name, kwargs in self.calls if name == method]

    def sent_texts(self, chat_id=None) -> List[str]:
        return [kw["text"] for kw in self.calls_of("send_message")
                if chat_id is None or kw["chat_id"] == chat_id]

    def reset(self) -> None:
        self.calls.clear()

    # -- client API ----------------------------------------------------------

    def send_message(self, chat_id, text, reply_markup=None, disable_preview=True) -> dict:
        self._record("send_message", chat_id=chat_id, text=text, reply_markup=reply_markup,
                     disable_preview=disable_preview)
        return self._new_message(chat_id, text=text, reply_markup=reply_markup)

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None) -> None:
        self._record("edit_message_text", chat_id=chat_id, message_id=message_id, text=text,
                     reply_markup=reply_markup)
        msg = self.messages.setdefault((chat_id, message_id),
                                       {"message_id": message_id, "chat": {"id": chat_id}})
        msg.update(text=text, reply_markup=reply_markup)

    def edit_reply_markup(self, chat_id, message_id, reply_markup=None) -> None:
        self._record("edit_reply_markup", chat_id=chat_id, message_id=message_id,
                     reply_markup=reply_markup)
        msg = self.messages.setdefault((chat_id, message_id),
                                       {"message_id": message_id, "chat": {"id": chat_id}})
        msg["reply_markup"] = reply_markup

    def answer_callback(self, callback_id, text=None, alert=False) -> None:
        self._record("answer_callback", callback_id=callback_id, text=text, alert=alert)

    def send_document(self, chat_id, file_id, caption=None) -> dict:
        self._record("send_document", chat_id=chat_id, file_id=file_id, caption=caption)
        return self._new_message(chat_id, document={"file_id": file_id}, caption=caption)

    def send_photo(self, chat_id, file_id, caption=None) -> dict:
        self._record("send_photo", chat_id=chat_id, file_id=file_id, caption=caption)
        return self._new_message(chat_id, photo=[{"file_id": file_id}], caption=caption)

    def get_file_url(self, file_id) -> str:
        self._record("get_file_url", file_id=file_id)
        return self.file_url_base + str(file_id)

    def set_webhook(self, url, secret, allowed_updates) -> bool:
        self._record("set_webhook", url=url, secret=secret, allowed_updates=allowed_updates)
        self.webhook = {"url": url, "pending_update_count": 0,
                        "allowed_updates": list(allowed_updates)}
        return True

    def get_webhook_info(self) -> dict:
        self._record("get_webhook_info")
        return dict(self.webhook)

    def set_my_commands(self, commands) -> bool:
        self._record("set_my_commands", commands=commands)
        self.commands = list(commands)
        return True

    def get_me(self) -> dict:
        self._record("get_me")
        return {"id": 100500, "is_bot": True, "first_name": "Test Bot", "username": "test_bot"}


def make_update(update_id: int, text: Optional[str] = None, chat_id: int = 1, **extra: Any) -> dict:
    """Minimal text-message update for webhook tests."""
    update: Dict[str, Any] = {"update_id": update_id}
    if text is not None:
        update["message"] = {"message_id": update_id, "chat": {"id": chat_id, "type": "private"},
                             "from": {"id": chat_id, "is_bot": False, "first_name": "Тест"},
                             "date": 0, "text": text}
    update.update(extra)
    return update
