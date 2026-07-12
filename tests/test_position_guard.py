"""Naked-position gate (Monthly Red Team Phase 1 guard 7) — build_position_guard
classification, plus the Risk-tab section markup (pipeline.portfolio_panel)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from pipeline.portfolio_panel import (  # pyright: ignore[reportPrivateUsage]
    _add_trigger_advisories,
    _position_guard_section,
)
from position_guard import (
    CHECK_ADD,
    CHECK_BEAR,
    CHECK_DOWNSIDE,
    CHECK_THESIS,
    build_position_guard,
)
from position_guard_cache import to_cache_model

_NOW = datetime(2026, 7, 11, 12, 0, 0)


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE dcf_runs ("
        "id INTEGER PRIMARY KEY, ticker TEXT, created_at TEXT, is_latest INTEGER, "
        "segment_name TEXT, live_price REAL, assumption_snapshot_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE position_sizing_intent ("
        "id INTEGER PRIMARY KEY, ticker TEXT, intent_kind TEXT, intent_value REAL, "
        "narrative TEXT, created_at TEXT)"
    )
    conn.execute("CREATE TABLE v_thesis_status (ticker TEXT, thesis_updated_at TEXT)")
    conn.execute(
        "CREATE TABLE position_entries ("
        "id INTEGER PRIMARY KEY, ticker TEXT, entry_conviction TEXT, exit_date TEXT)"
    )
    conn.commit()
    conn.close()
    return db_path


def _insert_position_entry(
    db_path: Path, ticker: str, *, conviction: str | None, exit_date: str | None = None
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO position_entries (ticker, entry_conviction, exit_date) VALUES (?, ?, ?)",
        (ticker, conviction, exit_date),
    )
    conn.commit()
    conn.close()


def _weights_cache(repo_root: Path, weights: dict[str, float]) -> None:
    cache_dir = repo_root / "data"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "portfolio_weights.json").write_text(
        json.dumps({"weights": weights}), encoding="utf-8"
    )


def _snapshot(bear_fv: float | None) -> str | None:
    if bear_fv is None:
        return None
    return json.dumps({"scenarios": {"bear": {"fair_value_per_share_usd": bear_fv}, "base": {}}})


def _insert_bear_run(
    db_path: Path, ticker: str, *, live_price: float | None, bear_fv: float | None
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO dcf_runs (ticker, created_at, is_latest, segment_name, "
        "live_price, assumption_snapshot_json) VALUES (?, ?, 1, NULL, ?, ?)",
        (ticker, "2026-06-01T00:00:00", live_price, _snapshot(bear_fv)),
    )
    conn.commit()
    conn.close()


def _insert_intent(
    db_path: Path,
    ticker: str,
    *,
    intent_kind: str = "target_weight_pct",
    narrative: str | None,
    created_at: str = "2026-07-01T00:00:00",
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO position_sizing_intent (ticker, intent_kind, intent_value, narrative, "
        "created_at) VALUES (?, ?, NULL, ?, ?)",
        (ticker, intent_kind, narrative, created_at),
    )
    conn.commit()
    conn.close()


def _insert_thesis(db_path: Path, ticker: str, thesis_updated_at: str | None) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO v_thesis_status (ticker, thesis_updated_at) VALUES (?, ?)",
        (ticker, thesis_updated_at),
    )
    conn.commit()
    conn.close()


def _write_holdings(
    repo_root: Path,
    ticker: str,
    *,
    break_rules: list[object] | None = None,
    business_model_rules: list[object] | None = None,
    break_rules_soft: list[object] | None = None,
) -> None:
    d = repo_root / "micro_thesis" / "holdings"
    d.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {"ticker": ticker, "name": ticker}
    if break_rules is not None:
        payload["break_rules"] = break_rules
    if business_model_rules is not None:
        payload["business_model_rules"] = business_model_rules
    if break_rules_soft is not None:
        payload["break_rules_soft"] = break_rules_soft
    (d / f"{ticker}.json").write_text(json.dumps(payload), encoding="utf-8")


def _fully_covered(db_path: Path, repo_root: Path, ticker: str, *, weight: float = 0.05) -> None:
    """Wire a name to pass all three checks by default, so tests can flip
    exactly one thing and see exactly one failure."""
    _insert_bear_run(db_path, ticker, live_price=100.0, bear_fv=60.0)  # -40%, ok
    _insert_thesis(db_path, ticker, "2026-07-05T00:00:00")  # 6d old
    _write_holdings(repo_root, ticker, break_rules=[{"rule_id": "x"}])


# ---------------------------------------------------------------------------
# build_position_guard — weight filtering / exclusions
# ---------------------------------------------------------------------------


def test_no_weights_returns_empty_report(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    report = build_position_guard(db_path, tmp_path, now=_NOW)
    assert report.rows == []


def test_excludes_cash_like_and_index_etfs(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _weights_cache(
        tmp_path,
        {
            "AAA": 0.05,
            "SGOV": 0.1,
            "FDRXX": 0.05,
            "CUR:USD": 0.02,
            "VTI": 0.1,
            "SPY": 0.02,
            "VOO": 0.02,
        },
    )
    _fully_covered(db_path, tmp_path, "AAA")
    report = build_position_guard(db_path, tmp_path, now=_NOW)
    assert [r.ticker for r in report.rows] == ["AAA"]


def test_excludes_below_min_weight(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"TINY": 0.003, "AAA": 0.05})  # 0.3% vs 5%
    _fully_covered(db_path, tmp_path, "AAA")
    report = build_position_guard(db_path, tmp_path, now=_NOW)
    assert [r.ticker for r in report.rows] == ["AAA"]


# ---------------------------------------------------------------------------
# Check (a) — downside trigger encoded
# ---------------------------------------------------------------------------


def test_downside_pass_via_holdings_break_rules(tmp_path: Path) -> None:
    """RBRK / BKNG shape: no price-rung narrative, but holdings JSON carries
    break rules."""
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"RBRK": 0.08})
    _insert_bear_run(db_path, "RBRK", live_price=100.0, bear_fv=40.0)
    _insert_thesis(db_path, "RBRK", "2026-07-08T00:00:00")
    _write_holdings(
        tmp_path, "RBRK", break_rules=[{"rule_id": "a"}], business_model_rules=[{"rule_id": "b"}]
    )
    _insert_intent(
        db_path,
        "RBRK",
        narrative="Target ~7% after trim; re-add discretion only below fair value ~$66.50.",
    )
    (report,) = (build_position_guard(db_path, tmp_path, now=_NOW),)
    (row,) = report.rows
    assert row.downside_trigger.passed
    assert "holdings JSON" in row.downside_trigger.reason


def test_downside_pass_via_narrative_price_rung_and_action(tmp_path: Path) -> None:
    """FLKR shape: a below-price rung immediately followed by a cut/exit
    action, no holdings-JSON rules needed."""
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"FLKR": 0.07})
    _insert_bear_run(db_path, "FLKR", live_price=60.0, bear_fv=30.0)
    _insert_thesis(db_path, "FLKR", "2026-07-10T00:00:00")
    _insert_intent(
        db_path,
        "FLKR",
        narrative=(
            "Two-sided exit ladder. DOWNSIDE (new): close <$57.50 (-20% from cycle high) "
            "-> cut to 3.5% (momentum leg broken); close <$50 -> exit the momentum position "
            "entirely."
        ),
    )
    report = build_position_guard(db_path, tmp_path, now=_NOW)
    (row,) = report.rows
    assert row.downside_trigger.passed
    assert "downside rung" in row.downside_trigger.reason


def test_downside_fails_when_rung_has_no_downside_action(tmp_path: Path) -> None:
    """RBRK's actual narrative shape ISOLATED (no holdings-JSON fallback): a
    below-price trigger whose action is RE-ADD (a buy), not a cut/exit —
    must NOT false-positive pass."""
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"RBRK": 0.08})
    _insert_bear_run(db_path, "RBRK", live_price=100.0, bear_fv=40.0)
    _insert_thesis(db_path, "RBRK", "2026-07-08T00:00:00")
    _insert_intent(
        db_path,
        "RBRK",
        narrative="Trimmed on overvaluation; re-add discretion only below fair value ~$66.50.",
    )
    report = build_position_guard(db_path, tmp_path, now=_NOW)
    (row,) = report.rows
    assert not row.downside_trigger.passed


def test_downside_fails_when_no_rung_at_all(tmp_path: Path) -> None:
    """BKNG's shape isolated: a funding rationale with no price rung."""
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"BKNG": 0.04})
    _insert_bear_run(db_path, "BKNG", live_price=100.0, bear_fv=40.0)
    _insert_thesis(db_path, "BKNG", "2026-07-06T00:00:00")
    _insert_intent(
        db_path,
        "BKNG",
        narrative="Target 4%; funded by trimming oversized RBRK. Staged entry.",
    )
    report = build_position_guard(db_path, tmp_path, now=_NOW)
    (row,) = report.rows
    assert not row.downside_trigger.passed
    assert CHECK_DOWNSIDE in row.failed_checks


def test_downside_fails_when_nothing_on_file_flkr_canonical_case(tmp_path: Path) -> None:
    """FLKR before its 2026-07-10 ladder: real position, zero downside plan —
    the canonical FAIL this check exists to catch."""
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"FLKR": 0.06})
    _insert_bear_run(db_path, "FLKR", live_price=60.0, bear_fv=30.0)
    _insert_thesis(db_path, "FLKR", "2026-06-01T00:00:00")
    report = build_position_guard(db_path, tmp_path, now=_NOW)
    (row,) = report.rows
    assert not row.downside_trigger.passed
    assert not row.passed


def test_downside_pass_via_explicit_intent_kind(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"AAA": 0.05})
    _insert_bear_run(db_path, "AAA", live_price=100.0, bear_fv=40.0)
    _insert_thesis(db_path, "AAA", "2026-07-05T00:00:00")
    _insert_intent(db_path, "AAA", intent_kind="downside_exit_price", narrative="cut at $50")
    report = build_position_guard(db_path, tmp_path, now=_NOW)
    (row,) = report.rows
    assert row.downside_trigger.passed
    assert "intent kind" in row.downside_trigger.reason


# ---------------------------------------------------------------------------
# Check (d) — add_trigger (ADVISORY, PR9 Bull-side symmetry): only applies to
# HIGH-CONVICTION held names; never a violation.
# ---------------------------------------------------------------------------


def test_add_trigger_not_applicable_for_non_high_conviction_name(tmp_path: Path) -> None:
    """No position_entries row at all (or conviction != high) -> add_trigger
    is None (not applicable), not a fail — the check simply doesn't fire."""
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"AAA": 0.05})
    _fully_covered(db_path, tmp_path, "AAA")
    report = build_position_guard(db_path, tmp_path, now=_NOW)
    (row,) = report.rows
    assert row.add_trigger is None
    assert row.passed  # not applicable never taints the violation-grade checks


def test_add_trigger_fails_for_high_conviction_name_with_no_add_rung(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"NU": 0.10})
    _fully_covered(db_path, tmp_path, "NU")
    _insert_position_entry(db_path, "NU", conviction="high")
    report = build_position_guard(db_path, tmp_path, now=_NOW)
    (row,) = report.rows
    assert row.add_trigger is not None
    assert not row.add_trigger.passed
    assert "no add-rung on file" in row.add_trigger.reason
    # ADVISORY: never taints the violation-grade result.
    assert row.passed
    assert CHECK_ADD not in row.failed_checks


def test_add_trigger_ignores_closed_high_conviction_entries(tmp_path: Path) -> None:
    """A high-conviction entry that has already exited (exit_date set) must
    not put the ticker in scope for the advisory — only OPEN high-conviction
    positions count."""
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"NU": 0.10})
    _fully_covered(db_path, tmp_path, "NU")
    _insert_position_entry(db_path, "NU", conviction="high", exit_date="2026-01-01")
    report = build_position_guard(db_path, tmp_path, now=_NOW)
    (row,) = report.rows
    assert row.add_trigger is None


def test_add_trigger_passes_via_explicit_intent_kind(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"NU": 0.10})
    _fully_covered(db_path, tmp_path, "NU")
    _insert_position_entry(db_path, "NU", conviction="high")
    _insert_intent(db_path, "NU", intent_kind="add_rung", narrative="add at $10, +1% of book")
    report = build_position_guard(db_path, tmp_path, now=_NOW)
    (row,) = report.rows
    assert row.add_trigger is not None
    assert row.add_trigger.passed
    assert "intent kind" in row.add_trigger.reason


def test_add_trigger_passes_via_narrative_price_rung_and_add_action(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"NU": 0.10})
    _fully_covered(db_path, tmp_path, "NU")
    _insert_position_entry(db_path, "NU", conviction="high")
    _insert_intent(
        db_path,
        "NU",
        narrative="[draft] UPSIDE: add <$10.00 (+1% of book) if thesis intact / no breach.",
    )
    report = build_position_guard(db_path, tmp_path, now=_NOW)
    (row,) = report.rows
    assert row.add_trigger is not None
    assert row.add_trigger.passed
    assert "add-rung" in row.add_trigger.reason


def test_add_trigger_ambiguous_rung_without_add_action_fails(tmp_path: Path) -> None:
    """A below-price rung whose nearby action is a DOWNSIDE verb (cut/exit),
    not an add-shaped one, must not false-positive pass the add check —
    mirrors RBRK's downside-check isolation test."""
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"NU": 0.10})
    _fully_covered(db_path, tmp_path, "NU")
    _insert_position_entry(db_path, "NU", conviction="high")
    _insert_intent(db_path, "NU", narrative="DOWNSIDE: close <$10.00 -> cut to 5%.")
    report = build_position_guard(db_path, tmp_path, now=_NOW)
    (row,) = report.rows
    assert row.add_trigger is not None
    assert not row.add_trigger.passed  # this is the downside rung, not an add-rung


def test_add_trigger_advisory_never_pollutes_violations_or_pill(tmp_path: Path) -> None:
    """A high-conviction name failing add_trigger while otherwise fully
    covered must still count as passed/clean — the NAKED POSITIONS pill and
    report.violations are unaffected by the advisory check."""
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"NU": 0.10})
    _fully_covered(db_path, tmp_path, "NU")
    _insert_position_entry(db_path, "NU", conviction="high")
    report = build_position_guard(db_path, tmp_path, now=_NOW)
    (row,) = report.rows
    assert row.add_trigger is not None
    assert not row.add_trigger.passed
    assert row.passed
    assert report.violations == []


# ---------------------------------------------------------------------------
# Check (b) — realistic bear (reuses bear_lint)
# ---------------------------------------------------------------------------


def test_bear_check_passes_ok_and_shallow(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"OK": 0.05, "SHALLOW": 0.05})
    for t in ("OK", "SHALLOW"):
        _insert_thesis(db_path, t, "2026-07-05T00:00:00")
        _write_holdings(tmp_path, t, break_rules=[{"rule_id": "x"}])
    _insert_bear_run(db_path, "OK", live_price=100.0, bear_fv=60.0)  # -40%
    _insert_bear_run(db_path, "SHALLOW", live_price=100.0, bear_fv=90.0)  # -10%, shallow
    report = build_position_guard(db_path, tmp_path, now=_NOW)
    by_ticker = {r.ticker: r for r in report.rows}
    assert by_ticker["OK"].realistic_bear.passed
    assert by_ticker["SHALLOW"].realistic_bear.passed


def test_bear_check_fails_missing_and_not_a_bear_with_class_shown(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"MISSING": 0.05, "NOTABEAR": 0.05})
    for t in ("MISSING", "NOTABEAR"):
        _insert_thesis(db_path, t, "2026-07-05T00:00:00")
        _write_holdings(tmp_path, t, break_rules=[{"rule_id": "x"}])
    # MISSING gets no dcf_runs row at all.
    _insert_bear_run(db_path, "NOTABEAR", live_price=100.0, bear_fv=105.0)  # AT/ABOVE
    report = build_position_guard(db_path, tmp_path, now=_NOW)
    by_ticker = {r.ticker: r for r in report.rows}
    missing = by_ticker["MISSING"].realistic_bear
    not_a_bear = by_ticker["NOTABEAR"].realistic_bear
    assert not missing.passed and "MISSING" in missing.reason
    assert not not_a_bear.passed and "NOT A BEAR" in not_a_bear.reason
    assert CHECK_BEAR in by_ticker["MISSING"].failed_checks


# ---------------------------------------------------------------------------
# Check (c) — thesis freshness
# ---------------------------------------------------------------------------


def test_thesis_freshness_pass_and_fail(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"FRESH": 0.05, "STALE": 0.05, "NOTHESIS": 0.05})
    for t in ("FRESH", "STALE", "NOTHESIS"):
        _insert_bear_run(db_path, t, live_price=100.0, bear_fv=60.0)
        _write_holdings(tmp_path, t, break_rules=[{"rule_id": "x"}])
    _insert_thesis(db_path, "FRESH", "2026-07-01T00:00:00")  # 10d old
    _insert_thesis(db_path, "STALE", "2026-01-01T00:00:00")  # >90d old
    # NOTHESIS: no row in v_thesis_status at all.
    report = build_position_guard(db_path, tmp_path, now=_NOW)
    by_ticker = {r.ticker: r for r in report.rows}
    assert by_ticker["FRESH"].thesis_fresh.passed
    assert not by_ticker["STALE"].thesis_fresh.passed
    assert not by_ticker["NOTHESIS"].thesis_fresh.passed
    assert CHECK_THESIS in by_ticker["STALE"].failed_checks
    assert CHECK_THESIS in by_ticker["NOTHESIS"].failed_checks


# ---------------------------------------------------------------------------
# Row / report aggregation
# ---------------------------------------------------------------------------


def test_row_passed_requires_all_three_checks(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"AAA": 0.05})
    _fully_covered(db_path, tmp_path, "AAA")
    report = build_position_guard(db_path, tmp_path, now=_NOW)
    (row,) = report.rows
    assert row.passed
    assert row.failed_checks == ()
    assert report.violations == []


def test_violations_sorted_largest_weight_first(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"SMALL": 0.02, "BIG": 0.10, "GOOD": 0.05})
    # SMALL and BIG both fail (no thesis, no bear, no downside); GOOD passes.
    _fully_covered(db_path, tmp_path, "GOOD")
    report = build_position_guard(db_path, tmp_path, now=_NOW)
    tickers_in_order = [r.ticker for r in report.rows]
    assert tickers_in_order == ["BIG", "SMALL", "GOOD"]  # violations first, big->small, then pass
    assert [r.ticker for r in report.violations] == ["BIG", "SMALL"]


def test_missing_db_degrades_to_failed_checks_not_a_crash(tmp_path: Path) -> None:
    _weights_cache(tmp_path, {"AAA": 0.05})
    report = build_position_guard(tmp_path / "nope.db", tmp_path, now=_NOW)
    (row,) = report.rows
    assert not row.passed
    assert not row.realistic_bear.passed
    assert not row.thesis_fresh.passed


# ---------------------------------------------------------------------------
# Risk-tab section markup
# ---------------------------------------------------------------------------


def test_position_guard_section_empty_state() -> None:
    html = _position_guard_section(None)
    assert "Naked-position gate" in html
    assert "NAKED POSITIONS: 0" in html
    assert "No weighted holdings to gate yet" in html


def test_position_guard_section_all_clear(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"AAA": 0.05})
    _fully_covered(db_path, tmp_path, "AAA")
    report = build_position_guard(db_path, tmp_path, now=_NOW)
    cache = to_cache_model(report, computed_at=_NOW)
    html = _position_guard_section(cache)
    assert "NAKED POSITIONS: 0" in html
    assert "k-pill-ok" in html
    assert "Every held name clears the naked-position gate" in html
    assert "AAA" not in html  # no chip/table when nothing is flagged


def test_position_guard_section_renders_violation_chip_and_table(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"FLKR": 0.06})
    _insert_bear_run(db_path, "FLKR", live_price=60.0, bear_fv=30.0)  # ok bear
    # No thesis, no downside rule -> two failing checks.
    report = build_position_guard(db_path, tmp_path, now=_NOW)
    cache = to_cache_model(report, computed_at=_NOW)
    html = _position_guard_section(cache)
    assert "NAKED POSITIONS: 1" in html and "k-pill-bad" in html
    assert "FLKR: no downside rule + thesis stale" in html
    assert "encode an exit ladder" in html
    assert "k-chip-bad" in html
    assert "<table" in html


# ---------------------------------------------------------------------------
# Advisory add-rung rendering (PR9): a separate quiet row/chips, never folded
# into NAKED POSITIONS / violations / the table above.
# ---------------------------------------------------------------------------


def test_add_trigger_advisories_helper_only_returns_failing_high_conviction_rows(
    tmp_path: Path,
) -> None:
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"NU": 0.10, "MELI": 0.08, "AAA": 0.05})
    for t in ("NU", "MELI", "AAA"):
        _fully_covered(db_path, tmp_path, t)
    _insert_position_entry(db_path, "NU", conviction="high")  # no add-rung -> advisory
    _insert_position_entry(db_path, "MELI", conviction="high")
    _insert_intent(db_path, "MELI", intent_kind="add_rung", narrative="add at $1355")  # covered
    # AAA: not high conviction -> add_trigger stays None, never an advisory.
    report = build_position_guard(db_path, tmp_path, now=_NOW)
    cache = to_cache_model(report, computed_at=_NOW)
    advisories = _add_trigger_advisories(cache)
    assert [r.ticker for r in advisories] == ["NU"]


def test_position_guard_section_renders_advisory_separately_from_violations(
    tmp_path: Path,
) -> None:
    """A high-conviction name with no add-rung, otherwise fully covered, must
    render a quiet advisory chip while NAKED POSITIONS stays 0 and no
    violation chip/table appears for it."""
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"NU": 0.10})
    _fully_covered(db_path, tmp_path, "NU")
    _insert_position_entry(db_path, "NU", conviction="high")
    report = build_position_guard(db_path, tmp_path, now=_NOW)
    cache = to_cache_model(report, computed_at=_NOW)
    html = _position_guard_section(cache)
    assert "NAKED POSITIONS: 0" in html  # advisory never counted
    assert "k-pill-bad" not in html  # the summary pill stays k-pill-ok
    assert "NU: high conviction, no add-rung encoded" in html
    assert "Add-rung advisory" in html
    assert "k-chip-bad" not in html  # quiet chip, not a violation-tone chip


def test_position_guard_section_omits_advisory_block_when_none_apply(tmp_path: Path) -> None:
    db_path = _make_db(tmp_path)
    _weights_cache(tmp_path, {"AAA": 0.05})
    _fully_covered(db_path, tmp_path, "AAA")
    report = build_position_guard(db_path, tmp_path, now=_NOW)
    cache = to_cache_model(report, computed_at=_NOW)
    html = _position_guard_section(cache)
    assert "Add-rung advisory" not in html
