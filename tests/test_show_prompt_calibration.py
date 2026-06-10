"""Tests for the prompt-calibration consumer (CLI + dashboard widget).

Closes the half-open loop: graders write prompt_calibration_scores;
these tests pin the read side — the CLI in execution/show_prompt_calibration
plus the Prompt-Quality panel in src/report/renderers/workspace_html.
"""

from __future__ import annotations

import io
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import show_prompt_calibration as cli  # noqa: E402

from llm.calibration import CalibrationScore, record_score  # noqa: E402
from report.renderers.workspace_html import _prompt_quality_panel  # noqa: E402

Capsys = pytest.CaptureFixture[str]


def _build_schema(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
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
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _seed_with_explicit_dates(
    db_path: Path, rows: list[tuple[str, str, float, datetime]]
) -> None:
    """Bypass record_score so we can stamp scored_at directly — needed by the
    window-filter test to put rows at controlled ages."""
    conn = sqlite3.connect(str(db_path))
    try:
        for purpose, version, score, when in rows:
            conn.execute(
                "INSERT INTO prompt_calibration_scores "
                "(purpose, prompt_version, score, scored_at) VALUES (?, ?, ?, ?)",
                (purpose, version, score, when.isoformat()),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_basic(db_path: Path) -> None:
    """Mixed bear_case (v2 + v3) and decision_audit (v1) scores — recent
    enough to fall inside the default 30-day window."""
    for purpose, version, score in [
        ("bear_case", "v2", 0.4),
        ("bear_case", "v2", 0.5),
        ("bear_case", "v3", 0.8),
        ("bear_case", "v3", 0.85),
        ("bear_case", "v3", 0.9),
        ("decision_audit", "v1", 0.6),
        ("decision_audit", "v1", 0.7),
    ]:
        record_score(
            CalibrationScore(purpose=purpose, prompt_version=version, score=score),
            db_path=db_path,
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _run_cli(argv: list[str], capsys: Capsys) -> str:
    rc = cli.main(argv)
    assert rc == 0
    return capsys.readouterr().out


def test_cli_empty_db_prints_helpful_message_and_exits_zero(
    tmp_path: Path, capsys: Capsys
) -> None:
    db = tmp_path / "portfolio.db"
    _build_schema(db)
    out = _run_cli(["--db", str(db)], capsys)
    assert "No calibration scores recorded yet" in out
    assert "grade_bear_cases" in out
    assert "grade_decisions" in out


def test_cli_missing_db_also_falls_back(tmp_path: Path, capsys: Capsys) -> None:
    """No DB file at all — should not raise, should print the hint."""
    out = _run_cli(["--db", str(tmp_path / "does-not-exist.db")], capsys)
    assert "No calibration scores recorded yet" in out


def test_cli_renders_human_table_for_populated_db(tmp_path: Path, capsys: Capsys) -> None:
    db = tmp_path / "portfolio.db"
    _build_schema(db)
    _seed_basic(db)

    out = _run_cli(["--db", str(db)], capsys)

    # Header + each (purpose, version) pair must appear, with the v3 group
    # sorted above v2 inside bear_case (DESC). decision_audit comes after
    # bear_case (ASC purpose).
    assert "PROMPT CALIBRATION" in out
    assert "bear_case" in out
    assert "decision_audit" in out
    assert "v3" in out and "v2" in out and "v1" in out

    bear_v3 = out.find("bear_case") + out[out.find("bear_case"):].find("v3")
    bear_v2 = out.find("bear_case") + out[out.find("bear_case"):].find("v2")
    decision_v1 = out.find("decision_audit")
    assert bear_v3 < bear_v2 < decision_v1


def test_cli_json_output_matches_schema(tmp_path: Path, capsys: Capsys) -> None:
    db = tmp_path / "portfolio.db"
    _build_schema(db)
    _seed_basic(db)

    out = _run_cli(["--db", str(db), "--json"], capsys)
    payload = json.loads(out)

    assert payload["window_days"] == 30
    assert payload["purpose"] is None
    assert payload["ticker"] is None
    keys_per_row = {
        "purpose",
        "prompt_version",
        "n_runs",
        "avg_score",
        "p25",
        "p50",
        "p75",
        "min_score",
        "max_score",
        "last_scored_at",
    }
    assert payload["summaries"], "expected at least one row"
    for row in payload["summaries"]:
        assert set(row.keys()) == keys_per_row
    rows_by_key = {(r["purpose"], r["prompt_version"]): r for r in payload["summaries"]}
    bear_v3 = rows_by_key[("bear_case", "v3")]
    assert bear_v3["n_runs"] == 3
    assert abs(bear_v3["avg_score"] - 0.85) < 1e-9


def test_cli_window_days_filter_excludes_old_rows(tmp_path: Path, capsys: Capsys) -> None:
    """--window-days N must filter out rows scored more than N days ago."""
    db = tmp_path / "portfolio.db"
    _build_schema(db)

    now = datetime.now(UTC).replace(tzinfo=None)
    _seed_with_explicit_dates(
        db,
        [
            ("bear_case", "v3", 0.9, now - timedelta(days=1)),   # in 7d window
            ("bear_case", "v3", 0.8, now - timedelta(days=5)),   # in 7d window
            ("bear_case", "v2", 0.4, now - timedelta(days=20)),  # only in 30d window
            ("bear_case", "v1", 0.2, now - timedelta(days=90)),  # only in all-time
        ],
    )

    # 7-day window: only v3 survives, with n=2.
    out_7d = json.loads(_run_cli(["--db", str(db), "--window-days", "7", "--json"], capsys))
    rows = {(r["purpose"], r["prompt_version"]): r for r in out_7d["summaries"]}
    assert set(rows.keys()) == {("bear_case", "v3")}
    assert rows[("bear_case", "v3")]["n_runs"] == 2

    # 30-day window: v3 and v2 (n=2 + n=1).
    out_30d = json.loads(_run_cli(["--db", str(db), "--window-days", "30", "--json"], capsys))
    rows = {(r["purpose"], r["prompt_version"]): r for r in out_30d["summaries"]}
    assert set(rows.keys()) == {("bear_case", "v3"), ("bear_case", "v2")}

    # All time: all three versions present.
    out_all = json.loads(_run_cli(["--db", str(db), "--window-days", "0", "--json"], capsys))
    rows = {(r["purpose"], r["prompt_version"]): r for r in out_all["summaries"]}
    assert set(rows.keys()) == {
        ("bear_case", "v3"),
        ("bear_case", "v2"),
        ("bear_case", "v1"),
    }


def test_cli_purpose_and_ticker_filters_narrow_results(tmp_path: Path, capsys: Capsys) -> None:
    db = tmp_path / "portfolio.db"
    _build_schema(db)
    record_score(
        CalibrationScore(
            purpose="bear_case", prompt_version="v3", score=0.8, ticker="META"
        ),
        db_path=db,
    )
    record_score(
        CalibrationScore(
            purpose="bear_case", prompt_version="v3", score=0.5, ticker="GOOG"
        ),
        db_path=db,
    )
    record_score(
        CalibrationScore(
            purpose="decision_audit", prompt_version="v1", score=0.4
        ),
        db_path=db,
    )

    bear_only = json.loads(
        _run_cli(["--db", str(db), "--purpose", "bear_case", "--json"], capsys)
    )
    assert {r["purpose"] for r in bear_only["summaries"]} == {"bear_case"}

    meta_only = json.loads(
        _run_cli(["--db", str(db), "--ticker", "META", "--json"], capsys)
    )
    assert all(r["n_runs"] == 1 for r in meta_only["summaries"])
    assert {(r["purpose"], r["prompt_version"]) for r in meta_only["summaries"]} == {
        ("bear_case", "v3")
    }


# ---------------------------------------------------------------------------
# Dashboard widget
# ---------------------------------------------------------------------------


def test_widget_hides_when_no_data(tmp_path: Path) -> None:
    body = io.StringIO()
    # No DB file at all → no panel (P4.2 hide-don't-stub; the Governance
    # coverage matrix is where data gaps stay visible).
    _prompt_quality_panel(body, tmp_path / "does-not-exist.db")
    assert body.getvalue() == ""


def test_widget_renders_table_with_populated_db(tmp_path: Path) -> None:
    db = tmp_path / "portfolio.db"
    _build_schema(db)
    _seed_basic(db)

    body = io.StringIO()
    _prompt_quality_panel(body, db)
    html = body.getvalue()

    assert "Prompt quality" in html
    assert "<table" in html
    # Column headers
    for col in ("Purpose", "Version", "avg", "p25", "p50", "p75", "Last scored", "30d trend"):
        assert col in html, f"missing column header: {col}"
    # Data rows for each (purpose, version) we seeded
    for purpose, version in [
        ("bear_case", "v3"),
        ("bear_case", "v2"),
        ("decision_audit", "v1"),
    ]:
        assert purpose in html and version in html

    # Sparkline emits an <svg>. v3 has 3 same-day data points → one <svg>.
    assert "<svg" in html
