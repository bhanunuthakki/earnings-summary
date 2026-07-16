"""Say-Do: track management commitments and match them against realized KPIs.

Pattern mirrors src/pipeline/kpi_persistence.py — Claude in-session reads
transcript_segments, emits a typed manifest of commitments, and this module
validates + persists. A separate matcher walks management_commitments where
period_target has elapsed and joins to kpi_facts to decide HIT / MISS / BEAT.

Outcome semantics (depend on the comparator the LLM tagged the commitment with):
  - GE / GT / "growth >= X" claims:    realized >= target -> HIT, realized < target -> MISS
  - LE / LT / "stay below X" claims:   realized <= target -> HIT, realized > target -> MISS
  - EQ / "deliver ~X":                 |realized - target| <= 5% of target -> HIT else MISS

BEAT is a strict subset of HIT: realized exceeds target by >= 10% of target.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from compute.thesis_evaluator import Comparator
from models.facts import Unit


class CommitmentOutcome(StrEnum):
    """Closed enum for match outcomes."""

    HIT = "hit"
    MISS = "miss"
    BEAT = "beat"
    NO_DATA = "no_data"


class CommitmentInput(BaseModel):
    """One forward-looking commitment extracted from a transcript segment.

    `transcript_segment_id` anchors provenance to the exact spoken passage.
    `period_target` is the calendar quarter management was guiding for; the
    matcher will look up the kpi_facts value for that period.
    """

    ticker: str
    period_made: datetime
    transcript_segment_id: int
    period_target: datetime
    kpi_name: str = Field(min_length=1, max_length=200)
    comparator: Comparator
    target_value: Decimal
    unit: Unit
    narrative: str = Field(min_length=1, max_length=1000)


class CommitmentExtractionManifest(BaseModel):
    """A batch of commitments extracted from one transcript or transcript-pair."""

    commitments: list[CommitmentInput]


def _validate_segment_exists(conn: sqlite3.Connection, segment_id: int) -> None:
    """Raise if the transcript_segment_id doesn't reference a real row."""
    cur = conn.execute("SELECT 1 FROM transcript_segments WHERE id = ? LIMIT 1", (segment_id,))
    if cur.fetchone() is None:
        raise ValueError(f"transcript_segment_id={segment_id} does not exist")


def persist_commitment(
    conn: sqlite3.Connection,
    *,
    commitment: CommitmentInput,
) -> int:
    """Insert one management_commitments row; returns its id."""
    _validate_segment_exists(conn, commitment.transcript_segment_id)
    cur = conn.execute(
        "INSERT INTO management_commitments "
        "(ticker, period_made, transcript_segment_id, period_target, "
        " kpi_name, comparator, target_value, unit, narrative) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            commitment.ticker.upper(),
            commitment.period_made,
            commitment.transcript_segment_id,
            commitment.period_target,
            commitment.kpi_name,
            commitment.comparator.value,
            str(commitment.target_value),
            commitment.unit.value,
            commitment.narrative,
        ),
    )
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def persist_manifest(
    conn: sqlite3.Connection,
    manifest: CommitmentExtractionManifest,
) -> list[int]:
    """Apply a manifest. Returns the list of new commitment ids inserted."""
    ids: list[int] = []
    for c in manifest.commitments:
        ids.append(persist_commitment(conn, commitment=c))
    conn.commit()
    return ids


@dataclass(frozen=True)
class CommitmentRow:
    """One management_commitments row hydrated for matching."""

    id: int
    ticker: str
    period_made: datetime
    period_target: datetime
    kpi_name: str
    comparator: Comparator
    target_value: Decimal
    unit: Unit
    narrative: str
    realized_value: Decimal | None
    outcome: CommitmentOutcome | None


def fetch_pending_commitments(
    conn: sqlite3.Connection,
    *,
    ticker: str | None = None,
) -> list[CommitmentRow]:
    """Return commitments where outcome is still NULL (haven't been matched yet)."""
    sql = (
        "SELECT id, ticker, period_made, period_target, kpi_name, comparator, "
        "       target_value, unit, narrative, realized_value, outcome "
        "FROM management_commitments WHERE outcome IS NULL"
    )
    params: tuple[str, ...] = ()
    if ticker is not None:
        sql += " AND ticker = ?"
        params = (ticker.upper(),)
    sql += " ORDER BY ticker, period_target, id"
    cur = conn.execute(sql, params)
    out: list[CommitmentRow] = []
    for row in cur.fetchall():
        out.append(_row_to_commitment(row))
    return out


def _row_to_commitment(row) -> CommitmentRow:
    pm = row["period_made"]
    pt = row["period_target"]
    if isinstance(pm, str):
        pm = datetime.fromisoformat(pm)
    if isinstance(pt, str):
        pt = datetime.fromisoformat(pt)
    realized = Decimal(str(row["realized_value"])) if row["realized_value"] is not None else None
    outcome = CommitmentOutcome(row["outcome"]) if row["outcome"] is not None else None
    return CommitmentRow(
        id=int(row["id"]),
        ticker=row["ticker"],
        period_made=pm,
        period_target=pt,
        kpi_name=row["kpi_name"],
        comparator=Comparator(row["comparator"]),
        target_value=Decimal(str(row["target_value"])),
        unit=Unit(row["unit"]),
        narrative=row["narrative"],
        realized_value=realized,
        outcome=outcome,
    )


def _classify_outcome(
    *, comparator: Comparator, target: Decimal, realized: Decimal
) -> CommitmentOutcome:
    """Apply outcome semantics. BEAT is a strict subset of HIT (>=10% over target)."""
    abs_target = abs(target) if target != 0 else Decimal("1")  # avoid div-by-zero
    over_pct = (realized - target) / abs_target * Decimal(100)

    if comparator in (Comparator.GE, Comparator.GT):
        if realized < target:
            return CommitmentOutcome.MISS
        if over_pct >= Decimal(10):
            return CommitmentOutcome.BEAT
        return CommitmentOutcome.HIT
    if comparator in (Comparator.LE, Comparator.LT):
        if realized > target:
            return CommitmentOutcome.MISS
        if (target - realized) / abs_target * Decimal(100) >= Decimal(10):
            return CommitmentOutcome.BEAT
        return CommitmentOutcome.HIT
    if comparator is Comparator.EQ:
        deviation_pct = abs(over_pct)
        if deviation_pct <= Decimal(5):
            return CommitmentOutcome.HIT
        return CommitmentOutcome.MISS
    raise ValueError(f"Unhandled comparator: {comparator}")


def _lookup_realized_value(
    conn: sqlite3.Connection, *, ticker: str, kpi_name: str, period_target: datetime
) -> tuple[Decimal, int] | None:
    """Find the kpi_facts value matching (ticker, kpi_name, period_target).

    Returns (value, source_doc_id) or None if no row exists.
    """
    cur = conn.execute(
        "SELECT kf.value, kf.source_doc_id "
        "FROM kpi_facts kf JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id "
        "WHERE kf.ticker = ? AND kd.name = ? AND kf.period_end = ? "
        "LIMIT 1",
        (ticker.upper(), kpi_name, period_target),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return (Decimal(str(row["value"])), int(row["source_doc_id"]))


@dataclass(frozen=True)
class MatchResult:
    """One matcher pass over a single commitment."""

    commitment_id: int
    ticker: str
    kpi_name: str
    period_target: datetime
    target_value: Decimal
    realized_value: Decimal | None
    outcome: CommitmentOutcome


def match_commitment(conn: sqlite3.Connection, commitment: CommitmentRow) -> MatchResult:
    """Look up the realized value, classify outcome, and persist back to the row."""
    looked_up = _lookup_realized_value(
        conn,
        ticker=commitment.ticker,
        kpi_name=commitment.kpi_name,
        period_target=commitment.period_target,
    )
    if looked_up is None:
        return MatchResult(
            commitment_id=commitment.id,
            ticker=commitment.ticker,
            kpi_name=commitment.kpi_name,
            period_target=commitment.period_target,
            target_value=commitment.target_value,
            realized_value=None,
            outcome=CommitmentOutcome.NO_DATA,
        )
    realized_value, realized_doc_id = looked_up
    outcome = _classify_outcome(
        comparator=commitment.comparator,
        target=commitment.target_value,
        realized=realized_value,
    )
    conn.execute(
        "UPDATE management_commitments SET realized_value = ?, "
        "realized_doc_id = ?, outcome = ?, evaluated_at = ? WHERE id = ?",
        (
            str(realized_value),
            realized_doc_id,
            outcome.value,
            datetime.now(),
            commitment.id,
        ),
    )

    # Persist to saydo_historical_metrics ledger if the table exists (supports test db isolation)
    table_check = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='saydo_historical_metrics'"
    ).fetchone()
    if table_check:
        cur_hist = conn.execute(
            "SELECT id FROM saydo_historical_metrics "
            "WHERE ticker = ? AND period_made = ? AND period_target = ? AND kpi_name = ?",
            (
                commitment.ticker,
                commitment.period_made,
                commitment.period_target,
                commitment.kpi_name,
            ),
        )
        existing = cur_hist.fetchone()
        if existing:
            conn.execute(
                "UPDATE saydo_historical_metrics SET "
                "  comparator = ?, target_value = ?, realized_value = ?, outcome = ?, "
                "  guidance_narrative = ?, evaluated_at = ? "
                "WHERE id = ?",
                (
                    commitment.comparator.value,
                    str(commitment.target_value),
                    str(realized_value),
                    outcome.value,
                    commitment.narrative,
                    datetime.now(),
                    existing["id"],
                ),
            )
        else:
            conn.execute(
                "INSERT INTO saydo_historical_metrics "
                "(ticker, period_made, period_target, kpi_name, comparator, "
                " target_value, realized_value, outcome, guidance_narrative, evaluated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    commitment.ticker,
                    commitment.period_made,
                    commitment.period_target,
                    commitment.kpi_name,
                    commitment.comparator.value,
                    str(commitment.target_value),
                    str(realized_value),
                    outcome.value,
                    commitment.narrative,
                    datetime.now(),
                ),
            )

    conn.commit()
    return MatchResult(
        commitment_id=commitment.id,
        ticker=commitment.ticker,
        kpi_name=commitment.kpi_name,
        period_target=commitment.period_target,
        target_value=commitment.target_value,
        realized_value=realized_value,
        outcome=outcome,
    )


# Stamped as llm_artifacts.dirty_reason when new grades land (wave B B10a).
_SAYDO_DIRTY_REASON = "saydo_grades_changed"


def _mark_narrative_artifacts_dirty(conn: sqlite3.Connection, tickers: set[str]) -> int:
    """After new Say-Do grades land for ``tickers``, dirty the cached narrative
    artifacts that interpret them — ``lens:mgmt_credibility_score`` per ticker
    and the portfolio-scoped ``lens:cross_portfolio_synthesis`` — so a
    corrected ledger can never be silently contradicted by a stale memo (the
    VEEV "0-for-3" reconciliation defect, wave B B10a). Same UPDATE shape as
    ``llm_artifact_store.mark_dirty``, run on the CALLER's connection so the
    flip commits with the grades. Best-effort: 0 when the table is absent."""
    if not tickers:
        return 0
    has_artifacts = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_artifacts'"
    ).fetchone()
    if has_artifacts is None:
        return 0
    n = 0
    for t in sorted(tickers):
        cur = conn.execute(
            "UPDATE llm_artifacts SET dirty = 1, dirty_reason = ? "
            "WHERE ticker = ? AND purpose = 'lens:mgmt_credibility_score' "
            "AND superseded_by_id IS NULL AND dirty = 0",
            (_SAYDO_DIRTY_REASON, t),
        )
        n += cur.rowcount
    cur = conn.execute(
        "UPDATE llm_artifacts SET dirty = 1, dirty_reason = ? "
        "WHERE scope = 'portfolio' AND purpose = 'lens:cross_portfolio_synthesis' "
        "AND superseded_by_id IS NULL AND dirty = 0",
        (_SAYDO_DIRTY_REASON,),
    )
    n += cur.rowcount
    conn.commit()
    return n


def match_pending(
    conn: sqlite3.Connection,
    *,
    ticker: str | None = None,
) -> list[MatchResult]:
    """Match every commitment with NULL outcome. Returns one result per
    commitment. Tickers that received an actual grade (NO_DATA writes nothing)
    also get their cached narrative artifacts marked dirty — see
    ``_mark_narrative_artifacts_dirty``."""
    pending = fetch_pending_commitments(conn, ticker=ticker)
    results = [match_commitment(conn, c) for c in pending]
    graded = {r.ticker for r in results if r.outcome != CommitmentOutcome.NO_DATA}
    _mark_narrative_artifacts_dirty(conn, graded)
    return results
