"""Point-in-time reader over the self-owned FMP analyst-estimates archive.

``execution/save_fmp_data.py`` snapshots every time-sensitive FMP endpoint to
``data/historical/fmp_snapshots/<YYYY-MM-DD>/<TICKER>_<suffix>.json`` — a
point-in-time consensus archive accreting since 2026-05-19. This module is its
first reader: "what was the consensus estimate for ticker T, metric M, fiscal
year Y, AS OF date D?"

Honesty rules (non-negotiable):

- AS OF D resolves to the LATEST snapshot dated <= D. The answer carries that
  snapshot date so the staleness is visible, never hidden.
- A date before the ticker's archive start is ``not_available`` — the archive
  cannot answer questions about a past it never observed. We NEVER backfill or
  interpolate between snapshots.
- A snapshot that exists but lacks the fiscal year / metric is an honest
  ``no_data_for_year`` / ``None`` value, not a nearest-neighbor guess.

Revision-momentum signals on top of this reader are explicitly out of scope
(src/signals scaffolds stay scaffolded).

CLI wrapper: ``execution/read_estimate_asof.py``.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import date
from pathlib import Path
from typing import Literal, cast

from models.fmp_payloads import FmpAnalystEstimateRecord

_DATE_DIR_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")

ANNUAL_SUFFIX = "_analyst_estimates_annual.json"

#: Metrics answerable from FmpAnalystEstimateRecord (vendor field names kept
#: verbatim — these ARE the provenance: endpoint=analyst-estimates, field=M).
SUPPORTED_METRICS: tuple[str, ...] = (
    "revenueAvg",
    "ebitdaAvg",
    "ebitAvg",
    "netIncomeAvg",
    "netIncomeHigh",
    "netIncomeLow",
    "epsAvg",
    "epsHigh",
    "epsLow",
    "numAnalystsRevenue",
    "numAnalystsEps",
)

ArchiveStatus = Literal["ok", "not_available", "no_data_for_year", "unknown_metric"]


@dataclass(frozen=True)
class ArchiveEstimate:
    """One as-of answer. ``snapshot_date`` is the archive date actually used;
    ``archive_start`` is the ticker's earliest snapshot (the honesty boundary
    quoted on every miss)."""

    status: ArchiveStatus
    ticker: str
    metric: str
    fiscal_year: int
    asof: str
    value: float | None = None
    snapshot_date: str | None = None
    archive_start: str | None = None
    detail: str | None = None

    def to_json(self) -> dict[str, object]:
        return cast("dict[str, object]", asdict(self))


def snapshot_dates(snapshots_dir: Path) -> list[str]:
    """All YYYY-MM-DD snapshot directories, ascending. Non-date dirs ignored."""
    if not snapshots_dir.exists():
        return []
    return sorted(
        d.name for d in snapshots_dir.iterdir() if d.is_dir() and _DATE_DIR_RX.match(d.name)
    )


def ticker_archive_dates(ticker: str, snapshots_dir: Path) -> list[str]:
    """Snapshot dates that actually carry this ticker's annual analyst
    estimates, ascending — the ticker's real archive coverage (sparser than
    the directory list; the cacher only re-snapshots on its cadence)."""
    name = f"{ticker.upper()}{ANNUAL_SUFFIX}"
    return [d for d in snapshot_dates(snapshots_dir) if (snapshots_dir / d / name).exists()]


def _load_rows(path: Path) -> list[FmpAnalystEstimateRecord]:
    """Read + validate one snapshot file. Rows that fail the (already
    ingest-gated) model are dropped individually; a malformed file yields []."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    rows: list[FmpAnalystEstimateRecord] = []
    for item in cast("list[object]", raw):
        if not isinstance(item, dict):
            continue
        try:
            rows.append(FmpAnalystEstimateRecord.model_validate(item))
        except Exception:
            continue
    return rows


def estimate_asof(
    ticker: str,
    metric: str,
    fiscal_year: int,
    asof: date,
    *,
    snapshots_dir: Path,
) -> ArchiveEstimate:
    """The consensus ``metric`` for ``ticker``'s fiscal year ``fiscal_year``
    as it stood on ``asof`` (annual estimates; latest snapshot <= asof)."""
    ticker = ticker.upper()
    base = ArchiveEstimate(
        status="not_available",
        ticker=ticker,
        metric=metric,
        fiscal_year=fiscal_year,
        asof=asof.isoformat(),
    )
    if metric not in SUPPORTED_METRICS:
        return replace(
            base,
            status="unknown_metric",
            detail=f"metric must be one of {', '.join(SUPPORTED_METRICS)}",
        )
    dates = ticker_archive_dates(ticker, snapshots_dir)
    if not dates:
        return replace(base, detail="no snapshots for this ticker in the archive")
    archive_start = dates[0]
    if asof.isoformat() < archive_start:
        return replace(
            base,
            archive_start=archive_start,
            detail=(
                f"asof {asof.isoformat()} predates the archive start "
                f"{archive_start}; point-in-time data does not exist and is "
                f"never interpolated"
            ),
        )
    usable = [d for d in dates if d <= asof.isoformat()]
    snapshot_date = usable[-1]
    rows = _load_rows(snapshots_dir / snapshot_date / f"{ticker}{ANNUAL_SUFFIX}")
    row = next((r for r in rows if r.date[:4] == str(fiscal_year)), None)
    if row is None:
        return replace(
            base,
            status="no_data_for_year",
            snapshot_date=snapshot_date,
            archive_start=archive_start,
            detail=(
                f"snapshot {snapshot_date} carries no FY{fiscal_year} row "
                f"(FMP Starter truncates to 10 rows/call)"
            ),
        )
    # Field access is dynamic (metric is data), but the value's type is
    # guaranteed by the FmpAnalystEstimateRecord validation above — one cast
    # at the validated boundary, per the repo typing convention.
    raw_value = cast("int | float | None", getattr(row, metric))
    return ArchiveEstimate(
        status="ok",
        ticker=ticker,
        metric=metric,
        fiscal_year=fiscal_year,
        asof=asof.isoformat(),
        value=float(raw_value) if raw_value is not None else None,
        snapshot_date=snapshot_date,
        archive_start=archive_start,
        detail=None if raw_value is not None else "row present but field null at source",
    )
