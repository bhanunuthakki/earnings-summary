"""Read-only shadow census for issuer KPI definition-revision adoption.

The census compares the current decision-grade reader membership with the
existing revision-aware resolver for every KPI definition owned by the active
portfolio roster.  It is evidence for planning a cutover; it never authorizes
reader activation.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import to_jsonable_python

from compute.kpi_resolver import (
    KpiRevisionSeriesBreak,
    KpiRevisionSeriesComparability,
    KpiRevisionSeriesExclusionReason,
    KpiRevisionSeriesStatus,
    resolve_revision_aware_kpi_series,
    revision_aware_kpi_schema_blockers,
    semantic_series_identity_sql,
)
from pipeline.kpi_semantics import semantic_admission_sql
from provenance.financial_fact_resolution import canonical_fact_relation
from provenance.verifier_identity import verifier_source_artifact_sha256
from sqlite_runtime import reject_forbidden_mac_checkout_database
from sqlite_snapshot import SnapshotManifest

_SOURCE_ROOT = Path(__file__).resolve().parents[1]
_REPOSITORY_ROOT = _SOURCE_ROOT.parent
_SUPPORTED_SNAPSHOT_SCHEMA_VERSION = "sqlite-reader-snapshot/v1"
_SUPPORTED_SNAPSHOT_CODE_CONFIG_VERSION = "sqlite-reader-snapshot/v1"


def _canonical_json(value: object) -> str:
    return json.dumps(
        to_jsonable_python(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("census clocks must be timezone-aware")
    return value.astimezone(UTC)


class KpiRevisionCensusReadiness(StrEnum):
    READY = "ready"
    READY_WITH_EXPLAINED_DIFFERENCES = "ready_with_explained_differences"
    BLOCKED = "blocked"


class SnapshotEvidenceState(BaseModel):
    """Identity evidence for the database artifact being rehearsed.

    A matching supported manifest binds the file to the snapshot producer's
    recorded identity and verification assertions.  It is deliberately not a
    fresh integrity check, approval, or production-authority token.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["manifest_matched", "unverified"]
    snapshot_path: str | None = None
    manifest_path: str | None = None
    snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    blocking_reasons: tuple[str, ...]

    @classmethod
    def unverified(cls, reason: str) -> SnapshotEvidenceState:
        return cls(status="unverified", blocking_reasons=(reason,))

    @model_validator(mode="after")
    def _closed_state(self) -> Self:
        if self.status == "manifest_matched":
            if (
                self.snapshot_path is None
                or self.manifest_path is None
                or self.snapshot_sha256 is None
                or self.manifest_sha256 is None
            ):
                raise ValueError("matched snapshot evidence requires paths and artifact hashes")
            if self.blocking_reasons:
                raise ValueError("matched snapshot evidence cannot carry blockers")
        elif not self.blocking_reasons:
            raise ValueError("unverified snapshot evidence requires a blocking reason")
        return self


class KpiFactCensusDisposition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_id: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=96)
    semantic_context_id: int | None = Field(default=None, gt=0)
    semantic_context_revision: int | None = Field(default=None, gt=0)
    definition_revision_id: str | None = Field(default=None, min_length=1, max_length=128)
    comparability_revision_id: str | None = Field(default=None, min_length=1, max_length=128)


class KpiDefinitionShadowCensus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str = Field(min_length=1, max_length=32)
    kpi_definition_id: int = Field(gt=0)
    definition_name: str = Field(min_length=1)
    resolver_status: KpiRevisionSeriesStatus
    anchor_definition_revision_id: str | None
    included_definition_revision_ids: tuple[str, ...]
    comparability_revisions: tuple[KpiRevisionSeriesComparability, ...]
    breaks: tuple[KpiRevisionSeriesBreak, ...]
    raw_current_fact_ids: tuple[int, ...]
    canonical_current_fact_ids: tuple[int, ...]
    legacy_fact_ids: tuple[int, ...]
    revision_aware_fact_ids: tuple[int, ...]
    fact_dispositions: tuple[KpiFactCensusDisposition, ...]
    blocking_reasons: tuple[str, ...]


class OutOfScopeKpiFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_id: int = Field(gt=0)
    ticker: str = Field(min_length=1, max_length=32)
    kpi_definition_id: int = Field(gt=0)
    period_end_status: Literal["within_cutoff", "invalid"] = "within_cutoff"
    reason: Literal["outside_active_portfolio"] = "outside_active_portfolio"


class InvalidInScopeKpiFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_id: int = Field(gt=0)
    ticker: str = Field(min_length=1, max_length=32)
    kpi_definition_id: int = Field(gt=0)
    reason: Literal[
        "definition_missing",
        "definition_ticker_mismatch",
        "invalid_period_end",
    ]


class KpiRevisionShadowCensus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["kpi-revision-shadow-census/v1"] = "kpi-revision-shadow-census/v1"
    scope: Literal["active_portfolio"] = "active_portfolio"
    comparison_contract: Literal["definition_root_current_reader_vs_revision_as_known"] = (
        "definition_root_current_reader_vs_revision_as_known"
    )
    effective_at: datetime
    known_at: datetime
    evaluated_at: datetime
    snapshot_evidence: SnapshotEvidenceState
    roster_observation_status: Literal["observed", "unavailable"]
    population_observation_status: Literal["observed", "unavailable"]
    active_portfolio_tickers: tuple[str, ...]
    roster_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    population_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verifier_code_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    series: tuple[KpiDefinitionShadowCensus, ...]
    out_of_scope_facts: tuple[OutOfScopeKpiFact, ...]
    invalid_in_scope_facts: tuple[InvalidInScopeKpiFact, ...]
    deterministic_readiness: KpiRevisionCensusReadiness
    deterministic_blocking_reasons: tuple[str, ...]
    activation_state: Literal["hold"] = "hold"
    activation_blocking_reasons: tuple[str, ...]
    authorizes_reader_activation: Literal[False] = False
    claims_consumer_parity: Literal[False] = False
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _receipt_is_exact(self) -> Self:
        payload = self.model_dump(mode="json", exclude={"receipt_sha256"})
        if self.receipt_sha256 != _sha256(_canonical_json(payload)):
            raise ValueError("shadow census receipt hash does not match its payload")
        return self


def verify_snapshot_evidence(*, database_path: Path, manifest_path: Path) -> SnapshotEvidenceState:
    """Verify snapshot artifact identity without treating it as approval."""

    database = database_path.expanduser().resolve()
    reject_forbidden_mac_checkout_database(database)
    manifest_file = manifest_path.expanduser().resolve()
    reasons: list[str] = []
    snapshot_sha256: str | None = None
    manifest_sha256: str | None = None
    try:
        snapshot_sha256 = _file_sha256(database)
    except OSError:
        reasons.append("snapshot_database_unavailable")
    try:
        manifest_bytes = manifest_file.read_bytes()
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        manifest = SnapshotManifest.model_validate_json(manifest_bytes)
    except (OSError, ValueError):
        manifest = None
        reasons.append("snapshot_manifest_invalid")
    if manifest is not None:
        if manifest.schema_version != _SUPPORTED_SNAPSHOT_SCHEMA_VERSION:
            reasons.append("snapshot_manifest_schema_unsupported")
        if manifest.code_config_version != _SUPPORTED_SNAPSHOT_CODE_CONFIG_VERSION:
            reasons.append("snapshot_manifest_code_unsupported")
        if Path(manifest.snapshot.path).expanduser().resolve() != database:
            reasons.append("snapshot_manifest_path_mismatch")
        if manifest.snapshot.sha256 != snapshot_sha256:
            reasons.append("snapshot_manifest_hash_mismatch")
        try:
            byte_size = database.stat().st_size
        except OSError:
            byte_size = None
        if manifest.snapshot.byte_size != byte_size:
            reasons.append("snapshot_manifest_size_mismatch")
        if manifest.verification.integrity_check != ("ok",):
            reasons.append("snapshot_manifest_integrity_unverified")
        if manifest.verification.foreign_key_check:
            reasons.append("snapshot_manifest_foreign_keys_unverified")
    wal_path = database.with_name(database.name + "-wal")
    shm_path = database.with_name(database.name + "-shm")
    try:
        if wal_path.stat().st_size:
            reasons.append("snapshot_has_nonempty_wal")
    except OSError:
        pass
    try:
        if shm_path.stat().st_size:
            reasons.append("snapshot_has_shm_sidecar")
    except OSError:
        pass
    if reasons or snapshot_sha256 is None or manifest_sha256 is None:
        return SnapshotEvidenceState(
            status="unverified",
            snapshot_path=str(database),
            manifest_path=str(manifest_file),
            snapshot_sha256=snapshot_sha256,
            manifest_sha256=manifest_sha256,
            blocking_reasons=tuple(dict.fromkeys(reasons)),
        )
    return SnapshotEvidenceState(
        status="manifest_matched",
        snapshot_path=str(database),
        manifest_path=str(manifest_file),
        snapshot_sha256=snapshot_sha256,
        manifest_sha256=manifest_sha256,
        blocking_reasons=(),
    )


_NON_BLOCKING_EXCLUSIONS = frozenset(
    {
        KpiRevisionSeriesExclusionReason.NOT_COMPARABLE.value,
        KpiRevisionSeriesExclusionReason.DISCONTINUED_DEFINITION.value,
        KpiRevisionSeriesExclusionReason.SEMANTIC_NOT_ADMITTED.value,
    }
)


def _require_snapshot_evidence_matches_connection(
    conn: sqlite3.Connection,
    evidence: SnapshotEvidenceState,
    *,
    reject_preexisting_transaction: bool,
) -> None:
    if evidence.status != "manifest_matched":
        return
    if reject_preexisting_transaction and conn.in_transaction:
        raise ValueError(
            "snapshot evidence does not match a connection with a pre-existing transaction"
        )
    rows = conn.execute("PRAGMA database_list").fetchall()
    main_paths = [str(row[2]) for row in rows if str(row[1]) == "main" and str(row[2])]
    if len(main_paths) != 1 or evidence.snapshot_path is None or evidence.manifest_path is None:
        raise ValueError("snapshot evidence does not match the census connection")
    connection_path = Path(main_paths[0]).expanduser().resolve()
    if connection_path != Path(evidence.snapshot_path).expanduser().resolve():
        raise ValueError("snapshot evidence does not match the census connection")
    observed = verify_snapshot_evidence(
        database_path=connection_path,
        manifest_path=Path(evidence.manifest_path),
    )
    if observed != evidence:
        raise ValueError("snapshot evidence does not match the census connection")


def audit_kpi_revision_shadow_census(
    conn: sqlite3.Connection,
    *,
    effective_at: datetime,
    known_at: datetime,
    evaluated_at: datetime,
    snapshot_evidence: SnapshotEvidenceState,
) -> KpiRevisionShadowCensus:
    """Census the complete database-owned active portfolio in one read snapshot."""

    effective = _utc(effective_at)
    known = _utc(known_at)
    evaluated = _utc(evaluated_at)
    if effective > known or known > evaluated:
        raise ValueError("census clocks must satisfy effective_at <= known_at <= evaluated_at")
    _require_snapshot_evidence_matches_connection(
        conn,
        snapshot_evidence,
        reject_preexisting_transaction=True,
    )

    original_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    owns_snapshot = not conn.in_transaction
    if owns_snapshot:
        conn.execute("BEGIN")
    try:
        result = _audit_snapshot(
            conn,
            effective_at=effective,
            known_at=known,
            evaluated_at=evaluated,
            snapshot_evidence=snapshot_evidence,
        )
        _require_snapshot_evidence_matches_connection(
            conn,
            snapshot_evidence,
            reject_preexisting_transaction=False,
        )
        return result
    finally:
        if owns_snapshot:
            conn.rollback()
        conn.row_factory = original_row_factory


def _audit_snapshot(
    conn: sqlite3.Connection,
    *,
    effective_at: datetime,
    known_at: datetime,
    evaluated_at: datetime,
    snapshot_evidence: SnapshotEvidenceState,
) -> KpiRevisionShadowCensus:
    schema_blockers = _schema_blockers(conn)
    roster_available = _required_columns_available(
        conn,
        "tracked_companies",
        {"ticker", "list_type", "archived_at"},
    )
    population_available = not schema_blockers
    roster = _active_portfolio_tickers(conn) if roster_available else ()
    series: tuple[KpiDefinitionShadowCensus, ...] = ()
    out_of_scope: tuple[OutOfScopeKpiFact, ...] = ()
    invalid_in_scope: tuple[InvalidInScopeKpiFact, ...] = ()
    if population_available:
        series = tuple(
            _census_definition(
                conn,
                ticker=str(row["ticker"]),
                definition_id=int(row["id"]),
                definition_name=str(row["name"]),
                effective_at=effective_at,
                known_at=known_at,
            )
            for row in conn.execute(
                "SELECT definition.id,definition.ticker,definition.name "
                "FROM kpi_definitions definition "
                "JOIN tracked_companies company ON UPPER(company.ticker)=UPPER(definition.ticker) "
                "WHERE company.list_type='portfolio' AND company.archived_at IS NULL "
                "ORDER BY UPPER(definition.ticker),definition.id"
            )
        )
        out_of_scope = _out_of_scope_facts(conn, effective_at=effective_at)
        invalid_in_scope = _invalid_in_scope_facts(conn, effective_at=effective_at)

    blockers = set(schema_blockers)
    if roster_available and not roster:
        blockers.add("active_portfolio_roster_empty")
    if population_available and not series:
        blockers.add("portfolio_kpi_population_empty")
    represented_tickers = {item.ticker for item in series}
    if population_available:
        blockers.update(
            f"portfolio_ticker_without_kpi_definition:{ticker}"
            for ticker in roster
            if ticker not in represented_tickers
        )
    if invalid_in_scope:
        blockers.add("invalid_in_scope_fact_identity")
    for item in series:
        blockers.update(item.blocking_reasons)
    explained_differences = any(
        item.legacy_fact_ids != item.revision_aware_fact_ids for item in series
    )
    readiness = (
        KpiRevisionCensusReadiness.BLOCKED
        if blockers
        else (
            KpiRevisionCensusReadiness.READY_WITH_EXPLAINED_DIFFERENCES
            if explained_differences
            else KpiRevisionCensusReadiness.READY
        )
    )
    activation_blockers = ["evidence_authority_unverified", "owner_activation_required"]
    if readiness is KpiRevisionCensusReadiness.BLOCKED:
        activation_blockers.append("deterministic_readiness_blocked")
    population_material = {
        "definitions": [
            {
                "definition_id": item.kpi_definition_id,
                "canonical_current_fact_ids": list(item.canonical_current_fact_ids),
                "raw_current_fact_ids": list(item.raw_current_fact_ids),
                "ticker": item.ticker,
            }
            for item in series
        ],
        "invalid_in_scope_facts": [item.model_dump(mode="json") for item in invalid_in_scope],
        "population_observation_status": ("observed" if population_available else "unavailable"),
        "roster": list(roster),
        "roster_observation_status": "observed" if roster_available else "unavailable",
    }
    payload = {
        "schema_version": "kpi-revision-shadow-census/v1",
        "scope": "active_portfolio",
        "comparison_contract": "definition_root_current_reader_vs_revision_as_known",
        "effective_at": effective_at,
        "known_at": known_at,
        "evaluated_at": evaluated_at,
        "snapshot_evidence": snapshot_evidence,
        "roster_observation_status": "observed" if roster_available else "unavailable",
        "population_observation_status": ("observed" if population_available else "unavailable"),
        "active_portfolio_tickers": roster,
        "roster_sha256": _sha256(
            _canonical_json(
                {
                    "status": "observed" if roster_available else "unavailable",
                    "tickers": list(roster),
                }
            )
        ),
        "population_sha256": _sha256(_canonical_json(population_material)),
        "verifier_code_sha256": verifier_code_sha256(),
        "series": series,
        "out_of_scope_facts": out_of_scope,
        "invalid_in_scope_facts": invalid_in_scope,
        "deterministic_readiness": readiness,
        "deterministic_blocking_reasons": tuple(sorted(blockers)),
        "activation_state": "hold",
        "activation_blocking_reasons": tuple(activation_blockers),
        "authorizes_reader_activation": False,
        "claims_consumer_parity": False,
    }
    return KpiRevisionShadowCensus.model_validate(
        payload | {"receipt_sha256": _sha256(_canonical_json(payload))}
    )


def verifier_code_sha256() -> str:
    return verifier_source_artifact_sha256(
        {
            "compute/kpi_resolver.py": _SOURCE_ROOT / "compute" / "kpi_resolver.py",
            "compute/kpi_revision_shadow_census.py": Path(__file__),
            "execution/audit_kpi_revision_shadow_census.py": (
                _REPOSITORY_ROOT / "execution" / "audit_kpi_revision_shadow_census.py"
            ),
            "pipeline/kpi_definition_revisions.py": (
                _SOURCE_ROOT / "pipeline" / "kpi_definition_revisions.py"
            ),
            "pipeline/kpi_semantics.py": _SOURCE_ROOT / "pipeline" / "kpi_semantics.py",
            "provenance/financial_fact_resolution.py": (
                _SOURCE_ROOT / "provenance" / "financial_fact_resolution.py"
            ),
            "provenance/verifier_identity.py": (
                _SOURCE_ROOT / "provenance" / "verifier_identity.py"
            ),
            "sqlite_runtime.py": _SOURCE_ROOT / "sqlite_runtime.py",
            "sqlite_snapshot.py": _SOURCE_ROOT / "sqlite_snapshot.py",
        }
    )


def _schema_blockers(conn: sqlite3.Connection) -> tuple[str, ...]:
    required = {
        "tracked_companies": {"ticker", "list_type", "archived_at"},
        "kpi_definitions": {"id", "ticker", "name"},
        "kpi_facts": {
            "id",
            "ticker",
            "period_end",
            "kpi_definition_id",
            "supersedes_id",
        },
    }
    blockers: list[str] = []
    for table, columns in required.items():
        observed = _table_columns(conn, table)
        if not observed:
            blockers.append(f"required_authority_unavailable:{table}")
            continue
        blockers.extend(
            f"required_column_unavailable:{table}:{column}" for column in sorted(columns - observed)
        )
    if blockers:
        return tuple(sorted(set(blockers)))

    current_view = "v_kpi_facts_resolved_current"
    current_view_type = _schema_object_type(conn, current_view)
    has_resolution_ledger = _schema_object_type(conn, "fact_observation_revisions") == "table"
    if has_resolution_ledger and current_view_type != "view":
        blockers.append(f"required_authority_unavailable:{current_view}")
    elif current_view_type == "view":
        try:
            current_columns = _table_columns(conn, current_view)
        except sqlite3.OperationalError:
            # SQLite resolves a view's dependencies during this PRAGMA.  Keep a
            # broken canonical authority inside the typed unavailable receipt;
            # errors from later population queries remain visible.
            blockers.append(f"required_authority_invalid:{current_view}")
        else:
            blockers.extend(
                f"required_column_unavailable:{current_view}:{column}"
                for column in sorted(
                    {"id", "ticker", "period_end", "kpi_definition_id"} - current_columns
                )
            )

    semantic_table = "kpi_fact_semantic_contexts"
    if _schema_object_type(conn, semantic_table) != "table":
        blockers.append(f"required_authority_unavailable:{semantic_table}")
    else:
        semantic_columns = _table_columns(conn, semantic_table)
        semantic_read_columns = {
            "id",
            "kpi_fact_id",
            "revision",
            "supersedes_context_id",
            "metric_name_as_reported",
            "accounting_basis",
            "consolidation_scope",
            "dimensions_json",
            "unit_scale",
            "status",
            "publication_lane",
            "knowledge_at",
            "created_at",
            "kpi_definition_revision_id",
        }
        blockers.extend(
            f"required_column_unavailable:{semantic_table}:{column}"
            for column in sorted(semantic_read_columns - semantic_columns)
        )

    blockers.extend(revision_aware_kpi_schema_blockers(conn))
    return tuple(sorted(set(blockers)))


def _schema_object_type(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute(
        "SELECT type FROM sqlite_master WHERE name=? AND type IN ('table','view')",
        (name,),
    ).fetchone()
    return None if row is None else str(row[0])


def _table_columns(conn: sqlite3.Connection, table: str) -> frozenset[str]:
    return frozenset(str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})"))


def _required_columns_available(
    conn: sqlite3.Connection,
    table: str,
    required: set[str],
) -> bool:
    observed = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    return required.issubset(observed)


def _active_portfolio_tickers(conn: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(
        str(row[0]).upper()
        for row in conn.execute(
            "SELECT DISTINCT UPPER(ticker) FROM tracked_companies "
            "WHERE list_type='portfolio' AND archived_at IS NULL ORDER BY UPPER(ticker)"
        )
    )


def _census_definition(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    definition_id: int,
    definition_name: str,
    effective_at: datetime,
    known_at: datetime,
) -> KpiDefinitionShadowCensus:
    relation = canonical_fact_relation(conn, "kpi_facts").sql
    semantic_join, semantic_where = semantic_admission_sql(conn, fail_closed=True)
    semantic_identity = semantic_series_identity_sql(conn, fact_relation=relation)
    cutoff = effective_at.isoformat()
    raw_ids = tuple(
        int(row[0])
        for row in conn.execute(
            "SELECT kf.id FROM kpi_facts kf "
            "WHERE UPPER(kf.ticker)=UPPER(?) AND kf.kpi_definition_id=? "
            "AND NOT EXISTS (SELECT 1 FROM kpi_facts successor "
            "WHERE successor.supersedes_id=kf.id) "
            "AND datetime(kf.period_end)<=datetime(?) ORDER BY kf.id",
            (ticker, definition_id, cutoff),
        )
    )
    canonical_current_ids = tuple(
        int(row[0])
        for row in conn.execute(
            f"SELECT kf.id FROM {relation} kf "  # nosec B608 -- resolver-owned relation
            "WHERE UPPER(kf.ticker)=UPPER(?) AND kf.kpi_definition_id=? "
            "AND datetime(kf.period_end)<=datetime(?) ORDER BY kf.id",
            (ticker, definition_id, cutoff),
        )
    )
    legacy_ids = tuple(
        int(row[0])
        for row in conn.execute(
            f"SELECT kf.id FROM {relation} kf {semantic_join} "  # nosec B608
            "WHERE UPPER(kf.ticker)=UPPER(?) AND kf.kpi_definition_id=? "
            f"AND datetime(kf.period_end)<=datetime(?) AND {semantic_where} "
            f"AND {semantic_identity} ORDER BY kf.id",  # nosec B608
            (ticker, definition_id, cutoff),
        )
    )
    resolution = resolve_revision_aware_kpi_series(
        conn,
        kpi_definition_id=definition_id,
        effective_at=effective_at,
        known_at=known_at,
    )
    revision_ids = resolution.eligible_fact_ids
    comparability_by_revision = {
        item.related_definition_revision_id: item for item in resolution.comparability_revisions
    }
    exclusion_by_fact = {
        fact_id: exclusion.reason.value
        for exclusion in resolution.exclusions
        for fact_id in exclusion.fact_ids
    }
    all_ids = sorted(set(raw_ids) | set(legacy_ids) | set(revision_ids) | set(exclusion_by_fact))
    lineage_by_fact = _semantic_lineage_by_fact(
        conn,
        all_fact_ids=tuple(all_ids),
        known_at=known_at,
    )
    dispositions: list[KpiFactCensusDisposition] = []
    blockers: set[str] = set()
    missing_current_authority = set(raw_ids) - set(canonical_current_ids)
    legacy_set = set(legacy_ids)
    revision_set = set(revision_ids)
    for fact_id in all_ids:
        lineage = lineage_by_fact.get(fact_id)
        definition_revision_id = None if lineage is None else lineage.definition_revision_id
        if fact_id in missing_current_authority:
            reason = "missing_current_resolution_authority"
            comparability_revision_id = None
            blockers.add(reason)
        elif fact_id in legacy_set and fact_id in revision_set:
            reason = "eligible_in_both"
            comparability_revision_id = None
        elif fact_id in legacy_set:
            reason = exclusion_by_fact.get(
                fact_id,
                (
                    resolution.status.value
                    if resolution.status
                    in {
                        KpiRevisionSeriesStatus.LEGACY_UNBOUND,
                        KpiRevisionSeriesStatus.QUARANTINED_DEFINITION,
                        KpiRevisionSeriesStatus.DISCONTINUED,
                        KpiRevisionSeriesStatus.HISTORICAL_FACT_AUTHORITY_UNAVAILABLE,
                    }
                    else "unattributed_legacy_only"
                ),
            )
            if reason not in _NON_BLOCKING_EXCLUSIONS:
                blockers.add(reason)
            comparability_revision_id = None
        elif fact_id in revision_set:
            comparability = (
                comparability_by_revision.get(definition_revision_id)
                if definition_revision_id is not None and fact_id not in raw_ids
                else None
            )
            if comparability is None:
                reason = "unexplained_revision_only"
                comparability_revision_id = None
                blockers.add(reason)
            else:
                reason = "explicit_revision_comparability_expansion"
                comparability_revision_id = comparability.comparability_revision_id
        else:
            reason = exclusion_by_fact.get(fact_id, "excluded_by_current_legacy_semantics")
            comparability_revision_id = None
        dispositions.append(
            KpiFactCensusDisposition(
                fact_id=fact_id,
                reason=reason,
                semantic_context_id=None if lineage is None else lineage.context_id,
                semantic_context_revision=None if lineage is None else lineage.context_revision,
                definition_revision_id=definition_revision_id,
                comparability_revision_id=comparability_revision_id,
            )
        )
    if resolution.status not in {
        KpiRevisionSeriesStatus.ELIGIBLE,
        KpiRevisionSeriesStatus.ELIGIBLE_WITH_BREAK,
    }:
        blockers.add(resolution.status.value)
    return KpiDefinitionShadowCensus(
        ticker=ticker.upper(),
        kpi_definition_id=definition_id,
        definition_name=definition_name,
        resolver_status=resolution.status,
        anchor_definition_revision_id=resolution.anchor_definition_revision_id,
        included_definition_revision_ids=resolution.included_definition_revision_ids,
        comparability_revisions=resolution.comparability_revisions,
        breaks=resolution.breaks,
        raw_current_fact_ids=raw_ids,
        canonical_current_fact_ids=canonical_current_ids,
        legacy_fact_ids=legacy_ids,
        revision_aware_fact_ids=revision_ids,
        fact_dispositions=tuple(dispositions),
        blocking_reasons=tuple(sorted(blockers)),
    )


def _out_of_scope_facts(
    conn: sqlite3.Connection, *, effective_at: datetime
) -> tuple[OutOfScopeKpiFact, ...]:
    return tuple(
        OutOfScopeKpiFact(
            fact_id=int(row[0]),
            ticker=str(row[1]).upper(),
            kpi_definition_id=int(row[2]),
            period_end_status="invalid" if int(row[3]) else "within_cutoff",
        )
        for row in conn.execute(
            "SELECT fact.id,fact.ticker,fact.kpi_definition_id,"
            "datetime(fact.period_end) IS NULL AS invalid_period_end FROM kpi_facts fact "
            "WHERE NOT EXISTS (SELECT 1 FROM kpi_facts successor "
            "WHERE successor.supersedes_id=fact.id) "
            "AND (datetime(fact.period_end)<=datetime(?) OR datetime(fact.period_end) IS NULL) "
            "AND NOT EXISTS ("
            "SELECT 1 FROM tracked_companies company WHERE "
            "UPPER(company.ticker)=UPPER(fact.ticker) AND company.list_type='portfolio' "
            "AND company.archived_at IS NULL) ORDER BY fact.id",
            (effective_at.isoformat(),),
        )
    )


def _invalid_in_scope_facts(
    conn: sqlite3.Connection, *, effective_at: datetime
) -> tuple[InvalidInScopeKpiFact, ...]:
    return tuple(
        InvalidInScopeKpiFact(
            fact_id=int(row["fact_id"]),
            ticker=str(row["fact_ticker"]).upper(),
            kpi_definition_id=int(row["kpi_definition_id"]),
            reason=(
                "invalid_period_end"
                if int(row["invalid_period_end"])
                else (
                    "definition_missing"
                    if row["definition_id"] is None
                    else "definition_ticker_mismatch"
                )
            ),
        )
        for row in conn.execute(
            "SELECT fact.id AS fact_id,fact.ticker AS fact_ticker,"
            "fact.kpi_definition_id,definition.id AS definition_id,"
            "datetime(fact.period_end) IS NULL AS invalid_period_end "
            "FROM kpi_facts fact "
            "JOIN tracked_companies company ON UPPER(company.ticker)=UPPER(fact.ticker) "
            "LEFT JOIN kpi_definitions definition ON definition.id=fact.kpi_definition_id "
            "WHERE company.list_type='portfolio' AND company.archived_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM kpi_facts successor "
            "WHERE successor.supersedes_id=fact.id) "
            "AND (datetime(fact.period_end) IS NULL OR (datetime(fact.period_end)<=datetime(?) "
            "AND (definition.id IS NULL OR UPPER(definition.ticker)<>UPPER(fact.ticker)))) "
            "ORDER BY fact.id",
            (effective_at.isoformat(),),
        )
    )


@dataclass(frozen=True)
class _SemanticLineage:
    context_id: int
    context_revision: int
    definition_revision_id: str | None


def _semantic_lineage_by_fact(
    conn: sqlite3.Connection,
    *,
    all_fact_ids: tuple[int, ...],
    known_at: datetime,
) -> dict[int, _SemanticLineage]:
    if not all_fact_ids:
        return {}
    if not _required_columns_available(
        conn,
        "kpi_fact_semantic_contexts",
        {
            "id",
            "kpi_fact_id",
            "revision",
            "supersedes_context_id",
            "knowledge_at",
            "created_at",
            "kpi_definition_revision_id",
        },
    ):
        return {}
    placeholders = ",".join("?" for _ in all_fact_ids)
    rows = conn.execute(
        "SELECT context.kpi_fact_id,context.id,context.revision,"
        "context.kpi_definition_revision_id "
        "FROM kpi_fact_semantic_contexts context WHERE "
        f"context.kpi_fact_id IN ({placeholders}) "  # nosec B608 -- integer ids
        "AND datetime(context.knowledge_at)<=datetime(?) "
        "AND datetime(context.created_at)<=datetime(?) "
        "AND NOT EXISTS (SELECT 1 FROM kpi_fact_semantic_contexts successor "
        "WHERE successor.supersedes_context_id=context.id "
        "AND datetime(successor.knowledge_at)<=datetime(?) "
        "AND datetime(successor.created_at)<=datetime(?)) ORDER BY context.kpi_fact_id",
        (*all_fact_ids, *(known_at.isoformat() for _ in range(4))),
    ).fetchall()
    result: dict[int, _SemanticLineage] = {}
    for row in rows:
        fact_id = int(row["kpi_fact_id"])
        if fact_id in result:
            raise RuntimeError(f"KPI fact {fact_id} has ambiguous semantic heads as known")
        result[fact_id] = _SemanticLineage(
            context_id=int(row["id"]),
            context_revision=int(row["revision"]),
            definition_revision_id=(
                None
                if row["kpi_definition_revision_id"] is None
                else str(row["kpi_definition_revision_id"])
            ),
        )
    return result


__all__ = [
    "InvalidInScopeKpiFact",
    "KpiDefinitionShadowCensus",
    "KpiFactCensusDisposition",
    "KpiRevisionCensusReadiness",
    "KpiRevisionShadowCensus",
    "OutOfScopeKpiFact",
    "SnapshotEvidenceState",
    "audit_kpi_revision_shadow_census",
    "verifier_code_sha256",
    "verify_snapshot_evidence",
]
