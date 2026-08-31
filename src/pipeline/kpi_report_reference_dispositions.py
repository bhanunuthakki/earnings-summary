"""Exact, append-only dispositions for KPI references in owner report configuration."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ReportKpiReferenceKind(StrEnum):
    CHART_PRIORITY = "chart_priority"
    TIER_1_KPI = "tier_1_kpi"
    TIER_2_KPI = "tier_2_kpi"
    TIER_3_KPI = "tier_3_kpi"
    BREAK_RULE = "break_rule"


class ReportKpiReferenceStatus(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    RETIRED = "retired"


class ReportKpiReferenceResolutionMethod(StrEnum):
    EXACT_DEFINITION_IDENTITY = "exact_definition_identity"
    UNIT_SURFACE_ALIAS = "unit_surface_alias"


class ReportKpiReferenceSourceStatus(StrEnum):
    VALID = "valid"
    MISSING = "missing"
    INVALID = "invalid"


class ReportKpiReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str = Field(min_length=1, max_length=32)
    source_path: str = Field(min_length=1, max_length=512)
    json_pointer: str = Field(min_length=1, max_length=512)
    reference_kind: ReportKpiReferenceKind
    requested_label: str = Field(min_length=1, max_length=256)
    reference_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _content_hash_matches(self) -> Self:
        expected = hashlib.sha256(self.requested_label.encode("utf-8")).hexdigest()
        if self.reference_content_sha256 != expected:
            raise ValueError("report KPI reference content hash mismatch")
        return self


class ReportKpiReferenceDisposition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ReportKpiReferenceStatus
    kpi_definition_id: int | None = Field(default=None, gt=0)
    definition_identity_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    evidence_fact_id: int | None = Field(default=None, gt=0)
    evidence_context_id: int | None = Field(default=None, gt=0)
    evidence_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    resolution_method: ReportKpiReferenceResolutionMethod | None = None
    policy_name: str | None = Field(default=None, min_length=1, max_length=128)
    policy_version: str | None = Field(default=None, min_length=1, max_length=64)
    policy_config_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reason_code: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _status_shape(self) -> Self:
        binding = (
            self.kpi_definition_id,
            self.definition_identity_sha256,
            self.evidence_fact_id,
            self.evidence_context_id,
            self.evidence_sha256,
            self.resolution_method,
            self.policy_name,
            self.policy_version,
            self.policy_config_sha256,
        )
        if self.status is ReportKpiReferenceStatus.RESOLVED:
            if any(value is None for value in binding):
                raise ValueError("resolved report KPI references require complete evidence binding")
        elif any(value is not None for value in binding):
            raise ValueError("unresolved or retired report KPI references cannot carry a binding")
        return self


class ReportKpiReferenceSourceState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str = Field(min_length=1, max_length=32)
    source_path: str = Field(min_length=1, max_length=512)
    status: ReportKpiReferenceSourceStatus
    reason_code: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def _status_shape(self) -> Self:
        if (self.status is ReportKpiReferenceSourceStatus.VALID) == (self.reason_code is not None):
            raise ValueError("valid report configuration cannot have an error reason")
        return self


class ReportKpiReferenceInventory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    references: tuple[ReportKpiReference, ...]
    source_states: tuple[ReportKpiReferenceSourceState, ...]


class ReportKpiReferenceDispositionRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int = Field(gt=0)
    user_id: str = Field(min_length=1, max_length=128)
    reference: ReportKpiReference
    disposition: ReportKpiReferenceDisposition
    revision: int = Field(gt=0)
    supersedes_resolution_id: int | None = Field(default=None, gt=0)
    reviewed_by: str = Field(min_length=1, max_length=128)
    knowledge_at: datetime

    @field_validator("knowledge_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("report KPI reference knowledge_at must be timezone-aware")
        return value.astimezone(UTC)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _table_columns(conn: sqlite3.Connection, name: str) -> frozenset[str]:
    return frozenset(str(row[1]) for row in conn.execute(f"PRAGMA table_info({name})"))


def _reference(
    *, ticker: str, source_path: str, pointer: str, kind: ReportKpiReferenceKind, label: str
) -> ReportKpiReference:
    stripped = label.strip()
    return ReportKpiReference(
        ticker=ticker,
        source_path=source_path,
        json_pointer=pointer,
        reference_kind=kind,
        requested_label=stripped,
        reference_content_sha256=hashlib.sha256(stripped.encode("utf-8")).hexdigest(),
    )


def load_report_kpi_reference_inventory(
    repo_root: Path, tickers: tuple[str, ...]
) -> ReportKpiReferenceInventory:
    """Load exact KPI references and fail-closed per-ticker source states."""
    out: list[ReportKpiReference] = []
    states: list[ReportKpiReferenceSourceState] = []
    for ticker in tickers:
        relative = f"micro_thesis/holdings/{ticker}.json"
        path = repo_root / relative
        if not path.exists():
            states.append(
                ReportKpiReferenceSourceState(
                    ticker=ticker,
                    source_path=relative,
                    status=ReportKpiReferenceSourceStatus.MISSING,
                    reason_code="report_configuration_missing",
                )
            )
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError:
            states.append(
                ReportKpiReferenceSourceState(
                    ticker=ticker,
                    source_path=relative,
                    status=ReportKpiReferenceSourceStatus.INVALID,
                    reason_code="report_configuration_unreadable",
                )
            )
            continue
        except json.JSONDecodeError:
            states.append(
                ReportKpiReferenceSourceState(
                    ticker=ticker,
                    source_path=relative,
                    status=ReportKpiReferenceSourceStatus.INVALID,
                    reason_code="report_configuration_json_invalid",
                )
            )
            continue
        if not isinstance(payload, dict):
            states.append(
                ReportKpiReferenceSourceState(
                    ticker=ticker,
                    source_path=relative,
                    status=ReportKpiReferenceSourceStatus.INVALID,
                    reason_code="report_configuration_root_invalid",
                )
            )
            continue
        root = cast("dict[str, object]", payload)
        payload_ticker = root.get("ticker")
        if payload_ticker is None:
            states.append(
                ReportKpiReferenceSourceState(
                    ticker=ticker,
                    source_path=relative,
                    status=ReportKpiReferenceSourceStatus.INVALID,
                    reason_code="report_configuration_ticker_missing",
                )
            )
            continue
        if not isinstance(payload_ticker, str) or not payload_ticker.strip():
            states.append(
                ReportKpiReferenceSourceState(
                    ticker=ticker,
                    source_path=relative,
                    status=ReportKpiReferenceSourceStatus.INVALID,
                    reason_code="report_configuration_ticker_invalid",
                )
            )
            continue
        if payload_ticker.strip().upper() != ticker.upper():
            states.append(
                ReportKpiReferenceSourceState(
                    ticker=ticker,
                    source_path=relative,
                    status=ReportKpiReferenceSourceStatus.INVALID,
                    reason_code="report_configuration_ticker_mismatch",
                )
            )
            continue
        ticker_references: list[ReportKpiReference] = []
        invalid_reason: str | None = None
        priorities = root.get("chart_priorities")
        if priorities is not None and not isinstance(priorities, list):
            invalid_reason = "report_chart_priorities_invalid"
        elif isinstance(priorities, list):
            for index, value in enumerate(cast("list[object]", priorities)):
                if not isinstance(value, str) or not value.strip():
                    invalid_reason = "report_chart_priority_entry_invalid"
                    break
                ticker_references.append(
                    _reference(
                        ticker=ticker,
                        source_path=relative,
                        pointer=f"/chart_priorities/{index}",
                        kind=ReportKpiReferenceKind.CHART_PRIORITY,
                        label=value,
                    )
                )
        tier_kinds = {
            "tier_1_kpis": ReportKpiReferenceKind.TIER_1_KPI,
            "tier_2_kpis": ReportKpiReferenceKind.TIER_2_KPI,
            "tier_3_kpis": ReportKpiReferenceKind.TIER_3_KPI,
        }
        for field, kind in tier_kinds.items():
            if invalid_reason is not None:
                break
            rows = root.get(field)
            if rows is None:
                continue
            if not isinstance(rows, list):
                invalid_reason = "report_kpi_tier_invalid"
                break
            for index, value in enumerate(cast("list[object]", rows)):
                if not isinstance(value, dict):
                    invalid_reason = "report_kpi_tier_entry_invalid"
                    break
                row = cast("dict[str, object]", value)
                label = row.get("name")
                if not isinstance(label, str) or not label.strip():
                    invalid_reason = "report_kpi_tier_name_invalid"
                    break
                ticker_references.append(
                    _reference(
                        ticker=ticker,
                        source_path=relative,
                        pointer=f"/{field}/{index}/name",
                        kind=kind,
                        label=label,
                    )
                )
        rules = root.get("break_rules")
        if invalid_reason is None and rules is not None and not isinstance(rules, list):
            invalid_reason = "report_break_rules_invalid"
        elif invalid_reason is None and isinstance(rules, list):
            for index, value in enumerate(cast("list[object]", rules)):
                if not isinstance(value, dict):
                    invalid_reason = "report_break_rule_entry_invalid"
                    break
                row = cast("dict[str, object]", value)
                label = row.get("kpi_name")
                if not isinstance(label, str) or not label.strip():
                    invalid_reason = "report_break_rule_name_invalid"
                    break
                ticker_references.append(
                    _reference(
                        ticker=ticker,
                        source_path=relative,
                        pointer=f"/break_rules/{index}/kpi_name",
                        kind=ReportKpiReferenceKind.BREAK_RULE,
                        label=label,
                    )
                )
        if invalid_reason is not None:
            states.append(
                ReportKpiReferenceSourceState(
                    ticker=ticker,
                    source_path=relative,
                    status=ReportKpiReferenceSourceStatus.INVALID,
                    reason_code=invalid_reason,
                )
            )
            continue
        out.extend(ticker_references)
        states.append(
            ReportKpiReferenceSourceState(
                ticker=ticker,
                source_path=relative,
                status=ReportKpiReferenceSourceStatus.VALID,
            )
        )
    return ReportKpiReferenceInventory(references=tuple(out), source_states=tuple(states))


def report_kpi_references(
    repo_root: Path, tickers: tuple[str, ...]
) -> tuple[ReportKpiReference, ...]:
    """Return exact references while callers that need completeness use the inventory."""
    return load_report_kpi_reference_inventory(repo_root, tickers).references


def current_report_kpi_reference_disposition(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    reference: ReportKpiReference,
) -> ReportKpiReferenceDispositionRevision | None:
    table = "report_kpi_reference_resolution_revisions"
    if not _table_exists(conn, table):
        return None
    row = conn.execute(
        "SELECT resolution.* FROM report_kpi_reference_resolution_revisions resolution "
        "WHERE resolution.user_id=? AND UPPER(resolution.ticker)=UPPER(?) "
        "AND resolution.source_path=? AND resolution.json_pointer=? AND NOT EXISTS ("
        "SELECT 1 FROM report_kpi_reference_resolution_revisions successor "
        "WHERE successor.supersedes_resolution_id=resolution.id) "
        "ORDER BY resolution.revision DESC LIMIT 1",
        (user_id, reference.ticker, reference.source_path, reference.json_pointer),
    ).fetchone()
    if row is None:
        return None
    values = dict(row) if isinstance(row, sqlite3.Row) else None
    if values is None:
        columns = [
            str(column[0])
            for column in conn.execute(
                "SELECT * FROM report_kpi_reference_resolution_revisions LIMIT 0"
            ).description
        ]
        values = dict(zip(columns, row, strict=True))
    stored_reference = ReportKpiReference(
        ticker=str(values["ticker"]),
        source_path=str(values["source_path"]),
        json_pointer=str(values["json_pointer"]),
        reference_kind=ReportKpiReferenceKind(str(values["reference_kind"])),
        requested_label=str(values["requested_label"]),
        reference_content_sha256=str(values["reference_content_sha256"]),
    )
    return ReportKpiReferenceDispositionRevision(
        id=int(values["id"]),
        user_id=str(values["user_id"]),
        reference=stored_reference,
        disposition=ReportKpiReferenceDisposition(
            status=ReportKpiReferenceStatus(str(values["status"])),
            kpi_definition_id=(
                None if values["kpi_definition_id"] is None else int(values["kpi_definition_id"])
            ),
            definition_identity_sha256=(
                None
                if values.get("definition_identity_sha256") is None
                else str(values["definition_identity_sha256"])
            ),
            evidence_fact_id=(
                None if values.get("evidence_fact_id") is None else int(values["evidence_fact_id"])
            ),
            evidence_context_id=(
                None
                if values.get("evidence_context_id") is None
                else int(values["evidence_context_id"])
            ),
            evidence_sha256=(
                None if values.get("evidence_sha256") is None else str(values["evidence_sha256"])
            ),
            resolution_method=(
                None
                if values.get("resolution_method") is None
                else ReportKpiReferenceResolutionMethod(str(values["resolution_method"]))
            ),
            policy_name=(None if values.get("policy_name") is None else str(values["policy_name"])),
            policy_version=(
                None if values.get("policy_version") is None else str(values["policy_version"])
            ),
            policy_config_sha256=(
                None
                if values.get("policy_config_sha256") is None
                else str(values["policy_config_sha256"])
            ),
            reason_code=str(values["reason_code"]),
        ),
        revision=int(values["revision"]),
        supersedes_resolution_id=(
            None
            if values["supersedes_resolution_id"] is None
            else int(values["supersedes_resolution_id"])
        ),
        reviewed_by=str(values["reviewed_by"]),
        knowledge_at=datetime.fromisoformat(str(values["knowledge_at"]).replace("Z", "+00:00")),
    )


def persist_report_kpi_reference_disposition(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    reference: ReportKpiReference,
    disposition: ReportKpiReferenceDisposition,
    reviewed_by: str,
    knowledge_at: datetime | None = None,
) -> int:
    """Append one reference disposition or return its idempotent current identity."""
    if not _table_exists(conn, "report_kpi_reference_resolution_revisions"):
        raise ValueError("report KPI reference disposition schema is unavailable")
    columns = _table_columns(conn, "report_kpi_reference_resolution_revisions")
    has_v2 = "definition_identity_sha256" in columns
    if disposition.status is not ReportKpiReferenceStatus.UNRESOLVED and not has_v2:
        raise ValueError("report KPI reference resolution v2 schema is unavailable")
    current = current_report_kpi_reference_disposition(conn, user_id=user_id, reference=reference)
    if (
        current is not None
        and current.reference == reference
        and current.disposition == disposition
    ):
        return current.id
    observed = knowledge_at or datetime.now(UTC)
    if observed.tzinfo is None:
        raise ValueError("report KPI reference knowledge_at must be timezone-aware")
    field_names = [
        "user_id",
        "ticker",
        "source_path",
        "json_pointer",
        "reference_kind",
        "requested_label",
        "reference_content_sha256",
        "status",
        "kpi_definition_id",
    ]
    payload: list[object] = [
        user_id,
        reference.ticker,
        reference.source_path,
        reference.json_pointer,
        reference.reference_kind.value,
        reference.requested_label,
        reference.reference_content_sha256,
        disposition.status.value,
        disposition.kpi_definition_id,
    ]
    if has_v2:
        field_names.extend(
            [
                "definition_identity_sha256",
                "evidence_fact_id",
                "evidence_context_id",
                "evidence_sha256",
                "resolution_method",
                "policy_name",
                "policy_version",
                "policy_config_sha256",
            ]
        )
        payload.extend(
            [
                disposition.definition_identity_sha256,
                disposition.evidence_fact_id,
                disposition.evidence_context_id,
                disposition.evidence_sha256,
                (
                    None
                    if disposition.resolution_method is None
                    else disposition.resolution_method.value
                ),
                disposition.policy_name,
                disposition.policy_version,
                disposition.policy_config_sha256,
            ]
        )
    field_names.extend(
        [
            "reason_code",
            "revision",
            "supersedes_resolution_id",
            "reviewed_by",
            "knowledge_at",
        ]
    )
    payload.extend(
        [
            disposition.reason_code,
            1 if current is None else current.revision + 1,
            None if current is None else current.id,
            reviewed_by,
            observed.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        ]
    )
    placeholders = ",".join("?" for _ in field_names)
    cursor = conn.execute(
        "INSERT INTO report_kpi_reference_resolution_revisions "
        f"({','.join(field_names)}) VALUES ({placeholders})",
        tuple(payload),
    )
    if cursor.lastrowid is None:
        raise RuntimeError("report KPI reference disposition insert returned no identity")
    return int(cursor.lastrowid)


__all__ = [
    "ReportKpiReference",
    "ReportKpiReferenceDisposition",
    "ReportKpiReferenceDispositionRevision",
    "ReportKpiReferenceInventory",
    "ReportKpiReferenceKind",
    "ReportKpiReferenceResolutionMethod",
    "ReportKpiReferenceSourceState",
    "ReportKpiReferenceSourceStatus",
    "ReportKpiReferenceStatus",
    "current_report_kpi_reference_disposition",
    "load_report_kpi_reference_inventory",
    "persist_report_kpi_reference_disposition",
    "report_kpi_references",
]
