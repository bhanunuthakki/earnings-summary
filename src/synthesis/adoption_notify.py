"""The auto-adopt announcement + Revert affordance (B4).

Auto-adopting a Tenet or stance straight from a session distill — no owner
tap — is only safe when the owner SEES it happen and can undo it in one tap
(the "receipts, not surprises" bar from the 2026-07-19 Action-UX review:
consequence-first copy, silent success is broken). ``announce_adoption`` pushes
that receipt to Telegram the instant an insight lands ``current`` via the
auto-adopt path, with an inline Revert button (``tr:revert:<id>`` —
``capture.research_notify.dispatch_callback`` routes it to
``synthesis.tenets.revert_tenet``).

Owner ruling (2026-07-19 program review): adoption receipts are EXEMPT from
``research.governor``'s 1-2/day nag budget — this announces something that
already happened (like a bank's "your card was charged" text), not a nag
asking for attention. Sent DIRECTLY via ``capture.telegram``, never routed
through the governor.

Best-effort and NEVER raises: a missing/misconfigured Telegram setup (no bot
token, no known chat id) degrades to a silent skip — the worldview "Adopted
this week" strip is the record of truth regardless of whether the Telegram
receipt went out, so a missing notification is never a hard failure for the
caller's auto-adopt pipeline."""

from __future__ import annotations

import logging
from pathlib import Path

from capture import telegram
from capture.token_store import CaptureSetupError, load_chat_id, load_token
from synthesis.insights import InsightRow

log = logging.getLogger(__name__)

# The insight kinds an auto-adopt announcement covers — mirrors
# synthesis.tenets._REVERTIBLE_KINDS (the Revert button this announcement
# carries only works for these).
_VALID_KINDS = ("tenet", "stance")


def _excerpt(text: str, limit: int = 200) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _repo_root_for(db_path: Path | str, repo_root: Path | str | None) -> Path:
    """The repo root to resolve the Telegram secret files under. ``db_path`` is
    conventionally ``<repo_root>/data/portfolio.db`` — the same
    ``repo_root or db_path.parent.parent`` derivation research_notify's
    ``cp:review`` branch uses when no explicit ``repo_root`` is given."""
    if repo_root is not None:
        return Path(repo_root)
    return Path(db_path).resolve().parent.parent


def announce_adoption(
    row: InsightRow, *, kind: str, db_path: Path | str, repo_root: Path | str | None = None
) -> None:
    """Push a Telegram receipt for an auto-adopted insight, with a Revert
    button. Best-effort — logs and returns on any failure (bad ``kind``,
    missing token/chat config, a network error); never raises into the
    caller's auto-adopt pipeline."""
    try:
        if kind not in _VALID_KINDS:
            log.warning({"event": "adoption_announce_skipped", "reason": "bad_kind", "kind": kind})
            return
        root = _repo_root_for(db_path, repo_root)
        try:
            token = load_token(repo_root=root)
        except CaptureSetupError:
            token = None
        chat_id = load_chat_id(root / "data" / "capture" / "telegram_chat_id.json")
        if not token or not chat_id:
            log.info(
                {
                    "event": "adoption_announce_skipped",
                    "reason": "no_telegram_config",
                    "insight_id": row.id,
                    "kind": kind,
                }
            )
            return
        text = (
            f'Adopted (from your session): "{_excerpt(row.body_md)}" — live now. '
            "Tap Revert to undo."
        )
        keyboard = telegram.inline_keyboard([[("Revert", f"tr:revert:{row.id}")]])
        telegram.send_message(token, chat_id, text, reply_markup=keyboard)
        log.info(
            {
                "event": "adoption_announced",
                "insight_id": row.id,
                "kind": kind,
                "scope_key": row.scope_key,
            }
        )
    except Exception as exc:  # best-effort — never break the auto-adopt caller
        log.warning({"event": "adoption_announce_failed", "insight_id": row.id, "error": str(exc)})
