"""Generic capture-all extractor over FMP-XBRL note-detail tables (Stage A).

The deterministic, no-LLM half of the "capture every reported number" program
(`directives/capture_every_number_program.md`). It walks every ``(Details)``
note table in an FMP ``form_10k`` JSON via :func:`table_extractors.base.iter_xbrl_table`
and emits one ``kpi_facts`` value per numeric cell — section-qualified name,
value expanded to the issuer's base units, provenance back to the filing — routed
through the shared :func:`pipeline.kpi_persistence.persist_manifest` contract with
``origin=CAPTURE`` (names canonicalized by ``compute.kpi_resolver.canonical_metric_name``
inside persist, so unit/casing/whitespace variants collapse instead of exploding
the registry). Once landed, every captured value is automatically askable,
DIY-pickable, and DCF-resolvable — the substrate is already universal.

Scope (v1, precision-first — the program owner's "bias toward Stage A's clean
structured labels first"):

  * ONLY ``(Details)`` note tables. The primary statements (``CONSOLIDATED ...``)
    are already captured into ``financial_facts`` by the dedicated
    ``compute.extract_*_facts`` extractors; re-capturing them here would mint
    duplicate metrics, so they are skipped. Narrative blobs, the cover page, and
    value-less ``(Tables)`` templates carry no clean numeric grid and are skipped.

  * ONLY monetary cells, expanded to ``Unit.ACTUAL``. A ``$ in Millions`` section
    freely interleaves rate rows (``"Coupon Rate": 0.0338``) and percentage rows
    (``"Percentage of revenue": 1``); blindly scaling those by 1e6 would be
    catastrophic and ``persist_manifest``'s range guard would NOT catch it (an
    ``actual`` value has no plausible-range check). So per-row label guards skip
    rate / percent / ratio / share-count rows, a sub-unit magnitude guard skips
    fractional "monetary" cells (a coupon rate that slipped the label net), and
    per-share rows are captured WITHOUT scaling. Everything skipped is DEFERRED to
    the LLM enumerate pass (Stage B), never silently dropped — each skip is tallied
    by reason in :class:`CaptureAudit` so coverage is measurable.

  * 10-K only (annual ``FY`` columns). A 10-Q section interleaves duration columns
    (3 / 6 / 9-months-ended) that share an end-date, so the period axis can't be
    derived deterministically from the column label alone; deferred.

The walker assumes table columns are PERIODS. When a column header doesn't parse
to a date (fair-value ``Level 1/2/3`` grids, member-dimension columns, …) that
column is skipped — which conveniently self-selects the walk down to genuine
time-series tables.

Entry point: registered as table_kind ``xbrl_capture_all`` in
``document_table_extractor._REGISTRY``; run via
``execution/extract_document_tables.py --table-kind xbrl_capture_all``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import cast

from models.documents import SourceType
from models.facts import FiscalPeriodType, Unit
from models.kpis import DefinitionOrigin
from pipeline import locators
from pipeline.capture_coverage import CaptureCoverageRecord, record_coverage
from pipeline.kpi_persistence import KpiExtractionManifest, KpiValue, persist_manifest
from pipeline.run_accounting import start_run
from table_extractors.base import (
    ExtractionOutcome,
    iter_xbrl_table,
    parse_units,
)

# Extracted OUT to their own modules (segment_quarterly_framework.md §2.2) —
# the ONE sanctioned edit to this otherwise-frozen module: import under the
# old private names so every call site below is untouched, behavior
# byte-identical. table_extractors.xbrl_value_classify also backs the new
# compute.segment_quarterly_10q extractor, which needs the identical guards.
from table_extractors.period_axis import parse_trailing_date as _parse_period

# Not referenced below directly (the moved _classify_value already closes
# over it) — kept as a module attribute because tests/test_generic_xbrl_
# capture.py asserts against it reflectively (g._RESIDUAL_RISK_CONFIDENCE).
from table_extractors.xbrl_value_classify import (  # noqa: F401
    RESIDUAL_RISK_CONFIDENCE as _RESIDUAL_RISK_CONFIDENCE,  # pyright: ignore[reportUnusedImport]
)
from table_extractors.xbrl_value_classify import SCALE_FACTOR as _SCALE_FACTOR
from table_extractors.xbrl_value_classify import build_name as _build_name
from table_extractors.xbrl_value_classify import classify_value as _classify_value
from table_extractors.xbrl_value_classify import (
    is_unit_ambiguous_section as _is_unit_ambiguous_section,
)
from table_extractors.xbrl_value_classify import (
    semantic_section_title as _semantic_section_title,
)

log = logging.getLogger(__name__)

EXTRACTOR_ID = "generic_xbrl_capture_v1"
EXTRACTOR_VERSION = "1"
TABLE_KIND = "xbrl_capture_all"
# kpi_facts.extracted_by tag — deterministic (NOT llm) so audits don't mislabel it.
EXTRACTED_BY = "capture_xbrl_v1"

# Section titles we never capture even when they carry "(Details)": these hold
# identifiers / dates / enumerations, not financial measurements.
_NONFINANCIAL_SECTION_TOKENS: tuple[str, ...] = (
    "insider trading",
    "cybersecurity",
    "cover page",
    "audit information",
)


@dataclass(slots=True)
class CaptureAudit:
    """Per-run coverage tally — every cell SEEN, and why each was/ wasn't kept.

    Surfaced so the silent-drop blind spot the program was built to kill stays
    visible: ``captured + sum(skipped.values()) == cells_seen``. PR3 lifts this
    into a persisted coverage log + folds in persist_manifest's range/unit drops.
    """

    sections_seen: int = 0
    sections_detail: int = 0
    rows_seen: int = 0
    cells_seen: int = 0
    captured: int = 0
    # Of the captured cells, the count down-weighted for residual mis-scale risk
    # (a percent/ratio-shaped magnitude that slipped the label net). A SUBSET of
    # ``captured``, not a separate bucket — it leaves the captured+skips==seen
    # invariant intact while making the confidence haircut measurable.
    penalized: int = 0
    # reason -> count, for every cell seen-but-not-captured.
    skipped: dict[str, int] = field(default_factory=dict[str, int])
    # validation_issues persist_manifest raised on the cells we DID hand it
    # (PLAUSIBLE_RANGE / UNIT_MISMATCH) — these dropped a value post-handoff.
    dropped_validation: int = 0

    def skip(self, reason: str, n: int = 1) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + n

    def as_dict(self) -> dict[str, object]:
        return {
            "sections_seen": self.sections_seen,
            "sections_detail": self.sections_detail,
            "rows_seen": self.rows_seen,
            "cells_seen": self.cells_seen,
            "captured": self.captured,
            "penalized": self.penalized,
            "dropped_validation": self.dropped_validation,
            "skipped": dict(sorted(self.skipped.items())),
        }


def extract(
    *,
    ticker: str,
    fiscal_year: int | None,
    fmp_payload: dict[str, object] | None,
    sec_text: object | None,  # unused — this extractor is FMP-XBRL only
    db_path: Path,
    repo_root: Path,
    filing_doc_id: int | None = None,
    as_of_date: date | None = None,
) -> ExtractionOutcome:
    """Walk every ``(Details)`` section of an FMP 10-K and capture monetary cells.

    Mirrors the :class:`table_extractors.base.ExtractorContract` signature so it
    drops into ``document_table_extractor._REGISTRY``.
    """
    del sec_text, as_of_date  # FMP-XBRL only; period derives from column labels

    if fmp_payload is None or fiscal_year is None:
        return ExtractionOutcome(
            table_kind=TABLE_KIND,
            ticker=ticker,
            fiscal_year=fiscal_year,
            status="no_data",
            notes="missing fmp_payload or fiscal_year",
        )

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        # The shared document_table_extractor._lookup_doc_id matches on
        # period_end's year, which is NULL on this repo's fmp_10k_json rows, so
        # filing_doc_id arrives None. Re-resolve on the file_path natural key.
        doc_id = filing_doc_id or _resolve_doc_id(
            conn, ticker=ticker, fiscal_year=fiscal_year, repo_root=repo_root
        )
        if doc_id is None:
            return ExtractionOutcome(
                table_kind=TABLE_KIND,
                ticker=ticker,
                fiscal_year=fiscal_year,
                status="no_data",
                notes="no fmp_10k_json documents row (file_path lookup missed)",
            )

        audit = CaptureAudit()
        # The fiscal-year-end (month, day) — the modal column date across detail
        # tables. Only columns landing on it are captured (as annual FY facts);
        # stray interim columns (a quarter-end comparative, a subsequent-event
        # date) are off-cycle and skipped. Handles non-December FYEs (Jan-31).
        fye_md = _detect_fye_monthday(fmp_payload)
        # period_end -> the captured values reported in that column.
        per_period: dict[datetime, list[KpiValue]] = defaultdict(list)
        for section_key, section in _iter_sections(fmp_payload):
            audit.sections_seen += 1
            _walk_section(section_key, section, per_period, audit, fye_md)

        if not per_period:
            _emit_coverage(repo_root, ticker, fiscal_year, audit)
            return ExtractionOutcome(
                table_kind=TABLE_KIND,
                ticker=ticker,
                fiscal_year=fiscal_year,
                status="no_data",
                notes=json.dumps(audit.as_dict()),
            )

        run_id = start_run(conn, directive="capture_xbrl", ticker_scope=[ticker.upper()])
        inserted = 0
        for period_end, values in per_period.items():
            manifest = KpiExtractionManifest(
                ticker=ticker.upper(),
                period_end=period_end,
                fiscal_period_type=FiscalPeriodType.FY,
                source_doc_id=doc_id,
                primary_source=SourceType.SEC_XBRL,
                extracted_by=EXTRACTED_BY,
                origin=DefinitionOrigin.CAPTURE,
                values=values,
            )
            result = persist_manifest(conn, run_id=run_id, manifest=manifest)
            inserted += result.inserted
            audit.dropped_validation += result.validation_issues
        audit.captured = sum(len(v) for v in per_period.values())
        _emit_coverage(repo_root, ticker, fiscal_year, audit)

        return ExtractionOutcome(
            table_kind=TABLE_KIND,
            ticker=ticker.upper(),
            fiscal_year=fiscal_year,
            n_rows_proposed=audit.captured,
            n_rows_inserted=inserted,
            status="ok" if inserted > 0 else "no_data",
            notes=json.dumps(audit.as_dict()),
        )
    finally:
        conn.close()


def _emit_coverage(
    repo_root: Path, ticker: str, fiscal_year: int | None, audit: CaptureAudit
) -> None:
    """Append this run's CaptureAudit to the coverage log (best-effort)."""
    record_coverage(
        repo_root,
        CaptureCoverageRecord(
            stage="xbrl_capture",
            ticker=ticker.upper(),
            source="10k",
            fiscal_year=fiscal_year,
            seen=audit.cells_seen,
            captured=audit.captured,
            penalized=audit.penalized,
            dropped_validation=audit.dropped_validation,
            skipped=dict(audit.skipped),
        ),
    )


def _iter_sections(payload: dict[str, object]):
    """Yield (outer_key, section_body) for every list-valued top-level section."""
    for key, value in payload.items():
        if not isinstance(value, list):
            continue
        yield key, cast("list[dict[str, object]]", value)


def _walk_section(
    section_key: str,
    section: list[dict[str, object]],
    per_period: dict[datetime, list[KpiValue]],
    audit: CaptureAudit,
    fye_md: tuple[int, int] | None,
) -> None:
    """Capture monetary cells from one section into ``per_period`` (mutates audit)."""
    if not section:
        return
    inner_title = _inner_title(section) or section_key
    if not _is_detail_section(inner_title):
        return
    units = parse_units(inner_title)
    scale = units.scale
    if scale not in _SCALE_FACTOR:
        # No trustworthy magnitude scale → defer the whole section to Stage B.
        audit.skip("section_no_scale_hint")
        return
    if _is_unit_ambiguous_section(inner_title):
        # Per-share / share-count / equity-rollforward → unit-mixed → defer wholesale.
        audit.skip("section_unit_ambiguous")
        return

    audit.sections_detail += 1
    semantic = _semantic_section_title(inner_title)
    factor = _SCALE_FACTOR[scale]

    rows = list(iter_xbrl_table(section))
    if not rows:
        return
    # Map each period column index -> a parsed period_end, keeping only columns
    # that land on the fiscal-year-end (annual). None = drop (not a date, or
    # off-cycle interim column).
    period_ends = [_accept_period(_parse_period(lbl), fye_md) for lbl in rows[0].period_labels]

    for row in rows:
        audit.rows_seen += 1
        name = _build_name(semantic, row.axis_path, row.label)
        if not name:
            continue
        for col, raw in enumerate(row.values):
            if raw is None:
                continue
            audit.cells_seen += 1
            if col >= len(period_ends) or period_ends[col] is None:
                audit.skip("off_cycle_or_unparseable_period")
                continue
            value, outcome, confidence = _classify_value(row.label, raw, factor)
            if value is None:
                audit.skip(outcome)
                continue
            if confidence < 1.0:
                audit.penalized += 1
            period_end = cast("datetime", period_ends[col])
            excerpt = _excerpt(row.label, raw, units.currency, scale)
            column_header = row.period_labels[col] if col < len(row.period_labels) else None
            per_period[period_end].append(
                KpiValue(
                    name=name,
                    value=value,
                    unit=Unit.ACTUAL,
                    # 1.0 for a normal monetary cell (deterministic table read); a
                    # haircut for a percent/ratio-shaped residual-risk magnitude
                    # (see _classify_value / _RESIDUAL_RISK_*). persist_manifest
                    # folds this self-report into the stored confidence.
                    confidence=confidence,
                    source_excerpt=excerpt,
                    # row.label / row.axis_path / the column's period label are
                    # ALL already local variables at this call site — persisting
                    # them into the locator (docs/design/provenance_clickthrough.md
                    # §3.2) is pure enrichment, no new extraction logic.
                    locator=locators.table_cell_locator(
                        section=section_key,
                        table_title=inner_title,
                        row_label=row.label,
                        row_axis_path=row.axis_path,
                        column_header=column_header,
                        json_path=None,
                        cell_value_as_extracted=str(raw),
                        verbatim_snippet=excerpt,
                    ),
                )
            )


def _inner_title(section: Sequence[object]) -> str | None:
    """First key of the section header dict — the FULL title (the outer payload
    key is truncated to ~30 chars and loses the '(Details)' + unit suffix).
    Tolerant of a malformed first element (a stray non-dict in the JSON)."""
    if not section or not isinstance(section[0], dict):
        return None
    keys = list(cast("dict[str, object]", section[0]).keys())
    return keys[0] if keys else None


def _is_detail_section(inner_title: str) -> bool:
    """True for an XBRL note ``(Details)`` table that isn't on the non-financial
    denylist. Primary statements / narrative / templates lack '(Details)'."""
    low = inner_title.lower()
    if "(details)" not in low:
        return False
    return not any(tok in low for tok in _NONFINANCIAL_SECTION_TOKENS)


def _excerpt(label: str, raw: object, currency: str, scale: str) -> str:
    """Compact provenance string for the source cell (clipped by persist)."""
    return f"{label}: {raw} [{currency} {scale}]"[:1024]


def _detect_fye_monthday(payload: dict[str, object]) -> tuple[int, int] | None:
    """The fiscal-year-end as a (month, day) — the MODAL column date across all
    capturable detail tables. None when no detail column parses to a date.

    A 10-K's annual columns share the issuer's FYE (Dec-31, or Jan-31 for a
    Jan-FYE filer); interim columns (a stray quarter-end, a subsequent-event
    date) are the minority, so the mode is the FYE. Self-contained — no need to
    know the issuer's calendar a priori.
    """
    counts: dict[tuple[int, int], int] = {}
    for key, section in _iter_sections(payload):
        if not section:
            continue
        inner = _inner_title(section) or key
        if not _is_detail_section(inner) or _is_unit_ambiguous_section(inner):
            continue
        if parse_units(inner).scale not in _SCALE_FACTOR:
            continue
        rows = list(iter_xbrl_table(section))
        if not rows:
            continue
        for lbl in rows[0].period_labels:
            d = _parse_period(lbl)
            if d is not None:
                counts[(d.month, d.day)] = counts.get((d.month, d.day), 0) + 1
    if not counts:
        return None
    # Most frequent (month, day); ties broken deterministically (latest in year).
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _accept_period(parsed: datetime | None, fye_md: tuple[int, int] | None) -> datetime | None:
    """Keep ``parsed`` only when it lands on the fiscal-year-end (month, day).
    When FYE detection failed (fye_md is None), accept any parsed date."""
    if parsed is None:
        return None
    if fye_md is not None and (parsed.month, parsed.day) != fye_md:
        return None
    return parsed


def _resolve_doc_id(
    conn: sqlite3.Connection, *, ticker: str, fiscal_year: int, repo_root: Path
) -> int | None:
    """documents.id for this ticker's fmp_10k_json, matched on the file_path
    natural key (period_end is NULL on these rows). Mirrors
    ``compute.segment_crosstabs_llm._lookup_doc_id`` (PR #148)."""
    try:
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='documents'"
        ).fetchone()
    except sqlite3.Error:
        return None
    if present is None:
        return None

    fname = f"{ticker.upper()}_form_10k_{fiscal_year}.json"
    rel = (repo_root / "data" / "historical" / "fmp" / fname).as_posix()
    rel_repo = f"data/historical/fmp/{fname}"
    for path in {rel, rel_repo}:
        try:
            row = conn.execute(
                "SELECT id FROM documents "
                "WHERE ticker = ? AND doc_type = 'fmp_10k_json' AND file_path = ? LIMIT 1",
                (ticker.upper(), path),
            ).fetchone()
        except sqlite3.Error:
            row = None
        if row is not None:
            return int(row[0])
    # Filename-suffix fallback for repo-root variations.
    try:
        row = conn.execute(
            "SELECT id FROM documents "
            "WHERE ticker = ? AND doc_type = 'fmp_10k_json' AND file_path LIKE ? "
            "ORDER BY id DESC LIMIT 1",
            (ticker.upper(), f"%{fname}"),
        ).fetchone()
    except sqlite3.Error:
        row = None
    return int(row[0]) if row is not None else None
