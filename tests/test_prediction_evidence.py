"""Prediction grading uses exact admitted identities and explicit database authority."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import predictions_store
from db_paths import db_path_context, resolve_db_path
from execution import grade_predictions
from llm.calibration import CalibrationScore
from pipeline.kpi_definition_revisions import persist_kpi_definition_revision
from pipeline.kpi_semantics import persist_kpi_semantic_context
from tests.fixtures.kpi_revision_setup import (
    NOW,
    definition_fixture,
    fact_fixture,
    revision_database,
    semantic_fixture,
)
from tests.fixtures.prediction_grading import (
    PREDICTIONS_SCHEMA,
    admit_prediction_facts,
    prediction_database,
)


def test_checkout_database_is_rejected_even_when_present(tmp_path: Path) -> None:
    path = tmp_path / "data" / "portfolio.db"
    path.parent.mkdir()
    path.touch()
    with pytest.raises(RuntimeError, match="checkout"):
        grade_predictions.grade_pending(tmp_path)


def test_absent_prediction_is_not_reported_written(tmp_path: Path) -> None:
    path = tmp_path / "explicit.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE predictions(id INTEGER PRIMARY KEY, outcome TEXT, "
            "realized_value REAL, realized_doc_id INTEGER, outcome_confidence REAL, "
            "notes TEXT, evaluator_run_id TEXT, evaluated_at TEXT)"
        )
    assert not predictions_store.grade(prediction_id=3, outcome="met", db_path=path)


def _seed(path: Path) -> int:
    conn = prediction_database(path)
    conn.executescript("""
        INSERT INTO kpi_definitions VALUES (1,'AMAT','Gross Margin','percent');
        INSERT INTO kpi_facts(id,ticker,period_end,fiscal_period_type,kpi_definition_id,
                              value,unit,source_doc_id,confidence)
        VALUES (1,'AMAT','2025-12-31','Q4',1,48.5,'percent',101,0.9);
    """)
    admit_prediction_facts(conn)
    conn.commit()
    conn.close()
    identity = predictions_store.record(
        ticker="AMAT",
        source_kind="mgmt_commitment",
        prediction_md="Guide 48.4%",
        made_at=datetime(2025, 1, 1, tzinfo=UTC),
        target_period=datetime(2025, 12, 31, tzinfo=UTC),
        kpi_name="Gross Margin",
        comparator="eq",
        target_value=48.4,
        target_unit="percent",
        db_path=path,
    )
    assert identity is not None
    return identity


def test_manifest_preserves_exact_identities_and_human_notes(tmp_path: Path) -> None:
    path = tmp_path / "explicit.db"
    identity = _seed(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE predictions SET notes='Human note\n  preserved spacing '")
    tally = grade_predictions.grade_pending(tmp_path, db_path=path, as_of=NOW)
    assert tally["graded"] == 1
    first = predictions_store.history(ticker="AMAT", db_path=path)[0]
    assert first.id == identity and first.outcome == "met"
    assert first.notes is not None
    assert first.notes.startswith("Human note\n  preserved spacing \n")
    manifest = json.loads(first.notes.splitlines()[-1])
    assert manifest["schema_version"] == "prediction-grading-evidence/v1"
    assert manifest["point"]["observation_id"] == "observation-1"
    assert manifest["point"]["resolution_id"] == "resolution-1"
    assert manifest["point"]["definition_revision_id"] == "definition-1"
    assert manifest["point"]["source_document_id"] == 101
    assert manifest["point"]["value"] == "48.5"
    assert manifest["point"]["locator_json"] == '{"pdf_page":7}'
    assert manifest["as_of"] == NOW.isoformat()
    assert manifest["window_days"] == 45
    assert grade_predictions.grade_pending(tmp_path, db_path=path, as_of=NOW)["graded"] == 0
    assert predictions_store.history(ticker="AMAT", db_path=path)[0] == first
    assert not predictions_store.grade(
        prediction_id=identity,
        outcome="missed",
        notes="stale second result",
        only_if_pending=True,
        db_path=path,
    )
    assert predictions_store.history(ticker="AMAT", db_path=path)[0] == first


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("UPDATE kpi_facts SET value=99", "historical_fact_authority_unavailable"),
        ("UPDATE kpi_facts SET unit='ratio'", "historical_fact_authority_unavailable"),
        ("UPDATE kpi_facts SET period_end='2025-12-30'", "historical_fact_authority_unavailable"),
        ("UPDATE kpi_facts SET locator='changed'", "historical_fact_authority_unavailable"),
        ("DELETE FROM kpi_fact_semantic_contexts", "empty_admitted_series"),
        ("DELETE FROM fact_observation_revisions", "empty_admitted_series"),
        (
            "INSERT INTO kpi_definitions VALUES (2,'AMAT','Gross Margin','percent')",
            "ambiguous_definition",
        ),
        ("UPDATE predictions SET target_unit='ratio'", "target_unit_mismatch"),
        ("UPDATE predictions SET target_unit=NULL", "target_unit_mismatch"),
        ("UPDATE kpi_facts SET currency='EUR'", "historical_fact_authority_unavailable"),
        (
            "UPDATE fact_resolution_outcomes SET resolution_status='unresolved_material'",
            "empty_admitted_series",
        ),
    ],
)
def test_unusable_evidence_remains_pending(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, mutation: str, reason: str
) -> None:
    path = tmp_path / "explicit.db"
    _seed(path)
    with sqlite3.connect(path) as conn:
        conn.execute(mutation)
    with caplog.at_level(logging.INFO):
        tally = grade_predictions.grade_pending(tmp_path, db_path=path, as_of=NOW)
    assert tally["graded"] == 0
    assert tally["skipped_unavailable_evidence"] == 1
    assert reason in caplog.text
    assert predictions_store.history(ticker="AMAT", db_path=path)[0].outcome == "pending"


def test_knowledge_cutoff_does_not_see_future_binding(tmp_path: Path) -> None:
    path = tmp_path / "explicit.db"
    _seed(path)
    tally = grade_predictions.grade_pending(
        tmp_path, db_path=path, as_of=NOW - timedelta(seconds=1)
    )
    assert tally["graded"] == 0
    assert tally["skipped_unavailable_evidence"] == 1


def test_stale_pending_list_cannot_replace_result_or_calibrate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "explicit.db"
    identity = _seed(path)
    pending = predictions_store.pending_for_grading(db_path=path, as_of=NOW)
    assert predictions_store.grade(
        prediction_id=identity, outcome="missed", notes="first manifest", db_path=path
    )

    def stale_pending(**_: object) -> list[predictions_store.Prediction]:
        return pending

    monkeypatch.setattr(predictions_store, "pending_for_grading", stale_pending)
    tally = grade_predictions.grade_pending(
        tmp_path, db_path=path, as_of=NOW, record_calibration=True
    )
    assert tally["graded"] == 0 and tally["skipped_write_failed"] == 1
    assert grade_predictions.extraction_quality_score(tally) is None
    retained = predictions_store.history(ticker="AMAT", db_path=path)[0]
    assert retained.outcome == "missed" and retained.notes == "first manifest"


def test_cli_explicit_database_and_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "explicit.db"
    _seed(path)
    before = path.read_bytes()
    assert (
        grade_predictions.main(["--repo-root", str(tmp_path), "--db", str(path), "--dry-run"]) == 0
    )
    assert path.read_bytes() == before
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(path))
    assert grade_predictions.main(["--repo-root", str(tmp_path), "--no-calibration"]) == 0
    assert predictions_store.history(ticker="AMAT", db_path=path)[0].outcome == "met"


def test_calibration_receives_scoped_database_and_resets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "explicit.db"
    _seed(path)
    seen: list[Path | None] = []

    def prompt_version(purpose: str) -> str:
        assert purpose == "management_prediction"
        seen.append(resolve_db_path(None))
        return "test-version"

    monkeypatch.setattr(grade_predictions, "prompt_version_for", prompt_version)
    with db_path_context(tmp_path / "outer.db"):
        grade_predictions.grade_pending(tmp_path, db_path=path, as_of=NOW, record_calibration=True)
        assert resolve_db_path(None) == tmp_path / "outer.db"
    assert seen == [path]


def test_currency_requires_explicit_unscaled_target(tmp_path: Path) -> None:
    path = tmp_path / "currency.db"
    conn = revision_database(path)
    conn.executescript(PREDICTIONS_SCHEMA)
    definition = persist_kpi_definition_revision(conn, definition_fixture())
    fact = fact_fixture(conn)
    persist_kpi_semantic_context(
        conn,
        kpi_fact_id=fact,
        context=semantic_fixture(),
        reviewed_by="owner",
        knowledge_at=NOW,
        kpi_definition_revision_id=definition.kpi_definition_revision_id,
    )
    conn.commit()
    conn.close()
    identity = predictions_store.record(
        ticker="NU",
        source_kind="mgmt_commitment",
        prediction_md="ARPAC $12",
        made_at=datetime(2024, 1, 1, tzinfo=UTC),
        target_period=datetime(2024, 12, 31, tzinfo=UTC),
        kpi_name="Monthly ARPAC",
        comparator="ge",
        target_value=12,
        target_unit="actual",
        db_path=path,
    )
    assert identity is not None
    assert grade_predictions.grade_pending(tmp_path, db_path=path, as_of=NOW)["graded"] == 0
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE predictions SET target_unit='USD'")
    assert grade_predictions.grade_pending(tmp_path, db_path=path, as_of=NOW)["graded"] == 1
    saved = predictions_store.history(ticker="NU", db_path=path)[0]
    assert saved.notes is not None
    manifest = json.loads(saved.notes)
    assert manifest["currency"] == "USD"
    assert manifest["definition"]["consolidation_scope"] == "consolidated"
    assert manifest["definition"]["accounting_basis"] == "management"
    assert manifest["point"]["value"] == "12.5"


@pytest.mark.parametrize(
    "second_period,target_period,reason",
    [
        ("2025-09-28", "2025-08-14", "ambiguous_period"),
        ("2025-06-30", "2025-06-30", "ambiguous_period_requires_explicit_selection"),
    ],
)
def test_equal_distance_periods_are_not_guessed(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    second_period: str,
    target_period: str,
    reason: str,
) -> None:
    path = tmp_path / "explicit.db"
    conn = prediction_database(path)
    conn.executescript("""
        INSERT INTO kpi_definitions VALUES (1,'AMAT','Gross Margin','percent');
        INSERT INTO kpi_facts(ticker,period_end,fiscal_period_type,kpi_definition_id,
                              value,unit,source_doc_id,confidence)
        VALUES ('AMAT','2025-06-30','Q2',1,48.5,'percent',101,0.9),
               ('AMAT','2025-09-28','Q3',1,48.0,'percent',102,0.9);
    """)
    conn.execute("UPDATE kpi_facts SET period_end=? WHERE id=2", (second_period,))
    admit_prediction_facts(conn)
    conn.commit()
    conn.close()
    predictions_store.record(
        ticker="AMAT",
        source_kind="mgmt_commitment",
        prediction_md="Guide 48.4%",
        made_at=datetime(2025, 1, 1, tzinfo=UTC),
        target_period=datetime.fromisoformat(target_period).replace(tzinfo=UTC),
        kpi_name="Gross Margin",
        comparator="eq",
        target_value=48.4,
        target_unit="percent",
        db_path=path,
    )
    with caplog.at_level(logging.INFO):
        tally = grade_predictions.grade_pending(tmp_path, db_path=path, as_of=NOW)
    assert tally["graded"] == 0 and reason in caplog.text


def test_missing_schema_is_failure_not_empty_success(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    path.touch()
    assert grade_predictions.main(["--repo-root", str(tmp_path), "--db", str(path)]) == 2
    assert (
        grade_predictions.main(["--repo-root", str(tmp_path), "--db", str(tmp_path / "missing.db")])
        == 2
    )
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE predictions SET target_value=90",
        "UPDATE predictions SET target_unit='ratio'",
        "UPDATE predictions SET comparator='le'",
        "UPDATE predictions SET notes='Owner updated note'",
    ],
)
def test_pending_owner_edit_invalidates_stale_calculation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    path = tmp_path / "explicit.db"
    _seed(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE predictions SET notes='Original note'")
    stale = predictions_store.pending_for_grading(db_path=path, as_of=NOW)
    with sqlite3.connect(path) as conn:
        conn.execute(mutation)
    expected = predictions_store.history(ticker="AMAT", db_path=path)[0]

    def stale_pending(**_: object) -> list[predictions_store.Prediction]:
        return stale

    def forbidden_calibration(*args: object, **kwargs: object) -> None:
        raise AssertionError("A stale calculation must not produce calibration")

    monkeypatch.setattr(predictions_store, "pending_for_grading", stale_pending)
    monkeypatch.setattr(grade_predictions, "record_score", forbidden_calibration)
    tally = grade_predictions.grade_pending(
        tmp_path, db_path=path, as_of=NOW, record_calibration=True
    )
    assert tally["graded"] == 0 and tally["skipped_write_failed"] == 1
    assert predictions_store.history(ticker="AMAT", db_path=path)[0] == expected


@pytest.mark.parametrize(
    "tag,reason",
    [
        (7, "concept_binding_unavailable"),
        (8, "concept_binding_unavailable"),
        (None, "concept_binding_unavailable"),
    ],
)
def test_explicit_concept_is_not_silently_ignored(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, tag: int | None, reason: str
) -> None:
    path = tmp_path / "explicit.db"
    _seed(path)
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE kpi_facts ADD COLUMN concept_id INTEGER")
        conn.execute("UPDATE kpi_facts SET concept_id=?", (tag,))
        conn.execute("UPDATE predictions SET kpi_concept_id=7")
    with caplog.at_level(logging.INFO):
        tally = grade_predictions.grade_pending(tmp_path, db_path=path, as_of=NOW)
    assert tally["graded"] == 0 and reason in caplog.text
    assert predictions_store.history(ticker="AMAT", db_path=path)[0].outcome == "pending"


@pytest.mark.parametrize("alias_has_facts", [True, False])
def test_richer_normalized_series_cannot_override_requested_identity(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, alias_has_facts: bool
) -> None:
    path = tmp_path / "explicit.db"
    conn = prediction_database(path)
    conn.executescript("""
        INSERT INTO kpi_definitions VALUES (1,'AMAT','Gross Margin','percent'),
                                           (2,'AMAT','Gross Margin (%)','percent');
        INSERT INTO kpi_facts(id,ticker,period_end,fiscal_period_type,kpi_definition_id,
                              value,unit,source_doc_id,confidence)
        VALUES (1,'AMAT','2025-12-31','Q4',1,48.5,'percent',101,0.9),
               (2,'AMAT','2025-12-31','Q4',2,80,'percent',102,0.9),
               (3,'AMAT','2025-09-30','Q3',2,79,'percent',103,0.9);
    """)
    if not alias_has_facts:
        conn.execute("DELETE FROM kpi_facts WHERE kpi_definition_id=2")
    admit_prediction_facts(conn)
    conn.commit()
    conn.close()
    predictions_store.record(
        ticker="AMAT",
        source_kind="mgmt_commitment",
        prediction_md="Guide 48.4%",
        made_at=datetime(2025, 1, 1, tzinfo=UTC),
        target_period=datetime(2025, 12, 31, tzinfo=UTC),
        kpi_name="Gross Margin",
        comparator="eq",
        target_value=48.4,
        target_unit="percent",
        db_path=path,
    )
    with caplog.at_level(logging.INFO):
        tally = grade_predictions.grade_pending(tmp_path, db_path=path, as_of=NOW)
    assert tally["graded"] == 0
    assert tally["skipped_unavailable_evidence"] == 1
    assert "ambiguous_definition" in caplog.text
    assert predictions_store.history(ticker="AMAT", db_path=path)[0].outcome == "pending"


def test_all_malformed_batch_retains_zero_extraction_calibration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "explicit.db"
    _seed(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE predictions SET comparator=NULL")
    scores: list[CalibrationScore] = []

    def record(score: CalibrationScore, *, db_path: Path | str | None = None) -> int:
        assert db_path == path
        scores.append(score)
        return 1

    monkeypatch.setattr(grade_predictions, "record_score", record)
    tally = grade_predictions.grade_pending(
        tmp_path, db_path=path, as_of=NOW, record_calibration=True
    )
    assert tally["graded"] == 0 and tally["skipped_unstructured"] == 1
    assert len(scores) == 1 and scores[0].score == 0.0
    assert scores[0].purpose == "management_prediction"


def test_prediction_not_yet_made_at_cutoff_is_not_graded_or_calibrated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "explicit.db"
    _seed(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE predictions SET made_at='2027-01-01T00:00:00+00:00'")

    def forbidden_calibration(*args: object, **kwargs: object) -> None:
        raise AssertionError("An unavailable future prediction is not a calibration case")

    monkeypatch.setattr(grade_predictions, "record_score", forbidden_calibration)
    tally = grade_predictions.grade_pending(
        tmp_path, db_path=path, as_of=NOW, record_calibration=True
    )
    assert tally["pending"] == 0 and tally["graded"] == 0
    assert predictions_store.history(ticker="AMAT", db_path=path)[0].outcome == "pending"


@pytest.mark.parametrize(
    "made_at,graded",
    [
        ("2026-09-06T11:00:00-07:00", 1),
        ("2026-09-06T11:00:01-07:00", 0),
        ("2026-09-06T11:00:00", 0),
    ],
)
def test_prediction_made_at_requires_an_aware_inclusive_cutoff(
    tmp_path: Path, made_at: str, graded: int
) -> None:
    path = tmp_path / "explicit.db"
    _seed(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE predictions SET made_at=?", (made_at,))
    tally = grade_predictions.grade_pending(tmp_path, db_path=path, as_of=NOW)
    assert tally["pending"] == tally["graded"] == graded
