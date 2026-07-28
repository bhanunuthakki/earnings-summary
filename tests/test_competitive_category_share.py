"""Piece 1 — annual 3rd-party category-share ingest into kpi_facts."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import competitive.category_share as category_share  # noqa: E402
from competitive.category_share import ingest_category_share, load_seed  # noqa: E402

from ._competitive_fixtures import kpi_conn  # noqa: E402


def _write_seed(repo_root: Path) -> None:
    seed = {
        "ticker": "RBRK",
        "entries": [
            {
                "metric": "Gartner MQ position — Rubrik (ordinal 1-4)",
                "fiscal_year": 2025,
                "value": 4,
                "unit": "count",
                "source": "Gartner MQ 2025",
                "label": "Leader",
                "note": "furthest in Vision",
            },
            {
                "metric": "Data-protection category share — Cohesity (%)",
                "fiscal_year": 2025,
                "value": 19,
                "unit": "percent",
                "source": "Owner brief",
                "label": "Data-resilience share",
                "note": None,
            },
            {
                "metric": "Data-protection category share — Rubrik (%)",
                "fiscal_year": 2025,
                "value": None,  # awaiting-source slot — must be skipped
                "unit": "percent",
                "source": "IDC (operator to fill)",
                "label": None,
                "note": None,
            },
        ],
    }
    path = repo_root / "micro_thesis" / "competitive" / "RBRK_category_share.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(seed), encoding="utf-8")


def test_load_seed_validates_and_returns_none_when_absent(tmp_path: Path) -> None:
    assert load_seed(tmp_path, "RBRK") is None
    _write_seed(tmp_path)
    seed = load_seed(tmp_path, "RBRK")
    assert seed is not None
    assert seed.ticker == "RBRK"
    assert len(seed.entries) == 3


def test_ingest_writes_grounded_facts_and_skips_null(tmp_path: Path) -> None:
    _write_seed(tmp_path)
    conn = kpi_conn()
    result = ingest_category_share(conn, tmp_path, "RBRK")

    # Two grounded metrics written; the null "awaiting source" entry skipped.
    assert result.inserted == 2
    assert result.skipped_awaiting_source == 1
    assert "Gartner MQ position — Rubrik (ordinal 1-4)" in result.written_metrics

    rows = conn.execute(
        "SELECT d.name, f.value, f.unit, f.fiscal_period_type, f.period_end, f.source_excerpt, "
        "       d.reporting_cadence "
        "FROM kpi_facts f JOIN kpi_definitions d ON d.id = f.kpi_definition_id "
        "WHERE f.ticker = 'RBRK' ORDER BY d.name"
    ).fetchall()
    by_name = {r["name"]: r for r in rows}
    assert set(by_name) == {
        "Gartner MQ position — Rubrik (ordinal 1-4)",
        "Data-protection category share — Cohesity (%)",
    }

    mq = by_name["Gartner MQ position — Rubrik (ordinal 1-4)"]
    assert float(mq["value"]) == 4.0
    assert mq["unit"] == "count"
    assert mq["fiscal_period_type"] == "FY"  # lands on the annual axis
    assert str(mq["period_end"]).startswith("2025-12-31")
    assert mq["reporting_cadence"] == "annual"  # definition marked cadence-aware
    assert "Leader" in mq["source_excerpt"]  # prose label travels in the excerpt

    cohesity = by_name["Data-protection category share — Cohesity (%)"]
    assert float(cohesity["value"]) == 19.0
    assert cohesity["unit"] == "percent"

    # The null-value Rubrik-share entry must NOT have created a fabricated fact.
    assert "Data-protection category share — Rubrik (%)" not in by_name
    assert [
        row[0]
        for row in conn.execute(
            "SELECT status FROM ingestion_runs "
            "WHERE directive='ingest_competitive_category_share'"
        ).fetchall()
    ] == ["ok"]


def test_ingest_is_idempotent(tmp_path: Path) -> None:
    _write_seed(tmp_path)
    conn = kpi_conn()
    first = ingest_category_share(conn, tmp_path, "RBRK")
    second = ingest_category_share(conn, tmp_path, "RBRK")
    assert first.inserted == 2
    assert second.inserted == 0  # replay writes nothing new
    assert second.skipped_existing == 2
    n = conn.execute("SELECT COUNT(*) FROM kpi_facts WHERE ticker = 'RBRK'").fetchone()[0]
    assert n == 2


def test_ingest_failure_closes_attempt_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_seed(tmp_path)
    conn = kpi_conn()

    def fail_persist(*_: object, **__: object) -> None:
        raise RuntimeError("persist failed")

    monkeypatch.setattr(category_share, "persist_manifest", fail_persist)
    with pytest.raises(RuntimeError, match="persist failed"):
        ingest_category_share(conn, tmp_path, "RBRK")

    row = conn.execute(
        "SELECT status, ended_at, error_summary FROM ingestion_runs"
    ).fetchone()
    assert row is not None
    assert row["status"] == "failed"
    assert row["ended_at"] is not None
    assert "persist failed" in str(row["error_summary"])


def test_ingest_missing_seed_is_noop(tmp_path: Path) -> None:
    conn = kpi_conn()
    result = ingest_category_share(conn, tmp_path, "RBRK")
    assert result.inserted == 0
    assert result.written_metrics == []


def test_committed_rbrk_seed_loads_and_is_grounded() -> None:
    """The real committed seed parses and at least one entry is grounded (so the
    KPI reads a real value), with the Rubrik-share slot left awaiting source."""
    seed = load_seed(PROJECT_ROOT, "RBRK")
    assert seed is not None
    grounded = [e for e in seed.entries if e.value is not None]
    awaiting = [e for e in seed.entries if e.value is None]
    assert grounded, "expected at least one grounded category datapoint"
    assert any(e.metric.startswith("Gartner MQ position") for e in grounded)
    assert any(e.metric == "Data-protection category share — Rubrik (%)" for e in awaiting)


def test_conn_fixture_is_sqlite() -> None:
    assert isinstance(kpi_conn(), sqlite3.Connection)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
