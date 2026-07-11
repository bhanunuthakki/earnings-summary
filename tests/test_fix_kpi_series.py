"""Tests for execution/fix_kpi_series.py — unit-error detection/fix,
non-monotonic detection (review-only, never auto-fixed), idempotence, and
--apply guardrails.

Uses the same by-file-path import pattern as
test_backfill_fiscal_period_stamps.py (execution/ scripts aren't on the
package path).
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def _load_module() -> Any:
    src = PROJECT_ROOT / "execution" / "fix_kpi_series.py"
    spec = importlib.util.spec_from_file_location("fix_kpi_series", src)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fix_kpi_series"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fix_mod() -> Any:
    return _load_module()


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE kpi_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            unit TEXT NOT NULL
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_end TIMESTAMP NOT NULL,
            fiscal_period_type TEXT NOT NULL,
            kpi_definition_id INTEGER NOT NULL,
            value NUMERIC(24, 6) NOT NULL,
            unit TEXT NOT NULL,
            source_doc_id INTEGER NOT NULL,
            source_excerpt VARCHAR(1024),
            extracted_by VARCHAR(64)
        );
        """
    )
    conn.commit()


def _seed_nu_total_customers(conn: sqlite3.Connection) -> int:
    """Mirrors the real prod NU def-641 series: a genuine non-monotonic dip
    at 2024-12-31, then a raw-count unit-error row at 2025-06-30."""
    conn.execute(
        "INSERT INTO kpi_definitions (id, ticker, name, unit) VALUES (641, 'NU', "
        "'Total customers (millions)', 'count')"
    )
    rows = [
        ("2024-09-30", "Q3", 109.7, 100),
        ("2024-12-31", "Q4", 95.0, 101),  # non-monotonic dip (genuine, review-only)
        ("2025-03-31", "Q1", 119.0, 102),
        ("2025-06-30", "Q2", 114000000.0, 103),  # unit-error row
        ("2025-09-30", "Q3", 110.0, 104),
        ("2025-12-31", "Q4", 114.0, 105),
    ]
    for period_end, ftype, value, source_doc_id in rows:
        conn.execute(
            "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, "
            "kpi_definition_id, value, unit, source_doc_id) "
            "VALUES ('NU', ?, ?, 641, ?, 'count', ?)",
            (period_end, ftype, value, source_doc_id),
        )
    conn.commit()
    return 641


def _build_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    _seed_nu_total_customers(conn)
    conn.close()
    return db_path


def test_dry_run_finds_unit_error_and_non_monotonic_without_writing(
    tmp_path: Path, fix_mod: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = _build_db(tmp_path)
    rc = fix_mod.main(["--db", str(db_path), "--ticker", "NU"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["apply"] is False
    assert report["applied"] == 0
    assert len(report["unit_error_findings"]) == 1
    ue = report["unit_error_findings"][0]
    assert ue["old_value"] == "114000000"
    assert ue["proposed_value"] == "114.000000"
    assert len(report["non_monotonic_findings"]) == 1
    nm = report["non_monotonic_findings"][0]
    assert nm["period_end"] == "2024-12-31"
    assert nm["prior_value"] == "109.7"

    # dry-run never writes.
    conn = sqlite3.connect(db_path)
    value = conn.execute("SELECT value FROM kpi_facts WHERE period_end = '2025-06-30'").fetchone()[
        0
    ]
    assert float(value) == 114000000.0
    conn.close()


def test_apply_corrects_only_the_unit_error_row(tmp_path: Path, fix_mod: Any) -> None:
    db_path = _build_db(tmp_path)
    rc = fix_mod.main(["--db", str(db_path), "--ticker", "NU", "--apply"])
    assert rc == 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    fixed = conn.execute(
        "SELECT value, extracted_by, source_excerpt FROM kpi_facts WHERE period_end = '2025-06-30'"
    ).fetchone()
    assert float(fixed["value"]) == pytest.approx(114.0)
    assert "fix:unit_scale" in fixed["extracted_by"]
    assert "fix_kpi_series.py" in fixed["source_excerpt"]

    # The non-monotonic row is NEVER auto-fixed — still the original value.
    untouched = conn.execute(
        "SELECT value FROM kpi_facts WHERE period_end = '2024-12-31'"
    ).fetchone()[0]
    assert float(untouched) == 95.0
    conn.close()


def test_apply_is_idempotent(
    tmp_path: Path, fix_mod: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """A second --apply run finds nothing left to fix — the corrected value
    is no longer a >jump-ratio outlier."""
    db_path = _build_db(tmp_path)
    fix_mod.main(["--db", str(db_path), "--ticker", "NU", "--apply"])
    capsys.readouterr()  # discard first run's output

    rc = fix_mod.main(["--db", str(db_path), "--ticker", "NU", "--apply"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["unit_error_findings"] == []
    assert report["applied"] == 0


def test_scoped_to_cumulative_marked_kpis_only(
    tmp_path: Path, fix_mod: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """A non-cumulative KPI with a huge outlier value is never scanned — the
    guard only applies to KPIs matching the cumulative-name allowlist."""
    db_path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(db_path)
    _create_schema(conn)
    conn.execute(
        "INSERT INTO kpi_definitions (id, ticker, name, unit) VALUES "
        "(999, 'NU', 'Monthly ARPAC (USD)', 'actual')"
    )
    for period_end, ftype, value, doc_id in (
        ("2025-03-31", "Q1", 12.0, 1),
        ("2025-06-30", "Q2", 12000000.0, 2),  # huge outlier, but not a cumulative-marked KPI
    ):
        conn.execute(
            "INSERT INTO kpi_facts (ticker, period_end, fiscal_period_type, "
            "kpi_definition_id, value, unit, source_doc_id) "
            "VALUES ('NU', ?, ?, 999, ?, 'actual', ?)",
            (period_end, ftype, value, doc_id),
        )
    conn.commit()
    conn.close()

    rc = fix_mod.main(["--db", str(db_path), "--ticker", "NU"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["scanned_definitions"] == 0
    assert report["unit_error_findings"] == []


def test_apply_rejected_against_readonly_uri(tmp_path: Path, fix_mod: Any) -> None:
    """--apply combined with a mode=ro URI is rejected before any connection
    attempt — the explicit safety rail for prod dry-runs."""
    db_path = _build_db(tmp_path)
    rc = fix_mod.main(["--db", f"file:{db_path}?mode=ro", "--uri", "--apply"])
    assert rc == 2


def test_readonly_uri_dry_run_reports_cleanly(
    tmp_path: Path, fix_mod: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """A read-only URI dry-run (the sanctioned way to point this script at
    prod) reports findings without needing write access."""
    db_path = _build_db(tmp_path)
    rc = fix_mod.main(["--db", f"file:{db_path}?mode=ro", "--uri", "--ticker", "NU"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert len(report["unit_error_findings"]) == 1


def test_kpi_name_requires_ticker(tmp_path: Path, fix_mod: Any) -> None:
    """--kpi-name without --ticker is a usage error (argparse exits 2)."""
    db_path = _build_db(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        fix_mod.main(["--db", str(db_path), "--kpi-name", "Total customers (millions)"])
    assert exc_info.value.code == 2
