"""Telegram bot-token loader — the single gitignored secret for capture.

The token is a @BotFather string kept at ``data/secrets/telegram_bot_token`` (one
line). ``data/`` and ``**/secrets/`` are already gitignored (mirrors the
portfolio-pins + gsheets-token convention), so the secret never enters the repo.
A missing/empty token raises ``CaptureSetupError`` — capture is simply
unconfigured, not broken.
"""

from __future__ import annotations

import os
from pathlib import Path

from db_paths import resolve_db_path

_ENV_OVERRIDE = "CAPTURE_TELEGRAM_TOKEN_FILE"


class CaptureSetupError(RuntimeError):
    """A capture adapter is not configured (missing token / credentials). The
    poller logs this and skips the adapter rather than crashing."""


def default_token_path() -> Path:
    """``data/secrets/telegram_bot_token`` beside the resolved DB, or the
    ``CAPTURE_TELEGRAM_TOKEN_FILE`` override. Falls back to the package-relative
    ``data/`` when no DB path is configured (e.g. a bare CLI invocation)."""
    override = os.environ.get(_ENV_OVERRIDE)
    if override:
        return Path(override)
    try:
        db = resolve_db_path(None)
    except Exception:  # resolve_db_path imports db.DB_PATH; tolerate its absence
        db = None
    base = db.parent if db is not None else Path(__file__).resolve().parents[2] / "data"
    return base / "secrets" / "telegram_bot_token"


def load_token(path: Path | str | None = None) -> str:
    """Read the bot token (stripped). Raises ``CaptureSetupError`` if the file is
    absent or empty."""
    token_path = Path(path) if path is not None else default_token_path()
    if not token_path.exists():
        raise CaptureSetupError(
            f"Telegram bot token not found at {token_path}. Create it with your "
            "@BotFather token (one line); data/secrets/ is gitignored."
        )
    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        raise CaptureSetupError(f"Telegram bot token file is empty: {token_path}")
    return token
