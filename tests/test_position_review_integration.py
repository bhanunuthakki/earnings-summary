"""Integration gaps flagged by the 2026-07-02 adversarial tax review, closed
end-to-end THROUGH ``advisor.position_review.build_pre_analysis`` — the real
orchestrator, not a stubbed stand-in. Every other position-review test
monkeypatches ``build_pre_analysis`` itself (fast, hermetic, unit-scoped); this
file is the complement: a real ``portfolio.db`` at head (``db.init_db()`` +
``alembic upgrade head``), the real tracker client with only its HTTP
boundary (``requests.get``) mocked (the ``test_portfolio_tracker_client.py``
pattern), and — for the tax-profile override — a REAL ``data/tax_profile.json``
file on disk.

Three gaps:

  (a) a multi-account ticker (taxable + Roth) where one taxable account has a
      share TRANSFER in its history — lot reconstruction must degrade to
      ``approximate`` with the transfer named in ``approx_reasons``, and the
      pre-analysis / tax block must still render (never raise);
  (b) the tracker-offline path exercised THROUGH ``build_pre_analysis`` (not
      ``unavailable_tax_view`` called directly) — a ConnectionError at the
      HTTP boundary must surface as the one-line "tracker offline (...)"
      degrade in ``pre.tax``;
  (c) a real ``data/tax_profile.json`` override actually changing the rates
      used in ``render_tax_lines``' rendered footnote, with the override
      file's path cited as the source.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import requests
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from advisor.position_review import build_pre_analysis, render_tax_lines  # noqa: E402
from integrations import portfolio_tracker_client as ptc  # noqa: E402


def _cfg(db_file: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
    return cfg


@pytest.fixture
def repo_root(tmp_path: Path) -> Iterator[Path]:
    """A real ``<repo_root>/data/portfolio.db`` at alembic head — mirrors
    ``test_dcf_live_write.py``'s fixture (init_db + stamp baseline + upgrade
    head) so ``build_pre_analysis``'s conn-backed reads (break-rules, DCF,
    instrument kind, sizing intents, notes/insights) hit real tables, not a
    hand-rolled approximation that could drift from the real schema."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_file = data_dir / "portfolio.db"
    import db as dbmod

    saved = (dbmod.DB_PATH, dbmod.DATA_DIR, dbmod.FMP_DIR)
    dbmod.set_db_path(str(db_file))
    dbmod.init_db()
    cfg = _cfg(db_file)
    command.stamp(cfg, "0000_baseline")
    command.upgrade(cfg, "head")
    try:
        yield tmp_path
    finally:
        dbmod.DB_PATH, dbmod.DATA_DIR, dbmod.FMP_DIR = saved


class _FakeResp:
    def __init__(self, payload: object, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self) -> object:
        return self._payload


# --------------------------------------------------------------------------- #
# (a) multi-account ticker with a transfer -> approximate, honest degrade
# --------------------------------------------------------------------------- #

_MULTI_ACCOUNT_HOLDINGS = [
    {
        "ticker": "RBRK",
        "name": "Rubrik",
        "security_id": 1,
        "total_quantity": "150",
        "total_value": "15000.00",
        "total_cost_basis": "9000.00",
        "unrealized_pnl": "6000.00",
        "has_unreliable_cost_basis": False,
        "currency": "USD",
        "accounts": [
            {
                "account_id": 6,
                "account_name": "Fidelity Brokerage",
                "quantity": "100",
                "institution_value": "10000.00",
                "cost_basis": "6000.00",
            },
            {
                "account_id": 5,
                "account_name": "RH Roth IRA",
                "quantity": "50",
                "institution_value": "5000.00",
                "cost_basis": "3000.00",
            },
        ],
    }
]
_MULTI_ACCOUNT_ITEMS = [
    {
        "item_id": 1,
        "institution_name": "Robinhood",
        "accounts": [
            {"account_id": 5, "name": "RH Roth IRA", "type": "investment", "subtype": "roth"}
        ],
    },
    {
        "item_id": 2,
        "institution_name": "Fidelity",
        "accounts": [
            {
                "account_id": 6,
                "name": "Fidelity Brokerage",
                "type": "investment",
                "subtype": "brokerage",
            }
        ],
    },
]
# The taxable (Fidelity) account bought 80 shares, then received a 20-share
# TRANSFER-IN (no basis carried) to reach the live 100 -- lot reconstruction
# cannot price the transferred lot's basis, so the whole taxable
# reconstruction must degrade to approximate.
_MULTI_ACCOUNT_TXNS = [
    {
        "plaid_investment_transaction_id": "t1",
        "account_id": 6,
        "account_name": "Fidelity Brokerage",
        "security_id": 1,
        "ticker": "RBRK",
        "date": "2025-02-01",
        "name": "BUY RBRK",
        "quantity": "80",
        "amount": "-4800.00",
        "type": "buy",
        "subtype": "buy",
        "currency": "USD",
    },
    {
        "plaid_investment_transaction_id": "t2",
        "account_id": 6,
        "account_name": "Fidelity Brokerage",
        "security_id": 1,
        "ticker": "RBRK",
        "date": "2025-08-15",
        "name": "TRANSFER RBRK",
        "quantity": "20",
        "amount": None,
        "type": "transfer",
        "subtype": "transfer",
        "currency": "USD",
    },
]


def _route_multi_account(url: str) -> _FakeResp:
    if "/holdings" in url:
        return _FakeResp(_MULTI_ACCOUNT_HOLDINGS)
    if "/plaid/items" in url:
        return _FakeResp(_MULTI_ACCOUNT_ITEMS)
    if "/transactions" in url:
        return _FakeResp(_MULTI_ACCOUNT_TXNS)
    return _FakeResp([], 404)


def test_multi_account_ticker_with_transfer_degrades_to_approximate(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _get(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        return _route_multi_account(url)

    monkeypatch.setattr(ptc.requests, "get", _get)
    monkeypatch.setenv("PORTFOLIO_TRACKER_API_URL", "http://tracker.test")

    pre = build_pre_analysis(repo_root, "RBRK", db_path=repo_root / "data" / "portfolio.db")

    assert pre.tax is not None and pre.tax.available
    assert pre.tax.approximate, "a taxable-account transfer must force an approximate view"
    assert any("transfer" in r.lower() for r in pre.tax.approx_reasons), pre.tax.approx_reasons
    # Sizing still resolves from the live tracker despite the tax degrade —
    # the two seams are independent (tax approximation never blocks sizing).
    assert pre.weight_source == "live"
    assert pre.market_value_usd == pytest.approx(15_000.0)
    # The rendered block is honest, not a crash: an approx line names the reason.
    lines = "\n".join(render_tax_lines(pre.tax))
    assert "(approx)" in lines
    assert "transfer" in lines.lower()


# --------------------------------------------------------------------------- #
# (b) tracker-offline path exercised THROUGH build_pre_analysis
# --------------------------------------------------------------------------- #


def test_tracker_offline_through_build_pre_analysis_degrades_honestly(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not stubbed at ``build_pre_analysis`` — a ConnectionError at the real
    HTTP boundary (``requests.get``) must surface as the tax view's one-line
    'tracker offline (...)' degrade, and the rest of the pre-analysis
    (weight from the materialized cache, break-rules, valuation) must still
    assemble rather than raising."""

    def _boom(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(ptc.requests, "get", _boom)
    monkeypatch.setenv("PORTFOLIO_TRACKER_API_URL", "http://tracker.test")

    pre = build_pre_analysis(repo_root, "RBRK", db_path=repo_root / "data" / "portfolio.db")

    assert pre.weight_source in ("unknown", "materialized")  # never "live" when offline
    assert pre.tax is not None
    assert pre.tax.available is False
    assert pre.tax.reason is not None and "tracker offline" in pre.tax.reason
    assert "ConnectionError" in pre.tax.reason
    lines = render_tax_lines(pre.tax)
    assert lines == [f"- Tax: unavailable ({pre.tax.reason})"]


# --------------------------------------------------------------------------- #
# (c) real data/tax_profile.json override switches the rendered rates
# --------------------------------------------------------------------------- #

_SINGLE_ACCOUNT_HOLDINGS = [
    {
        "ticker": "RBRK",
        "name": "Rubrik",
        "security_id": 1,
        "total_quantity": "100",
        "total_value": "10000.00",
        "total_cost_basis": "6000.00",
        "unrealized_pnl": "4000.00",
        "has_unreliable_cost_basis": False,
        "currency": "USD",
        "accounts": [
            {
                "account_id": 6,
                "account_name": "Fidelity Brokerage",
                "quantity": "100",
                "institution_value": "10000.00",
                "cost_basis": "6000.00",
            }
        ],
    }
]
_SINGLE_ACCOUNT_ITEMS = [
    {
        "item_id": 2,
        "institution_name": "Fidelity",
        "accounts": [
            {
                "account_id": 6,
                "name": "Fidelity Brokerage",
                "type": "investment",
                "subtype": "brokerage",
            }
        ],
    }
]
_SINGLE_ACCOUNT_TXNS = [
    {
        "plaid_investment_transaction_id": "t1",
        "account_id": 6,
        "account_name": "Fidelity Brokerage",
        "security_id": 1,
        "ticker": "RBRK",
        "date": "2024-01-15",
        "name": "BUY RBRK",
        "quantity": "100",
        "amount": "-6000.00",
        "type": "buy",
        "subtype": "buy",
        "currency": "USD",
    }
]


def _route_single_account(url: str) -> _FakeResp:
    if "/holdings" in url:
        return _FakeResp(_SINGLE_ACCOUNT_HOLDINGS)
    if "/plaid/items" in url:
        return _FakeResp(_SINGLE_ACCOUNT_ITEMS)
    if "/transactions" in url:
        return _FakeResp(_SINGLE_ACCOUNT_TXNS)
    return _FakeResp([], 404)


def test_real_tax_profile_override_switches_rendered_rates_and_cites_source(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hand-edited ``data/tax_profile.json`` (the gitignored override file —
    ``load_tax_profile``'s documented contract) must actually change the tax
    dollars rendered in the /review block, with the footnote naming the
    override file as the source rather than silently keeping the defaults."""
    override = {
        "federal_ordinary_rate": 0.37,
        "federal_lt_rate": 0.20,
        "niit_rate": 0.038,
        "state_rate": 0.0,  # e.g. moved to a no-income-tax state
        "magi_assumption": "2026 MFJ, $650k MAGI, TX resident (no state tax)",
    }
    (repo_root / "data" / "tax_profile.json").write_text(json.dumps(override), encoding="utf-8")

    def _get(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        return _route_single_account(url)

    monkeypatch.setattr(ptc.requests, "get", _get)
    monkeypatch.setenv("PORTFOLIO_TRACKER_API_URL", "http://tracker.test")

    pre = build_pre_analysis(repo_root, "RBRK", db_path=repo_root / "data" / "portfolio.db")

    assert pre.tax is not None and pre.tax.available
    assert "$650k MAGI, TX resident" in pre.tax.footnote
    assert "data/tax_profile.json" in pre.tax.footnote
    # The all-in ST rate is now 37% + 3.8% + 0% = 40.8%, not the default 45.1%.
    trim = pre.tax.trim
    assert trim is not None
    expected_st_rate = 0.37 + 0.038 + 0.0
    if trim.st_gain_usd is not None and trim.st_gain_usd > 0:
        assert trim.tax_low_usd == pytest.approx(trim.st_gain_usd * expected_st_rate, rel=1e-6)
    lines = "\n".join(render_tax_lines(pre.tax))
    assert "TX resident" in lines


def test_tax_profile_source_defaults_when_no_override_file(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The complement: with no override file on disk, the footnote cites the
    default profile, not a phantom override path."""

    def _get(url: str, timeout: float | None = None, params: object = None) -> _FakeResp:
        return _route_single_account(url)

    monkeypatch.setattr(ptc.requests, "get", _get)
    monkeypatch.setenv("PORTFOLIO_TRACKER_API_URL", "http://tracker.test")

    assert not (repo_root / "data" / "tax_profile.json").exists()
    pre = build_pre_analysis(repo_root, "RBRK", db_path=repo_root / "data" / "portfolio.db")
    assert pre.tax is not None and pre.tax.available
    assert "$450-500k MAGI" in pre.tax.footnote  # the owner-approved default
