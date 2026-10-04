"""Evaluate per-ticker thesis break rules against kpi_facts and write verdict to thesis_state.

Each holding has a `break_rules` section in `micro_thesis/holdings/<TICKER>.json`
encoding deterministic rules of the form: "KPI X comparator threshold for N
consecutive periods." This module joins those rules against the most-recent
N kpi_facts rows per KPI, classifies each rule (OK / WATCH / BREACH), and
returns the holding-level verdict (worst-rule wins).

Hard rules can opt into source-bound quarterly metric expressions. Legacy
rules retain their level-series behavior. Trend/slope signals remain soft
warnings; see `compute.soft_rule_evaluator` and the holdings schema.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from compute.kpi_resolver import (
    ANNUAL_FACT_PERIOD_TYPES,
    reporting_cadence_for,
    resolve_kpi_definition_name,
    semantic_series_identity_sql,
)
from compute.soft_rule_evaluator import (
    SoftEvaluationCapture,
    SoftRule,
    SoftRuleResult,
    SoftRuleStatus,
    evaluate_soft_rules,
    load_soft_rules,
    replay_soft_capture,
    soft_result_economic_payload,
)
from compute.thesis_episode_attention import deliver_episode_alert, supersede_prior
from compute.thesis_evaluation_episodes import (
    AcceptedObservationInput,
    CapturedKpiObservation,
    EpisodeCheckInput,
    EpisodeIdempotencyConflictError,
    EpisodeNondeterminismError,
    EpisodeSeverity,
    EpisodeStoreError,
    ForwardSemanticInput,
    HardRuleCapture,
    ProvenanceCompleteness,
    RetainedThesisContext,
    SemanticRuleInput,
    forward_episode_id,
    record_forward_episode,
)
from compute.thesis_evaluation_episodes import (
    KpiInputReference as KpiInputReference,
)
from compute.thesis_kpi_registration import refresh_thesis_kpi_registration
from compute.thesis_metric_series import (
    MetricEvaluationCapture,
    MetricExpression,
    MetricReplayError,
    MetricSeriesResult,
    calculate_metric_series,
    replay_metric_capture,
)
from identity import DEFAULT_USER_ID
from models.facts import Unit
from models.kpis import BreachStatus
from models.unit_convert import convert_unit
from pipeline.kpi_report_reference_dispositions import (
    ReportKpiReferenceKind,
    ReportKpiReferenceSourceStatus,
    load_report_kpi_reference_inventory,
)
from pipeline.kpi_report_reference_resolver import (
    report_kpi_reference_at,
    verified_report_kpi_reference_definition,
)
from pipeline.kpi_semantics import semantic_admission_sql
from provenance.financial_fact_resolution import canonical_fact_relation
from provenance.overrides import KPI as OVERRIDE_KPI
from provenance.overrides import active_scalar_override_map
from thesis_reunderwrite_gate import ReUnderwriteBlockedError, evaluate_gate

log = logging.getLogger(__name__)


class Comparator(StrEnum):
    """Closed enum of supported numeric comparators."""

    LT = "lt"
    LE = "le"
    GT = "gt"
    GE = "ge"
    EQ = "eq"


class RuleTier(StrEnum):
    """Two-tier rule taxonomy.

    UNIVERSAL: catastrophic tripwires that apply to every holding (e.g. outright
    revenue decline). Kept intentionally narrow — the noisy GAAP-margin universals
    were removed in favor of per-ticker thresholds because SBC-heavy software,
    capex-cycle pharma, and banks all distort GAAP op/net margin in different ways.

    BUSINESS_MODEL: per-ticker breakers that reflect the actual unit economics of
    the business (sub-ARR contribution margin for RBRK, NIM/efficiency ratio for
    NU, FRE growth for BN, etc.). These are the rules that should fire FIRST when
    the thesis is genuinely breaking.
    """

    UNIVERSAL = "universal"
    BUSINESS_MODEL = "business_model"


class BreakRule(BaseModel):
    """One deterministic break rule from a holdings JSON.

    `consecutive_periods` defaults to 1 (instantaneous breach). The most-recent
    `consecutive_periods` kpi_facts values for `kpi_name` are inspected; the rule
    fires if all of them satisfy `comparator threshold`.

    `tier` distinguishes catastrophic tripwires (universal) from per-ticker
    thesis breakers (business_model). Defaults to business_model so rules added
    only to the per-ticker list inherit the correct tag without explicit marking.
    """

    rule_id: str = Field(min_length=1, max_length=80)
    kpi_name: str = Field(min_length=1, max_length=200)
    comparator: Comparator
    threshold: Decimal
    unit: Unit
    consecutive_periods: int = Field(ge=1, le=12, default=1)
    # 1000 to match the say_do narrative cap (src/compute/say_do.py): a detailed
    # recalibration rationale (e.g. NU's requirement-relative capital-cushion
    # narrative) runs past the original 500 and shouldn't fail rule loading.
    narrative: str = Field(min_length=1, max_length=1000)
    tier: RuleTier = RuleTier.BUSINESS_MODEL
    metric_expression: MetricExpression | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    require_adjacent_quarters: bool = Field(default=False, exclude_if=lambda value: not value)


class HoldingsSpec(BaseModel):
    """Subset of holdings JSON used by the evaluator.

    Two parallel arrays of hard rules. `break_rules` carries the (narrow)
    universal tripwires shared across holdings; `business_model_rules` carries
    the per-ticker breakers that reflect the actual unit economics. Tier is
    assigned at load time based on which array a rule came from — the evaluator
    merges them into a single sequence before fetching history.

    `soft_rules` is the predicate-style YELLOW signals from `break_rules_soft`
    in the on-disk JSON. They never drive RED — the rollup escalates only to
    WARN when any soft rule fires (see `evaluate_ticker_thesis`).
    """

    ticker: str
    thesis: str
    break_rules: list[BreakRule] = Field(default_factory=lambda: list[BreakRule]())
    business_model_rules: list[BreakRule] = Field(default_factory=lambda: list[BreakRule]())
    soft_rules: list[SoftRule] = Field(default_factory=lambda: list[SoftRule]())


def kpi_input_projection_sql(
    conn: sqlite3.Connection,
    *,
    fact_alias: Literal["kf", "f"],
    definition_alias: Literal["kd", "d"],
    semantic_joined: bool,
) -> str:
    """Annotate the existing selection in the same SQL read, without reselection."""
    fact_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(kpi_facts)")}
    context_columns: set[str] = (
        {str(row[1]) for row in conn.execute("PRAGMA table_info(kpi_fact_semantic_contexts)")}
        if semantic_joined
        else set()
    )
    fields = [
        f"{fact_alias}.id AS input_fact_id",
        f"{fact_alias}.ticker AS input_ticker",
        f"{definition_alias}.id AS input_definition_id",
        f"{definition_alias}.name AS input_definition_name",
    ]
    for name in ("source_doc_id", "locator", "fiscal_period_type"):
        expression = f"{fact_alias}.{name}" if name in fact_columns else "NULL"
        fields.append(f"{expression} AS input_{name}")
    for name, alias in (
        ("id", "semantic_context_id"),
        ("kpi_definition_revision_id", "definition_revision_id"),
        ("source_row_label", "source_row_label"),
        ("source_column_header", "source_column_header"),
        ("source_value_text", "source_value_text"),
    ):
        expression = f"ksc.{name}" if name in context_columns else "NULL"
        fields.append(f"{expression} AS input_{alias}")
    return ", ".join(fields)


def kpi_input_reference(row: sqlite3.Row, *, selection_mode: str) -> KpiInputReference:
    """Project only fields of the exact selected row; absent fields remain absent."""

    def text_field(name: str) -> str | None:
        value = row[f"input_{name}"]
        return None if value is None else str(value)

    def id_field(name: str) -> int | None:
        value = row[f"input_{name}"]
        return None if value is None else int(value)

    return KpiInputReference(
        selection_mode=selection_mode,
        ticker=str(row["input_ticker"]),
        fact_id=int(row["input_fact_id"]),
        definition_id=int(row["input_definition_id"]),
        definition_name=str(row["input_definition_name"]),
        original_value=str(row["value"]),
        original_unit=str(row["unit"]),
        period_end=str(row["period_end"]),
        fiscal_period_type=text_field("fiscal_period_type"),
        source_doc_id=id_field("source_doc_id"),
        locator=text_field("locator"),
        semantic_context_id=id_field("semantic_context_id"),
        definition_revision_id=text_field("definition_revision_id"),
        source_row_label=text_field("source_row_label"),
        source_column_header=text_field("source_column_header"),
        source_value_text=text_field("source_value_text"),
    )


class _SelectedKpiValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    input: KpiInputReference | None
    reconciled_value: str
    reconciled_unit: str
    period_end: str


class _PlainKpiManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["kpi_selected_inputs@1"] = "kpi_selected_inputs@1"
    reader_policy: Literal["current_projection"] = "current_projection"
    identity_status: Literal["partial"] = "partial"
    inputs: tuple[_SelectedKpiValue, ...] = Field(min_length=1, max_length=12)


@dataclass(frozen=True)
class KpiObservation:
    """One historical kpi_facts value pulled for evaluation."""

    period_end: datetime
    value: Decimal
    unit: Unit
    provenance: dict[str, JsonValue] = field(
        default_factory=lambda: dict[str, JsonValue](), compare=False
    )
    fiscal_period_type: str | None = None
    fiscal_year: int | None = None
    input_reference: KpiInputReference | None = None


@dataclass(frozen=True)
class RuleEvaluation:
    """Per-rule outcome with the evidence that drove it."""

    rule: BreakRule
    status: BreachStatus
    observations: tuple[KpiObservation, ...]
    detail: str
    source_manifest: dict[str, JsonValue] | None = None


@dataclass(frozen=True)
class ThesisVerdict:
    """Holding-level rollup of all rule evaluations.

    `soft_rule_results` are predicate-style YELLOW signals. They never bubble
    a verdict to BREACH — only WARN when any one is YELLOW and no hard rule
    breached. See `_rollup_with_soft` for the precedence.
    """

    ticker: str
    thesis: str
    overall_status: BreachStatus
    rule_evaluations: tuple[RuleEvaluation, ...]
    evaluated_at: datetime
    soft_rule_results: tuple[SoftRuleResult, ...] = ()
    semantic_input: ForwardSemanticInput | None = None
    retained_context: RetainedThesisContext | None = None


_LEGACY_EVALUATOR_SEMANTIC_VERSION = "thesis-evaluator/v1"
_THESIS_EVALUATOR_SEMANTIC_VERSION = "thesis-evaluator/v2"
_RULESET_VERSION = "holdings-break-rules/v1"
_METRIC_EVALUATOR_SEMANTIC_VERSION = "thesis-evaluator/v3"
_METRIC_RULESET_VERSION = "holdings-break-rules/v2"
_HOLDINGS_PAYLOAD_ADAPTER = TypeAdapter(dict[str, JsonValue])
_RULE_PROJECTION_ADAPTER = TypeAdapter(list[dict[str, JsonValue]])


def _canonical_json(value: JsonValue) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256_json(value: JsonValue) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _semantic_evidence(value: JsonValue) -> JsonValue:
    """Retain selected source identity, excluding clocks of the evaluator read."""
    if isinstance(value, dict):
        return {
            key: _semantic_evidence(item)
            for key, item in value.items()
            if key not in {"cutoff", "knowledge_cutoff", "known_at", "effective_at"}
        }
    if isinstance(value, list):
        return [_semantic_evidence(item) for item in value]
    return value


def _plain_evidence_manifest(
    manifest: dict[str, JsonValue] | None,
    *,
    calculated: bool,
) -> _PlainKpiManifest | None:
    if calculated or manifest is None:
        return None
    try:
        return _PlainKpiManifest.model_validate_json(_canonical_json(manifest))
    except ValidationError:
        return None


def _plain_kpi_manifest(observations: tuple[KpiObservation, ...]) -> dict[str, JsonValue] | None:
    if not any(point.input_reference is not None for point in observations):
        return None
    manifest = _PlainKpiManifest(
        inputs=tuple(
            _SelectedKpiValue(
                input=point.input_reference,
                reconciled_value=str(point.value),
                reconciled_unit=point.unit.value,
                period_end=point.period_end.isoformat(),
            )
            for point in observations
        )
    )
    return _HOLDINGS_PAYLOAD_ADAPTER.validate_python(manifest.model_dump(mode="json"))


def _read_holdings_payload(path: Path) -> dict[str, JsonValue]:
    with open(path, encoding="utf-8") as handle:
        return _HOLDINGS_PAYLOAD_ADAPTER.validate_python(json.load(handle))


def _thesis_content_payload(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Project only owner-belief fields into the semantic thesis identity."""

    nuance_raw = payload.get("nuance")
    nuance: dict[str, JsonValue] = {}
    if isinstance(nuance_raw, dict):
        for key in ("bear_case", "adversarial_take", "stress_test"):
            value = nuance_raw.get(key)
            if isinstance(value, str):
                nuance[key] = value

    qualitative_raw = payload.get("thesis_breakers_qualitative")
    qualitative: list[JsonValue] = []
    if isinstance(qualitative_raw, list):
        for value in sorted(str(item) for item in qualitative_raw if isinstance(item, str)):
            qualitative.append(value)

    tier_one_raw = payload.get("tier_1_kpis")
    tier_one: list[JsonValue] = []
    if isinstance(tier_one_raw, list):
        for value in tier_one_raw:
            if not isinstance(value, dict):
                continue
            name = value.get("name")
            condition = value.get("break_condition")
            if isinstance(name, str) and isinstance(condition, str):
                tier_one.append({"name": name, "break_condition": condition})
    tier_one.sort(key=_canonical_json)

    projected: dict[str, JsonValue] = {
        "thesis": str(payload.get("thesis", "")),
        "key_driver": str(payload.get("key_driver", "")),
        "nuance": nuance,
        "thesis_breakers_qualitative": qualitative,
        "tier_1_kpis": tier_one,
    }
    return projected


def _new_hard_semantics(rule: BreakRule) -> bool:
    return rule.metric_expression is not None or rule.require_adjacent_quarters


def _new_soft_semantics(value: JsonValue) -> bool:
    if isinstance(value, dict):
        params = value.get("params")
        if value.get("type") == "metric_threshold" or (
            value.get("type") == "compound"
            and isinstance(params, dict)
            and params.get("require_same_period") is True
        ):
            return True
        return any(_new_soft_semantics(child) for child in value.values())
    return isinstance(value, list) and any(_new_soft_semantics(child) for child in value)


def _new_spec_semantics(spec: HoldingsSpec) -> bool:
    return any(
        _new_hard_semantics(rule) for rule in (*spec.break_rules, *spec.business_model_rules)
    ) or any(
        _new_soft_semantics(_HOLDINGS_PAYLOAD_ADAPTER.validate_python(rule.model_dump(mode="json")))
        for rule in spec.soft_rules
    )


def _new_verdict_semantics(verdict: ThesisVerdict) -> bool:
    if verdict.semantic_input is not None:
        return (
            verdict.semantic_input.evaluator_semantic_version == _METRIC_EVALUATOR_SEMANTIC_VERSION
        )
    return any(_new_hard_semantics(item.rule) for item in verdict.rule_evaluations)


def _semantic_rule(rule: BreakRule) -> SemanticRuleInput:
    return SemanticRuleInput(
        rule_id=rule.rule_id,
        definition={
            "tier": rule.tier.value,
            "kpi_name": rule.kpi_name,
            "comparator": rule.comparator.value,
            "threshold": str(rule.threshold),
            "unit": rule.unit.value,
            "consecutive_periods": rule.consecutive_periods,
            "narrative": rule.narrative,
            **(
                {
                    "metric_expression": (
                        _HOLDINGS_PAYLOAD_ADAPTER.validate_python(
                            rule.metric_expression.model_dump(mode="json")
                        )
                        if rule.metric_expression is not None
                        else None
                    ),
                    "require_adjacent_quarters": rule.require_adjacent_quarters,
                }
                if _new_hard_semantics(rule)
                else {}
            ),
        },
    )


def _hard_economic_payload(capture: HardRuleCapture) -> dict[str, JsonValue]:
    rows: list[JsonValue] | None = None
    if capture.observations is not None:
        rows = []
        for observation in capture.observations:
            provenance = observation.provenance
            raw_semantics = provenance.get("retained_semantic_context_json")
            semantics = (
                _HOLDINGS_PAYLOAD_ADAPTER.validate_json(raw_semantics)
                if isinstance(raw_semantics, str)
                else {}
            )
            rows.append(
                {
                    "period_end": observation.period_end.isoformat(),
                    "value": observation.value,
                    "unit": observation.unit,
                    "fiscal_period_type": provenance.get("fiscal_period_type"),
                    "currency": provenance.get("currency"),
                    "semantic_context": {
                        key: semantics.get(key)
                        for key in (
                            "metric_name_as_reported",
                            "accounting_basis",
                            "consolidation_scope",
                            "dimensions_json",
                            "unit_scale",
                            "publication_lane",
                            "status",
                        )
                    },
                }
            )
    return {
        "rule": capture.rule,
        "selected_definition": capture.selected_definition,
        "disposition": capture.disposition,
        "definition_semantics": None
        if capture.selected_definition_content is None
        else {
            key: capture.selected_definition_content.get(key)
            for key in ("name", "unit", "reporting_cadence", "definition_text", "definition_origin")
        },
        "observations": rows,
    }


def _build_semantic_input(
    *,
    payload: dict[str, JsonValue],
    spec: HoldingsSpec,
    evaluations: list[RuleEvaluation],
    soft_results: list[SoftRuleResult],
    hard_inputs: tuple[HardRuleCapture, ...] = (),
    soft_inputs: tuple[SoftEvaluationCapture, ...] = (),
    effective_soft_rules: list[SoftRule] | None = None,
    retain_context: bool = False,
) -> ForwardSemanticInput:
    new_semantics = _new_spec_semantics(spec)
    observations: list[AcceptedObservationInput] = []
    for evaluation in evaluations:
        manifest_hash = (
            _sha256_json(_semantic_evidence(evaluation.source_manifest))
            if evaluation.source_manifest is not None
            and _plain_evidence_manifest(
                evaluation.source_manifest, calculated=evaluation.rule.metric_expression is not None
            )
            is None
            else None
        )
        if manifest_hash is not None and not evaluation.observations:
            observations.append(
                AcceptedObservationInput(
                    metric_identity=f"hard:{evaluation.rule.rule_id}:source-manifest",
                    period_end="unresolved-period",
                    observed_value=manifest_hash,
                    accepted_value=manifest_hash,
                    unit="source-manifest-sha256",
                    currency=None,
                    material_source_semantics=("calculated-metric",),
                    restatement_semantics="source-manifest-retained",
                )
            )
        for observation in evaluation.observations:
            observations.append(
                AcceptedObservationInput(
                    metric_identity=(f"hard:{evaluation.rule.rule_id}:{evaluation.rule.kpi_name}"),
                    period_end=observation.period_end.isoformat(),
                    observed_value=str(observation.value),
                    accepted_value=str(observation.value),
                    unit=observation.unit.value,
                    currency=None,
                    material_source_semantics=(
                        ("calculated-metric", manifest_hash)
                        if manifest_hash is not None
                        else ("current-evaluator-selection",)
                    ),
                    restatement_semantics=(
                        "source-manifest-retained"
                        if manifest_hash is not None
                        else "source-provenance-not-retained"
                    ),
                )
            )
    for index, capture in enumerate(hard_inputs):
        observations.append(
            AcceptedObservationInput(
                metric_identity=f"hard-input:{index}",
                period_end="captured-input",
                observed_value=_sha256_json(_hard_economic_payload(capture)),
                accepted_value=_sha256_json(_hard_economic_payload(capture)),
                unit="input-sha256",
                material_source_semantics=("partial-captured-selection",),
                restatement_semantics="unknown",
            )
        )
    for index, capture in enumerate(soft_inputs):
        observations.append(
            AcceptedObservationInput(
                metric_identity=f"soft-input:{index}",
                period_end="captured-input",
                observed_value=_sha256_json(capture.economic_payload()),
                accepted_value=_sha256_json(capture.economic_payload()),
                unit="input-sha256",
                material_source_semantics=("captured-effective-series",),
                restatement_semantics="partial",
            )
        )
    # Result clocks and exact citation identities do not describe investment events.
    # Blocked rules have no series reads, so retain their disposition in identity.
    for result in soft_results:
        if "reason" in result.details:
            stable = soft_result_economic_payload(result)
            observations.append(
                AcceptedObservationInput(
                    metric_identity=f"soft-blocked:{result.rule_name}",
                    period_end="unresolved-period",
                    observed_value=_sha256_json(stable),
                    accepted_value=_sha256_json(stable),
                    unit="soft-disposition-sha256",
                    material_source_semantics=("blocked",),
                    restatement_semantics="partial",
                )
            )
    if not retain_context:
        for result in soft_results:
            details = _HOLDINGS_PAYLOAD_ADAPTER.validate_python(result.details)
            details_json = _canonical_json(
                _semantic_evidence(details) if new_semantics else details
            )
            details_sha = hashlib.sha256(details_json.encode("utf-8")).hexdigest()
            last_period = result.details.get("last_period")
            observations.append(
                AcceptedObservationInput(
                    metric_identity=f"soft:{result.rule_name}",
                    period_end=str(last_period) if last_period else "unresolved-period",
                    observed_value=details_sha,
                    accepted_value=details_sha,
                    unit="soft-evidence-sha256",
                    currency=None,
                    material_source_semantics=("soft-evaluator-projection",),
                    restatement_semantics="source-provenance-not-retained",
                )
            )
    return ForwardSemanticInput(
        ticker=spec.ticker,
        thesis_content_sha256=_sha256_json(_thesis_content_payload(payload)),
        ruleset_version=_METRIC_RULESET_VERSION if new_semantics else _RULESET_VERSION,
        evaluator_semantic_version=(
            _METRIC_EVALUATOR_SEMANTIC_VERSION
            if new_semantics
            else (
                _THESIS_EVALUATOR_SEMANTIC_VERSION
                if retain_context
                else _LEGACY_EVALUATOR_SEMANTIC_VERSION
            )
        ),
        hard_rules=tuple(
            _semantic_rule(rule) for rule in (*spec.break_rules, *spec.business_model_rules)
        ),
        soft_rules=tuple(
            SemanticRuleInput(
                rule_id=rule.name,
                definition=cast("dict[str, JsonValue]", rule.model_dump(mode="json")),
            )
            for rule in (spec.soft_rules if effective_soft_rules is None else effective_soft_rules)
        ),
        accepted_observations=tuple(observations),
    )


def load_holdings_spec(holdings_dir: Path, ticker: str) -> HoldingsSpec:
    """Read `<holdings_dir>/<TICKER>.json` and return a typed HoldingsSpec.

    The on-disk JSON may have other fields (tier_1_kpis, break_conditions, etc.)
    used by the LLM skill — we deliberately ignore those and only consume
    `break_rules` + `business_model_rules`. Missing arrays return empty (no rules
    to evaluate).

    Rules in `break_rules` are tagged tier=universal at load time; rules in
    `business_model_rules` are tagged tier=business_model. Any explicit `tier`
    in the on-disk JSON is overridden by the array the rule lives in — the JSON
    layout is the source of truth, not a redundant field.
    """
    path = holdings_dir / f"{ticker.upper()}.json"
    if not path.exists():
        raise FileNotFoundError(f"Holdings spec not found: {path}")
    payload = _read_holdings_payload(path)
    return _holdings_spec_from_payload(payload, path=path)


def _holdings_spec_from_payload(payload: dict[str, JsonValue], *, path: Path) -> HoldingsSpec:
    """Validate one already-read holdings snapshot into its typed rule spec."""

    ticker_value = payload.get("ticker")
    thesis_value = payload.get("thesis")
    if not isinstance(ticker_value, str) or not isinstance(thesis_value, str):
        raise ValueError(f"Holdings spec requires string ticker and thesis: {path}")

    def rule_rows(key: str) -> list[dict[str, JsonValue]]:
        value = payload.get(key)
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
            raise ValueError(f"Holdings spec {key} must be an array of objects: {path}")
        return [cast("dict[str, JsonValue]", row) for row in value]

    universal_rules = [_load_rule(row, RuleTier.UNIVERSAL) for row in rule_rows("break_rules")]
    business_rules = [
        _load_rule(row, RuleTier.BUSINESS_MODEL) for row in rule_rows("business_model_rules")
    ]
    soft_rules = load_soft_rules(cast("list[object]", rule_rows("break_rules_soft")))
    return HoldingsSpec(
        ticker=ticker_value,
        thesis=thesis_value,
        break_rules=universal_rules,
        business_model_rules=business_rules,
        soft_rules=soft_rules,
    )


def _load_rule(raw: dict[str, JsonValue], tier: RuleTier) -> BreakRule:
    """Validate one rule dict, forcing the tier based on its parent array.

    `raw` may not be a dict at runtime (malformed JSON) — Pydantic raises a
    ValidationError there which propagates naturally. We strip any caller-set
    `tier` so the array placement always wins.
    """
    cleaned = {k: v for k, v in raw.items() if k != "tier"}
    rule = BreakRule.model_validate(cleaned)
    return rule.model_copy(update={"tier": tier})


def fetch_kpi_observations(
    conn: sqlite3.Connection,
    ticker: str,
    kpi_name: str,
    n_periods: int,
) -> list[KpiObservation] | None:
    """Public alias of the history fetcher for external rule evaluators.

    The decision-condition trigger (triggers.decision_condition) evaluates
    falsifiable conditions through the SAME resolver/dedup/cadence path as
    break rules, so its comparisons can never drift from this module's —
    the same guarantee ``convert_unit`` gives the unit dimension (#317/#320).
    """
    return _fetch_kpi_history(conn, ticker, kpi_name, n_periods)


def _fetch_kpi_history(
    conn: sqlite3.Connection,
    ticker: str,
    kpi_name: str,
    n_periods: int,
    *,
    trace: dict[str, JsonValue] | None = None,
) -> list[KpiObservation] | None:
    """Return up to `n_periods` most-recent kpi_facts observations for the rule's KPI.

    The rule's ``kpi_name`` is first resolved to the canonical
    ``kpi_definitions.name`` via the shared resolver, so a short break-rule label
    ("Monthly ARPAC") reaches the richest definition ("Monthly ARPAC (USD)")
    rather than exact-matching a sparse fragmented duplicate (the bug PR #195
    fixed for the §3 chart). No resolvable definition → no observations (caller
    treats that as OK / no-data), exactly as an exact-name miss did.

    **Cadence-aware**: when the resolved definition is annual-cadence (e.g. NU's
    Basel III capital adequacy ratio, disclosed once a year in the 20-F) only
    fiscal-year-end (``FY``) rows are read, so ``n_periods`` counts YEARS, not the
    last N rows of mixed cadence. An annual metric with genuine interim prints
    (NU CAR has a few) therefore evaluates "2 consecutive periods" as two annual
    values, never a year-end value paired with an interim one. Quarterly KPIs are
    unchanged (every period type is read, as before).

    Facts come from the canonical current relation, so an unresolved or
    otherwise non-current candidate cannot become a break-rule input merely
    because it was ingested later. Explicitly quarantined semantic contexts are
    excluded as well. This keeps the evaluator aligned with the governed KPI
    read path rather than maintaining another source-ranking rule.
    """
    resolved_name = resolve_kpi_definition_name(conn, ticker, kpi_name)
    if trace is not None:
        trace["selected_definition"] = resolved_name
        trace["disposition"] = "definition_unresolved" if resolved_name is None else "selected"
    if resolved_name is None:
        # Unresolvable: no kpi_facts definition matches this rule's KPI name
        # (e.g. a derived "... YoY change (bps)" series the pipeline hasn't
        # materialized, or a metric never extracted). Signal None so the caller
        # marks the rule UNRESOLVED rather than silently OK.
        return None
    return _fetch_kpi_history_for_resolved_definition(
        conn,
        ticker,
        resolved_name,
        n_periods,
        trace=trace,
    )


def _fetch_kpi_history_for_resolved_definition(
    conn: sqlite3.Connection,
    ticker: str,
    resolved_name: str,
    n_periods: int,
    *,
    trace: dict[str, JsonValue] | None = None,
    require_adjacent_quarters: bool = False,
) -> list[KpiObservation] | None:
    """Read one definition name returned by the governed report-reference resolver."""

    # Annual-cadence breakers count YEARS: restrict to FY rows so the
    # consecutive-periods window is annual, never a mix of year-end + interim.
    period_filter = ""
    params: list[object] = [ticker.upper(), resolved_name]
    if require_adjacent_quarters:
        period_filter = " AND kf.fiscal_period_type IN ('Q1','Q2','Q3','Q4')"
    elif reporting_cadence_for(conn, ticker, resolved_name) == "annual":
        placeholders = ",".join("?" * len(ANNUAL_FACT_PERIOD_TYPES))
        period_filter = f" AND kf.fiscal_period_type IN ({placeholders})"
        params.extend(ANNUAL_FACT_PERIOD_TYPES)
    params.append(n_periods)
    fact_relation = canonical_fact_relation(conn, "kpi_facts")
    # Thesis decisions are decision-grade consumers, not a shadow rollout.
    # Missing/legacy semantic context must remain unresolved rather than
    # silently entering a break-rule series.
    semantic_join, semantic_where = semantic_admission_sql(conn, fail_closed=True)
    semantic_where += " AND " + semantic_series_identity_sql(conn)
    context_columns = [
        str(row[1]) for row in conn.execute("PRAGMA table_info(kpi_fact_semantic_contexts)")
    ]
    context_pairs = ",".join(f"'{column}',ksc.{column}" for column in context_columns)
    context_projection = (
        f", json_object({context_pairs}) AS retained_semantic_context_json" if semantic_join else ""
    )
    if trace is not None:
        trace["selection_mode"] = fact_relation.selection_mode
    input_projection = kpi_input_projection_sql(
        conn, fact_alias="kf", definition_alias="kd", semantic_joined=bool(semantic_join)
    )
    if fact_relation.selection_mode == "legacy_pre_cutover":
        # Pre-cutover fixtures have no canonical resolver relation. Preserve
        # their historic one-row-per-period shape by stable fact-row identity;
        # this is deliberately not a source-document ranking policy.
        query = (
            "WITH eligible AS ("
            f"SELECT kf.*{context_projection}, {input_projection}, "
            "ROW_NUMBER() OVER (PARTITION BY kf.kpi_definition_id, kf.period_end, "
            "kf.fiscal_period_type ORDER BY kf.id DESC) AS rn "
            f"FROM {fact_relation.sql} kf JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id "  # nosec B608 -- canonical relation is a closed internal identifier
            f"{semantic_join} "
            "WHERE kf.ticker = ? AND kd.name = ? AND " + semantic_where + period_filter + ") "
            "SELECT * FROM eligible "
            "WHERE rn = 1 ORDER BY period_end DESC LIMIT ?"
        )
    else:
        query = (
            f"SELECT kf.*{context_projection}, {input_projection} "
            f"FROM {fact_relation.sql} kf JOIN kpi_definitions kd ON kd.id = kf.kpi_definition_id "  # nosec B608 -- canonical relation is a closed internal identifier
            f"{semantic_join} "
            "WHERE kf.ticker = ? AND kd.name = ? AND " + semantic_where + period_filter + " "
            "ORDER BY kf.period_end DESC LIMIT ?"
        )
    cur = conn.execute(query, params)
    # Decision-grade thesis inputs never inherit admission from a mutable scalar
    # override. A corrected value becomes eligible only after it is persisted as
    # a source-reviewed superseding fact with its own admitted semantic head.
    ov_map = active_scalar_override_map(
        conn, ticker=ticker, fact_kind=OVERRIDE_KPI, fact_key=resolved_name
    )
    out: list[KpiObservation] = []
    selected_rows = cur.fetchall()
    if trace is not None:
        trace["selected_rows"] = [
            _HOLDINGS_PAYLOAD_ADAPTER.validate_python(dict(row)) for row in selected_rows
        ]
        trace["active_overrides"] = [
            _HOLDINGS_PAYLOAD_ADAPTER.validate_python(asdict(override))
            for override in ov_map.values()
        ]
    for row in selected_rows:
        period_key = str(row["period_end"])[:10]
        period_type = str(row["fiscal_period_type"])
        ov = ov_map.get((period_key, period_type))
        if ov is not None:
            if trace is not None:
                trace["disposition"] = "active_scalar_override"
                trace["override_id"] = ov.id
            log.warning(
                "thesis_kpi_history_unreviewed_override",
                extra={
                    "ticker": ticker.upper(),
                    "kpi_name": resolved_name,
                    "period_end": period_key,
                    "fiscal_period_type": period_type,
                    "override_id": ov.id,
                    "override_action": ov.action,
                },
            )
            return None
        period = row["period_end"]
        if isinstance(period, str):
            period = datetime.fromisoformat(period)
        value = Decimal(str(row["value"]))
        unit = Unit(row["unit"])
        out.append(
            KpiObservation(
                period_end=period,
                value=value,
                unit=unit,
                provenance=_HOLDINGS_PAYLOAD_ADAPTER.validate_python(dict(row)),
                fiscal_period_type=period_type,
                input_reference=kpi_input_reference(
                    row, selection_mode=fact_relation.selection_mode
                ),
            )
        )
    # The definition resolver can find fact-carrying definitions whose rows are
    # all semantically missing, quarantined, or non-current.  That is an
    # unevaluable decision input, not a passing empty series.
    if not out and trace is not None:
        trace["disposition"] = "no_admitted_rows"
    return out or None


def _compare(value: Decimal, comparator: Comparator, threshold: Decimal) -> bool:
    """Apply a comparator. No fallback — unsupported comparators raise upstream."""
    if comparator is Comparator.LT:
        return value < threshold
    if comparator is Comparator.LE:
        return value <= threshold
    if comparator is Comparator.GT:
        return value > threshold
    if comparator is Comparator.GE:
        return value >= threshold
    if comparator is Comparator.EQ:
        return value == threshold
    raise ValueError(f"Unhandled comparator: {comparator}")


# Unit reconciliation: a rule's threshold is in the rule's declared `unit`, but the
# kpi_facts that satisfy it may be stored in a different unit (the LLM extractor's
# per-call guess), so comparing the bare numbers misfires — `115_000_000 < 80` (raw
# dollars vs a `<80` millions threshold) could never breach. `evaluate_rule`
# reconciles every observation to the rule's unit via the shared `convert_unit`
# (imported above) before comparing, and persists the reconciled value so the §2
# brief panel reads in the threshold's unit. That same helper runs at persist time in
# `pipeline.kpi_persistence`, so a fact's stored unit and the unit it's compared in
# can't drift. Cross-family pairs (a money magnitude vs a proportion vs a raw count)
# have no valid conversion → the rule is surfaced UNRESOLVED, not mis-compared.


def _adjacent_rule_quarters(observations: list[KpiObservation]) -> bool:
    """Accept fiscal quarter labels, or calendar quarters when labels are absent."""
    quarterly_types = {"Q1", "Q2", "Q3", "Q4"}
    for observation in observations:
        if (
            observation.fiscal_period_type is not None
            and observation.fiscal_period_type not in quarterly_types
        ):
            return False
    for newer, older in pairwise(observations):
        if not 60 <= (newer.period_end - older.period_end).days <= 120:
            return False
        if newer.fiscal_period_type is not None and older.fiscal_period_type is not None:
            new_quarter, old_quarter = (
                int(newer.fiscal_period_type[1]),
                int(older.fiscal_period_type[1]),
            )
            if newer.fiscal_year is not None and older.fiscal_year is not None:
                if newer.fiscal_year * 4 + new_quarter != older.fiscal_year * 4 + old_quarter + 1:
                    return False
            elif new_quarter != old_quarter % 4 + 1:
                return False
        else:
            new_index = newer.period_end.year * 4 + (newer.period_end.month - 1) // 3
            old_index = older.period_end.year * 4 + (older.period_end.month - 1) // 3
            if new_index != old_index + 1:
                return False
    return True


def evaluate_rule(rule: BreakRule, observations: list[KpiObservation] | None) -> RuleEvaluation:
    """Classify one rule given its observations.

    Observations are first reconciled to the rule's declared unit (see
    ``convert_unit``) so the comparison runs on the same dimension as the
    threshold and the persisted evidence reads in the threshold's unit.

    UNRESOLVED: ``observations is None`` — the rule's KPI didn't resolve to any
                kpi_facts definition; OR an observation's stored unit can't be
                converted to the rule's unit (cross-family mismatch). Either way
                the breaker can't be evaluated — kept distinct from OK so an
                unevaluable breaker isn't read as passing.
    BREACH: have >= consecutive_periods observations and ALL match the rule.
    WARN:   any observation matches but not all consecutive_periods of them.
    OK:     no observation matches the rule (incl. resolved-but-no-rows-yet).
    """
    if observations is None:
        return RuleEvaluation(
            rule=rule,
            status=BreachStatus.UNRESOLVED,
            observations=(),
            detail=(
                "unresolved: rule KPI has no matching kpi_facts definition — "
                "needs a derived or extracted series for this metric"
            ),
        )
    if rule.require_adjacent_quarters and (
        len(observations) < rule.consecutive_periods
        or not _adjacent_rule_quarters(observations[: rule.consecutive_periods])
    ):
        return RuleEvaluation(
            rule=rule,
            status=BreachStatus.UNRESOLVED,
            observations=tuple(observations),
            detail="unresolved: insufficient adjacent quarterly observations",
        )
    if not observations:
        return RuleEvaluation(
            rule=rule,
            status=BreachStatus.OK,
            observations=(),
            detail="resolved, but no observations on file yet",
        )
    # Reconcile each observation to the rule's declared unit so the comparison
    # — and the evidence we persist for the §2 brief panel — is dimensionally
    # consistent with the threshold. A cross-family unit (e.g. a money magnitude
    # where the rule expects a percentage) has no valid conversion: surface it as
    # UNRESOLVED rather than compare blindly, keeping the raw observations as
    # evidence so the data gap is visible.
    reconciled: list[KpiObservation] = []
    for obs in observations:
        converted = convert_unit(obs.value, obs.unit, rule.unit)
        if converted is None:
            return RuleEvaluation(
                rule=rule,
                status=BreachStatus.UNRESOLVED,
                observations=tuple(observations),
                detail=(
                    f"unresolved: observation unit {obs.unit.value!r} is not "
                    f"convertible to rule unit {rule.unit.value!r} — fix the "
                    f"rule's declared unit or the KPI's stored unit"
                ),
            )
        reconciled.append(
            KpiObservation(
                period_end=obs.period_end,
                value=converted,
                unit=rule.unit,
                provenance=obs.provenance,
                fiscal_period_type=obs.fiscal_period_type,
                fiscal_year=obs.fiscal_year,
                input_reference=obs.input_reference,
            )
        )
    observations = reconciled
    matches = [_compare(obs.value, rule.comparator, rule.threshold) for obs in observations]
    matching_count = sum(matches)
    obs_tuple = tuple(observations)

    if matching_count == 0:
        return RuleEvaluation(
            rule=rule,
            status=BreachStatus.OK,
            observations=obs_tuple,
            detail=f"none of last {len(observations)} obs match",
        )
    if matching_count >= rule.consecutive_periods and all(matches[: rule.consecutive_periods]):
        latest = observations[0]
        return RuleEvaluation(
            rule=rule,
            status=BreachStatus.BREACH,
            observations=obs_tuple,
            detail=(
                f"breach: {rule.kpi_name}={latest.value} {rule.comparator.value} "
                f"{rule.threshold} for {rule.consecutive_periods} consecutive periods"
            ),
        )
    return RuleEvaluation(
        rule=rule,
        status=BreachStatus.WARN,
        observations=obs_tuple,
        detail=f"{matching_count}/{len(observations)} obs match (not yet consecutive)",
    )


_STATUS_RANK: dict[BreachStatus, int] = {
    # UNRESOLVED ranks with OK for the worst-rule rollup (a breaker we can't
    # evaluate must not raise a false BREACH); the §2 panel surfaces it per-rule
    # so the data gap stays visible.
    BreachStatus.UNRESOLVED: 0,
    BreachStatus.OK: 0,
    BreachStatus.WARN: 1,
    BreachStatus.BREACH: 2,
}


def _rollup_status(evaluations: list[RuleEvaluation]) -> BreachStatus:
    """Holding-level status from hard rules only = worst-rule status. Empty -> OK.

    UNRESOLVED never becomes the OVERALL verdict: it ranks with OK, but ``max``
    can surface an UNRESOLVED element on an otherwise all-clear holding. The
    overall is the worst EVALUABLE status (OK/WARN/BREACH); per-rule UNRESOLVED
    stays visible in the §2 panel so the data gap isn't hidden.
    """
    if not evaluations:
        return BreachStatus.OK
    worst = max(evaluations, key=lambda e: _STATUS_RANK[e.status]).status
    return BreachStatus.OK if worst is BreachStatus.UNRESOLVED else worst


def _rollup_with_soft(
    hard_evaluations: list[RuleEvaluation],
    soft_results: list[SoftRuleResult],
) -> BreachStatus:
    """Combined rollup: hard BREACH wins; else any soft YELLOW/UNRESOLVED → WARN; else OK.

    Hard WARN (some-but-not-all consecutive periods matched) is preserved as
    WARN. Soft rules never escalate past WARN — that's a design contract:
    "the curve is bending" is a watch signal, not a thesis-broken signal.

    A soft rule that's UNRESOLVED (couldn't be evaluated — no data, or a
    data-quality guard tripped) escalates to WARN too, not just YELLOW. This
    closes the exact gap the 2026-07 red-team audit found: NU's compound
    net-adds/Brazil-penetration tripwire existed only as thesis prose, one
    leg was already lit, and the panel showed plain OK because there was no
    machine signal at all. An UNRESOLVED soft rule IS a machine signal — "this
    needs attention, the data can't confirm or deny it" — and must be visible
    at the rollup level, not just in the per-rule soft-signals panel.
    """
    hard_status = _rollup_status(hard_evaluations)
    if hard_status is BreachStatus.BREACH:
        return BreachStatus.BREACH
    any_soft_needs_attention = any(
        r.status in (SoftRuleStatus.YELLOW, SoftRuleStatus.UNRESOLVED) for r in soft_results
    )
    if any_soft_needs_attention:
        return BreachStatus.WARN
    return hard_status


def _verified_report_rule_name(
    conn: sqlite3.Connection,
    *,
    repo_root: Path,
    ticker: str,
    json_pointer: str,
    selections: list[dict[str, JsonValue]] | None = None,
) -> str | None:
    reference = report_kpi_reference_at(
        repo_root,
        ticker=ticker,
        json_pointer=json_pointer,
    )
    if reference is None:
        if selections is not None:
            selections.append({"json_pointer": json_pointer, "disposition": "reference_missing"})
        return None
    verified = verified_report_kpi_reference_definition(
        conn,
        repo_root=repo_root,
        user_id=DEFAULT_USER_ID,
        reference=reference,
    )
    if selections is not None:
        selections.append(
            {
                "reference": reference.model_dump(mode="json"),
                "verified": None if verified is None else verified.model_dump(mode="json"),
                "disposition": "blocked" if verified is None else "verified",
            }
        )
    return None if verified is None else verified.definition_name


def _replace_json_pointer_value(
    payload: dict[str, JsonValue], *, json_pointer: str, value: str
) -> bool:
    """Replace one already-inventoried scalar without guessing through drift."""
    parts = [part.replace("~1", "/").replace("~0", "~") for part in json_pointer.split("/")[1:]]
    if not parts:
        return False
    current: object = payload
    for part in parts[:-1]:
        if isinstance(current, dict):
            current = cast("dict[str, object]", current).get(part)
        elif isinstance(current, list) and part.isdigit():
            items = cast("list[object]", current)
            index = int(part)
            if index >= len(items):
                return False
            current = items[index]
        else:
            return False
    final = parts[-1]
    if isinstance(current, dict) and final in current:
        cast("dict[str, object]", current)[final] = value
        return True
    if isinstance(current, list) and final.isdigit():
        items = cast("list[object]", current)
        index = int(final)
        if index < len(items):
            items[index] = value
            return True
    return False


def _soft_rules_with_raw_indexes(
    payload: dict[str, JsonValue],
) -> list[tuple[int, SoftRule]]:
    """Load executable soft rules without losing their source-array identity."""
    raw_rules = payload.get("break_rules_soft")
    if not isinstance(raw_rules, list):
        return []
    out: list[tuple[int, SoftRule]] = []
    for raw_index, raw_rule in enumerate(cast("list[object]", raw_rules)):
        loaded = load_soft_rules([raw_rule])
        if loaded:
            out.append((raw_index, loaded[0]))
    return out


def _verified_soft_rules(
    conn: sqlite3.Connection,
    *,
    repo_root: Path,
    ticker: str,
    payload: dict[str, JsonValue],
    selections: list[dict[str, JsonValue]] | None = None,
) -> tuple[list[tuple[int, SoftRule]], frozenset[int]]:
    """Bind every supported KPI soft-rule leaf to its reviewed definition."""
    inventory = load_report_kpi_reference_inventory(repo_root, (ticker.upper(),))
    source = inventory.source_states[0]
    if selections is not None:
        selections.append({"soft_inventory_source": source.model_dump(mode="json")})
    if source.status is not ReportKpiReferenceSourceStatus.VALID:
        original_rules = _soft_rules_with_raw_indexes(payload)
        return original_rules, frozenset(raw_index for raw_index, _ in original_rules)
    rebound = copy.deepcopy(payload)
    blocked: set[int] = set()
    for reference in inventory.references:
        if reference.reference_kind is not ReportKpiReferenceKind.SOFT_RULE_KPI:
            continue
        pointer_parts = reference.json_pointer.split("/")
        if len(pointer_parts) < 3 or not pointer_parts[2].isdigit():
            continue
        rule_index = int(pointer_parts[2])
        verified_name = _verified_report_rule_name(
            conn,
            repo_root=repo_root,
            ticker=ticker,
            json_pointer=reference.json_pointer,
            selections=selections,
        )
        if verified_name is None or not _replace_json_pointer_value(
            rebound,
            json_pointer=reference.json_pointer,
            value=verified_name or "",
        ):
            blocked.add(rule_index)
    return _soft_rules_with_raw_indexes(rebound), frozenset(blocked)


def _evaluate_verified_soft_rules(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    rules: list[tuple[int, SoftRule]],
    blocked_indexes: frozenset[int],
    captures: list[SoftEvaluationCapture] | None = None,
) -> list[SoftRuleResult]:
    out: list[SoftRuleResult] = []
    for raw_index, rule in rules:
        if raw_index not in blocked_indexes:
            out.extend(evaluate_soft_rules(ticker, [rule], conn, captures=captures))
            continue
        out.append(
            SoftRuleResult(
                rule_name=rule.name,
                status=SoftRuleStatus.UNRESOLVED,
                evidence="unresolved: report KPI reference is not currently verified",
                details={"reason": "unverified_report_kpi_reference"},
                evaluated_at=datetime.now(),
            )
        )
    return out


def _verified_metric_expression(
    conn: sqlite3.Connection,
    *,
    repo_root: Path,
    ticker: str,
    expression: MetricExpression,
    json_pointer: str,
    selections: list[dict[str, JsonValue]] | None = None,
) -> MetricExpression | None:
    """Resolve each KPI leaf through its reviewed, source-file-bound reference."""
    if expression.operation == "level" and expression.source == "kpi":
        name = _verified_report_rule_name(
            conn,
            repo_root=repo_root,
            ticker=ticker,
            json_pointer=f"{json_pointer}/name",
            selections=selections,
        )
        return None if name is None else expression.model_copy(update={"name": name})
    updates: dict[str, MetricExpression] = {}
    for key in ("numerator", "denominator", "input", "left", "right"):
        child = getattr(expression, key)
        if child is None:
            continue
        rebound = _verified_metric_expression(
            conn,
            repo_root=repo_root,
            ticker=ticker,
            expression=child,
            json_pointer=f"{json_pointer}/{key}",
            selections=selections,
        )
        if rebound is None:
            return None
        updates[key] = rebound
    return expression.model_copy(update=updates)


def _evaluate_metric_rule(rule: BreakRule, result: MetricSeriesResult) -> RuleEvaluation:
    manifest = _HOLDINGS_PAYLOAD_ADAPTER.validate_python(result.model_dump(mode="json"))
    if result.status != "available" or len(result.points) < rule.consecutive_periods:
        return RuleEvaluation(
            rule=rule,
            status=BreachStatus.UNRESOLVED,
            observations=(),
            detail=f"unresolved: {result.reason_code or 'insufficient_metric_periods'}",
            source_manifest=manifest,
        )
    calculated_history: list[KpiObservation] = []
    for point in reversed(result.points[-rule.consecutive_periods :]):
        try:
            unit = Unit.ACTUAL if point.currency == point.unit else Unit(point.unit)
        except ValueError:
            return RuleEvaluation(
                rule=rule,
                status=BreachStatus.UNRESOLVED,
                observations=(),
                detail=f"unresolved: unsupported calculated unit {point.unit!r}",
                source_manifest=manifest,
            )
        calculated_history.append(
            KpiObservation(
                point.period_end,
                point.value,
                unit,
                fiscal_period_type=point.fiscal_period,
                fiscal_year=point.fiscal_year,
            )
        )
    evaluation = evaluate_rule(rule, calculated_history)
    return RuleEvaluation(
        rule=evaluation.rule,
        status=evaluation.status,
        observations=evaluation.observations,
        detail=evaluation.detail,
        source_manifest=manifest,
    )


def _evaluate_hard_rule(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    rule: BreakRule,
    repo_root: Path | None,
    json_pointer: str,
    cutoff: datetime,
    captures: list[HardRuleCapture],
    references: list[dict[str, JsonValue]],
) -> RuleEvaluation:
    pointer = f"{json_pointer}/kpi_name"
    trace: dict[str, JsonValue] = {
        "selected_definition": None,
        "disposition": "unverified_report_reference",
    }
    metric_input: MetricEvaluationCapture | None = None
    if rule.metric_expression is not None:
        expression = rule.metric_expression
        pointer = f"{json_pointer}/metric_expression"
        if repo_root is not None:
            rebound = _verified_metric_expression(
                conn,
                repo_root=repo_root,
                ticker=ticker,
                expression=expression,
                json_pointer=pointer,
                selections=references,
            )
            if rebound is None:
                captures.append(
                    HardRuleCapture(
                        rule=_HOLDINGS_PAYLOAD_ADAPTER.validate_python(
                            rule.model_dump(mode="json")
                        ),
                        json_pointer=pointer,
                        selected_definition=None,
                        disposition="unverified_report_reference",
                        selection_details=trace,
                        observations=None,
                    )
                )
                return RuleEvaluation(
                    rule=rule,
                    status=BreachStatus.UNRESOLVED,
                    observations=(),
                    detail="unresolved: unverified metric KPI reference",
                )
            expression = rebound
        metric_captures: list[MetricEvaluationCapture] = []
        result = calculate_metric_series(
            conn, ticker, expression, cutoff=cutoff, captures=metric_captures
        )
        metric_input = metric_captures[0]
        evaluation = _evaluate_metric_rule(rule, result)
        history = list(evaluation.observations)
        trace["disposition"] = "selected" if result.status == "available" else "no_admitted_rows"
        trace["expression"] = expression.model_dump(mode="json")
    else:
        verified_name = (
            None
            if repo_root is None
            else _verified_report_rule_name(
                conn,
                repo_root=repo_root,
                ticker=ticker,
                json_pointer=pointer,
                selections=references,
            )
        )
        if repo_root is not None and verified_name is None:
            history = None
        elif verified_name is not None or rule.require_adjacent_quarters:
            resolved_name = verified_name or resolve_kpi_definition_name(
                conn, ticker, rule.kpi_name
            )
            trace["selected_definition"] = resolved_name
            trace["disposition"] = "definition_unresolved" if resolved_name is None else "selected"
            history = (
                None
                if resolved_name is None
                else _fetch_kpi_history_for_resolved_definition(
                    conn,
                    ticker,
                    resolved_name,
                    rule.consecutive_periods,
                    trace=trace,
                    require_adjacent_quarters=rule.require_adjacent_quarters,
                )
            )
        else:
            history = _fetch_kpi_history(
                conn, ticker, rule.kpi_name, rule.consecutive_periods, trace=trace
            )
        evaluation = evaluate_rule(rule, history)
        evaluation = replace(
            evaluation, source_manifest=_plain_kpi_manifest(evaluation.observations)
        )
    selected_name = trace.get("selected_definition")
    definition_row = (
        None
        if not isinstance(selected_name, str)
        else conn.execute(
            "SELECT * FROM kpi_definitions WHERE UPPER(ticker)=? AND name=?",
            (ticker.upper(), selected_name),
        ).fetchone()
    )
    captures.append(
        HardRuleCapture.model_validate(
            dict(
                rule=rule.model_dump(mode="json"),
                json_pointer=pointer,
                selected_definition=selected_name if isinstance(selected_name, str) else None,
                selected_definition_content=None
                if definition_row is None
                else _HOLDINGS_PAYLOAD_ADAPTER.validate_python(dict(definition_row)),
                disposition=trace["disposition"],
                selection_details=trace,
                observations=None
                if history is None
                else tuple(
                    CapturedKpiObservation(
                        period_end=obs.period_end,
                        value=str(obs.value),
                        unit=obs.unit.value,
                        provenance=obs.provenance,
                        input_reference=obs.input_reference,
                        fiscal_period_type=obs.fiscal_period_type,
                        fiscal_year=obs.fiscal_year,
                    )
                    for obs in history
                ),
                metric_input=metric_input,
            )
        )
    )
    references.append({"json_pointer": pointer, "selection": trace})
    return evaluation


def evaluate_ticker_thesis(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    holdings_dir: Path,
) -> ThesisVerdict:
    """End-to-end: load rules, fetch history, evaluate, roll up. No DB writes.

    Universal tripwires (`spec.break_rules`) are evaluated before per-ticker
    business-model rules (`spec.business_model_rules`); the §2 renderer relies
    on this order to keep catastrophic breakers visually first.
    """
    holdings_path = holdings_dir / f"{ticker.upper()}.json"
    payload = _read_holdings_payload(holdings_path)
    spec = _holdings_spec_from_payload(payload, path=holdings_path)
    cutoff = datetime.now(UTC)
    evaluations: list[RuleEvaluation] = []
    hard_inputs: list[HardRuleCapture] = []
    soft_inputs: list[SoftEvaluationCapture] = []
    references: list[dict[str, JsonValue]] = []
    repo_root = (
        holdings_dir.parent.parent
        if holdings_dir.name == "holdings" and holdings_dir.parent.name == "micro_thesis"
        else None
    )
    for array_name, rules in (
        ("break_rules", spec.break_rules),
        ("business_model_rules", spec.business_model_rules),
    ):
        for index, rule in enumerate(rules):
            evaluations.append(
                _evaluate_hard_rule(
                    conn,
                    ticker=ticker,
                    rule=rule,
                    repo_root=repo_root,
                    json_pointer=f"/{array_name}/{index}",
                    cutoff=cutoff,
                    captures=hard_inputs,
                    references=references,
                )
            )
    if repo_root is None:
        effective_soft_rules = spec.soft_rules
        soft_results = evaluate_soft_rules(
            spec.ticker.upper(), spec.soft_rules, conn, captures=soft_inputs
        )
    else:
        verified_soft_rules, blocked_soft_indexes = _verified_soft_rules(
            conn, repo_root=repo_root, ticker=ticker, payload=payload, selections=references
        )
        effective_soft_rules = [rule for _, rule in verified_soft_rules]
        soft_results = _evaluate_verified_soft_rules(
            conn,
            ticker=spec.ticker.upper(),
            rules=verified_soft_rules,
            blocked_indexes=blocked_soft_indexes,
            captures=soft_inputs,
        )
    semantic = _build_semantic_input(
        payload=payload,
        spec=spec,
        evaluations=evaluations,
        soft_results=soft_results,
        hard_inputs=tuple(hard_inputs),
        soft_inputs=tuple(soft_inputs),
        effective_soft_rules=effective_soft_rules,
        retain_context=True,
    )
    verdict = ThesisVerdict(
        ticker=spec.ticker.upper(),
        thesis=spec.thesis,
        overall_status=_rollup_with_soft(evaluations, soft_results),
        rule_evaluations=tuple(evaluations),
        evaluated_at=cutoff,
        soft_rule_results=tuple(soft_results),
        semantic_input=semantic,
    )
    blocked_results: list[SoftRuleResult] = []
    result_order: list[int] = []
    executed_index = 0
    for result in soft_results:
        if result.details.get("reason") == "unverified_report_kpi_reference":
            blocked_results.append(result)
            result_order.append(-len(blocked_results))
        else:
            result_order.append(executed_index)
            executed_index += 1
    context = RetainedThesisContext(
        ticker=verdict.ticker,
        holdings_payload=payload,
        belief_payload=_thesis_content_payload(payload),
        original_spec=spec.model_dump(mode="json"),
        hard_inputs=tuple(hard_inputs),
        soft_inputs=tuple(soft_inputs),
        effective_soft_rules=tuple(effective_soft_rules),
        soft_result_order=tuple(result_order),
        blocked_soft_results=tuple(blocked_results),
        reference_selections=tuple(references),
        evaluated_at=verdict.evaluated_at,
        semantic=semantic,
        severity=EpisodeSeverity(verdict.overall_status.value),
        evidence_as_of=_episode_evidence_as_of(verdict),
        rule_outputs=_episode_rule_projection(verdict),
        soft_outputs=_episode_soft_projection(verdict),
        stable_soft_outputs=tuple(soft_result_economic_payload(result) for result in soft_results),
    )
    return replace(verdict, retained_context=context)


def replay_check_context(context: RetainedThesisContext) -> ThesisVerdict:
    """Execute supported saved rules without opening current files or databases."""
    if context.semantic.evaluator_semantic_version not in {
        _THESIS_EVALUATOR_SEMANTIC_VERSION,
        _METRIC_EVALUATOR_SEMANTIC_VERSION,
    }:
        raise EpisodeStoreError("saved evaluator version cannot be replayed")
    spec = _holdings_spec_from_payload(
        context.holdings_payload, path=Path("retained-holdings.json")
    )
    if (
        spec.model_dump(mode="json") != context.original_spec
        or _thesis_content_payload(context.holdings_payload) != context.belief_payload
    ):
        raise EpisodeStoreError("saved original thesis/rules differ")
    original_hard = [
        rule.model_dump(mode="json") for rule in (*spec.break_rules, *spec.business_model_rules)
    ]
    if original_hard != [capture.rule for capture in context.hard_inputs]:
        raise EpisodeStoreError("saved hard rules differ from original rules")
    evaluations: list[RuleEvaluation] = []
    for capture in context.hard_inputs:
        rule = BreakRule.model_validate(capture.rule)
        if rule.metric_expression is not None:
            if capture.metric_input is None:
                if capture.disposition != "unverified_report_reference":
                    raise EpisodeStoreError("saved metric input is missing")
                evaluation = RuleEvaluation(
                    rule=rule,
                    status=BreachStatus.UNRESOLVED,
                    observations=(),
                    detail="unresolved: unverified metric KPI reference",
                )
            else:
                if (
                    capture.metric_input.ticker != context.ticker
                    or capture.metric_input.cutoff != context.evaluated_at
                ):
                    raise EpisodeStoreError("saved metric identity differs")
                try:
                    result = replay_metric_capture(capture.metric_input)
                except MetricReplayError as exc:
                    raise EpisodeStoreError(str(exc)) from exc
                evaluation = _evaluate_metric_rule(rule, result)
        else:
            if capture.metric_input is not None:
                raise EpisodeStoreError("level rule has unexpected metric input")
            evaluation = evaluate_rule(
                rule,
                None
                if capture.observations is None
                else [
                    KpiObservation(
                        period_end=obs.period_end,
                        value=Decimal(obs.value),
                        unit=Unit(obs.unit),
                        provenance=obs.provenance,
                        input_reference=obs.input_reference,
                        fiscal_period_type=obs.fiscal_period_type,
                        fiscal_year=obs.fiscal_year,
                    )
                    for obs in capture.observations
                ],
            )
            evaluation = replace(
                evaluation, source_manifest=_plain_kpi_manifest(evaluation.observations)
            )
        evaluations.append(evaluation)
    executed = [
        result for capture in context.soft_inputs for result in replay_soft_capture(capture)
    ]
    try:
        soft_results = [
            executed[index] if index >= 0 else context.blocked_soft_results[-index - 1]
            for index in context.soft_result_order
        ]
    except IndexError as exc:
        raise EpisodeStoreError("saved soft execution order is invalid") from exc
    if sorted(index for index in context.soft_result_order if index >= 0) != list(
        range(len(executed))
    ) or sorted(-index - 1 for index in context.soft_result_order if index < 0) != list(
        range(len(context.blocked_soft_results))
    ):
        raise EpisodeStoreError("saved soft execution population differs")
    effective_soft_rules = list(context.effective_soft_rules)
    semantic = _build_semantic_input(
        payload=context.holdings_payload,
        spec=spec,
        evaluations=evaluations,
        soft_results=soft_results,
        hard_inputs=context.hard_inputs,
        soft_inputs=context.soft_inputs,
        effective_soft_rules=effective_soft_rules,
        retain_context=True,
    )
    verdict = ThesisVerdict(
        ticker=context.ticker,
        thesis=spec.thesis,
        overall_status=_rollup_with_soft(evaluations, soft_results),
        rule_evaluations=tuple(evaluations),
        evaluated_at=context.evaluated_at,
        soft_rule_results=tuple(soft_results),
        semantic_input=semantic,
        retained_context=context,
    )
    if (
        semantic != context.semantic
        or EpisodeSeverity(verdict.overall_status.value) != context.severity
        or _episode_rule_projection(verdict) != context.rule_outputs
        or _episode_soft_projection(verdict) != context.soft_outputs
        or _episode_evidence_as_of(verdict) != context.evidence_as_of
    ):
        raise EpisodeStoreError("saved thesis output differs from deterministic replay")
    return verdict


def _serialize_soft_rule_results(verdict: ThesisVerdict) -> str | None:
    """Render soft rule results as a stable JSON string for the soft column.

    Returns None when there are no soft results, so the column stays NULL for
    holdings without `break_rules_soft` and the renderer can distinguish
    "no rules" from "rules evaluated, all green".
    """
    if not verdict.soft_rule_results:
        return None
    payload = [
        {
            "rule_name": r.rule_name,
            "status": r.status.value,
            "evidence": r.evidence,
            "evaluated_at": r.evaluated_at.isoformat(),
            "details": r.details,
        }
        for r in verdict.soft_rule_results
    ]
    return json.dumps(payload, separators=(",", ":"), default=str)


def _serialize_rule_evaluations(verdict: ThesisVerdict) -> str:
    """Render rule_evaluations as a stable JSON string for thesis_evaluations history.

    `tier` is included so the §2 renderer can split universal tripwires from
    per-ticker business-model breakers without re-reading the holdings JSON.
    Older persisted rows without `tier` are treated as business_model at parse
    time (see report.sections.thesis._parse_evaluation).
    """
    payload = [
        {
            "rule_id": e.rule.rule_id,
            "kpi_name": e.rule.kpi_name,
            "comparator": e.rule.comparator.value,
            "threshold": str(e.rule.threshold),
            "consecutive_periods": e.rule.consecutive_periods,
            "tier": e.rule.tier.value,
            "status": e.status.value,
            "detail": e.detail,
            "narrative": e.rule.narrative,
            **(
                {
                    "metric_expression": (
                        e.rule.metric_expression.model_dump(mode="json")
                        if e.rule.metric_expression is not None
                        else None
                    ),
                    "require_adjacent_quarters": e.rule.require_adjacent_quarters,
                    "source_manifest": e.source_manifest,
                }
                if _new_hard_semantics(e.rule)
                else {}
            ),
            **({"source_manifest": e.source_manifest} if e.source_manifest is not None else {}),
            "observations": [
                {
                    "period_end": obs.period_end.isoformat(),
                    "value": str(obs.value),
                    "unit": obs.unit.value,
                }
                for obs in e.observations
            ],
        }
        for e in verdict.rule_evaluations
    ]
    return json.dumps(payload, separators=(",", ":"))


def _episode_schema_active(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT type FROM sqlite_master WHERE name='thesis_evaluation_episodes'"
    ).fetchone()
    return row is not None and str(row[0]) == "table"


def _episode_rule_projection(verdict: ThesisVerdict) -> tuple[dict[str, JsonValue], ...]:
    parsed = _RULE_PROJECTION_ADAPTER.validate_json(_serialize_rule_evaluations(verdict))
    for row in parsed:
        manifest = row.get("source_manifest")
        if (
            isinstance(manifest, dict)
            and _plain_evidence_manifest(
                manifest, calculated=row.get("metric_expression") is not None
            )
            is not None
        ):
            if "metric_expression" in row:
                row["source_manifest"] = None  # preexisting v2 plain field
            else:
                row.pop("source_manifest")  # preexisting v1 plain projection
    if not _new_verdict_semantics(verdict):
        return tuple(parsed)
    return tuple(
        _HOLDINGS_PAYLOAD_ADAPTER.validate_python(_semantic_evidence(row)) for row in parsed
    )


def _plain_annotation_json(rule_json: str) -> str:
    rows = _RULE_PROJECTION_ADAPTER.validate_json(rule_json)
    annotations: dict[str, JsonValue] = {}
    for row in rows:
        manifest = row.get("source_manifest")
        rule_id = row.get("rule_id")
        if not isinstance(manifest, dict) or not isinstance(rule_id, str):
            continue
        admitted = _plain_evidence_manifest(
            manifest, calculated=row.get("metric_expression") is not None
        )
        if admitted is not None:
            annotations[rule_id] = _HOLDINGS_PAYLOAD_ADAPTER.validate_python(
                admitted.model_dump(mode="json")
            )
    return _canonical_json(_semantic_evidence(annotations))


def _check_plain_annotation_replay(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    run_id: str,
    annotation: str,
) -> bool:
    """Check exact run evidence before writes; clocks cannot prove membership."""
    receipt = conn.execute(
        "SELECT episode_id FROM thesis_evaluation_episode_check_receipts WHERE ticker=? AND run_id=?",
        (ticker, run_id),
    ).fetchone()
    if receipt is None:
        return False
    rows = conn.execute(
        "SELECT e.rule_evaluations_json FROM thesis_evaluation_episode_members m "
        "JOIN thesis_evaluations e ON e.id=m.evaluation_id "
        "WHERE m.episode_id=? AND e.run_id=?",
        (str(receipt[0]), run_id),
    ).fetchall()
    if not rows:
        rows = conn.execute(
            "SELECT e.rule_evaluations_json FROM thesis_evaluation_episode_members m "
            "JOIN thesis_evaluations e ON e.id=m.evaluation_id WHERE m.episode_id=?",
            (str(receipt[0]),),
        ).fetchall()
    retained = {_plain_annotation_json(str(row[0])) for row in rows}
    if retained != {annotation}:
        raise EpisodeIdempotencyConflictError(
            "ticker/run_id source annotation changed or cannot be proved from immutable episode members"
        )
    return True


def _needs_plain_evidence_anchor(
    conn: sqlite3.Connection,
    *,
    episode_id: str,
    annotation: str,
) -> bool:
    if annotation == "{}":
        return False
    latest = conn.execute(
        "SELECT e.rule_evaluations_json FROM thesis_evaluation_episode_members m "
        "JOIN thesis_evaluations e ON e.id=m.evaluation_id WHERE m.episode_id=? "
        "ORDER BY m.member_ordinal DESC LIMIT 1",
        (episode_id,),
    ).fetchone()
    return latest is None or _plain_annotation_json(str(latest[0])) != annotation


def _episode_soft_projection(
    verdict: ThesisVerdict,
) -> tuple[dict[str, JsonValue], ...] | None:
    if not verdict.soft_rule_results:
        return None
    return tuple(
        {
            "rule_name": result.rule_name,
            "status": result.status.value,
            "evidence": result.evidence,
            "details": (
                _semantic_evidence(_HOLDINGS_PAYLOAD_ADAPTER.validate_python(result.details))
                if _new_verdict_semantics(verdict)
                else cast("dict[str, JsonValue]", result.details)
            ),
        }
        for result in verdict.soft_rule_results
    )


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _episode_evidence_as_of(verdict: ThesisVerdict) -> datetime | None:
    observed: list[datetime] = [
        _aware_utc(observation.period_end)
        for evaluation in verdict.rule_evaluations
        for observation in evaluation.observations
    ]
    for result in verdict.soft_rule_results:
        raw = result.details.get("last_period")
        if not isinstance(raw, str):
            continue
        try:
            observed.append(_aware_utc(datetime.fromisoformat(raw)))
        except ValueError:
            continue
    return max(observed) if observed else None


def _insert_raw_evaluation(
    conn: sqlite3.Connection,
    verdict: ThesisVerdict,
    *,
    run_id: str | None,
) -> int:
    has_soft_col = any(
        row[1] == "soft_rule_results_json"
        for row in conn.execute("PRAGMA table_info(thesis_evaluations)").fetchall()
    )
    if has_soft_col:
        cursor = conn.execute(
            "INSERT INTO thesis_evaluations "
            "(ticker, evaluated_at, overall_status, rule_evaluations_json, "
            "soft_rule_results_json, run_id) VALUES (?, ?, ?, ?, ?, ?)",
            (
                verdict.ticker,
                verdict.evaluated_at,
                verdict.overall_status.value,
                _serialize_rule_evaluations(verdict),
                _serialize_soft_rule_results(verdict),
                run_id,
            ),
        )
    else:
        cursor = conn.execute(
            "INSERT INTO thesis_evaluations "
            "(ticker, evaluated_at, overall_status, rule_evaluations_json, run_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                verdict.ticker,
                verdict.evaluated_at,
                verdict.overall_status.value,
                _serialize_rule_evaluations(verdict),
                run_id,
            ),
        )
    if cursor.lastrowid is None:
        raise RuntimeError("thesis evaluation insert did not return an anchor row id")
    return int(cursor.lastrowid)


def refresh_thesis_mirror(
    conn: sqlite3.Connection,
    ticker: str,
    holdings_dir: Path,
    *,
    ingested_at: datetime | None = None,
) -> bool:
    """Re-sync the thesis_state *content mirror* from the on-disk holdings JSON.

    `thesis_state.raw_json` + `thesis` are a cache of
    `micro_thesis/holdings/<TICKER>.json`, first seeded by migration 0008. The
    file is the source of truth; this function makes the cache match it again.

    Ownership split (deliberate, to avoid two writers fighting over a column):
      * the evaluator (`persist_verdict`) owns ``breach_status`` + ``last_updated``
        — those are *evaluation* state, derived from kpi_facts, not file content;
      * this function owns ``thesis`` + ``raw_json`` + ``ingested_at`` — the
        *file* content and when it was last mirrored.

    Reads the file, upserts the three content columns, and returns True if the
    stored content actually differed (i.e. the row was stale/absent) so callers
    can report what they fixed. Comparison is on the *parsed* payload, not the
    raw string, because `json.dumps` separators/key-order are not stable across
    writers. Does NOT commit — the caller owns the transaction.

    Raises FileNotFoundError when the holdings file is missing, leaving any
    existing row untouched so a deleted/renamed file never silently blanks the
    mirror.
    """
    path = holdings_dir / f"{ticker.upper()}.json"
    if not path.exists():
        raise FileNotFoundError(f"Holdings spec not found: {path}")
    payload = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
    raw_json = json.dumps(payload)
    file_thesis = payload.get("thesis") if isinstance(payload.get("thesis"), str) else None

    prior = conn.execute(
        "SELECT thesis, raw_json FROM thesis_state WHERE ticker = ?", (ticker.upper(),)
    ).fetchone()
    drifted = True
    if prior is not None:
        stored: object
        try:
            stored = json.loads(prior["raw_json"]) if prior["raw_json"] else {}
        except json.JSONDecodeError:
            stored = None
        drifted = stored != payload or prior["thesis"] != file_thesis

    conn.execute(
        "INSERT INTO thesis_state (ticker, thesis, raw_json, ingested_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(ticker) DO UPDATE SET "
        "    thesis      = excluded.thesis, "
        "    raw_json    = excluded.raw_json, "
        "    ingested_at = excluded.ingested_at",
        (ticker.upper(), file_thesis, raw_json, ingested_at or datetime.now()),
    )
    return drifted


# Mirrors sync_thesis_state.py's ``_STUB_STATUS`` (kept as a plain literal here
# rather than imported — that module is a standalone CLI, not a library seam).
_STUB_REGENERATED_STATUS = "stub_regenerated_from_corruption"


def _is_corruption_stub(raw_json: object) -> bool:
    """True when a thesis_state row's mirrored content is only the
    corruption-recovery placeholder, never a real owner-underwritten thesis."""
    if not isinstance(raw_json, str) or not raw_json:
        return False
    try:
        parsed = json.loads(raw_json)
    except ValueError:
        return False
    if not isinstance(parsed, dict):
        return False
    status = cast("dict[str, object]", parsed).get("_status")
    return status == _STUB_REGENERATED_STATUS


def _persist_verdict_legacy(
    conn: sqlite3.Connection,
    verdict: ThesisVerdict,
    *,
    run_id: str | None = None,
    holdings_dir: Path | None = None,
    override: bool = False,
) -> None:
    """Update thesis_state.breach_status (current snapshot) AND append to thesis_evaluations (history).

    `thesis_state` is mutable (current-state row per ticker). `thesis_evaluations`
    is append-only — every evaluation produces a new row keyed by evaluated_at.

    When ``holdings_dir`` is supplied, the thesis_state *content mirror*
    (``thesis`` + ``raw_json``) is also re-synced from the on-disk holdings JSON
    via `refresh_thesis_mirror`, so an evaluation can never leave the mirror
    drifted from the file it was evaluated against. Omitting ``holdings_dir``
    preserves the historical behavior — breach_status/history only, raw_json
    left as-is — for callers that don't have the file at hand.

    Scored-miss gate (monthly_red_team.md Phase 3, PR7 — the NVO precedent): a
    thesis currently ``warn``/``breach`` whose text is being materially rewritten
    is a RE-UNDERWRITE. It is blocked with :class:`ReUnderwriteBlockedError`
    (naming the exact `execution/log_scored_miss.py` invocation to unblock it)
    unless ``override=True`` — which is honored but logged loudly, never a
    silent bypass. A ticker with no prior ``thesis_state`` row, one that is
    ``ok``/unset, or one whose stored content is only the corruption-recovery
    stub (``raw_json._status == "stub_regenerated_from_corruption"``, seeded by
    `sync_thesis_state.py`'s repair path) is never gated — a stub was never a
    real underwritten belief, so there is nothing to score a miss against. See
    ``thesis_reunderwrite_gate`` for the exact predicate.
    """
    prior = conn.execute(
        "SELECT thesis, breach_status, raw_json FROM thesis_state WHERE ticker = ?",
        (verdict.ticker,),
    ).fetchone()
    if prior is not None and not _is_corruption_stub(prior["raw_json"]):
        gate = evaluate_gate(
            conn,
            ticker=verdict.ticker,
            prior_thesis=prior["thesis"],
            prior_breach_status=prior["breach_status"],
            new_thesis=verdict.thesis,
        )
        if gate.is_reunderwrite:
            if gate.blocked and not override:
                raise ReUnderwriteBlockedError(verdict.ticker, onset=gate.onset)
            if gate.blocked and override:
                log.warning(
                    {
                        "event": "thesis_reunderwrite_gate_overridden",
                        "ticker": verdict.ticker,
                        "onset": gate.onset,
                        "run_id": run_id,
                    }
                )

    # Upsert: a thesis_state row may not exist yet (e.g. ticker added via raw SQL
    # bypassing the track_company → onboard_ticker → seed flow). New rows get an
    # empty raw_json placeholder; `holdings_dir` (when passed) re-mirrors it below.
    conn.execute(
        "INSERT INTO thesis_state "
        "(ticker, thesis, breach_status, last_updated, raw_json, ingested_at) "
        "VALUES (?, ?, ?, ?, '{}', ?) "
        "ON CONFLICT(ticker) DO UPDATE SET "
        "    breach_status = excluded.breach_status, "
        "    last_updated  = excluded.last_updated",
        (
            verdict.ticker,
            verdict.thesis,
            verdict.overall_status.value,
            verdict.evaluated_at,
            verdict.evaluated_at,  # ingested_at = evaluated_at for newly-created rows
        ),
    )
    # soft_rule_results_json is added by migration 0053. Older DBs without the
    # column will be missed by Alembic's auto-discovery only if someone bypasses
    # the migration path entirely — we detect that case and fall back to the
    # pre-0053 INSERT so the evaluator stays runnable on a stale schema.
    has_soft_col = any(
        row[1] == "soft_rule_results_json"
        for row in conn.execute("PRAGMA table_info(thesis_evaluations)").fetchall()
    )
    if has_soft_col:
        conn.execute(
            "INSERT INTO thesis_evaluations "
            "(ticker, evaluated_at, overall_status, rule_evaluations_json, "
            "soft_rule_results_json, run_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                verdict.ticker,
                verdict.evaluated_at,
                verdict.overall_status.value,
                _serialize_rule_evaluations(verdict),
                _serialize_soft_rule_results(verdict),
                run_id,
            ),
        )
    else:
        conn.execute(
            "INSERT INTO thesis_evaluations "
            "(ticker, evaluated_at, overall_status, rule_evaluations_json, run_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                verdict.ticker,
                verdict.evaluated_at,
                verdict.overall_status.value,
                _serialize_rule_evaluations(verdict),
                run_id,
            ),
        )
    # Keep the content mirror in lockstep with the file this verdict was built
    # from. A missing file (ticker tracked, thesis not yet authored) leaves the
    # placeholder mirror untouched — breach_status/history are already written.
    if holdings_dir is not None:
        with contextlib.suppress(FileNotFoundError):
            refresh_thesis_mirror(conn, verdict.ticker, holdings_dir)
            refresh_thesis_kpi_registration(
                conn,
                holdings_dir=holdings_dir,
                ticker=verdict.ticker,
                apply=True,
            )
    conn.commit()


def persist_verdict(
    conn: sqlite3.Connection,
    verdict: ThesisVerdict,
    *,
    run_id: str | None = None,
    holdings_dir: Path | None = None,
    override: bool = False,
) -> None:
    """Persist one current verdict and one owner-facing semantic episode.

    Versioned episode schemas retain one raw anchor for a materially distinct
    thesis/rules/evidence result, then record later identical scheduler checks
    as idempotent receipts. Pre-episode hand-built fixtures retain the legacy
    append-per-call behavior.
    """

    if holdings_dir is not None:
        with contextlib.suppress(FileNotFoundError):
            refresh_thesis_kpi_registration(
                conn,
                holdings_dir=holdings_dir,
                ticker=verdict.ticker,
                apply=False,
            )

    if not _episode_schema_active(conn):
        try:
            _persist_verdict_legacy(
                conn,
                verdict,
                run_id=run_id,
                holdings_dir=holdings_dir,
                override=override,
            )
        except Exception:
            conn.rollback()
            raise
        return
    if run_id is None:
        raise ValueError("run_id is required when semantic thesis episodes are active")
    if verdict.semantic_input is None:
        raise ValueError("semantic_input is required when semantic thesis episodes are active")

    if verdict.semantic_input.requires_retained_context:
        if verdict.retained_context is None:
            raise EpisodeStoreError("new thesis check requires retained context")
        replayed = replay_check_context(verdict.retained_context)
        if (
            replayed.semantic_input != verdict.semantic_input
            or _episode_rule_projection(replayed) != _episode_rule_projection(verdict)
            or _episode_soft_projection(replayed) != _episode_soft_projection(verdict)
            or replayed.overall_status != verdict.overall_status
            or _plain_annotation_json(_serialize_rule_evaluations(replayed))
            != _plain_annotation_json(_serialize_rule_evaluations(verdict))
        ):
            raise EpisodeNondeterminismError("verdict differs from retained deterministic context")
    annotation = _plain_annotation_json(_serialize_rule_evaluations(verdict))
    annotation_replayed = _check_plain_annotation_replay(
        conn, ticker=verdict.ticker, run_id=run_id, annotation=annotation
    )

    prior = conn.execute(
        "SELECT thesis, breach_status, raw_json FROM thesis_state WHERE ticker = ?",
        (verdict.ticker,),
    ).fetchone()
    if prior is not None and not _is_corruption_stub(prior["raw_json"]):
        gate = evaluate_gate(
            conn,
            ticker=verdict.ticker,
            prior_thesis=prior["thesis"],
            prior_breach_status=prior["breach_status"],
            new_thesis=verdict.thesis,
        )
        if gate.is_reunderwrite and gate.blocked and not override:
            raise ReUnderwriteBlockedError(verdict.ticker, onset=gate.onset)
        if gate.is_reunderwrite and gate.blocked and override:
            log.warning(
                {
                    "event": "thesis_reunderwrite_gate_overridden",
                    "ticker": verdict.ticker,
                    "onset": gate.onset,
                    "run_id": run_id,
                }
            )

    try:
        conn.execute(
            "INSERT INTO thesis_state "
            "(ticker, thesis, breach_status, last_updated, raw_json, ingested_at) "
            "VALUES (?, ?, ?, ?, '{}', ?) "
            "ON CONFLICT(ticker) DO UPDATE SET "
            "    breach_status = excluded.breach_status, "
            "    last_updated  = excluded.last_updated",
            (
                verdict.ticker,
                verdict.thesis,
                verdict.overall_status.value,
                verdict.evaluated_at,
                verdict.evaluated_at,
            ),
        )
        semantic = verdict.semantic_input
        severity = EpisodeSeverity(verdict.overall_status.value)
        episode_id = forward_episode_id(semantic=semantic, severity=severity)
        existing = conn.execute(
            "SELECT 1 FROM thesis_evaluation_episodes WHERE episode_id=?",
            (episode_id,),
        ).fetchone()
        retain_anchor = existing is None or (
            not annotation_replayed
            and _needs_plain_evidence_anchor(conn, episode_id=episode_id, annotation=annotation)
        )
        raw_id = _insert_raw_evaluation(conn, verdict, run_id=run_id) if retain_anchor else None
        episode_write = record_forward_episode(
            conn,
            semantic=semantic,
            check=EpisodeCheckInput(
                run_id=run_id,
                checked_at=_aware_utc(verdict.evaluated_at),
                evidence_as_of=_episode_evidence_as_of(verdict),
                severity=severity,
                provenance_completeness=ProvenanceCompleteness.PARTIAL,
                rule_evaluations=_episode_rule_projection(verdict),
                soft_rule_results=_episode_soft_projection(verdict),
                raw_evaluation_id=raw_id,
                retained_context=verdict.retained_context,
            ),
        )
        if episode_write.created:
            supersede_prior(
                conn,
                new_episode_id=episode_write.episode_id,
                superseded_at=_aware_utc(verdict.evaluated_at),
            )
            deliver_episode_alert(
                conn,
                episode_write.episode_id,
                delivered_at=_aware_utc(verdict.evaluated_at),
            )
        if holdings_dir is not None:
            with contextlib.suppress(FileNotFoundError):
                refresh_thesis_mirror(conn, verdict.ticker, holdings_dir)
                refresh_thesis_kpi_registration(
                    conn,
                    holdings_dir=holdings_dir,
                    ticker=verdict.ticker,
                    apply=True,
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
