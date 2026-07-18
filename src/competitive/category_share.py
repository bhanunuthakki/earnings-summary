"""Piece 1 — annual 3rd-party category-share ingest (manual-entry friendly).

3rd-party category position (Gartner Magic Quadrant, IDC data-protection market
share) isn't machine-readable on a schedule — it's published annually in
analyst reports. So this is a small, version-controlled structured store
(``micro_thesis/competitive/<TICKER>_category_share.json``) plus a loader that
writes each grounded datapoint into ``kpi_facts`` as an ANNUAL (FY) fact through
``persist_manifest`` — the same single write interface the IR-spreadsheet path
uses. Once ingested, the matching tier-2 KPI in ``RBRK.json`` reads a real
stored value (the thesis evaluator + report ledger resolve it by name).

The store is the manual-entry path: an operator adds a row each time a new MQ /
IDC report lands. An entry whose ``value`` is null is an explicit "awaiting
source" slot — it is SKIPPED (never written as a fabricated fact), so the KPI
simply has no datapoint for that year until the operator fills it in.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict

from competitive._docs import ensure_synthetic_document
from models.documents import SourceType
from models.facts import FiscalPeriodType, LegacyEscapeHatch, Unit
from models.kpis import ReportingCadence
from pipeline.kpi_persistence import KpiExtractionManifest, KpiValue, persist_manifest
from pipeline.run_accounting import start_run

_DOC_TYPE = "competitive_category_share"
_EXTRACTED_BY = "competitive_category_share"

# Analyst-curated seed entries (Gartner MQ / IDC placement) cite a published
# report by name/year (see _excerpt below) but have no JSON/PDF position in
# this repo's document store to anchor a FactLocator to.
_NO_LOCATOR = LegacyEscapeHatch(
    reason=(
        "analyst-curated competitive-share seed entry cites a published "
        "3rd-party report (Gartner MQ / IDC) with no cached JSON/PDF "
        "artifact in this repo to anchor a locator to -- source_excerpt "
        "carries the citation instead"
    )
)


class CategoryShareEntry(BaseModel):
    """One annual 3rd-party category datapoint for a metric.

    ``value`` is null for an "awaiting source" slot (skipped by the loader).
    ``label`` carries the prose form (e.g. "Leader") and lands in the fact's
    ``source_excerpt`` so the report shows the human-readable position alongside
    the chartable number.
    """

    model_config = ConfigDict(extra="forbid")

    metric: str
    fiscal_year: int
    value: Decimal | None
    unit: Unit
    source: str
    label: str | None = None
    note: str | None = None


class CategoryShareSeed(BaseModel):
    """The committed seed file for one ticker."""

    model_config = ConfigDict(extra="forbid")

    ticker: str
    readme: str | None = None
    entries: list[CategoryShareEntry]


@dataclass(slots=True)
class IngestResult:
    """Per-run tally from :func:`ingest_category_share`."""

    ticker: str
    inserted: int = 0
    skipped_existing: int = 0
    skipped_awaiting_source: int = 0
    written_metrics: list[str] = field(default_factory=list[str])


def seed_path(repo_root: Path, ticker: str) -> Path:
    return repo_root / "micro_thesis" / "competitive" / f"{ticker.upper()}_category_share.json"


def load_seed(repo_root: Path, ticker: str) -> CategoryShareSeed | None:
    """Load and validate the committed seed file; None when absent."""
    path = seed_path(repo_root, ticker)
    if not path.exists():
        return None
    return CategoryShareSeed.model_validate_json(path.read_text(encoding="utf-8"))


def ingest_category_share(conn: sqlite3.Connection, repo_root: Path, ticker: str) -> IngestResult:
    """Write each grounded annual datapoint in the seed into ``kpi_facts``.

    Groups entries by fiscal year (one manifest per year), marks every metric
    ANNUAL with a fiscal-year-end ``period_end`` so it lands on the annual axis,
    and routes through ``persist_manifest``. Idempotent: a replay reuses the
    synthetic document and the provenance-unique ``kpi_facts`` key, so nothing
    is double-written.
    """
    result = IngestResult(ticker=ticker.upper())
    seed = load_seed(repo_root, ticker)
    if seed is None:
        return result

    run_id = start_run(
        conn, directive="ingest_competitive_category_share", ticker_scope=[ticker.upper()]
    )

    # Group the writable (value-present) entries by fiscal year.
    by_year: dict[int, list[CategoryShareEntry]] = {}
    for entry in seed.entries:
        if entry.value is None:
            result.skipped_awaiting_source += 1
            continue
        by_year.setdefault(entry.fiscal_year, []).append(entry)

    for fiscal_year, entries in sorted(by_year.items()):
        period_end = datetime(fiscal_year, 12, 31)  # naive-UTC fiscal-year end
        doc_id = ensure_synthetic_document(
            conn,
            ticker=ticker,
            source_type=SourceType.MANUAL_ENTRY,
            doc_type=_DOC_TYPE,
            source_key=f"{_DOC_TYPE}:{ticker.upper()}:{fiscal_year}",
            period_end=period_end,
        )
        values = [
            KpiValue(
                name=e.metric,
                value=cast("Decimal", e.value),
                unit=e.unit,
                confidence=1.0,
                source_excerpt=_excerpt(e),
                locator=_NO_LOCATOR,
            )
            for e in entries
        ]
        manifest = KpiExtractionManifest(
            ticker=ticker.upper(),
            period_end=period_end,
            fiscal_period_type=FiscalPeriodType.FY,
            source_doc_id=doc_id,
            primary_source=SourceType.MANUAL_ENTRY,
            extracted_by=_EXTRACTED_BY,
            canonical_units={e.metric: e.unit for e in entries},
            cadences={e.metric: ReportingCadence.ANNUAL for e in entries},
            values=values,
        )
        outcome = persist_manifest(conn, run_id=run_id, manifest=manifest)
        result.inserted += outcome.inserted
        result.skipped_existing += outcome.skipped_existing
        for e in entries:
            if e.metric not in result.written_metrics:
                result.written_metrics.append(e.metric)
    return result


def _excerpt(entry: CategoryShareEntry) -> str:
    """Compose the fact's source_excerpt: prose label (if any) + source + note."""
    parts: list[str] = []
    if entry.label:
        parts.append(entry.label)
    parts.append(entry.source)
    if entry.note:
        parts.append(entry.note)
    return " — ".join(parts)
