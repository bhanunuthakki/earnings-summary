"""Inventory apply entrypoint retains terminal status independently of stdout."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

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
        assert conn.execute("SELECT COUNT(*) FROM evidence_source_observations").fetchone()[0] == 1
