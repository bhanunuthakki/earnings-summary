"""Typed contracts for paired, immutable performance experiments."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

PositiveFloat = Annotated[float, Field(gt=0, allow_inf_nan=False)]
NonNegativeFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]
NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]


class ExperimentDeclaration(BaseModel):
    """Tracked declaration that must be identical in both source revisions."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["performance-experiment-declaration/v1"]
    experiment_id: str
    cohort_kind: Literal["runner_fidelity_smoke"]
    representativeness: Literal["not_510s_or_production_feasibility_evidence"]
    workload_id: str
    workload_entrypoint: str
    workload_argv: tuple[str, ...]
    fixture_path: str
    runner_id: str
    repeats: Annotated[int, Field(ge=7, le=21)]
    timeout_seconds: PositiveFloat
    cache_state: Literal["fresh_process_os_cache_uncontrolled"]
    required_companions: tuple[
        Literal[
            "coverage_sha256",
            "fixture_sha256",
            "peak_rss_bytes",
            "result_sha256",
            "rows",
            "sql_statements",
            "workload_id",
        ],
        ...,
    ]


class RunnerIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    runner_id: str
    executable: str
    resolved_executable: str
    executable_sha256: str
    protocol_sha256: str
    python_version: str
    platform: str


class RuntimeBootstrapIdentity(BaseModel):
    """Disclosed common execution envelope applied outside both source arms."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    policy: Literal["alias_cache_redirect_v1"]
    bootstrap_sha256: str
    cache_lifecycle: Literal["fresh_external_output_directory_per_sample"]
    overridden_module_path: Literal["src/alias_manager.py"]
    overridden_names: tuple[Literal["CACHE_DIR", "ALIASES_FILE"], ...]
    injected_environment_keys: tuple[
        Literal[
            "PERFORMANCE_EXPERIMENT_SNAPSHOT",
            "PERFORMANCE_EXPERIMENT_WORKLOAD_ENTRYPOINT",
        ],
        ...,
    ]
    effective_argv: tuple[str, ...]


class SourceArmIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    arm: Literal["control", "treatment"]
    revision: str
    tree_sha256: str
    declaration_sha256: str
    fixture_sha256: str
    workload_sha256: str
    snapshot_path_sha256: str


class CompanionEnvelope(BaseModel):
    """Child observations; SQL and RSS are unverified and may vary per sample."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["performance-experiment-companion/v1"]
    workload_id: str
    revision: str
    fixture_sha256: str
    coverage_sha256: str
    result_sha256: str
    sql_statements: NonNegativeInt
    rows: NonNegativeInt
    peak_rss_bytes: NonNegativeInt


class ArmSample(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    arm: Literal["control", "treatment"]
    pair_ordinal: PositiveInt
    order: Literal[1, 2]
    elapsed_seconds: PositiveFloat
    companion: CompanionEnvelope
    companion_trust: Literal["self_reported_unverified"]
    variable_measurement_policy: Literal["sql_statements_and_peak_rss_are_per_sample_unverified"]


class ArmStats(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    count: NonNegativeInt
    median_seconds: PositiveFloat | None
    mad_seconds: NonNegativeFloat | None


class PairedStats(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    count: NonNegativeInt
    median_delta_seconds: float | None
    mad_delta_seconds: NonNegativeFloat | None
    bootstrap_ci_95_delta_seconds: tuple[float, float] | None


class IsolationProof(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    separate_snapshot_roots: bool
    immutable_archives: bool
    separate_output_directories: bool
    source_trees_unchanged: bool
    credential_environment_removed: bool
    inherited_git_environment_removed: bool
    process_isolation: Literal["unavailable"]
    network_isolation: Literal["unavailable"]


class PerformanceExperimentReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["performance-experiment-receipt/v2"]
    declaration: ExperimentDeclaration
    declaration_sha256: str
    runner: RunnerIdentity
    runtime_bootstrap: RuntimeBootstrapIdentity | None
    control: SourceArmIdentity
    treatment: SourceArmIdentity
    warmups: tuple[ArmSample, ArmSample]
    measured_samples: tuple[ArmSample, ...]
    control_stats: ArmStats
    treatment_stats: ArmStats
    paired_stats: PairedStats
    isolation: IsolationProof
    collection_status: Literal["COMPLETE", "FAIL"]
    causal_feasibility_status: Literal["HOLD"]
    admission_status: Literal["HOLD"]
    hold: Literal[True]
    hold_reasons: tuple[str, ...]


__all__ = [
    "ArmSample",
    "ArmStats",
    "CompanionEnvelope",
    "ExperimentDeclaration",
    "IsolationProof",
    "PairedStats",
    "PerformanceExperimentReceipt",
    "RunnerIdentity",
    "RuntimeBootstrapIdentity",
    "SourceArmIdentity",
]
