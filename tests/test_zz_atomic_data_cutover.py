"""Atomic, recoverable activation contracts for the final SQLite cutover."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast

import pytest

import provenance.atomic_cutover as atomic_cutover
from execution.activate_data_cutover import main
from provenance.atomic_cutover import (
    ActivationMode,
    ActivationRequest,
    ActivationRolledBackError,
    AtomicCutoverError,
    QuiescenceReceipt,
    activate_data_cutover,
    activation_payload_sha256,
    canonical_quiescence_json,
    quiescence_payload_sha256,
)

HEAD = "0260_pre_earnings_brief_plumbing"


class _VerifyDatabase(Protocol):
    def __call__(
        self,
        path: Path,
        *,
        expected_head: str,
    ) -> atomic_cutover.DatabaseVerification: ...


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _database(path: Path, *, value: str, revision: str = HEAD) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY);
            CREATE TABLE parent_rows (id INTEGER PRIMARY KEY);
            CREATE TABLE payload (
                id INTEGER PRIMARY KEY,
                value TEXT NOT NULL,
                parent_id INTEGER REFERENCES parent_rows(id)
            );
            INSERT INTO parent_rows VALUES (1);
            """
        )
        connection.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
        connection.execute("INSERT INTO payload VALUES (1, ?, 1)", (value,))
        connection.commit()
    finally:
        connection.close()


def _quiescence_receipt(live: Path, *, valid: bool = True) -> QuiescenceReceipt:
    captured_at = datetime.now(UTC)
    payload = {
        "schema_version": "1",
        "captured_at": captured_at.isoformat(),
        "valid_until": (captured_at + timedelta(minutes=5)).isoformat(),
        "live_database": str(live.resolve()),
        "live_database_sha256": _sha256(live),
        "expected_task_paths": [
            r"\earnings-summary\daily_fetch_and_brief",
            r"\earnings-summary\run_morning_pipeline",
        ],
        "tasks": [
            {
                "path": r"\earnings-summary\daily_fetch_and_brief",
                "state": "Disabled",
                "enabled": False,
            },
            {
                "path": r"\earnings-summary\run_morning_pipeline",
                "state": "Disabled",
                "enabled": False,
            },
        ],
        "expected_service_names": ["es-poller"],
        "services": [{"name": "es-poller", "state": "Stopped"}],
        "expected_listener_endpoints": ["127.0.0.1:7421", "127.0.0.1:8000"],
        "listeners": [
            {"host": "127.0.0.1", "port": 7421, "listening": False, "pid": None},
            {"host": "127.0.0.1", "port": 8000, "listening": False, "pid": None},
        ],
        "receipt_sha256": "0" * 64,
    }
    if not valid:
        payload["tasks"] = payload["tasks"][:-1]
    receipt = QuiescenceReceipt.model_validate_json(json.dumps(payload))
    return receipt.model_copy(update={"receipt_sha256": quiescence_payload_sha256(receipt)})


def _request(
    tmp_path: Path,
    *,
    mode: ActivationMode,
) -> tuple[ActivationRequest, Path, Path, Path, Path]:
    repo_root = tmp_path / "repo"
    live = repo_root / "data" / "portfolio.db"
    candidate = tmp_path / "candidate.db"
    rollback = repo_root / "data" / "portfolio.pre-cutover.db"
    failed_candidate = repo_root / "data" / "portfolio.failed-candidate.db"
    receipt_path = repo_root / "data" / "portfolio.activation-receipt.json"
    quiescence_path = tmp_path / "quiescence.json"
    _database(live, value="old-live")
    _database(candidate, value="new-candidate")
    quiescence = _quiescence_receipt(live)
    quiescence_path.write_text(canonical_quiescence_json(quiescence), encoding="utf-8")
    request = ActivationRequest(
        repo_root=repo_root,
        live_database=live,
        candidate_database=candidate,
        rollback_database=rollback,
        failed_candidate_database=failed_candidate,
        receipt_path=receipt_path,
        quiescence_receipt_path=quiescence_path,
        expected_quiescence_receipt_sha256=quiescence.receipt_sha256,
        expected_live_sha256=_sha256(live),
        expected_candidate_sha256=_sha256(candidate),
        expected_alembic_head=HEAD,
        mode=mode,
    )
    return request, live, candidate, rollback, failed_candidate


def _payload_value(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute("SELECT value FROM payload").fetchone()
    finally:
        connection.close()
    assert row is not None
    return str(row[0])


def test_dry_run_is_strictly_read_only(tmp_path: Path) -> None:
    request, live, candidate, rollback, failed_candidate = _request(
        tmp_path,
        mode=ActivationMode.DRY_RUN,
    )
    live_before = live.read_bytes()
    candidate_before = candidate.read_bytes()

    receipt = activate_data_cutover(request)

    assert receipt.mode is ActivationMode.DRY_RUN
    assert receipt.status == "ready"
    assert receipt.live_sha256_before == request.expected_live_sha256
    assert receipt.candidate_sha256 == request.expected_candidate_sha256
    assert live.read_bytes() == live_before
    assert candidate.read_bytes() == candidate_before
    assert not rollback.exists()
    assert not failed_candidate.exists()
    assert not request.receipt_path.exists()
    assert not (live.parent / ".job_locks").exists()


def test_refuses_stale_hash_before_mutation(tmp_path: Path) -> None:
    request, live, candidate, rollback, _failed_candidate = _request(
        tmp_path,
        mode=ActivationMode.APPLY,
    )
    request = request.model_copy(update={"expected_live_sha256": "f" * 64})

    with pytest.raises(AtomicCutoverError, match="live database SHA-256"):
        activate_data_cutover(request)

    assert _payload_value(live) == "old-live"
    assert _payload_value(candidate) == "new-candidate"
    assert not rollback.exists()


@pytest.mark.parametrize(
    "database_name,sidecar_suffix",
    [("live", "-wal"), ("candidate", "-shm"), ("candidate", "-journal")],
)
def test_refuses_sqlite_sidecars(
    tmp_path: Path,
    database_name: str,
    sidecar_suffix: str,
) -> None:
    request, live, candidate, rollback, _failed_candidate = _request(
        tmp_path,
        mode=ActivationMode.APPLY,
    )
    selected = live if database_name == "live" else candidate
    Path(f"{selected}{sidecar_suffix}").write_bytes(b"active sqlite sidecar")

    with pytest.raises(AtomicCutoverError, match="SQLite sidecar"):
        activate_data_cutover(request)

    assert _payload_value(live) == "old-live"
    assert _payload_value(candidate) == "new-candidate"
    assert not rollback.exists()


def test_refuses_invalid_or_unreviewed_quiescence_receipt(tmp_path: Path) -> None:
    request, live, candidate, rollback, _failed_candidate = _request(
        tmp_path,
        mode=ActivationMode.APPLY,
    )
    with pytest.raises(AtomicCutoverError, match="reviewed commitment"):
        activate_data_cutover(
            request.model_copy(update={"expected_quiescence_receipt_sha256": "e" * 64})
        )

    invalid = _quiescence_receipt(live, valid=False)
    request.quiescence_receipt_path.write_text(
        canonical_quiescence_json(invalid),
        encoding="utf-8",
    )
    request = request.model_copy(
        update={"expected_quiescence_receipt_sha256": invalid.receipt_sha256}
    )

    with pytest.raises(AtomicCutoverError, match="task inventory"):
        activate_data_cutover(request)

    assert _payload_value(live) == "old-live"
    assert _payload_value(candidate) == "new-candidate"
    assert not rollback.exists()


def test_refuses_path_alias_existing_rollback_and_cross_volume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, live, candidate, rollback, _failed_candidate = _request(
        tmp_path,
        mode=ActivationMode.DRY_RUN,
    )

    with pytest.raises(AtomicCutoverError, match="distinct"):
        activate_data_cutover(request.model_copy(update={"candidate_database": live}))

    rollback.write_bytes(b"occupied")
    with pytest.raises(AtomicCutoverError, match="rollback destination already exists"):
        activate_data_cutover(request)
    rollback.unlink()

    def split_volume(path: Path) -> str:
        return "candidate-volume" if path.resolve() == candidate.resolve() else "live-volume"

    monkeypatch.setattr(atomic_cutover, "_volume_identity", split_volume)
    with pytest.raises(AtomicCutoverError, match="same filesystem volume"):
        activate_data_cutover(request)


def test_refuses_database_with_an_untracked_hardlink_alias(tmp_path: Path) -> None:
    request, _live, candidate, _rollback, _failed_candidate = _request(
        tmp_path,
        mode=ActivationMode.DRY_RUN,
    )
    alias = tmp_path / "candidate-alias.db"
    try:
        os.link(candidate, alias)
    except OSError as exc:
        pytest.skip(f"filesystem does not support hard links: {exc}")

    with pytest.raises(AtomicCutoverError, match="multiple filesystem links"):
        activate_data_cutover(request)


def test_successful_switch_preserves_exact_rollback_and_receipt(tmp_path: Path) -> None:
    request, live, candidate, rollback, failed_candidate = _request(
        tmp_path,
        mode=ActivationMode.APPLY,
    )

    receipt = activate_data_cutover(request)

    assert receipt.status == "activated"
    assert receipt.activation_mechanism == (
        "windows_replace_file" if os.name == "nt" else "portable_rename_pair"
    )
    assert _payload_value(live) == "new-candidate"
    assert _payload_value(rollback) == "old-live"
    assert not candidate.exists()
    assert not failed_candidate.exists()
    assert _sha256(live) == request.expected_candidate_sha256
    assert _sha256(rollback) == request.expected_live_sha256
    persisted = json.loads(request.receipt_path.read_text(encoding="utf-8"))
    assert persisted["receipt_sha256"] == receipt.receipt_sha256
    assert persisted["status"] == "activated"
    assert activation_payload_sha256(receipt) == receipt.receipt_sha256


def test_apply_refuses_when_portfolio_lock_targets_another_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, live, candidate, rollback, _failed_candidate = _request(
        tmp_path,
        mode=ActivationMode.APPLY,
    )
    monkeypatch.setenv(
        "EARNINGS_SUMMARY_DB_PATH",
        str(tmp_path / "different-live.db"),
    )

    with pytest.raises(AtomicCutoverError, match="JobLock resolves to a different database"):
        activate_data_cutover(request)

    assert _payload_value(live) == "old-live"
    assert _payload_value(candidate) == "new-candidate"
    assert not rollback.exists()


def test_postcheck_failure_restores_live_and_preserves_failed_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, live, candidate, rollback, failed_candidate = _request(
        tmp_path,
        mode=ActivationMode.APPLY,
    )
    original_verify = cast(
        "_VerifyDatabase",
        getattr(atomic_cutover, "_verify_database"),
    )

    def fail_installed_candidate(path: Path, *, expected_head: str):
        if path.resolve() == live.resolve() and rollback.exists():
            raise AtomicCutoverError("forced active-database postcheck failure")
        return original_verify(path, expected_head=expected_head)

    monkeypatch.setattr(atomic_cutover, "_verify_database", fail_installed_candidate)

    with pytest.raises(ActivationRolledBackError) as exc_info:
        activate_data_cutover(request)

    assert exc_info.value.receipt.status == "rolled_back"
    assert _payload_value(live) == "old-live"
    assert not rollback.exists()
    assert not candidate.exists()
    assert _payload_value(failed_candidate) == "new-candidate"
    assert _sha256(live) == request.expected_live_sha256
    assert _sha256(failed_candidate) == request.expected_candidate_sha256
    persisted = json.loads(request.receipt_path.read_text(encoding="utf-8"))
    assert persisted["status"] == "rolled_back"
    assert persisted["failed_candidate_database"] == str(failed_candidate.resolve())


def test_candidate_head_and_foreign_keys_are_strict_gates(tmp_path: Path) -> None:
    request, live, candidate, rollback, _failed_candidate = _request(
        tmp_path,
        mode=ActivationMode.DRY_RUN,
    )
    connection = sqlite3.connect(candidate)
    try:
        connection.execute("UPDATE alembic_version SET version_num = 'wrong_head'")
        connection.commit()
    finally:
        connection.close()
    stale_candidate_hash = _sha256(candidate)
    request = request.model_copy(update={"expected_candidate_sha256": stale_candidate_hash})

    with pytest.raises(AtomicCutoverError, match="Alembic head"):
        activate_data_cutover(request)

    connection = sqlite3.connect(candidate)
    try:
        connection.execute("UPDATE alembic_version SET version_num = ?", (HEAD,))
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("UPDATE payload SET parent_id = 999")
        connection.commit()
    finally:
        connection.close()
    request = request.model_copy(update={"expected_candidate_sha256": _sha256(candidate)})

    with pytest.raises(AtomicCutoverError, match="foreign-key"):
        activate_data_cutover(request)

    assert _payload_value(live) == "old-live"
    assert not rollback.exists()


def test_cli_dry_run_emits_canonical_receipt_without_filesystem_mutation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request, live, candidate, rollback, failed_candidate = _request(
        tmp_path,
        mode=ActivationMode.DRY_RUN,
    )

    assert (
        main(
            [
                "--repo-root",
                str(request.repo_root),
                "--live-db",
                str(live),
                "--candidate-db",
                str(candidate),
                "--rollback-db",
                str(rollback),
                "--failed-candidate-db",
                str(failed_candidate),
                "--receipt-path",
                str(request.receipt_path),
                "--quiescence-receipt",
                str(request.quiescence_receipt_path),
                "--expected-quiescence-receipt-sha256",
                request.expected_quiescence_receipt_sha256,
                "--expected-live-sha256",
                request.expected_live_sha256,
                "--expected-candidate-sha256",
                request.expected_candidate_sha256,
                "--expected-alembic-head",
                request.expected_alembic_head,
            ]
        )
        == 0
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["mode"] == "dry-run"
    assert payload["status"] == "ready"
    assert json.loads(captured.err)["event"] == "data_cutover_activation_ready"
    assert _payload_value(live) == "old-live"
    assert _payload_value(candidate) == "new-candidate"
    assert not rollback.exists()
    assert not request.receipt_path.exists()
