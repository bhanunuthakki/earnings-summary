"""Synthetic-fixture unit tests for compute.comparable_sets
(docs/design/comparable_sets_bottoms_up.md §2-3).

Covers: pool construction (ETF exclusion), the rule ladder (Step A/B size
bands, Step C peer_selection union with the fetched_complete-vs-pool fix,
Step D override precedence incl. BN), metric_class classification, and
freeze idempotency (insert / no-op / update / same-day churn).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from compute.comparable_set_overrides import ComparableSetOverride  # noqa: E402
from compute.comparable_sets import (  # noqa: E402
    ComparableSetMember,
    ComparableSetResolution,
    freeze_comparable_set,
    load_pool,
    metric_class,
    resolve_comparable_set,
)


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE tracked_companies (id INTEGER PRIMARY KEY, user_id TEXT, ticker TEXT, "
        "list_type TEXT, instrument_type TEXT, archived_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE comparable_sets (comparable_set_id TEXT PRIMARY KEY, ticker TEXT, "
        "method_version INTEGER, resolved_at TEXT, metric_class TEXT, method_flags TEXT, "
        "source_summary TEXT)"
    )
    conn.execute(
        "CREATE TABLE comparable_set_members (comparable_set_id TEXT, member_ticker TEXT, "
        "membership_reason TEXT, context_only INTEGER, valid_from TEXT, valid_to TEXT, "
        "PRIMARY KEY (comparable_set_id, member_ticker, valid_from))"
    )
    return conn


def _add_company(
    conn: sqlite3.Connection, ticker: str, list_type: str, instrument_type: str = "equity"
) -> None:
    conn.execute(
        "INSERT INTO tracked_companies (user_id, ticker, list_type, instrument_type) "
        "VALUES ('bhanu', ?, ?, ?)",
        (ticker, list_type, instrument_type),
    )


def _write_profile(
    repo_root: Path,
    ticker: str,
    *,
    sector: str,
    industry: str,
    market_cap: float,
    exchange: str = "NASDAQ",
    active: bool = True,
    is_etf: bool = False,
) -> None:
    d = repo_root / "data" / "historical" / "fmp"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ticker}_profile.json").write_text(
        json.dumps(
            [
                {
                    "companyName": ticker,
                    "sector": sector,
                    "industry": industry,
                    "marketCap": market_cap,
                    "exchange": exchange,
                    "isActivelyTrading": active,
                    "isEtf": is_etf,
                    "isFund": False,
                }
            ]
        ),
        encoding="utf-8",
    )


def _write_peer_cache(repo_root: Path, ticker: str, suggestions: list[dict[str, str]]) -> None:
    d = repo_root / "data" / "peer_selection"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ticker}.json").write_text(
        json.dumps({"ticker": ticker, "suggestions": suggestions, "inputs_sha256": "x"}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# metric_class
# ---------------------------------------------------------------------------


def test_metric_class_financial_keywords() -> None:
    assert metric_class("Financial Services", "Banks - Regional") == "financial"
    assert metric_class("Financial Services", "Credit Services") == "financial"
    assert metric_class(None, "Asset Management") == "financial"


def test_metric_class_reit() -> None:
    assert metric_class("Real Estate", "REIT - Office") == "reit"


def test_metric_class_operating_default() -> None:
    assert metric_class("Technology", "Software - Application") == "operating"


# ---------------------------------------------------------------------------
# pool construction
# ---------------------------------------------------------------------------


def test_load_pool_excludes_etf_by_list_type_and_by_profile_flag(tmp_path: Path) -> None:
    conn = _make_conn()
    _add_company(conn, "SPY", "index_member", instrument_type="etf")  # excluded: instrument_type
    _add_company(
        conn, "FAKEETF", "index_member", instrument_type="equity"
    )  # excluded: profile flag
    _add_company(conn, "NU", "portfolio")
    _write_profile(
        tmp_path,
        "FAKEETF",
        sector="Financial Services",
        industry="Asset Management",
        market_cap=1e9,
        is_etf=True,
    )
    _write_profile(
        tmp_path, "NU", sector="Financial Services", industry="Banks - Regional", market_cap=6e10
    )
    pool = load_pool(conn, tmp_path, user_id="bhanu")
    assert "SPY" not in pool
    assert "FAKEETF" not in pool
    assert "NU" in pool


def test_load_pool_skips_archived(tmp_path: Path) -> None:
    conn = _make_conn()
    _add_company(conn, "OLD", "watchlist")
    conn.execute("UPDATE tracked_companies SET archived_at = '2026-01-01' WHERE ticker = 'OLD'")
    _write_profile(
        tmp_path, "OLD", sector="Technology", industry="Software - Application", market_cap=1e9
    )
    pool = load_pool(conn, tmp_path, user_id="bhanu")
    assert "OLD" not in pool


# ---------------------------------------------------------------------------
# rule ladder
# ---------------------------------------------------------------------------


def test_step_a_industry_and_size_band(tmp_path: Path) -> None:
    """8 in-industry, in-band peers meet MIN_COMPARABLE_SET_SIZE, so Step B
    never fires -- isolates Step A's industry + size-band filter."""
    conn = _make_conn()
    _add_company(conn, "SUBJ", "portfolio")
    _write_profile(
        tmp_path, "SUBJ", sector="Technology", industry="Software - Application", market_cap=1e10
    )
    for i in range(8):
        t = f"INBAND{i}"
        _add_company(conn, t, "index_member")
        _write_profile(
            tmp_path, t, sector="Technology", industry="Software - Application", market_cap=2e10
        )
    _add_company(conn, "TOOSMALL", "index_member")
    _write_profile(
        tmp_path, "TOOSMALL", sector="Technology", industry="Software - Application", market_cap=1e8
    )
    _add_company(conn, "WRONGIND", "index_member")
    _write_profile(
        tmp_path, "WRONGIND", sector="Technology", industry="Semiconductors", market_cap=1e10
    )

    resolution = resolve_comparable_set("SUBJ", tmp_path, conn)
    members = {m.member_ticker for m in resolution.members}
    assert "INBAND0" in members
    assert "TOOSMALL" not in members
    assert "WRONGIND" not in members
    assert resolution.source_summary["step_a_n"] == 8
    assert resolution.source_summary["step_b_n"] == 0


def test_step_b_widens_only_when_under_filled(tmp_path: Path) -> None:
    conn = _make_conn()
    _add_company(conn, "SUBJ", "portfolio")
    _add_company(conn, "SECTORPEER", "index_member")
    _write_profile(tmp_path, "SUBJ", sector="Healthcare", industry="Biotechnology", market_cap=1e10)
    # No industry match at all -- Step A should be empty, Step B should fire.
    _write_profile(
        tmp_path, "SECTORPEER", sector="Healthcare", industry="Drug Manufacturers", market_cap=1e10
    )

    resolution = resolve_comparable_set("SUBJ", tmp_path, conn)
    assert resolution.source_summary["step_a_n"] == 0
    assert resolution.source_summary["step_b_n"] == 1
    members = {m.member_ticker for m in resolution.members}
    assert "SECTORPEER" in members


def test_step_b_does_not_fire_when_step_a_meets_minimum(tmp_path: Path) -> None:
    conn = _make_conn()
    _add_company(conn, "SUBJ", "portfolio")
    for i in range(8):
        t = f"PEER{i}"
        _add_company(conn, t, "index_member")
        _write_profile(
            tmp_path, t, sector="Technology", industry="Software - Application", market_cap=1e10
        )
    _write_profile(
        tmp_path, "SUBJ", sector="Technology", industry="Software - Application", market_cap=1e10
    )

    resolution = resolve_comparable_set("SUBJ", tmp_path, conn)
    assert resolution.source_summary["step_a_n"] == 8
    assert resolution.source_summary["step_b_n"] == 0


def test_step_c_full_member_requires_pool_membership(tmp_path: Path) -> None:
    conn = _make_conn()
    _add_company(conn, "SUBJ", "portfolio")
    _add_company(conn, "INPOOLPEER", "index_member")
    _write_profile(
        tmp_path, "SUBJ", sector="Technology", industry="Software - Application", market_cap=1e10
    )
    _write_profile(
        tmp_path,
        "INPOOLPEER",
        sector="Consumer Cyclical",
        industry="Internet Retail",
        market_cap=5e10,
    )
    _write_peer_cache(
        tmp_path,
        "SUBJ",
        [
            {"ticker": "INPOOLPEER", "name": "In Pool", "why": "x"},
            {"ticker": "OUTOFPOOL", "name": "Not In Pool", "why": "x"},
        ],
    )
    resolution = resolve_comparable_set("SUBJ", tmp_path, conn)
    by_ticker = {m.member_ticker: m for m in resolution.members}
    assert by_ticker["INPOOLPEER"].context_only is False
    assert by_ticker["INPOOLPEER"].membership_reason == "llm_ratified"
    assert by_ticker["OUTOFPOOL"].context_only is True


def test_step_d_override_force_include_and_exclude(tmp_path: Path) -> None:
    conn = _make_conn()
    _add_company(conn, "BN", "portfolio")
    _write_profile(
        tmp_path, "BN", sector="Financial Services", industry="Asset Management", market_cap=9e10
    )
    resolution = resolve_comparable_set("BN", tmp_path, conn)
    by_ticker = {m.member_ticker: m for m in resolution.members}
    for forced in ("BAM", "BX", "KKR", "APO"):
        assert forced in by_ticker
        assert by_ticker[forced].membership_reason == "pinned_override"
    assert resolution.method_flags["whole_co_pe_not_meaningful"] is True
    assert resolution.method_flags["whole_co_ev_ebitda_not_meaningful"] is True
    assert resolution.metric_class == "financial"
    assert resolution.method_flags["primary_metric"] == "p_tbv"
    assert resolution.source_summary["override_applied"] is True


def test_step_d_force_exclude_vetoes_llm_ratified_peer(tmp_path: Path) -> None:
    conn = _make_conn()
    _add_company(conn, "ZZZ", "portfolio")
    _add_company(conn, "VETOED", "index_member")
    _write_profile(
        tmp_path, "ZZZ", sector="Technology", industry="Software - Application", market_cap=1e10
    )
    _write_profile(
        tmp_path, "VETOED", sector="Technology", industry="Software - Application", market_cap=1e10
    )
    _write_peer_cache(tmp_path, "ZZZ", [{"ticker": "VETOED", "name": "V", "why": "x"}])

    from compute import comparable_set_overrides as ov

    ov.COMPARABLE_SET_OVERRIDES["ZZZ"] = ComparableSetOverride(force_exclude=("VETOED",))
    try:
        resolution = resolve_comparable_set("ZZZ", tmp_path, conn)
    finally:
        del ov.COMPARABLE_SET_OVERRIDES["ZZZ"]
    members = {m.member_ticker for m in resolution.members}
    assert "VETOED" not in members


def test_resolve_unknown_ticker_returns_skipped_reason(tmp_path: Path) -> None:
    conn = _make_conn()
    resolution = resolve_comparable_set("NOPE", tmp_path, conn)
    assert resolution.skipped_reason is not None
    assert resolution.members == []


# ---------------------------------------------------------------------------
# freeze idempotency
# ---------------------------------------------------------------------------


def _resolution(ticker: str, members: list[tuple[str, str, bool]]) -> ComparableSetResolution:
    return ComparableSetResolution(
        ticker=ticker,
        method_version=1,
        metric_class="operating",
        members=[ComparableSetMember(t, r, c) for t, r, c in members],
        method_flags={},
        source_summary={},
    )


def test_freeze_first_insert_then_noop_then_update() -> None:
    conn = _make_conn()
    r1 = _resolution("NU", [("SOFI", "industry_seed", False), ("MELI", "llm_ratified", False)])
    out1 = freeze_comparable_set(conn, r1, as_of=date(2026, 1, 1))
    assert out1.action == "inserted"
    assert out1.n_members == 2

    # Re-resolve with identical membership on a LATER day -- no-op.
    out2 = freeze_comparable_set(conn, r1, as_of=date(2026, 1, 2))
    assert out2.action == "unchanged"

    # Membership actually changes on a later day -- update.
    r2 = _resolution("NU", [("SOFI", "industry_seed", False), ("STNE", "llm_ratified", False)])
    out3 = freeze_comparable_set(conn, r2, as_of=date(2026, 1, 3))
    assert out3.action == "updated"
    assert out3.opened == ["STNE"]
    assert out3.closed == ["MELI"]

    open_rows = conn.execute(
        "SELECT member_ticker FROM comparable_set_members WHERE comparable_set_id = 'NU_1' "
        "AND valid_to IS NULL ORDER BY member_ticker"
    ).fetchall()
    assert {r[0] for r in open_rows} == {"SOFI", "STNE"}


def test_freeze_same_day_update_does_not_violate_unique_constraint() -> None:
    """Two freezes on the SAME as_of date with a changed reason/context_only
    for an already-open member must update in place, not close+reopen
    (which would collide on the (set_id, member, valid_from) PK)."""
    conn = _make_conn()
    today = date(2026, 7, 17)
    r1 = _resolution("NU", [("SOFI", "llm_ratified", True)])
    freeze_comparable_set(conn, r1, as_of=today)

    r2 = _resolution("NU", [("SOFI", "llm_ratified", False)])
    out2 = freeze_comparable_set(conn, r2, refresh=True, as_of=today)
    assert out2.action == "updated"

    rows = conn.execute(
        "SELECT context_only, valid_from FROM comparable_set_members WHERE member_ticker = 'SOFI'"
    ).fetchall()
    assert len(rows) == 1  # updated in place, not a second row
    assert rows[0][0] == 0


def test_freeze_refresh_forces_rewrite_even_when_unchanged() -> None:
    conn = _make_conn()
    r1 = _resolution("NU", [("SOFI", "industry_seed", False)])
    freeze_comparable_set(conn, r1, as_of=date(2026, 1, 1))
    out2 = freeze_comparable_set(conn, r1, refresh=True, as_of=date(2026, 1, 2))
    assert out2.action != "unchanged"
