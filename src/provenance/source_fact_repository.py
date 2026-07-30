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
    record_coordinates,
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
            graph_items = (*publication.reported_facts, *publication.derived_facts)
            semantic_cells = self._semantic_cells_by_sha256(
                tuple(item.cell for item in graph_items)
            )
            existing_observation_keys = self._existing_observation_keys(
                tuple(item.observation.idempotency_key for item in graph_items)
            )
            cell_ids: list[str] = []
            cell_id_set: set[str] = set()
            for item in graph_items:
                if item.cell.fact_cell_id not in cell_id_set:
                    result = self._persist_semantic_cell(
                        item.cell,
                        existing=semantic_cells.get(str(item.cell.semantic_key_sha256)),
                    )
                    cell_ids.append(item.cell.fact_cell_id)
                    cell_id_set.add(item.cell.fact_cell_id)
                    if result.created:
                        created.append(result.record_id)

            observation_ids: list[str] = []
            new_reported = tuple(
                item.observation
                for item in publication.reported_facts
                if item.observation.idempotency_key not in existing_observation_keys
            )
            if new_reported:
                self._persist_new_reported_observations(
                    new_reported,
                    {
                        item.cell.fact_cell_id: str(item.cell.semantic_key_sha256)
                        for item in publication.reported_facts
                    },
                )
                created.extend(item.observation_id for item in new_reported)
            new_reported_ids = {item.observation_id for item in new_reported}
            for item in graph_items:
                if item.observation.observation_id in new_reported_ids:
                    observation_ids.append(item.observation.observation_id)
                    continue
                result = (
                    self._persist_observation(item.observation)
                    if item.observation.idempotency_key in existing_observation_keys
                    else self._plane.persist_observation(item.observation)
                )
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

    def _semantic_cells_by_sha256(
        self,
        cells: tuple[FactCellV2, ...],
    ) -> dict[str, tuple[object, ...]]:
        semantic_keys = tuple(dict.fromkeys(str(cell.semantic_key_sha256) for cell in cells))
        rows: dict[str, tuple[object, ...]] = {}
        for start in range(0, len(semantic_keys), 400):
            batch = semantic_keys[start : start + 400]
            placeholders = ",".join("?" for _ in batch)
            cursor = self._conn.execute(
                "SELECT seal.semantic_key_sha256,cell.fact_cell_id,"
                "cell.idempotency_key,seal.semantic_key_version,"
                "seal.semantic_identity_json,seal.dimension_set_json "
                "FROM fact_cell_identity_seals_v2 AS seal "
                "JOIN fact_cells_v2 AS cell "
                "ON cell.fact_cell_id=seal.fact_cell_id "
                f"WHERE seal.semantic_key_sha256 IN ({placeholders})",  # nosec B608 -- placeholders are generated from a bounded integer batch size
                batch,
            )
            for row in cursor:
                rows[str(row[0])] = tuple(row[1:])
        return rows

    def _existing_observation_keys(
        self,
        idempotency_keys: tuple[str, ...],
    ) -> set[str]:
        unique_keys = tuple(dict.fromkeys(idempotency_keys))
        existing: set[str] = set()
        for start in range(0, len(unique_keys), 400):
            batch = unique_keys[start : start + 400]
            placeholders = ",".join("?" for _ in batch)
            existing.update(
                str(row[0])
                for row in self._conn.execute(
                    "SELECT idempotency_key FROM fact_observations_v2 "
                    f"WHERE idempotency_key IN ({placeholders})",  # nosec B608 -- placeholders are generated from a bounded integer batch size
                    batch,
                )
            )
        return existing

    def _persist_new_reported_observations(
        self,
        observations: tuple[ReportedFactObservationV2, ...],
        semantic_keys_by_cell: dict[str, str],
    ) -> None:
        """Persist a preflight-confirmed new reported-observation batch."""
        runs_by_node: dict[str, tuple[object, ...]] = {}
        node_ids = tuple(dict.fromkeys(item.evidence_node_id for item in observations))
        for start in range(0, len(node_ids), 400):
            batch = node_ids[start : start + 400]
            placeholders = ",".join("?" for _ in batch)
            for row in self._conn.execute(
                "SELECT node.node_id,run.extraction_run_id,run.extractor_name,"
                "run.extractor_code_version,run.extractor_config_sha256,"
                "run.input_sha256,run.output_sha256 "
                "FROM evidence_nodes AS node "
                "JOIN evidence_extraction_runs AS run "
                "ON run.extraction_run_id=node.extraction_run_id "
                f"WHERE node.node_id IN ({placeholders})",  # nosec B608 -- bounded placeholders
                batch,
            ):
                runs_by_node[str(row[0])] = tuple(row[1:])
        missing_nodes = tuple(
            item.evidence_node_id
            for item in observations
            if item.evidence_node_id not in runs_by_node
        )
        if missing_nodes:
            raise ValueError("reported observation extraction run is missing")

        observation_columns, _ = self._plane.observation_values(observations[0])
        observation_values: list[tuple[object, ...]] = []
        anchor_values: list[tuple[object, ...]] = []
        payload_values: list[tuple[object, ...]] = []
        for observation in observations:
            columns, values = self._plane.observation_values(observation)
            if columns != observation_columns:
                raise RuntimeError("reported observation bulk columns changed within batch")
            observation_values.append(values)
            (
                extraction_run_id,
                extractor_name,
                extractor_code_version,
                extractor_config_sha256,
                extraction_input_sha256,
                extraction_output_sha256,
            ) = runs_by_node[observation.evidence_node_id]
            anchor_json = canonical_json(
                {
                    "document_version_id": observation.document_version_id,
                    "evidence_node_id": observation.evidence_node_id,
                    "extraction_input_sha256": str(extraction_input_sha256),
                    "extraction_output_sha256": str(extraction_output_sha256),
                    "extraction_run_id": str(extraction_run_id),
                    "extractor_code_version": str(extractor_code_version),
                    "extractor_config_sha256": str(extractor_config_sha256),
                    "extractor_name": str(extractor_name),
                    "raw_entry_sha256": observation.source_entry_sha256,
                    "source_locator_sha256": observation.source_locator_sha256,
                    "source_taxonomy_version": observation.source_taxonomy_version,
                    "subject_binding_revision_id": observation.subject_binding_revision_id,
                }
            )
            anchor_sha256 = digest_text(anchor_json)
            anchor_values.append(
                (
                    observation.observation_id,
                    f"{observation.idempotency_key}:anchor:v1",
                    observation.subject_binding_revision_id,
                    str(extraction_run_id),
                    observation.source_taxonomy_version,
                    str(extractor_name),
                    str(extractor_code_version),
                    str(extractor_config_sha256),
                    str(extraction_input_sha256),
                    str(extraction_output_sha256),
                    observation.source_entry_sha256,
                    anchor_json,
                    anchor_sha256,
                    observation.recorded_at,
                )
            )
            semantic_key = semantic_keys_by_cell.get(observation.fact_cell_id)
            if semantic_key is None:
                raise ValueError("observation requires a hardened fact-cell identity")
            payload_json = canonical_json(
                {
                    "decimals": observation.decimals,
                    "effective_at": observation.effective_at.isoformat(),
                    "fact_cell_semantic_key_sha256": semantic_key,
                    "is_nil": observation.is_nil,
                    "knowledge_at": observation.knowledge_at.isoformat(),
                    "method_config_sha256": observation.method_config_sha256,
                    "method_name": observation.method_name,
                    "method_version": observation.method_version,
                    "numeric_value": observation.numeric_value,
                    "observation_kind": observation.observation_kind,
                    "payload_version": "fact_observation_payload.v1",
                    "precision": observation.precision,
                    "provenance": {
                        "anchor_payload_sha256": anchor_sha256,
                        "document_version_id": observation.document_version_id,
                        "evidence_node_id": observation.evidence_node_id,
                        "source_context_id": observation.source_context_id,
                        "source_entry_sha256": observation.source_entry_sha256,
                        "source_locator_sha256": observation.source_locator_sha256,
                        "source_unit_id": observation.source_unit_id,
                    },
                    "raw_lexical_value": observation.raw_lexical_value,
                    "recorded_at": observation.recorded_at.isoformat(),
                    "revision_kind": observation.revision_kind,
                    "supersedes_observation_id": observation.supersedes_observation_id,
                    "text_value": observation.text_value,
                    "value_kind": observation.value_kind,
                }
            )
            payload_values.append(
                (
                    observation.observation_id,
                    f"{observation.idempotency_key}:payload:v1",
                    "fact_observation_payload.v1",
                    payload_json,
                    digest_text(payload_json),
                    observation.recorded_at,
                )
            )

        placeholders = ",".join("?" for _ in observation_columns)
        self._conn.executemany(
            "INSERT INTO fact_observations_v2 "
            f"({','.join(observation_columns)}) VALUES ({placeholders})",
            observation_values,
        )
        self._conn.executemany(
            "INSERT INTO fact_reported_observation_anchors_v2 "
            "(observation_id,idempotency_key,subject_binding_revision_id,"
            "extraction_run_id,source_taxonomy_version,extractor_name,"
            "extractor_code_version,extractor_config_sha256,"
            "extraction_input_sha256,extraction_output_sha256,raw_entry_sha256,"
            "anchor_payload_json,anchor_payload_sha256,recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            anchor_values,
        )
        self._conn.executemany(
            "INSERT INTO fact_observation_payload_commitments_v2 "
            "(observation_id,idempotency_key,payload_version,canonical_payload_json,"
            "observation_payload_sha256,committed_at) VALUES (?,?,?,?,?,?)",
            payload_values,
        )

    def _persist_semantic_cell(
        self,
        cell: FactCellV2,
        *,
        existing: tuple[object, ...] | None,
    ) -> PersistResult:
        """Reuse the first-seen envelope for one exact semantic coordinate."""
        if existing is None:
            return self._plane.persist_cell(cell)
        expected = (
            cell.fact_cell_id,
            cell.idempotency_key,
            cell.semantic_key_version,
            cell.semantic_identity_json,
            cell.dimensions_json,
        )
        if existing != expected:
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
        record_kinds: tuple[PublicationMemberKind, ...] = (
            "fact_cell",
            "fact_observation",
            "observation_relation",
            "derivation_seal",
            "extraction_seal",
            "resolution_revision",
        )
        coordinate_maps = {
            record_kind: record_coordinates(
                self._conn,
                record_kind,
                tuple(record_id for kind, record_id in records if kind == record_kind),
            )
            for record_kind in record_kinds
        }
        members: list[SourceFactPublicationMember] = []
        for ordinal, (record_kind, record_id) in enumerate(records):
            (
                member_record_idempotency_key,
                member_record_commitment_sha256,
            ) = coordinate_maps[record_kind][record_id]
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
        self._conn.executemany(
            "INSERT INTO source_fact_publication_members "  # nosec B608 -- trusted internal SQL shape; values remain bound
            f"({','.join(member_columns)}) "
            f"VALUES ({member_placeholders})",
            (tuple(getattr(member, column) for column in member_columns) for member in members),
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
        observation_ids = tuple(
            item.observation.observation_id for item in publication.reported_facts
        )
        complete: set[str] = set()
        for start in range(0, len(observation_ids), 400):
            batch = observation_ids[start : start + 400]
            placeholders = ",".join("?" for _ in batch)
            complete.update(
                str(row[0])
                for row in self._conn.execute(
                    "SELECT anchor.observation_id "
                    "FROM fact_reported_observation_anchors_v2 AS anchor "
                    "JOIN fact_extraction_run_completeness_seals_v2 AS seal "
                    "ON seal.extraction_run_id=anchor.extraction_run_id "
                    f"WHERE anchor.observation_id IN ({placeholders})",  # nosec B608 -- placeholders are generated from a bounded integer batch size
                    batch,
                )
            )
        missing = tuple(
            observation_id for observation_id in observation_ids if observation_id not in complete
        )
        if missing:
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
