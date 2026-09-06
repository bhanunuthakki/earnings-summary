"""Strict receipt models for raw local performance timing."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

ReceiptStatus: TypeAlias = Literal["HOLD", "FAIL"]
CollectionStatus: TypeAlias = Literal["COMPLETE", "INCOMPLETE"]
SourceIdentity: TypeAlias = Literal["clean_head", "working_tree"]

NonNegativeInt = Annotated[int, Field(ge=0)]
NonNegativeFloat = Annotated[float, Field(ge=0)]
PositiveFloat = Annotated[float, Field(gt=0)]
PositiveInt = Annotated[int, Field(gt=0)]

ADMISSION_HOLD_REASON = "causal/performance admission is deferred"
OUTPUT_PREVIEW_LIMIT = 4000
SCANNER_VERSION = "performance-timing/v1"


class TimingSample(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    label: Literal["measured"]
    ordinal: PositiveInt
    elapsed_seconds: PositiveFloat


class TimingStats(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    samples: list[PositiveFloat]
    count: NonNegativeInt
    median_seconds: PositiveFloat | None
    mad_seconds: NonNegativeFloat | None
    bootstrap_ci_95: tuple[PositiveFloat, PositiveFloat] | None
    stability_verdict: Literal["stable", "unstable", "insufficient"]


class CompanionMeasures(BaseModel):
    """Reserved raw companion slots; none can grant admission in this slice."""

    model_config = ConfigDict(extra="forbid", strict=True)
    sql_statements: NonNegativeInt | None
    rows: NonNegativeInt | None
    elapsed_seconds: PositiveFloat | None
    peak_rss_bytes: NonNegativeInt | None


def _empty_companions() -> CompanionMeasures:
    return CompanionMeasures(
        sql_statements=None,
        rows=None,
        elapsed_seconds=None,
        peak_rss_bytes=None,
    )


class PerformanceReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["performance-baseline/v1"]
    benchmark_command: str
    command_argv: list[str]
    revision: str
    source_identity: SourceIdentity
    source_sha256: str
    config_sha256: str
    scanner_sha256: str
    scanner_version: str
    timing: TimingStats
    timing_samples: list[TimingSample]
    warmup_seconds: PositiveFloat
    environment: dict[str, str]
    collection_status: CollectionStatus
    admission_status: Literal["HOLD"]
    status: ReceiptStatus
    hold: Literal[True]
    hold_reasons: list[str]
    exit_codes: list[int]
    output_sha256: str
    output_bytes: NonNegativeInt
    output_preview: str
    provenance: str
    companion_measures: CompanionMeasures = Field(default_factory=_empty_companions)


__all__ = [
    "ADMISSION_HOLD_REASON",
    "OUTPUT_PREVIEW_LIMIT",
    "SCANNER_VERSION",
    "CollectionStatus",
    "CompanionMeasures",
    "PerformanceReceipt",
    "ReceiptStatus",
    "SourceIdentity",
    "TimingSample",
    "TimingStats",
]
