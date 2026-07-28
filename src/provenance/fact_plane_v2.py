"""Evidence-first, entity-keyed fact plane.

This module is the only application write/read boundary for the v2 fact
schema.  Contracts are closed and immutable, semantic identity never depends
on a ticker, and multi-row writes are protected by SQLite savepoints so they
remain safe when called inside an existing transaction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    TypeAdapter,
    field_validator,
    model_validator,
)

PeriodKind = Literal["instant", "duration"]
RelationKind = Literal[
    "exact_duplicate_of",
    "amends",
    "reissues",
    "presentation_recast_of",
    "conflicts_with",
    "supersedes_for_as_known",
]
ResolutionStatus = Literal["resolved", "unresolved", "retired"]
ValueKind = Literal["numeric", "text", "nil"]
AccountingBasis = Literal[
    "us_gaap",
    "ifrs",
    "local_gaap",
    "management",
    "non_gaap",
    "other",
]
ConsolidationScope = Literal[
    "consolidated",
    "parent_only",
    "subsidiary",
    "segment",
    "security_specific",
    "other",
]
FiscalPeriod = Literal[
    "FY",
    "Q1",
    "Q2",
    "Q3",
    "Q4",
    "H1",
    "H2",
    "YTD",
    "TTM",
    "OTHER",
]
RevisionKind = Literal[
    "initial",
    "amendment",
    "reissue",
    "restatement",
    "correction",
    "presentation_recast",
]
CandidateEligibility = Literal["eligible", "ineligible"]
_SHA256_LENGTH = 64


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
    return hashlib.sha256(value.encode()).hexdigest()


def _sql_sha256(value: object) -> str:
    return _digest(str(value))


def _validate_sha256(value: str) -> str:
    if len(value) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError("must be a lowercase SHA-256 hex digest")
    return value


def _canonical_decimal(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("numeric_value must be a finite decimal") from exc
    if not parsed.is_finite():
        raise ValueError("numeric_value must be a finite decimal")
    normalized = format(parsed, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return "0" if normalized in {"", "-0"} else normalized


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _same_time(left: object, right: datetime) -> bool:
    try:
        return _utc(datetime.fromisoformat(str(left))) == _utc(right)
    except ValueError:
        return False


def _canonical_time_text(value: object) -> str:
    return datetime.fromisoformat(str(value)).isoformat()


def _sql_semantic_identity_v3(*values: object) -> str:
    (
        reporting_entity_id,
        scope_security_id,
        concept_namespace,
        concept_name,
        taxonomy_name,
        accounting_basis,
        consolidation_scope,
        period_kind,
        period_start,
        period_end,
        dimension_set_json,
        unit_key,
        currency,
    ) = values
    return _canonical_json(
        {
            "accounting_basis": accounting_basis,
            "consolidation_scope": consolidation_scope,
            "currency": currency,
            "dimensions": json.loads(str(dimension_set_json)),
            "period_end": _canonical_time_text(period_end),
            "period_kind": period_kind,
            "period_start": (None if period_start is None else _canonical_time_text(period_start)),
            "concept_name": concept_name,
            "concept_namespace": concept_namespace,
            "reporting_entity_id": reporting_entity_id,
            "scope_security_id": scope_security_id,
            "semantic_key_version": "fact_cell_semantic_key.v3",
            "taxonomy_name": taxonomy_name,
            "unit_key": unit_key,
        }
    )


def _sql_anchor_payload_v1(*values: object) -> str:
    keys = (
        "document_version_id",
        "evidence_node_id",
        "extraction_input_sha256",
        "extraction_output_sha256",
        "extraction_run_id",
        "extractor_code_version",
        "extractor_config_sha256",
        "extractor_name",
        "raw_entry_sha256",
        "source_locator_sha256",
        "source_taxonomy_version",
        "subject_binding_revision_id",
    )
    return _canonical_json(dict(zip(keys, values, strict=True)))


def _sql_observation_payload_v1(*values: object) -> str:
    (
        observation_kind,
        effective_at,
        cell_semantic_sha,
        is_nil,
        knowledge_at,
        method_config_sha,
        method_name,
        method_version,
        numeric_value,
        precision,
        decimals,
        provenance_json,
        raw_lexical_value,
        recorded_at,
        revision_kind,
        supersedes_observation_id,
        text_value,
        value_kind,
    ) = values
    return _canonical_json(
        {
            "decimals": decimals,
            "effective_at": _canonical_time_text(effective_at),
            "fact_cell_semantic_key_sha256": cell_semantic_sha,
            "is_nil": bool(is_nil),
            "knowledge_at": _canonical_time_text(knowledge_at),
            "method_config_sha256": method_config_sha,
            "method_name": method_name,
            "method_version": method_version,
            "numeric_value": numeric_value,
            "observation_kind": observation_kind,
            "payload_version": "fact_observation_payload.v1",
            "precision": precision,
            "provenance": json.loads(str(provenance_json)),
            "raw_lexical_value": raw_lexical_value,
            "recorded_at": _canonical_time_text(recorded_at),
            "revision_kind": revision_kind,
            "supersedes_observation_id": supersedes_observation_id,
            "text_value": text_value,
            "value_kind": value_kind,
        }
    )


def _sql_derivation_basis_v1(*values: object) -> str:
    (
        canonical_input_digest_sha256,
        execution_config_sha256,
        formula_definition_sha256,
        formula_id,
        formula_version,
        input_basis,
        knowledge_cutoff,
    ) = values
    return _canonical_json(
        {
            "canonical_input_digest_sha256": canonical_input_digest_sha256,
            "execution_config_sha256": execution_config_sha256,
            "formula_definition_sha256": formula_definition_sha256,
            "formula_id": formula_id,
            "formula_version": formula_version,
            "input_basis": input_basis,
            "knowledge_cutoff": _canonical_time_text(knowledge_cutoff),
        }
    )


class CanonicalJSONObject(RootModel[dict[str, JsonValue]]):
    """A closed JSON object with deterministic persistence bytes."""

    model_config = ConfigDict(frozen=True)

    @property
    def canonical_json(self) -> str:
        return _canonical_json(self.root)

    @property
    def canonical_sha256(self) -> str:
        return _digest(self.canonical_json)


class FactDimensionV2(_FrozenModel):
    """One normalized XBRL dimension, preserving typed-vs-explicit identity."""

    dimension_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    axis_namespace: str = Field(min_length=1)
    axis_name: str = Field(min_length=1)
    member_kind: Literal["explicit", "typed"]
    explicit_member_namespace: str | None = Field(default=None, min_length=1)
    explicit_member_name: str | None = Field(default=None, min_length=1)
    typed_member_value: CanonicalJSONObject | None = None
    recorded_at: datetime

    @model_validator(mode="after")
    def _member_shape(self) -> Self:
        if self.member_kind == "explicit":
            if (
                self.explicit_member_namespace is None
                or self.explicit_member_name is None
                or self.typed_member_value is not None
            ):
                raise ValueError("explicit dimensions require only an explicit member QName")
        elif (
            self.explicit_member_namespace is not None
            or self.explicit_member_name is not None
            or self.typed_member_value is None
        ):
            raise ValueError("typed dimensions require only canonical typed-member content")
        return self

    @property
    def canonical_member(self) -> dict[str, JsonValue]:
        return {
            "axis_name": self.axis_name,
            "axis_namespace": self.axis_namespace,
            "explicit_member_name": self.explicit_member_name,
            "explicit_member_namespace": self.explicit_member_namespace,
            "member_kind": self.member_kind,
            "typed_member_value": (
                None if self.typed_member_value is None else self.typed_member_value.root
            ),
        }


class FactCellV2(_FrozenModel):
    """One semantic fact coordinate keyed by reporting identity, never ticker."""

    fact_cell_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    reporting_entity_id: str = Field(min_length=1, max_length=128)
    scope_security_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    semantic_key_version: Literal["fact_cell_semantic_key.v3"] = "fact_cell_semantic_key.v3"
    semantic_key_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
    )
    concept_namespace: str = Field(min_length=1)
    concept_name: str = Field(min_length=1)
    taxonomy_name: str = Field(min_length=1)
    taxonomy_version: str | None = Field(default=None, min_length=1)
    accounting_basis: AccountingBasis
    consolidation_scope: ConsolidationScope
    period_kind: PeriodKind
    period_start: datetime | None = None
    period_end: datetime
    fiscal_year: int | None = Field(default=None, ge=1, le=9999)
    fiscal_period: FiscalPeriod | None = None
    dimensions: tuple[FactDimensionV2, ...] = ()
    unit_key: str = Field(min_length=1)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    effective_at: datetime
    knowledge_at: datetime
    recorded_at: datetime

    @field_validator("dimensions")
    @classmethod
    def _canonical_dimensions(
        cls,
        value: tuple[FactDimensionV2, ...],
    ) -> tuple[FactDimensionV2, ...]:
        identities = tuple((item.axis_namespace, item.axis_name) for item in value)
        if len(identities) != len(set(identities)):
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

    @field_validator("currency")
    @classmethod
    def _currency_upper(cls, value: str | None) -> str | None:
        return None if value is None else value.upper()

    @model_validator(mode="after")
    def _period_and_key(self) -> Self:
        if self.period_kind == "instant" and self.period_start is not None:
            raise ValueError("instant fact cells cannot have period_start")
        if self.period_kind == "duration" and self.period_start is None:
            raise ValueError("duration fact cells require period_start")
        if self.period_start is not None and self.period_end < self.period_start:
            raise ValueError("period_end must not precede period_start")
        effective = _utc(self.effective_at)
        knowledge = _utc(self.knowledge_at)
        recorded = _utc(self.recorded_at)
        if knowledge < effective or recorded < knowledge:
            raise ValueError("fact-cell clocks are inconsistent")
        expected = self.derive_semantic_key()
        if self.semantic_key_sha256 is None:
            object.__setattr__(self, "semantic_key_sha256", expected)
        elif _validate_sha256(self.semantic_key_sha256) != expected:
            raise ValueError("semantic_key_sha256 must match the canonical fact-cell identity")
        return self

    @property
    def dimensions_json(self) -> str:
        return _canonical_json([dimension.canonical_member for dimension in self.dimensions])

    @property
    def dimensions_sha256(self) -> str:
        return _digest(self.dimensions_json)

    @property
    def semantic_identity_json(self) -> str:
        """Canonical v3 coordinate; source taxonomy version is not identity."""
        return _canonical_json(
            {
                "accounting_basis": self.accounting_basis,
                "consolidation_scope": self.consolidation_scope,
                "currency": self.currency,
                "dimensions": [dimension.canonical_member for dimension in self.dimensions],
                "period_end": self.period_end.isoformat(),
                "period_kind": self.period_kind,
                "period_start": (
                    None if self.period_start is None else self.period_start.isoformat()
                ),
                "concept_name": self.concept_name,
                "concept_namespace": self.concept_namespace,
                "reporting_entity_id": self.reporting_entity_id,
                "scope_security_id": self.scope_security_id,
                "semantic_key_version": self.semantic_key_version,
                "taxonomy_name": self.taxonomy_name,
                "unit_key": self.unit_key,
            }
        )

    def derive_semantic_key(self) -> str:
        """Hash the complete semantic coordinate, excluding storage identity."""
        return _digest(self.semantic_identity_json)


class _ObservationBase(_FrozenModel):
    observation_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    fact_cell_id: str = Field(min_length=1, max_length=128)
    value_kind: ValueKind
    numeric_value: str | None = None
    text_value: str | None = Field(default=None, min_length=1)
    is_nil: bool = False
    raw_lexical_value: str | None = None
    method_name: str = Field(min_length=1, max_length=128)
    method_version: str = Field(min_length=1, max_length=64)
    method_config_sha256: str
    revision_kind: RevisionKind
    supersedes_observation_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    effective_at: datetime
    knowledge_at: datetime
    recorded_at: datetime

    _numeric = field_validator("numeric_value")(_canonical_decimal)
    _method_sha = field_validator("method_config_sha256")(_validate_sha256)

    @model_validator(mode="after")
    def _value_and_clocks(self) -> Self:
        if self.value_kind == "numeric":
            if self.numeric_value is None or self.text_value is not None or self.is_nil:
                raise ValueError("numeric observations require only numeric_value")
        elif self.value_kind == "text" and (
            self.text_value is None or self.numeric_value is not None or self.is_nil
        ):
            raise ValueError("text observations require only text_value")
        elif self.value_kind == "nil" and (
            self.numeric_value is not None or self.text_value is not None or not self.is_nil
        ):
            raise ValueError("nil observations cannot carry a parsed value")
        if (self.revision_kind == "initial") != (self.supersedes_observation_id is None):
            raise ValueError("observation revision parent is inconsistent")
        effective = _utc(self.effective_at)
        knowledge = _utc(self.knowledge_at)
        recorded = _utc(self.recorded_at)
        if knowledge < effective or recorded < knowledge:
            raise ValueError("observation clocks are inconsistent")
        return self


class ReportedFactObservationV2(_ObservationBase):
    observation_kind: Literal["reported"]
    document_version_id: str = Field(min_length=1, max_length=128)
    evidence_node_id: str = Field(min_length=1, max_length=128)
    source_locator: CanonicalJSONObject
    source_locator_sha256: str | None = None
    source_entry_sha256: str
    subject_binding_revision_id: str = Field(min_length=1, max_length=128)
    source_taxonomy_version: str = Field(min_length=1)
    source_context_id: str | None = Field(default=None, min_length=1)
    source_unit_id: str | None = Field(default=None, min_length=1)
    decimals: str | None = Field(default=None, min_length=1)
    precision: str | None = Field(default=None, min_length=1)
    legacy_match_revision_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )

    _entry_sha = field_validator("source_entry_sha256")(_validate_sha256)

    @model_validator(mode="after")
    def _locator_digest(self) -> Self:
        expected = self.source_locator.canonical_sha256
        if self.source_locator_sha256 is None:
            object.__setattr__(self, "source_locator_sha256", expected)
        elif _validate_sha256(self.source_locator_sha256) != expected:
            raise ValueError("source_locator_sha256 must match canonical locator JSON")
        return self


class DerivedFactObservationV2(_ObservationBase):
    observation_kind: Literal["derived"]
    formula_id: str = Field(min_length=1, max_length=128)
    formula_version: str = Field(min_length=1, max_length=128)


FactObservationV2 = Annotated[
    ReportedFactObservationV2 | DerivedFactObservationV2,
    Field(discriminator="observation_kind"),
]
_OBSERVATION_ADAPTER: TypeAdapter[FactObservationV2] = TypeAdapter(FactObservationV2)


class ObservationRelationV2(_FrozenModel):
    relation_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    subject_observation_id: str = Field(min_length=1, max_length=128)
    object_observation_id: str = Field(min_length=1, max_length=128)
    relation_kind: RelationKind
    reason_code: str = Field(min_length=1, max_length=128)
    reason_details: CanonicalJSONObject
    policy_name: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=64)
    policy_config_sha256: str
    effective_at: datetime
    knowledge_at: datetime
    recorded_at: datetime

    _policy_sha = field_validator("policy_config_sha256")(_validate_sha256)

    @model_validator(mode="after")
    def _not_reflexive(self) -> Self:
        if self.subject_observation_id == self.object_observation_id:
            raise ValueError("observation relations cannot be reflexive")
        if _utc(self.knowledge_at) < _utc(self.effective_at) or _utc(self.recorded_at) < _utc(
            self.knowledge_at
        ):
            raise ValueError("observation relation clocks are inconsistent")
        return self


class DerivationInputV2(_FrozenModel):
    edge_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    derived_observation_id: str = Field(min_length=1, max_length=128)
    input_position: int = Field(ge=0)
    input_observation_id: str = Field(min_length=1, max_length=128)
    input_resolution_revision_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    input_role: str = Field(min_length=1, max_length=128)
    recorded_at: datetime

    @property
    def canonical_json(self) -> str:
        return _canonical_json(self.model_dump(mode="json"))


class DerivationSealV2(_FrozenModel):
    derivation_seal_id: str = Field(min_length=1, max_length=128)
    derived_observation_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    ordered_inputs: tuple[DerivationInputV2, ...] = Field(min_length=1)
    ordered_inputs_sha256: str | None = None
    input_basis: Literal["as_reported", "as_known"]
    formula_definition_sha256: str
    formula_config_sha256: str
    seal_method: str = Field(min_length=1, max_length=128)
    seal_method_version: str = Field(min_length=1, max_length=64)
    effective_at: datetime
    knowledge_at: datetime
    recorded_at: datetime

    _formula_sha = field_validator("formula_config_sha256")(_validate_sha256)
    _formula_definition_sha = field_validator("formula_definition_sha256")(_validate_sha256)

    @model_validator(mode="after")
    def _canonical_inputs_and_digest(self) -> Self:
        positions = tuple(item.input_position for item in self.ordered_inputs)
        if positions != tuple(range(len(self.ordered_inputs))):
            raise ValueError("derivation inputs must use contiguous canonical positions")
        if any(
            item.derived_observation_id != self.derived_observation_id
            for item in self.ordered_inputs
        ):
            raise ValueError("all derivation inputs must belong to the sealed observation")
        input_ids = tuple(item.input_observation_id for item in self.ordered_inputs)
        if len(input_ids) != len(set(input_ids)):
            raise ValueError("a derivation input observation cannot be repeated")
        expected = self.canonical_inputs_sha256
        if self.ordered_inputs_sha256 is None:
            object.__setattr__(self, "ordered_inputs_sha256", expected)
        elif _validate_sha256(self.ordered_inputs_sha256) != expected:
            raise ValueError("ordered_inputs_sha256 must match ordered derivation inputs")
        if _utc(self.knowledge_at) < _utc(self.effective_at) or _utc(self.recorded_at) < _utc(
            self.knowledge_at
        ):
            raise ValueError("derivation seal clocks are inconsistent")
        return self

    @property
    def canonical_inputs_json(self) -> str:
        return _canonical_json(
            [
                {
                    "input_observation_id": item.input_observation_id,
                    "input_ordinal": item.input_position,
                    "input_resolution_revision_id": (item.input_resolution_revision_id),
                    "input_role": item.input_role,
                    "output_observation_id": item.derived_observation_id,
                }
                for item in self.ordered_inputs
            ]
        )

    @property
    def canonical_inputs_sha256(self) -> str:
        return _digest(self.canonical_inputs_json)


class FactResolutionCandidateV2(_FrozenModel):
    candidate_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    candidate_set_id: str = Field(min_length=1, max_length=128)
    fact_cell_id: str = Field(min_length=1, max_length=128)
    observation_id: str = Field(min_length=1, max_length=128)
    candidate_ordinal: int = Field(ge=0)
    eligibility: CandidateEligibility
    reason_code: str = Field(min_length=1, max_length=128)
    reason_details: CanonicalJSONObject
    candidate_payload_sha256: str | None = None
    recorded_at: datetime

    @field_validator("candidate_payload_sha256")
    @classmethod
    def _payload_sha(cls, value: str | None) -> str | None:
        return None if value is None else _validate_sha256(value)

    @property
    def digest_payload(self) -> dict[str, JsonValue]:
        if self.candidate_payload_sha256 is None:
            raise ValueError("candidate payload is assigned by FactPlaneV2 during persistence")
        return {
            "candidate_ordinal": self.candidate_ordinal,
            "candidate_payload_sha256": self.candidate_payload_sha256,
            "eligibility": self.eligibility,
            "observation_id": self.observation_id,
        }


class FactResolutionRevisionV2(_FrozenModel):
    resolution_revision_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    fact_cell_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(gt=0)
    status: ResolutionStatus
    candidate_set_id: str = Field(min_length=1, max_length=128)
    candidates: tuple[FactResolutionCandidateV2, ...]
    selected_observation_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    candidate_set_digest_sha256: str | None = None
    policy_name: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=128)
    policy_config_sha256: str
    reason_code: str = Field(min_length=1, max_length=128)
    reason_details: CanonicalJSONObject
    knowledge_cutoff: datetime
    effective_at: datetime
    recorded_at: datetime
    supersedes_resolution_revision_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )

    _policy_sha = field_validator("policy_config_sha256")(_validate_sha256)

    @field_validator("candidates")
    @classmethod
    def _canonical_candidates(
        cls,
        value: tuple[FactResolutionCandidateV2, ...],
    ) -> tuple[FactResolutionCandidateV2, ...]:
        observation_ids = tuple(item.observation_id for item in value)
        if len(observation_ids) != len(set(observation_ids)):
            raise ValueError("resolution candidates cannot repeat")
        if tuple(item.candidate_ordinal for item in value) != tuple(range(len(value))):
            raise ValueError("resolution candidates require contiguous canonical ordinals")
        return value

    @model_validator(mode="after")
    def _resolution_invariants(self) -> Self:
        if self.status == "resolved":
            selected = tuple(
                item
                for item in self.candidates
                if item.observation_id == self.selected_observation_id
            )
            if len(selected) != 1 or selected[0].eligibility != "eligible":
                raise ValueError("resolved revision must select an exact candidate")
        elif self.selected_observation_id is not None:
            raise ValueError("unresolved or retired revision cannot select a value")
        if (self.revision == 1) != (self.supersedes_resolution_revision_id is None):
            raise ValueError("resolution revision chain is incomplete")
        if any(
            candidate.candidate_set_id != self.candidate_set_id
            or candidate.fact_cell_id != self.fact_cell_id
            for candidate in self.candidates
        ):
            raise ValueError("resolution candidates must use the exact cell and set")
        if all(candidate.candidate_payload_sha256 is not None for candidate in self.candidates):
            expected = self.canonical_candidate_set_sha256
            if self.candidate_set_digest_sha256 is None:
                object.__setattr__(self, "candidate_set_digest_sha256", expected)
            elif _validate_sha256(self.candidate_set_digest_sha256) != expected:
                raise ValueError("candidate_set_digest_sha256 must match candidates")
        elif self.candidate_set_digest_sha256 is not None:
            raise ValueError("candidate-set digest cannot precede internal payload commitments")
        if _utc(self.recorded_at) < _utc(self.knowledge_cutoff):
            raise ValueError("recorded_at must not precede knowledge_cutoff")
        return self

    @property
    def candidate_set_json(self) -> str:
        return _canonical_json([candidate.digest_payload for candidate in self.candidates])

    @property
    def canonical_candidate_set_sha256(self) -> str:
        return _digest(self.candidate_set_json)


class FactAsReportedV2(_FrozenModel):
    cell: FactCellV2
    observations: tuple[ReportedFactObservationV2, ...]


class FactAsKnownV2(_FrozenModel):
    cell: FactCellV2
    cutoff: datetime
    resolution: FactResolutionRevisionV2 | None
    candidates: tuple[FactObservationV2, ...]
    canonical_observation: FactObservationV2 | None


@dataclass(frozen=True, slots=True)
class PersistResult:
    record_id: str
    created: bool


class ExtractionRunCompletenessSealV2(_FrozenModel):
    extraction_seal_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    extraction_run_id: str = Field(min_length=1, max_length=128)
    expected_node_count: int = Field(ge=0)
    completeness_policy_name: str = Field(min_length=1, max_length=128)
    completeness_policy_version: str = Field(min_length=1, max_length=64)
    completeness_policy_sha256: str
    knowledge_at: datetime
    recorded_at: datetime

    _policy_sha = field_validator("completeness_policy_sha256")(_validate_sha256)

    @model_validator(mode="after")
    def _clocks(self) -> Self:
        if _utc(self.recorded_at) < _utc(self.knowledge_at):
            raise ValueError("extraction seal clocks are inconsistent")
        return self


class FactPlaneV2:
    """Typed deep-module repository for the evidence-first fact schema."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.create_function(
            "fact_sha256",
            1,
            _sql_sha256,
            deterministic=True,
        )
        self._conn.create_function(
            "fact_cell_semantic_identity_v3",
            -1,
            _sql_semantic_identity_v3,
            deterministic=True,
        )
        self._conn.create_function(
            "fact_anchor_payload_v1",
            -1,
            _sql_anchor_payload_v1,
            deterministic=True,
        )
        self._conn.create_function(
            "fact_observation_payload_v1",
            -1,
            _sql_observation_payload_v1,
            deterministic=True,
        )
        self._conn.create_function(
            "fact_derivation_basis_v1",
            -1,
            _sql_derivation_basis_v1,
            deterministic=True,
        )

    def persist_cell(self, cell: FactCellV2) -> PersistResult:
        """Append a semantic cell or accept its exact idempotent replay."""
        columns = (
            "fact_cell_id",
            "idempotency_key",
            "reporting_entity_id",
            "scope_security_id",
            "semantic_key_version",
            "semantic_key_sha256",
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
            "effective_at",
            "knowledge_at",
            "recorded_at",
        )
        values = (
            cell.fact_cell_id,
            cell.idempotency_key,
            cell.reporting_entity_id,
            cell.scope_security_id,
            # 0238's physical envelope is retained for migration stability;
            # the authoritative semantic version lives in the 0240 seal.
            "fact_cell_semantic_key.v2",
            cell.semantic_key_sha256,
            cell.concept_namespace,
            cell.concept_name,
            cell.taxonomy_name,
            cell.taxonomy_version,
            cell.accounting_basis,
            cell.consolidation_scope,
            cell.period_kind,
            cell.period_start,
            cell.period_end,
            cell.fiscal_year,
            cell.fiscal_period,
            cell.dimensions_json,
            cell.dimensions_sha256,
            cell.unit_key,
            cell.currency,
            cell.effective_at,
            cell.knowledge_at,
            cell.recorded_at,
        )
        with self._savepoint("persist_fact_cell_v2"):
            result = self._insert_or_verify(
                table="fact_cells_v2",
                columns=columns,
                values=values,
                idempotency_key=cell.idempotency_key,
                record_id=cell.fact_cell_id,
            )
            for ordinal, dimension in enumerate(cell.dimensions):
                typed_json = (
                    None
                    if dimension.typed_member_value is None
                    else dimension.typed_member_value.canonical_json
                )
                self._insert_or_verify(
                    table="fact_dimensions_normalized_v2",
                    columns=(
                        "dimension_id",
                        "idempotency_key",
                        "fact_cell_id",
                        "dimension_ordinal",
                        "axis_namespace",
                        "axis_name",
                        "member_kind",
                        "explicit_member_namespace",
                        "explicit_member_name",
                        "typed_member_value_json",
                        "typed_member_value_sha256",
                        "recorded_at",
                    ),
                    values=(
                        dimension.dimension_id,
                        dimension.idempotency_key,
                        cell.fact_cell_id,
                        ordinal,
                        dimension.axis_namespace,
                        dimension.axis_name,
                        dimension.member_kind,
                        dimension.explicit_member_namespace,
                        dimension.explicit_member_name,
                        typed_json,
                        (None if typed_json is None else _digest(typed_json)),
                        dimension.recorded_at,
                    ),
                    idempotency_key=dimension.idempotency_key,
                    record_id=dimension.dimension_id,
                )
            self._insert_or_verify(
                table="fact_cell_identity_seals_v2",
                columns=(
                    "fact_cell_id",
                    "idempotency_key",
                    "semantic_key_version",
                    "semantic_identity_json",
                    "semantic_key_sha256",
                    "dimension_count",
                    "dimension_set_json",
                    "dimension_set_sha256",
                    "sealed_at",
                ),
                values=(
                    cell.fact_cell_id,
                    f"{cell.idempotency_key}:identity:v3",
                    cell.semantic_key_version,
                    cell.semantic_identity_json,
                    cell.semantic_key_sha256,
                    len(cell.dimensions),
                    cell.dimensions_json,
                    cell.dimensions_sha256,
                    cell.recorded_at,
                ),
                idempotency_key=f"{cell.idempotency_key}:identity:v3",
                record_id=cell.fact_cell_id,
            )
            return result

    def persist_observation(
        self,
        observation: FactObservationV2,
    ) -> PersistResult:
        """Append one evidence-anchored report or unsealed derivation."""
        validated = _OBSERVATION_ADAPTER.validate_python(observation)
        columns, values = self._observation_values(validated)
        with self._savepoint("persist_fact_observation_v2"):
            result = self._insert_or_verify(
                table="fact_observations_v2",
                columns=columns,
                values=values,
                idempotency_key=validated.idempotency_key,
                record_id=validated.observation_id,
            )
            anchor_sha: str | None = None
            if isinstance(validated, ReportedFactObservationV2):
                run = self._fetchone(
                    "SELECT run.* FROM evidence_nodes AS node "
                    "JOIN evidence_extraction_runs AS run "
                    "ON run.extraction_run_id = node.extraction_run_id "
                    "WHERE node.node_id = ?",
                    (validated.evidence_node_id,),
                )
                if run is None:
                    raise ValueError("reported observation extraction run is missing")
                anchor_json = _canonical_json(
                    {
                        "document_version_id": validated.document_version_id,
                        "evidence_node_id": validated.evidence_node_id,
                        "extraction_input_sha256": str(run["input_sha256"]),
                        "extraction_output_sha256": str(run["output_sha256"]),
                        "extraction_run_id": str(run["extraction_run_id"]),
                        "extractor_code_version": str(run["extractor_code_version"]),
                        "extractor_config_sha256": str(run["extractor_config_sha256"]),
                        "extractor_name": str(run["extractor_name"]),
                        "raw_entry_sha256": validated.source_entry_sha256,
                        "source_locator_sha256": validated.source_locator_sha256,
                        "source_taxonomy_version": (validated.source_taxonomy_version),
                        "subject_binding_revision_id": (validated.subject_binding_revision_id),
                    }
                )
                anchor_sha = _digest(anchor_json)
                self._insert_or_verify(
                    table="fact_reported_observation_anchors_v2",
                    columns=(
                        "observation_id",
                        "idempotency_key",
                        "subject_binding_revision_id",
                        "extraction_run_id",
                        "source_taxonomy_version",
                        "extractor_name",
                        "extractor_code_version",
                        "extractor_config_sha256",
                        "extraction_input_sha256",
                        "extraction_output_sha256",
                        "raw_entry_sha256",
                        "anchor_payload_json",
                        "anchor_payload_sha256",
                        "recorded_at",
                    ),
                    values=(
                        validated.observation_id,
                        f"{validated.idempotency_key}:anchor:v1",
                        validated.subject_binding_revision_id,
                        str(run["extraction_run_id"]),
                        validated.source_taxonomy_version,
                        str(run["extractor_name"]),
                        str(run["extractor_code_version"]),
                        str(run["extractor_config_sha256"]),
                        str(run["input_sha256"]),
                        str(run["output_sha256"]),
                        validated.source_entry_sha256,
                        anchor_json,
                        anchor_sha,
                        validated.recorded_at,
                    ),
                    idempotency_key=f"{validated.idempotency_key}:anchor:v1",
                    record_id=validated.observation_id,
                )
            if isinstance(validated, ReportedFactObservationV2):
                self._commit_observation_payload(
                    validated,
                    anchor_sha256=anchor_sha,
                    derivation_lineage=None,
                )
            return result

    def persist_relation(
        self,
        relation: ObservationRelationV2,
    ) -> PersistResult:
        """Append one explicit same-cell relationship."""
        columns = (
            "relation_id",
            "idempotency_key",
            "subject_observation_id",
            "object_observation_id",
            "relation_kind",
            "reason_code",
            "reason_details_json",
            "policy_name",
            "policy_version",
            "policy_config_sha256",
            "effective_at",
            "knowledge_at",
            "recorded_at",
        )
        values = (
            relation.relation_id,
            relation.idempotency_key,
            relation.subject_observation_id,
            relation.object_observation_id,
            relation.relation_kind,
            relation.reason_code,
            relation.reason_details.canonical_json,
            relation.policy_name,
            relation.policy_version,
            relation.policy_config_sha256,
            relation.effective_at,
            relation.knowledge_at,
            relation.recorded_at,
        )
        with self._savepoint("persist_fact_relation_v2"):
            return self._insert_or_verify(
                table="fact_observation_relations_v2",
                columns=columns,
                values=values,
                idempotency_key=relation.idempotency_key,
                record_id=relation.relation_id,
            )

    def finalize_derivation(self, seal: DerivationSealV2) -> PersistResult:
        """Atomically append the ordered input edges and their exact digest seal."""
        existing = self._fetchone(
            "SELECT derivation_seal_id FROM fact_derivation_seals_v2 WHERE idempotency_key = ?",
            (seal.idempotency_key,),
        )
        if existing is not None:
            result = self._verify_derivation_replay(seal)
            basis_sha = self._verify_derivation_basis_replay(seal)
            output_row = self._fetchone(
                "SELECT * FROM fact_observations_v2 WHERE observation_id = ?",
                (seal.derived_observation_id,),
            )
            if output_row is None:
                raise ValueError("derivation replay output is missing")
            output_observation = self._load_observation(output_row)
            self._commit_observation_payload(
                output_observation,
                anchor_sha256=None,
                derivation_lineage=(
                    seal.derivation_seal_id,
                    str(seal.ordered_inputs_sha256),
                    basis_sha,
                ),
            )
            return result
        with self._savepoint("finalize_fact_derivation_v2"):
            for edge in seal.ordered_inputs:
                columns = (
                    "edge_id",
                    "idempotency_key",
                    "output_observation_id",
                    "input_observation_id",
                    "input_resolution_revision_id",
                    "input_role",
                    "input_ordinal",
                    "recorded_at",
                )
                values = (
                    edge.edge_id,
                    edge.idempotency_key,
                    edge.derived_observation_id,
                    edge.input_observation_id,
                    edge.input_resolution_revision_id,
                    edge.input_role,
                    edge.input_position,
                    edge.recorded_at,
                )
                self._insert_or_verify(
                    table="fact_derivation_input_edges_v2",
                    columns=columns,
                    values=values,
                    idempotency_key=edge.idempotency_key,
                    record_id=edge.edge_id,
                )
            columns = (
                "derivation_seal_id",
                "idempotency_key",
                "output_observation_id",
                "input_count",
                "canonical_input_digest_sha256",
                "formula_config_sha256",
                "seal_method",
                "seal_method_version",
                "effective_at",
                "knowledge_at",
                "recorded_at",
            )
            values = (
                seal.derivation_seal_id,
                seal.idempotency_key,
                seal.derived_observation_id,
                len(seal.ordered_inputs),
                seal.ordered_inputs_sha256,
                seal.formula_config_sha256,
                seal.seal_method,
                seal.seal_method_version,
                seal.effective_at,
                seal.knowledge_at,
                seal.recorded_at,
            )
            result = self._insert_or_verify(
                table="fact_derivation_seals_v2",
                columns=columns,
                values=values,
                idempotency_key=seal.idempotency_key,
                record_id=seal.derivation_seal_id,
            )
            output = self._fetchone(
                "SELECT formula_id,formula_version FROM fact_observations_v2 "
                "WHERE observation_id = ? AND observation_kind = 'derived'",
                (seal.derived_observation_id,),
            )
            if output is None:
                raise ValueError("derivation seal output is not a derived observation")
            basis_json = _canonical_json(
                {
                    "canonical_input_digest_sha256": (seal.ordered_inputs_sha256),
                    "execution_config_sha256": seal.formula_config_sha256,
                    "formula_definition_sha256": (seal.formula_definition_sha256),
                    "formula_id": str(output["formula_id"]),
                    "formula_version": str(output["formula_version"]),
                    "input_basis": seal.input_basis,
                    "knowledge_cutoff": seal.knowledge_at.isoformat(),
                }
            )
            basis_sha = _digest(basis_json)
            self._insert_or_verify(
                table="fact_derivation_basis_commitments_v2",
                columns=(
                    "derivation_seal_id",
                    "idempotency_key",
                    "input_basis",
                    "formula_id",
                    "formula_version",
                    "formula_definition_sha256",
                    "execution_config_sha256",
                    "knowledge_cutoff",
                    "canonical_basis_json",
                    "canonical_basis_sha256",
                    "recorded_at",
                ),
                values=(
                    seal.derivation_seal_id,
                    f"{seal.idempotency_key}:basis:v1",
                    seal.input_basis,
                    str(output["formula_id"]),
                    str(output["formula_version"]),
                    seal.formula_definition_sha256,
                    seal.formula_config_sha256,
                    seal.knowledge_at,
                    basis_json,
                    basis_sha,
                    seal.recorded_at,
                ),
                idempotency_key=f"{seal.idempotency_key}:basis:v1",
                record_id=seal.derivation_seal_id,
            )
            output_row = self._fetchone(
                "SELECT * FROM fact_observations_v2 WHERE observation_id = ?",
                (seal.derived_observation_id,),
            )
            if output_row is None:
                raise ValueError("derived observation disappeared during sealing")
            output_observation = self._load_observation(output_row)
            if not isinstance(output_observation, DerivedFactObservationV2):
                raise ValueError("derivation output must remain derived")
            self._commit_observation_payload(
                output_observation,
                anchor_sha256=None,
                derivation_lineage=(
                    seal.derivation_seal_id,
                    str(seal.ordered_inputs_sha256),
                    basis_sha,
                ),
            )
            return result

    def seal_extraction_run(
        self,
        seal: ExtractionRunCompletenessSealV2,
    ) -> PersistResult:
        """Finalize one extraction run's exact node and reported-fact sets."""
        with self._savepoint("seal_fact_extraction_run_v2"):
            run = self._fetchone(
                "SELECT * FROM evidence_extraction_runs WHERE extraction_run_id = ?",
                (seal.extraction_run_id,),
            )
            if run is None or str(run["outcome"]) != "succeeded":
                raise ValueError("only succeeded extraction runs can be sealed")
            node_ids = tuple(
                str(row["node_id"])
                for row in self._fetchall(
                    "SELECT node_id FROM evidence_nodes "
                    "WHERE extraction_run_id = ? ORDER BY node_id",
                    (seal.extraction_run_id,),
                )
            )
            observation_ids = tuple(
                str(row["observation_id"])
                for row in self._fetchall(
                    "SELECT observation_id "
                    "FROM fact_reported_observation_anchors_v2 "
                    "WHERE extraction_run_id = ? ORDER BY observation_id",
                    (seal.extraction_run_id,),
                )
            )
            node_set_json = _canonical_json(list(node_ids))
            observation_set_json = _canonical_json(list(observation_ids))
            return self._insert_or_verify(
                table="fact_extraction_run_completeness_seals_v2",
                columns=(
                    "extraction_seal_id",
                    "idempotency_key",
                    "extraction_run_id",
                    "expected_node_count",
                    "observed_node_count",
                    "reported_fact_count",
                    "node_set_json",
                    "node_set_sha256",
                    "observation_set_json",
                    "observation_set_sha256",
                    "extractor_config_sha256",
                    "extraction_output_sha256",
                    "completeness_policy_name",
                    "completeness_policy_version",
                    "completeness_policy_sha256",
                    "knowledge_at",
                    "recorded_at",
                ),
                values=(
                    seal.extraction_seal_id,
                    seal.idempotency_key,
                    seal.extraction_run_id,
                    seal.expected_node_count,
                    len(node_ids),
                    len(observation_ids),
                    node_set_json,
                    _digest(node_set_json),
                    observation_set_json,
                    _digest(observation_set_json),
                    str(run["extractor_config_sha256"]),
                    str(run["output_sha256"]),
                    seal.completeness_policy_name,
                    seal.completeness_policy_version,
                    seal.completeness_policy_sha256,
                    seal.knowledge_at,
                    seal.recorded_at,
                ),
                idempotency_key=seal.idempotency_key,
                record_id=seal.extraction_seal_id,
            )

    def persist_resolution(
        self,
        resolution: FactResolutionRevisionV2,
    ) -> PersistResult:
        """Atomically stage a complete immutable candidate set and revision."""
        normalized_candidates: list[FactResolutionCandidateV2] = []
        for candidate in resolution.candidates:
            payload = self._fetchone(
                "SELECT observation_payload_sha256 "
                "FROM fact_observation_payload_commitments_v2 "
                "WHERE observation_id = ?",
                (candidate.observation_id,),
            )
            if payload is None:
                raise ValueError("resolution candidate has no committed observation payload")
            committed_sha = str(payload["observation_payload_sha256"])
            if (
                candidate.candidate_payload_sha256 is not None
                and candidate.candidate_payload_sha256 != committed_sha
            ):
                raise ValueError("caller candidate payload conflicts with internal commitment")
            normalized_candidates.append(
                candidate.model_copy(update={"candidate_payload_sha256": committed_sha})
            )
        normalized = FactResolutionRevisionV2.model_validate(
            {
                **resolution.model_dump(),
                "candidates": tuple(normalized_candidates),
                "candidate_set_digest_sha256": None,
            }
        )
        existing = self._fetchone(
            "SELECT resolution_revision_id FROM fact_resolution_revisions_v2 "
            "WHERE idempotency_key = ?",
            (normalized.idempotency_key,),
        )
        if existing is not None:
            return self._verify_resolution_replay(normalized)
        with self._savepoint("persist_fact_resolution_v2"):
            for candidate in normalized.candidates:
                columns = (
                    "candidate_id",
                    "idempotency_key",
                    "candidate_set_id",
                    "fact_cell_id",
                    "observation_id",
                    "candidate_ordinal",
                    "eligibility",
                    "reason_code",
                    "reason_details_json",
                    "candidate_payload_sha256",
                    "recorded_at",
                )
                values = (
                    candidate.candidate_id,
                    candidate.idempotency_key,
                    candidate.candidate_set_id,
                    candidate.fact_cell_id,
                    candidate.observation_id,
                    candidate.candidate_ordinal,
                    candidate.eligibility,
                    candidate.reason_code,
                    candidate.reason_details.canonical_json,
                    candidate.candidate_payload_sha256,
                    candidate.recorded_at,
                )
                self._insert_or_verify(
                    table="fact_resolution_candidates_v2",
                    columns=columns,
                    values=values,
                    idempotency_key=candidate.idempotency_key,
                    record_id=candidate.candidate_id,
                )
            columns, values = self._resolution_values(normalized)
            return self._insert_or_verify(
                table="fact_resolution_revisions_v2",
                columns=columns,
                values=values,
                idempotency_key=normalized.idempotency_key,
                record_id=normalized.resolution_revision_id,
            )

    def as_reported(self, fact_cell_id: str) -> FactAsReportedV2:
        """Return immutable reported values without applying resolution policy."""
        cell = self._load_cell(fact_cell_id)
        rows = self._fetchall(
            "SELECT * FROM fact_observations_v2 "
            "WHERE fact_cell_id = ? AND observation_kind = 'reported' "
            "ORDER BY knowledge_at, recorded_at, observation_id",
            (fact_cell_id,),
        )
        observations = tuple(
            observation
            for row in rows
            if isinstance(
                (observation := self._load_observation(row)),
                ReportedFactObservationV2,
            )
        )
        return FactAsReportedV2(cell=cell, observations=observations)

    def as_known(self, fact_cell_id: str, cutoff: datetime) -> FactAsKnownV2:
        """Replay the latest resolution that was knowable at ``cutoff``."""
        cell = self._load_cell(fact_cell_id)
        if _utc(cell.knowledge_at) > _utc(cutoff) or _utc(cell.recorded_at) > _utc(cutoff):
            raise ValueError(f"fact cell {fact_cell_id!r} was not known at the cutoff")
        row = self._fetchone(
            "SELECT * FROM fact_resolution_revisions_v2 "
            "WHERE fact_cell_id = ? AND knowledge_at <= ? AND recorded_at <= ? "
            "ORDER BY revision DESC, knowledge_at DESC, recorded_at DESC "
            "LIMIT 1",
            (fact_cell_id, cutoff, cutoff),
        )
        if row is None:
            return FactAsKnownV2(
                cell=cell,
                cutoff=cutoff,
                resolution=None,
                candidates=(),
                canonical_observation=None,
            )
        resolution = self._load_resolution(row)
        observations_by_id = {
            observation.observation_id: observation
            for observation in (
                self._load_observation(observation_row)
                for observation_row in self._fetchall(
                    "SELECT observation.* FROM fact_resolution_candidates_v2 "
                    "AS candidate JOIN fact_observations_v2 AS observation "
                    "ON observation.observation_id = candidate.observation_id "
                    "WHERE candidate.candidate_set_id = ? "
                    "AND observation.knowledge_at <= ? "
                    "AND observation.recorded_at <= ? "
                    "ORDER BY candidate.candidate_ordinal",
                    (resolution.candidate_set_id, cutoff, cutoff),
                )
            )
        }
        candidates = tuple(
            observations_by_id[candidate.observation_id]
            for candidate in resolution.candidates
            if candidate.observation_id in observations_by_id
        )
        expected_ids = tuple(candidate.observation_id for candidate in resolution.candidates)
        loaded_ids = tuple(candidate.observation_id for candidate in candidates)
        if loaded_ids != expected_ids:
            raise ValueError("as-known resolution candidate set is incomplete at cutoff")
        if resolution.status == "resolved" and resolution.selected_observation_id is None:
            raise ValueError("resolved as-known revision has no selected observation")
        canonical = (
            None
            if resolution.status != "resolved" or resolution.selected_observation_id is None
            else observations_by_id.get(resolution.selected_observation_id)
        )
        return FactAsKnownV2(
            cell=cell,
            cutoff=cutoff,
            resolution=resolution,
            candidates=candidates,
            canonical_observation=canonical,
        )

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
        *,
        table: str,
        columns: tuple[str, ...],
        values: tuple[object, ...],
        idempotency_key: str,
        record_id: str,
    ) -> PersistResult:
        placeholders = ",".join("?" for _ in columns)
        cursor = self._conn.execute(
            f"INSERT INTO {table} ({','.join(columns)}) "
            f"VALUES ({placeholders}) ON CONFLICT DO NOTHING",
            values,
        )
        if cursor.rowcount == 1:
            return PersistResult(record_id, True)
        existing = self._conn.execute(
            f"SELECT {','.join(columns)} FROM {table} WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing is None or not self._matches(tuple(existing), values):
            raise ValueError(f"immutable {table} identity {idempotency_key!r} conflicts")
        return PersistResult(record_id, False)

    @staticmethod
    def _matches(
        existing: tuple[object, ...],
        expected: tuple[object, ...],
    ) -> bool:
        if len(existing) != len(expected):
            return False
        for stored, supplied in zip(existing, expected, strict=True):
            if isinstance(supplied, datetime):
                if not _same_time(stored, supplied):
                    return False
            elif isinstance(supplied, bool):
                if bool(stored) is not supplied:
                    return False
            elif stored != supplied:
                return False
        return True

    @staticmethod
    def _observation_values(
        observation: FactObservationV2,
    ) -> tuple[tuple[str, ...], tuple[object, ...]]:
        columns = (
            "observation_id",
            "idempotency_key",
            "fact_cell_id",
            "observation_kind",
            "value_kind",
            "numeric_value",
            "text_value",
            "is_nil",
            "raw_lexical_value",
            "document_version_id",
            "evidence_node_id",
            "source_locator_json",
            "source_locator_sha256",
            "source_entry_sha256",
            "source_context_id",
            "source_unit_id",
            "decimals",
            "precision",
            "legacy_match_revision_id",
            "formula_id",
            "formula_version",
            "method_name",
            "method_version",
            "method_config_sha256",
            "revision_kind",
            "supersedes_observation_id",
            "effective_at",
            "knowledge_at",
            "recorded_at",
        )
        if isinstance(observation, ReportedFactObservationV2):
            provenance: tuple[object, ...] = (
                observation.document_version_id,
                observation.evidence_node_id,
                observation.source_locator.canonical_json,
                observation.source_locator_sha256,
                observation.source_entry_sha256,
                observation.source_context_id,
                observation.source_unit_id,
                observation.decimals,
                observation.precision,
                observation.legacy_match_revision_id,
                None,
                None,
            )
        else:
            provenance = (
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                observation.formula_id,
                observation.formula_version,
            )
        values = (
            observation.observation_id,
            observation.idempotency_key,
            observation.fact_cell_id,
            observation.observation_kind,
            observation.value_kind,
            observation.numeric_value,
            observation.text_value,
            observation.is_nil,
            observation.raw_lexical_value,
            *provenance,
            observation.method_name,
            observation.method_version,
            observation.method_config_sha256,
            observation.revision_kind,
            observation.supersedes_observation_id,
            observation.effective_at,
            observation.knowledge_at,
            observation.recorded_at,
        )
        return columns, values

    def _canonical_observation_payload(
        self,
        observation: FactObservationV2,
        *,
        anchor_sha256: str | None,
        derivation_lineage: tuple[str, str, str] | None,
    ) -> str:
        cell_seal = self._fetchone(
            "SELECT semantic_key_sha256 FROM fact_cell_identity_seals_v2 WHERE fact_cell_id = ?",
            (observation.fact_cell_id,),
        )
        if cell_seal is None:
            raise ValueError("observation requires a hardened fact-cell identity")
        provenance: dict[str, JsonValue]
        if isinstance(observation, ReportedFactObservationV2):
            if anchor_sha256 is None:
                raise ValueError("reported observation requires an anchor commitment")
            provenance = {
                "anchor_payload_sha256": anchor_sha256,
                "document_version_id": observation.document_version_id,
                "evidence_node_id": observation.evidence_node_id,
                "source_context_id": observation.source_context_id,
                "source_entry_sha256": observation.source_entry_sha256,
                "source_locator_sha256": observation.source_locator_sha256,
                "source_unit_id": observation.source_unit_id,
            }
        else:
            if anchor_sha256 is not None:
                raise ValueError("derived observation cannot carry a source anchor")
            if derivation_lineage is None:
                raise ValueError("derived payload requires its sealed lineage commitment")
            derivation_seal_id, input_digest, basis_digest = derivation_lineage
            provenance = {
                "canonical_input_digest_sha256": input_digest,
                "derivation_basis_sha256": basis_digest,
                "derivation_seal_id": derivation_seal_id,
                "formula_id": observation.formula_id,
                "formula_version": observation.formula_version,
            }
        return _canonical_json(
            {
                "decimals": (
                    observation.decimals
                    if isinstance(observation, ReportedFactObservationV2)
                    else None
                ),
                "effective_at": observation.effective_at.isoformat(),
                "fact_cell_semantic_key_sha256": str(cell_seal["semantic_key_sha256"]),
                "is_nil": observation.is_nil,
                "knowledge_at": observation.knowledge_at.isoformat(),
                "method_config_sha256": observation.method_config_sha256,
                "method_name": observation.method_name,
                "method_version": observation.method_version,
                "numeric_value": observation.numeric_value,
                "observation_kind": observation.observation_kind,
                "payload_version": "fact_observation_payload.v1",
                "precision": (
                    observation.precision
                    if isinstance(observation, ReportedFactObservationV2)
                    else None
                ),
                "provenance": provenance,
                "raw_lexical_value": observation.raw_lexical_value,
                "recorded_at": observation.recorded_at.isoformat(),
                "revision_kind": observation.revision_kind,
                "supersedes_observation_id": (observation.supersedes_observation_id),
                "text_value": observation.text_value,
                "value_kind": observation.value_kind,
            }
        )

    def _commit_observation_payload(
        self,
        observation: FactObservationV2,
        *,
        anchor_sha256: str | None,
        derivation_lineage: tuple[str, str, str] | None,
    ) -> PersistResult:
        payload_json = self._canonical_observation_payload(
            observation,
            anchor_sha256=anchor_sha256,
            derivation_lineage=derivation_lineage,
        )
        return self._insert_or_verify(
            table="fact_observation_payload_commitments_v2",
            columns=(
                "observation_id",
                "idempotency_key",
                "payload_version",
                "canonical_payload_json",
                "observation_payload_sha256",
                "committed_at",
            ),
            values=(
                observation.observation_id,
                f"{observation.idempotency_key}:payload:v1",
                "fact_observation_payload.v1",
                payload_json,
                _digest(payload_json),
                observation.recorded_at,
            ),
            idempotency_key=f"{observation.idempotency_key}:payload:v1",
            record_id=observation.observation_id,
        )

    @staticmethod
    def _resolution_values(
        resolution: FactResolutionRevisionV2,
    ) -> tuple[tuple[str, ...], tuple[object, ...]]:
        return (
            (
                "resolution_revision_id",
                "idempotency_key",
                "fact_cell_id",
                "revision",
                "status",
                "selected_observation_id",
                "candidate_set_id",
                "candidate_count",
                "candidate_set_digest_sha256",
                "policy_name",
                "policy_version",
                "policy_config_sha256",
                "reason_code",
                "reason_details_json",
                "effective_at",
                "knowledge_at",
                "recorded_at",
                "supersedes_resolution_revision_id",
            ),
            (
                resolution.resolution_revision_id,
                resolution.idempotency_key,
                resolution.fact_cell_id,
                resolution.revision,
                resolution.status,
                resolution.selected_observation_id,
                resolution.candidate_set_id,
                len(resolution.candidates),
                resolution.candidate_set_digest_sha256,
                resolution.policy_name,
                resolution.policy_version,
                resolution.policy_config_sha256,
                resolution.reason_code,
                resolution.reason_details.canonical_json,
                resolution.effective_at,
                resolution.knowledge_cutoff,
                resolution.recorded_at,
                resolution.supersedes_resolution_revision_id,
            ),
        )

    def _verify_derivation_replay(
        self,
        seal: DerivationSealV2,
    ) -> PersistResult:
        columns = (
            "derivation_seal_id",
            "idempotency_key",
            "output_observation_id",
            "input_count",
            "canonical_input_digest_sha256",
            "formula_config_sha256",
            "seal_method",
            "seal_method_version",
            "effective_at",
            "knowledge_at",
            "recorded_at",
        )
        expected = (
            seal.derivation_seal_id,
            seal.idempotency_key,
            seal.derived_observation_id,
            len(seal.ordered_inputs),
            seal.ordered_inputs_sha256,
            seal.formula_config_sha256,
            seal.seal_method,
            seal.seal_method_version,
            seal.effective_at,
            seal.knowledge_at,
            seal.recorded_at,
        )
        existing = self._conn.execute(
            f"SELECT {','.join(columns)} FROM fact_derivation_seals_v2 WHERE idempotency_key = ?",
            (seal.idempotency_key,),
        ).fetchone()
        edge_rows = self._fetchall(
            "SELECT edge_id,idempotency_key,output_observation_id,"
            "input_observation_id,input_resolution_revision_id,input_role,"
            "input_ordinal,recorded_at "
            "FROM fact_derivation_input_edges_v2 "
            "WHERE output_observation_id = ? ORDER BY input_ordinal",
            (seal.derived_observation_id,),
        )
        stored_edges = tuple(
            (
                row["edge_id"],
                row["idempotency_key"],
                row["output_observation_id"],
                row["input_observation_id"],
                row["input_resolution_revision_id"],
                row["input_role"],
                row["input_ordinal"],
                row["recorded_at"],
            )
            for row in edge_rows
        )
        expected_edges = tuple(
            (
                edge.edge_id,
                edge.idempotency_key,
                edge.derived_observation_id,
                edge.input_observation_id,
                edge.input_resolution_revision_id,
                edge.input_role,
                edge.input_position,
                edge.recorded_at,
            )
            for edge in seal.ordered_inputs
        )
        edges_match = len(stored_edges) == len(expected_edges) and all(
            self._matches(stored, expected_edge)
            for stored, expected_edge in zip(
                stored_edges,
                expected_edges,
                strict=True,
            )
        )
        if existing is None or not self._matches(tuple(existing), expected) or not edges_match:
            raise ValueError("immutable derivation seal idempotency conflict")
        return PersistResult(seal.derivation_seal_id, False)

    def _verify_derivation_basis_replay(
        self,
        seal: DerivationSealV2,
    ) -> str:
        output = self._fetchone(
            "SELECT formula_id,formula_version FROM fact_observations_v2 "
            "WHERE observation_id = ? AND observation_kind = 'derived'",
            (seal.derived_observation_id,),
        )
        if output is None:
            raise ValueError("derivation replay output is not derived")
        basis_json = _canonical_json(
            {
                "canonical_input_digest_sha256": seal.ordered_inputs_sha256,
                "execution_config_sha256": seal.formula_config_sha256,
                "formula_definition_sha256": seal.formula_definition_sha256,
                "formula_id": str(output["formula_id"]),
                "formula_version": str(output["formula_version"]),
                "input_basis": seal.input_basis,
                "knowledge_cutoff": seal.knowledge_at.isoformat(),
            }
        )
        columns = (
            "derivation_seal_id",
            "idempotency_key",
            "input_basis",
            "formula_id",
            "formula_version",
            "formula_definition_sha256",
            "execution_config_sha256",
            "knowledge_cutoff",
            "canonical_basis_json",
            "canonical_basis_sha256",
            "recorded_at",
        )
        expected: tuple[object, ...] = (
            seal.derivation_seal_id,
            f"{seal.idempotency_key}:basis:v1",
            seal.input_basis,
            str(output["formula_id"]),
            str(output["formula_version"]),
            seal.formula_definition_sha256,
            seal.formula_config_sha256,
            seal.knowledge_at,
            basis_json,
            _digest(basis_json),
            seal.recorded_at,
        )
        existing = self._conn.execute(
            f"SELECT {','.join(columns)} "
            "FROM fact_derivation_basis_commitments_v2 "
            "WHERE idempotency_key = ?",
            (f"{seal.idempotency_key}:basis:v1",),
        ).fetchone()
        if existing is None or not self._matches(tuple(existing), expected):
            raise ValueError("immutable derivation basis idempotency conflict")
        return _digest(basis_json)

    def _verify_resolution_replay(
        self,
        resolution: FactResolutionRevisionV2,
    ) -> PersistResult:
        columns, expected = self._resolution_values(resolution)
        existing = self._conn.execute(
            f"SELECT {','.join(columns)} FROM fact_resolution_revisions_v2 "
            "WHERE idempotency_key = ?",
            (resolution.idempotency_key,),
        ).fetchone()
        candidate_rows = self._fetchall(
            "SELECT candidate_id,idempotency_key,candidate_set_id,fact_cell_id,"
            "observation_id,candidate_ordinal,eligibility,reason_code,"
            "reason_details_json,candidate_payload_sha256,recorded_at "
            "FROM fact_resolution_candidates_v2 WHERE candidate_set_id = ? "
            "ORDER BY candidate_ordinal",
            (resolution.candidate_set_id,),
        )
        expected_candidates = tuple(
            (
                candidate.candidate_id,
                candidate.idempotency_key,
                candidate.candidate_set_id,
                candidate.fact_cell_id,
                candidate.observation_id,
                candidate.candidate_ordinal,
                candidate.eligibility,
                candidate.reason_code,
                candidate.reason_details.canonical_json,
                candidate.candidate_payload_sha256,
                candidate.recorded_at,
            )
            for candidate in resolution.candidates
        )
        stored_candidates = tuple(
            (
                row["candidate_id"],
                row["idempotency_key"],
                row["candidate_set_id"],
                row["fact_cell_id"],
                row["observation_id"],
                row["candidate_ordinal"],
                row["eligibility"],
                row["reason_code"],
                row["reason_details_json"],
                row["candidate_payload_sha256"],
                row["recorded_at"],
            )
            for row in candidate_rows
        )
        candidates_match = len(stored_candidates) == len(expected_candidates) and all(
            self._matches(stored, expected_candidate)
            for stored, expected_candidate in zip(
                stored_candidates,
                expected_candidates,
                strict=True,
            )
        )
        if existing is None or not self._matches(tuple(existing), expected) or not candidates_match:
            raise ValueError("immutable resolution idempotency conflict")
        return PersistResult(resolution.resolution_revision_id, False)

    def _load_cell(self, fact_cell_id: str) -> FactCellV2:
        row = self._fetchone(
            "SELECT cell.*, seal.semantic_key_version AS hardened_version, "
            "seal.semantic_key_sha256 AS hardened_sha "
            "FROM fact_cells_v2 AS cell "
            "JOIN fact_cell_identity_seals_v2 AS seal "
            "ON seal.fact_cell_id = cell.fact_cell_id "
            "WHERE cell.fact_cell_id = ?",
            (fact_cell_id,),
        )
        if row is None:
            raise ValueError(f"hardened fact cell {fact_cell_id!r} does not exist")
        dimension_rows = self._fetchall(
            "SELECT * FROM fact_dimensions_normalized_v2 "
            "WHERE fact_cell_id = ? ORDER BY dimension_ordinal",
            (fact_cell_id,),
        )
        dimensions = tuple(
            FactDimensionV2.model_validate(
                {
                    "dimension_id": str(item["dimension_id"]),
                    "idempotency_key": str(item["idempotency_key"]),
                    "axis_namespace": str(item["axis_namespace"]),
                    "axis_name": str(item["axis_name"]),
                    "member_kind": str(item["member_kind"]),
                    "explicit_member_namespace": item["explicit_member_namespace"],
                    "explicit_member_name": item["explicit_member_name"],
                    "typed_member_value": (
                        None
                        if item["typed_member_value_json"] is None
                        else CanonicalJSONObject.model_validate_json(
                            str(item["typed_member_value_json"])
                        )
                    ),
                    "recorded_at": self._required_datetime(item["recorded_at"]),
                }
            )
            for item in dimension_rows
        )
        return FactCellV2.model_validate(
            {
                "fact_cell_id": str(row["fact_cell_id"]),
                "idempotency_key": str(row["idempotency_key"]),
                "reporting_entity_id": str(row["reporting_entity_id"]),
                "scope_security_id": row["scope_security_id"],
                "semantic_key_version": str(row["hardened_version"]),
                "semantic_key_sha256": str(row["hardened_sha"]),
                "concept_namespace": str(row["concept_namespace"]),
                "concept_name": str(row["concept_name"]),
                "taxonomy_name": str(row["taxonomy_name"]),
                "taxonomy_version": row["taxonomy_version"],
                "accounting_basis": str(row["accounting_basis"]),
                "consolidation_scope": str(row["consolidation_scope"]),
                "period_kind": str(row["period_kind"]),
                "period_start": self._as_datetime(row["period_start"]),
                "period_end": self._required_datetime(row["period_end"]),
                "fiscal_year": (
                    None if row["fiscal_year"] is None else int(str(row["fiscal_year"]))
                ),
                "fiscal_period": row["fiscal_period"],
                "dimensions": dimensions,
                "unit_key": str(row["unit_key"]),
                "currency": row["currency"],
                "effective_at": self._required_datetime(row["effective_at"]),
                "knowledge_at": self._required_datetime(row["knowledge_at"]),
                "recorded_at": self._required_datetime(row["recorded_at"]),
            }
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
            "effective_at": self._required_datetime(row["effective_at"]),
            "knowledge_at": self._required_datetime(row["knowledge_at"]),
            "recorded_at": self._required_datetime(row["recorded_at"]),
        }
        if row["observation_kind"] == "reported":
            anchor = self._fetchone(
                "SELECT * FROM fact_reported_observation_anchors_v2 WHERE observation_id = ?",
                (str(row["observation_id"]),),
            )
            if anchor is None:
                raise ValueError("reported observation has no exact anchor")
            common.update(
                {
                    "document_version_id": str(row["document_version_id"]),
                    "evidence_node_id": str(row["evidence_node_id"]),
                    "source_locator": CanonicalJSONObject.model_validate_json(
                        str(row["source_locator_json"])
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
        else:
            common.update(
                {
                    "formula_id": str(row["formula_id"]),
                    "formula_version": str(row["formula_version"]),
                }
            )
        return _OBSERVATION_ADAPTER.validate_python(common)

    def _load_resolution(
        self,
        row: dict[str, object],
    ) -> FactResolutionRevisionV2:
        candidate_rows = self._fetchall(
            "SELECT * FROM fact_resolution_candidates_v2 "
            "WHERE candidate_set_id = ? ORDER BY candidate_ordinal",
            (str(row["candidate_set_id"]),),
        )
        candidates = tuple(
            FactResolutionCandidateV2.model_validate(
                {
                    "candidate_id": str(candidate["candidate_id"]),
                    "idempotency_key": str(candidate["idempotency_key"]),
                    "candidate_set_id": str(candidate["candidate_set_id"]),
                    "fact_cell_id": str(candidate["fact_cell_id"]),
                    "observation_id": str(candidate["observation_id"]),
                    "candidate_ordinal": int(str(candidate["candidate_ordinal"])),
                    "eligibility": str(candidate["eligibility"]),
                    "reason_code": str(candidate["reason_code"]),
                    "reason_details": (
                        CanonicalJSONObject.model_validate_json(
                            str(candidate["reason_details_json"])
                        )
                    ),
                    "candidate_payload_sha256": str(candidate["candidate_payload_sha256"]),
                    "recorded_at": self._required_datetime(candidate["recorded_at"]),
                }
            )
            for candidate in candidate_rows
        )
        return FactResolutionRevisionV2.model_validate(
            {
                "resolution_revision_id": str(row["resolution_revision_id"]),
                "idempotency_key": str(row["idempotency_key"]),
                "fact_cell_id": str(row["fact_cell_id"]),
                "revision": int(str(row["revision"])),
                "status": str(row["status"]),
                "candidate_set_id": str(row["candidate_set_id"]),
                "candidates": candidates,
                "selected_observation_id": row["selected_observation_id"],
                "candidate_set_digest_sha256": str(row["candidate_set_digest_sha256"]),
                "policy_name": str(row["policy_name"]),
                "policy_version": str(row["policy_version"]),
                "policy_config_sha256": str(row["policy_config_sha256"]),
                "reason_code": str(row["reason_code"]),
                "reason_details": CanonicalJSONObject.model_validate_json(
                    str(row["reason_details_json"])
                ),
                "knowledge_cutoff": self._required_datetime(row["knowledge_at"]),
                "effective_at": self._required_datetime(row["effective_at"]),
                "recorded_at": self._required_datetime(row["recorded_at"]),
                "supersedes_resolution_revision_id": row["supersedes_resolution_revision_id"],
            }
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
        return dict(zip((item[0] for item in cursor.description), tuple(row), strict=True))

    def _fetchall(
        self,
        sql: str,
        parameters: tuple[object, ...],
    ) -> tuple[dict[str, object], ...]:
        cursor = self._conn.execute(sql, parameters)
        names = tuple(item[0] for item in cursor.description)
        return tuple(dict(zip(names, tuple(row), strict=True)) for row in cursor.fetchall())

    @staticmethod
    def _as_datetime(value: object) -> datetime | None:
        if value is None:
            return None
        return datetime.fromisoformat(str(value))

    @classmethod
    def _required_datetime(cls, value: object) -> datetime:
        parsed = cls._as_datetime(value)
        if parsed is None:
            raise ValueError("required database clock is NULL")
        return parsed
