# pyright: reportPrivateUsage=false
#
# Drives the curate_peers router directly (module-private) to pin its
# pin/exclude/override behavior; the directive matches the repo convention.
"""S5 — steerable peers: the curate_peers route + the re-evaluable override.

The owner's "these are shit peers, remove this section unless you select
better" is now ACTIONABLE, not a memo. ``curate_peers`` pins rivals into the
existing ``competitive_watchlist`` (the +3 scorer field), excludes bad ones
into the new ``peer_exclude`` field, and persists a ``peers_section_override``
the peer scorer re-evaluates every build. A pinned bare TICKER absent from the
FMP pool is injected so it actually shows; the conditional override hides the
panel until enough credible comps qualify, then returns it on its own.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import process_report_comments as prc  # noqa: E402

import comments  # noqa: E402
from report.sections.p3_data import (  # noqa: E402
    PeerCompRow,
    evaluate_peers_override,
    load_peer_comp,
)


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


def _comment(text: str, key: str = "peer_comp") -> comments.Comment:
    return comments.Comment(
        id="c1",
        anchor=comments.Anchor(type="peer_comp", key=key),  # pyright: ignore[reportArgumentType]
        comment=text,
    )


def _canned(payload: dict[str, object]):
    def _f(*_a: object, **_k: object) -> str:
        return json.dumps(payload)

    return _f


def _holdings(tmp_path: Path, **fields: object) -> Path:
    payload: dict[str, object] = {"ticker": "NU", "name": "Nu", "thesis": "t"}
    payload.update(fields)
    path = tmp_path / "micro_thesis" / "holdings" / "NU.json"
    _write_json(path, payload)
    return path


# ---------------------------------------------------------------------------
# _route_curate_peers: pins → watchlist, excludes → peer_exclude, conditional → override
# ---------------------------------------------------------------------------


def test_pins_append_to_watchlist_dedup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _holdings(tmp_path, competitive_watchlist=["Itau Unibanco"])
    monkeypatch.setattr(
        prc,
        "call_llm",
        _canned(
            {
                "pin": ["HOOD", "Itau Unibanco"],  # one new, one already-pinned
                "exclude": [],
                "hide_unless_quality": False,
                "diff_summary": "pin Robinhood",
            }
        ),
    )
    res = prc._route_curate_peers(tmp_path, "NU", _comment("pin HOOD"), apply=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    # Append-only, case-insensitive dedupe — never drops a prior pin.
    assert data["competitive_watchlist"] == ["Itau Unibanco", "HOOD"]
    assert res["pinned"] == ["HOOD"]
    assert "competitive_watchlist" in cast("list[str]", res["fields_touched"])


def test_excludes_write_peer_exclude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _holdings(tmp_path)
    monkeypatch.setattr(
        prc,
        "call_llm",
        _canned(
            {"pin": [], "exclude": ["BARC"], "hide_unless_quality": False, "diff_summary": "drop"}
        ),
    )
    prc._route_curate_peers(tmp_path, "NU", _comment("drop Barclays"), apply=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["peer_exclude"] == ["BARC"]


def test_conditional_persists_reevaluable_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _holdings(tmp_path)
    monkeypatch.setattr(
        prc,
        "call_llm",
        _canned(
            {
                "pin": [],
                "exclude": [],
                "hide_unless_quality": True,
                "min_quality_peers": 3,
                "diff_summary": "hide unless 3 credible peers",
            }
        ),
    )
    res = prc._route_curate_peers(
        tmp_path, "NU", _comment("remove this section unless you select better peers"), apply=True
    )
    ov = json.loads(path.read_text(encoding="utf-8"))["peers_section_override"]
    assert ov["action"] == "hide"
    assert ov["condition"] == "peers_quality"
    assert ov["min_quality_peers"] == 3
    assert ov["source_comment_id"] == "c1"
    assert "created_at" in ov
    assert cast("dict[str, object]", res["section_override"])["action"] == "hide"


def test_dry_run_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _holdings(tmp_path, competitive_watchlist=["A"])
    monkeypatch.setattr(
        prc,
        "call_llm",
        _canned({"pin": ["HOOD"], "exclude": [], "hide_unless_quality": True, "diff_summary": "x"}),
    )
    prc._route_curate_peers(tmp_path, "NU", _comment("pin HOOD"), apply=False)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["competitive_watchlist"] == ["A"]  # untouched
    assert "peers_section_override" not in data


def test_no_actionable_curation_touches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _holdings(tmp_path)
    monkeypatch.setattr(
        prc,
        "call_llm",
        _canned({"pin": [], "exclude": [], "hide_unless_quality": False, "diff_summary": "n/a"}),
    )
    res = prc._route_curate_peers(tmp_path, "NU", _comment("nice section"), apply=True)
    assert res["fields_touched"] == []
    assert "peer_exclude" not in json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# evaluate_peers_override — the override-scoring contract
# ---------------------------------------------------------------------------


def _row(ticker: str, *, named: bool = False, metrics: bool = True) -> PeerCompRow:
    return PeerCompRow(
        peer_ticker=ticker,
        peer_name=ticker,
        market_cap_usd=(10e9 if metrics else None),
        revenue_ttm_usd=None,
        net_margin_ttm=None,
        roic_ttm=None,
        match_reasons=("named rival",) if named else ("same sector",),
    )


def test_override_noop_when_missing_or_wrong_action() -> None:
    assert evaluate_peers_override(None, [])[0] is False
    assert evaluate_peers_override({"action": "show"}, [_row("A", named=True)])[0] is False


def test_override_hides_below_quality_bar() -> None:
    hide, detail = evaluate_peers_override(
        {"action": "hide", "min_quality_peers": 2}, [_row("A", named=True)]
    )
    assert hide is True
    assert detail["quality_peers"] == 1
    assert detail["satisfied"] is False


def test_override_shows_once_bar_met() -> None:
    hide, detail = evaluate_peers_override(
        {"action": "hide", "min_quality_peers": 2},
        [_row("A", named=True), _row("B", named=True)],
    )
    assert hide is False
    assert detail["quality_peers"] == 2
    assert detail["satisfied"] is True


def test_override_named_without_computed_multiples_is_not_quality() -> None:
    # "better peers / computed multiples": a named pin with no metrics doesn't
    # satisfy the bar until its multiples resolve.
    hide, _ = evaluate_peers_override(
        {"action": "hide", "min_quality_peers": 1}, [_row("A", named=True, metrics=False)]
    )
    assert hide is True


# ---------------------------------------------------------------------------
# load_peer_comp — pool injection, exclusion, override re-evaluation
# ---------------------------------------------------------------------------


def _seed(
    tmp_path: Path,
    *,
    watchlist: list[str] | None = None,
    peer_exclude: list[str] | None = None,
    override: dict[str, object] | None = None,
) -> Path:
    fmp = tmp_path / "data" / "historical" / "fmp"
    _write_json(
        fmp / "NU_peers.json",
        [
            {"symbol": "BIGB", "companyName": "Big Global Bank"},
            {"symbol": "SMLB", "companyName": "Small Bank Co"},
        ],
    )
    _write_json(
        fmp / "NU_profile.json",
        _profile("Nu Holdings", "Financial Services", "Banks - Diversified", 70e9),
    )
    _write_json(
        fmp / "BIGB_profile.json",
        _profile("Big Global Bank", "Financial Services", "Banks - Diversified", 60e9),
    )
    _write_json(
        fmp / "SMLB_profile.json",
        _profile("Small Bank Co", "Financial Services", "Banks - Diversified", 50e9),
    )
    holdings: dict[str, object] = {"ticker": "NU"}
    if watchlist is not None:
        holdings["competitive_watchlist"] = watchlist
    if peer_exclude is not None:
        holdings["peer_exclude"] = peer_exclude
    if override is not None:
        holdings["peers_section_override"] = override
    _write_json(tmp_path / "micro_thesis" / "holdings" / "NU.json", holdings)
    return tmp_path


def test_pinned_ticker_absent_from_pool_is_injected(tmp_path: Path) -> None:
    repo = _seed(tmp_path, watchlist=["HOOD"])
    _write_json(
        tmp_path / "data" / "historical" / "fmp" / "HOOD_profile.json",
        _profile("Robinhood Markets", "Financial Services", "Capital Markets", 40e9),
    )
    rows = load_peer_comp("NU", repo_root=repo)
    hood = next((r for r in rows if r.peer_ticker == "HOOD"), None)
    assert hood is not None  # injected even though the FMP pool omitted it
    assert "named rival" in hood.match_reasons


def test_prose_name_pin_is_not_fabricated_into_a_ticker(tmp_path: Path) -> None:
    repo = _seed(tmp_path, watchlist=["Big Global Bank"])
    rows = load_peer_comp("NU", repo_root=repo)
    assert all(" " not in r.peer_ticker for r in rows)  # no "BIG GLOBAL BANK" pool ticker
    bigb = next(r for r in rows if r.peer_ticker == "BIGB")
    assert "named rival" in bigb.match_reasons  # still boosted via the name match


def test_peer_exclude_drops_by_ticker(tmp_path: Path) -> None:
    repo = _seed(tmp_path, peer_exclude=["BIGB"])
    tickers = {r.peer_ticker for r in load_peer_comp("NU", repo_root=repo)}
    assert "BIGB" not in tickers
    assert "SMLB" in tickers


def test_peer_exclude_drops_by_name(tmp_path: Path) -> None:
    repo = _seed(tmp_path, peer_exclude=["Big Global Bank"])
    assert "BIGB" not in {r.peer_ticker for r in load_peer_comp("NU", repo_root=repo)}


def test_override_hides_panel_then_returns_when_pinned(tmp_path: Path) -> None:
    override: dict[str, object] = {
        "action": "hide",
        "min_quality_peers": 2,
        "require_named": True,
        "require_metrics": True,
    }
    # No named rivals → the screen's sector matches aren't "quality" → hidden.
    repo = _seed(tmp_path, override=override)
    assert load_peer_comp("NU", repo_root=repo) == []

    # Pin two real in-pool rivals → they become named rivals with metrics → the
    # condition is satisfied and the panel returns on its own (no manual un-hide).
    repo2 = _seed(tmp_path, watchlist=["Big Global Bank", "Small Bank Co"], override=override)
    rows = load_peer_comp("NU", repo_root=repo2)
    assert {r.peer_ticker for r in rows} == {"BIGB", "SMLB"}
