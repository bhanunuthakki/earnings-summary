"""Deterministic filing-native XBRL output to hardened fact publications.

The adapter starts after a filing processor has already normalized XBRL.  It
does not fetch, parse, infer, or resolve CompanyFacts.  Every accepted input
ordinal receives exactly one in-memory disposition: publication or quarantine.
The database-backed :class:`SourceFactRepository` remains the authority that
verifies the selected subject binding and exact extraction/evidence chain.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    field_validator,
    model_validator,
)

from provenance.fact_plane_v2 import (
    AccountingBasis,
    CanonicalJSONObject,
    ConsolidationScope,
    ExtractionRunCompletenessSealV2,
    FactCellV2,
    FactDimensionV2,
    FiscalPeriod,
    PeriodKind,
    ReportedFactObservationV2,
    RevisionKind,
    ValueKind,
)
from provenance.source_fact_repository import (
    ReportedSourceFact,
    SourceFactPublication,
)

DimensionApplication = Literal["explicit", "defaulted"]
DimensionMemberKind = Literal["explicit", "typed"]
QuarantineReason = Literal[
    "conflicting_source_entry_identity",
    "invalid_fact_graph",
    "normalization_rejected",
]

_ADAPTER_NAME = "filing-native-xbrl-publication"
_ADAPTER_VERSION = "v1"
_DIMENSION_IDENTITY_VERSION = "fact_dimension_identity.v1"
_COMPLETENESS_POLICY = "all-run-nodes-and-disposed-normalized-entries"
_SHA256_LENGTH = 64
FILING_XBRL_NORMALIZED_OUTPUT_SCHEMA = "filing_xbrl_normalized_output"
FILING_XBRL_NORMALIZED_OUTPUT_VERSION = "v1"
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


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


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _validate_sha256(value: str) -> str:
    if len(value) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError("must be a lowercase SHA-256 hex digest")
    return value


def _dimension_id(
    fact_cell_semantic_key_sha256: str,
    member: dict[str, JsonValue],
) -> str:
    digest = _digest(
        {
            "dimension_identity_version": _DIMENSION_IDENTITY_VERSION,
            "fact_cell_semantic_key_sha256": (fact_cell_semantic_key_sha256),
            "member": member,
        }
    )
    return f"xbrl-dimension:{digest}"


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = format(value, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return "0" if normalized in {"", "-0"} else normalized


class FilingXbrlExtractionIdentity(_FrozenModel):
    """Exact identity of the succeeded deterministic extraction run."""

    document_version_id: str = Field(min_length=1, max_length=128)
    extraction_run_id: str = Field(min_length=1, max_length=128)
    extractor_name: str = Field(min_length=1, max_length=128)
    extractor_code_version: str = Field(min_length=1, max_length=64)
    extractor_config_sha256: str
    extraction_input_sha256: str
    extraction_output_sha256: str
    expected_evidence_node_count: int = Field(ge=0)
    knowledge_at: datetime
    recorded_at: datetime

    _config_sha = field_validator("extractor_config_sha256")(_validate_sha256)
    _input_sha = field_validator("extraction_input_sha256")(_validate_sha256)
    _output_sha = field_validator("extraction_output_sha256")(_validate_sha256)

    @model_validator(mode="after")
    def _clocks(self) -> Self:
        if self.recorded_at < self.knowledge_at:
            raise ValueError("extraction recorded_at must not precede knowledge_at")
        return self


class FilingXbrlSubjectIdentity(_FrozenModel):
    """The reporting subject already selected by the identity registry."""

    reporting_entity_id: str = Field(min_length=1, max_length=128)
    selected_subject_binding_revision_id: str = Field(
        min_length=1,
        max_length=128,
    )


class FilingXbrlDimension(_FrozenModel):
    """One effective XBRL dimension and how it entered the source context."""

    application: DimensionApplication
    axis_namespace: str = Field(min_length=1)
    axis_name: str = Field(min_length=1)
    member_kind: DimensionMemberKind
    explicit_member_namespace: str | None = Field(default=None, min_length=1)
    explicit_member_name: str | None = Field(default=None, min_length=1)
    typed_member_value: dict[str, JsonValue] | None = None

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
    def effective_member(self) -> dict[str, JsonValue]:
        return {
            "axis_name": self.axis_name,
            "axis_namespace": self.axis_namespace,
            "explicit_member_name": self.explicit_member_name,
            "explicit_member_namespace": self.explicit_member_namespace,
            "member_kind": self.member_kind,
            "typed_member_value": self.typed_member_value,
        }


class NormalizedFilingXbrlFact(_FrozenModel):
    """One already-normalized filing-native XBRL entry."""

    ordinal: int = Field(ge=0)
    evidence_node_id: str = Field(min_length=1, max_length=128)
    scope_security_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    concept_namespace: str = Field(min_length=1)
    concept_name: str = Field(min_length=1)
    taxonomy_name: str = Field(min_length=1)
    source_taxonomy_version: str = Field(min_length=1)
    accounting_basis: AccountingBasis
    consolidation_scope: ConsolidationScope
    period_kind: PeriodKind
    period_start: datetime | None = None
    period_end: datetime
    fiscal_year: int | None = Field(default=None, ge=1, le=9999)
    fiscal_period: FiscalPeriod | None = None
    dimensions: tuple[FilingXbrlDimension, ...] = ()
    unit_key: str = Field(min_length=1)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    value_kind: ValueKind
    numeric_value: Decimal | None = None
    text_value: str | None = Field(default=None, min_length=1)
    is_nil: bool = False
    raw_lexical_value: str | None = None
    source_context_id: str | None = Field(default=None, min_length=1)
    source_unit_id: str | None = Field(default=None, min_length=1)
    decimals: str | None = Field(default=None, min_length=1)
    precision: str | None = Field(default=None, min_length=1)
    source_locator: dict[str, JsonValue]
    source_locator_sha256: str
    source_entry_sha256: str
    revision_kind: RevisionKind = "initial"
    supersedes_observation_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    effective_at: datetime
    knowledge_at: datetime
    recorded_at: datetime

    _locator_sha = field_validator("source_locator_sha256")(_validate_sha256)
    _entry_sha = field_validator("source_entry_sha256")(_validate_sha256)

    @field_validator("currency")
    @classmethod
    def _currency_upper(cls, value: str | None) -> str | None:
        return None if value is None else value.upper()

    @model_validator(mode="after")
    def _closed_value_and_locator(self) -> Self:
        if self.source_context_id is None:
            raise ValueError("every XBRL fact requires source_context_id")
        if self.value_kind == "numeric":
            if self.numeric_value is None or self.text_value is not None or self.is_nil:
                raise ValueError("numeric facts require only numeric_value")
            if self.source_unit_id is None:
                raise ValueError("numeric XBRL facts require source_unit_id")
        elif self.value_kind == "text":
            if self.text_value is None or self.numeric_value is not None or self.is_nil:
                raise ValueError("text facts require only text_value")
        elif self.numeric_value is not None or self.text_value is not None or not self.is_nil:
            raise ValueError("nil facts cannot carry a parsed value")
        locator = CanonicalJSONObject(self.source_locator)
        if locator.canonical_sha256 != self.source_locator_sha256:
            raise ValueError("source_locator_sha256 must match the immutable source locator")
        identities = tuple(
            (dimension.axis_namespace, dimension.axis_name) for dimension in self.dimensions
        )
        if len(identities) != len(set(identities)):
            raise ValueError("dimension axes must be unique")
        return self


class FilingXbrlNormalizationRejection(_FrozenModel):
    """One raw source fact that deterministically failed normalization."""

    ordinal: int = Field(ge=0)
    evidence_node_id: str = Field(min_length=1, max_length=128)
    canonical_raw_fact_json: str = Field(min_length=2)
    raw_fact_sha256: str
    source_entry_sha256: str
    source_locator_sha256: str
    reason_code: str = Field(min_length=1, max_length=128)
    detail: str = Field(min_length=1, max_length=4096)
    knowledge_at: datetime
    recorded_at: datetime

    _raw_sha = field_validator("raw_fact_sha256")(_validate_sha256)
    _entry_sha = field_validator("source_entry_sha256")(_validate_sha256)
    _locator_sha = field_validator("source_locator_sha256")(_validate_sha256)

    @model_validator(mode="after")
    def _exact_raw_commitment(self) -> Self:
        try:
            parsed = _JSON_OBJECT.validate_json(self.canonical_raw_fact_json)
        except ValueError as exc:
            raise ValueError("canonical raw fact must be valid JSON") from exc
        if _canonical_json(parsed) != self.canonical_raw_fact_json:
            raise ValueError("raw fact JSON must be canonical")
        if _digest(parsed) != self.raw_fact_sha256:
            raise ValueError("raw fact SHA does not match its payload")
        if self.recorded_at < self.knowledge_at:
            raise ValueError("rejection recorded_at must not precede knowledge_at")
        return self


def _normalized_output_payload(
    extraction: FilingXbrlExtractionIdentity,
    subject: FilingXbrlSubjectIdentity,
    entries: tuple[NormalizedFilingXbrlFact, ...],
    rejections: tuple[FilingXbrlNormalizationRejection, ...],
) -> dict[str, JsonValue]:
    return {
        "entries": [
            entry.model_dump(mode="json")
            for entry in sorted(entries, key=lambda item: item.ordinal)
        ],
        "extraction": extraction.model_dump(
            mode="json",
            exclude={"extraction_output_sha256"},
        ),
        "normalized_output_schema_name": (FILING_XBRL_NORMALIZED_OUTPUT_SCHEMA),
        "normalized_output_schema_version": (FILING_XBRL_NORMALIZED_OUTPUT_VERSION),
        "rejections": [
            rejection.model_dump(mode="json")
            for rejection in sorted(rejections, key=lambda item: item.ordinal)
        ],
        "subject": subject.model_dump(mode="json"),
    }


class FilingXbrlNormalizedOutput(_FrozenModel):
    """One complete normalized processor output for one extraction run."""

    extraction: FilingXbrlExtractionIdentity
    subject: FilingXbrlSubjectIdentity
    entries: tuple[NormalizedFilingXbrlFact, ...]
    rejections: tuple[FilingXbrlNormalizationRejection, ...] = ()

    @classmethod
    def with_computed_digest(
        cls,
        *,
        extraction: FilingXbrlExtractionIdentity,
        subject: FilingXbrlSubjectIdentity,
        entries: tuple[NormalizedFilingXbrlFact, ...],
        rejections: tuple[FilingXbrlNormalizationRejection, ...] = (),
    ) -> FilingXbrlNormalizedOutput:
        output_sha256 = _digest(
            _normalized_output_payload(extraction, subject, entries, rejections)
        )
        return cls(
            extraction=extraction.model_copy(update={"extraction_output_sha256": output_sha256}),
            subject=subject,
            entries=entries,
            rejections=rejections,
        )

    @model_validator(mode="after")
    def _unique_ordinals_and_clocks(self) -> Self:
        items = (*self.entries, *self.rejections)
        ordinals = tuple(item.ordinal for item in items)
        if len(ordinals) != len(set(ordinals)):
            raise ValueError("normalized filing entry ordinals must be unique")
        for entry in items:
            if entry.knowledge_at < self.extraction.knowledge_at:
                raise ValueError("fact knowledge_at must not precede extraction knowledge_at")
            if entry.recorded_at < self.extraction.recorded_at:
                raise ValueError("fact recorded_at must not precede extraction recorded_at")
        if self.extraction.extraction_output_sha256 != self.canonical_payload_sha256:
            raise ValueError(
                "extraction_output_sha256 must match the canonical normalized filing-XBRL output"
            )
        return self

    @property
    def normalized_output_schema_name(self) -> str:
        return FILING_XBRL_NORMALIZED_OUTPUT_SCHEMA

    @property
    def normalized_output_schema_version(self) -> str:
        return FILING_XBRL_NORMALIZED_OUTPUT_VERSION

    @property
    def canonical_payload_json(self) -> str:
        return _canonical_json(
            _normalized_output_payload(
                self.extraction,
                self.subject,
                self.entries,
                self.rejections,
            )
        )

    @property
    def canonical_payload_sha256(self) -> str:
        return hashlib.sha256(self.canonical_payload_json.encode()).hexdigest()


class PublishedFilingXbrlDisposition(_FrozenModel):
    disposition: Literal["publish"] = "publish"
    ordinal: int = Field(ge=0)
    source_entry_sha256: str
    fact_cell_id: str
    observation_id: str

    _entry_sha = field_validator("source_entry_sha256")(_validate_sha256)


class QuarantinedFilingXbrlDisposition(_FrozenModel):
    disposition: Literal["quarantine"] = "quarantine"
    ordinal: int = Field(ge=0)
    source_entry_sha256: str
    reason: QuarantineReason
    detail: str = Field(min_length=1)

    _entry_sha = field_validator("source_entry_sha256")(_validate_sha256)


class DuplicateFilingXbrlDisposition(_FrozenModel):
    disposition: Literal["duplicate"] = "duplicate"
    ordinal: int = Field(ge=0)
    source_entry_sha256: str
    primary_ordinal: int = Field(ge=0)
    fact_cell_id: str
    observation_id: str

    _entry_sha = field_validator("source_entry_sha256")(_validate_sha256)

    @model_validator(mode="after")
    def _primary_precedes_duplicate(self) -> Self:
        if self.primary_ordinal >= self.ordinal:
            raise ValueError("duplicate primary must precede its duplicate ordinal")
        return self


FilingXbrlDisposition = Annotated[
    PublishedFilingXbrlDisposition
    | DuplicateFilingXbrlDisposition
    | QuarantinedFilingXbrlDisposition,
    Field(discriminator="disposition"),
]


class FilingXbrlEntryDispositionCommitment(_FrozenModel):
    """Canonical normalized input bound to its one adapter disposition."""

    ordinal: int = Field(ge=0)
    canonical_normalized_entry_json: str = Field(min_length=2)
    normalized_entry_sha256: str
    normalized_entry_identity_sha256: str
    source_entry_sha256: str
    source_locator_sha256: str
    disposition: FilingXbrlDisposition

    _entry_sha = field_validator("normalized_entry_sha256")(_validate_sha256)
    _identity_sha = field_validator("normalized_entry_identity_sha256")(_validate_sha256)
    _source_sha = field_validator("source_entry_sha256")(_validate_sha256)
    _locator_sha = field_validator("source_locator_sha256")(_validate_sha256)

    @model_validator(mode="after")
    def _exact_commitment(self) -> Self:
        try:
            parsed = _JSON_OBJECT.validate_json(self.canonical_normalized_entry_json)
        except ValueError as exc:
            raise ValueError("canonical normalized entry must be valid JSON") from exc
        if _canonical_json(parsed) != self.canonical_normalized_entry_json:
            raise ValueError("normalized entry JSON must be canonical")
        if _digest(parsed) != self.normalized_entry_sha256:
            raise ValueError("normalized entry SHA does not match its payload")
        identity_payload = dict(parsed)
        identity_payload.pop("ordinal", None)
        if _digest(identity_payload) != self.normalized_entry_identity_sha256:
            raise ValueError("normalized entry identity SHA does not match")
        if self.disposition.ordinal != self.ordinal:
            raise ValueError("entry and disposition ordinals must match")
        if self.disposition.source_entry_sha256 != self.source_entry_sha256:
            raise ValueError("entry and disposition source identities must match")
        return self


class FilingXbrlAdapterResult(_FrozenModel):
    publication: SourceFactPublication
    dispositions: tuple[FilingXbrlDisposition, ...]
    entry_commitments: tuple[FilingXbrlEntryDispositionCommitment, ...]
    total_count: int = Field(ge=0)
    published_count: int = Field(ge=0)
    duplicate_count: int = Field(ge=0)
    quarantined_count: int = Field(ge=0)
    published_fact_cell_ids: tuple[str, ...]
    published_observation_ids: tuple[str, ...]
    quarantined_ordinals: tuple[int, ...]

    @model_validator(mode="after")
    def _complete_dispositions(self) -> Self:
        published = tuple(
            item for item in self.dispositions if isinstance(item, PublishedFilingXbrlDisposition)
        )
        duplicates = tuple(
            item for item in self.dispositions if isinstance(item, DuplicateFilingXbrlDisposition)
        )
        quarantined = tuple(
            item for item in self.dispositions if isinstance(item, QuarantinedFilingXbrlDisposition)
        )
        if self.total_count != len(self.dispositions):
            raise ValueError("every normalized ordinal requires one disposition")
        if self.total_count != len(self.entry_commitments):
            raise ValueError("every normalized ordinal requires one commitment")
        if self.total_count != (
            self.published_count + self.duplicate_count + self.quarantined_count
        ):
            raise ValueError("adapter disposition counts do not reconcile")
        ordinals = tuple(item.ordinal for item in self.dispositions)
        if len(ordinals) != len(set(ordinals)):
            raise ValueError("adapter dispositions cannot repeat an ordinal")
        if self.published_count != len(published):
            raise ValueError("published disposition count is not exact")
        if self.duplicate_count != len(duplicates):
            raise ValueError("duplicate disposition count is not exact")
        if self.quarantined_count != len(quarantined):
            raise ValueError("quarantined disposition count is not exact")
        if tuple(item.disposition for item in self.entry_commitments) != (self.dispositions):
            raise ValueError("entry commitments must contain the exact dispositions")
        if tuple(item.ordinal for item in self.entry_commitments) != ordinals:
            raise ValueError("entry commitments must follow disposition order")
        if self.published_fact_cell_ids != tuple(item.fact_cell_id for item in published):
            raise ValueError("published fact-cell set is not exact")
        if self.published_observation_ids != tuple(item.observation_id for item in published):
            raise ValueError("published observation set is not exact")
        publication_pairs = tuple(
            (
                item.cell.fact_cell_id,
                item.observation.observation_id,
            )
            for item in self.publication.reported_facts
        )
        published_pairs = tuple((item.fact_cell_id, item.observation_id) for item in published)
        if publication_pairs != published_pairs:
            raise ValueError("publication reported facts must exactly match published dispositions")
        if (
            self.publication.derived_facts
            or self.publication.relations
            or self.publication.derivations
            or self.publication.resolutions
            or len(self.publication.extraction_seals) != 1
        ):
            raise ValueError(
                "filing-XBRL publication must contain only reported facts and one extraction seal"
            )
        published_by_ordinal = {item.ordinal: item for item in published}
        for duplicate in duplicates:
            primary = published_by_ordinal.get(duplicate.primary_ordinal)
            if (
                primary is None
                or duplicate.source_entry_sha256 != primary.source_entry_sha256
                or duplicate.fact_cell_id != primary.fact_cell_id
                or duplicate.observation_id != primary.observation_id
            ):
                raise ValueError("duplicate disposition must reference its exact published primary")
        return self


class FilingXbrlFactAdapter:
    """Build a deterministic, filing-native v2 publication graph."""

    def adapt(self, output: FilingXbrlNormalizedOutput) -> FilingXbrlAdapterResult:
        entries = tuple(sorted(output.entries, key=lambda entry: entry.ordinal))
        rejections = tuple(sorted(output.rejections, key=lambda entry: entry.ordinal))
        duplicate_primaries, conflict_dispositions = self._source_identity_groups(entries)
        reported: list[ReportedSourceFact] = []
        dispositions: list[FilingXbrlDisposition] = [
            QuarantinedFilingXbrlDisposition(
                ordinal=rejection.ordinal,
                source_entry_sha256=rejection.source_entry_sha256,
                reason="normalization_rejected",
                detail=f"{rejection.reason_code}: {rejection.detail}",
            )
            for rejection in rejections
        ]
        published_by_ordinal: dict[int, ReportedSourceFact] = {}

        for entry in entries:
            conflict = conflict_dispositions.get(entry.ordinal)
            if conflict is not None:
                dispositions.append(conflict)
                continue
            primary_ordinal = duplicate_primaries.get(entry.ordinal)
            if primary_ordinal is not None:
                primary = published_by_ordinal.get(primary_ordinal)
                if primary is None:
                    dispositions.append(
                        QuarantinedFilingXbrlDisposition(
                            ordinal=entry.ordinal,
                            source_entry_sha256=entry.source_entry_sha256,
                            reason="invalid_fact_graph",
                            detail=(
                                "exact duplicate cannot link because its "
                                "deterministic primary was not publishable"
                            ),
                        )
                    )
                    continue
                dispositions.append(
                    DuplicateFilingXbrlDisposition(
                        ordinal=entry.ordinal,
                        source_entry_sha256=entry.source_entry_sha256,
                        primary_ordinal=primary_ordinal,
                        fact_cell_id=primary.cell.fact_cell_id,
                        observation_id=primary.observation.observation_id,
                    )
                )
                continue
            try:
                source_fact = self._source_fact(output, entry)
            except ValueError as exc:
                dispositions.append(
                    QuarantinedFilingXbrlDisposition(
                        ordinal=entry.ordinal,
                        source_entry_sha256=entry.source_entry_sha256,
                        reason="invalid_fact_graph",
                        detail=str(exc),
                    )
                )
                continue
            reported.append(source_fact)
            published_by_ordinal[entry.ordinal] = source_fact
            dispositions.append(
                PublishedFilingXbrlDisposition(
                    ordinal=entry.ordinal,
                    source_entry_sha256=entry.source_entry_sha256,
                    fact_cell_id=source_fact.cell.fact_cell_id,
                    observation_id=source_fact.observation.observation_id,
                )
            )

        dispositions.sort(key=lambda item: item.ordinal)
        seal = self._extraction_seal(output, tuple(dispositions))
        observation_ids = tuple(item.observation.observation_id for item in reported)
        publication_digest = _digest(
            {
                "adapter": f"{_ADAPTER_NAME}:{_ADAPTER_VERSION}",
                "extraction_run_id": output.extraction.extraction_run_id,
                "observation_ids": sorted(observation_ids),
            }
        )
        publication = SourceFactPublication(
            publication_id=f"xbrl-publication:{publication_digest}",
            idempotency_key=f"xbrl-publication:{publication_digest}",
            created_at=output.extraction.knowledge_at,
            recorded_at=output.extraction.recorded_at,
            reported_facts=tuple(reported),
            extraction_seals=(seal,),
        )
        published = tuple(
            item for item in dispositions if isinstance(item, PublishedFilingXbrlDisposition)
        )
        quarantined = tuple(
            item for item in dispositions if isinstance(item, QuarantinedFilingXbrlDisposition)
        )
        duplicates = tuple(
            item for item in dispositions if isinstance(item, DuplicateFilingXbrlDisposition)
        )
        dispositions_by_ordinal = {item.ordinal: item for item in dispositions}
        source_items = tuple(sorted((*entries, *rejections), key=lambda item: item.ordinal))
        entry_commitments = tuple(
            FilingXbrlEntryDispositionCommitment(
                ordinal=item.ordinal,
                canonical_normalized_entry_json=_canonical_json(item),
                normalized_entry_sha256=_digest(item),
                normalized_entry_identity_sha256=_digest(
                    item.model_dump(mode="json", exclude={"ordinal"})
                ),
                source_entry_sha256=item.source_entry_sha256,
                source_locator_sha256=item.source_locator_sha256,
                disposition=dispositions_by_ordinal[item.ordinal],
            )
            for item in source_items
        )
        return FilingXbrlAdapterResult(
            publication=publication,
            dispositions=tuple(dispositions),
            entry_commitments=entry_commitments,
            total_count=len(source_items),
            published_count=len(published),
            duplicate_count=len(duplicates),
            quarantined_count=len(quarantined),
            published_fact_cell_ids=tuple(item.fact_cell_id for item in published),
            published_observation_ids=tuple(item.observation_id for item in published),
            quarantined_ordinals=tuple(item.ordinal for item in quarantined),
        )

    @staticmethod
    def _source_identity_groups(
        entries: tuple[NormalizedFilingXbrlFact, ...],
    ) -> tuple[
        dict[int, int],
        dict[int, QuarantinedFilingXbrlDisposition],
    ]:
        groups: defaultdict[str, list[NormalizedFilingXbrlFact]] = defaultdict(list)
        for entry in entries:
            groups[entry.source_entry_sha256].append(entry)
        duplicate_primaries: dict[int, int] = {}
        conflicts: dict[int, QuarantinedFilingXbrlDisposition] = {}
        for source_entry_sha256, group in groups.items():
            if len(group) == 1:
                continue
            payloads = {
                _canonical_json(
                    item.model_dump(
                        mode="json",
                        exclude={"ordinal"},
                    )
                )
                for item in group
            }
            ordered = tuple(sorted(group, key=lambda item: item.ordinal))
            if len(payloads) == 1:
                primary_ordinal = ordered[0].ordinal
                duplicate_primaries.update((item.ordinal, primary_ordinal) for item in ordered[1:])
                continue
            for item in ordered:
                conflicts[item.ordinal] = QuarantinedFilingXbrlDisposition(
                    ordinal=item.ordinal,
                    source_entry_sha256=source_entry_sha256,
                    reason="conflicting_source_entry_identity",
                    detail=("immutable source-entry identity maps to conflicting normalized facts"),
                )
        return duplicate_primaries, conflicts

    @staticmethod
    def _source_fact(
        output: FilingXbrlNormalizedOutput,
        entry: NormalizedFilingXbrlFact,
    ) -> ReportedSourceFact:
        provisional_dimensions = tuple(
            FactDimensionV2(
                dimension_id=("xbrl-dimension-provisional:" + _digest(dimension.effective_member)),
                idempotency_key=(
                    "xbrl-dimension-provisional:" + _digest(dimension.effective_member)
                ),
                axis_namespace=dimension.axis_namespace,
                axis_name=dimension.axis_name,
                member_kind=dimension.member_kind,
                explicit_member_namespace=(dimension.explicit_member_namespace),
                explicit_member_name=dimension.explicit_member_name,
                typed_member_value=(
                    None
                    if dimension.typed_member_value is None
                    else CanonicalJSONObject(dimension.typed_member_value)
                ),
                recorded_at=entry.recorded_at,
            )
            for dimension in entry.dimensions
        )
        cell_values = {
            "reporting_entity_id": output.subject.reporting_entity_id,
            "scope_security_id": entry.scope_security_id,
            "concept_namespace": entry.concept_namespace,
            "concept_name": entry.concept_name,
            "taxonomy_name": entry.taxonomy_name,
            "taxonomy_version": entry.source_taxonomy_version,
            "accounting_basis": entry.accounting_basis,
            "consolidation_scope": entry.consolidation_scope,
            "period_kind": entry.period_kind,
            "period_start": entry.period_start,
            "period_end": entry.period_end,
            "fiscal_year": entry.fiscal_year,
            "fiscal_period": entry.fiscal_period,
            "dimensions": provisional_dimensions,
            "unit_key": entry.unit_key,
            "currency": entry.currency,
            "effective_at": entry.effective_at,
            "knowledge_at": entry.knowledge_at,
            "recorded_at": entry.recorded_at,
        }
        provisional = FactCellV2.model_validate(
            {
                "fact_cell_id": "xbrl-cell:provisional",
                "idempotency_key": "xbrl-cell:provisional",
                **cell_values,
            }
        )
        semantic_sha256 = provisional.semantic_key_sha256
        if semantic_sha256 is None:
            raise ValueError("fact-cell semantic identity was not derived")
        dimensions = tuple(
            FactDimensionV2(
                dimension_id=(
                    _dimension_id(
                        semantic_sha256,
                        dimension.effective_member,
                    )
                ),
                idempotency_key=(
                    _dimension_id(
                        semantic_sha256,
                        dimension.effective_member,
                    )
                ),
                axis_namespace=dimension.axis_namespace,
                axis_name=dimension.axis_name,
                member_kind=dimension.member_kind,
                explicit_member_namespace=(dimension.explicit_member_namespace),
                explicit_member_name=dimension.explicit_member_name,
                typed_member_value=(
                    None
                    if dimension.typed_member_value is None
                    else CanonicalJSONObject(dimension.typed_member_value)
                ),
                recorded_at=entry.recorded_at,
            )
            for dimension in entry.dimensions
        )
        cell = FactCellV2.model_validate(
            {
                "fact_cell_id": f"xbrl-cell:{semantic_sha256}",
                "idempotency_key": f"xbrl-cell:{semantic_sha256}",
                **cell_values,
                "dimensions": dimensions,
            }
        )
        if cell.semantic_key_sha256 != semantic_sha256:
            raise ValueError("dimension IDs changed the semantic fact-cell identity")
        observation_digest = _digest(
            {
                "document_version_id": output.extraction.document_version_id,
                "evidence_node_id": entry.evidence_node_id,
                "extraction_run_id": output.extraction.extraction_run_id,
                "source_entry_sha256": entry.source_entry_sha256,
            }
        )
        observation = ReportedFactObservationV2(
            observation_id=f"xbrl-observation:{observation_digest}",
            idempotency_key=f"xbrl-observation:{observation_digest}",
            fact_cell_id=cell.fact_cell_id,
            observation_kind="reported",
            value_kind=entry.value_kind,
            numeric_value=_decimal_text(entry.numeric_value),
            text_value=entry.text_value,
            is_nil=entry.is_nil,
            raw_lexical_value=entry.raw_lexical_value,
            method_name=output.extraction.extractor_name,
            method_version=output.extraction.extractor_code_version,
            method_config_sha256=(output.extraction.extractor_config_sha256),
            revision_kind=entry.revision_kind,
            supersedes_observation_id=entry.supersedes_observation_id,
            effective_at=entry.effective_at,
            knowledge_at=entry.knowledge_at,
            recorded_at=entry.recorded_at,
            document_version_id=output.extraction.document_version_id,
            evidence_node_id=entry.evidence_node_id,
            source_locator=CanonicalJSONObject(entry.source_locator),
            source_locator_sha256=entry.source_locator_sha256,
            source_entry_sha256=entry.source_entry_sha256,
            subject_binding_revision_id=(output.subject.selected_subject_binding_revision_id),
            source_taxonomy_version=entry.source_taxonomy_version,
            source_context_id=entry.source_context_id,
            source_unit_id=entry.source_unit_id,
            decimals=entry.decimals,
            precision=entry.precision,
        )
        return ReportedSourceFact(cell=cell, observation=observation)

    @staticmethod
    def _extraction_seal(
        output: FilingXbrlNormalizedOutput,
        dispositions: tuple[FilingXbrlDisposition, ...],
    ) -> ExtractionRunCompletenessSealV2:
        policy_payload = {
            "adapter_name": _ADAPTER_NAME,
            "adapter_version": _ADAPTER_VERSION,
            "dispositions": [item.model_dump(mode="json") for item in dispositions],
            "extraction_input_sha256": (output.extraction.extraction_input_sha256),
            "extraction_output_sha256": (output.extraction.extraction_output_sha256),
            "expected_evidence_node_count": (output.extraction.expected_evidence_node_count),
        }
        seal_digest = _digest(
            {
                "extraction_run_id": output.extraction.extraction_run_id,
                "policy": policy_payload,
            }
        )
        return ExtractionRunCompletenessSealV2(
            extraction_seal_id=f"xbrl-extraction-seal:{seal_digest}",
            idempotency_key=f"xbrl-extraction-seal:{seal_digest}",
            extraction_run_id=output.extraction.extraction_run_id,
            expected_node_count=(output.extraction.expected_evidence_node_count),
            completeness_policy_name=_COMPLETENESS_POLICY,
            completeness_policy_version=_ADAPTER_VERSION,
            completeness_policy_sha256=_digest(policy_payload),
            knowledge_at=output.extraction.knowledge_at,
            recorded_at=output.extraction.recorded_at,
        )
