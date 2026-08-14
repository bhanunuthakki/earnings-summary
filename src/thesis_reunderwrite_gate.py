"""Scored-miss gate on re-underwrites (monthly_red_team.md Phase 3, PR7).

The NVO precedent named in the directive: NVO's GLP-1 thesis broke on US
pricing (``thesis_state.breach_status`` went to ``warn``/``breach``) and was
then RE-UNDERWRITTEN — the holdings JSON's ``thesis`` text was rewritten to a
fresh narrative — with no calibration entry ever recorded against the belief
that broke. The old belief simply vanished; nothing was ever scored wrong.
That is how thesis migration compounds without the record ever learning
anything: a graded miss is the one thing that makes the compounding loop
(``decision_calibration`` / ``calibration_coach``) work, and a silent
re-underwrite skips it.

This module is the ONE choke point that detects a RE-UNDERWRITE and blocks it
until a scored-miss calibration entry exists. It is deliberately narrow:

  RE-UNDERWRITE predicate — a ticker's ``thesis_state.breach_status`` is
      ``warn`` or ``breach`` (``compute.thesis_evaluator.BreachStatus`` collapses
      ``unresolved`` to ``ok``, so those two values are the only "thesis is
      currently broken" states) AND the incoming thesis text differs from the
      currently-stored text after whitespace normalization ("materially
      changed" — a re-flow/typo fix is not a re-underwrite; a rewritten
      narrative is). Both conditions are read BEFORE the caller overwrites
      ``thesis_state`` with the new verdict, so the pre-change breach state is
      what gates, not the freshly (possibly loosened) re-evaluated one.

  scored-miss lookup — a ``decisions`` row for the ticker with
      ``recommendation_kind = 'scored_miss'`` (written by
      ``execution/log_scored_miss.py``) whose ``created_at`` is on or after the
      breach's onset (the earliest ``thesis_evaluations`` row in the current
      unbroken warn/breach streak; falls back to the current
      ``thesis_state.last_updated`` when the evaluations history is absent —
      an honest, slightly-conservative substitute, documented here rather than
      silently approximated).

The caller (``compute.thesis_evaluator.persist_verdict``) raises
:class:`ReUnderwriteBlockedError` when the gate fires and ``override`` was not
passed; ``override=True`` never silently bypasses — the caller is responsible
for logging the override loudly (this module only computes the verdict, it
never blocks side-effect-free calls itself).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from compute.thesis_evaluation_episodes import episode_history_source

_BREACHED_STATUSES: frozenset[str] = frozenset({"warn", "breach"})
_WS_RX = re.compile(r"\s+")

RECOMMENDATION_KIND_SCORED_MISS = "scored_miss"


class ReUnderwriteBlockedError(RuntimeError):
    """Raised when a thesis re-underwrite is detected with no scored-miss
    calibration entry on file. Carries the ticker + onset so the caller's
    error message (and any UI surfacing it) can be actionable without
    re-deriving them."""

    def __init__(self, ticker: str, *, onset: str | None) -> None:
        self.ticker = ticker
        self.onset = onset
        onset_note = f" (breach on file since {onset})" if onset else ""
        super().__init__(
            f"BLOCKED: {ticker} is being re-underwritten while its thesis is "
            f"warn/breach{onset_note}, with no scored-miss calibration entry on "
            f"file. Run `python execution/log_scored_miss.py --ticker {ticker} "
            "--conviction ... --belief ... --outcome ...` to record what was "
            "believed and what happened FIRST, then re-run this evaluation. "
            "Escape hatch: pass override=True (CLI: --override) — every use is "
            "logged loudly, never silent."
        )


@dataclass(frozen=True, slots=True)
class GateVerdict:
    """The gate's read for one persist attempt. ``is_reunderwrite`` is the
    predicate alone (would this write count as a re-underwrite, regardless of
    scoring); ``blocked`` additionally requires no qualifying scored-miss row."""

    is_reunderwrite: bool
    blocked: bool
    onset: str | None
    scored_miss_found: bool


def is_material_change(old_thesis: str | None, new_thesis: str | None) -> bool:
    """True when the thesis text changed beyond whitespace reflow.

    Normalization collapses runs of whitespace and strips the ends — a
    re-wrapped paragraph or a trailing-space fix is not a re-underwrite, a
    rewritten narrative is. Deliberately simple (no fuzzy/edit-distance
    threshold): the directive's failure mode is a WHOLESALE rewrite (NVO's
    thesis text replaced end to end), not a one-word tweak, so an exact
    normalized-inequality check catches the real cases without false-positive
    noise on every evaluator run that merely re-persists the same text.
    """
    old_norm = _WS_RX.sub(" ", (old_thesis or "")).strip()
    new_norm = _WS_RX.sub(" ", (new_thesis or "")).strip()
    if not new_norm:
        return False  # nothing to underwrite yet — not a re-underwrite
    return old_norm != new_norm


def breach_onset(conn: sqlite3.Connection, ticker: str) -> str | None:
    """The earliest ``evaluated_at`` in the current unbroken warn/breach streak
    read off ``thesis_evaluations`` (newest-first, walked back while status
    stays non-OK). ``None`` when the table/ticker has no history — the caller
    falls back to ``thesis_state.last_updated``."""
    try:
        source = episode_history_source(conn)
        rows = conn.execute(
            f"SELECT evaluated_at, overall_status FROM {source.relation} "
            f"WHERE ticker = ? ORDER BY {source.first_seen_column} DESC",  # nosec B608 -- trusted closed relation
            (ticker,),
        ).fetchall()
    except (sqlite3.OperationalError, ValueError):
        return None
    onset: str | None = None
    for row in rows:
        status = str(row["overall_status"] or "").lower()
        if status in _BREACHED_STATUSES:
            onset = str(row["evaluated_at"])
        else:
            break
    return onset


def has_scored_miss_since(conn: sqlite3.Connection, ticker: str, since: str | None) -> bool:
    """True iff a ``decisions`` row logs a scored miss for ``ticker`` created on
    or after ``since`` (``None`` = any time — the honest-conservative fallback
    when no onset could be derived). Degrades to False (never raises) on a
    pre-decisions-table DB, matching the store's best-effort posture."""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(decisions)").fetchall()}
        if not cols:
            return False
        if since:
            row = conn.execute(
                "SELECT 1 FROM decisions WHERE UPPER(ticker) = ? "
                "AND recommendation_kind = ? AND created_at >= ? LIMIT 1",
                (ticker.upper(), RECOMMENDATION_KIND_SCORED_MISS, since),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM decisions WHERE UPPER(ticker) = ? "
                "AND recommendation_kind = ? LIMIT 1",
                (ticker.upper(), RECOMMENDATION_KIND_SCORED_MISS),
            ).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False


def evaluate_gate(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    prior_thesis: str | None,
    prior_breach_status: str | None,
    new_thesis: str | None,
) -> GateVerdict:
    """Read-only verdict — never raises, never writes. The caller decides what
    to do with ``blocked`` (raise, log-and-override, or ignore for a ticker
    that has no prior state at all)."""
    status = str(prior_breach_status or "").lower()
    reunderwrite = status in _BREACHED_STATUSES and is_material_change(prior_thesis, new_thesis)
    if not reunderwrite:
        return GateVerdict(
            is_reunderwrite=False, blocked=False, onset=None, scored_miss_found=False
        )
    onset = breach_onset(conn, ticker.upper())
    found = has_scored_miss_since(conn, ticker.upper(), onset)
    return GateVerdict(
        is_reunderwrite=True, blocked=not found, onset=onset, scored_miss_found=found
    )


__all__ = [
    "RECOMMENDATION_KIND_SCORED_MISS",
    "GateVerdict",
    "ReUnderwriteBlockedError",
    "breach_onset",
    "evaluate_gate",
    "has_scored_miss_since",
    "is_material_change",
]
