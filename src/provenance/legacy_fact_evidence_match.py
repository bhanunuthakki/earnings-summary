"""Typed append-only proof records for legacy-fact evidence matches.

This module persists matcher outputs; it does not implement matching or alter
fact-observation writers.  Every accepted record identifies the exact legacy
fact payload, current document-binding revision, evidence node, candidate set,
and relocated evidence cell that passed all six deterministic checks.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    field_validator,
    model_validator,
)

FactTable = Literal["financial_facts", "kpi_facts"]
MatchCheck = Literal["pass", "fail", "not_evaluated"]
MatchOutcome = Literal["accepted", "retryable", "terminal"]
_SHA256_LENGTH = 64


def _sha256(value: str) -> str:
    normalized = value.lower()
    if len(normalized) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("must be a lowercase SHA-256 hex digest")
    return normalized


def _optional_sha256(value: str | None) -> str | None:
    return None if value is None else _sha256(value)


def _timeline(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class CanonicalJSONObject(RootModel[dict[str, JsonValue]]):
    """A closed JSON object with stable bytes for hashing and persistence."""

    model_config = ConfigDict(frozen=True)

    @property
    def canonical_json(self) -> str:
        return json.dumps(
            self.root,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    @property
    def canonical_sha256(self) -> str:
        return hashlib.sha256(self.canonical_json.encode()).hexdigest()


class OriginalFactLocator(CanonicalJSONObject):
    """A preserved, non-empty locator from the legacy fact row."""

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        if not self.root:
            raise ValueError("original fact locator must not be empty")
        return self


class CompanyFactsRelocatedLocator(BaseModel):
    """Exact CompanyFacts entry identity after deterministic relocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    accession_number: str = Field(pattern=r"^\d{10}-\d{2}-\d{6}$")
    namespace: str = Field(min_length=1, max_length=128, pattern=r"^[^.\\[\\]]+$")
    concept: str = Field(min_length=1, max_length=256, pattern=r"^[^.\\[\\]]+$")
    unit: str = Field(min_length=1, max_length=128, pattern=r"^[^.\\[\\]]+$")
    entry_index: int = Field(ge=0)
    json_path: str = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def _exact_path(self) -> Self:
        expected = f"facts.{self.namespace}.{self.concept}.units.{self.unit}[{self.entry_index}]"
        if self.json_path != expected:
            raise ValueError("CompanyFacts json_path must exactly match entry identity")
        return self

    @property
    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    @property
    def canonical_sha256(self) -> str:
        return hashlib.sha256(self.canonical_json.encode()).hexdigest()


class FinancialFactPayloadV1(BaseModel):
    """Closed semantic snapshot of one ``financial_facts`` row."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["financial_fact_payload.v1"]
    fact_table: Literal["financial_facts"]
    fact_row_id: int = Field(gt=0)
    ticker: str = Field(min_length=1, max_length=32)
    period_end: str = Field(min_length=1, max_length=64)
    fiscal_period_type: str = Field(min_length=1, max_length=32)
    line_item: str = Field(min_length=1, max_length=256)
    value: str = Field(min_length=1)
    currency: str | None = Field(default=None, min_length=1, max_length=16)
    unit: str = Field(min_length=1, max_length=64)
    source_doc_id: int = Field(gt=0)
    extracted_by: str | None = Field(default=None, min_length=1, max_length=256)
    locator: OriginalFactLocator | None = None

    @property
    def canonical_json(self) -> str:
        return _canonical_model_json(self)

    @property
    def canonical_sha256(self) -> str:
        return hashlib.sha256(self.canonical_json.encode()).hexdigest()


class KpiFactPayloadV1(BaseModel):
    """Closed semantic snapshot of one ``kpi_facts`` row."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["kpi_fact_payload.v1"]
    fact_table: Literal["kpi_facts"]
    fact_row_id: int = Field(gt=0)
    ticker: str = Field(min_length=1, max_length=32)
    period_end: str = Field(min_length=1, max_length=64)
    fiscal_period_type: str = Field(min_length=1, max_length=32)
    kpi_definition_id: int = Field(gt=0)
    value: str = Field(min_length=1)
    currency: str | None = Field(default=None, min_length=1, max_length=8)
    unit: str = Field(min_length=1, max_length=64)
    source_doc_id: int = Field(gt=0)
    extracted_by: str | None = Field(default=None, min_length=1, max_length=256)
    locator: OriginalFactLocator | None = None
    source_excerpt: str | None = None
    computed_from: str | None = None
    formula_id: int | None = None
    formula_version: int | None = None

    @property
    def canonical_json(self) -> str:
        return _canonical_model_json(self)

    @property
    def canonical_sha256(self) -> str:
        return hashlib.sha256(self.canonical_json.encode()).hexdigest()


FactPayloadV1 = Annotated[
    FinancialFactPayloadV1 | KpiFactPayloadV1,
    Field(discriminator="fact_table"),
]


class CompanyFactsCandidateV1(BaseModel):
    """One typed candidate entry considered by a future matcher."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entry_sha256: str
    relocated_locator: CompanyFactsRelocatedLocator

    _entry_sha = field_validator("entry_sha256")(_sha256)

    @property
    def canonical_json(self) -> str:
        return _canonical_model_json(self)


class CompanyFactsCandidateManifestV1(BaseModel):
    """Closed candidate set whose order and digest are deterministic."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["companyfacts_candidate_manifest.v1"]
    candidates: tuple[CompanyFactsCandidateV1, ...]

    @model_validator(mode="after")
    def _unique_canonical_candidates(self) -> Self:
        identities = tuple(
            (candidate.entry_sha256, candidate.relocated_locator.canonical_json)
            for candidate in self.candidates
        )
        if len(identities) != len(set(identities)):
            raise ValueError("candidate manifest must not contain duplicates")
        if identities != tuple(sorted(identities)):
            raise ValueError("candidate manifest must use canonical sorted order")
        return self

    @property
    def canonical_json(self) -> str:
        return _canonical_model_json(self)

    @property
    def canonical_sha256(self) -> str:
        return hashlib.sha256(self.canonical_json.encode()).hexdigest()


def _canonical_model_json(model: BaseModel) -> str:
    return json.dumps(
        model.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


class LegacyFactEvidenceMatchRevision(BaseModel):
    """One immutable revision of a legacy fact-to-evidence decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    match_revision_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    fact_table: FactTable
    fact_row_id: int = Field(gt=0)
    issuer_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(gt=0)
    fact_payload: FactPayloadV1
    fact_payload_fingerprint_sha256: str | None = None
    original_locator: OriginalFactLocator | None = None
    original_locator_sha256: str | None = None
    relocated_locator: CompanyFactsRelocatedLocator | None = None
    relocated_locator_sha256: str | None = None
    legacy_binding_revision_id: str = Field(min_length=1, max_length=128)
    legacy_binding_revision: int = Field(gt=0)
    binding_scope_content_sha256: str
    evidence_node_id: str = Field(min_length=1, max_length=128)
    matched_entry_sha256: str | None = None
    candidate_manifest: CompanyFactsCandidateManifestV1
    candidate_manifest_sha256: str | None = None
    candidate_count: int | None = Field(default=None, ge=0)
    matched_candidate_count: int = Field(ge=0)
    issuer_check: MatchCheck
    context_check: MatchCheck
    unit_check: MatchCheck
    sign_check: MatchCheck
    fiscal_period_check: MatchCheck
    value_check: MatchCheck
    matcher_name: str = Field(min_length=1, max_length=128)
    matcher_version: str = Field(min_length=1, max_length=64)
    matcher_config_sha256: str
    outcome: MatchOutcome
    reason_code: str = Field(min_length=1, max_length=128)
    reason_details: CanonicalJSONObject
    effective_at: datetime
    knowledge_at: datetime
    recorded_at: datetime
    supersedes_match_revision_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )

    _fact_sha = field_validator("fact_payload_fingerprint_sha256")(_optional_sha256)
    _original_sha = field_validator("original_locator_sha256")(_optional_sha256)
    _relocated_sha = field_validator("relocated_locator_sha256")(_optional_sha256)
    _scope_sha = field_validator("binding_scope_content_sha256")(_sha256)
    _entry_sha = field_validator("matched_entry_sha256")(_optional_sha256)
    _candidate_sha = field_validator("candidate_manifest_sha256")(_optional_sha256)
    _matcher_sha = field_validator("matcher_config_sha256")(_sha256)

    @model_validator(mode="after")
    def _validate_revision(self) -> Self:
        self._set_or_check_digest(
            "fact_payload_fingerprint_sha256",
            self.fact_payload.canonical_sha256,
        )
        self._validate_optional_locator(
            "original_locator_sha256",
            self.original_locator,
        )
        payload_locator = self.fact_payload.locator
        if (None if payload_locator is None else payload_locator.canonical_json) != (
            None if self.original_locator is None else self.original_locator.canonical_json
        ):
            raise ValueError("original locator must exactly match the fact payload locator")
        self._validate_optional_locator(
            "relocated_locator_sha256",
            self.relocated_locator,
        )
        self._set_or_check_digest(
            "candidate_manifest_sha256",
            self.candidate_manifest.canonical_sha256,
        )
        expected_candidate_count = len(self.candidate_manifest.candidates)
        if self.candidate_count is None:
            object.__setattr__(
                self,
                "candidate_count",
                expected_candidate_count,
            )
        elif self.candidate_count != expected_candidate_count:
            raise ValueError("candidate_count must equal the typed candidate manifest")
        if self.matched_candidate_count > expected_candidate_count:
            raise ValueError("matched_candidate_count exceeds candidate_count")
        if (self.revision == 1) != (self.supersedes_match_revision_id is None):
            raise ValueError("legacy fact match revision chain is incomplete")
        checks = (
            self.issuer_check,
            self.context_check,
            self.unit_check,
            self.sign_check,
            self.fiscal_period_check,
            self.value_check,
        )
        all_pass = all(check == "pass" for check in checks)
        complete_match = (
            self.relocated_locator is not None
            and self.relocated_locator_sha256 is not None
            and self.matched_entry_sha256 is not None
        )
        unique_candidate = expected_candidate_count > 0 and self.matched_candidate_count == 1
        candidate_matches = (
            self.matched_entry_sha256,
            (None if self.relocated_locator is None else self.relocated_locator.canonical_json),
        ) in {
            (
                candidate.entry_sha256,
                candidate.relocated_locator.canonical_json,
            )
            for candidate in self.candidate_manifest.candidates
        }
        if self.outcome == "accepted" and not (
            all_pass and complete_match and unique_candidate and candidate_matches
        ):
            raise ValueError(
                "accepted legacy fact match requires six passing checks "
                "and exactly one typed relocated candidate"
            )
        if (
            self.outcome == "accepted"
            and isinstance(self.fact_payload, KpiFactPayloadV1)
            and (
                self.fact_payload.computed_from is not None
                or self.fact_payload.formula_id is not None
                or self.fact_payload.formula_version is not None
                or "derived" in (self.fact_payload.extracted_by or "").lower()
            )
        ):
            raise ValueError("accepted KPI evidence match requires a reported, non-derived fact")
        if self.outcome != "accepted" and all_pass and self.matched_candidate_count == 1:
            raise ValueError("a unique candidate with six passing checks must be accepted")
        effective = _timeline(self.effective_at)
        knowledge = _timeline(self.knowledge_at)
        recorded = _timeline(self.recorded_at)
        if knowledge < effective or recorded < knowledge:
            raise ValueError("legacy fact match clocks are inconsistent")
        return self

    def _set_or_check_digest(
        self,
        field_name: Literal[
            "fact_payload_fingerprint_sha256",
            "original_locator_sha256",
            "relocated_locator_sha256",
            "candidate_manifest_sha256",
        ],
        expected: str,
    ) -> None:
        supplied = getattr(self, field_name)
        if supplied is None:
            object.__setattr__(self, field_name, expected)
        elif supplied != expected:
            raise ValueError(f"{field_name} must match canonical JSON")

    def _validate_optional_locator(
        self,
        digest_field: Literal[
            "original_locator_sha256",
            "relocated_locator_sha256",
        ],
        locator: OriginalFactLocator | CompanyFactsRelocatedLocator | None,
    ) -> None:
        supplied = getattr(self, digest_field)
        if locator is None:
            if supplied is not None:
                raise ValueError(f"{digest_field} requires its locator")
            return
        self._set_or_check_digest(digest_field, locator.canonical_sha256)


@dataclass(frozen=True, slots=True)
class PersistResult:
    record_id: str
    created: bool


def _matches(
    existing: tuple[object, ...],
    expected: tuple[object, ...],
) -> bool:
    if len(existing) != len(expected):
        return False
    for stored, supplied in zip(existing, expected, strict=True):
        if isinstance(supplied, datetime):
            try:
                stored_time = datetime.fromisoformat(str(stored))
            except ValueError:
                return False
            if _timeline(stored_time) != _timeline(supplied):
                return False
        elif stored != supplied:
            return False
    return True


class LegacyFactEvidenceMatchLedger:
    """Exact-replay-only writer for legacy fact evidence match revisions."""

    _COLUMNS = (
        "match_revision_id",
        "idempotency_key",
        "fact_table",
        "fact_row_id",
        "issuer_id",
        "revision",
        "fact_payload_json",
        "fact_payload_fingerprint_sha256",
        "original_locator_json",
        "original_locator_sha256",
        "relocated_locator_json",
        "relocated_locator_sha256",
        "legacy_binding_revision_id",
        "legacy_binding_revision",
        "binding_scope_content_sha256",
        "evidence_node_id",
        "matched_entry_sha256",
        "candidate_manifest_json",
        "candidate_manifest_sha256",
        "candidate_count",
        "matched_candidate_count",
        "issuer_check",
        "context_check",
        "unit_check",
        "sign_check",
        "fiscal_period_check",
        "value_check",
        "matcher_name",
        "matcher_version",
        "matcher_config_sha256",
        "outcome",
        "reason_code",
        "reason_details_json",
        "effective_at",
        "knowledge_at",
        "recorded_at",
        "supersedes_match_revision_id",
    )

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def persist(
        self,
        record: LegacyFactEvidenceMatchRevision,
    ) -> PersistResult:
        validated = LegacyFactEvidenceMatchRevision.model_validate(record.model_dump())
        current_payload = self._read_fact_payload(
            validated.fact_table,
            validated.fact_row_id,
        )
        if current_payload.canonical_json != validated.fact_payload.canonical_json:
            raise ValueError("legacy fact payload does not exactly match the current fact row")
        values = self._values(validated)
        placeholders = ",".join("?" for _ in self._COLUMNS)
        cursor = self._conn.execute(
            "INSERT INTO legacy_fact_evidence_match_revisions "  # nosec B608 -- trusted internal SQL shape; values remain bound
            f"({','.join(self._COLUMNS)}) VALUES ({placeholders}) "
            "ON CONFLICT DO NOTHING",
            values,
        )
        if cursor.rowcount == 1:
            return PersistResult(validated.match_revision_id, True)
        existing = self._conn.execute(
            "SELECT " + ",".join(self._COLUMNS) + " "  # nosec B608 -- trusted internal SQL shape; values remain bound
            "FROM legacy_fact_evidence_match_revisions "
            "WHERE idempotency_key = ?",
            (validated.idempotency_key,),
        ).fetchone()
        if existing is None or not _matches(tuple(existing), values):
            raise ValueError("legacy fact match idempotency key conflicts with immutable data")
        return PersistResult(validated.match_revision_id, False)

    @staticmethod
    def _values(
        record: LegacyFactEvidenceMatchRevision,
    ) -> tuple[object, ...]:
        return (
            record.match_revision_id,
            record.idempotency_key,
            record.fact_table,
            record.fact_row_id,
            record.issuer_id,
            record.revision,
            record.fact_payload.canonical_json,
            record.fact_payload_fingerprint_sha256,
            (None if record.original_locator is None else record.original_locator.canonical_json),
            record.original_locator_sha256,
            (None if record.relocated_locator is None else record.relocated_locator.canonical_json),
            record.relocated_locator_sha256,
            record.legacy_binding_revision_id,
            record.legacy_binding_revision,
            record.binding_scope_content_sha256,
            record.evidence_node_id,
            record.matched_entry_sha256,
            record.candidate_manifest.canonical_json,
            record.candidate_manifest_sha256,
            record.candidate_count,
            record.matched_candidate_count,
            record.issuer_check,
            record.context_check,
            record.unit_check,
            record.sign_check,
            record.fiscal_period_check,
            record.value_check,
            record.matcher_name,
            record.matcher_version,
            record.matcher_config_sha256,
            record.outcome,
            record.reason_code,
            record.reason_details.canonical_json,
            record.effective_at,
            record.knowledge_at,
            record.recorded_at,
            record.supersedes_match_revision_id,
        )

    def _read_fact_payload(
        self,
        fact_table: FactTable,
        fact_row_id: int,
    ) -> FinancialFactPayloadV1 | KpiFactPayloadV1:
        columns: tuple[str, ...] = (
            (
                "id",
                "ticker",
                "period_end",
                "fiscal_period_type",
                "line_item",
                "value",
                "currency",
                "unit",
                "source_doc_id",
                "extracted_by",
                "locator",
            )
            if fact_table == "financial_facts"
            else (
                "id",
                "ticker",
                "period_end",
                "fiscal_period_type",
                "kpi_definition_id",
                "value",
                "unit",
                "source_doc_id",
                "extracted_by",
                "locator",
                "source_excerpt",
                "computed_from",
                "formula_id",
                "formula_version",
            )
        )
        if fact_table == "kpi_facts":
            kpi_columns = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(kpi_facts)").fetchall()
            }
            if "currency" in kpi_columns:
                currency_index = columns.index("unit")
                columns = (*columns[:currency_index], "currency", *columns[currency_index:])
        row = self._conn.execute(
            f"SELECT {','.join(columns)} FROM {fact_table} WHERE id = ?",  # nosec B608 -- trusted internal SQL shape; values remain bound
            (fact_row_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"{fact_table} row {fact_row_id} does not exist")
        values = dict(zip(columns, tuple(row), strict=True))
        locator_raw = values.pop("locator")
        locator = (
            None
            if locator_raw is None
            else OriginalFactLocator.model_validate_json(str(locator_raw))
        )
        payload_values: dict[str, object] = {
            key: (str(value) if key in {"period_end", "value"} else value)
            for key, value in values.items()
        }
        if fact_table == "kpi_facts":
            payload_values.setdefault("currency", None)
        loaded_fact_row_id = payload_values.pop("id")
        if not isinstance(loaded_fact_row_id, int):
            raise ValueError(f"{fact_table} row id must be an integer")
        payload_values.update(
            {
                "schema_version": (
                    "financial_fact_payload.v1"
                    if fact_table == "financial_facts"
                    else "kpi_fact_payload.v1"
                ),
                "fact_table": fact_table,
                "fact_row_id": loaded_fact_row_id,
                "locator": locator,
            }
        )
        if fact_table == "financial_facts":
            return FinancialFactPayloadV1.model_validate(payload_values)
        return KpiFactPayloadV1.model_validate(payload_values)
