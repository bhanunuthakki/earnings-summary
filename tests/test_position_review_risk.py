"""The C7 risk-aware ``/review`` block — ``RiskContext`` + ``_build_risk_context``
+ ``render_risk_lines`` + ``_risk_prompt_block``, and their wiring into
``render_pre_analysis_chat``/``render_pre_analysis_plain``.

Mirrors ``tests/test_position_review_capacity_block.py``'s shape and its
byte-identical-when-empty discipline: every sub-leg (book risk share/corr,
crowding cluster, C3 business-factor loadings, C5 event-scenario membership)
degrades independently and never raises; when EVERY leg comes back empty the
whole block is absent (``risk=None``) and ``/review`` renders EXACTLY as it
did before this block existed.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from advisor.position_review import (
    PreAnalysis,
    RiskContext,
    _build_risk_context,
    _risk_prompt_block,
    render_pre_analysis_chat,
    render_pre_analysis_plain,
    render_risk_lines,
)

_START = date(2024, 1, 1)


def _write_chart(repo: Path, ticker: str, returns: list[float], start_price: float = 100.0) -> None:
    """Duplicated from tests/test_what_if.py deliberately (repo convention:
    duplicate simple shared test fixtures rather than modularize them)."""
    fmp = repo / "data" / "historical" / "fmp"
    fmp.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = [{"date": _START.isoformat(), "adjClose": start_price}]
    price = start_price
    for i, r in enumerate(returns, start=1):
        price *= math.exp(r)
        rows.append({"date": (_START + timedelta(days=i)).isoformat(), "adjClose": price})
    (fmp / f"{ticker.upper()}_price_chart_10y_div_adj.json").write_text(
        json.dumps(rows), encoding="utf-8"
    )


def _write_weights_cache(repo_root: Path, weights: dict[str, float]) -> None:
    cache = repo_root / "data" / "portfolio_weights.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps({"computed_at": "2026-07-24T00:00:00", "weights": weights}), encoding="utf-8"
    )


_FACTOR_DDL = """
CREATE TABLE business_factor_exposures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    factor TEXT NOT NULL,
    loading REAL NOT NULL,
    rationale TEXT,
    provenance TEXT NOT NULL,
    input_sha TEXT,
    owner_edited INTEGER NOT NULL DEFAULT 0,
    is_latest INTEGER NOT NULL DEFAULT 1,
    superseded_by INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _write_factor_exposures(db_path: Path, rows: list[tuple[str, str, float, bool]]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_FACTOR_DDL)
    now = "2026-07-24T00:00:00"
    for ticker, factor, loading, is_latest in rows:
        conn.execute(
            "INSERT INTO business_factor_exposures "
            "(ticker, factor, loading, provenance, is_latest, created_at, updated_at) "
            "VALUES (?, ?, ?, 'segment_derived', ?, ?, ?)",
            (ticker, factor, loading, int(is_latest), now, now),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def market() -> list[float]:
    return [0.012 * math.sin(i / 6.0) + 0.0005 for i in range(200)]


# --------------------------------------------------------------------------- #
# _build_risk_context — full read (all legs populated)
# --------------------------------------------------------------------------- #


def test_full_risk_context_populates_every_leg(tmp_path: Path, market: list[float]) -> None:
    """NU + MELI with IDENTICAL price series (corr = 1.0, well past the 0.70
    crowding threshold) cluster together; NU also carries C3 loadings and is
    a NAMED member of the real (unmocked) EVENT_SCENARIOS 'joint_latam'
    scenario — every leg should populate for NU."""
    _write_chart(tmp_path, "NU", list(market))
    _write_chart(tmp_path, "MELI", list(market))
    _write_weights_cache(tmp_path, {"NU": 0.15, "MELI": 0.10})
    db_path = tmp_path / "data" / "portfolio.db"
    _write_factor_exposures(
        db_path,
        [
            ("NU", "Brazil consumer credit", 0.9, True),
            ("NU", "LatAm consumer/FX", 0.7, True),
            ("NU", "Mexico expansion", 0.3, True),
            ("NU", "STALE_FACTOR", 0.99, False),  # superseded — must be excluded
        ],
    )

    risk = _build_risk_context("NU", db_path, tmp_path)

    assert risk is not None
    assert risk.risk_share_pct is not None
    assert risk.corr_to_book is not None
    assert risk.crowding_cluster is not None
    assert "MELI" in risk.crowding_cluster
    # Highest-loading first, capped at 3, no is_latest=0 row.
    assert risk.top_factors == (
        ("Brazil consumer credit", 0.9),
        ("LatAm consumer/FX", 0.7),
        ("Mexico expansion", 0.3),
    )
    assert risk.event_scenarios == ("joint_latam",)
    assert not risk.is_empty()


def test_book_risk_share_matches_build_book_risk_directly(
    tmp_path: Path, market: list[float]
) -> None:
    """The risk_share_pct/corr_to_book legs are a pure passthrough of
    allocation.book_risk.build_book_risk's own numbers (times 100 for the
    percent unit) — never a re-derivation that could drift from the Risk
    tab's own read."""
    from allocation.book_risk import build_book_risk

    _write_chart(tmp_path, "NU", list(market))
    _write_chart(tmp_path, "MELI", [0.01 * math.cos(i / 5.0) for i in range(200)])
    weights = {"NU": 0.15, "MELI": 0.10}
    _write_weights_cache(tmp_path, weights)

    risk = _build_risk_context("NU", tmp_path / "does_not_exist.db", tmp_path)
    book = build_book_risk(tmp_path, list(weights), weights)

    assert risk is not None
    assert risk.risk_share_pct == pytest.approx(book.risk_share["NU"] * 100.0)
    assert risk.corr_to_book == pytest.approx(book.corr_to_book["NU"])


# --------------------------------------------------------------------------- #
# Independent per-leg degrade
# --------------------------------------------------------------------------- #


def test_absent_business_factor_table_degrades_that_leg_only(
    tmp_path: Path, market: list[float]
) -> None:
    """A real DB that predates the C3 migration (no business_factor_exposures
    table) must degrade ONLY the factors leg (sqlite3.OperationalError,
    caught) — the book-risk/crowding/event legs (which don't touch that
    table) still populate."""
    _write_chart(tmp_path, "NU", list(market))
    _write_chart(tmp_path, "MELI", list(market))
    _write_weights_cache(tmp_path, {"NU": 0.15, "MELI": 0.10})
    db_path = tmp_path / "data" / "portfolio.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(str(db_path)).close()  # a real, empty DB — no tables at all

    risk = _build_risk_context("NU", db_path, tmp_path)

    assert risk is not None
    assert risk.top_factors == ()
    assert any("business_factor_exposures" in r for r in risk.degraded_reasons)
    # The OTHER legs survived the table-absent leg's failure.
    assert risk.risk_share_pct is not None
    assert risk.event_scenarios == ("joint_latam",)


def test_no_database_on_file_degrades_factors_leg_cleanly(
    tmp_path: Path, market: list[float]
) -> None:
    """A db_path pointing at nothing on disk (never even opened) is the same
    clean-absence contract as a missing table — no exception, just a
    degraded-reason note."""
    _write_chart(tmp_path, "NU", list(market))
    _write_chart(tmp_path, "MELI", list(market))
    _write_weights_cache(tmp_path, {"NU": 0.15, "MELI": 0.10})

    risk = _build_risk_context("NU", tmp_path / "does_not_exist.db", tmp_path)

    assert risk is not None
    assert risk.top_factors == ()
    assert any("no database on file" in r for r in risk.degraded_reasons)


def test_empty_weights_cache_degrades_book_risk_and_crowding_legs(tmp_path: Path) -> None:
    """No materialized weights cache on disk -> book-risk and crowding legs
    both degrade (nothing to price against); a ticker outside every
    EVENT_SCENARIOS + no factor table still yields a fully-empty (None)
    context."""
    risk = _build_risk_context("ZZZZ", tmp_path / "does_not_exist.db", tmp_path)
    assert risk is None


def test_ticker_not_in_priced_matrix_degrades_book_risk_leg(
    tmp_path: Path, market: list[float]
) -> None:
    """NU and MELI are priced and weighted, but AAPL (unpriced, unweighted,
    and not a NAMED member of any EVENT_SCENARIOS entry) is being reviewed —
    every leg degrades and the whole context collapses to None."""
    _write_chart(tmp_path, "NU", list(market))
    _write_chart(tmp_path, "MELI", list(market))
    _write_weights_cache(tmp_path, {"NU": 0.15, "MELI": 0.10})

    risk = _build_risk_context("AAPL", tmp_path / "does_not_exist.db", tmp_path)

    assert risk is None  # nothing at all applies to AAPL in this fixture


# --------------------------------------------------------------------------- #
# Every-leg-degraded -> None -> byte-identical /review render (the plan's
# explicit degrade test)
# --------------------------------------------------------------------------- #


_PRE_DEFAULT = PreAnalysis(
    ticker="RBRK",
    weight_pct=10.0,
    weight_source="materialized",
    market_value_usd=None,
    unrealized_pnl_usd=None,
    target_weight_pct=None,
    target_band=None,
    weight_vs_band="no_band",
    conviction_1_5=None,
    concentration_flag=False,
    thesis_present=True,
    verdict_label="Intact",
    key_driver="Net new subscription ARR cadence",
    break_rule_status="intact",
    tripped_rules=(),
    dcf_gap_pct=-10.0,
    npv_per_share=91.09,
    dcf_live_price=80.35,
    dcf_date="2026-06-15",
    at_price=None,
    mos_bar=0.30,
    valuation_verdict="fair",
    conviction_encoded=True,
    has_stance=True,
    has_decision_note=True,
    is_index_instrument=False,
)


def test_every_leg_degraded_yields_none(tmp_path: Path) -> None:
    assert _build_risk_context("ZZZZ", tmp_path / "nope.db", tmp_path) is None


def test_chat_render_byte_identical_risk_none_vs_empty_context() -> None:
    pre_none = _PRE_DEFAULT
    pre_empty = replace(_PRE_DEFAULT, risk=RiskContext())
    assert render_pre_analysis_chat(pre_none) == render_pre_analysis_chat(pre_empty)


def test_plain_render_byte_identical_risk_none_vs_empty_context() -> None:
    pre_none = _PRE_DEFAULT
    pre_empty = replace(_PRE_DEFAULT, risk=RiskContext())
    assert render_pre_analysis_plain(pre_none) == render_pre_analysis_plain(pre_empty)


def test_chat_render_no_risk_lines_when_empty() -> None:
    out = render_pre_analysis_chat(_PRE_DEFAULT)
    assert "Risk:" not in out
    assert "Crowding:" not in out
    assert "Business factors:" not in out
    assert "Event scenarios:" not in out


def test_chat_render_risk_lines_appear_between_capacity_and_mechanical() -> None:
    risk = RiskContext(risk_share_pct=14.0, corr_to_book=0.62)
    pre = replace(_PRE_DEFAULT, risk=risk)
    out = render_pre_analysis_chat(pre)
    assert out.index("- Risk:") < out.index("- Mechanical read:")


# --------------------------------------------------------------------------- #
# render_risk_lines
# --------------------------------------------------------------------------- #


def test_render_risk_lines_none_and_empty_both_yield_no_lines() -> None:
    assert render_risk_lines(None) == []
    assert render_risk_lines(RiskContext()) == []


def test_render_risk_lines_full_context() -> None:
    risk = RiskContext(
        risk_share_pct=14.3,
        corr_to_book=0.62,
        crowding_cluster="co-moves with NU (avg corr 0.91, 25% combined weight)",
        top_factors=(("Brazil consumer credit", 0.9), ("LatAm consumer/FX", 0.7)),
        event_scenarios=("joint_latam",),
    )
    lines = render_risk_lines(risk)
    text = "\n".join(lines)
    assert "14% of book risk" in text
    assert "corr-to-book +0.62" in text
    assert "co-moves with NU" in text
    assert "Brazil consumer credit 0.9" in text and "LatAm consumer/FX 0.7" in text
    assert "joint_latam" in text


def test_render_risk_lines_partial_context_only_renders_populated_bits() -> None:
    risk = RiskContext(crowding_cluster="co-moves with NU (avg corr 0.91, 25% combined weight)")
    lines = render_risk_lines(risk)
    text = "\n".join(lines)
    assert "Risk:" not in text  # no risk_share_pct/corr_to_book -> no Risk: bullet
    assert "Crowding:" in text


# --------------------------------------------------------------------------- #
# _risk_prompt_block — the verdict-prompt RISK line
# --------------------------------------------------------------------------- #


def test_risk_prompt_block_empty_for_none_and_empty_context() -> None:
    assert _risk_prompt_block(None) == ""
    assert _risk_prompt_block(RiskContext()) == ""


def test_risk_prompt_block_contains_factors_and_scenarios() -> None:
    risk = RiskContext(
        risk_share_pct=14.0,
        corr_to_book=0.62,
        crowding_cluster="co-moves with NU (avg corr 0.91, 25% combined weight)",
        top_factors=(("Brazil consumer credit", 0.9), ("LatAm consumer/FX", 0.7)),
        event_scenarios=("joint_latam",),
    )
    block = _risk_prompt_block(risk)
    assert block.startswith("RISK:")
    assert "14% of book risk" in block
    assert "cluster: co-moves with NU" in block
    assert "factors: Brazil consumer credit 0.9, LatAm consumer/FX 0.7" in block
    assert "scenarios: joint_latam" in block
