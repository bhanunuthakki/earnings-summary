"""PR 3 — the predictions auto-grader: the pure comparator logic + an
end-to-end pass that grades the matchable past-due pendings and leaves the rest
pending (never guessing)."""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import grade_predictions  # noqa: E402

import predictions_store  # noqa: E402

_SCHEMA = """
CREATE TABLE predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL, source_kind TEXT NOT NULL,
    source_doc_id INTEGER, source_artifact_id INTEGER, source_excerpt TEXT,
    made_at TEXT NOT NULL, target_period TEXT, prediction_md TEXT NOT NULL,
    kpi_name TEXT, kpi_concept_id INTEGER, comparator TEXT,
    target_value REAL, target_unit TEXT,
    realized_value REAL, realized_doc_id INTEGER,
    outcome TEXT NOT NULL DEFAULT 'pending', outcome_confidence REAL,
    evaluated_at TEXT, evaluator_run_id TEXT, notes TEXT, created_at TEXT
);
CREATE TABLE kpi_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, name TEXT NOT NULL
);
CREATE TABLE kpi_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, period_end TEXT,
    fiscal_period_type TEXT, kpi_definition_id INTEGER, value REAL, unit TEXT,
    source_doc_id INTEGER, confidence REAL
);
"""


# ---------------------------------------------------------------------------
# Pure comparator logic
# ---------------------------------------------------------------------------


def test_grade_comparison_directional() -> None:
    g = grade_predictions.grade_comparison
    # A non-empty tuple is truthy, so `r and ...` narrows away the None branch.
    assert (r := g("ge", 10.0, 12.0, "percent")) and r[0] == "met" and r[1] == 0.9
    assert (r := g("ge", 10.0, -3.0, "percent")) and r[0] == "missed"
    assert (r := g("le", 50.0, 48.0, "percent")) and r[0] == "met"
    assert (r := g("gt", 10.0, 10.0, "percent")) and r[0] == "missed"  # strict >
    assert (r := g("lt", 10.0, 10.0, "percent")) and r[0] == "missed"


def test_grade_comparison_eq_tolerance() -> None:
    g = grade_predictions.grade_comparison
    # Percent-unit eq → absolute pp band (default 1.0pp): a 48.4% guide met at 48.5%.
    assert (r := g("eq", 48.4, 48.5, "percent")) and r[0] == "met" and r[1] == 0.7
    assert (r := g("eq", 48.4, 50.0, "percent")) and r[0] == "missed"  # 1.6pp off
    # Non-percent eq → relative band (default 5%).
    assert (r := g("eq", 100.0, 103.0, "usd")) and r[0] == "met"
    assert (r := g("eq", 100.0, 120.0, "usd")) and r[0] == "missed"


def test_grade_comparison_unknown_returns_none() -> None:
    assert grade_predictions.grade_comparison("between", 1.0, 2.0, "percent") is None


# ---------------------------------------------------------------------------
# End-to-end grade_pending
# ---------------------------------------------------------------------------


def test_grade_pending_grades_matchable_leaves_rest_pending(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    db = data / "portfolio.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()

    made = datetime(2025, 1, 1, tzinfo=UTC)
    # 1. MISSED — Revenue YoY ge 10, realized -3.48.
    pid_miss = predictions_store.record(
        ticker="AMAT",
        source_kind="mgmt_commitment",
        prediction_md="grow 10%+",
        made_at=made,
        target_period=datetime(2025, 10, 26, tzinfo=UTC),
        kpi_name="Revenue YoY Growth (USD)",
        comparator="ge",
        target_value=10.0,
        target_unit="percent",
        db_path=db,
    )
    # 2. MET — Gross Margin eq 48.4, realized 48.5 (within the 1.0pp band).
    pid_met = predictions_store.record(
        ticker="AMAT",
        source_kind="mgmt_commitment",
        prediction_md="guide 48.4%",
        made_at=made,
        target_period=datetime(2025, 7, 27, tzinfo=UTC),
        kpi_name="Gross Margin (GAAP)",
        comparator="eq",
        target_value=48.4,
        target_unit="percent",
        db_path=db,
    )
    # 3. SKIP (no_kpi) — a KPI with no definition/facts at all → never resolves.
    pid_no_kpi = predictions_store.record(
        ticker="AMAT",
        source_kind="mgmt_commitment",
        prediction_md="mystery metric",
        made_at=made,
        target_period=datetime(2025, 3, 1, tzinfo=UTC),
        kpi_name="Totally Unknown KPI",
        comparator="ge",
        target_value=5.0,
        target_unit="percent",
        db_path=db,
    )
    # 4. SKIP (no_fact) — resolves to Gross Margin, but no fact near 2020.
    pid_no_fact = predictions_store.record(
        ticker="AMAT",
        source_kind="mgmt_commitment",
        prediction_md="ancient target",
        made_at=made,
        target_period=datetime(2020, 1, 1, tzinfo=UTC),
        kpi_name="Gross Margin (GAAP)",
        comparator="ge",
        target_value=40.0,
        target_unit="percent",
        db_path=db,
    )
    # 5. FUTURE — target_period not yet elapsed → not even returned for grading.
    pid_future = predictions_store.record(
        ticker="AMAT",
        source_kind="mgmt_commitment",
        prediction_md="future guide",
        made_at=made,
        target_period=datetime(2099, 1, 1, tzinfo=UTC),
        kpi_name="Gross Margin (GAAP)",
        comparator="ge",
        target_value=40.0,
        target_unit="percent",
        db_path=db,
    )
    # Individual asserts (not a loop) so each id narrows int|None -> int for the
    # hist[...] lookups below.
    assert pid_miss is not None
    assert pid_met is not None
    assert pid_no_kpi is not None
    assert pid_no_fact is not None
    assert pid_future is not None

    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        INSERT INTO kpi_definitions (id, ticker, name)
            VALUES (1, 'AMAT', 'Revenue YoY Growth (USD)'), (2, 'AMAT', 'Gross Margin (GAAP)');
        INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, value, unit, source_doc_id, confidence)
            VALUES ('AMAT','2025-10-26','Q4',1,-3.48,'percent',101,0.9),
                   ('AMAT','2025-07-27','Q3',2,48.5,'percent',102,0.9);
        """
    )
    conn.commit()
    conn.close()

    tally = grade_predictions.grade_pending(tmp_path, as_of=datetime(2026, 6, 1, tzinfo=UTC))

    assert tally["pending"] == 4  # the future one is excluded by pending_for_grading
    assert tally["graded"] == 2
    assert tally["met"] == 1
    assert tally["missed"] == 1
    assert tally["skipped_no_kpi"] == 1
    assert tally["skipped_no_fact"] == 1

    hist = {p.id: p for p in predictions_store.history(ticker="AMAT", limit=50, db_path=db)}
    assert hist[pid_miss].outcome == "missed"
    assert hist[pid_miss].realized_value == pytest.approx(-3.48)
    assert hist[pid_miss].realized_doc_id == 101  # the source fact is captured for audit
    assert hist[pid_met].outcome == "met"
    # The grader never guesses: unmatchable + not-yet-due stay pending.
    assert hist[pid_no_kpi].outcome == "pending"
    assert hist[pid_no_fact].outcome == "pending"
    assert hist[pid_future].outcome == "pending"


def test_grade_pending_dry_run_writes_nothing(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    db = data / "portfolio.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()

    pid = predictions_store.record(
        ticker="AMAT",
        source_kind="mgmt_commitment",
        prediction_md="grow 10%+",
        made_at=datetime(2025, 1, 1, tzinfo=UTC),
        target_period=datetime(2025, 10, 26, tzinfo=UTC),
        kpi_name="Revenue YoY Growth (USD)",
        comparator="ge",
        target_value=10.0,
        target_unit="percent",
        db_path=db,
    )
    assert pid is not None
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        INSERT INTO kpi_definitions (id, ticker, name) VALUES (1, 'AMAT', 'Revenue YoY Growth (USD)');
        INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, value, unit, source_doc_id, confidence)
            VALUES ('AMAT','2025-10-26','Q4',1,-3.48,'percent',101,0.9);
        """
    )
    conn.commit()
    conn.close()

    tally = grade_predictions.grade_pending(
        tmp_path, dry_run=True, as_of=datetime(2026, 6, 1, tzinfo=UTC)
    )
    assert tally["graded"] == 1  # counted...
    hist = {p.id: p for p in predictions_store.history(ticker="AMAT", limit=10, db_path=db)}
    assert hist[pid].outcome == "pending"  # ...but nothing written


_CALIBRATION_SCHEMA = """
CREATE TABLE prompt_calibration_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purpose TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    ticker TEXT,
    score REAL NOT NULL,
    reason TEXT,
    scored_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    scored_by TEXT,
    artifact_id INTEGER
);
"""


def test_grade_pending_records_extraction_calibration(tmp_path: Path) -> None:
    """``record_calibration=True`` writes one calibration row for the run tagged
    ``management_prediction`` @ the registry version, with the gradeable fraction
    as the score. Two gradeable + one malformed (no_kpi) -> 2/3; no_fact excluded."""
    data = tmp_path / "data"
    data.mkdir()
    db = data / "portfolio.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA + _CALIBRATION_SCHEMA)
    conn.commit()
    conn.close()

    made = datetime(2025, 1, 1, tzinfo=UTC)
    # Two gradeable (1 missed, 1 met) ...
    predictions_store.record(
        ticker="AMAT",
        source_kind="mgmt_commitment",
        prediction_md="grow 10%+",
        made_at=made,
        target_period=datetime(2025, 10, 26, tzinfo=UTC),
        kpi_name="Revenue YoY Growth (USD)",
        comparator="ge",
        target_value=10.0,
        target_unit="percent",
        db_path=db,
    )
    predictions_store.record(
        ticker="AMAT",
        source_kind="mgmt_commitment",
        prediction_md="guide 48.4%",
        made_at=made,
        target_period=datetime(2025, 7, 27, tzinfo=UTC),
        kpi_name="Gross Margin (GAAP)",
        comparator="eq",
        target_value=48.4,
        target_unit="percent",
        db_path=db,
    )
    # ... and one malformed extraction (unresolvable KPI -> skipped_no_kpi).
    predictions_store.record(
        ticker="AMAT",
        source_kind="mgmt_commitment",
        prediction_md="mystery",
        made_at=made,
        target_period=datetime(2025, 3, 1, tzinfo=UTC),
        kpi_name="Totally Unknown KPI",
        comparator="ge",
        target_value=5.0,
        target_unit="percent",
        db_path=db,
    )
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        INSERT INTO kpi_definitions (id, ticker, name)
            VALUES (1, 'AMAT', 'Revenue YoY Growth (USD)'), (2, 'AMAT', 'Gross Margin (GAAP)');
        INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, kpi_definition_id, value, unit, source_doc_id, confidence)
            VALUES ('AMAT','2025-10-26','Q4',1,-3.48,'percent',101,0.9),
                   ('AMAT','2025-07-27','Q3',2,48.5,'percent',102,0.9);
        """
    )
    conn.commit()
    conn.close()

    tally = grade_predictions.grade_pending(
        tmp_path, record_calibration=True, as_of=datetime(2026, 6, 1, tzinfo=UTC)
    )
    assert tally["graded"] == 2
    assert tally["skipped_no_kpi"] == 1

    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT purpose, prompt_version, score, scored_by FROM prompt_calibration_scores"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    purpose, version, score, scored_by = rows[0]
    assert purpose == "management_prediction"
    assert version == "v1"
    assert scored_by == "grade_predictions"
    assert score == pytest.approx(2 / 3)
