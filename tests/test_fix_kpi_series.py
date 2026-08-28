"""Tests for the read-only KPI anomaly detector.

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


def test_apply_is_not_an_available_mutation_path(tmp_path: Path, fix_mod: Any) -> None:
    db_path = _build_db(tmp_path)
    with pytest.raises(SystemExit) as exc:
        fix_mod.main(["--db", str(db_path), "--ticker", "NU", "--apply"])
    assert exc.value.code == 2

    conn = sqlite3.connect(db_path)
    values = conn.execute("SELECT value FROM kpi_facts ORDER BY id").fetchall()
    assert [float(row[0]) for row in values] == [
        109.7,
        95.0,
        119.0,
        114000000.0,
        110.0,
        114.0,
    ]
    conn.close()


def test_read_only_scan_is_idempotent(
    tmp_path: Path, fix_mod: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = _build_db(tmp_path)
    fix_mod.main(["--db", str(db_path), "--ticker", "NU"])
    first = json.loads(capsys.readouterr().out)
    rc = fix_mod.main(["--db", str(db_path), "--ticker", "NU"])
    assert rc == 0
    second = json.loads(capsys.readouterr().out)
    assert second == first


def test_scale_anomaly_scan_does_not_infer_semantics_from_metric_name(
    tmp_path: Path, fix_mod: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """A large discontinuity is reviewable even when its label is unfamiliar."""
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
    assert report["scanned_definitions"] == 1
    assert len(report["unit_error_findings"]) == 1


def test_apply_rejected_against_readonly_uri(tmp_path: Path, fix_mod: Any) -> None:
    db_path = _build_db(tmp_path)
    with pytest.raises(SystemExit) as exc:
        fix_mod.main(["--db", f"file:{db_path}?mode=ro", "--uri", "--apply"])
    assert exc.value.code == 2


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
