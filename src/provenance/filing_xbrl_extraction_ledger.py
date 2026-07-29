"""Atomic durable publication for normalized filing-XBRL dispositions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    field_validator,
    model_validator,
)

from provenance.filing_xbrl_fact_adapter import (
    FILING_XBRL_NORMALIZED_OUTPUT_SCHEMA,
    FILING_XBRL_NORMALIZED_OUTPUT_VERSION,
    DuplicateFilingXbrlDisposition,
    FilingXbrlAdapterResult,
    FilingXbrlFactAdapter,
    FilingXbrlNormalizedOutput,
    PublishedFilingXbrlDisposition,
)
from provenance.source_fact_repository import (
    PublicationReceipt,
    SourceFactRepository,
)

LedgerDisposition = Literal["published", "duplicate", "quarantined"]
_DISPOSITION_COLUMNS = (
    "disposition_id",
    "idempotency_key",
    "extraction_run_id",
    "input_ordinal",
    "canonical_normalized_entry_json",
    "normalized_entry_sha256",
    "normalized_entry_identity_sha256",
    "source_entry_sha256",
    "source_locator_sha256",
    "disposition",
    "observation_id",
    "primary_input_ordinal",
    "quarantine_reason_code",
    "quarantine_reason_details_json",
    "quarantine_reason_details_sha256",
    "canonical_disposition_json",
    "disposition_sha256",
    "knowledge_at",
    "recorded_at",
)
_SEAL_COLUMNS = (
    "disposition_seal_id",
    "idempotency_key",
    "extraction_run_id",
    "publication_id",
    "normalized_output_schema_name",
    "normalized_output_schema_version",
    "extraction_output_sha256",
    "entry_count",
    "published_count",
    "duplicate_count",
    "quarantined_count",
    "canonical_disposition_set_json",
    "disposition_set_sha256",
    "completeness_policy_sha256",
    "knowledge_at",
    "recorded_at",
)
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_JSON_ARRAY = TypeAdapter(list[JsonValue])


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
    if not isinstance(value, str):
        value = _canonical_json(value)
    return hashlib.sha256(value.encode()).hexdigest()


def _validate_sha256(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("must be a lowercase SHA-256 hex digest")
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class FilingXbrlExtractionDispositionRecord(_FrozenModel):
    disposition_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    extraction_run_id: str = Field(min_length=1, max_length=128)
    input_ordinal: int = Field(ge=0)
    canonical_normalized_entry_json: str = Field(min_length=2)
    normalized_entry_sha256: str
    normalized_entry_identity_sha256: str
    source_entry_sha256: str
    source_locator_sha256: str
    disposition: LedgerDisposition
    observation_id: str | None = Field(default=None, min_length=1, max_length=128)
    primary_input_ordinal: int | None = Field(default=None, ge=0)
    quarantine_reason_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    quarantine_reason_details_json: str | None = Field(default=None, min_length=2)
    quarantine_reason_details_sha256: str | None = None
    canonical_disposition_json: str = Field(min_length=2)
    disposition_sha256: str
    knowledge_at: datetime
    recorded_at: datetime

    _entry_sha = field_validator("normalized_entry_sha256")(_validate_sha256)
    _identity_sha = field_validator("normalized_entry_identity_sha256")(_validate_sha256)
    _source_sha = field_validator("source_entry_sha256")(_validate_sha256)
    _locator_sha = field_validator("source_locator_sha256")(_validate_sha256)
    _disposition_sha = field_validator("disposition_sha256")(_validate_sha256)

    @field_validator("quarantine_reason_details_sha256")
    @classmethod
    def _optional_sha(cls, value: str | None) -> str | None:
        return None if value is None else _validate_sha256(value)

    @model_validator(mode="after")
    def _exact_shape(self) -> Self:
        if self.disposition == "published":
            if self.observation_id is None or any(
                item is not None
                for item in (
                    self.quarantine_reason_code,
                    self.quarantine_reason_details_json,
                    self.quarantine_reason_details_sha256,
                    self.primary_input_ordinal,
                )
            ):
                raise ValueError("published disposition has invalid shape")
        elif self.disposition == "duplicate":
            if (
                self.observation_id is None
                or self.primary_input_ordinal is None
                or self.primary_input_ordinal >= self.input_ordinal
                or any(
                    item is not None
                    for item in (
                        self.quarantine_reason_code,
                        self.quarantine_reason_details_json,
                        self.quarantine_reason_details_sha256,
                    )
                )
            ):
                raise ValueError("duplicate disposition has invalid shape")
        elif (
            self.observation_id is not None
            or self.primary_input_ordinal is not None
            or self.quarantine_reason_code is None
            or self.quarantine_reason_details_json is None
            or self.quarantine_reason_details_sha256 is None
        ):
            raise ValueError("quarantined disposition has invalid shape")
        if _digest(self.canonical_normalized_entry_json) != (self.normalized_entry_sha256):
            raise ValueError("normalized entry digest mismatch")
        normalized_entry = _JSON_OBJECT.validate_json(self.canonical_normalized_entry_json)
        normalized_entry.pop("ordinal", None)
        if _digest(normalized_entry) != self.normalized_entry_identity_sha256:
            raise ValueError("normalized entry identity digest mismatch")
        if (
            self.quarantine_reason_details_json is not None
            and _digest(self.quarantine_reason_details_json)
            != self.quarantine_reason_details_sha256
        ):
            raise ValueError("quarantine detail digest mismatch")
        expected = _canonical_json(
            {
                "disposition": self.disposition,
                "normalized_entry_identity_sha256": (self.normalized_entry_identity_sha256),
                "normalized_entry_sha256": self.normalized_entry_sha256,
                "observation_id": self.observation_id,
                "ordinal": self.input_ordinal,
                "primary_input_ordinal": self.primary_input_ordinal,
                "quarantine_reason_code": self.quarantine_reason_code,
                "quarantine_reason_details_sha256": (self.quarantine_reason_details_sha256),
                "source_entry_sha256": self.source_entry_sha256,
                "source_locator_sha256": self.source_locator_sha256,
            }
        )
        if self.canonical_disposition_json != expected:
            raise ValueError("canonical disposition payload mismatch")
        if _digest(expected) != self.disposition_sha256:
            raise ValueError("disposition digest mismatch")
        if _utc(self.recorded_at) < _utc(self.knowledge_at):
            raise ValueError("disposition clocks are inconsistent")
        return self

    @property
    def database_values(self) -> tuple[object, ...]:
        return tuple(getattr(self, column) for column in _DISPOSITION_COLUMNS)


class FilingXbrlExtractionDispositionSeal(_FrozenModel):
    disposition_seal_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)
    extraction_run_id: str = Field(min_length=1, max_length=128)
    publication_id: str = Field(min_length=1, max_length=128)
    normalized_output_schema_name: Literal["filing_xbrl_normalized_output"] = (
        FILING_XBRL_NORMALIZED_OUTPUT_SCHEMA
    )
    normalized_output_schema_version: Literal["v1"] = FILING_XBRL_NORMALIZED_OUTPUT_VERSION
    extraction_output_sha256: str
    entry_count: int = Field(ge=0)
    published_count: int = Field(ge=0)
    duplicate_count: int = Field(ge=0)
    quarantined_count: int = Field(ge=0)
    canonical_disposition_set_json: str = Field(min_length=2)
    disposition_set_sha256: str
    completeness_policy_sha256: str
    knowledge_at: datetime
    recorded_at: datetime

    _output_sha = field_validator("extraction_output_sha256")(_validate_sha256)
    _set_sha = field_validator("disposition_set_sha256")(_validate_sha256)
    _policy_sha = field_validator("completeness_policy_sha256")(_validate_sha256)

    @model_validator(mode="after")
    def _exact_seal(self) -> Self:
        if self.entry_count != (
            self.published_count + self.duplicate_count + self.quarantined_count
        ):
            raise ValueError("disposition seal counts do not reconcile")
        try:
            payload = _JSON_ARRAY.validate_json(self.canonical_disposition_set_json)
        except ValueError as exc:
            raise ValueError("disposition set must be valid JSON") from exc
        if len(payload) != self.entry_count:
            raise ValueError("disposition set count does not reconcile")
        if _canonical_json(payload) != self.canonical_disposition_set_json:
            raise ValueError("disposition set must be canonical JSON")
        if _digest(self.canonical_disposition_set_json) != (self.disposition_set_sha256):
            raise ValueError("disposition set digest mismatch")
        if _utc(self.recorded_at) < _utc(self.knowledge_at):
            raise ValueError("disposition seal clocks are inconsistent")
        return self

    @property
    def database_values(self) -> tuple[object, ...]:
        return tuple(getattr(self, column) for column in _SEAL_COLUMNS)


class FilingXbrlExtractionLedgerReceipt(_FrozenModel):
    publication_receipt: PublicationReceipt
    extraction_run_id: str
    disposition_seal_id: str
    disposition_ids: tuple[str, ...]
    entry_count: int = Field(ge=0)
    published_count: int = Field(ge=0)
    duplicate_count: int = Field(ge=0)
    quarantined_count: int = Field(ge=0)
    disposition_set_sha256: str
    exact_replay: bool

    _set_sha = field_validator("disposition_set_sha256")(_validate_sha256)


class FilingXbrlExtractionLedger:
    """Adapt, publish, dispose, and seal one normalized run atomically."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        adapter: FilingXbrlFactAdapter | None = None,
    ) -> None:
        self._conn = conn
        self._adapter = adapter or FilingXbrlFactAdapter()

    def publish(
        self,
        output: FilingXbrlNormalizedOutput,
    ) -> FilingXbrlExtractionLedgerReceipt:
        self._conn.execute("SAVEPOINT publish_filing_xbrl_extraction")
        try:
            adapted = self._adapter.adapt(output)
            result = FilingXbrlAdapterResult.model_validate(adapted.model_dump(mode="python"))
            self._require_exact_adapter_result(output, result)
            publication_receipt = SourceFactRepository(self._conn).publish(result.publication)
            records = self.build_disposition_records(output, result)
            created = tuple(self._persist_disposition(item) for item in records)
            seal = self._seal(output, result, records)
            seal_created = self._persist_seal(seal)
        except Exception:
            self._conn.execute("ROLLBACK TO SAVEPOINT publish_filing_xbrl_extraction")
            self._conn.execute("RELEASE SAVEPOINT publish_filing_xbrl_extraction")
            raise
        self._conn.execute("RELEASE SAVEPOINT publish_filing_xbrl_extraction")
        return FilingXbrlExtractionLedgerReceipt(
            publication_receipt=publication_receipt,
            extraction_run_id=output.extraction.extraction_run_id,
            disposition_seal_id=seal.disposition_seal_id,
            disposition_ids=tuple(item.disposition_id for item in records),
            entry_count=seal.entry_count,
            published_count=seal.published_count,
            duplicate_count=seal.duplicate_count,
            quarantined_count=seal.quarantined_count,
            disposition_set_sha256=seal.disposition_set_sha256,
            exact_replay=(
                publication_receipt.exact_replay and not any(created) and not seal_created
            ),
        )

    @staticmethod
    def _require_exact_adapter_result(
        output: FilingXbrlNormalizedOutput,
        result: FilingXbrlAdapterResult,
    ) -> None:
        extraction_seals = result.publication.extraction_seals
        if (
            len(extraction_seals) != 1
            or extraction_seals[0].extraction_run_id != output.extraction.extraction_run_id
        ):
            raise ValueError("filing-XBRL publication must seal the represented extraction run")
        entries = tuple(
            sorted((*output.entries, *output.rejections), key=lambda item: item.ordinal)
        )
        if tuple(item.ordinal for item in entries) != tuple(
            item.ordinal for item in result.entry_commitments
        ):
            raise ValueError("adapter commitments must exactly cover normalized entries")
        for entry, commitment in zip(
            entries,
            result.entry_commitments,
            strict=True,
        ):
            canonical_entry = _canonical_json(entry)
            entry_identity = entry.model_dump(
                mode="json",
                exclude={"ordinal"},
            )
            if (
                commitment.canonical_normalized_entry_json != canonical_entry
                or commitment.normalized_entry_sha256 != _digest(canonical_entry)
                or commitment.normalized_entry_identity_sha256 != _digest(entry_identity)
                or commitment.source_entry_sha256 != entry.source_entry_sha256
                or commitment.source_locator_sha256 != entry.source_locator_sha256
            ):
                raise ValueError("adapter commitment does not exactly match normalized entry")

    @staticmethod
    def build_disposition_records(
        output: FilingXbrlNormalizedOutput,
        result: FilingXbrlAdapterResult,
    ) -> tuple[FilingXbrlExtractionDispositionRecord, ...]:
        entries_by_ordinal = {
            item.ordinal: item for item in (*output.entries, *output.rejections)
        }
        records: list[FilingXbrlExtractionDispositionRecord] = []
        for commitment in result.entry_commitments:
            entry = entries_by_ordinal[commitment.ordinal]
            disposition = commitment.disposition
            if isinstance(disposition, PublishedFilingXbrlDisposition):
                kind: LedgerDisposition = "published"
                observation_id = disposition.observation_id
                primary_input_ordinal = None
                reason_code = None
                reason_json = None
                reason_sha256 = None
            elif isinstance(disposition, DuplicateFilingXbrlDisposition):
                kind = "duplicate"
                observation_id = disposition.observation_id
                primary_input_ordinal = disposition.primary_ordinal
                reason_code = None
                reason_json = None
                reason_sha256 = None
            else:
                kind = "quarantined"
                observation_id = None
                primary_input_ordinal = None
                reason_code = disposition.reason
                reason_json = _canonical_json({"detail": disposition.detail})
                reason_sha256 = _digest(reason_json)
            canonical_disposition_json = _canonical_json(
                {
                    "disposition": kind,
                    "normalized_entry_identity_sha256": (
                        commitment.normalized_entry_identity_sha256
                    ),
                    "normalized_entry_sha256": (commitment.normalized_entry_sha256),
                    "observation_id": observation_id,
                    "ordinal": commitment.ordinal,
                    "primary_input_ordinal": primary_input_ordinal,
                    "quarantine_reason_code": reason_code,
                    "quarantine_reason_details_sha256": reason_sha256,
                    "source_entry_sha256": commitment.source_entry_sha256,
                    "source_locator_sha256": commitment.source_locator_sha256,
                }
            )
            identity = f"{output.extraction.extraction_run_id}|{commitment.ordinal}"
            record_digest = _digest(identity)
            records.append(
                FilingXbrlExtractionDispositionRecord(
                    disposition_id=f"fxd_{record_digest}",
                    idempotency_key=f"fxdk_{record_digest}",
                    extraction_run_id=output.extraction.extraction_run_id,
                    input_ordinal=commitment.ordinal,
                    canonical_normalized_entry_json=(commitment.canonical_normalized_entry_json),
                    normalized_entry_sha256=(commitment.normalized_entry_sha256),
                    normalized_entry_identity_sha256=(commitment.normalized_entry_identity_sha256),
                    source_entry_sha256=commitment.source_entry_sha256,
                    source_locator_sha256=commitment.source_locator_sha256,
                    disposition=kind,
                    observation_id=observation_id,
                    primary_input_ordinal=primary_input_ordinal,
                    quarantine_reason_code=reason_code,
                    quarantine_reason_details_json=reason_json,
                    quarantine_reason_details_sha256=reason_sha256,
                    canonical_disposition_json=canonical_disposition_json,
                    disposition_sha256=_digest(canonical_disposition_json),
                    knowledge_at=entry.knowledge_at,
                    recorded_at=entry.recorded_at,
                )
            )
        return tuple(records)

    @staticmethod
    def _seal(
        output: FilingXbrlNormalizedOutput,
        result: FilingXbrlAdapterResult,
        records: tuple[FilingXbrlExtractionDispositionRecord, ...],
    ) -> FilingXbrlExtractionDispositionSeal:
        extraction_seals = result.publication.extraction_seals
        if len(extraction_seals) != 1:
            raise ValueError("filing-XBRL publication requires exactly one completeness seal")
        disposition_set_json = _canonical_json(
            [_JSON_OBJECT.validate_json(item.canonical_disposition_json) for item in records]
        )
        seal_identity = _digest(
            {
                "extraction_run_id": output.extraction.extraction_run_id,
                "publication_id": result.publication.publication_id,
                "schema_name": output.normalized_output_schema_name,
                "schema_version": output.normalized_output_schema_version,
            }
        )
        return FilingXbrlExtractionDispositionSeal(
            disposition_seal_id=f"fxds_{seal_identity}",
            idempotency_key=f"fxdsk_{seal_identity}",
            extraction_run_id=output.extraction.extraction_run_id,
            publication_id=result.publication.publication_id,
            extraction_output_sha256=output.canonical_payload_sha256,
            entry_count=result.total_count,
            published_count=result.published_count,
            duplicate_count=result.duplicate_count,
            quarantined_count=result.quarantined_count,
            canonical_disposition_set_json=disposition_set_json,
            disposition_set_sha256=_digest(disposition_set_json),
            completeness_policy_sha256=(extraction_seals[0].completeness_policy_sha256),
            knowledge_at=output.extraction.knowledge_at,
            recorded_at=output.extraction.recorded_at,
        )

    def _persist_disposition(
        self,
        record: FilingXbrlExtractionDispositionRecord,
    ) -> bool:
        existing = self._conn.execute(
            "SELECT "  # nosec B608 -- trusted internal SQL shape; values remain bound
            + ",".join(_DISPOSITION_COLUMNS)
            + " FROM filing_xbrl_extraction_dispositions "
            "WHERE disposition_id = ? OR idempotency_key = ? "
            "OR (extraction_run_id = ? AND input_ordinal = ?)",
            (
                record.disposition_id,
                record.idempotency_key,
                record.extraction_run_id,
                record.input_ordinal,
            ),
        ).fetchall()
        if existing:
            if len(existing) != 1 or not self._matches(
                tuple(existing[0]),
                record.database_values,
            ):
                raise ValueError("immutable filing-XBRL disposition conflicts with stored entry")
            return False
        placeholders = ",".join("?" for _ in _DISPOSITION_COLUMNS)
        self._conn.execute(
            "INSERT INTO filing_xbrl_extraction_dispositions ("  # nosec B608 -- trusted internal SQL shape; values remain bound
            + ",".join(_DISPOSITION_COLUMNS)
            + ") VALUES ("
            + placeholders
            + ")",
            record.database_values,
        )
        return True

    def _persist_seal(
        self,
        seal: FilingXbrlExtractionDispositionSeal,
    ) -> bool:
        existing = self._conn.execute(
            "SELECT " + ",".join(_SEAL_COLUMNS) + " FROM filing_xbrl_extraction_disposition_seals "  # nosec B608 -- trusted internal SQL shape; values remain bound
            "WHERE disposition_seal_id = ? OR idempotency_key = ? "
            "OR extraction_run_id = ? OR publication_id = ?",
            (
                seal.disposition_seal_id,
                seal.idempotency_key,
                seal.extraction_run_id,
                seal.publication_id,
            ),
        ).fetchall()
        if existing:
            if len(existing) != 1 or not self._matches(
                tuple(existing[0]),
                seal.database_values,
            ):
                raise ValueError(
                    "immutable filing-XBRL disposition seal conflicts with stored extraction"
                )
            return False
        placeholders = ",".join("?" for _ in _SEAL_COLUMNS)
        self._conn.execute(
            "INSERT INTO filing_xbrl_extraction_disposition_seals ("  # nosec B608 -- trusted internal SQL shape; values remain bound
            + ",".join(_SEAL_COLUMNS)
            + ") VALUES ("
            + placeholders
            + ")",
            seal.database_values,
        )
        return True

    @staticmethod
    def _matches(
        stored: tuple[object, ...],
        supplied: tuple[object, ...],
    ) -> bool:
        if len(stored) != len(supplied):
            return False
        for existing, expected in zip(stored, supplied, strict=True):
            if isinstance(expected, datetime):
                try:
                    if _utc(datetime.fromisoformat(str(existing))) != _utc(expected):
                        return False
                except ValueError:
                    return False
            elif existing != expected:
                return False
        return True
