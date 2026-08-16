from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "execution"))

import rehearse_data_backbone as cli  # noqa: E402
from upgrade_database import ACTIVE_HEAD  # noqa: E402

from provenance import data_backbone_rehearsal as rehearsal  # noqa: E402


def _write_db(path: Path, *, value: str = "owner-note") -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY);
            INSERT INTO alembic_version VALUES ('0008_add_fmp_recovery');
            CREATE TABLE tracked_companies (
                ticker TEXT PRIMARY KEY,
                notes TEXT NOT NULL
            );
            """
        )
        conn.execute("INSERT INTO tracked_companies VALUES ('META', ?)", (value,))
        conn.commit()
    finally:
        conn.close()


def _write_wal_mode_restored_db(path: Path, *, value: str = "owner-note") -> None:
    """Create a sidecar-free restored snapshot whose database header is WAL-mode."""
    active = path.with_name(f".{path.name}.active")
    conn = sqlite3.connect(active)
    try:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        conn.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY);
            INSERT INTO alembic_version VALUES ('0008_add_fmp_recovery');
            CREATE TABLE tracked_companies (
                ticker TEXT PRIMARY KEY,
                notes TEXT NOT NULL
            );
            """
        )
        conn.execute("INSERT INTO tracked_companies VALUES ('META', ?)", (value,))
        conn.commit()
        assert conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)
        shutil.copyfile(active, path)
    finally:
        conn.close()
        active.unlink(missing_ok=True)
        Path(f"{active}-wal").unlink(missing_ok=True)
        Path(f"{active}-shm").unlink(missing_ok=True)
    assert path.read_bytes()[18:20] == b"\x02\x02"
    assert not any(Path(f"{path}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal"))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_copy_corpus_rejects_symlink_and_leaves_destination_absent(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    try:
        (source / "escape.json").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    destination = tmp_path / "copy"

    with pytest.raises(rehearsal.RehearsalError, match="unsafe corpus entry"):
        rehearsal.copy_corpus_verified(source, destination)

    assert not destination.exists()


def test_manifest_rejects_symlink_root(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    linked_root = tmp_path / "linked-root"
    try:
        linked_root.symlink_to(source, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")

    with pytest.raises(rehearsal.RehearsalError, match="unsafe corpus entry"):
        rehearsal.build_corpus_manifest(linked_root)


def test_copy_corpus_rejects_reparse_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    item = source / "entry.json"
    item.write_text("{}", encoding="utf-8")

    def classify_reparse(stat_result: os.stat_result) -> bool:
        del stat_result
        return True

    monkeypatch.setattr(rehearsal, "is_reparse_point", classify_reparse)
    with pytest.raises(rehearsal.RehearsalError, match="unsafe corpus entry"):
        rehearsal.build_corpus_manifest(source)


def test_copy_corpus_detects_source_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    item = source / "META-income-statement.json"
    item.write_text('{"revenue":1}', encoding="utf-8")
    before = rehearsal.build_corpus_manifest(source)

    def mutate_then_copy(source_file: Path, destination_file: Path) -> None:
        source_file.write_text('{"revenue":2}', encoding="utf-8")
        destination_file.parent.mkdir(parents=True, exist_ok=True)
        destination_file.write_bytes(source_file.read_bytes())

    destination = tmp_path / "copy"
    with pytest.raises(rehearsal.RehearsalError, match="source corpus changed"):
        rehearsal.copy_corpus_verified(
            source,
            destination,
            expected_source=before,
            copy_file=mutate_then_copy,
        )
    assert destination.exists()


def test_copy_corpus_detects_copy_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    item = source / "META-income-statement.json"
    item.write_text('{"revenue":1}', encoding="utf-8")

    def corrupt_copy(source_file: Path, destination_file: Path) -> None:
        destination_file.parent.mkdir(parents=True, exist_ok=True)
        destination_file.write_bytes(source_file.read_bytes())
        destination_file.write_text("corrupt", encoding="utf-8")

    destination = tmp_path / "copy"
    with pytest.raises(rehearsal.RehearsalError, match="copied corpus differs"):
        rehearsal.copy_corpus_verified(source, destination, copy_file=corrupt_copy)
    assert destination.exists()


def test_table_commitment_detects_preservation_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    candidate = tmp_path / "candidate.db"
    _write_db(source)
    _write_db(candidate, value="changed")

    before = rehearsal.build_table_commitments(source)
    after = rehearsal.build_table_commitments(candidate)

    with pytest.raises(rehearsal.RehearsalError, match="preservation commitment mismatch"):
        rehearsal.require_equal_commitments(before, after)


def test_table_commitments_explicitly_record_absent_owner_tables(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    _write_db(database)

    commitments = {item.table_name: item for item in rehearsal.build_table_commitments(database)}

    assert commitments["tracked_companies"].present is True
    assert commitments["position_sizing_intent"].present is False
    assert commitments["positioning_intents"].present is False


def test_database_verification_rejects_foreign_key_violation(tmp_path: Path) -> None:
    database = tmp_path / "broken.db"
    conn = sqlite3.connect(database)
    try:
        conn.executescript(
            """
            PRAGMA foreign_keys=OFF;
            CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY);
            INSERT INTO alembic_version VALUES ('0008_add_fmp_recovery');
            CREATE TABLE parent (id INTEGER PRIMARY KEY);
            CREATE TABLE child (parent_id INTEGER REFERENCES parent(id));
            INSERT INTO child VALUES (42);
            """
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(rehearsal.RehearsalError, match="foreign-key"):
        rehearsal.verify_database(database, expected_head="0008_add_fmp_recovery")


def test_offline_receipt_rejects_network_or_manifest_gap() -> None:
    valid = {
        "run_id": "offline-corpus:9e4a714d-72be-4a4a-a8c1-747266be2098",
        "status": "DEGRADED_CORPUS",
        "discovered_file_count": 1,
        "selected_count": 1,
        "admitted_count": 1,
        "admitted_new_count": 1,
        "already_applied_count": 0,
        "eligible_count": 1,
        "corpus_count": 1,
        "failed_count": 0,
        "deferred_count": 0,
        "excluded_by_tier_count": 0,
        "skipped_count": 0,
        "pending_count": 1,
        "manifest_sha256": "a" * 64,
        "manifest_before_sha256": "a" * 64,
        "manifest_after_sha256": "a" * 64,
        "manifest_unchanged": True,
        "network_calls": 0,
        "mode": "offline_corpus_only",
        "exit_code": 2,
    }

    rehearsal.validate_offline_receipt(
        json.dumps(valid), return_code=2, copied_manifest_sha="a" * 64
    )
    with pytest.raises(rehearsal.RehearsalError, match="network_calls"):
        rehearsal.validate_offline_receipt(
            json.dumps({**valid, "network_calls": 1}),
            return_code=2,
            copied_manifest_sha="a" * 64,
        )
    with pytest.raises(rehearsal.RehearsalError, match="manifest"):
        rehearsal.validate_offline_receipt(
            json.dumps(valid), return_code=2, copied_manifest_sha="b" * 64
        )
    with pytest.raises(rehearsal.RehearsalError, match="terminal code"):
        rehearsal.validate_offline_receipt(
            json.dumps(valid), return_code=1, copied_manifest_sha="a" * 64
        )
    with pytest.raises(rehearsal.RehearsalError, match=r"pending.*selected"):
        rehearsal.validate_offline_receipt(
            json.dumps({**valid, "pending_count": 0}),
            return_code=2,
            copied_manifest_sha="a" * 64,
        )
    # A global backlog count would contaminate the receipt with rows that this
    # offline replay did not select. The terminal gate accepts only its exact
    # selected corpus obligations.
    with pytest.raises(rehearsal.RehearsalError, match=r"pending.*selected"):
        rehearsal.validate_offline_receipt(
            json.dumps({**valid, "pending_count": 2}),
            return_code=2,
            copied_manifest_sha="a" * 64,
        )
    with pytest.raises(rehearsal.RehearsalError, match="selected work arithmetic"):
        rehearsal.validate_offline_receipt(
            json.dumps(
                {
                    **valid,
                    "admitted_count": 0,
                    "admitted_new_count": 0,
                    "corpus_count": 0,
                }
            ),
            return_code=2,
            copied_manifest_sha="a" * 64,
        )


def test_forced_post_swap_failure_restores_throwaway_live(tmp_path: Path) -> None:
    live = tmp_path / "rehearsal-live.db"
    candidate = tmp_path / "candidate.db"
    _write_db(live, value="old")
    _write_db(candidate, value="new")
    live_sha = _sha(live)
    candidate_sha = _sha(candidate)

    with pytest.raises(rehearsal.SwapRehearsalRolledBackError, match="forced post-swap failure"):
        rehearsal.exercise_swap_and_rollback(
            tmp_path, live, candidate, force_post_swap_failure=True
        )

    assert _sha(live) == live_sha
    failed_candidates = tuple(tmp_path.glob("rehearsal-failed-candidate*.db"))
    assert len(failed_candidates) == 1
    assert _sha(failed_candidates[0]) == candidate_sha


def test_swap_rejects_paths_outside_explicit_rehearsal_root(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    live = tmp_path / "outside-live.db"
    candidate = tmp_path / "outside-candidate.db"
    _write_db(live)
    _write_db(candidate)

    with pytest.raises(rehearsal.RehearsalError, match="escapes explicit rehearsal root"):
        rehearsal.exercise_swap_and_rollback(allowed, live, candidate)


def test_online_backup_does_not_write_source_paths(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    candidate = tmp_path / "work" / "candidate.db"
    _write_db(source)
    before = _sha(source)

    rehearsal.online_backup_read_only(source, candidate)

    assert _sha(source) == before
    assert candidate.exists()
    assert not Path(f"{source}-wal").exists()
    assert not Path(f"{source}-shm").exists()


def test_online_backup_uses_immutable_connection_for_sidecar_free_wal_snapshot(
    tmp_path: Path,
) -> None:
    source = tmp_path / "restored-wal.db"
    candidate = tmp_path / "work" / "candidate.db"
    _write_wal_mode_restored_db(source)
    before = rehearsal.database_storage_identity(source)

    rehearsal.online_backup_read_only(source, candidate)

    assert rehearsal.database_storage_identity(source) == before
    assert not any(Path(f"{source}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal"))
    with sqlite3.connect(candidate) as connection:
        assert connection.execute("SELECT notes FROM tracked_companies").fetchone() == (
            "owner-note",
        )


def test_online_backup_refuses_real_wal_before_opening_or_mutating_shm(tmp_path: Path) -> None:
    source = tmp_path / "active.db"
    connection = sqlite3.connect(source)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute("CREATE TABLE active_rows (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO active_rows VALUES (1)")
        connection.commit()
        wal = Path(f"{source}-wal")
        shm = Path(f"{source}-shm")
        assert wal.exists() and shm.exists()
        shm_before = (shm.read_bytes(), shm.stat().st_mtime_ns, shm.stat().st_ctime_ns)

        with pytest.raises(rehearsal.RehearsalError, match="closed restored snapshot"):
            rehearsal.online_backup_read_only(source, tmp_path / "candidate.db")

        assert (shm.read_bytes(), shm.stat().st_mtime_ns, shm.stat().st_ctime_ns) == shm_before
        assert not (tmp_path / "candidate.db").exists()
    finally:
        connection.close()


def test_online_backup_detects_source_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.db"
    _write_db(source)
    real_sha = rehearsal.sha256_file
    observations = 0

    def changing_sha(path: Path) -> str:
        nonlocal observations
        observations += 1
        if path == source and observations > 1:
            return "f" * 64
        return real_sha(path)

    monkeypatch.setattr(rehearsal, "sha256_file", changing_sha)
    with pytest.raises(rehearsal.RehearsalError, match="source database changed"):
        rehearsal.online_backup_read_only(source, tmp_path / "candidate.db")


def test_database_storage_identity_binds_every_sqlite_sidecar_and_stat(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _write_db(source)
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{source}{suffix}").write_bytes(suffix.encode("ascii"))

    identity = rehearsal.database_storage_identity(source)

    assert tuple(entry.suffix for entry in identity.entries) == ("", "-wal", "-shm", "-journal")
    assert all(entry.modified_at_ns > 0 for entry in identity.entries)
    assert all(entry.change_at_ns > 0 for entry in identity.entries)


def test_cli_refuses_receipt_inside_source_corpus(tmp_path: Path) -> None:
    source_db = tmp_path / "source.db"
    source_corpus = tmp_path / "corpus"
    source_corpus.mkdir()
    _write_db(source_db)

    exit_code = cli.main(
        [
            "--repo-root",
            str(cli.PROJECT_ROOT),
            "--source-db",
            str(source_db),
            "--source-corpus",
            str(source_corpus),
            "--work-dir",
            str(tmp_path / "work"),
            "--receipt-path",
            str(source_corpus / "receipt.json"),
        ]
    )

    assert exit_code == 1
    assert not (source_corpus / "receipt.json").exists()


def test_cli_refuses_work_or_receipt_aliasing_database_sidecars(tmp_path: Path) -> None:
    source_db = tmp_path / "source.db"
    source_corpus = tmp_path / "corpus"
    source_corpus.mkdir()
    _write_db(source_db)
    failure_receipt = tmp_path / "failure.json"

    exit_code = cli.main(
        [
            "--repo-root",
            str(cli.PROJECT_ROOT),
            "--source-db",
            str(source_db),
            "--source-corpus",
            str(source_corpus),
            "--work-dir",
            f"{source_db}-wal",
            "--receipt-path",
            str(failure_receipt),
        ]
    )

    assert exit_code == 1
    failed = rehearsal.RehearsalFailureReceipt.model_validate_json(failure_receipt.read_text())
    assert failed.status == "failed"

    sidecar = Path(f"{source_db}-journal")
    sidecar.write_bytes(b"do-not-overwrite")
    exit_code = cli.main(
        [
            "--repo-root",
            str(cli.PROJECT_ROOT),
            "--source-db",
            str(source_db),
            "--source-corpus",
            str(source_corpus),
            "--work-dir",
            str(tmp_path / "work"),
            "--receipt-path",
            str(sidecar),
        ]
    )
    assert exit_code == 1
    assert sidecar.read_bytes() == b"do-not-overwrite"


def test_failure_destination_canonicalizes_lexical_parent_alias(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    source = tmp_path / "source.db"
    _write_db(source)
    args = argparse.Namespace(
        receipt_path=tmp_path / "source.db-wal",
        source_db=nested / ".." / "source.db",
        source_corpus=tmp_path / "corpus",
        work_dir=tmp_path / "work",
    )

    destination = cli.failure_destination(args)

    assert destination != Path(f"{source}-wal")
    assert not Path(f"{source}-wal").exists()


def test_failure_destination_canonicalizes_reparse_parent_alias(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    _write_db(source)
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")
    args = argparse.Namespace(
        receipt_path=alias / "source.db-shm",
        source_db=source,
        source_corpus=tmp_path / "corpus",
        work_dir=tmp_path / "work",
    )

    destination = cli.failure_destination(args)

    assert destination != Path(f"{source}-shm")
    assert not Path(f"{source}-shm").exists()


def _offline_receipt(manifest_sha: str) -> rehearsal.OfflineTerminalReceipt:
    return rehearsal.OfflineTerminalReceipt.model_validate(
        {
            "run_id": "offline-corpus:9e4a714d-72be-4a4a-a8c1-747266be2098",
            "status": "DEGRADED_CORPUS",
            "discovered_file_count": 1,
            "selected_count": 1,
            "admitted_count": 1,
            "admitted_new_count": 1,
            "already_applied_count": 0,
            "eligible_count": 1,
            "corpus_count": 1,
            "failed_count": 0,
            "deferred_count": 0,
            "excluded_by_tier_count": 0,
            "skipped_count": 0,
            "pending_count": 1,
            "manifest_sha256": manifest_sha,
            "network_calls": 0,
            "manifest_before_sha256": manifest_sha,
            "manifest_after_sha256": manifest_sha,
            "manifest_unchanged": True,
            "mode": "offline_corpus_only",
            "exit_code": 2,
        }
    )


def _clean_code_identity() -> rehearsal.CodeIdentity:
    observed = cli.code_identity(cli.PROJECT_ROOT)
    return observed.model_copy(
        update={
            "worktree_clean": True,
            "porcelain_sha256": hashlib.sha256(b"").hexdigest(),
        }
    )


def test_run_upgrade_explicitly_authorizes_the_isolated_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "rehearsal-repo" / "data" / "portfolio.db"
    backup = tmp_path / "candidate-pre-upgrade.db"
    captured: list[str] = []
    expected = rehearsal.UpgradeTerminalReceipt(
        status="upgraded",
        db_path=str(candidate),
        from_revision="0017_add_owner_decision_checkpoints",
        to_revision=ACTIVE_HEAD,
        backup_path=str(backup),
        completed_at="2026-08-15T12:00:00+00:00",
    )

    def complete(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.extend(command)
        return subprocess.CompletedProcess(command, 0, expected.model_dump_json(), "")

    monkeypatch.setattr(cli.subprocess, "run", complete)

    run_upgrade = getattr(cli, "_run_upgrade")
    assert run_upgrade(ROOT, candidate, backup) == expected
    assert captured[captured.index("--runtime-root") + 1] == str(candidate.parents[1])
    assert "--allow-isolated-db" in captured


def test_cli_plan_writes_self_sealed_receipt(tmp_path: Path) -> None:
    source_db = tmp_path / "source.db"
    source_corpus = tmp_path / "corpus"
    source_corpus.mkdir()
    (source_corpus / "META-income-statement.json").write_text("{}", encoding="utf-8")
    _write_db(source_db)
    receipt_path = tmp_path / "plan.json"

    exit_code = cli.main(
        [
            "--repo-root",
            str(cli.PROJECT_ROOT),
            "--source-db",
            str(source_db),
            "--source-corpus",
            str(source_corpus),
            "--work-dir",
            str(tmp_path / "future-work"),
            "--receipt-path",
            str(receipt_path),
        ]
    )

    assert exit_code == 0
    receipt = rehearsal.RehearsalReceipt.model_validate_json(receipt_path.read_text())
    assert receipt.status == "planned"
    assert receipt.code_identity.manifest_sha256
    assert receipt.source_schema_before == "0008_add_fmp_recovery"
    assert not (tmp_path / "future-work").exists()
    payload = json.loads(receipt_path.read_text())
    payload["receipt_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="seal"):
        rehearsal.RehearsalReceipt.model_validate_json(json.dumps(payload))


def test_cli_apply_refuses_dirty_code_identity_before_creating_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_db = tmp_path / "source.db"
    source_corpus = tmp_path / "corpus"
    source_corpus.mkdir()
    _write_db(source_db)
    dirty = _clean_code_identity().model_copy(update={"worktree_clean": False})

    def dirty_identity(repo_root: Path) -> rehearsal.CodeIdentity:
        del repo_root
        return dirty

    monkeypatch.setattr(cli, "code_identity", dirty_identity)
    work_dir = tmp_path / "work"
    failure_path = tmp_path / "failure.json"

    exit_code = cli.main(
        [
            "--repo-root",
            str(cli.PROJECT_ROOT),
            "--source-db",
            str(source_db),
            "--source-corpus",
            str(source_corpus),
            "--work-dir",
            str(work_dir),
            "--receipt-path",
            str(failure_path),
            "--apply-rehearsal",
        ]
    )

    assert exit_code == 1
    assert not work_dir.exists()
    failed = rehearsal.RehearsalFailureReceipt.model_validate_json(failure_path.read_text())
    assert "clean" in failed.failure_detail


def test_cli_apply_keeps_exact_sources_and_records_throwaway_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_db = tmp_path / "source.db"
    source_corpus = tmp_path / "corpus"
    source_corpus.mkdir()
    (source_corpus / "META-income-statement.json").write_text("{}", encoding="utf-8")
    _write_wal_mode_restored_db(source_db)
    source_storage = rehearsal.database_storage_identity(source_db)
    source_manifest = rehearsal.build_corpus_manifest(source_corpus)

    def no_op_upgrade(
        repo_root: Path, candidate: Path, backup: Path
    ) -> rehearsal.UpgradeTerminalReceipt:
        del repo_root, backup
        connection = sqlite3.connect(candidate)
        try:
            connection.execute("UPDATE alembic_version SET version_num=?", (ACTIVE_HEAD,))
            connection.commit()
        finally:
            connection.close()
        return rehearsal.UpgradeTerminalReceipt(
            status="upgraded",
            db_path=str(candidate),
            from_revision="0008_add_fmp_recovery",
            to_revision=ACTIVE_HEAD,
            backup_path=None,
            completed_at="2026-08-12T12:00:00+00:00",
        )

    monkeypatch.setattr(cli, "_run_upgrade", no_op_upgrade)

    def no_op_offline(
        repo_root: Path,
        rehearsal_root: Path,
        *,
        copied_manifest_sha: str,
    ) -> rehearsal.OfflineTerminalReceipt:
        del repo_root, rehearsal_root
        return _offline_receipt(copied_manifest_sha)

    monkeypatch.setattr(cli, "_run_offline_replay", no_op_offline)
    clean_identity = _clean_code_identity()

    def stable_identity(repo_root: Path) -> rehearsal.CodeIdentity:
        del repo_root
        return clean_identity

    monkeypatch.setattr(cli, "code_identity", stable_identity)
    real_connect = rehearsal.connect_sqlite
    source_roles: list[rehearsal.SQLiteConnectionRole] = []

    def observe_connection(
        path: str | os.PathLike[str],
        *,
        role: rehearsal.SQLiteConnectionRole,
        schema_preflight: bool | None = None,
    ) -> sqlite3.Connection:
        connection = real_connect(path, role=role, schema_preflight=schema_preflight)
        if Path(path).resolve() == source_db.resolve():
            source_roles.append(role)
            assert not any(
                Path(f"{source_db}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal")
            )
        return connection

    monkeypatch.setattr(rehearsal, "connect_sqlite", observe_connection)
    receipt_path = tmp_path / "apply.json"
    work_dir = tmp_path / "work"

    exit_code = cli.main(
        [
            "--repo-root",
            str(cli.PROJECT_ROOT),
            "--source-db",
            str(source_db),
            "--source-corpus",
            str(source_corpus),
            "--work-dir",
            str(work_dir),
            "--receipt-path",
            str(receipt_path),
            "--apply-rehearsal",
        ]
    )

    assert exit_code == 0
    receipt = rehearsal.RehearsalReceipt.model_validate_json(receipt_path.read_text())
    assert receipt.status == "passed"
    assert receipt.swap_rollback is not None and receipt.swap_rollback.rollback_restored
    assert receipt.forced_failure_rollback is not None
    assert receipt.forced_failure_rollback.rollback_restored
    assert source_roles == [rehearsal.SQLiteConnectionRole.QUIESCED_IMMUTABLE_READ_ONLY] * 4
    assert rehearsal.database_storage_identity(source_db) == source_storage
    assert rehearsal.build_corpus_manifest(source_corpus) == source_manifest


def test_cli_migration_failure_retains_evidence_with_failed_terminal_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_db = tmp_path / "source.db"
    source_corpus = tmp_path / "corpus"
    source_corpus.mkdir()
    (source_corpus / "META-income-statement.json").write_text("{}", encoding="utf-8")
    _write_db(source_db)
    work_dir = tmp_path / "work"
    receipt_path = tmp_path / "failed.json"

    def fail_upgrade(repo_root: Path, candidate: Path, backup: Path) -> object:
        del repo_root, candidate, backup
        raise rehearsal.RehearsalError("candidate migration failed")

    monkeypatch.setattr(cli, "_run_upgrade", fail_upgrade)
    clean_identity = _clean_code_identity()

    def stable_identity(repo_root: Path) -> rehearsal.CodeIdentity:
        del repo_root
        return clean_identity

    monkeypatch.setattr(cli, "code_identity", stable_identity)
    exit_code = cli.main(
        [
            "--repo-root",
            str(cli.PROJECT_ROOT),
            "--source-db",
            str(source_db),
            "--source-corpus",
            str(source_corpus),
            "--work-dir",
            str(work_dir),
            "--receipt-path",
            str(receipt_path),
            "--apply-rehearsal",
        ]
    )

    assert exit_code == 1
    assert work_dir.exists()
    failure = rehearsal.RehearsalFailureReceipt.model_validate_json(receipt_path.read_text())
    assert failure.status == "failed"
    assert failure.failure_type == "RehearsalError"
