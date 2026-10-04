"""Calculated thesis rules retain evidence and fail closed on unavailable periods."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Generator
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from compute import thesis_evaluator
from compute.soft_rule_evaluator import (
    SoftRule,
    SoftRuleResult,
    SoftRuleStatus,
    evaluate_soft_rules,
)
from compute.thesis_evaluation_episodes import ForwardSemanticInput
from compute.thesis_evaluator import (
    BreakRule,
    HoldingsSpec,
    KpiObservation,
    ThesisVerdict,
    evaluate_rule,
    evaluate_ticker_thesis,
    persist_verdict,
)
from models.facts import Unit
from models.kpis import BreachStatus
from tests import test_source_fact_repository as foundation
from tests.test_report_canonical_financials import seed_table


@pytest.fixture
def canonical_conn(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> Generator[sqlite3.Connection, None, None]:
    factory = cast(
        Callable[..., Generator[sqlite3.Connection, None, None]],
        getattr(foundation.conn, "__wrapped__"),
    )
    for database in factory(tmp_path, migrated_db):
        database.row_factory = sqlite3.Row
        yield database


def _rows(values: dict[str, list[str]]) -> list[tuple[str, str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str, str]] = []
    for name, series in values.items():
        for index, value in enumerate(series):
            year, quarter = 2022 + index // 4, index % 4 + 1
            month = quarter * 3
            day = 31 if month in (3, 12) else 30
            rows.append(
                (
                    name,
                    f"{year}-{month - 2:02d}-01",
                    f"{year}-{month:02d}-{day}",
                    f"Q{quarter}",
                    value,
                    "USD",
                )
            )
    return rows


def _level(name: str) -> dict[str, str]:
    return {"operation": "level", "source": "financial", "name": name}


def _rule(**updates: object) -> BreakRule:
    return BreakRule.model_validate(
        {
            "rule_id": "margin_floor",
            "kpi_name": "Calculated margin",
            "comparator": "lt",
            "threshold": "21",
            "unit": "percent",
            "consecutive_periods": 2,
            "narrative": "Watch margin.",
            **updates,
        }
    )


def _holdings(tmp_path: Path, rule: BreakRule) -> Path:
    holdings = tmp_path / "holdings"
    holdings.mkdir(exist_ok=True)
    (holdings / "SYNTH.json").write_text(
        json.dumps(
            {
                "ticker": "SYNTH",
                "thesis": "Synthetic owner thesis.",
                "business_model_rules": [rule.model_dump(mode="json")],
            }
        )
    )
    return holdings


def _soft(expression: dict[str, object], **updates: object) -> SoftRule:
    return SoftRule.model_validate(
        {
            "name": "metric_watch",
            "predicate": {
                "type": "metric_threshold",
                "params": {
                    "expression": expression,
                    "comparator": "lt",
                    "threshold": "21",
                    "periods": 2,
                    **updates,
                },
            },
        }
    )


def test_strict_quarters_reject_gap_without_changing_legacy() -> None:
    observations = [
        KpiObservation(datetime(2026, 6, 30), Decimal(10), Unit.PERCENT),
        KpiObservation(datetime(2025, 12, 31), Decimal(10), Unit.PERCENT),
    ]
    legacy = _rule()
    assert evaluate_rule(legacy, observations).status is BreachStatus.BREACH
    strict = _rule(require_adjacent_quarters=True)
    assert evaluate_rule(strict, observations).status is BreachStatus.UNRESOLVED


def test_strict_quarters_reject_annual_evidence() -> None:
    observations = [
        KpiObservation(datetime(2026, 6, 30), Decimal(10), Unit.PERCENT, fiscal_period_type="FY"),
        KpiObservation(datetime(2026, 3, 31), Decimal(10), Unit.PERCENT, fiscal_period_type="Q1"),
    ]
    assert (
        evaluate_rule(_rule(require_adjacent_quarters=True), observations).status
        is BreachStatus.UNRESOLVED
    )


def test_calculated_hard_rule_and_persisted_source_manifest(
    canonical_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    seed_table(canonical_conn, _rows({"revenue": ["100"] * 5, "operating_income": ["20"] * 5}))
    expression = {
        "operation": "ttm_ratio",
        "numerator": _level("operating_income"),
        "denominator": _level("revenue"),
    }
    holdings = _holdings(
        tmp_path, _rule(metric_expression=expression, require_adjacent_quarters=True)
    )
    verdict = evaluate_ticker_thesis(canonical_conn, ticker="SYNTH", holdings_dir=holdings)
    evaluation = verdict.rule_evaluations[0]
    assert evaluation.status is BreachStatus.BREACH
    assert [observation.value for observation in evaluation.observations] == [Decimal(20)] * 2
    canonical_conn.execute(
        "INSERT INTO ingestion_runs (run_id,started_at,directive,ticker_scope,status) "
        "VALUES ('metric-run','2026-10-03','test','[\"SYNTH\"]','ok')"
    )
    persist_verdict(canonical_conn, verdict, run_id="metric-run", holdings_dir=holdings)
    raw = canonical_conn.execute("SELECT rule_evaluations_json FROM thesis_evaluations").fetchone()[
        0
    ]
    persisted = json.loads(raw)[0]
    assert persisted["metric_expression"]["operation"] == "ttm_ratio"
    assert persisted["source_manifest"]["source_manifests"]
    assert "observation_id" in str(persisted["source_manifest"])
    assert persisted["require_adjacent_quarters"] is True


def test_expression_rule_missing_financials_is_unresolved(
    canonical_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    holdings = _holdings(tmp_path, _rule(metric_expression={"operation": "ttm_fcf_margin"}))
    verdict = evaluate_ticker_thesis(canonical_conn, ticker="SYNTH", holdings_dir=holdings)
    assert verdict.rule_evaluations[0].status is BreachStatus.UNRESOLVED


def test_hard_calculated_ratio_rejects_incomparable_currency(
    canonical_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    rows = _rows({"revenue": ["100"] * 5, "operating_income": ["20"] * 5})
    seed_table(canonical_conn, rows, currencies={index: "EUR" for index in range(5, 10)})
    expression = {
        "operation": "ttm_ratio",
        "numerator": _level("operating_income"),
        "denominator": _level("revenue"),
    }
    holdings = _holdings(tmp_path, _rule(metric_expression=expression))
    result = evaluate_ticker_thesis(canonical_conn, ticker="SYNTH", holdings_dir=holdings)
    evaluation = result.rule_evaluations[0]
    assert evaluation.status is BreachStatus.UNRESOLVED
    assert evaluation.source_manifest is not None
    assert evaluation.source_manifest["reason_code"] == "expression_input_coordinate_mismatch"


def test_soft_threshold_uses_decimal_and_keeps_manifest(canonical_conn: sqlite3.Connection) -> None:
    seed_table(canonical_conn, _rows({"revenue": ["13.000000000000000001"]}))
    rule = _soft(dict(_level("revenue")), comparator="gt", threshold="13", periods=1)
    result = evaluate_soft_rules("SYNTH", [rule], canonical_conn)[0]
    assert result.status is SoftRuleStatus.YELLOW
    assert result.details["source_manifest"]["source_manifests"]
    assert result.details["last_period"].startswith("2022-03-31")


def test_soft_calculation_unavailable_never_passes(canonical_conn: sqlite3.Connection) -> None:
    result = evaluate_soft_rules("SYNTH", [_soft({"operation": "ttm_fcf_margin"})], canonical_conn)[
        0
    ]
    assert result.status is SoftRuleStatus.UNRESOLVED
    assert result.details["source_manifest"]["status"] == "unavailable"


@pytest.mark.parametrize(
    ("comparator", "status"),
    [
        ("<", SoftRuleStatus.GREEN),
        ("<=", SoftRuleStatus.YELLOW),
        (">", SoftRuleStatus.GREEN),
        (">=", SoftRuleStatus.YELLOW),
    ],
)
def test_soft_comparator_symbols_preserve_exact_boundaries(
    canonical_conn: sqlite3.Connection,
    comparator: str,
    status: SoftRuleStatus,
) -> None:
    seed_table(canonical_conn, _rows({"revenue": ["13"]}))
    rule = _soft(dict(_level("revenue")), comparator=comparator, threshold="13", periods=1)
    assert evaluate_soft_rules("SYNTH", [rule], canonical_conn)[0].status is status


def test_compound_same_period_rejects_fired_stale_leg(canonical_conn: sqlite3.Connection) -> None:
    seed_table(canonical_conn, _rows({"revenue": ["10"] * 2, "operating_income": ["10"]}))
    children = [
        _soft(dict(_level(name)), periods=1).predicate.model_dump(mode="json")
        for name in ("revenue", "operating_income")
    ]
    rule = SoftRule.model_validate(
        {
            "name": "paired",
            "predicate": {
                "type": "compound",
                "params": {"op": "and", "predicates": children, "require_same_period": True},
            },
        }
    )
    result = evaluate_soft_rules("SYNTH", [rule], canonical_conn)[0]
    assert result.status is SoftRuleStatus.UNRESOLVED
    legacy = rule.model_copy(
        update={
            "predicate": rule.predicate.model_copy(
                update={"params": {"op": "and", "predicates": children}}
            )
        }
    )
    assert evaluate_soft_rules("SYNTH", [legacy], canonical_conn)[0].status is SoftRuleStatus.YELLOW


def test_rule_semantic_hash_changes_with_expression_and_adjacency(
    canonical_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    rule = _rule(metric_expression={"operation": "ttm_fcf_margin"})
    holdings = _holdings(tmp_path, rule)
    first = evaluate_ticker_thesis(canonical_conn, ticker="SYNTH", holdings_dir=holdings)
    _holdings(tmp_path, rule.model_copy(update={"require_adjacent_quarters": True}))
    second = evaluate_ticker_thesis(canonical_conn, ticker="SYNTH", holdings_dir=holdings)
    assert first.semantic_input is not None and second.semantic_input is not None
    assert first.semantic_input.ruleset_sha256 != second.semantic_input.ruleset_sha256
    assert first.semantic_input.evaluator_semantic_version == "thesis-evaluator/v3"


def test_legacy_semantic_dictionary_and_hash_keep_v1_baseline() -> None:
    rule = _rule()
    evaluation = evaluate_rule(
        rule, [KpiObservation(datetime(2026, 6, 30), Decimal(10), Unit.PERCENT)]
    )
    spec = HoldingsSpec(
        ticker="SYNTH", thesis="Synthetic owner thesis.", business_model_rules=[rule]
    )
    build_semantic_input = cast(
        Callable[..., ForwardSemanticInput], getattr(thesis_evaluator, "_build_semantic_input")
    )
    semantic = build_semantic_input(
        payload={"thesis": spec.thesis}, spec=spec, evaluations=[evaluation], soft_results=[]
    )
    assert semantic.canonical_payload() == {
        "fingerprint_policy_version": "forward_v1",
        "ticker": "SYNTH",
        # pragma: allowlist nextline secret -- fixed synthetic regression SHA-256
        "thesis_content_sha256": "372ede2541381b640ccac78ff832fd1e93bae3c1195dc87d069c23fe8c45fc1e",
        "rules": {
            "ruleset_version": "holdings-break-rules/v1",
            "hard_rules": [
                {
                    "rule_id": "margin_floor",
                    "definition": {
                        "tier": "business_model",
                        "kpi_name": "Calculated margin",
                        "comparator": "lt",
                        "threshold": "21",
                        "unit": "percent",
                        "consecutive_periods": 2,
                        "narrative": "Watch margin.",
                    },
                }
            ],
            "soft_rules": [],
        },
        "accepted_observations": [
            {
                "metric_identity": "hard:margin_floor:Calculated margin",
                "period_end": "2026-06-30T00:00:00",
                "observed_value": "10",
                "accepted_value": "10",
                "unit": "percent",
                "currency": None,
                "material_source_semantics": ["current-evaluator-selection"],
                "restatement_semantics": "source-provenance-not-retained",
            }
        ],
        "evaluator_semantic_version": "thesis-evaluator/v1",
    }
    assert (
        semantic.semantic_input_sha256
        # pragma: allowlist nextline secret -- fixed synthetic regression SHA-256
        == "d1b3aa45bea5b9e9836968d410682e2d3ca12f4165a2c56b26feaa528955e862"
    )
    soft = SoftRuleResult(
        "legacy", SoftRuleStatus.GREEN, "unchanged", {"cutoff": "retained"}, datetime(2026, 6, 30)
    )
    verdict = ThesisVerdict(
        ticker="SYNTH",
        thesis=spec.thesis,
        overall_status=evaluation.status,
        rule_evaluations=(evaluation,),
        evaluated_at=datetime(2026, 6, 30),
        soft_rule_results=(soft,),
        semantic_input=semantic,
    )
    rule_projection = cast(
        Callable[[ThesisVerdict], tuple[dict[str, object], ...]],
        getattr(thesis_evaluator, "_episode_rule_projection"),
    )
    soft_projection = cast(
        Callable[[ThesisVerdict], tuple[dict[str, object], ...] | None],
        getattr(thesis_evaluator, "_episode_soft_projection"),
    )
    projected = rule_projection(verdict)[0]
    assert set(projected) == {
        "rule_id",
        "kpi_name",
        "comparator",
        "threshold",
        "consecutive_periods",
        "tier",
        "status",
        "detail",
        "narrative",
        "observations",
    }
    assert soft_projection(verdict) == (
        {
            "rule_name": "legacy",
            "status": "green",
            "evidence": "unchanged",
            "details": {"cutoff": "retained"},
        },
    )
    legacy_soft = build_semantic_input(
        payload={"thesis": spec.thesis}, spec=spec, evaluations=[evaluation], soft_results=[soft]
    )
    assert legacy_soft.accepted_observations[-1].accepted_value == (
        # pragma: allowlist nextline secret -- fixed synthetic regression SHA-256
        "9974e1393601f2906b19349a616f3242016539ff6bd95ff4ed09b8fd48a67baa"
    )


def test_legacy_compound_keeps_previous_evidence_shape(canonical_conn: sqlite3.Connection) -> None:
    children = [
        {
            "type": "series_below",
            "params": {
                "metric": "revenue",
                "source": "financial",
                "threshold": "20",
                "periods": 1,
            },
        }
    ]
    rule = SoftRule.model_validate(
        {
            "name": "legacy_compound",
            "predicate": {
                "type": "compound",
                "params": {"op": "and", "predicates": children},
            },
        }
    )
    result = evaluate_soft_rules("SYNTH", [rule], canonical_conn)[0]
    assert set(result.details) == {"op", "children", "predicate_type", "fired"}
    assert "last_period" not in result.details["children"][0]


def test_legacy_persistence_rolls_back_registration_failure(
    canonical_conn: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holdings = _holdings(tmp_path, _rule())
    verdict = evaluate_ticker_thesis(canonical_conn, ticker="SYNTH", holdings_dir=holdings)
    canonical_conn.commit()
    before = canonical_conn.execute("SELECT COUNT(*) FROM thesis_evaluations").fetchone()[0]

    def legacy_schema(_conn: sqlite3.Connection) -> bool:
        return False

    monkeypatch.setattr("compute.thesis_evaluator._episode_schema_active", legacy_schema)

    def registration(_conn: sqlite3.Connection, **options: object) -> tuple[()]:
        if options["apply"] is True:
            raise ValueError("synthetic registration failure")
        return ()

    monkeypatch.setattr("compute.thesis_evaluator.refresh_thesis_kpi_registration", registration)
    with pytest.raises(ValueError, match="synthetic registration failure"):
        persist_verdict(canonical_conn, verdict, holdings_dir=holdings)
    assert canonical_conn.in_transaction is False
    assert canonical_conn.execute("SELECT COUNT(*) FROM thesis_evaluations").fetchone()[0] == before
    assert (
        canonical_conn.execute("SELECT COUNT(*) FROM thesis_state WHERE ticker='SYNTH'").fetchone()[
            0
        ]
        == 0
    )


def test_invalid_registry_configuration_is_rejected_before_persistence(
    canonical_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    holdings = _holdings(tmp_path, _rule())
    path = holdings / "SYNTH.json"
    payload = json.loads(path.read_text())
    payload["kpi_registry_candidates"] = [
        {"name": "valid"},
        {"name": "invalid", "unexpected": True},
    ]
    path.write_text(json.dumps(payload))
    verdict = evaluate_ticker_thesis(canonical_conn, ticker="SYNTH", holdings_dir=holdings)
    canonical_conn.commit()
    with pytest.raises(ValueError):
        persist_verdict(
            canonical_conn, verdict, run_id="invalid-registration", holdings_dir=holdings
        )
    assert canonical_conn.execute("SELECT COUNT(*) FROM thesis_evaluations").fetchone()[0] == 0
    assert (
        canonical_conn.execute(
            "SELECT COUNT(*) FROM user_kpi_registry WHERE ticker='SYNTH'"
        ).fetchone()[0]
        == 0
    )


def test_repeated_calculation_keeps_same_semantic_episode(
    canonical_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    seed_table(
        canonical_conn,
        _rows(
            {
                "revenue": ["100"] * 5,
                "operating_cash_flow": ["40"] * 5,
                "capital_expenditure": ["-10"] * 5,
            }
        ),
    )
    holdings = _holdings(tmp_path, _rule(metric_expression={"operation": "ttm_fcf_margin"}))
    first = evaluate_ticker_thesis(canonical_conn, ticker="SYNTH", holdings_dir=holdings)
    second = evaluate_ticker_thesis(canonical_conn, ticker="SYNTH", holdings_dir=holdings)
    assert first.semantic_input is not None and second.semantic_input is not None
    assert first.semantic_input.semantic_input_sha256 == second.semantic_input.semantic_input_sha256
    for index, verdict in enumerate((first, second)):
        run_id = f"repeat-metric-{index}"
        canonical_conn.execute(
            "INSERT INTO ingestion_runs (run_id,started_at,directive,ticker_scope,status) "
            "VALUES (?,'2026-10-03','test','[\"SYNTH\"]','ok')",
            (run_id,),
        )
        persist_verdict(canonical_conn, verdict, run_id=run_id, holdings_dir=holdings)
    assert canonical_conn.execute("SELECT COUNT(*) FROM thesis_evaluations").fetchone()[0] == 1
    assert (
        canonical_conn.execute(
            "SELECT COUNT(*) FROM thesis_evaluation_episode_check_receipts"
        ).fetchone()[0]
        == 2
    )
