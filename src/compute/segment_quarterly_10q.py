# pyright: reportPrivateUsage=false
# This module reuses two private helpers from generic_xbrl_capture.py by
# cross-module import (_is_detail_section, _detect_fye_monthday) — the same
# established convention llm/cli.py documents for _setup_verified/
# _claude_cli_path and compute/segment_crosstabs_llm.py already uses for
# llm_client._call_claude. generic_xbrl_capture.py itself is not edited here
# beyond its own sanctioned import-only change (see that module's docstring).
"""Deterministic Stage-A 10-Q segment extractor + LLM Stage-B fallback
(docs/design/segment_quarterly_framework.md §2, Phase 1 — 10-K-regime
domestic filers only).

Fills the going-forward gap FMP's Starter-plan downgrade opened: quarterly
segment revenue/OI, no longer available from the derived
``revenue-*-segmentation`` endpoints (annual-only now), instead read from
the as-filed ``{T}_form_10q_{Y}_{Q}.json`` (``financial-reports-json``,
cached as ``fmp_10q_json``) using the deterministic period-axis resolver in
``table_extractors.period_axis`` + the shared value-classification guards in
``table_extractors.xbrl_value_classify`` (the identical guards
``generic_xbrl_capture.py`` uses — same FMP JSON shape, same mis-scale
traps).

Scope (Phase 1): ``filing_regime == '10-K'`` tickers only
(``source_routing.segment_pipeline_for_regime`` returns ``tenq_10k_regime``).
Q4 derivation, recast handling, and the ``segment_quarterly_coverage`` audit
table are Phase 2 — the one-hop Q2/Q3 discrete derivation (§2.6) IS Phase 1
scope (it's part of the period-axis resolver's own classification algorithm,
not the Q4-specific §3 logic).

Phase 2 note: the §2.6 guard writes below (missing-anchor, sign-sanity,
cross-check-against-consolidated) were log-only in Phase 1; Phase 2 promotes
them to ALSO write a ``segment_quarterly_coverage`` row (``pipeline.
segment_quarterly_coverage.record_coverage``) so they're queryable per-cell,
not just grep-able in stderr. See ``compute.segment_q4_derive`` for the Q4
sibling of this same guard set.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

from compute.segments import RECONCILE_TOLERANCE_OVER
from llm.structured import StructuredParseError, call_llm_structured
from models.facts import FactLocator, FiscalPeriodType, SegmentDimension, SegmentDimType, Unit
from pipeline.segment_junction_writer import write_segment_facts_junction
from pipeline.segment_quarterly_coverage import record_coverage
from table_extractors.base import iter_xbrl_table, parse_units
from table_extractors.generic_xbrl_capture import _is_detail_section
from table_extractors.period_axis import (
    NominalQuarter,
    PeriodAxisClass,
    PeriodAxisResult,
    classify_period_column,
)
from table_extractors.xbrl_value_classify import (
    SCALE_FACTOR,
    classify_value,
    is_unit_ambiguous_section,
)

EXTRACTOR_ID = "segment_quarterly_10q_v1"
EXTRACTOR_VERSION = "1"
LLM_PURPOSE = "segment_10q_period_disambiguate"

# Row-label -> junction metric. Mirrors compute.segment_oi_10k's convention
# (BUSINESS_UNIT dim_type, revenue/cost_of_revenue/operating_income metrics)
# — this is the quarterly-cadence sibling of that 10-K-shaped extractor, NOT
# a replacement for compute/segments.py's FMP 1-axis product/geo reader
# (Finding 1's "match this shape exactly" is about that endpoint's own
# consumers, a separate concern).
_REVENUE_TOKENS = ("total revenue", "net sales", "revenues")
_COST_TOKENS = ("total costs and expenses", "cost of revenue", "costs and expenses")
_OI_TOKENS = ("operating income", "income (loss) from operations", "income from operations")

_SECTION_KEYWORD_RX = re.compile(r"segment", re.IGNORECASE)

# Confidence decay per derivation hop (§2.6 point 3) — a placeholder per the
# design doc's own §7 risk #4, not yet calibrated against real data.
_DERIVE_CONFIDENCE_DECAY = 0.97
_SIGN_SANITY_FLOOR_CONFIDENCE = 0.3


@dataclass(slots=True)
class SegmentQuarterlyResult:
    ticker: str
    year: int
    quarter: str
    source_path: str | None = None
    source_sha256: str | None = None
    source_doc_id: int | None = None
    periods_inserted: int = 0
    dimensions_inserted: int = 0
    derived_inserted: int = 0
    llm_fallback_calls: int = 0
    skipped_reason: str | None = None
    skip_counts: dict[str, int] = field(default_factory=dict[str, int])


def _cache_path(repo_root: Path, ticker: str, year: int, quarter: str) -> Path:
    out_dir = repo_root / "data" / "segment_quarterly"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{ticker}_{year}_{quarter}.json"


def _locate_10q(repo_root: Path, ticker: str, year: int, quarter: str) -> Path | None:
    path = repo_root / "data" / "historical" / "fmp" / f"{ticker}_form_10q_{year}_{quarter}.json"
    return path if path.exists() else None


def _resolve_doc_id(
    conn: sqlite3.Connection, *, ticker: str, year: int, quarter: str, repo_root: Path
) -> int | None:
    """documents.id for this ticker's fmp_10q_json filing. Mirrors
    generic_xbrl_capture._resolve_doc_id (same file_path natural-key lookup,
    same filename-suffix fallback), scoped to the 10-Q filename shape."""
    try:
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='documents'"
        ).fetchone()
    except sqlite3.Error:
        return None
    if present is None:
        return None
    fname = f"{ticker}_form_10q_{year}_{quarter}.json"
    rel = (repo_root / "data" / "historical" / "fmp" / fname).as_posix()
    rel_repo = f"data/historical/fmp/{fname}"
    for path in {rel, rel_repo}:
        try:
            row = conn.execute(
                "SELECT id FROM documents "
                "WHERE ticker = ? AND doc_type = 'fmp_10q_json' AND file_path = ? LIMIT 1",
                (ticker, path),
            ).fetchone()
        except sqlite3.Error:
            row = None
        if row is not None:
            return int(row[0])
    try:
        row = conn.execute(
            "SELECT id FROM documents "
            "WHERE ticker = ? AND doc_type = 'fmp_10q_json' AND file_path LIKE ? "
            "ORDER BY id DESC LIMIT 1",
            (ticker, f"%{fname}"),
        ).fetchone()
    except sqlite3.Error:
        row = None
    return int(row[0]) if row is not None else None


def _resolve_fye_monthday(
    conn: sqlite3.Connection, *, ticker: str, repo_root: Path
) -> tuple[int, int] | None:
    """(month, day) fiscal-year-end for ``ticker``.

    Primary: ``tracked_companies.fiscal_year_end`` ("MM-DD", written by
    ``fmp_doc_index.set_fiscal_year_end_from_fmp``). Fallback: the modal-date
    detector already implemented for the 10-K capture-all pass
    (``generic_xbrl_capture._detect_fye_monthday``), run against the
    ticker's most recent 10-K JSON if one is on disk — reused by import, not
    reforked (§2.1's "no fourth copy" instruction is about
    ``_FYE_OFFSETS``-style tables; this is the existing detector function
    itself).
    """
    try:
        row = conn.execute(
            "SELECT fiscal_year_end FROM tracked_companies WHERE ticker = ? LIMIT 1",
            (ticker,),
        ).fetchone()
    except sqlite3.Error:
        row = None
    if row is not None:
        raw = row["fiscal_year_end"] if isinstance(row, sqlite3.Row) else row[0]
        if isinstance(raw, str) and len(raw) == 5 and raw[2] == "-":
            try:
                month, day = int(raw[:2]), int(raw[3:])
                return (month, day)
            except ValueError:
                pass

    fmp_dir = repo_root / "data" / "historical" / "fmp"
    if not fmp_dir.exists():
        return None
    candidates = sorted(fmp_dir.glob(f"{ticker}_form_10k_*.json"))
    if not candidates:
        return None
    from table_extractors.generic_xbrl_capture import _detect_fye_monthday

    try:
        payload = json.loads(candidates[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return _detect_fye_monthday(cast("dict[str, object]", payload))


def _row_metric(label: str) -> str | None:
    """Classify a segment-note row label into a junction metric, or None
    when the row doesn't match a recognized shape (skipped, tallied)."""
    ll = label.lower()
    if any(tok in ll for tok in _OI_TOKENS):
        return "operating_income"
    if any(tok in ll for tok in _COST_TOKENS):
        return "cost_of_revenue"
    if any(tok in ll for tok in _REVENUE_TOKENS):
        return "revenue"
    return None


def _inner_title(section: Sequence[object]) -> str | None:
    if not section or not isinstance(section[0], dict):
        return None
    keys = list(cast("dict[str, object]", section[0]).keys())
    return keys[0] if keys else None


def _iter_segment_sections(payload: dict[str, object]):
    for key, value in payload.items():
        if not isinstance(value, list) or not value:
            continue
        section = cast("list[dict[str, object]]", value)
        inner = _inner_title(section) or key
        if not _SECTION_KEYWORD_RX.search(inner):
            continue
        if not _is_detail_section(inner):
            continue
        yield key, inner, section


@dataclass(slots=True)
class _CapturedCell:
    dim_name: str
    metric: str
    value: Decimal
    period_end: date
    fiscal_period_type: FiscalPeriodType
    period_basis: Literal["discrete", "cumulative"]
    raw_label: str
    section_key: str
    confidence: float


def extract_for_ticker(
    ticker: str,
    year: int,
    quarter: NominalQuarter,
    repo_root: Path,
    conn: sqlite3.Connection,
    *,
    db_path: Path | None = None,
    refresh: bool = False,
) -> SegmentQuarterlyResult:
    """End-to-end 10-Q segment quarterly extraction for one (ticker, year,
    quarter). Idempotent on (ticker, year, quarter, source_sha256) via a
    cache at ``data/segment_quarterly/{T}_{Y}_{Q}.json`` unless
    ``refresh=True``; the DB side is independently idempotent (the junction
    writer dedupes on its natural keys).

    ``db_path`` is not part of the design doc's literal CLI contract but is
    threaded through so the LLM Stage-B fallback's budget/ledger writes land
    in the SAME database ``conn`` points at (call_llm_structured needs a
    path, not a connection, to scope its DB-backed layers).
    """
    ticker = ticker.upper()
    result = SegmentQuarterlyResult(ticker=ticker, year=year, quarter=quarter)
    cache_path = _cache_path(repo_root, ticker, year, quarter)

    source_path = _locate_10q(repo_root, ticker, year, quarter)
    if source_path is None:
        result.skipped_reason = "no_10q_json_fetched"
        return result

    raw_bytes = source_path.read_bytes()
    sha256 = hashlib.sha256(raw_bytes).hexdigest()
    result.source_path = str(source_path)
    result.source_sha256 = sha256

    if not refresh and cache_path.exists():
        try:
            cached_obj: object = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached_obj = None
        if isinstance(cached_obj, dict):
            cached = cast("dict[str, object]", cached_obj)
            if cached.get("source_sha256") == sha256:
                return _result_from_cache(cached)

    doc_id = _resolve_doc_id(conn, ticker=ticker, year=year, quarter=quarter, repo_root=repo_root)
    if doc_id is None:
        result.skipped_reason = "no_fmp_10q_json_documents_row"
        _write_cache(cache_path, result)
        return result
    result.source_doc_id = doc_id

    fye_md = _resolve_fye_monthday(conn, ticker=ticker, repo_root=repo_root)
    if fye_md is None:
        result.skipped_reason = "fiscal_year_end_unresolvable"
        _write_cache(cache_path, result)
        return result
    fye_month, fye_day = fye_md

    payload_obj: object = json.loads(raw_bytes.decode("utf-8"))
    if not isinstance(payload_obj, dict):
        result.skipped_reason = "unparseable_10q_json"
        _write_cache(cache_path, result)
        return result
    payload = cast("dict[str, object]", payload_obj)

    captured: list[_CapturedCell] = []
    skip_counts: dict[str, int] = {}
    llm_calls = 0

    for section_key, inner_title, section in _iter_segment_sections(payload):
        units = parse_units(inner_title)
        if units.scale not in SCALE_FACTOR:
            skip_counts["section_no_scale_hint"] = skip_counts.get("section_no_scale_hint", 0) + 1
            continue
        if is_unit_ambiguous_section(inner_title):
            skip_counts["section_unit_ambiguous"] = skip_counts.get("section_unit_ambiguous", 0) + 1
            continue
        factor = SCALE_FACTOR[units.scale]

        rows = list(iter_xbrl_table(section))
        if not rows:
            continue
        labels = rows[0].period_labels
        classifications = [
            classify_period_column(
                lbl,
                nominal_quarter=quarter,
                fiscal_year=year,
                fye_month=fye_month,
                fye_day=fye_day,
            )
            for lbl in labels
        ]

        ambiguous_idx = [
            i for i, c in enumerate(classifications) if c.classification == "ambiguous_same_date"
        ]
        if ambiguous_idx:
            resolved = _disambiguate_via_llm(
                ticker,
                labels,
                ambiguous_idx,
                quarter=quarter,
                year=year,
                fye_month=fye_month,
                fye_day=fye_day,
                db_path=db_path,
            )
            if resolved is not None:
                llm_calls += 1
                for i, (duration, cumulative) in resolved.items():
                    if i >= len(classifications):
                        continue
                    c = classifications[i]
                    new_class: PeriodAxisClass
                    if duration == 3 and not cumulative:
                        new_class = "current_discrete"
                    elif cumulative:
                        new_class = "current_cumulative"
                    else:
                        new_class = "off_cycle"
                    classifications[i] = PeriodAxisResult(
                        new_class,
                        c.parsed,
                        c.current_end,
                        c.prior_year_end,
                        "" if new_class != "off_cycle" else "off_cycle_or_unparseable_period",
                    )

        for row in rows:
            if not row.axis_path:
                # No segment axis marker scoping this row — a whole-filing
                # rollup line, not a per-segment cell. Skip (not a segment
                # dimension).
                continue
            segment_name = row.axis_path[-1]
            metric = _row_metric(row.label)
            if metric is None:
                skip_counts["unrecognized_segment_row_label"] = (
                    skip_counts.get("unrecognized_segment_row_label", 0) + 1
                )
                continue
            for col, raw in enumerate(row.values):
                if raw is None:
                    continue
                if col >= len(classifications):
                    continue
                cls = classifications[col]
                if cls.classification == "off_cycle":
                    skip_counts[cls.reason_code or "off_cycle_or_unparseable_period"] = (
                        skip_counts.get(cls.reason_code or "off_cycle_or_unparseable_period", 0) + 1
                    )
                    continue
                if cls.classification == "ambiguous_same_date":
                    skip_counts["period_axis_ambiguous_no_duration_prefix"] = (
                        skip_counts.get("period_axis_ambiguous_no_duration_prefix", 0) + 1
                    )
                    continue
                value, reason, confidence = classify_value(row.label, raw, factor)
                if value is None:
                    skip_counts[reason] = skip_counts.get(reason, 0) + 1
                    continue
                period_end = (
                    cls.current_end
                    if cls.classification in ("current_discrete", "current_cumulative")
                    else cls.prior_year_end
                )
                fpt = _fiscal_period_type_for(cls.classification, quarter)
                basis: Literal["discrete", "cumulative"] = (
                    "cumulative" if cls.classification == "current_cumulative" else "discrete"
                )
                captured.append(
                    _CapturedCell(
                        dim_name=segment_name,
                        metric=metric,
                        value=value,
                        period_end=period_end,
                        fiscal_period_type=fpt,
                        period_basis=basis,
                        raw_label=labels[col] if col < len(labels) else "",
                        section_key=section_key,
                        confidence=confidence,
                    )
                )

    periods_inserted, dims_inserted = _persist(conn, ticker=ticker, doc_id=doc_id, cells=captured)
    derived_inserted = _derive_discrete_from_cumulative(
        conn,
        ticker=ticker,
        year=year,
        quarter=quarter,
        cells=captured,
        source_doc_id=doc_id,
    )
    conn.commit()

    result.periods_inserted = periods_inserted
    result.dimensions_inserted = dims_inserted
    result.derived_inserted = derived_inserted
    result.llm_fallback_calls = llm_calls
    result.skip_counts = skip_counts
    if not captured:
        result.skipped_reason = result.skipped_reason or "no_segment_sections_matched"
    _write_cache(cache_path, result)
    return result


def _fiscal_period_type_for(classification: str, quarter: NominalQuarter) -> FiscalPeriodType:
    if classification == "current_discrete":
        return FiscalPeriodType(quarter)
    if classification == "current_cumulative":
        return FiscalPeriodType.H1 if quarter == "Q2" else FiscalPeriodType.NINE_MONTHS
    # prior_year_comparative: same nominal shape, prior fiscal year.
    return FiscalPeriodType(quarter)


def _persist(
    conn: sqlite3.Connection, *, ticker: str, doc_id: int, cells: list[_CapturedCell]
) -> tuple[int, int]:
    grouped: dict[tuple[date, FiscalPeriodType, str, str], list[_CapturedCell]] = {}
    for cell in cells:
        key = (cell.period_end, cell.fiscal_period_type, cell.period_basis, cell.raw_label)
        grouped.setdefault(key, []).append(cell)

    periods_inserted = 0
    dims_inserted = 0
    for (period_end, fpt, basis, raw_label), group in grouped.items():
        dims = [
            SegmentDimension(
                dim_type=SegmentDimType.BUSINESS_UNIT,
                dim_name=c.dim_name,
                value=c.value,
                metric=c.metric,
                disclosure_status="reported",
                method_version=(
                    "tenq_discrete_v1" if basis == "discrete" else "tenq_cumulative_v1"
                ),
                confidence=c.confidence,
                extracted_by=EXTRACTOR_ID,
                locator=FactLocator(section=c.section_key).to_json(),
            )
            for c in group
        ]
        p_ins, d_ins = write_segment_facts_junction(
            conn,
            ticker=ticker,
            period_end=datetime(period_end.year, period_end.month, period_end.day),
            fiscal_period_type=fpt,
            source_doc_id=doc_id,
            currency=None,
            unit=Unit.ACTUAL,
            dimensions=dims,
            period_basis=basis,
            raw_period_label=raw_label,
            period_method_version="period_axis_v1",
        )
        periods_inserted += p_ins
        dims_inserted += d_ins
    return periods_inserted, dims_inserted


def _derive_discrete_from_cumulative(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    year: int,
    quarter: NominalQuarter,
    cells: list[_CapturedCell],
    source_doc_id: int,
) -> int:
    """§2.6 one-hop derivation: Q2 discrete = H1 cumulative − Q1 discrete;
    Q3 discrete = 9M cumulative − (Q1 discrete + Q2 discrete). Q1 never
    needs derivation (its own discrete column IS the answer).

    Guards, all recorded via BOTH a stderr JSON log line AND a
    ``segment_quarterly_coverage`` row (Phase 2 promoted these from
    log-only to persisted — the stderr line stays for real-time log
    tailing, never silent either way):
      1. missing prior anchor -> do not derive.
      2. sign sanity -> still persisted, confidence floors at 0.3.
      3. confidence decays one hop (``_DERIVE_CONFIDENCE_DECAY``).
      4. cross-check against consolidated revenue (RECONCILE_TOLERANCE_OVER).
    """
    if quarter == "Q1":
        return 0
    cumulative_cells = [c for c in cells if c.period_basis == "cumulative"]
    if not cumulative_cells:
        return 0

    prior_quarters: tuple[FiscalPeriodType, ...] = (
        (FiscalPeriodType.Q1,) if quarter == "Q2" else (FiscalPeriodType.Q1, FiscalPeriodType.Q2)
    )
    derived_inserted = 0
    for cell in cumulative_cells:
        priors: list[Decimal] = []
        prior_confidences: list[float] = []
        missing = False
        for pq in prior_quarters:
            prior_end = _quarter_end_for_fiscal_year(
                conn, ticker=ticker, fiscal_year=year, quarter=pq
            )
            if prior_end is None:
                missing = True
                break
            row = conn.execute(
                """
                SELECT sd.value, sd.confidence
                FROM segment_dimensions sd
                JOIN segment_periods sp ON sp.id = sd.period_id
                WHERE sp.ticker = ? AND sp.period_end = ? AND sp.fiscal_period_type = ?
                  AND sd.dim_name = ? AND sd.metric = ? AND sd.dim_type = 'business_unit'
                ORDER BY sd.id DESC LIMIT 1
                """,
                (ticker, prior_end, pq.value, cell.dim_name, cell.metric),
            ).fetchone()
            if row is None:
                missing = True
                break
            priors.append(Decimal(str(row[0])))
            prior_confidences.append(float(row[1]) if row[1] is not None else 1.0)
        if missing:
            sys.stderr.write(
                json.dumps(
                    {
                        "event": "segment_quarterly_derive_skipped",
                        "reason": "missing_prior_anchor_for_subtraction",
                        "ticker": ticker,
                        "fiscal_year": year,
                        "quarter": quarter,
                        "segment": cell.dim_name,
                        "metric": cell.metric,
                    }
                )
                + "\n"
            )
            record_coverage(
                conn,
                ticker=ticker,
                period_end=datetime(
                    cell.period_end.year, cell.period_end.month, cell.period_end.day
                ),
                fiscal_period_type=quarter,
                dim_type="business_unit",
                dim_name=cell.dim_name,
                status="not_computable",
                reason_code="missing_prior_anchor_for_subtraction",
                source_doc_id=source_doc_id,
                method_version="segment_q2q3_derive_v1",
            )
            continue

        derived_value = cell.value - sum(priors, start=Decimal("0"))
        tolerance_breach = False
        if cell.metric == "revenue" and derived_value < 0:
            confidence = _SIGN_SANITY_FLOOR_CONFIDENCE
            tolerance_breach = True
        else:
            confidence = min([cell.confidence, *prior_confidences]) * _DERIVE_CONFIDENCE_DECAY

        derive_quarter = FiscalPeriodType(quarter)
        derived_from = json.dumps(
            {
                "display": f"{quarter} discrete = cumulative - prior discrete quarter(s)",
                "inputs": [
                    {
                        "ref": "segment_dimension",
                        "period_end": cell.period_end.isoformat(),
                        "fiscal_period_type": cell.fiscal_period_type.value,
                    },
                    *(
                        {"ref": "segment_dimension", "fiscal_period_type": pq.value}
                        for pq in prior_quarters
                    ),
                ],
            },
            sort_keys=True,
        )
        dim = SegmentDimension(
            dim_type=SegmentDimType.BUSINESS_UNIT,
            dim_name=cell.dim_name,
            value=derived_value,
            metric=cell.metric,
            disclosure_status="derived",
            method_version="segment_q2q3_derive_v1",
            confidence=confidence,
            extracted_by=EXTRACTOR_ID,
            derived_from=derived_from,
        )
        _, d_ins = write_segment_facts_junction(
            conn,
            ticker=ticker,
            period_end=datetime(cell.period_end.year, cell.period_end.month, cell.period_end.day),
            fiscal_period_type=derive_quarter,
            source_doc_id=source_doc_id,
            currency=None,
            unit=Unit.ACTUAL,
            dimensions=[dim],
            period_basis="derived",
            period_method_version="segment_q2q3_derive_v1",
        )
        derived_inserted += d_ins
        if tolerance_breach:
            sys.stderr.write(
                json.dumps(
                    {
                        "event": "segment_quarterly_derive_tolerance_breach",
                        "reason": "negative_derived_value",
                        "ticker": ticker,
                        "quarter": quarter,
                        "segment": cell.dim_name,
                        "metric": cell.metric,
                        "value": str(derived_value),
                    }
                )
                + "\n"
            )
            record_coverage(
                conn,
                ticker=ticker,
                period_end=datetime(
                    cell.period_end.year, cell.period_end.month, cell.period_end.day
                ),
                fiscal_period_type=derive_quarter,
                dim_type="business_unit",
                dim_name=cell.dim_name,
                status="tolerance_breach",
                reason_code="negative_derived_value",
                source_doc_id=source_doc_id,
                method_version="segment_q2q3_derive_v1",
            )
    _cross_check_consolidated(conn, ticker=ticker, year=year, quarter=quarter)
    return derived_inserted


def _quarter_end_for_fiscal_year(
    conn: sqlite3.Connection, *, ticker: str, fiscal_year: int, quarter: FiscalPeriodType
) -> datetime | None:
    row = conn.execute(
        """
        SELECT sp.period_end FROM segment_periods sp
        WHERE sp.ticker = ? AND sp.fiscal_period_type = ?
          AND strftime('%Y', sp.period_end) = ?
        ORDER BY sp.period_end DESC LIMIT 1
        """,
        (ticker, quarter.value, str(fiscal_year)),
    ).fetchone()
    if row is None:
        return None
    raw = row[0]
    if isinstance(raw, str):
        return datetime.fromisoformat(raw[:19])
    return cast("datetime | None", raw)


def _cross_check_consolidated(
    conn: sqlite3.Connection, *, ticker: str, year: int, quarter: NominalQuarter
) -> None:
    """§2.6 point 4: Σ derived segments' Qn revenue vs. consolidated Qn
    revenue from financial_facts, same RECONCILE_TOLERANCE_OVER band
    compute/segments.py already uses. Never blocks a write; Phase 2 promotes
    this from log-only to ALSO a whole-filing-level (dim_type/dim_name=NULL)
    ``segment_quarterly_coverage`` row on breach."""
    try:
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='financial_facts'"
        ).fetchone()
    except sqlite3.Error:
        return
    if present is None:
        return
    row = conn.execute(
        """
        SELECT sd.value FROM segment_dimensions sd
        JOIN segment_periods sp ON sp.id = sd.period_id
        WHERE sp.ticker = ? AND sd.metric = 'revenue' AND sd.disclosure_status = 'derived'
          AND sp.fiscal_period_type = ? AND strftime('%Y', sp.period_end) = ?
        """,
        (ticker, quarter, str(year)),
    ).fetchall()
    if not row:
        return
    seg_sum = sum((Decimal(str(r[0])) for r in row), start=Decimal("0"))
    rev_row = conn.execute(
        """
        SELECT value FROM financial_facts
        WHERE ticker = ? AND line_item = 'revenue' AND fiscal_period_type = ?
          AND strftime('%Y', period_end) = ?
        ORDER BY id DESC LIMIT 1
        """,
        (ticker, quarter, str(year)),
    ).fetchone()
    if rev_row is None:
        return
    consolidated = Decimal(str(rev_row[0]))
    if consolidated == 0:
        return
    if seg_sum > consolidated * Decimal(str(1 + RECONCILE_TOLERANCE_OVER)):
        sys.stderr.write(
            json.dumps(
                {
                    "event": "segment_quarterly_derive_tolerance_breach",
                    "reason": "sum_exceeds_consolidated_revenue",
                    "ticker": ticker,
                    "quarter": quarter,
                    "fiscal_year": year,
                    "segment_sum": str(seg_sum),
                    "consolidated_revenue": str(consolidated),
                }
            )
            + "\n"
        )
        period_end = _quarter_end_for_fiscal_year(
            conn, ticker=ticker, fiscal_year=year, quarter=FiscalPeriodType(quarter)
        )
        if period_end is not None:
            record_coverage(
                conn,
                ticker=ticker,
                period_end=period_end,
                fiscal_period_type=quarter,
                dim_type=None,
                dim_name=None,
                status="tolerance_breach",
                reason_code="sum_exceeds_consolidated_revenue",
                source_doc_id=None,
                method_version="segment_q2q3_derive_v1",
            )


# ---------------------------------------------------------------------------
# LLM Stage B — period-axis disambiguation fallback
# ---------------------------------------------------------------------------


def _build_disambiguate_prompt(
    labels: list[str],
    indices: list[int],
    *,
    nominal_quarter: NominalQuarter,
    cumulative_months: int,
    current_end: str,
    prior_year_end: str,
) -> str:
    ambiguous = [{"index": i, "label": labels[i]} for i in indices]
    return f"""You are disambiguating column headers in a 10-Q filing's segment note table.

The filing is nominal fiscal quarter {nominal_quarter}. Its expected end-dates are:
- current period end: {current_end}
- prior-year comparative end: {prior_year_end}
- the current cumulative (year-to-date) column, if present, covers {cumulative_months} months.

The following column headers could not be resolved deterministically (no
"N Months Ended" duration prefix, or an unusual phrasing) — for EACH one,
read the header's English prose and return the duration in months, the
period end-date (ISO YYYY-MM-DD), and whether it's a cumulative (year-to-date)
column rather than the discrete 3-month quarter.

Ambiguous columns:
{json.dumps(ambiguous, indent=2)}

Return STRICT JSON, no markdown fence, no commentary:

{{
  "columns": [
    {{"index": 0, "duration_months": 3, "end_date": "{current_end}", "is_cumulative": false}}
  ]
}}

One entry per ambiguous column above, by index. If a column's shape genuinely
cannot be determined from its header text, omit it from the array (do not guess)."""


def _disambiguate_via_llm(
    ticker: str,
    labels: list[str],
    indices: list[int],
    *,
    quarter: NominalQuarter,
    year: int,
    fye_month: int,
    fye_day: int,
    db_path: Path | None,
) -> dict[int, tuple[int | None, bool]] | None:
    """Returns {{index: (duration_months, is_cumulative)}} for however many
    of ``indices`` the LLM resolved, or None on a call/parse failure (the
    caller leaves those columns off_cycle/ambiguous — never a silent guess)."""
    from table_extractors.period_axis import expected_period_ends

    current_end, prior_year_end = expected_period_ends(quarter, year, fye_month, fye_day)
    cumulative_months = {"Q1": 3, "Q2": 6, "Q3": 9}[quarter]
    prompt = _build_disambiguate_prompt(
        labels,
        indices,
        nominal_quarter=quarter,
        cumulative_months=cumulative_months,
        current_end=current_end.isoformat(),
        prior_year_end=prior_year_end.isoformat(),
    )
    try:
        payload = call_llm_structured(
            prompt,
            purpose=LLM_PURPOSE,
            ticker=ticker,
            expect="object",
            required_keys=("columns",),
            db_path=db_path,
        )
    except StructuredParseError:
        return None
    return _parse_disambiguate_response(payload)


def _parse_disambiguate_response(payload: object) -> dict[int, tuple[int | None, bool]] | None:
    if not isinstance(payload, dict):
        return None
    columns = cast("dict[str, object]", payload).get("columns")
    if not isinstance(columns, list):
        return None
    out: dict[int, tuple[int | None, bool]] = {}
    for entry in cast("list[object]", columns):
        if not isinstance(entry, dict):
            continue
        e = cast("dict[str, object]", entry)
        idx = e.get("index")
        if not isinstance(idx, int) or isinstance(idx, bool):
            continue
        duration = e.get("duration_months")
        duration_int = (
            duration if isinstance(duration, int) and not isinstance(duration, bool) else None
        )
        is_cumulative = bool(e.get("is_cumulative"))
        out[idx] = (duration_int, is_cumulative)
    return out


def disambiguate_periods_for_eval(
    labels: list[str],
    *,
    nominal_quarter: str,
    cumulative_months: int,
    current_end: str,
    prior_year_end: str,
) -> dict[str, object]:
    """Eval-harness entry point (evals.golden_classifiers) for the
    ``segment_10q_period_disambiguate`` golden set — calls the same prompt +
    parse path Stage B uses, over an explicit label list rather than an
    ambiguous-index subset (every golden case is, by construction, one the
    deterministic path couldn't resolve)."""
    indices = list(range(len(labels)))
    prompt = _build_disambiguate_prompt(
        labels,
        indices,
        nominal_quarter=cast("NominalQuarter", nominal_quarter),
        cumulative_months=cumulative_months,
        current_end=current_end,
        prior_year_end=prior_year_end,
    )
    payload = call_llm_structured(
        prompt,
        purpose=LLM_PURPOSE,
        expect="object",
        required_keys=("columns",),
    )
    parsed = _parse_disambiguate_response(payload) or {}
    return {
        "columns": [
            {"index": i, "duration_months": d, "is_cumulative": c}
            for i, (d, c) in sorted(parsed.items())
        ]
    }


# ---------------------------------------------------------------------------
# Cache I/O
# ---------------------------------------------------------------------------


def _write_cache(path: Path, result: SegmentQuarterlyResult) -> None:
    path.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")


def _result_from_cache(cached: dict[str, object]) -> SegmentQuarterlyResult:
    """Rebuild a SegmentQuarterlyResult from its cached JSON dict — a
    validated JSON boundary, so each field is read with a type check rather
    than a blind ``**kwargs`` unpack into the dataclass constructor."""
    skip_counts_raw = cached.get("skip_counts")
    skip_counts = (
        {str(k): int(cast("int", v)) for k, v in cast("dict[str, object]", skip_counts_raw).items()}
        if isinstance(skip_counts_raw, dict)
        else {}
    )
    return SegmentQuarterlyResult(
        ticker=str(cached.get("ticker", "")),
        year=int(cast("int", cached.get("year", 0))),
        quarter=str(cached.get("quarter", "")),
        source_path=cast("str | None", cached.get("source_path")),
        source_sha256=cast("str | None", cached.get("source_sha256")),
        source_doc_id=cast("int | None", cached.get("source_doc_id")),
        periods_inserted=int(cast("int", cached.get("periods_inserted", 0))),
        dimensions_inserted=int(cast("int", cached.get("dimensions_inserted", 0))),
        derived_inserted=int(cast("int", cached.get("derived_inserted", 0))),
        llm_fallback_calls=int(cast("int", cached.get("llm_fallback_calls", 0))),
        skipped_reason=cast("str | None", cached.get("skipped_reason")),
        skip_counts=skip_counts,
    )
