"""Telegram bot-token loader — the single gitignored secret for capture.

The token is a @BotFather string in the external secrets directory — accepted as a
one-line ``telegram_bot_token`` file or a ``telegram_bot_token.json`` holding the
bare string or ``{"token": "..."}``. A legacy in-repo file is read only during
migration and is never a write destination. A missing/empty token raises
``CaptureSetupError`` — capture is simply unconfigured, not broken.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

from db_paths import resolve_db_path
from runtime.secrets import secret_file

_ENV_OVERRIDE = "CAPTURE_TELEGRAM_TOKEN_FILE"
_BASENAME = "telegram_bot_token"


class CaptureSetupError(RuntimeError):
    """A capture adapter is not configured (missing token). The poller logs this
    and skips the adapter rather than crashing."""


def default_token_path(*, repo_root: Path | None = None) -> Path:
    """External Telegram token path, or the explicit environment override."""
    override = os.environ.get(_ENV_OVERRIDE)
    if override:
        return Path(override)
    root = repo_root
    if root is None:
        try:
            db = resolve_db_path(None)
        except Exception:  # resolve_db_path imports db.DB_PATH; tolerate its absence
            db = None
        root = db.parent.parent if db is not None else Path(__file__).resolve().parents[2]
    return secret_file(_BASENAME, repo_root=root)


def _resolve_existing(base: Path) -> Path | None:
    """The token file: the given path, or its ``.json`` sibling."""
    if base.exists():
        return base
    json_sibling = base.with_suffix(".json")
    if json_sibling.exists():
        return json_sibling
    return None


def _parse_token(text: str) -> str:
    """Accept a bare token, a JSON string, or a ``{"token": "..."}`` wrapper."""
    stripped = text.strip()
    first = stripped[:1]
    if first in ("{", "["):
        try:
            obj: object = json.loads(stripped)
        except ValueError:
            return stripped
        if isinstance(obj, dict):
            tok = cast("dict[str, object]", obj).get("token")
            return tok.strip() if isinstance(tok, str) else ""
        return ""
    if first == '"':
        try:
            obj2: object = json.loads(stripped)
        except ValueError:
            return stripped
        return obj2.strip() if isinstance(obj2, str) else stripped
    return stripped


def load_token(path: Path | str | None = None, *, repo_root: Path | None = None) -> str:
    """Read the bot token, tolerating a ``.json`` file and a ``{"token": ...}``
    wrapper. Raises ``CaptureSetupError`` if absent or empty."""
    base = Path(path) if path is not None else default_token_path(repo_root=repo_root)
    resolved = _resolve_existing(base)
    if resolved is None:
        raise CaptureSetupError(
            f"Telegram bot token not found at {base} (or {base.with_suffix('.json')}). "
            "Configure it in the external earnings-summary secrets directory."
        )
    token = _parse_token(resolved.read_text(encoding="utf-8"))
    if not token:
        raise CaptureSetupError(f"Telegram bot token file is empty/unreadable: {resolved}")
    return token


def default_chat_id_path() -> Path:
    """``data/capture/telegram_chat_id.json`` beside the resolved DB — the
    owner's chat id, persisted by the poller from landed captures so the coach
    can INITIATE (governed pings) without waiting for an inbound message."""
    try:
        db = resolve_db_path(None)
    except Exception:
        db = None
    base = db.parent if db is not None else Path(__file__).resolve().parents[2] / "data"
    return base / "capture" / "telegram_chat_id.json"


def save_chat_id(chat_id: int, path: Path | str | None = None) -> None:
    """Best-effort persist (the poller calls this on every landed capture)."""
    import json

    target = Path(path) if path else default_chat_id_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"chat_id": int(chat_id)}), encoding="utf-8")
    except OSError:
        pass


def load_chat_id(path: Path | str | None = None) -> int | None:
    import json

    target = Path(path) if path else default_chat_id_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    value = cast("dict[str, object]", raw).get("chat_id")
    return int(value) if isinstance(value, int) else None
