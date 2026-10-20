"""Thin Telegram Bot API client on top of ``requests``.

The bot token is part of every API URL, so URLs are never logged and network
exceptions (whose text contains the URL) are replaced by a token-free error.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

import requests

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
NOT_MODIFIED = "message is not modified"

# urllib3 logs request lines (with the token in the path) at DEBUG level.
_urllib3_log = logging.getLogger("urllib3.connectionpool")
if _urllib3_log.level == logging.NOTSET:
    _urllib3_log.setLevel(logging.INFO)


class TelegramError(Exception):
    """Bot API or network failure. ``description`` never contains the token."""

    def __init__(self, description: str, error_code: Optional[int] = None):
        super().__init__(description)
        self.description = description
        self.error_code = error_code


class TelegramClient:
    def __init__(self, token: str, timeout: float = 15, session: Optional[requests.Session] = None):
        self._token = token
        self.timeout = timeout
        self.session = session if session is not None else requests.Session()

    def __repr__(self) -> str:
        return "TelegramClient(token=***)"

    # -- transport -----------------------------------------------------------

    def _call(self, method: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        body = {k: v for k, v in (payload or {}).items() if v is not None}
        url = f"{API_BASE}/bot{self._token}/{method}"
        network_error = None
        try:
            resp = self.session.post(url, json=body, timeout=self.timeout)
        except requests.RequestException as exc:
            network_error = type(exc).__name__
        if network_error is not None:
            # raised outside the except block: no chained exception with the URL
            log.warning("Telegram %s: network error %s", method, network_error)
            raise TelegramError(f"network error: {network_error}")

        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            log.warning("Telegram %s: non-JSON response, HTTP %s", method, resp.status_code)
            raise TelegramError(f"HTTP {resp.status_code}: invalid response", resp.status_code)
        if data.get("ok"):
            return data.get("result")

        description = str(data.get("description") or f"HTTP {resp.status_code}")
        code = data.get("error_code")
        if NOT_MODIFIED in description:
            return None
        log.warning("Telegram %s failed: %s %s", method, code, description)
        raise TelegramError(description, code)

    # -- methods -------------------------------------------------------------

    def send_message(self, chat_id, text, reply_markup=None, disable_preview=True) -> dict:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                   "reply_markup": reply_markup}
        if disable_preview:
            payload["link_preview_options"] = {"is_disabled": True}
        return self._call("sendMessage", payload)

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None) -> None:
        self._call("editMessageText", {
            "chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML",
            "reply_markup": reply_markup, "link_preview_options": {"is_disabled": True},
        })

    def edit_reply_markup(self, chat_id, message_id, reply_markup=None) -> None:
        self._call("editMessageReplyMarkup", {
            "chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup,
        })

    def answer_callback(self, callback_id, text=None, alert=False) -> None:
        self._call("answerCallbackQuery", {
            "callback_query_id": callback_id, "text": text, "show_alert": bool(alert),
        })

    def send_document(self, chat_id, file_id, caption=None) -> dict:
        return self._call("sendDocument", {
            "chat_id": chat_id, "document": file_id, "caption": caption,
            "parse_mode": "HTML" if caption else None,
        })

    def send_photo(self, chat_id, file_id, caption=None) -> dict:
        return self._call("sendPhoto", {
            "chat_id": chat_id, "photo": file_id, "caption": caption,
            "parse_mode": "HTML" if caption else None,
        })

    def get_file_url(self, file_id) -> str:
        """Download URL for server-side fetching only: it embeds the token, never expose it."""
        result = self._call("getFile", {"file_id": file_id}) or {}
        path = result.get("file_path")
        if not path:
            raise TelegramError("file is not available for download")
        return f"{API_BASE}/file/bot{self._token}/{path}"

    def set_webhook(self, url, secret, allowed_updates) -> Any:
        return self._call("setWebhook", {
            "url": url, "secret_token": secret, "allowed_updates": list(allowed_updates),
        })

    def get_webhook_info(self) -> dict:
        return self._call("getWebhookInfo")

    def set_my_commands(self, commands) -> Any:
        return self._call("setMyCommands", {"commands": _normalize_commands(commands)})

    def get_me(self) -> dict:
        return self._call("getMe")


def _normalize_commands(commands: Iterable) -> List[Dict[str, str]]:
    result = []
    for item in commands:
        if isinstance(item, dict):
            result.append({"command": item["command"], "description": item["description"]})
        else:
            command, description = item
            result.append({"command": command, "description": description})
    return result
