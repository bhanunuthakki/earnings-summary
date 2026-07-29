"""Canonical typed append boundary for the hardened evidence-first fact plane."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from provenance.fact_plane_v2 import (
    DerivationSealV2,
    DerivedFactObservationV2,
    ExtractionRunCompletenessSealV2,
    FactCellV2,
    FactPlaneV2,
    FactResolutionRevisionV2,
    ObservationRelationV2,
    PersistResult,
    ReportedFactObservationV2,
)
from provenance.source_fact_publication import (
    PUBLICATION_PAYLOAD_VERSION,
    RECORD_COMMITMENT_VERSION,
    PublicationMemberKind,
    SourceFactPublicationMember,
    canonical_json,
    digest_text,
    publication_member_id,
    publication_member_idempotency_key,
    publication_member_payload,
    publication_payload,
    publication_seal_id,
    publication_seal_idempotency_key,
    record_commitment,
    record_idempotency_key,
    verify_source_fact_publication,
)
from provenance.source_fact_stream import (
    append_verified_publication_event,
    publication_event_for_publication,
    register_source_fact_stream_functions,
    require_source_fact_stream_schema,
)

_UNSET_PUBLICATION_TIME = datetime(1970, 1, 1, tzinfo=UTC)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReportedSourceFact(_FrozenModel):
    cell: FactCellV2
    observation: ReportedFactObservationV2

    @model_validator(mode="after")
    def _same_cell(self) -> Self:
        if self.observation.fact_cell_id != self.cell.fact_cell_id:
            raise ValueError("reported observation must belong to its cell")
        return self


class DerivedSourceFact(_FrozenModel):
    cell: FactCellV2
    observation: DerivedFactObservationV2

    @model_validator(mode="after")
    def _same_cell(self) -> Self:
        if self.observation.fact_cell_id != self.cell.fact_cell_id:
            raise ValueError("derived observation must belong to its cell")
        return self


class SourceFactPublication(_FrozenModel):
    """One deterministic publication unit, ordered by dependency."""

    publication_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    created_at: datetime = _UNSET_PUBLICATION_TIME
    recorded_at: datetime = _UNSET_PUBLICATION_TIME
    reported_facts: tuple[ReportedSourceFact, ...] = ()
    derived_facts: tuple[DerivedSourceFact, ...] = ()
    relations: tuple[ObservationRelationV2, ...] = ()
    derivations: tuple[DerivationSealV2, ...] = ()
    extraction_seals: tuple[ExtractionRunCompletenessSealV2, ...] = ()
    resolutions: tuple[FactResolutionRevisionV2, ...] = ()

    @model_validator(mode="after")
    def _closed_graph(self) -> Self:
        clocks = [
            item.observation.recorded_at for item in (*self.reported_facts, *self.derived_facts)
        ]
        clocks.extend(item.recorded_at for item in self.relations)
        clocks.extend(item.recorded_at for item in self.derivations)
        clocks.extend(item.recorded_at for item in self.extraction_seals)
        clocks.extend(item.recorded_at for item in self.resolutions)
        if (
            self.created_at == _UNSET_PUBLICATION_TIME
            or self.recorded_at == _UNSET_PUBLICATION_TIME
        ):
            if not clocks:
                raise ValueError(
                    "an empty publication requires explicit created_at and recorded_at"
                )
            published_at = max(clocks, key=_utc)
            if self.created_at == _UNSET_PUBLICATION_TIME:
                object.__setattr__(self, "created_at", published_at)
            if self.recorded_at == _UNSET_PUBLICATION_TIME:
                object.__setattr__(
                    self,
                    "recorded_at",
                    max((published_at, self.created_at), key=_utc),
                )
        if _utc(self.recorded_at) < _utc(self.created_at):
            raise ValueError("publication clocks are inconsistent")
        observations = tuple(
            item.observation for item in (*self.reported_facts, *self.derived_facts)
        )
        observation_ids = tuple(item.observation_id for item in observations)
        if len(observation_ids) != len(set(observation_ids)):
            raise ValueError("publication observations cannot repeat")
        idempotency_keys = tuple(item.idempotency_key for item in observations)
        if len(idempotency_keys) != len(set(idempotency_keys)):
            raise ValueError("publication observation idempotency keys cannot repeat")
        derivation_outputs = tuple(item.derived_observation_id for item in self.derivations)
        if len(derivation_outputs) != len(set(derivation_outputs)):
            raise ValueError("derived observations can have only one seal")
        extraction_runs = tuple(item.extraction_run_id for item in self.extraction_seals)
        if len(extraction_runs) != len(set(extraction_runs)):
            raise ValueError("extraction runs can have only one completeness seal")
        return self


class PublicationReceipt(_FrozenModel):
    publication_id: str
    idempotency_key: str
    cell_ids: tuple[str, ...]
    observation_ids: tuple[str, ...]
    relation_ids: tuple[str, ...]
    derivation_seal_ids: tuple[str, ...]
    extraction_seal_ids: tuple[str, ...]
    resolution_revision_ids: tuple[str, ...]
    publication_payload_sha256: str
    publication_seal_id: str
    publication_sequence: int = Field(gt=0)
    publication_event_sha256: str = Field(min_length=64, max_length=64)
    created_record_ids: tuple[str, ...]
    exact_replay: bool


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _sql_publication_payload_v1(*values: object) -> str:
    (
        publication_id,
        idempotency_key,
        member_set_sha256,
        cell_count,
        observation_count,
        relation_count,
        derivation_seal_count,
        extraction_seal_count,
        resolution_revision_count,
        member_count,
        created_at,
        recorded_at,
    ) = values
    return publication_payload(
        publication_id=publication_id,
        idempotency_key=idempotency_key,
        member_set_sha256=member_set_sha256,
        cell_count=cell_count,
        observation_count=observation_count,
        relation_count=relation_count,
        derivation_seal_count=derivation_seal_count,
        extraction_seal_count=extraction_seal_count,
        resolution_revision_count=resolution_revision_count,
        member_count=member_count,
        created_at=created_at,
        recorded_at=recorded_at,
    )


class SourceFactRepository:
    """Sequence and publish hardened facts without any legacy fallback."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._plane = FactPlaneV2(conn)
        self._conn.create_function(
            "source_fact_publication_payload_v1",
            -1,
            _sql_publication_payload_v1,
            deterministic=True,
        )
        register_source_fact_stream_functions(self._conn)

    def publish(
        self,
        publication: SourceFactPublication,
    ) -> PublicationReceipt:
        """Publish one exact graph or accept its complete immutable replay."""
        require_source_fact_stream_schema(self._conn)
        created: list[str] = []
        with self._savepoint():
            cell_ids: list[str] = []
            for item in (*publication.reported_facts, *publication.derived_facts):
                if item.cell.fact_cell_id not in cell_ids:
                    result = self._persist_semantic_cell(item.cell)
                    cell_ids.append(item.cell.fact_cell_id)
                    if result.created:
                        created.append(result.record_id)

            observation_ids: list[str] = []
            for item in (*publication.reported_facts, *publication.derived_facts):
                result = self._persist_observation(item.observation)
                observation_ids.append(item.observation.observation_id)
                if result.created:
                    created.append(result.record_id)

            relation_ids: list[str] = []
            for relation in publication.relations:
                result = self._plane.persist_relation(relation)
                relation_ids.append(relation.relation_id)
                if result.created:
                    created.append(result.record_id)

            derivation_ids: list[str] = []
            for derivation in publication.derivations:
                result = self._plane.finalize_derivation(derivation)
                derivation_ids.append(derivation.derivation_seal_id)
                if result.created:
                    created.append(result.record_id)

            extraction_ids: list[str] = []
            for extraction in publication.extraction_seals:
                result = self._plane.seal_extraction_run(extraction)
                extraction_ids.append(extraction.extraction_seal_id)
                if result.created:
                    created.append(result.record_id)

            self._require_complete_reported_extractions(publication)

            resolution_ids: list[str] = []
            for resolution in publication.resolutions:
                result = self._plane.persist_resolution(resolution)
                resolution_ids.append(resolution.resolution_revision_id)
                if result.created:
                    created.append(result.record_id)

            members = self._publication_members(publication, tuple(cell_ids))
            (
                publication_payload_sha256,
                publication_seal_id,
                ledger_created,
            ) = self._persist_publication_ledger(publication, members)
            publication_event = (
                append_verified_publication_event(
                    self._conn,
                    publication_id=publication.publication_id,
                    sequence_basis="transactional_publish",
                    assigned_at=datetime.now(UTC),
                )
                if ledger_created
                else publication_event_for_publication(
                    self._conn,
                    publication_id=publication.publication_id,
                )
            )
            return PublicationReceipt(
                publication_id=publication.publication_id,
                idempotency_key=publication.idempotency_key,
                cell_ids=tuple(cell_ids),
                observation_ids=tuple(observation_ids),
                relation_ids=tuple(relation_ids),
                derivation_seal_ids=tuple(derivation_ids),
                extraction_seal_ids=tuple(extraction_ids),
                resolution_revision_ids=tuple(resolution_ids),
                publication_payload_sha256=publication_payload_sha256,
                publication_seal_id=publication_seal_id,
                publication_sequence=(publication_event.publication_sequence),
                publication_event_sha256=publication_event.event_sha256,
                created_record_ids=tuple(created),
                exact_replay=not created and not ledger_created,
            )

    def _persist_semantic_cell(self, cell: FactCellV2) -> PersistResult:
        """Reuse the first-seen envelope for one exact semantic coordinate."""
        row = self._conn.execute(
            "SELECT cell.fact_cell_id,cell.idempotency_key,"
            "seal.semantic_key_version,seal.semantic_identity_json,"
            "seal.dimension_set_json "
            "FROM fact_cell_identity_seals_v2 AS seal "
            "JOIN fact_cells_v2 AS cell "
            "ON cell.fact_cell_id = seal.fact_cell_id "
            "WHERE seal.semantic_key_sha256 = ?",
            (cell.semantic_key_sha256,),
        ).fetchone()
        if row is None:
            return self._plane.persist_cell(cell)
        expected = (
            cell.fact_cell_id,
            cell.idempotency_key,
            cell.semantic_key_version,
            cell.semantic_identity_json,
            cell.dimensions_json,
        )
        if tuple(row) != expected:
            raise ValueError(
                "existing semantic fact cell conflicts with deterministic "
                "identity or normalized dimensions"
            )
        return PersistResult(cell.fact_cell_id, False)

    def _publication_members(
        self,
        publication: SourceFactPublication,
        cell_ids: tuple[str, ...],
    ) -> tuple[SourceFactPublicationMember, ...]:
        records: tuple[tuple[PublicationMemberKind, str], ...] = (
            *(("fact_cell", record_id) for record_id in cell_ids),
            *(
                (
                    "fact_observation",
                    item.observation.observation_id,
                )
                for item in (
                    *publication.reported_facts,
                    *publication.derived_facts,
                )
            ),
            *(("observation_relation", item.relation_id) for item in publication.relations),
            *(("derivation_seal", item.derivation_seal_id) for item in publication.derivations),
            *(
                ("extraction_seal", item.extraction_seal_id)
                for item in publication.extraction_seals
            ),
            *(
                ("resolution_revision", item.resolution_revision_id)
                for item in publication.resolutions
            ),
        )
        members: list[SourceFactPublicationMember] = []
        for ordinal, (record_kind, record_id) in enumerate(records):
            member_record_idempotency_key = record_idempotency_key(
                self._conn,
                record_kind,
                record_id,
            )
            member_record_commitment_sha256 = record_commitment(
                self._conn,
                record_kind,
                record_id,
            )
            canonical = publication_member_payload(
                member_ordinal=ordinal,
                record_kind=record_kind,
                record_id=record_id,
                record_idempotency_key=member_record_idempotency_key,
                record_commitment_sha256=member_record_commitment_sha256,
            )
            members.append(
                SourceFactPublicationMember(
                    publication_member_id=publication_member_id(
                        publication.publication_id,
                        ordinal,
                        record_kind,
                        record_id,
                    ),
                    idempotency_key=publication_member_idempotency_key(
                        publication.idempotency_key,
                        ordinal,
                        record_kind,
                        member_record_idempotency_key,
                    ),
                    publication_id=publication.publication_id,
                    member_ordinal=ordinal,
                    record_kind=record_kind,
                    record_id=record_id,
                    record_idempotency_key=member_record_idempotency_key,
                    record_commitment_version=RECORD_COMMITMENT_VERSION,
                    record_commitment_sha256=member_record_commitment_sha256,
                    canonical_member_json=canonical,
                    canonical_member_sha256=digest_text(canonical),
                    recorded_at=publication.recorded_at,
                )
            )
        return tuple(members)

    def _persist_publication_ledger(
        self,
        publication: SourceFactPublication,
        members: tuple[SourceFactPublicationMember, ...],
    ) -> tuple[str, str, bool]:
        member_set_json = canonical_json(
            [json.loads(member.canonical_member_json) for member in members]
        )
        member_set_sha256 = digest_text(member_set_json)
        counts = {
            kind: sum(member.record_kind == kind for member in members)
            for kind in (
                "fact_cell",
                "fact_observation",
                "observation_relation",
                "derivation_seal",
                "extraction_seal",
                "resolution_revision",
            )
        }
        payload_json = publication_payload(
            publication_id=publication.publication_id,
            idempotency_key=publication.idempotency_key,
            member_set_sha256=member_set_sha256,
            cell_count=counts["fact_cell"],
            observation_count=counts["fact_observation"],
            relation_count=counts["observation_relation"],
            derivation_seal_count=counts["derivation_seal"],
            extraction_seal_count=counts["extraction_seal"],
            resolution_revision_count=counts["resolution_revision"],
            member_count=len(members),
            created_at=publication.created_at,
            recorded_at=publication.recorded_at,
        )
        publication_payload_sha256 = digest_text(payload_json)
        generated_publication_seal_id = publication_seal_id(publication.publication_id)
        seal_idempotency_key = publication_seal_idempotency_key(publication.idempotency_key)
        header_columns = (
            "publication_id",
            "idempotency_key",
            "payload_version",
            "canonical_publication_payload_json",
            "publication_payload_sha256",
            "member_set_sha256",
            "cell_count",
            "observation_count",
            "relation_count",
            "derivation_seal_count",
            "extraction_seal_count",
            "resolution_revision_count",
            "member_count",
            "created_at",
            "recorded_at",
        )
        header_values: tuple[object, ...] = (
            publication.publication_id,
            publication.idempotency_key,
            PUBLICATION_PAYLOAD_VERSION,
            payload_json,
            publication_payload_sha256,
            member_set_sha256,
            counts["fact_cell"],
            counts["fact_observation"],
            counts["observation_relation"],
            counts["derivation_seal"],
            counts["extraction_seal"],
            counts["resolution_revision"],
            len(members),
            publication.created_at,
            publication.recorded_at,
        )
        existing = self._conn.execute(
            "SELECT " + ",".join(header_columns) + " "  # nosec B608 -- trusted internal SQL shape; values remain bound
            "FROM source_fact_publications "
            "WHERE publication_id = ? OR idempotency_key = ?",
            (publication.publication_id, publication.idempotency_key),
        ).fetchall()
        if existing:
            if len(existing) != 1 or not self._matches(
                tuple(existing[0]),
                header_values,
            ):
                raise ValueError(
                    "immutable source-fact publication identity conflicts with stored graph"
                )
            seal_values: tuple[object, ...] = (
                generated_publication_seal_id,
                seal_idempotency_key,
                publication.publication_id,
                len(members),
                member_set_json,
                member_set_sha256,
                publication_payload_sha256,
                publication.recorded_at,
            )
            stored_seal = self._conn.execute(
                "SELECT publication_seal_id,idempotency_key,publication_id,"
                "member_count,canonical_member_set_json,member_set_sha256,"
                "publication_payload_sha256,sealed_at "
                "FROM source_fact_publication_seals "
                "WHERE publication_id = ?",
                (publication.publication_id,),
            ).fetchone()
            if stored_seal is None or not self._matches(
                tuple(stored_seal),
                seal_values,
            ):
                raise ValueError("stored source-fact publication is not exactly sealed")
            verified = verify_source_fact_publication(
                self._conn,
                publication_id=publication.publication_id,
                cutoff=publication.recorded_at,
            )
            return (
                verified.publication_payload_sha256,
                verified.publication_seal_id,
                False,
            )

        placeholders = ",".join("?" for _ in header_columns)
        self._conn.execute(
            "INSERT INTO source_fact_publications "  # nosec B608 -- trusted internal SQL shape; values remain bound
            f"({','.join(header_columns)}) VALUES ({placeholders})",
            header_values,
        )
        member_columns = tuple(SourceFactPublicationMember.model_fields)
        member_placeholders = ",".join("?" for _ in member_columns)
        for member in members:
            values = tuple(getattr(member, column) for column in member_columns)
            self._conn.execute(
                "INSERT INTO source_fact_publication_members "  # nosec B608 -- trusted internal SQL shape; values remain bound
                f"({','.join(member_columns)}) "
                f"VALUES ({member_placeholders})",
                values,
            )
        self._conn.execute(
            "INSERT INTO source_fact_publication_seals "
            "(publication_seal_id,idempotency_key,publication_id,"
            "member_count,canonical_member_set_json,member_set_sha256,"
            "publication_payload_sha256,sealed_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                generated_publication_seal_id,
                seal_idempotency_key,
                publication.publication_id,
                len(members),
                member_set_json,
                member_set_sha256,
                publication_payload_sha256,
                publication.recorded_at,
            ),
        )
        verified = verify_source_fact_publication(
            self._conn,
            publication_id=publication.publication_id,
            cutoff=publication.recorded_at,
        )
        return (
            verified.publication_payload_sha256,
            verified.publication_seal_id,
            True,
        )

    @staticmethod
    def _matches(
        existing: tuple[object, ...],
        expected: tuple[object, ...],
    ) -> bool:
        if len(existing) != len(expected):
            return False
        for stored, supplied in zip(existing, expected, strict=True):
            if isinstance(supplied, datetime):
                try:
                    if _utc(datetime.fromisoformat(str(stored))) != _utc(supplied):
                        return False
                except ValueError:
                    return False
            elif isinstance(supplied, bool):
                if bool(stored) is not supplied:
                    return False
            elif stored != supplied:
                return False
        return True

    def _persist_observation(
        self,
        observation: ReportedFactObservationV2 | DerivedFactObservationV2,
    ) -> PersistResult:
        existing = self._conn.execute(
            "SELECT * FROM fact_observations_v2 WHERE idempotency_key = ?",
            (observation.idempotency_key,),
        )
        row = existing.fetchone()
        if row is None:
            return self._plane.persist_observation(observation)
        names = tuple(item[0] for item in existing.description)
        stored = dict(zip(names, tuple(row), strict=True))
        if isinstance(observation, ReportedFactObservationV2):
            reports = self._plane.as_reported(observation.fact_cell_id).observations
            exact = tuple(
                item for item in reports if item.idempotency_key == observation.idempotency_key
            )
            if exact != (observation,):
                raise ValueError(
                    "immutable reported observation idempotency conflicts with stored evidence"
                )
        else:
            loaded = DerivedFactObservationV2.model_validate(
                {
                    "observation_id": str(stored["observation_id"]),
                    "idempotency_key": str(stored["idempotency_key"]),
                    "fact_cell_id": str(stored["fact_cell_id"]),
                    "observation_kind": "derived",
                    "value_kind": str(stored["value_kind"]),
                    "numeric_value": stored["numeric_value"],
                    "text_value": stored["text_value"],
                    "is_nil": bool(stored["is_nil"]),
                    "raw_lexical_value": stored["raw_lexical_value"],
                    "method_name": str(stored["method_name"]),
                    "method_version": str(stored["method_version"]),
                    "method_config_sha256": str(stored["method_config_sha256"]),
                    "revision_kind": str(stored["revision_kind"]),
                    "supersedes_observation_id": stored["supersedes_observation_id"],
                    "effective_at": datetime.fromisoformat(str(stored["effective_at"])),
                    "knowledge_at": datetime.fromisoformat(str(stored["knowledge_at"])),
                    "recorded_at": datetime.fromisoformat(str(stored["recorded_at"])),
                    "formula_id": str(stored["formula_id"]),
                    "formula_version": str(stored["formula_version"]),
                }
            )
            if loaded != observation:
                raise ValueError(
                    "immutable derived observation idempotency conflicts with stored lineage"
                )
        return PersistResult(observation.observation_id, False)

    def _require_complete_reported_extractions(
        self,
        publication: SourceFactPublication,
    ) -> None:
        for item in publication.reported_facts:
            row = self._conn.execute(
                "SELECT 1 FROM fact_reported_observation_anchors_v2 AS anchor "
                "JOIN fact_extraction_run_completeness_seals_v2 AS seal "
                "ON seal.extraction_run_id = anchor.extraction_run_id "
                "WHERE anchor.observation_id = ?",
                (item.observation.observation_id,),
            ).fetchone()
            if row is None:
                raise ValueError(
                    "reported observations must have a complete extraction seal before publication"
                )

    @contextmanager
    def _savepoint(self) -> Generator[None, None, None]:
        self._conn.execute("SAVEPOINT publish_source_fact_graph")
        try:
            yield
        except Exception:
            self._conn.execute("ROLLBACK TO SAVEPOINT publish_source_fact_graph")
            self._conn.execute("RELEASE SAVEPOINT publish_source_fact_graph")
            raise
        self._conn.execute("RELEASE SAVEPOINT publish_source_fact_graph")
