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
    segment_rows = _build_weighting_rows(
        ticker=ticker,
        repo_root=repo_root,
        metric="revenue_by_product",
        descriptions=cached.segments,
        rules=rules,
    )
    geo_rows = _build_weighting_rows(
        ticker=ticker,
        repo_root=repo_root,
        metric="revenue_by_geography",
        descriptions=cached.geographies,
        rules=rules,
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


def _build_weighting_rows(
    ticker: str,
    repo_root: Path,
    metric: str,
    descriptions: list[dict[str, str | None]],
    rules: TickerRules,
) -> list[SegmentWeighting]:
    """Merge latest-period segment_facts revenue with LLM-written descriptions.

    Returns rows sorted by descending share. Segments below 1% of the bucket
    roll up into a single "Other" row (matching the §5 segments behavior so
    the weighting summary is consistent with the detail table).
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
    rows: list[SegmentWeighting] = []
    other_rev = 0.0
    other_count = 0
    sorted_items = sorted(latest.items(), key=lambda kv: abs(kv[1]), reverse=True)
    for name, value in sorted_items:
        share = abs(value) / total
        if share < 0.01:
            other_rev += value
            other_count += 1
            continue
        rows.append(
            SegmentWeighting(
                name=name,
                revenue_usd_m=value / 1_000_000.0,
                share_pct=share,
                description=description_by_name.get(name),
            )
        )
    if other_count > 0:
        rows.append(
            SegmentWeighting(
                name=f"Other ({other_count} < 1%)",
                revenue_usd_m=other_rev / 1_000_000.0,
                share_pct=abs(other_rev) / total,
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
    if conn is None or not has_table(conn, "segment_facts"):
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
    placeholders = ",".join("?" * len(QUARTERLY_PERIOD_TYPES))
    cur = conn.execute(
        f"""
        SELECT period_end, segment_name, value
        FROM segment_facts
        WHERE ticker = ? AND metric = ?
          AND fiscal_period_type IN ({placeholders})
        """,
        (ticker, metric, *QUARTERLY_PERIOD_TYPES),
    )
    return [dict(cast("sqlite3.Row", r)) for r in cur.fetchall()]


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
