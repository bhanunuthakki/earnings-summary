from __future__ import annotations

import hashlib
import json
import socket
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from comments import load_store
from compute import company_description, platform_diagram, segment_definitions, valuation_basis
from report.builder import build_report
from report.offline_artifact import (
    DependencyClass,
    DependencyRecord,
    OfflineArtifactPayload,
    OfflineBoundaryError,
    UnclassifiedDependencyError,
    classify_dependency,
    offline_runtime_guard,
    snapshot_database,
    write_offline_artifact,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_cache_loaders_do_not_create_directories_on_read(tmp_path: Path) -> None:
    assert company_description.load_description(tmp_path, "NU") is None
    assert platform_diagram.load_diagram(tmp_path, "NU") is None
    assert valuation_basis.load(tmp_path, "NU") is None
    assert segment_definitions.load_definitions(tmp_path, "NU") == {}
    assert load_store(tmp_path, "NU", date(2026, 8, 1)).comments == []

    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/report/builder.py", DependencyClass.CODE),
        ("data/portfolio.db", DependencyClass.DATABASE),
        ("data/portfolio.db-wal", DependencyClass.DATABASE_WAL),
        ("data/portfolio.db-shm", DependencyClass.DATABASE_SHM),
        ("data/holdings/NU.json", DependencyClass.PORTFOLIO),
        ("data/historical/fmp/NU_price_chart.json", DependencyClass.PRICE),
        ("data/estimates/NU.json", DependencyClass.ESTIMATE),
        ("data/company_description/NU.json", DependencyClass.FILESYSTEM),
        ("config/report.json", DependencyClass.CONFIG),
        ("dcf/NU.xlsx", DependencyClass.DCF),
        ("policies/report.json", DependencyClass.POLICY),
    ],
)
def test_offline_dependency_classifier_is_fail_closed(path: str, expected: DependencyClass) -> None:
    assert classify_dependency(Path(path), ticker="NU") is expected


def test_offline_dependency_classifier_rejects_unknown_inputs() -> None:
    with pytest.raises(UnclassifiedDependencyError, match="unclassified offline dependency"):
        classify_dependency(Path("data/mystery/NU.bin"), ticker="NU")


def test_database_snapshot_binds_db_and_wal_without_mutating_source(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    conn = sqlite3.connect(source)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE facts (value TEXT NOT NULL)")
    conn.execute("INSERT INTO facts VALUES ('sealed')")
    conn.commit()
    assert source.with_name("source.db-wal").is_file()

    before = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (source, source.with_name("source.db-wal"), source.with_name("source.db-shm"))
        if path.is_file()
    }
    destination = tmp_path / "isolated" / "portfolio.db"
    records = snapshot_database(source, destination)
    after = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (source, source.with_name("source.db-wal"), source.with_name("source.db-shm"))
        if path.is_file()
    }
    conn.close()

    assert before == after
    assert {record.dependency_class for record in records} >= {
        DependencyClass.DATABASE,
        DependencyClass.DATABASE_WAL,
    }
    isolated = sqlite3.connect(f"{destination.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    assert isolated.execute("SELECT value FROM facts").fetchone() == ("sealed",)
    isolated.close()


def test_offline_guard_denies_network_llm_subprocess_and_non_output_writes(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    with offline_runtime_guard(output_dir):
        with pytest.raises(OfflineBoundaryError, match="network"):
            socket.socket().connect(("127.0.0.1", 9))
        with pytest.raises(OfflineBoundaryError, match="subprocess"):
            subprocess.run(["claude", "--version"], check=False)
        with pytest.raises(OfflineBoundaryError, match="write outside"):
            (tmp_path / "cache.json").write_text("forbidden", encoding="utf-8")
        (output_dir / "allowed.json").write_text("{}", encoding="utf-8")


def _payload() -> OfflineArtifactPayload:
    return OfflineArtifactPayload(
        html="<html>\r\n<body>sealed</body>\r\n</html>\r\n",
        markdown="# Sealed\r\n",
        sections={"ticker": "NU", "generation_date": "2026-08-01"},
        status={"snapshot": "ok"},
        numeric_provenance={"valuation": {"source": "dcf_run", "fact_id": 7}},
    )


def test_fixed_as_of_artifact_is_byte_identical_and_receipt_is_immutable(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    dependencies: list[DependencyRecord] = []
    write_offline_artifact(
        output_dir=first,
        ticker="NU",
        as_of=date(2026, 8, 1),
        payload=_payload(),
        dependencies=dependencies,
    )
    write_offline_artifact(
        output_dir=second,
        ticker="NU",
        as_of=date(2026, 8, 1),
        payload=_payload(),
        dependencies=dependencies,
    )

    names = {
        "report.html",
        "report.md",
        "sections.json",
        "status.json",
        "numeric_provenance.json",
        "receipt.json",
    }
    assert {path.name for path in first.iterdir()} == names
    assert {name: (first / name).read_bytes() for name in names} == {
        name: (second / name).read_bytes() for name in names
    }
    receipt = json.loads((first / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["as_of"] == "2026-08-01"
    assert receipt["network_attempts"] == 0
    assert receipt["llm_attempts"] == 0
    assert receipt["canonical_mutations"] == []

    (first / "report.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(OfflineBoundaryError, match="immutable offline artifact differs"):
        write_offline_artifact(
            output_dir=first,
            ticker="NU",
            as_of=date(2026, 8, 1),
            payload=_payload(),
            dependencies=dependencies,
        )


def test_report_builder_accepts_explicit_generation_date(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # All section builders tolerate an empty staged repository. The explicit
    # date is the only new behavior; the default remains date.today().
    def no_suppressed_sections(_ticker: str, _repo_root: Path) -> set[str]:
        return set()

    monkeypatch.setattr("report.builder.suppressed_sections_for_ticker", no_suppressed_sections)
    spec = build_report("NU", tmp_path, generation_date=date(2026, 8, 1))
    assert spec.generation_date == date(2026, 8, 1)


def test_offline_cli_repeats_byte_identically_from_isolated_snapshot(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "portfolio.db")
    source_sha256 = hashlib.sha256(database.read_bytes()).hexdigest()
    outputs = (tmp_path / "artifact-a", tmp_path / "artifact-b")

    for output in outputs:
        completed = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "execution" / "build_offline_artifact.py"),
                "--ticker",
                "NU",
                "--repo-root",
                str(PROJECT_ROOT),
                "--database",
                str(database),
                "--output-dir",
                str(output),
                "--as-of",
                "2026-08-01",
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr

    assert hashlib.sha256(database.read_bytes()).hexdigest() == source_sha256
    first = {path.name: path.read_bytes() for path in outputs[0].iterdir()}
    second = {path.name: path.read_bytes() for path in outputs[1].iterdir()}
    assert first == second
