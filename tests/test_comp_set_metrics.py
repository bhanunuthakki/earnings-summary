"""Unit tests for compute.comp_set_metrics -- the bottoms-up aggregate math
(docs/design/comparable_sets_bottoms_up.md section 5): the all-or-nothing TTM
coverage gate, the negative-earnings median/aggregate split, coverage honesty,
and the financial/operating metric-class gating.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import compute.comp_set_metrics as csm  # noqa: E402
from compute.comp_set_metrics import _sum_last4  # noqa: E402  # pyright: ignore[reportPrivateUsage]
from compute.comparable_sets import MetricClass  # noqa: E402


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _quarterly(dates: list[str], **fields: list[float | None]) -> list[dict[str, object]]:
    """Build a quarterly-records list; ``fields`` maps column -> per-date values."""
    out: list[dict[str, object]] = []
    for i, d in enumerate(dates):
        row: dict[str, object] = {"date": d}
        for key, vals in fields.items():
            if vals[i] is not None:
                row[key] = vals[i]
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# _sum_last4 — all-or-nothing coverage gate
# ---------------------------------------------------------------------------


def test_sum_last4_requires_all_four_quarters() -> None:
    full: list[dict[str, object]] = [
        {"netIncome": 10.0},
        {"netIncome": 20.0},
        {"netIncome": 30.0},
        {"netIncome": 40.0},
    ]
    assert _sum_last4(full, "netIncome") == 100.0

    partial: list[dict[str, object]] = [
        {"netIncome": 10.0},
        {"netIncome": 20.0},
        {"netIncome": 30.0},
    ]
    assert _sum_last4(partial, "netIncome") is None

    one_missing: list[dict[str, object]] = [
        {"netIncome": 10.0},
        {},
        {"netIncome": 30.0},
        {"netIncome": 40.0},
    ]
    assert _sum_last4(one_missing, "netIncome") is None


def test_load_member_financials_ttm_gate(tmp_path: Path) -> None:
    fmp_dir = tmp_path / "data" / "historical" / "fmp"
    dates = ["2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30", "2025-03-31"]
    _write_json(
        fmp_dir / "FULL_income_statement_quarterly.json",
        _quarterly(
            dates,
            netIncome=[10.0, 10.0, 10.0, 10.0, 999.0],
            ebitda=[20.0, 20.0, 20.0, 20.0, 999.0],
            revenue=[100.0] * 5,
        ),
    )
    _write_json(
        fmp_dir / "THIN_income_statement_quarterly.json",
        # Only 3 quarters on file -- recently IPO'd, per doc section 3.2.
        _quarterly(
            dates[:3],
            netIncome=[10.0, 10.0, 10.0],
            ebitda=[20.0, 20.0, 20.0],
            revenue=[100.0, 100.0, 100.0],
        ),
    )

    full = csm.load_member_financials(fmp_dir, "FULL", context_only=False, as_of=date(2026, 7, 17))
    assert full.ttm_net_income == 40.0
    assert full.ttm_ebitda == 80.0

    thin = csm.load_member_financials(fmp_dir, "THIN", context_only=False, as_of=date(2026, 7, 17))
    assert thin.ttm_net_income is None  # <4 quarters -- naturally excluded, no fabrication
    assert thin.ttm_ebitda is None


# ---------------------------------------------------------------------------
# Negative-earnings median/aggregate split (section 5.2)
# ---------------------------------------------------------------------------


def _member(
    ticker: str,
    *,
    market_cap: float | None,
    ttm_net_income: float | None,
    context_only: bool = False,
    ttm_ebitda: float | None = None,
    enterprise_value: float | None = None,
) -> csm.MemberFinancials:
    return csm.MemberFinancials(
        ticker=ticker,
        context_only=context_only,
        market_cap=market_cap,
        enterprise_value=enterprise_value,
        ev_approximated=False,
        ttm_net_income=ttm_net_income,
        ttm_ebitda=ttm_ebitda,
        p_b=None,
        p_tbv=None,
        rev_yoy=None,
        fcf_yield_ttm=None,
    )


def test_pe_median_excludes_non_positive_earnings_aggregate_includes() -> None:
    members = [
        _member("A", market_cap=100.0, ttm_net_income=10.0),  # PE 10
        _member("B", market_cap=200.0, ttm_net_income=20.0),  # PE 10
        _member("C", market_cap=50.0, ttm_net_income=-5.0),  # loss-maker
    ]
    results = {r.stat_type: r for r in csm.compute_pe(members)}
    # Median excludes C (negative earnings) -> median of [10, 10] = 10.
    assert results["median"].value == 10.0
    assert results["median"].n_valid == 2
    assert results["median"].n_members == 3
    # Aggregate includes C's negative earnings in the summed denominator:
    # (100+200+50) / (10+20-5) = 350/25 = 14.0
    assert results["aggregate"].value == pytest.approx(14.0)
    assert results["aggregate"].n_valid == 3


def test_pe_aggregate_undefined_when_denominator_non_positive() -> None:
    members = [
        _member("A", market_cap=100.0, ttm_net_income=-10.0),
        _member("B", market_cap=200.0, ttm_net_income=-20.0),
    ]
    results = {r.stat_type: r for r in csm.compute_pe(members)}
    assert results["aggregate"].value is None
    assert (
        results["aggregate"].method_flags.get("aggregate_pe_undefined_negative_denominator") is True
    )
    # Median has no positive-earnings members either -> None, not a crash.
    assert results["median"].value is None


def test_ev_ebitda_same_negative_handling() -> None:
    members = [
        _member("A", market_cap=1, ttm_net_income=1, enterprise_value=100.0, ttm_ebitda=10.0),
        _member("B", market_cap=1, ttm_net_income=1, enterprise_value=50.0, ttm_ebitda=-5.0),
    ]
    results = {r.stat_type: r for r in csm.compute_ev_ebitda(members)}
    assert results["median"].value == 10.0  # only A qualifies
    assert results["median"].n_valid == 1
    assert results["aggregate"].value == pytest.approx(150.0 / 5.0)  # (100-5) -> 5, not undefined


# ---------------------------------------------------------------------------
# Coverage honesty (section 5.5)
# ---------------------------------------------------------------------------


def test_thin_coverage_is_written_and_flagged_not_dropped() -> None:
    members = [
        _member("A", market_cap=100.0, ttm_net_income=10.0),
        _member("B", market_cap=None, ttm_net_income=None),
        _member("C", market_cap=None, ttm_net_income=None),
        _member("D", market_cap=None, ttm_net_income=None),
    ]
    results = {r.stat_type: r for r in csm.compute_pe(members)}
    median = results["median"]
    assert median.n_members == 4
    assert median.n_valid == 1
    assert median.coverage_pct == 0.25
    assert median.value == 10.0  # still computed and stored, never dropped
    assert median.method_flags.get("coverage") == "thin"


def test_context_only_members_excluded_from_n_members_denominator() -> None:
    """context_only members (out-of-pool LLM peers, market-cap-only) can never
    resolve a metric value -- they must not count toward n_members or the
    coverage_pct denominator would be permanently capped for a reason unrelated
    to real data thinness (docs/design section 14)."""
    members = [
        _member("A", market_cap=100.0, ttm_net_income=10.0),
        _member("B", market_cap=100.0, ttm_net_income=10.0),
        _member("ROSTER_ONLY", market_cap=None, ttm_net_income=None, context_only=True),
    ]
    results = {r.stat_type: r for r in csm.compute_pe(members)}
    assert results["median"].n_members == 2  # ROSTER_ONLY excluded entirely
    assert results["median"].coverage_pct == 1.0
    assert "coverage" not in results["median"].method_flags


# ---------------------------------------------------------------------------
# Metric-class gating (section 5.4)
# ---------------------------------------------------------------------------


def test_financial_class_skips_ev_ebitda_computes_pe_and_book_multiples() -> None:
    members = [
        csm.MemberFinancials(
            ticker="BANK1",
            context_only=False,
            market_cap=100.0,
            enterprise_value=150.0,
            ev_approximated=True,
            ttm_net_income=10.0,
            ttm_ebitda=20.0,
            p_b=1.5,
            p_tbv=2.0,
            rev_yoy=None,
            fcf_yield_ttm=None,
        )
    ]
    results = csm.compute_metrics_for_set(members, MetricClass.FINANCIAL)
    metrics_present = {r.metric for r in results}
    assert "pe_ttm" in metrics_present  # never suppressed, section 5.4
    assert "ev_ebitda_ttm" not in metrics_present  # not computed at all for financial
    assert "p_b" in metrics_present
    assert "p_tbv" in metrics_present


def test_operating_class_computes_ev_ebitda_not_book_multiples() -> None:
    members = [
        csm.MemberFinancials(
            ticker="TECH1",
            context_only=False,
            market_cap=100.0,
            enterprise_value=110.0,
            ev_approximated=True,
            ttm_net_income=10.0,
            ttm_ebitda=20.0,
            p_b=None,
            p_tbv=None,
            rev_yoy=None,
            fcf_yield_ttm=None,
        )
    ]
    results = csm.compute_metrics_for_set(members, MetricClass.OPERATING)
    metrics_present = {r.metric for r in results}
    assert "ev_ebitda_ttm" in metrics_present
    assert "p_b" not in metrics_present
    assert "p_tbv" not in metrics_present


def test_method_flags_passthrough_annotates_every_row() -> None:
    members = [
        csm.MemberFinancials(
            ticker="BX",
            context_only=False,
            market_cap=100.0,
            enterprise_value=None,
            ev_approximated=False,
            ttm_net_income=10.0,
            ttm_ebitda=None,
            p_b=None,
            p_tbv=None,
            rev_yoy=None,
            fcf_yield_ttm=None,
        )
    ]
    results = csm.compute_metrics_for_set(
        members,
        MetricClass.OPERATING,
        method_flags_passthrough={"whole_co_pe_not_meaningful": True},
    )
    assert all(r.method_flags.get("whole_co_pe_not_meaningful") is True for r in results)


# ---------------------------------------------------------------------------
# Persistence — upsert idempotency
# ---------------------------------------------------------------------------


def test_persist_metrics_daily_upserts_on_rerun(tmp_path: Path) -> None:
    from alembic.config import Config

    import db as dbmod
    from alembic import command

    db_path = tmp_path / "test.db"
    dbmod.set_db_path(str(db_path))
    dbmod.init_db()
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.stamp(cfg, "0000_baseline")
    command.upgrade(cfg, "head")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        result = csm.MetricResult("pe_ttm", "median", 12.0, 5, 4, 0.8, {})
        n1 = csm.persist_metrics_daily(
            conn,
            scope_type="comparable_set",
            scope_key="NU_1",
            as_of_date=date(2026, 7, 17),
            results=[result],
            method_version=1,
        )
        assert n1 == 1
        row = conn.execute(
            "SELECT value FROM comp_set_metrics_daily WHERE scope_key = 'NU_1'"
        ).fetchone()
        assert row["value"] == 12.0

        updated = csm.MetricResult("pe_ttm", "median", 13.5, 5, 5, 1.0, {})
        csm.persist_metrics_daily(
            conn,
            scope_type="comparable_set",
            scope_key="NU_1",
            as_of_date=date(2026, 7, 17),
            results=[updated],
            method_version=1,
        )
        rows = conn.execute(
            "SELECT value, coverage_pct FROM comp_set_metrics_daily WHERE scope_key = 'NU_1'"
        ).fetchall()
        assert len(rows) == 1  # upsert, not a duplicate row
        assert rows[0]["value"] == 13.5
        assert rows[0]["coverage_pct"] == 1.0
    finally:
        conn.close()
