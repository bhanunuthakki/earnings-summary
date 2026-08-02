"""Tests for the Socratic think-through (master build P2.4; step 1
backgrounded wave3b Task 4).

Layers:
* the parsers (questions list, forced-stance line) — strict where the flow
  must fail visibly, tolerant where the memo should persist anyway,
* question + memo generation with the LLM mocked — owner-first validation,
  the persistence contract (stance + horizon on the memo row, ledger entry,
  Q&A transcript appended), transient-degrade vs hard-stop-propagate,
* the prelude persistence round-trip (``persist_prelude`` /
  ``read_current_prelude``) the background job and the result-read route
  share,
* the server seams — the job-starting action, the result-read route, the
  synchronous memo endpoint, and the standalone page — over an alembic-built
  DB, with a non-spawning job registry (no real subprocess),
* the Memos panel's think-through entry + stance pill.
"""

from __future__ import annotations

import sqlite3
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
from advisor.context import AdvisorContext, TickerValuation, calibration_block  # noqa: E402
from advisor.socratic import (  # noqa: E402
    SocraticPrelude,
    generate_decision_memo,
    generate_questions,
    parse_questions,
    parse_stance,
    persist_prelude,
    read_current_prelude,
)
from advisor.store import AdvisorMemoRow, get_memo  # noqa: E402
from decision_calibration import CalibrationStats, ConvictionBucket  # noqa: E402
from dispatch_registry import Registry  # noqa: E402
from integrations.portfolio_tracker_client import (  # noqa: E402
    LivePortfolio,
    PortfolioAnalytics,
)
from llm.cli import LLMSetupError  # noqa: E402
from pipeline.advisor_memos_panel import compose_memos_page, render_socratic_page  # noqa: E402
from pipeline.allocation_decisions_panel import SizingAuditRow  # noqa: E402
from user_state.ledger import list_entries  # noqa: E402


class _NonSpawningRegistry(Registry):
    """Records job starts without forking a real subprocess (same pattern as
    tests/test_advisor_memos.py) — the route tests below only need to assert
    that a job got registered, not that ``run_socratic_questions.py``
    actually completes end to end."""

    def start(self, *, ticker, kind, argv, spawn=True):  # type: ignore[override]
        return super().start(ticker=ticker, kind=kind, argv=argv, spawn=False)


_PRIOR_HEAD = "0059_kpi_facts_restatement"

# ``command.stamp(_PRIOR_HEAD)`` marks that revision current WITHOUT running
# 0001..0059 — so ``llm_artifacts`` (created in 0035, well before the stamp)
# never actually gets created by this fixture's upgrade-to-head, even though
# a REAL `alembic upgrade head` run always creates it. Only wave3b Task 4's
# prelude-persistence tests below exercise it through this fixture; every
# other DB-backed test in this file predates that need. Idempotent
# CREATE-IF-NOT-EXISTS closes the gap without touching the stamp/upgrade
# shortcut itself (which many tests below rely on staying fast).
_LLM_ARTIFACTS_DDL = """
CREATE TABLE IF NOT EXISTS llm_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16),
    scope VARCHAR(64) NOT NULL DEFAULT 'ticker',
    purpose VARCHAR(64) NOT NULL,
    fiscal_period VARCHAR(10),
    content_md TEXT,
    content_json TEXT,
    input_sha256 VARCHAR(64) NOT NULL,
    output_sha256 VARCHAR(64),
    model VARCHAR(64),
    prompt_version VARCHAR(32) NOT NULL DEFAULT 'v1',
    generated_at DATETIME NOT NULL,
    expires_at DATETIME,
    superseded_by_id INTEGER,
    dirty BOOLEAN NOT NULL DEFAULT 0,
    dirty_reason VARCHAR(128),
    source_doc_ids TEXT,
    parent_artifact_ids TEXT,
    llm_call_id INTEGER
)
"""


def _build_db(tmp_path: Path) -> Path:
    db = tmp_path / "data" / "portfolio.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.stamp(cfg, _PRIOR_HEAD)
    command.upgrade(cfg, "head")
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(_LLM_ARTIFACTS_DDL)
        conn.commit()
    finally:
        conn.close()
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
# Calibration injection (L1 — the calibration return path into the advisor)
# --------------------------------------------------------------------------- #


def _calib(*, high_hit: float = 0.4, high_n: int = 10) -> CalibrationStats:
    correct = round(high_hit * high_n)
    return CalibrationStats(
        total=high_n + 2,
        graded=high_n,
        overall_hit_rate=high_hit if high_n else None,
        by_conviction=[
            ConvictionBucket(
                conviction="high",
                graded=high_n,
                correct=correct,
                wrong=high_n - correct,
                mixed=0,
                ungraded=0,
                hit_rate=high_hit if high_n else None,
            )
        ],
        action_mix={},
        reversals=[],
        reversals_vindicated=2,
        reversals_cost=1,
        time_to_outcome=[],
    )


def test_calibration_block_challenges_the_cohort(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, "NU")  # the audit row rates NU 4/5 → high cohort
    ctx.calibration = _calib(high_hit=0.4, high_n=10)
    block = calibration_block(ctx, "NU")
    assert "challenge this conviction against your own" in block.lower()
    assert "graded calls 40% correct (n=10)" in block
    assert "NU is a high-conviction name (you rate it 4/5)" in block
    # L-seam 3: the focus-cohort rate is now widened with its Wilson 95% CI.
    assert "your high-conviction calls have graded 40% correct (95% CI 17-69%, n=10)" in block
    assert "why THIS one is different" in block
    assert "2 vindicated vs 1 cost" in block


def test_calibration_block_surfaces_brier_and_expectancy(tmp_path: Path) -> None:
    import dataclasses

    from decision_calibration import ConvictionCalibration, ConvictionReliabilityRow, Expectancy

    ctx = _ctx(tmp_path, "NU")
    # L-seam 2: a Brier that beats the base-rate baseline; L-seam 1: winners
    # bigger than losers. CalibrationStats is frozen → replace, don't mutate.
    ctx.calibration = dataclasses.replace(
        _calib(high_hit=0.4, high_n=12),
        conviction_calibration=ConvictionCalibration(
            n=12,
            brier=0.18,
            baseline_brier=0.24,
            base_rate=0.4,
            rows=[ConvictionReliabilityRow(conviction="high", predicted=0.75, observed=0.4, n=12)],
        ),
        expectancy=Expectancy(
            n=12,
            wins=5,
            losses=7,
            avg_win=4000.0,
            avg_loss=1000.0,
            slugging=4.0,
            expectancy=83.0,
            total=996.0,
        ),
    )
    block = calibration_block(ctx, "NU")
    assert "Conviction Brier 0.180 vs 0.240 baseline (n=12)" in block
    assert "discriminates" in block
    assert "slugging 4.0x" in block
    assert "realized alpha/call" in block


def test_calibration_block_empty_when_nothing_graded(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, "NU")
    assert calibration_block(ctx, "NU") == ""  # default calibration is None
    ctx.calibration = _calib(high_n=0)
    assert calibration_block(ctx, "NU") == ""  # graded == 0 → stay silent


def test_socratic_prompts_carry_the_calibration_challenge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts: list[str] = []

    def fake_llm(prompt: str, **_kwargs: object) -> str:
        prompts.append(prompt)
        return "1. read?\n2. horizon?\n3. wrong?"

    monkeypatch.setattr(socratic_mod, "call_llm", fake_llm)
    ctx = _ctx(tmp_path, "NU")
    ctx.calibration = _calib(high_hit=0.4, high_n=10)
    generate_questions(tmp_path, "NU", ctx=ctx)
    # The block reached the prompt, and the prompt instructs the model to use it.
    assert "your high-conviction calls have graded 40% correct (95% CI 17-69%, n=10)" in prompts[0]
    assert "documented calibration" in prompts[0]


def test_questions_prompt_carries_the_premortem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L8 item d: the decision-moment pre-mortem (calibration_coach) rides into
    the questions prompt next to the calibration challenge — the L1 injection
    point extended."""
    import calibration_coach

    prompts: list[str] = []

    def fake_llm(prompt: str, **_kwargs: object) -> str:
        prompts.append(prompt)
        return "1. read?\n2. horizon?\n3. wrong?"

    monkeypatch.setattr(socratic_mod, "call_llm", fake_llm)
    monkeypatch.setattr(
        calibration_coach,
        "premortem_block",
        lambda *_a, **_k: "**Pre-mortem:**\n1. like RBRK, you size up before the print",
    )
    generate_questions(tmp_path, "NU", ctx=_ctx(tmp_path))
    assert "like RBRK, you size up before the print" in prompts[0]
    assert "Pre-mortem" in prompts[0] and "strongest parallel" in prompts[0].lower()


def test_questions_prompt_degrades_without_premortem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A thin/absent pre-mortem must not break the flow — the slot fills with the
    honest placeholder and the questions still generate."""
    import calibration_coach

    prompts: list[str] = []
    monkeypatch.setattr(
        socratic_mod, "call_llm", lambda p, **_k: prompts.append(p) or "1. a?\n2. b?\n3. c?"
    )
    monkeypatch.setattr(calibration_coach, "premortem_block", lambda *_a, **_k: "")
    prelude = generate_questions(tmp_path, "NU", ctx=_ctx(tmp_path))
    assert len(prelude.questions) == 3
    assert "no pre-mortem" in prompts[0]


# --------------------------------------------------------------------------- #
# Prelude persistence (wave3b Task 4 — the background job's result channel)
# --------------------------------------------------------------------------- #


def test_persist_and_read_current_prelude_roundtrip(tmp_path: Path) -> None:
    db = _build_db(tmp_path)
    prelude = SocraticPrelude(
        ticker="NU", questions=["Your read?", "Horizon?", "What breaks it?"], context_block="ctx"
    )
    artifact_id = persist_prelude(db, prelude)
    assert artifact_id is not None

    got = read_current_prelude(db, "nu")
    assert got is not None
    assert got.ticker == "NU"
    assert got.questions == prelude.questions
    assert got.context_block == "ctx"


def test_read_current_prelude_none_before_any_run(tmp_path: Path) -> None:
    db = _build_db(tmp_path)
    assert read_current_prelude(db, "NU") is None


def test_persist_prelude_each_call_lands_a_fresh_row(tmp_path: Path) -> None:
    """Each generate click is a deliberate new LLM spend — never served
    stale from an artifact cache keyed only on ticker."""
    db = _build_db(tmp_path)
    persist_prelude(db, SocraticPrelude(ticker="NU", questions=["a?", "b?", "c?"], context_block="1"))
    persist_prelude(
        db, SocraticPrelude(ticker="NU", questions=["x?", "y?", "z?"], context_block="2")
    )
    got = read_current_prelude(db, "NU")
    assert got is not None and got.questions == ["x?", "y?", "z?"]  # the LATEST run wins


def test_run_socratic_questions_script_persists_prelude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The background job's actual entrypoint
    (execution/run_socratic_questions.py), exercised in-process (no real
    subprocess spawn). ``generate_questions`` itself is mocked at the module
    level it's imported from — the script's OWN job is the persist step, not
    re-proving generate_questions' grounding (already covered above); this
    also keeps the test offline (no tracker/network round-trip)."""
    db = _build_db(tmp_path)

    def fake_generate(repo_root: Path, ticker: str, **_kwargs: object) -> SocraticPrelude:
        return SocraticPrelude(ticker=ticker.upper(), questions=["a?", "b?", "c?"], context_block="c")

    monkeypatch.setattr(socratic_mod, "generate_questions", fake_generate)

    import run_socratic_questions

    exit_code = run_socratic_questions.main(["NU", "--repo-root", str(tmp_path)])
    assert exit_code == 0

    prelude = read_current_prelude(db, "NU")
    assert prelude is not None and len(prelude.questions) == 3


def test_run_socratic_questions_script_exit_1_on_transient_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient generation failure (matches parse_questions' ValueError on
    an unparseable completion, or any other non-hard-stop) must fail the job
    visibly (exit 1) rather than persist a garbage/empty prelude — the owner
    retries from the page."""

    def failing(repo_root: Path, ticker: str, **_kwargs: object) -> SocraticPrelude:
        raise ValueError("expected >= 3 questions, parsed 1")

    monkeypatch.setattr(socratic_mod, "generate_questions", failing)

    import run_socratic_questions

    exit_code = run_socratic_questions.main(["NU", "--repo-root", str(tmp_path)])
    assert exit_code == 1
    assert read_current_prelude(tmp_path / "data" / "portfolio.db", "NU") is None


def test_run_socratic_questions_script_exit_1_when_persistence_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``persist_prelude`` returning ``None`` (DB unavailable —
    llm_artifact_store.upsert's own documented degrade) must ALSO fail the
    job: persistence is this job's deliverable, not a side effect, since the
    page has no other way to read a background job's result back."""

    def fake_generate(repo_root: Path, ticker: str, **_kwargs: object) -> SocraticPrelude:
        return SocraticPrelude(ticker=ticker.upper(), questions=["a?", "b?", "c?"], context_block="c")

    monkeypatch.setattr(socratic_mod, "generate_questions", fake_generate)
    monkeypatch.setattr(socratic_mod, "persist_prelude", lambda *_a, **_k: None)

    import run_socratic_questions

    exit_code = run_socratic_questions.main(["NU", "--repo-root", str(tmp_path)])
    assert exit_code == 1


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
    # Task 4 (wave3b): Step 1 now runs through the jobs registry as a real
    # subprocess (execution/run_socratic_questions.py) — the non-spawning
    # registry records the job without forking, matching
    # tests/test_advisor_memos.py's pattern for every other job-backed route.
    app = comments_server.create_app(tmp_path, registry=_NonSpawningRegistry())
    return app.test_client()


def test_socratic_questions_action_starts_job(client: FlaskClient) -> None:
    """Task 4: the route no longer blocks — it registers a background job
    and returns immediately, same shape as every other ``/actions/<name>``
    starter (advisor-memo, position-review, ...)."""
    resp = client.post("/actions/socratic-questions", json={"ticker": "nu"})
    assert resp.status_code == 201
    payload = resp.get_json()
    assert payload["ticker"] == "NU"
    assert payload["kind"] == "socratic-questions"
    assert payload["stream_url"].startswith("/actions/stream/")


def test_socratic_questions_result_route_roundtrip(client: FlaskClient, tmp_path: Path) -> None:
    """The GET-by-ticker result route reads back exactly what the background
    job would have persisted (simulated here via ``persist_prelude`` directly
    — the job itself is exercised end to end by
    ``test_run_socratic_questions_script_persists_prelude`` below), and 404s
    before anything has been generated."""
    db_path = tmp_path / "data" / "portfolio.db"
    assert client.get("/api/socratic/questions/NU").status_code == 404

    persist_prelude(
        db_path,
        SocraticPrelude(
            ticker="NU", questions=["Your read?", "Horizon?", "What breaks it?"], context_block="ctx"
        ),
    )
    resp = client.get("/api/socratic/questions/nu")
    assert resp.status_code == 200
    qp = resp.get_json()
    assert qp["ticker"] == "NU" and len(qp["questions"]) == 3


def test_socratic_memo_route_roundtrip(client: FlaskClient, tmp_path: Path) -> None:
    """Step 2 (the memo POST) is unchanged — synchronous, no job involved."""
    m = client.post(
        "/api/socratic/memo",
        json={
            "ticker": "NU",
            "questions": ["Your read?", "Horizon?", "What breaks it?"],
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
    assert client.post("/actions/socratic-questions", json={}).status_code == 400
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
    """Task 4: the page no longer blocks on load — it reveals the flow with
    an honest-cost button rather than firing a synchronous fetch()."""
    resp = client.get("/socratic/nu")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'data-autostart-ticker="NU"' in body
    assert "Socratic think-through" in body
    assert "Generate 3 questions" in body and "~2 min" in body  # the honest cost label
    assert "/actions/socratic-questions" in body  # starts the background job
    assert "/api/socratic/questions/" in body  # reads the persisted result back
    assert 'href="/#portfolio_record"' in body  # the fixed back-link (was the stale #advisor_memos)
    assert "Portfolio &rarr; Record" in body


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
