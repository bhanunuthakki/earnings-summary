"""Tests for the Red Team engine (src/redteam/) — lens rotation, idempotency,
per-item degrade (transient defer vs. hard-stop propagate), dry-run (zero LLM
calls), and cross-book skip-on-empty-input. No live LLM calls anywhere in this
file — every ``call_llm_structured`` site is monkeypatched.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from llm.cli import LLMBudgetExceeded  # noqa: E402
from llm.structured import StructuredParseError  # noqa: E402
from redteam import cross_book, engine, lenses, store  # noqa: E402
from redteam.models import RedTeamLLMItem  # noqa: E402

_HOLDING_JSON: dict[str, object] = {
    "ticker": "NU",
    "name": "Nu Holdings",
    "thesis": "Digital bank thesis text.",
    "verdict": "Intact",
    "key_driver": "Customer x ARPAC x NIM",
    "tier_1_kpis": [],
}


def _make_repo_root(tmp_path: Path, tickers_weights: dict[str, float]) -> Path:
    repo_root = tmp_path / "repo"
    (repo_root / "micro_thesis" / "holdings").mkdir(parents=True)
    (repo_root / "data").mkdir(parents=True)
    for ticker in tickers_weights:
        payload: dict[str, object] = dict(_HOLDING_JSON)
        payload["ticker"] = ticker
        (repo_root / "micro_thesis" / "holdings" / f"{ticker}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
    (repo_root / "data" / "portfolio_weights.json").write_text(
        json.dumps({"computed_at": "2026-08-01T00:00:00", "weights": tickers_weights}),
        encoding="utf-8",
    )
    return repo_root


def _make_db(tmp_path: Path, name: str = "test.db") -> Path:
    """A minimal SQLite DB carrying just ``red_team_items`` — the store layer
    doesn't need the rest of the app schema, and building the real DB via
    Alembic for every test would be needlessly slow (that round-trip is
    covered by tests/test_migration_0147_red_team_items.py)."""
    db_path = tmp_path / name
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE red_team_items (
            id INTEGER PRIMARY KEY,
            run_key TEXT NOT NULL,
            ticker TEXT,
            lens TEXT NOT NULL,
            kind TEXT NOT NULL,
            attack_md TEXT NOT NULL,
            question_md TEXT NOT NULL,
            proposed_change_md TEXT NOT NULL,
            severity TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            defer_count INTEGER NOT NULL DEFAULT 0,
            response_md TEXT,
            responded_at TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()
    return db_path


_STUB_ITEM: dict[str, object] = {
    "attack_md": "Stub attack.",
    "question_md": "Stub question?",
    "proposed_change_md": "Stub proposed change.",
    "severity": "med",
}


def _stub_ok(*_args: object, **_kwargs: object) -> dict[str, object]:
    """A typed ``call_llm_structured`` stub returning a valid item payload —
    used everywhere a test needs a lens/pass to succeed without a live call."""
    return dict(_STUB_ITEM)


# ---------------------------------------------------------------------------
# Lens rotation
# ---------------------------------------------------------------------------


def test_lens_rotation_is_deterministic() -> None:
    a = lenses.lens_for("NU", 100)
    b = lenses.lens_for("NU", 100)
    assert a == b
    assert a in lenses.LENS_NAMES if hasattr(lenses, "LENS_NAMES") else True


def test_lens_rotation_never_repeats_consecutive_months() -> None:
    for ticker in ("NU", "MELI", "WIX", "RBRK", "BN"):
        for month_index in range(0, 24):
            this_lens = lenses.lens_for(ticker, month_index)
            next_lens = lenses.lens_for(ticker, month_index + 1)
            assert this_lens != next_lens


def test_month_index_increases_monotonically_across_year_boundary() -> None:
    assert lenses.month_index_for("2026-12") + 1 == lenses.month_index_for("2027-01")


# ---------------------------------------------------------------------------
# Dry-run: zero LLM calls
# ---------------------------------------------------------------------------


def test_dry_run_makes_zero_llm_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = _make_repo_root(tmp_path, {"NU": 0.10, "MELI": 0.08})
    db_path = _make_db(tmp_path)

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("call_llm_structured must never be called in --dry-run")

    monkeypatch.setattr(lenses, "call_llm_structured", _boom)
    monkeypatch.setattr(cross_book, "call_llm_structured", _boom)

    result = engine.run_red_team(repo_root=repo_root, db_path=db_path, month="2026-08", dry_run=True)

    assert result.dry_run is True
    assert not result.already_done
    assert sorted(result.held_tickers) == ["MELI", "NU"]
    assert result.tally.get("would_generate", 0) == 2
    assert result.first_prompt is not None
    assert store.list_items_for_run(db_path=db_path, run_key=result.run_key) == []


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_idempotency_skips_unless_forced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = _make_repo_root(tmp_path, {"NU": 0.10})
    db_path = _make_db(tmp_path)

    calls = {"n": 0}

    def _stub(*_a: object, **_k: object) -> dict[str, object]:
        calls["n"] += 1
        return dict(_STUB_ITEM)

    monkeypatch.setattr(lenses, "call_llm_structured", _stub)
    monkeypatch.setattr(cross_book, "call_llm_structured", _stub)

    first = engine.run_red_team(repo_root=repo_root, db_path=db_path, month="2026-08")
    assert not first.already_done
    assert calls["n"] >= 1
    n_after_first = calls["n"]

    second = engine.run_red_team(repo_root=repo_root, db_path=db_path, month="2026-08")
    assert second.already_done
    assert calls["n"] == n_after_first  # no new LLM calls on the skipped re-run

    third = engine.run_red_team(repo_root=repo_root, db_path=db_path, month="2026-08", force=True)
    assert not third.already_done
    assert calls["n"] > n_after_first  # --force re-generates


# ---------------------------------------------------------------------------
# Per-item degrade: transient defers + continues, hard stop propagates
# ---------------------------------------------------------------------------


def test_transient_failure_defers_that_item_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _make_repo_root(tmp_path, {"AAA": 0.10, "BBB": 0.08})
    db_path = _make_db(tmp_path)

    def _flaky(prompt: str, *, ticker: str | None = None, **_k: object) -> dict[str, object]:
        if ticker == "AAA":
            raise RuntimeError("transient: claude -p exit 1")
        return dict(_STUB_ITEM)

    monkeypatch.setattr(lenses, "call_llm_structured", _flaky)
    monkeypatch.setattr(cross_book, "call_llm_structured", _stub_ok)

    result = engine.run_red_team(repo_root=repo_root, db_path=db_path, month="2026-08")

    assert result.tally.get("deferred_transient", 0) == 1
    assert result.tally.get("generated", 0) >= 1  # BBB (and cross-book passes that fired) succeeded
    items = store.list_items_for_run(db_path=db_path, run_key=result.run_key)
    tickers_persisted = {i.ticker for i in items if i.kind == "per_name"}
    assert "AAA" not in tickers_persisted
    assert "BBB" in tickers_persisted


def test_hard_stop_propagates_and_halts_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _make_repo_root(tmp_path, {"AAA": 0.10})
    db_path = _make_db(tmp_path)

    def _budget_blown(*_a: object, **_k: object) -> object:
        raise LLMBudgetExceeded("red_team_attack: monthly cap exceeded")

    monkeypatch.setattr(lenses, "call_llm_structured", _budget_blown)

    with pytest.raises(LLMBudgetExceeded):
        engine.run_red_team(repo_root=repo_root, db_path=db_path, month="2026-08")


def test_parse_failure_on_both_attempts_tallies_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = _make_repo_root(tmp_path, {"AAA": 0.10})
    db_path = _make_db(tmp_path)

    def _unparseable(*_a: object, **_k: object) -> object:
        raise StructuredParseError("bad json twice", raw_head="not json")

    monkeypatch.setattr(lenses, "call_llm_structured", _unparseable)
    monkeypatch.setattr(cross_book, "call_llm_structured", _stub_ok)

    result = engine.run_red_team(repo_root=repo_root, db_path=db_path, month="2026-08")
    assert result.tally.get("parse_failed", 0) == 1
    per_name_items = [
        i for i in store.list_items_for_run(db_path=db_path, run_key=result.run_key) if i.kind == "per_name"
    ]
    assert per_name_items == []


def test_missing_thesis_json_is_skipped_not_fabricated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "micro_thesis" / "holdings").mkdir(parents=True)
    (repo_root / "data").mkdir(parents=True)
    # NU has no holdings JSON on file at all.
    (repo_root / "data" / "portfolio_weights.json").write_text(
        json.dumps({"computed_at": "2026-08-01T00:00:00", "weights": {"NU": 0.10}}),
        encoding="utf-8",
    )
    db_path = _make_db(tmp_path)
    monkeypatch.setattr(lenses, "call_llm_structured", _stub_ok)
    monkeypatch.setattr(cross_book, "call_llm_structured", _stub_ok)

    result = engine.run_red_team(repo_root=repo_root, db_path=db_path, month="2026-08")
    assert result.tally.get("no_thesis", 0) == 1
    assert result.tally.get("generated", 0) == 0 or all(
        i.kind == "cross_book" for i in store.list_items_for_run(db_path=db_path, run_key=result.run_key)
    )


# ---------------------------------------------------------------------------
# Held-name filter: weight floor + cash-like/index-ETF exclusion
# ---------------------------------------------------------------------------


def test_held_tickers_excludes_cash_likes_index_etfs_and_sub_floor_weights(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo_root(
        tmp_path,
        {
            "NU": 0.10,
            "SGOV": 0.05,
            "CUR:USD": 0.01,
            "VTI": 0.20,
            "SPY": 0.05,
            "TINY": 0.001,  # below the 0.5% floor
        },
    )
    held = engine.held_tickers(repo_root)
    assert held == {"NU": 0.10}


# ---------------------------------------------------------------------------
# Cross-book: skip on empty input, no LLM call
# ---------------------------------------------------------------------------


def test_factor_block_prompt_none_when_no_clusters() -> None:
    assert cross_book.factor_block_prompt(Path("."), clusters=[], weights={}) is None


def test_style_drift_prompt_none_when_no_usable_beta() -> None:
    assert (
        cross_book.style_drift_prompt(legs=[("Value", "VTV-VUG", None)], strategy_summary="x")
        is None
    )


def test_human_capital_prompt_none_when_no_ai_linked_holding() -> None:
    assert cross_book.human_capital_prompt(weights={"NU": 0.1, "MELI": 0.1}) is None


def test_human_capital_prompt_fires_on_ai_linked_holding() -> None:
    prompt = cross_book.human_capital_prompt(weights={"META": 0.1, "NU": 0.1})
    assert prompt is not None
    assert "META" in prompt


# ---------------------------------------------------------------------------
# RedTeamLLMItem schema validation
# ---------------------------------------------------------------------------


def test_redteam_llm_item_rejects_bad_severity() -> None:
    with pytest.raises(Exception):
        RedTeamLLMItem.model_validate(
            {
                "attack_md": "a",
                "question_md": "q",
                "proposed_change_md": "p",
                "severity": "extreme",
            }
        )
