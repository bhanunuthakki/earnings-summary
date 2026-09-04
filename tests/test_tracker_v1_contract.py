"""Contract tests for ``integrations.portfolio_tracker_v1`` (PRD §8.1 Phase 1).

Fully hermetic — no live provider, no network. Fixtures come from
``tests/fixtures/tracker_v1/`` (official, vendored verbatim from the sibling
``portfolio-tracker`` repo) and ``tests/fixtures/tracker_v1/synthetic/``
(consumer hand-derived, for the endpoints without an official fixture yet).
Network is mocked at the ``requests.Session.get`` layer (same technique as
``tests/test_portfolio_tracker_client.py``'s ``_FakeResp`` — no third-party
mocking library is added).
"""

from __future__ import annotations

import json
import logging
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
import requests
from pydantic import BaseModel, ValidationError

from integrations import portfolio_tracker_v1 as tv1

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "tracker_v1"
# tests/fixtures/tracker_v1/synthetic/ is currently empty — every v1 endpoint
# now has an official fixture (provider PR #52). It's kept as a fallback
# location for any future endpoint that ships without one; see its README.


def _load(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data, dict)
    return cast("dict[str, object]", data)


def _deep_copy(data: dict[str, object]) -> dict[str, object]:
    """A plain-JSON deep copy (fixtures are JSON-shaped, so round-tripping
    through json is sufficient and avoids adding a copy-library dependency)."""
    return cast("dict[str, object]", json.loads(json.dumps(data)))


def _meta(schema_version: str = "1.0.0") -> dict[str, object]:
    """A minimal, schema-valid V1Meta envelope for hand-built payloads."""
    return {
        "schema_version": schema_version,
        "generated_at": "2026-07-23T06:00:00Z",
        "as_of": "2026-07-22",
        "currency": "USD",
        "source_providers": ["plaid"],
        "account_coverage": {
            "included_account_ids": [1],
            "excluded_account_ids": [],
            "lagging_account_ids": [],
        },
        "last_successful_sync_at": "2026-07-22T12:00:00",
        "is_partial": False,
        "is_stale": False,
        "warnings": [],
        "methodology": None,
        "methodology_version": None,
        "links": {},
    }


def _accounts_payload(schema_version: str = "1.0.0") -> dict[str, object]:
    return {"meta": _meta(schema_version), "accounts": []}


def _txn_row(txn_id: str) -> dict[str, object]:
    return {
        "transaction_id": txn_id,
        "account_id": 1,
        "account_name": "Test Brokerage",
        "security_id": None,
        "ticker": None,
        "date": "2026-07-01",
        "name": "Test txn",
        "quantity": "0",
        "amount": "10.000000",
        "price": None,
        "fees": None,
        "type": "cash",
        "subtype": None,
        "currency": "USD",
        "override_classification": None,
        "effective_classification": "external_in",
    }


def _txn_page(next_cursor: str | None, txn_ids: list[str]) -> dict[str, object]:
    return {
        "meta": _meta(),
        "start_date": "2026-06-23",
        "end_date": "2026-07-23",
        "transactions": [_txn_row(t) for t in txn_ids],
        "next_cursor": next_cursor,
    }


class _FakeResponse:
    """Minimal ``requests.Response`` stand-in — just ``.status_code``/``.json()``."""

    def __init__(self, payload: object, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> object:
        return self._payload


# ---------------------------------------------------------------------------
# Official fixtures parse into their response models
# ---------------------------------------------------------------------------

_OFFICIAL_FIXTURES: list[tuple[str, type[BaseModel]]] = [
    ("accounts.json", tv1.AccountsV1Result),
    ("cash-flows.json", tv1.CashFlowsV1Result),
    ("health.json", tv1.HealthV1),
    ("portfolio-snapshot.json", tv1.PortfolioSnapshotV1),
    ("portfolio-snapshot.partial.json", tv1.PortfolioSnapshotV1),
    ("portfolio-snapshot.stale.json", tv1.PortfolioSnapshotV1),
    ("positioning.json", tv1.PositioningV1Result),
    ("securities.json", tv1.SecuritiesV1Result),
    ("transactions.json", tv1.TransactionsV1Result),
]


@pytest.mark.parametrize(("filename", "model"), _OFFICIAL_FIXTURES)
def test_official_fixture_parses(filename: str, model: type[BaseModel]) -> None:
    data = _load(FIXTURES_DIR / filename)
    parsed = model.model_validate(data)
    assert parsed is not None


def test_accounts_typed_values() -> None:
    result = tv1.AccountsV1Result.model_validate(_load(FIXTURES_DIR / "accounts.json"))
    assert isinstance(result.meta.as_of, date)
    brokerage = next(a for a in result.accounts if a.account_id == 3)
    assert brokerage.value == Decimal("6800.000000")
    assert isinstance(brokerage.value, Decimal)
    assert not isinstance(brokerage.value, float)
    assert brokerage.tax_treatment == "taxable"
    roth = next(a for a in result.accounts if a.account_id == 1)
    assert roth.tax_treatment == "roth"
    excluded = next(a for a in result.accounts if a.account_id == 4)
    assert excluded.exclusion_reason == "operator_excluded"
    assert excluded.warnings[0].code == "NO_CANONICAL_LINK"


def test_transactions_typed_values_including_scientific_notation_quantity() -> None:
    """The official fixture's cash-type rows carry ``quantity: "0E-10"`` —
    valid Decimal grammar but NOT matching the OpenAPI ``pattern`` regex on
    that field (a contract quirk, documented in the client module
    docstring). Confirms our parser accepts it as a plain zero."""
    result = tv1.TransactionsV1Result.model_validate(_load(FIXTURES_DIR / "transactions.json"))
    cash_row = next(t for t in result.transactions if t.transaction_id == "fx-t4")
    assert cash_row.quantity == Decimal("0E-10")
    assert cash_row.quantity == 0
    buy_row = next(t for t in result.transactions if t.transaction_id == "fx-t1")
    assert buy_row.quantity == Decimal("5.0000000000")
    assert buy_row.ticker == "BBBB"


def test_portfolio_snapshot_partial_flags() -> None:
    result = tv1.PortfolioSnapshotV1.model_validate(
        _load(FIXTURES_DIR / "portfolio-snapshot.partial.json")
    )
    assert result.meta.is_partial is True
    assert result.meta.is_stale is False
    codes = {w.code for w in result.meta.warnings}
    assert "PARTIAL_COVERAGE" in codes


def test_portfolio_snapshot_stale_flags() -> None:
    result = tv1.PortfolioSnapshotV1.model_validate(
        _load(FIXTURES_DIR / "portfolio-snapshot.stale.json")
    )
    assert result.meta.is_stale is True
    assert result.meta.is_partial is False
    codes = {w.code for w in result.meta.warnings}
    assert "STALE_HOLDINGS" in codes


# ---------------------------------------------------------------------------
# The seven previously-fixture-less endpoints — official fixtures added
# 2026-07-24 (provider PR #52, "complete the v1 consumer fixture suite").
# tests/fixtures/tracker_v1/synthetic/ is now empty (kept as a fallback for
# any future endpoint that ships without one) — see that directory's README.
# ---------------------------------------------------------------------------

_NEWLY_COVERED_FIXTURES: list[tuple[str, type[BaseModel]]] = [
    ("positions.json", tv1.PositionsV1Result),
    ("position-snapshots.json", tv1.PositionSnapshotsV1Result),
    ("data-quality.json", tv1.DataQualityV1Result),
    ("performance.json", tv1.PerformanceV1Result),
    ("position-performance.json", tv1.PositionPerformanceV1Result),
    ("risk.json", tv1.RiskV1Result),
    ("exit-quality.json", tv1.ExitQualityV1Result),
]


@pytest.mark.parametrize(("filename", "model"), _NEWLY_COVERED_FIXTURES)
def test_newly_covered_fixture_parses(filename: str, model: type[BaseModel]) -> None:
    data = _load(FIXTURES_DIR / filename)
    parsed = model.model_validate(data)
    assert parsed is not None


def test_positions_result_has_no_meta_envelope() -> None:
    """Contract quirk: PositionsV1Result carries no meta/schema_version at
    all (see its docstring). Confirm the model has no ``meta`` attribute."""
    result = tv1.PositionsV1Result.model_validate(_load(FIXTURES_DIR / "positions.json"))
    assert not hasattr(result, "meta")


def test_data_quality_fixture_typed_values() -> None:
    """The official fixture carries one informational snapshot-history finding."""
    result = tv1.DataQualityV1Result.model_validate(_load(FIXTURES_DIR / "data-quality.json"))
    assert len(result.report.findings) == 1
    assert result.report.summary_counts == {"info": 1}
    severities = [f.severity for f in result.report.findings]
    assert severities.count("warning") == 0
    assert severities.count("info") == 1


def test_performance_fixture_has_full_year_series() -> None:
    """The official fixture is a full 365-point daily series."""
    result = tv1.PerformanceV1Result.model_validate(_load(FIXTURES_DIR / "performance.json"))
    assert result.series.calculation_status == "available"
    assert result.series.calculation_reason_codes == []
    assert len(result.series.points) == 365
    first = result.series.points[0]
    assert isinstance(first.portfolio_value, Decimal)
    assert first.portfolio_return_pct == Decimal("0")
    assert first.spy_return_pct == Decimal("0")
    assert first.spy_equivalent_value == result.series.base_value
    receipt = result.series.equation_receipt
    assert receipt is not None
    assert receipt.benchmark_price_resolution_policy == "same_day_or_previous_us_market_close"
    assert receipt.spy.price_inputs
    assert receipt.qqq.price_inputs


@pytest.mark.parametrize("mutation", ["missing_policy", "unknown_policy", "empty_inputs"])
def test_performance_receipt_market_session_lineage_is_required(mutation: str) -> None:
    data = _deep_copy(_load(FIXTURES_DIR / "performance.json"))
    series = cast("dict[str, object]", data["series"])
    receipt = cast("dict[str, object]", series["equation_receipt"])
    if mutation == "missing_policy":
        receipt.pop("benchmark_price_resolution_policy")
    elif mutation == "unknown_policy":
        receipt["benchmark_price_resolution_policy"] = "nearest_close"
    else:
        spy = cast("dict[str, object]", receipt["spy"])
        spy["price_inputs"] = []

    with pytest.raises(ValidationError):
        tv1.PerformanceV1Result.model_validate(data)


@pytest.mark.parametrize(
    "field",
    ["net_external_cashflow_in", "backfill_start_unreliable"],
)
def test_performance_required_financial_provenance_field_omission_is_invalid(field: str) -> None:
    data = _deep_copy(_load(FIXTURES_DIR / "performance.json"))
    series = cast("dict[str, object]", data["series"])
    series.pop(field)

    with pytest.raises(ValidationError):
        tv1.PerformanceV1Result.model_validate(data)


@pytest.mark.parametrize(
    "field",
    ["net_external_cashflow_in", "backfill_start_unreliable"],
)
def test_performance_fetch_fails_closed_when_required_field_is_omitted(
    field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _deep_copy(_load(FIXTURES_DIR / "performance.json"))
    series = cast("dict[str, object]", data["series"])
    series.pop(field)

    def fake_get(
        self: requests.Session,
        url: str,
        params: object = None,
        timeout: object = None,
    ) -> _FakeResponse:
        return _FakeResponse(data)

    monkeypatch.setattr(requests.Session, "get", fake_get)
    fetch = tv1.TrackerV1Client().get_performance()

    assert fetch.available is False
    assert fetch.data is None
    assert fetch.error is not None
    assert "schema_validation_error" in fetch.error
    assert field in fetch.error


def test_risk_fixture_has_meta_beta_drawdown_top_level_shape() -> None:
    """RiskV1Result is {meta, beta, drawdown} — two sibling result keys, NOT
    one nested "result" (unlike position-performance/exit-quality)."""
    result = tv1.RiskV1Result.model_validate(_load(FIXTURES_DIR / "risk.json"))
    assert result.beta.benchmark == "SPY"
    assert len(result.drawdown.underwater) == 365
    assert isinstance(result.drawdown.max_drawdown_pct, Decimal)


# ---------------------------------------------------------------------------
# Unknown additive field tolerance
# ---------------------------------------------------------------------------


def test_unknown_additive_field_tolerated() -> None:
    data = _deep_copy(_load(FIXTURES_DIR / "accounts.json"))
    accounts = cast("list[dict[str, object]]", data["accounts"])
    accounts[0]["brand_new_field_from_a_future_minor_bump"] = "surprise"
    data["a_whole_new_top_level_field"] = {"nested": True}
    result = tv1.AccountsV1Result.model_validate(data)
    assert len(result.accounts) == len(accounts)


# ---------------------------------------------------------------------------
# Major-version fail-closed gate (module-level function + end to end)
# ---------------------------------------------------------------------------


def test_check_major_version_rejects_incompatible() -> None:
    # White-box unit test of the internal gate function itself (the
    # end-to-end tests below exercise it through the public client API).
    err = tv1._check_major_version("2.0.0")  # pyright: ignore[reportPrivateUsage]
    assert err is not None
    assert "2.0.0" in err
    assert "1" in err


@pytest.mark.parametrize("version", ["1.0.0", "1.7.3", "1.99.99"])
def test_check_major_version_accepts_minor_patch_drift(version: str) -> None:
    assert tv1._check_major_version(version) is None  # pyright: ignore[reportPrivateUsage]


def test_major_version_rejected_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _accounts_payload(schema_version="2.0.0")

    def fake_get(
        self: requests.Session, url: str, params: object = None, timeout: object = None
    ) -> _FakeResponse:
        return _FakeResponse(payload)

    monkeypatch.setattr(requests.Session, "get", fake_get)
    client = tv1.TrackerV1Client()
    fetch = client.get_accounts()
    assert fetch.available is False
    assert fetch.data is None
    assert fetch.error is not None
    assert "2.0.0" in fetch.error
    assert "1" in fetch.error


def test_minor_patch_version_accepted_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _deep_copy(_load(FIXTURES_DIR / "accounts.json"))
    cast("dict[str, object]", data["meta"])["schema_version"] = "1.7.3"

    def fake_get(
        self: requests.Session, url: str, params: object = None, timeout: object = None
    ) -> _FakeResponse:
        return _FakeResponse(data)

    monkeypatch.setattr(requests.Session, "get", fake_get)
    client = tv1.TrackerV1Client()
    fetch = client.get_accounts()
    assert fetch.available is True
    assert fetch.data is not None
    assert fetch.data.meta.schema_version == "1.7.3"


def test_health_is_exempt_from_the_version_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """HealthV1 has a flat top-level ``schema_version`` but, per the
    provider-session contract clarification, is deliberately EXEMPT from the
    major-version fail-closed gate ("envelope-carrying" means the full
    V1Meta shape, which Health lacks — it's also the discovery/probe
    endpoint itself). A MAJOR-incompatible schema_version must still parse
    successfully rather than being rejected."""
    bad_health = _deep_copy(_load(FIXTURES_DIR / "health.json"))
    bad_health["schema_version"] = "2.0.0"

    def fake_get(
        self: requests.Session, url: str, params: object = None, timeout: object = None
    ) -> _FakeResponse:
        return _FakeResponse(bad_health)

    monkeypatch.setattr(requests.Session, "get", fake_get)
    client = tv1.TrackerV1Client()
    fetch = client.get_health()
    assert fetch.available is True
    assert fetch.data is not None
    assert fetch.data.schema_version == "2.0.0"


def test_positions_endpoint_version_gate_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """PositionsV1Result has no schema_version anywhere — the gate must not
    block it (there's nothing to check, not a bypass)."""
    data = _load(FIXTURES_DIR / "positions.json")

    def fake_get(
        self: requests.Session, url: str, params: object = None, timeout: object = None
    ) -> _FakeResponse:
        return _FakeResponse(data)

    monkeypatch.setattr(requests.Session, "get", fake_get)
    client = tv1.TrackerV1Client()
    fetch = client.get_positions()
    assert fetch.available is True
    assert fetch.meta is None


# ---------------------------------------------------------------------------
# Decimal integrity — never float
# ---------------------------------------------------------------------------


def _walk_leaves(obj: object) -> list[object]:
    """Every non-container leaf value reachable from a parsed model tree."""
    if isinstance(obj, BaseModel):
        leaves: list[object] = []
        for name in type(obj).model_fields:
            leaves.extend(_walk_leaves(getattr(obj, name)))
        return leaves
    if isinstance(obj, list):
        out: list[object] = []
        for item in cast("list[object]", obj):
            out.extend(_walk_leaves(item))
        return out
    if isinstance(obj, dict):
        out2: list[object] = []
        for value in cast("dict[object, object]", obj).values():
            out2.extend(_walk_leaves(value))
        return out2
    return [obj]


def test_decimal_integrity_no_float_anywhere_in_snapshot() -> None:
    """portfolio-snapshot.json has no legitimately-float fields (those only
    exist on BetaResult/PositionCorrelationRow, not in this payload) — every
    numeric leaf should be Decimal, int, or bool, never float."""
    result = tv1.PortfolioSnapshotV1.model_validate(_load(FIXTURES_DIR / "portfolio-snapshot.json"))
    for leaf in _walk_leaves(result):
        assert not isinstance(leaf, float), f"found a float leaking through: {leaf!r}"


def test_decimal_integrity_spot_checks() -> None:
    result = tv1.PortfolioSnapshotV1.model_validate(_load(FIXTURES_DIR / "portfolio-snapshot.json"))
    assert isinstance(result.total_market_value, Decimal)
    for value in result.by_tax_treatment.values():
        assert isinstance(value, Decimal)
    assert isinstance(result.equity_fraction.equity_fraction, Decimal)
    for position in result.positions:
        assert isinstance(position.market_value, Decimal)
        for lot in position.accounts:
            assert isinstance(lot.quantity, Decimal)


def test_decimal_field_rejects_raw_json_float() -> None:
    """A raw JSON float (rather than the contract's decimal string) in a
    money field must fail validation, not silently truncate precision."""
    bad = _deep_copy(_load(FIXTURES_DIR / "accounts.json"))
    accounts = cast("list[dict[str, object]]", bad["accounts"])
    accounts[0]["value"] = 6800.0
    with pytest.raises(ValidationError):
        tv1.AccountsV1Result.model_validate(bad)


# ---------------------------------------------------------------------------
# Cursor pagination
# ---------------------------------------------------------------------------


def test_pagination_follows_cursor_and_concatenates(monkeypatch: pytest.MonkeyPatch) -> None:
    page1 = _txn_page("cursor-page-2", ["t1"])
    page2 = _txn_page(None, ["t2", "t3"])
    calls: list[dict[str, object]] = []

    def fake_get(
        self: requests.Session,
        url: str,
        params: dict[str, object] | None = None,
        timeout: object = None,
    ) -> _FakeResponse:
        calls.append(dict(params or {}))
        if params and params.get("cursor") == "cursor-page-2":
            return _FakeResponse(page2)
        return _FakeResponse(page1)

    monkeypatch.setattr(requests.Session, "get", fake_get)
    client = tv1.TrackerV1Client()
    fetch = client.get_all_transactions()
    assert fetch.available is True
    assert fetch.data is not None
    assert [t.transaction_id for t in fetch.data] == ["t1", "t2", "t3"]
    assert len(calls) == 2


def test_pagination_invalid_cursor_surfaces_code(monkeypatch: pytest.MonkeyPatch) -> None:
    error_body = {
        "error": {
            "code": "INVALID_CURSOR",
            "message": "cursor is not valid base64",
            "request_id": "req-1",
            "resource": "/api/v1/transactions",
            "retryable": False,
            "recovery": "Restart pagination from the first page (omit `cursor`).",
        }
    }

    def fake_get(
        self: requests.Session, url: str, params: object = None, timeout: object = None
    ) -> _FakeResponse:
        return _FakeResponse(error_body, status=400)

    monkeypatch.setattr(requests.Session, "get", fake_get)
    client = tv1.TrackerV1Client()
    fetch = client.get_all_transactions()
    assert fetch.available is False
    assert fetch.error is not None
    assert "INVALID_CURSOR" in fetch.error


def test_pagination_page_cap_stops_runaway_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server that never returns a null next_cursor must not spin forever."""

    def fake_get(
        self: requests.Session, url: str, params: object = None, timeout: object = None
    ) -> _FakeResponse:
        return _FakeResponse(_txn_page("always-more", ["t1"]))

    monkeypatch.setattr(requests.Session, "get", fake_get)
    client = tv1.TrackerV1Client()
    fetch = client.get_all_transactions()
    assert fetch.available is False
    assert fetch.error is not None
    assert "page_cap" in fetch.error


# ---------------------------------------------------------------------------
# Offline behavior — never raise
# ---------------------------------------------------------------------------


def test_connection_refused_returns_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(
        self: requests.Session, url: str, params: object = None, timeout: object = None
    ) -> _FakeResponse:
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(requests.Session, "get", fake_get)
    client = tv1.TrackerV1Client()
    fetch = client.get_health()
    assert fetch.available is False
    assert fetch.data is None
    assert fetch.error is not None
    assert "connection_error" in fetch.error.lower()


def test_timeout_returns_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(
        self: requests.Session, url: str, params: object = None, timeout: object = None
    ) -> _FakeResponse:
        raise requests.Timeout("too slow")

    monkeypatch.setattr(requests.Session, "get", fake_get)
    client = tv1.TrackerV1Client()
    fetch = client.get_portfolio_snapshot()
    assert fetch.available is False
    assert fetch.error is not None
    assert "timeout" in fetch.error.lower()


def test_non_dict_json_body_returns_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(
        self: requests.Session, url: str, params: object = None, timeout: object = None
    ) -> _FakeResponse:
        return _FakeResponse(["not", "an", "object"])

    monkeypatch.setattr(requests.Session, "get", fake_get)
    client = tv1.TrackerV1Client()
    fetch = client.get_health()
    assert fetch.available is False
    assert fetch.error is not None
    assert "unexpected_shape" in fetch.error


def test_schema_validation_failure_returns_unavailable_without_leaking_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = _deep_copy(_load(FIXTURES_DIR / "accounts.json"))
    accounts = cast("list[dict[str, object]]", bad["accounts"])
    accounts[0]["tax_treatment"] = "not_a_real_treatment"

    def fake_get(
        self: requests.Session, url: str, params: object = None, timeout: object = None
    ) -> _FakeResponse:
        return _FakeResponse(bad)

    monkeypatch.setattr(requests.Session, "get", fake_get)
    client = tv1.TrackerV1Client()
    fetch = client.get_accounts()
    assert fetch.available is False
    assert fetch.error is not None
    assert "schema_validation_error" in fetch.error
    # the reason names the failing field, not the offending value
    assert "tax_treatment" in fetch.error
    assert "not_a_real_treatment" not in fetch.error


# ---------------------------------------------------------------------------
# Telemetry redaction
# ---------------------------------------------------------------------------


def test_telemetry_log_has_no_payload_contents(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    data = _load(FIXTURES_DIR / "accounts.json")

    def fake_get(
        self: requests.Session, url: str, params: object = None, timeout: object = None
    ) -> _FakeResponse:
        return _FakeResponse(data)

    monkeypatch.setattr(requests.Session, "get", fake_get)
    client = tv1.TrackerV1Client()
    with caplog.at_level(logging.INFO, logger="tracker_v1"):
        fetch = client.get_accounts()
    assert fetch.available is True

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    # Fixture values that must never appear in a log line.
    for leaked in (
        "Example Brokerage",
        "Example Roth IRA",
        "Example HSA",
        "6800.000000",
        "12000.000000",
    ):
        assert leaked not in log_text
    # The allowed telemetry fields ARE expected to be present.
    assert "/api/v1/accounts" in log_text
    assert "schema_version=1.1.0" in log_text
    assert "status=200" in log_text


def test_telemetry_log_on_connection_error_has_no_status_leak(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fake_get(
        self: requests.Session, url: str, params: object = None, timeout: object = None
    ) -> _FakeResponse:
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(requests.Session, "get", fake_get)
    client = tv1.TrackerV1Client()
    with caplog.at_level(logging.INFO, logger="tracker_v1"):
        client.get_health()
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "/api/v1/health" in log_text
    assert "status=no_response" in log_text


# ---------------------------------------------------------------------------
# Base URL resolution
# ---------------------------------------------------------------------------


def test_base_url_defaults_to_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PORTFOLIO_TRACKER_API_URL", raising=False)
    client = tv1.TrackerV1Client()
    assert client.base_url == "http://127.0.0.1:8000"


def test_base_url_honors_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORTFOLIO_TRACKER_API_URL", "http://127.0.0.1:9999/")
    client = tv1.TrackerV1Client()
    assert client.base_url == "http://127.0.0.1:9999"


def test_base_url_explicit_arg_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORTFOLIO_TRACKER_API_URL", "http://127.0.0.1:9999")
    client = tv1.TrackerV1Client(base_url="http://127.0.0.1:7777")
    assert client.base_url == "http://127.0.0.1:7777"


# ---------------------------------------------------------------------------
# probe_v1()
# ---------------------------------------------------------------------------


def test_probe_v1_hits_health_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_urls: list[str] = []

    def fake_get(
        self: requests.Session, url: str, params: object = None, timeout: object = None
    ) -> _FakeResponse:
        seen_urls.append(url)
        return _FakeResponse(_load(FIXTURES_DIR / "health.json"))

    monkeypatch.setattr(requests.Session, "get", fake_get)
    # The no-arg constructor resolves its base URL through the environment, so
    # this assertion is only about the COMPILED-IN default. Without the delenv
    # the test reads the developer's own tracker URL and fails on any machine
    # that sets one (it does here: PORTFOLIO_TRACKER_API_URL=...:8001).
    monkeypatch.delenv("PORTFOLIO_TRACKER_API_URL", raising=False)
    client = tv1.TrackerV1Client()
    fetch = client.probe_v1()
    assert fetch.available is True
    assert fetch.data is not None
    assert fetch.data.status == "ok"
    assert seen_urls == ["http://127.0.0.1:8000/api/v1/health"]
