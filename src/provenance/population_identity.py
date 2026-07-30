"""Close recorded evidence subjects from existing canonical issuer authority.

This bridge does not reinterpret tickers or fetch fresh authority data.  It
uses the already-audited current legacy issuer bindings and the immutable
reporting-entity registry, and it fails closed whenever a recorded issuer
does not resolve to exactly one legal registrant.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from provenance.population_completeness import (
    PopulationArtifactSetCommitment,
    PopulationPlaneVerification,
    PopulationTemporalScope,
    canonical_json,
    digest_text,
    stream_population_artifact_set,
)
from provenance.reporting_entity_registry import (
    EvidenceSubjectBindingRevision,
    ReportingEntityRegistry,
)

_POLICY_NAME = "recorded_document_subject_identity_closure"
_POLICY_VERSION = "2"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PopulationIdentityRequest(_FrozenModel):
    apply: bool = False
    knowledge_cutoff: datetime
    operation_recorded_at: datetime

    @model_validator(mode="after")
    def _ordered_clocks(self) -> Self:
        if _utc(self.operation_recorded_at) < _utc(self.knowledge_cutoff):
            raise ValueError("operation_recorded_at must not precede knowledge_cutoff")
        return self


class PopulationIdentityItem(_FrozenModel):
    recorded_issuer_id: str
    outcome: Literal["selected", "unresolved", "conflict"]
    issuer_id: str | None = None
    reporting_entity_id: str | None = None
    reason_code: str
    created: bool = False


class PopulationIdentityResult(_FrozenModel):
    mode: Literal["dry_run", "apply"]
    policy_name: str
    policy_version: str
    policy_config_sha256: str = Field(min_length=64, max_length=64)
    expected_count: int
    selected_count: int
    unresolved_count: int
    conflict_count: int
    created_count: int
    input_commitment_sha256: str = Field(min_length=64, max_length=64)
    output_commitment_sha256: str = Field(min_length=64, max_length=64)
    items: tuple[PopulationIdentityItem, ...]


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _record_id(*parts: str) -> str:
    return f"subject-binding:{_digest(*parts)}"


def populate_recorded_subject_bindings(
    conn: sqlite3.Connection,
    request: PopulationIdentityRequest,
) -> PopulationIdentityResult:
    """Plan or append exact subject bindings for every recorded document issuer."""

    knowledge, observed = (
        _db_time(request.knowledge_cutoff),
        _db_time(request.operation_recorded_at),
    )
    recorded_ids = _recorded_issuer_ids(conn, request)
    input_commitment = _digest(
        "population-identity-input.v1",
        _canonical_json(recorded_ids),
    )
    policy_config = {
        "canonical_identity_sources": (
            "issuer_entities",
            "legacy_issuer_binding_revisions@K/O",
        ),
        "recorded_scope_source": "evidence_document_versions@K/O",
        "reporting_entity_kind": "legal_registrant",
        "selection_rule": "exactly_one",
        "temporal_scope": {"knowledge_cutoff": knowledge, "observed_through": observed},
        "version": _POLICY_VERSION,
    }
    policy_sha = _digest(_canonical_json(policy_config))
    registry = ReportingEntityRegistry(conn)
    items: list[PopulationIdentityItem] = []
    created_count = 0
    for recorded_id in recorded_ids:
        current = _binding_as_of(conn, recorded_id, request)
        selected_current = _validated_selected_current(conn, current, request)
        if selected_current is not None:
            issuer_id, reporting_entity_id = selected_current
            item = PopulationIdentityItem(
                recorded_issuer_id=recorded_id,
                outcome="selected",
                issuer_id=issuer_id,
                reporting_entity_id=reporting_entity_id,
                reason_code="current_authoritative_subject_preserved",
            )
            items.append(item)
            continue
        target = _resolve_target(conn, recorded_id, request)
        if target is None:
            item = _unresolved_item(
                conn,
                registry,
                recorded_issuer_id=recorded_id,
                current=current,
                request=request,
                policy_sha=policy_sha,
            )
        else:
            issuer_id, reporting_entity_id = target
            current_semantics = (
                None
                if current is None
                else (
                    None if current[2] is None else str(current[2]),
                    None if current[3] is None else str(current[3]),
                    str(current[5]),
                )
            )
            if current_semantics not in {
                None,
                (issuer_id, reporting_entity_id, "selected"),
                (None, None, "unresolved"),
            }:
                item = PopulationIdentityItem(
                    recorded_issuer_id=recorded_id,
                    outcome="conflict",
                    issuer_id=issuer_id,
                    reporting_entity_id=reporting_entity_id,
                    reason_code="current_subject_binding_conflicts",
                )
            else:
                created = _persist_binding(
                    conn,
                    registry,
                    recorded_issuer_id=recorded_id,
                    issuer_id=issuer_id,
                    reporting_entity_id=reporting_entity_id,
                    current=current,
                    request=request,
                    policy_sha=policy_sha,
                )
                item = PopulationIdentityItem(
                    recorded_issuer_id=recorded_id,
                    outcome="selected",
                    issuer_id=issuer_id,
                    reporting_entity_id=reporting_entity_id,
                    reason_code="unique_legal_registrant_selected",
                    created=created,
                )
        items.append(item)
        created_count += int(item.created)
    output_payload = [
        {
            "issuer_id": item.issuer_id,
            "outcome": item.outcome,
            "reason_code": item.reason_code,
            "recorded_issuer_id": item.recorded_issuer_id,
            "reporting_entity_id": item.reporting_entity_id,
        }
        for item in items
    ]
    return PopulationIdentityResult(
        mode="apply" if request.apply else "dry_run",
        policy_name=_POLICY_NAME,
        policy_version=_POLICY_VERSION,
        policy_config_sha256=policy_sha,
        expected_count=len(items),
        selected_count=sum(item.outcome == "selected" for item in items),
        unresolved_count=sum(item.outcome == "unresolved" for item in items),
        conflict_count=sum(item.outcome == "conflict" for item in items),
        created_count=created_count,
        input_commitment_sha256=input_commitment,
        output_commitment_sha256=_digest(
            "population-identity-output.v1",
            _canonical_json(output_payload),
        ),
        items=tuple(items),
    )


def _recorded_issuer_ids(
    conn: sqlite3.Connection,
    request: PopulationIdentityRequest,
) -> tuple[str, ...]:
    return tuple(
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT version.issuer_id "
            "FROM evidence_document_versions version "
            "JOIN evidence_source_observations observation "
            "ON observation.observation_id=version.observation_id "
            "WHERE datetime(observation.observed_at)<=datetime(?) "
            "AND datetime(observation.retrieved_at)<=datetime(?) "
            "AND datetime(version.recorded_at)<=datetime(?) "
            "ORDER BY version.issuer_id",
            (
                _db_time(request.knowledge_cutoff),
                _db_time(request.operation_recorded_at),
                _db_time(request.operation_recorded_at),
            ),
        )
    )


def _resolve_target(
    conn: sqlite3.Connection,
    recorded_id: str,
    request: PopulationIdentityRequest,
) -> tuple[str, str] | None:
    issuer = conn.execute(
        "SELECT issuer_id FROM issuer_entities "
        "WHERE issuer_id=? AND datetime(created_at)<=datetime(?)",
        (recorded_id, _db_time(request.knowledge_cutoff)),
    ).fetchone()
    if issuer is None:
        issuer = conn.execute(
            "SELECT issuer_id FROM legacy_issuer_binding_revisions binding "
            "WHERE recorded_issuer_id=? AND outcome='selected' "
            "AND datetime(knowledge_at)<=datetime(?) "
            "AND datetime(recorded_at)<=datetime(?) "
            "AND NOT EXISTS ("
            " SELECT 1 FROM legacy_issuer_binding_revisions newer "
            " WHERE newer.recorded_issuer_id=binding.recorded_issuer_id "
            " AND newer.revision>binding.revision "
            " AND datetime(newer.knowledge_at)<=datetime(?) "
            " AND datetime(newer.recorded_at)<=datetime(?)"
            ") ORDER BY revision DESC,binding_revision_id DESC LIMIT 1",
            (
                recorded_id,
                _db_time(request.knowledge_cutoff),
                _db_time(request.operation_recorded_at),
                _db_time(request.knowledge_cutoff),
                _db_time(request.operation_recorded_at),
            ),
        ).fetchone()
    if issuer is None:
        return None
    issuer_id = str(issuer[0])
    entities = conn.execute(
        "SELECT reporting_entity_id FROM reporting_entities "
        "WHERE issuer_id=? AND reporting_entity_kind='legal_registrant' "
        "AND datetime(created_at)<=datetime(?) "
        "ORDER BY reporting_entity_id",
        (issuer_id, _db_time(request.knowledge_cutoff)),
    ).fetchall()
    if len(entities) != 1:
        return None
    return issuer_id, str(entities[0][0])


def _binding_as_of(
    conn: sqlite3.Connection,
    recorded_id: str,
    request: PopulationIdentityRequest,
) -> tuple[object, ...] | None:
    row = conn.execute(
        "SELECT binding_revision_id,revision,issuer_id,reporting_entity_id,"
        "security_id,outcome FROM recorded_subject_binding_revisions binding "
        "WHERE recorded_issuer_id=? "
        "AND datetime(knowledge_at)<=datetime(?) "
        "AND datetime(recorded_at)<=datetime(?) "
        "AND NOT EXISTS ("
        " SELECT 1 FROM recorded_subject_binding_revisions newer "
        " WHERE newer.recorded_issuer_id=binding.recorded_issuer_id "
        " AND newer.revision>binding.revision "
        " AND datetime(newer.knowledge_at)<=datetime(?) "
        " AND datetime(newer.recorded_at)<=datetime(?)"
        ") ORDER BY revision DESC,binding_revision_id DESC LIMIT 1",
        (
            recorded_id,
            _db_time(request.knowledge_cutoff),
            _db_time(request.operation_recorded_at),
            _db_time(request.knowledge_cutoff),
            _db_time(request.operation_recorded_at),
        ),
    ).fetchone()
    return None if row is None else tuple(row)


def _validated_selected_current(
    conn: sqlite3.Connection,
    current: tuple[object, ...] | None,
    request: PopulationIdentityRequest,
) -> tuple[str, str] | None:
    if current is None or str(current[5]) != "selected" or current[2] is None or current[3] is None:
        return None
    issuer_id = str(current[2])
    reporting_entity_id = str(current[3])
    row = conn.execute(
        "SELECT 1 FROM reporting_entities "
        "WHERE reporting_entity_id=? AND issuer_id=? "
        "AND datetime(created_at)<=datetime(?)",
        (reporting_entity_id, issuer_id, _db_time(request.knowledge_cutoff)),
    ).fetchone()
    if row is None:
        return None
    return issuer_id, reporting_entity_id


def _unresolved_item(
    conn: sqlite3.Connection,
    registry: ReportingEntityRegistry,
    *,
    recorded_issuer_id: str,
    current: tuple[object, ...] | None,
    request: PopulationIdentityRequest,
    policy_sha: str,
) -> PopulationIdentityItem:
    issuer = conn.execute(
        "SELECT COALESCE(entity.issuer_id,binding.issuer_id) "
        "FROM (SELECT ? AS recorded_issuer_id) recorded "
        "LEFT JOIN issuer_entities entity "
        "ON entity.issuer_id=recorded.recorded_issuer_id "
        "LEFT JOIN legacy_issuer_binding_revisions binding "
        "ON binding.binding_revision_id=("
        " SELECT candidate.binding_revision_id "
        " FROM legacy_issuer_binding_revisions candidate "
        " WHERE candidate.recorded_issuer_id=recorded.recorded_issuer_id "
        " AND datetime(candidate.knowledge_at)<=datetime(?) "
        " AND datetime(candidate.recorded_at)<=datetime(?) "
        " ORDER BY candidate.revision DESC,candidate.binding_revision_id DESC LIMIT 1"
        ") AND binding.outcome='selected'",
        (
            recorded_issuer_id,
            _db_time(request.knowledge_cutoff),
            _db_time(request.operation_recorded_at),
        ),
    ).fetchone()
    reason_code = (
        "canonical_issuer_missing"
        if issuer is None or issuer[0] is None
        else "unique_legal_registrant_missing"
    )
    current_outcome = None if current is None else str(current[5])
    created = False
    if current_outcome == "selected":
        return PopulationIdentityItem(
            recorded_issuer_id=recorded_issuer_id,
            outcome="conflict",
            reason_code="selected_binding_target_no_longer_resolves",
        )
    if current_outcome != "unresolved" and request.apply:
        _require_no_later_binding(conn, recorded_issuer_id, current, request)
        revision = 1 if current is None else int(str(current[1])) + 1
        record_id = _record_id(
            recorded_issuer_id,
            "unresolved",
            reason_code,
            str(revision),
        )
        created = registry.persist(
            EvidenceSubjectBindingRevision(
                binding_revision_id=record_id,
                idempotency_key=record_id,
                recorded_issuer_id=recorded_issuer_id,
                revision=revision,
                issuer_id=None,
                reporting_entity_id=None,
                security_id=None,
                outcome="unresolved",
                decision_kind="deterministic",
                reason_code=reason_code,
                reason_details=(
                    ("policy_config_sha256", policy_sha),
                    ("policy_name", _POLICY_NAME),
                    ("policy_version", _POLICY_VERSION),
                ),
                material_dissent=False,
                effective_at=request.knowledge_cutoff,
                knowledge_at=request.knowledge_cutoff,
                recorded_at=request.operation_recorded_at,
                supersedes_binding_revision_id=(None if current is None else str(current[0])),
            )
        ).created
    return PopulationIdentityItem(
        recorded_issuer_id=recorded_issuer_id,
        outcome="unresolved",
        reason_code=reason_code,
        created=created,
    )


def _persist_binding(
    conn: sqlite3.Connection,
    registry: ReportingEntityRegistry,
    *,
    recorded_issuer_id: str,
    issuer_id: str,
    reporting_entity_id: str,
    current: tuple[object, ...] | None,
    request: PopulationIdentityRequest,
    policy_sha: str,
) -> bool:
    if (
        current is not None
        and str(current[2]) == issuer_id
        and str(current[3]) == reporting_entity_id
        and str(current[5]) == "selected"
    ):
        return False
    if not request.apply:
        return False
    _require_no_later_binding(conn, recorded_issuer_id, current, request)
    revision = 1 if current is None else int(str(current[1])) + 1
    record_id = _record_id(
        recorded_issuer_id,
        issuer_id,
        reporting_entity_id,
        str(revision),
    )
    return registry.persist(
        EvidenceSubjectBindingRevision(
            binding_revision_id=record_id,
            idempotency_key=record_id,
            recorded_issuer_id=recorded_issuer_id,
            revision=revision,
            issuer_id=issuer_id,
            reporting_entity_id=reporting_entity_id,
            security_id=None,
            outcome="selected",
            decision_kind="deterministic",
            reason_code="unique_legal_registrant_selected",
            reason_details=(
                ("policy_config_sha256", policy_sha),
                ("policy_name", _POLICY_NAME),
                ("policy_version", _POLICY_VERSION),
            ),
            material_dissent=False,
            effective_at=request.knowledge_cutoff,
            knowledge_at=request.knowledge_cutoff,
            recorded_at=request.operation_recorded_at,
            supersedes_binding_revision_id=None if current is None else str(current[0]),
        )
    ).created


def _require_no_later_binding(
    conn: sqlite3.Connection,
    recorded_issuer_id: str,
    current: tuple[object, ...] | None,
    request: PopulationIdentityRequest,
) -> None:
    latest = conn.execute(
        "SELECT binding_revision_id FROM recorded_subject_binding_revisions "
        "WHERE recorded_issuer_id=? ORDER BY revision DESC,binding_revision_id DESC LIMIT 1",
        (recorded_issuer_id,),
    ).fetchone()
    current_id = None if current is None else str(current[0])
    if latest is not None and str(latest[0]) != current_id:
        raise ValueError(
            "cannot append a historical subject binding after a later recorded revision"
        )
    if _utc(request.operation_recorded_at) < _utc(request.knowledge_cutoff):
        raise ValueError("identity operation clock precedes knowledge cutoff")


def verify_identity_scope(
    conn: sqlite3.Connection,
    scope: PopulationTemporalScope,
) -> PopulationPlaneVerification:
    """Verify the exact selected subject-binding set at K as observed through O."""

    request = PopulationIdentityRequest(
        knowledge_cutoff=scope.knowledge_cutoff,
        operation_recorded_at=scope.observed_through,
    )
    recorded_ids = _recorded_issuer_ids(conn, request)
    if not recorded_ids:
        raise ValueError("identity scope is empty")
    selected: list[tuple[str, str, str]] = []
    failures: list[str] = []
    for recorded_id in recorded_ids:
        current = _binding_as_of(conn, recorded_id, request)
        validated = _validated_selected_current(conn, current, request)
        if current is None or validated is None:
            failures.append(recorded_id)
            continue
        if str(current[5]) != "selected":
            failures.append(recorded_id)
            continue
        if _parse_time(
            conn.execute(
                "SELECT knowledge_at FROM recorded_subject_binding_revisions "
                "WHERE binding_revision_id=?",
                (str(current[0]),),
            ).fetchone()[0]
        ) != _utc(scope.knowledge_cutoff):
            raise ValueError("identity binding knowledge clock drift")
        selected.append((recorded_id, str(current[0]), validated[1]))
    artifact = stream_population_artifact_set(
        conn,
        table="recorded_subject_binding_revisions",
        query="""
            WITH scoped(recorded_issuer_id) AS (
                SELECT DISTINCT version.issuer_id
                FROM evidence_document_versions version
                JOIN evidence_source_observations observation
                  ON observation.observation_id=version.observation_id
                WHERE datetime(observation.observed_at)<=datetime(?)
                  AND datetime(observation.retrieved_at)<=datetime(?)
                  AND datetime(version.recorded_at)<=datetime(?)
            ),
            ranked AS (
                SELECT binding.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY binding.recorded_issuer_id
                           ORDER BY binding.revision DESC,binding.binding_revision_id DESC
                       ) AS scope_rank
                FROM recorded_subject_binding_revisions binding
                JOIN scoped ON scoped.recorded_issuer_id=binding.recorded_issuer_id
                WHERE datetime(binding.knowledge_at)<=datetime(?)
                  AND datetime(binding.recorded_at)<=datetime(?)
            )
            SELECT binding_revision_id AS artifact_id,
                   fact_sha256(json_object(
                       'issuer_id',issuer_id,
                       'outcome',outcome,
                       'recorded_issuer_id',recorded_issuer_id,
                       'reporting_entity_id',reporting_entity_id,
                       'revision',revision
                   )) AS payload_sha256,
                   fact_sha256(json_object(
                       'idempotency_key',idempotency_key,
                       'reason_code',reason_code,
                       'reason_details_json',json(reason_details_json),
                       'supersedes_binding_revision_id',supersedes_binding_revision_id
                   )) AS seal_sha256,
                   knowledge_at,
                   recorded_at
            FROM ranked
            WHERE scope_rank=1 AND outcome='selected'
            ORDER BY binding_revision_id
        """,
        params=(
            _db_time(scope.knowledge_cutoff),
            _db_time(scope.observed_through),
            _db_time(scope.observed_through),
            _db_time(scope.knowledge_cutoff),
            _db_time(scope.observed_through),
        ),
        selection_policy_id="identity-scope-as-of-k-o.v2",
    )
    input_material: dict[str, JsonValue] = {
        "knowledge_cutoff": _db_time(scope.knowledge_cutoff),
        "observed_through": _db_time(scope.observed_through),
        "recorded_issuer_ids": cast(JsonValue, list(recorded_ids)),
    }
    details: dict[str, JsonValue] = {
        "failed_recorded_issuer_ids": cast(JsonValue, failures),
        "selected_bindings": cast(
            JsonValue,
            [
                {
                    "binding_revision_id": binding_id,
                    "recorded_issuer_id": recorded_id,
                    "reporting_entity_id": reporting_entity_id,
                }
                for recorded_id, binding_id, reporting_entity_id in selected
            ],
        ),
        "temporal_policy": "knowledge_at<=K;recorded_at<=O;binding_knowledge_at=K",
    }
    return _plane_verification(
        expected=len(recorded_ids),
        materialized=len(selected),
        failed=len(failures),
        input_sha=digest_text(canonical_json(input_material)),
        artifact=artifact,
        details=details,
    )


def _plane_verification(
    *,
    expected: int,
    materialized: int,
    failed: int,
    input_sha: str,
    artifact: PopulationArtifactSetCommitment,
    details: dict[str, JsonValue],
) -> PopulationPlaneVerification:
    artifact_set = artifact
    output_material = {
        "artifact_sets": [artifact_set.model_dump(mode="json")],
        "details": details,
        "exclusion_counts": {},
        "expected_count": expected,
        "failed_count": failed,
        "materialized_count": materialized,
        "plane_name": "identity_scope",
    }
    return PopulationPlaneVerification(
        plane_name="identity_scope",
        expected_count=expected,
        materialized_count=materialized,
        excluded_count=0,
        failed_count=failed,
        exclusion_counts={},
        input_commitment_sha256=input_sha,
        output_commitment_sha256=digest_text(canonical_json(output_material)),
        artifact_sets=(artifact_set,),
        details=details,
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _db_time(value: datetime) -> str:
    return _utc(value).isoformat()


def _parse_time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    return _utc(parsed)
