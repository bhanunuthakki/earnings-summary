"""Tests for the advisor memos build (master build P2.3).

Layers:
* advisor_memos store over an alembic-built DB (0077 CHECKs live, including
  the widened analyst_notes source CHECK admitting 'advisor'),
* the deterministic swap screen (margin bar, breach + stale-DCF exclusion),
* memo generation with the LLM mocked — persistence contract (memo + note +
  ledger backlinks), screen gating (no LLM spend when nothing clears),
  transient-failure degradation vs hard-stop propagation,
* panel composition + the two server seams (panel fragment, advisor-memo
  action) over a non-spawning job registry.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from flask.testing import FlaskClient

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "execution"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import comments_server  # noqa: E402

import advisor.memos as memos_mod  # noqa: E402
from advisor.context import (  # noqa: E402
    AdvisorContext,
    SwapCandidate,
    TickerValuation,
    screen_swap_candidates,
)
from advisor.memos import generate_next_dollar_memo, run_swap_checks  # noqa: E402
from advisor.store import AdvisorMemoRow, get_memo, insert_memo, list_memos  # noqa: E402
from dispatch_registry import Registry  # noqa: E402
from integrations.portfolio_tracker_client import (  # noqa: E402
    LivePortfolio,
    PortfolioAnalytics,
)
from llm.cli import LLMSetupError  # noqa: E402
from pipeline.advisor_memos_panel import compose_memos_page  # noqa: E402
from pipeline.allocation_decisions_panel import SizingAuditRow  # noqa: E402
from user_state.ledger import list_entries  # noqa: E402
from user_state.notes import create_note, list_notes  # noqa: E402

_PRIOR_HEAD = "0059_kpi_facts_restatement"


def _build_db(tmp_path: Path) -> Path:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.stamp(cfg, _PRIOR_HEAD)
    command.upgrade(cfg, "head")
    return db


# --------------------------------------------------------------------------- #
# Store + migration
# --------------------------------------------------------------------------- #


def test_store_roundtrip_and_validation(tmp_path: Path) -> None:
    db = _build_db(tmp_path)
    memo = insert_memo(
        user_id="bhanu",
        kind="swap_check",
        ticker="nu",
        counter_ticker="tsm",
        title="Swap check · NU vs TSM",
        body_md="## The screen's case\nNU upside +10%, TSM +40%.",
        context={"margin_pp": 30.0},
        db_path=db,
    )
    assert memo.ticker == "NU" and memo.counter_ticker == "TSM"
    assert memo.score_status == "pending" and memo.stance is None
    fetched = get_memo(memo.id, db_path=db)
    assert fetched is not None and fetched.context == {"margin_pp": 30.0}
    assert [m.id for m in list_memos(user_id="bhanu", db_path=db)] == [memo.id]
    assert list_memos(user_id="bhanu", kind="next_dollar", db_path=db) == []

    with pytest.raises(ValueError, match="kind"):
        insert_memo(user_id="bhanu", kind="bogus", ticker=None, title="t", body_md="b", db_path=db)
    with pytest.raises(ValueError, match="stance"):
        insert_memo(
            user_id="bhanu",
            kind="socratic",
            ticker="NU",
            title="t",
            body_md="b",
            stance="moon",
            db_path=db,
        )


def test_position_review_kind_persists_against_real_check_constraint(tmp_path: Path) -> None:
    """Regression for the prod IntegrityError (2026-07-02 adversarial review):
    0077's ``ck_advisor_memos_kind`` CHECK predated the 'position_review' kind
    that ``advisor.store.MEMO_KINDS`` has carried since P2's position-review
    service landed. Every position-review unit test stubs persistence
    (``persist=False`` or a monkeypatched ``persist_memo``), so nothing ever
    exercised a real INSERT against the real CHECK — 0140 widens it; this
    pins the widened constraint actually accepts the kind end-to-end."""
    db = _build_db(tmp_path)
    memo = insert_memo(
        user_id="bhanu",
        kind="position_review",
        ticker="rbrk",
        title="Position review: RBRK -> trim",
        body_md="**RBRK** — position-review verdict: **trim**",
        context={"valuation_verdict": "trim"},
        stance="trim",
        db_path=db,
    )
    assert memo.kind == "position_review" and memo.ticker == "RBRK" and memo.stance == "trim"
    assert [m.id for m in list_memos(user_id="bhanu", kind="position_review", db_path=db)] == [
        memo.id
    ]


def test_widened_note_source_accepts_advisor(tmp_path: Path) -> None:
    db = _build_db(tmp_path)
    note = create_note(
        user_id="bhanu",
        ticker=None,
        kind="observation",
        body="[advisor memo #1] test",
        source="advisor",
        db_path=db,
    )
    assert note.source == "advisor"


# --------------------------------------------------------------------------- #
# Swap screen
# --------------------------------------------------------------------------- #


def _val(
    ticker: str,
    upside: float,
    *,
    list_type: str = "watchlist",
    verdict: str | None = "ok",
    dcf_date: str = "2026-06-01",
    stub: bool = False,
) -> TickerValuation:
    return TickerValuation(
        ticker=ticker,
        upside_pct=upside,
        dcf_date=dcf_date,
        list_type=list_type,
        verdict=verdict,
        stub_thesis=stub,
    )


_NOW = datetime(2026, 6, 10, tzinfo=UTC)


def test_screen_ranks_and_marks_cleared() -> None:
    holdings = {
        "NU": _val("NU", 30.0, list_type="portfolio"),
        "WIX": _val("WIX", 5.0, list_type="portfolio"),
    }
    candidates = {"TSM": _val("TSM", 45.0), "V": _val("V", 10.0)}
    screen = screen_swap_candidates(holdings, candidates, margin_pp=15.0, now=_NOW)
    assert [s.holding for s in screen] == ["WIX", "NU"]  # widest margin first
    wix, nu = screen
    assert wix.candidate == "TSM" and wix.margin_pp == pytest.approx(40.0) and wix.cleared
    assert nu.margin_pp == pytest.approx(15.0) and nu.cleared  # bar inclusive
    tighter = screen_swap_candidates(holdings, candidates, margin_pp=41.0, now=_NOW)
    assert not any(s.cleared for s in tighter)


def test_screen_excludes_breached_stale_and_implausible_candidates() -> None:
    holdings = {"NU": _val("NU", 10.0, list_type="portfolio")}
    candidates = {
        "BAD": _val("BAD", 90.0, verdict="breach"),
        "OLD": _val("OLD", 80.0, dcf_date="2024-01-01"),
        "FX": _val("FX", 519.0),  # currency-artifact DCF: > IMPLAUSIBLE_UPSIDE_PCT
        "OK": _val("OK", 35.0),
    }
    screen = screen_swap_candidates(holdings, candidates, margin_pp=15.0, now=_NOW)
    (row,) = screen
    assert row.candidate == "OK"  # breach + stale + implausible excluded
    assert row.cleared
    none = screen_swap_candidates(
        holdings,
        {"BAD": candidates["BAD"], "OLD": candidates["OLD"], "FX": candidates["FX"]},
        margin_pp=15.0,
        now=_NOW,
    )
    assert none == []


def test_screen_never_crowns_a_stub_thesis_candidate() -> None:
    """Red-team wave A: a name whose thesis_state.thesis is the literal
    "STUB: needs user-authored thesis" placeholder is unresearched — however
    juicy its sweep-built DCF looks, it must never be the "best alternative"."""
    holdings = {"NU": _val("NU", 10.0, list_type="portfolio")}
    candidates = {
        "STB": _val("STB", 60.0, stub=True),  # widest upside, but a stub thesis
        "OK": _val("OK", 35.0),
    }
    screen = screen_swap_candidates(holdings, candidates, margin_pp=15.0, now=_NOW)
    (row,) = screen
    assert row.candidate == "OK"
    # An all-stub pool yields NO swap rows at all rather than crowning a stub.
    only_stub = screen_swap_candidates(
        holdings, {"STB": candidates["STB"]}, margin_pp=15.0, now=_NOW
    )
    assert only_stub == []


def test_load_valuations_flags_stub_thesis_names(tmp_path) -> None:
    """load_valuations joins thesis_state minimally to stamp ``stub_thesis``,
    so the screen's eligibility filter has the signal without re-querying."""
    import sqlite3 as _sqlite3

    from advisor.context import load_valuations

    conn = _sqlite3.connect(str(tmp_path / "vals.db"))
    conn.row_factory = _sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tracked_companies (
            ticker TEXT PRIMARY KEY, list_type TEXT NOT NULL, archived_at TEXT
        );
        CREATE TABLE thesis_state (ticker TEXT NOT NULL, thesis TEXT);
        CREATE TABLE dcf_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL, valuation_date TEXT,
            npv_per_share REAL, live_price REAL, created_at TEXT
        );
        """
    )
    for ticker, list_type, thesis in (
        ("NU", "portfolio", "Real thesis."),
        ("STB", "watchlist", "STUB: needs user-authored thesis"),
        ("OK", "evaluation", "Another real thesis."),
    ):
        conn.execute(
            "INSERT INTO tracked_companies (ticker, list_type) VALUES (?, ?)",
            (ticker, list_type),
        )
        conn.execute("INSERT INTO thesis_state (ticker, thesis) VALUES (?, ?)", (ticker, thesis))
        conn.execute(
            "INSERT INTO dcf_runs (ticker, valuation_date, npv_per_share, live_price, "
            "created_at) VALUES (?, '2026-06-01', 12.0, 10.0, '2026-06-01T00:00:00')",
            (ticker,),
        )
    conn.commit()

    holdings, candidates = load_valuations(conn)
    conn.close()
    assert holdings["NU"].stub_thesis is False
    assert candidates["STB"].stub_thesis is True
    assert candidates["OK"].stub_thesis is False


# --------------------------------------------------------------------------- #
# Memo generation (LLM mocked)
# --------------------------------------------------------------------------- #


def _audit_row(ticker: str, weight: float | None = 5.0) -> SizingAuditRow:
    return SizingAuditRow(
        ticker=ticker,
        name=None,
        verdict="ok",
        conviction=4.0,
        conviction_at="2026-06-01",
        target_weight_pct=None,
        target_at=None,
        weight_pct=weight,
        market_value=5000.0 if weight is not None else None,
        fv_gap_pct=None,
        alpha_usd=120.0,
        alpha_frac=0.02,
    )


def _ctx(
    repo_root: Path,
    holdings_val: dict[str, TickerValuation],
    candidates_val: dict[str, TickerValuation],
) -> AdvisorContext:
    return AdvisorContext(
        repo_root=repo_root,
        audit_rows=[_audit_row(t) for t in holdings_val],
        holdings_val=holdings_val,
        candidates_val=candidates_val,
        live=LivePortfolio(available=False, api_url="http://x", error="down"),
        analytics=PortfolioAnalytics(available=False, api_url="http://x"),
        open_notes_block="",
        generated_at="2026-06-10T12:00:00+00:00",
    )


def test_next_dollar_persists_memo_note_and_backlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _build_db(tmp_path)
    calls: list[str] = []

    def fake_llm(prompt: str, **kwargs: object) -> str:
        calls.append(str(kwargs.get("purpose")))
        assert "NEVER instruct" in prompt
        return "## Where the next dollar works hardest\nNU has the widest gap.\n\nPlain conclusion line."

    monkeypatch.setattr(memos_mod, "call_llm", fake_llm)
    ctx = _ctx(
        tmp_path, {"NU": _val("NU", 30.0, list_type="portfolio")}, {"TSM": _val("TSM", 45.0)}
    )
    result = generate_next_dollar_memo(tmp_path, user_id="bhanu", ctx=ctx)
    assert result.ok and result.memo_id is not None
    assert calls == ["advisor_next_dollar"]

    memo = get_memo(result.memo_id, db_path=db)
    assert memo is not None
    assert memo.kind == "next_dollar" and memo.ticker is None
    assert memo.horizon_days == 90
    assert memo.context is not None and "holding_upsides" in memo.context
    # Memory: a portfolio-level advisor note exists and backlinks resolve.
    assert memo.note_id is not None and memo.ledger_entry_id is None
    notes = list_notes(user_id="bhanu", db_path=db)
    assert any(n.id == memo.note_id and n.source == "advisor" for n in notes)


def test_swap_checks_gate_on_screen_and_write_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _build_db(tmp_path)
    calls: list[str] = []

    def fake_llm(prompt: str, **kwargs: object) -> str:
        calls.append(str(kwargs.get("ticker")))
        assert "swap-discipline" in prompt  # phrase wraps across a line break
        return "## The screen's case\nMargin is wide.\n\nLean: evidence does not clear the bar."

    monkeypatch.setattr(memos_mod, "call_llm", fake_llm)
    holdings = {
        "NU": _val("NU", 5.0, list_type="portfolio"),
        "WIX": _val("WIX", 8.0, list_type="portfolio"),
    }
    candidates = {"TSM": _val("TSM", 45.0)}
    results = run_swap_checks(
        tmp_path, user_id="bhanu", margin_pp=15.0, ctx=_ctx(tmp_path, holdings, candidates)
    )
    # Both pairs clear, but the candidate dedupe spends ONE call (same TSM).
    assert len(results) == 1 and results[0].ok
    assert calls == ["NU"]  # widest margin first (NU upside 5 < WIX 8)
    memo = get_memo(results[0].memo_id or 0, db_path=db)
    assert memo is not None and memo.kind == "swap_check"
    assert memo.ticker == "NU" and memo.counter_ticker == "TSM"
    assert memo.ledger_entry_id is not None
    entries = list_entries(user_id="bhanu", ticker="NU", db_path=db)
    assert any(e.entry_kind == "advisor_memo" and e.id == memo.ledger_entry_id for e in entries)


def test_swap_checks_spend_nothing_when_bar_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _build_db(tmp_path)

    def fail_if_called(prompt: str, **kwargs: object) -> str:
        raise AssertionError("LLM must not be called when no pair clears the bar")

    monkeypatch.setattr(memos_mod, "call_llm", fail_if_called)
    holdings = {"NU": _val("NU", 30.0, list_type="portfolio")}
    candidates = {"TSM": _val("TSM", 35.0)}  # margin 5pp < 15pp bar
    results = run_swap_checks(
        tmp_path, user_id="bhanu", margin_pp=15.0, ctx=_ctx(tmp_path, holdings, candidates)
    )
    assert results == []


def test_transient_llm_failure_degrades_hard_stop_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _build_db(tmp_path)
    ctx = _ctx(tmp_path, {"NU": _val("NU", 30.0, list_type="portfolio")}, {})

    def transient(prompt: str, **kwargs: object) -> str:
        raise TimeoutError("claude CLI timed out")

    monkeypatch.setattr(memos_mod, "call_llm", transient)
    result = generate_next_dollar_memo(tmp_path, user_id="bhanu", ctx=ctx)
    assert not result.ok and result.skipped_reason is not None
    assert "TimeoutError" in result.skipped_reason

    def hard(prompt: str, **kwargs: object) -> str:
        raise LLMSetupError("claude CLI not found")

    monkeypatch.setattr(memos_mod, "call_llm", hard)
    with pytest.raises(LLMSetupError):
        generate_next_dollar_memo(tmp_path, user_id="bhanu", ctx=ctx)


# --------------------------------------------------------------------------- #
# Panel + server seams
# --------------------------------------------------------------------------- #


def test_compose_memos_page_renders_all_sections(tmp_path: Path) -> None:
    screen = [
        SwapCandidate(
            holding="NU",
            holding_upside_pct=10.0,
            holding_dcf_date="2026-06-01",
            candidate="TSM",
            candidate_upside_pct=45.0,
            candidate_dcf_date="2026-06-01",
            candidate_list="watchlist",
            margin_pp=35.0,
            cleared=True,
        )
    ]
    memo = AdvisorMemoRow(
        id=1,
        user_id="bhanu",
        kind="next_dollar",
        ticker=None,
        counter_ticker=None,
        title="Next-dollar memo · 2026-06-10",
        body_md="## Where the next dollar works hardest\n- NU",
        context=None,
        stance=None,
        horizon_days=90,
        score_status="pending",
        note_id=7,
        ledger_entry_id=None,
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
    )
    html = compose_memos_page(screen, [memo])
    assert "Swap-discipline screen" in html and "clears the bar" in html
    assert "Memo record" in html and "Next-dollar memo" in html
    assert "/actions/advisor-memo" in html and "note #7" in html
    empty = compose_memos_page([], [])
    assert "No memos yet" in empty


class _NonSpawningRegistry(Registry):
    """Records starts without forking a subprocess (same as the actions tests)."""

    def start(self, *, ticker, kind, argv, spawn=True):  # type: ignore[override]
        return super().start(ticker=ticker, kind=kind, argv=argv, spawn=False)


@pytest.fixture
def client(tmp_path: Path) -> FlaskClient:
    _build_db(tmp_path)
    app = comments_server.create_app(tmp_path, registry=_NonSpawningRegistry())
    return app.test_client()


def test_advisor_memos_panel_route(client: FlaskClient, tmp_path: Path) -> None:
    insert_memo(
        user_id="bhanu",
        kind="next_dollar",
        ticker=None,
        title="Next-dollar memo · 2026-06-10",
        body_md="## Section\nbody",
        db_path=tmp_path / "data" / "portfolio.db",
    )
    resp = client.get("/api/panel/advisor_memos")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Memo record" in body and "Next-dollar memo" in body


def test_advisor_memo_action_starts_job(client: FlaskClient) -> None:
    resp = client.post("/actions/advisor-memo", json={"kind": "next_dollar"})
    assert resp.status_code == 201
    payload = resp.get_json()
    assert payload["kind"] == "advisor-next_dollar"
    assert payload["stream_url"].startswith("/actions/stream/")
    assert client.post("/actions/advisor-memo", json={"kind": "bogus"}).status_code == 400
    assert client.post("/actions/advisor-memo", json={}).status_code == 400
