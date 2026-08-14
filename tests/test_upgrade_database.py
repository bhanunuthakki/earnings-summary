from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
import upgrade_database as upgrade_database_module
from upgrade_database import ACTIVE_HEAD, UpgradeDatabaseError, upgrade_database

from execution import portfolio_readiness_receipt as readiness_module

ROOT = Path(__file__).resolve().parents[1]


def _authoritative_runtime(
    monkeypatch: pytest.MonkeyPatch,
    runtime_root: Path,
) -> None:
    monkeypatch.setattr(
        upgrade_database_module,
        "authoritative_managed_runtime_root",
        lambda: runtime_root.resolve(),
    )


def test_upgrade_requires_safe_sqlite_before_touching_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "must-not-exist.db"

    def unsafe_runtime() -> None:
        raise RuntimeError("unsafe SQLite test sentinel")

    monkeypatch.setattr(
        upgrade_database_module,
        "require_safe_sqlite_writer_runtime",
        unsafe_runtime,
    )
    with pytest.raises(RuntimeError, match="unsafe SQLite test sentinel"):
        upgrade_database(db_path, repo_root=ROOT, runtime_root=ROOT, allow_isolated_db=True)
    assert not db_path.exists()


def test_upgrade_classifies_database_only_after_lock_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "locked-classification.db"
    db_path.touch()
    lock_held = False

    @contextmanager
    def fake_lock(*_args: object, **_kwargs: object) -> Generator[None]:
        nonlocal lock_held
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    def revisions_only_under_lock(_db_path: Path) -> tuple[str, ...]:
        assert lock_held
        return (ACTIVE_HEAD,)

    monkeypatch.setattr(upgrade_database_module, "hold_run_lock", fake_lock)
    monkeypatch.setattr(upgrade_database_module, "_read_revisions", revisions_only_under_lock)

    def accept_integrity(_path: Path) -> None:
        return None

    monkeypatch.setattr(upgrade_database_module, "_integrity_check", accept_integrity)

    receipt = upgrade_database(
        db_path,
        repo_root=ROOT,
        runtime_root=ROOT,
        allow_isolated_db=True,
    )

    assert receipt.status == "already_current"


def test_live_upgrade_requires_phase0_receipt_inside_shared_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "portfolio.db"
    db_path.touch()
    lock_held = False

    @contextmanager
    def fake_lock(*_args: object, **_kwargs: object) -> Generator[None]:
        nonlocal lock_held
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    monkeypatch.setattr(upgrade_database_module, "hold_run_lock", fake_lock)

    runtime_root = tmp_path / "runtime"
    _authoritative_runtime(monkeypatch, runtime_root)

    def canonical_db(root: Path) -> Path:
        assert root == runtime_root.resolve()
        return db_path

    def prior_revision(_path: Path) -> tuple[str, ...]:
        return (upgrade_database_module.OPERATION_EVENTS_CONTRACT_REVISION,)

    monkeypatch.setattr(upgrade_database_module, "portfolio_db_path", canonical_db)
    monkeypatch.setattr(
        upgrade_database_module,
        "_read_revisions",
        prior_revision,
    )

    with pytest.raises(UpgradeDatabaseError, match="Phase-0 backup/restore"):
        upgrade_database(db_path, repo_root=ROOT, runtime_root=runtime_root)

    assert lock_held is False


def test_live_upgrade_revalidates_phase0_receipt_while_lock_is_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "portfolio.db"
    db_path.touch()
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text("{}", encoding="utf-8")
    lock_held = False
    runtime_root = tmp_path / "runtime"
    _authoritative_runtime(monkeypatch, runtime_root)
    fetch_events: list[str] = []

    @contextmanager
    def fake_lock(*_args: object, **_kwargs: object) -> Generator[None]:
        nonlocal lock_held
        lock_held = True
        try:
            yield
        finally:
            lock_held = False

    class _Blocked:
        ready = False
        blocking_reasons = ("test_block",)

    origin = readiness_module.OriginMainObservation(
        sha="a" * 40,
        fetched_at=datetime.now(UTC),
    )

    def fetch_before_lock(root: Path) -> readiness_module.OriginMainObservation:
        assert lock_held is False
        assert root == ROOT.resolve()
        fetch_events.append("fetch")
        return origin

    def collect_under_lock(**kwargs: object) -> _Blocked:
        assert lock_held is True
        assert fetch_events == ["fetch"]
        assert kwargs["mode"] == "migration"
        assert kwargs["runtime_root"] == runtime_root.resolve()
        resolver = kwargs["origin_resolver"]
        assert callable(resolver)
        assert resolver(ROOT.resolve()) == origin
        return _Blocked()

    monkeypatch.setattr(upgrade_database_module, "hold_run_lock", fake_lock)

    def canonical_db(root: Path) -> Path:
        assert root == runtime_root.resolve()
        return db_path

    def prior_revision(_path: Path) -> tuple[str, ...]:
        return (upgrade_database_module.OPERATION_EVENTS_CONTRACT_REVISION,)

    monkeypatch.setattr(upgrade_database_module, "portfolio_db_path", canonical_db)
    monkeypatch.setattr(
        upgrade_database_module,
        "_read_revisions",
        prior_revision,
    )
    monkeypatch.setattr(readiness_module, "collect_readiness", collect_under_lock)
    monkeypatch.setattr(readiness_module, "fetch_origin_main", fetch_before_lock)

    with pytest.raises(UpgradeDatabaseError, match="test_block"):
        upgrade_database(
            db_path,
            repo_root=ROOT,
            runtime_root=runtime_root,
            phase0_backup_restore_receipt=receipt_path,
        )

    assert lock_held is False


def test_explicit_database_outside_runtime_requires_isolated_opt_in(tmp_path: Path) -> None:
    db_path = tmp_path / "explicit.db"
    runtime_root = tmp_path / "runtime"

    with pytest.raises(UpgradeDatabaseError, match="does not match the runtime database"):
        upgrade_database(db_path, repo_root=ROOT, runtime_root=runtime_root)

    assert not db_path.exists()


def test_candidate_runtime_cannot_relabel_authoritative_live_db_as_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authoritative_runtime = tmp_path / "managed-runtime"
    candidate_runtime = tmp_path / "candidate-worktree"
    live_db = authoritative_runtime / "data" / "portfolio.db"
    live_db.parent.mkdir(parents=True)
    live_db.write_bytes(b"live-database-sentinel")
    before = live_db.read_bytes()
    monkeypatch.delenv("EARNINGS_SUMMARY_DB_PATH", raising=False)
    _authoritative_runtime(monkeypatch, authoritative_runtime)

    with pytest.raises(UpgradeDatabaseError, match="authoritative managed runtime"):
        upgrade_database(
            live_db,
            repo_root=ROOT,
            runtime_root=candidate_runtime,
            allow_isolated_db=True,
        )

    assert live_db.read_bytes() == before


def test_candidate_runtime_cannot_relabel_environment_configured_live_db_as_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authoritative_runtime = tmp_path / "managed-runtime"
    candidate_runtime = tmp_path / "candidate-worktree"
    live_db = tmp_path / "configured-live.db"
    live_db.write_bytes(b"configured-live-database-sentinel")
    candidate_alias = tmp_path / "candidate-visible-alias.db"
    os.link(live_db, candidate_alias)
    before = live_db.read_bytes()
    monkeypatch.setenv("EARNINGS_SUMMARY_DB_PATH", str(live_db))
    _authoritative_runtime(monkeypatch, authoritative_runtime)

    with pytest.raises(UpgradeDatabaseError, match="authoritative managed runtime"):
        upgrade_database(
            candidate_alias,
            repo_root=ROOT,
            runtime_root=candidate_runtime,
            allow_isolated_db=True,
        )

    assert live_db.read_bytes() == before


def _revision(db_path: Path) -> str:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0])


def test_upgrade_database_creates_fresh_db_and_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.db"

    created = upgrade_database(
        db_path,
        repo_root=ROOT,
        runtime_root=ROOT,
        allow_isolated_db=True,
    )
    repeated = upgrade_database(
        db_path,
        repo_root=ROOT,
        runtime_root=ROOT,
        allow_isolated_db=True,
    )

    assert created.status == "created"
    assert repeated.status == "already_current"
    assert _revision(db_path) == ACTIVE_HEAD


def test_upgrade_database_bridges_archived_revision_with_verified_backup(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy.db"
    upgrade_database(
        db_path,
        repo_root=ROOT,
        runtime_root=ROOT,
        allow_isolated_db=True,
    )
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("UPDATE alembic_version SET version_num='0273_post_earnings_readout_budget'")
        conn.commit()
    finally:
        conn.close()
    backup_path = tmp_path / "before.db"

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "execution" / "sqlite_bootstrap.py"),
            str(ROOT / "execution" / "upgrade_database.py"),
            "--db-path",
            str(db_path),
            "--repo-root",
            str(ROOT),
            "--runtime-root",
            str(ROOT),
            "--allow-isolated-db",
            "--backup-path",
            str(backup_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)

    assert receipt["status"] == "bridged"
    assert receipt["backup_path"] == str(backup_path.resolve())
    assert _revision(db_path) == ACTIVE_HEAD
    assert _revision(backup_path) == "0273_post_earnings_readout_budget"


def test_archived_bridge_rejects_closed_detail_lookalike_before_revision_mutation(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "lookalike-closed-journal.db"
    upgrade_database(
        db_path,
        repo_root=ROOT,
        runtime_root=ROOT,
        allow_isolated_db=True,
    )
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            DROP TRIGGER trg_operation_events_no_update;
            DROP TRIGGER trg_operation_events_no_delete;
            DROP INDEX ix_operation_events_operation_id;
            DROP TABLE operation_events;
            CREATE TABLE operation_events (
                event_id TEXT NOT NULL PRIMARY KEY,
                operation_id TEXT NOT NULL,
                event_kind TEXT NOT NULL,
                event_sha256 TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                status TEXT,
                exit_code INTEGER,
                severity TEXT,
                detail_code TEXT,
                detail_reason TEXT,
                CONSTRAINT ck_lookalike_detail CHECK (
                    detail_reason IS NULL OR detail_reason='terminal_detail_withheld'
                )
            );
            UPDATE alembic_version
            SET version_num='0273_post_earnings_readout_budget';
            """
        )
        conn.commit()
    finally:
        conn.close()
    backup_path = tmp_path / "lookalike.before.db"

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "execution" / "sqlite_bootstrap.py"),
            str(ROOT / "execution" / "upgrade_database.py"),
            "--db-path",
            str(db_path),
            "--repo-root",
            str(ROOT),
            "--runtime-root",
            str(ROOT),
            "--allow-isolated-db",
            "--backup-path",
            str(backup_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "operation_events" in result.stderr
    assert _revision(db_path) == "0273_post_earnings_readout_budget"
    assert _revision(backup_path) == "0273_post_earnings_readout_budget"


def test_managed_wrapper_upgrades_exact_0010_to_0013_with_backup_and_closed_journal_schema(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = migrated_db(tmp_path / "exact-0010.db", target="0010_add_rehearsal_io_indexes")
    backup_path = tmp_path / "exact-0010.before.db"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "execution" / "sqlite_bootstrap.py"),
            str(ROOT / "execution" / "upgrade_database.py"),
            "--db-path",
            str(db_path),
            "--repo-root",
            str(ROOT),
            "--runtime-root",
            str(ROOT),
            "--allow-isolated-db",
            "--backup-path",
            str(backup_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "upgraded"
    assert receipt["from_revision"] == "0010_add_rehearsal_io_indexes"
    assert receipt["to_revision"] == ACTIVE_HEAD
    assert receipt["backup_path"] == str(backup_path.resolve())
    assert _revision(db_path) == ACTIVE_HEAD
    assert _revision(backup_path) == "0010_add_rehearsal_io_indexes"
    conn = sqlite3.connect(db_path)
    try:
        assert {row[0] for row in conn.execute("SELECT name FROM sqlite_master")} >= {
            "operation_requests",
            "operation_events",
            "ix_llm_calls_trace_id_called_at",
        }
        event_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='operation_events'"
        ).fetchone()[0]
        assert "terminal_detail_withheld" in event_sql
    finally:
        conn.close()


def test_managed_upgrade_rejects_weaker_preexisting_same_column_journal_table(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = migrated_db(tmp_path / "weak-journal.db", target="0010_add_rehearsal_io_indexes")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE operation_requests (
                operation_id TEXT PRIMARY KEY,
                idempotency_key_sha256 TEXT NOT NULL UNIQUE,
                request_sha256 TEXT NOT NULL,
                actor TEXT NOT NULL,
                job_name TEXT NOT NULL,
                trigger_kind TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                scope_json TEXT NOT NULL,
                command_sha256 TEXT NOT NULL,
                write_sets_json TEXT NOT NULL,
                requested_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "execution" / "sqlite_bootstrap.py"),
            str(ROOT / "execution" / "upgrade_database.py"),
            "--db-path",
            str(db_path),
            "--repo-root",
            str(ROOT),
            "--runtime-root",
            str(ROOT),
            "--allow-isolated-db",
            "--backup-path",
            str(tmp_path / "weak-journal.before.db"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "operation_requests constraints do not match migration 0011" in result.stderr
    assert _revision(db_path) == "0010_add_rehearsal_io_indexes"


def test_managed_upgrade_rejects_partial_preexisting_owned_llm_index(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = migrated_db(tmp_path / "partial-llm-index.db", target="0010_add_rehearsal_io_indexes")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE INDEX ix_llm_calls_trace_id_called_at "
            "ON llm_calls(trace_id,called_at) WHERE trace_id IS NOT NULL"
        )
        conn.commit()
    finally:
        conn.close()
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "execution" / "sqlite_bootstrap.py"),
            str(ROOT / "execution" / "upgrade_database.py"),
            "--db-path",
            str(db_path),
            "--repo-root",
            str(ROOT),
            "--runtime-root",
            str(ROOT),
            "--allow-isolated-db",
            "--backup-path",
            str(tmp_path / "partial-llm-index.before.db"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "ix_llm_calls_trace_id_called_at does not match migration 0011" in result.stderr
    assert _revision(db_path) == "0010_add_rehearsal_io_indexes"


def test_upgrade_database_rejects_nonempty_unversioned_db(tmp_path: Path) -> None:
    db_path = tmp_path / "unknown.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE operator_data(id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(UpgradeDatabaseError, match="refusing to guess"):
        upgrade_database(
            db_path,
            repo_root=ROOT,
            runtime_root=ROOT,
            allow_isolated_db=True,
        )


def test_upgrade_database_rejects_foreign_key_corruption(tmp_path: Path) -> None:
    db_path = tmp_path / "foreign-key-corrupt.db"
    upgrade_database(
        db_path,
        repo_root=ROOT,
        runtime_root=ROOT,
        allow_isolated_db=True,
    )
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            """
            INSERT INTO alerts
                (user_id, ticker, trigger_kind, fired_at, evidence_json, signature_sha)
            VALUES ('missing-tenant', 'TEST', 'material_news',
                    '2026-08-08T00:00:00', '{}', 'fk-corruption-test')
            """
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(UpgradeDatabaseError, match="foreign_key_check failed"):
        upgrade_database(
            db_path,
            repo_root=ROOT,
            runtime_root=ROOT,
            allow_isolated_db=True,
        )


def test_upgrade_database_cli_emits_valid_json_receipt(tmp_path: Path) -> None:
    db_path = tmp_path / "cli.db"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "execution" / "sqlite_bootstrap.py"),
            str(ROOT / "execution" / "upgrade_database.py"),
            "--db-path",
            str(db_path),
            "--repo-root",
            str(ROOT),
            "--runtime-root",
            str(ROOT),
            "--allow-isolated-db",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "created"
    assert payload["to_revision"] == ACTIVE_HEAD
