"""Regression: dcf_runs.over_under_pct must obey the documented convention --
(live_price - npv_per_share) / npv_per_share as a DECIMAL ratio (alembic 0024,
src/dcf/valuation.over_under_pct). The analytical dashboard's trigger ladder
reads the STORED column (> 0.20 -> 'sell', > 0.10 -> 'trim', < -mos_bar ->
initiate candidate).

Incident (#368): four bespoke builders (bank credit / holdco SOTP / fintech
SOTP / platform DCF) hand-rolled percent UPSIDE, (fair/price - 1) * 100 --
wrong sign AND scale -- so deeply under-valued NU (+79.82) / BN (+26.56)
classified 'sell' while over-valued HDB / SOFI escaped the ladder.

The fix is structural, three layers, each pinned here:
  1. DcfRunRow carries NO over_under_pct field -- writers cannot supply one.
  2. persist.upsert() derives it centrally (persist.derive_over_under is the
     only producer), including the #291 non-positive-fair-value -> NULL guard.
  3. Migration 0076 adds a DB CHECK so even raw SQL cannot persist an
     inconsistent value (real-chain round-trip in
     test_migration_0076_over_under_check.py; the schema below mirrors it).

The four writer tests drive each bespoke builder's real persist_dcf_run with
an under-valued fixture and assert the stored row is self-consistent: negative,
decimal-scale, exactly (live - fair) / fair.
"""

from __future__ import annotations

import dataclasses
import sqlite3
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import build_bank_dcf as bank  # noqa: E402
import build_fintech_sotp as fintech  # noqa: E402
import build_holdco_sotp as holdco  # noqa: E402
import build_nu_platform_dcf as platform_dcf  # noqa: E402

from dcf import persist  # noqa: E402
from dcf.provenance import DcfInputProvenance  # noqa: E402

# Mirrors the production table post-0076: the named CHECK is the migration's
# _RATIO_CHECK verbatim, so the raw-insert test below exercises the same
# predicate prod enforces.
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
    revenue_growths_json TEXT, fcf_margin REAL,
    input_sha256 TEXT, workbook_sha256 TEXT, engine_version TEXT,
    inputs_as_of TEXT, provenance_json TEXT,
    CONSTRAINT ck_dcf_runs_over_under_ratio CHECK (
        over_under_pct IS NULL
        OR live_price IS NULL OR npv_per_share IS NULL
        OR live_price <= 0 OR npv_per_share <= 0
        OR ABS(over_under_pct - (1.0 * live_price / npv_per_share - 1.0)) <= 0.005
    )
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


# --------------------------------------------------------------------------- #
# Layer 1 + 2: the persist chokepoint derives over_under_pct; writers can't.
# --------------------------------------------------------------------------- #


def _row(live_price: float | None = 10.0, npv_per_share: float = 20.0) -> persist.DcfRunRow:
    return persist.DcfRunRow(
        ticker="ZZT",
        valuation_date=date(2026, 6, 10),
        horizon_years=5,
        wacc=0.10,
        npv=1000.0,
        npv_per_share=npv_per_share,
        shares_outstanding=5.0e7,
        currency="USD",
        live_price=live_price,
        live_price_at=None,
        mos_bar_used=None,
        assumption_snapshot_json="{}",
        provenance=DcfInputProvenance(
            input_sha256="a" * 64,
            workbook_sha256="b" * 64,
            engine_version="test@1",
            inputs_as_of=datetime(2026, 6, 10, 8, 0, tzinfo=UTC),
            detail={"equity_bridge_receipt": {"status": "verified"}},
        ),
    )


def test_dcf_run_row_has_no_over_under_field() -> None:
    """Writers cannot supply over_under_pct at all -- reintroducing the field
    would re-open the hand-rolled-convention hole #368 closed."""
    assert "over_under_pct" not in {f.name for f in dataclasses.fields(persist.DcfRunRow)}


def test_upsert_derives_ratio_centrally(repo_root: Path) -> None:
    with sqlite3.connect(str(repo_root / "data" / "portfolio.db")) as conn:
        persist.upsert(conn, _row(live_price=10.0, npv_per_share=20.0))
    live, fair, stored = _stored(repo_root, "ZZT")
    assert stored == pytest.approx(-0.5)
    _assert_ratio_convention(live, fair, stored)


@pytest.mark.parametrize(
    ("live_price", "npv_per_share"),
    [(None, 20.0), (10.0, 0.0), (10.0, -3.0)],
)
def test_upsert_stores_null_when_over_under_undefined(
    repo_root: Path, live_price: float | None, npv_per_share: float
) -> None:
    """No live price, or a non-positive fair value (the #291 case) -> NULL."""
    with sqlite3.connect(str(repo_root / "data" / "portfolio.db")) as conn:
        persist.upsert(conn, _row(live_price=live_price, npv_per_share=npv_per_share))
        row = conn.execute("SELECT over_under_pct FROM dcf_runs WHERE ticker = 'ZZT'").fetchone()
    assert row is not None
    assert row[0] is None


def test_derive_over_under_matches_convention() -> None:
    assert persist.derive_over_under(12.29, 22.10) == pytest.approx(-0.4439, abs=1e-4)  # NU
    assert persist.derive_over_under(15.71, 9.66) == pytest.approx(0.6263, abs=1e-4)  # SOFI
    assert persist.derive_over_under(None, 20.0) is None
    assert persist.derive_over_under(0.0, 20.0) is None
    assert persist.derive_over_under(10.0, 0.0) is None
    assert persist.derive_over_under(10.0, -3.0) is None


# --------------------------------------------------------------------------- #
# Layer 3: the 0076 CHECK blocks raw SQL that bypasses persist.upsert.
# --------------------------------------------------------------------------- #


def test_check_constraint_rejects_percent_scale_raw_insert(repo_root: Path) -> None:
    """The exact +79.82 shape of the NU incident, written without going through
    the chokepoint, must die at the DB."""
    with (
        sqlite3.connect(str(repo_root / "data" / "portfolio.db")) as conn,
        pytest.raises(sqlite3.IntegrityError, match="ck_dcf_runs_over_under_ratio"),
    ):
        conn.execute(
            "INSERT INTO dcf_runs (ticker, valuation_date, horizon_years, wacc,"
            " terminal_growth, npv, npv_per_share, shares_outstanding, currency,"
            " live_price, over_under_pct, assumption_snapshot_json,"
            " revenue_growths_json, fcf_margin)"
            " VALUES ('ZZB', '2026-06-09', 10, 0.125, 0, 1.0, 22.10, 1.0, 'USD',"
            " 12.29, 79.82, '{}', '[]', 0)"
        )


# --------------------------------------------------------------------------- #
# End-to-end: each bespoke builder's real persist_dcf_run stores the ratio.
# --------------------------------------------------------------------------- #


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
    a = dataclasses.replace(a, price=m.vps_usd * 0.8)
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
