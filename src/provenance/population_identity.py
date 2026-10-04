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
from dataclasses import dataclass
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
_POLICY_VERSION = "3"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PopulationIdentityRequest(_FrozenModel):
    apply: bool = False
    knowledge_cutoff: datetime
    operation_recorded_at: datetime
    document_version_ids: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def _ordered_clocks(self) -> Self:
        if self.document_version_ids is not None and (
            len(set(self.document_version_ids)) != len(self.document_version_ids)
            or any(not item or item != item.strip() for item in self.document_version_ids)
        ):
            raise ValueError("document scope requires unique nonblank exact identities")
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
    witnesses = {
        recorded_id: _issuer_witness(conn, recorded_id, request) for recorded_id in recorded_ids
    }
    input_commitment = _digest(
        "population-identity-input.v2",
        _canonical_json(recorded_ids),
        _canonical_json([witness.commitment for witness in witnesses.values()]),
    )
    if request.document_version_ids is not None:
        input_commitment = _digest(
            "population-identity-input.scoped.v2",
            input_commitment,
            _canonical_json(sorted(request.document_version_ids)),
        )
    policy_config: dict[str, object] = {
        "canonical_identity_sources": (
            "issuer_entities",
            "legacy_issuer_binding_revisions@K/O",
        ),
        "recorded_scope_source": "evidence_document_versions@K/O",
        "new_subject_reporting_entity_kind": "legal_registrant",
        "new_subject_selection_rule": "exactly_one",
        "material_dissent": "conflict_without_append",
        "current_subject_preservation_rule": "issuer_entity_and_optional_security_agree",
        "temporal_scope": {"knowledge_cutoff": knowledge, "observed_through": observed},
        "version": _POLICY_VERSION,
    }
    if request.document_version_ids is not None:
        policy_config["document_version_ids"] = tuple(sorted(request.document_version_ids))
    policy_sha = _digest(_canonical_json(policy_config))
    registry = ReportingEntityRegistry(conn)
    items: list[PopulationIdentityItem] = []
    created_count = 0
    for recorded_id in recorded_ids:
        current = _binding_as_of(conn, recorded_id, request)
        witness = witnesses[recorded_id]
        if (
            witness.material_dissent
            or (current is not None and bool(current[6]))
            or (witness.issuer_id is not None and not witness.target_present)
        ):
            items.append(
                PopulationIdentityItem(
                    recorded_issuer_id=recorded_id,
                    outcome="conflict",
                    reason_code="issuer_witness_material_dissent"
                    if witness.material_dissent
                    else "current_subject_material_dissent"
                    if current is not None and bool(current[6])
                    else "issuer_witness_target_missing",
                )
            )
            continue
        if current is not None and str(current[5]) == "selected":
            selected_current = _validated_selected_current(conn, current, request, witness)
            if selected_current is None:
                item = PopulationIdentityItem(
                    recorded_issuer_id=recorded_id,
                    outcome="conflict",
                    reason_code="current_subject_binding_conflicts",
                )
            else:
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
        target = _resolve_target(conn, witness, request)
        if target is None:
            item = _unresolved_item(
                conn,
                registry,
                recorded_issuer_id=recorded_id,
                current=current,
                request=request,
                policy_sha=policy_sha,
                issuer_witness=witness,
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
    query = (
        "SELECT version.document_version_id,version.issuer_id,observation.observed_at,"
        "observation.retrieved_at,version.recorded_at "
        "FROM evidence_document_versions version "
        "JOIN evidence_source_observations observation "
        "ON observation.observation_id=version.observation_id "
    )
    parameters: list[str] = []
    if request.document_version_ids is not None:
        query += "WHERE version.document_version_id IN (SELECT value FROM json_each(?)) "
        parameters.append(json.dumps(request.document_version_ids))
    query += "ORDER BY version.issuer_id,version.document_version_id"
    issuer_ids: set[str] = set()
    document_ids: set[str] = set()
    knowledge, observed = _utc(request.knowledge_cutoff), _utc(request.operation_recorded_at)
    for row in conn.execute(query, parameters):
        observed_at, retrieved_at, recorded_at = tuple(_parse_time(value) for value in row[2:5])
        if observed_at <= knowledge and retrieved_at <= observed and recorded_at <= observed:
            issuer_ids.add(str(row[1]))
            if request.document_version_ids is not None:
                document_ids.add(str(row[0]))
    if request.document_version_ids is not None and document_ids != set(
        request.document_version_ids
    ):
        raise ValueError("selected document unavailable within identity cutoff")
    return tuple(sorted(issuer_ids))


@dataclass(frozen=True)
class _IssuerWitness:
    issuer_id: str | None
    material_dissent: bool
    target_present: bool
    commitment: dict[str, JsonValue]


def _issuer_witness(
    conn: sqlite3.Connection,
    recorded_id: str,
    request: PopulationIdentityRequest,
) -> _IssuerWitness:
    canonical = conn.execute(
        "SELECT issuer_id,created_at FROM issuer_entities WHERE issuer_id=?",
        (recorded_id,),
    ).fetchone()
    if canonical is not None and _parse_time(canonical[1]) <= _utc(request.knowledge_cutoff):
        return _IssuerWitness(
            str(canonical[0]),
            False,
            True,
            {
                "recorded_issuer_id": recorded_id,
                "source": "issuer_entities",
                "issuer_id": str(canonical[0]),
                "created_at": str(canonical[1]),
                "material_dissent": False,
            },
        )
    binding = None
    for candidate in conn.execute(
        "SELECT binding_revision_id,revision,issuer_id,outcome,material_dissent,knowledge_at,recorded_at "
        "FROM legacy_issuer_binding_revisions binding "
        "WHERE recorded_issuer_id=? ORDER BY revision DESC,binding_revision_id DESC",
        (recorded_id,),
    ):
        knowledge_at, recorded_at = _parse_time(candidate[5]), _parse_time(candidate[6])
        if knowledge_at <= _utc(request.knowledge_cutoff) and recorded_at <= _utc(
            request.operation_recorded_at
        ):
            binding = candidate
            break
    if binding is None:
        return _IssuerWitness(
            None,
            False,
            False,
            {
                "recorded_issuer_id": recorded_id,
                "source": "legacy_issuer_binding_revisions",
                "binding_revision_id": None,
            },
        )
    issuer_id = None if binding[2] is None or str(binding[3]) != "selected" else str(binding[2])
    target = (
        None
        if issuer_id is None
        else conn.execute(
            "SELECT created_at FROM issuer_entities WHERE issuer_id=?",
            (issuer_id,),
        ).fetchone()
    )
    dissent = bool(binding[4])
    return _IssuerWitness(
        issuer_id,
        dissent,
        target is not None and _parse_time(target[0]) <= _utc(request.knowledge_cutoff),
        {
            "recorded_issuer_id": recorded_id,
            "source": "legacy_issuer_binding_revisions",
            "binding_revision_id": str(binding[0]),
            "revision": int(binding[1]),
            "issuer_id": None if binding[2] is None else str(binding[2]),
            "outcome": str(binding[3]),
            "material_dissent": dissent,
            "knowledge_at": str(binding[5]),
            "recorded_at": str(binding[6]),
            "issuer_created_at": None if target is None else str(target[0]),
        },
    )


def _resolve_target(
    conn: sqlite3.Connection,
    witness: _IssuerWitness,
    request: PopulationIdentityRequest,
) -> tuple[str, str] | None:
    if witness.issuer_id is None or witness.material_dissent or not witness.target_present:
        return None
    issuer_id = witness.issuer_id
    entity_rows = conn.execute(
        "SELECT reporting_entity_id,created_at FROM reporting_entities "
        "WHERE issuer_id=? AND reporting_entity_kind='legal_registrant' "
        "ORDER BY reporting_entity_id",
        (issuer_id,),
    )
    entities = [row for row in entity_rows if _parse_time(row[1]) <= _utc(request.knowledge_cutoff)]
    if len(entities) != 1:
        return None
    return issuer_id, str(entities[0][0])


def _binding_as_of(
    conn: sqlite3.Connection,
    recorded_id: str,
    request: PopulationIdentityRequest,
) -> tuple[object, ...] | None:
    for row in conn.execute(
        "SELECT binding_revision_id,revision,issuer_id,reporting_entity_id,"
        "security_id,outcome,material_dissent,knowledge_at,recorded_at "
        "FROM recorded_subject_binding_revisions "
        "WHERE recorded_issuer_id=? ORDER BY revision DESC,binding_revision_id DESC",
        (recorded_id,),
    ):
        knowledge_at, recorded_at = _parse_time(row[7]), _parse_time(row[8])
        if knowledge_at <= _utc(request.knowledge_cutoff) and recorded_at <= _utc(
            request.operation_recorded_at
        ):
            return tuple(row[:7])
    return None


def _validated_selected_current(
    conn: sqlite3.Connection,
    current: tuple[object, ...] | None,
    request: PopulationIdentityRequest,
    witness: _IssuerWitness,
) -> tuple[str, str] | None:
    if (
        current is None
        or str(current[5]) != "selected"
        or current[2] is None
        or current[3] is None
        or bool(current[6])
        or witness.material_dissent
        or not witness.target_present
        or str(current[2]) != witness.issuer_id
    ):
        return None
    issuer_id = str(current[2])
    reporting_entity_id = str(current[3])
    row = conn.execute(
        "SELECT created_at FROM reporting_entities WHERE reporting_entity_id=? AND issuer_id=?",
        (reporting_entity_id, issuer_id),
    ).fetchone()
    if row is None or _parse_time(row[0]) > _utc(request.knowledge_cutoff):
        return None
    if current[4] is not None:
        security = conn.execute(
            "SELECT created_at FROM securities WHERE security_id=? AND issuer_id=?",
            (str(current[4]), issuer_id),
        ).fetchone()
        if security is None or _parse_time(security[0]) > _utc(request.knowledge_cutoff):
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
    issuer_witness: _IssuerWitness,
) -> PopulationIdentityItem:
    reason_code = (
        "canonical_issuer_missing"
        if issuer_witness.issuer_id is None
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
    selected_artifact_ids: list[str] = []
    failures: list[str] = []
    witnesses: list[JsonValue] = []
    for recorded_id in recorded_ids:
        current = _binding_as_of(conn, recorded_id, request)
        if current is not None and str(current[5]) == "selected":
            selected_artifact_ids.append(str(current[0]))
        witness = _issuer_witness(conn, recorded_id, request)
        witnesses.append(witness.commitment)
        validated = _validated_selected_current(conn, current, request, witness)
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
            SELECT binding_revision_id AS artifact_id,
                   fact_sha256(json_object(
                       'issuer_id',issuer_id,
                       'material_dissent',material_dissent,
                       'security_id',security_id,
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
            FROM recorded_subject_binding_revisions
            WHERE binding_revision_id IN (SELECT value FROM json_each(?))
            ORDER BY binding_revision_id
        """,
        params=(json.dumps(sorted(selected_artifact_ids)),),
        selection_policy_id="identity-scope-as-of-k-o.v3",
    )
    input_material: dict[str, JsonValue] = {
        "knowledge_cutoff": _db_time(scope.knowledge_cutoff),
        "observed_through": _db_time(scope.observed_through),
        "recorded_issuer_ids": cast(JsonValue, list(recorded_ids)),
        "issuer_witnesses": witnesses,
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
