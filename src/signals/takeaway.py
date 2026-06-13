"""DIET-lane takeaway summarizer for ``media_appearance`` signals.

The information-diet's podcast-RSS leg (S11) stores the raw RSS description in
``signals.body`` at write time. Raw RSS copy is typically marketing language
("tune in to hear X discuss Y") rather than an investment-relevant distillation.
This module provides a post-write Sonnet pass that replaces a short/absent body
with a 2-4 sentence takeaway: what was said that could matter to an investor
tracking this company or fund.

Usage
-----
Called by ``execution/summarize_podcast_episodes.py`` (daily, after the RSS
poll), or directly::

    from signals.takeaway import summarize_pending
    written = summarize_pending(db_path)

The budget row (migration 0103) caps monthly spend at $5 with ``on_exceed='skip'``
so a blown cap simply leaves body as-is rather than blocking the pipeline.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from llm.cli import LLMSetupError, call_llm, is_hard_stop
from llm.prompt_versions import prompt_version_for

log = logging.getLogger(__name__)

PURPOSE = "podcast_takeaway_summary"
_BODY_TOO_SHORT = 280  # raw RSS copy is usually < this; a proper takeaway is longer


def build_prompt(show_name: str, title: str, raw_body: str | None) -> str:
    description_block = (
        f"\nRaw show notes / description from the RSS feed:\n{raw_body.strip()}"
        if raw_body and raw_body.strip()
        else "\n(No description provided in the RSS feed.)"
    )
    version = prompt_version_for(PURPOSE)
    return f"""[podcast_takeaway_summary {version}]
You are a senior equity-research analyst who tracks the information diet of a concentrated long-only portfolio. A podcast episode has been flagged because a company in the portfolio (or a rostered fund manager) was mentioned.

Show: {show_name}
Episode: {title}{description_block}

Write a 2-4 sentence investment-relevant takeaway covering:
- What investment thesis, macro view, or company-specific topic was discussed
- Why it matters for a portfolio-level investor (what to watch or reconsider)
- Any named companies, metrics, or catalysts that surfaced

Do NOT write: "In this episode..." or "Tune in to...". Do NOT pad with generic framing. Write as a briefing sentence you would send to a PM before a weekly review. 150-280 characters target (hard cap: 400 chars).

Return ONLY the takeaway text. No headers, no bullets, no attribution."""


def summarize_one(
    conn: sqlite3.Connection,
    *,
    signal_id: int,
    show_name: str,
    title: str,
    raw_body: str | None,
) -> bool:
    """Summarize one media_appearance signal and UPDATE its body. Return True iff updated.

    Raises on hard LLM stops (budget cap / setup error). Degrades gracefully on
    transient LLM failure (logs + returns False)."""
    prompt = build_prompt(show_name, title, raw_body)
    try:
        takeaway = call_llm(prompt, purpose=PURPOSE).strip()
    except LLMSetupError:
        raise
    except Exception as exc:
        if is_hard_stop(exc):
            raise
        log.warning(
            {
                "event": "podcast_takeaway_llm_error",
                "signal_id": signal_id,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return False
    if not takeaway:
        return False
    takeaway = takeaway[:400]
    conn.execute(
        "UPDATE signals SET body = ? WHERE id = ?",
        (takeaway, signal_id),
    )
    conn.commit()
    log.info(
        {
            "event": "podcast_takeaway_written",
            "signal_id": signal_id,
            "chars": len(takeaway),
        }
    )
    return True


def summarize_pending(
    db_path: Path | str,
    *,
    limit: int = 20,
    now: datetime | None = None,
) -> int:
    """Summarize up to ``limit`` media_appearance signals with short/absent body.

    Returns the number of signals whose body was updated. Skips on a missing
    ``signals`` table (pre-0095 DB). Hard-stops from the LLM budget cap propagate
    (the caller's error handler decides whether to log-and-continue or abort).
    """
    _now = now or datetime.now(UTC)
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error as exc:
        log.warning({"event": "podcast_takeaway_db_open_failed", "error": str(exc)})
        return 0
    try:
        rows = conn.execute(
            """
            SELECT id, firm, title, body
            FROM signals
            WHERE signal_type = 'media_appearance'
              AND (body IS NULL OR length(body) < :threshold)
            ORDER BY published_at DESC
            LIMIT :limit
            """,
            {"threshold": _BODY_TOO_SHORT, "limit": limit},
        ).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return 0

    written = 0
    for row in rows:
        signal_id: int = int(row[0])
        show_name: str = str(row[1]) if row[1] else "Unknown show"
        title: str = str(row[2]) if row[2] else "Untitled"
        raw_body: str | None = str(row[3]) if row[3] else None
        try:
            if summarize_one(
                conn,
                signal_id=signal_id,
                show_name=show_name,
                title=title,
                raw_body=raw_body,
            ):
                written += 1
        except (LLMSetupError, Exception) as exc:
            if is_hard_stop(exc):
                log.warning(
                    {
                        "event": "podcast_takeaway_hard_stop",
                        "signal_id": signal_id,
                        "error": str(exc),
                        "remaining_signals": len(rows) - written,
                    }
                )
                break
            log.warning(
                {
                    "event": "podcast_takeaway_skip",
                    "signal_id": signal_id,
                    "error": str(exc),
                }
            )
    conn.close()
    log.info(
        {
            "event": "podcast_takeaway_done",
            "written": written,
            "checked": len(rows),
            "run_at": _now.isoformat(),
        }
    )
    return written
