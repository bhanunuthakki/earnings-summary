"""Cutoff-aware, provenance-complete structured fact search.

Numeric facts are a separate retrieval lane from narrative document chunks.
This module deliberately does not create embeddings for fact values and does
not allow document hits to satisfy the typed :class:`FactHit` contract.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from provenance.fact_plane_v2 import FactCellV2
from provenance.fact_read_model import (
    ExactDerivationReference,
    ExactEvidenceReference,
    FactAdmissionError,
    FactReadModel,
    FactValueRecord,
)

_SHA256_LENGTH = 64

MembershipStatus = Literal[
    "included",
    "unresolved_material",
    "missing_provenance",
    "quarantined",
]
FactValueKind = Literal["numeric", "text", "nil"]
FactObservationKind = Literal["reported", "derived"]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _canonical_json(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_sha256(value: str) -> str:
    normalized = value.lower()
    if len(normalized) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("must be a lowercase SHA-256 hex digest")
    return normalized


def _optional_sha256(value: str | None) -> str | None:
    return None if value is None else _validate_sha256(value)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _as_int(value: object) -> int:
    return int(str(value))


def _canonical_decimal(value: Decimal | str | int) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("numeric values must be finite decimals") from exc
    if not parsed.is_finite():
        raise ValueError("numeric values must be finite decimals")
    return parsed


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = format(value, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return "0" if normalized in {"", "-0"} else normalized


_JSON_OBJECT_ADAPTER: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])
_JSON_ARRAY_ADAPTER: TypeAdapter[list[JsonValue]] = TypeAdapter(list[JsonValue])


def _json_object(value: str | None) -> dict[str, JsonValue] | None:
    if value is None:
        return None
    return _JSON_OBJECT_ADAPTER.validate_json(value)


def _json_array(value: str) -> list[JsonValue]:
    return _JSON_ARRAY_ADAPTER.validate_json(value)


class FactDimension(_FrozenModel):
    axis_namespace: str = Field(min_length=1)
    axis_name: str = Field(min_length=1)
    member_kind: Literal["explicit", "typed"]
    explicit_member_namespace: str | None = None
    explicit_member_name: str | None = None
    typed_member_value: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def _member_shape(self) -> Self:
        if self.member_kind == "explicit":
            if (
                self.explicit_member_namespace is None
                or self.explicit_member_name is None
                or self.typed_member_value is not None
            ):
                raise ValueError("explicit dimensions require only an exact member")
        elif (
            self.explicit_member_namespace is not None
            or self.explicit_member_name is not None
            or self.typed_member_value is None
        ):
            raise ValueError("typed dimensions require only typed-member content")
        return self

    @property
    def canonical_member(self) -> dict[str, JsonValue]:
        return {
            "axis_name": self.axis_name,
            "axis_namespace": self.axis_namespace,
            "explicit_member_name": self.explicit_member_name,
            "explicit_member_namespace": self.explicit_member_namespace,
            "member_kind": self.member_kind,
            "typed_member_value": self.typed_member_value,
        }


class ReportedFactEvidence(_FrozenModel):
    provenance_kind: Literal["reported"]
    document_version_id: str = Field(min_length=1, max_length=128)
    evidence_node_id: str = Field(min_length=1, max_length=128)
    source_locator: dict[str, JsonValue]
    source_locator_sha256: str
    source_entry_sha256: str
    subject_binding_revision_id: str = Field(min_length=1, max_length=128)
    extraction_run_id: str = Field(min_length=1, max_length=128)
    extraction_seal_id: str = Field(min_length=1, max_length=128)
    source_taxonomy_version: str = Field(min_length=1)
    anchor_payload_sha256: str
    source_context_id: str | None = None
    source_unit_id: str | None = None
    decimals: str | None = None
    precision: str | None = None
    legacy_match_revision_id: str | None = None

    _locator_sha = field_validator("source_locator_sha256")(_validate_sha256)
    _entry_sha = field_validator("source_entry_sha256")(_validate_sha256)
    _anchor_sha = field_validator("anchor_payload_sha256")(_validate_sha256)

    @model_validator(mode="after")
    def _locator_is_exact(self) -> Self:
        if _digest(_canonical_json(self.source_locator)) != self.source_locator_sha256:
            raise ValueError("source_locator_sha256 must match source_locator")
        return self


class DerivationInput(_FrozenModel):
    input_ordinal: int = Field(ge=0)
    input_observation_id: str = Field(min_length=1, max_length=128)
    input_resolution_revision_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    input_role: str = Field(min_length=1, max_length=128)


class DerivedFactLineage(_FrozenModel):
    provenance_kind: Literal["derived"]
    formula_id: str = Field(min_length=1, max_length=128)
    formula_version: str = Field(min_length=1, max_length=128)
    derivation_seal_id: str = Field(min_length=1, max_length=128)
    formula_config_sha256: str
    canonical_input_digest_sha256: str
    derivation_basis_sha256: str
    input_basis: Literal["as_reported", "as_known"]
    formula_definition_sha256: str
    knowledge_cutoff: datetime
    recorded_at: datetime
    inputs: tuple[DerivationInput, ...] = Field(min_length=1)

    _formula_sha = field_validator("formula_config_sha256")(_validate_sha256)
    _input_sha = field_validator("canonical_input_digest_sha256")(_validate_sha256)
    _basis_sha = field_validator("derivation_basis_sha256")(_validate_sha256)
    _definition_sha = field_validator("formula_definition_sha256")(_validate_sha256)

    @model_validator(mode="after")
    def _inputs_are_complete(self) -> Self:
        ordinals = tuple(item.input_ordinal for item in self.inputs)
        if ordinals != tuple(range(len(self.inputs))):
            raise ValueError("derivation inputs must have contiguous canonical ordinals")
        payload = [
            {
                "input_observation_id": item.input_observation_id,
                "input_ordinal": item.input_ordinal,
                "input_resolution_revision_id": (item.input_resolution_revision_id),
                "input_role": item.input_role,
                "output_observation_id": "",
            }
            for item in self.inputs
        ]
        # The v2 seal includes the output observation ID.  It is injected by
        # FactHit's cross-contract validator before comparing the digest.
        if not payload:
            raise ValueError("derived lineage cannot be empty")
        return self


FactProvenance = Annotated[
    ReportedFactEvidence | DerivedFactLineage,
    Field(discriminator="provenance_kind"),
]


class FactHit(_FrozenModel):
    """One resolved fact with sufficient lineage to verify its exact value."""

    hit_kind: Literal["fact"] = "fact"
    fact_hit_id: str = Field(min_length=1, max_length=128)
    projection_run_id: str = Field(min_length=1, max_length=128)
    fact_cell_id: str = Field(min_length=1, max_length=128)
    reporting_entity_id: str = Field(min_length=1, max_length=128)
    scope_security_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    semantic_key_sha256: str
    semantic_key_version: Literal["fact_cell_semantic_key.v3"]
    concept_namespace: str = Field(min_length=1)
    concept_name: str = Field(min_length=1)
    taxonomy_name: str = Field(min_length=1)
    taxonomy_version: str | None = None
    accounting_basis: str = Field(min_length=1)
    consolidation_scope: str = Field(min_length=1)
    period_kind: Literal["instant", "duration"]
    period_start: datetime | None = None
    period_end: datetime
    fiscal_year: int | None = Field(default=None, ge=1, le=9999)
    fiscal_period: str | None = None
    dimensions: tuple[FactDimension, ...] = ()
    dimensions_sha256: str
    unit_key: str = Field(min_length=1)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    resolution_revision_id: str = Field(min_length=1, max_length=128)
    resolution_revision: int = Field(gt=0)
    candidate_set_id: str = Field(min_length=1, max_length=128)
    candidate_count: int = Field(gt=0)
    candidate_set_digest_sha256: str
    resolution_policy_name: str = Field(min_length=1, max_length=128)
    resolution_policy_version: str = Field(min_length=1, max_length=128)
    observation_id: str = Field(min_length=1, max_length=128)
    observation_payload_sha256: str
    observation_kind: FactObservationKind
    value_kind: FactValueKind
    numeric_value: Decimal | None = None
    text_value: str | None = None
    is_nil: bool = False
    raw_lexical_value: str | None = None
    cell_knowledge_at: datetime
    observation_effective_at: datetime
    observation_knowledge_at: datetime
    observation_recorded_at: datetime
    resolution_knowledge_at: datetime
    knowledge_cutoff: datetime
    provenance: FactProvenance
    row_sha256: str | None = None

    _semantic_sha = field_validator("semantic_key_sha256")(_validate_sha256)
    _observation_payload_sha = field_validator("observation_payload_sha256")(_validate_sha256)
    _dimensions_sha = field_validator("dimensions_sha256")(_validate_sha256)
    _candidate_sha = field_validator("candidate_set_digest_sha256")(_validate_sha256)
    _row_sha = field_validator("row_sha256")(_optional_sha256)

    @field_validator("numeric_value")
    @classmethod
    def _canonical_numeric_value(
        cls,
        value: Decimal | str | int | None,
    ) -> Decimal | None:
        return None if value is None else _canonical_decimal(value)

    @field_validator("currency")
    @classmethod
    def _currency_upper(cls, value: str | None) -> str | None:
        return None if value is None else value.upper()

    @field_validator("dimensions")
    @classmethod
    def _dimensions_canonical(
        cls,
        value: tuple[FactDimension, ...],
    ) -> tuple[FactDimension, ...]:
        if len({(dimension.axis_namespace, dimension.axis_name) for dimension in value}) != len(
            value
        ):
            raise ValueError("dimension axes must be unique")
        return tuple(
            sorted(
                value,
                key=lambda item: (
                    item.axis_namespace,
                    item.axis_name,
                    item.member_kind,
                    _canonical_json(item.canonical_member),
                ),
            )
        )

    @model_validator(mode="after")
    def _closed_fact_contract(self) -> Self:
        if self.period_kind == "instant" and self.period_start is not None:
            raise ValueError("instant facts cannot have period_start")
        if self.period_kind == "duration" and self.period_start is None:
            raise ValueError("duration facts require period_start")
        if self.period_start is not None and self.period_end < self.period_start:
            raise ValueError("period_end cannot precede period_start")
        if self.value_kind == "numeric":
            if self.numeric_value is None or self.text_value is not None or self.is_nil:
                raise ValueError("numeric facts require only numeric_value")
        elif self.value_kind == "text":
            if self.text_value is None or self.numeric_value is not None or self.is_nil:
                raise ValueError("text facts require only text_value")
        elif self.numeric_value is not None or self.text_value is not None or not self.is_nil:
            raise ValueError("nil facts cannot carry a parsed value")
        if self.observation_kind != self.provenance.provenance_kind:
            raise ValueError("observation kind and provenance kind must agree")
        if _as_utc(self.observation_knowledge_at) > _as_utc(self.knowledge_cutoff):
            raise ValueError("fact observation was not known at projection cutoff")
        if _as_utc(self.observation_recorded_at) > _as_utc(self.knowledge_cutoff):
            raise ValueError("fact observation was not recorded at projection cutoff")
        dimensions_payload = [dimension.canonical_member for dimension in self.dimensions]
        if _digest(_canonical_json(dimensions_payload)) != self.dimensions_sha256:
            raise ValueError("dimensions_sha256 must match dimensions")
        if isinstance(self.provenance, DerivedFactLineage):
            input_payload = [
                {
                    "input_observation_id": item.input_observation_id,
                    "input_ordinal": item.input_ordinal,
                    "input_resolution_revision_id": (item.input_resolution_revision_id),
                    "input_role": item.input_role,
                    "output_observation_id": self.observation_id,
                }
                for item in self.provenance.inputs
            ]
            if (
                _digest(_canonical_json(input_payload))
                != self.provenance.canonical_input_digest_sha256
            ):
                raise ValueError("derivation input digest does not match exact inputs")
        expected = self.canonical_row_sha256
        if self.row_sha256 is None:
            object.__setattr__(self, "row_sha256", expected)
        elif self.row_sha256 != expected:
            raise ValueError("row_sha256 must match the canonical fact hit")
        return self

    @property
    def canonical_row_sha256(self) -> str:
        payload = self.model_dump(mode="json", exclude={"row_sha256"})
        return _digest(_canonical_json(payload))


class DocumentHit(_FrozenModel):
    """Minimal narrative-hit contract, intentionally incapable of being a fact."""

    hit_kind: Literal["document"] = "document"
    manifest_id: str = Field(min_length=1, max_length=128)
    chunk_id: str = Field(min_length=1, max_length=128)
    evidence_node_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1)
    bundle_sha256: str

    _bundle_sha = field_validator("bundle_sha256")(_validate_sha256)


GroundedHit = Annotated[
    DocumentHit | FactHit,
    Field(discriminator="hit_kind"),
]
_GROUNDED_HIT_ADAPTER: TypeAdapter[GroundedHit] = TypeAdapter(GroundedHit)


class FactSearchFilter(_FrozenModel):
    reporting_entity_ids: tuple[str, ...] = ()
    concept_namespaces: tuple[str, ...] = ()
    concept_names: tuple[str, ...] = ()
    concept_query: str | None = Field(default=None, min_length=1)
    period_start_min: datetime | None = None
    period_end_min: datetime | None = None
    period_end_max: datetime | None = None
    dimensions: tuple[FactDimension, ...] = ()
    unit_keys: tuple[str, ...] = ()
    currencies: tuple[str, ...] = ()
    numeric_min: Decimal | None = None
    numeric_max: Decimal | None = None
    value_kinds: tuple[FactValueKind, ...] = ()

    @field_validator("numeric_min", "numeric_max")
    @classmethod
    def _canonical_numeric_bound(
        cls,
        value: Decimal | str | int | None,
    ) -> Decimal | None:
        return None if value is None else _canonical_decimal(value)

    @field_validator("currencies")
    @classmethod
    def _currencies_upper(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(item.upper() for item in value)

    @model_validator(mode="after")
    def _ranges(self) -> Self:
        if (
            self.period_end_min is not None
            and self.period_end_max is not None
            and self.period_end_min > self.period_end_max
        ):
            raise ValueError("period_end_min cannot exceed period_end_max")
        if (
            self.numeric_min is not None
            and self.numeric_max is not None
            and self.numeric_min > self.numeric_max
        ):
            raise ValueError("numeric_min cannot exceed numeric_max")
        return self


class FactProjectionSpec(_FrozenModel):
    projection_run_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    projection_key: str = Field(min_length=1, max_length=256)
    revision: int = Field(gt=0)
    manifest_id: str = Field(min_length=1, max_length=128)
    knowledge_cutoff: datetime
    config_sha256: str
    code_version: str = Field(min_length=1, max_length=128)
    supersedes_projection_run_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    recorded_at: datetime

    _config_sha = field_validator("config_sha256")(_validate_sha256)

    @model_validator(mode="after")
    def _revision_chain_shape(self) -> Self:
        if (self.revision == 1) != (self.supersedes_projection_run_id is None):
            raise ValueError("projection revision parent is inconsistent")
        if _as_utc(self.recorded_at) < _as_utc(self.knowledge_cutoff):
            raise ValueError("projection cannot be recorded before its cutoff")
        return self


class FactProjectionMembership(_FrozenModel):
    membership_id: str = Field(min_length=1, max_length=128)
    projection_run_id: str = Field(min_length=1, max_length=128)
    fact_cell_id: str = Field(min_length=1, max_length=128)
    disposition: MembershipStatus
    reason_code: str = Field(min_length=1, max_length=128)
    reason_details: dict[str, JsonValue] = {}
    resolution_revision_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    membership_bundle_sha256: str | None = None
    recorded_at: datetime

    _bundle_sha = field_validator("membership_bundle_sha256")(_optional_sha256)

    @model_validator(mode="after")
    def _bundle_is_exact(self) -> Self:
        if self.disposition == "included" and self.resolution_revision_id is None:
            raise ValueError("included memberships require a resolution")
        expected = self.canonical_bundle_sha256
        if self.membership_bundle_sha256 is None:
            object.__setattr__(self, "membership_bundle_sha256", expected)
        elif self.membership_bundle_sha256 != expected:
            raise ValueError("membership_bundle_sha256 must match membership")
        return self

    @property
    def canonical_bundle_sha256(self) -> str:
        return _digest(
            _canonical_json(
                self.model_dump(
                    mode="json",
                    exclude={"membership_bundle_sha256"},
                )
            )
        )


class FactProjectionSeal(_FrozenModel):
    projection_seal_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    projection_run_id: str = Field(min_length=1, max_length=128)
    manifest_id: str = Field(min_length=1, max_length=128)
    eligible_fact_cell_count: int = Field(ge=0)
    membership_count: int = Field(ge=0)
    included_count: int = Field(ge=0)
    unresolved_material_count: int = Field(ge=0)
    missing_provenance_count: int = Field(ge=0)
    quarantined_count: int = Field(ge=0)
    row_count: int = Field(ge=0)
    membership_set_sha256: str
    row_set_sha256: str
    config_sha256: str
    sealed_at: datetime

    _membership_sha = field_validator("membership_set_sha256")(_validate_sha256)
    _row_sha = field_validator("row_set_sha256")(_validate_sha256)
    _config_sha = field_validator("config_sha256")(_validate_sha256)

    @model_validator(mode="after")
    def _counts_reconcile(self) -> Self:
        dispositions = (
            self.included_count
            + self.unresolved_material_count
            + self.missing_provenance_count
            + self.quarantined_count
        )
        if self.membership_count != self.eligible_fact_cell_count:
            raise ValueError("every eligible cell requires one membership")
        if dispositions != self.membership_count:
            raise ValueError("membership disposition counts do not reconcile")
        if self.row_count != self.included_count:
            raise ValueError("every included membership requires one row")
        return self


class FactProjectionResult(_FrozenModel):
    projection: FactProjectionSpec
    memberships: tuple[FactProjectionMembership, ...]
    rows: tuple[FactHit, ...]
    seal: FactProjectionSeal
    created: bool


class RankedGroundedHit(_FrozenModel):
    rank: int = Field(gt=0)
    score: float
    hit: GroundedHit


class FactProjectionError(RuntimeError):
    """Raised when a projection cannot prove a complete, sealed snapshot."""


class FactSearchProjectionStore:
    """Build, query, and trace immutable structured-fact projections."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def build_projection(self, spec: FactProjectionSpec) -> FactProjectionResult:
        """Atomically account for every cutoff-eligible v2 fact cell.

        A projection can be sealed even when some cells are unresolved or lack
        provenance, because those gaps are first-class memberships rather than
        silently omitted facts.  Only the ``included`` disposition creates a
        searchable row.
        """
        self._require_complete_manifest(spec)
        existing = self._fetchone(
            "SELECT * FROM search_fact_projection_runs WHERE idempotency_key = ?",
            (spec.idempotency_key,),
        )
        if existing is not None:
            self._verify_projection_replay(existing, spec)
            return self.load_projection(spec.projection_run_id, created=False)

        with self._savepoint("build_fact_search_projection"):
            self._insert_projection_run(spec)
            memberships: list[FactProjectionMembership] = []
            hits: list[FactHit] = []
            cell_rows = self._fetchall(
                "SELECT * FROM fact_cells_v2 "
                "WHERE knowledge_at <= ? AND recorded_at <= ? "
                "ORDER BY fact_cell_id",
                (spec.knowledge_cutoff, spec.recorded_at),
            )
            for cell in cell_rows:
                membership, hit = self._project_cell(spec, cell)
                self._insert_membership(membership)
                memberships.append(membership)
                if hit is not None:
                    self._insert_fact_hit(hit, recorded_at=spec.recorded_at)
                    hits.append(hit)

            seal = self._make_seal(spec, memberships, hits)
            self._insert_seal(seal)
        return FactProjectionResult(
            projection=spec,
            memberships=tuple(memberships),
            rows=tuple(hits),
            seal=seal,
            created=True,
        )

    def load_projection(
        self,
        projection_run_id: str,
        *,
        created: bool = False,
    ) -> FactProjectionResult:
        run = self._fetchone(
            "SELECT * FROM search_fact_projection_runs WHERE projection_run_id = ?",
            (projection_run_id,),
        )
        if run is None:
            raise FactProjectionError(f"unknown projection {projection_run_id!r}")
        projection = self._spec_from_row(run)
        memberships = tuple(
            self._membership_from_row(row)
            for row in self._fetchall(
                "SELECT * FROM search_fact_projection_memberships "
                "WHERE projection_run_id = ? ORDER BY fact_cell_id",
                (projection_run_id,),
            )
        )
        hits = tuple(
            self._fact_hit_from_bundle(str(row["row_bundle_json"]))
            for row in self._fetchall(
                "SELECT row_bundle_json FROM search_fact_projection_rows "
                "WHERE projection_run_id = ? ORDER BY fact_cell_id",
                (projection_run_id,),
            )
        )
        seal_row = self._fetchone(
            "SELECT * FROM search_fact_projection_seals WHERE projection_run_id = ?",
            (projection_run_id,),
        )
        if seal_row is None:
            raise FactProjectionError("projection exists but is not sealed")
        seal = self._seal_from_row(seal_row)
        self._verify_loaded_seal(projection, memberships, hits, seal)
        return FactProjectionResult(
            projection=projection,
            memberships=memberships,
            rows=hits,
            seal=seal,
            created=created,
        )

    def search(
        self,
        projection_run_id: str,
        filters: FactSearchFilter,
        *,
        limit: int = 50,
    ) -> tuple[FactHit, ...]:
        """Return structured facts using exact filters and Decimal comparisons."""
        if limit <= 0 or limit > 1_000:
            raise ValueError("limit must be between 1 and 1000")
        if (
            self._fetchone(
                "SELECT 1 FROM search_fact_projection_seals WHERE projection_run_id = ?",
                (projection_run_id,),
            )
            is None
        ):
            raise FactProjectionError("fact search requires a sealed projection")

        clauses = ["projection_run_id = ?"]
        parameters: list[object] = [projection_run_id]
        self._add_in_filter(
            clauses,
            parameters,
            "reporting_entity_id",
            filters.reporting_entity_ids,
        )
        self._add_in_filter(
            clauses,
            parameters,
            "concept_namespace",
            filters.concept_namespaces,
        )
        self._add_in_filter(
            clauses,
            parameters,
            "concept_name",
            filters.concept_names,
        )
        self._add_in_filter(clauses, parameters, "unit_key", filters.unit_keys)
        self._add_in_filter(clauses, parameters, "currency", filters.currencies)
        self._add_in_filter(
            clauses,
            parameters,
            "value_kind",
            filters.value_kinds,
        )
        if filters.concept_query is not None:
            escaped = (
                filters.concept_query.lower()
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            clauses.append(
                "(lower(concept_name) LIKE ? ESCAPE '\\' "
                "OR lower(concept_namespace) LIKE ? ESCAPE '\\' "
                "OR lower(taxonomy_name) LIKE ? ESCAPE '\\')"
            )
            parameters.extend((f"%{escaped}%",) * 3)
        if filters.period_start_min is not None:
            clauses.append("period_start >= ?")
            parameters.append(filters.period_start_min)
        if filters.period_end_min is not None:
            clauses.append("period_end >= ?")
            parameters.append(filters.period_end_min)
        if filters.period_end_max is not None:
            clauses.append("period_end <= ?")
            parameters.append(filters.period_end_max)

        rows = self._fetchall(
            "SELECT row_bundle_json FROM search_fact_projection_rows WHERE "  # nosec B608 -- trusted internal SQL shape; values remain bound
            + " AND ".join(clauses)
            + " ORDER BY period_end DESC, concept_namespace, concept_name, "
            "fact_cell_id",
            tuple(parameters),
        )
        requested_dimensions = {
            _canonical_json(dimension.canonical_member) for dimension in filters.dimensions
        }
        hits: list[FactHit] = []
        for row in rows:
            hit = self._fact_hit_from_bundle(str(row["row_bundle_json"]))
            actual_dimensions = {
                _canonical_json(dimension.canonical_member) for dimension in hit.dimensions
            }
            if not requested_dimensions.issubset(actual_dimensions):
                continue
            if filters.numeric_min is not None and (
                hit.numeric_value is None or hit.numeric_value < filters.numeric_min
            ):
                continue
            if filters.numeric_max is not None and (
                hit.numeric_value is None or hit.numeric_value > filters.numeric_max
            ):
                continue
            hits.append(hit)
            if len(hits) == limit:
                break
        return tuple(hits)

    def persist_trace_hits(
        self,
        trace_id: str,
        hits: Sequence[RankedGroundedHit],
        *,
        recorded_at: datetime,
    ) -> None:
        """Persist one globally ranked, heterogeneous retrieval trace.

        The discriminated hit contract plus the migration's XOR foreign-key
        guard make it impossible to submit a document chunk as a fact hit.
        """
        if (
            self._fetchone(
                "SELECT 1 FROM ask_retrieval_traces WHERE trace_id = ?",
                (trace_id,),
            )
            is None
        ):
            raise FactProjectionError(f"unknown retrieval trace {trace_id!r}")
        ranks = tuple(item.rank for item in hits)
        if ranks != tuple(range(1, len(hits) + 1)):
            raise ValueError("trace hit ranks must be contiguous and start at one")
        identities: set[tuple[str, str]] = set()
        with self._savepoint("persist_grounded_trace_hits"):
            for ranked in hits:
                hit = _GROUNDED_HIT_ADAPTER.validate_python(ranked.hit)
                if isinstance(hit, DocumentHit):
                    if (
                        self._fetchone(
                            "SELECT 1 FROM search_chunks AS chunk "
                            "JOIN search_corpus_manifest_seals AS seal "
                            "ON seal.manifest_id = chunk.manifest_id "
                            "WHERE chunk.chunk_id = ? "
                            "AND chunk.manifest_id = ? "
                            "AND chunk.evidence_node_id = ? "
                            "AND chunk.text = ? "
                            "AND seal.completion_status = 'complete'",
                            (
                                hit.chunk_id,
                                hit.manifest_id,
                                hit.evidence_node_id,
                                hit.text,
                            ),
                        )
                        is None
                    ):
                        raise FactProjectionError(
                            "document trace hit does not match an exact sealed chunk"
                        )
                    identity = ("document", hit.chunk_id)
                    values: tuple[object, ...] = (
                        trace_id,
                        ranked.rank,
                        "document",
                        hit.manifest_id,
                        hit.chunk_id,
                        None,
                        None,
                        ranked.score,
                        hit.bundle_sha256,
                        recorded_at,
                    )
                else:
                    if (
                        self._fetchone(
                            "SELECT 1 FROM search_fact_projection_rows AS row "
                            "JOIN search_fact_projection_seals AS seal "
                            "ON seal.projection_run_id = row.projection_run_id "
                            "WHERE row.fact_hit_id = ? "
                            "AND row.projection_run_id = ? "
                            "AND row.row_bundle_sha256 = ?",
                            (
                                hit.fact_hit_id,
                                hit.projection_run_id,
                                hit.row_sha256,
                            ),
                        )
                        is None
                    ):
                        raise FactProjectionError(
                            "fact trace hit does not match an exact sealed fact row"
                        )
                    identity = ("fact", hit.fact_hit_id)
                    values = (
                        trace_id,
                        ranked.rank,
                        "fact",
                        None,
                        None,
                        hit.projection_run_id,
                        hit.fact_hit_id,
                        ranked.score,
                        hit.row_sha256,
                        recorded_at,
                    )
                if identity in identities:
                    raise ValueError("a retrieval source cannot appear twice in one trace")
                identities.add(identity)
                columns = (
                    "trace_id",
                    "rank",
                    "hit_kind",
                    "manifest_id",
                    "chunk_id",
                    "projection_run_id",
                    "fact_hit_id",
                    "score",
                    "bundle_sha256",
                    "recorded_at",
                )
                self._insert_or_verify(
                    "ask_retrieval_trace_hits",
                    columns,
                    values,
                    identity_columns=("trace_id", "rank"),
                    identity_values=(trace_id, ranked.rank),
                )

    def _project_cell(
        self,
        spec: FactProjectionSpec,
        cell: dict[str, object],
    ) -> tuple[FactProjectionMembership, FactHit | None]:
        fact_cell_id = str(cell["fact_cell_id"])
        if _as_utc(_parse_datetime(cell["recorded_at"])) > _as_utc(spec.knowledge_cutoff):
            return (
                self._membership(
                    spec,
                    fact_cell_id,
                    "quarantined",
                    "cell_recorded_after_cutoff",
                    {
                        "cell_recorded_at": str(cell["recorded_at"]),
                    },
                ),
                None,
            )
        resolution = self._fetchone(
            "SELECT * FROM fact_resolution_revisions_v2 "
            "WHERE fact_cell_id = ? AND knowledge_at <= ? AND recorded_at <= ? "
            "ORDER BY revision DESC, knowledge_at DESC, recorded_at DESC "
            "LIMIT 1",
            (fact_cell_id, spec.knowledge_cutoff, spec.knowledge_cutoff),
        )
        resolution_id = None if resolution is None else str(resolution["resolution_revision_id"])
        try:
            FactReadModel(self._conn).cell(
                fact_cell_id,
                cutoff=spec.knowledge_cutoff,
            )
        except FactAdmissionError as exc:
            disposition: MembershipStatus = exc.disposition
            if resolution_id is None and disposition == "missing_provenance":
                disposition = "quarantined"
            return (
                self._membership(
                    spec,
                    fact_cell_id,
                    disposition,
                    exc.reason_code,
                    {
                        "record_id": exc.record_id,
                        "record_kind": exc.record_kind,
                    },
                    resolution_id=resolution_id,
                ),
                None,
            )
        if resolution is None:
            return (
                self._membership(
                    spec,
                    fact_cell_id,
                    "unresolved_material",
                    "no_as_known_resolution",
                    {},
                ),
                None,
            )
        assert resolution_id is not None
        if resolution["status"] != "resolved":
            return (
                self._membership(
                    spec,
                    fact_cell_id,
                    "unresolved_material",
                    f"resolution_{resolution['status']}",
                    {"resolution_revision_id": resolution_id},
                    resolution_id=resolution_id,
                ),
                None,
            )
        candidate_rows = self._fetchall(
            "SELECT * FROM fact_resolution_candidates_v2 "
            "WHERE candidate_set_id = ? ORDER BY candidate_ordinal",
            (resolution["candidate_set_id"],),
        )
        candidate_reason = self._candidate_set_problem(resolution, candidate_rows)
        if candidate_reason is not None:
            return (
                self._membership(
                    spec,
                    fact_cell_id,
                    "quarantined",
                    candidate_reason,
                    {"resolution_revision_id": resolution_id},
                    resolution_id=resolution_id,
                ),
                None,
            )
        selected_id = resolution["selected_observation_id"]
        if selected_id is None:
            return (
                self._membership(
                    spec,
                    fact_cell_id,
                    "quarantined",
                    "resolved_without_selection",
                    {"resolution_revision_id": resolution_id},
                    resolution_id=resolution_id,
                ),
                None,
            )
        selected = [
            row
            for row in candidate_rows
            if row["observation_id"] == selected_id and row["eligibility"] == "eligible"
        ]
        if len(selected) != 1:
            return (
                self._membership(
                    spec,
                    fact_cell_id,
                    "quarantined",
                    "selected_observation_not_exact_eligible_candidate",
                    {"resolution_revision_id": resolution_id},
                    resolution_id=resolution_id,
                ),
                None,
            )
        observation = self._fetchone(
            "SELECT * FROM fact_observations_v2 WHERE observation_id = ?",
            (selected_id,),
        )
        if observation is None or observation["fact_cell_id"] != fact_cell_id:
            return (
                self._membership(
                    spec,
                    fact_cell_id,
                    "quarantined",
                    "selected_observation_missing_or_wrong_cell",
                    {"observation_id": str(selected_id)},
                    resolution_id=resolution_id,
                ),
                None,
            )
        if any(
            _as_utc(_parse_datetime(observation[column])) > _as_utc(spec.knowledge_cutoff)
            for column in ("knowledge_at", "recorded_at")
        ):
            return (
                self._membership(
                    spec,
                    fact_cell_id,
                    "quarantined",
                    "selected_observation_after_cutoff",
                    {"observation_id": str(selected_id)},
                    resolution_id=resolution_id,
                ),
                None,
            )
        try:
            value = self._load_hardened_value(
                spec,
                cell,
                resolution,
                observation,
                selected[0],
            )
            provenance = self._provenance_from_value(value)
            hit = self._make_fact_hit(
                spec,
                cell,
                resolution,
                observation,
                provenance,
                value.observation_payload_sha256,
            )
        except FactAdmissionError as exc:
            return (
                self._membership(
                    spec,
                    fact_cell_id,
                    exc.disposition,
                    exc.reason_code,
                    {
                        "record_id": exc.record_id,
                        "record_kind": exc.record_kind,
                    },
                    resolution_id=resolution_id,
                ),
                None,
            )
        except (FactProjectionError, ValidationError, ValueError) as exc:
            return (
                self._membership(
                    spec,
                    fact_cell_id,
                    "missing_provenance",
                    "provenance_incomplete",
                    {"detail": str(exc)},
                    resolution_id=resolution_id,
                ),
                None,
            )
        membership = self._membership(
            spec,
            fact_cell_id,
            "included",
            "resolved_with_exact_provenance",
            {
                "fact_hit_id": hit.fact_hit_id,
                "row_sha256": hit.row_sha256,
            },
            resolution_id=resolution_id,
        )
        return membership, hit

    def _candidate_set_problem(
        self,
        resolution: dict[str, object],
        candidates: Sequence[dict[str, object]],
    ) -> str | None:
        if _as_int(resolution["candidate_count"]) != len(candidates):
            return "candidate_set_count_mismatch"
        if tuple(_as_int(row["candidate_ordinal"]) for row in candidates) != tuple(
            range(len(candidates))
        ):
            return "candidate_set_ordinals_incomplete"
        if any(row["fact_cell_id"] != resolution["fact_cell_id"] for row in candidates):
            return "candidate_set_wrong_cell"
        digest_payload = [
            {
                "candidate_ordinal": _as_int(row["candidate_ordinal"]),
                "candidate_payload_sha256": row["candidate_payload_sha256"],
                "eligibility": row["eligibility"],
                "observation_id": row["observation_id"],
            }
            for row in candidates
        ]
        if _digest(_canonical_json(digest_payload)) != resolution["candidate_set_digest_sha256"]:
            return "candidate_set_digest_mismatch"
        for candidate in candidates:
            payload = self._fetchone(
                "SELECT observation_payload_sha256 "
                "FROM fact_observation_payload_commitments_v2 "
                "WHERE observation_id = ?",
                (candidate["observation_id"],),
            )
            if payload is None:
                return "candidate_payload_commitment_missing"
            if candidate["candidate_payload_sha256"] != payload["observation_payload_sha256"]:
                return "candidate_payload_commitment_mismatch"
        return None

    def _load_hardened_value(
        self,
        spec: FactProjectionSpec,
        cell: dict[str, object],
        resolution: dict[str, object],
        observation: dict[str, object],
        selected_candidate: dict[str, object],
    ) -> FactValueRecord:
        read_model = FactReadModel(self._conn)
        fact_cell_id = str(cell["fact_cell_id"])
        observation_id = str(observation["observation_id"])
        try:
            hardened_cell = read_model.cell(
                fact_cell_id,
                cutoff=spec.knowledge_cutoff,
            )
            bundle = read_model.provenance_bundle(
                observation_id,
                cutoff=spec.knowledge_cutoff,
            )
            snapshot = read_model.as_known(fact_cell_id, spec.knowledge_cutoff)
        except ValueError as exc:
            raise FactProjectionError(str(exc)) from exc
        if (
            hardened_cell.semantic_key_sha256 != cell["semantic_key_sha256"]
            or bundle.cell != hardened_cell
            or bundle.observation.observation_id != observation_id
        ):
            raise FactProjectionError("typed fact read model disagrees with source rows")
        if hardened_cell.semantic_key_sha256 is None:
            raise FactProjectionError("typed fact cell lacks a semantic key")
        if (
            snapshot.resolution is None
            or snapshot.resolution.resolution_revision_id != resolution["resolution_revision_id"]
            or snapshot.canonical_value is None
            or snapshot.canonical_value.observation_id != observation_id
        ):
            raise FactProjectionError(
                "selected resolution is not the exact hardened as-known value"
            )
        self._verify_identity_seal(spec, hardened_cell, cell)
        payload_row = self._fetchone(
            "SELECT * FROM fact_observation_payload_commitments_v2 WHERE observation_id = ?",
            (observation_id,),
        )
        if payload_row is None:
            raise FactProjectionError("observation payload commitment is missing")
        payload_json = str(payload_row["canonical_payload_json"])
        if (
            payload_row["payload_version"] != "fact_observation_payload.v1"
            or _canonical_json(bundle.canonical_payload) != payload_json
            or _digest(payload_json) != payload_row["observation_payload_sha256"]
            or payload_row["observation_payload_sha256"]
            != selected_candidate["candidate_payload_sha256"]
            or _as_utc(_parse_datetime(payload_row["committed_at"]))
            > _as_utc(spec.knowledge_cutoff)
        ):
            raise FactProjectionError(
                "selected candidate payload commitment is not exact at cutoff"
            )
        self._verify_observation_payload(
            hardened_cell.semantic_key_sha256,
            observation,
            bundle.observation,
            payload_json,
        )
        if bundle.observation.evidence is not None:
            self._verify_reported_proof(
                spec,
                observation_id,
                bundle.observation.evidence,
            )
        elif bundle.observation.derivation is not None:
            self._verify_derived_proof(
                spec,
                observation_id,
                bundle.observation.derivation,
            )
        else:
            raise FactProjectionError("observation has no hardened provenance")
        return bundle.observation

    def _verify_identity_seal(
        self,
        spec: FactProjectionSpec,
        hardened_cell: FactCellV2,
        cell: dict[str, object],
    ) -> None:
        seal = self._fetchone(
            "SELECT * FROM fact_cell_identity_seals_v2 WHERE fact_cell_id = ?",
            (cell["fact_cell_id"],),
        )
        if seal is None:
            raise FactProjectionError("fact cell identity seal is missing")
        semantic_identity_json = hardened_cell.semantic_identity_json
        dimensions_json = hardened_cell.dimensions_json
        dimensions = hardened_cell.dimensions
        if (
            seal["semantic_key_version"] != "fact_cell_semantic_key.v3"
            or seal["semantic_identity_json"] != semantic_identity_json
            or _digest(semantic_identity_json) != seal["semantic_key_sha256"]
            or seal["semantic_key_sha256"] != cell["semantic_key_sha256"]
            or _as_int(seal["dimension_count"]) != len(dimensions)
            or seal["dimension_set_json"] != dimensions_json
            or _digest(dimensions_json) != seal["dimension_set_sha256"]
            or _as_utc(_parse_datetime(seal["sealed_at"])) > _as_utc(spec.knowledge_cutoff)
        ):
            raise FactProjectionError("fact cell identity seal is not exact at cutoff")

    def _verify_observation_payload(
        self,
        semantic_key_sha256: str,
        observation: dict[str, object],
        value: FactValueRecord,
        payload_json: str,
    ) -> None:
        provenance: dict[str, object]
        if value.evidence is not None:
            provenance = {
                "anchor_payload_sha256": value.evidence.anchor_payload_sha256,
                "document_version_id": value.evidence.document_version_id,
                "evidence_node_id": value.evidence.evidence_node_id,
                "source_context_id": observation["source_context_id"],
                "source_entry_sha256": value.evidence.source_entry_sha256,
                "source_locator_sha256": value.evidence.source_locator_sha256,
                "source_unit_id": observation["source_unit_id"],
            }
        elif value.derivation is not None:
            provenance = {
                "canonical_input_digest_sha256": (value.derivation.canonical_input_digest_sha256),
                "derivation_basis_sha256": (value.derivation.derivation_basis_sha256),
                "derivation_seal_id": value.derivation.derivation_seal_id,
                "formula_id": value.derivation.formula_id,
                "formula_version": value.derivation.formula_version,
            }
        else:
            raise FactProjectionError("observation payload lacks provenance")
        expected = _canonical_json(
            {
                "decimals": observation["decimals"],
                "effective_at": _parse_datetime(observation["effective_at"]).isoformat(),
                "fact_cell_semantic_key_sha256": semantic_key_sha256,
                "is_nil": bool(observation["is_nil"]),
                "knowledge_at": _parse_datetime(observation["knowledge_at"]).isoformat(),
                "method_config_sha256": observation["method_config_sha256"],
                "method_name": observation["method_name"],
                "method_version": observation["method_version"],
                "numeric_value": observation["numeric_value"],
                "observation_kind": observation["observation_kind"],
                "payload_version": "fact_observation_payload.v1",
                "precision": observation["precision"],
                "provenance": provenance,
                "raw_lexical_value": observation["raw_lexical_value"],
                "recorded_at": _parse_datetime(observation["recorded_at"]).isoformat(),
                "revision_kind": observation["revision_kind"],
                "supersedes_observation_id": observation["supersedes_observation_id"],
                "text_value": observation["text_value"],
                "value_kind": observation["value_kind"],
            }
        )
        if payload_json != expected:
            raise FactProjectionError("observation commitment does not bind the selected value")

    def _verify_reported_proof(
        self,
        spec: FactProjectionSpec,
        observation_id: str,
        evidence: ExactEvidenceReference,
    ) -> None:
        if evidence.extraction_seal_id is None:
            raise FactProjectionError("reported fact lacks an extraction seal")
        anchor = self._fetchone(
            "SELECT * FROM fact_reported_observation_anchors_v2 WHERE observation_id = ?",
            (observation_id,),
        )
        extraction = self._fetchone(
            "SELECT * FROM fact_extraction_run_completeness_seals_v2 WHERE extraction_run_id = ?",
            (evidence.extraction_run_id,),
        )
        manifest_anchor = self._fetchone(
            "SELECT 1 FROM evidence_nodes AS node "
            "JOIN evidence_extraction_runs AS run "
            "ON run.extraction_run_id = node.extraction_run_id "
            "JOIN search_corpus_document_memberships AS membership "
            "ON membership.document_version_id = run.document_version_id "
            "WHERE node.node_id = ? AND run.document_version_id = ? "
            "AND membership.manifest_id = ? "
            "AND membership.membership_status = 'included' LIMIT 1",
            (
                evidence.evidence_node_id,
                evidence.document_version_id,
                spec.manifest_id,
            ),
        )
        if anchor is None or extraction is None or manifest_anchor is None:
            raise FactProjectionError("reported fact lacks complete exact manifest evidence")
        anchor_payload = _canonical_json(
            {
                "document_version_id": evidence.document_version_id,
                "evidence_node_id": evidence.evidence_node_id,
                "extraction_input_sha256": evidence.input_sha256,
                "extraction_output_sha256": evidence.output_sha256,
                "extraction_run_id": evidence.extraction_run_id,
                "extractor_code_version": evidence.extractor_code_version,
                "extractor_config_sha256": evidence.extractor_config_sha256,
                "extractor_name": evidence.extractor_name,
                "raw_entry_sha256": evidence.source_entry_sha256,
                "source_locator_sha256": evidence.source_locator_sha256,
                "source_taxonomy_version": evidence.source_taxonomy_version,
                "subject_binding_revision_id": (evidence.subject_binding_revision_id),
            }
        )
        node_ids = [
            str(row["node_id"])
            for row in self._fetchall(
                "SELECT node_id FROM evidence_nodes WHERE extraction_run_id = ? ORDER BY node_id",
                (evidence.extraction_run_id,),
            )
        ]
        observation_ids = [
            str(row["observation_id"])
            for row in self._fetchall(
                "SELECT observation_id "
                "FROM fact_reported_observation_anchors_v2 "
                "WHERE extraction_run_id = ? ORDER BY observation_id",
                (evidence.extraction_run_id,),
            )
        ]
        node_set_json = _canonical_json(node_ids)
        observation_set_json = _canonical_json(observation_ids)
        if (
            anchor["anchor_payload_json"] != anchor_payload
            or _digest(anchor_payload) != anchor["anchor_payload_sha256"]
            or anchor["anchor_payload_sha256"] != evidence.anchor_payload_sha256
            or anchor["recorded_at"] is None
            or _as_utc(_parse_datetime(anchor["recorded_at"])) > _as_utc(spec.knowledge_cutoff)
            or extraction["extraction_seal_id"] != evidence.extraction_seal_id
            or _as_utc(_parse_datetime(extraction["knowledge_at"])) > _as_utc(spec.knowledge_cutoff)
            or _as_utc(_parse_datetime(extraction["recorded_at"])) > _as_utc(spec.knowledge_cutoff)
            or _as_int(extraction["expected_node_count"]) != len(node_ids)
            or _as_int(extraction["observed_node_count"]) != len(node_ids)
            or _as_int(extraction["reported_fact_count"]) != len(observation_ids)
            or extraction["node_set_json"] != node_set_json
            or extraction["node_set_sha256"] != _digest(node_set_json)
            or extraction["observation_set_json"] != observation_set_json
            or extraction["observation_set_sha256"] != _digest(observation_set_json)
            or extraction["extractor_config_sha256"] != evidence.extractor_config_sha256
            or extraction["extraction_output_sha256"] != evidence.output_sha256
        ):
            raise FactProjectionError(
                "reported anchor or extraction completeness seal is not exact"
            )

    def _verify_derived_proof(
        self,
        spec: FactProjectionSpec,
        observation_id: str,
        derivation: ExactDerivationReference,
    ) -> None:
        seal = self._fetchone(
            "SELECT * FROM fact_derivation_seals_v2 "
            "WHERE derivation_seal_id = ? AND output_observation_id = ?",
            (derivation.derivation_seal_id, observation_id),
        )
        basis = self._fetchone(
            "SELECT * FROM fact_derivation_basis_commitments_v2 WHERE derivation_seal_id = ?",
            (derivation.derivation_seal_id,),
        )
        if seal is None or basis is None:
            raise FactProjectionError("derived fact lacks a sealed derivation basis")
        basis_json = _canonical_json(
            {
                "canonical_input_digest_sha256": (derivation.canonical_input_digest_sha256),
                "execution_config_sha256": (derivation.execution_config_sha256),
                "formula_definition_sha256": (derivation.formula_definition_sha256),
                "formula_id": derivation.formula_id,
                "formula_version": derivation.formula_version,
                "input_basis": derivation.input_basis,
                "knowledge_cutoff": derivation.knowledge_cutoff.isoformat(),
            }
        )
        if (
            seal["canonical_input_digest_sha256"] != derivation.canonical_input_digest_sha256
            or seal["formula_config_sha256"] != derivation.execution_config_sha256
            or _as_utc(_parse_datetime(seal["knowledge_at"])) > _as_utc(spec.knowledge_cutoff)
            or _as_utc(_parse_datetime(seal["recorded_at"])) > _as_utc(spec.knowledge_cutoff)
            or basis["canonical_basis_json"] != basis_json
            or _digest(basis_json) != basis["canonical_basis_sha256"]
            or basis["canonical_basis_sha256"] != derivation.derivation_basis_sha256
            or _as_utc(_parse_datetime(basis["knowledge_cutoff"])) > _as_utc(spec.knowledge_cutoff)
            or _as_utc(_parse_datetime(basis["recorded_at"])) > _as_utc(spec.knowledge_cutoff)
        ):
            raise FactProjectionError("derived derivation seal or basis commitment is not exact")

    def _provenance_from_value(self, value: FactValueRecord) -> FactProvenance:
        if value.evidence is not None:
            evidence = value.evidence
            if evidence.extraction_seal_id is None:
                raise FactProjectionError("reported fact lacks an extraction completeness seal")
            return ReportedFactEvidence(
                provenance_kind="reported",
                document_version_id=evidence.document_version_id,
                evidence_node_id=evidence.evidence_node_id,
                source_locator=evidence.source_locator.root,
                source_locator_sha256=evidence.source_locator_sha256,
                source_entry_sha256=evidence.source_entry_sha256,
                subject_binding_revision_id=(evidence.subject_binding_revision_id),
                extraction_run_id=evidence.extraction_run_id,
                extraction_seal_id=evidence.extraction_seal_id,
                source_taxonomy_version=evidence.source_taxonomy_version,
                anchor_payload_sha256=evidence.anchor_payload_sha256,
            )
        if value.derivation is None:
            raise FactProjectionError("fact value has no exact provenance")
        derivation = value.derivation
        edges = self._fetchall(
            "SELECT input_observation_id,input_resolution_revision_id,"
            "input_role,input_ordinal FROM fact_derivation_input_edges_v2 "
            "WHERE output_observation_id = ? ORDER BY input_ordinal",
            (value.observation_id,),
        )
        if len(edges) != len(derivation.input_observation_ids):
            raise FactProjectionError("derived input edge set is incomplete")
        return DerivedFactLineage(
            provenance_kind="derived",
            formula_id=derivation.formula_id,
            formula_version=derivation.formula_version,
            derivation_seal_id=derivation.derivation_seal_id,
            formula_config_sha256=derivation.execution_config_sha256,
            canonical_input_digest_sha256=(derivation.canonical_input_digest_sha256),
            derivation_basis_sha256=derivation.derivation_basis_sha256,
            input_basis=derivation.input_basis,
            formula_definition_sha256=derivation.formula_definition_sha256,
            knowledge_cutoff=derivation.knowledge_cutoff,
            recorded_at=derivation.recorded_at,
            inputs=tuple(
                DerivationInput(
                    input_ordinal=_as_int(edge["input_ordinal"]),
                    input_observation_id=str(edge["input_observation_id"]),
                    input_resolution_revision_id=self._optional_text(
                        edge["input_resolution_revision_id"]
                    ),
                    input_role=str(edge["input_role"]),
                )
                for edge in edges
            ),
        )

    def _make_fact_hit(
        self,
        spec: FactProjectionSpec,
        cell: dict[str, object],
        resolution: dict[str, object],
        observation: dict[str, object],
        provenance: FactProvenance,
        observation_payload_sha256: str,
    ) -> FactHit:
        numeric_value = (
            None
            if observation["numeric_value"] is None
            else _canonical_decimal(str(observation["numeric_value"]))
        )
        hit_identity = _digest(
            _canonical_json(
                {
                    "projection_run_id": spec.projection_run_id,
                    "fact_cell_id": cell["fact_cell_id"],
                    "resolution_revision_id": resolution["resolution_revision_id"],
                    "observation_id": observation["observation_id"],
                }
            )
        )
        dimensions = tuple(
            FactDimension.model_validate(item)
            for item in _json_array(str(cell["canonical_dimensions_json"]))
        )
        return FactHit.model_validate(
            dict(
                fact_hit_id=f"fact-hit-{hit_identity}",
                projection_run_id=spec.projection_run_id,
                fact_cell_id=str(cell["fact_cell_id"]),
                reporting_entity_id=str(cell["reporting_entity_id"]),
                scope_security_id=self._optional_text(cell["scope_security_id"]),
                semantic_key_sha256=str(cell["semantic_key_sha256"]),
                semantic_key_version="fact_cell_semantic_key.v3",
                concept_namespace=str(cell["concept_namespace"]),
                concept_name=str(cell["concept_name"]),
                taxonomy_name=str(cell["taxonomy_name"]),
                taxonomy_version=self._optional_text(cell["taxonomy_version"]),
                accounting_basis=str(cell["accounting_basis"]),
                consolidation_scope=str(cell["consolidation_scope"]),
                period_kind=str(cell["period_kind"]),
                period_start=(
                    None if cell["period_start"] is None else _parse_datetime(cell["period_start"])
                ),
                period_end=_parse_datetime(cell["period_end"]),
                fiscal_year=(None if cell["fiscal_year"] is None else _as_int(cell["fiscal_year"])),
                fiscal_period=self._optional_text(cell["fiscal_period"]),
                dimensions=dimensions,
                dimensions_sha256=str(cell["canonical_dimensions_sha256"]),
                unit_key=str(cell["unit_key"]),
                currency=self._optional_text(cell["currency"]),
                resolution_revision_id=str(resolution["resolution_revision_id"]),
                resolution_revision=_as_int(resolution["revision"]),
                candidate_set_id=str(resolution["candidate_set_id"]),
                candidate_count=_as_int(resolution["candidate_count"]),
                candidate_set_digest_sha256=str(resolution["candidate_set_digest_sha256"]),
                resolution_policy_name=str(resolution["policy_name"]),
                resolution_policy_version=str(resolution["policy_version"]),
                observation_id=str(observation["observation_id"]),
                observation_payload_sha256=observation_payload_sha256,
                observation_kind=str(observation["observation_kind"]),
                value_kind=str(observation["value_kind"]),
                numeric_value=numeric_value,
                text_value=self._optional_text(observation["text_value"]),
                is_nil=bool(observation["is_nil"]),
                raw_lexical_value=self._optional_text(observation["raw_lexical_value"]),
                cell_knowledge_at=_parse_datetime(cell["knowledge_at"]),
                observation_effective_at=_parse_datetime(observation["effective_at"]),
                observation_knowledge_at=_parse_datetime(observation["knowledge_at"]),
                observation_recorded_at=_parse_datetime(observation["recorded_at"]),
                resolution_knowledge_at=_parse_datetime(resolution["knowledge_at"]),
                knowledge_cutoff=spec.knowledge_cutoff,
                provenance=provenance,
            )
        )

    def _membership(
        self,
        spec: FactProjectionSpec,
        fact_cell_id: str,
        disposition: MembershipStatus,
        reason_code: str,
        reason_details: dict[str, JsonValue],
        *,
        resolution_id: str | None = None,
    ) -> FactProjectionMembership:
        identity = _digest(
            _canonical_json(
                {
                    "projection_run_id": spec.projection_run_id,
                    "fact_cell_id": fact_cell_id,
                }
            )
        )
        return FactProjectionMembership(
            membership_id=f"fact-membership-{identity}",
            projection_run_id=spec.projection_run_id,
            fact_cell_id=fact_cell_id,
            disposition=disposition,
            resolution_revision_id=resolution_id,
            reason_code=reason_code,
            reason_details=reason_details,
            recorded_at=spec.recorded_at,
        )

    def _make_seal(
        self,
        spec: FactProjectionSpec,
        memberships: Sequence[FactProjectionMembership],
        hits: Sequence[FactHit],
    ) -> FactProjectionSeal:
        membership_payload = [
            {
                "membership_bundle_sha256": membership.membership_bundle_sha256,
                "membership_id": membership.membership_id,
            }
            for membership in sorted(
                memberships,
                key=lambda item: item.membership_id,
            )
        ]
        row_payload = [
            {
                "fact_hit_id": hit.fact_hit_id,
                "row_bundle_sha256": hit.row_sha256,
            }
            for hit in sorted(
                hits,
                key=lambda item: item.fact_hit_id,
            )
        ]
        disposition_counts = {
            status: sum(membership.disposition == status for membership in memberships)
            for status in (
                "included",
                "unresolved_material",
                "missing_provenance",
                "quarantined",
            )
        }
        identity = _digest(
            _canonical_json(
                {
                    "projection_run_id": spec.projection_run_id,
                    "membership_set_sha256": _digest(_canonical_json(membership_payload)),
                    "row_set_sha256": _digest(_canonical_json(row_payload)),
                }
            )
        )
        return FactProjectionSeal(
            projection_seal_id=f"fact-projection-seal-{identity}",
            idempotency_key=f"fact-projection-seal:{spec.projection_run_id}",
            projection_run_id=spec.projection_run_id,
            manifest_id=spec.manifest_id,
            eligible_fact_cell_count=len(memberships),
            membership_count=len(memberships),
            included_count=disposition_counts["included"],
            unresolved_material_count=disposition_counts["unresolved_material"],
            missing_provenance_count=disposition_counts["missing_provenance"],
            quarantined_count=disposition_counts["quarantined"],
            row_count=len(hits),
            membership_set_sha256=_digest(_canonical_json(membership_payload)),
            row_set_sha256=_digest(_canonical_json(row_payload)),
            config_sha256=spec.config_sha256,
            sealed_at=spec.recorded_at,
        )

    def _require_complete_manifest(self, spec: FactProjectionSpec) -> None:
        row = self._fetchone(
            "SELECT manifest.knowledge_cutoff, seal.completion_status "
            "FROM search_corpus_manifests AS manifest "
            "JOIN search_corpus_manifest_seals AS seal "
            "ON seal.manifest_id = manifest.manifest_id "
            "WHERE manifest.manifest_id = ?",
            (spec.manifest_id,),
        )
        if row is None or row["completion_status"] != "complete":
            raise FactProjectionError("fact projection requires a complete sealed corpus manifest")
        if row["knowledge_cutoff"] is None or _as_utc(
            _parse_datetime(row["knowledge_cutoff"])
        ) != _as_utc(spec.knowledge_cutoff):
            raise FactProjectionError(
                "fact projection and corpus manifest require the exact same cutoff"
            )

    def _insert_projection_run(self, spec: FactProjectionSpec) -> None:
        columns = (
            "projection_run_id",
            "idempotency_key",
            "projection_key",
            "revision",
            "manifest_id",
            "knowledge_cutoff",
            "config_sha256",
            "code_version",
            "supersedes_projection_run_id",
            "recorded_at",
        )
        values = (
            spec.projection_run_id,
            spec.idempotency_key,
            spec.projection_key,
            spec.revision,
            spec.manifest_id,
            spec.knowledge_cutoff,
            spec.config_sha256,
            spec.code_version,
            spec.supersedes_projection_run_id,
            spec.recorded_at,
        )
        self._insert_or_verify(
            "search_fact_projection_runs",
            columns,
            values,
            identity_columns=("idempotency_key",),
            identity_values=(spec.idempotency_key,),
        )

    def _insert_membership(self, membership: FactProjectionMembership) -> None:
        columns = (
            "membership_id",
            "projection_run_id",
            "fact_cell_id",
            "disposition",
            "resolution_revision_id",
            "reason_code",
            "reason_details_json",
            "membership_bundle_sha256",
            "recorded_at",
        )
        values = (
            membership.membership_id,
            membership.projection_run_id,
            membership.fact_cell_id,
            membership.disposition,
            membership.resolution_revision_id,
            membership.reason_code,
            _canonical_json(membership.reason_details),
            membership.membership_bundle_sha256,
            membership.recorded_at,
        )
        self._insert_or_verify(
            "search_fact_projection_memberships",
            columns,
            values,
            identity_columns=("membership_id",),
            identity_values=(membership.membership_id,),
        )

    def _insert_fact_hit(
        self,
        hit: FactHit,
        *,
        recorded_at: datetime,
    ) -> None:
        reported = hit.provenance if isinstance(hit.provenance, ReportedFactEvidence) else None
        derived = hit.provenance if isinstance(hit.provenance, DerivedFactLineage) else None
        columns = (
            "fact_hit_id",
            "idempotency_key",
            "projection_run_id",
            "fact_cell_id",
            "resolution_revision_id",
            "observation_id",
            "reporting_entity_id",
            "scope_security_id",
            "concept_namespace",
            "concept_name",
            "taxonomy_name",
            "taxonomy_version",
            "accounting_basis",
            "consolidation_scope",
            "period_kind",
            "period_start",
            "period_end",
            "fiscal_year",
            "fiscal_period",
            "canonical_dimensions_json",
            "canonical_dimensions_sha256",
            "unit_key",
            "currency",
            "observation_kind",
            "value_kind",
            "numeric_value",
            "text_value",
            "is_nil",
            "raw_lexical_value",
            "candidate_set_id",
            "candidate_count",
            "candidate_set_digest_sha256",
            "document_version_id",
            "evidence_node_id",
            "source_locator_json",
            "source_locator_sha256",
            "source_entry_sha256",
            "legacy_match_revision_id",
            "derivation_seal_id",
            "derivation_input_count",
            "derivation_input_digest_sha256",
            "cell_knowledge_at",
            "observation_knowledge_at",
            "resolution_knowledge_at",
            "row_bundle_json",
            "row_bundle_sha256",
            "recorded_at",
        )
        row_bundle_json = _canonical_json(hit)
        values = (
            hit.fact_hit_id,
            f"fact-hit:{hit.projection_run_id}:{hit.fact_cell_id}",
            hit.projection_run_id,
            hit.fact_cell_id,
            hit.resolution_revision_id,
            hit.observation_id,
            hit.reporting_entity_id,
            hit.scope_security_id,
            hit.concept_namespace,
            hit.concept_name,
            hit.taxonomy_name,
            hit.taxonomy_version,
            hit.accounting_basis,
            hit.consolidation_scope,
            hit.period_kind,
            hit.period_start,
            hit.period_end,
            hit.fiscal_year,
            hit.fiscal_period,
            _canonical_json([dimension.model_dump(mode="json") for dimension in hit.dimensions]),
            hit.dimensions_sha256,
            hit.unit_key,
            hit.currency,
            hit.observation_kind,
            hit.value_kind,
            _decimal_text(hit.numeric_value),
            hit.text_value,
            hit.is_nil,
            hit.raw_lexical_value,
            hit.candidate_set_id,
            hit.candidate_count,
            hit.candidate_set_digest_sha256,
            None if reported is None else reported.document_version_id,
            None if reported is None else reported.evidence_node_id,
            None if reported is None else _canonical_json(reported.source_locator),
            None if reported is None else reported.source_locator_sha256,
            None if reported is None else reported.source_entry_sha256,
            None if reported is None else reported.legacy_match_revision_id,
            None if derived is None else derived.derivation_seal_id,
            None if derived is None else len(derived.inputs),
            None if derived is None else derived.canonical_input_digest_sha256,
            hit.cell_knowledge_at,
            hit.observation_knowledge_at,
            hit.resolution_knowledge_at,
            row_bundle_json,
            hit.row_sha256,
            recorded_at,
        )
        self._insert_or_verify(
            "search_fact_projection_rows",
            columns,
            values,
            identity_columns=("fact_hit_id",),
            identity_values=(hit.fact_hit_id,),
        )

    def _insert_seal(self, seal: FactProjectionSeal) -> None:
        columns = (
            "projection_seal_id",
            "idempotency_key",
            "projection_run_id",
            "manifest_id",
            "eligible_fact_cell_count",
            "membership_count",
            "included_count",
            "unresolved_material_count",
            "missing_provenance_count",
            "quarantined_count",
            "row_count",
            "membership_set_sha256",
            "row_set_sha256",
            "config_sha256",
            "sealed_at",
        )
        values = (
            seal.projection_seal_id,
            seal.idempotency_key,
            seal.projection_run_id,
            seal.manifest_id,
            seal.eligible_fact_cell_count,
            seal.membership_count,
            seal.included_count,
            seal.unresolved_material_count,
            seal.missing_provenance_count,
            seal.quarantined_count,
            seal.row_count,
            seal.membership_set_sha256,
            seal.row_set_sha256,
            seal.config_sha256,
            seal.sealed_at,
        )
        self._insert_or_verify(
            "search_fact_projection_seals",
            columns,
            values,
            identity_columns=("idempotency_key",),
            identity_values=(seal.idempotency_key,),
        )

    def _verify_projection_replay(
        self,
        row: dict[str, object],
        spec: FactProjectionSpec,
    ) -> None:
        stored = self._spec_from_row(row)
        if stored != spec:
            raise FactProjectionError("projection idempotency key conflicts with existing run")

    def _verify_loaded_seal(
        self,
        projection: FactProjectionSpec,
        memberships: Sequence[FactProjectionMembership],
        hits: Sequence[FactHit],
        seal: FactProjectionSeal,
    ) -> None:
        expected = self._make_seal(projection, memberships, hits)
        comparable = (
            "projection_run_id",
            "manifest_id",
            "eligible_fact_cell_count",
            "membership_count",
            "included_count",
            "unresolved_material_count",
            "missing_provenance_count",
            "quarantined_count",
            "row_count",
            "membership_set_sha256",
            "row_set_sha256",
            "config_sha256",
        )
        if any(getattr(seal, field) != getattr(expected, field) for field in comparable):
            raise FactProjectionError(
                "stored projection seal does not match exact memberships and rows"
            )

    @staticmethod
    def _add_in_filter(
        clauses: list[str],
        parameters: list[object],
        column: str,
        values: Sequence[str],
    ) -> None:
        if not values:
            return
        clauses.append(f"{column} IN ({','.join('?' for _ in values)})")
        parameters.extend(values)

    @staticmethod
    def _optional_text(value: object) -> str | None:
        return None if value is None else str(value)

    @contextmanager
    def _savepoint(self, name: str) -> Generator[None, None, None]:
        self._conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except Exception:
            self._conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            self._conn.execute(f"RELEASE SAVEPOINT {name}")
            raise
        self._conn.execute(f"RELEASE SAVEPOINT {name}")

    def _insert_or_verify(
        self,
        table: str,
        columns: tuple[str, ...],
        values: tuple[object, ...],
        *,
        identity_columns: tuple[str, ...],
        identity_values: tuple[object, ...],
    ) -> None:
        cursor = self._conn.execute(
            f"INSERT INTO {table} ({','.join(columns)}) VALUES "  # nosec B608 -- trusted internal SQL shape; values remain bound
            f"({','.join('?' for _ in columns)}) ON CONFLICT DO NOTHING",
            values,
        )
        if cursor.rowcount == 1:
            return
        where = " AND ".join(f"{column} = ?" for column in identity_columns)
        row = self._fetchone(
            f"SELECT {','.join(columns)} FROM {table} WHERE {where}",  # nosec B608 -- trusted internal SQL shape; values remain bound
            identity_values,
        )
        if row is None:
            raise FactProjectionError(f"immutable {table} insert conflicted without exact identity")
        existing = tuple(row[column] for column in columns)
        if not self._values_equal(existing, values):
            raise FactProjectionError(f"immutable {table} identity conflicts with existing data")

    @staticmethod
    def _values_equal(
        stored: Sequence[object],
        expected: Sequence[object],
    ) -> bool:
        for left, right in zip(stored, expected, strict=True):
            if isinstance(right, datetime):
                try:
                    if _as_utc(_parse_datetime(left)) != _as_utc(right):
                        return False
                except ValueError:
                    return False
            elif isinstance(right, bool):
                if bool(left) is not right:
                    return False
            elif left != right:
                return False
        return True

    def _fetchone(
        self,
        statement: str,
        parameters: tuple[object, ...],
    ) -> dict[str, object] | None:
        cursor = self._conn.execute(statement, parameters)
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            description[0]: value
            for description, value in zip(
                cursor.description or (),
                row,
                strict=True,
            )
        }

    def _fetchall(
        self,
        statement: str,
        parameters: tuple[object, ...],
    ) -> list[dict[str, object]]:
        cursor = self._conn.execute(statement, parameters)
        columns = tuple(description[0] for description in cursor.description or ())
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    @staticmethod
    def _fact_hit_from_bundle(bundle_json: str) -> FactHit:
        return FactHit.model_validate_json(bundle_json)

    @staticmethod
    def _spec_from_row(row: dict[str, object]) -> FactProjectionSpec:
        return FactProjectionSpec(
            projection_run_id=str(row["projection_run_id"]),
            idempotency_key=str(row["idempotency_key"]),
            projection_key=str(row["projection_key"]),
            revision=_as_int(row["revision"]),
            manifest_id=str(row["manifest_id"]),
            knowledge_cutoff=_parse_datetime(row["knowledge_cutoff"]),
            config_sha256=str(row["config_sha256"]),
            code_version=str(row["code_version"]),
            supersedes_projection_run_id=(
                None
                if row["supersedes_projection_run_id"] is None
                else str(row["supersedes_projection_run_id"])
            ),
            recorded_at=_parse_datetime(row["recorded_at"]),
        )

    @staticmethod
    def _membership_from_row(
        row: dict[str, object],
    ) -> FactProjectionMembership:
        reason_details = _json_object(str(row["reason_details_json"])) or {}
        return FactProjectionMembership.model_validate(
            dict(
                membership_id=str(row["membership_id"]),
                projection_run_id=str(row["projection_run_id"]),
                fact_cell_id=str(row["fact_cell_id"]),
                disposition=str(row["disposition"]),
                resolution_revision_id=(
                    None
                    if row["resolution_revision_id"] is None
                    else str(row["resolution_revision_id"])
                ),
                reason_code=str(row["reason_code"]),
                reason_details=reason_details,
                membership_bundle_sha256=str(row["membership_bundle_sha256"]),
                recorded_at=_parse_datetime(row["recorded_at"]),
            )
        )

    @staticmethod
    def _seal_from_row(row: dict[str, object]) -> FactProjectionSeal:
        return FactProjectionSeal(
            projection_seal_id=str(row["projection_seal_id"]),
            idempotency_key=str(row["idempotency_key"]),
            projection_run_id=str(row["projection_run_id"]),
            manifest_id=str(row["manifest_id"]),
            eligible_fact_cell_count=_as_int(row["eligible_fact_cell_count"]),
            membership_count=_as_int(row["membership_count"]),
            included_count=_as_int(row["included_count"]),
            unresolved_material_count=_as_int(row["unresolved_material_count"]),
            missing_provenance_count=_as_int(row["missing_provenance_count"]),
            quarantined_count=_as_int(row["quarantined_count"]),
            row_count=_as_int(row["row_count"]),
            membership_set_sha256=str(row["membership_set_sha256"]),
            row_set_sha256=str(row["row_set_sha256"]),
            config_sha256=str(row["config_sha256"]),
            sealed_at=_parse_datetime(row["sealed_at"]),
        )


__all__ = [
    "DerivationInput",
    "DerivedFactLineage",
    "DocumentHit",
    "FactDimension",
    "FactHit",
    "FactProjectionError",
    "FactProjectionMembership",
    "FactProjectionResult",
    "FactProjectionSeal",
    "FactProjectionSpec",
    "FactSearchFilter",
    "FactSearchProjectionStore",
    "GroundedHit",
    "RankedGroundedHit",
    "ReportedFactEvidence",
]
