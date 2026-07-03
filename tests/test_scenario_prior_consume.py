"""PR E — scenario_reward consumes the per-name prior; the card surfaces E[V] + skew.

Covers:
  * scenario_reward.parse_scenario_prior_weights + the reward using per-name odds
    (the 3 allocation surfaces inherit it for free — they already pass snapshot_json);
  * snapshot._scenario_prior_card / _scenario_ev_skew (card parse + E[V]/skew);
  * ValuationSnapshot carries the fields off a fixture dcf_runs row;
  * the markdown + workspace cards render weights + E[V] + skew + rationale.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from io import StringIO
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dcf.scenario_reward import parse_scenario_prior_weights, scenario_reward  # noqa: E402
from report.models import SectionStatus, SnapshotSection, ValuationSnapshot  # noqa: E402
from report.renderers.markdown import (  # noqa: E402
    _valuation_card_md,  # pyright: ignore[reportPrivateUsage]  # internal seam
)
from report.renderers.workspace_html import (  # noqa: E402
    _valuation_summary_panel,  # pyright: ignore[reportPrivateUsage]  # internal seam
)
from report.sections import snapshot as snapshot_mod  # noqa: E402


def _approx(x: float) -> object:
    return pytest.approx(x, abs=1e-9)


def _snap(
    *,
    bull: float | None = 150.0,
    base: float = 100.0,
    bear: float | None = 50.0,
    weights: dict[str, float] | None = None,
    rationale: str = "fragile high-multiple thesis",
    set_by: str = "llm",
) -> str:
    scenarios: dict[str, object] = {"base": {"fair_value_per_share_usd": base}}
    if bull is not None:
        scenarios["bull"] = {"fair_value_per_share_usd": bull}
    if bear is not None:
        scenarios["bear"] = {"fair_value_per_share_usd": bear}
    d: dict[str, object] = {"format": "redesign", "scenarios": scenarios}
    if weights is not None:
        d["scenario_prior"] = {"weights": weights, "rationale": rationale, "set_by": set_by}
    return json.dumps(d)


# --------------------------------------------------------------------------- #
# 1. parse_scenario_prior_weights
# --------------------------------------------------------------------------- #
def test_parse_weights_valid_and_normalized() -> None:
    w = parse_scenario_prior_weights(_snap(weights={"bull": 0.2, "base": 0.5, "bear": 0.3}))
    assert w == {"bull": 0.2, "base": 0.5, "bear": 0.3}
    # Non-summing weights are normalized defensively.
    w2 = parse_scenario_prior_weights(_snap(weights={"bull": 2.0, "base": 5.0, "bear": 3.0}))
    assert w2 is not None and abs(sum(w2.values()) - 1.0) < 1e-9
    assert round(w2["bear"], 4) == 0.3


def test_parse_weights_absent_and_malformed() -> None:
    assert parse_scenario_prior_weights(_snap()) is None  # no scenario_prior block
    assert parse_scenario_prior_weights(None) is None
    assert parse_scenario_prior_weights("{bad") is None
    assert parse_scenario_prior_weights(json.dumps({"scenario_prior": {"weights": "x"}})) is None
    # A missing leg → None (falls back to global).
    assert (
        parse_scenario_prior_weights(
            json.dumps({"scenario_prior": {"weights": {"bull": 0.3, "base": 0.7}}})
        )
        is None
    )
    # A negative weight → None.
    assert (
        parse_scenario_prior_weights(
            json.dumps({"scenario_prior": {"weights": {"bull": -0.1, "base": 0.7, "bear": 0.4}}})
        )
        is None
    )


# --------------------------------------------------------------------------- #
# 2. scenario_reward uses the per-name prior (allocation surfaces inherit it)
# --------------------------------------------------------------------------- #
def test_reward_global_prior_is_symmetric() -> None:
    # Symmetric fair values (bull +50% / bear -50%) at price = base: global 25/50/25
    # gives E[V] = 0.
    r = scenario_reward(price=100.0, base_fv=100.0, snapshot_json=_snap())
    assert r is not None
    assert r.weights_source == "global"
    assert abs(r.expected_return) < 1e-9
    assert abs(r.skew) < 1e-9


def test_reward_bear_heavy_prior_lowers_expectation() -> None:
    r = scenario_reward(
        price=100.0,
        base_fv=100.0,
        snapshot_json=_snap(weights={"bull": 0.15, "base": 0.50, "bear": 0.35}),
    )
    assert r is not None
    assert r.weights_source == "per_name"
    # 0.15*(+0.5) + 0.50*0 + 0.35*(-0.5) = -0.10
    assert r.expected_return == _approx(-0.10)
    assert r.skew == _approx(-0.10)


def test_reward_bull_heavy_prior_raises_expectation() -> None:
    r = scenario_reward(
        price=100.0,
        base_fv=100.0,
        snapshot_json=_snap(weights={"bull": 0.35, "base": 0.50, "bear": 0.15}),
    )
    assert r is not None
    assert r.expected_return == _approx(0.10)


def test_reward_renormalizes_over_present_legs() -> None:
    # Bear scenario absent: the bear weight mass is renormalized over bull + base.
    r = scenario_reward(
        price=100.0,
        base_fv=100.0,
        snapshot_json=_snap(bear=None, weights={"bull": 0.20, "base": 0.50, "bear": 0.30}),
    )
    assert r is not None
    # present = bull(0.2) + base(0.5) = 0.7 mass → bull 0.2/0.7, base 0.5/0.7.
    assert r.expected_return == _approx((0.2 / 0.7) * 0.5)


# --------------------------------------------------------------------------- #
# 3. snapshot card parse + E[V]/skew
# --------------------------------------------------------------------------- #
def test_scenario_prior_card_parse() -> None:
    w, rationale, set_by = snapshot_mod._scenario_prior_card(  # pyright: ignore[reportPrivateUsage]
        _snap(weights={"bull": 0.15, "base": 0.50, "bear": 0.35})
    )
    assert w == {"bull": 0.15, "base": 0.50, "bear": 0.35}
    assert rationale == "fragile high-multiple thesis"
    assert set_by == "llm"
    # No block → all None.
    assert snapshot_mod._scenario_prior_card(_snap()) == (None, None, None)  # pyright: ignore[reportPrivateUsage]


def test_scenario_ev_skew() -> None:
    ev, skew = snapshot_mod._scenario_ev_skew(  # pyright: ignore[reportPrivateUsage]
        100.0, 100.0, _snap(weights={"bull": 0.15, "base": 0.50, "bear": 0.35})
    )
    assert ev == _approx(-0.10)
    assert skew == _approx(-0.10)
    # No tails → None (a point estimate has no skew to show).
    ev2, skew2 = snapshot_mod._scenario_ev_skew(  # pyright: ignore[reportPrivateUsage]
        100.0, 100.0, json.dumps({"format": "redesign"})
    )
    assert ev2 is None and skew2 is None


# --------------------------------------------------------------------------- #
# 4. fixture dcf_runs row → ValuationSnapshot fields
# --------------------------------------------------------------------------- #
def _seed_repo(tmp_path: Path, snapshot_json: str) -> Path:
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    conn = sqlite3.connect(str(repo / "data" / "portfolio.db"))
    conn.executescript(
        """
        CREATE TABLE dcf_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT UNIQUE,
            valuation_date TEXT, horizon_years INTEGER, wacc REAL, terminal_growth REAL,
            npv REAL, npv_per_share REAL, shares_outstanding REAL,
            currency TEXT, notes TEXT, run_id TEXT,
            live_price REAL, live_price_at TEXT, over_under_pct REAL,
            mos_bar_used REAL, assumption_snapshot_json TEXT,
            revenue_growths_json TEXT, fcf_margin REAL, breakdown_json TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO dcf_runs (ticker, valuation_date, horizon_years, wacc, terminal_growth,"
        " npv, npv_per_share, shares_outstanding, currency, live_price, over_under_pct,"
        " mos_bar_used, assumption_snapshot_json) VALUES"
        " ('TEST','2026-07-02',10,0.09,0,1500,100.0,100000000.0,'USD',100.0,0.0,0.25,?)",
        (snapshot_json,),
    )
    conn.commit()
    conn.close()
    return repo


def test_valuation_snapshot_carries_scenario_prior(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path, _snap(weights={"bull": 0.15, "base": 0.50, "bear": 0.35}))
    v = snapshot_mod._valuation_snapshot(  # pyright: ignore[reportPrivateUsage]
        "TEST", repo, current_price=None, model_link=None, mos_bar=None
    )
    assert v.scenario_weights == {"bull": 0.15, "base": 0.50, "bear": 0.35}
    assert v.scenario_set_by == "llm"
    assert v.scenario_rationale == "fragile high-multiple thesis"
    assert v.scenario_expected_return == _approx(-0.10)
    assert v.scenario_skew == _approx(-0.10)


def test_valuation_snapshot_reads_newest_run_not_superseded_history(tmp_path: Path) -> None:
    """The versioned dcf_runs schema (migration 0137) keeps superseded history
    rows per ticker. The card read MUST select the newest run — a bare LIMIT 1
    (no ORDER BY) reads the OLDEST row in practice, rendering a stale valuation
    with no priced_in/scenario_prior blocks (the bug the first prod hydration
    surfaced: every refreshed name got a second row and the card went blank)."""
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    conn = sqlite3.connect(str(repo / "data" / "portfolio.db"))
    # No UNIQUE(ticker): the 0137 schema keeps one row per (run), many per ticker.
    conn.executescript(
        """
        CREATE TABLE dcf_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT,
            valuation_date TEXT, horizon_years INTEGER, wacc REAL, terminal_growth REAL,
            npv REAL, npv_per_share REAL, shares_outstanding REAL,
            currency TEXT, notes TEXT, run_id TEXT,
            live_price REAL, live_price_at TEXT, over_under_pct REAL,
            mos_bar_used REAL, assumption_snapshot_json TEXT,
            revenue_growths_json TEXT, fcf_margin REAL, breakdown_json TEXT
        );
        """
    )
    old_snap = _snap()  # pre-hydration: no scenario_prior block
    new_snap = _snap(weights={"bull": 0.15, "base": 0.50, "bear": 0.35})
    conn.execute(
        "INSERT INTO dcf_runs (ticker, valuation_date, horizon_years, wacc, terminal_growth,"
        " npv, npv_per_share, shares_outstanding, currency, live_price, over_under_pct,"
        " mos_bar_used, assumption_snapshot_json) VALUES"
        " ('TEST','2026-07-02',10,0.09,0,1500,90.0,100000000.0,'USD',95.0,0.0556,0.25,?)",
        (old_snap,),
    )
    conn.execute(
        "INSERT INTO dcf_runs (ticker, valuation_date, horizon_years, wacc, terminal_growth,"
        " npv, npv_per_share, shares_outstanding, currency, live_price, over_under_pct,"
        " mos_bar_used, assumption_snapshot_json) VALUES"
        " ('TEST','2026-07-03',10,0.09,0,1500,100.0,100000000.0,'USD',100.0,0.0,0.25,?)",
        (new_snap,),
    )
    conn.commit()
    conn.close()

    v = snapshot_mod._valuation_snapshot(  # pyright: ignore[reportPrivateUsage]
        "TEST", repo, current_price=None, model_link=None, mos_bar=None
    )
    # The NEWEST run's values + blocks, not the superseded 07-02 row.
    assert v.consolidated_npv_per_share == _approx(100.0)
    assert str(v.valuation_date) == "2026-07-03"
    assert v.scenario_weights == {"bull": 0.15, "base": 0.50, "bear": 0.35}


# --------------------------------------------------------------------------- #
# 5. rendering
# --------------------------------------------------------------------------- #
def _card() -> ValuationSnapshot:
    return ValuationSnapshot(
        consolidated_npv_per_share=100.0,
        current_price=100.0,
        valuation_model_label="FCFF DCF",
        scenario_weights={"bull": 0.15, "base": 0.50, "bear": 0.35},
        scenario_rationale="fragile high-multiple thesis",
        scenario_set_by="llm",
        scenario_expected_return=-0.10,
        scenario_skew=-0.10,
    )


def test_markdown_card_renders_scenario_prior() -> None:
    out = StringIO()
    _valuation_card_md(out, _card())
    text = out.getvalue()
    assert "Scenario prior" in text
    assert "E[V] -10%" in text
    assert "skew -10.0pts" in text
    assert "15/50/35" in text
    assert "LLM-set" in text
    assert "fragile high-multiple thesis" in text


def test_markdown_card_silent_without_prior() -> None:
    out = StringIO()
    _valuation_card_md(
        out, ValuationSnapshot(consolidated_npv_per_share=100.0, current_price=100.0)
    )
    assert "Scenario prior" not in out.getvalue()


def test_workspace_card_renders_scenario_prior() -> None:
    snap = SnapshotSection(status=SectionStatus.OK, ticker="TEST", valuation=_card())
    body = StringIO()
    _valuation_summary_panel(body, snap)
    html_out = body.getvalue()
    assert "Scenario prior" in html_out
    assert "15/50/35 bull/base/bear" in html_out
    assert "Expected value E[V]" in html_out
    assert "-10%" in html_out
    assert "fragile high-multiple thesis" in html_out
