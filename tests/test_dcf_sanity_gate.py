"""The DCF trust gate (migration 0182 + program review 2026-07-19).

Three seams, each the smallest test that would have caught the live defects:

  - ``derive_sanity_flag`` boundaries — the single producer of
    ``dcf_runs.sanity_flag`` values, derived at the same write chokepoint as
    ``over_under_pct`` itself so no writer can persist an extreme valuation
    unflagged.
  - ``upsert`` stamps the flag on a real at-head schema (outlier row flagged,
    in-bounds row NULL) — and keeps working on a pre-0182 schema without the
    column (the degrade contract every dcf_runs reader/writer follows).
  - ``load_dcf`` (the lens-side reader) withholds the valuation numbers of a
    flagged run while keeping the flag visible — so no LLM lens can quote an
    unreviewed fair value (the TSM "97% undervalued" class).

Plus the refresh-side FX belt-and-braces: a non-USD reporter whose workbook
carries fx 1.0 must fail its refresh, not persist local-currency-as-USD.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from dcf.persist import (
    SANITY_OVER_UNDER_LIMIT,
    DcfPromotionBlocked,
    DcfRunRow,
    derive_sanity_flag,
    upsert,
)
from dcf.provenance import DcfInputProvenance
from synthesis.lenses._shared import load_dcf

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _cfg(db_file: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
    return cfg


@pytest.fixture
def db_path(tmp_path: Path) -> Iterator[Path]:
    """A real at-head DB in ``<tmp>/data/portfolio.db`` (the repo-root shape
    ``load_dcf`` resolves), mirroring the save/restore discipline of
    ``test_dcf_live_write.py``."""
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
        yield db_file
    finally:
        dbmod.DB_PATH, dbmod.DATA_DIR, dbmod.FMP_DIR = saved


def _row(ticker: str, *, npv_per_share: float, live_price: float | None) -> DcfRunRow:
    return DcfRunRow(
        ticker=ticker,
        valuation_date=date(2026, 7, 19),
        horizon_years=10,
        wacc=0.10,
        npv=10_000.0,
        npv_per_share=npv_per_share,
        shares_outstanding=1e9,
        currency="USD",
        live_price=live_price,
        live_price_at=None,
        mos_bar_used=None,
        assumption_snapshot_json=json.dumps({"fx_to_usd": 1.0}),
        provenance=DcfInputProvenance(
            input_sha256="a" * 64,
            workbook_sha256="b" * 64,
            engine_version="test@1",
            inputs_as_of=datetime(2026, 7, 19, 8, 0, tzinfo=UTC),
            detail={"equity_bridge_receipt": {"status": "verified"}},
        ),
    )


# ---------------------------------------------------------------- derive_sanity_flag


def test_flag_none_within_the_limit() -> None:
    assert derive_sanity_flag(None) is None
    assert derive_sanity_flag(0.0) is None
    assert derive_sanity_flag(SANITY_OVER_UNDER_LIMIT) is None  # boundary: not strictly past
    assert derive_sanity_flag(-SANITY_OVER_UNDER_LIMIT) is None


def test_flag_outlier_past_the_limit_both_directions() -> None:
    assert derive_sanity_flag(0.61) == "outlier"
    assert derive_sanity_flag(-0.61) == "outlier"
    assert derive_sanity_flag(-0.97) == "outlier"  # the TSM row


# ---------------------------------------------------------------------------- upsert


def test_upsert_stamps_outlier_on_extreme_over_under(db_path: Path) -> None:
    # fair 10, live 25 -> over_under = +1.5, well past 0.6
    with sqlite3.connect(str(db_path)) as conn:
        with pytest.raises(DcfPromotionBlocked, match="owner_review"):
            upsert(conn, _row("TSM", npv_per_share=10.0, live_price=25.0))
        assert conn.execute("SELECT COUNT(*) FROM dcf_runs WHERE ticker='TSM'").fetchone()[0] == 0


def test_upsert_leaves_flag_null_within_bounds(db_path: Path) -> None:
    # fair 10, live 12 -> over_under = +0.2
    with sqlite3.connect(str(db_path)) as conn:
        upsert(conn, _row("NU", npv_per_share=10.0, live_price=12.0))
        flag = conn.execute(
            "SELECT sanity_flag FROM dcf_runs WHERE ticker='NU' AND is_latest=1"
        ).fetchone()[0]
    assert flag is None


def test_upsert_still_works_on_a_pre_0182_schema(tmp_path: Path) -> None:
    """No sanity_flag column still cannot promote an unreviewed outlier."""
    db_file = tmp_path / "old.db"
    with sqlite3.connect(str(db_file)) as conn:
        conn.execute(
            """
            CREATE TABLE dcf_runs (
                id INTEGER PRIMARY KEY,
                ticker TEXT, valuation_date TEXT, horizon_years INT, wacc REAL,
                terminal_growth REAL, npv REAL, npv_per_share REAL,
                shares_outstanding REAL, currency TEXT, notes TEXT, run_id TEXT,
                live_price REAL, live_price_at TEXT, over_under_pct REAL,
                mos_bar_used REAL, assumption_snapshot_json TEXT,
                revenue_growths_json TEXT, fcf_margin REAL, segment_name TEXT,
                input_sha256 TEXT, workbook_sha256 TEXT, engine_version TEXT,
                inputs_as_of TEXT, provenance_json TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute("CREATE UNIQUE INDEX uq_dcf_runs_ticker ON dcf_runs(ticker)")
        with pytest.raises(DcfPromotionBlocked, match="owner_review"):
            upsert(conn, _row("WIX", npv_per_share=10.0, live_price=25.0))
        got = conn.execute("SELECT COUNT(*) FROM dcf_runs WHERE ticker='WIX'").fetchone()
    assert got is not None and got[0] == 0


# -------------------------------------------------------------------------- load_dcf


def test_load_dcf_withholds_numbers_of_a_flagged_run(db_path: Path, tmp_path: Path) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        upsert(conn, _row("TSM", npv_per_share=10.0, live_price=12.0))
        conn.execute("UPDATE dcf_runs SET sanity_flag='outlier' WHERE ticker='TSM'")
    out = load_dcf("TSM", tmp_path)
    assert out is not None
    assert out["sanity_flag"] == "outlier"
    assert out["npv_per_share"] is None
    assert out["over_under_pct"] is None
    assert out["mos_bar_used"] is None


def test_load_dcf_passes_an_unflagged_run_through(db_path: Path, tmp_path: Path) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        upsert(conn, _row("NU", npv_per_share=10.0, live_price=12.0))
    out = load_dcf("NU", tmp_path)
    assert out is not None
    assert out["sanity_flag"] is None
    assert out["npv_per_share"] == pytest.approx(10.0)
    assert out["over_under_pct"] == pytest.approx(0.2)


# ------------------------------------------------------------------ refresh FX guard


def test_unconverted_fx_reason_fails_a_non_usd_reporter_at_fx_1(tmp_path: Path) -> None:
    import importlib.util
    import sys as _sys

    spec = importlib.util.spec_from_file_location(
        "refresh_dcf", PROJECT_ROOT / "execution" / "refresh_dcf.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: the script defines dataclasses, and dataclass
    # resolution looks the defining module up in sys.modules.
    previous = _sys.modules.get("refresh_dcf")
    _sys.modules["refresh_dcf"] = mod
    try:
        spec.loader.exec_module(mod)
        fmp = tmp_path / "data" / "historical" / "fmp"
        fmp.mkdir(parents=True)
        (fmp / "TSM_income_statement_quarterly.json").write_text(
            json.dumps([{"reportedCurrency": "TWD"}]), encoding="utf-8"
        )
        reason = mod._unconverted_fx_reason(tmp_path, "TSM", 1.0)
        assert reason is not None and "TWD" in reason
        # Converted workbook (fx != 1.0) passes; USD reporter at 1.0 passes; missing
        # cache stays quiet (a USD name with no cache must not fail its refresh).
        assert mod._unconverted_fx_reason(tmp_path, "TSM", 0.031) is None
        (fmp / "NU_income_statement_quarterly.json").write_text(
            json.dumps([{"reportedCurrency": "USD"}]), encoding="utf-8"
        )
        assert mod._unconverted_fx_reason(tmp_path, "NU", 1.0) is None
        assert mod._unconverted_fx_reason(tmp_path, "GOOG", 1.0) is None
    finally:
        if previous is None:
            _sys.modules.pop("refresh_dcf", None)
        else:
            _sys.modules["refresh_dcf"] = previous
