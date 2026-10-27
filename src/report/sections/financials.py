"""Canonical financial tables with exact source coordinates and provenance.

Production builds use admitted source observations. ``build_legacy_shadow``
and its loaders retain the previous ranking solely for explicit parity tests.
Quarterly calendar buckets are display labels; annual years are issuer fiscal
identities. KPI chart-priority resolution is a separate existing reader.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import cast

from compute.kpi_resolver import (
    ANNUAL_FACT_PERIOD_TYPES,
    QUARTERLY_FACT_PERIOD_TYPES,
    KpiResolverReadMemo,
    matching_kpi_definition_ids,
    reporting_cadence_for,
    resolve_kpi_definition_names,
    semantic_series_identity_sql,
)
from db_paths import configured_db_path, require_db_path
from identity import DEFAULT_USER_ID
from pipeline.confidence import IssuesByTicker, display_issues_for_fact, load_unresolved_issues
from pipeline.kpi_report_reference_dispositions import canonical_financial_chart_priority
from pipeline.kpi_report_reference_resolver import (
    report_kpi_reference_at,
    verified_report_kpi_reference_definition,
)
from pipeline.kpi_semantics import semantic_admission_sql
from provenance.financial_fact_resolution import canonical_fact_relation
from provenance.overrides import KPI as OVERRIDE_KPI
from provenance.overrides import OverrideAction, chip_override_map, qualify_note
from provenance.source_read_context import SourceReadContext
from report.file_capture import read_captured_text
from report.models import (
    AnnualKpiSeries,
    AnnualLineItem,
    CellSource,
    FinancialsSection,
    GrowthMetrics,
    KpiSeries,
    QuarterlyLineItem,
    SectionStatus,
)
from report.sections._common import (
    ANNUAL_PERIOD_TYPES,
    DISPLAY_QUARTERS,
    QUARTERLY_PERIOD_TYPES,
    UNDERLYING_QUARTERS,
    calendar_quarter_key,
    compute_growth,
    document_version_ids,
    has_table,
    level_cagr,
    missing,
    open_repo_db,
    quarter_label,
)
from sources.financial_growth_evidence import (
    GROWTH_WINDOWS,
    FinancialGrowthReference,
    GrowthFormula,
)
from sources.report_financials import (
    FinancialEvidenceReference,
    FinancialTableCell,
    annual_comparison_supported,
    read_financial_table,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite
from timeseries.loaders import (
    load_financial_cell_provenance,
    load_financial_fact_provenance,
    reader_source_order_sql,
    reader_tier_join_sql,
    reader_tier_rank_sql,
)

# Tier-aware winner ordering for §3's direct financial_facts reads — the SAME
# (source_quality_tier rank, id) contract the Series loaders enforce, so a
# deterministic SEC XBRL row beats an FMP row on the ~162k FMP+SEC duplicated
# keys. The prior ``source_doc_id DESC`` pick was source-agnostic. The
# ROW_NUMBER() CTEs LEFT JOIN documents as ``d`` and interpolate the fragment
# from ``reader_tier_rank_sql`` in their ORDER BY (NULL/absent tier → rank 0, and
# on a pre-0053 fixture without the column the whole fragment degrades to "0",
# collapsing to the id-only pick). Computed per-connection so a legacy DB is safe.

# (metrics-view column, display name, unit, display digits)
_LINE_ITEM_SPECS: list[tuple[str, str, str, int]] = [
    ("revenue", "Revenue", "USD millions", 0),
    ("gross_profit", "Gross profit", "USD millions", 0),
    ("operating_income", "Operating income", "USD millions", 0),
    ("net_income", "Net income", "USD millions", 0),
    ("eps_diluted", "EPS (diluted)", "USD", 2),
    ("operating_cash_flow", "Operating cash flow", "USD millions", 0),
    ("free_cash_flow", "Free cash flow", "USD millions", 0),
    ("capex", "Capex", "USD millions", 0),
]

# metrics-view column -> financial_facts.line_item, for the per-cell source
# chips (P3.3). The view renames only capital_expenditure; everything else
# is identity. Keep in lockstep with migration 0012's view DDL.
_COL_TO_FACT_LINE_ITEM: dict[str, str] = {
    "revenue": "revenue",
    "gross_profit": "gross_profit",
    "operating_income": "operating_income",
    "net_income": "net_income",
    "eps_diluted": "eps_diluted",
    "operating_cash_flow": "operating_cash_flow",
    "free_cash_flow": "free_cash_flow",
    "capex": "capital_expenditure",
}

ANNUAL_HISTORY_YEARS = 10

# Fallback when a holdings JSON omits chart_priorities.
_DEFAULT_CHART_PRIORITIES: tuple[str, ...] = (
    "Revenue",
    "Operating income",
    "Operating cash flow",
    "Free cash flow",
)


def build_legacy_shadow(
    ticker: str,
    repo_root: Path,
    *,
    conn: sqlite3.Connection | None = None,
) -> FinancialsSection:
    db_conn = open_repo_db(repo_root, conn)
    if db_conn is None:
        return _missing(
            "no DB at data/portfolio.db",
            "alembic upgrade head && python execution/extract_facts.py --ticker " + ticker.upper(),
        )
    if not has_table(db_conn, "financial_facts"):
        if conn is None:
            db_conn.close()
        return _missing(
            "financial_facts table absent",
            "alembic upgrade head && python execution/extract_facts.py --ticker " + ticker.upper(),
            "§3 reads facts directly — the metrics view's MAX(period_end) cross-labels "
            "fiscal quarters for off-calendar reporters.",
        )

    quarterly_rows = _load_quarterly(db_conn, ticker)
    annual_rows = _load_annual(db_conn, ticker)
    # Unresolved validation_issues for the ticker, parsed once — formatted
    # into per-cell popover strings below (S2 PR3: disagreement at the cell).
    issue_signals = load_unresolved_issues(db_conn)

    if not quarterly_rows and not annual_rows:
        if conn is None:
            db_conn.close()
        return _missing(
            "no facts in metrics view",
            f"python execution/extract_facts.py --ticker {ticker.upper()}",
        )

    deduped_quarterly = _dedupe_by_calendar_quarter(quarterly_rows)[-UNDERLYING_QUARTERS:]
    quarter_labels_full = [quarter_label(str(r["period_end"])) for r in deduped_quarterly]
    display_labels = quarter_labels_full[-DISPLAY_QUARTERS:]

    # Per-cell source provenance for the P3.3 chips — one query for every
    # (line_item, period) pair; best-effort ({} on legacy DBs).
    cell_prov = load_financial_cell_provenance(
        ticker, sorted(set(_COL_TO_FACT_LINE_ITEM.values())), repo_root
    )

    line_items: list[QuarterlyLineItem] = []
    for col, name, unit, digits in _LINE_ITEM_SPECS:
        full_series = [_to_display(r.get(col), col) for r in deduped_quarterly]
        if all(v is None for v in full_series):
            continue
        prov_by_period = cell_prov.get(_COL_TO_FACT_LINE_ITEM.get(col, col), {})
        sources_full = [
            to_cell_source(prov_by_period.get(_period_date_key(r.get("period_end"))))
            for r in deduped_quarterly
        ]
        if issue_signals:
            fact_item = _COL_TO_FACT_LINE_ITEM.get(col, col)
            for i, r in enumerate(deduped_quarterly):
                src = sources_full[i]
                if src is None:
                    continue
                try:
                    raw_v = float(str(r.get(col)))
                except (TypeError, ValueError):
                    raw_v = 0.0
                strings = display_issues_for_fact(
                    issue_signals,
                    ticker=ticker.upper(),
                    item=fact_item,
                    period_end=_period_date_key(r.get("period_end")),
                    value=raw_v,
                    displayed_tier=src.source,
                )
                if strings:
                    # Append, not overwrite — preserve any override qualify note (P6).
                    src.issues = [*src.issues, *strings]
        line_items.append(
            QuarterlyLineItem(
                line_item=name,
                unit=unit,
                digits=digits,
                quarters=display_labels,
                values=full_series[-DISPLAY_QUARTERS:],
                growth=compute_growth(full_series),
                levels_full=full_series,
                sources_full=sources_full,
            )
        )

    annual_years, annual_items = _build_annual(annual_rows)

    quarterly_ok = bool(line_items)
    annual_ok = bool(annual_items)
    status = (
        SectionStatus.OK
        if (quarterly_ok and annual_ok)
        else (SectionStatus.PARTIAL if (quarterly_ok or annual_ok) else SectionStatus.MISSING_DATA)
    )

    requested_priorities = _read_chart_priorities_request(ticker, repo_root)
    resolved_priorities, kpi_series, annual_kpi_series, annual_kpi_years = _resolve_priorities(
        requested_priorities,
        line_items,
        ticker,
        repo_root,
        display_labels,
        quarter_labels_full,
        conn=db_conn,
    )
    _stamp_observation_versions(
        db_conn,
        line_items=line_items,
        annual_items=annual_items,
        kpi_series=kpi_series,
        annual_kpi_series=annual_kpi_series,
    )
    if conn is None:
        db_conn.close()

    currency = "USD"
    if deduped_quarterly:
        raw_ccy = deduped_quarterly[-1].get("currency")
        if isinstance(raw_ccy, str) and raw_ccy.strip():
            currency = raw_ccy.strip().upper()

    return FinancialsSection(
        status=status,
        quarter_labels=display_labels,
        line_items=line_items,
        annual_years=annual_years,
        annual_line_items=annual_items,
        chart_priorities=resolved_priorities,
        kpi_chart_series=kpi_series,
        annual_kpi_chart_series=annual_kpi_series,
        annual_kpi_years=annual_kpi_years,
        quarter_labels_full=quarter_labels_full,
        currency=currency,
    )


def _stamp_observation_versions(
    conn: sqlite3.Connection,
    *,
    line_items: list[QuarterlyLineItem],
    annual_items: list[AnnualLineItem],
    kpi_series: list[KpiSeries],
    annual_kpi_series: list[AnnualKpiSeries],
) -> None:
    """Stamp every chip's cited document with its Observation Version.

    One batched resolve for the whole section (VAL-CROSS-002(3)): a chip whose
    document carries an Observation Version links the full provenance chain;
    a chip without one renders the explicit "not recorded" degraded state in
    its popover — never a silently partial chain. No-op without chips or
    without the evidence plane.
    """
    chips = [
        src
        for group in (line_items, annual_items, kpi_series, annual_kpi_series)
        for row in group
        for src in row.sources_full
        if src is not None
    ]
    if not chips:
        return
    versions = document_version_ids(conn, [chip.doc_id for chip in chips])
    for chip in chips:
        if chip.doc_id is not None and chip.document_version_id is None:
            chip.document_version_id = versions.get(chip.doc_id)


def _period_date_key(raw: object) -> str:
    """Coerce a metrics-view period_end to the YYYY-MM-DD key the cell
    provenance map is keyed by. Tolerant: non-string/short values key to ''
    (which simply never matches a provenance entry)."""
    s = str(raw) if raw is not None else ""
    return s[:10]


def to_cell_source(prov: dict[str, object] | None) -> CellSource | None:
    """Map one load_financial_cell_provenance payload to the report model."""
    if prov is None:
        return None

    def _opt(key: str) -> str | None:
        v = prov.get(key)
        return str(v) if v is not None else None

    source = prov.get("source")
    raw_doc_id = prov.get("source_doc_id")
    raw_fact_id = prov.get("fact_id")
    raw_confidence = prov.get("confidence")
    raw_issues = prov.get("issues")
    issues = (
        [str(x) for x in cast("list[object]", raw_issues)] if isinstance(raw_issues, list) else []
    )
    return CellSource(
        source=str(source) if source is not None else "unknown",
        fetched_at=_opt("fetched_at"),
        source_url=_opt("source_url"),
        doc_type=_opt("doc_type"),
        accession_number=_opt("accession_number"),
        filing_date=_opt("filing_date"),
        locator=_opt("locator"),
        doc_id=int(raw_doc_id) if isinstance(raw_doc_id, int) else None,
        fact_id=int(raw_fact_id) if isinstance(raw_fact_id, int) else None,
        confidence=float(raw_confidence) if isinstance(raw_confidence, (int, float)) else None,
        extracted_by=_opt("extracted_by"),
        override=_opt("override"),
        issues=issues,
    )


# Retain the private spelling used by older confidence-scoring consumers.
_to_cell_source = to_cell_source


def _read_chart_priorities_request(ticker: str, repo_root: Path) -> list[str]:
    holdings_path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    try:
        raw_text = read_captured_text(repo_root, holdings_path)
    except FileNotFoundError:
        return list(_DEFAULT_CHART_PRIORITIES)
    try:
        decoded: object = json.loads(raw_text)
    except (json.JSONDecodeError, OSError):
        return list(_DEFAULT_CHART_PRIORITIES)
    if not isinstance(decoded, dict):
        return list(_DEFAULT_CHART_PRIORITIES)
    holdings = cast(dict[str, object], decoded)
    raw: object = holdings.get("chart_priorities") or []
    if isinstance(raw, list):
        values = cast(list[object], raw)
        cleaned = [x for x in values if isinstance(x, str)]
        if cleaned:
            return cleaned
    return list(_DEFAULT_CHART_PRIORITIES)


def _resolve_priorities(
    requested: list[str],
    line_items: list[QuarterlyLineItem],
    ticker: str,
    repo_root: Path,
    quarter_labels: list[str],
    quarter_labels_full: list[str],
    *,
    conn: sqlite3.Connection | None = None,
) -> tuple[list[str], list[KpiSeries], list[AnnualKpiSeries], list[int]]:
    """Resolve each requested name against line_items → kpi_facts → drop.

    Each name is tried first against the financials line_items (case-insensitive).
    If not found, queried against kpi_facts. If neither has data, dropped silently.
    Preserves the holdings-JSON ordering.

    Cadence-aware: a name whose definition is annual-cadence
    (``kpi_definitions.reporting_cadence='annual'``) resolves to an
    ``AnnualKpiSeries`` on the fiscal-year axis instead of the gappy quarterly
    series — kept out of ``kpi_chart_series`` so the quarterly heatmap never shows
    a mostly-empty annual metric. Returns
    ``(resolved_names, quarterly_series, annual_series, annual_years)``.
    """
    li_map = {li.line_item.casefold(): li.line_item for li in line_items}
    resolved: list[str] = []
    kpi_series: list[KpiSeries] = []
    annual_raw: list[tuple[str, str, dict[int, float], dict[int, CellSource]]] = []

    db_conn = open_repo_db(repo_root, conn)

    try:
        # Phase 1 — classify each requested name. Financial line-item names
        # resolve against the §2 line items with no DB access; KPI names are
        # verified through the reviewed reference machinery (per-name
        # disposition-ledger lookup and, for as-authored references, the
        # exact-name fallback — unchanged).
        financial_by_index: dict[int, str] = {}
        verified_names: list[tuple[int, str]] = []
        for index, name in enumerate(requested):
            financial_priority = canonical_financial_chart_priority(name)
            if financial_priority is not None:
                resolved_name = li_map.get(financial_priority.display_name.casefold())
                if resolved_name is not None:
                    financial_by_index[index] = resolved_name
                continue
            if db_conn is None:
                continue
            reference = report_kpi_reference_at(
                repo_root,
                ticker=ticker,
                json_pointer=f"/chart_priorities/{index}",
            )
            verified = (
                None
                if reference is None
                else verified_report_kpi_reference_definition(
                    db_conn,
                    repo_root=repo_root,
                    user_id=DEFAULT_USER_ID,
                    reference=reference,
                )
            )
            if verified is not None:
                verified_names.append((index, verified.definition_name))

        # Phase 2 — cadence split + one batched resolver scan per cadence
        # family. The canonical observation-count scan is the heavy resolver
        # lookup; the shared batch seam evaluates the whole report's labels in
        # one pass per cadence family where the per-KPI readers used to pay it
        # once per name. The per-build context (built exactly once) hoists the
        # constant semantic-SQL triple and schema guards every per-KPI query
        # re-derived.
        context: _KpiReadContext | None = None
        annual_indexes: set[int] = set()
        kpi_results: dict[int, KpiSeries] = {}
        annual_results: dict[int, tuple[str, str, dict[int, float], dict[int, CellSource]]] = {}
        if db_conn is not None and verified_names:
            context = _KpiReadContext(db_conn)
            # Register the per-build context so every per-KPI reader call below
            # (unchanged public signatures) shares it; popped in finally so it
            # never spans builds.
            _BUILD_CONTEXTS[db_conn] = context
            cadence_by_index: dict[int, str] = {}
            for index, verified_name in verified_names:
                cadence_by_index[index] = context.reporting_cadence(db_conn, ticker, verified_name)
            annual_indexes = {
                index for index, cadence in cadence_by_index.items() if cadence == "annual"
            }
            for names, period_types in (
                (
                    [name for index, name in verified_names if index not in annual_indexes],
                    QUARTERLY_FACT_PERIOD_TYPES,
                ),
                (
                    [name for index, name in verified_names if index in annual_indexes],
                    ANNUAL_FACT_PERIOD_TYPES,
                ),
            ):
                if not names:
                    continue
                context.remember_resolved_names(
                    ticker,
                    names,
                    period_types,
                    resolve_kpi_definition_names(db_conn, ticker, names, period_types=period_types),
                )

            # Phase 3 — per KPI, exactly one merged series-plus-sources query.
            for index, verified_name in verified_names:
                if index in annual_indexes:
                    annual = _annual_kpi_raw_for(db_conn, ticker, verified_name)
                    if annual is not None:
                        annual_results[index] = annual
                    continue
                series = _kpi_series_for(
                    db_conn,
                    ticker,
                    verified_name,
                    quarter_labels,
                    quarter_labels_full,
                )
                if series is not None:
                    kpi_results[index] = series

        # Emit in requested order — financial line items and KPI series
        # interleave exactly as the single-pass reader always did.
        for index in range(len(requested)):
            if index in financial_by_index:
                resolved.append(financial_by_index[index])
            elif index in kpi_results:
                series = kpi_results[index]
                kpi_series.append(series)
                resolved.append(series.name)
            elif index in annual_results:
                annual = annual_results[index]
                annual_raw.append(annual)
                resolved.append(annual[0])
    finally:
        if db_conn is not None:
            _BUILD_CONTEXTS.pop(db_conn, None)
            if conn is None:
                db_conn.close()

    annual_series, annual_years = _align_annual_kpis(annual_raw)
    return resolved, kpi_series, annual_series, annual_years


def _kpi_cell_sources_for(
    conn: sqlite3.Connection,
    ticker: str,
    resolved_name: str,
    period_types: tuple[str, ...],
    *,
    context: _KpiReadContext | None = None,
) -> dict[str, CellSource]:
    """Per-period provenance for one resolved KPI definition, keyed by the
    period_end ISO date (YYYY-MM-DD) — the kpi_facts twin of
    ``load_financial_cell_provenance`` (KPI chip universality, S2 PR2).

    Row pick matches ``_kpi_series_for`` / ``_annual_kpi_raw_for`` exactly:
    source-quality tier first, issuer document over a same-tier generic vendor
    row, then fact id.  Thus the chip always describes the number the series
    displays. Best-effort: ``{}`` on legacy DBs
    without a documents table or the 0054/0075 provenance columns — the
    series then renders without chips, never breaks.

    The report build itself now derives chips from the one merged
    series-plus-sources query per KPI (:func:`_KpiReadContext.merged_series_rows`
    + :func:`_cell_sources_from_rows`); this standalone reader remains the
    one-off consumer seam with the identical row pick and construction.
    """
    ctx = _active_kpi_read_context(conn, context)
    definition_ids = ctx.matching_definition_ids(conn, ticker, resolved_name)
    if not definition_ids:
        return {}
    try:
        rows = conn.execute(
            ctx.chip_sources_sql(period_types=period_types, definition_ids=definition_ids),
            (ticker.upper(), *definition_ids, *period_types),
        ).fetchall()
    except sqlite3.Error:
        return {}
    return _cell_sources_from_rows(
        conn,
        ctx.unresolved_issues,
        ticker=ticker,
        resolved_name=resolved_name,
        period_types=period_types,
        rows=rows,
    )


kpi_cell_sources_for = _kpi_cell_sources_for


def _cell_sources_from_rows(
    conn: sqlite3.Connection,
    issue_signals: IssuesByTicker,
    *,
    ticker: str,
    resolved_name: str,
    period_types: tuple[str, ...],
    rows: list[sqlite3.Row],
) -> dict[str, CellSource]:
    """Build the per-period ``CellSource`` map from ranked chip rows.

    Shared by the standalone chip reader (:func:`_kpi_cell_sources_for`) and
    the merged series-plus-sources path so both construct chips identically:
    tier label, document provenance, unresolved-issue annotations, and the
    qualify-override note.
    """
    out: dict[str, CellSource] = {}
    for r in rows:
        raw_conf = r["confidence"]
        raw_doc_id = r["source_doc_id"]
        period_iso = str(r["period_end"])[:10]
        tier = str(r["source"]) if r["source"] is not None else "unknown"
        issues: list[str] = []
        if issue_signals:
            try:
                fact_value = float(r["value"])
            except (TypeError, ValueError):
                fact_value = 0.0
            issues = display_issues_for_fact(
                issue_signals,
                ticker=ticker.upper(),
                item=resolved_name,
                period_end=period_iso,
                value=fact_value,
                displayed_tier=tier,
            )
        raw_fact_id = r["fact_id"]
        out[period_iso] = CellSource(
            source=tier,
            fetched_at=str(r["fetched_at"]) if r["fetched_at"] is not None else None,
            source_url=str(r["source_url"]) if r["source_url"] is not None else None,
            doc_type=str(r["doc_type"]) if r["doc_type"] is not None else None,
            accession_number=(
                str(r["accession_number"]) if r["accession_number"] is not None else None
            ),
            filing_date=str(r["filing_date"]) if r["filing_date"] is not None else None,
            locator=str(r["locator"]) if r["locator"] is not None else None,
            doc_id=int(raw_doc_id) if raw_doc_id is not None else None,
            fact_id=int(raw_fact_id) if raw_fact_id is not None else None,
            fact_table="kpi_facts",
            confidence=float(raw_conf) if raw_conf is not None else None,
            extracted_by=str(r["extracted_by"]) if r["extracted_by"] is not None else None,
            computed_from=str(r["computed_from"]) if r["computed_from"] is not None else None,
            issues=issues,
        )
    # A qualify action does not replace the admitted fact, so preserve its
    # warning on the source chip. Replace/drop actions are rejected above by
    # semantic_series_identity_sql until a reviewed fact supersedes them.
    ov_map = chip_override_map(
        conn,
        ticker=ticker,
        fact_kind=OVERRIDE_KPI,
        fact_key=resolved_name,
        period_types=period_types,
    )
    for period_iso, ov in ov_map.items():
        cell = out.get(period_iso)
        if cell is not None and ov.action == OverrideAction.QUALIFY.value:
            out[period_iso] = cell.model_copy(update={"issues": [*cell.issues, qualify_note(ov)]})
    return out


def _kpi_facts_columns(conn: sqlite3.Connection) -> set[str]:
    """kpi_facts column names — guards the optional audit/lineage selects
    (extracted_by: 0054; computed_from: 0087) on legacy fixture DBs."""
    try:
        return {str(row[1]) for row in conn.execute("PRAGMA table_info(kpi_facts)")}
    except sqlite3.Error:
        return set()


def _kpi_source_order_sql(conn: sqlite3.Connection) -> str:
    """Report-only canonical KPI winner order.

    It preserves the global quality-tier contract, while using issuer origin as
    a deterministic tie-break over a same-tier FMP row.  The latter is needed
    because both IR-document and FMP-normalized facts historically share the
    ``fmp_normalized`` tier.
    """
    if not has_table(conn, "documents"):
        return "NULL"
    return reader_source_order_sql(conn)


def _kpi_source_join_sql(conn: sqlite3.Connection) -> str:
    """Document join paired with :func:`_kpi_source_order_sql`."""
    return reader_tier_join_sql(conn, fact_alias="kf")


class _KpiReadContext:
    """Constant-per-build KPI read context (audit performance-serving F6).

    Every chart-priority KPI used to re-derive the same schema-guarded SQL
    fragments per call: the semantic-SQL triple (``canonical_fact_relation`` +
    ``semantic_admission_sql`` + ``semantic_series_identity_sql``, each carrying
    its sqlite_master/PRAGMA probes and the blocked-override DISTINCT scan),
    the ``kpi_facts`` column guards, the report source order/join fragments,
    and the unresolved-issue signals. One context is built per financials
    section build (and per direct reader call that is not handed one), so a
    build with N KPIs pays those probes once instead of per KPI, and the
    memoized resolver lookups let the merged series/sources query path never
    re-run a name-resolution or definition-id scan it has already answered.

    Pure read-path hoist: every fragment is a function of the read-only
    connection alone, so computing it once per build cannot change any pick.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.resolver_memo = KpiResolverReadMemo()
        self.fact_relation = canonical_fact_relation(conn, "kpi_facts").sql
        self.semantic_join, self.semantic_where = semantic_admission_sql(conn, fail_closed=True)
        self.semantic_identity = semantic_series_identity_sql(
            conn, fact_relation=self.fact_relation, memo=self.resolver_memo
        )
        self.kpi_facts_columns = _kpi_facts_columns(conn)
        self.source_order_sql = _kpi_source_order_sql(conn)
        self.source_join_sql = _kpi_source_join_sql(conn)
        self.documents_available = has_table(conn, "documents")
        self.unresolved_issues: IssuesByTicker = load_unresolved_issues(conn)
        self._definition_ids: dict[tuple[str, str], tuple[int, ...]] = {}
        self._resolved_names: dict[tuple[str, str, tuple[str, ...]], str | None] = {}

    def matching_definition_ids(
        self, conn: sqlite3.Connection, ticker: str, resolved_name: str
    ) -> tuple[int, ...]:
        """Memoized :func:`matching_kpi_definition_ids` per (ticker, name)."""
        key = (ticker.upper(), resolved_name)
        if key not in self._definition_ids:
            self._definition_ids[key] = matching_kpi_definition_ids(
                conn, ticker, resolved_name, memo=self.resolver_memo
            )
        return self._definition_ids[key]

    def reporting_cadence(self, conn: sqlite3.Connection, ticker: str, name: str) -> str:
        """:func:`reporting_cadence_for` — unchanged call signature (monkeypatch seam)."""
        return reporting_cadence_for(conn, ticker, name)

    def resolve_name(
        self,
        conn: sqlite3.Connection,
        ticker: str,
        kpi_name: str,
        period_types: tuple[str, ...],
    ) -> str | None:
        """Memoized canonical name resolution per (ticker, name, cadence family).

        Delegates to the shared batch seam one label at a time — identical
        ranking contract — so a call that has already been answered (e.g. by the
        per-cadence batch in :func:`_resolve_priorities`) is free.
        """
        key = (ticker.upper(), kpi_name, period_types)
        if key not in self._resolved_names:
            resolved = resolve_kpi_definition_names(
                conn, ticker, (kpi_name,), period_types=period_types
            )
            self._resolved_names[key] = resolved.get(kpi_name)
        return self._resolved_names[key]

    def remember_resolved_names(
        self,
        ticker: str,
        kpi_names: list[str],
        period_types: tuple[str, ...],
        resolved: dict[str, str | None],
    ) -> None:
        """Seed the memo from a batch resolution pass (same ranking contract)."""
        for name in kpi_names:
            self._resolved_names[(ticker.upper(), name, period_types)] = resolved.get(name)

    def extracted_by_select(self) -> str:
        return (
            "kf.extracted_by"
            if "extracted_by" in self.kpi_facts_columns
            else "NULL AS extracted_by"
        )

    def computed_from_select(self) -> str:
        return (
            "kf.computed_from"
            if "computed_from" in self.kpi_facts_columns
            else "NULL AS computed_from"
        )

    def merged_series_with_chips_sql(
        self, *, period_types: tuple[str, ...], definition_ids: tuple[int, ...]
    ) -> str:
        """One identity-anchored query per KPI: the ranked series population
        with the chip columns riding along.

        ``rn_series`` ranks the full LEFT-JOIN population exactly as the
        series-only query always did. ``rn_chip`` ranks the document-bearing
        partition — the same population the standalone chip query's INNER JOIN
        selected — so a chip still describes the best document-backed fact for
        the period even when the displayed series winner has no document.
        """
        placeholders = ",".join("?" * len(period_types))
        definition_placeholders = ",".join("?" * len(definition_ids))
        return f"""
            WITH ranked AS (
                SELECT kf.period_end, kf.value, kf.unit, kd.name,
                       kf.locator, kf.confidence, kf.id AS fact_id,
                       kf.source_doc_id,
                       {self.extracted_by_select()},
                       {self.computed_from_select()},
                       d.fetched_at, d.source_url, d.doc_type,
                       d.accession_number, d.filing_date,
                       d.source_quality_tier AS source,
                       (d.id IS NOT NULL) AS doc_present,
                       ROW_NUMBER() OVER (
                           PARTITION BY kf.period_end, kf.fiscal_period_type
                           ORDER BY {self.source_order_sql}, kf.id DESC
                       ) AS rn_series,
                       ROW_NUMBER() OVER (
                           PARTITION BY kf.period_end, kf.fiscal_period_type, (d.id IS NOT NULL)
                           ORDER BY {self.source_order_sql}, kf.id DESC
                       ) AS rn_chip
                FROM {self.fact_relation} kf
                JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id
                {self.source_join_sql}
                {self.semantic_join}
                WHERE kf.ticker = ?
                  AND {self.semantic_where} AND {self.semantic_identity}
                  AND kf.kpi_definition_id IN ({definition_placeholders})
                  AND kf.fiscal_period_type IN ({placeholders})
            )
            SELECT * FROM ranked
            WHERE rn_series = 1 OR (doc_present AND rn_chip = 1)
            ORDER BY period_end ASC
            """  # nosec B608 -- closed canonical relation/semantic/order clauses; values remain bound

    def series_only_sql(
        self, *, period_types: tuple[str, ...], definition_ids: tuple[int, ...]
    ) -> str:
        """The pre-merge series query — the fallback shape when the merged
        query's chip columns are not executable on a legacy schema (chips then
        fail closed to ``{}`` exactly as the standalone sources reader always
        did) and the exact shape used when the documents table is absent."""
        placeholders = ",".join("?" * len(period_types))
        definition_placeholders = ",".join("?" * len(definition_ids))
        return f"""
            WITH ranked AS (
                SELECT kf.period_end, kf.value, kf.unit, kd.name,
                       ROW_NUMBER() OVER (
                           PARTITION BY kf.period_end, kf.fiscal_period_type
                           ORDER BY {self.source_order_sql}, kf.id DESC
                       ) AS rn
                FROM {self.fact_relation} kf
                JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id
                {self.source_join_sql}
                {self.semantic_join}
                WHERE kf.ticker = ?
                  AND {self.semantic_where} AND {self.semantic_identity}
                  AND kf.kpi_definition_id IN ({definition_placeholders})
                  AND kf.fiscal_period_type IN ({placeholders})
            )
            SELECT period_end, value, unit, name FROM ranked
            WHERE rn = 1 ORDER BY period_end ASC
            """  # nosec B608 -- closed canonical relation/semantic/order clauses; values remain bound

    def chip_sources_sql(
        self, *, period_types: tuple[str, ...], definition_ids: tuple[int, ...]
    ) -> str:
        """The standalone chip query (INNER JOIN documents) — the direct twin
        :func:`_kpi_cell_sources_for` still serves for one-off consumers."""
        placeholders = ",".join("?" * len(period_types))
        definition_placeholders = ",".join("?" * len(definition_ids))
        return f"""
            WITH ranked AS (
                SELECT kf.period_end, kf.locator, kf.confidence,
                       kf.id AS fact_id,
                       kf.source_doc_id, kf.value,
                       {self.extracted_by_select()},
                       {self.computed_from_select()},
                       d.fetched_at, d.source_url, d.doc_type,
                       d.accession_number, d.filing_date,
                       d.source_quality_tier AS source,
                       ROW_NUMBER() OVER (
                           PARTITION BY kf.period_end, kf.fiscal_period_type
                           ORDER BY {self.source_order_sql}, kf.id DESC
                       ) AS rn
                FROM {self.fact_relation} kf
                JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id
                JOIN documents d ON d.id = kf.source_doc_id
                {self.semantic_join}
                WHERE kf.ticker = ?
                  AND {self.semantic_where} AND {self.semantic_identity}
                  AND kf.kpi_definition_id IN ({definition_placeholders})
                  AND kf.fiscal_period_type IN ({placeholders})
            )
            SELECT * FROM ranked WHERE rn = 1
            """  # nosec B608 -- closed canonical relation/semantic/order clauses; values remain bound

    def merged_series_rows(
        self,
        conn: sqlite3.Connection,
        *,
        ticker: str,
        period_types: tuple[str, ...],
        definition_ids: tuple[int, ...],
    ) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
        """Execute the merged query once and split it into
        ``(series_rows, chip_rows)``.

        On a legacy schema where the chip columns make the merged query
        unexecutable, re-run the series-only shape and return no chip rows —
        reproducing the pre-merge contract where the series worked and the
        chip reader failed closed to ``{}`` best-effort. A series-side failure
        propagates unchanged (the fallback raises the same error).
        """
        if not self.documents_available:
            rows = conn.execute(
                self.series_only_sql(period_types=period_types, definition_ids=definition_ids),
                (ticker.upper(), *definition_ids, *period_types),
            ).fetchall()
            return list(rows), []
        try:
            rows = conn.execute(
                self.merged_series_with_chips_sql(
                    period_types=period_types, definition_ids=definition_ids
                ),
                (ticker.upper(), *definition_ids, *period_types),
            ).fetchall()
        except sqlite3.Error:
            rows = conn.execute(
                self.series_only_sql(period_types=period_types, definition_ids=definition_ids),
                (ticker.upper(), *definition_ids, *period_types),
            ).fetchall()
            return list(rows), []
        series_rows = [row for row in rows if row["rn_series"] == 1]
        chip_rows = [row for row in rows if row["doc_present"] and row["rn_chip"] == 1]
        return series_rows, chip_rows


_BUILD_CONTEXTS: dict[sqlite3.Connection, _KpiReadContext] = {}


def _active_kpi_read_context(
    conn: sqlite3.Connection, context: _KpiReadContext | None = None
) -> _KpiReadContext:
    """The read context a per-KPI reader call should use.

    An explicit ``context`` wins (internal passes between readers). Otherwise a
    context registered for this connection by an in-flight
    :func:`_resolve_priorities` build is shared, so every per-KPI query in the
    build reuses the one hoisted semantic-SQL triple, schema guards, and
    memoized resolver lookups. A reader called outside any build (the one-off
    consumer seam, and the exact seam ``tests/`` monkeypatch with fixed
    signatures) builds its own throwaway context — the same per-call derivation
    the readers always did. The registering build pops its entry in ``finally``,
    so a context never spans builds: writes between builds (e.g. fact
    overrides) are always re-derived, and the registry only ever holds the
    in-flight build's own connection reference (``sqlite3.Connection`` is not
    weak-referenceable, so a strong key with disciplined pop is the lifetime
    contract).
    """
    if context is not None:
        return context
    return _BUILD_CONTEXTS.get(conn) or _KpiReadContext(conn)


def _quarter_label_for_period(period_end_iso: str) -> str | None:
    """``YYYY-MM-DD`` → the report's ``"YYYY Qn"`` label; None on bad input."""
    try:
        year = int(period_end_iso[:4])
        month = int(period_end_iso[5:7])
    except ValueError:
        return None
    return f"{year} Q{(month - 1) // 3 + 1}"


def _annual_kpi_raw_for(
    conn: sqlite3.Connection,
    ticker: str,
    kpi_name: str,
    *,
    context: _KpiReadContext | None = None,
) -> tuple[str, str, dict[int, float], dict[int, CellSource]] | None:
    """Pull an annual-cadence KPI's fiscal-year-end series as ``(canonical_name,
    pretty_unit, {fiscal_year: value}, {fiscal_year: CellSource})``, or None
    when nothing resolves.

    Reads only ``FY``/``annual`` rows (``ANNUAL_FACT_PERIOD_TYPES``) so interim
    prints on an otherwise-annual metric never pollute the annual axis; dedupes
    by quality tier, issuer preference, then fact id. ``period_end`` year is the
    axis key. The series and its per-period chips come from ONE identity-
    anchored query per KPI (the merged path); the chip rows are the best
    document-backed fact per period — the same rows the standalone
    :func:`_kpi_cell_sources_for` reader selects.
    """
    ctx = _active_kpi_read_context(conn, context)
    resolved_name = ctx.resolve_name(conn, ticker, kpi_name, ANNUAL_FACT_PERIOD_TYPES)
    if resolved_name is None:
        return None
    definition_ids = ctx.matching_definition_ids(conn, ticker, resolved_name)
    if not definition_ids:
        return None
    series_rows, chip_rows = ctx.merged_series_rows(
        conn,
        ticker=ticker,
        period_types=ANNUAL_FACT_PERIOD_TYPES,
        definition_ids=definition_ids,
    )
    if not series_rows:
        return None
    by_year: dict[int, float] = {}
    canonical_name = kpi_name
    canonical_unit = ""
    for r in series_rows:
        canonical_name = str(r["name"])
        canonical_unit = str(r["unit"] or "")
        period_end = str(r["period_end"])[:10]
        try:
            year = int(period_end[:4])
            by_year[year] = float(str(r["value"]))
        except ValueError:
            continue
    if not by_year:
        return None
    src_by_year = {
        year: src
        for period_iso, src in _cell_sources_from_rows(
            conn,
            ctx.unresolved_issues,
            ticker=ticker,
            resolved_name=resolved_name,
            period_types=ANNUAL_FACT_PERIOD_TYPES,
            rows=chip_rows,
        ).items()
        if (year := _safe_year(period_iso)) is not None
    }
    return canonical_name, _pretty_unit(canonical_unit), by_year, src_by_year


def _safe_year(period_end_iso: str) -> int | None:
    try:
        return int(period_end_iso[:4])
    except ValueError:
        return None


def _align_annual_kpis(
    annual_raw: list[tuple[str, str, dict[int, float], dict[int, CellSource]]],
) -> tuple[list[AnnualKpiSeries], list[int]]:
    """Build a shared fiscal-year axis (union of all series' years, newest
    ``ANNUAL_HISTORY_YEARS``) and align each annual KPI to it. Returns
    ``([], [])`` when there are no annual KPIs."""
    if not annual_raw:
        return [], []
    all_years: set[int] = set()
    for _name, _unit, by_year, _src in annual_raw:
        all_years.update(by_year)
    years = sorted(all_years)[-ANNUAL_HISTORY_YEARS:]
    series = [
        AnnualKpiSeries(
            name=name,
            unit=unit,
            years=years,
            values=[by_year.get(y) for y in years],
            sources_full=[src_by_year.get(y) for y in years],
            confidence_full=[
                src.confidence if (src := src_by_year.get(y)) is not None else None for y in years
            ],
        )
        for name, unit, by_year, src_by_year in annual_raw
    ]
    return series, years


def _kpi_series_for(
    conn: sqlite3.Connection,
    ticker: str,
    kpi_name: str,
    quarter_labels: list[str],
    quarter_labels_full: list[str],
    *,
    context: _KpiReadContext | None = None,
) -> KpiSeries | None:
    """Pull a kpi_facts series, aligned to both display + full-history labels.

    The series and its per-period chips come from ONE identity-anchored query
    per KPI: the merged (:class:`_KpiReadContext`) query ranks the full series
    population (``rn_series``) and the document-bearing chip partition
    (``rn_chip``) in a single pass, replacing the former series query plus a
    second, separately ranked sources query for the same definition. Row pick
    and chip provenance are unchanged.
    """
    ctx = _active_kpi_read_context(conn, context)
    resolved_name = ctx.resolve_name(conn, ticker, kpi_name, QUARTERLY_FACT_PERIOD_TYPES)
    if resolved_name is None:
        return None
    definition_ids = ctx.matching_definition_ids(conn, ticker, resolved_name)
    if not definition_ids:
        return None
    series_rows, chip_rows = ctx.merged_series_rows(
        conn,
        ticker=ticker,
        period_types=QUARTERLY_FACT_PERIOD_TYPES,
        definition_ids=definition_ids,
    )
    if not series_rows:
        return None
    by_label: dict[str, float] = {}
    canonical_name = kpi_name
    canonical_unit = ""
    for r in series_rows:
        canonical_name = str(r["name"])
        canonical_unit = str(r["unit"] or "")
        period_end = str(r["period_end"])[:10]
        # Period_end YYYY-MM-DD → "YYYY Qn" matching report quarter labels.
        try:
            year = int(period_end[:4])
            month = int(period_end[5:7])
        except ValueError:
            continue
        quarter = (month - 1) // 3 + 1
        label = f"{year} Q{quarter}"
        try:
            by_label[label] = float(str(r["value"]))
        except ValueError:
            continue
    values = [by_label.get(lbl) for lbl in quarter_labels]
    if all(v is None for v in values):
        return None
    levels_full = [by_label.get(lbl) for lbl in quarter_labels_full]
    # Per-period chips (S2 PR2): map provenance rows onto the same label axis
    # the values use; best-effort ({} on legacy DBs → series without chips).
    src_by_label: dict[str, CellSource] = {}
    for period_iso, src in _cell_sources_from_rows(
        conn,
        ctx.unresolved_issues,
        ticker=ticker,
        resolved_name=resolved_name,
        period_types=QUARTERLY_FACT_PERIOD_TYPES,
        rows=chip_rows,
    ).items():
        label = _quarter_label_for_period(period_iso)
        if label is not None:
            src_by_label[label] = src
    sources_full: list[CellSource | None] = [src_by_label.get(lbl) for lbl in quarter_labels_full]
    return KpiSeries(
        name=canonical_name,
        unit=_pretty_unit(canonical_unit),
        quarters=quarter_labels,
        values=values,
        levels_full=levels_full,
        sources_full=sources_full if any(s is not None for s in sources_full) else [],
        confidence_full=(
            [s.confidence if s is not None else None for s in sources_full]
            if any(s is not None for s in sources_full)
            else []
        ),
    )


kpi_series_for = _kpi_series_for


def _pretty_unit(raw_unit: str) -> str:
    if raw_unit == "percent":
        return "%"
    if raw_unit == "actual":
        return ""
    return raw_unit


# ---------------------------------------------------------------------------
# Quarterly path
# ---------------------------------------------------------------------------

# The line-item pivot §3 renders, named exactly as the old ``metrics`` view
# named its columns (so every downstream consumer — _LINE_ITEM_SPECS, the
# currency pluck, the per-cell provenance keys — is unchanged). ``capex``
# aliases the ``capital_expenditure`` line_item, matching the view. Each
# ``MAX(CASE …)`` selects the single non-null value of the one rn=1 winner per
# line_item in the group (it is a pick-the-non-null, NOT a numeric/string max).
_FACT_PIVOT = """
    MAX(period_end) AS period_end,
    MAX(currency) AS currency,
    MAX(fiscal_period_type) AS fiscal_period_type,
    MAX(CASE WHEN line_item = 'revenue' THEN value END) AS revenue,
    MAX(CASE WHEN line_item = 'gross_profit' THEN value END) AS gross_profit,
    MAX(CASE WHEN line_item = 'operating_income' THEN value END) AS operating_income,
    MAX(CASE WHEN line_item = 'net_income' THEN value END) AS net_income,
    MAX(CASE WHEN line_item = 'eps_diluted' THEN value END) AS eps_diluted,
    MAX(CASE WHEN line_item = 'operating_cash_flow' THEN value END) AS operating_cash_flow,
    MAX(CASE WHEN line_item = 'free_cash_flow' THEN value END) AS free_cash_flow,
    MAX(CASE WHEN line_item = 'capital_expenditure' THEN value END) AS capex
""".strip()

# Quarterly facts keyed by CALENDAR QUARTER derived from the actual period_end
# date — deliberately NOT the ``metrics`` view. The view groups by
# (fiscal_year=substr(period_end,1,4), fiscal_period_type) and stamps each
# group with MAX(period_end) over all ~35 line items. For off-calendar
# fiscal-year reporters FMP and SEC disagree on the fiscal-quarter LABEL, so a
# single mislabeled fact (e.g. AMAT's Q4-ending net_income filed under 'Q3' in
# SEC companyfacts) drags the whole group's period_end — and, because each
# line_item's winner is picked independently, sometimes its VALUES — into the
# wrong quarter. In prod this shifted/collided/dropped quarters for AMAT, MU,
# TOL, VEEV (COST, off by only a few days, was unaffected). Keying identity off
# the real period_end DATE instead makes the wrong fiscal_period_type
# label inert and scopes MAX(period_end) within the quarter. PR #452 took the
# research cockpit off the same view for the same reason. Bonus: filtering
# ticker inside the scan is ~70x faster than the view's ROW_NUMBER() over all
# ~726k facts. Day-level period-end wobble within a quarter (FMP vs SEC ±1 day)
# is folded into the calendar-quarter GROUP BY here, and _dedupe_by_calendar_quarter
# stays downstream as a defensive pass-through.
_QUARTERLY_FACTS_SQL = """
WITH dedup AS (
    SELECT
        ff.period_end AS period_end, ff.currency AS currency,
        ff.fiscal_period_type AS fiscal_period_type,
        ff.line_item AS line_item, ff.value AS value,
        CAST(substr(ff.period_end, 1, 4) AS INTEGER) AS cal_year,
        (CAST(substr(ff.period_end, 6, 2) AS INTEGER) - 1) / 3 + 1 AS cal_quarter,
        ROW_NUMBER() OVER (
            PARTITION BY
                CAST(substr(ff.period_end, 1, 4) AS INTEGER),
                (CAST(substr(ff.period_end, 6, 2) AS INTEGER) - 1) / 3 + 1,
                ff.line_item
            ORDER BY {tier_rank} DESC, ff.id DESC
        ) AS rn
    FROM {fact_relation} ff
    {doc_join}
    WHERE ff.ticker = ? AND ff.fiscal_period_type IN ({marks})
)
SELECT
{pivot}
FROM dedup
WHERE rn = 1
GROUP BY cal_year, cal_quarter
ORDER BY period_end DESC
LIMIT ?
"""


def _load_quarterly(conn: sqlite3.Connection, ticker: str) -> list[dict[str, object]]:
    """One deduped row per calendar quarter from the canonical fact relation.

    See ``_QUARTERLY_FACTS_SQL`` for why this bypasses the ``metrics`` view.
    Over-pulls a generous window; ``_dedupe_by_calendar_quarter`` runs
    downstream as a defensive pass-through (the SQL already returns one row per
    calendar quarter)."""
    placeholders = ",".join("?" * len(QUARTERLY_PERIOD_TYPES))
    cursor = conn.cursor()
    cursor.execute(
        _QUARTERLY_FACTS_SQL.format(
            marks=placeholders,
            pivot=_FACT_PIVOT,
            tier_rank=reader_tier_rank_sql(conn),
            doc_join=reader_tier_join_sql(conn),
            fact_relation=canonical_fact_relation(conn, "financial_facts").sql,
        ),
        (ticker.upper(), *QUARTERLY_PERIOD_TYPES, UNDERLYING_QUARTERS * 4),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    rows.reverse()  # → oldest first
    return rows


def _dedupe_by_calendar_quarter(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Merge rows that share a calendar quarter — first non-null wins per column.

    ``_load_quarterly`` already returns one row per calendar quarter, so this is
    now a defensive pass-through; it remains as a safety net that would still
    coalesce should two rows ever land in the same calendar quarter, preferring
    a 'Qx' fiscal_period_type over 'quarterly'.
    """
    groups: dict[tuple[int, int], list[dict[str, object]]] = {}
    for r in rows:
        key = calendar_quarter_key(r["period_end"])
        groups.setdefault(key, []).append(r)

    merged: list[dict[str, object]] = []
    for _key, group in sorted(groups.items()):
        ordered = sorted(group, key=_priority)
        merged.append(_coalesce_rows(ordered))
    return merged


def dedupe_by_calendar_quarter(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Public cross-surface seam for the canonical quarterly winner rule."""
    return _dedupe_by_calendar_quarter(rows)


def _priority(row: dict[str, object]) -> tuple[int, str]:
    """Sort key: prefer Q-bucket, then earlier period_end (deterministic)."""
    fpt = str(row.get("fiscal_period_type") or "")
    is_q = 0 if fpt.startswith("Q") else 1
    return (is_q, str(row.get("period_end") or ""))


def _coalesce_rows(ordered: Iterable[dict[str, object]]) -> dict[str, object]:
    """First non-null value per key wins. Caller must order by preference."""
    out: dict[str, object] = {}
    for r in ordered:
        for k, v in r.items():
            if k not in out or out[k] is None:
                out[k] = v
    return out


def _to_display(value: object, column: str) -> float | None:
    if value is None:
        return None
    try:
        v = float(str(value))
    except (TypeError, ValueError):
        return None
    if column.startswith("eps"):
        return v
    return v / 1_000_000.0


# ---------------------------------------------------------------------------
# Annual path
# ---------------------------------------------------------------------------


# One deduped row per fiscal year, read straight from financial_facts (same
# direct-read rationale as the quarterly path). The annual axis only ever uses
# the period_end YEAR (substr 1..4), so it was never quarter-cross-labeled —
# but reading facts directly keeps §3 entirely off the slow/date-buggy view on
# one code path. fiscal_year = the period_end's calendar year, exactly the
# view's grouping, so values are byte-for-byte unchanged from the view here.
_ANNUAL_FACTS_SQL = """
WITH dedup AS (
    SELECT
        ff.period_end AS period_end, ff.currency AS currency,
        ff.fiscal_period_type AS fiscal_period_type,
        ff.line_item AS line_item, ff.value AS value,
        CAST(substr(ff.period_end, 1, 4) AS INTEGER) AS fiscal_year,
        ROW_NUMBER() OVER (
            PARTITION BY CAST(substr(ff.period_end, 1, 4) AS INTEGER), ff.line_item
            ORDER BY {tier_rank} DESC, ff.id DESC
        ) AS rn
    FROM {fact_relation} ff
    {doc_join}
    WHERE ff.ticker = ? AND ff.fiscal_period_type IN ({marks})
)
SELECT
{pivot}
FROM dedup
WHERE rn = 1
GROUP BY fiscal_year
ORDER BY period_end DESC
LIMIT ?
"""


def _load_annual(conn: sqlite3.Connection, ticker: str) -> list[dict[str, object]]:
    placeholders = ",".join("?" * len(ANNUAL_PERIOD_TYPES))
    cursor = conn.cursor()
    cursor.execute(
        _ANNUAL_FACTS_SQL.format(
            marks=placeholders,
            pivot=_FACT_PIVOT,
            tier_rank=reader_tier_rank_sql(conn),
            doc_join=reader_tier_join_sql(conn),
            fact_relation=canonical_fact_relation(conn, "financial_facts").sql,
        ),
        (ticker.upper(), *ANNUAL_PERIOD_TYPES, ANNUAL_HISTORY_YEARS * 3),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    rows.reverse()
    return rows


def financials_reader_value(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    line_item: str,
    fiscal_period_type: str,
    period_end: str,
) -> float | None:
    """Return the exact value the financials reader renders for one fact coordinate.

    The reader-tier audit uses this public projection boundary rather than
    duplicating the report pivot or reaching into its private loaders.
    """
    fact_to_column = {fact: column for column, fact in _COL_TO_FACT_LINE_ITEM.items()}
    column = fact_to_column.get(line_item)
    if column is None:
        return None
    rows = (
        _load_annual(conn, ticker)
        if fiscal_period_type in ANNUAL_PERIOD_TYPES
        else _load_quarterly(conn, ticker)
    )
    for row in rows:
        if str(row.get("period_end"))[:10] != period_end:
            continue
        raw = row.get(column)
        if raw is None:
            return None
        try:
            return float(str(raw))
        except (TypeError, ValueError):
            return None
    return None


def _build_annual(rows: list[dict[str, object]]) -> tuple[list[int], list[AnnualLineItem]]:
    if not rows:
        return ([], [])
    by_year: dict[int, list[dict[str, object]]] = {}
    for r in rows:
        y = int(str(r["period_end"])[:4])
        by_year.setdefault(y, []).append(r)

    merged_by_year: dict[int, dict[str, object]] = {
        y: _coalesce_rows(sorted(group, key=_priority)) for y, group in by_year.items()
    }
    years = sorted(merged_by_year.keys())[-ANNUAL_HISTORY_YEARS:]

    items: list[AnnualLineItem] = []
    for col, name, unit, digits in _LINE_ITEM_SPECS:
        series = [_to_display(merged_by_year[y].get(col), col) for y in years]
        if all(v is None for v in series):
            continue
        items.append(
            AnnualLineItem(line_item=name, unit=unit, digits=digits, years=years, values=series)
        )
    return (years, items)


def _missing(detail: str, fix: str, extra: str | None = None) -> FinancialsSection:
    full_detail = detail if extra is None else f"{detail}. {extra}"
    return FinancialsSection(
        status=SectionStatus.MISSING_DATA,
        missing=missing(stage="COMPUTE(extract_facts)", fix_command=fix, detail=full_detail),
    )


# Map metrics-view column names to the financial_facts.line_item string they
# came from. The `capex` column aliases `capital_expenditure` in the view —
# every other column shares its line_item name. Used by build_per_metric to
# look up provenance via the tier-aware loader.
_LINE_ITEM_TO_FACT_KEY: dict[str, str] = {
    "revenue": "revenue",
    "gross_profit": "gross_profit",
    "operating_income": "operating_income",
    "net_income": "net_income",
    "eps_diluted": "eps_diluted",
    "operating_cash_flow": "operating_cash_flow",
    "free_cash_flow": "free_cash_flow",
    "capex": "capital_expenditure",
}


def build_per_metric_legacy_shadow(
    ticker: str,
    repo_root: Path,
    *,
    conn: sqlite3.Connection | None = None,
) -> dict[str, dict[str, object]]:
    """Per-line-item provenance for the §3 quarterly facts.

    For each line item the financials table renders, look up the tier-
    winning row for the latest quarter via `load_financial_fact_provenance`
    and surface its (source, fact_id, fetched_at) for the brief audit
    log. Keys are suffixed with `_q_latest` so a future extension to
    annual / per-period provenance can sit alongside without colliding.

    Returns {} when no facts are present (synthetic env, fresh ticker,
    missing migrations) — caller writes a sparse per_metric dict.
    """
    out: dict[str, dict[str, object]] = {}
    for view_col, fact_key in _LINE_ITEM_TO_FACT_KEY.items():
        prov = load_financial_fact_provenance(
            ticker.upper(), fact_key, repo_root=repo_root, conn=conn
        )
        if prov is None:
            continue
        out[f"{view_col}_q_latest"] = prov
    return out


def _canonical_source(
    cell: FinancialTableCell | None, *, ticker: str, as_of: datetime
) -> CellSource | None:
    if cell is None or not cell.available or cell.provenance is None:
        return None
    bundle = cell.provenance
    evidence = bundle.evidence
    if (
        evidence is None
        or cell.canonical_resolution_revision_id is None
        or cell.metric_definition_revision_id is None
    ):
        return None
    return CellSource(
        source=cell.source_kind or "canonical_reported",
        fetched_at=cell.source_retrieved_at.isoformat() if cell.source_retrieved_at else None,
        source_url=cell.source_url,
        doc_id=cell.legacy_document_id,
        locator=evidence.source_locator.model_dump_json(),
        extracted_by=bundle.observation.method_name,
        source_scope_label=cell.source_scope_label,
        document_version_id=evidence.document_version_id,
        canonical_reference=FinancialEvidenceReference(
            ticker=ticker.upper(),
            concept=cell.concept,
            canonical_metric_cell_id=cell.canonical_metric_cell_id,
            observation_id=bundle.observation.observation_id,
            canonical_resolution_revision_id=cell.canonical_resolution_revision_id,
            metric_definition_revision_id=cell.metric_definition_revision_id,
            as_of=as_of,
        ),
    )


def _comparable_window(cells: list[FinancialTableCell | None]) -> bool:
    if not cells or any(
        cell is None or not cell.available or cell.provenance is None for cell in cells
    ):
        return False
    bundles = [
        cell.provenance for cell in cells if cell is not None and cell.provenance is not None
    ]
    identities = {
        (
            bundle.cell.reporting_entity_id,
            bundle.cell.currency,
            bundle.cell.unit_key,
            bundle.cell.accounting_basis,
            bundle.cell.consolidation_scope,
        )
        for bundle in bundles
    }
    metrics = {
        (cell.metric_id, cell.metric_definition_revision_id) for cell in cells if cell is not None
    }
    return len(identities) == len(metrics) == 1 and all(
        newer.cell.period_start is not None
        and (newer.cell.period_start.date() - older.cell.period_end.date()).days == 1
        and older.cell.fiscal_year is not None
        and newer.cell.fiscal_year is not None
        and older.cell.fiscal_period in {"Q1", "Q2", "Q3", "Q4"}
        and newer.cell.fiscal_period in {"Q1", "Q2", "Q3", "Q4"}
        and newer.cell.fiscal_year * 4 + int(newer.cell.fiscal_period[1])
        == older.cell.fiscal_year * 4 + int(older.cell.fiscal_period[1]) + 1
        for older, newer in pairwise(bundles)
    )


def _source_period_ends(cells: list[FinancialTableCell | None]) -> list[date | None]:
    return [
        cell.provenance.cell.period_end.date()
        if cell is not None and cell.available and cell.provenance is not None
        else None
        for cell in cells
    ]


def _annual_windows_supported(cells: list[FinancialTableCell | None]) -> bool:
    """Check inclusive TTM coverage as well as source-end comparison spans."""
    first = cells[0] if cells else None
    if first is None or first.provenance is None or first.provenance.cell.period_start is None:
        return False
    boundaries = [
        first.provenance.cell.period_start.date() - timedelta(days=1),
        *_source_period_ends(cells),
    ]
    return annual_comparison_supported(boundaries, 0, len(cells))


def _canonical_growth(
    cells: list[FinancialTableCell | None], values: list[float | None]
) -> GrowthMetrics:
    growth = compute_growth(values)
    dates = _source_period_ends(cells)
    return GrowthMetrics(
        qoq=growth.qoq if _comparable_window(cells[-2:]) else None,
        yoy=growth.yoy
        if len(cells) >= 5
        and _comparable_window(cells[-5:])
        and annual_comparison_supported(dates, len(cells) - 5, len(cells) - 1)
        else None,
        cagr_1y_ttm=growth.cagr_1y_ttm
        if len(cells) >= 8
        and _comparable_window(cells[-8:])
        and _annual_windows_supported(cells[-8:])
        else None,
        cagr_3y_ttm=growth.cagr_3y_ttm
        if len(cells) >= 16
        and _comparable_window(cells[-16:])
        and _annual_windows_supported(cells[-16:])
        else None,
    )


_LEVEL_GROWTH_FORMULAS: tuple[tuple[int, GrowthFormula], ...] = (
    (1, "cagr_1y_level"),
    (2, "cagr_2y_level"),
    (3, "cagr_3y_level"),
)


def _growth_reference(
    cells: list[FinancialTableCell | None], formula: GrowthFormula, *, ticker: str, as_of: datetime
) -> FinancialGrowthReference | None:
    size = GROWTH_WINDOWS[formula]
    window = cells[-size:]
    if len(window) != size or not _comparable_window(window):
        return None
    if formula.endswith("_level"):
        years = {"cagr_1y_level": 1, "cagr_2y_level": 2, "cagr_3y_level": 3}[formula]
        if not annual_comparison_supported(_source_period_ends(window), 0, size - 1):
            return None
        values = [
            float(cell.display_value)
            if cell is not None and cell.display_value is not None
            else None
            for cell in window
        ]
        value = level_cagr(values[-1], values[0], years)
    else:
        growth = _canonical_growth(
            window,
            [
                float(cell.display_value)
                if cell is not None and cell.display_value is not None
                else None
                for cell in window
            ],
        )
        value = {
            "qoq": growth.qoq,
            "yoy": growth.yoy,
            "cagr_1y_ttm": growth.cagr_1y_ttm,
            "cagr_3y_ttm": growth.cagr_3y_ttm,
        }[formula]
    if value is None:
        return None
    sources = [_canonical_source(cell, ticker=ticker, as_of=as_of) for cell in window]
    refs = tuple(
        source.canonical_reference
        for source in sources
        if source is not None and source.canonical_reference is not None
    )
    if len(refs) != size:
        return None
    return FinancialGrowthReference(formula=formula, inputs=refs)


def read_growth_evidence(
    conn: sqlite3.Connection,
    reference: FinancialGrowthReference,
    *,
    source_context: SourceReadContext | None = None,
) -> tuple[float, tuple[FinancialTableCell, ...]] | None:
    """Reconstruct the original calculation through the report admission owner."""
    first = reference.inputs[0]
    projection = read_financial_table(
        conn, first.ticker, as_of=first.as_of, source_context=source_context
    )
    selected = {cell.canonical_metric_cell_id: cell for cell in projection.cells if cell.available}
    cells: list[FinancialTableCell] = []
    for expected in reference.inputs:
        cell = selected.get(expected.canonical_metric_cell_id)
        if cell is None:
            return None
        source = _canonical_source(cell, ticker=projection.ticker, as_of=projection.as_of)
        if source is None or source.canonical_reference != expected:
            return None
        cells.append(cell)
    window: list[FinancialTableCell | None] = list(cells)
    if (
        _growth_reference(window, reference.formula, ticker=first.ticker, as_of=first.as_of)
        != reference
    ):
        return None
    values = [
        float(cell.display_value) if cell.display_value is not None else None for cell in cells
    ]
    if reference.formula.endswith("_level"):
        years = {"cagr_1y_level": 1, "cagr_2y_level": 2, "cagr_3y_level": 3}[reference.formula]
        value = level_cagr(values[-1], values[0], years)
    else:
        growth = _canonical_growth(window, values)
        value = {
            "qoq": growth.qoq,
            "yoy": growth.yoy,
            "cagr_1y_ttm": growth.cagr_1y_ttm,
            "cagr_3y_ttm": growth.cagr_3y_ttm,
        }[reference.formula]
    return (value, tuple(cells)) if value is not None else None


def build(
    ticker: str,
    repo_root: Path,
    *,
    conn: sqlite3.Connection | None = None,
    as_of: datetime | None = None,
    source_context: SourceReadContext | None = None,
) -> FinancialsSection:
    """Project exact canonical selections; the legacy reader is shadow-only."""
    cutoff = datetime.now(UTC) if as_of is None else as_of
    own_connection = conn is None
    if conn is None:
        try:
            path = require_db_path(configured_db_path(repo_root))
            if path == (repo_root / "data" / "portfolio.db").resolve():
                raise RuntimeError("checkout-local database prohibited")
            conn = connect_sqlite(path, role=SQLiteConnectionRole.READ_ONLY)
            conn.row_factory = sqlite3.Row
        except (OSError, RuntimeError, sqlite3.Error):
            return _missing(
                "configured canonical database unavailable",
                "Configure the canonical database or pass the request-scoped connection",
            )
    original_factory = conn.row_factory
    try:
        conn.row_factory = sqlite3.Row
        projection = read_financial_table(conn, ticker, as_of=cutoff, source_context=source_context)
        quarters = sorted(
            {
                cell.display_coordinate
                for cell in projection.cells
                if cell.cadence == "quarterly" and cell.display_coordinate is not None
            }
        )[-UNDERLYING_QUARTERS:]
        if quarters:
            first_year, first_q = (int(value) for value in quarters[0].split(" Q"))
            last_year, last_q = (int(value) for value in quarters[-1].split(" Q"))
            quarters = [
                f"{index // 4} Q{index % 4 + 1}"
                for index in range(first_year * 4 + first_q - 1, last_year * 4 + last_q)
            ][-UNDERLYING_QUARTERS:]
        years = sorted(
            {
                int(cell.display_coordinate)
                for cell in projection.cells
                if cell.cadence == "annual" and cell.display_coordinate is not None
            }
        )[-ANNUAL_HISTORY_YEARS:]
        available = [cell for cell in projection.cells if cell.available]
        selected = {
            (cell.concept, cell.cadence, cell.display_coordinate): cell for cell in available
        }
        currencies = {
            cell.provenance.cell.currency for cell in available if cell.provenance is not None
        }
        currency = next(iter(currencies)) if len(currencies) == 1 else "unavailable"
        quarterly: list[QuarterlyLineItem] = []
        annual: list[AnnualLineItem] = []
        for col, name, _unit, digits in _LINE_ITEM_SPECS:
            concept = _COL_TO_FACT_LINE_ITEM[col]
            qcells = [selected.get((concept, "quarterly", quarter)) for quarter in quarters]
            acells = [selected.get((concept, "annual", str(year))) for year in years]
            qvalues = [
                float(cell.display_value)
                if cell is not None and cell.display_value is not None
                else None
                for cell in qcells
            ]
            avalues = [
                float(cell.display_value)
                if cell is not None and cell.display_value is not None
                else None
                for cell in acells
            ]
            unit = f"{currency}/share" if col == "eps_diluted" else f"{currency} millions"
            if any(value is not None for value in qvalues):
                quarterly.append(
                    QuarterlyLineItem(
                        line_item=name,
                        unit=unit,
                        digits=digits,
                        quarters=quarters[-DISPLAY_QUARTERS:],
                        values=qvalues[-DISPLAY_QUARTERS:],
                        levels_full=qvalues,
                        sources_full=[
                            _canonical_source(
                                cell, ticker=projection.ticker, as_of=projection.as_of
                            )
                            for cell in qcells
                        ],
                        growth=_canonical_growth(qcells, qvalues),
                        growth_evidence={
                            formula: reference
                            for formula in ("qoq", "yoy", "cagr_1y_ttm", "cagr_3y_ttm")
                            if (
                                reference := _growth_reference(
                                    qcells,
                                    formula,
                                    ticker=projection.ticker,
                                    as_of=projection.as_of,
                                )
                            )
                            is not None
                        },
                        yoy_evidence_full=[
                            _growth_reference(
                                qcells[: index + 1],
                                "yoy",
                                ticker=projection.ticker,
                                as_of=projection.as_of,
                            )
                            for index in range(len(qcells))
                        ],
                        level_cagr_evidence={
                            str(years): reference
                            for years, formula in _LEVEL_GROWTH_FORMULAS
                            if (
                                reference := _growth_reference(
                                    qcells,
                                    formula,
                                    ticker=projection.ticker,
                                    as_of=projection.as_of,
                                )
                            )
                            is not None
                        },
                        comparison_period_ends=_source_period_ends(qcells),
                        comparison_eligible_edges=[
                            False,
                            *(
                                _comparable_window([older, newer])
                                for older, newer in pairwise(qcells)
                            ),
                        ],
                    )
                )
            if any(value is not None for value in avalues):
                annual.append(
                    AnnualLineItem(
                        line_item=name,
                        unit=unit,
                        digits=digits,
                        years=years,
                        values=avalues,
                        sources_full=[
                            _canonical_source(
                                cell, ticker=projection.ticker, as_of=projection.as_of
                            )
                            for cell in acells
                        ],
                    )
                )
        requested = _read_chart_priorities_request(ticker, repo_root)
        priorities, kpis, annual_kpis, kpi_years = _resolve_priorities(
            requested,
            quarterly,
            ticker,
            repo_root,
            quarters[-DISPLAY_QUARTERS:],
            quarters,
            conn=conn,
        )
        reasons = {
            *projection.reason_codes,
            *(reason for cell in projection.cells for reason in cell.reason_codes),
        }
        if not quarterly:
            reasons.add("quarterly_financial_cells_unavailable")
        if not annual:
            reasons.add("annual_financial_cells_unavailable")
        rejected = bool(reasons)
        status = (
            SectionStatus.OK
            if quarterly and annual and not rejected
            else SectionStatus.PARTIAL
            if quarterly or annual
            else SectionStatus.MISSING_DATA
        )
        _stamp_observation_versions(
            conn,
            line_items=quarterly,
            annual_items=annual,
            kpi_series=kpis,
            annual_kpi_series=annual_kpis,
        )
        return FinancialsSection(
            status=status,
            missing=missing(
                "CANONICAL(financial_table)",
                "Review canonical source admission and fiscal coordinates",
                "; ".join(sorted(reasons)),
            )
            if status != SectionStatus.OK
            else None,
            quarter_labels=quarters[-DISPLAY_QUARTERS:],
            quarter_labels_full=quarters,
            line_items=quarterly,
            annual_years=years,
            annual_line_items=annual,
            chart_priorities=priorities,
            kpi_chart_series=kpis,
            annual_kpi_chart_series=annual_kpis,
            annual_kpi_years=kpi_years,
            currency=currency or "unavailable",
            canonical_financial_table=projection,
        )
    finally:
        conn.row_factory = original_factory
        if own_connection:
            conn.close()


def build_per_metric(
    ticker: str, repo_root: Path, *, conn: sqlite3.Connection | None = None
) -> dict[str, dict[str, object]]:
    """Latest-quarter audit entries come from the same exact canonical projection."""
    section = build(ticker, repo_root, conn=conn)
    projection = section.canonical_financial_table
    if projection is None:
        return {}
    out: dict[str, dict[str, object]] = {}
    for cell in sorted(projection.cells, key=lambda item: item.display_coordinate or ""):
        if cell.available and cell.cadence == "quarterly":
            key = "capex" if cell.concept == "capital_expenditure" else cell.concept
            out[f"{key}_q_latest"] = cell.model_dump(mode="json")
    return out
