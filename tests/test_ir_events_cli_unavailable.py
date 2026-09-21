"""CLI defaults, failure distinctions and no implicit database creation."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from execution import ingest_ir_events
from signals.ir_events import record_ir_events_batch


def test_missing_database_never_created(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    database = tmp_path / "missing.db"
    assert ingest_ir_events.main(["--db", str(database), "--dry-run", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "unavailable"
    assert payload["reason_code"] == "database_unavailable"
    assert not database.exists()


def test_apply_disabled_before_opening_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("IR_EVENTS_APPLY_ENABLED", raising=False)
    assert ingest_ir_events.main(["--db", str(tmp_path / "missing.db"), "--apply", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "disabled"


def test_no_issuer_evidence_is_not_empty_success(
    tmp_path: Path, migrated_db: Callable[..., Path], capsys: pytest.CaptureFixture[str]
) -> None:
    database = migrated_db(tmp_path / "events.db")
    with sqlite3.connect(database) as conn:
        conn.execute(
            "INSERT INTO tracked_companies(ticker,name,list_type) VALUES('ACME','Acme','portfolio')"
        )
    assert ingest_ir_events.main(["--db", str(database), "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry_run"
    assert payload["status"] == "error"
    assert payload["attempts"][0]["status"] == "unsupported"
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ir_event_runs").fetchone() == (0,)


def test_legitimate_empty_domain_batch_remains_empty_success() -> None:
    with sqlite3.connect(":memory:") as conn:
        result = record_ir_events_batch(conn, [], mode="dry_run")
    assert result.status == "empty"
    assert result.inserted == 0
