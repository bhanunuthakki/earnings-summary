"""Read-only, immutable evidence selection for one declared SEC analysis.

The archive remains unchanged. Capture dependencies and research documents are
distinct; selecting a period does not admit facts or waive processing lanes.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from provenance.source_coverage import ExpectedDocument
from provenance.source_inventory_seal import InventoryComponent, component_digest

_PERIODIC = frozenset({"10-K", "10-Q", "20-F", "40-F"})
_CURRENT = frozenset({"8-K", "6-K"})
_POSITIVE = frozenset({"captured", "extracted", "indexed"})
AnalysisDocumentRole = Literal["research_document", "package_dependency", "outside_scope"]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AnalysisAccessionSelection(_Frozen):
    accession_number: str = Field(pattern=r"^\d{10}-\d{2}-\d{6}$")
    reason: str = Field(min_length=1, max_length=512)

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("extra accession requires an explicit reason")
        return value


class AnalysisScopeRequest(_Frozen):
    purpose: str = Field(min_length=1, max_length=256)
    issuer_id: str = Field(min_length=1, max_length=128)
    inventory_key: str = Field(min_length=1, max_length=256)
    required_period_ends: tuple[date, ...] = Field(min_length=1)
    extra_accessions: tuple[AnalysisAccessionSelection, ...] = ()
    require_latest_period: bool = True
    cutoff_at: datetime
    observed_through: datetime

    @field_validator("cutoff_at", "observed_through")
    @classmethod
    def _aware_clock(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("analysis clocks require an explicit time zone")
        return value

    @model_validator(mode="after")
    def _request(self) -> Self:
        if any(not value.strip() for value in (self.purpose, self.issuer_id, self.inventory_key)):
            raise ValueError("analysis purpose and identity must be nonempty")
        if tuple(sorted(set(self.required_period_ends))) != self.required_period_ends:
            raise ValueError("required period ends must be sorted and unique")
        accessions = tuple(item.accession_number for item in self.extra_accessions)
        if tuple(sorted(set(accessions))) != accessions:
            raise ValueError("extra accessions must be sorted and unique")
        if _utc(self.observed_through) < _utc(self.cutoff_at):
            raise ValueError("observed_through must not precede cutoff_at")
        return self


class AnalysisInventoryIdentity(_Frozen):
    inventory_key: str
    snapshot_id: str
    revision: int = Field(gt=0)
    component_digest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AnalysisScopeEntry(_Frozen):
    expected_document: ExpectedDocument
    role: AnalysisDocumentRole
    reason: str = Field(min_length=1)

    @property
    def expected_document_id(self) -> str:
        return self.expected_document.expected_document_id

    @property
    def expected_document_key(self) -> str:
        return self.expected_document.expected_document_key


class AnalysisEvidenceScope(_Frozen):
    schema_version: Literal["analysis-evidence-scope/v1"] = "analysis-evidence-scope/v1"
    request: AnalysisScopeRequest
    inventory: AnalysisInventoryIdentity
    accession_numbers: tuple[str, ...] = Field(min_length=1)
    entries: tuple[AnalysisScopeEntry, ...] = Field(min_length=1)
    scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_id: str = Field(pattern=r"^analysis-scope:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _commitment(self) -> Self:
        keys = tuple(entry.expected_document_id for entry in self.entries)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("analysis entries must be sorted and unique")
        if self.accession_numbers != tuple(sorted(set(self.accession_numbers))):
            raise ValueError("analysis accessions must be sorted and unique")
        if self.scope_sha256 != _sha(
            self.model_dump(mode="json", exclude={"scope_sha256", "scope_id"})
        ):
            raise ValueError("analysis scope content commitment differs")
        if self.scope_id != "analysis-scope:" + self.scope_sha256:
            raise ValueError("analysis scope identity differs")
        return self


class AnalysisDocumentCoverage(_Frozen):
    expected_document_id: str
    expected_document_key: str
    issuer_id: str
    role: AnalysisDocumentRole
    coverage_status: str
    document_version_id: str | None


def _sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _clock(value: object) -> datetime:
    """Retain fractional seconds; SQLite datetime() would discard them."""
    return _utc(datetime.fromisoformat(str(value)))


def _rows(
    conn: sqlite3.Connection, query: str, parameters: tuple[object, ...]
) -> list[dict[str, object]]:
    cursor = conn.execute(query, parameters)
    names = tuple(item[0] for item in cursor.description or ())
    return [dict(zip(names, row, strict=True)) for row in cursor]


def build_analysis_scope(
    conn: sqlite3.Connection,
    request: AnalysisScopeRequest,
    *,
    require_current_inventory: bool = True,
) -> AnalysisEvidenceScope:
    """Select current evidence, or explicitly reconstruct a retained selection.

    Retained verification selects the latest inventory visible at the original
    observation clock. It does not skip a newer incomplete visible inventory.
    """
    inventory_relation = (
        "v_source_inventory_current" if require_current_inventory else "source_inventory_snapshots"
    )
    rows = _rows(
        conn,
        "SELECT inventory.*,seal.expected_component_count,seal.component_digest_sha256,"
        f"seal.completion_status,seal.sealed_at FROM {inventory_relation} inventory "  # nosec B608 -- closed internal relation selection
        "LEFT JOIN source_inventory_snapshot_seals seal ON seal.snapshot_id=inventory.snapshot_id "
        "WHERE inventory.inventory_key=?",
        (request.inventory_key,),
    )
    if not require_current_inventory:
        rows = [row for row in rows if _clock(row["recorded_at"]) <= _utc(request.observed_through)]
        if rows:
            latest_revision = max(int(str(row["revision"])) for row in rows)
            rows = [row for row in rows if int(str(row["revision"])) == latest_revision]
    if len(rows) != 1:
        raise ValueError("analysis inventory is missing or ambiguous")
    inventory = rows[0]
    if (
        inventory["issuer_id"] != request.issuer_id
        or inventory["source_kind"] != "sec_submissions"
        or inventory["authoritative"] != 1
        or inventory["outcome"] != "succeeded"
        or inventory["completion_status"] != "complete"
        or inventory["sealed_at"] is None
        or _clock(inventory["recorded_at"]) > _utc(request.observed_through)
        or _clock(inventory["sealed_at"]) > _utc(request.observed_through)
    ):
        raise ValueError("analysis requires current complete authoritative issuer SEC inventory")
    snapshot_id = str(inventory["snapshot_id"])
    components = tuple(
        InventoryComponent.model_validate(row)
        for row in _rows(
            conn,
            "SELECT * FROM source_inventory_components WHERE snapshot_id=? ORDER BY ordinal,component_key",
            (snapshot_id,),
        )
        if require_current_inventory or _clock(row["recorded_at"]) <= _utc(request.observed_through)
    )
    if (
        len(components) != inventory["expected_component_count"]
        or component_digest(components) != inventory["component_digest_sha256"]
        or any(item.required and item.outcome != "succeeded" for item in components)
        or any(_utc(item.recorded_at) > _utc(request.observed_through) for item in components)
    ):
        raise ValueError("analysis inventory seal does not match its component population")
    documents = tuple(
        ExpectedDocument.model_validate(row)
        for row in _rows(
            conn,
            "SELECT * FROM expected_documents WHERE snapshot_id=? ORDER BY expected_document_id",
            (snapshot_id,),
        )
        if require_current_inventory or _clock(row["recorded_at"]) <= _utc(request.observed_through)
    )
    if not documents or any(
        item.issuer_id != request.issuer_id
        or item.source_kind != "sec_filing"
        or item.expectation_basis != "authoritative"
        or _utc(item.recorded_at) > _utc(request.observed_through)
        for item in documents
    ):
        raise ValueError(
            "analysis expected documents have invalid issuer, authority or observation time"
        )
    primaries = tuple(item for item in documents if item.document_type == "filing")
    if any(item.filing_at is None for item in primaries):
        raise ValueError("analysis primary filing has unknown publication time")
    observed = tuple(
        item
        for item in primaries
        if item.filing_at is not None and _utc(item.filing_at) <= _utc(request.cutoff_at)
    )
    periodic = tuple(
        item for item in observed if (item.form_type or "").removesuffix("/A") in _PERIODIC
    )
    known_periodic = tuple(item for item in periodic if item.period_end is not None)
    newest_known = max(
        (_utc(item.filing_at) for item in known_periodic if item.filing_at is not None),
        default=None,
    )
    declared_bases: dict[date, ExpectedDocument] = {}
    for period in request.required_period_ends:
        bases = tuple(
            item
            for item in known_periodic
            if item.period_end is not None
            and item.period_end.date() == period
            and item.form_type in _PERIODIC
        )
        if len(bases) != 1:
            raise ValueError("required reporting period has absent or ambiguous base filing")
        declared_bases[period] = bases[0]
    earliest_declared_filing = min(
        _utc(item.filing_at) for item in declared_bases.values() if item.filing_at is not None
    )
    if any(
        item.period_end is None
        and (
            (
                (item.form_type or "").endswith("/A")
                and item.filing_at is not None
                and _utc(item.filing_at) >= earliest_declared_filing
            )
            or newest_known is None
            or (
                not (item.form_type or "").endswith("/A")
                and item.filing_at is not None
                and _utc(item.filing_at) >= newest_known
            )
        )
        for item in periodic
    ):
        raise ValueError(
            "analysis current periodic filing or amendment has unknown reporting period"
        )
    periods = {item.period_end.date() for item in periodic if item.period_end is not None}
    if not periods or (
        request.require_latest_period and max(periods) not in request.required_period_ends
    ):
        raise ValueError("analysis does not include latest observed reporting period")
    selected: dict[str, str] = {}
    for period in request.required_period_ends:
        candidates = tuple(
            item
            for item in periodic
            if item.period_end is not None and item.period_end.date() == period
        )
        base = declared_bases[period]
        for item in candidates:
            if (item.form_type or "").removesuffix(
                "/A"
            ) != base.form_type or item.accession_number is None:
                raise ValueError("analysis amendment or base filing identity is ambiguous")
            selected[item.accession_number] = "required_reporting_period:" + period.isoformat()
    for extra in request.extra_accessions:
        matching = tuple(
            item for item in observed if item.accession_number == extra.accession_number
        )
        if (
            len(matching) != 1
            or (matching[0].form_type or "").removesuffix("/A") not in _PERIODIC | _CURRENT
        ):
            raise ValueError(
                "extra accession is missing, ambiguous, future or outside reporting policy"
            )
        extra_document = matching[0]
        if (extra_document.form_type or "").endswith("/A") and (
            extra_document.form_type or ""
        ).removesuffix("/A") in _CURRENT:
            raise ValueError(
                "current-report amendment requires related-original evidence unavailable in inventory"
            )
        if (extra_document.form_type or "").removesuffix("/A") in _PERIODIC and (
            extra_document.period_end is None
            or extra_document.period_end.date() not in request.required_period_ends
        ):
            raise ValueError(
                "extra periodic filing must belong to a declared required reporting period"
            )
        selected[extra.accession_number] = "explicit_related_evidence:" + extra.reason
    for accession in selected:
        package_primary = tuple(item for item in primaries if item.accession_number == accession)
        if len(package_primary) != 1:
            raise ValueError("selected accession requires exactly one primary")
        primary = package_primary[0]
        if any(
            item.form_type != primary.form_type
            or item.period_start != primary.period_start
            or item.period_end != primary.period_end
            for item in documents
            if item.accession_number == accession
        ):
            raise ValueError(
                "selected package member form or reporting period differs from primary"
            )
    entries: list[AnalysisScopeEntry] = []
    for document in documents:
        accession = document.accession_number
        if accession in selected:
            if not document.source_url or not document.primary_document:
                raise ValueError("selected package member lacks source identity")
            role: AnalysisDocumentRole = (
                "research_document" if document.document_type == "filing" else "package_dependency"
            )
            reason = selected[accession]
        else:
            role = "outside_scope"
            reason = (
                "outside_declared_analysis_periods_and_related_accessions"
                if document.period_end is not None
                else "outside_scope_reporting_period_unknown"
            )
        entries.append(AnalysisScopeEntry(expected_document=document, role=role, reason=reason))
    identity = AnalysisInventoryIdentity(
        inventory_key=request.inventory_key,
        snapshot_id=snapshot_id,
        revision=int(str(inventory["revision"])),
        component_digest_sha256=str(inventory["component_digest_sha256"]),
    )
    material = {
        "schema_version": "analysis-evidence-scope/v1",
        "request": request.model_dump(mode="json"),
        "inventory": identity.model_dump(mode="json"),
        "accession_numbers": sorted(selected),
        "entries": [entry.model_dump(mode="json") for entry in entries],
    }
    digest = _sha(material)
    return AnalysisEvidenceScope.model_validate(
        material | {"scope_sha256": digest, "scope_id": "analysis-scope:" + digest}
    )


def verify_analysis_scope(
    conn: sqlite3.Connection,
    scope: AnalysisEvidenceScope,
    *,
    require_current_inventory: bool = True,
) -> None:
    """Recompute the selection; a self-rehashed fabricated receipt cannot pass."""
    if (
        build_analysis_scope(
            conn, scope.request, require_current_inventory=require_current_inventory
        )
        != scope
    ):
        raise ValueError("analysis scope is stale, changed or fabricated")


def resolve_analysis_coverage(
    conn: sqlite3.Connection,
    scope: AnalysisEvidenceScope,
    cutoff_at: datetime,
    observed_through: datetime,
    *,
    require_current_inventory: bool = True,
) -> tuple[AnalysisDocumentCoverage, ...]:
    """Read selected and outside coverage without changing the archive."""
    verify_analysis_scope(conn, scope, require_current_inventory=require_current_inventory)
    if _utc(cutoff_at) != _utc(scope.request.cutoff_at) or _utc(observed_through) < _utc(
        scope.request.observed_through
    ):
        raise ValueError("analysis coverage clocks differ from its selection")
    result: list[AnalysisDocumentCoverage] = []
    for entry in scope.entries:
        expected = entry.expected_document
        rows = _rows(
            conn,
            "SELECT coverage_status,document_version_id,knowledge_at,recorded_at "
            "FROM source_coverage_assessments WHERE expected_document_id=? ORDER BY revision DESC",
            (expected.expected_document_id,),
        )
        rows = [
            row
            for row in rows
            if _clock(row["knowledge_at"]) <= _utc(cutoff_at)
            and _clock(row["recorded_at"]) <= _utc(observed_through)
        ]
        status = str(rows[0]["coverage_status"]) if rows else "unassessed"
        document_id = (
            None
            if not rows or rows[0]["document_version_id"] is None
            else str(rows[0]["document_version_id"])
        )
        if entry.role != "outside_scope" and status in _POSITIVE:
            if document_id is None:
                raise ValueError("positive analysis coverage lacks document version")
            evidence = _rows(
                conn,
                "SELECT document.*,observation.retrieved_at,observation.source_url,"
                "observation.blob_sha256 AS observation_blob,blob.recorded_at AS blob_recorded_at "
                "FROM v_evidence_document_versions_canonical document "
                "JOIN evidence_source_observations observation ON observation.observation_id=document.observation_id "
                "JOIN evidence_content_blobs blob ON blob.sha256=document.blob_sha256 "
                "WHERE document.document_version_id=?",
                (document_id,),
            )
            if len(evidence) != 1:
                raise ValueError("analysis document canonical identity is missing or ambiguous")
            actual = evidence[0]
            for name in (
                "issuer_id",
                "document_type",
                "form_type",
                "accession_number",
                "source_url",
            ):
                if actual[name] != getattr(expected, name):
                    raise ValueError("analysis document does not match expected source identity")
            for name in ("period_start", "period_end"):
                wanted = getattr(expected, name)
                value = actual[name]
                parsed = None if value is None else _utc(datetime.fromisoformat(str(value)))
                if parsed != (None if wanted is None else _utc(wanted)):
                    raise ValueError("analysis document period differs from sealed expectation")
            if _utc(datetime.fromisoformat(str(actual["retrieved_at"]))) > _utc(cutoff_at):
                raise ValueError("analysis document source observation exceeds knowledge cutoff")
            if actual["blob_sha256"] != actual["observation_blob"] or any(
                _utc(datetime.fromisoformat(str(actual[name]))) > _utc(observed_through)
                for name in ("recorded_at", "retrieved_at", "blob_recorded_at")
            ):
                raise ValueError("analysis document bytes or observation clocks differ")
        result.append(
            AnalysisDocumentCoverage(
                expected_document_id=expected.expected_document_id,
                expected_document_key=expected.expected_document_key,
                issuer_id=expected.issuer_id,
                role=entry.role,
                coverage_status=status,
                document_version_id=document_id,
            )
        )
    return tuple(result)


def require_analysis_documents(
    conn: sqlite3.Connection,
    scope: AnalysisEvidenceScope,
    cutoff_at: datetime,
    observed_through: datetime,
    *,
    require_current_inventory: bool = True,
) -> tuple[str, ...]:
    """Require every selected dependency; return only admitted research documents."""
    coverage = resolve_analysis_coverage(
        conn,
        scope,
        cutoff_at,
        observed_through,
        require_current_inventory=require_current_inventory,
    )
    if any(
        item.role != "outside_scope"
        and (item.coverage_status not in _POSITIVE or item.document_version_id is None)
        for item in coverage
    ):
        raise ValueError("analysis selected package capture is incomplete")
    ids = tuple(
        sorted(
            item.document_version_id
            for item in coverage
            if item.role == "research_document" and item.document_version_id is not None
        )
    )
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("analysis research document versions are empty or duplicated")
    return ids
