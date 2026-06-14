"""The confidence-observation ledger: capture + backfill (Close-the-Loops L10).

Writes the ``confidence_observations`` rows that grade stored confidence against
ground truth (see the alembic 0106 docstring for the schema rationale). Two
producers, one idempotent table:

* **Forward capture** (``record_restatement_observation``) — called from the
  four ``insert_*_with_restatement_detection`` sites the moment a supersede
  happens, so the discarded ``superseded_id`` becomes a graded observation
  carrying the OLD fact's *point-in-time* confidence. Best-effort by
  construction: it sits on the ingest hot path and must never raise.

* **Historical backfill** (``backfill_restatement_observations`` +
  ``backfill_disagreement_observations``) — walks the supersede chains already
  in the DB and the resolved cross-source disagreements, seeding observations
  for everything that happened before forward capture was wired. Idempotent via
  ``UNIQUE(fact_table, fact_id, kind)``: re-running inserts nothing new, and a
  fact already captured forward is never double-recorded.

Outcome semantics live in one place (``restatement_outcome`` /
``IMMATERIAL_TOLERANCE_PCT``) so capture and backfill agree on what "the number
held" means.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from timeseries.loaders import SOURCE_QUALITY_TIER_RANK

log = logging.getLogger(__name__)


def _tier_rank(tier: object) -> int:
    """Canonical source-quality rank (higher wins); unknown / None → 0.
    Shares ``timeseries.loaders.SOURCE_QUALITY_TIER_RANK`` so the authority a
    disagreement is graded against is the same row the loaders surface."""
    if not isinstance(tier, str):
        return 0
    return SOURCE_QUALITY_TIER_RANK.get(tier, 0)


# A re-report whose value moves the graded fact by <= this percent is treated as
# the SAME number confirmed (the fact held → outcome 1). Mirrors the
# source-disagreement tolerance (validation_engine: 0.5%) so "agreement" means
# the same thing on both the disagreement and the restatement side.
IMMATERIAL_TOLERANCE_PCT = 0.5

FINANCIAL_FACTS = "financial_facts"
KPI_FACTS = "kpi_facts"

KIND_RESTATEMENT = "restatement"
KIND_DISAGREEMENT = "disagreement"


@dataclass(frozen=True, slots=True)
class _GradedFact:
    """The graded (older / lower-tier) fact's snapshot context."""

    ticker: str | None
    line_item: str | None
    period_end: str | None
    value: float | None
    confidence: float
    source_tier: str | None


def _now_iso() -> str:
    """Naive-UTC ISO stamp — the repo storage convention."""
    return datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")


def _delta_pct(old: float | None, new: float | None) -> float | None:
    """``|new - old| / |old| * 100``, or None when old is missing / zero."""
    if old is None or new is None or old == 0.0:
        return None
    return abs(new - old) / abs(old) * 100.0


def restatement_outcome(old: float | None, new: float | None) -> int:
    """1 when the re-reported number HELD (within the immaterial tolerance),
    0 when it was materially restated.

    Both-zero (or new within tolerance of old) → held. A missing/zero old with a
    non-zero new is a material appearance → 0. A missing new is treated as held
    (no evidence the old was wrong)."""
    if new is None:
        return 1
    if old is None:
        return 0 if new != 0.0 else 1
    if old == 0.0:
        return 1 if new == 0.0 else 0
    pct = abs(new - old) / abs(old) * 100.0
    return 1 if pct <= IMMATERIAL_TOLERANCE_PCT else 0


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _fetch_graded_fact(
    conn: sqlite3.Connection, *, fact_table: str, fact_id: int
) -> _GradedFact | None:
    """Snapshot the graded fact's ticker / line_item / value / confidence / tier
    in one query, or None when the row is gone."""
    if fact_table == FINANCIAL_FACTS:
        sql = (
            "SELECT ff.ticker, ff.line_item, ff.period_end, CAST(ff.value AS REAL) AS v, "
            "       ff.confidence, d.source_quality_tier AS tier "
            "FROM financial_facts ff LEFT JOIN documents d ON d.id = ff.source_doc_id "
            "WHERE ff.id = ?"
        )
    elif fact_table == KPI_FACTS:
        sql = (
            "SELECT kf.ticker, kd.name AS line_item, kf.period_end, CAST(kf.value AS REAL) AS v, "
            "       kf.confidence, d.source_quality_tier AS tier "
            "FROM kpi_facts kf "
            "JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id "
            "LEFT JOIN documents d ON d.id = kf.source_doc_id "
            "WHERE kf.id = ?"
        )
    else:
        return None
    try:
        row = conn.execute(sql, (fact_id,)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    value = row["v"]
    return _GradedFact(
        ticker=None if row["ticker"] is None else str(row["ticker"]),
        line_item=None if row["line_item"] is None else str(row["line_item"]),
        period_end=None if row["period_end"] is None else str(row["period_end"])[:10],
        value=None if value is None else float(value),
        confidence=float(row["confidence"]),
        source_tier=None if row["tier"] is None else str(row["tier"]),
    )


def _insert_observation(
    conn: sqlite3.Connection,
    *,
    fact_table: str,
    fact_id: int,
    kind: str,
    graded: _GradedFact,
    new_value: float | None,
    outcome: int,
    user_id: str,
    observed_at: str,
) -> int | None:
    """INSERT OR IGNORE one observation. Returns the new row id, or None on a
    UNIQUE conflict (already captured) or any sqlite error."""
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO confidence_observations "
            "(user_id, fact_table, fact_id, kind, predicted_confidence, source_tier, "
            " line_item, ticker, period_end, old_value, new_value, delta_pct, outcome, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                fact_table,
                fact_id,
                kind,
                graded.confidence,
                graded.source_tier,
                graded.line_item,
                graded.ticker,
                graded.period_end,
                graded.value,
                new_value,
                _delta_pct(graded.value, new_value),
                outcome,
                observed_at,
            ),
        )
    except sqlite3.Error as exc:
        log.debug({"event": "confidence_obs_insert_failed", "fact_id": fact_id, "error": str(exc)})
        return None
    if cur.rowcount == 0:
        return None
    return int(cur.lastrowid) if cur.lastrowid is not None else None


def record_restatement_observation(
    conn: sqlite3.Connection,
    *,
    fact_table: str,
    superseded_id: int,
    new_value: object,
    user_id: str = "bhanu",
    observed_at: str | None = None,
) -> int | None:
    """Grade the just-superseded fact against the value that replaced it.

    Called from the restatement-detection insert sites with the ``superseded_id``
    they used to discard. Best-effort: returns the observation id, or None when
    the ledger table is absent, the old row is gone, or anything else goes wrong
    — it NEVER raises (this runs on the ingest hot path).

    ``new_value`` is the value of the row that just superseded the old one; it is
    coerced to float (the callers pass a Decimal / Decimal-string).
    """
    if not _has_table(conn, "confidence_observations"):
        return None
    try:
        new_f = float(str(new_value)) if new_value is not None else None
    except (TypeError, ValueError):
        new_f = None
    graded = _fetch_graded_fact(conn, fact_table=fact_table, fact_id=superseded_id)
    if graded is None:
        return None
    return _insert_observation(
        conn,
        fact_table=fact_table,
        fact_id=superseded_id,
        kind=KIND_RESTATEMENT,
        graded=graded,
        new_value=new_f,
        outcome=restatement_outcome(graded.value, new_f),
        user_id=user_id,
        observed_at=observed_at or _now_iso(),
    )


# ---------------------------------------------------------------------------
# Historical backfill — seed observations for chains already in the DB
# ---------------------------------------------------------------------------


def backfill_restatement_observations(
    conn: sqlite3.Connection, *, user_id: str = "bhanu", observed_at: str | None = None
) -> int:
    """Seed restatement observations for every supersede chain already in the DB.

    Walks ``financial_facts`` and ``kpi_facts`` ``supersedes_id`` links, grading
    the OLD (superseded) row against the value of the row that supersedes it.
    Idempotent via the UNIQUE constraint. Returns the number of NEW observations
    inserted (re-runs return 0). Does NOT commit — the caller owns the
    transaction (so a dry-run can roll back)."""
    if not _has_table(conn, "confidence_observations"):
        return 0
    stamp = observed_at or _now_iso()
    inserted = 0
    for fact_table in (FINANCIAL_FACTS, KPI_FACTS):
        if not _has_table(conn, fact_table):
            continue
        try:
            pairs = conn.execute(
                f"SELECT new.supersedes_id AS old_id, CAST(new.value AS REAL) AS new_value "
                f"FROM {fact_table} new WHERE new.supersedes_id IS NOT NULL"
            ).fetchall()
        except sqlite3.Error:
            continue
        for pair in pairs:
            old_id = int(pair["old_id"])
            new_value = None if pair["new_value"] is None else float(pair["new_value"])
            graded = _fetch_graded_fact(conn, fact_table=fact_table, fact_id=old_id)
            if graded is None:
                continue
            obs_id = _insert_observation(
                conn,
                fact_table=fact_table,
                fact_id=old_id,
                kind=KIND_RESTATEMENT,
                graded=graded,
                new_value=new_value,
                outcome=restatement_outcome(graded.value, new_value),
                user_id=user_id,
                observed_at=stamp,
            )
            if obs_id is not None:
                inserted += 1
    return inserted


def backfill_disagreement_observations(
    conn: sqlite3.Connection, *, user_id: str = "bhanu", observed_at: str | None = None
) -> int:
    """Seed disagreement observations from RESOLVED cross-source disagreements.

    For each resolved ``source_disagreement`` validation issue, find the
    disagreeing ``financial_facts`` rows for the (ticker, period, line_item) and
    grade every fact that is NOT the highest-tier one against that highest tier
    (the truth proxy): outcome 0 (it disagreed with the authority). This tests
    whether the disagreement *penalty* is harsh enough — facts penalized to ~0.79
    should, if the penalty is calibrated, rarely have held.

    Idempotent. Returns the number of NEW observations inserted. Does NOT commit
    — the caller owns the transaction.
    """
    if not _has_table(conn, "confidence_observations"):
        return 0
    if not _has_table(conn, "validation_issues") or not _has_table(conn, FINANCIAL_FACTS):
        return 0
    stamp = observed_at or _now_iso()
    try:
        issues = conn.execute(
            "SELECT ticker, raw_value FROM validation_issues "
            "WHERE rule = 'source_disagreement' AND resolved_at IS NOT NULL "
            "AND raw_value IS NOT NULL AND ticker IS NOT NULL"
        ).fetchall()
    except sqlite3.Error:
        return 0
    inserted = 0
    for issue in issues:
        parsed = _parse_disagreement(str(issue["raw_value"]))
        if parsed is None:
            continue
        line_item, period = parsed
        inserted += _grade_disagreement_group(
            conn,
            ticker=str(issue["ticker"]),
            line_item=line_item,
            period=period,
            user_id=user_id,
            observed_at=stamp,
        )
    return inserted


def _parse_disagreement(raw: str) -> tuple[str, str] | None:
    """Lift (line_item, period YYYY-MM-DD) out of a source_disagreement raw_value:
    ``"revenue @ 2025-12-31: fmp=100 vs sec_xbrl=101 (0.99%)"``."""
    if " @ " not in raw:
        return None
    item, rest = raw.split(" @ ", 1)
    period = rest[:10]
    if len(period) != 10:
        return None
    return item.strip(), period


def _grade_disagreement_group(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    line_item: str,
    period: str,
    user_id: str,
    observed_at: str,
) -> int:
    """Grade the non-authority sides of one disagreed (ticker, period, line_item)."""
    try:
        rows = conn.execute(
            "SELECT ff.id, CAST(ff.value AS REAL) AS v, ff.confidence, "
            "       d.source_quality_tier AS tier "
            "FROM financial_facts ff LEFT JOIN documents d ON d.id = ff.source_doc_id "
            "WHERE ff.ticker = ? AND ff.line_item = ? AND substr(ff.period_end, 1, 10) = ?",
            (ticker.upper(), line_item, period),
        ).fetchall()
    except sqlite3.Error:
        return 0
    if len(rows) < 2:
        return 0
    authority = max(rows, key=lambda r: _tier_rank(r["tier"]))
    authority_tier = authority["tier"]
    authority_value = None if authority["v"] is None else float(authority["v"])
    inserted = 0
    for r in rows:
        if int(r["id"]) == int(authority["id"]):
            continue
        if _tier_rank(r["tier"]) >= _tier_rank(authority_tier):
            continue  # same-tier disagreement — no authority to grade against
        graded = _GradedFact(
            ticker=ticker.upper(),
            line_item=line_item,
            period_end=period,
            value=None if r["v"] is None else float(r["v"]),
            confidence=float(r["confidence"]),
            source_tier=None if r["tier"] is None else str(r["tier"]),
        )
        obs_id = _insert_observation(
            conn,
            fact_table=FINANCIAL_FACTS,
            fact_id=int(r["id"]),
            kind=KIND_DISAGREEMENT,
            graded=graded,
            new_value=authority_value,
            outcome=restatement_outcome(graded.value, authority_value),
            user_id=user_id,
            observed_at=observed_at,
        )
        if obs_id is not None:
            inserted += 1
    return inserted
