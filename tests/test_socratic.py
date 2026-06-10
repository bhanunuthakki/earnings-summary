"""Tests for the Socratic think-through (master build P2.4).

Layers:
* the parsers (questions list, forced-stance line) — strict where the flow
  must fail visibly, tolerant where the memo should persist anyway,
* question + memo generation with the LLM mocked — owner-first validation,
  the persistence contract (stance + horizon on the memo row, ledger entry,
  Q&A transcript appended), transient-degrade vs hard-stop-propagate,
* the server seams — both sync endpoints + the standalone page — over an
  alembic-built DB,
* the Memos panel's think-through entry + stance pill.
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

import advisor.socratic as socratic_mod  # noqa: E402
from advisor.context import AdvisorContext, TickerValuation  # noqa: E402
from advisor.socratic import (  # noqa: E402
    generate_decision_memo,
    generate_questions,
    parse_questions,
    parse_stance,
)
from advisor.store import AdvisorMemoRow, get_memo  # noqa: E402
from integrations.portfolio_tracker_client import (  # noqa: E402
    LivePortfolio,
    PortfolioAnalytics,
)
from llm.cli import LLMSetupError  # noqa: E402
from pipeline.advisor_memos_panel import compose_memos_page, render_socratic_page  # noqa: E402
from pipeline.allocation_decisions_panel import SizingAuditRow  # noqa: E402
from user_state.ledger import list_entries  # noqa: E402

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
# Parsers
# --------------------------------------------------------------------------- #


def test_parse_questions_accepts_numbered_and_bulleted() -> None:
    raw = "1. What is your read?\n2) Horizon?\n- What would make you wrong?\nnoise line"
    assert parse_questions(raw) == [
        "What is your read?",
        "Horizon?",
        "What would make you wrong?",
    ]


def test_parse_questions_caps_at_five_and_fails_below_three() -> None:
    raw = "\n".join(f"{i}. Q{i}?" for i in range(1, 8))
    assert len(parse_questions(raw)) == 5
    with pytest.raises(ValueError, match="expected >= 3"):
        parse_questions("1. only one?\n2. and two?")


def test_parse_stance_last_line_wins_and_validates() -> None:
    body = "## Bull\n...\nSTANCE: add\n\nrevision below\n\nstance: TRIM\n"
    assert parse_stance(body) == "trim"
    assert parse_stance("## Bull\nno stance line") is None
    assert parse_stance("STANCE: moonshot") is None


# --------------------------------------------------------------------------- #
# Generation (LLM mocked)
# --------------------------------------------------------------------------- #


def _audit_row(ticker: str) -> SizingAuditRow:
    return SizingAuditRow(
        ticker=ticker,
        name=None,
        verdict="ok",
        conviction=4.0,
        conviction_at="2026-06-01",
        target_weight_pct=6.0,
        target_at="2026-06-01",
        weight_pct=4.2,
        market_value=4200.0,
        fv_gap_pct=None,
        alpha_usd=350.0,
        alpha_frac=0.083,
        mismatch_score=2.0,
        mismatch_reasons=["-1.8pp vs stated target 6.0%"],
    )


def _ctx(repo_root: Path, ticker: str = "NU") -> AdvisorContext:
    return AdvisorContext(
        repo_root=repo_root,
        audit_rows=[_audit_row(ticker)],
        holdings_val={
            ticker: TickerValuation(
                ticker=ticker,
                upside_pct=30.0,
                dcf_date="2026-06-01",
                list_type="portfolio",
                verdict="ok",
            )
        },
        candidates_val={},
        live=LivePortfolio(available=False, api_url="http://x", error="down"),
        analytics=PortfolioAnalytics(available=False, api_url="http://x"),
        open_notes_block="",
        generated_at="2026-06-10T12:00:00+00:00",
    )


def test_generate_questions_grounds_and_parses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts: list[str] = []

    def fake_llm(prompt: str, **kwargs: object) -> str:
        prompts.append(prompt)
        assert kwargs.get("purpose") == "advisor_socratic_questions"
        return "1. Given the 4.2% weight vs your 6% target, what's holding you back?\n2. What horizon?\n3. What breaks it?"

    monkeypatch.setattr(socratic_mod, "call_llm", fake_llm)
    prelude = generate_questions(tmp_path, "nu", ctx=_ctx(tmp_path))
    assert prelude.ticker == "NU" and len(prelude.questions) == 3
    # The prompt carried the sizing context the questions must ground in.
    assert "Weight 4.2% of book" in prompts[0]
    assert "DCF upside +30%" in prompts[0]
    assert "current read" in prompts[0].lower()  # owner-read coverage requirement


def test_generate_questions_raises_on_unparseable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def chatty(*a: object, **k: object) -> str:
        return "I think NU is great because..."

    monkeypatch.setattr(socratic_mod, "call_llm", chatty)
    with pytest.raises(ValueError, match="expected >= 3"):
        generate_questions(tmp_path, "NU", ctx=_ctx(tmp_path))


_MEMO_BODY = (
    "## Bull\n- NIM holding\n\n## Bear\n- credit cycle\n\n"
    "## What would change my mind\n- NPL > 8%\n\n## Stance if forced\n"
    "Weighing the answers, the gap argues patience.\n\nSTANCE: add"
)


def test_decision_memo_persists_stance_horizon_ledger_and_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _build_db(tmp_path)
    seen: dict[str, object] = {}

    def fake_llm(prompt: str, **kwargs: object) -> str:
        seen["prompt"] = prompt
        assert kwargs.get("purpose") == "advisor_socratic_memo"
        return _MEMO_BODY

    monkeypatch.setattr(socratic_mod, "call_llm", fake_llm)
    questions = ["Your read?", "Horizon?", "What breaks it?"]
    answers = ["Market misses deposit franchise", "3+ years", "NPL inflection past 8%"]
    result = generate_decision_memo(
        tmp_path,
        "NU",
        questions=questions,
        answers=answers,
        horizon_days=180,
        ctx=_ctx(tmp_path),
    )
    assert result.ok and result.memo_id is not None
    # The owner's answers reached the prompt.
    prompt = str(seen["prompt"])
    assert "Market misses deposit franchise" in prompt and "180 days" in prompt

    memo = get_memo(result.memo_id, db_path=db)
    assert memo is not None
    assert memo.kind == "socratic" and memo.ticker == "NU"
    assert memo.stance == "add" and memo.horizon_days == 180
    assert memo.score_status == "pending"
    # Q&A transcript rides the body; context carries the structured pair.
    assert "Think-through transcript" in memo.body_md
    assert "A1: Market misses deposit franchise" in memo.body_md
    assert memo.context is not None and memo.context["answers"] == answers
    # Ticker-scoped: ledger entry written + backlinked.
    assert memo.ledger_entry_id is not None
    entries = list_entries(user_id="bhanu", ticker="NU", db_path=db)
    assert any(e.id == memo.ledger_entry_id and e.entry_kind == "advisor_memo" for e in entries)


def test_decision_memo_validates_owner_first_inputs(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(ValueError, match="questions but"):
        generate_decision_memo(tmp_path, "NU", questions=["a?"], answers=[], ctx=ctx)
    with pytest.raises(ValueError, match="owner-first"):
        generate_decision_memo(tmp_path, "NU", questions=["a?", "b?"], answers=["", "  "], ctx=ctx)
    with pytest.raises(ValueError, match="horizon_days"):
        generate_decision_memo(
            tmp_path, "NU", questions=["a?"], answers=["x"], horizon_days=0, ctx=ctx
        )


def test_decision_memo_missing_stance_still_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _build_db(tmp_path)

    def no_stance(*a: object, **k: object) -> str:
        return "## Bull\nfine\n## Stance if forced\nunclear"

    monkeypatch.setattr(socratic_mod, "call_llm", no_stance)
    result = generate_decision_memo(
        tmp_path, "NU", questions=["q?"], answers=["a"], ctx=_ctx(tmp_path)
    )
    assert result.ok and result.memo_id is not None
    memo = get_memo(result.memo_id, db_path=db)
    assert memo is not None and memo.stance is None  # unscoreable for direction, still recorded


def test_decision_memo_transient_vs_hard_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _build_db(tmp_path)
    ctx = _ctx(tmp_path)

    def transient(*a: object, **k: object) -> str:
        raise TimeoutError("CLI timeout")

    monkeypatch.setattr(socratic_mod, "call_llm", transient)
    result = generate_decision_memo(tmp_path, "NU", questions=["q?"], answers=["a"], ctx=ctx)
    assert not result.ok and "TimeoutError" in (result.skipped_reason or "")

    def hard(*a: object, **k: object) -> str:
        raise LLMSetupError("claude missing")

    monkeypatch.setattr(socratic_mod, "call_llm", hard)
    with pytest.raises(LLMSetupError):
        generate_decision_memo(tmp_path, "NU", questions=["q?"], answers=["a"], ctx=ctx)


# --------------------------------------------------------------------------- #
# Server seams + panel
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FlaskClient:
    _build_db(tmp_path)

    # The flow builds its context against the test repo root; keep it offline-
    # deterministic by mocking only the LLM boundary.
    def routed(prompt: str, **k: object) -> str:
        if k.get("purpose") == "advisor_socratic_questions":
            return "1. Your read?\n2. Horizon?\n3. What breaks it?"
        return _MEMO_BODY

    monkeypatch.setattr(socratic_mod, "call_llm", routed)
    app = comments_server.create_app(tmp_path)
    return app.test_client()


def test_socratic_routes_roundtrip(client: FlaskClient, tmp_path: Path) -> None:
    q = client.post("/api/socratic/questions", json={"ticker": "nu"})
    assert q.status_code == 200
    qp = q.get_json()
    assert qp["ticker"] == "NU" and len(qp["questions"]) == 3

    m = client.post(
        "/api/socratic/memo",
        json={
            "ticker": "NU",
            "questions": qp["questions"],
            "answers": ["read", "long", "NPL"],
            "horizon_days": 90,
        },
    )
    assert m.status_code == 200
    mp = m.get_json()
    assert mp["stance"] == "add" and mp["memo_id"] >= 1
    assert "Bull" in mp["body_html"]
    memo = get_memo(mp["memo_id"], db_path=tmp_path / "data" / "portfolio.db")
    assert memo is not None and memo.kind == "socratic"


def test_socratic_routes_validate(client: FlaskClient) -> None:
    assert client.post("/api/socratic/questions", json={}).status_code == 400
    assert (
        client.post(
            "/api/socratic/memo", json={"ticker": "NU", "questions": "x", "answers": []}
        ).status_code
        == 400
    )
    # length mismatch surfaces as a 400 from the generator's validation
    assert (
        client.post(
            "/api/socratic/memo",
            json={"ticker": "NU", "questions": ["a?"], "answers": ["x", "y"]},
        ).status_code
        == 400
    )


def test_socratic_standalone_page(client: FlaskClient) -> None:
    resp = client.get("/socratic/nu")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'data-autostart-ticker="NU"' in body
    assert "Socratic think-through" in body
    assert "/api/socratic/questions" in body  # the flow JS shipped with the page


def test_panel_carries_think_through_entry_and_stance_pill() -> None:
    memo = AdvisorMemoRow(
        id=9,
        user_id="bhanu",
        kind="socratic",
        ticker="NU",
        counter_ticker=None,
        title="Socratic think-through · NU · 2026-06-10",
        body_md="## Bull\n- x\n\nSTANCE: add",
        context=None,
        stance="add",
        horizon_days=180,
        score_status="pending",
        note_id=1,
        ledger_entry_id=2,
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
    )
    html = compose_memos_page([], [memo], holdings=["NU", "META"])
    assert 'id="am-soc-ticker"' in html and ">META<" in html
    assert "Think through" in html
    assert "stance: add · 180d" in html
    assert 'title="scoring pending"' in html
    # No holdings -> no selector, page still composes.
    bare = compose_memos_page([], [])
    assert 'id="am-soc-ticker"' not in bare


def test_render_socratic_page_is_self_contained() -> None:
    html = render_socratic_page("meli")
    assert html.startswith("<!doctype html>")
    assert 'data-autostart-ticker="MELI"' in html
    assert "soc-body" in html and "/api/socratic/memo" in html
