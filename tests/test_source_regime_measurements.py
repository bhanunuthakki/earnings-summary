from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from io import BytesIO
from pathlib import Path

import pytest
import requests

from net.client import HostRateBudget, HttpClient, RetryPolicy
from provenance.source_regime import SourceRegime
from sources import registry
from sources.telemetry import (
    SourceAttemptMeasurement,
    SourceMeasurementScope,
    persist_source_attempt,
    source_measurement_report,
    source_measurement_scope,
)


def test_transport_attempts_are_durable_and_unknown_cost_is_not_zero(
    tmp_path: Path, migrated_db: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    database = migrated_db(tmp_path / "fixture.db")
    monkeypatch.setattr(registry, "_DB_PATH", database)
    session = requests.Session()
    responses: list[requests.Response] = []
    for code in (429, 200):
        response = requests.Response()
        response.status_code = code
        response.raw = BytesIO(b'[{"fixture":1}]')
        responses.append(response)

    def request(*_args: object, **_kwargs: object) -> requests.Response:
        return responses.pop(0)

    monkeypatch.setattr(session, "request", request)
    events: list[Mapping[str, object]] = []
    client = HttpClient(
        session=session,
        rate_budget=HostRateBudget({}),
        retry=RetryPolicy(max_attempts=2, backoff_base_s=0),
        sleep=lambda _seconds: None,
        event_sink=events.append,
    )
    with source_measurement_scope(
        SourceMeasurementScope(
            run_id="fixture-run", regime=SourceRegime.COMBINED, ticker_scope=("WIX",)
        )
    ):
        result = client.request_json(
            "GET",
            "https://financialmodelingprep.com/stable/income-statement?apikey=VERY_PRIVATE",
            params={"symbol": "WIX"},
        )
    assert result.status_code == 200
    assert len(events) == 2
    assert all(event["measurement_persisted"] for event in events)
    with sqlite3.connect(database) as conn:
        rows = conn.execute(
            "SELECT payload_json FROM source_regime_measurements ORDER BY rowid"
        ).fetchall()
        assert len(rows) == 2
        events_payload = [json.loads(row[0]) for row in rows]
        assert [row["retry_count"] for row in events_payload] == [0, 1]
        assert [row["http_status"] for row in events_payload] == [429, 200]
        assert all(row["bytes_received"] == 15 for row in events_payload)
        assert "VERY_PRIVATE" not in str(rows)
        summary = source_measurement_report(conn, run_id="fixture-run")
        assert summary["status"] == "partial"
        assert summary["provider_cost_usd"] is None
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert registry.summarize_source_calls(db_path=database) == []


def test_measurement_replay_immutability_and_legacy_rows(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    database = migrated_db(tmp_path / "fixture.db")
    with sqlite3.connect(database) as conn:
        conn.execute(
            "INSERT INTO source_calls (source_name,kind,status) VALUES ('fmp','legacy','ok')"
        )
        conn.commit()
        event = SourceAttemptMeasurement(
            run_id="run",
            provider="fmp",
            endpoint="https://user:password@example.invalid/data?apikey=SECRET",
            latency_ms=5,
            retry_count=0,
            status="ok",
            http_status=200,
        )
        first = persist_source_attempt(conn, event)
        before = conn.total_changes
        assert persist_source_attempt(conn, event) == first
        assert conn.total_changes == before
        assert conn.execute("SELECT count(*) FROM source_calls").fetchone()[0] == 2
        assert event.endpoint == "example.invalid/data"
        with pytest.raises(ValueError, match="replay conflict"):
            persist_source_attempt(conn, event.model_copy(update={"latency_ms": 9}))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM source_regime_measurements")
        conn.rollback()


def test_measurement_atomicity_and_caller_transaction_protection(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    with sqlite3.connect(migrated_db(tmp_path / "fixture.db")) as conn:
        event = SourceAttemptMeasurement(
            run_id="run",
            provider="fmp",
            endpoint="example.invalid/data",
            latency_ms=1,
            retry_count=0,
            status="network_error",
        )
        conn.execute(
            "INSERT INTO source_calls(source_name,kind,status) VALUES ('legacy','caller','ok')"
        )
        with pytest.raises(ValueError, match="own transaction"):
            persist_source_attempt(conn, event)
        assert conn.in_transaction
        conn.rollback()
        conn.execute(
            "CREATE TRIGGER fail_measurement BEFORE INSERT ON source_regime_measurements BEGIN SELECT RAISE(ABORT,'injected failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
            persist_source_attempt(conn, event)
        assert conn.execute("SELECT count(*) FROM source_calls").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM source_regime_measurements").fetchone()[0] == 0
