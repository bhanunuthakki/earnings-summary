"""S6 PR2 — Bull/Bear scenario range on the §1 valuation card.

Covers the three layers:
  * snapshot._scenario_range — parsing the "scenarios" block out of
    dcf_runs.assumption_snapshot_json (absent/malformed/null-tolerant);
  * snapshot._valuation_snapshot — a fixture dcf_runs row flows into
    ValuationSnapshot.bull/bear_npv_per_share;
  * the renderers — workspace card shows bear·base·bull with upside at each
    (replacing the single-point row) plus the price-position track, and falls
    back to the legacy single-point readout when the run has no scenarios;
    markdown gains the range row.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from io import StringIO
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

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

# --------------------------------------------------------------------------- #
# _scenario_range parsing
# --------------------------------------------------------------------------- #


def _snapshot_json(bull: float | None, bear: float | None) -> str:
    return json.dumps(
        {
            "format": "redesign",
            "scenarios": {
                "base": {"fair_value_per_share_usd": 610.79},
                "bull": {"fair_value_per_share_usd": bull, "deltas": {"margin_term": 0.02}},
                "bear": {"fair_value_per_share_usd": bear, "deltas": {"margin_term": -0.02}},
            },
        }
    )


def test_scenario_range_parses_bull_and_bear() -> None:
    bull, bear = snapshot_mod._scenario_range(  # pyright: ignore[reportPrivateUsage]
        _snapshot_json(859.0, 426.17)
    )
    assert bull == 859.0
    assert bear == 426.17


def test_scenario_range_null_scenario_stays_none() -> None:
    """An un-valuable scenario is persisted as null and must read back None."""
    bull, bear = snapshot_mod._scenario_range(  # pyright: ignore[reportPrivateUsage]
        _snapshot_json(None, 426.17)
    )
    assert bull is None
    assert bear == 426.17


def test_scenario_range_tolerates_legacy_and_malformed_snapshots() -> None:
    rng = snapshot_mod._scenario_range  # pyright: ignore[reportPrivateUsage]
    assert rng(json.dumps({"format": "redesign"})) == (None, None)  # pre-S6 row
    assert rng(None) == (None, None)
    assert rng("") == (None, None)
    assert rng("{not json") == (None, None)
    assert rng(json.dumps({"scenarios": "oops"})) == (None, None)
    assert rng(json.dumps({"scenarios": {"bull": {"fair_value_per_share_usd": True}}})) == (
        None,
        None,
    )  # bool is not a fair value


# --------------------------------------------------------------------------- #
# dcf_runs row -> ValuationSnapshot (fixture DB)
# --------------------------------------------------------------------------- #


def _seed_repo(tmp_path: Path, snapshot_json: str | None) -> Path:
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
        " ('TEST', '2026-06-12', 10, 0.0965, 0, 1500000, 610.79, 2500000000.0, 'USD',"
        "  568.43, -0.0694, 0.25, ?)",
        (snapshot_json,),
    )
    conn.commit()
    conn.close()
    return repo


def test_valuation_snapshot_carries_scenario_range(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path, _snapshot_json(859.0, 426.17))
    v = snapshot_mod._valuation_snapshot(  # pyright: ignore[reportPrivateUsage]
        "TEST", repo, current_price=None, model_link=None, mos_bar=None
    )
    assert v.consolidated_npv_per_share == 610.79
    assert v.bull_npv_per_share == 859.0
    assert v.bear_npv_per_share == 426.17


def test_valuation_snapshot_without_scenarios_stays_single_point(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path, json.dumps({"format": "redesign"}))
    v = snapshot_mod._valuation_snapshot(  # pyright: ignore[reportPrivateUsage]
        "TEST", repo, current_price=None, model_link=None, mos_bar=None
    )
    assert v.consolidated_npv_per_share == 610.79
    assert v.bull_npv_per_share is None
    assert v.bear_npv_per_share is None


# --------------------------------------------------------------------------- #
# Workspace card rendering
# --------------------------------------------------------------------------- #


def _render_panel(v: ValuationSnapshot) -> str:
    snap = SnapshotSection(status=SectionStatus.OK, ticker="TEST", valuation=v)
    body = StringIO()
    _valuation_summary_panel(body, snap)
    return body.getvalue()


def test_card_renders_range_with_upside_at_each_and_track() -> None:
    html_out = _render_panel(
        ValuationSnapshot(
            consolidated_npv_per_share=610.79,
            current_price=568.43,
            bull_npv_per_share=859.0,
            bear_npv_per_share=426.17,
            mos_bar=0.25,
            trigger_status="hold",
        )
    )
    # the three scenario cells with per-scenario upside vs the live price
    assert 'class="scenario-cell bear"' in html_out
    assert 'class="scenario-cell base"' in html_out
    assert 'class="scenario-cell bull"' in html_out
    assert "+51%" in html_out  # bull upside (859.00 / 568.43 - 1)
    assert "-25%" in html_out  # bear downside
    assert "+7%" in html_out  # base upside
    # the single-point row is REPLACED, not duplicated
    assert "Consolidated NPV / share" not in html_out
    assert "Implied vs consolidated" not in html_out
    # the bear->bull track with the live-price marker
    assert "scenario-bar-track" in html_out
    assert "scenario-bar-price" in html_out
    # MoS bar semantics untouched
    assert "Margin-of-safety bar" in html_out
    assert "25%" in html_out


def test_card_falls_back_to_single_point_without_scenarios() -> None:
    html_out = _render_panel(
        ValuationSnapshot(consolidated_npv_per_share=610.79, current_price=568.43)
    )
    assert "Consolidated NPV / share" in html_out
    assert "Implied vs consolidated" in html_out
    assert "scenario-cell" not in html_out
    assert "scenario-bar" not in html_out


def test_card_degenerate_scenario_renders_dash_and_skips_track() -> None:
    """Bull present, bear un-valuable (None): the range strip still renders —
    the bear cell shows an em-dash — but the track needs both ends, so it is
    omitted rather than drawn against a fake endpoint."""
    html_out = _render_panel(
        ValuationSnapshot(
            consolidated_npv_per_share=610.79,
            current_price=568.43,
            bull_npv_per_share=859.0,
            bear_npv_per_share=None,
        )
    )
    assert 'class="scenario-cell bull"' in html_out
    assert "scenario-bar-track" not in html_out


# --------------------------------------------------------------------------- #
# Markdown renderer
# --------------------------------------------------------------------------- #


def test_markdown_card_gains_scenario_range_row() -> None:
    out = StringIO()
    _valuation_card_md(
        out,
        ValuationSnapshot(
            consolidated_npv_per_share=610.79,
            current_price=568.43,
            bull_npv_per_share=859.0,
            bear_npv_per_share=426.17,
        ),
    )
    text = out.getvalue()
    assert "| Scenario range (bear / base / bull) | $426.17 / $610.79 / $859.00 |" in text


def test_markdown_card_without_scenarios_unchanged() -> None:
    out = StringIO()
    _valuation_card_md(
        out, ValuationSnapshot(consolidated_npv_per_share=610.79, current_price=568.43)
    )
    assert "Scenario range" not in out.getvalue()
