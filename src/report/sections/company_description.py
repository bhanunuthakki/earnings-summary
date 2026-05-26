"""§2 Company description — read the cached extraction and overlay latest-period
segment / geography weighting from `segment_facts`.

The expensive part (10-K scan + LLM synthesis) runs separately via
`execution/extract_company_description.py`, which writes
`data/company_description/{TICKER}.json`. This section consumes that cache and
attaches the live revenue-share numbers so the relative weighting always
reflects the latest quarter even when the LLM-written prose is stale.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import cast

from compute.company_description import load_description
from compute.platform_diagram import load_diagram
from report.models import (
    CompanyDescriptionSection,
    SectionStatus,
    SegmentWeighting,
)
from report.rules import TickerRules, load_rules
from report.sections._common import (
    QUARTERLY_PERIOD_TYPES,
    calendar_quarter_key,
    has_table,
    missing,
    open_repo_db,
)


def build(ticker: str, repo_root: Path) -> CompanyDescriptionSection:
    ticker = ticker.upper()
    cached = load_description(repo_root, ticker)
    if cached is None:
        return CompanyDescriptionSection(
            status=SectionStatus.MISSING_DATA,
            missing=missing(
                stage="SYNTHESIZE(company_description)",
                fix_command=(
                    f"python execution/extract_company_description.py --ticker {ticker}"
                ),
                detail=(
                    "Reads the latest 10-K (data/historical/fmp/) + profile.json, "
                    "calls Claude Sonnet to synthesize the description, "
                    "caches to data/company_description/."
                ),
            ),
        )
    if cached.skipped_reason:
        return CompanyDescriptionSection(
            status=SectionStatus.MISSING_DATA,
            missing=missing(
                stage="SYNTHESIZE(company_description)",
                fix_command=(
                    f"python execution/extract_company_description.py --ticker {ticker} --refresh"
                ),
                detail=cached.skipped_reason,
            ),
            sector=cached.sector,
            industry=cached.industry,
        )

    rules = load_rules(ticker, repo_root)
    # OI by segment name is requested on BOTH tables — for most companies OI
    # aligns with the product breakdown (META, GOOG, MELI, NU), but for
    # geography-segmented P&L reporters (AMZN reports OI by NA / International
    # / AWS, not by product line) it only joins to the geography table. The
    # renderer hides the column on tables where no row carries OI, so this is
    # a no-op for tickers where the join fails on both sides.
    segment_rows = _build_weighting_rows(
        ticker=ticker,
        repo_root=repo_root,
        metric="revenue_by_product",
        descriptions=cached.segments,
        rules=rules,
        include_operating_income=True,
    )
    geo_rows = _build_weighting_rows(
        ticker=ticker,
        repo_root=repo_root,
        metric="revenue_by_geography",
        descriptions=cached.geographies,
        rules=rules,
        include_operating_income=True,
    )

    # Optional: platform diagram is a separate extraction pipeline so the
    # section degrades gracefully when it hasn't been run yet. Missing cache
    # or absent diagram fields => section still renders without the visual.
    diagram_cached = load_diagram(repo_root, ticker)
    platform_diagram = diagram_cached.diagram if diagram_cached else None
    platform_caption = diagram_cached.caption if diagram_cached else None

    return CompanyDescriptionSection(
        status=SectionStatus.OK,
        elevator_pitch=cached.elevator_pitch,
        platform_diagram=platform_diagram,
        platform_caption=platform_caption,
        business_overview=cached.business_overview,
        revenue_model=cached.revenue_model,
        segment_breakdown=segment_rows,
        geographic_breakdown=geo_rows,
        source_fiscal_year=cached.fiscal_year,
        cached_at=_parse_iso(cached.extracted_at_end),
        sector=cached.sector,
        industry=cached.industry,
    )


# Aliases applied AFTER " Segment" suffix-stripping and lowercasing — joins
# revenue-side names to OI-side names that differ only by abbreviation /
# common synonyms. Add new entries here when a ticker has a documented
# revenue↔OI naming gap that the suffix strip alone doesn't bridge.
_OI_KEY_ALIASES: dict[str, str] = {
    "amazon web services": "aws",
    "google services": "services",  # GOOG: Search/YouTube/Apps roll up to "Services"
}


def _normalize_oi_key(name: str) -> str:
    """Collapse minor naming variation between the revenue and OI segment_facts rows.

    Lowercases, strips a trailing " segment" suffix (the most common gap —
    AMZN's revenue_by_geography rows are "...Segment" while OI rows are
    not), then applies a small alias table for abbreviation collisions
    (e.g. "Amazon Web Services" ↔ "AWS").
    """
    base = name.strip().lower()
    if base.endswith(" segment"):
        base = base[: -len(" segment")].rstrip()
    return _OI_KEY_ALIASES.get(base, base)


def _build_weighting_rows(
    ticker: str,
    repo_root: Path,
    metric: str,
    descriptions: list[dict[str, str | None]],
    rules: TickerRules,
    include_operating_income: bool = False,
) -> list[SegmentWeighting]:
    """Merge latest-period segment_facts revenue with LLM-written descriptions.

    Returns rows sorted by descending share. Segments below 1% of the bucket
    roll up into a single "Other" row (matching the §5 segments behavior so
    the weighting summary is consistent with the detail table).

    When `include_operating_income=True` (typically the product/segment table,
    NOT the geography table), also pulls the latest-period operating_income
    metric from segment_facts and merges it onto each row by canonical
    segment name. Rows without an OI fact get `operating_income_usd_m=None`
    and the renderer shows "—" for that cell — common for sub-segments and
    geography buckets that aren't reported as P&L segments.
    """
    description_by_name = {
        str(d["name"]): d.get("description") for d in descriptions if d.get("name")
    }
    latest = _latest_period_totals(ticker, repo_root, metric, rules)
    if not latest:
        return [
            SegmentWeighting(name=name, description=desc)
            for name, desc in description_by_name.items()
        ]
    total = sum(abs(v) for v in latest.values())
    if total == 0:
        return [
            SegmentWeighting(name=name, description=desc)
            for name, desc in description_by_name.items()
        ]

    # Pull OI by-segment for the same latest quarter. The OI table often uses
    # different naming than revenue (e.g. AMZN reports OI as "North America" /
    # "International" / "AWS" while geographic revenue is "North America
    # Segment" / "International Segment" / "Amazon Web Services Segment", and
    # product revenue is "Online Stores" / "Third-Party Seller Services" /
    # ...). We try exact match first, then a normalized match that strips the
    # " Segment" suffix and applies a small alias table. Rows that still fail
    # to join show `operating_income_usd_m=None` and the renderer suppresses
    # the column when no row in the table has OI.
    oi_by_name: dict[str, float] = {}
    if include_operating_income:
        oi_by_name = _latest_period_totals(
            ticker, repo_root, "operating_income", rules
        )
    oi_total = sum(abs(v) for v in oi_by_name.values()) if oi_by_name else 0.0
    oi_lookup_normalized = {_normalize_oi_key(k): v for k, v in oi_by_name.items()}

    def _resolve_oi(rev_name: str) -> float | None:
        # Exact match wins (most companies — META/GOOG/MELI/NU/NOW segment
        # names line up across metrics).
        if rev_name in oi_by_name:
            return oi_by_name[rev_name]
        # Otherwise try the normalized key: strip " Segment" suffix and run
        # both sides through a tiny alias table so "Amazon Web Services" /
        # "AWS" / "Amazon Web Services Segment" collapse onto the same key.
        return oi_lookup_normalized.get(_normalize_oi_key(rev_name))

    rows: list[SegmentWeighting] = []
    other_rev = 0.0
    other_oi = 0.0
    other_count = 0
    sorted_items = sorted(latest.items(), key=lambda kv: abs(kv[1]), reverse=True)
    for name, value in sorted_items:
        share = abs(value) / total
        oi_value = _resolve_oi(name)
        if share < 0.01:
            other_rev += value
            if oi_value is not None:
                other_oi += oi_value
            other_count += 1
            continue
        rows.append(
            SegmentWeighting(
                name=name,
                revenue_usd_m=value / 1_000_000.0,
                share_pct=share,
                operating_income_usd_m=(
                    oi_value / 1_000_000.0 if oi_value is not None else None
                ),
                oi_share_pct=(
                    abs(oi_value) / oi_total
                    if oi_value is not None and oi_total > 0
                    else None
                ),
                description=description_by_name.get(name),
            )
        )
    if other_count > 0:
        rows.append(
            SegmentWeighting(
                name=f"Other ({other_count} < 1%)",
                revenue_usd_m=other_rev / 1_000_000.0,
                share_pct=abs(other_rev) / total,
                operating_income_usd_m=(
                    other_oi / 1_000_000.0 if other_oi != 0 else None
                ),
                oi_share_pct=(
                    abs(other_oi) / oi_total
                    if other_oi != 0 and oi_total > 0
                    else None
                ),
                description=None,
            )
        )
    return rows


def _latest_period_totals(
    ticker: str, repo_root: Path, metric: str, rules: TickerRules
) -> dict[str, float]:
    """Sum the LATEST available quarter's segment values by canonical segment name.

    Mirrors the §5 segments canonicalization (rules.canonicalize_segment) so
    weighting matches the table elsewhere in the report.
    """
    conn = open_repo_db(repo_root)
    if conn is None or not has_table(conn, "segment_dimensions"):
        if conn is not None:
            conn.close()
        return {}
    rows = _load_rows(conn, ticker, metric)
    conn.close()
    if not rows:
        return {}
    canonical_by_quarter: dict[tuple[int, int], dict[str, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    for r in rows:
        seg = rules.canonicalize_segment(str(r["segment_name"]))
        if seg is None:
            continue
        q = calendar_quarter_key(r["period_end"])
        canonical_by_quarter[q][seg] += float(cast("float", r["value"]))
    if not canonical_by_quarter:
        return {}
    latest_q = max(canonical_by_quarter.keys())
    return dict(canonical_by_quarter[latest_q])


def _load_rows(
    conn: sqlite3.Connection, ticker: str, metric: str
) -> list[dict[str, object]]:
    """Read segment cells matching a legacy `segment_facts` metric.

    Maps the legacy metric to the junction's (dim_type, metric) pair before
    querying; the returned rows expose the legacy column names
    (period_end, segment_name, value) so the caller's grid math stays
    unchanged.
    """
    if metric == "revenue_by_product":
        dim_type, junction_metric = ("product", "revenue")
    elif metric == "revenue_by_geography":
        dim_type, junction_metric = ("geography", "revenue")
    elif metric == "operating_income":
        dim_type, junction_metric = ("business_unit", "operating_income")
    else:
        dim_type, junction_metric = ("business_unit", metric)
    placeholders = ",".join("?" * len(QUARTERLY_PERIOD_TYPES))
    cur = conn.execute(
        f"""
        SELECT
            sp.period_end AS period_end,
            sd.dim_name AS segment_name,
            sd.value AS value
        FROM segment_periods sp
        JOIN segment_dimensions sd ON sd.period_id = sp.id
        WHERE sp.ticker = ?
          AND sd.dim_type = ?
          AND sd.metric = ?
          AND sp.fiscal_period_type IN ({placeholders})
        """,
        (ticker, dim_type, junction_metric, *QUARTERLY_PERIOD_TYPES),
    )
    return [dict(cast("sqlite3.Row", r)) for r in cur.fetchall()]


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
