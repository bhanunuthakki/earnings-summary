"""Phase-1 Wave-3: the DCF assumption-tweak extractor + deterministic recompute.

Three layers, mirroring the #712 posture (no test can corrupt live valuations
except through the same real chokepoint prod uses):
  - the EXTRACTOR (bounded {param, new_value}) is validated with an injected caller;
  - the CRUX unit-mapping (RedesignValuation -> proposed dcf_runs row) is pinned by a
    round-trip over the PURE ``dcf.redesign.value`` with a hand-built RedesignInputs;
  - the full chain (recompute -> draft_dcf_proposal -> apply -> live dcf_runs) is run
    once against a real ``dcf_runs`` built to production schema.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from dcf import redesign
from dcf.fact_drivers import DRIVER_FIELDS_BY_KEY, apply_to_inputs
from research.dcf_artifact import apply_dcf_proposal, draft_dcf_proposal
from research.dcf_tweak import (
    DcfTweakCall,
    draft_dcf_tweak_proposal,
    extract_dcf_tweak,
    recompute_row_from_inputs,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# A valid redesign input set (mirrors tests/test_fact_drivers._BASE) so value() yields
# a positive fair value without a workbook.
_BASE = redesign.RedesignInputs(
    segments=("Cloud", "Devices"),
    base_revenue_by_segment={"Cloud": 600.0, "Devices": 400.0},
    near_growth_by_segment={"Cloud": 0.10, "Devices": 0.05},
    terminal_growth_by_segment={"Cloud": 0.03, "Devices": 0.02},
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


def _fixed(result: dict[str, object]) -> DcfTweakCall:
    def caller(_wondering: str) -> dict[str, object]:
        return result

    return caller


# --------------------------------------------------------------------------- #
# 1. the extractor: bounded {param, new_value}, else None
# --------------------------------------------------------------------------- #


def test_extract_accepts_an_in_bounds_high_confidence_tweak() -> None:
    out = extract_dcf_tweak(
        "what if NU's tax rate were 21%?",
        "NU",
        call=_fixed({"param": "tax_rate", "new_value": 0.21, "confidence": "high"}),
    )
    assert out == {"param": "tax_rate", "new_value": 0.21}


def test_extract_rejects_an_out_of_bounds_value() -> None:
    # tax_rate bound is [0, 0.6]; 1.5 is out of range -> refuse the tweak.
    out = extract_dcf_tweak(
        "tax rate 150%",
        call=_fixed({"param": "tax_rate", "new_value": 1.5, "confidence": "high"}),
    )
    assert out is None


def test_extract_rejects_an_off_registry_param() -> None:
    out = extract_dcf_tweak(
        "bump the sales multiple",
        call=_fixed({"param": "revenue_multiple", "new_value": 5.0, "confidence": "high"}),
    )
    assert out is None


def test_extract_requires_high_confidence() -> None:
    out = extract_dcf_tweak(
        "maybe margins improve a bit?",
        call=_fixed({"param": "near_op_margin", "new_value": 0.3, "confidence": "low"}),
    )
    assert out is None


def test_extract_rejects_a_non_finite_or_missing_value() -> None:
    assert (
        extract_dcf_tweak(
            "x", call=_fixed({"param": "beta", "new_value": None, "confidence": "high"})
        )
        is None
    )
    assert (
        extract_dcf_tweak(
            "x", call=_fixed({"param": "beta", "new_value": "high", "confidence": "high"})
        )
        is None
    )


def test_extract_empty_wondering_returns_none() -> None:
    calls = {"n": 0}

    def spy(_w: str) -> dict[str, object]:
        calls["n"] += 1
        return {}

    assert extract_dcf_tweak("   ", call=spy) is None
    assert calls["n"] == 0


def test_extract_default_degrades_on_a_parse_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from llm import structured as structured_mod

    def boom(*_a: object, **_k: object) -> object:
        raise structured_mod.StructuredParseError("unusable", raw_head="{...")

    monkeypatch.setattr(structured_mod, "call_llm_structured", boom)
    assert extract_dcf_tweak("what if tax is 21%?", "NU") is None


# --------------------------------------------------------------------------- #
# 2. the CRUX unit-mapping (pure value() -> proposed row)
# --------------------------------------------------------------------------- #


def test_recompute_row_matches_the_pure_valuation_unit_contract() -> None:
    tweaked = apply_to_inputs(_BASE, DRIVER_FIELDS_BY_KEY["near_op_margin"], 0.25)
    rv = redesign.value(tweaked)
    row = recompute_row_from_inputs(
        tweaked,
        "nu",
        tweak={"param": "near_op_margin", "new_value": 0.25},
        live_price=12.29,
    )
    assert row["ticker"] == "NU"  # normalized
    assert row["npv"] == pytest.approx(rv.operating_value_usd_m)  # EV, $M
    assert row["npv_per_share"] == pytest.approx(rv.value_per_share_usd)  # USD/sh
    assert row["shares_outstanding"] == pytest.approx(rv.diluted_shares_m * 1_000_000.0)  # absolute
    assert row["horizon_years"] == len(rv.fcff_stream_m)
    assert row["wacc"] == pytest.approx(rv.wacc)
    assert row["currency"] == "USD"
    assert row["live_price"] == pytest.approx(12.29)
    snap = json.loads(str(row["assumption_snapshot_json"]))
    assert snap["tweak"] == {"param": "near_op_margin", "new_value": 0.25}


# --------------------------------------------------------------------------- #
# 3. full chain: recompute -> draft -> apply -> real dcf_runs
# --------------------------------------------------------------------------- #


@pytest.fixture
def db_path(tmp_path: Path) -> Iterator[Path]:
    db_file = tmp_path / "ledger.db"
    import db as dbmod

    saved = (dbmod.DB_PATH, dbmod.DATA_DIR, dbmod.FMP_DIR)
    dbmod.set_db_path(str(db_file))
    dbmod.init_db()
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
    command.stamp(cfg, "0000_baseline")
    command.upgrade(cfg, "head")
    try:
        yield db_file
    finally:
        dbmod.DB_PATH, dbmod.DATA_DIR, dbmod.FMP_DIR = saved


def test_tweak_recompute_flows_through_apply_to_a_real_dcf_runs(db_path: Path) -> None:
    tweaked = apply_to_inputs(_BASE, DRIVER_FIELDS_BY_KEY["near_op_margin"], 0.25)
    rv = redesign.value(tweaked)
    assert rv.value_per_share_usd > 0  # the fixture must be valuable for the draft to persist
    row = recompute_row_from_inputs(
        tweaked,
        "NU",
        live_price=rv.value_per_share_usd,
        live_price_at=datetime(2026, 6, 30, 8, 0, 0),
    )

    pid = draft_dcf_proposal(ticker="NU", proposed_row=row, db_path=db_path)
    assert pid is not None
    note = apply_dcf_proposal(pid, db_path=db_path)  # direct applier + real _default_persist
    assert "live" in note

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        stored = conn.execute("SELECT * FROM dcf_runs WHERE ticker='NU'").fetchone()
    assert stored is not None
    assert stored["npv_per_share"] == pytest.approx(rv.value_per_share_usd)
    assert stored["npv"] == pytest.approx(rv.operating_value_usd_m)
    assert stored["shares_outstanding"] == pytest.approx(rv.diluted_shares_m * 1_000_000.0)
    # over_under derived at the chokepoint from live vs the RECOMPUTED fair value.
    assert stored["over_under_pct"] == pytest.approx(0.0, abs=1e-6)


# --------------------------------------------------------------------------- #
# 4. the orchestrator (extract -> recompute -> draft)
# --------------------------------------------------------------------------- #


def _valid_row() -> dict[str, object]:
    tweaked = apply_to_inputs(_BASE, DRIVER_FIELDS_BY_KEY["tax_rate"], 0.21)
    return recompute_row_from_inputs(tweaked, "NU", tweak={"param": "tax_rate", "new_value": 0.21})


def test_orchestrator_extracts_recomputes_and_drafts(db_path: Path) -> None:
    def fake_extract(_w: str, _t: str | None = None) -> dict[str, object] | None:
        return {"param": "tax_rate", "new_value": 0.21}

    def fake_recompute(
        _ticker: str, _param: str, _new_value: float, *, repo_root: Path | None = None
    ) -> dict[str, object] | None:
        return _valid_row()

    pid = draft_dcf_tweak_proposal(
        wondering="what if NU's tax rate were 21%?",
        ticker="nu",
        extract_fn=fake_extract,
        recompute_fn=fake_recompute,
        db_path=db_path,
    )
    assert pid is not None
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        prop = conn.execute(
            "SELECT kind, ticker FROM research_proposals WHERE id=?", (pid,)
        ).fetchone()
    assert prop is not None and prop["kind"] == "dcf" and prop["ticker"] == "NU"


def test_orchestrator_no_tweak_drafts_nothing() -> None:
    def none_extract(_w: str, _t: str | None = None) -> dict[str, object] | None:
        return None

    def unused_recompute(
        _ticker: str, _param: str, _new_value: float, *, repo_root: Path | None = None
    ) -> dict[str, object] | None:
        raise AssertionError("recompute must not run without a tweak")

    assert (
        draft_dcf_tweak_proposal(
            wondering="hmm", ticker="NU", extract_fn=none_extract, recompute_fn=unused_recompute
        )
        is None
    )


def test_orchestrator_unvaluable_recompute_drafts_nothing() -> None:
    def fake_extract(_w: str, _t: str | None = None) -> dict[str, object] | None:
        return {"param": "tax_rate", "new_value": 0.21}

    def none_recompute(
        _ticker: str, _param: str, _new_value: float, *, repo_root: Path | None = None
    ) -> dict[str, object] | None:
        return None

    assert (
        draft_dcf_tweak_proposal(
            wondering="x", ticker="NU", extract_fn=fake_extract, recompute_fn=none_recompute
        )
        is None
    )
