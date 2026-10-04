"""Retained tools must honor configured state without creating checkout state."""

from __future__ import annotations

import os
import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from db_paths import db_path_context
from runtime.python_process import managed_python_argv
from sources import registry
from sources.telemetry import SourceAttemptMeasurement

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "script",
    [
        "extract_company_description.py",
        "extract_platform_diagram.py",
        "extract_segment_definitions.py",
    ],
)
def test_extractor_child_uses_parent_database_target(
    script: str, tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    database = migrated_db(tmp_path / "retained" / "facts.db")
    result = subprocess.run(
        managed_python_argv(
            ROOT,
            f"execution/{script}",
            "--all",
            "--repo-root",
            str(artifacts),
        ),
        env={**os.environ, "EARNINGS_SUMMARY_DB_PATH": str(database)},
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"
    assert not (artifacts / "data" / "portfolio.db").exists()


def test_logical_and_physical_calls_use_same_configured_database(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    database = migrated_db(tmp_path / "retained" / "facts.db")
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(database))
    monkeypatch.setattr(registry, "_DB_PATH", ROOT / "data" / "portfolio.db")
    registry.log_call(
        source_name="synthetic",
        kind="financial_statement",
        ticker="TEST",
        status=registry.CallStatus.OK,
    )
    assert registry.log_http_measurement(
        SourceAttemptMeasurement(
            run_id="synthetic-run",
            provider="synthetic",
            endpoint="https://fixture.invalid/financials",
            latency_ms=7,
            retry_count=0,
            status="ok",
        )
    )
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_regime_measurements").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM source_calls WHERE source_name='synthetic' AND kind='financial_statement'"
            ).fetchone()[0]
            == 1
        )
    summary = registry.summarize_source_calls()
    assert any(
        item.source_name == "synthetic" and item.kind == "financial_statement" for item in summary
    )


def test_missing_explicit_telemetry_target_does_not_fall_back(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    database = migrated_db(tmp_path / "retained" / "facts.db")
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(database))
    monkeypatch.setattr(registry, "_DB_PATH", tmp_path / "missing.db")
    registry.log_call(
        source_name="synthetic", kind="financial_statement", ticker="TEST", status="ok"
    )
    assert registry.summarize_source_calls() == []
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_calls").fetchone()[0] == 0
    assert not (tmp_path / "missing.db").exists()


def test_scoped_extractor_database_owns_both_log_kinds(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = migrated_db(tmp_path / "configured.db")
    selected = migrated_db(tmp_path / "selected.db")
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(configured))
    monkeypatch.setattr(registry, "_DB_PATH", ROOT / "data" / "portfolio.db")
    with db_path_context(selected):
        registry.log_call(
            source_name="scoped", kind="financial_statement", ticker="TEST", status="ok"
        )
        assert registry.log_http_measurement(
            SourceAttemptMeasurement(
                run_id="scoped-fixture",
                provider="scoped",
                endpoint="https://fixture.invalid",
                latency_ms=1,
                retry_count=0,
                status="ok",
            )
        )
    with sqlite3.connect(configured) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_calls").fetchone()[0] == 0
    with sqlite3.connect(selected) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_calls").fetchone()[0] == 2


def test_seed_grader_receives_exact_database_and_artifact_root(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    import backfill_seed_decisions as entrypoint

    database = migrated_db(tmp_path / "retained" / "facts.db")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    seed = tmp_path / "seed.json"
    seed.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "backfill_seed_decisions",
            "--repo-root",
            str(artifacts),
            "--db-path",
            str(database),
            "--seed-json",
            str(seed),
        ],
    )
    writes: list[Path] = []
    launches: list[list[str]] = []

    def seed_rows(target: Path, _seed: Path) -> dict[str, int]:
        writes.append(target)
        return {"inserted": 1}

    def grade(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        launches.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(entrypoint, "backfill_seed_decisions", seed_rows)
    monkeypatch.setattr(entrypoint.subprocess, "run", grade)
    assert entrypoint.main() == 0
    assert writes == [database]
    assert launches[0][-4:] == ["--db-path", str(database), "--repo-root", str(artifacts)]
    assert str(ROOT / "execution" / "grade_decisions.py") in launches[0]
    assert not (artifacts / "data" / "portfolio.db").exists()


def test_extractor_without_database_authority_stops_before_work(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    environment = dict(os.environ)
    environment.pop("EARNINGS_SUMMARY_DB_PATH", None)
    result = subprocess.run(
        managed_python_argv(
            ROOT, "execution/extract_company_description.py", "--all", "--repo-root", str(artifacts)
        ),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 1
    assert "checkout-default portfolio database is prohibited" in result.stderr
    assert not (artifacts / "data" / "portfolio.db").exists()
