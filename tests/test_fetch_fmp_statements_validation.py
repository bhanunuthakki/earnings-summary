"""Tests for the pre-insert validation gate in execution/fetch_fmp_statements.py.

The gate Pydantic-validates the first record of every FMP statements response
*before* writing the JSON to disk. On schema drift it halts the task and dumps
the raw response to .tmp/fmp_validation_failures/ for inspection.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from execution.fetch_fmp_statements import (  # noqa: E402
    FetchTask,
    _dump_validation_failure,
    _validate_response,
)


def _task(endpoint: str) -> FetchTask:
    return FetchTask(
        ticker="GOOG",
        endpoint=endpoint,
        period="quarter",
        file_suffix=f"{endpoint.replace('-', '_')}_quarterly",
        limit=80,
        out_path=Path("/tmp/should-not-be-written.json"),
    )


def test_validator_accepts_canonical_income_statement_record() -> None:
    """A well-formed FMP income-statement record passes the gate."""
    task = _task("income-statement")
    records: list[object] = [
        {
            "date": "2025-09-30",
            "symbol": "GOOG",
            "reportedCurrency": "USD",
            "period": "Q3",
            "revenue": 102_350_000_000,
            "operatingIncome": 31_200_000_000,
            "netIncome": 28_100_000_000,
            "eps": 2.30,
            "epsDiluted": 2.29,
            "weightedAverageShsOut": 12_100_000_000,
            "weightedAverageShsOutDil": 12_220_000_000,
        }
    ]
    assert _validate_response(task, records) is None


def test_validator_rejects_record_missing_required_field() -> None:
    """A response that drops `date` (required by FmpIncomeStatementRecord)
    triggers a ValidationError — schema drift signal."""
    task = _task("income-statement")
    records: list[object] = [
        {
            "symbol": "GOOG",
            "period": "Q3",
            # date intentionally absent
            "revenue": 100,
        }
    ]
    err = _validate_response(task, records)
    assert err is not None
    locs = [list(e["loc"]) for e in err.errors()]
    assert ["date"] in locs


def test_validator_passes_through_empty_response() -> None:
    """An empty list is a 200-OK no-data signal, not schema drift — must not
    halt the task. The skip lets the fetcher still write an empty JSON file
    so the cache reflects "we asked and got nothing"."""
    task = _task("income-statement")
    assert _validate_response(task, []) is None


def test_validator_skips_endpoint_without_model() -> None:
    """key-metrics has no Pydantic model in fmp_payloads.py yet — the gate
    must not block the fetch. Adding a model later would flip this on
    without code change at the call site."""
    task = _task("key-metrics")
    records: list[object] = [{"anything": "goes"}]
    assert _validate_response(task, records) is None


def test_dump_writes_payload_for_inspection(tmp_path: Path, monkeypatch: object) -> None:
    """When a validation error fires, the raw response + error breakdown lands
    in .tmp/fmp_validation_failures/ keyed by ticker + file_suffix + timestamp."""
    import execution.fetch_fmp_statements as mod

    dump_dir = tmp_path / "_dumps"
    monkeypatch.setattr(mod, "_VALIDATION_DUMP_DIR", dump_dir)  # type: ignore[attr-defined]

    task = _task("income-statement")
    bad_records: list[object] = [{"symbol": "GOOG", "period": "Q3"}]
    err = _validate_response(task, bad_records)
    assert err is not None

    out_path = _dump_validation_failure(task, bad_records, err)
    assert out_path.exists()
    assert out_path.parent == dump_dir
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["ticker"] == "GOOG"
    assert payload["endpoint"] == "income-statement"
    assert payload["raw_response"] == bad_records
    assert any(e["msg"] for e in payload["validation_errors"])
