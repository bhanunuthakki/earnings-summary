"""Tests for the frontier-research overlay (src/llm/frontier.py) and the
model_ladder PR3 additions (backend_for, ladder_sha, JUDGE_POOL). All network
calls are DI'd fakes — the suite never spends and never hits the real network."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from llm.frontier import (
    FRONTIER_PURPOSE,
    frontier_sha,
    load_candidate_models,
    merged_backend_for,
    merged_cheaper_candidates,
    merged_is_cheaper,
    merged_rank,
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
# Merged price lookup (static ladder union discovered overlay)
# ---------------------------------------------------------------------------


def test_merged_rank_covers_overlay_models(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    _seed_candidate(db_path, "prov/cheap-model", input_usd=0.05, output_usd=0.10)
    assert merged_rank(db_path, "claude-sonnet-5") == pytest.approx(3.142857, rel=1e-4)
    assert merged_rank(db_path, "prov/cheap-model") == pytest.approx(0.0571428, rel=1e-4)
    assert merged_rank(db_path, "nonexistent-model") is None
    assert merged_is_cheaper(db_path, "prov/cheap-model", "claude-sonnet-5") is True
    assert merged_is_cheaper(db_path, "claude-sonnet-5", "prov/cheap-model") is False
    # Unpriced side -> not cheaper (unknown cost is not evidence of savings).
    assert merged_is_cheaper(db_path, "nonexistent-model", "claude-sonnet-5") is False


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


def test_merged_backend_for_uses_overlay_family(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    _seed_candidate(db_path, "gemini-2.5-flash-lite", family="gemini")
    assert merged_backend_for(db_path, "gemini-2.5-flash-lite") == "gemini"


def test_merged_backend_for_rejects_unknown_overlay_family(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    _seed_candidate(db_path, "vendor/model", family="unknown")
    with pytest.raises(ValueError, match="unsupported candidate family"):
        merged_backend_for(db_path, "vendor/model")


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
# Catalog-fetch research (DI'd fake HTTP GET — never hits the real network)
# ---------------------------------------------------------------------------


def _catalog_returning(data: Sequence[Mapping[str, object]]) -> Callable[[str], object]:
    def _get(url: str) -> object:
        assert url == "https://openrouter.ai/api/v1/models"
        return {"data": data}

    return _get


def _openrouter_row(
    model_id: str, *, prompt_usd_per_token: float, completion_usd_per_token: float
) -> dict[str, object]:
    return {
        "id": model_id,
        "pricing": {
            "prompt": str(prompt_usd_per_token),
            "completion": str(completion_usd_per_token),
        },
    }


def test_research_no_llm_call_involved(tmp_path: Path) -> None:
    """The purpose id still exists for ledger/isolation bookkeeping continuity,
    but run_frontier_research makes zero LLM calls — only an HTTP GET."""
    assert FRONTIER_PURPOSE == "model_frontier_research"


def test_research_validates_and_upserts(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    data = [
        _openrouter_row(
            "mistralai/new-cheap", prompt_usd_per_token=0.15e-6, completion_usd_per_token=0.45e-6
        ),
        {"id": "not-a-slug", "pricing": {"prompt": "0.0000001", "completion": "0.0000002"}},
        {"id": "gemini-9-ultra", "pricing": {"prompt": "-1", "completion": "5"}},
        # Already in the static ladder -> skipped (the checked-in ladder is
        # the price authority for its own rows).
        _openrouter_row(
            "deepseek/deepseek-chat", prompt_usd_per_token=0.3e-6, completion_usd_per_token=1.1e-6
        ),
    ]
    n = run_frontier_research(db_path, http_get=_catalog_returning(data))
    assert n == 1
    models = load_candidate_models(db_path)
    assert set(models) == {"mistralai/new-cheap"}
    assert models["mistralai/new-cheap"].promise == 0.5  # neutral: no capability guess
    assert models["mistralai/new-cheap"].input_usd_per_mtok == pytest.approx(0.15)
    assert models["mistralai/new-cheap"].output_usd_per_mtok == pytest.approx(0.45)


def test_research_caps_to_cheapest_n(tmp_path: Path) -> None:
    """Hundreds of catalog rows -> only the cheapest _MAX_CATALOG_ROWS survive,
    deterministically (no LLM judgment call needed)."""
    db_path = _db(tmp_path)
    data = [
        _openrouter_row(
            f"prov/model-{i}", prompt_usd_per_token=i * 1e-6, completion_usd_per_token=i * 2e-6
        )
        for i in range(1, 51)
    ]
    n = run_frontier_research(db_path, http_get=_catalog_returning(data))
    assert n == 12
    models = load_candidate_models(db_path)
    # The 12 cheapest are model-1..model-12.
    assert set(models) == {f"prov/model-{i}" for i in range(1, 13)}


def test_research_reprices_already_discovered_row(tmp_path: Path) -> None:
    """An overlay row IS eligible for re-pricing on a later pass — only the
    static ladder is off-limits."""
    db_path = _db(tmp_path)
    first = [
        _openrouter_row("prov/m1", prompt_usd_per_token=0.5e-6, completion_usd_per_token=1.0e-6)
    ]
    second = [
        _openrouter_row("prov/m1", prompt_usd_per_token=0.2e-6, completion_usd_per_token=0.6e-6)
    ]
    assert run_frontier_research(db_path, http_get=_catalog_returning(first)) == 1
    assert run_frontier_research(db_path, http_get=_catalog_returning(second)) == 1
    m = load_candidate_models(db_path)["prov/m1"]
    assert m.input_usd_per_mtok == pytest.approx(0.2)


def test_research_failure_degrades_to_zero(tmp_path: Path) -> None:
    db_path = _db(tmp_path)

    def _broken(url: str) -> object:
        raise RuntimeError("network down")

    assert run_frontier_research(db_path, http_get=_broken) == 0
    assert load_candidate_models(db_path) == {}
    # Malformed top-level shape degrades the same way.
    assert run_frontier_research(db_path, http_get=lambda url: {"unexpected": True}) == 0
    assert run_frontier_research(db_path, http_get=lambda url: ["not", "a", "dict"]) == 0


def test_research_drops_zero_and_negative_prices(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    data = [
        {"id": "prov/free-model", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "prov/bad-model", "pricing": {"prompt": "-0.001", "completion": "0.002"}},
        _openrouter_row(
            "prov/valid-model", prompt_usd_per_token=0.1e-6, completion_usd_per_token=0.2e-6
        ),
    ]
    n = run_frontier_research(db_path, http_get=_catalog_returning(data))
    assert n == 1
    assert set(load_candidate_models(db_path)) == {"prov/valid-model"}
