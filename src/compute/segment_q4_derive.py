"""Q4 segment derivation: FY minus sum-of-Q1..Q3, at the segment-dimension
level (docs/design/segment_quarterly_framework.md §3, Phase 2).

``Q4_segment = FY_segment - (Q1_segment + Q2_segment + Q3_segment)``, joined
per matching segment identity (§3.1) across four source documents (the 10-K
FY filing + three 10-Qs). Same one-hop-subtraction guard set
``compute.segment_quarterly_10q``'s Q2/Q3 derivation already established
(§2.6: missing-anchor, sign-sanity, confidence-decay,
cross-check-against-consolidated) — this module generalizes it to four
inputs and adds the matching-key + recast/supersede logic specific to
combining independently-filed documents (§3.1-§3.4).

Recast handling (§3.2): each FY/Q1/Q2/Q3 anchor is looked up as the LATEST
filing on file for that logical (ticker, fiscal_period_type, fiscal_year)
key (highest ``documents.fetched_at``, id as tiebreaker) rather than the
first one found — so a later filing that recasts an earlier quarter's
segment breakdown is picked up automatically on a re-run. When a fresh
derivation disagrees with an existing derived Q4 cell for the same logical
key, the new row chains via ``supersedes_id`` to the old one (never
mutates/deletes, mirrors ``pipeline.restatement_detector``'s "later filing
wins" pattern) — bounded by the recasting document's own comparative-column
reach (§3.2 point 4): a 10-K's segment note carries at most 2 prior
comparative years, a 10-Q at most 1. A "recast" that would reach further
back than that is out of algorithmic reach and is recorded, never guessed.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

from compute.segments import RECONCILE_TOLERANCE_OVER, segment_sum_exceeds_revenue
from models.facts import FiscalPeriodType, SegmentDimension, SegmentDimType, Unit
from pipeline.restatement_detector import is_later_filing
from pipeline.segment_junction_writer import write_segment_facts_junction
from pipeline.segment_quarterly_coverage import record_coverage

METHOD_VERSION = "segment_q4_derive_v1"
_Q2Q3_METHOD_VERSION = "segment_q2q3_derive_v1"  # compute.segment_quarterly_10q's tag

# Confidence decay per derivation hop (§2.6 point 3 / §3.4) -- same constant
# Phase 1 uses, not a second independently-tuned number.
_DERIVE_CONFIDENCE_DECAY = 0.97
_SIGN_SANITY_FLOOR_CONFIDENCE = 0.3

# §3.2 point 4: a 10-K's segment note carries FY + up to 2 prior comparative
# years; a 10-Q carries current + 1 prior-year comparative. Recast
# propagation reaches exactly as far back as the recasting document's own
# comparative window -- no further.
_COMPARATIVE_WINDOW_BY_DOC_TYPE: dict[str, int] = {
    "fmp_10k_json": 2,
    "fmp_10q_json": 1,
}
_DEFAULT_COMPARATIVE_WINDOW = 1

_QUARTERS: tuple[FiscalPeriodType, ...] = (
    FiscalPeriodType.Q1,
    FiscalPeriodType.Q2,
    FiscalPeriodType.Q3,
)

# Point-in-time balance metrics: "Q4 = FY - (Q1+Q2+Q3)" is only meaningful
# for flow metrics; subtracting three balance-sheet snapshots from a year-end
# snapshot produces a large negative artifact, not a quarter. The fpi_6k
# route's geography tables carry non_current_assets alongside revenue
# (IAS 34 / IFRS 8 disclosure shape), so these must be refused, not derived.
_BALANCE_METRICS = frozenset({"non_current_assets"})


@dataclass(slots=True)
class Q4DeriveResult:
    ticker: str
    fiscal_year: int
    derived_inserted: int = 0
    superseded_count: int = 0
    not_computable_count: int = 0
    tolerance_breach_count: int = 0
    skipped_reason: str | None = None
    reason_counts: dict[str, int] = field(default_factory=dict[str, int])


@dataclass(slots=True)
class _SegmentCell:
    id: int
    period_id: int
    dim_type: str
    dim_name: str
    metric: str
    value: Decimal
    confidence: float
    disclosure_status: str
    method_version: str | None
    segment_entity_id: int | None
    source_doc_id: int
    period_end: datetime


def _parse_dt(raw: object) -> datetime | None:
    if isinstance(raw, datetime):
        return raw
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _latest_period_row(
    conn: sqlite3.Connection, *, ticker: str, fiscal_period_type: FiscalPeriodType, fiscal_year: int
) -> tuple[int, datetime, int] | None:
    """(period_id, period_end, source_doc_id) for the LATEST segment_periods
    row matching (ticker, fiscal_period_type) whose period_end falls in
    ``fiscal_year`` -- "latest" by highest documents.fetched_at, id as
    tiebreaker (mirrors ``restatement_detector.is_later_filing``'s own
    fetched_at fallback). Picking the latest anchor, not just any anchor, is
    what lets a later recasting filing get picked up on a re-run without a
    separate "did anything change" pre-check.
    """
    rows = conn.execute(
        """
        SELECT sp.id, sp.period_end, sp.source_doc_id, d.fetched_at
        FROM segment_periods sp
        LEFT JOIN documents d ON d.id = sp.source_doc_id
        WHERE sp.ticker = ? AND sp.fiscal_period_type = ?
          AND strftime('%Y', sp.period_end) = ?
        """,
        (ticker, fiscal_period_type.value, str(fiscal_year)),
    ).fetchall()
    if not rows:
        return None
    best: tuple[int, datetime, int] | None = None
    best_key: tuple[str, int] = ("", -1)
    for r in rows:
        pid, period_end_raw, source_doc_id, fetched_at_raw = r[0], r[1], r[2], r[3]
        fetched_at = str(fetched_at_raw) if fetched_at_raw is not None else ""
        key = (fetched_at, int(pid))
        if key > best_key:
            period_end = _parse_dt(period_end_raw)
            if period_end is None:
                continue
            best_key = key
            best = (int(pid), period_end, int(source_doc_id))
    return best


def _cells_for_period_id(conn: sqlite3.Connection, period_id: int) -> list[_SegmentCell]:
    rows = conn.execute(
        """
        SELECT sd.id, sd.period_id, sd.dim_type, sd.dim_name, sd.metric, sd.value,
               sd.confidence, sd.disclosure_status, sd.method_version, sd.segment_entity_id,
               sp.source_doc_id, sp.period_end
        FROM segment_dimensions sd
        JOIN segment_periods sp ON sp.id = sd.period_id
        WHERE sd.period_id = ?
        """,
        (period_id,),
    ).fetchall()
    out: list[_SegmentCell] = []
    for r in rows:
        period_end = _parse_dt(r[11])
        if period_end is None:
            continue
        out.append(
            _SegmentCell(
                id=int(r[0]),
                period_id=int(r[1]),
                dim_type=str(r[2]),
                dim_name=str(r[3]),
                metric=str(r[4]),
                value=Decimal(str(r[5])),
                confidence=float(r[6]) if r[6] is not None else 1.0,
                disclosure_status=str(r[7]) if r[7] is not None else "reported",
                method_version=cast("str | None", r[8]),
                segment_entity_id=cast("int | None", r[9]),
                source_doc_id=int(r[10]),
                period_end=period_end,
            )
        )
    return out


def _find_match(target: _SegmentCell, candidates: list[_SegmentCell]) -> _SegmentCell | None:
    """§3.1 matching key, priority order: (1) segment_entity_id when
    non-NULL on both sides, (2) literal (dim_type, dim_name) match."""
    same_metric = [c for c in candidates if c.metric == target.metric]
    if target.segment_entity_id is not None:
        for c in same_metric:
            if c.segment_entity_id is not None and c.segment_entity_id == target.segment_entity_id:
                return c
    for c in same_metric:
        if c.dim_type == target.dim_type and c.dim_name == target.dim_name:
            return c
    return None


def _segment_identity_present(target: _SegmentCell, candidates: list[_SegmentCell]) -> bool:
    """True if ANY cell (regardless of metric) in ``candidates`` matches
    ``target``'s segment identity -- used to distinguish "the segment itself
    is unmatched this quarter" (§3.1 point 3, ``unmatched_segment_identity``)
    from "the segment exists but this specific metric wasn't captured"
    (``missing_prior_anchor_for_subtraction``)."""
    if target.segment_entity_id is not None:
        for c in candidates:
            if c.segment_entity_id is not None and c.segment_entity_id == target.segment_entity_id:
                return True
    return any(c.dim_type == target.dim_type and c.dim_name == target.dim_name for c in candidates)


def _hop_count(cell: _SegmentCell) -> int:
    """Derivation hops already embodied in ``cell`` -- 0 for an as-filed
    reported value, 1 for Phase 1's Q2/Q3 one-hop derivation. An unfamiliar
    'derived' method_version (a future deriver this module doesn't know
    about) defaults to 1 hop -- conservative, never silently 0."""
    if cell.disclosure_status != "derived":
        return 0
    if cell.method_version == _Q2Q3_METHOD_VERSION:
        return 1
    if cell.method_version == METHOD_VERSION:
        return 3
    return 1


def _comparative_window_years(conn: sqlite3.Connection, source_doc_id: int) -> int:
    row = conn.execute("SELECT doc_type FROM documents WHERE id = ?", (source_doc_id,)).fetchone()
    if row is None:
        return _DEFAULT_COMPARATIVE_WINDOW
    doc_type = str(row[0]) if row[0] is not None else ""
    return _COMPARATIVE_WINDOW_BY_DOC_TYPE.get(doc_type, _DEFAULT_COMPARATIVE_WINDOW)


def _document_reporting_year(conn: sqlite3.Connection, source_doc_id: int) -> int | None:
    row = conn.execute("SELECT period_end FROM documents WHERE id = ?", (source_doc_id,)).fetchone()
    if row is None:
        return None
    dt = _parse_dt(row[0])
    return dt.year if dt is not None else None


def _find_existing_q4_head(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    period_end: datetime,
    dim_type: str,
    dim_name: str,
    metric: str,
) -> _SegmentCell | None:
    row = conn.execute(
        """
        SELECT sd.id, sd.period_id, sd.dim_type, sd.dim_name, sd.metric, sd.value,
               sd.confidence, sd.disclosure_status, sd.method_version, sd.segment_entity_id,
               sp.source_doc_id, sp.period_end
        FROM segment_dimensions sd
        JOIN segment_periods sp ON sp.id = sd.period_id
        WHERE sp.ticker = ? AND sp.fiscal_period_type = 'Q4' AND sp.period_end = ?
          AND sd.dim_type = ? AND sd.dim_name = ? AND sd.metric = ?
          AND sd.disclosure_status = 'derived'
        ORDER BY sd.id DESC LIMIT 1
        """,
        (ticker, period_end, dim_type, dim_name, metric),
    ).fetchone()
    if row is None:
        return None
    pend = _parse_dt(row[11])
    if pend is None:
        return None
    return _SegmentCell(
        id=int(row[0]),
        period_id=int(row[1]),
        dim_type=str(row[2]),
        dim_name=str(row[3]),
        metric=str(row[4]),
        value=Decimal(str(row[5])),
        confidence=float(row[6]) if row[6] is not None else 1.0,
        disclosure_status=str(row[7]),
        method_version=cast("str | None", row[8]),
        segment_entity_id=cast("int | None", row[9]),
        source_doc_id=int(row[10]),
        period_end=pend,
    )


def _existing_fy_doc_id_from_derived_from(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    inputs = cast("dict[str, object]", payload).get("inputs")
    if not isinstance(inputs, list):
        return None
    for entry in cast("list[object]", inputs):
        if not isinstance(entry, dict):
            continue
        e = cast("dict[str, object]", entry)
        if e.get("fiscal_period_type") == "FY":
            doc_id = e.get("doc_id")
            if isinstance(doc_id, int) and not isinstance(doc_id, bool):
                return doc_id
    return None


_VALUE_EPSILON = Decimal("0.01")


def derive_for_ticker(
    ticker: str, fiscal_year: int, repo_root: Path, conn: sqlite3.Connection
) -> Q4DeriveResult:
    """FY - (Q1+Q2+Q3) at the segment level for one (ticker, fiscal_year).

    ``repo_root`` is accepted for CLI-contract parity with
    ``compute.segment_quarterly_10q.extract_for_ticker`` (unused today --
    this module reads exclusively from the DB, no on-disk JSON of its own).
    """
    _ = repo_root
    ticker = ticker.upper()
    result = Q4DeriveResult(ticker=ticker, fiscal_year=fiscal_year)

    fy_row = _latest_period_row(
        conn, ticker=ticker, fiscal_period_type=FiscalPeriodType.FY, fiscal_year=fiscal_year
    )
    if fy_row is None:
        result.skipped_reason = "no_fy_segment_data"
        return result
    fy_period_id, fy_period_end, fy_source_doc_id = fy_row
    fy_cells = _cells_for_period_id(conn, fy_period_id)
    if not fy_cells:
        result.skipped_reason = "no_fy_segment_data"
        return result

    quarter_rows: dict[FiscalPeriodType, tuple[int, datetime, int] | None] = {}
    quarter_cells: dict[FiscalPeriodType, list[_SegmentCell]] = {}
    for q in _QUARTERS:
        row = _latest_period_row(conn, ticker=ticker, fiscal_period_type=q, fiscal_year=fiscal_year)
        quarter_rows[q] = row
        quarter_cells[q] = _cells_for_period_id(conn, row[0]) if row is not None else []

    if all(quarter_rows[q] is None for q in _QUARTERS):
        result.skipped_reason = "no_quarterly_segment_data"
        return result

    for fy_cell in fy_cells:
        if fy_cell.metric in _BALANCE_METRICS:
            # A balance snapshot has no Q4 flow to derive -- refuse with an
            # honest coverage row rather than writing a negative artifact.
            result.reason_counts["balance_metric_not_derivable"] = (
                result.reason_counts.get("balance_metric_not_derivable", 0) + 1
            )
            result.not_computable_count += 1
            record_coverage(
                conn,
                ticker=ticker,
                period_end=fy_period_end,
                fiscal_period_type="Q4",
                dim_type=fy_cell.dim_type,
                dim_name=fy_cell.dim_name,
                status="not_computable",
                reason_code="balance_metric_not_derivable",
                source_doc_id=fy_source_doc_id,
                method_version=METHOD_VERSION,
            )
            continue
        # §3.1 point 3: whether the segment identity itself appears in ANY
        # quarter (even if not the one currently missing a match) decides
        # the reason code -- "unmatched_segment_identity" means the segment
        # never shows up anywhere across Q1-Q3 (a genuinely new/renamed
        # segment, and the primary recast signal); "missing_prior_anchor_
        # for_subtraction" means the segment identity IS known elsewhere but
        # this particular quarter's filing/metric wasn't captured.
        all_quarter_candidates = [c for q in _QUARTERS for c in quarter_cells[q]]
        segment_identity_seen_anywhere = _segment_identity_present(fy_cell, all_quarter_candidates)

        inputs: dict[FiscalPeriodType, _SegmentCell] = {}
        missing_reason: str | None = None
        for q in _QUARTERS:
            match = _find_match(fy_cell, quarter_cells[q])
            if match is None:
                missing_reason = (
                    "missing_prior_anchor_for_subtraction"
                    if segment_identity_seen_anywhere
                    else "unmatched_segment_identity"
                )
                break
            inputs[q] = match

        if missing_reason is not None:
            result.reason_counts[missing_reason] = result.reason_counts.get(missing_reason, 0) + 1
            result.not_computable_count += 1
            record_coverage(
                conn,
                ticker=ticker,
                period_end=fy_period_end,
                fiscal_period_type="Q4",
                dim_type=fy_cell.dim_type,
                dim_name=fy_cell.dim_name,
                status="not_computable",
                reason_code=missing_reason,
                source_doc_id=fy_source_doc_id,
                method_version=METHOD_VERSION,
            )
            continue

        q1, q2, q3 = (
            inputs[FiscalPeriodType.Q1],
            inputs[FiscalPeriodType.Q2],
            inputs[FiscalPeriodType.Q3],
        )
        derived_value = fy_cell.value - (q1.value + q2.value + q3.value)
        hops = 1 + _hop_count(q1) + _hop_count(q2) + _hop_count(q3)
        confidences = [fy_cell.confidence, q1.confidence, q2.confidence, q3.confidence]
        tolerance_breach = False
        if fy_cell.metric == "revenue" and derived_value < 0:
            confidence = _SIGN_SANITY_FLOOR_CONFIDENCE
            tolerance_breach = True
        else:
            confidence = min(confidences) * (_DERIVE_CONFIDENCE_DECAY**hops)

        existing = _find_existing_q4_head(
            conn,
            ticker=ticker,
            period_end=fy_period_end,
            dim_type=fy_cell.dim_type,
            dim_name=fy_cell.dim_name,
            metric=fy_cell.metric,
        )
        supersedes_id: int | None = None
        if existing is not None:
            if abs(existing.value - derived_value) <= _VALUE_EPSILON:
                # Same inputs (or inputs that net to the same figure) already
                # derived this cell -- idempotent no-op, matches the design
                # doc's "(ticker, fiscal_year, {input source_doc_ids})" key.
                continue
            if not is_later_filing(
                conn,
                new_source_doc_id=fy_source_doc_id,
                incumbent_source_doc_id=existing.source_doc_id,
            ):
                # The stored figure came from an equal-or-later filing than
                # this run's own FY anchor -- do not overwrite with a stale
                # recomputation.
                continue
            old_fy_doc_id = _existing_fy_doc_id_from_derived_from(
                _derived_from_of(conn, existing.id)
            )
            window = _comparative_window_years(conn, fy_source_doc_id)
            new_reporting_year = _document_reporting_year(conn, fy_source_doc_id)
            old_reporting_year = (
                _document_reporting_year(conn, old_fy_doc_id) if old_fy_doc_id is not None else None
            )
            if (
                new_reporting_year is not None
                and old_reporting_year is not None
                and (new_reporting_year - old_reporting_year) > window
            ):
                result.not_computable_count += 1
                record_coverage(
                    conn,
                    ticker=ticker,
                    period_end=fy_period_end,
                    fiscal_period_type="Q4",
                    dim_type=fy_cell.dim_type,
                    dim_name=fy_cell.dim_name,
                    status="not_computable",
                    reason_code="recast_beyond_comparative_window",
                    source_doc_id=fy_source_doc_id,
                    method_version=METHOD_VERSION,
                )
                continue
            supersedes_id = existing.id

        derived_from = json.dumps(
            {
                "display": "Q4 = FY - (Q1 + Q2 + Q3)",
                "inputs": [
                    {
                        "ref": "segment_dimension",
                        "id": fy_cell.id,
                        "period_end": fy_cell.period_end.isoformat(),
                        "doc_id": fy_source_doc_id,
                        "fiscal_period_type": "FY",
                    },
                    *(
                        {
                            "ref": "segment_dimension",
                            "id": c.id,
                            "period_end": c.period_end.isoformat(),
                            "doc_id": c.source_doc_id,
                            "fiscal_period_type": fpt.value,
                        }
                        for fpt, c in (
                            (FiscalPeriodType.Q1, q1),
                            (FiscalPeriodType.Q2, q2),
                            (FiscalPeriodType.Q3, q3),
                        )
                    ),
                ],
            },
            sort_keys=True,
        )
        dim = SegmentDimension(
            dim_type=SegmentDimType(fy_cell.dim_type),
            dim_name=fy_cell.dim_name,
            value=derived_value,
            metric=fy_cell.metric,
            disclosure_status="derived",
            method_version=METHOD_VERSION,
            confidence=confidence,
            extracted_by="segment_q4_derive_v1",
            derived_from=derived_from,
            supersedes_id=supersedes_id,
        )
        _, dims_inserted = write_segment_facts_junction(
            conn,
            ticker=ticker,
            period_end=fy_period_end,
            fiscal_period_type=FiscalPeriodType.Q4,
            source_doc_id=fy_source_doc_id,
            currency=None,
            unit=Unit.ACTUAL,
            dimensions=[dim],
            period_basis="derived",
            period_method_version=METHOD_VERSION,
        )
        result.derived_inserted += dims_inserted
        if supersedes_id is not None and dims_inserted:
            result.superseded_count += 1
        if tolerance_breach:
            result.tolerance_breach_count += 1
            record_coverage(
                conn,
                ticker=ticker,
                period_end=fy_period_end,
                fiscal_period_type="Q4",
                dim_type=fy_cell.dim_type,
                dim_name=fy_cell.dim_name,
                status="tolerance_breach",
                reason_code="negative_derived_value",
                source_doc_id=fy_source_doc_id,
                method_version=METHOD_VERSION,
            )
            sys.stderr.write(
                json.dumps(
                    {
                        "event": "segment_q4_derive_tolerance_breach",
                        "reason": "negative_derived_value",
                        "ticker": ticker,
                        "fiscal_year": fiscal_year,
                        "segment": fy_cell.dim_name,
                        "metric": fy_cell.metric,
                        "value": str(derived_value),
                    }
                )
                + "\n"
            )

    _cross_check_consolidated(
        conn, ticker=ticker, fiscal_year=fiscal_year, period_end=fy_period_end
    )
    conn.commit()
    return result


def _derived_from_of(conn: sqlite3.Connection, segment_dimension_id: int) -> str | None:
    row = conn.execute(
        "SELECT derived_from FROM segment_dimensions WHERE id = ?", (segment_dimension_id,)
    ).fetchone()
    if row is None:
        return None
    return cast("str | None", row[0])


def _cross_check_consolidated(
    conn: sqlite3.Connection, *, ticker: str, fiscal_year: int, period_end: datetime
) -> None:
    """§3.3: Sigma derived-Q4-per-segment vs. consolidated Q4 revenue from
    financial_facts, same RECONCILE_TOLERANCE_OVER band segment_quarterly_10q
    and compute.segments both already use (reused via
    ``segment_sum_exceeds_revenue`` rather than a hand-rolled comparison, one
    step more reused than Phase 1's own inline check). Log + coverage-row,
    never blocks a write -- the derived rows are already committed above."""
    try:
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='financial_facts'"
        ).fetchone()
    except sqlite3.Error:
        return
    if present is None:
        return
    seg_rows = conn.execute(
        """
        SELECT sd.value FROM segment_dimensions sd
        JOIN segment_periods sp ON sp.id = sd.period_id
        WHERE sp.ticker = ? AND sd.metric = 'revenue' AND sd.disclosure_status = 'derived'
          AND sd.method_version = ? AND sp.fiscal_period_type = 'Q4'
          AND strftime('%Y', sp.period_end) = ?
        """,
        (ticker, METHOD_VERSION, str(fiscal_year)),
    ).fetchall()
    if not seg_rows:
        return
    rev_row = conn.execute(
        """
        SELECT value FROM financial_facts
        WHERE ticker = ? AND line_item = 'revenue' AND fiscal_period_type = 'Q4'
          AND strftime('%Y', period_end) = ?
        ORDER BY id DESC LIMIT 1
        """,
        (ticker, str(fiscal_year)),
    ).fetchone()
    if rev_row is None:
        return
    consolidated = Decimal(str(rev_row[0]))
    if consolidated == 0:
        return
    values = [Decimal(str(r[0])) for r in seg_rows]
    exceeds, seg_sum = segment_sum_exceeds_revenue(
        values, consolidated, tolerance=RECONCILE_TOLERANCE_OVER
    )
    if not exceeds:
        return
    record_coverage(
        conn,
        ticker=ticker,
        period_end=period_end,
        fiscal_period_type="Q4",
        dim_type=None,
        dim_name=None,
        status="tolerance_breach",
        reason_code="sum_exceeds_consolidated_revenue",
        source_doc_id=None,
        method_version=METHOD_VERSION,
    )
    sys.stderr.write(
        json.dumps(
            {
                "event": "segment_q4_derive_tolerance_breach",
                "reason": "sum_exceeds_consolidated_revenue",
                "ticker": ticker,
                "fiscal_year": fiscal_year,
                "segment_sum": str(seg_sum),
                "consolidated_revenue": str(consolidated),
            }
        )
        + "\n"
    )
