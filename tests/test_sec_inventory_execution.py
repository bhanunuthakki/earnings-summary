"""Inventory apply entrypoint retains terminal status independently of stdout."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn, cast

import pytest
import requests

from execution import sync_sec_filing_inventory as sync
from filings.models import HardStopError, SourceContractError, TransientError
from provenance.inventory_identity import InventorySubject
from provenance.sec_execution import read_sec_executions


def test_inventory_apply_failure_is_durable_before_any_network(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    db = migrated_db(tmp_path / "inventory.db")
    monkeypatch.setattr(sync, "PROJECT_ROOT", tmp_path)

    def no_network(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("invalid identity must fail before network")

    monkeypatch.setattr(requests.Session, "get", no_network)
    result = sync.main(
        [
            "--db",
            str(db),
            "--ticker",
            "ACME",
            "--cik",
            "1",
            "--revision",
            "1",
            "--blob-root",
            str(tmp_path / "blobs"),
            "--apply",
        ]
    )
    assert result == 2
    with sqlite3.connect(db) as conn:
        receipt = read_sec_executions(conn, ticker="ACME")[0]
        assert receipt.state == "failed"
        assert receipt.result is not None and receipt.result.reason_code == "inventory_failed"
        assert conn.execute("SELECT COUNT(*) FROM source_inventory_snapshots").fetchone()[0] == 0


@pytest.mark.parametrize("apply", [False, True])
def test_sec_hard_stop_is_structured_nonretryable_and_never_exposes_contact(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    apply: bool,
) -> None:
    db = migrated_db(tmp_path / "inventory.db")
    monkeypatch.setattr(sync, "PROJECT_ROOT", tmp_path)

    def no_issuer_registry(_conn: sqlite3.Connection) -> bool:
        return False

    monkeypatch.setattr(sync, "issuer_registry_available", no_issuer_registry)
    monkeypatch.setattr(sync, "sec_user_agent", lambda: "synthetic contact@example.test")

    def blocked(*_args: object, **_kwargs: object) -> NoReturn:
        raise HardStopError("synthetic contact@example.test was denied")

    monkeypatch.setattr(sync, "_fetch", blocked)
    args = [
        "--db",
        str(db),
        "--ticker",
        "ACME",
        "--cik",
        "1",
        "--revision",
        "1",
        "--blob-root",
        str(tmp_path / "blobs"),
    ]
    if apply:
        args.append("--apply")
    assert sync.main(args) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "outcome": "blocked",
        "reason_code": "sec_inventory_hard_stop",
        "retryable": False,
    }
    assert "contact@example.test" not in captured.out + captured.err
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_inventory_snapshots").fetchone()[0] == 0
        if apply:
            receipt = read_sec_executions(conn, ticker="ACME")[0]
            assert receipt.state == "failed"


@pytest.mark.parametrize(
    ("source", "contact"),
    [
        ("process", "research\nbad@example.test"),
        ("file", "research\nbad@example.test"),
        ("process", "research 😀@example.test"),
        ("file", "research 😀@example.test"),
        ("process", "short"),
        ("file", "short"),
        ("process", "x" * 513),
        ("file", "x" * 513),
    ],
)
def test_invalid_contact_blocks_inventory_before_transport_without_disclosure(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source: str,
    contact: str,
) -> None:
    db = migrated_db(tmp_path / "inventory.db")
    monkeypatch.setattr(sync, "PROJECT_ROOT", tmp_path)

    def no_issuer_registry(_conn: sqlite3.Connection) -> bool:
        return False

    monkeypatch.setattr(sync, "issuer_registry_available", no_issuer_registry)
    env_file = tmp_path / "runtime.env"
    if source == "process":
        monkeypatch.setenv("EDGAR_USER_AGENT", contact)
        env_file.write_text('EDGAR_USER_AGENT="research valid@example.test"\n', encoding="utf-8")
    else:
        monkeypatch.delenv("EDGAR_USER_AGENT", raising=False)
        env_file.write_text(f'EDGAR_USER_AGENT="{contact}"\n', encoding="utf-8")
    monkeypatch.setenv("EARNINGS_SUMMARY_ENV_FILE", str(env_file))

    def no_network(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("invalid SEC contact must fail before network")

    monkeypatch.setattr(requests.Session, "get", no_network)
    assert sync.main(["--db", str(db), "--ticker", "ACME", "--cik", "1", "--revision", "1"]) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "outcome": "blocked",
        "reason_code": "sec_contact_configuration_invalid",
        "retryable": False,
    }
    assert contact not in captured.out + captured.err
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_inventory_snapshots").fetchone()[0] == 0


@pytest.mark.parametrize("error", [SourceContractError, TransientError, RuntimeError])
def test_other_inventory_errors_are_not_reported_as_sec_hard_stops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: type[Exception],
) -> None:
    def fail(_args: object) -> NoReturn:
        raise error("synthetic failure")

    monkeypatch.setattr(sync, "_run", fail)
    with pytest.raises(error):
        sync.main(
            [
                "--db",
                str(tmp_path / "unused.db"),
                "--ticker",
                "ACME",
                "--cik",
                "1",
                "--revision",
                "1",
            ]
        )
    assert capsys.readouterr().out == ""


def test_incompatible_timed_checkpoint_blocks_cli_before_source_network(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = migrated_db(tmp_path / "inventory.db")
    monkeypatch.setattr(sync, "PROJECT_ROOT", tmp_path)
    run_root = tmp_path / "checkpoint" / "0000000001"
    run_root.mkdir(parents=True)
    state_path = run_root / "state.v2.json"
    state = json.dumps(
        {
            "cik": "0000000001",
            "retrieval_config_sha256": "b" * 64,
            "collector_code_version": "sync-sec-filing-inventory@5",
            "entries": [],
        }
    )
    state_path.write_text(state, encoding="utf-8")

    def no_network(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("incompatible checkpoint must block before source network")

    monkeypatch.setattr(requests.Session, "get", no_network)
    assert (
        sync.main(
            [
                "--db",
                str(db),
                "--ticker",
                "ACME",
                "--cik",
                "1",
                "--revision",
                "1",
                "--package-checkpoint-root",
                str(tmp_path / "checkpoint"),
                "--apply",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "outcome": "blocked",
        "reason_code": "sec_inventory_checkpoint_invalid",
        "retryable": False,
    }
    assert str(run_root) not in captured.out + captured.err
    assert state_path.read_text(encoding="utf-8") == state
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM evidence_source_observations").fetchone()[0] == 0


@pytest.mark.parametrize("defect", ["missing", "tampered", "future"])
def test_invalid_timed_response_blocks_cli_before_source_network(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    defect: str,
) -> None:
    db = migrated_db(tmp_path / "inventory.db")
    monkeypatch.setattr(sync, "PROJECT_ROOT", tmp_path)
    run_root = tmp_path / "checkpoint" / "0000000001"
    response_root = run_root / "responses"
    response_root.mkdir(parents=True)
    index = b"valid index"
    manifest = b"valid manifest"
    index_sha = hashlib.sha256(index).hexdigest()
    manifest_sha = hashlib.sha256(manifest).hexdigest()
    (response_root / index_sha).write_bytes(index)
    if defect == "tampered":
        (response_root / manifest_sha).write_bytes(b"tampered manifest")
    elif defect == "future":
        (response_root / manifest_sha).write_bytes(manifest)
    before_manifest = (
        (response_root / manifest_sha).read_bytes()
        if (response_root / manifest_sha).exists()
        else None
    )
    past = datetime.now(UTC) - timedelta(days=1)
    manifest_clock = past + timedelta(days=2) if defect == "future" else past
    config_sha = hashlib.sha256(
        json.dumps(
            {
                "collector": cast(str, getattr(sync, "_COLLECTOR")),
                "timeout": cast(tuple[int, int], getattr(sync, "_TIMEOUT")),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    state_path = run_root / "state.v2.json"
    state = json.dumps(
        {
            "cik": "0000000001",
            "retrieval_config_sha256": config_sha,
            "collector_code_version": cast(str, getattr(sync, "_COLLECTOR")),
            "entries": [
                {
                    "accession_number": "0000000001-26-000001",
                    "index_sha256": index_sha,
                    "index_observed_at": past.isoformat(),
                    "index_retrieved_at": past.isoformat(),
                    "manifest_sha256": manifest_sha,
                    "manifest_observed_at": manifest_clock.isoformat(),
                    "manifest_retrieved_at": manifest_clock.isoformat(),
                }
            ],
        }
    )
    state_path.write_text(state, encoding="utf-8")

    def no_network(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("invalid retained response must block before source network")

    monkeypatch.setattr(requests.Session, "get", no_network)
    assert (
        sync.main(
            [
                "--db",
                str(db),
                "--ticker",
                "ACME",
                "--cik",
                "1",
                "--revision",
                "1",
                "--package-checkpoint-root",
                str(tmp_path / "checkpoint"),
                "--apply",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "outcome": "blocked",
        "reason_code": "sec_inventory_checkpoint_invalid",
        "retryable": False,
    }
    assert str(run_root) not in captured.out + captured.err
    assert state_path.read_text(encoding="utf-8") == state
    assert (response_root / index_sha).read_bytes() == index
    assert (
        (response_root / manifest_sha).read_bytes()
        if (response_root / manifest_sha).exists()
        else None
    ) == before_manifest
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM evidence_source_observations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM evidence_content_blobs").fetchone()[0] == 0


def test_inventory_success_receipt_binds_real_reconciled_snapshot(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    db = migrated_db(tmp_path / "inventory.db")
    monkeypatch.setattr(sync, "PROJECT_ROOT", tmp_path)

    # Identity resolution has its own contract suite. This focused integration
    # supplies one explicit synthetic approved identity; parsing, immutable byte
    # capture, reconciliation/seal and execution receipt remain real.
    def approved_identity(*_args: object, **_kwargs: object) -> InventorySubject:
        return InventorySubject(
            issuer_id="issuer-acme",
            ticker="ACME",
            source_url="https://data.sec.gov/submissions/CIK0000000001.json",
        )

    monkeypatch.setattr(sync, "resolve_sec_inventory_subject", approved_identity)
    monkeypatch.setattr(sync, "sec_user_agent", lambda: "synthetic test@example.test")
    response_at = datetime.now(UTC) - timedelta(seconds=10)
    recorded_at = response_at + timedelta(seconds=1)
    clocks = iter((response_at, recorded_at))
    monkeypatch.setattr(sync, "_utc_now", lambda: next(clocks))
    columns: dict[str, list[str]] = {
        key: []
        for key in (
            "accessionNumber",
            "filingDate",
            "reportDate",
            "acceptanceDateTime",
            "act",
            "form",
            "fileNumber",
            "filmNumber",
            "items",
            "size",
            "isXBRL",
            "isInlineXBRL",
            "primaryDocument",
            "primaryDocDescription",
        )
    }
    body = json.dumps(
        {
            "cik": "1",
            "name": "Synthetic issuer",
            "tickers": ["ACME"],
            "filings": {"recent": columns, "files": []},
        }
    ).encode()

    class Response:
        status_code = 200
        content = body

    def get(*_args: object, **_kwargs: object) -> Response:
        return Response()

    monkeypatch.setattr(requests.Session, "get", get)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO issuer_entities VALUES('issuer-acme','Synthetic issuer','operating_company','2026-01-01')"
        )
    result = sync.main(
        [
            "--db",
            str(db),
            "--ticker",
            "ACME",
            "--cik",
            "1",
            "--revision",
            "1",
            "--blob-root",
            str(tmp_path / "blobs"),
            "--package-checkpoint-root",
            str(tmp_path / "checkpoint"),
            "--apply",
        ]
    )
    assert result == 0
    with sqlite3.connect(db) as conn:
        receipt = read_sec_executions(conn, ticker="ACME")[0]
        assert receipt.state == "succeeded"
        assert receipt.result is not None
        snapshot = conn.execute(
            "SELECT snapshot_id FROM source_inventory_snapshot_seals WHERE completion_status='complete'"
        ).fetchone()
        assert snapshot is not None
        assert receipt.result.snapshot_ids == (snapshot[0],)
        row = conn.execute(
            "SELECT observed_at, retrieved_at FROM evidence_source_observations"
        ).fetchone()
        assert row is not None
        assert datetime.fromisoformat(row[0]) == response_at
        assert datetime.fromisoformat(row[1]) == response_at


def test_retrieval_observation_identity_uses_response_clock_not_recording_clock(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db = migrated_db(tmp_path / "inventory.db")
    observed = datetime(2026, 9, 30, 12, tzinfo=UTC)
    retrieved = observed + timedelta(seconds=1)
    recorded = retrieved + timedelta(seconds=5)
    capture = cast(Callable[..., str], getattr(sync, "_capture_component"))
    with sqlite3.connect(db) as conn:

        def persist(at: datetime, got: datetime, stored: datetime) -> str:
            return capture(
                conn,
                body=b"synthetic SEC response",
                url="https://data.sec.gov/submissions/CIK0000000001.json",
                blob_root=tmp_path / "blobs",
                config_sha="a" * 64,
                observed_at=at,
                retrieved_at=got,
                recorded_at=stored,
            )

        first = persist(observed, retrieved, recorded)
        replay = persist(observed, retrieved, recorded + timedelta(seconds=1))
        second = persist(
            observed + timedelta(minutes=1),
            retrieved + timedelta(minutes=1),
            recorded + timedelta(minutes=1),
        )
        assert first == replay
        assert first != second
        rows = conn.execute(
            "SELECT observation_id, observed_at, retrieved_at "
            "FROM evidence_source_observations ORDER BY retrieved_at"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0][0] == first
        assert datetime.fromisoformat(rows[0][1]) == observed
        assert datetime.fromisoformat(rows[0][2]) == retrieved
        assert rows[1][0] == second
