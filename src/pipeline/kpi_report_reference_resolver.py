"""Evidence-bound proposals and fail-closed reads for report KPI references."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from enum import StrEnum
from pathlib import Path
from typing import TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field

from compute.kpi_resolver import normalize_kpi_name
from models.facts import Unit
from pipeline.kpi_report_reference_dispositions import (
    ReportKpiReference,
    ReportKpiReferenceDisposition,
    ReportKpiReferenceDispositionRevision,
    ReportKpiReferenceInventory,
    ReportKpiReferenceResolutionMethod,
    ReportKpiReferenceSourceState,
    ReportKpiReferenceSourceStatus,
    ReportKpiReferenceStatus,
    current_report_kpi_reference_disposition,
    load_report_kpi_reference_inventory,
)
from pipeline.kpi_semantics import KpiUnitScale, validate_admitted_unit_scale
from provenance.financial_fact_resolution import canonical_fact_relation

POLICY_NAME = "report_kpi_reference_resolution"
POLICY_VERSION = "v2"


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


POLICY_CONFIG_SHA256 = _canonical_sha256(
    {
        "candidate_key": "conservative_unit_surface_normalization",
        "candidate_cardinality": "exactly_one_definition",
        "evidence": "canonical_current_fact_and_current_admitted_context",
        "lane": "current_actual",
        "overrides": "active_replace_or_drop_blocks",
        "reader": "reconstruct_and_compare_all_fingerprints",
    }
)


class ReportKpiReferenceProposalOutcome(StrEnum):
    CANDIDATE = "candidate"
    NONE = "none"
    AMBIGUOUS = "ambiguous"
    BLOCKED = "blocked"


class ReportKpiReferenceResolutionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reference: ReportKpiReference
    outcome: ReportKpiReferenceProposalOutcome
    reason_code: str = Field(min_length=1, max_length=128)
    candidate_definition_ids: tuple[int, ...]
    proposed_disposition: ReportKpiReferenceDisposition | None = None


class ResolvedReportKpiReferenceBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    resolution_id: int = Field(gt=0)
    kpi_definition_id: int = Field(gt=0)
    definition_name: str = Field(min_length=1, max_length=256)
    evidence_fact_id: int = Field(gt=0)
    evidence_context_id: int = Field(gt=0)
    resolution_method: ReportKpiReferenceResolutionMethod


class VerifiedReportKpiReferenceDefinition(BaseModel):
    """Definition identity that a report consumer may safely read."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kpi_definition_id: int = Field(gt=0)
    definition_name: str = Field(min_length=1, max_length=256)
    resolution_id: int | None = Field(default=None, gt=0)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _row_dict(cursor: sqlite3.Cursor, row: object) -> dict[str, object]:
    if isinstance(row, sqlite3.Row):
        return cast("dict[str, object]", dict(row))
    if not isinstance(row, tuple) or cursor.description is None:
        raise TypeError("report KPI resolution query returned an invalid row")
    description = cast("tuple[tuple[object, ...], ...]", cursor.description)
    values = cast("tuple[object, ...]", row)
    return dict(zip((str(column[0]) for column in description), values, strict=True))


def _as_int(value: object) -> int:
    if not isinstance(value, (int, str)):
        raise TypeError("report KPI resolution identity is not an integer")
    return int(value)


def _current_inventory_reference(
    repo_root: Path, reference: ReportKpiReference
) -> ReportKpiReference | None:
    inventory = load_report_kpi_reference_inventory(repo_root, (reference.ticker,))
    if len(inventory.source_states) != 1:
        return None
    source = inventory.source_states[0]
    if source.status is not ReportKpiReferenceSourceStatus.VALID:
        return None
    matches = [
        item
        for item in inventory.references
        if item.source_path == reference.source_path and item.json_pointer == reference.json_pointer
    ]
    if len(matches) != 1 or matches[0] != reference:
        return None
    return matches[0]


def _definition_row(
    conn: sqlite3.Connection, *, ticker: str, definition_id: int
) -> dict[str, object] | None:
    cursor = conn.execute(
        "SELECT id,ticker,name,unit,primary_source,fallback_source,reporting_cadence,"
        "definition_origin FROM kpi_definitions WHERE id=? AND UPPER(ticker)=UPPER(?)",
        (definition_id, ticker),
    )
    row = cursor.fetchone()
    return None if row is None else _row_dict(cursor, row)


def definition_identity_sha256(row: dict[str, object]) -> str:
    """Fingerprint the mutable definition projection used by report readers."""

    return _canonical_sha256(
        {
            "id": _as_int(row["id"]),
            "ticker": str(row["ticker"]).upper(),
            "name": str(row["name"]),
            "unit": str(row["unit"]),
            "primary_source": str(row["primary_source"]),
            "fallback_source": (
                None if row["fallback_source"] is None else str(row["fallback_source"])
            ),
            "reporting_cadence": str(row["reporting_cadence"]),
            "definition_origin": str(row["definition_origin"]),
        }
    )


def _evidence_row(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    definition_id: int,
    fact_id: int | None = None,
    context_id: int | None = None,
) -> dict[str, object] | None:
    relation = canonical_fact_relation(conn, "kpi_facts").sql
    predicates = [
        "UPPER(fact.ticker)=UPPER(?)",
        "fact.kpi_definition_id=?",
        "context.status='admitted'",
        "context.publication_lane='current_actual'",
        "NOT EXISTS (SELECT 1 FROM kpi_fact_semantic_contexts context_successor "
        "WHERE context_successor.supersedes_context_id=context.id)",
    ]
    params: list[object] = [ticker, definition_id]
    if fact_id is not None:
        predicates.append("fact.id=?")
        params.append(fact_id)
    if context_id is not None:
        predicates.append("context.id=?")
        params.append(context_id)
    cursor = conn.execute(
        "SELECT fact.id AS fact_id,fact.ticker AS fact_ticker,fact.period_end,"
        "fact.fiscal_period_type,fact.kpi_definition_id,fact.value,fact.unit AS fact_unit,"
        "fact.currency,fact.source_doc_id,fact.locator,fact.source_excerpt,fact.extracted_by,"
        "fact.confidence,fact.supersedes_id,context.id AS context_id,"
        "context.kpi_fact_id,context.revision AS context_revision,"
        "context.metric_name_as_reported,context.reported_period_end,context.period_role,"
        "context.publication_lane,context.accounting_basis,context.consolidation_scope,"
        "context.dimensions_json,context.unit_scale,context.source_row_label,"
        "context.source_column_header,context.source_value_text,context.status AS context_status,"
        "context.reviewed_by AS context_reviewed_by,context.knowledge_at AS context_knowledge_at,"
        "document.id AS document_id,document.ticker AS document_ticker,document.source_type,"
        "document.doc_type,document.period_end AS document_period_end,"
        "document.sha256 AS document_sha256 "
        f"FROM {relation} fact "  # nosec B608 -- resolver-owned constant relation
        "JOIN kpi_fact_semantic_contexts context ON context.kpi_fact_id=fact.id "
        "JOIN documents document ON document.id=fact.source_doc_id WHERE "
        + " AND ".join(predicates)
        + " ORDER BY fact.period_end DESC,fact.id DESC LIMIT 1",
        tuple(params),
    )
    row = cursor.fetchone()
    return None if row is None else _row_dict(cursor, row)


def evidence_identity_sha256(reference: ReportKpiReference, row: dict[str, object]) -> str:
    """Fingerprint the exact reference plus fact/context/document review evidence."""

    return _canonical_sha256({"reference": reference.model_dump(mode="json"), "evidence": row})


def _semantic_compatible(
    reference: ReportKpiReference,
    definition: dict[str, object],
    evidence: dict[str, object],
) -> bool:
    wanted = normalize_kpi_name(reference.requested_label)
    if normalize_kpi_name(str(definition["name"])) != wanted:
        return False
    if normalize_kpi_name(str(evidence["metric_name_as_reported"])) != wanted:
        return False
    if str(evidence["fact_ticker"]).upper() != reference.ticker.upper():
        return False
    if str(evidence["document_ticker"]).upper() != reference.ticker.upper():
        return False
    if _as_int(evidence["kpi_definition_id"]) != _as_int(definition["id"]):
        return False
    if (
        not str(evidence["locator"] or "").strip()
        or not str(evidence["source_excerpt"] or "").strip()
    ):
        return False
    document_sha = str(evidence["document_sha256"] or "")
    if len(document_sha) != 64 or any(char not in "0123456789abcdef" for char in document_sha):
        return False
    try:
        validate_admitted_unit_scale(
            Unit(str(evidence["fact_unit"])), KpiUnitScale(str(evidence["unit_scale"]))
        )
    except ValueError:
        return False
    return True


def _active_override_blocks(
    conn: sqlite3.Connection, reference: ReportKpiReference, definition_name: str
) -> bool:
    if not _table_exists(conn, "fact_overrides"):
        return True
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(fact_overrides)")}
    required = {"ticker", "fact_kind", "fact_key", "action", "status"}
    if not required.issubset(columns):
        return True
    keys = {normalize_kpi_name(reference.requested_label), normalize_kpi_name(definition_name)}
    rows = conn.execute(
        "SELECT fact_key FROM fact_overrides WHERE UPPER(ticker)=UPPER(?) "
        "AND fact_kind='kpi' AND status='active' AND action IN ('replace','drop')",
        (reference.ticker,),
    ).fetchall()
    return any(normalize_kpi_name(str(row[0])) in keys for row in rows)


def propose_report_kpi_reference_resolution(
    conn: sqlite3.Connection,
    *,
    repo_root: Path,
    reference: ReportKpiReference,
) -> ReportKpiReferenceResolutionProposal:
    """Return a deterministic review candidate without persisting or accepting it."""

    if _current_inventory_reference(repo_root, reference) is None:
        return ReportKpiReferenceResolutionProposal(
            reference=reference,
            outcome=ReportKpiReferenceProposalOutcome.BLOCKED,
            reason_code="report_reference_identity_changed",
            candidate_definition_ids=(),
        )
    rows = conn.execute(
        "SELECT id,name FROM kpi_definitions WHERE UPPER(ticker)=UPPER(?) ORDER BY id",
        (reference.ticker,),
    ).fetchall()
    wanted = normalize_kpi_name(reference.requested_label)
    candidate_ids = tuple(int(row[0]) for row in rows if normalize_kpi_name(str(row[1])) == wanted)
    if not candidate_ids:
        return ReportKpiReferenceResolutionProposal(
            reference=reference,
            outcome=ReportKpiReferenceProposalOutcome.NONE,
            reason_code="no_matching_reported_definition",
            candidate_definition_ids=(),
        )
    if len(candidate_ids) != 1:
        return ReportKpiReferenceResolutionProposal(
            reference=reference,
            outcome=ReportKpiReferenceProposalOutcome.AMBIGUOUS,
            reason_code="ambiguous_definition_family",
            candidate_definition_ids=candidate_ids,
        )
    definition = _definition_row(conn, ticker=reference.ticker, definition_id=candidate_ids[0])
    evidence = _evidence_row(conn, ticker=reference.ticker, definition_id=candidate_ids[0])
    if (
        definition is None
        or evidence is None
        or not _semantic_compatible(reference, definition, evidence)
    ):
        return ReportKpiReferenceResolutionProposal(
            reference=reference,
            outcome=ReportKpiReferenceProposalOutcome.BLOCKED,
            reason_code="source_review_evidence_unavailable",
            candidate_definition_ids=candidate_ids,
        )
    if _active_override_blocks(conn, reference, str(definition["name"])):
        return ReportKpiReferenceResolutionProposal(
            reference=reference,
            outcome=ReportKpiReferenceProposalOutcome.BLOCKED,
            reason_code="active_scalar_override",
            candidate_definition_ids=candidate_ids,
        )
    method = (
        ReportKpiReferenceResolutionMethod.EXACT_DEFINITION_IDENTITY
        if reference.requested_label.strip().casefold()
        == str(definition["name"]).strip().casefold()
        else ReportKpiReferenceResolutionMethod.UNIT_SURFACE_ALIAS
    )
    disposition = ReportKpiReferenceDisposition(
        status=ReportKpiReferenceStatus.RESOLVED,
        kpi_definition_id=candidate_ids[0],
        definition_identity_sha256=definition_identity_sha256(definition),
        evidence_fact_id=_as_int(evidence["fact_id"]),
        evidence_context_id=_as_int(evidence["context_id"]),
        evidence_sha256=evidence_identity_sha256(reference, evidence),
        resolution_method=method,
        policy_name=POLICY_NAME,
        policy_version=POLICY_VERSION,
        policy_config_sha256=POLICY_CONFIG_SHA256,
        reason_code=method.value,
    )
    return ReportKpiReferenceResolutionProposal(
        reference=reference,
        outcome=ReportKpiReferenceProposalOutcome.CANDIDATE,
        reason_code="source_review_candidate",
        candidate_definition_ids=candidate_ids,
        proposed_disposition=disposition,
    )


def resolve_report_kpi_reference_binding(
    conn: sqlite3.Connection,
    *,
    repo_root: Path,
    user_id: str,
    reference: ReportKpiReference,
) -> ResolvedReportKpiReferenceBinding | None:
    """Return an exact binding only while every reviewed identity remains current."""

    if _current_inventory_reference(repo_root, reference) is None:
        return None
    revision = current_report_kpi_reference_disposition(conn, user_id=user_id, reference=reference)
    return _resolve_report_kpi_reference_binding(
        conn, user_id=user_id, reference=reference, revision=revision
    )


def _resolve_report_kpi_reference_binding(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    reference: ReportKpiReference,
    revision: ReportKpiReferenceDispositionRevision | None,
) -> ResolvedReportKpiReferenceBinding | None:
    if revision is None or revision.reference != reference:
        return None
    disposition = revision.disposition
    if disposition.status is not ReportKpiReferenceStatus.RESOLVED:
        return None
    current_heads = conn.execute(
        "SELECT id FROM report_kpi_reference_resolution_revisions resolution "
        "WHERE resolution.user_id=? AND UPPER(resolution.ticker)=UPPER(?) "
        "AND resolution.source_path=? AND resolution.json_pointer=? AND NOT EXISTS ("
        "SELECT 1 FROM report_kpi_reference_resolution_revisions successor "
        "WHERE successor.supersedes_resolution_id=resolution.id)",
        (user_id, reference.ticker, reference.source_path, reference.json_pointer),
    ).fetchall()
    if [int(row[0]) for row in current_heads] != [revision.id]:
        return None
    definition_id = disposition.kpi_definition_id
    fact_id = disposition.evidence_fact_id
    context_id = disposition.evidence_context_id
    if definition_id is None or fact_id is None or context_id is None:
        return None
    definition = _definition_row(conn, ticker=reference.ticker, definition_id=definition_id)
    if definition is None or definition_identity_sha256(definition) != (
        disposition.definition_identity_sha256
    ):
        return None
    evidence = _evidence_row(
        conn,
        ticker=reference.ticker,
        definition_id=definition_id,
        fact_id=fact_id,
        context_id=context_id,
    )
    if (
        evidence is None
        or evidence_identity_sha256(reference, evidence) != disposition.evidence_sha256
    ):
        return None
    if not _semantic_compatible(reference, definition, evidence):
        return None
    if _active_override_blocks(conn, reference, str(definition["name"])):
        return None
    if disposition.policy_name != POLICY_NAME or disposition.policy_version != POLICY_VERSION:
        return None
    if disposition.policy_config_sha256 != POLICY_CONFIG_SHA256:
        return None
    method = disposition.resolution_method
    if method is None:
        return None
    actual_method = (
        ReportKpiReferenceResolutionMethod.EXACT_DEFINITION_IDENTITY
        if reference.requested_label.strip().casefold()
        == str(definition["name"]).strip().casefold()
        else ReportKpiReferenceResolutionMethod.UNIT_SURFACE_ALIAS
    )
    if method is not actual_method:
        return None
    return ResolvedReportKpiReferenceBinding(
        resolution_id=revision.id,
        kpi_definition_id=definition_id,
        definition_name=str(definition["name"]),
        evidence_fact_id=fact_id,
        evidence_context_id=context_id,
        resolution_method=method,
    )


def report_kpi_reference_at(
    repo_root: Path,
    *,
    ticker: str,
    json_pointer: str,
) -> ReportKpiReference | None:
    """Return one exact, currently valid holdings reference by JSON pointer."""

    inventory = load_report_kpi_reference_inventory(repo_root, (ticker.upper(),))
    if len(inventory.source_states) != 1:
        return None
    if inventory.source_states[0].status is not ReportKpiReferenceSourceStatus.VALID:
        return None
    matches = [item for item in inventory.references if item.json_pointer == json_pointer]
    return matches[0] if len(matches) == 1 else None


def verified_report_kpi_reference_definition(
    conn: sqlite3.Connection,
    *,
    repo_root: Path,
    user_id: str,
    reference: ReportKpiReference,
) -> VerifiedReportKpiReferenceDefinition | None:
    """Resolve one report reference without ever falling back past its review state.

    A reviewed RESOLVED row is reconstructed through the evidence-bound reader.
    UNRESOLVED and RETIRED rows block consumption.  An as-authored reference with
    no review history may use only a sole case-insensitive exact definition name;
    normalized aliases require the append-only reviewed binding.
    """

    if _current_inventory_reference(repo_root, reference) is None:
        return None
    revision = current_report_kpi_reference_disposition(conn, user_id=user_id, reference=reference)
    return _verified_report_kpi_reference_definition(
        conn, user_id=user_id, reference=reference, revision=revision, repo_root=repo_root
    )


def _verified_report_kpi_reference_definition(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    reference: ReportKpiReference,
    revision: ReportKpiReferenceDispositionRevision | None,
    repo_root: Path | None = None,
    exact_definition_rows: tuple[tuple[int, str], ...] | None = None,
) -> VerifiedReportKpiReferenceDefinition | None:
    if revision is not None:
        if revision.reference != reference:
            return None
        if revision.disposition.status is not ReportKpiReferenceStatus.RESOLVED:
            return None
        # Standalone readers retain their fresh nested source/disposition read.
        # The batch owns one inventory and the final source freshness check.
        binding = (
            _resolve_report_kpi_reference_binding(
                conn, user_id=user_id, reference=reference, revision=revision
            )
            if repo_root is None
            else resolve_report_kpi_reference_binding(
                conn, repo_root=repo_root, user_id=user_id, reference=reference
            )
        )
        if binding is None:
            return None
        return VerifiedReportKpiReferenceDefinition(
            kpi_definition_id=binding.kpi_definition_id,
            definition_name=binding.definition_name,
            resolution_id=binding.resolution_id,
        )
    rows = exact_definition_rows
    if rows is None:
        rows = tuple(
            (int(row[0]), str(row[1]))
            for row in conn.execute(
                "SELECT id,name FROM kpi_definitions WHERE UPPER(ticker)=UPPER(?) "
                "AND LOWER(TRIM(name))=LOWER(TRIM(?)) ORDER BY id",
                (reference.ticker, reference.requested_label),
            )
        )
    if len(rows) != 1:
        return None
    if _active_override_blocks(conn, reference, str(rows[0][1])):
        return None
    return VerifiedReportKpiReferenceDefinition(
        kpi_definition_id=int(rows[0][0]),
        definition_name=str(rows[0][1]),
    )


def _exact_definition_rows_for_references(
    conn: sqlite3.Connection, references: tuple[ReportKpiReference, ...]
) -> tuple[tuple[tuple[int, str], ...], ...]:
    """Read one batch projection; SQLite owns both comparison-key transforms.

    This projection is local to this call, not an all-request database snapshot.
    Reviewed bindings still reconstruct their current heads and evidence.
    """
    if not references:
        return ()
    variable_limit = conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
    chunk_size = min(250, variable_limit // 2)
    if chunk_size < 1:
        raise ValueError("SQLite variable limit cannot bind report reference keys")
    keys: list[tuple[str, str] | None] = [None] * len(references)
    for start in range(0, len(references), chunk_size):
        chunk = references[start : start + chunk_size]
        params: list[object] = []
        for reference in chunk:
            params.extend((reference.ticker, reference.requested_label))
        marks = ",".join(f"({ordinal},?,?)" for ordinal in range(start, start + len(chunk)))
        rows = conn.execute(
            "WITH requested(ordinal,ticker,label) AS (VALUES "  # nosec B608 -- bound values only
            + marks
            + ") SELECT ordinal,UPPER(ticker),LOWER(TRIM(label)) FROM requested ORDER BY ordinal",
            tuple(params),
        )
        for row in rows:
            if row[1] is not None and row[2] is not None:
                keys[int(row[0])] = (str(row[1]), str(row[2]))
    tickers = tuple(dict.fromkeys(key[0] for key in keys if key is not None))
    definitions: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for start in range(0, len(tickers), chunk_size):
        chunk_tickers = tickers[start : start + chunk_size]
        marks = ",".join("?" for _ in chunk_tickers)
        rows = conn.execute(
            "SELECT id,name,UPPER(ticker),LOWER(TRIM(name)) FROM kpi_definitions "
            f"WHERE UPPER(ticker) IN ({marks}) ORDER BY id",  # nosec B608 -- bound values only
            chunk_tickers,
        )
        for row in rows:
            if row[2] is not None and row[3] is not None:
                definitions.setdefault((str(row[2]), str(row[3])), []).append(
                    (int(row[0]), str(row[1]))
                )
    return tuple(() if key is None else tuple(definitions.get(key, ())) for key in keys)


_ReportReferenceRead: TypeAlias = tuple[
    ReportKpiReference,
    ReportKpiReferenceDispositionRevision | None,
    VerifiedReportKpiReferenceDefinition | None,
]


def read_report_kpi_reference_definitions(
    conn: sqlite3.Connection,
    *,
    repo_root: Path,
    user_id: str,
    tickers: tuple[str, ...],
) -> tuple[ReportKpiReferenceInventory, tuple[_ReportReferenceRead, ...]]:
    """Reconstruct one request, retaining live evidence and final source checks."""
    inventory = load_report_kpi_reference_inventory(repo_root, tickers)
    sources_by_ticker: dict[str, list[ReportKpiReferenceSourceState]] = {}
    references_by_pointer: dict[tuple[str, str], list[ReportKpiReference]] = {}
    for source in inventory.source_states:
        sources_by_ticker.setdefault(source.ticker, []).append(source)
    for reference in inventory.references:
        references_by_pointer.setdefault(
            (reference.source_path, reference.json_pointer), []
        ).append(reference)
    reference_revisions = tuple(
        (
            reference,
            current_report_kpi_reference_disposition(conn, user_id=user_id, reference=reference),
        )
        for reference in inventory.references
    )
    unreviewed_references = tuple(
        reference
        for reference, revision in reference_revisions
        if revision is None
        and len(sources_by_ticker.get(reference.ticker, [])) == 1
        and sources_by_ticker[reference.ticker][0].status is ReportKpiReferenceSourceStatus.VALID
        and len(references_by_pointer[(reference.source_path, reference.json_pointer)]) == 1
    )
    exact_rows = _exact_definition_rows_for_references(conn, unreviewed_references)
    definitions_by_pointer = {
        (reference.source_path, reference.json_pointer): rows
        for reference, rows in zip(unreviewed_references, exact_rows, strict=True)
    }
    reads: list[_ReportReferenceRead] = []
    for reference, revision in reference_revisions:
        sources = sources_by_ticker.get(reference.ticker, [])
        matches = references_by_pointer[(reference.source_path, reference.json_pointer)]
        verified = (
            _verified_report_kpi_reference_definition(
                conn,
                user_id=user_id,
                reference=reference,
                revision=revision,
                exact_definition_rows=definitions_by_pointer.get(
                    (reference.source_path, reference.json_pointer)
                ),
            )
            if len(sources) == 1
            and sources[0].status is ReportKpiReferenceSourceStatus.VALID
            and len(matches) == 1
            and matches[0] == reference
            else None
        )
        reads.append((reference, revision, verified))
    current_exact_rows = _exact_definition_rows_for_references(conn, unreviewed_references)
    current_unreviewed_revisions = tuple(
        current_report_kpi_reference_disposition(conn, user_id=user_id, reference=reference)
        for reference in unreviewed_references
    )
    changed_unreviewed_pointers = {
        (reference.source_path, reference.json_pointer)
        for reference, initial_rows, current_rows, current_revision in zip(
            unreviewed_references,
            exact_rows,
            current_exact_rows,
            current_unreviewed_revisions,
            strict=True,
        )
        if initial_rows != current_rows or current_revision is not None
    }
    if changed_unreviewed_pointers:
        # Invalidate changed unreviewed reads; never bind new definitions or history.
        reads = [
            (
                reference,
                revision,
                None
                if revision is None
                and (reference.source_path, reference.json_pointer) in changed_unreviewed_pointers
                else verified,
            )
            for reference, revision, verified in reads
        ]
    current = load_report_kpi_reference_inventory(repo_root, tickers)
    changed = {
        ticker
        for ticker in tickers
        if tuple(ref for ref in inventory.references if ref.ticker == ticker)
        != tuple(ref for ref in current.references if ref.ticker == ticker)
        or tuple(state for state in inventory.source_states if state.ticker == ticker)
        != tuple(state for state in current.source_states if state.ticker == ticker)
    }
    if changed:
        # Use the existing invalid-source disposition. Never return a healthy
        # census from references whose configuration changed during this read.
        inventory = ReportKpiReferenceInventory(
            references=tuple(ref for ref in inventory.references if ref.ticker not in changed),
            source_states=tuple(
                state.model_copy(
                    update={
                        "status": ReportKpiReferenceSourceStatus.INVALID,
                        "reason_code": "report_configuration_changed_during_scope",
                    }
                )
                if state.ticker in changed
                else state
                for state in inventory.source_states
            ),
        )
        reads = [read for read in reads if read[0].ticker not in changed]
    return inventory, tuple(reads)


__all__ = [
    "POLICY_CONFIG_SHA256",
    "POLICY_NAME",
    "POLICY_VERSION",
    "ReportKpiReferenceProposalOutcome",
    "ReportKpiReferenceResolutionProposal",
    "ResolvedReportKpiReferenceBinding",
    "VerifiedReportKpiReferenceDefinition",
    "definition_identity_sha256",
    "evidence_identity_sha256",
    "propose_report_kpi_reference_resolution",
    "read_report_kpi_reference_definitions",
    "report_kpi_reference_at",
    "resolve_report_kpi_reference_binding",
    "verified_report_kpi_reference_definition",
]
