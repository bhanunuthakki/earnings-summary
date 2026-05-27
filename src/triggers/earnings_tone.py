"""Earnings-tone sensor — skeleton implementation (PR-N8a).

Fires after a new earnings transcript lands locally. The detection logic
intentionally splits across two PRs so the cheap arrival-detection
half can be reviewed and tested in isolation from the LLM diff pass:

  * **This PR (N8a)** — real ``scan()`` that returns a
    :class:`TriggerCandidate` whenever a transcript for ``ticker``
    arrived in the last 24h. ``should_fire()`` is wired but
    feature-flagged off; ``build_alert`` / ``draft_actions`` raise
    ``NotImplementedError`` rather than emit placeholder text.

  * **Next PR (N8)** — implements the LLM diff pass against
    ``_prompts/earnings_tone_diff.txt``, builds the real alert + queued
    actions, and flips ``_FEATURE_ENABLED`` to ``True``.

"Landed locally" is keyed off ``documents.fetched_at`` (the FK target
of ``transcripts.document_id``) rather than ``transcripts.call_date``.
``call_date`` is the day of the call; ``fetched_at`` is the day we
ingested it. The user could be away from the desk on the day of the
call — they want an alert when *we have* the transcript, not when the
company said the words.

Failure mode: every query path is best-effort. Missing tables, sqlite
errors, malformed rows → return ``[]``. The morning driver runs across
every ticker in the watchlist; one broken DB shouldn't break the whole
fan-out.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from triggers.base import (
    AlertDraft,
    Cadence,
    QueuedActionDraft,
    ThesisAnchor,
    TriggerCandidate,
    UserStateContext,
)

log = logging.getLogger(__name__)

# PR-N8 will flip this to True when the LLM diff pass is wired.
_FEATURE_ENABLED = False

# Detection window: transcripts whose source document was fetched in
# the last 24 hours count as "just landed". The morning driver runs
# daily, so a 24h window catches one quarter's worth of new transcripts
# without re-firing on yesterday's batch.
_ARRIVAL_WINDOW = timedelta(hours=24)


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    """True iff ``table`` exists. Returns False on any sqlite error."""
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _format_threshold(threshold: datetime) -> str:
    """Format a datetime for sqlite TEXT-stored DATETIME comparison.

    The repo's ``documents.fetched_at`` lands as the default sqlalchemy
    representation — ``'YYYY-MM-DD HH:MM:SS[.ffffff]'`` — so string
    compare against the same shape is correct.
    """
    return threshold.strftime("%Y-%m-%d %H:%M:%S")


class EarningsToneTrigger:
    """``Cadence.ON_EARNINGS`` sensor over the ``transcripts`` table."""

    kind: ClassVar[str] = "earnings_tone"
    cadence: ClassVar[Cadence] = Cadence.ON_EARNINGS

    def scan(
        self, ticker: str, db: sqlite3.Connection
    ) -> list[TriggerCandidate]:
        """Emit one candidate per fresh transcript arrival in the last 24h.

        Joins ``transcripts`` to ``documents`` so the freshness gate is
        ``documents.fetched_at`` (when we ingested it), not
        ``transcripts.call_date`` (which is often NULL at ingest and
        reflects the call itself, not arrival).

        Returns ``[]`` when either table is missing, the query errors, or
        no transcript arrived in window.
        """
        if not _has_table(db, "transcripts") or not _has_table(db, "documents"):
            log.debug(
                {
                    "event": "earnings_tone_scan_skipped",
                    "ticker": ticker,
                    "reason": "transcripts_or_documents_table_missing",
                }
            )
            return []

        now = datetime.now(UTC).replace(tzinfo=None)
        threshold = _format_threshold(now - _ARRIVAL_WINDOW)
        try:
            row = db.execute(
                "SELECT t.id, t.fiscal_period_type, t.period_end, d.fetched_at"
                + " FROM transcripts t"
                + " JOIN documents d ON d.id = t.document_id"
                + " WHERE t.ticker = ? AND d.fetched_at >= ?"
                + " ORDER BY d.fetched_at DESC"
                + " LIMIT 1",
                (ticker, threshold),
            ).fetchone()
        except sqlite3.Error as exc:
            log.debug(
                {
                    "event": "earnings_tone_scan_query_failed",
                    "ticker": ticker,
                    "error": str(exc),
                }
            )
            return []

        if row is None:
            return []

        transcript_id_raw, fiscal_period_type_raw, period_end_raw, fetched_at_raw = (
            row[0],
            row[1],
            row[2],
            row[3],
        )
        if (
            not isinstance(transcript_id_raw, int)
            or not isinstance(fiscal_period_type_raw, str)
            or not isinstance(period_end_raw, str)
            or not isinstance(fetched_at_raw, str)
        ):
            log.debug(
                {
                    "event": "earnings_tone_scan_row_malformed",
                    "ticker": ticker,
                }
            )
            return []

        fiscal_period = period_end_raw[:4]  # calendar year of period end
        key = f"{ticker}:{fiscal_period_type_raw}:{fiscal_period}"
        candidate = TriggerCandidate(
            ticker=ticker,
            kind=self.kind,
            key=key,
            evidence={
                "transcript_id": transcript_id_raw,
                "fiscal_period": fiscal_period,
                "fiscal_period_type": fiscal_period_type_raw,
                "published_at": fetched_at_raw,
            },
            computed_at=now,
        )
        return [candidate]

    def should_fire(
        self,
        candidate: TriggerCandidate,
        user_state: UserStateContext,
    ) -> bool:
        """Feature-flagged off until PR-N8 wires the LLM diff pass.

        Kept as a module-level constant (not a constructor arg) so PR-N8
        can grep-and-replace cleanly. The truthiness clause is academic
        while the flag is False; it documents that a real fire decision
        will at minimum require a real candidate.
        """
        _ = user_state
        return _FEATURE_ENABLED and bool(candidate)

    def build_alert(
        self,
        candidate: TriggerCandidate,
        anchor: ThesisAnchor | None,
    ) -> AlertDraft:
        _ = candidate, anchor
        raise NotImplementedError(
            "Earnings-tone alert generation lands in PR-N8 (LLM diff pass)."
        )

    def draft_actions(
        self,
        alert: AlertDraft,
        candidate: TriggerCandidate,
    ) -> list[QueuedActionDraft]:
        _ = alert, candidate
        raise NotImplementedError("Earnings-tone action drafting lands in PR-N8.")
