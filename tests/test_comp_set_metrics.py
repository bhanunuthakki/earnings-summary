"""Synthetic-fixture unit tests for compute.comp_set_metrics
(docs/design/comparable_sets_bottoms_up.md §5)."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.comp_set_metrics import (  # noqa: E402
    compute_comparable_set_metrics,
    load_member_snapshot,
    upsert_metric_rows,
)
from compute.comparable_sets import ComparableSetMember  # noqa: E402

AS_OF = date(2026, 7, 17)


def _write(tmp_path: Path, ticker: str, suffix: str, rows: list[dict[str, object]]) -> None:
    d = tmp_path / "data" / "historical" / "fmp"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ticker}_{suffix}.json").write_text(json.dumps(rows), encoding="utf-8")


def _quarterly_dates(n: int) -> list[str]:
    """n descending quarter-end ISO dates ending at 2026-03-31."""
    out: list[str] = []
    year, month = 2026, 3
    for _ in range(n):
        out.append(f"{year}-{month:02d}-{28 if month != 3 else 31}")
        month -= 3
        if month <= 0:
            month += 12
            year -= 1
    return out


def _seed_member(
    tmp_path: Path,
    ticker: str,
    *,
    market_cap: float,
    net_incomes: list[float],  # newest-first, need >= 8 for rev_yoy + ttm
    ebitdas: list[float] | None = None,
    revenues: list[float] | None = None,
    currency: str = "USD",
    equity: float | None = None,
    goodwill: float = 0.0,
    intangibles: float = 0.0,
    fcf_yields: list[float] | None = None,
    ev: float = 0.0,
) -> None:
    dates = _quarterly_dates(len(net_incomes))
    ebitdas = ebitdas or [ni * 1.3 for ni in net_incomes]
    revenues = revenues or [ni * 5 for ni in net_incomes]
    _write(
        tmp_path,
        ticker,
        "income_statement_quarterly",
        [
            {
                "date": d,
                "netIncome": ni,
                "ebitda": eb,
                "revenue": rev,
                "reportedCurrency": currency,
            }
            for d, ni, eb, rev in zip(dates, net_incomes, ebitdas, revenues)
        ],
    )
    fcf_yields = fcf_yields or [0.02] * len(net_incomes)
    _write(
        tmp_path,
        ticker,
        "key_metrics_quarterly",
        [
            {
                "date": d,
                "marketCap": market_cap,
                "enterpriseValue": ev or market_cap * 1.1,
                "freeCashFlowYield": fy,
                "reportedCurrency": currency,
            }
            for d, fy in zip(dates, fcf_yields)
        ],
    )
    _write(
        tmp_path,
        ticker,
        "historical_market_cap",
        [{"date": d, "marketCap": market_cap} for d in dates],
    )
    if equity is not None:
        _write(
            tmp_path,
            ticker,
            "balance_sheet_quarterly",
            [
                {
                    "date": d,
                    "totalStockholdersEquity": equity,
                    "goodwill": goodwill,
                    "intangibleAssets": intangibles,
                    "reportedCurrency": currency,
                }
                for d in dates
            ],
        )


# ---------------------------------------------------------------------------
# load_member_snapshot
# ---------------------------------------------------------------------------


def test_snapshot_requires_all_4_trailing_quarters(tmp_path: Path) -> None:
    _seed_member(tmp_path, "THIN", market_cap=1e9, net_incomes=[1e8, 1e8, 1e8])  # only 3
    snap = load_member_snapshot(tmp_path, "THIN", AS_OF)
    assert snap.ttm_net_income is None
    assert snap.ttm_ebitda is None


def test_snapshot_sums_4_quarters(tmp_path: Path) -> None:
    _seed_member(tmp_path, "FULL", market_cap=1e9, net_incomes=[1e8] * 8)
    snap = load_member_snapshot(tmp_path, "FULL", AS_OF)
    assert snap.ttm_net_income == 4e8


def test_snapshot_nulls_currency_denominated_fields_for_non_usd(tmp_path: Path) -> None:
    _seed_member(
        tmp_path, "FOREIGN", market_cap=1e9, net_incomes=[1e11] * 8, currency="INR", equity=5e11
    )
    snap = load_member_snapshot(tmp_path, "FOREIGN", AS_OF)
    assert snap.ttm_net_income is None
    assert snap.equity is None
    assert snap.tangible_book is None
    assert snap.non_usd_currency is True
    # rev_yoy and fcf_yield remain -- same-currency ratios, no guard.
    assert snap.rev_yoy_pct is not None
    assert snap.fcf_yield_ttm is not None


def test_snapshot_computes_rev_yoy(tmp_path: Path) -> None:
    # newest-first: [current, q-1, q-2, q-3, q-4(=1y ago), ...]
    revs = [110.0, 100.0, 100.0, 100.0, 100.0, 90.0, 90.0, 90.0]
    _seed_member(tmp_path, "GROWER", market_cap=1e9, net_incomes=[1.0] * 8, revenues=revs)
    snap = load_member_snapshot(tmp_path, "GROWER", AS_OF)
    assert snap.rev_yoy_pct is not None
    assert abs(snap.rev_yoy_pct - 10.0) < 0.01


def test_snapshot_p_tbv_subtracts_goodwill_and_intangibles(tmp_path: Path) -> None:
    _seed_member(
        tmp_path,
        "BANK",
        market_cap=1e9,
        net_incomes=[1e8] * 8,
        equity=5e8,
        goodwill=1e8,
        intangibles=5e7,
    )
    snap = load_member_snapshot(tmp_path, "BANK", AS_OF)
    assert snap.equity == 5e8
    assert snap.tangible_book == 5e8 - 1e8 - 5e7


# ---------------------------------------------------------------------------
# compute_comparable_set_metrics
# ---------------------------------------------------------------------------


def _members(tickers: list[str]) -> list[ComparableSetMember]:
    return [ComparableSetMember(t, "industry_seed", context_only=False) for t in tickers]


def test_pe_median_excludes_lossmaking_but_aggregate_includes(tmp_path: Path) -> None:
    # ttm_net_income sums the 4 newest quarters -> 4e8; PE = 1e9 / 4e8 = 2.5.
    _seed_member(tmp_path, "A", market_cap=1e9, net_incomes=[1e8] * 8)
    _seed_member(tmp_path, "B", market_cap=1e9, net_incomes=[-1e8] * 8)  # lossmaking
    rows = compute_comparable_set_metrics(tmp_path, _members(["A", "B"]), "operating", AS_OF)
    pe_median = next(r for r in rows if r.metric == "pe_ttm" and r.stat_type == "median")
    pe_agg = next(r for r in rows if r.metric == "pe_ttm" and r.stat_type == "aggregate")
    assert pe_median.n_valid == 1  # only A
    assert pe_median.value == 2.5
    assert pe_agg.n_valid == 2  # both A and B contribute to the sum
    # Sum mcap = 2e9, sum earnings = 1e8 + -1e8 = 0 -> undefined denominator
    assert pe_agg.value is None
    assert pe_agg.method_flags.get("aggregate_pe_undefined_negative_denominator") is True


def test_ev_ebitda_only_for_operating_class(tmp_path: Path) -> None:
    _seed_member(tmp_path, "OPCO", market_cap=1e9, net_incomes=[1e8] * 8)
    op_rows = compute_comparable_set_metrics(tmp_path, _members(["OPCO"]), "operating", AS_OF)
    assert any(r.metric == "ev_ebitda_ttm" for r in op_rows)

    fin_rows = compute_comparable_set_metrics(tmp_path, _members(["OPCO"]), "financial", AS_OF)
    assert not any(r.metric == "ev_ebitda_ttm" for r in fin_rows)
    assert any(r.metric == "p_b" for r in fin_rows)
    assert any(r.metric == "p_tbv" for r in fin_rows)


def test_reit_class_returns_no_rows_in_phase_1(tmp_path: Path) -> None:
    _seed_member(tmp_path, "REITCO", market_cap=1e9, net_incomes=[1e8] * 8)
    rows = compute_comparable_set_metrics(tmp_path, _members(["REITCO"]), "reit", AS_OF)
    assert rows == []


def test_context_only_members_never_contribute(tmp_path: Path) -> None:
    _seed_member(tmp_path, "REAL", market_cap=1e9, net_incomes=[1e8] * 8)
    _seed_member(tmp_path, "CTXONLY", market_cap=1e9, net_incomes=[1e8] * 8)
    members = [
        ComparableSetMember("REAL", "industry_seed", context_only=False),
        ComparableSetMember("CTXONLY", "llm_ratified", context_only=True),
    ]
    rows = compute_comparable_set_metrics(tmp_path, members, "operating", AS_OF)
    pe_agg = next(r for r in rows if r.metric == "pe_ttm" and r.stat_type == "aggregate")
    assert pe_agg.n_members == 1  # CTXONLY excluded from the roster count entirely


def test_thin_coverage_flag(tmp_path: Path) -> None:
    _seed_member(tmp_path, "OK1", market_cap=1e9, net_incomes=[1e8] * 8)
    _seed_member(tmp_path, "NODATA", market_cap=1e9, net_incomes=[1e8, 1e8])  # < 4 quarters
    rows = compute_comparable_set_metrics(tmp_path, _members(["OK1", "NODATA"]), "operating", AS_OF)
    pe_median = next(r for r in rows if r.metric == "pe_ttm" and r.stat_type == "median")
    assert pe_median.coverage_pct == 0.5
    # Not below 0.5, so no thin flag at exactly 0.5 (flag fires strictly < 0.5).
    rows2 = compute_comparable_set_metrics(
        tmp_path, _members(["OK1", "NODATA", "NODATA2"]), "operating", AS_OF
    )
    # NODATA2 doesn't even have files -> also 0 contribution.
    pe_median2 = next(r for r in rows2 if r.metric == "pe_ttm" and r.stat_type == "median")
    assert pe_median2.coverage_pct < 0.5
    assert pe_median2.method_flags.get("coverage") == "thin"


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


def _metrics_table_conn() -> sqlite3.Connection:
    """In-memory comp_set_metrics_daily matching the post-0172 schema
    (incl. the Phase 2 `locator` column)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE comp_set_metrics_daily (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "scope_type TEXT, scope_key TEXT, as_of_date TEXT, metric TEXT, stat_type TEXT, "
        "value REAL, n_members INTEGER, n_valid INTEGER, coverage_pct REAL, "
        "method_version INTEGER, method_flags TEXT, computed_at TEXT, locator TEXT, "
        "UNIQUE(scope_type, scope_key, as_of_date, metric, stat_type, method_version))"
    )
    return conn


def test_upsert_metric_rows_is_idempotent(tmp_path: Path) -> None:
    conn = _metrics_table_conn()
    _seed_member(tmp_path, "A", market_cap=1e9, net_incomes=[1e8] * 8)
    rows = compute_comparable_set_metrics(tmp_path, _members(["A"]), "operating", AS_OF)
    n1 = upsert_metric_rows(conn, "comparable_set", "A_1", AS_OF, 1, rows)
    n2 = upsert_metric_rows(conn, "comparable_set", "A_1", AS_OF, 1, rows)  # re-run, same day
    assert n1 == n2 == len(rows)
    count = conn.execute("SELECT COUNT(*) FROM comp_set_metrics_daily").fetchone()[0]
    assert count == len(rows)  # upserted, not duplicated


# ---------------------------------------------------------------------------
# provenance (Phase 2 — per-#911 locator rule)
# ---------------------------------------------------------------------------


def test_computed_rows_carry_derived_locators(tmp_path: Path) -> None:
    _seed_member(tmp_path, "A", market_cap=1e9, net_incomes=[1e8] * 8)
    rows = compute_comparable_set_metrics(tmp_path, _members(["A"]), "operating", AS_OF)
    assert rows  # sanity
    for row in rows:
        assert row.locator is not None, f"{row.metric}:{row.stat_type} missing locator"
        raw = row.locator.to_json()
        assert raw is not None
        payload = json.loads(raw)
        assert payload.get("kind") == "derived"
        display = payload.get("derived", {}).get("display", "")
        assert "bottoms-up" in display


def test_upsert_persists_locator_json(tmp_path: Path) -> None:
    conn = _metrics_table_conn()
    _seed_member(tmp_path, "A", market_cap=1e9, net_incomes=[1e8] * 8)
    rows = compute_comparable_set_metrics(tmp_path, _members(["A"]), "operating", AS_OF)
    upsert_metric_rows(conn, "comparable_set", "A_1", AS_OF, 1, rows)
    stored = conn.execute(
        "SELECT locator FROM comp_set_metrics_daily WHERE metric = 'pe_ttm' "
        "AND stat_type = 'median'"
    ).fetchone()
    assert stored["locator"] is not None
    assert json.loads(stored["locator"])["kind"] == "derived"
