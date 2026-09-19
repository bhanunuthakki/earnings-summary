"""Explicit database ownership and subprocess failure regression coverage."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from compute.segment_cache import apply_overrides

ROOT = Path(__file__).resolve().parents[1]


def _batch():
    spec = importlib.util.spec_from_file_location(
        "audit_dcf_batch", ROOT / "execution/build_all_redesigned_dcf.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_override_database_cannot_silently_use_raw_facts(tmp_path: Path) -> None:
    with pytest.raises((OSError, RuntimeError)):
        apply_overrides(
            [{"data": {"Cloud": 123}}],
            ticker="TEST",
            dim_type="product",
            db_path=str(tmp_path / "missing.db"),
        )


def test_failed_assumption_refresh_stops_ticker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    batch = _batch()
    calls: list[str] = []

    def run(script: str, ticker: str, dest: Path | None = None):
        calls.append(script)
        return (
            ("", "assumption refresh failed", 2)
            if "assumptions" in script
            else ("RESULT\tTEST", "", 0)
        )

    monkeypatch.setattr(batch, "_run", run)
    monkeypatch.setattr(
        sys, "argv", ["batch", "--tickers", "TEST", "--opus", "--out-dir", str(tmp_path)]
    )
    assert batch.main() == 1
    assert calls == ["refresh_dcf_assumptions.py"]


def test_failed_builder_cannot_claim_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    batch = _batch()

    def failed_build(*args: object) -> tuple[str, str, int]:
        return "RESULT\tTEST", "failed after output", 2

    monkeypatch.setattr(batch, "_run", failed_build)
    monkeypatch.setattr(sys, "argv", ["batch", "--tickers", "TEST", "--out-dir", str(tmp_path)])
    assert batch.main() == 1


def test_segment_override_schema_is_required(tmp_path: Path) -> None:
    database = tmp_path / "missing_schema.sqlite"
    sqlite3.connect(database).close()
    with pytest.raises(sqlite3.OperationalError, match="fact_overrides"):
        apply_overrides([], ticker="TEST", dim_type="product", db_path=str(database))


def test_artifact_root_does_not_replace_database_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from db_paths import db_path_context
    from execution import build_artifacts

    database = tmp_path / "external.sqlite"
    sqlite3.connect(database).close()
    repository = tmp_path / "checkout"
    for name in ("PROJECT_ROOT", "DB_PATH", "DATA_DIR", "FMP_DIR"):
        monkeypatch.setattr(build_artifacts.db, name, getattr(build_artifacts.db, name))
    with db_path_context(database):
        build_artifacts.configure_artifact_runtime(repository)
    assert Path(build_artifacts.db.DB_PATH) == database.resolve()
    assert not (repository / "data" / "portfolio.db").exists()


def test_report_panels_read_only_explicit_external_database(tmp_path: Path) -> None:
    from report.renderers.workspace_data import load_workspace_p3_panels

    database = tmp_path / "external.sqlite"
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE macro_sensitivities (ticker TEXT, series_id TEXT, beta REAL, r_squared REAL, lookback_window_days INTEGER, computed_at TEXT)"
        )
        conn.execute(
            "INSERT INTO macro_sensitivities VALUES ('TEST', 'vix', -0.4, 0.15, 90, '2026-05-01 00:00:00')"
        )
    repository = tmp_path / "checkout"
    assert not load_workspace_p3_panels("TEST", repository).macro_sensitivities
    panels = load_workspace_p3_panels("TEST", repository, db_path=database)
    assert len(panels.macro_sensitivities) == 1
    assert not (repository / "data" / "portfolio.db").exists()


def test_provider_neutral_assumption_alias(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    batch = _batch()
    calls: list[str] = []

    def run(script: str, ticker: str, dest: Path | None = None) -> tuple[str, str, int]:
        calls.append(script)
        return "RESULT\tTEST", "", 0

    monkeypatch.setattr(batch, "_run", run)
    monkeypatch.setattr(
        sys,
        "argv",
        ["batch", "--tickers", "TEST", "--refresh-assumptions", "--out-dir", str(tmp_path)],
    )
    assert batch.main() == 0
    assert calls == ["refresh_dcf_assumptions.py", "build_redesigned_dcf.py"]


def test_capture_affordance_survives_missing_report_database() -> None:
    from pipeline.you_said import render_you_said_strip_for_path

    html = render_you_said_strip_for_path(None, "test")
    assert "No decision on file for TEST" in html
    assert "Capture a decision" in html
    assert 'data-capture-ticker="TEST"' in html


def test_supplied_override_connection_requires_schema() -> None:
    with (
        sqlite3.connect(":memory:") as conn,
        pytest.raises(sqlite3.OperationalError, match="fact_overrides"),
    ):
        apply_overrides([], ticker="TEST", dim_type="product", conn=conn)


@pytest.mark.parametrize("source", ["explicit", "environment", "context"])
def test_checkout_database_is_rejected_regardless_of_configuration(
    source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import db
    from db_paths import db_path_context, require_db_path

    forbidden = ROOT / "data" / "portfolio.db"
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(forbidden))
    monkeypatch.setattr(db, "DB_PATH", str(forbidden))
    with (
        db_path_context(forbidden if source == "context" else None),
        pytest.raises(RuntimeError, match="checkout"),
    ):
        require_db_path(forbidden if source == "explicit" else None)


def test_explicit_fixture_named_portfolio_database_is_allowed(tmp_path: Path) -> None:
    from db_paths import require_db_path

    fixture = tmp_path / "data" / "portfolio.db"
    fixture.parent.mkdir()
    sqlite3.connect(fixture).close()
    assert require_db_path(fixture) == fixture


def test_artifact_cli_propagates_database_to_subprocesses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from execution import build_artifacts

    database = tmp_path / "external.sqlite"
    sqlite3.connect(database).close()
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", "")
    for name in ("PROJECT_ROOT", "DB_PATH", "DATA_DIR", "FMP_DIR"):
        monkeypatch.setattr(build_artifacts.db, name, getattr(build_artifacts.db, name))
    monkeypatch.setattr(
        build_artifacts,
        "_parse_args",
        lambda: SimpleNamespace(repo_root=tmp_path, db_path=database),
    )

    def no_tickers(*_: object) -> list[str]:
        return []

    monkeypatch.setattr(build_artifacts, "_resolve_tickers", no_tickers)
    assert build_artifacts.main() == 0
    assert os.environ["EARNINGS_SUMMARY_DB_PATH"] == str(database)
