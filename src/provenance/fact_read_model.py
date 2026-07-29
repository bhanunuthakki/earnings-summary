"""Typed, v2-only read model for investor-grade fact consumption."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from provenance.fact_plane_v2 import (
    CanonicalJSONObject,
    DerivedFactObservationV2,
    FactAsKnownV2,
    FactCellV2,
    FactDimensionV2,
    FactObservationV2,
    FactPlaneV2,
    ReportedFactObservationV2,
)
from provenance.source_fact_publication import (
    PublicationMemberKind,
    PublicationVerificationError,
    verify_source_fact_publication,
)

AdmissionDisposition = Literal["missing_provenance", "quarantined"]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class FactAdmissionError(RuntimeError):
    """A fact graph cannot cross the sealed-publication read boundary."""

    def __init__(
        self,
        reason_code: str,
        *,
        record_kind: PublicationMemberKind,
        record_id: str,
        disposition: AdmissionDisposition,
    ) -> None:
        self.reason_code: str = reason_code
        self.record_kind: PublicationMemberKind = record_kind
        self.record_id: str = record_id
        self.disposition: AdmissionDisposition = disposition
        super().__init__(f"{reason_code}: {record_kind} {record_id!r} is not admissible")


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FactSelector(_FrozenModel):
    reporting_entity_id: str = Field(min_length=1, max_length=128)
    concept_namespace: str | None = Field(default=None, min_length=1)
    concept_name: str | None = Field(default=None, min_length=1)
    scope_security_id: str | None = Field(default=None, min_length=1)
    unit_key: str | None = Field(default=None, min_length=1)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    dimension_set_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
    )
    period_start_at_or_after: datetime | None = None
    period_end_at_or_before: datetime | None = None


class ExactEvidenceReference(_FrozenModel):
    document_version_id: str
    evidence_node_id: str
    subject_binding_revision_id: str
    extraction_run_id: str
    extraction_seal_id: str | None
    source_taxonomy_version: str
    source_locator: CanonicalJSONObject
    source_locator_sha256: str
    source_entry_sha256: str
    anchor_payload_sha256: str
    input_sha256: str
    output_sha256: str
    extractor_name: str
    extractor_code_version: str
    extractor_config_sha256: str
    recorded_issuer_id: str
    canonical_issuer_id: str
    reporting_entity_id: str
    node_recorded_at: datetime
    document_recorded_at: datetime
    extraction_completed_at: datetime


class ExactDerivationReference(_FrozenModel):
    derivation_seal_id: str
    input_basis: Literal["as_reported", "as_known"]
    input_observation_ids: tuple[str, ...]
    input_resolution_revision_ids: tuple[str | None, ...]
    canonical_input_digest_sha256: str
    derivation_basis_sha256: str
    formula_id: str
    formula_version: str
    formula_definition_sha256: str
    execution_config_sha256: str
    knowledge_cutoff: datetime
    recorded_at: datetime


class FactValueRecord(_FrozenModel):
    fact_cell_id: str
    observation_id: str
    observation_kind: Literal["reported", "derived"]
    value_kind: Literal["numeric", "text", "nil"]
    decimal_value: Decimal | None
    text_value: str | None
    is_nil: bool
    raw_lexical_value: str | None
    unit_key: str
    currency: str | None
    period_kind: Literal["instant", "duration"]
    period_start: datetime | None
    period_end: datetime
    dimensions: tuple[FactDimensionV2, ...]
    revision_kind: str
    method_name: str
    method_version: str
    method_config_sha256: str
    observation_payload_sha256: str
    effective_at: datetime
    knowledge_at: datetime
    recorded_at: datetime
    evidence: ExactEvidenceReference | None
    derivation: ExactDerivationReference | None


class ResolutionCandidateRecord(_FrozenModel):
    candidate_id: str
    candidate_ordinal: int
    eligibility: Literal["eligible", "ineligible"]
    reason_code: str
    reason_details: CanonicalJSONObject
    candidate_payload_sha256: str
    selected: bool
    observation: FactValueRecord


class ResolutionDecisionRecord(_FrozenModel):
    resolution_revision_id: str
    revision: int
    status: Literal["resolved", "unresolved", "retired"]
    selected_observation_id: str | None
    candidate_set_id: str
    candidate_set_digest_sha256: str
    policy_name: str
    policy_version: str
    policy_config_sha256: str
    reason_code: str
    reason_details: CanonicalJSONObject
    effective_at: datetime
    knowledge_at: datetime
    recorded_at: datetime
    candidates: tuple[ResolutionCandidateRecord, ...]
    dissent: tuple[ResolutionCandidateRecord, ...]


class FactSnapshot(_FrozenModel):
    cell: FactCellV2
    cutoff: datetime
    resolution: ResolutionDecisionRecord | None
    canonical_value: FactValueRecord | None


class FactCoverage(_FrozenModel):
    reporting_entity_id: str
    cutoff: datetime
    total_cells: int
    resolved_cells: int
    unresolved_cells: int
    retired_cells: int
    cells_without_resolution: int
    reported_observations: int
    derived_observations: int
    complete_reported_observations: int


class FactRelationRecord(_FrozenModel):
    relation_id: str
    subject_observation_id: str
    object_observation_id: str
    relation_kind: str
    reason_code: str
    reason_details: CanonicalJSONObject
    policy_name: str
    policy_version: str
    policy_config_sha256: str
    effective_at: datetime
    knowledge_at: datetime
    recorded_at: datetime


class ProvenanceBundle(_FrozenModel):
    cell: FactCellV2
    observation: FactValueRecord
    payload_version: str
    canonical_payload: CanonicalJSONObject
    observation_payload_sha256: str
    evidence: ExactEvidenceReference | None
    derivation: ExactDerivationReference | None
    relations: tuple[FactRelationRecord, ...]


class FactReadModel:
    """Only typed read boundary for hardened facts; never consults legacy rows."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._plane = FactPlaneV2(conn)

    def cell(self, fact_cell_id: str, *, cutoff: datetime) -> FactCellV2:
        row = self._fetchone(
            "SELECT knowledge_at,recorded_at FROM fact_cells_v2 WHERE fact_cell_id = ?",
            (fact_cell_id,),
        )
        if row is None:
            raise ValueError(f"fact cell {fact_cell_id!r} does not exist")
        if any(
            _utc(self._datetime(row[column])) > _utc(cutoff)
            for column in ("knowledge_at", "recorded_at")
        ):
            raise FactAdmissionError(
                "fact_cell_after_cutoff",
                record_kind="fact_cell",
                record_id=fact_cell_id,
                disposition="missing_provenance",
            )
        self._admit_record("fact_cell", fact_cell_id, cutoff=cutoff)
        return self._plane.as_reported(fact_cell_id).cell

    def as_reported(
        self,
        fact_cell_id: str,
        *,
        cutoff: datetime,
    ) -> tuple[FactValueRecord, ...]:
        cell = self.cell(fact_cell_id, cutoff=cutoff)
        rows = self._fetchall(
            "SELECT * FROM fact_observations_v2 "
            "WHERE fact_cell_id = ? AND observation_kind = 'reported' "
            "AND knowledge_at <= ? AND recorded_at <= ? "
            "ORDER BY knowledge_at,recorded_at,observation_id",
            (fact_cell_id, cutoff, cutoff),
        )
        observations = tuple(self._load_observation(row) for row in rows)
        for observation in observations:
            self._admit_observation_graph(observation, cutoff=cutoff)
        return tuple(self._value_record(observation, cell) for observation in observations)

    def as_known(self, fact_cell_id: str, cutoff: datetime) -> FactSnapshot:
        known = self._plane.as_known(fact_cell_id, cutoff)
        self._admit_record("fact_cell", fact_cell_id, cutoff=cutoff)
        if known.resolution is not None:
            self._admit_record(
                "resolution_revision",
                known.resolution.resolution_revision_id,
                cutoff=cutoff,
            )
            for observation in known.candidates:
                self._admit_observation_graph(observation, cutoff=cutoff)
        return self._snapshot(known)

    def current_resolved(
        self,
        fact_cell_id: str,
        *,
        cutoff: datetime,
    ) -> FactSnapshot | None:
        snapshot = self.as_known(fact_cell_id, cutoff)
        if snapshot.resolution is None or snapshot.resolution.status != "resolved":
            return None
        return snapshot

    def series(
        self,
        selector: FactSelector,
        *,
        cutoff: datetime,
    ) -> tuple[FactSnapshot, ...]:
        cell_ids = self._select_cell_ids(selector, cutoff=cutoff)
        return tuple(self.as_known(cell_id, cutoff) for cell_id in cell_ids)

    def latest(
        self,
        selector: FactSelector,
        *,
        cutoff: datetime,
    ) -> FactSnapshot | None:
        resolved = tuple(
            item
            for item in self.series(selector, cutoff=cutoff)
            if item.canonical_value is not None
        )
        if not resolved:
            return None
        latest_period = max(item.cell.period_end for item in resolved)
        latest = tuple(item for item in resolved if item.cell.period_end == latest_period)
        if len(latest) != 1:
            raise ValueError(
                "latest is ambiguous across multiple semantic coordinates; narrow the selector"
            )
        return latest[0]

    def catalog(
        self,
        reporting_entity_id: str,
        *,
        cutoff: datetime,
    ) -> tuple[FactCellV2, ...]:
        selector = FactSelector(reporting_entity_id=reporting_entity_id)
        return tuple(
            self.cell(cell_id, cutoff=cutoff)
            for cell_id in self._select_cell_ids(selector, cutoff=cutoff)
        )

    def coverage(
        self,
        reporting_entity_id: str,
        *,
        cutoff: datetime,
    ) -> FactCoverage:
        snapshots = self.series(
            FactSelector(reporting_entity_id=reporting_entity_id),
            cutoff=cutoff,
        )
        statuses = tuple(
            None if item.resolution is None else item.resolution.status for item in snapshots
        )
        observations = tuple(
            observation
            for snapshot in snapshots
            for observation in self.raw_observations(
                snapshot.cell.fact_cell_id,
                cutoff=cutoff,
            )
        )
        return FactCoverage(
            reporting_entity_id=reporting_entity_id,
            cutoff=cutoff,
            total_cells=len(snapshots),
            resolved_cells=statuses.count("resolved"),
            unresolved_cells=statuses.count("unresolved"),
            retired_cells=statuses.count("retired"),
            cells_without_resolution=statuses.count(None),
            reported_observations=sum(item.observation_kind == "reported" for item in observations),
            derived_observations=sum(item.observation_kind == "derived" for item in observations),
            complete_reported_observations=sum(
                item.observation_kind == "reported"
                and item.evidence is not None
                and item.evidence.extraction_seal_id is not None
                for item in observations
            ),
        )

    def raw_observations(
        self,
        fact_cell_id: str,
        *,
        cutoff: datetime,
    ) -> tuple[FactValueRecord, ...]:
        cell = self.cell(fact_cell_id, cutoff=cutoff)
        rows = self._fetchall(
            "SELECT * FROM fact_observations_v2 WHERE fact_cell_id = ? "
            "AND knowledge_at <= ? AND recorded_at <= ? "
            "ORDER BY knowledge_at,recorded_at,observation_id",
            (fact_cell_id, cutoff, cutoff),
        )
        observations = tuple(self._load_observation(row) for row in rows)
        for observation in observations:
            self._admit_observation_graph(observation, cutoff=cutoff)
        return tuple(self._value_record(item, cell) for item in observations)

    def relations(
        self,
        observation_id: str,
        *,
        cutoff: datetime,
    ) -> tuple[FactRelationRecord, ...]:
        self._admit_record(
            "fact_observation",
            observation_id,
            cutoff=cutoff,
        )
        rows = self._bounded_relation_rows(observation_id, cutoff=cutoff)
        for row in rows:
            self._admit_record(
                "observation_relation",
                str(row["relation_id"]),
                cutoff=cutoff,
            )
        return tuple(self._relation_record(row) for row in rows)

    def _bounded_relation_rows(
        self,
        observation_id: str,
        *,
        cutoff: datetime,
    ) -> tuple[dict[str, object], ...]:
        return self._fetchall(
            "SELECT * FROM fact_observation_relations_v2 "
            "WHERE (subject_observation_id = ? OR object_observation_id = ?) "
            "AND knowledge_at <= ? AND recorded_at <= ? "
            "ORDER BY knowledge_at,recorded_at,relation_id",
            (observation_id, observation_id, cutoff, cutoff),
        )

    def provenance_bundle(
        self,
        observation_id: str,
        *,
        cutoff: datetime,
    ) -> ProvenanceBundle:
        row = self._fetchone(
            "SELECT observation.*, payload.payload_version, "
            "payload.canonical_payload_json, "
            "payload.observation_payload_sha256 "
            "FROM fact_observations_v2 AS observation "
            "JOIN fact_observation_payload_commitments_v2 AS payload "
            "ON payload.observation_id = observation.observation_id "
            "WHERE observation.observation_id = ? "
            "AND observation.knowledge_at <= ? "
            "AND observation.recorded_at <= ?",
            (observation_id, cutoff, cutoff),
        )
        if row is None:
            raise ValueError(f"committed observation {observation_id!r} does not exist")
        loaded = self._load_observation(row)
        self._admit_observation_graph(loaded, cutoff=cutoff)
        cell = self.cell(str(row["fact_cell_id"]), cutoff=cutoff)
        observation = self._value_record(loaded, cell)
        return ProvenanceBundle(
            cell=cell,
            observation=observation,
            payload_version=str(row["payload_version"]),
            canonical_payload=CanonicalJSONObject.model_validate_json(
                str(row["canonical_payload_json"])
            ),
            observation_payload_sha256=str(row["observation_payload_sha256"]),
            evidence=observation.evidence,
            derivation=observation.derivation,
            relations=self.relations(observation_id, cutoff=cutoff),
        )

    def _admit_observation_graph(
        self,
        observation: FactObservationV2,
        *,
        cutoff: datetime,
    ) -> None:
        observation_id = observation.observation_id
        self._admit_record(
            "fact_observation",
            observation_id,
            cutoff=cutoff,
        )
        if isinstance(observation, ReportedFactObservationV2):
            extraction = self._fetchone(
                "SELECT extraction.extraction_seal_id "
                "FROM fact_reported_observation_anchors_v2 AS anchor "
                "JOIN fact_extraction_run_completeness_seals_v2 AS extraction "
                "ON extraction.extraction_run_id = anchor.extraction_run_id "
                "WHERE anchor.observation_id = ? "
                "AND extraction.knowledge_at <= ? "
                "AND extraction.recorded_at <= ?",
                (observation_id, cutoff, cutoff),
            )
            if extraction is None:
                raise FactAdmissionError(
                    "reported_extraction_seal_unpublished",
                    record_kind="fact_observation",
                    record_id=observation_id,
                    disposition="missing_provenance",
                )
            self._admit_record(
                "extraction_seal",
                str(extraction["extraction_seal_id"]),
                cutoff=cutoff,
            )
        else:
            derivation = self._fetchone(
                "SELECT derivation_seal_id "
                "FROM fact_derivation_seals_v2 "
                "WHERE output_observation_id = ? "
                "AND knowledge_at <= ? AND recorded_at <= ?",
                (observation_id, cutoff, cutoff),
            )
            if derivation is None:
                raise FactAdmissionError(
                    "derived_derivation_seal_unpublished",
                    record_kind="fact_observation",
                    record_id=observation_id,
                    disposition="missing_provenance",
                )
            self._admit_record(
                "derivation_seal",
                str(derivation["derivation_seal_id"]),
                cutoff=cutoff,
            )
        for relation in self._bounded_relation_rows(
            observation_id,
            cutoff=cutoff,
        ):
            self._admit_record(
                "observation_relation",
                str(relation["relation_id"]),
                cutoff=cutoff,
            )

    def _admit_record(
        self,
        record_kind: PublicationMemberKind,
        record_id: str,
        *,
        cutoff: datetime,
    ) -> None:
        try:
            rows = self._fetchall(
                "SELECT DISTINCT publication.publication_id "
                "FROM source_fact_publication_members AS member "
                "JOIN source_fact_publications AS publication "
                "ON publication.publication_id = member.publication_id "
                "JOIN source_fact_publication_seals AS seal "
                "ON seal.publication_id = publication.publication_id "
                "WHERE member.record_kind = ? AND member.record_id = ? "
                "AND member.recorded_at <= ? "
                "AND publication.recorded_at <= ? AND seal.sealed_at <= ? "
                "ORDER BY publication.recorded_at,publication.publication_id",
                (record_kind, record_id, cutoff, cutoff, cutoff),
            )
        except sqlite3.OperationalError as exc:
            raise FactAdmissionError(
                "publication_ledger_unavailable",
                record_kind=record_kind,
                record_id=record_id,
                disposition="missing_provenance",
            ) from exc
        if not rows:
            raise FactAdmissionError(
                "record_not_in_sealed_publication",
                record_kind=record_kind,
                record_id=record_id,
                disposition="missing_provenance",
            )
        for row in rows:
            self._verify_publication(
                str(row["publication_id"]),
                cutoff=cutoff,
                requested_kind=record_kind,
                requested_id=record_id,
            )

    def _verify_publication(
        self,
        publication_id: str,
        *,
        cutoff: datetime,
        requested_kind: PublicationMemberKind,
        requested_id: str,
    ) -> None:
        try:
            verify_source_fact_publication(
                self._conn,
                publication_id=publication_id,
                cutoff=cutoff,
            )
        except PublicationVerificationError as exc:
            raise FactAdmissionError(
                exc.reason_code,
                record_kind=requested_kind,
                record_id=requested_id,
                disposition=exc.disposition,
            ) from exc

    def _snapshot(self, known: FactAsKnownV2) -> FactSnapshot:
        if known.resolution is None:
            return FactSnapshot(
                cell=known.cell,
                cutoff=known.cutoff,
                resolution=None,
                canonical_value=None,
            )
        values = {
            item.observation_id: self._value_record(item, known.cell) for item in known.candidates
        }
        candidate_records = tuple(
            ResolutionCandidateRecord(
                candidate_id=candidate.candidate_id,
                candidate_ordinal=candidate.candidate_ordinal,
                eligibility=candidate.eligibility,
                reason_code=candidate.reason_code,
                reason_details=candidate.reason_details,
                candidate_payload_sha256=str(candidate.candidate_payload_sha256),
                selected=(candidate.observation_id == known.resolution.selected_observation_id),
                observation=values[candidate.observation_id],
            )
            for candidate in known.resolution.candidates
        )
        dissent = tuple(item for item in candidate_records if not item.selected)
        decision = ResolutionDecisionRecord(
            resolution_revision_id=(known.resolution.resolution_revision_id),
            revision=known.resolution.revision,
            status=known.resolution.status,
            selected_observation_id=(known.resolution.selected_observation_id),
            candidate_set_id=known.resolution.candidate_set_id,
            candidate_set_digest_sha256=str(known.resolution.candidate_set_digest_sha256),
            policy_name=known.resolution.policy_name,
            policy_version=known.resolution.policy_version,
            policy_config_sha256=known.resolution.policy_config_sha256,
            reason_code=known.resolution.reason_code,
            reason_details=known.resolution.reason_details,
            effective_at=known.resolution.effective_at,
            knowledge_at=known.resolution.knowledge_cutoff,
            recorded_at=known.resolution.recorded_at,
            candidates=candidate_records,
            dissent=dissent,
        )
        canonical = (
            None
            if known.resolution.selected_observation_id is None
            else values[known.resolution.selected_observation_id]
        )
        return FactSnapshot(
            cell=known.cell,
            cutoff=known.cutoff,
            resolution=decision,
            canonical_value=canonical,
        )

    def _value_record(
        self,
        observation: FactObservationV2,
        cell: FactCellV2,
    ) -> FactValueRecord:
        payload = self._fetchone(
            "SELECT observation_payload_sha256 "
            "FROM fact_observation_payload_commitments_v2 "
            "WHERE observation_id = ?",
            (observation.observation_id,),
        )
        if payload is None:
            raise ValueError("raw observation lacks a hardened payload commitment")
        evidence = (
            self._evidence_reference(observation.observation_id)
            if isinstance(observation, ReportedFactObservationV2)
            else None
        )
        derivation = (
            self._derivation_reference(observation.observation_id)
            if isinstance(observation, DerivedFactObservationV2)
            else None
        )
        return FactValueRecord(
            fact_cell_id=cell.fact_cell_id,
            observation_id=observation.observation_id,
            observation_kind=observation.observation_kind,
            value_kind=observation.value_kind,
            decimal_value=(
                None if observation.numeric_value is None else Decimal(observation.numeric_value)
            ),
            text_value=observation.text_value,
            is_nil=observation.is_nil,
            raw_lexical_value=observation.raw_lexical_value,
            unit_key=cell.unit_key,
            currency=cell.currency,
            period_kind=cell.period_kind,
            period_start=cell.period_start,
            period_end=cell.period_end,
            dimensions=tuple(cell.dimensions),
            revision_kind=observation.revision_kind,
            method_name=observation.method_name,
            method_version=observation.method_version,
            method_config_sha256=observation.method_config_sha256,
            observation_payload_sha256=str(payload["observation_payload_sha256"]),
            effective_at=observation.effective_at,
            knowledge_at=observation.knowledge_at,
            recorded_at=observation.recorded_at,
            evidence=evidence,
            derivation=derivation,
        )

    def _evidence_reference(
        self,
        observation_id: str,
    ) -> ExactEvidenceReference:
        row = self._fetchone(
            "SELECT observation.document_version_id,"
            "observation.evidence_node_id,observation.source_locator_json,"
            "observation.source_locator_sha256,observation.source_entry_sha256,"
            "anchor.subject_binding_revision_id,anchor.extraction_run_id,"
            "anchor.source_taxonomy_version,anchor.anchor_payload_sha256,"
            "run.input_sha256,run.output_sha256,run.extractor_name,"
            "run.extractor_code_version,run.extractor_config_sha256,"
            "run.completed_at,node.recorded_at AS node_recorded_at,"
            "document.recorded_at AS document_recorded_at,"
            "binding.recorded_issuer_id,binding.issuer_id,"
            "binding.reporting_entity_id,extraction.extraction_seal_id "
            "FROM fact_observations_v2 AS observation "
            "JOIN fact_reported_observation_anchors_v2 AS anchor "
            "ON anchor.observation_id = observation.observation_id "
            "JOIN evidence_extraction_runs AS run "
            "ON run.extraction_run_id = anchor.extraction_run_id "
            "JOIN evidence_nodes AS node "
            "ON node.node_id = observation.evidence_node_id "
            "JOIN evidence_document_versions AS document "
            "ON document.document_version_id = observation.document_version_id "
            "JOIN recorded_subject_binding_revisions AS binding "
            "ON binding.binding_revision_id = anchor.subject_binding_revision_id "
            "LEFT JOIN fact_extraction_run_completeness_seals_v2 AS extraction "
            "ON extraction.extraction_run_id = anchor.extraction_run_id "
            "WHERE observation.observation_id = ?",
            (observation_id,),
        )
        if row is None:
            raise ValueError("reported observation lacks exact evidence provenance")
        return ExactEvidenceReference(
            document_version_id=str(row["document_version_id"]),
            evidence_node_id=str(row["evidence_node_id"]),
            subject_binding_revision_id=str(row["subject_binding_revision_id"]),
            extraction_run_id=str(row["extraction_run_id"]),
            extraction_seal_id=(
                None if row["extraction_seal_id"] is None else str(row["extraction_seal_id"])
            ),
            source_taxonomy_version=str(row["source_taxonomy_version"]),
            source_locator=CanonicalJSONObject.model_validate_json(str(row["source_locator_json"])),
            source_locator_sha256=str(row["source_locator_sha256"]),
            source_entry_sha256=str(row["source_entry_sha256"]),
            anchor_payload_sha256=str(row["anchor_payload_sha256"]),
            input_sha256=str(row["input_sha256"]),
            output_sha256=str(row["output_sha256"]),
            extractor_name=str(row["extractor_name"]),
            extractor_code_version=str(row["extractor_code_version"]),
            extractor_config_sha256=str(row["extractor_config_sha256"]),
            recorded_issuer_id=str(row["recorded_issuer_id"]),
            canonical_issuer_id=str(row["issuer_id"]),
            reporting_entity_id=str(row["reporting_entity_id"]),
            node_recorded_at=self._datetime(row["node_recorded_at"]),
            document_recorded_at=self._datetime(row["document_recorded_at"]),
            extraction_completed_at=self._datetime(row["completed_at"]),
        )

    def _derivation_reference(
        self,
        observation_id: str,
    ) -> ExactDerivationReference:
        row = self._fetchone(
            "SELECT seal.derivation_seal_id,"
            "seal.canonical_input_digest_sha256,basis.* "
            "FROM fact_derivation_seals_v2 AS seal "
            "JOIN fact_derivation_basis_commitments_v2 AS basis "
            "ON basis.derivation_seal_id = seal.derivation_seal_id "
            "WHERE seal.output_observation_id = ?",
            (observation_id,),
        )
        if row is None:
            raise ValueError("derived observation lacks a sealed derivation basis")
        input_basis = str(row["input_basis"])
        if input_basis not in {"as_reported", "as_known"}:
            raise ValueError("derived observation has an invalid input basis")
        edges = self._fetchall(
            "SELECT input_observation_id,input_resolution_revision_id "
            "FROM fact_derivation_input_edges_v2 "
            "WHERE output_observation_id = ? ORDER BY input_ordinal",
            (observation_id,),
        )
        return ExactDerivationReference(
            derivation_seal_id=str(row["derivation_seal_id"]),
            input_basis=cast(
                Literal["as_reported", "as_known"],
                input_basis,
            ),
            input_observation_ids=tuple(str(item["input_observation_id"]) for item in edges),
            input_resolution_revision_ids=tuple(
                (
                    None
                    if item["input_resolution_revision_id"] is None
                    else str(item["input_resolution_revision_id"])
                )
                for item in edges
            ),
            canonical_input_digest_sha256=str(row["canonical_input_digest_sha256"]),
            derivation_basis_sha256=str(row["canonical_basis_sha256"]),
            formula_id=str(row["formula_id"]),
            formula_version=str(row["formula_version"]),
            formula_definition_sha256=str(row["formula_definition_sha256"]),
            execution_config_sha256=str(row["execution_config_sha256"]),
            knowledge_cutoff=self._datetime(row["knowledge_cutoff"]),
            recorded_at=self._datetime(row["recorded_at"]),
        )

    def _load_observation(
        self,
        row: dict[str, object],
    ) -> FactObservationV2:
        common: dict[str, object] = {
            "observation_id": str(row["observation_id"]),
            "idempotency_key": str(row["idempotency_key"]),
            "fact_cell_id": str(row["fact_cell_id"]),
            "observation_kind": str(row["observation_kind"]),
            "value_kind": str(row["value_kind"]),
            "numeric_value": row["numeric_value"],
            "text_value": row["text_value"],
            "is_nil": bool(row["is_nil"]),
            "raw_lexical_value": row["raw_lexical_value"],
            "method_name": str(row["method_name"]),
            "method_version": str(row["method_version"]),
            "method_config_sha256": str(row["method_config_sha256"]),
            "revision_kind": str(row["revision_kind"]),
            "supersedes_observation_id": row["supersedes_observation_id"],
            "effective_at": self._datetime(row["effective_at"]),
            "knowledge_at": self._datetime(row["knowledge_at"]),
            "recorded_at": self._datetime(row["recorded_at"]),
        }
        if row["observation_kind"] == "reported":
            anchor = self._fetchone(
                "SELECT subject_binding_revision_id,source_taxonomy_version "
                "FROM fact_reported_observation_anchors_v2 "
                "WHERE observation_id = ?",
                (str(row["observation_id"]),),
            )
            if anchor is None:
                raise ValueError("reported observation has no exact anchor")
            return ReportedFactObservationV2.model_validate(
                {
                    **common,
                    "document_version_id": str(row["document_version_id"]),
                    "evidence_node_id": str(row["evidence_node_id"]),
                    "source_locator": (
                        CanonicalJSONObject.model_validate_json(str(row["source_locator_json"]))
                    ),
                    "source_locator_sha256": str(row["source_locator_sha256"]),
                    "source_entry_sha256": str(row["source_entry_sha256"]),
                    "subject_binding_revision_id": str(anchor["subject_binding_revision_id"]),
                    "source_taxonomy_version": str(anchor["source_taxonomy_version"]),
                    "source_context_id": row["source_context_id"],
                    "source_unit_id": row["source_unit_id"],
                    "decimals": row["decimals"],
                    "precision": row["precision"],
                    "legacy_match_revision_id": row["legacy_match_revision_id"],
                }
            )
        return DerivedFactObservationV2.model_validate(
            {
                **common,
                "formula_id": str(row["formula_id"]),
                "formula_version": str(row["formula_version"]),
            }
        )

    def _select_cell_ids(
        self,
        selector: FactSelector,
        *,
        cutoff: datetime,
    ) -> tuple[str, ...]:
        conditions = [
            "cell.reporting_entity_id = ?",
            "cell.knowledge_at <= ?",
            "cell.recorded_at <= ?",
        ]
        parameters: list[object] = [
            selector.reporting_entity_id,
            cutoff,
            cutoff,
        ]
        for column, value in (
            ("cell.concept_namespace", selector.concept_namespace),
            ("cell.concept_name", selector.concept_name),
            ("cell.scope_security_id", selector.scope_security_id),
            ("cell.unit_key", selector.unit_key),
            ("cell.currency", selector.currency),
            (
                "seal.dimension_set_sha256",
                selector.dimension_set_sha256,
            ),
        ):
            if value is not None:
                conditions.append(f"{column} = ?")
                parameters.append(value)
        if selector.period_start_at_or_after is not None:
            conditions.append("(cell.period_start IS NULL OR cell.period_start >= ?)")
            parameters.append(selector.period_start_at_or_after)
        if selector.period_end_at_or_before is not None:
            conditions.append("cell.period_end <= ?")
            parameters.append(selector.period_end_at_or_before)
        rows = self._conn.execute(
            "SELECT cell.fact_cell_id FROM fact_cells_v2 AS cell "  # nosec B608 -- trusted internal SQL shape; values remain bound
            "JOIN fact_cell_identity_seals_v2 AS seal "
            "ON seal.fact_cell_id = cell.fact_cell_id WHERE "
            + " AND ".join(conditions)
            + " ORDER BY cell.period_end,cell.semantic_key_sha256",
            tuple(parameters),
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def _relation_record(
        self,
        row: dict[str, object],
    ) -> FactRelationRecord:
        return FactRelationRecord(
            relation_id=str(row["relation_id"]),
            subject_observation_id=str(row["subject_observation_id"]),
            object_observation_id=str(row["object_observation_id"]),
            relation_kind=str(row["relation_kind"]),
            reason_code=str(row["reason_code"]),
            reason_details=CanonicalJSONObject.model_validate_json(str(row["reason_details_json"])),
            policy_name=str(row["policy_name"]),
            policy_version=str(row["policy_version"]),
            policy_config_sha256=str(row["policy_config_sha256"]),
            effective_at=self._datetime(row["effective_at"]),
            knowledge_at=self._datetime(row["knowledge_at"]),
            recorded_at=self._datetime(row["recorded_at"]),
        )

    def _fetchone(
        self,
        sql: str,
        parameters: tuple[object, ...],
    ) -> dict[str, object] | None:
        cursor = self._conn.execute(sql, parameters)
        row = cursor.fetchone()
        if row is None:
            return None
        return dict(
            zip(
                (item[0] for item in cursor.description),
                tuple(row),
                strict=True,
            )
        )

    def _fetchall(
        self,
        sql: str,
        parameters: tuple[object, ...],
    ) -> tuple[dict[str, object], ...]:
        cursor = self._conn.execute(sql, parameters)
        names = tuple(item[0] for item in cursor.description)
        return tuple(dict(zip(names, tuple(row), strict=True)) for row in cursor.fetchall())

    @staticmethod
    def _datetime(value: object) -> datetime:
        return datetime.fromisoformat(str(value))
