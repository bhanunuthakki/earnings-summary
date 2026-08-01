# pyright: reportPrivateUsage=false
"""Fail-closed, exhaustive resolution of a canonical metric coordinate.

This boundary deliberately has no ``candidates`` or ``relations`` argument.
Everything it reasons over is read from the sealed 0242 admission graph and
the bitemporal 0243 binding graph at the caller's explicit cutoff.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from provenance.source_fact_publication import verify_source_fact_publication

Status = Literal["resolved", "unresolved", "retired"]
MAX_CANDIDATES_PER_CANONICAL_CELL = 500
_RESOLUTION_SCOPE_VERSION = "canonical-resolution-snapshot-scope.v1"


def _json(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(value: object) -> str:
    return hashlib.sha256((value if isinstance(value, str) else _json(value)).encode()).hexdigest()


def _sqlite_sha(value: object) -> str:
    return _sha(str(value))


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _time(value: datetime) -> str:
    return _utc(value).isoformat()


class ResolutionPolicy(BaseModel):
    """A named deterministic rule; source tier is intentionally absent."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    config: dict[str, object]

    @property
    def config_sha256(self) -> str:
        return _sha(self.config)


class ResolutionReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    canonical_metric_cell_id: str
    cutoff_at: datetime
    candidate_universe_id: str
    relation_set_id: str
    canonical_resolution_revision_id: str
    status: Status
    selected_observation_id: str | None
    reason_code: str
    exact_replay: bool


class ResolutionPlan(BaseModel):
    """Read-only deterministic outcome over one exact candidate graph."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    canonical_metric_cell_id: str
    cutoff_at: datetime
    observed_through: datetime
    policy_name: str
    policy_version: str
    policy_config_sha256: str
    candidate_universe_id: str
    candidate_count: int = Field(ge=0)
    eligible_candidate_count: int = Field(ge=0)
    candidate_set_sha256: str = Field(min_length=64, max_length=64)
    relation_set_id: str
    relation_count: int = Field(ge=0)
    relation_set_sha256: str = Field(min_length=64, max_length=64)
    canonical_resolution_revision_id: str
    status: Status
    selected_observation_id: str | None
    reason_code: str
    reason_details_sha256: str = Field(min_length=64, max_length=64)

    @property
    def commitment_sha256(self) -> str:
        return _sha(self)


class ResolutionSnapshotScope(BaseModel):
    """The explicit, immutable issuer universe committed by a resolution snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    issuer_id: str = Field(min_length=1, max_length=128)
    reporting_entity_ids: tuple[str, ...] = Field(min_length=1)
    scope_version: Literal["canonical-resolution-snapshot-scope.v1"] = _RESOLUTION_SCOPE_VERSION

    @field_validator("reporting_entity_ids")
    @classmethod
    def _ordered_unique_entities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not entity_id or len(entity_id) > 128 for entity_id in value):
            raise ValueError("reporting entity ids must be non-empty and at most 128 chars")
        if tuple(sorted(set(value))) != value:
            raise ValueError("reporting entity ids must be sorted and unique")
        return value

    @property
    def canonical_json(self) -> str:
        return _json(self)

    @property
    def scope_sha256(self) -> str:
        return _sha(self.canonical_json)


class VerifiedResolutionSnapshot(BaseModel):
    """Exact verification receipt for a sealed, issuer-scoped snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    resolution_snapshot_id: str
    scope: ResolutionSnapshotScope
    cutoff_at: datetime
    recorded_at: datetime
    member_count: int
    member_set_sha256: str
    scope_member_set_sha256: str
    scope_sha256: str
    snapshot_commitment_sha256: str


@dataclass(frozen=True)
class _Candidate:
    observation_id: str
    fact_cell_id: str
    binding_revision_id: str
    payload_sha256: str
    publication_id: str | None
    publication_seal_id: str | None
    publication_member_id: str | None
    source_member_sha256: str | None
    source_member_commitment_sha256: str | None
    binding_commitment_sha256: str
    mapping_commitment_sha256: str
    filing_disposition_id: str | None
    source_lane: str
    eligibility: Literal["eligible", "ineligible"]
    reason_code: str
    numeric_value: str | None
    text_value: str | None
    is_nil: bool
    currency: str | None
    unit_key: str
    effective_at: str
    knowledge_at: str
    recorded_at: str

    @property
    def value_key(self) -> tuple[object, ...]:
        return (self.numeric_value, self.text_value, self.is_nil, self.currency, self.unit_key)


@dataclass(frozen=True)
class _PreparedResolution:
    plan: ResolutionPlan
    candidates: list[_Candidate]
    eligible: list[_Candidate]
    relations: list[dict[str, object]]
    reason: tuple[str, dict[str, object]]


class CanonicalFactResolutionEngine:
    """The sole durable cross-cell resolution boundary."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        conn.execute("PRAGMA foreign_keys = ON")
        conn.create_function("fact_sha256", 1, _sqlite_sha, deterministic=True)

    def resolve(
        self,
        canonical_metric_cell_id: str,
        knowledge_cutoff: datetime,
        policy: ResolutionPolicy,
        *,
        recorded_at: datetime,
    ) -> ResolutionReceipt:
        """Enumerate all sealed eligible assertions and persist one exact result."""
        cutoff, written_at = _utc(knowledge_cutoff), _utc(recorded_at)
        prepared = self._prepare_resolution(
            canonical_metric_cell_id,
            cutoff,
            policy,
            observed_through=written_at,
        )
        plan = prepared.plan
        resolution_key = _sha(
            [
                canonical_metric_cell_id,
                _time(cutoff),
                policy.name,
                policy.version,
                policy.config_sha256,
            ]
        )
        self._conn.execute("SAVEPOINT canonical_fact_resolution")
        try:
            universe_replay = self._persist_universe(
                plan.candidate_universe_id,
                canonical_metric_cell_id,
                cutoff,
                written_at,
                prepared.candidates,
            )
            relation_replay = self._persist_relation_set(
                plan.relation_set_id,
                plan.candidate_universe_id,
                cutoff,
                written_at,
                prepared.relations,
            )
            resolution_replay = self._persist_resolution(
                plan.canonical_resolution_revision_id,
                resolution_key,
                canonical_metric_cell_id,
                plan.candidate_universe_id,
                plan.relation_set_id,
                cutoff,
                written_at,
                policy,
                plan.status,
                plan.selected_observation_id,
                prepared.reason,
            )
            self._verify_universe(plan.candidate_universe_id, prepared.candidates)
            self._verify_relation_set(plan.relation_set_id, prepared.relations)
        except Exception:
            self._conn.execute("ROLLBACK TO SAVEPOINT canonical_fact_resolution")
            self._conn.execute("RELEASE SAVEPOINT canonical_fact_resolution")
            raise
        self._conn.execute("RELEASE SAVEPOINT canonical_fact_resolution")
        return ResolutionReceipt(
            canonical_metric_cell_id=canonical_metric_cell_id,
            cutoff_at=cutoff,
            candidate_universe_id=plan.candidate_universe_id,
            relation_set_id=plan.relation_set_id,
            canonical_resolution_revision_id=plan.canonical_resolution_revision_id,
            status=plan.status,
            selected_observation_id=plan.selected_observation_id,
            reason_code=plan.reason_code,
            exact_replay=universe_replay and relation_replay and resolution_replay,
        )

    def plan(
        self,
        canonical_metric_cell_id: str,
        knowledge_cutoff: datetime,
        policy: ResolutionPolicy,
        *,
        observed_through: datetime,
    ) -> ResolutionPlan:
        """Return the exact deterministic outcome without persisting evidence."""

        return self._prepare_resolution(
            canonical_metric_cell_id,
            knowledge_cutoff,
            policy,
            observed_through=observed_through,
        ).plan

    def _prepare_resolution(
        self,
        canonical_metric_cell_id: str,
        knowledge_cutoff: datetime,
        policy: ResolutionPolicy,
        *,
        observed_through: datetime,
    ) -> _PreparedResolution:
        cutoff, observed = _utc(knowledge_cutoff), _utc(observed_through)
        if observed < cutoff:
            raise ValueError("resolution observed_through must not precede knowledge_cutoff")
        if not canonical_metric_cell_id:
            raise ValueError("canonical_metric_cell_id is required")
        candidates = self._enumerate(
            canonical_metric_cell_id,
            cutoff,
            observed_through=observed,
        )
        if len(candidates) > MAX_CANDIDATES_PER_CANONICAL_CELL:
            raise ValueError(
                "canonical candidate universe exceeds the bounded relation policy "
                f"({MAX_CANDIDATES_PER_CANONICAL_CELL})"
            )
        universe_id = f"cfu_{_sha([canonical_metric_cell_id, _time(cutoff)])[:40]}"
        relation_set_id = f"cfrs_{_sha([universe_id, 'relations.v2'])[:39]}"
        resolution_key = _sha(
            [
                canonical_metric_cell_id,
                _time(cutoff),
                policy.name,
                policy.version,
                policy.config_sha256,
            ]
        )
        resolution_id = f"cfr_{resolution_key[:40]}"
        eligible = [candidate for candidate in candidates if candidate.eligibility == "eligible"]
        relations = self._relations(
            relation_set_id,
            eligible,
            cutoff,
            observed_through=observed,
        )
        status, selected, reason = self._outcome(eligible, relations)
        plan = ResolutionPlan(
            canonical_metric_cell_id=canonical_metric_cell_id,
            cutoff_at=cutoff,
            observed_through=observed,
            policy_name=policy.name,
            policy_version=policy.version,
            policy_config_sha256=policy.config_sha256,
            candidate_universe_id=universe_id,
            candidate_count=len(candidates),
            eligible_candidate_count=len(eligible),
            candidate_set_sha256=_sha([asdict(candidate) for candidate in candidates]),
            relation_set_id=relation_set_id,
            relation_count=len(relations),
            relation_set_sha256=_sha(relations),
            canonical_resolution_revision_id=resolution_id,
            status=status,
            selected_observation_id=selected,
            reason_code=reason[0],
            reason_details_sha256=_sha(reason[1]),
        )
        return _PreparedResolution(
            plan=plan,
            candidates=candidates,
            eligible=eligible,
            relations=relations,
            reason=reason,
        )

    def as_known(
        self,
        canonical_metric_cell_id: str,
        cutoff_at: datetime,
        *,
        observed_through: datetime | None = None,
    ) -> ResolutionReceipt | None:
        cutoff = _utc(cutoff_at)
        observed = cutoff if observed_through is None else _utc(observed_through)
        if observed < cutoff:
            raise ValueError("observed_through must not precede cutoff_at")
        row = self._conn.execute(
            "SELECT canonical_resolution_revision_id,candidate_universe_id,relation_set_id,"
            "status,selected_observation_id,reason_code "
            "FROM canonical_fact_resolution_revisions WHERE canonical_metric_cell_id=? "
            "AND knowledge_at<=? AND recorded_at<=? ORDER BY revision DESC LIMIT 1",
            (canonical_metric_cell_id, _time(cutoff), _time(observed)),
        ).fetchone()
        if row is None:
            return None
        self._verify_universe(str(row[1]), None)
        self._verify_relation_set(str(row[2]), None)
        self._verify_resolution(str(row[0]))
        return ResolutionReceipt(
            canonical_metric_cell_id=canonical_metric_cell_id,
            cutoff_at=cutoff,
            candidate_universe_id=str(row[1]),
            relation_set_id=str(row[2]),
            canonical_resolution_revision_id=str(row[0]),
            status=cast(Status, row[3]),
            selected_observation_id=row[4],
            reason_code=str(row[5]),
            exact_replay=True,
        )

    def seal_snapshot(
        self,
        resolution_snapshot_id: str,
        cutoff_at: datetime,
        recorded_at: datetime,
        scope: ResolutionSnapshotScope,
    ) -> VerifiedResolutionSnapshot:
        cutoff, recorded = _utc(cutoff_at), _utc(recorded_at)
        if recorded < cutoff:
            raise ValueError("snapshot recorded_at must not precede cutoff_at")
        self._verify_scope_registry(scope)
        members = self._latest_resolution_members(
            cutoff,
            scope,
            recorded_cutoff=recorded,
        )
        member_json = _json(members)
        key = f"snapshot:{resolution_snapshot_id}"
        scope_key = f"snapshot-scope:{resolution_snapshot_id}"
        scope_members = [
            {"reporting_entity_id": entity_id} for entity_id in scope.reporting_entity_ids
        ]
        scope_member_json = _json(scope_members)
        scope_commitment = {
            "cutoff_at": _time(cutoff),
            "member_set_sha256": _sha(member_json),
            "resolution_snapshot_id": resolution_snapshot_id,
            "scope_member_set_sha256": _sha(scope_member_json),
            "scope_sha256": scope.scope_sha256,
        }
        existing = self._conn.execute(
            "SELECT cutoff_at,member_count,canonical_member_set_json,member_set_sha256,recorded_at FROM canonical_fact_resolution_snapshot_seals WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        expected = (_time(cutoff), len(members), member_json, _sha(member_json), _time(recorded))
        if existing is not None:
            if tuple(existing) != expected:
                raise ValueError("canonical snapshot idempotency conflict")
            return self.verify_snapshot(resolution_snapshot_id, cutoff)
        self._conn.execute("SAVEPOINT seal_canonical_resolution_snapshot")
        try:
            self._conn.execute(
                "INSERT INTO canonical_fact_resolution_snapshot_scope_headers "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    resolution_snapshot_id,
                    scope_key,
                    scope.issuer_id,
                    scope.scope_version,
                    scope.canonical_json,
                    scope.scope_sha256,
                    _time(cutoff),
                    _time(recorded),
                ),
            )
            for ordinal, member in enumerate(scope_members):
                self._conn.execute(
                    "INSERT INTO canonical_fact_resolution_snapshot_scope_members VALUES (?,?,?,?)",
                    (
                        resolution_snapshot_id,
                        ordinal,
                        member["reporting_entity_id"],
                        _sha(member),
                    ),
                )
            for ordinal, member in enumerate(members):
                digest = _sha(member)
                self._conn.execute(
                    "INSERT INTO canonical_fact_resolution_snapshot_members VALUES (?,?,?,?,?,?,?)",
                    (
                        resolution_snapshot_id,
                        ordinal,
                        member["canonical_metric_cell_id"],
                        member["candidate_universe_id"],
                        member["relation_set_id"],
                        member["canonical_resolution_revision_id"],
                        digest,
                    ),
                )
            self._conn.execute(
                "INSERT INTO canonical_fact_resolution_snapshot_seals VALUES (?,?,?,?,?,?,?)",
                (resolution_snapshot_id, key, *expected),
            )
            self._conn.execute(
                "INSERT INTO canonical_fact_resolution_snapshot_scope_seals VALUES (?,?,?,?,?,?,?)",
                (
                    resolution_snapshot_id,
                    len(scope_members),
                    scope_member_json,
                    _sha(scope_member_json),
                    _json(scope_commitment),
                    _sha(scope_commitment),
                    _time(recorded),
                ),
            )
            receipt = self.verify_snapshot(
                resolution_snapshot_id,
                cutoff,
                observed_through=recorded,
            )
        except Exception:
            self._conn.execute("ROLLBACK TO SAVEPOINT seal_canonical_resolution_snapshot")
            self._conn.execute("RELEASE SAVEPOINT seal_canonical_resolution_snapshot")
            raise
        self._conn.execute("RELEASE SAVEPOINT seal_canonical_resolution_snapshot")
        return receipt

    def verify_snapshot(
        self,
        resolution_snapshot_id: str,
        cutoff_at: datetime,
        *,
        observed_through: datetime | None = None,
    ) -> VerifiedResolutionSnapshot:
        requested_observed = None if observed_through is None else _utc(observed_through)
        if requested_observed is not None and requested_observed < _utc(cutoff_at):
            raise ValueError("observed_through must not precede cutoff_at")
        scope_row = self._conn.execute(
            "SELECT issuer_id,scope_version,canonical_scope_json,scope_sha256,"
            "cutoff_at,recorded_at "
            "FROM canonical_fact_resolution_snapshot_scope_headers "
            "WHERE resolution_snapshot_id=?",
            (resolution_snapshot_id,),
        ).fetchone()
        if scope_row is None:
            raise ValueError("canonical snapshot scope is missing")
        try:
            parsed_scope = ResolutionSnapshotScope.model_validate_json(str(scope_row[2]))
        except ValueError as exc:
            raise ValueError("canonical snapshot scope is malformed") from exc
        if (
            parsed_scope.issuer_id != str(scope_row[0])
            or parsed_scope.scope_version != str(scope_row[1])
            or parsed_scope.canonical_json != str(scope_row[2])
            or parsed_scope.scope_sha256 != str(scope_row[3])
        ):
            raise ValueError("canonical snapshot scope is missing or tampered")
        self._verify_scope_registry(parsed_scope)
        row = self._conn.execute(
            "SELECT cutoff_at,member_count,canonical_member_set_json,member_set_sha256,recorded_at FROM canonical_fact_resolution_snapshot_seals WHERE resolution_snapshot_id=?",
            (resolution_snapshot_id,),
        ).fetchone()
        if row is None:
            raise ValueError("canonical snapshot is missing")
        observed = (
            _utc(datetime.fromisoformat(str(row[4])))
            if requested_observed is None
            else requested_observed
        )
        cutoff = _time(_utc(cutoff_at))
        if str(row[0]) != cutoff or str(scope_row[4]) != cutoff:
            raise ValueError("snapshot cutoff must be explicit and exact")
        if (
            _utc(datetime.fromisoformat(str(scope_row[5]))) > observed
            or _utc(datetime.fromisoformat(str(row[4]))) > observed
        ):
            raise ValueError("canonical snapshot was not recorded by observed_through")
        scope_member_rows = self._conn.execute(
            "SELECT reporting_entity_id,member_sha256 "
            "FROM canonical_fact_resolution_snapshot_scope_members "
            "WHERE resolution_snapshot_id=? ORDER BY member_ordinal",
            (resolution_snapshot_id,),
        ).fetchall()
        scope_members = [
            {"reporting_entity_id": scope_member[0]} for scope_member in scope_member_rows
        ]
        if tuple(
            member["reporting_entity_id"] for member in scope_members
        ) != parsed_scope.reporting_entity_ids or any(
            _sha(member) != scope_member[1]
            for member, scope_member in zip(scope_members, scope_member_rows, strict=True)
        ):
            raise ValueError("canonical snapshot scope members are missing or tampered")
        members = self._conn.execute(
            "SELECT canonical_metric_cell_id,candidate_universe_id,relation_set_id,canonical_resolution_revision_id,member_sha256 FROM canonical_fact_resolution_snapshot_members WHERE resolution_snapshot_id=? ORDER BY member_ordinal",
            (resolution_snapshot_id,),
        ).fetchall()
        payload = [
            {
                "canonical_metric_cell_id": r[0],
                "candidate_universe_id": r[1],
                "relation_set_id": r[2],
                "canonical_resolution_revision_id": r[3],
            }
            for r in members
        ]
        if (
            len(payload) != row[1]
            or _json(payload) != row[2]
            or _sha(row[2]) != row[3]
            or any(_sha(item) != member[4] for item, member in zip(payload, members, strict=True))
        ):
            raise ValueError("canonical snapshot members are missing or tampered")
        live_payload = self._latest_resolution_members(
            _utc(cutoff_at),
            parsed_scope,
            recorded_cutoff=datetime.fromisoformat(str(row[4])),
        )
        if _json(payload) != _json(live_payload):
            raise ValueError("canonical snapshot is not exhaustive latest-as-known state")
        for member in payload:
            self._verify_universe(str(member["candidate_universe_id"]), None)
            self._verify_relation_set(str(member["relation_set_id"]), None)
            self._verify_resolution(str(member["canonical_resolution_revision_id"]))
        scope_seal = self._conn.execute(
            "SELECT member_count,canonical_member_set_json,member_set_sha256,"
            "canonical_snapshot_commitment_json,snapshot_commitment_sha256,sealed_at "
            "FROM canonical_fact_resolution_snapshot_scope_seals "
            "WHERE resolution_snapshot_id=?",
            (resolution_snapshot_id,),
        ).fetchone()
        scope_member_json = _json(scope_members)
        commitment = {
            "cutoff_at": cutoff,
            "member_set_sha256": str(row[3]),
            "resolution_snapshot_id": resolution_snapshot_id,
            "scope_member_set_sha256": _sha(scope_member_json),
            "scope_sha256": parsed_scope.scope_sha256,
        }
        if (
            scope_seal is None
            or int(scope_seal[0]) != len(scope_members)
            or str(scope_seal[1]) != scope_member_json
            or str(scope_seal[2]) != _sha(scope_member_json)
            or str(scope_seal[3]) != _json(commitment)
            or str(scope_seal[4]) != _sha(commitment)
        ):
            raise ValueError("canonical snapshot scope seal is missing or tampered")
        if _utc(datetime.fromisoformat(str(scope_seal[5]))) > observed:
            raise ValueError("canonical snapshot was not sealed by observed_through")
        return VerifiedResolutionSnapshot(
            resolution_snapshot_id=resolution_snapshot_id,
            scope=parsed_scope,
            cutoff_at=_utc(cutoff_at),
            recorded_at=datetime.fromisoformat(str(row[4])),
            member_count=int(row[1]),
            member_set_sha256=str(row[3]),
            scope_member_set_sha256=str(scope_seal[2]),
            scope_sha256=parsed_scope.scope_sha256,
            snapshot_commitment_sha256=str(scope_seal[4]),
        )

    def _verify_scope_registry(self, scope: ResolutionSnapshotScope) -> None:
        rows = self._conn.execute(
            "SELECT reporting_entity_id,issuer_id FROM reporting_entities "
            "WHERE reporting_entity_id IN (SELECT value FROM json_each(?)) "
            "ORDER BY reporting_entity_id",
            (_json(list(scope.reporting_entity_ids)),),
        ).fetchall()
        actual = tuple((str(row[0]), str(row[1])) for row in rows)
        expected = tuple(
            (reporting_entity_id, scope.issuer_id)
            for reporting_entity_id in scope.reporting_entity_ids
        )
        if actual != expected:
            raise ValueError(
                "canonical resolution snapshot scope is not an exact registered issuer universe"
            )

    def _latest_resolution_members(
        self,
        cutoff: datetime,
        scope: ResolutionSnapshotScope,
        *,
        recorded_cutoff: datetime | None = None,
    ) -> list[dict[str, object]]:
        cutoff_s = _time(cutoff)
        recorded_cutoff_s = _time(recorded_cutoff or cutoff)
        rows = self._conn.execute(
            "SELECT r.canonical_metric_cell_id,r.candidate_universe_id,"
            "r.relation_set_id,r.canonical_resolution_revision_id "
            "FROM canonical_fact_resolution_revisions r "
            "JOIN canonical_metric_cells cell "
            "ON cell.canonical_metric_cell_id=r.canonical_metric_cell_id "
            "WHERE r.knowledge_at<=? AND r.recorded_at<=? "
            "AND cell.reporting_entity_id IN (SELECT value FROM json_each(?)) "
            "AND NOT EXISTS (SELECT 1 "
            "FROM canonical_fact_resolution_revisions newer "
            "WHERE newer.canonical_metric_cell_id=r.canonical_metric_cell_id "
            "AND newer.knowledge_at<=? AND newer.recorded_at<=? "
            "AND newer.revision>r.revision) "
            "ORDER BY r.canonical_metric_cell_id",
            (
                cutoff_s,
                recorded_cutoff_s,
                _json(list(scope.reporting_entity_ids)),
                cutoff_s,
                recorded_cutoff_s,
            ),
        ).fetchall()
        return [
            {
                "canonical_metric_cell_id": row[0],
                "candidate_universe_id": row[1],
                "relation_set_id": row[2],
                "canonical_resolution_revision_id": row[3],
            }
            for row in rows
        ]

    def _verify_resolution(self, resolution_id: str) -> None:
        row = self._conn.execute(
            "SELECT canonical_metric_cell_id,revision,candidate_universe_id,"
            "relation_set_id,candidate_universe_seal_id,"
            "relation_set_seal_id,candidate_universe_sha256,"
            "relation_set_sha256,policy_name,policy_version,"
            "policy_config_sha256,status,selected_observation_id,"
            "reason_code,reason_details_json,canonical_resolution_json,"
            "resolution_sha256,supersedes_resolution_revision_id "
            "FROM canonical_fact_resolution_revisions "
            "WHERE canonical_resolution_revision_id=?",
            (resolution_id,),
        ).fetchone()
        if row is None or _sha(str(row[15])) != str(row[16]):
            raise ValueError("canonical resolution commitment is missing or tampered")
        payload = _json(
            {
                "candidate_universe_id": row[2],
                "candidate_universe_seal_id": row[4],
                "candidate_universe_sha256": row[6],
                "canonical_metric_cell_id": row[0],
                "policy_config_sha256": row[10],
                "policy_name": row[8],
                "policy_version": row[9],
                "reason_code": row[13],
                "reason_details": json.loads(str(row[14])),
                "relation_set_id": row[3],
                "relation_set_seal_id": row[5],
                "relation_set_sha256": row[7],
                "revision": row[1],
                "selected_observation_id": row[12],
                "status": row[11],
                "supersedes_resolution_revision_id": row[17],
            }
        )
        if payload != str(row[15]):
            raise ValueError("canonical resolution row does not match commitment")
        universe = self._conn.execute(
            "SELECT candidate_universe_seal_id,member_set_sha256 "
            "FROM canonical_fact_candidate_universe_seals "
            "WHERE candidate_universe_id=?",
            (row[2],),
        ).fetchone()
        relation = self._conn.execute(
            "SELECT relation_set_seal_id,relation_set_sha256 "
            "FROM canonical_fact_relation_set_seals WHERE relation_set_id=?",
            (row[3],),
        ).fetchone()
        if (
            universe is None
            or relation is None
            or tuple(universe) != (row[4], row[6])
            or tuple(relation) != (row[5], row[7])
        ):
            raise ValueError("canonical resolution input seal linkage changed")
        if int(row[1]) > 1:
            parent = self._conn.execute(
                "SELECT canonical_resolution_revision_id "
                "FROM canonical_fact_resolution_revisions "
                "WHERE canonical_metric_cell_id=? AND revision=?",
                (row[0], int(row[1]) - 1),
            ).fetchone()
            if parent is None or parent[0] != row[17]:
                raise ValueError("canonical resolution parent chain is incomplete")

    def _enumerate(
        self,
        cell_id: str,
        cutoff: datetime,
        *,
        observed_through: datetime | None = None,
    ) -> list[_Candidate]:
        cutoff_s = _time(cutoff)
        observed_s = _time(cutoff if observed_through is None else observed_through)
        rows = self._conn.execute(
            """
            SELECT o.observation_id,o.fact_cell_id,o.observation_kind,
                   b.binding_revision_id,p.observation_payload_sha256,
                   b.commitment_sha256,mapping.commitment_sha256,
                   o.numeric_value,o.text_value,o.is_nil,c.currency,c.unit_key,
                   o.effective_at,o.knowledge_at,o.recorded_at
            FROM fact_cell_canonical_binding_revisions b
            JOIN fact_observations_v2 o
              ON o.observation_id=b.source_observation_id
             AND o.fact_cell_id=b.fact_cell_id
            JOIN fact_cells_v2 c ON c.fact_cell_id=o.fact_cell_id
            JOIN fact_observation_payload_commitments_v2 p ON p.observation_id=o.observation_id
            JOIN metric_mapping_revisions mapping ON mapping.mapping_revision_id=b.mapping_revision_id
            JOIN canonical_metric_cell_seals cell_seal
              ON cell_seal.canonical_metric_cell_id=b.canonical_metric_cell_id
            WHERE b.canonical_metric_cell_id=? AND b.binding_status='bound'
              AND o.knowledge_at<=? AND o.recorded_at<=?
              AND b.knowledge_at<=? AND b.recorded_at<=?
              AND mapping.knowledge_at<=? AND mapping.recorded_at<=?
              AND cell_seal.sealed_at<=?
              AND NOT EXISTS (
                SELECT 1 FROM fact_cell_canonical_binding_revisions newer
                WHERE newer.source_observation_id=b.source_observation_id
                  AND newer.knowledge_at<=? AND newer.recorded_at<=?
                  AND newer.revision>b.revision)
            ORDER BY o.observation_id
            LIMIT ?
        """,
            (
                cell_id,
                cutoff_s,
                observed_s,
                cutoff_s,
                observed_s,
                cutoff_s,
                observed_s,
                observed_s,
                cutoff_s,
                observed_s,
                MAX_CANDIDATES_PER_CANONICAL_CELL + 1,
            ),
        ).fetchall()
        candidates: list[_Candidate] = []
        for row in rows:
            observation_id = str(row[0])
            publication = self._conn.execute(
                "SELECT m.publication_id,s.publication_seal_id,"
                "m.publication_member_id,m.canonical_member_sha256,"
                "m.record_commitment_sha256 "
                "FROM source_fact_publication_members m "
                "JOIN source_fact_publications p "
                "ON p.publication_id=m.publication_id "
                "JOIN source_fact_publication_seals s "
                "ON s.publication_id=m.publication_id "
                "WHERE m.record_kind='fact_observation' AND m.record_id=? "
                "AND datetime(p.created_at)<=datetime(?) "
                "AND datetime(p.recorded_at)<=datetime(?) "
                "AND datetime(s.sealed_at)<=datetime(?) "
                "ORDER BY s.sealed_at DESC,m.publication_id DESC LIMIT 1",
                (observation_id, cutoff_s, observed_s, observed_s),
            ).fetchone()
            observation_kind = str(row[2])
            if observation_kind != "reported":
                lane = "derived_terminal_exclusion"
                eligibility: Literal["eligible", "ineligible"] = "ineligible"
                reason_code = "derived_observation_not_admitted"
                publication = None
            elif publication is None:
                lane = "missing_publication_exclusion"
                eligibility = "ineligible"
                reason_code = "missing_sealed_source_publication"
            else:
                verified = verify_source_fact_publication(
                    self._conn,
                    publication_id=str(publication[0]),
                    cutoff=_utc(cutoff),
                    observed_through=_utc(cutoff if observed_through is None else observed_through),
                )
                if verified.publication_seal_id != str(publication[1]):
                    raise ValueError("source publication seal identity changed")
                eligibility = "eligible"
                reason_code = "sealed_source_publication"
                lane = "reported_source_publication"
            filing = None
            if publication is not None:
                filing = self._conn.execute(
                    "SELECT d.disposition_id FROM "
                    "filing_xbrl_extraction_dispositions d "
                    "JOIN filing_xbrl_extraction_disposition_seals s "
                    "ON s.extraction_run_id=d.extraction_run_id "
                    "WHERE d.observation_id=? AND d.disposition='published' "
                    "AND s.publication_id=? AND d.knowledge_at<=? "
                    "AND d.recorded_at<=? ORDER BY d.input_ordinal LIMIT 1",
                    (observation_id, str(publication[0]), cutoff_s, observed_s),
                ).fetchone()
                if filing is not None:
                    lane = "filing_xbrl"
                    reason_code = "sealed_filing_xbrl_admission"
            candidates.append(
                _Candidate(
                    observation_id=observation_id,
                    fact_cell_id=str(row[1]),
                    binding_revision_id=str(row[3]),
                    payload_sha256=str(row[4]),
                    publication_id=(None if publication is None else str(publication[0])),
                    publication_seal_id=(None if publication is None else str(publication[1])),
                    publication_member_id=(None if publication is None else str(publication[2])),
                    source_member_sha256=(None if publication is None else str(publication[3])),
                    source_member_commitment_sha256=(
                        None if publication is None else str(publication[4])
                    ),
                    binding_commitment_sha256=str(row[5]),
                    mapping_commitment_sha256=str(row[6]),
                    filing_disposition_id=(None if filing is None else str(filing[0])),
                    source_lane=lane,
                    eligibility=eligibility,
                    reason_code=reason_code,
                    numeric_value=row[7],
                    text_value=row[8],
                    is_nil=bool(row[9]),
                    currency=row[10],
                    unit_key=str(row[11]),
                    effective_at=str(row[12]),
                    knowledge_at=str(row[13]),
                    recorded_at=str(row[14]),
                )
            )
        return candidates

    def _persist_universe(
        self,
        universe_id: str,
        cell_id: str,
        cutoff: datetime,
        recorded_at: datetime,
        candidates: list[_Candidate],
    ) -> bool:
        members = [
            {
                "binding_revision_id": c.binding_revision_id,
                "binding_commitment_sha256": c.binding_commitment_sha256,
                "candidate_ordinal": i,
                "eligibility": c.eligibility,
                "filing_disposition_id": c.filing_disposition_id,
                "mapping_commitment_sha256": c.mapping_commitment_sha256,
                "observation_id": c.observation_id,
                "observation_payload_sha256": c.payload_sha256,
                "publication_id": c.publication_id,
                "publication_member_id": c.publication_member_id,
                "publication_seal_id": c.publication_seal_id,
                "reason_code": c.reason_code,
                "source_lane": c.source_lane,
                "source_publication_record_commitment_sha256": c.source_member_commitment_sha256,
                "source_publication_member_sha256": c.source_member_sha256,
            }
            for i, c in enumerate(candidates)
        ]
        member_json, now = _json(members), _time(recorded_at)
        existing = self._conn.execute(
            "SELECT canonical_member_set_json,member_set_sha256 FROM canonical_fact_candidate_universe_revisions WHERE idempotency_key=?",
            (f"universe:{universe_id}",),
        ).fetchone()
        if existing is not None:
            if tuple(existing) != (member_json, _sha(member_json)):
                raise ValueError("candidate universe replay conflict")
            self._verify_universe(universe_id, candidates)
            return True
        self._conn.execute(
            "INSERT INTO canonical_fact_candidate_universe_revisions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                universe_id,
                f"universe:{universe_id}",
                cell_id,
                now,
                len(members),
                member_json,
                _sha(member_json),
                _time(cutoff),
                _time(cutoff),
                now,
            ),
        )
        for ordinal, candidate in enumerate(candidates):
            identity = f"{universe_id}:{candidate.observation_id}"
            self._conn.execute(
                "INSERT INTO canonical_fact_candidate_dispositions ("
                "candidate_disposition_id,idempotency_key,"
                "candidate_universe_id,candidate_ordinal,observation_id,"
                "source_fact_cell_id,binding_revision_id,"
                "binding_commitment_sha256,mapping_commitment_sha256,"
                "observation_payload_sha256,source_publication_id,"
                "source_publication_seal_id,source_publication_member_id,"
                "source_publication_member_sha256,"
                "source_record_commitment_sha256,filing_disposition_id,"
                "source_lane,eligibility,reason_code,reason_details_json,"
                "effective_at,knowledge_at,recorded_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"cfd_{_sha(identity)[:40]}",
                    f"candidate:{identity}",
                    universe_id,
                    ordinal,
                    candidate.observation_id,
                    candidate.fact_cell_id,
                    candidate.binding_revision_id,
                    candidate.binding_commitment_sha256,
                    candidate.mapping_commitment_sha256,
                    candidate.payload_sha256,
                    candidate.publication_id,
                    candidate.publication_seal_id,
                    candidate.publication_member_id,
                    candidate.source_member_sha256,
                    candidate.source_member_commitment_sha256,
                    candidate.filing_disposition_id,
                    candidate.source_lane,
                    candidate.eligibility,
                    candidate.reason_code,
                    "{}",
                    candidate.effective_at,
                    candidate.knowledge_at,
                    now,
                ),
            )
        self._conn.execute(
            "INSERT INTO canonical_fact_candidate_universe_seals VALUES (?,?,?,?,?,?)",
            (
                universe_id,
                f"cfus_{_sha(universe_id)[:40]}",
                len(members),
                member_json,
                _sha(member_json),
                now,
            ),
        )
        return False

    def _relations(
        self,
        relation_set_id: str,
        candidates: list[_Candidate],
        cutoff: datetime,
        *,
        observed_through: datetime | None = None,
    ) -> list[dict[str, object]]:
        by_id = {c.observation_id: c for c in candidates}
        relations: list[dict[str, object]] = []
        # Preserve 0242 duplicate entry ordinal without inflating candidates.
        for candidate in candidates:
            duplicates = self._conn.execute(
                "SELECT disposition_id,input_ordinal FROM filing_xbrl_extraction_dispositions d JOIN filing_xbrl_extraction_disposition_seals s ON s.extraction_run_id=d.extraction_run_id WHERE d.observation_id=? AND d.disposition='duplicate' AND d.knowledge_at<=? AND d.recorded_at<=? AND s.knowledge_at<=? AND s.recorded_at<=? ORDER BY d.input_ordinal",
                (
                    candidate.observation_id,
                    _time(cutoff),
                    _time(cutoff if observed_through is None else observed_through),
                    _time(cutoff),
                    _time(cutoff if observed_through is None else observed_through),
                ),
            ).fetchall()
            for duplicate_id, ordinal in duplicates:
                relations.append(
                    {
                        "subject_filing_disposition_id": str(duplicate_id),
                        "subject_observation_id": None,
                        "object_observation_id": candidate.observation_id,
                        "relation_kind": "exact_duplicate_of",
                        "evidence": {"input_ordinal": ordinal, "kind": "0242_duplicate"},
                    }
                )
        for candidate in candidates:
            parent = self._conn.execute(
                "SELECT supersedes_observation_id,revision_kind FROM fact_observations_v2 WHERE observation_id=?",
                (candidate.observation_id,),
            ).fetchone()
            if parent is not None and parent[0] in by_id:
                kind = "recasts" if parent[1] == "presentation_recast" else "supersedes"
                relations.append(
                    {
                        "subject_filing_disposition_id": None,
                        "subject_observation_id": candidate.observation_id,
                        "object_observation_id": str(parent[0]),
                        "relation_kind": kind,
                        "evidence": {"revision_kind": parent[1]},
                    }
                )
        for left_index, left in enumerate(candidates):
            for right in candidates[left_index + 1 :]:
                if left.value_key != right.value_key:
                    kind = "conflicts_with"
                    evidence = {
                        "left": left.value_key,
                        "right": right.value_key,
                    }
                else:
                    kind = "source_equivalent_to"
                    evidence = {"value_key": left.value_key}
                for subject, object_id in (
                    (left.observation_id, right.observation_id),
                    (right.observation_id, left.observation_id),
                ):
                    relations.append(
                        {
                            "subject_filing_disposition_id": None,
                            "subject_observation_id": subject,
                            "object_observation_id": object_id,
                            "relation_kind": kind,
                            "evidence": evidence,
                        }
                    )
        return relations

    def _persist_relation_set(
        self,
        relation_set_id: str,
        universe_id: str,
        cutoff: datetime,
        recorded_at: datetime,
        relations: list[dict[str, object]],
    ) -> bool:
        payload = _json(relations)
        existing = self._conn.execute(
            "SELECT canonical_relation_set_json,relation_set_sha256 FROM canonical_fact_relation_set_revisions WHERE idempotency_key=?",
            (f"relations:{relation_set_id}",),
        ).fetchone()
        if existing is not None:
            if tuple(existing) != (payload, _sha(payload)):
                raise ValueError("relation set replay conflict")
            return True
        clock, written_at = _time(cutoff), _time(recorded_at)
        self._conn.execute(
            "INSERT INTO canonical_fact_relation_set_revisions VALUES (?,?,?,?,?,?,?,?,?)",
            (
                relation_set_id,
                f"relations:{relation_set_id}",
                universe_id,
                len(relations),
                payload,
                _sha(payload),
                clock,
                clock,
                written_at,
            ),
        )
        for ordinal, relation in enumerate(relations):
            evidence = _json(relation["evidence"])
            identity = f"{relation_set_id}:{ordinal}"
            self._conn.execute(
                "INSERT INTO canonical_fact_relation_assertions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"cfra_{_sha(identity)[:39]}",
                    f"relation:{identity}",
                    relation_set_id,
                    ordinal,
                    relation["subject_observation_id"],
                    relation["subject_filing_disposition_id"],
                    relation["object_observation_id"],
                    relation["relation_kind"],
                    evidence,
                    _sha(evidence),
                    clock,
                    clock,
                    written_at,
                ),
            )
        self._conn.execute(
            "INSERT INTO canonical_fact_relation_set_seals VALUES (?,?,?,?,?,?)",
            (
                relation_set_id,
                f"cfrss_{_sha(relation_set_id)[:39]}",
                len(relations),
                payload,
                _sha(payload),
                written_at,
            ),
        )
        return False

    @staticmethod
    def _outcome(
        candidates: list[_Candidate], relations: list[dict[str, object]]
    ) -> tuple[Status, str | None, tuple[str, dict[str, object]]]:
        if not candidates:
            return "unresolved", None, ("no_admitted_observation", {})
        values = {candidate.value_key for candidate in candidates}
        if len(values) == 1:
            return (
                "resolved",
                candidates[0].observation_id,
                (
                    "exact_assertion_agreement",
                    {"retained_observations": [c.observation_id for c in candidates]},
                ),
            )
        dominates: dict[str, set[str]] = {}
        for relation in relations:
            if relation["relation_kind"] in {"supersedes", "recasts", "amends"}:
                subject = str(relation["subject_observation_id"])
                dominates.setdefault(subject, set()).add(str(relation["object_observation_id"]))
        # A policy may select only a complete deterministic revision chain. A
        # superseder that still has an undominated dissenting assertion remains
        # unresolved even when it happens to be the newest source assertion.
        closure: dict[str, set[str]] = {}
        for candidate in candidates:
            seen: set[str] = set()
            todo = list(dominates.get(candidate.observation_id, set()))
            while todo:
                item = todo.pop()
                if item not in seen:
                    seen.add(item)
                    todo.extend(dominates.get(item, set()))
            closure[candidate.observation_id] = seen
        all_ids = {candidate.observation_id for candidate in candidates}
        winners = [
            candidate
            for candidate in candidates
            if closure[candidate.observation_id] >= all_ids - {candidate.observation_id}
        ]
        if len(winners) == 1:
            return (
                "resolved",
                winners[0].observation_id,
                (
                    "deterministic_amendment_or_recast",
                    {
                        "relation_evidence": [
                            r
                            for r in relations
                            if r["subject_observation_id"] == winners[0].observation_id
                        ]
                    },
                ),
            )
        return (
            "unresolved",
            None,
            (
                "materially_conflicting_assertions",
                {"observations": [c.observation_id for c in candidates]},
            ),
        )

    def _persist_resolution(
        self,
        resolution_id: str,
        key: str,
        cell_id: str,
        universe_id: str,
        relation_set_id: str,
        cutoff: datetime,
        recorded_at: datetime,
        policy: ResolutionPolicy,
        status: Status,
        selected: str | None,
        reason: tuple[str, dict[str, object]],
    ) -> bool:
        universe_seal = self._conn.execute(
            "SELECT candidate_universe_seal_id,member_set_sha256 "
            "FROM canonical_fact_candidate_universe_seals "
            "WHERE candidate_universe_id=?",
            (universe_id,),
        ).fetchone()
        relation_seal = self._conn.execute(
            "SELECT relation_set_seal_id,relation_set_sha256 "
            "FROM canonical_fact_relation_set_seals WHERE relation_set_id=?",
            (relation_set_id,),
        ).fetchone()
        if universe_seal is None or relation_seal is None:
            raise ValueError("canonical resolution inputs are not finally sealed")
        existing = self._conn.execute(
            "SELECT candidate_universe_id,relation_set_id,policy_name,policy_version,policy_config_sha256,status,selected_observation_id,reason_code,reason_details_json FROM canonical_fact_resolution_revisions WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        expected = (
            universe_id,
            relation_set_id,
            policy.name,
            policy.version,
            policy.config_sha256,
            status,
            selected,
            reason[0],
            _json(reason[1]),
        )
        if existing is not None:
            if tuple(existing) != expected:
                raise ValueError("canonical resolution replay conflict")
            return True
        revision = self._conn.execute(
            "SELECT COALESCE(MAX(revision),0)+1 FROM canonical_fact_resolution_revisions WHERE canonical_metric_cell_id=?",
            (cell_id,),
        ).fetchone()[0]
        clock, written_at = _time(cutoff), _time(recorded_at)
        prior = None
        if revision > 1:
            prior = self._conn.execute(
                "SELECT canonical_resolution_revision_id FROM canonical_fact_resolution_revisions WHERE canonical_metric_cell_id=? AND revision=?",
                (cell_id, revision - 1),
            ).fetchone()[0]
        payload = _json(
            {
                "candidate_universe_id": universe_id,
                "candidate_universe_seal_id": universe_seal[0],
                "candidate_universe_sha256": universe_seal[1],
                "canonical_metric_cell_id": cell_id,
                "policy_config_sha256": policy.config_sha256,
                "policy_name": policy.name,
                "policy_version": policy.version,
                "reason_code": reason[0],
                "reason_details": reason[1],
                "relation_set_id": relation_set_id,
                "relation_set_seal_id": relation_seal[0],
                "relation_set_sha256": relation_seal[1],
                "revision": revision,
                "selected_observation_id": selected,
                "status": status,
                "supersedes_resolution_revision_id": prior,
            }
        )
        self._conn.execute(
            "INSERT INTO canonical_fact_resolution_revisions ("
            "canonical_resolution_revision_id,idempotency_key,"
            "canonical_metric_cell_id,revision,candidate_universe_id,"
            "relation_set_id,candidate_universe_seal_id,"
            "relation_set_seal_id,candidate_universe_sha256,"
            "relation_set_sha256,policy_name,policy_version,"
            "policy_config_sha256,status,selected_observation_id,"
            "reason_code,reason_details_json,canonical_resolution_json,"
            "resolution_sha256,effective_at,knowledge_at,recorded_at,"
            "supersedes_resolution_revision_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                resolution_id,
                key,
                cell_id,
                revision,
                universe_id,
                relation_set_id,
                universe_seal[0],
                relation_seal[0],
                universe_seal[1],
                relation_seal[1],
                policy.name,
                policy.version,
                policy.config_sha256,
                status,
                selected,
                reason[0],
                _json(reason[1]),
                payload,
                _sha(payload),
                clock,
                clock,
                written_at,
                prior,
            ),
        )
        return False

    def _verify_universe(self, universe_id: str, expected: list[_Candidate] | None) -> None:
        row = self._conn.execute(
            "SELECT h.member_count,h.canonical_member_set_json,"
            "h.member_set_sha256,s.candidate_universe_seal_id "
            "FROM canonical_fact_candidate_universe_revisions h "
            "JOIN canonical_fact_candidate_universe_seals s "
            "ON s.candidate_universe_id=h.candidate_universe_id "
            "AND s.member_count=h.member_count "
            "AND s.canonical_member_set_json=h.canonical_member_set_json "
            "AND s.member_set_sha256=h.member_set_sha256 "
            "WHERE h.candidate_universe_id=?",
            (universe_id,),
        ).fetchone()
        members = self._conn.execute(
            "SELECT candidate_ordinal,observation_id,binding_revision_id,"
            "binding_commitment_sha256,filing_disposition_id,"
            "mapping_commitment_sha256,observation_payload_sha256,"
            "source_publication_id,source_publication_member_id,"
            "source_publication_seal_id,reason_code,source_lane,"
            "source_publication_member_sha256,"
            "source_record_commitment_sha256,eligibility "
            "FROM canonical_fact_candidate_dispositions "
            "WHERE candidate_universe_id=? ORDER BY candidate_ordinal",
            (universe_id,),
        ).fetchall()
        payload = [
            {
                "binding_commitment_sha256": r[3],
                "binding_revision_id": r[2],
                "candidate_ordinal": r[0],
                "eligibility": r[14],
                "filing_disposition_id": r[4],
                "mapping_commitment_sha256": r[5],
                "observation_id": r[1],
                "observation_payload_sha256": r[6],
                "publication_id": r[7],
                "publication_member_id": r[8],
                "publication_seal_id": r[9],
                "reason_code": r[10],
                "source_lane": r[11],
                "source_publication_member_sha256": r[12],
                "source_publication_record_commitment_sha256": r[13],
            }
            for r in members
        ]
        if (
            row is None
            or len(payload) != row[0]
            or _json(payload) != row[1]
            or _sha(row[1]) != row[2]
            or [r[0] for r in members] != list(range(len(members)))
        ):
            raise ValueError("candidate universe is missing, non-contiguous, or tampered")
        if expected is not None and [r[1] for r in members] != [
            candidate.observation_id for candidate in expected
        ]:
            raise ValueError("candidate universe is not exhaustive at cutoff")

    def _verify_relation_set(
        self, relation_set_id: str, expected: list[dict[str, object]] | None
    ) -> None:
        row = self._conn.execute(
            "SELECT h.relation_count,h.canonical_relation_set_json,"
            "h.relation_set_sha256,s.relation_set_seal_id "
            "FROM canonical_fact_relation_set_revisions h "
            "JOIN canonical_fact_relation_set_seals s "
            "ON s.relation_set_id=h.relation_set_id "
            "AND s.relation_count=h.relation_count "
            "AND s.canonical_relation_set_json=h.canonical_relation_set_json "
            "AND s.relation_set_sha256=h.relation_set_sha256 "
            "WHERE h.relation_set_id=?",
            (relation_set_id,),
        ).fetchone()
        rows = self._conn.execute(
            "SELECT relation_ordinal,subject_observation_id,subject_filing_disposition_id,object_observation_id,relation_kind,evidence_json,evidence_sha256 FROM canonical_fact_relation_assertions WHERE relation_set_id=? ORDER BY relation_ordinal",
            (relation_set_id,),
        ).fetchall()
        payload = [
            {
                "subject_filing_disposition_id": r[2],
                "subject_observation_id": r[1],
                "object_observation_id": r[3],
                "relation_kind": r[4],
                "evidence": json.loads(r[5]),
            }
            for r in rows
        ]
        if (
            row is None
            or len(rows) != row[0]
            or _json(payload) != row[1]
            or _sha(row[1]) != row[2]
            or [r[0] for r in rows] != list(range(len(rows)))
            or any(_sha(r[5]) != r[6] for r in rows)
        ):
            raise ValueError("relation set is missing, non-contiguous, or tampered")
        if expected is not None and _json(payload) != _json(expected):
            raise ValueError("relation set replay mismatch")
