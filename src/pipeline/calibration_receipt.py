"""Calibration receipt -- "when you've been here before" (owner-ratified
design review, 2026-08-02).

At a decision moment (an allocation recommendation's adopt/disposition
buttons, a decision-draft confirm card) the owner has usually made a similar
call before. This module answers, in one muted line, how that track record
reads: ``decisions.outcome_label`` graded against the SAME action verb (and
optionally the same ticker) -- "Your last 4 trims: 2 right, 1 wrong, 1
ungraded."

Signal-quality bar (feedback_inbox_signal_quality_bar.md): a receipt with no
history is noise, so this renders ONLY when the cohort carries at least 2
GRADED rows (``outcome_label`` in correct/wrong/mixed/unfalsifiable) --
otherwise the caller gets an empty string and shows nothing. Deterministic,
zero LLM calls, a plain read over ``decisions``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from html import escape

__all__ = [
    "CalibrationReceipt",
    "compute_calibration_receipt",
    "render_calibration_receipt",
    "render_calibration_receipt_for",
]

_MIN_GRADED = 2
_DEFAULT_LIMIT = 20
_GRADED_LABELS: frozenset[str] = frozenset({"correct", "wrong", "mixed", "unfalsifiable"})


@dataclass(frozen=True, slots=True)
class CalibrationReceipt:
    """One verb's (optionally ticker-scoped) recent track record."""

    verb: str
    ticker: str | None
    total: int
    right: int
    wrong: int
    mixed: int
    unfalsifiable: int
    ungraded: int
    rows: tuple[tuple[str | None, str, str | None], ...]  # (ticker, made_at, outcome_label)

    @property
    def graded(self) -> int:
        return self.right + self.wrong + self.mixed + self.unfalsifiable


def _pluralize(verb: str) -> str:
    v = verb.lower()
    if v.endswith(("s", "x", "ch", "sh")):
        return v + "es"
    return v + "s"


def compute_calibration_receipt(
    conn: sqlite3.Connection,
    *,
    action: str,
    ticker: str | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> CalibrationReceipt | None:
    """The owner's recent track record on ``action`` (optionally scoped to
    ``ticker``), most-recent-first, capped at ``limit`` rows. ``None`` when
    the verb is blank, the read fails (missing/pre-migration DB), or the
    cohort has fewer than 2 GRADED rows -- the caller renders nothing rather
    than a receipt with no signal."""
    verb = action.strip().lower()
    if not verb:
        return None
    t = ticker.strip().upper() if ticker else None
    try:
        if t:
            rows = conn.execute(
                "SELECT ticker, made_at, outcome_label FROM decisions "
                "WHERE recommendation_kind = ? AND UPPER(ticker) = ? "
                "ORDER BY made_at DESC LIMIT ?",
                (verb, t, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT ticker, made_at, outcome_label FROM decisions "
                "WHERE recommendation_kind = ? ORDER BY made_at DESC LIMIT ?",
                (verb, limit),
            ).fetchall()
    except sqlite3.Error:
        return None
    if not rows:
        return None

    right = wrong = mixed = unfalsifiable = ungraded = 0
    cohort: list[tuple[str | None, str, str | None]] = []
    for r in rows:
        label_raw = r["outcome_label"]
        label = str(label_raw) if label_raw else None
        cohort.append((r["ticker"], str(r["made_at"] or ""), label))
        if label == "correct":
            right += 1
        elif label == "wrong":
            wrong += 1
        elif label == "mixed":
            mixed += 1
        elif label == "unfalsifiable":
            unfalsifiable += 1
        else:
            ungraded += 1

    graded = right + wrong + mixed + unfalsifiable
    if graded < _MIN_GRADED:
        return None
    return CalibrationReceipt(
        verb=verb,
        ticker=t,
        total=len(rows),
        right=right,
        wrong=wrong,
        mixed=mixed,
        unfalsifiable=unfalsifiable,
        ungraded=ungraded,
        rows=tuple(cohort),
    )


def render_calibration_receipt(receipt: CalibrationReceipt) -> str:
    """Pure render of an already-computed receipt -- one muted line, the
    cohort's (ticker, date, outcome) rows riding in ``title`` for hover."""
    parts: list[str] = []
    if receipt.right:
        parts.append(f"{receipt.right} right")
    if receipt.wrong:
        parts.append(f"{receipt.wrong} wrong")
    if receipt.mixed:
        parts.append(f"{receipt.mixed} mixed")
    if receipt.unfalsifiable:
        parts.append(f"{receipt.unfalsifiable} unfalsifiable")
    if receipt.ungraded:
        parts.append(f"{receipt.ungraded} ungraded")
    detail = ", ".join(parts)
    line = f"Your last {receipt.total} {_pluralize(receipt.verb)}: {detail}."
    tooltip = "; ".join(
        f"{tk or 'PORTFOLIO'} {made[:10]}: {outcome or 'ungraded'}"
        for tk, made, outcome in receipt.rows
    )
    title = f' title="{escape(tooltip, quote=True)}"' if tooltip else ""
    return f'<p class="cr-receipt"{title}>{escape(line)}</p>'


def render_calibration_receipt_for(
    conn: sqlite3.Connection,
    *,
    action: str,
    ticker: str | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> str:
    """Compute + render in one call; ``""`` when the receipt is suppressed
    (blank verb, read failure, or fewer than 2 graded rows in the cohort)."""
    receipt = compute_calibration_receipt(conn, action=action, ticker=ticker, limit=limit)
    if receipt is None:
        return ""
    return render_calibration_receipt(receipt)
