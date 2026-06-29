"""The capture poller loop — long-poll Telegram, ingest each musing, advance the
offset cursor.

Runs as a standalone process (a Windows scheduled task via
``execution/capture_poller.py``), never a Flask thread: capture liveness is
decoupled from the dashboard being open, and the task gets RestartOnFailure +
per-run logs for free. The offset is persisted so a restart never re-ingests a
message, and each ingest is independently dedup-guarded on ``tg:<update_id>``.

The loop is pure orchestration over the already-tested ``ingest`` + ``telegram``
seams; ``poll_once`` is unit-testable with a mocked Telegram client.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from pathlib import Path
from typing import cast

from capture import ingest, telegram
from capture.matcher import RosterIndex

log = logging.getLogger(__name__)

# Per-status confirmation replies (plain ASCII — no emoji). A duplicate/empty
# gets no reply (silence = "already had it / nothing to say").
_CONFIRM: dict[str, str] = {
    "landed": "Captured.",
    "needs_ticker": "Captured. (Which ticker? Set it from the Ledger.)",
    "transcription_failed": "Couldn't transcribe that one - I kept the audio, try again?",
    "no_audio": "That voice note came through empty - try again?",
}


def load_offset(path: Path | str) -> int | None:
    """The next getUpdates offset, or None on first run / unreadable file."""
    try:
        raw: object = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(raw, dict):
        offset = cast("dict[str, object]", raw).get("offset")
        if isinstance(offset, int):
            return offset
    return None


def save_offset(path: Path | str, offset: int) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"offset": offset}), encoding="utf-8")


def _confirm(
    token: str, update: telegram.Update, status: str, ticker: str | None, *, enabled: bool
) -> None:
    """Best-effort capture confirmation back into the thread."""
    if not enabled or update.chat_id is None:
        return
    text = _CONFIRM.get(status)
    if status == "landed" and ticker:
        text = f"Captured. ({ticker})"
    if not text:
        return
    # a failed confirmation never blocks capture
    with contextlib.suppress(telegram.TelegramError):
        telegram.send_message(token, update.chat_id, text)


def poll_once(
    token: str,
    *,
    db_path: Path | str | None,
    offset_path: Path | str,
    audio_dir: Path | str,
    roster: RosterIndex | None = None,
    poll_timeout: int = 50,
    confirm: bool = True,
) -> dict[str, int]:
    """One long-poll batch: ingest every update, advance the offset. Returns a
    small counts dict for the run log. Never raises on a per-update failure."""
    offset = load_offset(offset_path)
    updates = telegram.get_updates(token, offset=offset, timeout=poll_timeout)
    counts: dict[str, int] = {"updates": len(updates)}

    def bump(key: str) -> None:
        counts[key] = counts.get(key, 0) + 1

    for update in updates:
        bump(f"kind_{update.kind}")
        if update.kind == "text" and update.text:
            result = ingest.ingest_capture(
                channel="telegram",
                media_kind="text",
                text=update.text,
                external_ref=f"tg:{update.update_id}",
                roster=roster,
                db_path=db_path,
            )
            bump(result.status)
            _confirm(token, update, result.status, result.ticker, enabled=confirm)
        elif update.kind == "voice" and update.voice_file_id:
            dest = Path(audio_dir) / f"tg_{update.update_id}.oga"
            try:
                file_path = telegram.get_file_path(token, update.voice_file_id)
                Path(audio_dir).mkdir(parents=True, exist_ok=True)
                telegram.download_file(token, file_path, dest)
            except telegram.TelegramError:
                bump("download_failed")
                continue
            result = ingest.ingest_capture(
                channel="telegram",
                media_kind="voice",
                audio_path=dest,
                external_ref=f"tg:{update.update_id}",
                roster=roster,
                db_path=db_path,
            )
            bump(result.status)
            if result.status == "landed":
                dest.unlink(missing_ok=True)  # raw audio is transient — purge once landed
            _confirm(token, update, result.status, result.ticker, enabled=confirm)
        # callback updates drive the Phase-1 research surface; ignored in Phase 0.

    new_offset = telegram.next_offset(updates)
    if new_offset is not None:
        save_offset(offset_path, new_offset)
    return counts


def run_forever(
    token: str,
    *,
    db_path: Path | str | None,
    offset_path: Path | str,
    audio_dir: Path | str,
    roster: RosterIndex | None = None,
    poll_timeout: int = 50,
    error_backoff: float = 5.0,
) -> None:
    """Poll until killed. A transient Telegram/network error backs off and
    retries (the loop must survive any single-batch failure); the long-poll
    itself is the pace (no busy-wait)."""
    while True:
        try:
            counts = poll_once(
                token,
                db_path=db_path,
                offset_path=offset_path,
                audio_dir=audio_dir,
                roster=roster,
                poll_timeout=poll_timeout,
            )
            if counts.get("updates"):
                log.info({"event": "capture_poll", **counts})
        except telegram.TelegramError as exc:
            log.warning({"event": "capture_poll_telegram_error", "error": str(exc)})
            time.sleep(error_backoff)
        except Exception as exc:
            log.error({"event": "capture_poll_error", "error": str(exc)})
            time.sleep(error_backoff)
