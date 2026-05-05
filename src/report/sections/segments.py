"""§4 Segments — 12-quarter wide-form per segment × {revenue_by_product,
revenue_by_geography, operating_income}.

Sources:
  - segment_facts table (populated by execution/extract_facts.py from FMP segment
    JSON and 10-Q/10-K notes).

Per-ticker hygiene rules (segment renames + drops) live in the holdings JSON
under `data_rules` and are applied via report.rules.TickerRules.canonicalize.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Literal, cast

from report.models import (
    SectionStatus,
    SegmentSeries,
    SegmentsSection,
)
from report.rules import TickerRules, load_rules
from report.sections._common import (
    DISPLAY_QUARTERS,
    QUARTERLY_PERIOD_TYPES,
    UNDERLYING_QUARTERS,
    calendar_quarter_key,
    compute_growth,
    has_table,
    missing,
    open_repo_db,
    quarter_label,
)

MetricKey = Literal["revenue_by_product", "revenue_by_geography", "operating_income"]
_BUCKETS: tuple[MetricKey, ...] = ("revenue_by_product", "revenue_by_geography", "operating_income")


def build(ticker: str, repo_root: Path) -> SegmentsSection:
    rules = load_rules(ticker, repo_root)
    conn = open_repo_db(repo_root)
    if conn is None or not has_table(conn, "segment_facts"):
        if conn is not None:
            conn.close()
        return SegmentsSection(
            status=SectionStatus.MISSING_DATA,
            missing=missing(
                stage="COMPUTE(extract_facts)",
                fix_command=f"python execution/extract_facts.py --ticker {ticker.upper()} --doc-type fmp_segment_product",
            ),
        )

    raw = _load_segment_rows(conn, ticker)
    conn.close()

    if not raw:
        return SegmentsSection(
            status=SectionStatus.MISSING_DATA,
            missing=missing(
                stage="COMPUTE(extract_facts)",
                fix_command=f"python execution/extract_facts.py --ticker {ticker.upper()}",
                detail="segment_facts has no rows for this ticker",
            ),
        )

    canonical = _apply_rules(raw, rules)
    deduped = _dedupe_by_quarter(canonical)
    quarters_full, display_labels = _quarter_axes(deduped)

    grids = _build_grids(deduped, quarters_full, display_labels)
    return SegmentsSection(
        status=SectionStatus.OK if any(grids.values()) else SectionStatus.PARTIAL,
        quarter_labels=display_labels,
        revenue_by_product=grids["revenue_by_product"],
        revenue_by_geography=grids["revenue_by_geography"],
        operating_income=grids["operating_income"],
    )


# ---------------------------------------------------------------------------
# Loading + canonicalization
# ---------------------------------------------------------------------------


def _load_segment_rows(conn: sqlite3.Connection, ticker: str) -> list[dict[str, object]]:
    """Pull a generous window of quarterly segment rows; we'll dedupe in Python."""
    placeholders = ",".join("?" * len(QUARTERLY_PERIOD_TYPES))
    cursor = conn.cursor()
    cursor.execute(
        f"""
        SELECT period_end, segment_name, metric, value, fiscal_period_type
        FROM segment_facts
        WHERE ticker = ? AND fiscal_period_type IN ({placeholders})
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
        if metric not in _BUCKETS:
            continue
        bucket: MetricKey = cast(MetricKey, metric)
        seg = str(r["segment_name"])
        by_metric_segment[bucket][seg][calendar_quarter_key(r["period_end"])] = float(r["value"])

    grids: dict[MetricKey, list[SegmentSeries]] = {bucket: [] for bucket in _BUCKETS}
    display_quarter_count = len(display_labels)
    for bucket in _BUCKETS:
        for segment_name, qmap in sorted(by_metric_segment[bucket].items()):
            raw_series = [_optional_millions(qmap.get(q)) for q in quarters_full]
            cleaned_series = _drop_outliers(raw_series)
            display_values = cleaned_series[-display_quarter_count:]
            grids[bucket].append(
                SegmentSeries(
                    segment_name=segment_name,
                    metric=bucket,
                    quarters=display_labels,
                    values=display_values,
                    growth=compute_growth(cleaned_series),
                )
            )
    return grids


def _optional_millions(value: float | None) -> float | None:
    return None if value is None else value / 1_000_000.0
