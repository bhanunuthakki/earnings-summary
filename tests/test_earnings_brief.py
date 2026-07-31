"""Pre-earnings brief generator (src/earnings_brief.py, owner ruling 2026-07-31).

Pins the token-discipline contract: scope (portfolio + marked evaluation),
the 7-day ER window, per-(ticker, er_date) idempotency via the llm_artifacts
input hash, the T-1 bounded refresh (≤2 calls per cycle), budget-skip, and
the per-item degrade posture (transient defers, hard stop propagates).
The LLM is always monkeypatched — zero real calls in this file.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

import earnings_brief
from earnings_brief import (
    BUDGET_SKIPPED,
    CACHE_HIT,
    DEFERRED_TRANSIENT,
    GENERATED,
    HELD_FOR_REFRESH_WINDOW,
    BriefCandidate,
    eligible_tickers,
    generate_all,
    generate_brief,
)
from llm_artifact_store import read_current

TODAY = date(2026, 8, 1)

_SCHEMA = """
CREATE TABLE tracked_companies (
    ticker TEXT NOT NULL, name TEXT, list_type TEXT NOT NULL, archived_at TIMESTAMP);
CREATE TABLE ticker_settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL,
    bypass_budget INTEGER NOT NULL DEFAULT 0,
    auto_pre_earnings_brief INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    CONSTRAINT uq_ticker_settings_ticker UNIQUE (ticker));
CREATE TABLE expected_earnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL,
    expected_date TEXT NOT NULL, detected_source TEXT,
    first_seen_at TEXT, last_seen_at TEXT);
CREATE TABLE llm_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT, scope TEXT NOT NULL DEFAULT 'ticker', purpose TEXT NOT NULL,
    fiscal_period TEXT, content_md TEXT, content_json TEXT,
    input_sha256 TEXT NOT NULL, output_sha256 TEXT, model TEXT,
    prompt_version TEXT NOT NULL DEFAULT 'v1',
    generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, expires_at TIMESTAMP,
    superseded_by_id INTEGER, dirty INTEGER NOT NULL DEFAULT 0, dirty_reason TEXT,
    source_doc_ids TEXT, parent_artifact_ids TEXT, llm_call_id INTEGER);
"""


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "brief.db"
    conn = sqlite3.connect(str(p))
    try:
        conn.executescript(_SCHEMA)
        conn.executemany(
            "INSERT INTO tracked_companies (ticker, list_type) VALUES (?, ?)",
            [
                ("NU", "portfolio"),  # held → always in scope
                ("WIX", "evaluation"),  # marked below → in scope
                ("ZZZ", "evaluation"),  # unmarked → NEVER in scope
                ("FAR", "portfolio"),  # held but ER outside window
            ],
        )
        conn.execute(
            "INSERT INTO ticker_settings (ticker, auto_pre_earnings_brief, created_at, "
            "updated_at) VALUES ('WIX', 1, 't', 't')"
        )
        conn.executemany(
            "INSERT INTO expected_earnings (ticker, expected_date) VALUES (?, ?)",
            [
                ("NU", "2026-08-05"),  # T-4
                ("WIX", "2026-08-02"),  # T-1
                ("ZZZ", "2026-08-03"),
                ("FAR", "2026-09-20"),  # outside the 7d window
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return p


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the LLM with a canned brief; returns the call log (tickers)."""
    calls: list[str] = []

    def _fake(prompt: str, **kwargs: object) -> str:
        calls.append(str(kwargs.get("ticker")))
        return "**What this quarter must show** — canned brief body."

    monkeypatch.setattr(earnings_brief, "call_llm", _fake)
    monkeypatch.setattr(earnings_brief, "should_skip_for_budget", lambda *a, **k: None)
    return calls


# ---------------------------------------------------------------------------
# Scope + window
# ---------------------------------------------------------------------------


def test_eligibility_scope_and_window(db: Path) -> None:
    got = eligible_tickers(db, today=TODAY)
    # Soonest first; WIX marked in, ZZZ unmarked out, FAR outside the window.
    assert [(c.ticker, c.er_date.isoformat(), c.days_until) for c in got] == [
        ("WIX", "2026-08-02", 1),
        ("NU", "2026-08-05", 4),
    ]


def test_eligibility_degrades_to_portfolio_only_without_settings_table(tmp_path: Path) -> None:
    p = tmp_path / "bare.db"
    conn = sqlite3.connect(str(p))
    try:
        conn.executescript(
            "CREATE TABLE tracked_companies (ticker TEXT, name TEXT, list_type TEXT, "
            "archived_at TIMESTAMP);"
            "CREATE TABLE expected_earnings (id INTEGER PRIMARY KEY, ticker TEXT, "
            "expected_date TEXT);"
        )
        conn.execute("INSERT INTO tracked_companies VALUES ('NU', 'Nu', 'portfolio', NULL)")
        conn.execute("INSERT INTO tracked_companies VALUES ('WIX', 'Wix', 'evaluation', NULL)")
        conn.executemany(
            "INSERT INTO expected_earnings (ticker, expected_date) VALUES (?, ?)",
            [("NU", "2026-08-05"), ("WIX", "2026-08-02")],
        )
        conn.commit()
    finally:
        conn.close()
    got = eligible_tickers(p, today=TODAY)
    # No opt-in store → the evaluation name is (loudly) out, the held name in.
    assert [c.ticker for c in got] == ["NU"]


# ---------------------------------------------------------------------------
# Idempotency + bounded refresh
# ---------------------------------------------------------------------------


def test_generate_persists_and_second_run_is_free(db: Path, fake_llm: list[str]) -> None:
    repo = db.parent
    tally = generate_all(db, repo, today=TODAY)
    assert tally[GENERATED] == 2
    assert fake_llm == ["WIX", "NU"]  # soonest first
    art = read_current(
        ticker="NU", purpose="pre_earnings_brief", fiscal_period="2026-08-05", db_path=db
    )
    assert art is not None
    assert "canned brief body" in (art.content_md or "")
    assert art.prompt_version  # stamped from the registry

    # Second run, nothing changed: pure cache hits, ZERO further LLM calls.
    tally2 = generate_all(db, repo, today=TODAY)
    assert tally2[CACHE_HIT] == 2
    assert tally2[GENERATED] == 0
    assert fake_llm == ["WIX", "NU"]


def test_input_drift_refreshes_only_inside_t1(db: Path, fake_llm: list[str]) -> None:
    repo = db.parent
    nu = BriefCandidate("NU", date(2026, 8, 5), 4)
    assert generate_brief(db, repo, nu, today=TODAY) == GENERATED

    # Drift the inputs (a new open watch item changes the assembled context).
    import earnings_brief as eb

    original = eb.assemble_context

    def _drifted(*args: object, **kwargs: object) -> list[str]:
        return [*original(*args, **kwargs), "## New watch item\n- NIM trajectory"]  # type: ignore[arg-type]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(eb, "assemble_context", _drifted)
        # Still T-4: the drift is HELD, no call burned.
        assert generate_brief(db, repo, nu, today=TODAY) == HELD_FOR_REFRESH_WINDOW
        # At T-1 the same drift regenerates — the one allowed refresh.
        nu_t1 = BriefCandidate("NU", date(2026, 8, 5), 1)
        assert generate_brief(db, repo, nu_t1, today=date(2026, 8, 4)) == GENERATED
    assert fake_llm == ["NU", "NU"]  # exactly 2 calls for the whole cycle


def test_force_regenerates(db: Path, fake_llm: list[str]) -> None:
    repo = db.parent
    nu = BriefCandidate("NU", date(2026, 8, 5), 4)
    assert generate_brief(db, repo, nu, today=TODAY) == GENERATED
    assert generate_brief(db, repo, nu, today=TODAY, force=True) == GENERATED
    assert fake_llm == ["NU", "NU"]


# ---------------------------------------------------------------------------
# Degrade posture
# ---------------------------------------------------------------------------


def test_transient_failure_defers_that_ticker_only(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def _flaky(prompt: str, **kwargs: object) -> str:
        t = str(kwargs.get("ticker"))
        calls.append(t)
        if t == "WIX":
            raise TimeoutError("transient CLI timeout")
        return "brief body"

    monkeypatch.setattr(earnings_brief, "call_llm", _flaky)
    monkeypatch.setattr(earnings_brief, "should_skip_for_budget", lambda *a, **k: None)
    tally = generate_all(db, db.parent, today=TODAY)
    assert tally[DEFERRED_TRANSIENT] == 1
    assert tally[GENERATED] == 1
    # The deferred name persisted nothing — it IS the retry queue.
    assert (
        read_current(
            ticker="WIX", purpose="pre_earnings_brief", fiscal_period="2026-08-02", db_path=db
        )
        is None
    )


def test_empty_response_is_transient_and_never_persisted(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(earnings_brief, "call_llm", lambda prompt, **k: "   ")
    monkeypatch.setattr(earnings_brief, "should_skip_for_budget", lambda *a, **k: None)
    tally = generate_all(db, db.parent, today=TODAY)
    assert tally[DEFERRED_TRANSIENT] == 2
    assert tally[GENERATED] == 0


def test_hard_stop_propagates(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from llm.cli import LLMSetupError

    def _hard(prompt: str, **kwargs: object) -> str:
        raise LLMSetupError("no CLI on PATH")

    monkeypatch.setattr(earnings_brief, "call_llm", _hard)
    monkeypatch.setattr(earnings_brief, "should_skip_for_budget", lambda *a, **k: None)
    with pytest.raises(LLMSetupError):
        generate_all(db, db.parent, today=TODAY)


def test_budget_skip_stops_before_any_call(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(earnings_brief, "call_llm", lambda prompt, **k: calls.append("x") or "body")
    monkeypatch.setattr(earnings_brief, "should_skip_for_budget", lambda *a, **k: object())
    tally = generate_all(db, db.parent, today=TODAY)
    assert tally[BUDGET_SKIPPED] == 1
    assert calls == []
