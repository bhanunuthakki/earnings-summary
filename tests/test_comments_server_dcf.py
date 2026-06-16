"""Tests for the in-app DCF recompute API on comments_server.py.

The recompute route is the heart of the modify→recompute loop (resolves the
Sheets/Excel round-trip gap): it reconstructs an edited assumption set from JSON
via ``RedesignInputs.from_dict`` and runs the pure engine — no xlsx, no DB. The
inputs GET seeds the editable card from the live workbook.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from flask.testing import FlaskClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402

from dcf import redesign  # noqa: E402

_BASE = redesign.RedesignInputs(
    segments=("Total company",),
    base_revenue_by_segment={"Total company": 1000.0},
    near_growth_by_segment={"Total company": 0.10},
    terminal_growth_by_segment={"Total company": 0.03},
    near_op_margin=0.20,
    terminal_op_margin=0.25,
    tax_rate=0.24,
    capex_2026_m=60.0,
    terminal_capex_da=1.05,
    da_ratio=0.05,
    consensus_years=5,
    wacc=0.09,
    beta=1.2,
    risk_free_rate=0.043,
    equity_risk_premium=0.045,
    cost_of_debt=0.045,
    terminal_method="Exit multiple",
    terminal_basis="EV/EBITDA",
    exit_multiple=12.0,
    terminal_growth_g=0.03,
    current_price=50.0,
    cash_m=100.0,
    total_debt_m=200.0,
    diluted_shares_m=100.0,
    fx_to_usd=1.0,
)


@pytest.fixture
def client(tmp_path: Path) -> FlaskClient:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # create_app only stores the db path; the recompute/inputs routes never open
    # it, but mirror the real layout so the app boots like production.
    sqlite3.connect(str(data_dir / "portfolio.db")).close()
    app = comments_server.create_app(tmp_path)
    return app.test_client()


def test_recompute_returns_scenarios_and_grid(client: FlaskClient) -> None:
    resp = client.post("/api/dcf/recompute", json={"inputs": _BASE.to_dict()})
    assert resp.status_code == 200
    body = resp.get_json()
    base = body["fair_value_per_share_usd"]
    assert base == pytest.approx(redesign.value(_BASE).value_per_share_usd)
    sc = body["scenarios"]
    assert sc["bear"] < sc["base"] < sc["bull"]
    assert sc["base"] == pytest.approx(base)
    # over/under is the decimal (live-fair)/fair convention (0076).
    assert body["over_under_pct"] == pytest.approx((_BASE.current_price - base) / base)
    grid = body["sensitivity"]
    assert len(grid["wacc_axis"]) == 7
    assert len(grid["values"]) == 7 and len(grid["values"][0]) == 7
    assert grid["values"][3][3] == pytest.approx(base)


def test_recompute_reflects_an_edit(client: FlaskClient) -> None:
    edited = _BASE.to_dict()
    edited["wacc"] = 0.13
    resp = client.post("/api/dcf/recompute", json={"inputs": edited})
    assert resp.status_code == 200
    assert resp.get_json()["fair_value_per_share_usd"] < redesign.value(_BASE).value_per_share_usd


def test_recompute_requires_inputs(client: FlaskClient) -> None:
    assert client.post("/api/dcf/recompute", json={}).status_code == 400


def test_recompute_rejects_invalid_inputs(client: FlaskClient) -> None:
    bad = _BASE.to_dict()
    del bad["wacc"]
    resp = client.post("/api/dcf/recompute", json={"inputs": bad})
    assert resp.status_code == 400
    assert "wacc" in resp.get_json()["error"]


def test_recompute_degenerate_base_returns_422(client: FlaskClient) -> None:
    """A perpetuity terminal with WACC ≤ g is well-formed but un-valuable —
    surfaced as 422 with the reason, not a 500."""
    degenerate = _BASE.to_dict()
    degenerate["terminal_method"] = "Perpetuity"
    degenerate["wacc"] = 0.025
    degenerate["terminal_growth_g"] = 0.03
    resp = client.post("/api/dcf/recompute", json={"inputs": degenerate})
    assert resp.status_code == 422
    assert "WACC" in resp.get_json()["error"]


def test_recompute_options_preflight(client: FlaskClient) -> None:
    assert client.open("/api/dcf/recompute", method="OPTIONS").status_code == 204


def test_inputs_404_when_no_workbook(client: FlaskClient) -> None:
    assert client.get("/api/dcf/inputs/NU").status_code == 404


def test_save_requires_ticker_and_inputs(client: FlaskClient) -> None:
    assert client.post("/api/dcf/save", json={"inputs": _BASE.to_dict()}).status_code == 400
    assert client.post("/api/dcf/save", json={"ticker": "NU"}).status_code == 400


def test_save_409_when_no_workbook(client: FlaskClient) -> None:
    """A well-formed save with no redesigned workbook to edit is a 409, not a
    500 — the durable-save end-to-end path is covered in test_dcf_redesign."""
    resp = client.post("/api/dcf/save", json={"ticker": "NU", "inputs": _BASE.to_dict()})
    assert resp.status_code == 409
    assert "no redesigned workbook" in resp.get_json()["error"]


def test_save_rejects_degenerate_inputs(client: FlaskClient) -> None:
    degenerate = _BASE.to_dict()
    degenerate["terminal_method"] = "Perpetuity"
    degenerate["wacc"] = 0.025
    degenerate["terminal_growth_g"] = 0.03
    resp = client.post("/api/dcf/save", json={"ticker": "NU", "inputs": degenerate})
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# Inject-fact route (S6) — the DIY fact → DCF driver bridge. HTTP contract here;
# the value resolution / converter / round-trip live in test_fact_drivers and
# test_dcf_redesign.
# --------------------------------------------------------------------------- #
def test_inject_fact_options_preflight(client: FlaskClient) -> None:
    assert client.open("/api/dcf/inject-fact", method="OPTIONS").status_code == 204


def test_inject_fact_requires_ticker_token_field(client: FlaskClient) -> None:
    assert client.post("/api/dcf/inject-fact", json={}).status_code == 400
    assert client.post("/api/dcf/inject-fact", json={"ticker": "NU"}).status_code == 400
    assert (
        client.post("/api/dcf/inject-fact", json={"ticker": "NU", "token": "kpi:ROE"}).status_code
        == 400
    )


def test_inject_fact_rejects_wacc_target(client: FlaskClient) -> None:
    """WACC is not an injectable field (no input cell) — the route 400s an
    attempt to target it directly (the guardrail surfaced to the client)."""
    resp = client.post(
        "/api/dcf/inject-fact", json={"ticker": "NU", "token": "kpi:ROE", "field": "wacc"}
    )
    assert resp.status_code == 400
    assert "unknown driver field" in resp.get_json()["error"]


def test_inject_fact_rejects_unparseable_token(client: FlaskClient) -> None:
    resp = client.post(
        "/api/dcf/inject-fact",
        json={"ticker": "NU", "token": "not-a-token", "field": "near_op_margin"},
    )
    assert resp.status_code == 400


def test_inject_fact_404_when_no_fcff_workbook(client: FlaskClient) -> None:
    """A well-formed request for a ticker with no redesigned FCFF workbook (a
    never-built name, or an archetype model) is a 404 — the happy-path reprice +
    JSON/xlsx sync is covered end-to-end in test_dcf_redesign."""
    resp = client.post(
        "/api/dcf/inject-fact",
        json={"ticker": "NU", "token": "kpi:ROE", "field": "near_op_margin"},
    )
    assert resp.status_code == 404
    assert "no editable FCFF DCF model" in resp.get_json()["error"]


# --------------------------------------------------------------------------- #
# Inject-fact-SHEET route (S7) — the DIY fact → DCF reference-sheet bridge. HTTP
# contract here; the survival-across-refresh proof lives in test_dcf_redesign.
# --------------------------------------------------------------------------- #
def test_inject_fact_sheet_options_preflight(client: FlaskClient) -> None:
    assert client.open("/api/dcf/inject-fact-sheet", method="OPTIONS").status_code == 204


def test_inject_fact_sheet_requires_ticker_and_token(client: FlaskClient) -> None:
    assert client.post("/api/dcf/inject-fact-sheet", json={}).status_code == 400
    assert client.post("/api/dcf/inject-fact-sheet", json={"ticker": "NU"}).status_code == 400


def test_inject_fact_sheet_rejects_unparseable_token(client: FlaskClient) -> None:
    resp = client.post("/api/dcf/inject-fact-sheet", json={"ticker": "NU", "token": "not-a-token"})
    assert resp.status_code == 400


def test_inject_fact_sheet_404_when_no_dcf_model(client: FlaskClient) -> None:
    """A reference attaches to a ticker's DCF; a never-built name is a 404. The
    happy-path write + survival is covered end-to-end in test_dcf_redesign."""
    resp = client.post("/api/dcf/inject-fact-sheet", json={"ticker": "NU", "token": "kpi:ROE"})
    assert resp.status_code == 404
    assert "no DCF model" in resp.get_json()["error"]


def test_reference_facts_empty_when_none(client: FlaskClient) -> None:
    resp = client.get("/api/dcf/reference-facts/NU")
    assert resp.status_code == 200
    assert resp.get_json() == {"ticker": "NU", "facts": []}
