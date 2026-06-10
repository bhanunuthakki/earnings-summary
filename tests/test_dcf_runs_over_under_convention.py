"""Regression: bespoke dcf_runs writers must store over_under_pct as the
documented DECIMAL RATIO (live_price - npv_per_share) / npv_per_share.

Convention: alembic/versions/0024_dcf_runs_extras.py + src/dcf/valuation.py
over_under_pct(). The analytical dashboard's trigger ladder reads the STORED
column (> 0.20 -> 'sell', > 0.10 -> 'trim', < -mos_bar -> initiate candidate).

The bank / holdco-SOTP / fintech-SOTP / platform-DCF builders originally
persisted percent UPSIDE, (fair / price - 1) * 100 -- wrong sign AND scale --
which made deeply UNDER-valued names (NU +79.82, BN +26.56) classify as 'sell'.
Each test drives the real persist_dcf_run with an under-valued fixture and
asserts the stored row is self-consistent under the ratio convention: negative,
decimal-scale, and exactly (live - fair) / fair.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import build_bank_dcf as bank  # noqa: E402
import build_fintech_sotp as fintech  # noqa: E402
import build_holdco_sotp as holdco  # noqa: E402
import build_nu_platform_dcf as platform_dcf  # noqa: E402

_DCF_RUNS_SCHEMA = """
CREATE TABLE dcf_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT UNIQUE,
    valuation_date TEXT, horizon_years INTEGER,
    wacc REAL, terminal_growth REAL,
    npv REAL, npv_per_share REAL, shares_outstanding REAL,
    currency TEXT, notes TEXT, run_id TEXT,
    live_price REAL, live_price_at TEXT, over_under_pct REAL,
    mos_bar_used REAL, assumption_snapshot_json TEXT,
    revenue_growths_json TEXT, fcf_margin REAL
);
"""


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """Throwaway repo root holding an empty dcf_runs table."""
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(_DCF_RUNS_SCHEMA)
    return tmp_path


def _stored(repo_root: Path, ticker: str) -> tuple[float, float, float]:
    """(live_price, npv_per_share, over_under_pct) of the persisted row."""
    with sqlite3.connect(str(repo_root / "data" / "portfolio.db")) as conn:
        row = conn.execute(
            "SELECT live_price, npv_per_share, over_under_pct FROM dcf_runs WHERE ticker = ?",
            (ticker,),
        ).fetchone()
    assert row is not None, f"no dcf_runs row for {ticker}"
    live, fair, ou = row
    assert live is not None and fair is not None and ou is not None
    return float(live), float(fair), float(ou)


def _assert_ratio_convention(live: float, fair: float, stored: float) -> None:
    assert stored == pytest.approx((live - fair) / fair)
    # The fixtures trade BELOW fair value: the ratio must be negative and on
    # decimal scale. Percent-upside (the original bug) fails both: positive,
    # and tens-of-points magnitude.
    assert live < fair
    assert stored < 0
    assert abs(stored) < 1.0


def test_bank_writer_stores_decimal_ratio(monkeypatch: pytest.MonkeyPatch, repo_root: Path) -> None:
    monkeypatch.setattr(bank, "REPO", repo_root)
    a = bank.Actuals(
        book=10000.0,
        ea=22700.0,
        nii=3859.0,
        fees=2000.0,
        opex=1500.0,
        credit_cost=1000.0,
        pretax=3359.0,
        tax_rate=0.25,
        ni=2519.0,
        equity=4000.0,
        equity_prior=3500.0,
        shares=1000.0,
        price=10.0,
    )
    s = bank.Assum()
    m = bank.mirror(a, s)
    assert a.price < m.vps_usd  # fixture is under-valued
    assert bank.persist_dcf_run(a, s, m)
    live, fair, stored = _stored(repo_root, bank.T)
    assert fair == pytest.approx(m.vps_usd)
    _assert_ratio_convention(live, fair, stored)


def test_holdco_sotp_writer_stores_decimal_ratio(
    monkeypatch: pytest.MonkeyPatch, repo_root: Path
) -> None:
    monkeypatch.setattr(holdco, "REPO", repo_root)
    # eq $B, value/share $60, live price $45: trading 25% below NAV.
    assert holdco.persist_dcf_run(100.0, 60.0, 45.0, 0.08, {"model": "holdco_sotp"})
    live, fair, stored = _stored(repo_root, holdco.T)
    assert stored == pytest.approx(-0.25)
    _assert_ratio_convention(live, fair, stored)


def test_fintech_sotp_writer_stores_decimal_ratio(
    monkeypatch: pytest.MonkeyPatch, repo_root: Path
) -> None:
    monkeypatch.setattr(fintech, "REPO", repo_root)
    s = fintech.Sotp(price=5.0)  # default SOTP value/share is ~$9-10: under-valued
    eq, vps = fintech.value(s)
    assert s.price < vps
    assert fintech.persist_dcf_run(s, eq, vps)
    live, fair, stored = _stored(repo_root, fintech.T)
    assert fair == pytest.approx(vps)
    _assert_ratio_convention(live, fair, stored)


def test_platform_dcf_writer_stores_decimal_ratio(
    monkeypatch: pytest.MonkeyPatch, repo_root: Path
) -> None:
    """The literal incident shape: NU platform DCF, price ~$12 vs value/share
    ~$22, must persist a negative decimal (~ -0.44), not +79.82."""
    monkeypatch.setattr(platform_dcf, "REPO", repo_root)
    s = platform_dcf.Assum()
    m = platform_dcf.mirror(s)
    assert s.price < m.vps
    assert platform_dcf.persist_dcf_run(s, m)
    live, fair, stored = _stored(repo_root, platform_dcf.T)
    assert fair == pytest.approx(m.vps)
    _assert_ratio_convention(live, fair, stored)
