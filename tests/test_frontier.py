"""Tests for the frontier-research overlay (src/llm/frontier.py) and the
model_ladder PR3 additions (backend_for, ladder_sha, JUDGE_POOL). All LLM calls
are DI'd fakes — the suite never spends."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from llm.frontier import (
    FRONTIER_PURPOSE,
    frontier_sha,
    load_candidate_models,
    merged_cheaper_candidates,
    promise_of,
    run_frontier_research,
)
from llm.model_ladder import JUDGE_POOL, backend_for, cheaper_candidates, ladder_sha


def _db(tmp_path: Path) -> Path:
    db_path = tmp_path / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE candidate_models (
            model_id TEXT PRIMARY KEY, family TEXT NOT NULL,
            input_usd_per_mtok REAL NOT NULL, output_usd_per_mtok REAL NOT NULL,
            promise REAL NOT NULL DEFAULT 0.5,
            source TEXT NOT NULL DEFAULT 'frontier_research',
            status TEXT NOT NULL DEFAULT 'active',
            source_url TEXT, notes TEXT, research_run_id TEXT,
            first_seen_at TEXT NOT NULL, verified_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()
    return db_path


def _seed_candidate(
    db_path: Path,
    model_id: str,
    *,
    family: str = "openrouter",
    input_usd: float = 0.2,
    output_usd: float = 0.4,
    promise: float = 0.8,
    status: str = "active",
) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO candidate_models (model_id, family, input_usd_per_mtok,"
        " output_usd_per_mtok, promise, status, first_seen_at, verified_at)"
        " VALUES (?, ?, ?, ?, ?, ?, '2026-07-01T00:00:00', '2026-07-01T00:00:00')",
        (model_id, family, input_usd, output_usd, promise, status),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# model_ladder additions
# ---------------------------------------------------------------------------


def test_backend_for_known_families_and_slug_convention() -> None:
    assert backend_for("claude-sonnet-4-6") == "claude"
    assert backend_for("gemini-3-flash-preview") == "gemini"
    assert backend_for("deepseek/deepseek-chat") == "openrouter"  # static ladder
    # Frontier-DISCOVERED slug (not in the static ladder) still dispatches right.
    assert backend_for("mistralai/some-new-model") == "openrouter"
    assert backend_for("claude-future-99") == "claude"  # unknown non-slug -> claude


def test_ladder_sha_is_deterministic() -> None:
    assert ladder_sha() == ladder_sha()
    assert len(ladder_sha()) == 64


def test_judge_pool_spans_two_families() -> None:
    assert set(JUDGE_POOL) >= {"claude", "gemini"}
    assert "openrouter" not in JUDGE_POOL  # candidates, not judges, until certified


# ---------------------------------------------------------------------------
# Overlay reads + the merged pool
# ---------------------------------------------------------------------------


def test_load_candidate_models_active_only(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    _seed_candidate(db_path, "prov/live-model")
    _seed_candidate(db_path, "prov/retired-model", status="retired")
    models = load_candidate_models(db_path)
    assert set(models) == {"prov/live-model"}
    assert promise_of(db_path, "prov/live-model") == 0.8
    assert promise_of(db_path, "claude-sonnet-4-6") == 0.5  # static -> neutral


def test_merged_cheaper_candidates_unions_overlay(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    _seed_candidate(db_path, "prov/cheap-model", input_usd=0.05, output_usd=0.10)
    merged = merged_cheaper_candidates(db_path, "claude-sonnet-4-6", include_openrouter=True)
    static = cheaper_candidates("claude-sonnet-4-6", include_openrouter=True)
    assert set(merged) == set(static) | {"prov/cheap-model"}
    # Cheapest-first: the $0.06-blended overlay row leads.
    assert merged[0] == "prov/cheap-model"


def test_merged_pool_respects_openrouter_opt_out(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    _seed_candidate(db_path, "prov/cheap-model", input_usd=0.05, output_usd=0.10)
    merged = merged_cheaper_candidates(db_path, "claude-sonnet-4-6", include_openrouter=False)
    assert "prov/cheap-model" not in merged
    assert merged == cheaper_candidates("claude-sonnet-4-6", include_openrouter=False)


def test_merged_pool_never_includes_expensive_overlay(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    _seed_candidate(db_path, "prov/pricey-model", input_usd=50.0, output_usd=200.0)
    merged = merged_cheaper_candidates(db_path, "claude-sonnet-4-6", include_openrouter=True)
    assert "prov/pricey-model" not in merged


def test_frontier_sha_changes_with_overlay(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    before = frontier_sha(db_path)
    _seed_candidate(db_path, "prov/new-model")
    after = frontier_sha(db_path)
    assert before != after
    assert frontier_sha(db_path) == after  # deterministic


# ---------------------------------------------------------------------------
# Research (DI'd web call — never spends)
# ---------------------------------------------------------------------------


def _web_returning(payload: str) -> Callable[..., str]:
    def _call(prompt: str, **kwargs: object) -> str:
        assert kwargs.get("purpose") == FRONTIER_PURPOSE
        assert kwargs.get("scope") == "meta_eval"
        return payload

    return _call


def test_research_validates_and_upserts(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    payload = """
    {"candidates": [
      {"model_id": "mistralai/new-cheap", "family": "openrouter",
       "input_usd_per_mtok": 0.15, "output_usd_per_mtok": 0.45, "promise": 0.7,
       "source_url": "https://openrouter.ai/models", "notes": "new + cheap"},
      {"model_id": "not-a-slug", "family": "openrouter",
       "input_usd_per_mtok": 0.1, "output_usd_per_mtok": 0.2},
      {"model_id": "gemini-9-ultra", "family": "gemini",
       "input_usd_per_mtok": -1, "output_usd_per_mtok": 5},
      {"model_id": "deepseek/deepseek-chat", "family": "openrouter",
       "input_usd_per_mtok": 0.3, "output_usd_per_mtok": 1.1}
    ]}
    """
    n = run_frontier_research(db_path, web_call=_web_returning(payload))
    # Only the first row survives: bad slug dropped, negative price dropped,
    # already-known static-ladder id dropped (the checked-in ladder is the
    # price authority for its own rows).
    assert n == 1
    models = load_candidate_models(db_path)
    assert set(models) == {"mistralai/new-cheap"}
    assert models["mistralai/new-cheap"].promise == 0.7


def test_research_upsert_updates_prices(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    first = '{"candidates": [{"model_id": "prov/m1", "family": "openrouter", "input_usd_per_mtok": 0.5, "output_usd_per_mtok": 1.0}]}'
    second = '{"candidates": [{"model_id": "prov/m1", "family": "openrouter", "input_usd_per_mtok": 0.2, "output_usd_per_mtok": 0.6, "promise": 0.9}]}'
    assert run_frontier_research(db_path, web_call=_web_returning(first)) == 1
    assert run_frontier_research(db_path, web_call=_web_returning(second)) == 1
    m = load_candidate_models(db_path)["prov/m1"]
    assert m.input_usd_per_mtok == 0.2
    assert m.promise == 0.9


def test_research_failure_degrades_to_zero(tmp_path: Path) -> None:
    db_path = _db(tmp_path)

    def _broken(prompt: str, **kwargs: object) -> str:
        raise RuntimeError("web transport down")

    assert run_frontier_research(db_path, web_call=_broken) == 0
    assert load_candidate_models(db_path) == {}
    # Unparseable output degrades the same way.
    assert run_frontier_research(db_path, web_call=_web_returning("not json")) == 0
