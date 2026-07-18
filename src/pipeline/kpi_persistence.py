"""Persist structured KPI extractions from IR documents into kpi_facts + validation_issues.

This module is the interface between (a) an LLM-extracted manifest of KPI values
and (b) the provenance-tagged kpi_facts table. The LLM produces a typed payload
(KpiExtractionManifest); this module validates it, looks up / creates the
matching kpi_definitions row per (ticker, name), inserts kpi_facts rows tagged
with source_doc_id, and writes validation_issues for anything that fails
sanity checks (range bounds, missing units, etc.).

Single source of truth for the data contract — no other module should INSERT
directly into kpi_facts.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from compute.kpi_resolver import canonical_metric_name, normalize_kpi_name
from credibility.observations import KPI_FACTS, record_restatement_observation
from models.documents import SourceType, tier_for_source_type
from models.facts import FactLocator, FiscalPeriodType, LegacyEscapeHatch, Unit
from models.kpis import DefinitionOrigin, ReportingCadence, ThesisTier
from models.unit_convert import convert_unit
from models.validation import Severity, ValidationRule
from pipeline.confidence import score_confidence
from pipeline.restatement_detector import insert_kpi_with_restatement_detection

log = logging.getLogger(__name__)

# kpi_facts.source_excerpt is VARCHAR(1024) (alembic 0033) — clip on write so
# an over-eager extractor can't push a page of context into the column.
_SOURCE_EXCERPT_MAX = 1024

# A captured ACTUAL (whole-currency-unit) fact above this magnitude is almost
# certainly a mis-scale, not a real line item. The Stage-A XBRL walker expands a
# cell by its section scale (x1e3/1e6/1e9), and a count/ratio that slipped the
# label denylist gets the x1e6 treatment too — a billions-scale share count
# (NU's ~4.6e9 shares) becomes ~$4.6e15. No single reported financial line item
# reaches this magnitude (the largest legit single cells — mega-bank total assets
# and gross derivative notionals — top out near 1e14), so a value past the cap is
# dropped-with-log (a PLAUSIBLE_RANGE issue) rather than stored at face confidence.
# The narrower count/percent traps are deferred upstream (generic_xbrl_capture);
# this is the persist-time backstop for what slips through into ANY ACTUAL fact
# (Stage A or the Stage-B enumerate path), the unit the existing PERCENT/RATIO/BPS
# guards left unchecked.
_ACTUAL_PLAUSIBLE_MAX = Decimal("1e15")


class KpiValue(BaseModel):
    """One LLM-extracted KPI value tied to a tracked metric."""

    name: str = Field(min_length=1, max_length=200)
    value: Decimal
    unit: Unit
    confidence: float = Field(ge=0.0, le=1.0, default=0.95)
    # Verbatim snippet from the source document that supports the value —
    # lands in kpi_facts.source_excerpt (clipped to _SOURCE_EXCERPT_MAX).
    source_excerpt: str | None = None
    # Sub-document position (alembic 0075) — e.g. FactLocator(pdf_page=7) for
    # an IR-deck extraction, or transcript_line for a transcript-anchored one.
    # Required (no None default) as of the persist-time enforcement flip
    # (docs/design/provenance_clickthrough.md §4.1): every caller supplies a
    # real FactLocator or an explicit LegacyEscapeHatch(reason=...) — see
    # pipeline.locators.resolve_locator_for_persist, called from
    # persist_manifest below, for how the two branches serialize at the
    # actual kpi_facts INSERT.
    locator: FactLocator | LegacyEscapeHatch


class KpiExtractionManifest(BaseModel):
    """A batch of KPI values extracted from one IR document.

    `source_doc_id` is the documents.id of the IR PDF the LLM read; provenance
    chains every emitted kpi_facts row back to that document.

    `model_name` is the LLM model id used for extraction (e.g.
    `'claude-haiku-4-5-20251001'`); persisted into kpi_facts.extracted_by
    as `'llm:<model_name>'` so audit queries can group by extractor.
    Optional — when None, persist_manifest tags rows as plain `'llm'`.
    """

    ticker: str
    period_end: datetime
    fiscal_period_type: FiscalPeriodType
    source_doc_id: int
    primary_source: SourceType = SourceType.IR_DOC
    model_name: str | None = None
    # Override for kpi_facts.extracted_by. Default (None) keeps the LLM-derived
    # 'llm[:model]' tag; deterministic sources (e.g. IR-spreadsheet parsing) set
    # an explicit tag like 'ir_spreadsheet' so audits don't mislabel them as LLM.
    extracted_by: str | None = None
    # Who authored the kpi_definitions rows these values land in. The default
    # ANALYST is the curated-watchlist path (tier_*_kpis): names are stored
    # VERBATIM under their exact watchlist spelling. The capture-every-number
    # extractors set CAPTURE — which switches persist_manifest into capture mode:
    # each value's name is routed through ``compute.kpi_resolver.canonical_metric_name``
    # (collapsing unit/casing/whitespace variants onto an existing definition,
    # minting a clean name otherwise) and minted definitions are stamped CAPTURE so
    # the long tail stays distinguishable from the analyst registry (migration 0113).
    origin: DefinitionOrigin = DefinitionOrigin.ANALYST
    # Authoritative per-KPI unit, keyed by KpiValue.name. Populated by callers
    # that know the canonical unit a metric SHOULD be stored in — the LLM path
    # fills it from each holding's break-rule `unit` (the unit its threshold is
    # expressed in). When a name is present here, persist_manifest reconciles the
    # extracted value/unit to it (see `_reconcile_unit`) rather than trusting the
    # LLM's per-call unit guess, and treats the unit as authoritative for the
    # kpi_definitions row. Absent name → the extracted unit is used unchanged, so
    # generic callers (IR spreadsheet/PDF) keep their existing behavior.
    canonical_units: dict[str, Unit] = Field(default_factory=dict)
    # Authoritative per-KPI reporting cadence, keyed by KpiValue.name. Populated
    # by callers that know a metric's native frequency — an annual-only 20-F/10-K
    # figure (bank capital ratios) sets ANNUAL so the definition is marked
    # cadence-aware (see `find_or_create_kpi_definition`). Absent name → the
    # definition keeps its cadence (defaults to QUARTERLY for a brand-new row).
    # When a value IS annual, the caller must also set `fiscal_period_type=FY`
    # (with a fiscal-year-end `period_end`) so the fact lands on the annual axis.
    cadences: dict[str, ReportingCadence] = Field(default_factory=dict)
    # PER-NAME definition origin, keyed by KpiValue.name — for a MIXED manifest
    # that carries both curated and long-tail rows in one batch (S4's IR-spreadsheet
    # ingest: a few analyst tier KPIs + the captured long tail). It OVERRIDES the
    # manifest-level `origin` for the names it lists; a name absent here falls back
    # to `origin`. Like `origin`, it is honoured ONLY when a row is first created —
    # a re-encounter never rewrites an existing definition's origin. A caller using
    # this path canonicalizes names ITSELF before persisting (it leaves the
    # manifest-level `origin` at ANALYST, so persist_manifest's capture-mode
    # name-collapse is a no-op and the already-canonical names are stored verbatim).
    origins: dict[str, DefinitionOrigin] = Field(default_factory=dict)
    values: list[KpiValue]


def _find_kpi_definition(conn: sqlite3.Connection, ticker: str, name: str) -> int | None:
    """Return kpi_definitions.id for (ticker, name), or None if not registered."""
    cur = conn.execute(
        "SELECT id FROM kpi_definitions WHERE ticker = ? AND name = ?",
        (ticker.upper(), name),
    )
    row = cur.fetchone()
    return int(row["id"]) if row is not None else None


def _create_kpi_definition(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    name: str,
    unit: Unit,
    primary_source: SourceType,
    threshold_tier: ThesisTier | None = None,
    origin: DefinitionOrigin = DefinitionOrigin.ANALYST,
) -> int:
    """Register a new kpi_definitions row; returns the new id.

    ``origin`` is stamped ONCE here (default ``ANALYST`` — the curated watchlist).
    The capture-every-number extractors pass ``CAPTURE`` so the long tail is
    distinguishable from the analyst registry. Schema-defensive: the
    ``definition_origin`` column (migration 0113) is only written when present, so
    persistence stays runnable on a pre-0113 / minimal test DB (the column then
    backfills to its ``'analyst'`` default on migrate).
    """
    cols = [
        "ticker",
        "name",
        "unit",
        "primary_source",
        "fallback_source",
        "ir_url",
        "threshold_tier",
        "threshold_low",
        "threshold_high",
        "notes",
    ]
    vals: list[object | None] = [
        ticker.upper(),
        name,
        unit.value,
        primary_source.value,
        None,
        None,
        threshold_tier.value if threshold_tier is not None else None,
        None,
        None,
        None,
    ]
    if _kpi_definitions_has_column(conn, "definition_origin"):
        cols.append("definition_origin")
        vals.append(origin.value)
    placeholders = ", ".join("?" * len(vals))
    cur = conn.execute(
        f"INSERT INTO kpi_definitions ({', '.join(cols)}) VALUES ({placeholders})",
        vals,
    )
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def find_or_create_kpi_definition(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    name: str,
    unit: Unit,
    primary_source: SourceType,
    authoritative: bool = False,
    reporting_cadence: ReportingCadence | None = None,
    origin: DefinitionOrigin = DefinitionOrigin.ANALYST,
) -> int:
    """Lookup-or-insert kpi_definitions for (ticker, name); returns the id.

    Unit on the row is otherwise first-writer-wins: whichever extraction created
    the definition stamped its (possibly wrong) unit, and later extractions never
    correct it, so the definition's unit drifts from the metric's true unit
    (e.g. NU "Capital adequacy ratio" stamped ``ratio`` though every fact and the
    break-rule are ``percent``). When ``authoritative`` is set the caller has a
    trusted canonical unit (the holding's break-rule unit) — reconcile the
    existing row to it so the definition's metadata stops drifting. ``unit`` is
    used verbatim for a brand-new row regardless of the flag.

    ``reporting_cadence`` (when provided) is likewise authoritative: a caller that
    knows a metric is annual-only (a 20-F/10-K capital ratio) stamps ANNUAL so the
    definition is marked cadence-aware whether it already existed or is created
    here. A brand-new row otherwise defaults to QUARTERLY (the column default).

    ``origin`` (default ``ANALYST``) is stamped only on a BRAND-NEW row and never
    rewrites an existing one — an analyst-authored definition stays ``ANALYST``
    even when the capture pass later re-encounters it. The capture-every-number
    extractors pass ``CAPTURE`` (after canonicalizing the name via
    ``compute.kpi_resolver.canonical_metric_name``) so the long tail is
    distinguishable from the curated watchlist (migration 0113).
    """
    existing = _find_kpi_definition(conn, ticker, name)
    if existing is not None:
        if authoritative:
            _reconcile_definition_unit(conn, existing, unit)
        if reporting_cadence is not None:
            _reconcile_definition_cadence(conn, existing, reporting_cadence)
        return existing
    new_id = _create_kpi_definition(
        conn, ticker=ticker, name=name, unit=unit, primary_source=primary_source, origin=origin
    )
    if reporting_cadence is not None:
        _reconcile_definition_cadence(conn, new_id, reporting_cadence)
    return new_id


def _reconcile_definition_unit(
    conn: sqlite3.Connection, kpi_definition_id: int, unit: Unit
) -> None:
    """Correct an existing definition's unit to the authoritative ``unit``.

    Idempotent: the ``unit != ?`` guard makes it a no-op when the row already
    carries the canonical unit, so the common (already-correct) path writes
    nothing.
    """
    conn.execute(
        "UPDATE kpi_definitions SET unit = ? WHERE id = ? AND unit != ?",
        (unit.value, kpi_definition_id, unit.value),
    )


def _kpi_definitions_has_column(conn: sqlite3.Connection, column: str) -> bool:
    """True iff PRAGMA reports ``column`` on kpi_definitions. Used to no-op the
    cadence reconcile on a pre-0072 / minimal test DB that lacks the column."""
    return any(
        str(r["name"] if hasattr(r, "keys") else r[1]) == column
        for r in conn.execute("PRAGMA table_info(kpi_definitions)").fetchall()
    )


def _reconcile_definition_cadence(
    conn: sqlite3.Connection, kpi_definition_id: int, cadence: ReportingCadence
) -> None:
    """Set an existing definition's reporting_cadence to the authoritative
    ``cadence``.

    Idempotent (the ``reporting_cadence != ?`` guard no-ops when already correct)
    and schema-defensive: silently skips when the column is absent (pre-0072 or a
    minimal test DB) so persistence stays runnable on a stale schema, mirroring
    the restatement detector's column-tolerance.
    """
    if not _kpi_definitions_has_column(conn, "reporting_cadence"):
        return
    conn.execute(
        "UPDATE kpi_definitions SET reporting_cadence = ? WHERE id = ? AND reporting_cadence != ?",
        (cadence.value, kpi_definition_id, cadence.value),
    )


def _reconcile_unit(
    value: Decimal, extracted_unit: Unit, canonical: Unit | None
) -> tuple[Decimal, Unit, bool]:
    """Reconcile an extracted (value, unit) to the KPI's canonical unit.

    Returns ``(value, unit, mismatch)``:

    - No canonical, or canonical already equals the extracted unit → the value
      and unit pass through unchanged, ``mismatch=False``.
    - Canonical differs and is dimensionally compatible (same family) → the value
      is rescaled into the canonical unit and returned with it, ``mismatch=False``.
    - Canonical differs across families (no valid conversion) → the *original*
      value/unit pass through (never rescaled across dimensions) and
      ``mismatch=True`` so the caller can flag it for review.
    """
    if canonical is None or canonical is extracted_unit:
        return value, extracted_unit, False
    converted = convert_unit(value, extracted_unit, canonical)
    if converted is None:
        return value, extracted_unit, True
    return converted, canonical, False


# ---------------------------------------------------------------------------
# Cumulative-series sanity guard (red-team PR2 item 4,
# directives/monthly_red_team.md Phase 1 "KPI series sanity"): a KPI marked
# cumulative (never-decreasing, e.g. a running customer count) is checked
# against its chronological neighbors at persist time. A decrease, or a
# >1000x unit-scale jump between adjacent prints (a raw-count row landing
# inside a millions-scale series — the exact NU "Total customers" def 641
# corruption the audit found), is REJECTED rather than silently written. This
# is a small explicit allowlist (matched on the normalized name, so "Total
# customers", "Total customers (millions)", and per-ticker variants all
# match) rather than a new kpi_definitions column — extend the set when a new
# cumulative-count metric needs the guard.
# ---------------------------------------------------------------------------

_CUMULATIVE_KPI_NAME_MARKERS: frozenset[str] = frozenset({"total customers"})

_UNIT_JUMP_RATIO = Decimal("1000")


def _is_cumulative_kpi(name: str) -> bool:
    normalized = normalize_kpi_name(name)
    return any(marker in normalized for marker in _CUMULATIVE_KPI_NAME_MARKERS)


def _decimal_unit_jump(a: Decimal, b: Decimal, *, ratio_limit: Decimal = _UNIT_JUMP_RATIO) -> bool:
    """True when `b` is a >ratio_limit× jump from `a` in either direction."""
    lo, hi = sorted((abs(a), abs(b)))
    if lo == 0:
        return hi > 0 and hi > ratio_limit
    return hi / lo > ratio_limit


def _cumulative_guard_violation(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    kpi_definition_id: int,
    period_end: datetime,
    value: Decimal,
) -> str | None:
    """Return a violation reason if writing `value` at `period_end` would break
    monotonicity or introduce a >1000x unit-scale jump versus this cumulative
    KPI's chronologically-adjacent prints already on file. None = guard clears.

    Checks both neighbors (not just "latest") so a backfill anywhere in the
    timeline — not only an append at the end — is guarded.
    """
    rows = conn.execute(
        "SELECT period_end, value FROM kpi_facts "
        "WHERE ticker = ? AND kpi_definition_id = ? ORDER BY period_end",
        (ticker.upper(), kpi_definition_id),
    ).fetchall()
    prev_v: Decimal | None = None
    prev_pe: str | None = None
    next_v: Decimal | None = None
    next_pe: str | None = None
    for row in rows:
        row_pe_raw = row["period_end"]
        row_pe = (
            row_pe_raw
            if isinstance(row_pe_raw, datetime)
            else datetime.fromisoformat(str(row_pe_raw))
        )
        if row_pe < period_end:
            prev_v, prev_pe = Decimal(str(row["value"])), row_pe.date().isoformat()
        elif row_pe > period_end and next_v is None:
            next_v, next_pe = Decimal(str(row["value"])), row_pe.date().isoformat()
    if prev_v is not None:
        if value < prev_v:
            return (
                f"non-monotonic: {value} at {period_end.date().isoformat()} < "
                f"prior {prev_v} at {prev_pe} — cumulative series should not decrease"
            )
        if _decimal_unit_jump(prev_v, value):
            return (
                f"unit discontinuity: {prev_v} at {prev_pe} -> {value} at "
                f"{period_end.date().isoformat()} (>{_UNIT_JUMP_RATIO}x jump)"
            )
    if next_v is not None:
        if next_v < value:
            return (
                f"non-monotonic: next print {next_v} at {next_pe} < "
                f"{value} at {period_end.date().isoformat()} — cumulative series should not decrease"
            )
        if _decimal_unit_jump(value, next_v):
            return (
                f"unit discontinuity: {value} at {period_end.date().isoformat()} -> "
                f"{next_v} at {next_pe} (>{_UNIT_JUMP_RATIO}x jump)"
            )
    return None


def _insert_kpi_fact(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    period_end: datetime,
    fiscal_period_type: FiscalPeriodType,
    kpi_definition_id: int,
    value: Decimal,
    unit: Unit,
    source_doc_id: int,
    confidence: float = 1.0,
    extracted_by: str | None = None,
    locator: str | None = None,
    source_excerpt: str | None = None,
) -> bool:
    """Insert one kpi_facts row, routed through the restatement detector.

    Returns True iff a row was actually written. Returns False on the
    no-op path: same source_doc_id replay under the post-0059
    `uq_kpi_facts_provenance` (or same logical key under the legacy
    `uq_kpi_facts_logical`).

    When a different source_doc_id targets an existing logical key AND the
    new document is strictly later than the incumbent's, the detector
    writes the new row with `supersedes_id` pointing at the incumbent so
    the tier+id-aware loader picks the restated value while the original
    survives for time-travel queries.
    """
    new_id, superseded_id = insert_kpi_with_restatement_detection(
        conn,
        ticker=ticker,
        period_end=period_end,
        fiscal_period_type=fiscal_period_type.value,
        kpi_definition_id=kpi_definition_id,
        value=value,
        unit=unit.value,
        source_doc_id=source_doc_id,
        confidence=confidence,
        extracted_by=extracted_by,
        locator=locator,
        source_excerpt=source_excerpt,
    )
    # L10: capture the restatement instead of discarding superseded_id.
    if superseded_id is not None:
        _ = record_restatement_observation(
            conn, fact_table=KPI_FACTS, superseded_id=superseded_id, new_value=value
        )
    return new_id is not None


def purge_duplicate_kpi_facts(conn: sqlite3.Connection) -> int:
    """Delete kpi_facts rows that share (ticker, period_end, fiscal_period_type,
    kpi_definition_id) with a row having a higher source_doc_id; keep only the
    latest-source_doc_id row per logical tuple. Returns the number of rows deleted.

    Used by migration 0030 to backfill legacy duplicate rows before the narrower
    UNIQUE index `uq_kpi_facts_logical` can be applied. Safe to re-run: once the
    table is clean, the EXISTS clause matches nothing and the call is a no-op.
    """
    cur = conn.execute(
        "DELETE FROM kpi_facts "
        "WHERE EXISTS ("
        "  SELECT 1 FROM kpi_facts other "
        "  WHERE other.ticker = kpi_facts.ticker "
        "    AND other.period_end = kpi_facts.period_end "
        "    AND other.fiscal_period_type = kpi_facts.fiscal_period_type "
        "    AND other.kpi_definition_id = kpi_facts.kpi_definition_id "
        "    AND other.source_doc_id > kpi_facts.source_doc_id"
        ")"
    )
    return cur.rowcount


def record_validation_issue(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    source_doc_id: int | None,
    ticker: str | None,
    severity: Severity,
    rule: ValidationRule,
    raw_value: str | None,
    expected: str | None,
) -> int:
    """Insert one validation_issues row; returns its id."""
    cur = conn.execute(
        "INSERT INTO validation_issues "
        "(run_id, source_doc_id, ticker, severity, rule, raw_value, expected, "
        " raised_at, resolved_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        (
            run_id,
            source_doc_id,
            ticker.upper() if ticker is not None else None,
            severity.value,
            rule.value,
            raw_value,
            expected,
            datetime.now(),
        ),
    )
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def guard_llm_extracted_parent(
    conn: sqlite3.Connection,
    *,
    source_type: SourceType,
    parent_document_id: int | None,
    ticker: str,
    doc_id: int,
    run_id: str | None = None,
    strict: bool = False,
) -> None:
    """Enforce data_provenance.md §2 at every ``llm_extracted`` documents insert.

    A no-op unless ``source_type`` is ``LLM_EXTRACTED`` and ``parent_document_id``
    is still unset. Default (``strict=False`` — every production write path)
    degrades: logs a WARN ``validation_issues`` row and lets the caller continue,
    matching this module's existing quarantine philosophy (a batch ingestion run
    is never aborted over one row's provenance gap). Pass ``strict=True`` (tests,
    or a deliberate direct/manual insert) to raise immediately instead.
    """
    if source_type is not SourceType.LLM_EXTRACTED or parent_document_id is not None:
        return
    if strict:
        raise ValueError(
            f"{ticker}: llm_extracted document {doc_id} inserted without "
            "parent_document_id (directives/data_provenance.md §2)"
        )
    record_validation_issue(
        conn,
        run_id=run_id or "document_guard",
        source_doc_id=doc_id,
        ticker=ticker,
        severity=Severity.WARN,
        rule=ValidationRule.MISSING_FIELD,
        raw_value="parent_document_id",
        expected="non-null parent_document_id required for llm_extracted documents (data_provenance.md §2)",
    )


def _validate_value_range(value: Decimal, unit: Unit) -> tuple[bool, str | None]:
    """Sanity-check a (value, unit) pair. Returns (is_ok, reason_if_not)."""
    if unit is Unit.PERCENT and (value < Decimal("-1000") or value > Decimal("1000")):
        return (False, f"percent={value} outside plausible range [-1000, 1000]")
    if unit is Unit.RATIO and (value < Decimal("-100") or value > Decimal("100")):
        return (False, f"ratio={value} outside plausible range [-100, 100]")
    if unit is Unit.BPS and (value < Decimal("-100000") or value > Decimal("100000")):
        return (False, f"bps={value} outside plausible range")
    if unit is Unit.ACTUAL and abs(value) > _ACTUAL_PLAUSIBLE_MAX:
        return (
            False,
            f"actual={value} exceeds plausible magnitude cap {_ACTUAL_PLAUSIBLE_MAX:.0e} "
            "(likely a count/ratio mis-scaled by the section unit)",
        )
    return (True, None)


class PersistResult(BaseModel):
    """Per-manifest application outcome."""

    inserted: int
    skipped_existing: int
    validation_issues: int


def persist_manifest(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    manifest: KpiExtractionManifest,
) -> PersistResult:
    """Apply one KpiExtractionManifest. Validates each value, inserts kpi_facts,
    emits validation_issues for failures, and returns a per-manifest tally."""
    # Local import: pipeline.locators imports record_validation_issue FROM
    # this module, so a module-level `from pipeline import locators` here
    # would be circular. By call time both modules are fully loaded.
    from pipeline.locators import resolve_locator_for_persist

    inserted = 0
    skipped = 0
    issues = 0
    extracted_by = manifest.extracted_by or (
        f"llm:{manifest.model_name}" if manifest.model_name else "llm"
    )

    for kpi in manifest.values:
        # Reconcile the extracted value/unit to the KPI's canonical unit (the
        # holding's break-rule unit) BEFORE validating/storing, so kpi_facts is
        # written in the unit its threshold is compared against — not the LLM's
        # per-call guess. A cross-family canonical can't be applied: keep the
        # value as-extracted and flag it rather than rescale across dimensions.
        canonical = manifest.canonical_units.get(kpi.name)
        value, unit, unit_mismatch = _reconcile_unit(kpi.value, kpi.unit, canonical)
        if unit_mismatch:
            record_validation_issue(
                conn,
                run_id=run_id,
                source_doc_id=manifest.source_doc_id,
                ticker=manifest.ticker,
                severity=Severity.WARN,
                rule=ValidationRule.UNIT_MISMATCH,
                raw_value=f"{kpi.name}={kpi.value} {kpi.unit.value}",
                expected=(
                    f"unit convertible to {canonical.value}" if canonical is not None else None
                ),
            )
            issues += 1

        ok, reason = _validate_value_range(value, unit)
        if not ok:
            record_validation_issue(
                conn,
                run_id=run_id,
                source_doc_id=manifest.source_doc_id,
                ticker=manifest.ticker,
                severity=Severity.WARN,
                rule=ValidationRule.PLAUSIBLE_RANGE,
                raw_value=f"{kpi.name}={value} {unit.value}",
                expected=reason,
            )
            issues += 1
            continue

        # Capture mode (origin=CAPTURE): collapse the raw label onto an existing
        # definition (or mint a clean canonical) BEFORE find_or_create, so the
        # long tail doesn't explode the registry with unit/casing variants. The
        # resolve reads live state on this same connection, so a name minted for
        # an earlier value in this manifest is reused by a later equivalent one
        # (within-batch dedup). The analyst path stores names verbatim.
        store_name = (
            canonical_metric_name(conn, manifest.ticker, kpi.name, unit)
            if manifest.origin is DefinitionOrigin.CAPTURE
            else kpi.name
        )
        kpi_def_id = find_or_create_kpi_definition(
            conn,
            ticker=manifest.ticker,
            name=store_name,
            unit=unit,
            primary_source=manifest.primary_source,
            # A successfully-applied canonical is authoritative for the
            # definition's unit too; a cross-family mismatch is not (we kept the
            # extracted unit), so don't let it overwrite the definition.
            authoritative=canonical is not None and not unit_mismatch,
            # Mark the definition's native frequency when the caller declared it
            # (annual-only 20-F/10-K metrics). Absent → cadence left as-is.
            reporting_cadence=manifest.cadences.get(kpi.name),
            # Stamp who authored a BRAND-NEW definition; never rewrites an existing
            # row (an analyst def re-seen by capture stays ANALYST). A per-name
            # `origins` entry (S4's mixed analyst+capture spreadsheet manifests)
            # wins; otherwise the manifest-level `origin` (S3's single-origin
            # manifests), which defaults to ANALYST.
            origin=manifest.origins.get(kpi.name, manifest.origin),
        )

        # Cumulative-series sanity guard: reject (never guess-fix) a write that
        # would break monotonicity or introduce a unit-scale jump on a KPI
        # marked cumulative. Logged loud to stderr per the schema-drift rule —
        # the fix belongs in the data (execution/fix_kpi_series.py), not a
        # silent persist-time rescale.
        if _is_cumulative_kpi(store_name):
            violation = _cumulative_guard_violation(
                conn,
                ticker=manifest.ticker,
                kpi_definition_id=kpi_def_id,
                period_end=manifest.period_end,
                value=value,
            )
            if violation is not None:
                log.error(
                    {
                        "event": "cumulative_kpi_guard_violation",
                        "ticker": manifest.ticker,
                        "kpi_name": store_name,
                        "period_end": manifest.period_end.date().isoformat(),
                        "value": str(value),
                        "reason": violation,
                    }
                )
                record_validation_issue(
                    conn,
                    run_id=run_id,
                    source_doc_id=manifest.source_doc_id,
                    ticker=manifest.ticker,
                    severity=Severity.WARN,
                    rule=(
                        ValidationRule.NON_MONOTONIC_CUMULATIVE
                        if "non-monotonic" in violation
                        else ValidationRule.MAGNITUDE_JUMP
                    ),
                    raw_value=f"{store_name}={value} at {manifest.period_end.date().isoformat()}",
                    expected=violation,
                )
                issues += 1
                continue

        excerpt = kpi.source_excerpt.strip()[:_SOURCE_EXCERPT_MAX] if kpi.source_excerpt else None
        was_inserted = _insert_kpi_fact(
            conn,
            ticker=manifest.ticker,
            period_end=manifest.period_end,
            fiscal_period_type=manifest.fiscal_period_type,
            kpi_definition_id=kpi_def_id,
            value=value,
            unit=unit,
            source_doc_id=manifest.source_doc_id,
            # The deterministic tier+method prior scaled by the extractor's
            # per-value self-report (pipeline.confidence's documented formula) —
            # an LLM readout of an IR deck no longer stores a flat 0.95.
            confidence=score_confidence(
                tier=tier_for_source_type(manifest.primary_source),
                extracted_by=extracted_by,
                self_reported=kpi.confidence,
            ),
            extracted_by=extracted_by,
            locator=resolve_locator_for_persist(
                conn,
                locator=kpi.locator,
                run_id=run_id,
                source_doc_id=manifest.source_doc_id,
                ticker=manifest.ticker,
            ),
            source_excerpt=excerpt or None,
        )
        if was_inserted:
            inserted += 1
        else:
            skipped += 1

    conn.commit()
    return PersistResult(inserted=inserted, skipped_existing=skipped, validation_issues=issues)
