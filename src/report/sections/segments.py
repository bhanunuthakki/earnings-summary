"""§4 Segments — 12-quarter wide-form per segment × {revenue_by_product,
revenue_by_geography, operating_income}.

Sources:
  - segment_periods + segment_dimensions tables (populated by
    execution/extract_facts.py via src/pipeline/segment_junction_writer.py
    from FMP segment JSON and 10-Q/10-K notes). The reader maps junction
    (dim_type, metric) tuples back to the legacy "revenue_by_product" /
    "revenue_by_geography" / "operating_income" string so downstream grid
    builders keep their existing metric vocabulary.

Per-ticker hygiene rules (segment renames + drops) live in the holdings JSON
under `data_rules` and are applied via report.rules.TickerRules.canonicalize.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Literal

from report.models import (
    SectionStatus,
    SegmentSecondaryExpansion,
    SegmentSeries,
    SegmentsSection,
)
from report.rules import TickerRules, load_rules
from report.sections._common import (
    ANNUAL_PERIOD_TYPES,
    DISPLAY_QUARTERS,
    QUARTERLY_PERIOD_TYPES,
    UNDERLYING_QUARTERS,
    calendar_quarter_key,
    compute_growth,
    missing,
    open_repo_db,
)
from report.sections._ts_signals import (
    format_signals_as_prompt_block,
    load_segment_signals,
)

MetricKey = Literal[
    "revenue_by_product",
    "revenue_by_geography",
    "operating_income",
    "capex_by_segment",
    "headcount_by_segment",
]
_BUCKETS: tuple[MetricKey, ...] = (
    "revenue_by_product",
    "revenue_by_geography",
    "operating_income",
    "capex_by_segment",
    "headcount_by_segment",
)
# Raw `metric` values in segment_facts → SegmentsSection bucket name. The
# extractor emits `metric='capex'` / `'headcount'` (matching the junction's
# wire-level convention) but the renderer model uses `_by_segment` suffixes
# to disambiguate from the cross-tab axes in the junction expansions.
_METRIC_TO_BUCKET: dict[str, MetricKey] = {
    "revenue_by_product": "revenue_by_product",
    "revenue_by_geography": "revenue_by_geography",
    "operating_income": "operating_income",
    "capex": "capex_by_segment",
    "headcount": "headcount_by_segment",
}


def build(
    ticker: str,
    repo_root: Path,
    *,
    conn: sqlite3.Connection | None = None,
) -> SegmentsSection:
    rules = load_rules(ticker, repo_root)
    db_conn = open_repo_db(repo_root, conn)
    if db_conn is None or not _junction_tables_present(db_conn):
        if db_conn is not None and conn is None:
            db_conn.close()
        return SegmentsSection(
            status=SectionStatus.MISSING_DATA,
            missing=missing(
                stage="COMPUTE(extract_facts)",
                fix_command=f"python execution/extract_facts.py --ticker {ticker.upper()} --doc-type fmp_segment_product",
            ),
        )

    raw = _load_segment_rows(db_conn, ticker)

    if not raw:
        if conn is None:
            db_conn.close()
        return SegmentsSection(
            status=SectionStatus.MISSING_DATA,
            missing=missing(
                stage="COMPUTE(extract_facts)",
                fix_command=f"python execution/extract_facts.py --ticker {ticker.upper()}",
                detail="segment_periods has no rows for this ticker",
            ),
        )

    canonical = _apply_rules(raw, rules)
    deduped = _dedupe_by_quarter(canonical)
    quarters_full, display_labels = _quarter_axes(deduped)

    grids = _build_grids(deduped, quarters_full, display_labels)
    definitions, definitions_year = _load_segment_definitions(ticker, repo_root)
    quarter_labels_full = [f"{y} Q{q}" for (y, q) in quarters_full]

    # Junction-driven secondary expansions (e.g. AWS by geography). Empty when
    # the junction tables are absent OR carry only the same single-dim
    # breakdowns that are already represented in `grids`.
    secondary_expansions: list[SegmentSecondaryExpansion] = []
    secondary_expansions = _build_secondary_expansions(
        db_conn,
        ticker,
        quarters_full,
        display_labels,
        primary_segment_names=_primary_segment_names(grids),
    )
    ts_context_md = format_signals_as_prompt_block(
        load_segment_signals(ticker, repo_root=repo_root, conn=db_conn),
        heading="Segment Time-Series Context",
    )
    if conn is None:
        db_conn.close()

    return SegmentsSection(
        status=SectionStatus.OK if any(grids.values()) else SectionStatus.PARTIAL,
        quarter_labels=display_labels,
        revenue_by_product=grids["revenue_by_product"],
        revenue_by_geography=grids["revenue_by_geography"],
        operating_income=grids["operating_income"],
        capex_by_segment=grids["capex_by_segment"],
        headcount_by_segment=grids["headcount_by_segment"],
        segment_definitions=definitions,
        segment_definitions_fiscal_year=definitions_year,
        quarter_labels_full=quarter_labels_full,
        secondary_expansions=secondary_expansions,
        ts_context_md=ts_context_md,
    )


def _load_segment_definitions(ticker: str, repo_root: Path) -> tuple[dict[str, str], int | None]:
    """Read the cached segment definitions file produced by extract_segment_definitions.py."""
    cache_path = repo_root / "data" / "segment_definitions" / f"{ticker.upper()}.json"
    if not cache_path.exists():
        return ({}, None)
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    raw = cached.get("definitions") or {}
    out = {str(k): str(v) for k, v in raw.items() if isinstance(v, str) and v.strip()}
    return (out, cached.get("fiscal_year"))


# ---------------------------------------------------------------------------
# Loading + canonicalization
# ---------------------------------------------------------------------------


def _load_segment_rows(conn: sqlite3.Connection, ticker: str) -> list[dict[str, object]]:
    """Pull a generous window of quarterly segment rows; we'll dedupe in Python.

    Reads from the segment_periods + segment_dimensions junction. The CASE
    expression collapses the junction's (dim_type, metric) pair back to the
    legacy metric vocabulary that `_METRIC_TO_BUCKET` and the grid-building
    helpers downstream still speak — `revenue_by_product`,
    `revenue_by_geography`, `operating_income`, `capex`, `headcount`. Dim
    rows that don't match one of those combos are skipped via the WHERE
    filter so we don't surface stray cross-tab cells in the primary grids
    (those land in the secondary-expansion path).
    """
    placeholders = ",".join("?" * len(QUARTERLY_PERIOD_TYPES))
    cursor = conn.cursor()
    cursor.execute(
        f"""
        SELECT
            sp.period_end AS period_end,
            sd.dim_name AS segment_name,
            CASE
                WHEN sd.dim_type = 'product' AND sd.metric = 'revenue'
                    THEN 'revenue_by_product'
                WHEN sd.dim_type = 'geography' AND sd.metric = 'revenue'
                    THEN 'revenue_by_geography'
                WHEN sd.dim_type = 'business_unit' AND sd.metric = 'operating_income'
                    THEN 'operating_income'
                WHEN sd.dim_type = 'business_unit' AND sd.metric = 'capex'
                    THEN 'capex'
                WHEN sd.dim_type = 'business_unit' AND sd.metric = 'headcount'
                    THEN 'headcount'
            END AS metric,
            sd.value AS value,
            sp.fiscal_period_type AS fiscal_period_type
        FROM segment_periods sp
        JOIN segment_dimensions sd ON sd.period_id = sp.id
        WHERE sp.ticker = ?
          AND sp.fiscal_period_type IN ({placeholders})
          AND (
            (sd.dim_type = 'product' AND sd.metric = 'revenue')
            OR (sd.dim_type = 'geography' AND sd.metric = 'revenue')
            OR (sd.dim_type = 'business_unit' AND sd.metric IN (
                'operating_income', 'capex', 'headcount'
            ))
          )
        """,
        (ticker.upper(), *QUARTERLY_PERIOD_TYPES),
    )
    return [dict(r) for r in cursor.fetchall()]


def _apply_rules(rows: list[dict[str, object]], rules: TickerRules) -> list[dict[str, object]]:
    """Canonicalize segment names + drop rows the rules denylist."""
    out: list[dict[str, object]] = []
    for r in rows:
        canonical = rules.canonicalize_segment(str(r["segment_name"]))
        if canonical is None:
            continue
        out.append({**r, "segment_name": canonical})
    return out


def _dedupe_by_quarter(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Merge rows that share (calendar_quarter, segment, metric).

    Prefer rows with a 'Qx' fiscal_period_type over 'quarterly'; for the
    same bucket prefer the most recent period_end.
    """
    groups: dict[tuple[tuple[int, int], str, str], list[dict[str, object]]] = defaultdict(list)
    for r in rows:
        key = (
            calendar_quarter_key(r["period_end"]),
            str(r["segment_name"]),
            str(r["metric"]),
        )
        groups[key].append(r)

    out: list[dict[str, object]] = []
    for group in groups.values():
        group.sort(
            key=lambda r: (
                0 if str(r.get("fiscal_period_type") or "").startswith("Q") else 1,
                -ord_period(r["period_end"]),
            )
        )
        out.append(group[0])
    return out


def ord_period(p: object) -> int:
    """Encode period_end YYYY-MM-DD as a sortable integer for descending order."""
    s = str(p)[:10].replace("-", "")
    return int(s) if s.isdigit() else 0


# ---------------------------------------------------------------------------
# Grid building
# ---------------------------------------------------------------------------


def _quarter_axes(rows: list[dict[str, object]]) -> tuple[list[tuple[int, int]], list[str]]:
    keys = sorted({calendar_quarter_key(r["period_end"]) for r in rows})
    keys = keys[-UNDERLYING_QUARTERS:]
    display_keys = keys[-DISPLAY_QUARTERS:]
    display_labels = [f"{y} Q{q}" for (y, q) in display_keys]
    return (keys, display_labels)


_OUTLIER_MULTIPLIER = 5.0


def _drop_outliers(values: list[float | None]) -> list[float | None]:
    """Null out points >5× the median magnitude of the trailing 4 quarters.

    Upstream FMP segment files occasionally carry annual or unit-corrupted
    values for a single quarter (e.g., GOOG GCP 2026-Q1 = $462B vs $13-21B
    elsewhere). Without a defense, those points dominate growth columns and
    silently corrupt the DCF base. Conservative cap; flagged in render.
    """
    cleaned: list[float | None] = list(values)
    for i in range(len(cleaned)):
        v = cleaned[i]
        if v is None:
            continue
        baseline = _trailing_median_magnitude(cleaned, i, window=4)
        if baseline is None or baseline == 0:
            continue
        if abs(v) > _OUTLIER_MULTIPLIER * baseline:
            cleaned[i] = None
    return cleaned


def _trailing_median_magnitude(values: list[float | None], idx: int, window: int) -> float | None:
    start = max(0, idx - window)
    sample = [abs(v) for v in values[start:idx] if v is not None]
    if not sample:
        return None
    sample.sort()
    n = len(sample)
    return sample[n // 2] if n % 2 else (sample[n // 2 - 1] + sample[n // 2]) / 2


_MIN_SHARE_OF_BUCKET = 0.01  # segments below 1% of bucket latest-quarter total roll up to "Other"


def _build_grids(
    rows: list[dict[str, object]],
    quarters_full: list[tuple[int, int]],
    display_labels: list[str],
) -> dict[MetricKey, list[SegmentSeries]]:
    by_metric_segment: dict[MetricKey, dict[str, dict[tuple[int, int], float]]] = {
        bucket: defaultdict(dict) for bucket in _BUCKETS
    }
    for r in rows:
        metric = str(r["metric"])
        bucket = _METRIC_TO_BUCKET.get(metric)
        if bucket is None:
            continue
        seg = str(r["segment_name"])
        by_metric_segment[bucket][seg][calendar_quarter_key(r["period_end"])] = float(r["value"])

    grids: dict[MetricKey, list[SegmentSeries]] = {bucket: [] for bucket in _BUCKETS}
    display_quarter_count = len(display_labels)
    for bucket in _BUCKETS:
        prepared: list[tuple[SegmentSeries, float | None]] = []
        bucket_unit = _bucket_display_unit(bucket)
        for segment_name, qmap in by_metric_segment[bucket].items():
            raw_series = [_to_display_units(qmap.get(q), bucket) for q in quarters_full]
            cleaned_series = _drop_outliers(raw_series)
            display_values = cleaned_series[-display_quarter_count:]
            latest = _last_non_null(display_values)
            series = SegmentSeries(
                segment_name=segment_name,
                metric=bucket,
                quarters=display_labels,
                values=display_values,
                growth=compute_growth(cleaned_series),
                unit=bucket_unit,
                levels_full=cleaned_series,
            )
            prepared.append((series, latest))
        grids[bucket] = _sort_and_rollup(prepared, display_labels, bucket)
    return grids


def _bucket_display_unit(bucket: MetricKey) -> str:
    """SegmentSeries.unit value for a bucket — drives matrix-row tooltips."""
    if bucket == "headcount_by_segment":
        return "employees"
    return "USD millions"


def _to_display_units(value: float | None, bucket: MetricKey) -> float | None:
    """Scale a raw segment_facts value to its bucket's display unit.

    Currency buckets (revenue / OI / capex) are stored in raw dollars by the
    extractor and shown in millions; headcount is stored as a count and shown
    verbatim.
    """
    if value is None:
        return None
    if bucket == "headcount_by_segment":
        return value
    return value / 1_000_000.0


def _sort_and_rollup(
    prepared: list[tuple[SegmentSeries, float | None]],
    display_labels: list[str],
    bucket: MetricKey,
) -> list[SegmentSeries]:
    """Sort by latest descending; aggregate sub-1%-of-total segments into 'Other'.

    Magnitude-based for OI too — a -$5B losing segment is more material than a
    $50M one regardless of sign, so both buckets use abs() to filter and sort.
    """
    total_magnitude = sum(abs(latest) for _, latest in prepared if latest is not None)
    if total_magnitude == 0:
        return [s for s, _ in sorted(prepared, key=lambda p: p[0].segment_name)]
    kept: list[tuple[SegmentSeries, float | None]] = []
    rolled: list[SegmentSeries] = []
    for series, latest in prepared:
        share = (abs(latest) / total_magnitude) if latest is not None else 0.0
        if share < _MIN_SHARE_OF_BUCKET:
            rolled.append(series)
        else:
            kept.append((series, latest))
    kept.sort(key=lambda p: abs(p[1] or 0.0), reverse=True)
    out = [s for s, _ in kept]
    if rolled:
        out.append(_aggregate_other(rolled, display_labels, bucket))
    return out


def _aggregate_other(
    rolled: list[SegmentSeries],
    display_labels: list[str],
    bucket: MetricKey,
) -> SegmentSeries:
    n = len(display_labels)
    summed: list[float | None] = []
    for i in range(n):
        col = [s.values[i] for s in rolled if i < len(s.values) and s.values[i] is not None]
        summed.append(sum(col) if col else None)  # type: ignore[arg-type]
    # Aggregate full history too — uses the union of underlying quarter counts.
    full_n = max((len(s.levels_full) for s in rolled), default=0)
    summed_full: list[float | None] = []
    for i in range(full_n):
        col_full = [
            s.levels_full[i]
            for s in rolled
            if i < len(s.levels_full) and s.levels_full[i] is not None
        ]
        summed_full.append(sum(col_full) if col_full else None)  # type: ignore[arg-type]
    label = f"Other ({len(rolled)} segments < 1%)"
    return SegmentSeries(
        segment_name=label,
        metric=bucket,
        quarters=display_labels,
        values=summed,
        growth=compute_growth(summed),
        levels_full=summed_full,
    )


def _last_non_null(values: list[float | None]) -> float | None:
    for v in reversed(values):
        if v is not None:
            return v
    return None


def _optional_millions(value: float | None) -> float | None:
    return None if value is None else value / 1_000_000.0


# ---------------------------------------------------------------------------
# Junction (segment_periods + segment_dimensions) — secondary expansions
# ---------------------------------------------------------------------------


def _primary_segment_names(
    grids: dict[MetricKey, list[SegmentSeries]],
) -> set[str]:
    """Names that already render in the primary grids — we skip them when
    building secondary expansions so the user doesn't see the same axis
    twice (once in the main table, once in the "by ..." expander)."""
    out: set[str] = set()
    for series_list in grids.values():
        for s in series_list:
            out.add(s.segment_name)
    return out


def _junction_tables_present(conn: sqlite3.Connection) -> bool:
    """Both junction tables must exist for an expansion to be possible."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name IN ('segment_periods', 'segment_dimensions')"
    ).fetchall()
    return len(rows) >= 2


def _build_secondary_expansions(
    conn: sqlite3.Connection,
    ticker: str,
    quarters_full: list[tuple[int, int]],
    display_labels: list[str],
    *,
    primary_segment_names: set[str],
) -> list[SegmentSecondaryExpansion]:
    """Group junction dim rows into per-dim_type secondary breakdowns.

    Each dim_type (geography / channel / customer_segment / business_unit)
    that carries rows OTHER than those already rendered in the primary grids
    becomes one SegmentSecondaryExpansion. `parent_label` is None at this
    stage — the renderer presents a flat "by <dim_type>" subtable beneath the
    primary segments grid.
    """
    if not _junction_tables_present(conn):
        return []
    # Pull every revenue-flavored dim cell — anything whose metric is 'revenue'
    # itself OR carries a 'revenue' / 'sales' / 'gmv' affix (e.g. AWS_revenue,
    # cloud_revenue) so cross-tab expansions for specific lines surface here.
    # Both quarterly and annual rows participate — a FY row maps to (year, 4)
    # via calendar_quarter_key, and when a true Qx row exists at the same key
    # we prefer the quarterly one (see iteration order below).
    period_types: tuple[str, ...] = QUARTERLY_PERIOD_TYPES + ANNUAL_PERIOD_TYPES
    placeholders = ",".join("?" * len(period_types))
    rows = conn.execute(
        f"""
        SELECT sp.period_end, sp.fiscal_period_type, sd.dim_type, sd.dim_name,
               sd.value, sd.metric
        FROM segment_periods sp
        JOIN segment_dimensions sd ON sd.period_id = sp.id
        WHERE sp.ticker = ?
          AND sp.fiscal_period_type IN ({placeholders})
          AND (
            sd.metric = 'revenue'
            OR sd.metric LIKE '%revenue%'
            OR sd.metric LIKE '%sales%'
            OR sd.metric LIKE '%gmv%'
          )
        """,
        (ticker.upper(), *period_types),
    ).fetchall()
    if not rows:
        return []
    # Sort so annual rows are processed FIRST and quarterly rows LAST. The
    # dict assignment ``by_axis[...][qkey] = v`` is last-write-wins, so this
    # ordering means a quarterly cell overwrites an annual cell at the same
    # qkey — i.e. preference flows quarterly > annual for tickers that
    # disclose both. For tickers with only annual data (the common 10-K
    # case), the annual rows survive unmodified.
    annual_set = set(ANNUAL_PERIOD_TYPES)
    sorted_rows = sorted(rows, key=lambda r: 0 if str(r["fiscal_period_type"]) in annual_set else 1)

    # Group by (dim_type, metric) so AWS_revenue-by-geography is its own
    # expansion, distinct from the global revenue-by-geography table that
    # already appears in the primary grids.
    by_axis: dict[tuple[str, str], dict[str, dict[tuple[int, int], float]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    for r in sorted_rows:
        dim_type = str(r["dim_type"])
        metric_str = str(r["metric"])
        dim_name = str(r["dim_name"])
        # Skip cells already represented in a primary grid (avoid double-render)
        # — but ONLY when the metric is the generic 'revenue', because a more
        # specific metric (AWS_revenue) is genuinely a different cross-section.
        if metric_str == "revenue" and dim_name in primary_segment_names:
            continue
        qkey = calendar_quarter_key(r["period_end"])
        if qkey not in quarters_full:
            continue
        try:
            v = float(r["value"])
        except (TypeError, ValueError):
            continue
        by_axis[(dim_type, metric_str)][dim_name][qkey] = v

    display_quarter_count = len(display_labels)
    out: list[SegmentSecondaryExpansion] = []
    for (dim_type, metric_str), by_name in by_axis.items():
        rows_out: list[SegmentSeries] = []
        for name, qmap in by_name.items():
            full_series = [_optional_millions(qmap.get(q)) for q in quarters_full]
            display_values = full_series[-display_quarter_count:]
            rows_out.append(
                SegmentSeries(
                    segment_name=name,
                    metric="revenue_by_product",
                    quarters=display_labels,
                    values=display_values,
                    growth=compute_growth(full_series),
                    levels_full=full_series,
                )
            )
        if not rows_out:
            continue
        rows_out.sort(key=lambda s: -(next((v for v in reversed(s.values) if v is not None), 0.0)))
        # When the metric encodes a specific subject (e.g. 'AWS_revenue' →
        # parent="AWS"), surface that as the parent_label so the renderer can
        # caption the expansion as "by geography under AWS". Cells with the
        # generic 'revenue' metric have no parent — they're a flat secondary
        # axis breakdown of the consolidated business.
        if metric_str != "revenue":
            parent = metric_str.rsplit("_revenue", 1)[0].rsplit("_sales", 1)[0]
            parent_label: str | None = parent.replace("_", " ").strip() or None
        else:
            parent_label = None
        out.append(
            SegmentSecondaryExpansion(
                dim_type=dim_type,
                parent_label=parent_label,
                rows=rows_out,
            )
        )
    out.sort(key=lambda e: (e.dim_type, e.parent_label or ""))
    return out
