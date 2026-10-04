"""Retained deterministic input is independent of current holdings and facts."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from compute.soft_rule_evaluator import (
    CapturedReplayError,
    SoftEvaluationCapture,
    SoftRule,
    evaluate_soft_rules,
    replay_soft_capture,
)
from compute.thesis_evaluation_episodes import (
    EpisodeIdempotencyConflictError,
    EpisodeStoreError,
    read_check_context,
)
from compute.thesis_evaluator import (
    ThesisVerdict,
    evaluate_ticker_thesis,
    persist_verdict,
    replay_check_context,
)
from provenance.fact_plane_v2 import FactDimensionV2
from sources.canonical_financial_series import (
    CanonicalFinancialObservation,
    CanonicalFinancialSeriesReader,
)
from tests import test_compute_soft_rule_evaluator as soft_test_helpers
from tests import test_compute_thesis_evaluator as hard_test_helpers
from tests.test_compute_soft_rule_evaluator import canonical_conn as canonical_conn

_create_schema = cast(
    Callable[[sqlite3.Connection], None], getattr(hard_test_helpers, "_create_schema")
)
_seed_kpi = cast(
    Callable[[sqlite3.Connection, str, str, list[tuple[str, float]]], None],
    getattr(hard_test_helpers, "_seed_kpi"),
)
_seed_financial = cast(
    Callable[[sqlite3.Connection, str, str, Sequence[float]], None],
    getattr(soft_test_helpers, "_seed_financial"),
)


def test_saved_check_reopens_original_thesis_offline(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = tmp_path / "context.db"
    migrated_db(database, target="head")
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    holdings = tmp_path / "holdings"
    holdings.mkdir()
    source = holdings / "ZZZ.json"
    source.write_text(json.dumps({"ticker": "ZZZ", "thesis": "Original thesis A"}))
    first = evaluate_ticker_thesis(connection, ticker="ZZZ", holdings_dir=holdings)
    connection.execute(
        "INSERT INTO ingestion_runs(run_id,started_at,directive,ticker_scope,status) "
        "VALUES ('context-A','2026-10-03T00:00:00+00:00','test','[]','ok')"
    )
    connection.commit()
    persist_verdict(connection, first, run_id="context-A")
    receipt_id = str(
        connection.execute(
            "SELECT receipt_id FROM thesis_evaluation_episode_check_receipts"
        ).fetchone()[0]
    )
    source.write_text(json.dumps({"ticker": "ZZZ", "thesis": "Changed thesis B"}))
    second = evaluate_ticker_thesis(connection, ticker="ZZZ", holdings_dir=holdings)
    connection.execute(
        "INSERT INTO ingestion_runs(run_id,started_at,directive,ticker_scope,status) VALUES ('context-B','2026-10-03T00:00:00+00:00','test','[]','ok')"
    )
    connection.commit()
    persist_verdict(connection, second, run_id="context-B")
    assert first.semantic_input != second.semantic_input
    assert connection.execute("SELECT COUNT(*) FROM thesis_evaluation_episodes").fetchone()[0] == 2
    saved = read_check_context(connection, receipt_id=receipt_id)
    connection.close()
    source.unlink()
    assert saved.context is not None
    assert saved.context.holdings_payload["thesis"] == "Original thesis A"
    replayed = replay_check_context(saved.context)
    assert replayed.thesis == first.thesis
    assert replayed.overall_status == first.overall_status
    assert replayed.semantic_input == first.semantic_input


def _write_holdings(
    directory: Path,
    *,
    ticker: str = "TEST",
    soft: list[dict[str, Any]] | None = None,
    hard: list[dict[str, Any]] | None = None,
) -> None:
    directory.mkdir(exist_ok=True)
    (directory / f"{ticker}.json").write_text(
        json.dumps(
            {
                "ticker": ticker,
                "thesis": "Durable economics",
                "break_rules": hard or [],
                "break_rules_soft": soft or [],
            }
        )
    )


def _rule(kind: str, params: dict[str, Any], name: str = "soft") -> dict[str, Any]:
    return {"name": name, "predicate": {"type": kind, "params": params}}


@pytest.mark.parametrize(
    "predicate",
    [
        _rule(
            "series_below",
            {"metric": "Metric", "source": "kpi", "threshold": 20.0000004, "periods": 2},
        ),
        _rule("series_above", {"metric": "Metric", "source": "kpi", "threshold": 3, "periods": 2}),
        _rule(
            "series_decel", {"metric": "Metric", "source": "kpi", "threshold_bps": 1, "periods": 2}
        ),
        _rule(
            "ratio_breach",
            {
                "numerator": {"name": "Metric", "source": "kpi"},
                "denominator": {"name": "Other", "source": "kpi"},
                "threshold": 5,
                "periods": 2,
                "direction": "below",
            },
        ),
        _rule(
            "trajectory",
            {"kpi_name": "Metric", "threshold": 10, "comparator": "lt", "lookback_prints": 4},
        ),
        _rule(
            "series_below",
            {
                "metric": "Metric",
                "source": "kpi",
                "derived": "delta",
                "threshold": 20,
                "periods": 1,
            },
        ),
        _rule(
            "compound",
            {
                "op": "or",
                "predicates": [
                    {
                        "type": "series_below",
                        "params": {
                            "metric": "Metric",
                            "source": "kpi",
                            "threshold": 20,
                            "periods": 2,
                        },
                    },
                    {
                        "type": "series_above",
                        "params": {
                            "metric": "Missing",
                            "source": "kpi",
                            "threshold": 20,
                            "periods": 2,
                        },
                    },
                ],
            },
        ),
    ],
)
def test_soft_predicates_replay_complete_used_populations(predicate: dict[str, Any]) -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    _create_schema(connection)
    values = [
        (
            f"{2024 + index // 4}-{(index % 4 + 1) * 3:02d}-{31 if index % 4 in (0, 3) else 30:02d}",
            value,
        )
        for index, value in enumerate([30, 20, 21, 22, 23, 24, 25, 20.0000003])
    ]
    _seed_kpi(connection, "TEST", "Metric", values)
    _seed_kpi(connection, "TEST", "Other", [(period, 2) for period, _ in values])
    captures: list[SoftEvaluationCapture] = []
    results = evaluate_soft_rules(
        "TEST", [SoftRule.model_validate(predicate)], connection, captures=captures
    )
    connection.close()
    assert len(captures) == 1
    assert len(captures[0].reads[0].series) == 8
    assert captures[0].reads[0].series[-1][1] == 20.0000003
    assert replay_soft_capture(captures[0]) == results
    with pytest.raises(CapturedReplayError, match="not captured"):
        replay_soft_capture(captures[0].model_copy(update={"reads": ()}))


def test_hard_raw_unit_and_soft_input_survive_current_input_mutation(tmp_path: Path) -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    _create_schema(connection)
    _seed_kpi(connection, "TEST", "Metric", [("2026-03-31", 1234.56789)])
    connection.execute("UPDATE kpi_facts SET unit='bps'")
    connection.commit()
    _write_holdings(
        tmp_path,
        hard=[
            {
                "rule_id": "floor",
                "kpi_name": "Metric",
                "comparator": "lt",
                "threshold": "13",
                "unit": "percent",
                "narrative": "Threshold uses percent",
            }
        ],
        soft=[
            _rule(
                "series_below",
                {"metric": "Metric", "source": "kpi", "threshold": 1300, "periods": 1},
            )
        ],
    )
    verdict = evaluate_ticker_thesis(connection, ticker="TEST", holdings_dir=tmp_path)
    context = verdict.retained_context
    assert context is not None
    assert context.hard_inputs[0].observations is not None
    assert context.hard_inputs[0].observations[0].unit == "bps"
    assert context.hard_inputs[0].observations[0].value == "1234.56789"
    assert str(verdict.rule_evaluations[0].observations[0].value) == "12.3456789"
    assert context.hard_inputs[0].observations[0].provenance["source_doc_id"] == 1
    connection.execute("UPDATE kpi_facts SET value=999999")
    connection.close()
    (tmp_path / "TEST.json").unlink()
    assert replay_check_context(context).rule_evaluations == verdict.rule_evaluations


def test_financial_provenance_and_clocks_do_not_create_v2_events(
    canonical_conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_financial(canonical_conn, "SYNTH", "Revenue", [100, 120, 140])
    holdings = tmp_path / "holdings"
    _write_holdings(
        holdings,
        ticker="SYNTH",
        soft=[_rule("series_below", {"metric": "Revenue", "threshold": 200, "periods": 2})],
    )
    first = evaluate_ticker_thesis(canonical_conn, ticker="SYNTH", holdings_dir=holdings)
    assert first.retained_context is not None
    original_read = CanonicalFinancialSeriesReader.read

    def changed_read(reader: CanonicalFinancialSeriesReader, metric: str, **kwargs: Any):
        series = original_read(reader, metric, **kwargs)
        observations: list[CanonicalFinancialObservation] = []
        for item in series.observations:
            # This fixture changes an existing source identity, never an absent one.
            assert item.document_version_id is not None
            observations.append(
                item.model_copy(
                    update={
                        "observation_id": item.observation_id + "-new-source",
                        "document_version_id": item.document_version_id + "-new-source",
                    }
                )
            )
        return series.model_copy(update={"observations": tuple(observations)})

    monkeypatch.setattr(CanonicalFinancialSeriesReader, "read", changed_read)
    second = evaluate_ticker_thesis(canonical_conn, ticker="SYNTH", holdings_dir=holdings)
    assert second.retained_context is not None
    assert first.semantic_input == second.semantic_input
    assert first.retained_context.content_sha256 != second.retained_context.content_sha256
    for run, verdict in (("source-A", first), ("source-B", second)):
        canonical_conn.execute(
            "INSERT INTO ingestion_runs(run_id,started_at,directive,ticker_scope,status) VALUES (?,?,'test','[]','ok')",
            (run, datetime.now(UTC).isoformat()),
        )
        canonical_conn.commit()
        persist_verdict(canonical_conn, verdict, run_id=run)
    assert (
        canonical_conn.execute(
            "SELECT COUNT(*) FROM thesis_evaluation_episodes WHERE ticker='SYNTH'"
        ).fetchone()[0]
        == 1
    )
    assert (
        canonical_conn.execute(
            "SELECT COUNT(*) FROM thesis_evaluation_episode_check_receipts WHERE ticker='SYNTH'"
        ).fetchone()[0]
        == 2
    )
    assert (
        canonical_conn.execute(
            "SELECT COUNT(*) FROM thesis_evaluations WHERE ticker='SYNTH'"
        ).fetchone()[0]
        == 1
    )
    assert (
        canonical_conn.execute("SELECT COUNT(*) FROM alerts WHERE ticker='SYNTH'").fetchone()[0]
        == 1
    )
    persist_verdict(canonical_conn, second, run_id="source-B")
    assert (
        canonical_conn.execute(
            "SELECT duplicate_run_count FROM thesis_evaluation_episodes WHERE ticker='SYNTH'"
        ).fetchone()[0]
        == 1
    )
    receipts = canonical_conn.execute(
        "SELECT receipt_id FROM thesis_evaluation_episode_check_receipts WHERE ticker='SYNTH' ORDER BY checked_at"
    ).fetchall()
    first_saved = read_check_context(canonical_conn, receipt_id=str(receipts[0][0]))
    second_saved = read_check_context(canonical_conn, receipt_id=str(receipts[1][0]))
    assert first_saved.context is not None and second_saved.context is not None

    def no_current_read(*args: Any, **kwargs: Any) -> None:
        pytest.fail("replay attempted a current source read")

    monkeypatch.setattr(CanonicalFinancialSeriesReader, "read", no_current_read)
    assert replay_check_context(first_saved.context).soft_rule_results == first.soft_rule_results
    assert replay_check_context(second_saved.context).soft_rule_results == second.soft_rule_results


def test_caught_read_failure_replays_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    _create_schema(connection)

    def fail_read(*args: Any, **kwargs: Any) -> None:
        raise ValueError("synthetic read failed")

    monkeypatch.setattr(CanonicalFinancialSeriesReader, "read", fail_read)
    captures: list[SoftEvaluationCapture] = []
    results = evaluate_soft_rules(
        "TEST",
        [
            SoftRule.model_validate(
                _rule("series_below", {"metric": "Revenue", "threshold": 2, "periods": 1})
            )
        ],
        connection,
        captures=captures,
    )
    connection.close()
    assert captures[0].reads[0].error_type == "ValueError"
    assert replay_soft_capture(captures[0]) == results


def test_definition_read_failure_is_retained_with_following_rule(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    _create_schema(connection)
    _seed_kpi(connection, "TEST", "Metric", [("2026-03-31", 10)])
    _seed_kpi(connection, "TEST", "Other", [("2026-03-31", 30)])
    _write_holdings(
        tmp_path,
        soft=[
            _rule(
                "series_below",
                {"metric": metric, "source": "kpi", "threshold": 20, "periods": 1},
                name=metric,
            )
            for metric in ("Metric", "Other")
        ],
    )
    deny_metadata = False
    denied = 0

    def trace(statement: str) -> None:
        nonlocal deny_metadata
        if "retained_semantic_context_json" in statement and "'Metric'" in statement:
            deny_metadata = True

    def authorize(
        action: int,
        table: str | None,
        column: str | None,
        database_name: str | None,
        trigger: str | None,
    ) -> int:
        nonlocal deny_metadata, denied
        del column, database_name, trigger
        if deny_metadata and action == sqlite3.SQLITE_READ and table == "kpi_definitions":
            deny_metadata = False
            denied += 1
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_trace_callback(trace)
    connection.set_authorizer(authorize)
    verdict = evaluate_ticker_thesis(connection, ticker="TEST", holdings_dir=tmp_path)
    connection.set_authorizer(None)
    connection.set_trace_callback(None)
    assert denied == 1
    assert verdict.soft_rule_results[0].status.value == "unresolved"
    assert verdict.soft_rule_results[1].status.value == "green"
    context = verdict.retained_context
    assert context is not None
    assert replay_check_context(context).soft_rule_results == verdict.soft_rule_results
    reads = context.soft_inputs[0].reads
    assert len(reads) == 2
    assert reads[0].series[0][1] == 10
    assert reads[0].rows and reads[0].selected_definition == "Metric"
    assert reads[0].error_type == "DatabaseError"
    assert reads[1].metric == "Other" and reads[1].error_message is None
    connection.close()
    database = tmp_path / "definition-failure.db"
    migrated_db(database, target="head")
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute(
        "INSERT INTO ingestion_runs(run_id,started_at,directive,ticker_scope,status) "
        "VALUES ('definition-failure','2026-10-03T00:00:00+00:00','test','[]','ok')"
    )
    connection.commit()
    persist_verdict(connection, verdict, run_id="definition-failure")
    receipt = connection.execute(
        "SELECT receipt_id FROM thesis_evaluation_episode_check_receipts "
        "WHERE run_id='definition-failure'"
    ).fetchone()
    saved = read_check_context(connection, receipt_id=str(receipt[0]))
    connection.close()
    (tmp_path / "TEST.json").unlink()
    assert saved.context is not None
    assert replay_check_context(saved.context).soft_rule_results == verdict.soft_rule_results


def _save_empty(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> tuple[sqlite3.Connection, ThesisVerdict, str]:
    database = tmp_path / "saved-context.db"
    migrated_db(database, target="head")
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    _write_holdings(tmp_path)
    verdict = evaluate_ticker_thesis(connection, ticker="TEST", holdings_dir=tmp_path)
    connection.execute(
        "INSERT INTO ingestion_runs(run_id,started_at,directive,ticker_scope,status) VALUES ('saved','2026-10-03T00:00:00+00:00','test','[]','ok')"
    )
    connection.commit()
    persist_verdict(connection, verdict, run_id="saved")
    receipt_id = str(
        connection.execute(
            "SELECT receipt_id FROM thesis_evaluation_episode_check_receipts"
        ).fetchone()[0]
    )
    return connection, verdict, receipt_id


@pytest.mark.parametrize("corruption", ["missing", "hash", "version", "identity", "output"])
def test_new_context_corruption_fails_explicitly(
    tmp_path: Path, migrated_db: Callable[..., Path], corruption: str
) -> None:
    connection, _, receipt_id = _save_empty(tmp_path, migrated_db)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute(
            "UPDATE thesis_evaluation_episode_check_receipts SET context_json=NULL WHERE receipt_id=?",
            (receipt_id,),
        )
    connection.execute("DROP TRIGGER trg_thesis_evaluation_episode_check_receipts_no_update")
    if corruption == "missing":
        connection.execute(
            "UPDATE thesis_evaluation_episode_check_receipts SET context_json=NULL,context_sha256=NULL"
        )
    elif corruption == "hash":
        connection.execute(
            "UPDATE thesis_evaluation_episode_check_receipts SET context_sha256=?", ("0" * 64,)
        )
    else:
        context = json.loads(
            str(
                connection.execute(
                    "SELECT context_json FROM thesis_evaluation_episode_check_receipts"
                ).fetchone()[0]
            )
        )
        if corruption == "version":
            context["format_version"] = "future/v999"
        elif corruption == "identity":
            context["ticker"] = "OTHER"
        else:
            context["severity"] = "warn"
        connection.execute(
            "UPDATE thesis_evaluation_episode_check_receipts SET context_json=?",
            (json.dumps(context),),
        )
    with pytest.raises(EpisodeStoreError):
        read_check_context(connection, receipt_id=receipt_id)
    connection.close()


def test_same_run_changed_retained_context_conflicts_without_side_effects(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    connection, first, _ = _save_empty(tmp_path, migrated_db)
    assert first.retained_context is not None
    payload = dict(first.retained_context.holdings_payload)
    payload["unused_note"] = "Changed full source context, identical economic projection"
    changed = replace(
        first,
        retained_context=first.retained_context.model_copy(update={"holdings_payload": payload}),
    )
    with pytest.raises(EpisodeIdempotencyConflictError):
        persist_verdict(connection, changed, run_id="saved")
    assert (
        connection.execute("SELECT duplicate_run_count FROM thesis_evaluation_episodes").fetchone()[
            0
        ]
        == 0
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM thesis_evaluation_episode_check_receipts"
        ).fetchone()[0]
        == 1
    )
    assert connection.execute("SELECT COUNT(*) FROM thesis_evaluations").fetchone()[0] == 1
    connection.close()


def test_invalid_new_context_rolls_back_episode_and_attention(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    connection, first, _ = _save_empty(tmp_path, migrated_db)
    assert first.retained_context is not None
    _write_holdings(
        tmp_path,
        hard=[
            {
                "rule_id": "missing",
                "kpi_name": "Missing",
                "comparator": "lt",
                "threshold": "1",
                "unit": "percent",
                "narrative": "Unresolved breaker",
            }
        ],
    )
    second = evaluate_ticker_thesis(connection, ticker="TEST", holdings_dir=tmp_path)
    connection.execute(
        "INSERT INTO ingestion_runs(run_id,started_at,directive,ticker_scope,status) VALUES ('invalid','2026-10-03T00:00:00+00:00','test','[]','ok')"
    )
    connection.commit()
    # A failure at the receipt insert occurs after episode/anchor/mirror mutation.
    connection.execute(
        "CREATE TRIGGER reject_context_receipt BEFORE INSERT ON thesis_evaluation_episode_check_receipts BEGIN SELECT RAISE(ABORT,'synthetic receipt failure'); END"
    )
    before_mirror = tuple(
        connection.execute(
            "SELECT thesis,breach_status FROM thesis_state WHERE ticker='TEST'"
        ).fetchone()
    )
    with pytest.raises(EpisodeIdempotencyConflictError):
        persist_verdict(connection, second, run_id="invalid")
    assert connection.execute("SELECT COUNT(*) FROM thesis_evaluation_episodes").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM thesis_evaluations").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0
    assert (
        tuple(
            connection.execute(
                "SELECT thesis,breach_status FROM thesis_state WHERE ticker='TEST'"
            ).fetchone()
        )
        == before_mirror
    )
    connection.close()


def test_old_forward_receipt_is_explicitly_unavailable(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    connection, first, _ = _save_empty(tmp_path, migrated_db)
    assert first.semantic_input is not None
    old_semantic = first.semantic_input.model_copy(
        update={"evaluator_semantic_version": "thesis-evaluator/v1"}
    )
    legacy = replace(first, semantic_input=old_semantic, retained_context=None)
    connection.execute(
        "INSERT INTO ingestion_runs(run_id,started_at,directive,ticker_scope,status) VALUES ('old-run','2026-10-03T00:00:00+00:00','test','[]','ok')"
    )
    connection.commit()
    persist_verdict(connection, legacy, run_id="old-run")
    row = connection.execute(
        "SELECT receipt_id,receipt_sha256 FROM thesis_evaluation_episode_check_receipts WHERE run_id='old-run'"
    ).fetchone()
    saved = read_check_context(connection, receipt_id=str(row[0]))
    assert saved.status == "legacy_context_unavailable" and saved.context is None
    assert (
        connection.execute(
            "SELECT receipt_sha256 FROM thesis_evaluation_episode_check_receipts WHERE run_id='old-run'"
        ).fetchone()[0]
        == row[1]
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM thesis_evaluation_episode_check_receipts"
        ).fetchone()[0]
        == 2
    )
    connection.close()


def test_blocked_report_rules_reconstruct_without_current_files(tmp_path: Path) -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    _create_schema(connection)
    holdings = tmp_path / "micro_thesis" / "holdings"
    holdings.mkdir(parents=True)
    _write_holdings(
        holdings,
        soft=[
            _rule(
                "series_below",
                {"metric": "Unverified metric", "source": "kpi", "threshold": 1, "periods": 1},
            )
        ],
    )
    verdict = evaluate_ticker_thesis(connection, ticker="TEST", holdings_dir=holdings)
    assert verdict.retained_context is not None
    assert verdict.soft_rule_results[0].details["reason"] == "unverified_report_kpi_reference"
    assert not verdict.retained_context.soft_inputs
    assert len(verdict.retained_context.blocked_soft_results) == 1
    connection.close()
    (holdings / "TEST.json").unlink()
    assert (
        replay_check_context(verdict.retained_context).soft_rule_results
        == verdict.soft_rule_results
    )


def test_additive_migration_preserves_old_records_and_append_only_guards(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = tmp_path / "prior.db"
    prior_rows: dict[str, list[tuple[Any, ...]]] = {}
    tables = (
        "thesis_evaluations",
        "thesis_evaluation_episodes",
        "thesis_evaluation_episode_members",
        "thesis_evaluation_episode_check_receipts",
    )

    def seed(path: Path) -> None:
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        _write_holdings(tmp_path)
        verdict = evaluate_ticker_thesis(connection, ticker="TEST", holdings_dir=tmp_path)
        assert verdict.semantic_input is not None
        legacy = replace(
            verdict,
            semantic_input=verdict.semantic_input.model_copy(
                update={"evaluator_semantic_version": "thesis-evaluator/v1"}
            ),
            retained_context=None,
        )
        connection.execute(
            "INSERT INTO ingestion_runs(run_id,started_at,directive,ticker_scope,status) VALUES ('historic','2026-10-03T00:00:00+00:00','test','[]','ok')"
        )
        connection.commit()
        persist_verdict(connection, legacy, run_id="historic")
        for table in tables:
            prior_rows[table] = [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]  # nosec B608 -- closed test table list
        connection.close()

    migrated_db(
        database,
        target="0051_thesis_check_context",
        upgrade_from="0048_metric_computation_output_observation",
        before_upgrade=seed,
    )
    connection = sqlite3.connect(database)
    assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
        "0051_thesis_check_context",
    )
    assert "ix_kpi_facts_supersedes_id" in {
        row[1] for row in connection.execute("PRAGMA index_list(kpi_facts)")
    }
    for table in tables:
        rows = [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]  # nosec B608 -- closed test table list
        expected = prior_rows[table]
        if table == "thesis_evaluation_episode_check_receipts":
            assert rows == [(*row, None, None) for row in expected]
        else:
            assert rows == expected
    receipt_id = str(prior_rows["thesis_evaluation_episode_check_receipts"][0][0])
    assert (
        read_check_context(connection, receipt_id=receipt_id).status == "legacy_context_unavailable"
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("DELETE FROM thesis_evaluation_episode_check_receipts")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("UPDATE thesis_evaluation_episode_check_receipts SET context_json='{}'")
    with pytest.raises(sqlite3.IntegrityError, match="context required"):
        connection.execute(
            "INSERT INTO thesis_evaluation_episode_check_receipts SELECT receipt_id,idempotency_key_sha256,episode_id,ticker,run_id,checked_at,outcome,semantic_input_sha256,result_sha256,receipt_sha256,'{}',NULL FROM thesis_evaluation_episode_check_receipts"
        )
    connection.close()


def test_material_kpi_semantics_changes_identity_while_source_ids_do_not(tmp_path: Path) -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    _create_schema(connection)
    connection.execute("ALTER TABLE kpi_fact_semantic_contexts ADD COLUMN accounting_basis TEXT")
    _seed_kpi(connection, "TEST", "Metric", [("2026-03-31", 10)])
    _write_holdings(
        tmp_path,
        soft=[
            _rule(
                "series_below", {"metric": "Metric", "source": "kpi", "threshold": 20, "periods": 1}
            )
        ],
    )
    first = evaluate_ticker_thesis(connection, ticker="TEST", holdings_dir=tmp_path)
    connection.execute("UPDATE kpi_facts SET source_doc_id=2")
    connection.commit()
    second = evaluate_ticker_thesis(connection, ticker="TEST", holdings_dir=tmp_path)
    assert first.semantic_input == second.semantic_input
    assert first.retained_context is not None and second.retained_context is not None
    assert first.retained_context.content_sha256 != second.retained_context.content_sha256
    connection.execute("UPDATE kpi_fact_semantic_contexts SET accounting_basis='non_gaap'")
    connection.commit()
    changed = evaluate_ticker_thesis(connection, ticker="TEST", holdings_dir=tmp_path)
    assert changed.semantic_input != second.semantic_input
    connection.close()


def test_financial_dimension_provenance_is_separate_from_economic_members(
    canonical_conn: sqlite3.Connection,
) -> None:
    _seed_financial(canonical_conn, "SYNTH", "Revenue", [100, 120])
    captures: list[SoftEvaluationCapture] = []
    evaluate_soft_rules(
        "SYNTH",
        [
            SoftRule.model_validate(
                _rule("series_below", {"metric": "Revenue", "threshold": 200, "periods": 1})
            )
        ],
        canonical_conn,
        captures=captures,
    )
    read = captures[0].reads[0]
    assert read.financial is not None
    dimension = FactDimensionV2(
        dimension_id="dimension-A",
        idempotency_key="dimension-key-A",
        axis_namespace="urn:synthetic",
        axis_name="Region",
        member_kind="explicit",
        explicit_member_namespace="urn:synthetic",
        explicit_member_name="Global",
        recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    financial = read.financial.model_copy(
        update={
            "observations": tuple(
                item.model_copy(update={"dimensions": (dimension,)})
                for item in read.financial.observations
            )
        }
    )
    first = read.model_copy(update={"financial": financial})
    other_dimension = dimension.model_copy(
        update={
            "dimension_id": "dimension-B",
            "idempotency_key": "dimension-key-B",
            "recorded_at": datetime(2026, 10, 3, tzinfo=UTC),
        }
    )
    second = read.model_copy(
        update={
            "financial": financial.model_copy(
                update={
                    "observations": tuple(
                        item.model_copy(update={"dimensions": (other_dimension,)})
                        for item in financial.observations
                    )
                }
            )
        }
    )
    assert first.economic_payload() == second.economic_payload()
    changed_member = dimension.model_copy(update={"explicit_member_name": "US"})
    changed = read.model_copy(
        update={
            "financial": financial.model_copy(
                update={
                    "observations": tuple(
                        item.model_copy(update={"dimensions": (changed_member,)})
                        for item in financial.observations
                    )
                }
            )
        }
    )
    assert first.economic_payload() != changed.economic_payload()
