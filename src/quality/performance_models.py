"""Typed models and constants for local performance evidence."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

ReceiptStatus: TypeAlias = Literal["PASS", "HOLD", "FAIL"]
CohortName: TypeAlias = Literal[
    "integrity", "migrations", "route_cold_warm", "dcf", "source_analysis", "ci"
]
NonNegativeInt = Annotated[int, Field(ge=0)]
NonNegativeFloat = Annotated[float, Field(ge=0)]
PositiveFloat = Annotated[float, Field(gt=0)]
EXTERNAL_TRAP_PROOF_VERSION = "bha115-network-trap-v1"
EXTERNAL_TRAP_BOUNDARIES: tuple[str, ...] = (
    "socket.socket",
    "socket.create_connection",
    "http.client.HTTPConnection.connect",
    "http.client.HTTPSConnection.connect",
    "urllib.request.urlopen",
    "onmymind.reply._default_call",
    "research.run._call_web",
    "research.run._call_struct",
)


def external_trap_proof_sha256(*, events: tuple[str, ...]) -> str:
    """Hash the frozen trap coverage together with observed boundary events."""
    payload = {
        "proof_version": EXTERNAL_TRAP_PROOF_VERSION,
        "coverage": EXTERNAL_TRAP_BOUNDARIES,
        "events": events,
    }
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


class TimingStats(BaseModel):
    model_config = ConfigDict(extra="forbid")
    samples: list[PositiveFloat]
    count: NonNegativeInt
    minimum_seconds: PositiveFloat | None
    median_seconds: PositiveFloat | None
    mean_seconds: PositiveFloat | None
    maximum_seconds: PositiveFloat | None
    stdev_seconds: NonNegativeFloat | None


class TimingSample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Literal["cold", "warm"]
    elapsed_seconds: PositiveFloat


class SourceAnalysisSummary(BaseModel):
    """Revision-paired source-analysis evidence, including cache truth."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    baseline_cold_seconds: tuple[PositiveFloat, ...] = ()
    current_cold_seconds: tuple[PositiveFloat, ...] = ()
    baseline_warmup_seconds: tuple[PositiveFloat, ...] = ()
    current_warmup_seconds: tuple[PositiveFloat, ...] = ()
    paired_delta_seconds: tuple[float, ...] = ()
    paired_delta_bootstrap_ci_95: tuple[float, float] | None = None
    baseline_peak_rss_bytes: tuple[NonNegativeInt, ...] = ()
    current_peak_rss_bytes: tuple[NonNegativeInt, ...] = ()
    cache_disposition: Literal["observed", "no-cache"] = "no-cache"
    cache_hits: NonNegativeInt = 0
    cache_misses: NonNegativeInt = 0
    parsed_once: bool = False
    warmup_count: NonNegativeInt = 0
    regression_over_10_percent: bool = False
    rss_disposition: Literal["per-invocation", "unavailable"] = "unavailable"
    trusted_scanner_sha256: str | None = None
    trusted_scanner_wrapper_sha256: str | None = None


class CompanionMeasures(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sql_statements: NonNegativeInt | None
    rows: NonNegativeInt | None
    elapsed_seconds: PositiveFloat | None
    peak_rss_bytes: NonNegativeInt | None


class RouteCausalCompanion(BaseModel):
    """Measured evidence for one route in the fixed route cohort."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    route_name: str
    phase: Literal["cold", "warm"]
    method: Literal["GET", "POST"]
    status_code: int
    allowed_success_statuses: tuple[int, ...]
    elapsed_seconds: PositiveFloat
    sql_statements: NonNegativeInt
    connection_count: NonNegativeInt
    response_sha256: str
    auth_fixture_identity: str
    fixture_sha256: str
    external_call_hold_seconds: NonNegativeFloat
    network_disabled: StrictBool
    state_sha256: str | None = None
    external_attempt_count: NonNegativeInt = 0
    external_trap_sha256: str | None = None
    external_trap_proof_version: str = ""
    external_trap_coverage: tuple[str, ...] = ()
    external_trap_events: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_external_trap_proof(self) -> RouteCausalCompanion:
        """Reject partial or empty-event-only network proofs when supplied."""
        has_proof = bool(
            self.external_trap_sha256
            or self.external_trap_proof_version
            or self.external_trap_coverage
            or self.external_trap_events
        )
        if not has_proof:
            return self
        if self.external_trap_proof_version != EXTERNAL_TRAP_PROOF_VERSION:
            raise ValueError("external trap proof version is missing or unsupported")
        if self.external_trap_coverage != EXTERNAL_TRAP_BOUNDARIES:
            raise ValueError("external trap proof coverage is incomplete")
        if any(event not in EXTERNAL_TRAP_BOUNDARIES for event in self.external_trap_events):
            raise ValueError("external trap proof contains an unknown event")
        expected = external_trap_proof_sha256(events=self.external_trap_events)
        if self.external_trap_sha256 != expected:
            raise ValueError("external trap proof digest does not bind coverage and events")
        return self


class CausalRunEnvelope(BaseModel):
    """Required per-invocation envelope emitted by a benchmark command."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    sql_statements: NonNegativeInt
    rows: NonNegativeInt
    elapsed_seconds: PositiveFloat
    peak_rss_bytes: NonNegativeInt
    alembic_revision: str | None
    alembic_invocations: NonNegativeInt = 0
    migration_elapsed_seconds: PositiveFloat | None = None
    schema_object_count: NonNegativeInt | None = None
    query_plan_sha256: str | None
    connection_role: Literal["read", "write", "request_scoped_read", "none"]
    stage: str
    revision: str
    route_companions: tuple[RouteCausalCompanion, ...] = ()
    artifact_sheet_names: tuple[str, ...] = ()
    stage_timings: dict[str, PositiveFloat] = Field(default_factory=dict)
    stage_peak_rss_bytes: dict[str, NonNegativeInt] = Field(default_factory=dict)
    rss_semantics: Literal[
        "process_high_water",
        "process_plus_children_high_water_upper_bound",
        "unavailable",
    ] = "unavailable"
    artifact_sha256: str | None = None
    artifact_parity_sha256: str | None = None
    artifact_byte_parity: bool | None = None
    semantic_parity: bool | None = None
    formula_sha256: str | None = None
    receipt_sha256: str | None = None
    phase: Literal["warmup", "cold", "warm"] | None = None
    process_peak_rss_bytes: NonNegativeInt | None = None
    cache_state: Literal["observed", "no-cache", "unknown"] = "unknown"
    cache_hits: NonNegativeInt | None = None
    cache_misses: NonNegativeInt | None = None
    parsed_once: bool | None = None


def _empty_causal_runs() -> list[CausalRunEnvelope]:
    return []


class PerformanceReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "performance-baseline/v1"
    benchmark_command: str
    command_argv: list[str]
    revision: str | None
    source_sha256: str | None
    config_sha256: str | None
    timing: TimingStats
    environment: dict[str, str]
    status: ReceiptStatus
    hold: bool
    hold_reasons: list[str]
    exit_codes: list[int]
    output_sha256: str | None
    output_bytes: NonNegativeInt
    output: str
    warmup_seconds: PositiveFloat | None
    timing_samples: list[TimingSample]
    median_seconds: PositiveFloat | None
    mad_seconds: NonNegativeFloat | None
    bootstrap_ci_95: tuple[PositiveFloat, PositiveFloat] | None
    stability_verdict: Literal["stable", "unstable", "insufficient"]
    adaptive_verdict: Literal["eligible", "hold", "failed"]
    companion_measures: CompanionMeasures
    provenance: Literal["mac_guidance", "approved_windows_production_shaped"]
    causal_runs: list[CausalRunEnvelope] = Field(default_factory=_empty_causal_runs)
    source_analysis: SourceAnalysisSummary | None = None


class CausalEvidence(BaseModel):
    """Typed companions that explain what a timing sample exercised."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    sql_statements: NonNegativeInt | None
    rows: NonNegativeInt | None
    elapsed_seconds: PositiveFloat | None
    peak_rss_bytes: NonNegativeInt | None
    alembic_revision: str | None
    query_plan_sha256: str | None
    connection_role: Literal["read", "write", "request_scoped_read", "none"]
    stage: str | None


class FrozenPerformanceCohort(BaseModel):
    """Versioned declaration of a Train-0 benchmark cohort."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    cohort: CohortName
    config_version: str = "train0/v1"
    declared_command: str
    route_count: NonNegativeInt = 0
    route_names: tuple[str, ...] = ()


class PerformanceEvidenceReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: str = "performance-evidence/v1"
    cohort: FrozenPerformanceCohort
    baseline: PerformanceReceipt
    causal_evidence: CausalEvidence
    causal_runs: tuple[CausalRunEnvelope, ...]
    baseline_revision: str | None
    current_revision: str | None
    paired_identity: bool
