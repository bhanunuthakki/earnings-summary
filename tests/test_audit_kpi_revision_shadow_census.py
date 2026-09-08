"""CLI contract for the non-authorizing KPI revision shadow census."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

import compute.kpi_revision_shadow_census as census_module
import execution.audit_kpi_revision_shadow_census as census_cli
from compute.kpi_revision_shadow_census import (
    KpiRevisionShadowCensus,
    SnapshotEvidenceState,
    verify_snapshot_evidence,
)
from sqlite_snapshot import SnapshotManifest, SnapshotRequest, create_snapshot

STAMP = datetime(2026, 9, 7, 12, tzinfo=UTC)


def _reader_snapshot(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source.db"
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE alembic_version(version_num TEXT NOT NULL)")
    conn.execute("INSERT INTO alembic_version VALUES ('test-head')")
    conn.execute("CREATE TABLE marker(value TEXT NOT NULL)")
    conn.execute("INSERT INTO marker VALUES ('unchanged')")
    conn.commit()
    conn.close()
    snapshot = tmp_path / "reader.db"
    result = create_snapshot(SnapshotRequest(source_path=source, destination_path=snapshot))
    return snapshot, result.manifest_path


def test_cli_emits_one_read_only_hold_receipt_for_unverified_snapshot(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "uncertified-snapshot.db"
    conn = sqlite3.connect(database)
    conn.execute("CREATE TABLE marker(value TEXT NOT NULL)")
    conn.execute("INSERT INTO marker VALUES ('unchanged')")
    conn.commit()
    conn.close()
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    assert (
        census_cli.main(
            [
                "--db-path",
                str(database),
                "--snapshot-manifest",
                str(tmp_path / "missing.manifest.json"),
                "--effective-at",
                STAMP.isoformat(),
                "--known-at",
                STAMP.isoformat(),
                "--evaluated-at",
                STAMP.isoformat(),
            ]
        )
        == 2
    )

    captured = capsys.readouterr()
    assert captured.out.count("\n") == 1
    payload = json.loads(captured.out)
    assert payload["activation_state"] == "hold"
    assert payload["authorizes_reader_activation"] is False
    assert payload["claims_consumer_parity"] is False
    assert payload["snapshot_evidence"]["status"] == "unverified"
    assert payload["deterministic_readiness"] == "blocked"
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_cli_help_runs_through_managed_sqlite_bootstrap_without_new_path_mutation() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            str(root / "execution" / "sqlite_bootstrap.py"),
            str(root / "execution" / "audit_kpi_revision_shadow_census.py"),
            "--help",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--snapshot-manifest" in result.stdout
    assert "--known-at" in result.stdout


def test_snapshot_evidence_rejects_forbidden_checkout_path_before_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbidden = Path(census_module.__file__).resolve().parents[2] / "data" / "portfolio.db"

    def unexpected_read(path: Path) -> str:
        raise AssertionError(f"forbidden path was read: {path}")

    monkeypatch.setattr(census_module, "_file_sha256", unexpected_read)
    with pytest.raises(RuntimeError, match="Mac checkout database is prohibited"):
        verify_snapshot_evidence(
            database_path=forbidden,
            manifest_path=forbidden.with_suffix(".manifest.json"),
        )


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("schema_version", "snapshot_manifest_schema_unsupported"),
        ("code_config_version", "snapshot_manifest_code_unsupported"),
    ],
)
def test_snapshot_evidence_rejects_unsupported_manifest_contract(
    tmp_path: Path,
    field: str,
    reason: str,
) -> None:
    snapshot, manifest_path = _reader_snapshot(tmp_path)
    payload = SnapshotManifest.model_validate_json(manifest_path.read_bytes()).model_dump(
        mode="json"
    )
    payload[field] = "unsupported/v9"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    evidence = verify_snapshot_evidence(
        database_path=snapshot,
        manifest_path=manifest_path,
    )

    assert evidence.status == "unverified"
    assert reason in evidence.blocking_reasons


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_snapshot_evidence_rejects_nonempty_sqlite_sidecars(
    tmp_path: Path,
    suffix: str,
) -> None:
    snapshot, manifest_path = _reader_snapshot(tmp_path)
    snapshot.with_name(snapshot.name + suffix).write_bytes(b"active-sidecar")

    evidence = verify_snapshot_evidence(
        database_path=snapshot,
        manifest_path=manifest_path,
    )

    assert evidence.status == "unverified"
    expected = "snapshot_has_nonempty_wal" if suffix == "-wal" else "snapshot_has_shm_sidecar"
    assert expected in evidence.blocking_reasons


def test_cli_rejects_snapshot_mutation_during_census(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot, manifest_path = _reader_snapshot(tmp_path)
    actual_audit = census_cli.audit_kpi_revision_shadow_census

    def audit_then_mutate(
        conn: sqlite3.Connection,
        *,
        effective_at: datetime,
        known_at: datetime,
        evaluated_at: datetime,
        snapshot_evidence: SnapshotEvidenceState,
    ) -> KpiRevisionShadowCensus:
        result = actual_audit(
            conn,
            effective_at=effective_at,
            known_at=known_at,
            evaluated_at=evaluated_at,
            snapshot_evidence=snapshot_evidence,
        )
        snapshot.write_bytes(snapshot.read_bytes() + b"changed")
        return result

    monkeypatch.setattr(census_cli, "audit_kpi_revision_shadow_census", audit_then_mutate)
    with pytest.raises(RuntimeError, match="snapshot identity changed"):
        census_cli.main(
            [
                "--db-path",
                str(snapshot),
                "--snapshot-manifest",
                str(manifest_path),
                "--effective-at",
                STAMP.isoformat(),
                "--known-at",
                STAMP.isoformat(),
                "--evaluated-at",
                STAMP.isoformat(),
            ]
        )
    assert capsys.readouterr().out == ""
