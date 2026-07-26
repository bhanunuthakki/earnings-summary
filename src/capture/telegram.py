"""Telegram Bot API client (urllib, no new deps) — long-poll capture + the
two-way research surface.

The app LONG-POLLS Telegram (``getUpdates`` with a server-side ``timeout``), so
there are no open ports and no inbound exposure of the machine. Voice memos
arrive as a ``file_id`` that ``getFile`` + a download resolve to the ``.oga``
bytes. ``send_message`` + ``inline_keyboard`` carry research proposals back into
the thread (Phase 1) with the four actions as buttons.

Parsing (``parse_update`` / ``next_offset`` / ``inline_keyboard``) is pure and
unit-tested; the HTTP calls are thin wrappers over ``urllib``.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from log_redact import redact

log = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"
_FILE = "https://api.telegram.org/file/bot{token}/{file_path}"


class TelegramError(RuntimeError):
    """A Telegram API call failed (network error or ``ok: false`` response)."""


@dataclass(frozen=True, slots=True)
class Update:
    """One decoded update. ``kind`` ∈ {text, voice, callback, document, other}."""

    update_id: int
    kind: str
    chat_id: int | None = None
    message_id: int | None = None
    text: str | None = None
    voice_file_id: str | None = None
    callback_data: str | None = None
    callback_query_id: str | None = None
    document_file_id: str | None = None
    document_file_name: str | None = None
    document_mime_type: str | None = None
    # The ORIGINAL card's text (callback_query.message.text) — carried so a
    # callback handler can editMessageText with the same body plus a state
    # stamp, rather than needing a separate fetch. None for non-callback kinds.
    message_text: str | None = None
    # message.reply_to_message.message_id — set when this text is a REPLY to an
    # earlier bot message (the owner thumbed "reply" on an On My Mind card). The
    # poller matches it to the note carrying that telegram_message_id and routes
    # the text through the shared reply core. None for non-reply messages.
    reply_to_message_id: int | None = None


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _as_dict(value: object) -> dict[str, object]:
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def parse_update(raw: dict[str, object]) -> Update:
    """Decode one raw update into an ``Update``. Callbacks first (the research
    surface), then voice, then text; anything else is 'other'."""
    update_id = _as_int(raw.get("update_id")) or 0

    cq = _as_dict(raw.get("callback_query"))
    if cq:
        msg = _as_dict(cq.get("message"))
        chat = _as_dict(msg.get("chat"))
        return Update(
            update_id=update_id,
            kind="callback",
            chat_id=_as_int(chat.get("id")),
            message_id=_as_int(msg.get("message_id")),
            callback_data=_as_str(cq.get("data")),
            callback_query_id=_as_str(cq.get("id")),
            message_text=_as_str(msg.get("text")),
        )

    msg = _as_dict(raw.get("message"))
    if msg:
        chat = _as_dict(msg.get("chat"))
        chat_id = _as_int(chat.get("id"))
        message_id = _as_int(msg.get("message_id"))
        reply_to = _as_int(_as_dict(msg.get("reply_to_message")).get("message_id"))
        file_id = _as_str(_as_dict(msg.get("voice")).get("file_id"))
        if file_id:
            return Update(update_id, "voice", chat_id, message_id, voice_file_id=file_id)
        doc = _as_dict(msg.get("document"))
        doc_file_id = _as_str(doc.get("file_id"))
        if doc_file_id:
            # Telegram sends caption (user's note alongside the file) in msg["caption"]
            caption = _as_str(msg.get("caption"))
            return Update(
                update_id,
                "document",
                chat_id,
                message_id,
                text=caption,
                document_file_id=doc_file_id,
                document_file_name=_as_str(doc.get("file_name")),
                document_mime_type=_as_str(doc.get("mime_type")),
            )
        text = _as_str(msg.get("text"))
        if text and text.strip():
            return Update(
                update_id,
                "text",
                chat_id,
                message_id,
                text=text,
                reply_to_message_id=reply_to,
            )
        return Update(update_id, "other", chat_id, message_id)

    return Update(update_id, "other")


def parse_updates(raw_list: object) -> list[Update]:
    items = cast("list[object]", raw_list) if isinstance(raw_list, list) else []
    return [u for u in (parse_update(_as_dict(r)) for r in items) if u.update_id]


def next_offset(updates: list[Update]) -> int | None:
    """The ``offset`` to pass next so already-seen updates are not redelivered:
    max(update_id) + 1, or None when there were no updates."""
    ids = [u.update_id for u in updates if u.update_id]
    return max(ids) + 1 if ids else None


def inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, object]:
    """Build a ``reply_markup`` from rows of (label, callback_data) — the
    research-proposal action buttons (approve / research-further / steer / reject)."""
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row] for row in rows
        ]
    }


# --------------------------------------------------------------------------
# HTTP (thin urllib wrappers; the side-effecting layer)
# --------------------------------------------------------------------------


def _request(url: str, *, data: dict[str, object] | None = None, timeout: float = 60) -> object:
    """One Telegram API request. POSTs JSON when ``data`` is given, else GET.
    Returns the ``result`` payload; raises ``TelegramError`` on failure."""
    body = json.dumps(data).encode("utf-8") if data is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = cast("dict[str, object]", json.loads(resp.read().decode("utf-8")))
    except urllib.error.HTTPError as exc:
        description = f"HTTP {exc.code}: {exc.reason}"
        try:
            error_payload: object = json.loads(exc.read().decode("utf-8"))
            if isinstance(error_payload, dict):
                payload_map = cast("dict[str, object]", error_payload)
                raw_description = payload_map.get("description")
                if isinstance(raw_description, str) and raw_description.strip():
                    description = raw_description.strip()
        except (OSError, UnicodeError, ValueError) as parse_exc:
            log.warning(
                {
                    "event": "telegram_error_body_unreadable",
                    "status": exc.code,
                    "error": f"{type(parse_exc).__name__}: {redact(parse_exc)}",
                }
            )
        raise TelegramError(f"telegram api error: {redact(description)}") from None
    except (OSError, ValueError) as exc:
        raise TelegramError(f"telegram request failed: {redact(exc)}") from None
    if not payload.get("ok"):
        raise TelegramError(f"telegram api error: {redact(payload.get('description'))}")
    return payload.get("result")


def get_updates(token: str, *, offset: int | None = None, timeout: int = 50) -> list[Update]:
    """Long-poll for updates. ``timeout`` is the server-side hold (no busy-wait)."""
    params: dict[str, str] = {"timeout": str(timeout)}
    if offset is not None:
        params["offset"] = str(offset)
    url = _API.format(token=token, method="getUpdates") + "?" + urllib.parse.urlencode(params)
    return parse_updates(_request(url, timeout=timeout + 10))


def get_file_path(token: str, file_id: str) -> str:
    """Resolve a ``file_id`` to a downloadable ``file_path`` via getFile."""
    url = (
        _API.format(token=token, method="getFile")
        + "?"
        + urllib.parse.urlencode({"file_id": file_id})
    )
    result = _as_dict(_request(url))
    file_path = _as_str(result.get("file_path"))
    if not file_path:
        raise TelegramError("getFile returned no file_path")
    return file_path


def download_file(token: str, file_path: str, dest: Path | str) -> Path:
    """Download a resolved ``file_path`` (e.g. a voice .oga) to ``dest``."""
    url = _FILE.format(token=token, file_path=file_path)
    out = Path(dest)
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            out.write_bytes(resp.read())
    except OSError as exc:
        raise TelegramError(f"telegram file download failed: {exc}") from exc
    return out


def send_message(
    token: str, chat_id: int, text: str, *, reply_markup: dict[str, object] | None = None
) -> object:
    """Send a message to a chat, optionally with an inline keyboard."""
    data: dict[str, object] = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        data["reply_markup"] = reply_markup
    return _request(_API.format(token=token, method="sendMessage"), data=data)


def answer_callback(token: str, callback_query_id: str, *, text: str | None = None) -> object:
    """Acknowledge a button press so Telegram stops the loading spinner."""
    data: dict[str, object] = {"callback_query_id": callback_query_id}
    if text:
        data["text"] = text
    return _request(_API.format(token=token, method="answerCallbackQuery"), data=data)


def edit_message(
    token: str,
    chat_id: int,
    message_id: int,
    text: str,
    *,
    reply_markup: dict[str, object] | None = None,
) -> object:
    """Edit a previously-sent message's text (and inline keyboard) in place —
    editMessageText. Passing ``reply_markup=None`` strips the keyboard (an empty
    markup, not an absent field, is what clears it on Telegram's side)."""
    data: dict[str, object] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "reply_markup": reply_markup if reply_markup is not None else {"inline_keyboard": []},
    }
    return _request(_API.format(token=token, method="editMessageText"), data=data)
