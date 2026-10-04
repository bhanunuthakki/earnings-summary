"""Calculated thesis evidence can be repeated after its live inputs disappear."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import JsonValue

from compute.soft_rule_evaluator import (
    CapturedReplayError,
    SoftEvaluationCapture,
    SoftRule,
    SoftRuleStatus,
    evaluate_soft_rules,
    replay_soft_capture,
)
from compute.thesis_evaluation_episodes import (
    EpisodeCheckInput,
    EpisodeSeverity,
    EpisodeStoreError,
    ForwardSemanticInput,
    HardRuleCapture,
    ProvenanceCompleteness,
    RetainedThesisContext,
    read_check_context,
    record_forward_episode,
)
from compute.thesis_evaluator import evaluate_ticker_thesis, persist_verdict, replay_check_context
from compute.thesis_metric_series import (
    MetricEvaluationCapture,
    MetricExpression,
    MetricReplayError,
    calculate_metric_series,
    replay_metric_capture,
)
from models.kpis import BreachStatus
from tests.test_report_canonical_financials import seed_table
from tests.test_thesis_metric_integration import canonical_conn as canonical_conn


def _holdings(tmp_path: Path, *, unavailable: bool = False) -> Path:
    expression: dict[str, object] = (
        {"operation": "ttm_fcf_margin"}
        if unavailable
        else {
            "operation": "ttm_ratio",
            "numerator": {"operation": "level", "name": "operating_income"},
            "denominator": {"operation": "level", "name": "revenue"},
        }
    )
    holdings = tmp_path / "holdings"
    holdings.mkdir()
    (holdings / "SYNTH.json").write_text(
        json.dumps(
            {
                "ticker": "SYNTH",
                "thesis": "Original synthetic margin thesis",
                "business_model_rules": [
                    {
                        "rule_id": "margin",
                        "kpi_name": "calculated margin",
                        "comparator": "lt",
                        "threshold": "21",
                        "unit": "percent",
                        "consecutive_periods": 2,
                        "narrative": "Watch the margin",
                        "metric_expression": expression,
                        "require_adjacent_quarters": True,
                    }
                ],
                "break_rules_soft": [
                    {
                        "name": "margin_watch",
                        "predicate": {
                            "type": "metric_threshold",
                            "params": {
                                "expression": expression,
                                "comparator": "lt",
                                "threshold": "21",
                                "periods": 2,
                            },
                        },
                    }
                ],
            }
        )
    )
    return holdings


def _seed(conn: sqlite3.Connection) -> None:
    periods = [
        ("2022-01-01", "2022-03-31", "Q1"),
        ("2022-04-01", "2022-06-30", "Q2"),
        ("2022-07-01", "2022-09-30", "Q3"),
        ("2022-10-01", "2022-12-31", "Q4"),
        ("2023-01-01", "2023-03-31", "Q1"),
    ]
    seed_table(
        conn,
        [
            (name, start, end, quarter, value, "USD")
            for name, value in (("revenue", "100"), ("operating_income", "20"))
            for start, end, quarter in periods
        ],
    )


def test_calculated_check_persists_and_replays_without_database_or_holdings(
    canonical_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    _seed(canonical_conn)
    holdings = _holdings(tmp_path)
    verdict = evaluate_ticker_thesis(canonical_conn, ticker="SYNTH", holdings_dir=holdings)
    assert verdict.overall_status is BreachStatus.BREACH
    assert verdict.retained_context is not None
    assert verdict.retained_context.hard_inputs[0].metric_input is not None
    assert len(verdict.retained_context.hard_inputs[0].metric_input.reads) == 2
    assert len(verdict.retained_context.soft_inputs[0].metric_reads) == 1
    canonical_conn.execute(
        "INSERT INTO ingestion_runs(run_id,started_at,directive,ticker_scope,status) "
        "VALUES ('metric-context','2026-10-03T00:00:00+00:00','test','[\"SYNTH\"]','ok')"
    )
    persist_verdict(canonical_conn, verdict, run_id="metric-context", holdings_dir=holdings)
    receipt_id = canonical_conn.execute(
        "SELECT receipt_id FROM thesis_evaluation_episode_check_receipts"
    ).fetchone()[0]
    stored = read_check_context(canonical_conn, receipt_id=receipt_id)
    assert stored.context is not None
    context = RetainedThesisContext.model_validate_json(stored.context.model_dump_json())
    canonical_conn.close()
    (holdings / "SYNTH.json").unlink()
    replayed = replay_check_context(context)
    assert replayed.thesis == verdict.thesis
    assert replayed.semantic_input == verdict.semantic_input
    assert replayed.rule_evaluations == verdict.rule_evaluations
    assert replayed.soft_rule_results == verdict.soft_rule_results
    assert replayed.retained_context is not None
    assert replayed.retained_context.content_sha256 == context.content_sha256


@pytest.mark.parametrize("change", ["missing", "extra", "reorder", "leaf_value", "result_value"])
def test_metric_replay_refuses_changed_used_population_or_arithmetic(
    canonical_conn: sqlite3.Connection,
    tmp_path: Path,
    change: str,
) -> None:
    _seed(canonical_conn)
    verdict = evaluate_ticker_thesis(
        canonical_conn, ticker="SYNTH", holdings_dir=_holdings(tmp_path)
    )
    assert verdict.retained_context is not None
    context = verdict.retained_context
    hard = context.hard_inputs[0]
    capture = hard.metric_input
    assert capture is not None
    reads = capture.reads
    result = capture.result
    if change == "missing":
        reads = reads[:-1]
    elif change == "extra":
        reads = (*reads, reads[0])
    elif change == "reorder":
        reads = tuple(reversed(reads))
    elif change == "leaf_value":
        leaf_result = reads[0].result
        assert leaf_result is not None
        point = leaf_result.points[0].model_copy(update={"value": Decimal(900)})
        reads = (
            reads[0].model_copy(
                update={
                    "result": leaf_result.model_copy(
                        update={"points": (point, *leaf_result.points[1:])}
                    )
                }
            ),
            *reads[1:],
        )
    else:
        point = result.points[0].model_copy(update={"value": Decimal(900)})
        result = result.model_copy(update={"points": (point, *result.points[1:])})
    changed = capture.model_copy(update={"reads": reads, "result": result})
    with pytest.raises(MetricReplayError):
        replay_metric_capture(changed)
    with pytest.raises(EpisodeStoreError):
        replay_check_context(
            context.model_copy(
                update={
                    "hard_inputs": (hard.model_copy(update={"metric_input": changed}),),
                }
            )
        )
    soft = context.soft_inputs[0]
    with pytest.raises(CapturedReplayError):
        replay_soft_capture(soft.model_copy(update={"metric_reads": (changed,)}))


def test_missing_metric_sources_remain_unresolved_offline(
    canonical_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    holdings = _holdings(tmp_path, unavailable=True)
    verdict = evaluate_ticker_thesis(canonical_conn, ticker="SYNTH", holdings_dir=holdings)
    assert verdict.rule_evaluations[0].status is BreachStatus.UNRESOLVED
    assert verdict.retained_context is not None
    capture = verdict.retained_context.hard_inputs[0].metric_input
    assert capture is not None and capture.result.status == "unavailable"
    assert len(capture.reads) == 3
    canonical_conn.close()
    (holdings / "SYNTH.json").unlink()
    replayed = replay_check_context(verdict.retained_context)
    assert replayed.rule_evaluations == verdict.rule_evaluations
    assert replayed.soft_rule_results == verdict.soft_rule_results


def test_metric_capture_refuses_a_naive_saved_cutoff(
    canonical_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    _seed(canonical_conn)
    verdict = evaluate_ticker_thesis(
        canonical_conn, ticker="SYNTH", holdings_dir=_holdings(tmp_path)
    )
    assert verdict.retained_context is not None
    capture = verdict.retained_context.hard_inputs[0].metric_input
    assert capture is not None and capture.cutoff.tzinfo is UTC
    with pytest.raises(MetricReplayError, match="timezone"):
        replay_metric_capture(capture.model_copy(update={"cutoff": datetime(2026, 10, 3)}))


def test_source_schema_failure_is_retained_without_retry() -> None:
    conn = sqlite3.connect(":memory:")
    captures: list[MetricEvaluationCapture] = []
    result = calculate_metric_series(
        conn,
        "SYNTH",
        MetricExpression(operation="level", source="kpi", name="Missing"),
        cutoff=datetime(2026, 10, 3, tzinfo=UTC),
        captures=captures,
    )
    assert result.status == "unavailable"
    assert result.reason_code == "metric_source_schema_or_value_unavailable"
    assert len(captures) == 1
    assert len(captures[0].reads) == 1
    assert captures[0].reads[0].error_type == "OperationalError"
    conn.close()
    assert replay_metric_capture(captures[0]) == result


def test_old_calculated_v2_receipt_is_explicitly_unavailable_for_replay(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    semantic = ForwardSemanticInput(
        ticker="SYNTH",
        thesis_content_sha256="a" * 64,
        ruleset_version="holdings-break-rules/v2",
        evaluator_semantic_version="thesis-evaluator/v2",
        hard_rules=(),
        soft_rules=(),
        accepted_observations=(),
    )
    checked_at = datetime(2026, 10, 3, tzinfo=UTC)
    receipt_ids: list[str] = []
    old_rows: list[tuple[object, ...]] = []

    def seed(path: Path) -> None:
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.execute(
            "INSERT INTO ingestion_runs(run_id,started_at,directive,ticker_scope,status) "
            "VALUES ('old-metric-v2','2026-10-03T00:00:00+00:00','test','[]','ok')"
        )
        raw_cursor = connection.execute(
            "INSERT INTO thesis_evaluations(ticker,evaluated_at,overall_status,rule_evaluations_json,run_id) "
            "VALUES ('SYNTH','2026-10-03T00:00:00+00:00','ok','[]','old-metric-v2')"
        )
        assert raw_cursor.lastrowid is not None
        written = record_forward_episode(
            connection,
            semantic=semantic,
            check=EpisodeCheckInput(
                run_id="old-metric-v2",
                checked_at=checked_at,
                evidence_as_of=None,
                severity=EpisodeSeverity.OK,
                provenance_completeness=ProvenanceCompleteness.PARTIAL,
                rule_evaluations=(),
                raw_evaluation_id=raw_cursor.lastrowid,
            ),
        )
        receipt_ids.append(written.check_id)
        old_rows.extend(
            tuple(row)
            for row in connection.execute("SELECT * FROM thesis_evaluation_episode_check_receipts")
        )
        connection.commit()
        connection.close()

    database = migrated_db(
        tmp_path / "old-calculated.db",
        upgrade_from="0050_kpi_fact_supersedes_lookup_index",
        before_upgrade=seed,
    )
    canonical_conn = sqlite3.connect(database)
    canonical_conn.row_factory = sqlite3.Row
    assert [
        tuple(row)
        for row in canonical_conn.execute("SELECT * FROM thesis_evaluation_episode_check_receipts")
    ] == [(*row, None, None) for row in old_rows]
    saved = read_check_context(canonical_conn, receipt_id=receipt_ids[0])
    assert saved.status == "legacy_context_unavailable"
    assert saved.context is None
    assert tuple(
        canonical_conn.execute(
            "SELECT context_json,context_sha256 FROM thesis_evaluation_episode_check_receipts"
        ).fetchone()
    ) == (None, None)
    before = canonical_conn.total_changes
    with pytest.raises(EpisodeStoreError, match="requires retained context"):
        record_forward_episode(
            canonical_conn,
            semantic=semantic.model_copy(
                update={
                    "evaluator_semantic_version": "thesis-evaluator/v3",
                }
            ),
            check=EpisodeCheckInput(
                run_id="old-metric-v2",
                checked_at=checked_at,
                evidence_as_of=None,
                severity=EpisodeSeverity.OK,
                provenance_completeness=ProvenanceCompleteness.PARTIAL,
                rule_evaluations=(),
            ),
        )
    assert canonical_conn.total_changes == before

    canonical_conn.close()


def test_old_optional_capture_fields_do_not_change_serialized_bytes() -> None:
    soft: dict[str, JsonValue] = {
        "ticker": "SYNTH",
        "cutoff": "2026-10-03T00:00:00Z",
        "rules": [],
        "reads": [],
        "results": [],
        "unit_jump_ratio": 1000.0,
    }
    assert SoftEvaluationCapture.model_validate(soft).model_dump(mode="json") == soft
    hard: dict[str, JsonValue] = {
        "rule": {},
        "json_pointer": "/break_rules/0/kpi_name",
        "selected_definition": None,
        "disposition": "definition_unresolved",
        "selection_details": {},
        "observations": None,
        "selected_definition_content": None,
    }
    assert HardRuleCapture.model_validate(hard).model_dump(mode="json") == hard


def test_actual_release564_scalar_context_replays_after_new_rule_schema() -> None:
    fixture = Path(__file__).parent / "fixtures/thesis_check_context/release564_scalar.json"
    context = RetainedThesisContext.model_validate_json(fixture.read_bytes())
    original_rules = context.original_spec["business_model_rules"]
    assert isinstance(original_rules, list) and isinstance(original_rules[0], dict)
    assert "metric_expression" not in original_rules[0]
    replayed = replay_check_context(context)
    assert replayed.thesis == "Original synthetic scalar thesis"
    assert replayed.semantic_input == context.semantic
    assert replayed.rule_evaluations[0].status is BreachStatus.UNRESOLVED
    assert replayed.retained_context is not None
    assert replayed.retained_context.content_sha256 == context.content_sha256


def test_changed_v3_metric_source_is_rejected_before_any_persistence(
    canonical_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    _seed(canonical_conn)
    verdict = evaluate_ticker_thesis(
        canonical_conn, ticker="SYNTH", holdings_dir=_holdings(tmp_path)
    )
    assert verdict.retained_context is not None
    context = verdict.retained_context
    hard = context.hard_inputs[0]
    capture = hard.metric_input
    assert capture is not None
    leaf = capture.reads[0]
    assert leaf.result is not None
    point = leaf.result.points[0].model_copy(update={"value": Decimal(900)})
    changed_leaf = leaf.model_copy(
        update={
            "result": leaf.result.model_copy(update={"points": (point, *leaf.result.points[1:])})
        }
    )
    changed_capture = capture.model_copy(update={"reads": (changed_leaf, *capture.reads[1:])})
    changed_context = context.model_copy(
        update={
            "hard_inputs": (hard.model_copy(update={"metric_input": changed_capture}),),
        }
    )
    canonical_conn.execute(
        "INSERT INTO ingestion_runs(run_id,started_at,directive,ticker_scope,status) "
        "VALUES ('metric-tamper','2026-10-03T00:00:00+00:00','test','[]','ok')"
    )
    canonical_conn.commit()
    before = canonical_conn.total_changes
    with pytest.raises(EpisodeStoreError, match="calculated result differs"):
        persist_verdict(
            canonical_conn,
            replace(verdict, retained_context=changed_context),
            run_id="metric-tamper",
        )
    assert canonical_conn.total_changes == before
    for table in (
        "thesis_evaluations",
        "thesis_evaluation_episodes",
        "thesis_evaluation_episode_check_receipts",
        "thesis_state",
    ):
        assert canonical_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0  # nosec B608 -- fixed literal test table set


def test_actual_constructor_schema_denial_is_retained_without_retry() -> None:
    conn = sqlite3.connect(":memory:")

    def deny_pragma(
        action: int, _one: str | None, _two: str | None, _database: str | None, _trigger: str | None
    ) -> int:
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_PRAGMA else sqlite3.SQLITE_OK

    conn.set_authorizer(deny_pragma)
    captures: list[MetricEvaluationCapture] = []
    try:
        result = calculate_metric_series(
            conn,
            "SYNTH",
            MetricExpression(operation="level", name="revenue"),
            cutoff=datetime(2026, 10, 3, tzinfo=UTC),
            captures=captures,
        )
        assert result.status == "unavailable"
        assert result.reason_code == "metric_source_schema_or_value_unavailable"
        assert len(captures) == 1
        assert captures[0].reads == ()
    finally:
        conn.close()
    assert replay_metric_capture(captures[0]) == result


def _deny_pragma(
    action: int, _one: str | None, _two: str | None, _database: str | None, _trigger: str | None
) -> int:
    return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_PRAGMA else sqlite3.SQLITE_OK


@pytest.mark.parametrize(
    "predicate_type,params",
    [
        (
            "metric_threshold",
            {
                "expression": {"operation": "level", "name": "revenue"},
                "comparator": "lt",
                "threshold": "100",
                "periods": 1,
            },
        ),
        (
            "series_below",
            {"metric": "revenue", "source": "financial", "threshold": 100, "periods": 1},
        ),
    ],
)
def test_public_soft_reader_setup_failure_is_retained_and_replayed(
    canonical_conn: sqlite3.Connection, predicate_type: str, params: dict[str, JsonValue]
) -> None:
    rule = SoftRule.model_validate(
        {"name": "public_setup_failure", "predicate": {"type": predicate_type, "params": params}}
    )
    canonical_conn.set_authorizer(_deny_pragma)
    captures: list[SoftEvaluationCapture] = []
    try:
        results = evaluate_soft_rules("SYNTH", [rule], canonical_conn, captures=captures)
        assert len(results) == 1
        assert results[0].status is SoftRuleStatus.UNRESOLVED
        assert len(captures) == 1
        if predicate_type == "metric_threshold":
            assert len(captures[0].metric_reads) == 1
            assert captures[0].metric_reads[0].setup_error_type == "DatabaseError"
            assert captures[0].metric_reads[0].reads == ()
        else:
            assert len(captures[0].reads) == 1
            assert captures[0].reads[0].error_type == "DatabaseError"
    finally:
        canonical_conn.close()
    assert replay_soft_capture(captures[0]) == results


def test_public_thesis_retains_hard_and_soft_reader_setup_failure(
    canonical_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    holdings = _holdings(tmp_path)
    canonical_conn.set_authorizer(_deny_pragma)
    try:
        verdict = evaluate_ticker_thesis(canonical_conn, ticker="SYNTH", holdings_dir=holdings)
        assert verdict.retained_context is not None
        assert verdict.rule_evaluations[0].status is BreachStatus.UNRESOLVED
        assert verdict.soft_rule_results[0].status is SoftRuleStatus.UNRESOLVED
        context = verdict.retained_context
        assert context.hard_inputs[0].metric_input is not None
        assert context.hard_inputs[0].metric_input.setup_error_type == "DatabaseError"
        assert context.soft_inputs[0].metric_reads[0].setup_error_type == "DatabaseError"
    finally:
        canonical_conn.close()
    (holdings / "SYNTH.json").unlink()
    replayed = replay_check_context(context)
    assert replayed.rule_evaluations == verdict.rule_evaluations
    assert replayed.soft_rule_results == verdict.soft_rule_results
