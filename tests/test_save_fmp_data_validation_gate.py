# pyright: reportPrivateUsage=false
# This suite unit-tests module-private gate helpers (_validate_stable_record,
# _dump_validation_failure, _STABLE_VALIDATORS) by design.
"""Tests for the ported pre-write validation gate in execution/save_fmp_data.py.

The gate (ported from the retired execution/fetch_fmp_statements.py) Pydantic-
validates the first record of a STABLE statement response *before* the JSON is
cached. On schema drift it dumps the raw body to .tmp/fmp_validation_failures/
and skips the write, recording the endpoint as an error rather than overwriting
good cached JSON with a malformed envelope.

Covers plan section 7.2 (gate halts on drift / passes valid), 7.3 (validation
scoped to stable rungs only — legacy v3/v4 bodies are not gated), and 7.4
(refusal modes 403 / empty / error still classified correctly with the gate in
place).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

# The module sys.exit(1)s at import if FMP_API_KEY is unset; seed a dummy key
# before importing it (the network layer is monkey-patched in every test, so the
# key is never actually used).
os.environ.setdefault("FMP_API_KEY", "test-key-unused")

import execution.save_fmp_data as sfd

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _income_record(**overrides: object) -> dict[str, object]:
    rec: dict[str, object] = {
        "date": "2025-09-30",
        "symbol": "GOOG",
        "reportedCurrency": "USD",
        "period": "Q3",
        "fiscalYear": "2025",
        "revenue": 102_350_000_000,
        "operatingIncome": 31_200_000_000,
        "netIncome": 28_100_000_000,
    }
    rec.update(overrides)
    return rec


# ---------------------------------------------------------------------------
# _validate_stable_record — unit (plan 7.2 / 7.3)
# ---------------------------------------------------------------------------


def test_validator_accepts_canonical_income_statement() -> None:
    body: list[object] = [_income_record()]
    assert sfd._validate_stable_record("income-statement", "stable:income-statement", body) is None


def test_validator_accepts_canonical_cashflow_under_stable_spelling() -> None:
    """The cash-flow catalog path is the stable spelling 'cashflow-statement'
    (not the v3-ish 'cash-flow-statement'); the validator map must key on it."""
    assert "cashflow-statement" in sfd._STABLE_VALIDATORS
    body: list[object] = [
        {"date": "2025-09-30", "symbol": "GOOG", "period": "Q3", "freeCashFlow": 25_000_000_000}
    ]
    assert (
        sfd._validate_stable_record("cashflow-statement", "stable:cashflow-statement", body) is None
    )


def test_validator_rejects_record_missing_required_date() -> None:
    """A stable response that drops `date` (required) is schema drift."""
    bad: list[object] = [{"symbol": "GOOG", "period": "Q3", "revenue": 100}]
    err = sfd._validate_stable_record("income-statement", "stable:income-statement", bad)
    assert err is not None
    assert ["date"] in [list(e["loc"]) for e in err.errors()]


def test_validator_skips_non_stable_rung() -> None:
    """Plan 7.3: a v3/v4 fallback rung returns v3-shaped data; it must NOT be run
    through the stable-shaped model (no false schema-drift halt on legacy data).
    Even a body that would fail the stable model passes when the rung is v3."""
    v3_shaped: list[object] = [{"calendarYear": "2025", "symbol": "GOOG"}]  # no `date`
    assert (
        sfd._validate_stable_record("income-statement", "v3-path:income-statement", v3_shaped)
        is None
    )
    assert (
        sfd._validate_stable_record("income-statement", "v4-query:income-statement", v3_shaped)
        is None
    )
    assert sfd._validate_stable_record("income-statement", None, v3_shaped) is None


def test_validator_passes_empty_body() -> None:
    """An empty list is a 200-OK no-data signal (freshly-IPO'd ticker), not drift."""
    assert sfd._validate_stable_record("income-statement", "stable:income-statement", []) is None


def test_validator_skips_endpoint_without_model() -> None:
    """key-metrics has no model in _STABLE_VALIDATORS — the gate must not block it."""
    body: list[object] = [{"anything": "goes"}]
    assert sfd._validate_stable_record("key-metrics", "stable:key-metrics", body) is None


def test_validator_tolerates_non_list_body() -> None:
    assert (
        sfd._validate_stable_record("income-statement", "stable:income-statement", {"x": 1}) is None
    )


# ---------------------------------------------------------------------------
# _dump_validation_failure — unit
# ---------------------------------------------------------------------------


def test_dump_writes_payload_for_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dump_dir = tmp_path / "dumps"
    monkeypatch.setattr(sfd, "_VALIDATION_DUMP_DIR", dump_dir)

    bad: list[object] = [{"symbol": "GOOG", "period": "Q3"}]  # missing `date`
    err = sfd._validate_stable_record("income-statement", "stable:income-statement", bad)
    assert err is not None

    out_path = sfd._dump_validation_failure(
        "GOOG", "income-statement", "quarter", "income_statement_quarterly", bad, err
    )
    assert out_path.exists()
    assert out_path.parent == dump_dir
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["ticker"] == "GOOG"
    assert payload["endpoint"] == "income-statement"
    assert payload["period"] == "quarter"
    assert payload["raw_response"] == bad
    assert any(e["msg"] for e in payload["validation_errors"])


# ---------------------------------------------------------------------------
# run_ticker integration — the gate halts the write (plan 7.2 / 7.3 / 7.4)
# ---------------------------------------------------------------------------


def _wire_single_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    endpoint: str = "income-statement",
    suffix: str = "income_statement_quarterly",
    period: str = "quarter",
) -> list[tuple[object, ...]]:
    """Point save_fmp_data at tmp dirs and a single job; capture status rows.

    Returns the list that accumulates the flushed status rows so tests can
    inspect the recorded status. PROJECT_ROOT is repointed at tmp_path so the
    `relative_to(PROJECT_ROOT)` calls in run_ticker resolve for tmp paths.
    """
    fmp_dir = tmp_path / "fmp"
    fmp_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sfd, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(sfd, "FMP_DIR", fmp_dir)
    monkeypatch.setattr(sfd, "_VALIDATION_DUMP_DIR", tmp_path / ".tmp" / "fmp_validation_failures")

    def fake_list_type(ticker: str) -> str:
        return "evaluation"

    monkeypatch.setattr(sfd, "_list_type_for", fake_list_type)

    def fake_jobs(symbol: str, list_type: str = "portfolio") -> list[dict[str, object]]:
        return [
            {
                "path": endpoint,
                "symbol": symbol,
                "period": period,
                "suffix": suffix,
                "extra": {},
                "file_override": None,
            }
        ]

    monkeypatch.setattr(sfd, "per_ticker_jobs", fake_jobs)

    flushed: list[tuple[object, ...]] = []

    def fake_flush(rows: list[tuple[object, ...]]) -> None:
        flushed.extend(rows)

    monkeypatch.setattr(sfd, "_flush_status_batch", fake_flush)
    return flushed


def test_run_ticker_valid_stable_writes_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire_single_job(tmp_path, monkeypatch)
    body: list[object] = [_income_record()]

    def fake_call(endpoint: str, ticker: str, extra: dict[str, object]):
        return (200, body, None, "stable:income-statement")

    monkeypatch.setattr(sfd, "fmp_call", fake_call)

    summary = sfd.run_ticker("GOOG")

    assert summary["ok"] == 1
    assert summary["error"] == 0
    assert (tmp_path / "fmp" / "GOOG_income_statement_quarterly.json").exists()
    # No drift dump written on the happy path.
    assert not (tmp_path / ".tmp" / "fmp_validation_failures").exists()


def test_run_ticker_drift_skips_write_and_records_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan 7.2: a drifted STABLE body -> ValidationError -> dump written, status
    error, and NO statement file written (good cache not overwritten)."""
    flushed = _wire_single_job(tmp_path, monkeypatch)
    drifted: list[object] = [{"symbol": "GOOG", "period": "Q3"}]  # missing `date`

    def fake_call(endpoint: str, ticker: str, extra: dict[str, object]):
        return (200, drifted, None, "stable:income-statement")

    monkeypatch.setattr(sfd, "fmp_call", fake_call)

    summary = sfd.run_ticker("GOOG")

    assert summary["error"] == 1
    assert summary["ok"] == 0
    # The statement file must NOT be written.
    assert not (tmp_path / "fmp" / "GOOG_income_statement_quarterly.json").exists()
    # A dump must land in .tmp/fmp_validation_failures/.
    dump_dir = tmp_path / ".tmp" / "fmp_validation_failures"
    assert dump_dir.exists()
    dumps = list(dump_dir.glob("GOOG_income_statement_quarterly_*.json"))
    assert len(dumps) == 1
    # The recorded status row cites schema_drift (status is row[3], error_msg row[10]).
    assert [r[3] for r in flushed] == ["error"]
    err_msg = flushed[0][10]
    assert isinstance(err_msg, str) and "schema_drift" in err_msg


def test_run_ticker_v3_fallback_written_without_stable_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan 7.3: a v3 fallback response (legacy tier) is written WITHOUT being run
    through the stable validator — even though its v3 shape would fail the stable
    model — so legacy data is never falsely halted as schema drift."""
    _wire_single_job(tmp_path, monkeypatch)
    v3_body: list[object] = [{"calendarYear": "2025", "symbol": "GOOG"}]  # no `date`

    def fake_call(endpoint: str, ticker: str, extra: dict[str, object]):
        return (200, v3_body, None, "v3-path:income-statement")

    monkeypatch.setattr(sfd, "fmp_call", fake_call)

    summary = sfd.run_ticker("GOOG")

    assert summary["ok"] == 1
    assert summary["error"] == 0
    assert (tmp_path / "fmp" / "GOOG_income_statement_quarterly.json").exists()


def test_run_ticker_403_still_forbidden_with_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan 7.4 regression: a true tier restriction is still classified forbidden;
    the gate only acts on real 200 bodies and cannot affect refusal handling."""
    _wire_single_job(tmp_path, monkeypatch)

    def fake_call(endpoint: str, ticker: str, extra: dict[str, object]):
        return (403, None, "tier-restricted", None)

    monkeypatch.setattr(sfd, "fmp_call", fake_call)
    summary = sfd.run_ticker("GOOG")
    assert summary["forbidden"] == 1
    assert summary["ok"] == 0
    assert summary["error"] == 0


def test_run_ticker_empty_still_empty_with_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan 7.4 regression: a 200-empty stable probe is still classified empty."""
    _wire_single_job(tmp_path, monkeypatch)

    def fake_call(endpoint: str, ticker: str, extra: dict[str, object]):
        return (200, None, "empty-list", "stable:income-statement")

    monkeypatch.setattr(sfd, "fmp_call", fake_call)
    summary = sfd.run_ticker("GOOG")
    assert summary["empty"] == 1
    assert summary["ok"] == 0
    assert summary["error"] == 0


def test_run_ticker_error_body_still_error_with_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan 7.4 regression: a 200 with an FMP `Error Message` body (surfaced by
    _http_get as body=None + err) is still classified error."""
    _wire_single_job(tmp_path, monkeypatch)

    def fake_call(endpoint: str, ticker: str, extra: dict[str, object]):
        return (200, None, "fmp-err: Limit Reach", None)

    monkeypatch.setattr(sfd, "fmp_call", fake_call)
    summary = sfd.run_ticker("GOOG")
    assert summary["error"] == 1
    assert summary["ok"] == 0
    assert summary["forbidden"] == 0
