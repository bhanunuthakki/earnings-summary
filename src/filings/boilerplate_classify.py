"""P3 boilerplate-vs-substance classifier — orchestration.

``docs/design/disclosure_change_build_stack.md`` §P3: turns P0's mechanical
``item_added``/``item_removed``/``item_reworded`` events into a
``boilerplate_update`` vs ``substantive`` verdict on ``disclosure_events``.

Pipeline per ticker:

1. Fetch that ticker's item-level events (``fetch_item_events``).
2. Extract each event's diff hunk — the changed excerpt only, never a whole
   item body/section/document (``_hunk_for_event``, using
   ``filings.specificity.extract_diff_hunk`` for reworded items).
3. Score the hunk deterministically (``filings.specificity.specificity_metrics``).
   ``LOW``/``HIGH`` bands resolve immediately, in plain code, at zero token
   cost — this is where "~90% of candidates die in plain code" happens for
   P3, the same as it did for P0/P1's suppression stages.
4. The **Filzen 2015 override**: risk-section growth beyond
   ``RISK_GROWTH_QOQ_WORDS_THRESHOLD`` words QoQ predicts lower abnormal
   returns and more future negative earnings shocks
   (``disclosure_change_signals.md`` §1.2) — a directly implementable
   threshold. Operationalized as a deterministic gate override: an event in
   a (ticker, risk_factors, period) bucket whose risk section grew past this
   threshold is barred from the confident-``LOW`` (boilerplate) fast path,
   because Filzen's finding is specifically that this growth carries real
   information, not filler — it must clear LLM triage or the confident-HIGH
   fast path instead.
5. Whatever survives (``AMBIGUOUS`` band) is batched ONE call per ticker to
   ``filings.boilerplate_triage``.
6. Persist every resolved verdict/confidence/interpretation back onto the
   ``disclosure_events`` row by id (``write_classifications``). A degraded or
   missing LLM verdict is left ``unclassified`` — NEVER defaulted to
   ``boilerplate_update`` (silent-degradation-class rule).

Token efficiency, concretely: the ``text_sha256`` skip already means an
unchanged item never gets an event at all (P0's job); this module additionally
never sends more than a ~500-char stored excerpt (or the further-reduced diff
hunk for reworded items), never a whole section/document, and only calls the
LLM at all for the fraction of events the deterministic bands could not
resolve.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from filings.boilerplate_triage import TriageCandidate, triage_events
from filings.models import HardStopError
from filings.specificity import (
    SpecificityBand,
    SpecificityMetrics,
    extract_diff_hunk,
    looks_tabular,
    specificity_metrics,
)
from filings.store import table_exists

log = logging.getLogger(__name__)

DETECTOR_VERSION = "boilerplate_classify_v1"
EVENTS_TABLE = "disclosure_events"
SECTIONS_TABLE = "filing_sections"

BOILERPLATE_VERDICT = "boilerplate_update"
SUBSTANTIVE_VERDICT = "substantive"
UNCLASSIFIED_VERDICT = "unclassified"

ITEM_EVENT_TYPES: tuple[str, ...] = ("item_added", "item_removed", "item_reworded")

#: Filzen 2015: risk-section growth beyond this many words QoQ predicts
#: lower abnormal returns and more future negative earnings shocks — see
#: module docstring for how this is operationalized as a gate override.
RISK_GROWTH_QOQ_WORDS_THRESHOLD = 100
RISK_FACTORS_CANONICAL_ID = "risk_factors"

#: Duplicated (not imported) from filings.cross_sectional_detrend — small,
#: stable constant; per the repo's "duplicate simple shared logic, don't
#: modularize" convention, keeping this module independently importable.
_PERIOD_RANK: dict[str, int] = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4, "FY": 5}


def _require_events_table(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, EVENTS_TABLE):
        raise HardStopError(
            f"{EVENTS_TABLE} missing — run `alembic upgrade head` (migration 0200) "
            "before classifying disclosure items"
        )


@dataclass(slots=True, frozen=True)
class ItemEventRow:
    """Read-side projection of one item-level ``disclosure_events`` row.

    Carries ``id`` — unlike ``filings.item_diff.DisclosureEvent``, whose
    dataclass omits it — because this module updates rows in place by id.
    """

    id: int
    ticker: str
    event_type: str
    canonical_id: str | None
    fiscal_year: int | None
    fiscal_period: str | None
    subject: str
    subject_label: str | None
    prior_excerpt: str | None
    current_excerpt: str | None
    evidence_quote: str | None
    verdict: str


def fetch_item_events(
    conn: sqlite3.Connection,
    ticker: str | None = None,
    *,
    only_unclassified: bool = True,
) -> list[ItemEventRow]:
    """Fetch item-level events, optionally scoped to one ticker and/or
    restricted to rows this module has not yet classified (the default —
    idempotent, so a re-run only touches new/changed rows)."""
    _require_events_table(conn)
    placeholders = ",".join("?" * len(ITEM_EVENT_TYPES))
    clauses = [f"event_type IN ({placeholders})"]
    params: list[object] = list(ITEM_EVENT_TYPES)
    if ticker is not None:
        clauses.append("ticker = ?")
        params.append(ticker.strip().upper())
    if only_unclassified:
        clauses.append(f"verdict = '{UNCLASSIFIED_VERDICT}'")

    prior_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"""
            SELECT id, ticker, event_type, canonical_id, fiscal_year, fiscal_period,
                   subject, subject_label, prior_excerpt, current_excerpt, evidence_quote, verdict
            FROM {EVENTS_TABLE}
            WHERE {" AND ".join(clauses)}
            ORDER BY ticker, fiscal_year, fiscal_period, subject
            """,
            tuple(params),
        ).fetchall()
    finally:
        conn.row_factory = prior_factory

    return [
        ItemEventRow(
            id=int(r["id"]),
            ticker=str(r["ticker"]),
            event_type=str(r["event_type"]),
            canonical_id=r["canonical_id"],
            fiscal_year=r["fiscal_year"],
            fiscal_period=r["fiscal_period"],
            subject=str(r["subject"]),
            subject_label=r["subject_label"],
            prior_excerpt=r["prior_excerpt"],
            current_excerpt=r["current_excerpt"],
            evidence_quote=r["evidence_quote"],
            verdict=str(r["verdict"]),
        )
        for r in rows
    ]


def _hunk_for_event(row: ItemEventRow) -> str:
    """The DIFF HUNK for one event — never the whole item body."""
    if row.event_type == "item_added":
        return row.current_excerpt or row.evidence_quote or ""
    if row.event_type == "item_removed":
        return row.prior_excerpt or row.evidence_quote or ""
    # item_reworded: reduce to just the changed spans between the two
    # (already <=500-char) stored excerpts.
    hunk = extract_diff_hunk(row.prior_excerpt or "", row.current_excerpt or "")
    return hunk or row.evidence_quote or ""


def compute_risk_growth_flags(
    conn: sqlite3.Connection, tickers: Sequence[str] | None = None
) -> dict[tuple[str, int | None, str | None], bool]:
    """(ticker, fiscal_year, fiscal_period) -> True when that ticker's
    ``risk_factors`` section grew by more than
    ``RISK_GROWTH_QOQ_WORDS_THRESHOLD`` words versus the immediately
    preceding STORED period (Filzen 2015) — see module docstring.

    Word count (not char count, unlike P2's length-delta axis) because
    Filzen's own threshold is stated in words.
    """
    if not table_exists(conn, SECTIONS_TABLE):
        return {}
    params: list[object] = [RISK_FACTORS_CANONICAL_ID]
    ticker_clause = ""
    if tickers:
        upper = [t.strip().upper() for t in tickers]
        ticker_clause = f" AND ticker IN ({','.join('?' * len(upper))})"
        params.extend(upper)
    rows = conn.execute(
        f"""
        SELECT ticker, fiscal_year, fiscal_period, text
        FROM {SECTIONS_TABLE}
        WHERE canonical_id = ?{ticker_clause}
        """,
        tuple(params),
    ).fetchall()

    word_totals: dict[tuple[str, int | None, str | None], int] = {}
    for ticker, fy, fp, text in rows:
        key = (str(ticker).upper(), fy, fp)
        word_totals[key] = word_totals.get(key, 0) + len(str(text).split())

    by_ticker: dict[str, list[tuple[int, int, int | None, str | None, int]]] = {}
    for (ticker, fy, fp), words in word_totals.items():
        rank = _PERIOD_RANK.get(str(fp) if fp is not None else "", 0)
        by_ticker.setdefault(ticker, []).append((fy or 0, rank, fy, fp, words))

    flags: dict[tuple[str, int | None, str | None], bool] = {}
    for ticker, entries in by_ticker.items():
        entries.sort(key=lambda e: (e[0], e[1]))
        prev_words: int | None = None
        for _fy_sort, _rank, fy, fp, words in entries:
            if prev_words is not None and (words - prev_words) > RISK_GROWTH_QOQ_WORDS_THRESHOLD:
                flags[(ticker, fy, fp)] = True
            prev_words = words
    return flags


@dataclass(slots=True)
class ClassificationResult:
    event_id: int
    ticker: str
    event_type: str
    subject: str
    verdict: str
    confidence: float | None
    interpretation_md: str
    specificity_score: float
    band: SpecificityBand
    risk_growth_flagged: bool
    llm_used: bool
    #: True when the table-shape override withheld the confident-HIGH fast path
    #: (or would have, had the hunk scored HIGH). Recorded rather than inferred
    #: so the override's real effect is countable instead of invisible.
    table_shaped: bool = False


def _classify_deterministic(
    row: ItemEventRow, risk_growth_flagged: bool
) -> tuple[SpecificityBand, SpecificityMetrics, str, bool]:
    """Deterministic band for one event, with both gate overrides applied.

    Returns ``(band, metrics, hunk, table_shaped)``. The two overrides are
    mirror images, each barring ONE confident fast path and deferring to LLM
    triage rather than asserting the opposite verdict:

    * **Filzen 2015** bars confident-LOW when risk-section growth says the text
      carries information despite reading generic.
    * **Table shape** bars confident-HIGH, because the specificity proxy's
      number-density premise inverts on extracted table content — see
      ``filings.specificity.looks_tabular`` for the measured evidence.
    """
    hunk = _hunk_for_event(row)
    metrics = specificity_metrics(hunk)
    band = metrics.band
    if risk_growth_flagged and band is SpecificityBand.LOW:
        # Filzen 2015 override — see module docstring.
        band = SpecificityBand.AMBIGUOUS
    table_shaped = looks_tabular(hunk)
    if table_shaped and band is SpecificityBand.HIGH:
        # Table-shape override: a table row is dense with numbers because it is
        # a table, not because it is firm-specific. Withhold the fast path and
        # let triage read it; never assert 'boilerplate' from shape alone.
        band = SpecificityBand.AMBIGUOUS
    return band, metrics, hunk, table_shaped


def classify_ticker_events(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    risk_growth_flags: dict[tuple[str, int | None, str | None], bool] | None = None,
    only_unclassified: bool = True,
    db_path: Path | str | None = None,
) -> tuple[list[ClassificationResult], bool]:
    """Classify one ticker's item-level events. Returns
    ``(results, llm_degraded)`` — ``llm_degraded`` is True iff an LLM call
    for this ticker's survivors failed/parsed badly, in which case those
    survivors resolve to ``unclassified`` (never a fabricated verdict).
    """
    rows = fetch_item_events(conn, ticker, only_unclassified=only_unclassified)
    if not rows:
        return [], False

    flags = risk_growth_flags or {}
    resolved: list[ClassificationResult] = []
    survivors: list[tuple[ItemEventRow, str, SpecificityMetrics, bool]] = []
    table_barred = 0

    for row in rows:
        flagged = flags.get((row.ticker, row.fiscal_year, row.fiscal_period), False)
        band, metrics, hunk, table_shaped = _classify_deterministic(row, flagged)
        if table_shaped and metrics.band is SpecificityBand.HIGH:
            table_barred += 1
        if band is SpecificityBand.LOW:
            resolved.append(
                ClassificationResult(
                    event_id=row.id,
                    ticker=row.ticker,
                    event_type=row.event_type,
                    subject=row.subject,
                    verdict=BOILERPLATE_VERDICT,
                    confidence=round(1.0 - metrics.specificity_score, 4),
                    interpretation_md=(
                        f"deterministic: specificity {metrics.specificity_score:.3f} "
                        f"(boilerplate phrase density {metrics.boilerplate_density:.3f})"
                    ),
                    specificity_score=metrics.specificity_score,
                    band=band,
                    risk_growth_flagged=flagged,
                    llm_used=False,
                    table_shaped=table_shaped,
                )
            )
        elif band is SpecificityBand.HIGH:
            resolved.append(
                ClassificationResult(
                    event_id=row.id,
                    ticker=row.ticker,
                    event_type=row.event_type,
                    subject=row.subject,
                    verdict=SUBSTANTIVE_VERDICT,
                    confidence=round(metrics.specificity_score, 4),
                    interpretation_md=(
                        f"deterministic: specificity {metrics.specificity_score:.3f} "
                        "(numbers/entities/dates dense, minimal boilerplate)"
                    ),
                    specificity_score=metrics.specificity_score,
                    band=band,
                    risk_growth_flagged=flagged,
                    llm_used=False,
                    table_shaped=table_shaped,
                )
            )
        else:
            survivors.append((row, hunk, metrics, table_shaped))

    if table_barred:
        # The override moves cost, so it reports cost: these events would have
        # resolved free on the fast path and now consume triage instead.
        log.info(
            {
                "event": "boilerplate_table_shape_override",
                "ticker": ticker,
                "barred_from_confident_high": table_barred,
                "events_scanned": len(rows),
                "note": "table-shaped hunks deferred to LLM triage; "
                "number density is not firm-specificity on extracted tables",
            }
        )

    llm_degraded = False
    if survivors:
        candidates: list[TriageCandidate] = [
            (row.id, row.event_type, row.subject_label or row.subject, hunk)
            for row, hunk, _m, _t in survivors
        ]
        outcome = triage_events(ticker, candidates, db_path=db_path)
        llm_degraded = outcome.degraded
        for row, _hunk, metrics, table_shaped in survivors:
            flagged = flags.get((row.ticker, row.fiscal_year, row.fiscal_period), False)
            verdict_out = outcome.verdicts.get(row.id)
            if verdict_out is None:
                resolved.append(
                    ClassificationResult(
                        event_id=row.id,
                        ticker=row.ticker,
                        event_type=row.event_type,
                        subject=row.subject,
                        verdict=UNCLASSIFIED_VERDICT,
                        confidence=None,
                        interpretation_md=(
                            "llm_triage_degraded"
                            if outcome.degraded
                            else "llm_triage_missing_verdict"
                        ),
                        specificity_score=metrics.specificity_score,
                        band=metrics.band,
                        risk_growth_flagged=flagged,
                        llm_used=True,
                        table_shaped=table_shaped,
                    )
                )
                continue
            resolved.append(
                ClassificationResult(
                    event_id=row.id,
                    ticker=row.ticker,
                    event_type=row.event_type,
                    subject=row.subject,
                    verdict=verdict_out.verdict.value,
                    confidence=verdict_out.confidence,
                    interpretation_md=verdict_out.rationale,
                    specificity_score=metrics.specificity_score,
                    band=metrics.band,
                    risk_growth_flagged=flagged,
                    llm_used=True,
                    table_shaped=table_shaped,
                )
            )
    return resolved, llm_degraded


def write_classifications(conn: sqlite3.Connection, results: Sequence[ClassificationResult]) -> int:
    """Persist verdict/confidence/interpretation_md back onto the
    ``disclosure_events`` row by id. Idempotent — re-running with the same
    inputs writes the same values."""
    _require_events_table(conn)
    written = 0
    for r in results:
        conn.execute(
            f"UPDATE {EVENTS_TABLE} SET verdict = ?, confidence = ?, interpretation_md = ? WHERE id = ?",
            (r.verdict, r.confidence, r.interpretation_md, r.event_id),
        )
        written += 1
    return written
