"""P4.2 scored comparable selection (`p3_data.load_peer_comp`).

The owner flagged the old behavior — first ``max_peers`` of FMP's
alphabetical peer screen — as wrong (NU led with Barclays, NOW with Applied
Materials). The pool usually *contains* the right names; these tests pin the
scoring that surfaces them: named rivals from the thesis watchlist first,
then industry/sector + scale affinity, and zero-affinity candidates dropped
entirely (an empty result hides the panel — hide-don't-stub).
"""

from __future__ import annotations

import json
from pathlib import Path

from report.sections.p3_data import load_peer_comp


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _profile(
    name: str, sector: str | None, industry: str | None, mkt_cap: float | None
) -> list[dict[str, object]]:
    rec: dict[str, object] = {"companyName": name}
    if sector is not None:
        rec["sector"] = sector
    if industry is not None:
        rec["industry"] = industry
    if mkt_cap is not None:
        rec["mktCap"] = mkt_cap
    return [rec]


def _seed_repo(tmp_path: Path) -> Path:
    """A repo whose FMP cache mirrors the NU shape: the peer pool mixes one
    true rival (accent-bearing name, matched via the thesis watchlist),
    same-industry banks of various scale, and an off-industry candidate."""
    fmp = tmp_path / "data" / "historical" / "fmp"
    _write_json(
        fmp / "NU_peers.json",
        [
            {"symbol": "AAAA", "companyName": "Aaaa Industrial"},
            {"symbol": "BIGB", "companyName": "Big Global Bank"},
            {"symbol": "ITUB", "companyName": "Itaú Unibanco Holding S.A."},
            {"symbol": "SMLB", "companyName": "Small Bank Co"},
        ],
    )
    _write_json(
        fmp / "NU_profile.json",
        _profile("Nu Holdings", "Financial Services", "Banks - Diversified", 70e9),
    )
    # AAAA: wrong industry + wrong sector + wrong scale -> zero affinity, dropped.
    _write_json(
        fmp / "AAAA_profile.json",
        _profile("Aaaa Industrial", "Industrials", "Conglomerates", 900e9),
    )
    # BIGB: same industry but 10x the cap -> industry-only score.
    _write_json(
        fmp / "BIGB_profile.json",
        _profile("Big Global Bank", "Financial Services", "Banks - Diversified", 700e9),
    )
    # ITUB: named rival (watchlist) + same industry + similar scale.
    _write_json(
        fmp / "ITUB_profile.json",
        _profile("Itaú Unibanco Holding S.A.", "Financial Services", "Banks - Diversified", 60e9),
    )
    _write_json(
        fmp / "ITUB_key_metrics_ttm.json",
        [{"revenueTTM": 40e9, "netIncomePerRevenueTTM": 0.25, "roicTTM": 0.12}],
    )
    # SMLB: same industry + similar scale.
    _write_json(
        fmp / "SMLB_profile.json",
        _profile("Small Bank Co", "Financial Services", "Banks - Diversified", 30e9),
    )
    _write_json(
        tmp_path / "micro_thesis" / "holdings" / "NU.json",
        {"competitive_watchlist": ["Itau Unibanco", "Bradesco"]},
    )
    return tmp_path


def test_named_rival_outranks_alphabetical_head(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    rows = load_peer_comp("NU", repo_root=repo)
    tickers = [r.peer_ticker for r in rows]
    # ITUB (watchlist rival, accent-insensitive name match) ranks first even
    # though it sits third in FMP's alphabetical pool.
    assert tickers[0] == "ITUB"
    assert "named rival" in rows[0].match_reasons
    assert "same industry" in rows[0].match_reasons
    assert "similar scale" in rows[0].match_reasons
    # TTM metrics still ride along for the selected peer.
    assert rows[0].revenue_ttm_usd == 40e9


def test_zero_affinity_candidate_dropped(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    rows = load_peer_comp("NU", repo_root=repo)
    assert "AAAA" not in {r.peer_ticker for r in rows}


def test_scale_proximity_breaks_industry_ties(tmp_path: Path) -> None:
    repo = _seed_repo(tmp_path)
    rows = load_peer_comp("NU", repo_root=repo)
    tickers = [r.peer_ticker for r in rows]
    # SMLB (industry + scale = 3) outranks BIGB (industry only = 2).
    assert tickers.index("SMLB") < tickers.index("BIGB")
    smlb = next(r for r in rows if r.peer_ticker == "SMLB")
    assert "similar scale" in smlb.match_reasons


def test_no_peers_file_returns_empty(tmp_path: Path) -> None:
    assert load_peer_comp("NU", repo_root=tmp_path) == []


def test_peers_without_profiles_score_zero_and_hide(tmp_path: Path) -> None:
    """A pool with no profile data has nothing to score on -> empty (the
    renderer then hides the panel instead of showing an unexplained list)."""
    fmp = tmp_path / "data" / "historical" / "fmp"
    _write_json(fmp / "XX_peers.json", ["YY", "ZZ"])
    assert load_peer_comp("XX", repo_root=tmp_path) == []


def test_payload_identity_fallback_when_profile_missing(tmp_path: Path) -> None:
    """A rival with NO cached profile still scores via the name + market cap
    the peers payload itself carries — the real-data ITUB case (foreign
    rivals sit outside the cached-profile universe)."""
    fmp = tmp_path / "data" / "historical" / "fmp"
    _write_json(
        fmp / "NU_peers.json",
        [{"symbol": "ITUB", "companyName": "Itaú Unibanco Holding S.A.", "mktCap": 60e9}],
    )
    _write_json(
        fmp / "NU_profile.json",
        _profile("Nu Holdings", "Financial Services", "Banks - Diversified", 70e9),
    )
    _write_json(
        tmp_path / "micro_thesis" / "holdings" / "NU.json",
        {"competitive_watchlist": ["Itau Unibanco"]},
    )
    rows = load_peer_comp("NU", repo_root=tmp_path)
    assert [r.peer_ticker for r in rows] == ["ITUB"]
    assert rows[0].peer_name == "Itaú Unibanco Holding S.A."
    assert rows[0].market_cap_usd == 60e9
    assert "named rival" in rows[0].match_reasons
    assert "similar scale" in rows[0].match_reasons


def test_legacy_peerslist_payload_shape(tmp_path: Path) -> None:
    """The older {peersList: [...]} payload shape still feeds the pool."""
    fmp = tmp_path / "data" / "historical" / "fmp"
    _write_json(fmp / "TT_peers.json", [{"symbol": "TT", "peersList": ["PP"]}])
    _write_json(fmp / "TT_profile.json", _profile("Target Co", "Technology", "Software", 10e9))
    _write_json(fmp / "PP_profile.json", _profile("Peer Co", "Technology", "Software", 12e9))
    rows = load_peer_comp("TT", repo_root=tmp_path)
    assert [r.peer_ticker for r in rows] == ["PP"]
    assert "same industry" in rows[0].match_reasons


def test_tracked_peer_gets_relevance_boost(tmp_path: Path) -> None:
    """PR7: a peer the platform already tracks outranks an untracked one with
    the same FMP affinity - it is a relevance signal and its data is local."""
    import sqlite3

    repo = _seed_repo(tmp_path)
    db = repo / "data" / "portfolio.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT, archived_at TEXT)")
    conn.execute("INSERT INTO tracked_companies VALUES ('BIGB', 'evaluation', NULL)")
    conn.commit()
    conn.close()

    rows = load_peer_comp("NU", repo_root=repo)
    tickers = [r.peer_ticker for r in rows]
    bigb = next(r for r in rows if r.peer_ticker == "BIGB")
    assert "tracked" in bigb.match_reasons
    # BIGB (industry 2 + tracked 2 = 4) now outranks SMLB (industry + scale = 3).
    assert tickers.index("BIGB") < tickers.index("SMLB")


def test_all_dash_unnamed_rows_dropped(tmp_path: Path) -> None:
    """PR7: a candidate that passes the affinity screen but resolves NO
    metrics at all (no cap, no TTM files) renders as a wall of em-dashes -
    drop it. A named rival survives metric-less (informative by itself)."""
    fmp = tmp_path / "data" / "historical" / "fmp"
    _write_json(
        fmp / "UBER_peers.json",
        [
            {"symbol": "GHST", "companyName": "Ghost Metrics Inc"},
            {"symbol": "LYFT", "companyName": "Lyft, Inc."},
        ],
    )
    _write_json(
        fmp / "UBER_profile.json",
        _profile("Uber Technologies", "Technology", "Software - Application", 150e9),
    )
    # GHST: same industry (score 2) but no cap anywhere and no TTM files.
    _write_json(
        fmp / "GHST_profile.json",
        _profile("Ghost Metrics Inc", "Technology", "Software - Application", None),
    )
    # LYFT: named rival via the watchlist, also metric-less - must survive.
    _write_json(
        tmp_path / "micro_thesis" / "holdings" / "UBER.json",
        {"competitive_watchlist": ["Lyft"]},
    )
    rows = load_peer_comp("UBER", repo_root=tmp_path)
    tickers = [r.peer_ticker for r in rows]
    assert "GHST" not in tickers
    assert "LYFT" in tickers
