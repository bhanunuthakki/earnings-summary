"""Build one bounded KPI refresh manifest from reviewed, verbatim evidence.

This builder is read-only. It converts owner-reviewed decisions into the
guarded refresh contract only after binding every decision to the exact
content-addressed review partition and current database heads. Canonical-current
repairs retain v5 compatibility; quarantined legacy predecessors require v6.
The downstream executor remains the sole writer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from execution.apply_kpi_semantic_refresh import (  # noqa: E402
    MAX_KNOWLEDGE_AT_FUTURE_SKEW,
    RefreshEntry,
    RefreshManifest,
    RepairBlockedError,
    SemanticEvidenceQuotes,
    schema_revision,
    validate_refresh_entry,
)
from models.facts import Currency, Unit  # noqa: E402
from operations.kpi_semantic_review_export import KpiSemanticReviewExport  # noqa: E402
from pipeline.kpi_semantic_review import (  # noqa: E402
    QUARANTINED_PREDECESSOR_SCOPE_REASON,
    KpiEvidenceCandidate,
    KpiEvidenceLocatorCoordinates,
    KpiSemanticReviewItem,
    KpiSemanticReviewState,
    fact_locator_from_evidence_coordinates,
)
from pipeline.kpi_semantic_scope import portfolio_tickers, scoped_kpi_definitions  # noqa: E402
from pipeline.kpi_semantics import (  # noqa: E402
    KpiSemanticContext,
    KpiSemanticStatus,
    current_kpi_semantic_context,
    normalize_source_numeric,
    parse_source_numeric,
)
from provenance.evidence_ledger import EvidenceLocator  # noqa: E402
from provenance.financial_fact_resolution import canonical_fact_relation  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

MAX_REVIEWED_DECISIONS_PER_MANIFEST = 25
_SHA256 = r"^[0-9a-f]{64}$"


class ReviewedKpiSemanticDecision(BaseModel):
    """One owner decision tied to a candidate in the immutable review batch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_id: int = Field(gt=0)
    action: Literal["bind_existing", "supersede"]
    expected_context_head_id: int | None = Field(default=None, gt=0)
    expected_context_revision: int = Field(ge=0)
    expected_old_source_sha256: str = Field(pattern=_SHA256)
    evidence_candidate_index: int = Field(ge=0)
    currency: Currency | None = None
    context: KpiSemanticContext
    semantic_evidence: SemanticEvidenceQuotes

    @model_validator(mode="after")
    def _admission_is_explicit(self) -> Self:
        if (self.expected_context_head_id is None) != (self.expected_context_revision == 0):
            raise ValueError("expected context head and revision conflict")
        if self.context.status is not KpiSemanticStatus.ADMITTED:
            raise ValueError("reviewed semantic decision must be admitted")
        if self.context.source_value_text is not None:
            raise ValueError("source value text is derived from the selected evidence candidate")
        return self


class KpiSemanticRefreshDecisionBatch(BaseModel):
    """Owner-reviewed decisions plus the authority fields of the output manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["kpi_semantic_refresh_decisions.v2"]
    review_export_sha256: str = Field(pattern=_SHA256)
    review_batch_sha256: str = Field(pattern=_SHA256)
    reviewer: str = Field(min_length=1, max_length=128)
    logical_idempotency_key: str = Field(min_length=1, max_length=256)
    knowledge_at: datetime
    review_bundle_sha256: str = Field(pattern=_SHA256)
    expected_schema_revision: str = Field(min_length=1, max_length=160)
    backup_restore_evidence_id: str = Field(pattern=_SHA256)
    decisions: tuple[ReviewedKpiSemanticDecision, ...] = Field(
        min_length=1, max_length=MAX_REVIEWED_DECISIONS_PER_MANIFEST
    )

    @field_validator("knowledge_at")
    @classmethod
    def _knowledge_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("decision knowledge_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _facts_are_unique(self) -> Self:
        fact_ids = [decision.fact_id for decision in self.decisions]
        if len(set(fact_ids)) != len(fact_ids):
            raise ValueError("decision batch repeats a fact")
        return self


def _semantic_quotes(decision: ReviewedKpiSemanticDecision) -> tuple[str, ...]:
    evidence = decision.semantic_evidence
    return (
        evidence.metric_name_quote,
        evidence.reported_period_quote,
        evidence.accounting_basis_quote,
        evidence.consolidation_scope_quote,
        evidence.unit_scale_quote,
        *evidence.dimension_quotes.values(),
    )


def _validated_candidate_coordinates(
    conn: sqlite3.Connection,
    *,
    source_doc_id: int,
    candidate: KpiEvidenceCandidate,
) -> KpiEvidenceLocatorCoordinates:
    row = conn.execute(
        "SELECT node.locator_json,node.locator_sha256 FROM evidence_nodes node "
        "JOIN evidence_extraction_runs run ON run.extraction_run_id=node.extraction_run_id "
        "JOIN evidence_document_versions version "
        "ON version.document_version_id=run.document_version_id "
        "WHERE node.node_id=? AND version.legacy_document_id=?",
        (candidate.evidence_node_id, source_doc_id),
    ).fetchone()
    if row is None or row["locator_json"] is None:
        raise ValueError("selected evidence candidate locator is unavailable")
    try:
        locator = EvidenceLocator.model_validate_json(str(row["locator_json"]))
    except ValueError as exc:
        raise ValueError("selected evidence candidate locator is invalid") from exc
    if (
        str(row["locator_json"]) != locator.canonical_json
        or str(row["locator_sha256"] or "") != locator.canonical_sha256
        or candidate.locator_sha256 != locator.canonical_sha256
    ):
        raise ValueError("selected evidence candidate locator hash changed after review")
    coordinates = KpiEvidenceLocatorCoordinates.from_evidence_locator(locator)
    if coordinates != candidate.locator_coordinates:
        raise ValueError("selected evidence candidate coordinates changed after review")
    return coordinates


def _fact_row(
    conn: sqlite3.Connection, *, fact_id: int, allow_quarantined: bool
) -> tuple[sqlite3.Row, str]:
    relation = canonical_fact_relation(conn, "kpi_facts")
    if relation.selection_mode != "resolved_view":
        raise ValueError("refresh manifest requires the resolved current-fact view")
    fact_source = "kpi_facts" if allow_quarantined else relation.sql
    cursor = conn.execute(
        f"SELECT fact.*,definition.name AS definition_name,"  # nosec B608 -- resolver-owned relation
        f"definition.ticker AS definition_ticker,document.sha256 AS old_source_sha256 "
        f"FROM {fact_source} fact JOIN kpi_definitions definition "  # nosec B608 -- closed internal relation choice
        "ON definition.id=fact.kpi_definition_id JOIN documents document "
        "ON document.id=fact.source_doc_id WHERE fact.id=?",
        (fact_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise ValueError("reviewed fact head changed after review batch preparation")
    if allow_quarantined and (
        conn.execute(
            f"SELECT 1 FROM {relation.sql} WHERE id=?",  # nosec B608 -- resolver-owned relation
            (fact_id,),
        ).fetchone()
        is not None
    ):
        raise ValueError("quarantined predecessor became canonical after review")
    if not isinstance(row, sqlite3.Row):
        columns = [str(column[0]) for column in cursor.description]
        values = dict(zip(columns, row, strict=True))
        raise TypeError(f"refresh manifest requires sqlite3.Row, got columns {tuple(values)}")
    return row, str(row["old_source_sha256"])


def _validate_batch_item_against_row(item: KpiSemanticReviewItem, row: sqlite3.Row) -> None:
    """Keep every stale-batch field comparison in one auditable boundary."""
    if item.legacy_source_doc_id is None:
        raise ValueError("review-ready decision lacks the legacy source identity")
    fact_id = item.fact_id
    expected = (
        fact_id,
        item.ticker.upper(),
        item.period_end[:10],
        item.fiscal_period_type,
        item.kpi_definition_id,
        Decimal(item.value),
        item.unit,
        item.legacy_source_doc_id,
        item.kpi_name,
    )
    actual = (
        int(row["id"]),
        str(row["ticker"]).upper(),
        str(row["period_end"])[:10],
        str(row["fiscal_period_type"]),
        int(row["kpi_definition_id"]),
        Decimal(str(row["value"])),
        str(row["unit"]),
        int(row["source_doc_id"]),
        str(row["definition_name"]),
    )
    if actual != expected:
        raise ValueError("reviewed fact fields changed after review batch preparation")


def _entry_for_decision(
    conn: sqlite3.Connection,
    *,
    item: KpiSemanticReviewItem,
    decision: ReviewedKpiSemanticDecision,
) -> RefreshEntry:
    if item.state is not KpiSemanticReviewState.SOURCE_REVIEW_REQUIRED:
        raise ValueError("decision requires source_review_required review state")
    if item.evidence_search_incomplete:
        raise ValueError("decision evidence search is incomplete")
    if item.evidence_candidates_truncated:
        raise ValueError("decision evidence candidates are truncated")
    candidates = item.evidence_candidates
    if decision.evidence_candidate_index >= len(candidates):
        raise ValueError("selected evidence candidate is unavailable")
    candidate = candidates[decision.evidence_candidate_index]
    source_excerpt = str(candidate.excerpt)
    if any(quote not in source_excerpt for quote in _semantic_quotes(decision)):
        raise ValueError("semantic evidence quote is not verbatim in the selected review excerpt")

    quarantined_predecessor = item.scope_reasons == (QUARANTINED_PREDECESSOR_SCOPE_REASON,)
    if quarantined_predecessor and decision.action != "supersede":
        raise ValueError("quarantined predecessor decisions must supersede")
    row, old_source_sha = _fact_row(
        conn,
        fact_id=decision.fact_id,
        allow_quarantined=quarantined_predecessor,
    )
    _validate_batch_item_against_row(item, row)
    if item.legacy_source_doc_id is None:
        raise ValueError("review-ready decision lacks the legacy source identity")
    current = current_kpi_semantic_context(conn, kpi_fact_id=decision.fact_id)
    current_head = None if current is None else current.id
    current_revision = 0 if current is None else current.revision
    if (current_head, current_revision) != (
        decision.expected_context_head_id,
        decision.expected_context_revision,
    ):
        raise ValueError("semantic context head changed after owner review")
    if old_source_sha != decision.expected_old_source_sha256:
        raise ValueError("old source identity changed after owner review")
    source_doc_id = item.source_doc_id
    source_content_sha256 = item.source_content_sha256
    source_observation_version = item.source_observation_version
    if source_doc_id is None or source_content_sha256 is None or source_observation_version is None:
        raise ValueError("review-ready decision lacks exact source identity")
    coordinates = _validated_candidate_coordinates(
        conn,
        source_doc_id=source_doc_id,
        candidate=candidate,
    )
    locator = fact_locator_from_evidence_coordinates(
        coordinates,
        verbatim_snippet=candidate.excerpt,
    )
    locator_json = locator.to_json()
    if locator_json is None:
        raise ValueError("reviewed decision requires a concrete fact locator")
    unit = Unit(item.unit)
    value = normalize_source_numeric(
        parse_source_numeric(str(candidate.source_value_text)),
        unit=unit,
        unit_scale=decision.context.unit_scale,
    )
    return RefreshEntry(
        action=decision.action,
        predecessor_resolution_state=(
            "quarantined_legacy" if quarantined_predecessor else "canonical_current"
        ),
        old_fact_id=decision.fact_id,
        expected_fact_head_id=decision.fact_id,
        expected_context_head_id=decision.expected_context_head_id,
        expected_context_revision=decision.expected_context_revision,
        expected_old_source_doc_id=item.legacy_source_doc_id,
        expected_old_source_sha256=decision.expected_old_source_sha256,
        source_doc_id=int(source_doc_id),
        source_content_sha256=str(source_content_sha256),
        source_observation_version=str(source_observation_version),
        source_period_end=item.source_period_end,
        evidence_node_id=candidate.evidence_node_id,
        evidence_locator_sha256=candidate.locator_sha256,
        fact_locator_sha256=hashlib.sha256(locator_json.encode("utf-8")).hexdigest(),
        source_excerpt=source_excerpt,
        source_value_text=candidate.source_value_text,
        value=value,
        unit=unit,
        currency=decision.currency,
        locator=locator,
        context=decision.context,
        semantic_evidence=decision.semantic_evidence,
        expected_inserted_fact_rows=0 if decision.action == "bind_existing" else 1,
        expected_inserted_context_rows=1,
    )


def build_kpi_semantic_refresh_manifest(
    conn: sqlite3.Connection,
    *,
    repo_root: Path,
    review_export: KpiSemanticReviewExport,
    decisions: KpiSemanticRefreshDecisionBatch,
    now: datetime,
) -> RefreshManifest:
    """Build and fully read-validate one safely grouped refresh manifest."""
    conn.row_factory = sqlite3.Row
    review_export = KpiSemanticReviewExport.model_validate(review_export.model_dump(mode="json"))
    review_batch = review_export.review
    decisions = KpiSemanticRefreshDecisionBatch.model_validate(decisions.model_dump(mode="json"))
    if now.tzinfo is None:
        raise ValueError("manifest build time must be timezone-aware")
    current_time = now.astimezone(UTC)
    # A v2 export is one cursor-bound partition; nonterminal partitions are
    # expected and carry their continuation in the sealed export envelope.
    # Item-level evidence incompleteness still fails closed in
    # ``_entry_for_decision`` below.
    if decisions.review_export_sha256 != review_export.content_sha256:
        raise ValueError("decision review export hash does not match the sealed export")
    if decisions.review_batch_sha256 != review_batch.content_sha256:
        raise ValueError("decision nested review hash does not match the review payload")
    if decisions.knowledge_at < review_batch.observed_at:
        raise ValueError("decision knowledge_at predates the observed review evidence")
    if decisions.knowledge_at > current_time + MAX_KNOWLEDGE_AT_FUTURE_SKEW:
        raise ValueError("decision knowledge_at exceeds the allowed future clock skew")
    if decisions.expected_schema_revision != review_export.schema_revision:
        raise ValueError("decision schema revision does not match the sealed export")
    if schema_revision(conn) != decisions.expected_schema_revision:
        raise ValueError("database schema changed after decision preparation")
    items_by_fact = {item.fact_id: item for item in review_batch.items}
    selected: list[tuple[KpiSemanticReviewItem, ReviewedKpiSemanticDecision]] = []
    for decision in sorted(decisions.decisions, key=lambda value: value.fact_id):
        item = items_by_fact.get(decision.fact_id)
        if item is None:
            raise ValueError("decision fact is absent from the content-addressed review partition")
        selected.append((item, decision))
    group_keys = {(item.ticker, item.source_doc_id, decision.action) for item, decision in selected}
    if len(group_keys) != 1:
        raise ValueError(
            "one refresh manifest must contain one ticker, source document, and action"
        )
    allowed = {
        row.kpi_definition_id
        for row in scoped_kpi_definitions(
            conn,
            repo_root=repo_root,
            user_id=review_export.user_id,
        )
        if row.kpi_definition_id is not None
    }
    owner_ticker_set = frozenset(portfolio_tickers(conn, user_id=review_export.user_id))
    entries: list[RefreshEntry] = []
    for item, decision in selected:
        entry = _entry_for_decision(conn, item=item, decision=decision)
        try:
            validate_refresh_entry(
                conn,
                entry,
                allowed,
                owner_tickers=owner_ticker_set,
            )
        except RepairBlockedError as exc:
            raise ValueError(f"reviewed decision failed guarded validation: {exc.code}") from None
        entries.append(entry)
    manifest_schema = (
        "kpi_semantic_refresh.v6"
        if any(entry.predecessor_resolution_state == "quarantined_legacy" for entry in entries)
        else "kpi_semantic_refresh.v5"
    )
    return RefreshManifest(
        schema_version=manifest_schema,
        user_id=review_export.user_id,
        logical_idempotency_key=decisions.logical_idempotency_key,
        reviewer=decisions.reviewer,
        knowledge_at=decisions.knowledge_at,
        review_bundle_sha256=decisions.review_bundle_sha256,
        expected_schema_revision=decisions.expected_schema_revision,
        backup_restore_evidence_id=decisions.backup_restore_evidence_id,
        entries=tuple(entries),
    )


def write_refresh_manifest(path: Path, manifest: RefreshManifest) -> None:
    """Atomically write deterministic JSON without touching the database."""
    encoded = manifest.model_dump_json(indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == encoded:
        return
    temporary = path.with_suffix(path.suffix + f".{uuid4().hex}.tmp")
    temporary.write_text(encoded, encoding="utf-8")
    temporary.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--review-export", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database = args.db.resolve()
    output = args.output.resolve()
    inputs = {database, args.review_export.resolve(), args.decisions.resolve()}
    if output in inputs:
        raise ValueError("refresh manifest output must not overwrite an input")
    review_export = KpiSemanticReviewExport.model_validate_json(
        args.review_export.read_text(encoding="utf-8")
    )
    decisions = KpiSemanticRefreshDecisionBatch.model_validate_json(
        args.decisions.read_text(encoding="utf-8")
    )
    conn = connect_sqlite(database, role=SQLiteConnectionRole.READ_ONLY)
    try:
        manifest = build_kpi_semantic_refresh_manifest(
            conn,
            repo_root=args.repo_root,
            review_export=review_export,
            decisions=decisions,
            now=datetime.now(UTC),
        )
    finally:
        conn.close()
    write_refresh_manifest(output, manifest)
    print(
        json.dumps(
            {
                "event": "kpi_semantic_refresh_manifest_built",
                "entries": len(manifest.entries),
                "manifest_sha256": manifest.content_sha256(),
                "output": str(output),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
