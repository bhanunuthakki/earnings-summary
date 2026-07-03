"""LLM-driven peer selection (directives/peer_selection_llm.md).

Three layers, pinned independently:
  * ``suggest_peers`` — the generator: schema-validate / dedupe / drop self &
    prose tickers; raise (don't silently empty) on unusable JSON.
  * ``load_peer_comp`` merge — LLM-vouched names lead the FMP screen, the `why`
    surfaces, corroboration ranks higher, and the S5 curation layers
    (peer_exclude / override) still win.
  * the mode-A eval — recall scoring + parse-failure handling.

The existing peer tests run with NO peer_selection cache, so they exercise the
unchanged FMP screen; these add the with-generator behavior on top.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import compute.peer_selection as ps  # noqa: E402
from compute.peer_selection import (  # noqa: E402
    PeerFetchOutcome,
    PeerSuggestion,
    _fetch_peer_fundamentals,  # pyright: ignore[reportPrivateUsage]  # internal seam under test
    extract_for_ticker,
    suggest_peers,
)
from evals.peer_selection import (  # noqa: E402
    PeerCase,
    grade_peer_selection_case,
    load_peer_selection_golden,
    run_peer_selection_eval,
)
from llm.structured import StructuredParseError  # noqa: E402
from report.sections.p3_data import load_peer_comp  # noqa: E402


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


# ---------------------------------------------------------------------------
# suggest_peers — the generator
# ---------------------------------------------------------------------------


def test_suggest_peers_validates_dedupes_drops_self_and_prose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload: list[object] = [
        {"ticker": "intr", "name": "Inter & Co", "why": "LatAm digital bank"},
        {"ticker": "INTR", "name": "Inter dup", "why": "dup dropped"},  # case-dup
        {"ticker": "NU", "name": "Nu", "why": "self — dropped"},  # self
        {"ticker": "Itau Unibanco", "name": "Itau", "why": "prose, not a ticker"},  # not ticker
        {"name": "missing ticker", "why": "x"},  # invalid → ValidationError
        {"ticker": "STNE", "name": "StoneCo", "why": "LatAm fintech"},
    ]

    def _fake_call(*_a: object, **_k: object) -> object:
        return payload

    monkeypatch.setattr(ps, "call_llm_structured", _fake_call)
    out = suggest_peers(
        ticker="NU",
        name="Nu Holdings",
        business_description="digital bank",
        segments=[],
        sector="Financial Services",
        industry="Banks - Diversified",
    )
    assert [s.ticker for s in out] == ["INTR", "STNE"]  # normalized, deduped, self/prose gone
    assert out[0].why == "LatAm digital bank"


def test_suggest_peers_forwards_backend_kwarg(monkeypatch: pytest.MonkeyPatch) -> None:
    """suggest_peers(..., backend="gemini") must pass backend through to call_llm_structured."""
    captured: dict[str, object] = {}

    def _fake_structured(_prompt: str, **kwargs: object) -> object:
        captured.update(kwargs)
        return [{"ticker": "INTR", "name": "Inter & Co", "why": "digital bank"}]

    monkeypatch.setattr(ps, "call_llm_structured", _fake_structured)
    suggest_peers(
        ticker="NU",
        name="Nu Holdings",
        business_description="digital bank",
        segments=[],
        sector=None,
        industry=None,
        model="gemini-2.5-flash",
        backend="gemini",
    )
    assert captured.get("backend") == "gemini"
    assert captured.get("model") == "gemini-2.5-flash"


def test_suggest_peers_propagates_parse_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> object:
        raise StructuredParseError("unusable twice", raw_head="not json")

    monkeypatch.setattr(ps, "call_llm_structured", _boom)
    with pytest.raises(StructuredParseError):
        suggest_peers(
            ticker="NU",
            name="Nu",
            business_description="d",
            segments=[],
            sector=None,
            industry=None,
        )


# ---------------------------------------------------------------------------
# load_peer_comp — the merge (LLM seeds the pool, S5 stays intact)
# ---------------------------------------------------------------------------


def _seed_merge(tmp_path: Path, *, suggestions: list[dict[str, str]], **holdings: object) -> Path:
    """NU with an FMP screen of [BIGB, SMLB] and a peer_selection cache. INTR is
    an LLM-only name absent from the FMP pool; BIGB is in BOTH (corroboration).
    All three carry a profile so they resolve metrics (not all-dash dropped)."""
    fmp = tmp_path / "data" / "historical" / "fmp"
    _write_json(
        fmp / "NU_peers.json",
        [
            {"symbol": "BIGB", "companyName": "Big Global Bank"},
            {"symbol": "SMLB", "companyName": "Small Bank Co"},
        ],
    )
    _write_json(
        fmp / "NU_profile.json", _profile("Nu", "Financial Services", "Banks - Diversified", 70e9)
    )
    # Same industry + similar scale for all three, so the only ranking
    # differentiator is the thesis-peer + corroboration bonus.
    _write_json(
        fmp / "BIGB_profile.json",
        _profile("Big Global Bank", "Financial Services", "Banks - Diversified", 60e9),
    )
    _write_json(
        fmp / "SMLB_profile.json",
        _profile("Small Bank Co", "Financial Services", "Banks - Diversified", 50e9),
    )
    _write_json(
        fmp / "INTR_profile.json",
        _profile("Inter & Co", "Financial Services", "Banks - Diversified", 55e9),
    )
    _write_json(
        tmp_path / "data" / "peer_selection" / "NU.json",
        {"ticker": "NU", "suggestions": suggestions, "inputs_sha256": "x"},
    )
    holdings_payload: dict[str, object] = {"ticker": "NU", **holdings}
    _write_json(tmp_path / "micro_thesis" / "holdings" / "NU.json", holdings_payload)
    return tmp_path


def test_llm_peer_leads_with_why_and_thesis_tag(tmp_path: Path) -> None:
    repo = _seed_merge(
        tmp_path,
        suggestions=[{"ticker": "INTR", "name": "Inter & Co", "why": "LatAm digital bank"}],
    )
    rows = load_peer_comp("NU", repo_root=repo)
    tickers = [r.peer_ticker for r in rows]
    assert "INTR" in tickers
    intr = next(r for r in rows if r.peer_ticker == "INTR")
    assert "thesis peer" in intr.match_reasons
    assert "LatAm digital bank" in intr.match_reasons  # the why → Why column
    # The thesis peer outranks the FMP-only SMLB (which has no thesis bonus).
    assert tickers.index("INTR") < tickers.index("SMLB")


def test_corroborated_llm_peer_outranks_non_corroborated(tmp_path: Path) -> None:
    repo = _seed_merge(
        tmp_path,
        suggestions=[
            {"ticker": "INTR", "name": "Inter & Co", "why": "digital bank"},
            {"ticker": "BIGB", "name": "Big Global Bank", "why": "diversified bank comp"},
        ],
    )
    rows = load_peer_comp("NU", repo_root=repo)
    tickers = [r.peer_ticker for r in rows]
    # BIGB is in BOTH the LLM set and the FMP screen → corroboration bonus →
    # it outranks INTR (LLM-only) despite identical industry/scale affinity.
    assert tickers.index("BIGB") < tickers.index("INTR")


def test_peer_exclude_drops_an_llm_suggestion(tmp_path: Path) -> None:
    repo = _seed_merge(
        tmp_path,
        suggestions=[{"ticker": "INTR", "name": "Inter & Co", "why": "digital bank"}],
        peer_exclude=["INTR"],
    )
    tickers = {r.peer_ticker for r in load_peer_comp("NU", repo_root=repo)}
    assert "INTR" not in tickers  # owner exclusion wins over the LLM


def test_override_still_gates_panel_with_llm_peers(tmp_path: Path) -> None:
    # require_named: an LLM thesis peer is NOT a "named rival" (owner didn't
    # vouch for it), so the hide-unless-named override keeps the panel hidden.
    repo = _seed_merge(
        tmp_path,
        suggestions=[{"ticker": "INTR", "name": "Inter & Co", "why": "digital bank"}],
        peers_section_override={"action": "hide", "min_quality_peers": 1, "require_named": True},
    )
    assert load_peer_comp("NU", repo_root=repo) == []


def test_no_cache_is_unchanged_fmp_screen(tmp_path: Path) -> None:
    # No peer_selection cache → behaves exactly like the pre-generator screen.
    fmp = tmp_path / "data" / "historical" / "fmp"
    _write_json(fmp / "NU_peers.json", [{"symbol": "BIGB", "companyName": "Big Global Bank"}])
    _write_json(
        fmp / "NU_profile.json", _profile("Nu", "Financial Services", "Banks - Diversified", 70e9)
    )
    _write_json(
        fmp / "BIGB_profile.json",
        _profile("Big Global Bank", "Financial Services", "Banks - Diversified", 60e9),
    )
    rows = load_peer_comp("NU", repo_root=tmp_path)
    assert [r.peer_ticker for r in rows] == ["BIGB"]
    assert "thesis peer" not in rows[0].match_reasons


def test_metricless_thesis_peer_dropped_not_rendered_as_dashes(tmp_path: Path) -> None:
    """A suggested peer whose fundamentals never resolved (foreign-only
    listing, delisted symbol, fetch never ran) must NOT render as an all-dash
    row — hide-don't-stub applies to thesis peers too (the directive's
    "suggested peers render WITH computed multiples" acceptance criterion)."""
    repo = _seed_merge(
        tmp_path,
        suggestions=[
            {"ticker": "INTR", "name": "Inter & Co", "why": "digital bank"},
            {"ticker": "GHST", "name": "Ghost Listing", "why": "no data anywhere"},
        ],
    )
    rows = load_peer_comp("NU", repo_root=repo)
    tickers = [r.peer_ticker for r in rows]
    assert "INTR" in tickers  # has a profile → metrics → renders
    assert "GHST" not in tickers  # metric-less thesis peer → dropped


# ---------------------------------------------------------------------------
# _fetch_peer_fundamentals — key resolution, per-file top-up, 429 abort
# ---------------------------------------------------------------------------


class _StubResponse:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        return self._body


class _StubRequests:
    """Stands in for the lazily-imported ``requests`` module: records every
    (endpoint, params) and returns a fixed response."""

    def __init__(self, response: _StubResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, params: dict[str, str], timeout: int) -> _StubResponse:
        self.calls.append((url.rsplit("/", 1)[-1], dict(params)))
        return self.response


def _no_sleep(_seconds: float) -> None:
    return None


def _install_stub(
    monkeypatch: pytest.MonkeyPatch, response: _StubResponse, *, api_key: str | None = "k-env"
) -> _StubRequests:
    stub = _StubRequests(response)
    monkeypatch.setitem(sys.modules, "requests", stub)
    monkeypatch.setattr(ps.time, "sleep", _no_sleep)
    if api_key is None:
        monkeypatch.delenv("FMP_API_KEY", raising=False)
    else:
        monkeypatch.setenv("FMP_API_KEY", api_key)
    return stub


def test_fetch_tops_up_only_missing_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A peer with a cached profile but nothing else gets the OTHER three files
    fetched (the old profile-exists check skipped it forever), including the
    quarterly income statement the revenue column needs."""
    stub = _install_stub(monkeypatch, _StubResponse(200, [{"revenue": 1}]))
    fmp = tmp_path / "data" / "historical" / "fmp"
    _write_json(fmp / "INTR_profile.json", _profile("Inter & Co", None, None, 1e9))

    outcome = _fetch_peer_fundamentals(["INTR"], tmp_path, self_ticker="NU")
    assert outcome.fetched_any == ["INTR"]
    assert outcome.fetched_complete == ["INTR"]  # all 4 endpoints resolved
    endpoints = [c[0].split("?")[0] for c in stub.calls]
    assert endpoints == ["key-metrics-ttm", "ratios-ttm", "income-statement"]
    income_params = stub.calls[-1][1]
    assert income_params["period"] == "quarter" and income_params["limit"] == "4"
    for suffix in ("key_metrics_ttm", "financial_ratios_ttm", "income_statement_quarterly"):
        assert (fmp / f"INTR_{suffix}.json").exists()


def test_fetch_fully_cached_peer_costs_zero_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _install_stub(monkeypatch, _StubResponse(200, [{"x": 1}]))
    fmp = tmp_path / "data" / "historical" / "fmp"
    for suffix in (
        "profile",
        "key_metrics_ttm",
        "financial_ratios_ttm",
        "income_statement_quarterly",
    ):
        _write_json(fmp / f"INTR_{suffix}.json", [{"x": 1}])
    assert _fetch_peer_fundamentals(["INTR"], tmp_path, self_ticker="NU") == PeerFetchOutcome()
    assert stub.calls == []


def test_fetch_aborts_whole_run_on_429(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """429 = the FMP daily budget is exhausted — stop immediately instead of
    burning the remaining per-build call budget on more 429s."""
    stub = _install_stub(monkeypatch, _StubResponse(429, []))
    outcome = _fetch_peer_fundamentals(["INTR", "STNE"], tmp_path, self_ticker="NU")
    assert outcome == PeerFetchOutcome()
    assert len(stub.calls) == 1  # first 429 aborts; STNE never attempted
    assert not (tmp_path / "data" / "historical" / "fmp" / "INTR_profile.json").exists()


def test_fetch_resolves_key_from_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No FMP_API_KEY in the environment → the key is read from repo_root/.env
    (the LLM build never exports it; this was the root cause of the prod-wide
    fetched_peers=[])."""
    stub = _install_stub(monkeypatch, _StubResponse(200, [{"x": 1}]), api_key=None)
    (tmp_path / ".env").write_text("FMP_API_KEY=k-dotenv\n", encoding="utf-8")
    outcome = _fetch_peer_fundamentals(["INTR"], tmp_path, self_ticker="NU")
    assert outcome.fetched_any == ["INTR"]
    assert all(c[1]["apikey"] == "k-dotenv" for c in stub.calls)


def test_fetch_skips_cleanly_without_any_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _install_stub(monkeypatch, _StubResponse(200, [{"x": 1}]), api_key=None)
    assert _fetch_peer_fundamentals(["INTR"], tmp_path, self_ticker="NU") == PeerFetchOutcome()
    assert stub.calls == []


class _StubRequestsPerEndpoint:
    """Like ``_StubRequests`` but the response varies by endpoint — models a
    free-tier account where some ``/stable`` endpoints 402 for a given symbol
    while others (``profile``) succeed."""

    def __init__(self, by_endpoint: dict[str, _StubResponse], default: _StubResponse) -> None:
        self._by_endpoint = by_endpoint
        self._default = default
        self.calls: list[str] = []

    def get(self, url: str, params: dict[str, str], timeout: int) -> _StubResponse:
        endpoint = url.rsplit("/", 1)[-1]
        self.calls.append(endpoint)
        return self._by_endpoint.get(endpoint, self._default)


def test_fetch_marks_partial_free_tier_peer_as_not_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the fetched_peers/fetched_complete split: a free-tier
    account can 402 on key-metrics-ttm/ratios-ttm/income-statement while
    profile still succeeds (observed for GRAB/INTR/KSPI/PAGS) — that peer must
    land in fetched_any (so the panel doesn't skip it) but NOT in
    fetched_complete (so partial-data peers stay distinguishable from
    fully-resolved ones in the cache)."""
    stub = _StubRequestsPerEndpoint(
        by_endpoint={"profile": _StubResponse(200, [{"symbol": "GRAB"}])},
        default=_StubResponse(402, {"error": "premium endpoint"}),
    )
    monkeypatch.setitem(sys.modules, "requests", stub)
    monkeypatch.setattr(ps.time, "sleep", _no_sleep)
    monkeypatch.setenv("FMP_API_KEY", "k-env")

    outcome = _fetch_peer_fundamentals(["GRAB"], tmp_path, self_ticker="NU")

    assert outcome.fetched_any == ["GRAB"]
    assert outcome.fetched_complete == []
    fmp = tmp_path / "data" / "historical" / "fmp"
    assert (fmp / "GRAB_profile.json").exists()
    for suffix in ("key_metrics_ttm", "financial_ratios_ttm", "income_statement_quarterly"):
        assert not (fmp / f"GRAB_{suffix}.json").exists()


# ---------------------------------------------------------------------------
# extract_for_ticker — cache idempotency (no LLM/FMP on a cache hit)
# ---------------------------------------------------------------------------


def test_extract_caches_and_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sqlite3

    calls = {"n": 0}

    def _fake_suggest(**_k: object) -> list[PeerSuggestion]:
        calls["n"] += 1
        return [PeerSuggestion(ticker="INTR", name="Inter & Co", why="digital bank")]

    monkeypatch.setattr(ps, "suggest_peers", _fake_suggest)
    fmp = tmp_path / "data" / "historical" / "fmp"
    _write_json(
        fmp / "NU_profile.json", _profile("Nu", "Financial Services", "Banks - Diversified", 70e9)
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    r1 = extract_for_ticker("NU", tmp_path, conn, fetch_fundamentals=False)
    assert [s["ticker"] for s in r1.suggestions] == ["INTR"]
    assert calls["n"] == 1
    # Second call with unchanged inputs → cache hit, no second LLM call.
    r2 = extract_for_ticker("NU", tmp_path, conn, fetch_fundamentals=False)
    assert [s["ticker"] for s in r2.suggestions] == ["INTR"]
    assert calls["n"] == 1
    # --refresh forces a regenerate.
    extract_for_ticker("NU", tmp_path, conn, refresh=True, fetch_fundamentals=False)
    assert calls["n"] == 2
    conn.close()


def test_cache_hit_still_tops_up_missing_fundamentals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cache hit skips the LLM but still runs the fundamentals top-up and
    persists newly fetched peers — this is what heals the prod caches written
    before the fetch had a key (fetched_peers=[] everywhere)."""
    import sqlite3

    llm_calls = {"n": 0}

    def _fake_suggest(**_k: object) -> list[PeerSuggestion]:
        llm_calls["n"] += 1
        return [PeerSuggestion(ticker="INTR", name="Inter & Co", why="digital bank")]

    monkeypatch.setattr(ps, "suggest_peers", _fake_suggest)
    fmp = tmp_path / "data" / "historical" / "fmp"
    _write_json(
        fmp / "NU_profile.json", _profile("Nu", "Financial Services", "Banks - Diversified", 70e9)
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    # First run without fetch — mirrors the pre-fix prod caches.
    r1 = extract_for_ticker("NU", tmp_path, conn, fetch_fundamentals=False)
    assert r1.fetched_peers == [] and llm_calls["n"] == 1

    fetch_calls: list[list[str]] = []

    def _fake_fetch(peers: list[str], _root: Path, *, self_ticker: str) -> PeerFetchOutcome:
        fetch_calls.append(list(peers))
        return PeerFetchOutcome(fetched_any=["INTR"], fetched_complete=["INTR"])

    monkeypatch.setattr(ps, "_fetch_peer_fundamentals", _fake_fetch)
    # Second run: cache hit (no LLM call) but the top-up fetch runs and the
    # cache is updated with what it fetched.
    r2 = extract_for_ticker("NU", tmp_path, conn, fetch_fundamentals=True)
    assert llm_calls["n"] == 1  # still one LLM call — cache hit
    assert fetch_calls == [["INTR"]]
    assert r2.fetched_peers == ["INTR"]
    assert r2.fetched_complete == ["INTR"]
    cached = json.loads(
        (tmp_path / "data" / "peer_selection" / "NU.json").read_text(encoding="utf-8")
    )
    assert cached["fetched_peers"] == ["INTR"]
    assert cached["fetched_complete"] == ["INTR"]
    conn.close()


def test_extract_records_parse_failure_without_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    def _boom(**_k: object) -> list[PeerSuggestion]:
        raise StructuredParseError("nope", raw_head="x")

    monkeypatch.setattr(ps, "suggest_peers", _boom)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    res = extract_for_ticker("NU", tmp_path, conn, fetch_fundamentals=False)
    assert res.suggestions == []
    assert res.skipped_reason and "parse failure" in res.skipped_reason
    conn.close()


# ---------------------------------------------------------------------------
# the eval — loader + recall grading
# ---------------------------------------------------------------------------

_GOLDEN = {
    "purpose": "peer_selection",
    "version": 1,
    "cases": [
        {
            "id": "g1",
            "ticker": "NU",
            "name": "Nu Holdings",
            "sector": "Financial Services",
            "industry": "Banks - Diversified",
            "segments": ["Credit card"],
            "business_description": "A digital bank.",
            "expected_peers": ["SOFI", "INTR", "STNE"],
            "also_valid": ["MELI"],
        },
        {
            "id": "g2",
            "ticker": "UBER",
            "name": "Uber",
            "sector": "Technology",
            "industry": "Software - Application",
            "segments": ["Mobility"],
            "business_description": "A ride-hail marketplace.",
            "expected_peers": ["LYFT", "DASH"],
        },
    ],
}


def _golden_file(tmp_path: Path) -> Path:
    p = tmp_path / "peer_selection.json"
    p.write_text(json.dumps(_GOLDEN), encoding="utf-8")
    return p


def test_golden_loads_and_validates(tmp_path: Path) -> None:
    cases = load_peer_selection_golden(_golden_file(tmp_path))
    assert [c.case_id for c in cases] == ["g1", "g2"]
    assert cases[0].expected_peers == frozenset({"SOFI", "INTR", "STNE"})
    assert cases[0].also_valid == frozenset({"MELI"})


def test_golden_rejects_empty_expected(tmp_path: Path) -> None:
    bad: dict[str, object] = {
        "purpose": "peer_selection",
        "cases": [{"id": "x", "ticker": "NU", "business_description": "d", "expected_peers": []}],
    }
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="expected_peers"):
        load_peer_selection_golden(p)


def test_grade_recall_partial_credit() -> None:
    case = PeerCase(
        case_id="g1",
        ticker="NU",
        name="Nu",
        business_description="d",
        sector=None,
        industry=None,
        segments=(),
        expected_peers=frozenset({"SOFI", "INTR", "STNE"}),
        also_valid=frozenset({"MELI"}),
    )
    # 2 of 3 expected found → recall 2/3; an also_valid extra doesn't hurt.
    res = grade_peer_selection_case(case, suggest_fn=lambda _c: ["SOFI", "INTR", "MELI"])
    assert res.score == pytest.approx(2 / 3)
    assert res.passed is True  # >= 0.5


def test_grade_scores_parse_failure_zero() -> None:
    case = PeerCase(
        case_id="g1",
        ticker="NU",
        name="Nu",
        business_description="d",
        sector=None,
        industry=None,
        segments=(),
        expected_peers=frozenset({"SOFI"}),
    )

    def _boom(_c: PeerCase) -> list[str]:
        raise StructuredParseError("nope", raw_head="x")

    res = grade_peer_selection_case(case, suggest_fn=_boom)
    assert res.score == 0.0
    assert res.failure_stage == "call"


def test_run_eval_averages_recall(tmp_path: Path) -> None:
    answers = {"NU": ["SOFI", "INTR", "STNE"], "UBER": ["LYFT"]}  # 1.0 and 0.5
    summary = run_peer_selection_eval(
        golden_path=_golden_file(tmp_path),
        code_root=PROJECT_ROOT,
        suggest_fn=lambda c: answers[c.ticker],
    )
    assert summary.n_cases == 2
    assert summary.avg_score == pytest.approx((1.0 + 0.5) / 2)
    assert summary.purpose == "peer_selection"
