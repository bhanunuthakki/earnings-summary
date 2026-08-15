"""Hermetic unit tests for sealed offline build boundary and preflight checks."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from pipeline.offline_build_boundary import (
    DependencyPreflightCheck,
    OfflineBuildBoundary,
    OfflineInputManifest,
    compute_db_wal_hash,
)


def test_frozen_contracts_immutability() -> None:
    preflight = DependencyPreflightCheck(
        passed=True,
        ticker="TEST",
        as_of_date=date(2026, 3, 31),
        checked_files=("data/portfolio.db",),
        code_commit_sha="a" * 40,
        database_integrity_status="OK",
        canary_manifest_verified=True,
        violations=(),
        checked_at=datetime.now(UTC),
    )
    with pytest.raises(ValidationError):
        preflight.passed = False  # type: ignore[misc]

    manifest = OfflineInputManifest(
        ticker="TEST",
        as_of_date=date(2026, 3, 31),
        code_commit_sha="a" * 40,
        database_wal_sha256="b" * 64,
        canary_manifest_sha256="c" * 64,
        output_dir="/tmp/test",
        manifest_hash="d" * 64,
    )
    with pytest.raises(ValidationError):
        manifest.ticker = "MUTATE"  # type: ignore[misc]


def test_compute_db_wal_hash(tmp_path: Path) -> None:
    db_file = tmp_path / "test.db"
    assert compute_db_wal_hash(db_file) != ""

    # Create sqlite file and verify hash stability
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE t (id INT, val TEXT)")
    conn.execute("INSERT INTO t VALUES (1, 'hello')")
    conn.commit()
    conn.close()

    h1 = compute_db_wal_hash(db_file)
    h2 = compute_db_wal_hash(db_file)
    assert h1 == h2
    assert len(h1) == 64


def test_preflight_on_mock_repo(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    data_dir = repo_root / "data"
    data_dir.mkdir()
    db_path = data_dir / "portfolio.db"

    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE companies (ticker TEXT PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO companies VALUES ('TEST', 'Test Corp')")
    conn.commit()

    boundary = OfflineBuildBoundary(repo_root=repo_root)
    preflight = boundary.run_preflight("TEST", date(2026, 3, 31), conn=conn)

    assert preflight.passed
    assert preflight.ticker == "TEST"
    assert preflight.database_integrity_status == "OK"
    assert len(preflight.violations) == 0

    conn.close()


def test_preflight_fails_on_missing_canary_file(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    data_dir = repo_root / "data"
    data_dir.mkdir()
    db_path = data_dir / "portfolio.db"

    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE companies (ticker TEXT PRIMARY KEY, name TEXT)")
    conn.commit()

    # Create a manifest declaring a missing file for WIX
    canary_manifest = data_dir / "fmp_canary_manifest.json"
    manifest_data = {
        "files": [
            {
                "ticker": "WIX",
                "filename": "missing_wix_file.json",
                "sha256": "0" * 64,
            }
        ]
    }
    canary_manifest.write_text(json.dumps(manifest_data), encoding="utf-8")

    boundary = OfflineBuildBoundary(repo_root=repo_root)
    preflight = boundary.run_preflight("WIX", date(2026, 3, 31), conn=conn)

    assert not preflight.passed
    assert any("Missing 1 sealed canary files for WIX" in v for v in preflight.violations)
    conn.close()


def test_offline_build_determinism_and_receipt_emission(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    data_dir = repo_root / "data"
    data_dir.mkdir()
    db_path = data_dir / "portfolio.db"
    output_dir = tmp_path / "output"

    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE companies (ticker TEXT PRIMARY KEY, name TEXT)")
    conn.commit()

    boundary = OfflineBuildBoundary(repo_root=repo_root)

    # Monkeypatch render_artifacts to return deterministic strings
    def mock_render(ticker: str, repo: Path, conn: sqlite3.Connection | None = None) -> tuple[str, str, str]:
        html = f"<html><body>Report for {ticker}</body></html>"
        md = f"# Report for {ticker}"
        sections = json.dumps({"ticker": ticker, "sections": []})
        return html, md, sections

    boundary.render_artifacts = mock_render  # type: ignore[assignment]

    receipt = boundary.execute_sealed_build(
        ticker="TEST",
        as_of_date=date(2026, 3, 31),
        output_dir=output_dir,
        verify_determinism=True,
        conn=conn,
    )

    assert receipt.ticker == "TEST"
    assert receipt.as_of_date == date(2026, 3, 31)
    assert receipt.deterministic_two_pass_verified
    assert len(receipt.emitted_files) == 4

    # Verify all files written
    for fp_str in receipt.emitted_files:
        assert Path(fp_str).exists()

    receipt_json = json.loads(Path(output_dir / "2026-03-31_offline_receipt.json").read_text(encoding="utf-8"))
    assert receipt_json["build_id"] == receipt.build_id
    assert receipt_json["html_sha256"] == receipt.html_sha256

    conn.close()
