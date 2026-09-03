from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

PRICE_AS_OF = "2026-09-03T11:45:00Z"
PRICE_AT = datetime(2026, 9, 3, 11, 45, tzinfo=UTC)

import comments_server  # noqa: E402
import simulate_trade_impact as simulation  # noqa: E402

from dcf.latest import LatestDcfRow  # noqa: E402


def _write_weights(repo_root: Path, weights: dict[str, float]) -> None:
    path = repo_root / "data" / "portfolio_weights.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"computed_at": "2026-09-03T12:00:00", "weights": weights}),
        encoding="utf-8",
    )


def _missing_db(repo_root: Path) -> Path:
    return repo_root / "missing.db"


def test_simulation_uses_observed_weight_and_explicit_trade_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_weights(tmp_path, {"ABC": 0.035})
    monkeypatch.setattr(simulation, "configured_db_path", _missing_db)
    request = simulation.SimulateTradeRequest(
        ticker="abc",
        action="add",
        shares=100,
        target_weight=0.05,
        estimated_price=12.5,
        price_currency="USD",
        price_as_of=PRICE_AT,
    )

    result = simulation.simulate_trade_impact(tmp_path, request)

    assert result.current_weight == 0.035
    assert result.projected_weight == 0.05
    assert result.weight_delta == pytest.approx(0.015)
    assert result.estimated_capital_delta == 1_250
    assert result.cash_impact_usd == -1_250
    assert result.weights_as_of.isoformat() == "2026-09-03T12:00:00+00:00"
    assert result.price_source == "request"
    assert result.price_currency == "USD"
    assert result.price_as_of.isoformat() == "2026-09-03T11:45:00+00:00"
    assert result.price_source_ref == "request"
    assert "not modeled" in result.risk_summary


def test_simulation_fails_closed_without_weights(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(simulation, "configured_db_path", _missing_db)
    request = simulation.SimulateTradeRequest(
        ticker="ABC",
        action="add",
        shares=100,
        target_weight=0.05,
        estimated_price=12.5,
        price_currency="USD",
        price_as_of=PRICE_AT,
    )

    with pytest.raises(
        simulation.SimulationDataUnavailableError, match="portfolio_weights_unavailable"
    ):
        simulation.simulate_trade_impact(tmp_path, request)


def test_simulation_requires_non_fabricated_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_weights(tmp_path, {"ABC": 0.035})
    monkeypatch.setattr(simulation, "configured_db_path", _missing_db)
    request = simulation.SimulateTradeRequest(
        ticker="ABC",
        action="add",
        shares=100,
        target_weight=0.05,
    )

    with pytest.raises(simulation.SimulationDataUnavailableError, match="price_unavailable"):
        simulation.simulate_trade_impact(tmp_path, request)


def test_simulation_rejects_direction_mismatch(tmp_path: Path) -> None:
    _write_weights(tmp_path, {"ABC": 0.05})
    request = simulation.SimulateTradeRequest(
        ticker="ABC",
        action="add",
        shares=100,
        target_weight=0.04,
        estimated_price=12.5,
        price_currency="USD",
        price_as_of=PRICE_AT,
    )

    with pytest.raises(ValueError, match="cannot reduce"):
        simulation.simulate_trade_impact(tmp_path, request)


@pytest.mark.parametrize("field", ["shares", "target_weight"])
def test_simulation_requires_trade_dimensions(field: str) -> None:
    payload: dict[str, object] = {
        "ticker": "ABC",
        "action": "add",
        "shares": 100,
        "target_weight": 0.05,
        "estimated_price": 12.5,
        "price_currency": "USD",
        "price_as_of": PRICE_AS_OF,
    }
    payload.pop(field)

    with pytest.raises(ValidationError):
        simulation.SimulateTradeRequest.model_validate(payload)


def test_simulation_route_surfaces_unavailable_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(simulation, "configured_db_path", _missing_db)
    response = (
        comments_server.create_app(tmp_path)
        .test_client()
        .post(
            "/api/positioning/simulate",
            json={
                "ticker": "ABC",
                "action": "add",
                "shares": 100,
                "target_weight": 0.05,
                "estimated_price": 12.5,
                "price_currency": "USD",
                "price_as_of": PRICE_AS_OF,
            },
        )
    )

    assert response.status_code == 503
    assert response.get_json() == {"error": "portfolio_weights_unavailable"}


def test_simulation_route_rejects_missing_trade_dimensions(tmp_path: Path) -> None:
    response = (
        comments_server.create_app(tmp_path)
        .test_client()
        .post(
            "/api/positioning/simulate",
            json={"ticker": "ABC", "action": "add", "estimated_price": 12.5},
        )
    )

    assert response.status_code == 400
    assert "shares" in response.get_json()["error"]


@pytest.mark.parametrize(
    ("field", "value"),
    [("shares", float("inf")), ("target_weight", float("nan")), ("estimated_price", float("inf"))],
)
def test_simulation_rejects_non_finite_numbers(field: str, value: float) -> None:
    payload: dict[str, object] = {
        "ticker": "ABC",
        "action": "add",
        "shares": 100,
        "target_weight": 0.05,
        "estimated_price": 12.5,
        "price_currency": "USD",
        "price_as_of": PRICE_AS_OF,
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        simulation.SimulateTradeRequest.model_validate(payload)


@pytest.mark.parametrize("missing", ["price_currency", "price_as_of"])
def test_explicit_price_requires_currency_and_observed_time(missing: str) -> None:
    payload: dict[str, object] = {
        "ticker": "ABC",
        "action": "add",
        "shares": 100,
        "target_weight": 0.05,
        "estimated_price": 12.5,
        "price_currency": "USD",
        "price_as_of": PRICE_AS_OF,
    }
    payload.pop(missing)

    with pytest.raises(ValidationError):
        simulation.SimulateTradeRequest.model_validate(payload)


def test_explicit_price_rejects_ambiguous_timestamp() -> None:
    with pytest.raises(ValidationError, match="timezone offset"):
        simulation.SimulateTradeRequest(
            ticker="ABC",
            action="add",
            shares=100,
            target_weight=0.05,
            estimated_price=12.5,
            price_currency="USD",
            price_as_of=datetime(2026, 9, 3, 11, 45),
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"computed_at": PRICE_AS_OF, "weights": "corrupt"},
        {"computed_at": "not-a-timestamp", "weights": {"ABC": 0.03}},
        {"computed_at": PRICE_AS_OF, "weights": {"ABC": float("nan")}},
        {"computed_at": PRICE_AS_OF, "weights": {"ABC": 1.2}},
        {"computed_at": PRICE_AS_OF, "weights": {" ABC ": 0.03}},
    ],
)
def test_simulation_fails_closed_on_malformed_weight_snapshot(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    path = tmp_path / "data" / "portfolio_weights.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    request = simulation.SimulateTradeRequest(
        ticker="ABC",
        action="add",
        shares=100,
        target_weight=0.05,
        estimated_price=12.5,
        price_currency="USD",
        price_as_of=PRICE_AT,
    )

    with pytest.raises(
        simulation.SimulationDataUnavailableError, match="portfolio_weights_unavailable"
    ):
        simulation.simulate_trade_impact(tmp_path, request)


def test_simulation_api_rejects_infinite_trade_value(tmp_path: Path) -> None:
    response = (
        comments_server.create_app(tmp_path)
        .test_client()
        .post(
            "/api/positioning/simulate",
            json={
                "ticker": "ABC",
                "action": "add",
                "shares": float("inf"),
                "target_weight": 0.05,
                "estimated_price": 12.5,
                "price_currency": "USD",
                "price_as_of": PRICE_AS_OF,
            },
        )
    )

    assert response.status_code == 400


def _dcf_row(
    *, currency: str | None = "USD", live_price_at: str | None = PRICE_AS_OF
) -> LatestDcfRow:
    return LatestDcfRow(
        ticker="ABC",
        id=42,
        created_at="2026-09-03T12:00:00Z",
        valuation_date="2026-09-03",
        npv_per_share=20.0,
        live_price=12.5,
        currency=currency,
        live_price_at=live_price_at,
        over_under_pct=None,
        sanity_flag=None,
        assumption_snapshot_json=None,
    )


def test_simulation_uses_dcf_price_with_currency_and_source_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_weights(tmp_path, {"ABC": 0.035})
    db_path = tmp_path / "dcf.db"
    sqlite_conn = simulation.sqlite3.connect(db_path)
    sqlite_conn.close()

    def configured_db(_repo_root: Path) -> Path:
        return db_path

    def latest_row(_conn: object, _ticker: str) -> LatestDcfRow:
        return _dcf_row()

    monkeypatch.setattr(simulation, "configured_db_path", configured_db)
    monkeypatch.setattr(simulation, "latest_dcf_row", latest_row)
    request = simulation.SimulateTradeRequest(
        ticker="ABC", action="add", shares=100, target_weight=0.05
    )

    result = simulation.simulate_trade_impact(tmp_path, request)

    assert result.price_source == "dcf"
    assert result.price_source_ref == "dcf_runs:42"
    assert result.price_currency == "USD"
    assert result.price_as_of.isoformat() == "2026-09-03T11:45:00+00:00"
    assert result.cash_impact_usd == -1_250


@pytest.mark.parametrize(
    ("currency", "live_price_at"),
    [(None, PRICE_AS_OF), ("EUR", PRICE_AS_OF), ("USD", None), ("USD", "invalid")],
)
def test_simulation_rejects_dcf_price_without_usd_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    currency: str | None,
    live_price_at: str | None,
) -> None:
    _write_weights(tmp_path, {"ABC": 0.035})
    db_path = tmp_path / "dcf.db"
    sqlite_conn = simulation.sqlite3.connect(db_path)
    sqlite_conn.close()

    def configured_db(_repo_root: Path) -> Path:
        return db_path

    def latest_row(_conn: object, _ticker: str) -> LatestDcfRow:
        return _dcf_row(currency=currency, live_price_at=live_price_at)

    monkeypatch.setattr(simulation, "configured_db_path", configured_db)
    monkeypatch.setattr(simulation, "latest_dcf_row", latest_row)
    request = simulation.SimulateTradeRequest(
        ticker="ABC", action="add", shares=100, target_weight=0.05
    )

    with pytest.raises(simulation.SimulationDataUnavailableError, match="price_unavailable"):
        simulation.simulate_trade_impact(tmp_path, request)
