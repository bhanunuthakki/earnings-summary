"""Append-only, verifier-derived population, audit, parity, and cutover receipts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from runtime.deterministic import as_utc as _utc

REQUIRED_POPULATION_PLANES = (
    "identity_scope",
    "source_fact_ontology",
    "canonical_resolution",
    "canonical_projection",
    "document_processing",
    "research_snapshot",
    "retrieval_runtime",
)
REQUIRED_CUTOVER_AUDIT_GATES = (
    "source_fact_publications",
    "source_fact_publication_stream",
    "filing_xbrl_dispositions",
    "ontology_snapshots",
    "canonical_resolution_snapshots",
    "canonical_projection_generations",
    "document_processing_evidence",
    "document_processing_snapshots",
    "research_snapshots",
    "embedding_runtime_promotions",
    "embedding_runtime_artifacts",
    "embedding_runtime_projection_seals",
)
PopulationPlaneName = Literal[
    "identity_scope",
    "source_fact_ontology",
    "canonical_resolution",
    "canonical_projection",
    "document_processing",
    "research_snapshot",
    "retrieval_runtime",
]
ReceiptStatus = Literal["complete", "blocked"]
AUDIT_RECEIPT_VERSION = "population-cutover-audit.v2"
CUTOVER_RECEIPT_VERSION = "population-cutover-receipt.v3"
_FINDING_CODE_CHARACTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"  # pragma: allowlist secret
PLANE_EXCLUSION_REASON_CODES: dict[str, frozenset[str]] = {
    "identity_scope": frozenset(),
    "source_fact_ontology": frozenset(
        {
            "after_data_cutoff",
            "derived_without_formula_lineage",
            "incomplete_extraction_run",
            "llm_synthesized_source",
            "no_selected_subject_binding_as_of_cutoff",
            "unapproved_document_type",
        }
    ),
    "canonical_resolution": frozenset(),
    "canonical_projection": frozenset(),
    "document_processing": frozenset(
        {
            "expected_document_not_current",
            "sec_form_outside_reporting_policy",
            "sec_supporting_artifact",
            "sec_xbrl_report_attachment",
        }
    ),
    "research_snapshot": frozenset(),
    "retrieval_runtime": frozenset(),
}


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def canonical_json(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_canonical_json_default,
    )


def _canonical_json_default(value: object) -> str:
    if isinstance(value, datetime):
        return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _sql_sha256(value: object) -> str:
    return digest_text(str(value))


def _validate_sha256(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("must be a lowercase SHA-256 hex digest")
    return value


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


class PopulationTemporalScope(_FrozenModel):
    """The economic knowledge clock and the later system-observation clock."""

    knowledge_cutoff: datetime
    observed_through: datetime

    @model_validator(mode="after")
    def _ordered_clocks(self) -> Self:
        knowledge = _utc(self.knowledge_cutoff)
        observed = _utc(self.observed_through)
        if knowledge > observed:
            raise ValueError("knowledge_cutoff must not follow observed_through")
        return self


class PopulationArtifactSetCommitment(_FrozenModel):
    """Memory-bounded commitment to one explicitly selected persisted artifact set."""

    table: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9_]+$")
    row_count: int = Field(ge=0)
    rows_sha256: str
    selection_policy_id: str = Field(min_length=1, max_length=256)

    _rows_sha = field_validator("rows_sha256")(_validate_sha256)


class PopulationPlaneVerification(_FrozenModel):
    """One standardized verifier result for a complete temporal plane."""

    plane_name: PopulationPlaneName
    expected_count: int = Field(gt=0)
    materialized_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    exclusion_counts: dict[str, int]
    input_commitment_sha256: str
    output_commitment_sha256: str
    artifact_sets: tuple[PopulationArtifactSetCommitment, ...]
    details: dict[str, JsonValue]

    _input_sha = field_validator("input_commitment_sha256")(_validate_sha256)
    _output_sha = field_validator("output_commitment_sha256")(_validate_sha256)

    @model_validator(mode="after")
    def _exact_commitments(self) -> Self:
        if self.expected_count != (
            self.materialized_count + self.excluded_count + self.failed_count
        ):
            raise ValueError("population verification counts do not conserve")
        if sum(self.exclusion_counts.values()) != self.excluded_count:
            raise ValueError("population verification exclusions do not conserve")
        allowed = PLANE_EXCLUSION_REASON_CODES[self.plane_name]
        if any(
            reason not in allowed or isinstance(count, bool) or count < 0
            for reason, count in self.exclusion_counts.items()
        ):
            raise ValueError("population verification exclusion contract is invalid")
        ordered_sets = tuple(
            sorted(
                self.artifact_sets,
                key=lambda item: (item.table, item.selection_policy_id),
            )
        )
        if self.artifact_sets != ordered_sets or len(
            {(item.table, item.selection_policy_id) for item in self.artifact_sets}
        ) != len(self.artifact_sets):
            raise ValueError("population artifact sets must be sorted and unique")
        if not self.artifact_sets:
            raise ValueError("population verification requires persisted artifact commitments")
        output_material = {
            "artifact_sets": [item.model_dump(mode="json") for item in self.artifact_sets],
            "details": self.details,
            "exclusion_counts": dict(sorted(self.exclusion_counts.items())),
            "expected_count": self.expected_count,
            "failed_count": self.failed_count,
            "materialized_count": self.materialized_count,
            "plane_name": self.plane_name,
        }
        if self.output_commitment_sha256 != digest_text(canonical_json(output_material)):
            raise ValueError("population verification output commitment is invalid")
        return self


def stream_population_artifact_set(
    conn: sqlite3.Connection,
    *,
    table: str,
    query: str,
    params: tuple[object, ...],
    selection_policy_id: str,
    fetch_size: int = 250,
) -> PopulationArtifactSetCommitment:
    """Stream an explicit, dual-clock-scoped artifact query into one exact digest.

    Callers own the query and temporal predicate.  This helper deliberately
    refuses inferred table scans: every query must expose the immutable artifact
    identity, payload and seal commitments, and both actual clocks in a stable
    order.
    """

    normalized = " ".join(query.strip().split()).upper()
    if not normalized.startswith("SELECT ") and not normalized.startswith("WITH "):
        raise ValueError("population artifact query must be an explicit SELECT")
    if " ORDER BY " not in f" {normalized} ":
        raise ValueError("population artifact query must declare deterministic ordering")
    if fetch_size < 1:
        raise ValueError("population artifact fetch_size must be positive")
    if not selection_policy_id:
        raise ValueError("population artifact selection_policy_id is required")
    conn.create_function("fact_sha256", 1, _sql_sha256)
    original_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(query, params)
        required = (
            "artifact_id",
            "payload_sha256",
            "seal_sha256",
            "knowledge_at",
            "recorded_at",
        )
        columns = tuple(item[0] for item in (cursor.description or ()))
        if columns != required:
            raise ValueError(
                "population artifact query must return exactly artifact_id, "
                "payload_sha256, seal_sha256, knowledge_at, recorded_at"
            )
        digest = hashlib.sha256()
        digest.update(
            canonical_json(
                {
                    "columns": list(required),
                    "selection_policy_id": selection_policy_id,
                    "table": table,
                }
            ).encode()
        )
        row_count = 0
        while batch := cursor.fetchmany(fetch_size):
            for row in batch:
                payload_sha = _validate_sha256(str(row["payload_sha256"]))
                seal_sha = _validate_sha256(str(row["seal_sha256"]))
                knowledge_at = _utc(datetime.fromisoformat(str(row["knowledge_at"])))
                recorded_at = _utc(datetime.fromisoformat(str(row["recorded_at"])))
                digest.update(b"\n")
                digest.update(
                    canonical_json(
                        {
                            "artifact_id": str(row["artifact_id"]),
                            "knowledge_at": knowledge_at,
                            "payload_sha256": payload_sha,
                            "recorded_at": recorded_at,
                            "seal_sha256": seal_sha,
                        }
                    ).encode()
                )
                row_count += 1
    finally:
        conn.row_factory = original_row_factory
    return PopulationArtifactSetCommitment(
        table=table,
        row_count=row_count,
        rows_sha256=digest.hexdigest(),
        selection_policy_id=selection_policy_id,
    )


class _CutoverWriteAuthority:
    __slots__ = ()


_CUTOVER_WRITE_AUTHORITY = _CutoverWriteAuthority()


def _require_cutover_write_authority(authority: object) -> None:
    if authority is not _CUTOVER_WRITE_AUTHORITY:
        raise RuntimeError("population cutover writes require internal verifier authority")


def _validate_audit_evidence(
    evidence: dict[str, JsonValue],
    *,
    temporal_scope: PopulationTemporalScope,
    eligible_count: int,
    verified_count: int,
    failed_count: int,
) -> None:
    required_keys = {
        "coverage",
        "findings",
        "gate_evidence",
        "has_blockers",
        "schema_version",
        "tables_present",
        "watermark_material",
        "watermark_sha256",
    }
    if set(evidence) != required_keys:
        raise ValueError("population audit evidence contract is not exact")
    coverage = evidence["coverage"]
    gate_evidence = evidence["gate_evidence"]
    findings = evidence["findings"]
    tables_present = evidence["tables_present"]
    if evidence["schema_version"] != "data-cutover-readiness-audit/v1":
        raise ValueError("population audit schema version is invalid")
    if not isinstance(findings, list) or not isinstance(tables_present, list):
        raise ValueError("population audit findings and tables must be arrays")
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != {
            "code",
            "count",
            "query_context",
            "remediation",
            "samples",
            "severity",
        }:
            raise ValueError("population audit finding contract is invalid")
        code = finding["code"]
        count = finding["count"]
        query_context = finding["query_context"]
        remediation = finding["remediation"]
        samples = finding["samples"]
        severity = finding["severity"]
        if (
            not isinstance(code, str)
            or not code
            or any(character not in _FINDING_CODE_CHARACTERS for character in code)
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
            or not isinstance(query_context, str)
            or remediation not in {"repairable", "backfill", "reingest", "manual", "hard-stop"}
            or not isinstance(samples, list)
            or any(not isinstance(sample, str) for sample in samples)
            or severity not in {"advisory", "warning", "blocker"}
        ):
            raise ValueError("population audit finding value is invalid")
        if severity == "blocker":
            raise ValueError("population audit evidence cannot contain blocking findings")
    if any(not isinstance(table, str) or not table for table in tables_present):
        raise ValueError("population audit tables_present must be sorted and unique")
    present_table_names = [cast(str, table) for table in tables_present]
    if present_table_names != sorted(set(present_table_names)):
        raise ValueError("population audit tables_present must be sorted and unique")
    if not isinstance(coverage, list) or not isinstance(gate_evidence, list):
        raise ValueError("population audit coverage and gate evidence must be arrays")
    coverage_by_gate: dict[str, dict[str, JsonValue]] = {}
    for raw in coverage:
        if (
            not isinstance(raw, dict)
            or set(raw) != {"eligible_count", "failed_count", "gate", "verified_count"}
            or not isinstance(raw.get("gate"), str)
        ):
            raise ValueError("population audit coverage row is invalid")
        coverage_by_gate[str(raw["gate"])] = raw
    expected_gates = tuple(sorted(REQUIRED_CUTOVER_AUDIT_GATES))
    if tuple(sorted(coverage_by_gate)) != expected_gates or len(coverage) != len(expected_gates):
        raise ValueError("population audit coverage must contain exactly 13 gates")
    totals = {"eligible_count": 0, "verified_count": 0, "failed_count": 0}
    for row in coverage_by_gate.values():
        counts: dict[str, int] = {}
        for key in totals:
            raw_count = row.get(key)
            if not isinstance(raw_count, int) or isinstance(raw_count, bool) or raw_count < 0:
                raise ValueError("population audit coverage counts are invalid")
            counts[key] = raw_count
            totals[key] += raw_count
        if counts["eligible_count"] != (counts["verified_count"] + counts["failed_count"]):
            raise ValueError("population audit gate counts do not conserve")
    if totals != {
        "eligible_count": eligible_count,
        "verified_count": verified_count,
        "failed_count": failed_count,
    }:
        raise ValueError("population audit aggregate counts differ from coverage")

    gate_digests: list[dict[str, str]] = []
    seen_evidence: set[str] = set()
    for raw in gate_evidence:
        if not isinstance(raw, dict) or set(raw) != {
            "gate",
            "gate_evidence_sha256",
            "tables",
        }:
            raise ValueError("population audit gate evidence row is invalid")
        gate = raw["gate"]
        tables = raw["tables"]
        digest = raw["gate_evidence_sha256"]
        if not isinstance(gate, str) or gate in seen_evidence or gate not in expected_gates:
            raise ValueError("population audit gate evidence identity is invalid")
        if not isinstance(tables, list) or not isinstance(digest, str):
            raise ValueError("population audit gate evidence payload is invalid")
        table_names: set[str] = set()
        for table in tables:
            if not isinstance(table, dict) or set(table) != {
                "row_count",
                "rows_sha256",
                "table",
            }:
                raise ValueError("population audit table evidence row is invalid")
            table_name = table["table"]
            row_count = table["row_count"]
            rows_sha256 = table["rows_sha256"]
            if (
                not isinstance(table_name, str)
                or table_name in table_names
                or not isinstance(row_count, int)
                or isinstance(row_count, bool)
                or row_count < 0
                or not isinstance(rows_sha256, str)
            ):
                raise ValueError("population audit table evidence value is invalid")
            _validate_sha256(rows_sha256)
            table_names.add(table_name)
        expected_digest = digest_text(canonical_json({"gate": gate, "tables": tables}))
        if _validate_sha256(digest) != expected_digest:
            raise ValueError("population audit gate evidence commitment is invalid")
        seen_evidence.add(gate)
        gate_digests.append({"gate": gate, "gate_evidence_sha256": digest})
    if tuple(sorted(seen_evidence)) != expected_gates:
        raise ValueError("population audit evidence must contain exactly 13 gates")
    gate_digests.sort(key=lambda item: item["gate"])
    expected_material: dict[str, JsonValue] = {
        "knowledge_cutoff": _timestamp(temporal_scope.knowledge_cutoff),
        "observed_through": _timestamp(temporal_scope.observed_through),
        "gates": cast(JsonValue, gate_digests),
    }
    if evidence["watermark_material"] != expected_material:
        raise ValueError("population audit watermark material is invalid")
    watermark = evidence["watermark_sha256"]
    if not isinstance(watermark, str) or _validate_sha256(watermark) != digest_text(
        canonical_json(expected_material)
    ):
        raise ValueError("population audit watermark commitment is invalid")
    if evidence["has_blockers"] is not False:
        raise ValueError("population audit evidence cannot contain blockers")


def population_run_identity(
    policy_config_sha256: str,
    source_snapshot_sha256: str,
    temporal_scope: PopulationTemporalScope,
) -> str:
    payload = canonical_json(
        {
            "knowledge_cutoff": _utc(temporal_scope.knowledge_cutoff),
            "observed_through": _utc(temporal_scope.observed_through),
            "policy_config_sha256": _validate_sha256(policy_config_sha256),
            "source_snapshot_sha256": _validate_sha256(source_snapshot_sha256),
            "version": "population-run-identity.v2",
        }
    )
    return "population-run:" + digest_text(payload)


class PopulationRun(_FrozenModel):
    population_run_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    policy_name: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=64)
    policy_config_sha256: str
    source_snapshot_sha256: str
    temporal_scope: PopulationTemporalScope
    verified_at: datetime

    _policy_sha = field_validator("policy_config_sha256")(_validate_sha256)
    _source_sha = field_validator("source_snapshot_sha256")(_validate_sha256)

    @model_validator(mode="after")
    def _clocks(self) -> Self:
        if _utc(self.verified_at) < _utc(self.temporal_scope.observed_through):
            raise ValueError("population run verification predates observed_through")
        expected = population_run_identity(
            self.policy_config_sha256,
            self.source_snapshot_sha256,
            self.temporal_scope,
        )
        if self.population_run_id != expected or self.idempotency_key != expected:
            raise ValueError("population run identity must be derived from policy, source, cutoff")
        return self

    @property
    def canonical_identity_json(self) -> str:
        return canonical_json(
            {
                "knowledge_cutoff": _utc(self.temporal_scope.knowledge_cutoff),
                "observed_through": _utc(self.temporal_scope.observed_through),
                "policy_config_sha256": self.policy_config_sha256,
                "source_snapshot_sha256": self.source_snapshot_sha256,
                "version": "population-run-identity.v2",
            }
        )

    @property
    def identity_sha256(self) -> str:
        return digest_text(self.canonical_identity_json)


class PopulationPlaneReceipt(_FrozenModel):
    population_run_id: str = Field(min_length=1, max_length=128)
    plane_name: PopulationPlaneName
    expected_count: int = Field(gt=0)
    materialized_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    input_commitment_sha256: str
    output_commitment_sha256: str
    status: ReceiptStatus
    details: dict[str, JsonValue]
    temporal_scope: PopulationTemporalScope
    verified_at: datetime

    _input_sha = field_validator("input_commitment_sha256")(_validate_sha256)
    _output_sha = field_validator("output_commitment_sha256")(_validate_sha256)

    @model_validator(mode="after")
    def _conservation(self) -> Self:
        if self.expected_count != (
            self.materialized_count + self.excluded_count + self.failed_count
        ):
            raise ValueError("population plane counts do not conserve the expected universe")
        if (self.status == "complete") != (self.failed_count == 0 and self.materialized_count > 0):
            raise ValueError("population plane status does not match its failed count")
        verifier = self.details.get("verifier")
        exclusions = self.details.get("exclusion_counts")
        result = self.details.get("result")
        artifact_sets = self.details.get("artifact_sets")
        scope = self.details.get("temporal_scope")
        if set(self.details) != {
            "artifact_sets",
            "exclusion_counts",
            "result",
            "temporal_scope",
            "verifier",
        }:
            raise ValueError("population plane details contract is not exact")
        if (
            not isinstance(verifier, dict)
            or not isinstance(exclusions, dict)
            or not isinstance(result, dict)
            or not isinstance(artifact_sets, list)
            or not isinstance(scope, dict)
        ):
            raise ValueError(
                "population plane details require verifier, result, and exclusion_counts"
            )
        parsed_scope = PopulationTemporalScope.model_validate(scope)
        if parsed_scope != self.temporal_scope:
            raise ValueError("population plane details scope differs from receipt scope")
        parsed_artifacts = tuple(
            PopulationArtifactSetCommitment.model_validate(item) for item in artifact_sets
        )
        if not parsed_artifacts:
            raise ValueError("population plane receipt requires artifact commitments")
        if _utc(self.verified_at) < _utc(self.temporal_scope.observed_through):
            raise ValueError("population plane verification predates observed_through")
        required_verifier = {"name", "version", "code_sha256", "result_sha256"}
        if set(verifier) != required_verifier:
            raise ValueError("population plane verifier contract is incomplete")
        _validate_sha256(str(verifier["code_sha256"]))
        result_sha256 = _validate_sha256(str(verifier["result_sha256"]))
        if result_sha256 != digest_text(canonical_json(result)):
            raise ValueError("population plane verifier result commitment is invalid")
        allowed = PLANE_EXCLUSION_REASON_CODES[self.plane_name]
        counts: dict[str, int] = {}
        for reason, raw_count in exclusions.items():
            if reason not in allowed or not isinstance(raw_count, int) or raw_count < 0:
                raise ValueError("population plane exclusion contract is invalid")
            counts[str(reason)] = raw_count
        if sum(counts.values()) != self.excluded_count:
            raise ValueError("population plane exclusions do not conserve excluded_count")
        return self

    @property
    def canonical_details_json(self) -> str:
        return canonical_json(self.details)

    @property
    def details_sha256(self) -> str:
        return digest_text(self.canonical_details_json)


class PopulationParityReceipt(_FrozenModel):
    population_run_id: str = Field(min_length=1, max_length=128)
    eligible_legacy_count: int = Field(gt=0)
    canonical_count: int = Field(gt=0)
    matched_count: int = Field(ge=0)
    mismatched_count: int = Field(ge=0)
    absent_count: int = Field(ge=0)
    extra_count: int = Field(ge=0)
    status: ReceiptStatus
    report: dict[str, JsonValue]
    temporal_scope: PopulationTemporalScope
    verified_at: datetime

    @model_validator(mode="after")
    def _conservation(self) -> Self:
        if self.eligible_legacy_count != (
            self.matched_count + self.mismatched_count + self.absent_count
        ):
            raise ValueError("legacy parity counts do not conserve eligible rows")
        if self.canonical_count != self.matched_count + self.extra_count:
            raise ValueError("legacy parity counts do not conserve canonical rows")
        clean = self.mismatched_count == self.absent_count == self.extra_count == 0
        if (self.status == "complete") != clean:
            raise ValueError("legacy parity status does not match its mismatch counts")
        if _utc(self.verified_at) < _utc(self.temporal_scope.observed_through):
            raise ValueError("population parity verification predates observed_through")
        return self

    @property
    def canonical_report_json(self) -> str:
        return canonical_json(self.report)

    @property
    def report_sha256(self) -> str:
        return digest_text(self.canonical_report_json)


class PopulationAuditReceipt(_FrozenModel):
    population_run_id: str = Field(min_length=1, max_length=128)
    verifier_name: str = Field(min_length=1, max_length=128)
    verifier_version: str = Field(min_length=1, max_length=64)
    verifier_code_sha256: str
    verifier_config_sha256: str
    temporal_scope: PopulationTemporalScope
    verified_at: datetime
    required_gate_count: int = Field(gt=0)
    eligible_count: int = Field(gt=0)
    verified_count: int = Field(gt=0)
    failed_count: int = Field(ge=0)
    evidence: dict[str, JsonValue]

    _code_sha = field_validator("verifier_code_sha256")(_validate_sha256)
    _config_sha = field_validator("verifier_config_sha256")(_validate_sha256)

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if _utc(self.verified_at) < _utc(self.temporal_scope.observed_through):
            raise ValueError("population audit predates observed_through")
        if self.eligible_count != self.verified_count + self.failed_count:
            raise ValueError("population audit counts do not conserve")
        if self.failed_count:
            raise ValueError("population audit receipt cannot contain failures")
        if (
            self.verifier_name != "population-cutover-readiness-auditor"
            or self.verifier_version != "2"
        ):
            raise ValueError("population audit verifier identity is invalid")
        if self.required_gate_count != len(REQUIRED_CUTOVER_AUDIT_GATES):
            raise ValueError("population audit must bind exactly the 13 governed gates")
        _validate_audit_evidence(
            self.evidence,
            temporal_scope=self.temporal_scope,
            eligible_count=self.eligible_count,
            verified_count=self.verified_count,
            failed_count=self.failed_count,
        )
        return self

    @property
    def canonical_evidence_json(self) -> str:
        return canonical_json(self.evidence)

    @property
    def evidence_sha256(self) -> str:
        return digest_text(self.canonical_evidence_json)

    @property
    def canonical_receipt_json(self) -> str:
        return canonical_json(
            {
                "audit_version": AUDIT_RECEIPT_VERSION,
                "knowledge_cutoff": _utc(self.temporal_scope.knowledge_cutoff),
                "observed_through": _utc(self.temporal_scope.observed_through),
                "verified_at": _utc(self.verified_at),
                "eligible_count": self.eligible_count,
                "evidence_sha256": self.evidence_sha256,
                "failed_count": self.failed_count,
                "population_run_id": self.population_run_id,
                "required_gate_count": self.required_gate_count,
                "verified_count": self.verified_count,
                "verifier_code_sha256": self.verifier_code_sha256,
                "verifier_config_sha256": self.verifier_config_sha256,
                "verifier_name": self.verifier_name,
                "verifier_version": self.verifier_version,
            }
        )

    @property
    def receipt_sha256(self) -> str:
        return digest_text(self.canonical_receipt_json)

    @property
    def cutoff_at(self) -> datetime:
        return self.temporal_scope.knowledge_cutoff

    @property
    def audited_at(self) -> datetime:
        return self.verified_at


class PopulationCutoverReceipt(_FrozenModel):
    population_run_id: str
    required_plane_count: int
    complete_plane_count: int
    audit_receipt_sha256: str
    receipt_set_sha256: str
    temporal_scope: PopulationTemporalScope
    sealed_at: datetime

    _audit_sha = field_validator("audit_receipt_sha256")(_validate_sha256)
    _receipt_sha = field_validator("receipt_set_sha256")(_validate_sha256)

    @model_validator(mode="after")
    def _seal_clock(self) -> Self:
        if _utc(self.sealed_at) < _utc(self.temporal_scope.observed_through):
            raise ValueError("population cutover seal predates observed_through")
        return self
