"""Selected current KPI inputs survive existing artifact and episode owners."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from pydantic import JsonValue

import earnings_brief
import earnings_readout
from compute.thesis_evaluation_episodes import (
    EpisodeIdempotencyConflictError,
    EpisodeNondeterminismError,
    EpisodeStoreError,
    read_check_context,
)
from compute.thesis_evaluator import (
    ThesisVerdict,
    evaluate_ticker_thesis,
    persist_verdict,
    replay_check_context,
)
from llm_artifact_store import read_current
from pipeline.kpi_semantics import (
    KpiSemanticContext,
    current_kpi_semantic_context,
    persist_kpi_semantic_context,
)
from tests.test_research_cockpit import NOW
from tests.test_research_cockpit import conn as conn
from tests.test_research_cockpit import head_template as head_template


def _database_path(connection: sqlite3.Connection) -> Path:
    return Path(str(connection.execute("PRAGMA database_list").fetchone()[2]))


def _no_evidence(*_args: object, **_kwargs: object) -> str:
    return ""


def _selected_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT id,source_doc_id,value FROM kpi_facts WHERE ticker='NU' "
        "AND kpi_definition_id=(SELECT id FROM kpi_definitions "
        "WHERE ticker='NU' AND name='Monthly ARPAC (USD)') "
        "AND value IN (12.4,11.2) ORDER BY period_end DESC"
    ).fetchall()


def _use_actual_unit(conn: sqlite3.Connection) -> None:
    definition = conn.execute(
        "SELECT id FROM kpi_definitions WHERE ticker='NU' AND name='Monthly ARPAC (USD)'"
    ).fetchone()
    assert definition is not None
    conn.execute("UPDATE kpi_definitions SET unit='actual' WHERE id=?", (definition[0],))
    conn.execute("UPDATE kpi_facts SET unit='actual' WHERE kpi_definition_id=?", (definition[0],))
    # The reused cockpit fixture retains a legacy naive knowledge clock. Add
    # an explicit aware revision for this test's known synthetic metadata.
    # The original head remains immutable; later edits use the public owner.
    for fact in _selected_rows(conn):
        prior = conn.execute(
            "SELECT * FROM kpi_fact_semantic_contexts WHERE kpi_fact_id=? ORDER BY revision DESC LIMIT 1",
            (fact["id"],),
        ).fetchone()
        assert prior is not None
        KpiSemanticContext.model_validate(
            {
                "metric_name_as_reported": prior["metric_name_as_reported"],
                "reported_period_end": prior["reported_period_end"],
                "period_role": prior["period_role"],
                "publication_lane": prior["publication_lane"],
                "accounting_basis": prior["accounting_basis"],
                "consolidation_scope": prior["consolidation_scope"],
                "dimensions": json.loads(str(prior["dimensions_json"])),
                "unit_scale": prior["unit_scale"],
                "source_row_label": prior["source_row_label"],
                "source_column_header": prior["source_column_header"],
                "status": prior["status"],
                "reason_code": prior["reason_code"],
            }
        )
        conn.execute(
            "INSERT INTO kpi_fact_semantic_contexts(kpi_fact_id,revision,supersedes_context_id,"
            "metric_name_as_reported,reported_period_end,period_role,publication_lane,accounting_basis,"
            "consolidation_scope,dimensions_json,unit_scale,source_row_label,source_column_header,"
            "source_value_text,status,reason_code,reviewed_by,knowledge_at,kpi_definition_revision_id) "
            "SELECT kpi_fact_id,revision+1,id,metric_name_as_reported,reported_period_end,period_role,"
            "publication_lane,accounting_basis,consolidation_scope,dimensions_json,unit_scale,source_row_label,"
            "source_column_header,source_value_text,status,reason_code,'test_selected_input_retention',?,"
            "kpi_definition_revision_id FROM kpi_fact_semantic_contexts WHERE id=?",
            (NOW.isoformat(), prior["id"]),
        )
    conn.commit()


def _append_source_context(conn: sqlite3.Connection, label: str) -> None:
    fact_id = int(_selected_rows(conn)[0]["id"])
    prior = current_kpi_semantic_context(conn, kpi_fact_id=fact_id)
    assert prior is not None
    revision_id = persist_kpi_semantic_context(
        conn,
        kpi_fact_id=fact_id,
        context=prior.context.model_copy(update={"source_row_label": label}),
        reviewed_by="test_selected_input_retention",
        knowledge_at=NOW + timedelta(minutes=1),
    )
    assert revision_id is not None and revision_id != prior.id
    conn.commit()
    current = current_kpi_semantic_context(conn, kpi_fact_id=fact_id)
    assert current is not None and current.supersedes_context_id == prior.id


def _assert_pair_retained(source: dict[str, object], rows: list[sqlite3.Row]) -> None:
    inputs = source.get("selected_inputs")
    assert isinstance(inputs, list)
    encoded = json.dumps(inputs)
    for row in rows:
        assert f'"fact_id": {row["id"]}' in encoded
        assert f'"source_doc_id": {row["source_doc_id"]}' in encoded
        assert f'"original_value": "{row["value"]}"' in encoded
    assert source["identity_status"] == "partial"
    assert "canonical_observation_id" not in encoded


def _append_readout_target(conn: sqlite3.Connection) -> None:
    # One transcript version belongs to one immutable document. The second
    # fixture document already has its evidence ledger binding.
    conn.execute(
        "INSERT INTO transcripts(document_id,ticker,period_end,fiscal_period_type,has_qa_section) "
        "SELECT id,ticker,'2026-03-31','Q1',1 FROM documents WHERE ticker='NU' "
        "AND id NOT IN (SELECT document_id FROM transcripts) ORDER BY id LIMIT 1"
    )
    conn.commit()


def test_public_readout_retains_both_selected_kpi_inputs(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _append_readout_target(conn)
    prompts: list[str] = []

    def llm(prompt: str, **_kwargs: object) -> str:
        prompts.append(prompt)
        return "Readout from selected inputs."

    monkeypatch.setattr(earnings_readout, "call_llm", llm)
    database = _database_path(conn)
    outcome = earnings_readout.generate_for_ticker(database, tmp_path, "NU", today=NOW.date())
    assert outcome.status == earnings_readout.GENERATED
    artifact = read_current(
        ticker="NU", purpose=earnings_readout.PURPOSE, fiscal_period="2026-03-31", db_path=database
    )
    assert artifact is not None and isinstance(artifact.content_json, dict)
    blocks = cast(list[dict[str, object]], artifact.content_json["blocks"])
    block = next(item for item in blocks if item["kind"] == "tracked_kpi_moves")
    assert "12.4 usd" in str(block["content"])
    assert "11.2" in str(block["content"])
    assert str(block["content"]) in prompts[0]
    _assert_pair_retained(cast(dict[str, object], block["source"]), _selected_rows(conn))
    assert artifact.source_doc_ids is not None
    assert {int(row["source_doc_id"]) for row in _selected_rows(conn)} <= set(
        artifact.source_doc_ids
    )
    assert artifact.content_json["schema_version"] == "post_earnings_readout_context@2"
    assert artifact.content_json["grounding_status"] == "partial"


def test_public_brief_source_only_change_invalidates_existing_input_hash(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_actual_unit(conn)
    prompts: list[str] = []

    def llm(prompt: str, **_kwargs: object) -> str:
        prompts.append(prompt)
        return "Brief from selected inputs."

    monkeypatch.setattr(earnings_brief, "call_llm", llm)
    monkeypatch.setattr(earnings_brief, "_evidence_text", _no_evidence)
    database = _database_path(conn)
    candidate = earnings_brief.BriefCandidate("NU", NOW.date() + timedelta(days=1), 1)
    assert (
        earnings_brief.generate_brief(database, tmp_path, candidate, today=NOW.date())
        == "generated"
    )
    first = read_current(
        ticker="NU",
        purpose=earnings_brief.PURPOSE,
        fiscal_period=candidate.er_date.isoformat(),
        db_path=database,
    )
    assert first is not None and isinstance(first.content_json, dict)
    blocks = cast(list[dict[str, object]], first.content_json["blocks"])
    block = next(item for item in blocks if "Tracked tier-1 KPIs" in str(item["content"]))
    _assert_pair_retained(cast(dict[str, object], block["source"]), _selected_rows(conn))
    assert (
        earnings_brief.generate_brief(database, tmp_path, candidate, today=NOW.date())
        == "cache_hit"
    )
    _append_source_context(conn, "Exact original source row")
    assert (
        earnings_brief.generate_brief(database, tmp_path, candidate, today=NOW.date())
        == "generated"
    )
    second = read_current(
        ticker="NU",
        purpose=earnings_brief.PURPOSE,
        fiscal_period=candidate.er_date.isoformat(),
        db_path=database,
    )
    assert second is not None
    assert first.input_sha256 != second.input_sha256
    assert len(prompts) == 2 and prompts[0] == prompts[1]


def _plain_holdings(
    conn: sqlite3.Connection, tmp_path: Path, *, require_adjacent_quarters: bool = False
) -> Path:
    holdings = tmp_path / "holdings"
    holdings.mkdir()
    _use_actual_unit(conn)
    (holdings / "NU.json").write_text(
        json.dumps(
            {
                "ticker": "NU",
                "thesis": "Selected unit economics.",
                "break_rules": [
                    {
                        "rule_id": "arpac-below-20",
                        "kpi_name": "Monthly ARPAC (USD)",
                        "comparator": "lt",
                        "threshold": 20,
                        "unit": "actual",
                        "consecutive_periods": 1 if require_adjacent_quarters else 2,
                        "narrative": "ARPAC remains below 20.",
                        **(
                            {"require_adjacent_quarters": True} if require_adjacent_quarters else {}
                        ),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return holdings


def test_public_readout_source_only_change_invalidates_existing_input_hash(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_actual_unit(conn)
    _append_readout_target(conn)
    prompts: list[str] = []

    def llm(prompt: str, **_kwargs: object) -> str:
        prompts.append(prompt)
        return "Readout from selected inputs."

    monkeypatch.setattr(earnings_readout, "call_llm", llm)
    database = _database_path(conn)
    assert (
        earnings_readout.generate_for_ticker(database, tmp_path, "NU", today=NOW.date()).status
        == "generated"
    )
    first = read_current(
        ticker="NU", purpose=earnings_readout.PURPOSE, fiscal_period="2026-03-31", db_path=database
    )
    assert first is not None
    assert (
        earnings_readout.generate_for_ticker(database, tmp_path, "NU", today=NOW.date()).status
        == "cache_hit"
    )
    _append_source_context(conn, "Exact source row retained for readout")
    assert (
        earnings_readout.generate_for_ticker(database, tmp_path, "NU", today=NOW.date()).status
        == "generated"
    )
    second = read_current(
        ticker="NU", purpose=earnings_readout.PURPOSE, fiscal_period="2026-03-31", db_path=database
    )
    assert second is not None and first.input_sha256 != second.input_sha256
    assert len(prompts) == 2 and prompts[0] == prompts[1]
    assert isinstance(second.content_json, dict)
    blocks = cast(list[dict[str, object]], second.content_json["blocks"])
    source = next(block["source"] for block in blocks if block["kind"] == "tracked_kpi_moves")
    _assert_pair_retained(cast(dict[str, object], source), _selected_rows(conn))


def _historical_unannotated_verdict(verdict: ThesisVerdict) -> ThesisVerdict:
    """Construct the pre-annotation test input; never patch replay or admission.

    New main previously removed only the output annotation. With retained input
    references, its historical fixture must also omit that new optional input.
    The actual release564 immutable bytes are tested separately and unchanged.
    """
    context = verdict.retained_context
    assert context is not None
    historical = context.model_copy(
        update={
            "hard_inputs": tuple(
                capture.model_copy(
                    update={
                        "observations": None
                        if capture.observations is None
                        else tuple(
                            point.model_copy(update={"input_reference": None})
                            for point in capture.observations
                        )
                    }
                )
                for capture in context.hard_inputs
            )
        }
    )
    legacy = replace(
        verdict,
        rule_evaluations=tuple(
            replace(
                item,
                source_manifest=None,
                observations=tuple(
                    replace(point, input_reference=None) for point in item.observations
                ),
            )
            for item in verdict.rule_evaluations
        ),
        retained_context=historical,
    )
    replayed = replay_check_context(historical)
    assert replayed.rule_evaluations == legacy.rule_evaluations
    assert replayed.semantic_input == legacy.semantic_input
    return legacy


def test_adjacent_quarter_plain_annotation_preserves_existing_null_episode_projection(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    holdings = _plain_holdings(conn, tmp_path, require_adjacent_quarters=True)
    annotated = evaluate_ticker_thesis(conn, ticker="NU", holdings_dir=holdings)
    evaluation = annotated.rule_evaluations[0]
    assert evaluation.rule.require_adjacent_quarters and evaluation.source_manifest is not None
    # The reused current-view fixture includes its old 99 row. Preserve the
    # evaluator's existing selected value; this test changes only evidence.
    assert str(evaluation.observations[0].value) == "99"
    assert evaluation.status.value == "ok"
    assert evaluation.observations[0].input_reference is not None
    assert evaluation.observations[0].input_reference.original_value == "99"
    legacy = _historical_unannotated_verdict(annotated)
    before: tuple[str, str] | None = None
    for run, verdict in (("adjacent-legacy", legacy), ("adjacent-annotated", annotated)):
        conn.execute(
            "INSERT INTO ingestion_runs(run_id,started_at,directive,ticker_scope,status) "
            "VALUES (?,?,'test_selected_input_retention','[\"NU\"]','ok')",
            (run, NOW.isoformat()),
        )
        conn.commit()
        persist_verdict(conn, verdict, run_id=run)
        if run == "adjacent-legacy":
            stored = conn.execute(
                "SELECT rule_evaluations_json,result_sha256 FROM thesis_evaluation_episodes WHERE ticker='NU'"
            ).fetchone()
            assert stored is not None
            before = (str(stored[0]), str(stored[1]))
    episode = conn.execute(
        "SELECT rule_evaluations_json,result_sha256 FROM thesis_evaluation_episodes WHERE ticker='NU'"
    ).fetchone()
    assert episode is not None
    assert (str(episode[0]), str(episode[1])) == before
    projection = json.loads(str(episode["rule_evaluations_json"]))[0]
    assert projection["metric_expression"] is None
    assert projection["require_adjacent_quarters"] is True
    assert "source_manifest" in projection and projection["source_manifest"] is None
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM thesis_evaluation_episodes WHERE ticker='NU'"
        ).fetchone()[0]
        == 1
    )
    members = conn.execute(
        "SELECT e.rule_evaluations_json FROM thesis_evaluation_episode_members m "
        "JOIN thesis_evaluations e ON e.id=m.evaluation_id ORDER BY m.member_ordinal"
    ).fetchall()
    assert len(members) == 2
    assert json.loads(str(members[0][0])) == json.loads(str(episode["rule_evaluations_json"]))
    assert json.loads(str(members[0][0]))[0]["source_manifest"] is None
    assert json.loads(str(members[1][0]))[0]["source_manifest"] == evaluation.source_manifest


@pytest.mark.parametrize(
    "lookalike",
    [
        {"schema_version": "kpi_selected_inputs@1"},
        {"schema_version": "kpi_selected_inputs@1", "inputs": []},
        {"schema_version": "kpi_selected_inputs@1", "inputs": [{"reconciled_value": "12.4"}]},
    ],
)
def test_malformed_plain_manifest_label_cannot_bypass_episode_result_checks(
    conn: sqlite3.Connection, tmp_path: Path, lookalike: dict[str, JsonValue]
) -> None:
    holdings = _plain_holdings(conn, tmp_path)
    annotated = evaluate_ticker_thesis(conn, ticker="NU", holdings_dir=holdings)
    evaluation = annotated.rule_evaluations[0]
    legacy = _historical_unannotated_verdict(annotated)
    malformed = replace(
        annotated, rule_evaluations=(replace(evaluation, source_manifest=lookalike),)
    )
    for run in ("malformed-legacy", "malformed-attempt"):
        conn.execute(
            "INSERT INTO ingestion_runs(run_id,started_at,directive,ticker_scope,status) "
            "VALUES (?,?,'test_selected_input_retention','[\"NU\"]','ok')",
            (run, NOW.isoformat()),
        )
    conn.commit()
    persist_verdict(conn, legacy, run_id="malformed-legacy")
    before = conn.execute(
        "SELECT semantic_input_sha256,result_sha256 FROM thesis_evaluation_episodes WHERE ticker='NU'"
    ).fetchone()
    with pytest.raises(EpisodeNondeterminismError):
        persist_verdict(conn, malformed, run_id="malformed-attempt")
    after = conn.execute(
        "SELECT semantic_input_sha256,result_sha256 FROM thesis_evaluation_episodes WHERE ticker='NU'"
    ).fetchall()
    assert len(after) == 1 and tuple(after[0]) == tuple(before)
    assert conn.execute("SELECT COUNT(*) FROM thesis_evaluation_episode_members").fetchone()[0] == 1


def test_plain_annotation_replay_retains_new_evidence_in_same_episode(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    holdings = _plain_holdings(conn, tmp_path)
    annotated = evaluate_ticker_thesis(conn, ticker="NU", holdings_dir=holdings)
    assert annotated.rule_evaluations[0].source_manifest is not None
    legacy = _historical_unannotated_verdict(annotated)
    first_at = datetime(2026, 10, 3, 12, tzinfo=UTC)

    def persist(value: ThesisVerdict, ordinal: int) -> None:
        # Each scheduler run owns a distinct check receipt. Source annotations
        # must not change the episode, result hash or inbox carrier.
        run = f"plain-source-retention-{ordinal}"
        stamp = first_at + timedelta(minutes=ordinal)
        conn.execute(
            "INSERT INTO ingestion_runs(run_id,started_at,directive,ticker_scope,status) "
            "VALUES (?,?,'test_selected_input_retention','[\"NU\"]','ok')",
            (run, stamp.isoformat()),
        )
        conn.commit()
        persist_verdict(conn, replace(value, evaluated_at=stamp), run_id=run)

    persist(legacy, 1)
    before = conn.execute(
        "SELECT episode_id,semantic_input_sha256,result_sha256 FROM thesis_evaluation_episodes WHERE ticker='NU'"
    ).fetchone()
    with pytest.raises(EpisodeIdempotencyConflictError):
        persist_verdict(
            conn,
            replace(annotated, evaluated_at=first_at + timedelta(minutes=1)),
            run_id="plain-source-retention-1",
        )
    persist(annotated, 2)
    with pytest.raises(EpisodeIdempotencyConflictError):
        persist_verdict(
            conn,
            replace(legacy, evaluated_at=first_at + timedelta(minutes=2)),
            run_id="plain-source-retention-2",
        )
    persist(annotated, 3)
    _append_source_context(conn, "More exact source row")
    changed = evaluate_ticker_thesis(conn, ticker="NU", holdings_dir=holdings)
    assert changed.semantic_input == annotated.semantic_input
    with pytest.raises(EpisodeIdempotencyConflictError):
        persist_verdict(
            conn,
            replace(changed, evaluated_at=first_at + timedelta(minutes=3)),
            run_id="plain-source-retention-3",
        )
    persist(changed, 4)
    persist_verdict(
        conn,
        replace(changed, evaluated_at=first_at + timedelta(minutes=4)),
        run_id="plain-source-retention-4",
    )
    after = conn.execute(
        "SELECT episode_id,semantic_input_sha256,result_sha256 FROM thesis_evaluation_episodes WHERE ticker='NU'"
    ).fetchall()
    assert len(after) == 1 and tuple(after[0]) == tuple(before)
    rows = conn.execute(
        "SELECT e.rule_evaluations_json FROM thesis_evaluations e "
        "JOIN thesis_evaluation_episode_members m ON m.evaluation_id=e.id "
        "WHERE m.episode_id=? ORDER BY m.member_ordinal",
        (before["episode_id"],),
    ).fetchall()
    assert len(rows) == 3  # legacy anchor, first annotation, exact metadata change
    assert "source_manifest" not in json.loads(str(rows[0][0]))[0]
    assert (
        json.loads(str(rows[1][0]))[0]["source_manifest"]
        == annotated.rule_evaluations[0].source_manifest
    )
    assert (
        json.loads(str(rows[2][0]))[0]["source_manifest"]
        == changed.rule_evaluations[0].source_manifest
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM thesis_evaluation_episode_check_receipts WHERE ticker='NU'"
        ).fetchone()[0]
        == 4
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE thesis_evaluation_episode_id=?",
            (before["episode_id"],),
        ).fetchone()[0]
        == 1
    )


@pytest.mark.parametrize("changed_clock", ["later", "equal", "backdated"])
def test_plain_annotation_no_member_receipt_rejects_ambiguous_history(
    conn: sqlite3.Connection, tmp_path: Path, changed_clock: str
) -> None:
    holdings = _plain_holdings(conn, tmp_path)
    first = evaluate_ticker_thesis(conn, ticker="NU", holdings_dir=holdings)
    assert first.rule_evaluations[0].source_manifest is not None
    stamps = {
        "direct": datetime(2026, 10, 3, 12, tzinfo=UTC),
        "deduplicated": datetime(2026, 10, 3, 12, 1, tzinfo=UTC),
        "changed": {
            "later": datetime(2026, 10, 3, 12, 2, tzinfo=UTC),
            "equal": datetime(2026, 10, 3, 12, 1, tzinfo=UTC),
            "backdated": datetime(2026, 10, 3, 11, 59, tzinfo=UTC),
        }[changed_clock],
    }

    def persist(verdict: ThesisVerdict, run: str) -> None:
        conn.execute(
            "INSERT INTO ingestion_runs(run_id,started_at,directive,ticker_scope,status) "
            "VALUES (?,?,'test_selected_input_retention','[\"NU\"]','ok')",
            (run, stamps[run].isoformat()),
        )
        conn.commit()
        persist_verdict(conn, replace(verdict, evaluated_at=stamps[run]), run_id=run)

    persist(first, "direct")
    persist(first, "deduplicated")
    # No direct raw member, but every member currently proves the same source
    # annotation. An ordinary exact replay remains valid.
    persist_verdict(
        conn, replace(first, evaluated_at=stamps["deduplicated"]), run_id="deduplicated"
    )
    _append_source_context(conn, "Different exact source row")
    changed = evaluate_ticker_thesis(conn, ticker="NU", holdings_dir=holdings)
    assert changed.semantic_input == first.semantic_input
    persist(changed, "changed")
    before = conn.execute(
        "SELECT COUNT(*) FROM thesis_evaluations WHERE run_id IS NOT NULL"
    ).fetchone()[0]
    with pytest.raises(EpisodeIdempotencyConflictError):
        persist_verdict(
            conn, replace(first, evaluated_at=stamps["deduplicated"]), run_id="deduplicated"
        )
    # The first run has its own immutable raw member, so later source changes
    # and their clock order do not prevent exact replay of that known input.
    persist_verdict(conn, replace(first, evaluated_at=stamps["direct"]), run_id="direct")
    assert (
        conn.execute("SELECT COUNT(*) FROM thesis_evaluations WHERE run_id IS NOT NULL").fetchone()[
            0
        ]
        == before
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM thesis_evaluation_episodes WHERE ticker='NU'"
        ).fetchone()[0]
        == 1
    )


def _context_run(conn: sqlite3.Connection, run: str) -> None:
    conn.execute(
        "INSERT INTO ingestion_runs(run_id,started_at,directive,ticker_scope,status) "
        "VALUES (?,?,'test_plain_input_context','[\"NU\"]','ok')",
        (run, NOW.isoformat()),
    )
    conn.commit()


def test_plain_selected_references_replay_from_saved_context_offline(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    holdings = _plain_holdings(conn, tmp_path)
    verdict = evaluate_ticker_thesis(conn, ticker="NU", holdings_dir=holdings)
    manifest = verdict.rule_evaluations[0].source_manifest
    assert manifest is not None
    context = verdict.retained_context
    assert context is not None and context.hard_inputs[0].observations is not None
    references = tuple(point.input_reference for point in context.hard_inputs[0].observations)
    assert all(reference is not None for reference in references)
    for reference in references:
        assert reference is not None
        assert reference.reader_policy == "current_projection"
        assert reference.identity_status == "partial"
        assert "immutable_observation_version" in reference.missing_source_identities
    _context_run(conn, "plain-context-offline")
    persist_verdict(conn, verdict, run_id="plain-context-offline")
    receipt = conn.execute(
        "SELECT receipt_id FROM thesis_evaluation_episode_check_receipts "
        "WHERE ticker='NU' AND run_id='plain-context-offline'"
    ).fetchone()
    assert receipt is not None
    saved = read_check_context(conn, receipt_id=str(receipt[0]))
    assert saved.context is not None
    raw = saved.context.model_dump_json()
    restored = type(saved.context).model_validate_json(raw)
    assert restored.content_sha256 == saved.context.content_sha256
    conn.execute("UPDATE kpi_facts SET value=999999 WHERE ticker='NU'")
    conn.close()
    (holdings / "NU.json").unlink()
    replayed = replay_check_context(restored)
    assert replayed.rule_evaluations[0].source_manifest == manifest
    assert replayed.rule_evaluations[0].observations == verdict.rule_evaluations[0].observations
    assert replayed.semantic_input == verdict.semantic_input


def test_plain_annotation_tamper_refuses_before_any_verdict_write(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    holdings = _plain_holdings(conn, tmp_path)
    verdict = evaluate_ticker_thesis(conn, ticker="NU", holdings_dir=holdings)
    evaluation = verdict.rule_evaluations[0]
    assert evaluation.source_manifest is not None
    changed = json.loads(json.dumps(evaluation.source_manifest))
    changed["inputs"][0]["input"]["source_row_label"] = "Uncaptured source label"
    tampered = replace(verdict, rule_evaluations=(replace(evaluation, source_manifest=changed),))
    _context_run(conn, "plain-context-tamper")
    before = conn.total_changes
    with pytest.raises(EpisodeStoreError, match="retained deterministic context"):
        persist_verdict(conn, tampered, run_id="plain-context-tamper")
    assert conn.total_changes == before
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM thesis_evaluation_episode_check_receipts WHERE ticker='NU'"
        ).fetchone()[0]
        == 0
    )


def test_plain_source_change_retains_distinct_context_without_new_economic_episode(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    holdings = _plain_holdings(conn, tmp_path)
    first = evaluate_ticker_thesis(conn, ticker="NU", holdings_dir=holdings)
    assert first.rule_evaluations[0].source_manifest is not None
    _append_source_context(conn, "More precise source label")
    second = evaluate_ticker_thesis(conn, ticker="NU", holdings_dir=holdings)
    assert first.semantic_input == second.semantic_input
    assert first.retained_context is not None and second.retained_context is not None
    assert first.retained_context.content_sha256 != second.retained_context.content_sha256
    assert first.rule_evaluations[0].source_manifest != second.rule_evaluations[0].source_manifest
    for run, value in (("plain-context-first", first), ("plain-context-second", second)):
        _context_run(conn, run)
        persist_verdict(conn, value, run_id=run)
        persist_verdict(conn, value, run_id=run)
    with pytest.raises(EpisodeIdempotencyConflictError, match="source annotation changed"):
        persist_verdict(conn, second, run_id="plain-context-first")
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM thesis_evaluation_episodes WHERE ticker='NU'"
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM thesis_evaluation_episode_check_receipts WHERE ticker='NU'"
        ).fetchone()[0]
        == 2
    )
    assert conn.execute("SELECT COUNT(*) FROM thesis_evaluation_episode_members").fetchone()[0] == 2
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE ticker='NU' "
            "AND thesis_evaluation_episode_id IS NOT NULL"
        ).fetchone()[0]
        == 1
    )
