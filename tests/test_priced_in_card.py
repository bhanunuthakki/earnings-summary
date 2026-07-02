"""Reverse-DCF "Priced in vs your case" — persistence contract + card render.

Covers the layers PR B adds on top of the pure solver (dcf.reverse, PR A):
  * the producer/parser contract — PricedIn.to_snapshot_dict round-trips through
    snapshot._priced_in into a PricedInCard, tolerant of absent/malformed blocks;
  * snapshot._valuation_snapshot — a fixture dcf_runs row carries priced_in;
  * the renderers — the markdown + workspace valuation cards gain the block, and
    a bespoke archetype (no redesigned workbook) renders an explicit n/a;
  * backfill_priced_in.patch_snapshot_priced_in — patches only the priced_in key.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from io import StringIO
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "execution"))

import backfill_priced_in  # noqa: E402

from dcf import redesign, reverse  # noqa: E402
from report.models import (  # noqa: E402
    SectionStatus,
    SnapshotSection,
    ValuationSnapshot,
)
from report.renderers.markdown import (  # noqa: E402
    _valuation_card_md,  # pyright: ignore[reportPrivateUsage]  # testing an internal seam
)
from report.renderers.workspace_html import (  # noqa: E402
    _valuation_summary_panel,  # pyright: ignore[reportPrivateUsage]  # testing an internal seam
)
from report.sections import snapshot as snapshot_mod  # noqa: E402

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


def _pi_block(
    *,
    g_impl: float | None = 0.11,
    t_impl: float | None = 14.3,
    g_note: str = "",
    t_note: str = "",
) -> dict[str, object]:
    """A hand-built priced_in block (the shape refresh_dcf persists)."""
    return {
        "price": 55.0,
        "base_value_per_share_usd": 50.0,
        "growth": {
            "lever": "revenue_cagr_5y",
            "label": "Implied 5y revenue CAGR",
            "unit": "pct",
            "base_value": 0.08,
            "implied_value": g_impl,
            "note": g_note,
        },
        "terminal": {
            "lever": "exit_multiple",
            "label": "Implied exit multiple (EV/EBITDA)",
            "unit": "turns",
            "base_value": 12.0,
            "implied_value": t_impl,
            "note": t_note,
        },
    }


# --------------------------------------------------------------------------- #
# 1. Producer → parser round-trip
# --------------------------------------------------------------------------- #
def test_solver_output_round_trips_through_parser() -> None:
    pi = reverse.solve_priced_in(_BASE, price=redesign.value(_BASE).value_per_share_usd * 1.3)
    assert pi is not None
    snap = json.dumps({"format": "redesign", "priced_in": pi.to_snapshot_dict()})
    card = snapshot_mod._priced_in(snap)  # pyright: ignore[reportPrivateUsage]
    assert card is not None
    assert card.growth.lever == "revenue_cagr_5y"
    assert card.terminal.lever == "exit_multiple"
    assert card.terminal.implied_value is not None
    assert card.growth.base_value == pi.growth.base_value


def test_parser_tolerates_absent_and_malformed() -> None:
    parse = snapshot_mod._priced_in  # pyright: ignore[reportPrivateUsage]
    assert parse(None) is None
    assert parse("") is None
    assert parse("{not json") is None
    assert parse(json.dumps({"format": "redesign"})) is None  # no priced_in
    assert parse(json.dumps({"priced_in": "oops"})) is None
    # A lever with a bad unit is rejected (the whole block degrades to None).
    bad = _pi_block()
    bad_growth = bad["growth"]
    assert isinstance(bad_growth, dict)
    bad_growth["unit"] = "bogus"
    assert parse(json.dumps({"priced_in": bad})) is None
    # A missing terminal lever degrades to None.
    partial = _pi_block()
    del partial["terminal"]
    assert parse(json.dumps({"priced_in": partial})) is None


def test_parser_keeps_unsolved_lever_as_none_with_note() -> None:
    block = _pi_block(t_impl=None, t_note="market implies an exit multiple outside model bounds")
    card = snapshot_mod._priced_in(  # pyright: ignore[reportPrivateUsage]
        json.dumps({"priced_in": block})
    )
    assert card is not None
    assert card.terminal.implied_value is None
    assert card.terminal.gap_display is None
    assert "outside model bounds" in card.terminal.note
    assert card.terminal.implied_display == "n/a"


# --------------------------------------------------------------------------- #
# 2. Lever display formatting (shared by both renderers)
# --------------------------------------------------------------------------- #
def test_lever_display_formats_pct_and_turns() -> None:
    card = snapshot_mod._priced_in(  # pyright: ignore[reportPrivateUsage]
        json.dumps({"priced_in": _pi_block(g_impl=0.11, t_impl=14.3)})
    )
    assert card is not None
    assert card.growth.base_display == "8.0%"
    assert card.growth.implied_display == "11.0%"
    assert card.growth.gap_display == "+3.0pts"
    assert card.terminal.base_display == "12.0x"
    assert card.terminal.implied_display == "14.3x"
    assert card.terminal.gap_display == "+2.3x"


# --------------------------------------------------------------------------- #
# 3. dcf_runs row → ValuationSnapshot.priced_in (fixture DB)
# --------------------------------------------------------------------------- #
def _seed_repo(tmp_path: Path, snapshot_json: str) -> Path:
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    conn = sqlite3.connect(str(repo / "data" / "portfolio.db"))
    conn.executescript(
        """
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
            breakdown_json TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO dcf_runs (ticker, valuation_date, horizon_years, wacc, terminal_growth,"
        " npv, npv_per_share, shares_outstanding, currency, live_price, over_under_pct,"
        " mos_bar_used, assumption_snapshot_json) VALUES"
        " ('TEST', '2026-07-02', 10, 0.09, 0, 1500, 50.0, 100000000.0, 'USD',"
        "  55.0, 0.10, 0.25, ?)",
        (snapshot_json,),
    )
    conn.commit()
    conn.close()
    return repo


def test_valuation_snapshot_carries_priced_in(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path, json.dumps({"format": "redesign", "priced_in": _pi_block()}))
    v = snapshot_mod._valuation_snapshot(  # pyright: ignore[reportPrivateUsage]
        "TEST", repo, current_price=None, model_link=None, mos_bar=None
    )
    assert v.priced_in is not None
    assert v.priced_in.growth.implied_value == 0.11
    assert v.valuation_model_label == "FCFF DCF"


# --------------------------------------------------------------------------- #
# 4. Markdown card
# --------------------------------------------------------------------------- #
def _md(v: ValuationSnapshot) -> str:
    out = StringIO()
    _valuation_card_md(out, v)
    return out.getvalue()


def test_markdown_card_gains_priced_in_block() -> None:
    card = snapshot_mod._priced_in(  # pyright: ignore[reportPrivateUsage]
        json.dumps({"priced_in": _pi_block()})
    )
    text = _md(
        ValuationSnapshot(
            consolidated_npv_per_share=50.0,
            current_price=55.0,
            valuation_model_label="FCFF DCF",
            priced_in=card,
        )
    )
    assert "Priced in vs your case" in text
    assert "Implied 5y revenue CAGR" in text
    assert "8.0%" in text and "11.0%" in text and "+3.0pts" in text
    assert "Implied exit multiple (EV/EBITDA)" in text
    assert "12.0x" in text and "14.3x" in text and "+2.3x" in text


def test_markdown_card_bespoke_archetype_shows_na() -> None:
    text = _md(
        ValuationSnapshot(
            consolidated_npv_per_share=48.7,
            current_price=50.0,
            valuation_model_label="SOTP / NAV",
            priced_in=None,
        )
    )
    assert "n/a for SOTP / NAV model" in text


def test_markdown_card_fcff_without_block_is_silent() -> None:
    # A redesigned FCFF row that predates the block: no n/a noise (it will be
    # backfilled), no block.
    text = _md(
        ValuationSnapshot(
            consolidated_npv_per_share=50.0,
            current_price=55.0,
            valuation_model_label="FCFF DCF",
            priced_in=None,
        )
    )
    assert "Priced in" not in text


# --------------------------------------------------------------------------- #
# 5. Workspace card
# --------------------------------------------------------------------------- #
def _html(v: ValuationSnapshot) -> str:
    snap = SnapshotSection(status=SectionStatus.OK, ticker="TEST", valuation=v)
    body = StringIO()
    _valuation_summary_panel(body, snap)
    return body.getvalue()


def test_workspace_card_gains_priced_in_rows() -> None:
    card = snapshot_mod._priced_in(  # pyright: ignore[reportPrivateUsage]
        json.dumps({"priced_in": _pi_block()})
    )
    html_out = _html(
        ValuationSnapshot(
            consolidated_npv_per_share=50.0,
            current_price=55.0,
            valuation_model_label="FCFF DCF",
            priced_in=card,
        )
    )
    assert "Priced in vs your case" in html_out
    assert "Implied 5y revenue CAGR" in html_out
    assert "8.0% -&gt; 11.0% (+3.0pts)" in html_out or "8.0% -> 11.0% (+3.0pts)" in html_out


def test_workspace_card_bespoke_shows_na() -> None:
    html_out = _html(
        ValuationSnapshot(
            consolidated_npv_per_share=48.7,
            current_price=50.0,
            valuation_model_label="Excess return",
            priced_in=None,
        )
    )
    assert "n/a for Excess return model" in html_out


# --------------------------------------------------------------------------- #
# 6. Backfill patch (pure)
# --------------------------------------------------------------------------- #
def test_patch_preserves_other_keys_and_sets_priced_in() -> None:
    original = json.dumps(
        {
            "format": "redesign",
            "wacc": 0.09,
            "scenarios": {"base": {"fair_value_per_share_usd": 50}},
        }
    )
    patched = backfill_priced_in.patch_snapshot_priced_in(original, _pi_block())
    data = json.loads(patched)
    assert data["wacc"] == 0.09  # untouched
    assert data["scenarios"]["base"]["fair_value_per_share_usd"] == 50  # untouched
    assert data["priced_in"]["growth"]["implied_value"] == 0.11


def test_patch_none_removes_block_and_rejects_non_object() -> None:
    original = json.dumps({"wacc": 0.09, "priced_in": _pi_block()})
    patched = backfill_priced_in.patch_snapshot_priced_in(original, None)
    assert "priced_in" not in json.loads(patched)
    import pytest

    with pytest.raises(ValueError, match="not a JSON object"):
        backfill_priced_in.patch_snapshot_priced_in(json.dumps([1, 2, 3]), _pi_block())
