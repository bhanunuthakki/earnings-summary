"""Tests for the MELI sum-of-the-parts platform DCF
(``execution/build_meli_platform_dcf.py``): the value-of-record mirror — the SOTP
identity, the convex growth fade, the credit-book capital charge — plus the
``refresh_dcf`` routing that dispatches MELI to this builder.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from itertools import pairwise
from pathlib import Path

import pytest

from execution import build_meli_platform_dcf as meli
from execution import refresh_dcf

_BASE = meli.Assum(derive_capm=0)  # explicit scalar discount rates (no DB read)


# --------------------------------------------------------------------------- #
# Value-of-record mirror
# --------------------------------------------------------------------------- #
def test_mirror_is_deterministic() -> None:
    assert meli.mirror(_BASE).vps == meli.mirror(_BASE).vps


def test_sotp_identity_equity_is_sum_of_parts() -> None:
    """Equity value = operating EV + credit-book equity + net non-operating cash."""
    m = meli.mirror(_BASE)
    assert m.equity_value == pytest.approx(m.operating_ev + m.credit_equity_value + _BASE.net_cash)
    assert m.vps == pytest.approx(m.equity_value / _BASE.shares)


def test_operating_ev_decomposes_into_pv_stream_plus_terminal() -> None:
    m = meli.mirror(_BASE)
    assert m.operating_ev == pytest.approx(m.pv_op_fcff + m.pv_op_terminal)
    assert m.credit_equity_value == pytest.approx(m.pv_credit_fcfe + m.pv_credit_terminal)


def test_operating_revenue_is_commerce_plus_fintech_payments() -> None:
    m = meli.mirror(_BASE)
    for r in m.rows:
        assert r.op_rev == pytest.approx(r.comm_rev + r.fpay_rev)


def test_year_one_revenue_reconciles_to_base_plus_near_growth() -> None:
    m = meli.mirror(_BASE)
    y1 = m.rows[0]
    assert y1.comm_rev == pytest.approx(_BASE.comm_rev0 * (1 + _BASE.comm_g_near))
    assert y1.fpay_rev == pytest.approx(_BASE.fpay_rev0 * (1 + _BASE.fpay_g_near))


# --------------------------------------------------------------------------- #
# Convex growth fade
# --------------------------------------------------------------------------- #
def test_fade_hits_near_at_year_one_and_terminal_at_year_n() -> None:
    near, terminal = 0.30, 0.05
    assumptions = dataclasses.replace(_BASE, comm_g_near=near, comm_g_term=terminal)
    rows = meli.mirror(assumptions).rows
    rates = [rows[0].comm_rev / assumptions.comm_rev0 - 1] + [
        row.comm_rev / previous.comm_rev - 1 for previous, row in pairwise(rows)
    ]
    assert rates[0] == pytest.approx(near)
    assert rates[-1] == pytest.approx(terminal)


def test_fade_is_convex_front_loaded() -> None:
    """A convex fade sheds more growth in the first half than a straight line —
    the year-by-year rate sits BELOW the linear interpolation through the middle."""
    near, term = 0.30, 0.05
    assumptions = dataclasses.replace(_BASE, comm_g_near=near, comm_g_term=term)
    rows = meli.mirror(assumptions).rows
    rates = [rows[0].comm_rev / assumptions.comm_rev0 - 1] + [
        row.comm_rev / previous.comm_rev - 1 for previous, row in pairwise(rows)
    ]
    for index, rate in enumerate(rates[1:-1], start=2):
        linear = near + (term - near) * (index - 1) / (assumptions.years - 1)
        assert rate < linear


# --------------------------------------------------------------------------- #
# Driver sensitivities
# --------------------------------------------------------------------------- #
def test_higher_commerce_growth_raises_value() -> None:
    faster = dataclasses.replace(_BASE, comm_g_near=_BASE.comm_g_near + 0.05)
    assert meli.mirror(faster).vps > meli.mirror(_BASE).vps


def test_higher_nimal_raises_credit_value() -> None:
    richer = dataclasses.replace(_BASE, nimal_term=_BASE.nimal_term + 0.03)
    base = meli.mirror(_BASE)
    assert meli.mirror(richer).credit_equity_value > base.credit_equity_value


def test_higher_capital_ratio_lowers_near_term_credit_fcfe() -> None:
    """A heavier capital charge ties up more equity as the book grows, cutting the
    distributable credit FCFE — the capital intensity an FCFF model can't see."""
    heavier = dataclasses.replace(_BASE, cap_ratio=_BASE.cap_ratio + 0.05)
    assert meli.mirror(heavier).rows[0].credit_fcfe < meli.mirror(_BASE).rows[0].credit_fcfe


def test_higher_wacc_lowers_operating_ev() -> None:
    pricier = dataclasses.replace(_BASE, wacc=_BASE.wacc + 0.02)
    assert meli.mirror(pricier).operating_ev < meli.mirror(_BASE).operating_ev


def test_credit_terminal_roe_bounds_terminal_value() -> None:
    """A lower sustainable terminal ROE means more earnings must be retained to
    fund growth, so the credit terminal value falls."""
    lower_roe = dataclasses.replace(_BASE, credit_terminal_roe=0.18)
    assert meli.mirror(lower_roe).credit_terminal < meli.mirror(_BASE).credit_terminal


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #
def test_refresh_routes_meli_to_sotp_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A holdings ``valuation_model`` override of 'meli_platform_sotp' resolves to
    the new archetype, so refresh dispatches to the MELI SOTP builder."""
    hp = tmp_path / "micro_thesis" / "holdings" / "MELI.json"
    hp.parent.mkdir(parents=True)
    hp.write_text(
        json.dumps({"ticker": "MELI", "valuation_model": "meli_platform_sotp"}),
        encoding="utf-8",
    )
    db_path = tmp_path / "synthetic.db"
    db_path.touch()
    calls: list[list[str]] = []

    def run_builder(
        command: list[str], *, env: dict[str, str], **_: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        Path(env["DCF_PROMOTE_DEST"]).parent.mkdir(parents=True, exist_ok=True)
        Path(env["DCF_PROMOTE_DEST"]).touch()
        return subprocess.CompletedProcess(command, 0, stdout="RESULT dcf_runs=ok\n", stderr="")

    def configured_path(_root: Path) -> Path:
        return db_path

    monkeypatch.setattr(refresh_dcf, "configured_db_path", configured_path)
    monkeypatch.setattr(refresh_dcf.subprocess, "run", run_builder)
    monkeypatch.setattr(
        sys, "argv", ["refresh_dcf.py", "--ticker", "MELI", "--repo-root", str(tmp_path)]
    )

    assert refresh_dcf.main() == 0
    assert len(calls) == 1
    assert calls[0][-1].endswith("build_meli_platform_dcf.py")


def _write_geo(
    repo: Path, *, annual: list[dict[str, object]], quarterly: list[dict[str, object]]
) -> None:
    fmp = repo / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True, exist_ok=True)
    (fmp / "MELI_geo_segments_annual.json").write_text(json.dumps(annual), encoding="utf-8")
    (fmp / "MELI_geo_segments_quarterly.json").write_text(json.dumps(quarterly), encoding="utf-8")


def test_load_assumptions_records_only_the_selected_annual_geo_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_geo(
        tmp_path,
        annual=[{"fiscalYear": 2025, "period": "FY", "data": {"Brazil": 100.0}}],
        quarterly=[{"fiscalYear": 2026, "period": "Q1", "data": {"Argentina": 100.0}}],
    )
    monkeypatch.setattr(meli, "REPO", tmp_path)

    assumptions = meli.load_assumptions("MELI")

    assert assumptions.country_risk_premium == pytest.approx(
        meli.country_risk.COUNTRY_CRP["Brazil"]
    )
    assert assumptions.country_risk_source["path"] == (
        "data/historical/fmp/MELI_geo_segments_annual.json"
    )
    assert assumptions.country_risk_source["selection"] == "annual_latest_fiscal_year"


def test_load_assumptions_records_quarterly_when_annual_is_unusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_geo(
        tmp_path,
        annual=[{"fiscalYear": 2025, "period": "FY", "data": {}}],
        quarterly=[
            {"fiscalYear": 2026, "period": quarter, "data": {"Mexico": 25.0}}
            for quarter in ("Q1", "Q2", "Q3", "Q4")
        ],
    )
    monkeypatch.setattr(meli, "REPO", tmp_path)

    assumptions = meli.load_assumptions("MELI")

    assert assumptions.country_risk_premium == pytest.approx(
        meli.country_risk.COUNTRY_CRP["Mexico"]
    )
    assert assumptions.country_risk_source["path"] == (
        "data/historical/fmp/MELI_geo_segments_quarterly.json"
    )
    assert assumptions.country_risk_source["selection"] == "quarterly_latest_four"


def test_owner_country_risk_override_reads_and_records_no_geo_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = tmp_path / "data" / "bank_assumptions" / "MELI_sotp.json"
    owner.parent.mkdir(parents=True)
    owner.write_text(json.dumps({"country_risk_premium": 0.0}), encoding="utf-8")
    _write_geo(
        tmp_path,
        annual=[{"fiscalYear": 2025, "period": "FY", "data": {"Argentina": 100.0}}],
        quarterly=[],
    )
    monkeypatch.setattr(meli, "REPO", tmp_path)

    def _unexpected_geo_read(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("owner CRP override must prevent any geo read")

    monkeypatch.setattr(meli.country_risk, "country_risk_observation", _unexpected_geo_read)

    assumptions = meli.load_assumptions("MELI")

    assert assumptions.country_risk_premium == 0.0
    assert assumptions.country_risk_source == {}


def test_load_assumptions_fails_without_geo_or_owner_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(meli, "REPO", tmp_path)

    with pytest.raises(RuntimeError, match="country risk unavailable"):
        meli.load_assumptions("MELI")


def test_main_fails_before_workbook_persistence_with_infinite_geography(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_geo(
        tmp_path,
        annual=[{"fiscalYear": 2025, "period": "FY", "data": {"Brazil": float("inf")}}],
        quarterly=[],
    )
    destination = tmp_path / "MELI.xlsx"
    sentinel = b"existing-workbook-must-survive"
    destination.write_bytes(sentinel)
    monkeypatch.setattr(meli, "REPO", tmp_path)
    monkeypatch.setattr(meli, "DEST", destination)

    with pytest.raises(
        meli.country_risk.CountryRiskUnavailableError,
        match="geographic_revenue_unattributable",
    ):
        meli.main()

    assert destination.read_bytes() == sentinel
