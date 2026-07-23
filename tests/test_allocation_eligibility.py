"""Tests for src/allocation/eligibility.py — the PRD §7.3 decision-ready gate.

Hand-rolled minimal schemas (no alembic-head fixture), modeled on
tests/test_position_guard.py's ``_make_db``/``_write_holdings`` pattern: only
the tables/columns each check reads are created.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from allocation.eligibility import (
    CHECK_CANDIDATE_FIT,
    CHECK_DIRECTIONAL_HYPOTHESIS,
    CHECK_DISCONFIRMERS,
    CHECK_KPI_COVERAGE,
    CHECK_PORTFOLIO_CONTEXT,
    CHECK_PRICE_FRESHNESS,
    CHECK_SOURCE_PROVENANCE,
    CHECK_USABLE_DCF,
    assess_eligibility,
    assess_universe,
    cash_assessment,
)

TICKER = "EVAL"
PORTFOLIO_TICKER = "HELD"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _iso(delta: timedelta = timedelta()) -> str:
    return (_now() - delta).isoformat()


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE dcf_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT,
            created_at TEXT,
            is_latest INTEGER,
            segment_name TEXT,
            valuation_date TEXT,
            npv_per_share REAL,
            live_price REAL,
            live_price_at TEXT,
            sanity_flag TEXT
        );
        CREATE TABLE kpi_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT,
            name TEXT
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kpi_definition_id INTEGER,
            ticker TEXT
        );
        CREATE TABLE thesis_state (
            ticker TEXT,
            thesis TEXT,
            raw_json TEXT,
            ingested_at TEXT
        );
        CREATE TABLE tracked_companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            ticker TEXT,
            name TEXT,
            list_type TEXT,
            added_at TEXT,
            sec_validated INTEGER,
            ir_url TEXT,
            instrument_type TEXT,
            filing_regime TEXT,
            fiscal_year_end TEXT,
            fmp_data_saved INTEGER,
            fmp_data_upto TEXT,
            archived_at TEXT
        );
        """
    )
    conn.commit()
    conn.close()
    return db_path


def _insert_dcf_row(
    db_path: Path,
    ticker: str,
    *,
    live_price_at: str | None,
    valuation_date: str = "2026-07-15",
    npv_per_share: float | None = 120.0,
    live_price: float | None = 100.0,
    sanity_flag: str | None = None,
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO dcf_runs (ticker, created_at, is_latest, segment_name, valuation_date, "
        "npv_per_share, live_price, live_price_at, sanity_flag) "
        "VALUES (?, ?, 1, NULL, ?, ?, ?, ?, ?)",
        (
            ticker,
            "2026-07-15T00:00:00",
            valuation_date,
            npv_per_share,
            live_price,
            live_price_at,
            sanity_flag,
        ),
    )
    conn.commit()
    conn.close()


def _insert_kpi_coverage(db_path: Path, ticker: str, *, n_def: int, n_covered: int) -> None:
    conn = sqlite3.connect(str(db_path))
    def_ids: list[int] = []
    for i in range(n_def):
        cur = conn.execute(
            "INSERT INTO kpi_definitions (ticker, name) VALUES (?, ?)", (ticker, f"kpi_{i}")
        )
        def_ids.append(int(cur.lastrowid or 0))
    for def_id in def_ids[:n_covered]:
        conn.execute(
            "INSERT INTO kpi_facts (kpi_definition_id, ticker) VALUES (?, ?)", (def_id, ticker)
        )
    conn.commit()
    conn.close()


def _insert_thesis(
    db_path: Path,
    ticker: str,
    *,
    thesis: str | None,
    raw_json: str | None = None,
    ingested_at: str | None = None,
) -> None:
    conn = sqlite3.connect(str(db_path))
    if thesis is not None:
        conn.execute(
            "INSERT INTO thesis_state (ticker, thesis, raw_json, ingested_at) VALUES (?, ?, ?, ?)",
            (ticker, thesis, raw_json, ingested_at or _iso()),
        )
    conn.commit()
    conn.close()


def _insert_tracked_company(
    db_path: Path, ticker: str, *, list_type: str, user_id: str = "bhanu"
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO tracked_companies (user_id, ticker, name, list_type, instrument_type) "
        "VALUES (?, ?, ?, ?, 'equity')",
        (user_id, ticker, ticker, list_type),
    )
    conn.commit()
    conn.close()


def _write_holdings(
    repo_root: Path,
    ticker: str,
    *,
    break_rules: list[object] | None = None,
    business_model_rules: list[object] | None = None,
    thesis_breakers_qualitative: list[str] | None = None,
) -> None:
    d = repo_root / "micro_thesis" / "holdings"
    d.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {"ticker": ticker, "thesis": f"{ticker} thesis placeholder."}
    if break_rules is not None:
        payload["break_rules"] = break_rules
    if business_model_rules is not None:
        payload["business_model_rules"] = business_model_rules
    if thesis_breakers_qualitative is not None:
        payload["thesis_breakers_qualitative"] = thesis_breakers_qualitative
    (d / f"{ticker}.json").write_text(json.dumps(payload), encoding="utf-8")


_FULL_BREAK_RULE: dict[str, object] = {
    "rule_id": "r1",
    "kpi_name": "Revenue growth YoY",
    "comparator": "lt",
    "threshold": 0,
    "unit": "percent",
    "narrative": "Revenue growth turns negative for a full quarter.",
}


def _write_weights_cache(repo_root: Path, weights: dict[str, float], *, computed_at: str) -> None:
    cache_dir = repo_root / "data"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "portfolio_weights.json").write_text(
        json.dumps({"computed_at": computed_at, "weights": weights}), encoding="utf-8"
    )


def _write_candidate_fit_cache(
    repo_root: Path, ticker: str, *, fit: float = 1.1, computed_at: str
) -> None:
    cache_dir = repo_root / "data"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "candidate_fit.json").write_text(
        json.dumps(
            {
                "version": 2,
                "computed_at": computed_at,
                "book": {},
                "target": {},
                "fits": {ticker: {"fit": fit, "why": "test", "partial": False, "factors": []}},
            }
        ),
        encoding="utf-8",
    )


def _fully_eligible(db_path: Path, repo_root: Path, ticker: str, *, with_fit: bool = True) -> None:
    """Wire ``ticker`` to pass all 8 checks — tests flip exactly one thing."""
    _insert_dcf_row(db_path, ticker, live_price_at=_iso(timedelta(days=1)))
    _insert_kpi_coverage(db_path, ticker, n_def=2, n_covered=2)
    if with_fit:
        _write_candidate_fit_cache(repo_root, ticker, computed_at=_iso(timedelta(hours=1)))
    _insert_thesis(
        db_path, ticker, thesis=f"{ticker} grows revenue 20%/yr on strong unit economics."
    )
    _write_holdings(repo_root, ticker, break_rules=[_FULL_BREAK_RULE])
    _write_weights_cache(repo_root, {ticker: 0.05}, computed_at=_iso(timedelta(hours=1)))


# --------------------------------------------------------------------------- #
# Cash
# --------------------------------------------------------------------------- #


def test_cash_always_eligible() -> None:
    a = cash_assessment()
    assert a.eligible
    assert a.blocking_reasons == ()
    assert a.ticker == "CASH"
    assert a.portfolio_fit_status == "cash"
    assert all(c.passed for c in a.checks.values())


# --------------------------------------------------------------------------- #
# Full-pass baseline (evaluation name — the strictest class)
# --------------------------------------------------------------------------- #


def test_fully_eligible_evaluation_name_passes_every_check(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _fully_eligible(db_path, tmp_path, TICKER)
    a = assess_eligibility(db_path, tmp_path, TICKER, list_type="evaluation")
    assert a.eligible, a.blocking_reasons
    assert a.blocking_reasons == ()
    assert a.hypothesis_origin == "user_authored"
    assert a.portfolio_fit_status == "scored"
    assert all(c.passed for c in a.checks.values())


# --------------------------------------------------------------------------- #
# Check 1 — price freshness
# --------------------------------------------------------------------------- #


def test_stale_price_blocks(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _fully_eligible(db_path, tmp_path, TICKER)
    # Overwrite the dcf row with a stale live_price_at (> 7d).
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM dcf_runs")
    conn.commit()
    conn.close()
    _insert_dcf_row(db_path, TICKER, live_price_at=_iso(timedelta(days=10)))
    a = assess_eligibility(db_path, tmp_path, TICKER, list_type="evaluation")
    assert not a.eligible
    assert not a.checks[CHECK_PRICE_FRESHNESS].passed
    assert any("stale" in r for r in a.blocking_reasons)


def test_missing_live_price_at_blocks(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _fully_eligible(db_path, tmp_path, TICKER)
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM dcf_runs")
    conn.commit()
    conn.close()
    _insert_dcf_row(db_path, TICKER, live_price_at=None)
    a = assess_eligibility(db_path, tmp_path, TICKER, list_type="evaluation")
    assert not a.eligible
    assert not a.checks[CHECK_PRICE_FRESHNESS].passed


# --------------------------------------------------------------------------- #
# Check 2 — usable DCF
# --------------------------------------------------------------------------- #


def test_outlier_sanity_flag_blocks(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _fully_eligible(db_path, tmp_path, TICKER)
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM dcf_runs")
    conn.commit()
    conn.close()
    _insert_dcf_row(db_path, TICKER, live_price_at=_iso(timedelta(days=1)), sanity_flag="outlier")
    a = assess_eligibility(db_path, tmp_path, TICKER, list_type="evaluation")
    assert not a.eligible
    assert not a.checks[CHECK_USABLE_DCF].passed
    assert any("sanity_flag" in r for r in a.blocking_reasons)
    # Price freshness itself still passes — sanity_flag is orthogonal.
    assert a.checks[CHECK_PRICE_FRESHNESS].passed


def test_missing_fair_value_blocks_usable_dcf(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _fully_eligible(db_path, tmp_path, TICKER)
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM dcf_runs")
    conn.commit()
    conn.close()
    _insert_dcf_row(db_path, TICKER, live_price_at=_iso(timedelta(days=1)), npv_per_share=None)
    a = assess_eligibility(db_path, tmp_path, TICKER, list_type="evaluation")
    assert not a.eligible
    assert not a.checks[CHECK_USABLE_DCF].passed


def test_no_dcf_row_blocks_both_price_and_usable_checks(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _fully_eligible(db_path, tmp_path, TICKER)
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM dcf_runs")
    conn.commit()
    conn.close()
    a = assess_eligibility(db_path, tmp_path, TICKER, list_type="evaluation")
    assert not a.eligible
    assert not a.checks[CHECK_PRICE_FRESHNESS].passed
    assert not a.checks[CHECK_USABLE_DCF].passed


# --------------------------------------------------------------------------- #
# Check 3 — KPI coverage
# --------------------------------------------------------------------------- #


def test_low_kpi_coverage_blocks_evaluation_name(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _fully_eligible(db_path, tmp_path, TICKER)
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM kpi_facts")
    conn.execute("DELETE FROM kpi_definitions")
    conn.commit()
    conn.close()
    _insert_kpi_coverage(db_path, TICKER, n_def=4, n_covered=1)  # 25% < 50% floor
    a = assess_eligibility(db_path, tmp_path, TICKER, list_type="evaluation")
    assert not a.eligible
    assert not a.checks[CHECK_KPI_COVERAGE].passed


def test_zero_kpi_definitions_warns_portfolio_name_but_blocks_evaluation_name(
    tmp_path: Path,
) -> None:
    db_path = _make_db(tmp_path)
    _fully_eligible(db_path, tmp_path, TICKER, with_fit=False)
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM kpi_facts")
    conn.execute("DELETE FROM kpi_definitions")
    conn.commit()
    conn.close()

    a_eval = assess_eligibility(db_path, tmp_path, TICKER, list_type="evaluation")
    assert not a_eval.eligible
    assert not a_eval.checks[CHECK_KPI_COVERAGE].passed

    a_portfolio = assess_eligibility(db_path, tmp_path, TICKER, list_type="portfolio")
    assert a_portfolio.checks[CHECK_KPI_COVERAGE].passed
    assert any("no KPI definitions" in w for w in a_portfolio.warning_reasons)


# --------------------------------------------------------------------------- #
# Check 4 — candidate fit
# --------------------------------------------------------------------------- #


def test_missing_candidate_fit_blocks_evaluation_name_but_not_portfolio_name(
    tmp_path: Path,
) -> None:
    db_path = _make_db(tmp_path)
    _fully_eligible(db_path, tmp_path, TICKER, with_fit=False)  # no candidate_fit.json at all

    a_eval = assess_eligibility(db_path, tmp_path, TICKER, list_type="evaluation")
    assert not a_eval.eligible
    assert not a_eval.checks[CHECK_CANDIDATE_FIT].passed
    assert a_eval.portfolio_fit_status == "missing"

    a_portfolio = assess_eligibility(db_path, tmp_path, TICKER, list_type="portfolio")
    assert a_portfolio.checks[CHECK_CANDIDATE_FIT].passed
    assert a_portfolio.portfolio_fit_status == "held"


# --------------------------------------------------------------------------- #
# Check 5 — explicit directional hypothesis
# --------------------------------------------------------------------------- #


def test_stub_thesis_is_eligible_and_labeled_system_drafted(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _fully_eligible(db_path, tmp_path, TICKER)
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM thesis_state")
    conn.commit()
    conn.close()
    _insert_thesis(db_path, TICKER, thesis="STUB: needs user-authored thesis")
    a = assess_eligibility(db_path, tmp_path, TICKER, list_type="evaluation")
    assert a.eligible, a.blocking_reasons
    assert a.hypothesis_origin == "system_drafted"
    assert a.checks[CHECK_DIRECTIONAL_HYPOTHESIS].passed


def test_corruption_stub_is_missing_and_blocks(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _fully_eligible(db_path, tmp_path, TICKER)
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM thesis_state")
    conn.commit()
    conn.close()
    _insert_thesis(
        db_path,
        TICKER,
        thesis="placeholder",
        raw_json=json.dumps({"_status": "stub_regenerated_from_corruption"}),
    )
    a = assess_eligibility(db_path, tmp_path, TICKER, list_type="evaluation")
    assert not a.eligible
    assert a.hypothesis_origin == "missing"
    assert not a.checks[CHECK_DIRECTIONAL_HYPOTHESIS].passed


def test_no_thesis_row_is_missing_and_blocks(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _fully_eligible(db_path, tmp_path, TICKER)
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM thesis_state")
    conn.commit()
    conn.close()
    a = assess_eligibility(db_path, tmp_path, TICKER, list_type="evaluation")
    assert not a.eligible
    assert a.hypothesis_origin == "missing"


# --------------------------------------------------------------------------- #
# Check 6 — disconfirmers
# --------------------------------------------------------------------------- #


def test_zero_disconfirmers_blocks(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _fully_eligible(db_path, tmp_path, TICKER)
    (tmp_path / "micro_thesis" / "holdings" / f"{TICKER}.json").write_text(
        json.dumps({"ticker": TICKER, "thesis": "placeholder"}), encoding="utf-8"
    )
    a = assess_eligibility(db_path, tmp_path, TICKER, list_type="evaluation")
    assert not a.eligible
    assert not a.checks[CHECK_DISCONFIRMERS].passed


def test_qualitative_only_disconfirmers_pass_with_warning(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _fully_eligible(db_path, tmp_path, TICKER)
    _write_holdings(
        tmp_path,
        TICKER,
        thesis_breakers_qualitative=["A larger competitor undercuts pricing materially."],
    )
    a = assess_eligibility(db_path, tmp_path, TICKER, list_type="evaluation")
    assert a.eligible, a.blocking_reasons
    assert a.checks[CHECK_DISCONFIRMERS].passed
    assert any("qualitative-only" in w for w in a.warning_reasons)


# --------------------------------------------------------------------------- #
# Check 7 — portfolio context
# --------------------------------------------------------------------------- #


def test_missing_weights_cache_blocks(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _fully_eligible(db_path, tmp_path, TICKER)
    (tmp_path / "data" / "portfolio_weights.json").unlink()
    a = assess_eligibility(db_path, tmp_path, TICKER, list_type="evaluation")
    assert not a.eligible
    assert not a.checks[CHECK_PORTFOLIO_CONTEXT].passed


def test_stale_weights_cache_warns_but_does_not_block(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _fully_eligible(db_path, tmp_path, TICKER)
    _write_weights_cache(tmp_path, {TICKER: 0.05}, computed_at=_iso(timedelta(hours=72)))
    a = assess_eligibility(db_path, tmp_path, TICKER, list_type="evaluation")
    assert a.checks[CHECK_PORTFOLIO_CONTEXT].passed
    assert any("weights cache" in w for w in a.warning_reasons)
    # Overall eligibility is unaffected by a warning-only check.
    assert a.eligible


# --------------------------------------------------------------------------- #
# Check 8 — source provenance
# --------------------------------------------------------------------------- #


def test_source_freshness_populated_for_every_required_source(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _fully_eligible(db_path, tmp_path, TICKER)
    a = assess_eligibility(db_path, tmp_path, TICKER, list_type="evaluation")
    assert a.checks[CHECK_SOURCE_PROVENANCE].passed
    for key in ("price", "dcf", "weights", "thesis", "fit"):
        assert key in a.source_freshness, key


# --------------------------------------------------------------------------- #
# input_sha
# --------------------------------------------------------------------------- #


def test_input_sha_stable_for_identical_inputs(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _fully_eligible(db_path, tmp_path, TICKER)
    a1 = assess_eligibility(db_path, tmp_path, TICKER, list_type="evaluation")
    a2 = assess_eligibility(db_path, tmp_path, TICKER, list_type="evaluation")
    assert a1.input_sha == a2.input_sha


def test_input_sha_changes_when_a_source_changes(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _fully_eligible(db_path, tmp_path, TICKER)
    a1 = assess_eligibility(db_path, tmp_path, TICKER, list_type="evaluation")
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM dcf_runs")
    conn.commit()
    conn.close()
    _insert_dcf_row(db_path, TICKER, live_price_at=_iso(timedelta(days=2)), npv_per_share=150.0)
    a2 = assess_eligibility(db_path, tmp_path, TICKER, list_type="evaluation")
    assert a1.input_sha != a2.input_sha


# --------------------------------------------------------------------------- #
# assess_universe
# --------------------------------------------------------------------------- #


def test_assess_universe_covers_portfolio_and_evaluation_lists(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _fully_eligible(db_path, tmp_path, PORTFOLIO_TICKER, with_fit=False)
    _fully_eligible(db_path, tmp_path, TICKER)
    _insert_tracked_company(db_path, PORTFOLIO_TICKER, list_type="portfolio")
    _insert_tracked_company(db_path, TICKER, list_type="evaluation")
    out = assess_universe(db_path, tmp_path)
    assert set(out) == {PORTFOLIO_TICKER, TICKER}
    assert out[PORTFOLIO_TICKER].list_type == "portfolio"
    assert out[TICKER].list_type == "evaluation"


def test_assess_universe_missing_db_degrades_to_empty(tmp_path: Path) -> None:
    out = assess_universe(tmp_path / "nope.db", tmp_path)
    assert out == {}
