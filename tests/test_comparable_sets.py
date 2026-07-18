"""Unit tests for compute.comparable_sets -- the rule-ladder resolver and the
freeze/versioning semantics (docs/design/comparable_sets_bottoms_up.md sections
3, 3.3). Uses the real alembic chain (like the migration tests) so the resolver
runs against the actual tracked_companies/comparable_sets table shapes, with
synthetic FMP profile caches written to a tmp repo_root.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import compute.comparable_sets as cs  # noqa: E402
from compute.comparable_set_overrides import ComparableSetOverride  # noqa: E402


def _build_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture(scope="module")
def head_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db = tmp_path_factory.mktemp("comparable_sets_unit_tmpl") / "at_head.db"
    import db as dbmod

    dbmod.set_db_path(str(db))
    dbmod.init_db()
    cfg = _build_config(db)
    command.stamp(cfg, "0000_baseline")
    command.upgrade(cfg, "head")
    return db


@pytest.fixture
def conn(head_template: Path, tmp_path: Path) -> Iterator[sqlite3.Connection]:
    import shutil

    db_path = tmp_path / "test.db"
    shutil.copy(head_template, db_path)
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _insert_tracked(
    conn: sqlite3.Connection,
    ticker: str,
    list_type: str,
    *,
    instrument_type: str | None = "equity",
    user_id: str = "bhanu",
) -> None:
    conn.execute(
        "INSERT INTO tracked_companies (user_id, ticker, name, list_type, instrument_type) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, ticker, ticker, list_type, instrument_type),
    )
    conn.commit()


def _write_profile(
    repo_root: Path,
    ticker: str,
    *,
    sector: str,
    industry: str,
    market_cap: float,
    exchange: str = "NASDAQ",
    is_actively_trading: bool = True,
    is_etf: bool = False,
) -> None:
    fmp_dir = repo_root / "data" / "historical" / "fmp"
    fmp_dir.mkdir(parents=True, exist_ok=True)
    rec = {
        "companyName": f"{ticker} Inc",
        "sector": sector,
        "industry": industry,
        "marketCap": market_cap,
        "exchangeShortName": exchange,
        "isActivelyTrading": is_actively_trading,
        "isEtf": is_etf,
        "isFund": False,
    }
    (fmp_dir / f"{ticker}_profile.json").write_text(json.dumps([rec]), encoding="utf-8")


def _write_peer_cache(
    repo_root: Path,
    ticker: str,
    *,
    suggestions: list[dict[str, str]],
    fetched_complete: list[str],
    fetched_peers: list[str],
) -> None:
    out_dir = repo_root / "data" / "peer_selection"
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "ticker": ticker,
        "suggestions": suggestions,
        "fetched_complete": fetched_complete,
        "fetched_peers": fetched_peers,
    }
    (out_dir / f"{ticker}.json").write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# metric_class_for
# ---------------------------------------------------------------------------


def test_metric_class_for_financial_keywords() -> None:
    assert cs.metric_class_for("Financial Services", "Credit Services") == cs.MetricClass.FINANCIAL
    assert cs.metric_class_for("Financial Services", "Banks - Regional") == cs.MetricClass.FINANCIAL
    assert cs.metric_class_for(None, "Insurance - Diversified") == cs.MetricClass.FINANCIAL


def test_metric_class_for_reit_keywords() -> None:
    assert cs.metric_class_for("Real Estate", "REIT - Industrial") == cs.MetricClass.REIT


def test_metric_class_for_default_operating() -> None:
    assert cs.metric_class_for("Technology", "Software - Application") == cs.MetricClass.OPERATING
    assert cs.metric_class_for(None, None) == cs.MetricClass.OPERATING


# ---------------------------------------------------------------------------
# Rule ladder — Step A / B
# ---------------------------------------------------------------------------


def test_step_a_industry_seed_finds_enough_members(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """8 same-industry, same-size-band members -> Step A alone suffices, no widen."""
    _insert_tracked(conn, "SUBJ", "portfolio")
    _write_profile(
        tmp_path, "SUBJ", sector="Technology", industry="Software - Application", market_cap=10e9
    )
    for i in range(8):
        t = f"IND{i}"
        _insert_tracked(conn, t, "index_member")
        _write_profile(
            tmp_path, t, sector="Technology", industry="Software - Application", market_cap=10e9
        )
    # A sector-only match that must NOT show up via Step A (wrong industry).
    _insert_tracked(conn, "SECTORONLY", "index_member")
    _write_profile(
        tmp_path, "SECTORONLY", sector="Technology", industry="Semiconductors", market_cap=10e9
    )

    pool = cs.load_pool(conn, tmp_path)
    resolved = cs.resolve_comparable_set("SUBJ", pool, tmp_path)

    member_tickers = {m.ticker for m in resolved.members}
    assert member_tickers == {f"IND{i}" for i in range(8)}
    assert all(m.reason == cs.MembershipReason.INDUSTRY_SEED for m in resolved.members)
    assert resolved.source_summary["step_a_n"] == 8
    assert resolved.source_summary["step_b_n"] == 0


def test_step_b_widens_when_step_a_under_8(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """Only 3 same-industry peers -> Step B fires and adds same-sector members."""
    _insert_tracked(conn, "SUBJ", "portfolio")
    _write_profile(
        tmp_path, "SUBJ", sector="Technology", industry="Software - Application", market_cap=10e9
    )
    for i in range(3):
        t = f"IND{i}"
        _insert_tracked(conn, t, "index_member")
        _write_profile(
            tmp_path, t, sector="Technology", industry="Software - Application", market_cap=10e9
        )
    for i in range(5):
        t = f"SEC{i}"
        _insert_tracked(conn, t, "index_member")
        _write_profile(tmp_path, t, sector="Technology", industry="Semiconductors", market_cap=10e9)

    pool = cs.load_pool(conn, tmp_path)
    resolved = cs.resolve_comparable_set("SUBJ", pool, tmp_path)

    member_tickers = {m.ticker for m in resolved.members}
    assert member_tickers == {f"IND{i}" for i in range(3)} | {f"SEC{i}" for i in range(5)}
    reasons = {m.ticker: m.reason for m in resolved.members}
    assert all(reasons[f"IND{i}"] == cs.MembershipReason.INDUSTRY_SEED for i in range(3))
    assert all(reasons[f"SEC{i}"] == cs.MembershipReason.SECTOR_WIDENED for i in range(5))
    assert resolved.source_summary["step_b_n"] == 5


def test_size_band_and_exchange_guards_exclude_candidates(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _insert_tracked(conn, "SUBJ", "portfolio")
    _write_profile(
        tmp_path, "SUBJ", sector="Technology", industry="Software - Application", market_cap=10e9
    )
    # Too small for even Step B's wider 10x sector band (10e9/10 = 1e9 floor) --
    # Step B necessarily fires here since Step A alone under-fills (< 8).
    _insert_tracked(conn, "TOOSMALL", "index_member")
    _write_profile(
        tmp_path,
        "TOOSMALL",
        sector="Technology",
        industry="Software - Application",
        market_cap=0.5e9,
    )
    # Right size, right industry, but foreign-exchange listed.
    _insert_tracked(conn, "FOREIGN", "index_member")
    _write_profile(
        tmp_path,
        "FOREIGN",
        sector="Technology",
        industry="Software - Application",
        market_cap=10e9,
        exchange="LSE",
    )
    # Right size/industry but delisted.
    _insert_tracked(conn, "GHOST", "index_member")
    _write_profile(
        tmp_path,
        "GHOST",
        sector="Technology",
        industry="Software - Application",
        market_cap=10e9,
        is_actively_trading=False,
    )
    # A real, valid candidate.
    _insert_tracked(conn, "GOOD", "index_member")
    _write_profile(
        tmp_path, "GOOD", sector="Technology", industry="Software - Application", market_cap=10e9
    )

    pool = cs.load_pool(conn, tmp_path)
    resolved = cs.resolve_comparable_set("SUBJ", pool, tmp_path)
    member_tickers = {m.ticker for m in resolved.members}
    assert member_tickers == {"GOOD"}


def test_etf_excluded_from_pool_belt_and_suspenders(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A list_type='index_member' row whose profile isEtf=True must not enter the
    pool even though list_type itself isn't 'etf' (doc section 2)."""
    _insert_tracked(conn, "SUBJ", "portfolio")
    _write_profile(
        tmp_path, "SUBJ", sector="Technology", industry="Software - Application", market_cap=10e9
    )
    _insert_tracked(conn, "SNEAKYETF", "index_member")
    _write_profile(
        tmp_path,
        "SNEAKYETF",
        sector="Technology",
        industry="Software - Application",
        market_cap=10e9,
        is_etf=True,
    )
    pool = cs.load_pool(conn, tmp_path)
    assert "SNEAKYETF" not in {m.ticker for m in pool}


# ---------------------------------------------------------------------------
# Rule ladder — Step C
# ---------------------------------------------------------------------------


def test_step_c_unions_llm_peers_full_and_context_only(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _insert_tracked(conn, "SUBJ", "portfolio")
    _write_profile(
        tmp_path, "SUBJ", sector="Financial Services", industry="Credit Services", market_cap=60e9
    )
    # In-pool, fully fetched LLM peer.
    _insert_tracked(conn, "INPOOL", "index_member")
    _write_profile(
        tmp_path,
        "INPOOL",
        sector="Financial Services",
        industry="Consumer Finance",
        market_cap=20e9,
    )

    _write_peer_cache(
        tmp_path,
        "SUBJ",
        suggestions=[
            {"ticker": "INPOOL", "name": "In Pool Co", "why": "peer"},
            {"ticker": "OUTOFPOOL", "name": "Out Co", "why": "peer, market-cap only"},
            {"ticker": "NODATA", "name": "No Data Co", "why": "suggested but nothing resolved"},
        ],
        fetched_complete=["INPOOL"],
        fetched_peers=["INPOOL", "OUTOFPOOL"],
    )

    pool = cs.load_pool(conn, tmp_path)
    resolved = cs.resolve_comparable_set("SUBJ", pool, tmp_path)
    by_ticker = {m.ticker: m for m in resolved.members}

    assert "NODATA" not in by_ticker  # nothing resolved for it -- not rosterable
    assert by_ticker["OUTOFPOOL"].reason == cs.MembershipReason.LLM_RATIFIED
    assert by_ticker["OUTOFPOOL"].context_only is True
    # INPOOL already entered via Step A/B (Consumer Finance != Credit Services, so
    # it wasn't a Step-A hit; but it's in the pool and fetched_complete -- Step C
    # should still add it as a full (non-context) member.
    assert by_ticker["INPOOL"].context_only is False
    assert resolved.source_summary["step_c_n"] == 3


# ---------------------------------------------------------------------------
# Rule ladder — Step D (pinned override)
# ---------------------------------------------------------------------------


def test_step_d_force_include_and_force_exclude_win(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert_tracked(conn, "SUBJ", "portfolio")
    _write_profile(
        tmp_path, "SUBJ", sector="Technology", industry="Software - Application", market_cap=10e9
    )
    for i in range(8):
        t = f"IND{i}"
        _insert_tracked(conn, t, "index_member")
        _write_profile(
            tmp_path, t, sector="Technology", industry="Software - Application", market_cap=10e9
        )

    override = ComparableSetOverride(
        force_include=["PINNED"],
        force_exclude=["IND0"],
        method_flags={"whole_co_pe_not_meaningful": True},
    )

    def _fake_get_override(ticker: str) -> ComparableSetOverride | None:
        return override if ticker == "SUBJ" else None

    monkeypatch.setattr(cs, "get_override", _fake_get_override)

    pool = cs.load_pool(conn, tmp_path)
    resolved = cs.resolve_comparable_set("SUBJ", pool, tmp_path)
    member_tickers = {m.ticker for m in resolved.members}

    assert "IND0" not in member_tickers  # force_exclude always wins
    assert "PINNED" in member_tickers
    pinned = next(m for m in resolved.members if m.ticker == "PINNED")
    assert pinned.reason == cs.MembershipReason.PINNED_OVERRIDE
    assert resolved.source_summary["override_applied"] is True
    assert resolved.method_flags == {"whole_co_pe_not_meaningful": True}


def test_subject_not_in_pool_raises(conn: sqlite3.Connection, tmp_path: Path) -> None:
    pool = cs.load_pool(conn, tmp_path)
    with pytest.raises(ValueError, match="not in the comparable-set candidate pool"):
        cs.resolve_comparable_set("GHOST", pool, tmp_path)


# ---------------------------------------------------------------------------
# Freeze / versioning
# ---------------------------------------------------------------------------


def _minimal_resolved(
    ticker: str, members: list[tuple[str, cs.MembershipReason, bool]]
) -> cs.ResolvedSet:
    return cs.ResolvedSet(
        ticker=ticker,
        metric_class=cs.MetricClass.OPERATING,
        members=[cs.MemberResolution(t, r, c) for t, r, c in members],
        source_summary={
            "step_a_n": len(members),
            "step_b_n": 0,
            "step_c_n": 0,
            "override_applied": False,
        },
    )


def test_freeze_first_run_inserts_set_and_members(conn: sqlite3.Connection) -> None:
    resolved = _minimal_resolved("NU", [("SOFI", cs.MembershipReason.LLM_RATIFIED, False)])
    outcome = cs.freeze_comparable_set(conn, resolved)
    assert outcome.changed is True
    assert outcome.comparable_set_id == "NU_1"
    row = conn.execute(
        "SELECT ticker, metric_class FROM comparable_sets WHERE comparable_set_id = ?", ("NU_1",)
    ).fetchone()
    assert row["ticker"] == "NU"
    members = cs.active_members(conn, "NU_1")
    assert members == [("SOFI", cs.MembershipReason.LLM_RATIFIED, False)]


def test_freeze_rerun_with_unchanged_membership_is_noop(conn: sqlite3.Connection) -> None:
    resolved = _minimal_resolved("NU", [("SOFI", cs.MembershipReason.LLM_RATIFIED, False)])
    cs.freeze_comparable_set(conn, resolved)
    outcome = cs.freeze_comparable_set(conn, resolved)
    assert outcome.changed is False
    assert outcome.members_added == []
    assert outcome.members_removed == []


def test_freeze_refresh_forces_rewrite_even_if_unchanged(conn: sqlite3.Connection) -> None:
    resolved = _minimal_resolved("NU", [("SOFI", cs.MembershipReason.LLM_RATIFIED, False)])
    cs.freeze_comparable_set(conn, resolved)
    outcome = cs.freeze_comparable_set(conn, resolved, refresh=True)
    assert outcome.changed is True


def test_freeze_membership_change_closes_and_opens_rows(conn: sqlite3.Connection) -> None:
    from datetime import date

    first = _minimal_resolved("NU", [("SOFI", cs.MembershipReason.LLM_RATIFIED, False)])
    cs.freeze_comparable_set(conn, first, as_of=date(2026, 1, 1))

    second = _minimal_resolved(
        "NU",
        [
            ("MELI", cs.MembershipReason.INDUSTRY_SEED, False),
        ],
    )
    outcome = cs.freeze_comparable_set(conn, second, as_of=date(2026, 2, 1))
    assert outcome.changed is True
    assert outcome.members_added == ["MELI"]
    assert outcome.members_removed == ["SOFI"]

    # SOFI's row should be closed at 2026-02-01, not deleted.
    closed = conn.execute(
        "SELECT valid_to FROM comparable_set_members "
        "WHERE comparable_set_id = 'NU_1' AND member_ticker = 'SOFI'"
    ).fetchone()
    assert closed["valid_to"] == "2026-02-01"

    active = cs.active_members(conn, "NU_1")
    assert active == [("MELI", cs.MembershipReason.INDUSTRY_SEED, False)]


def test_comparable_set_id_is_deterministic() -> None:
    assert cs.comparable_set_id_for("nu") == "NU_1"
    assert cs.comparable_set_id_for("NU", method_version=2) == "NU_2"


def test_get_method_flags_roundtrip(conn: sqlite3.Connection) -> None:
    resolved = cs.ResolvedSet(
        ticker="BN",
        metric_class=cs.MetricClass.OPERATING,
        members=[cs.MemberResolution("BX", cs.MembershipReason.PINNED_OVERRIDE, False)],
        source_summary={},
        method_flags={"whole_co_pe_not_meaningful": True},
    )
    outcome = cs.freeze_comparable_set(conn, resolved)
    flags = cs.get_method_flags(conn, outcome.comparable_set_id)
    assert flags == {"whole_co_pe_not_meaningful": True}


def test_get_method_flags_empty_when_none(conn: sqlite3.Connection) -> None:
    assert cs.get_method_flags(conn, "DOES_NOT_EXIST_1") == {}
