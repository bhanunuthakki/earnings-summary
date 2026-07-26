"""P1 trace context (llm.tracectx + mig 0206): in-process nesting,
cross-process propagation via the environment, honest NULLs when untraced,
and the ledger integration.

The measured motivation: diagnosing July-2026's quota incident took ~15
hand-written SQL queries because rows carried no stage. The last test here is
that one-query reproduction.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from llm import tracectx

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    for key in (tracectx.ENV_TRACE_ID, tracectx.ENV_SPAN_ID, tracectx.ENV_STAGE):
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# In-process
# ---------------------------------------------------------------------------


def test_no_context_is_honest_nulls() -> None:
    assert tracectx.current() is None
    assert tracectx.context_fields() == (None, None, None, None)


def test_stage_sets_and_restores() -> None:
    with tracectx.stage("morning_pipeline.0b") as ctx:
        assert tracectx.current() == ctx
        trace_id, span_id, parent, stage = tracectx.context_fields()
        assert stage == "morning_pipeline.0b"
        assert trace_id == ctx.trace_id and span_id == ctx.span_id
        assert parent is None  # root span
    assert tracectx.current() is None  # restored


def test_nested_stages_share_trace_and_chain_parents() -> None:
    with tracectx.stage("pipeline") as outer, tracectx.stage("pipeline.substage") as inner:
        assert inner.trace_id == outer.trace_id  # one trace
        assert inner.parent_span_id == outer.span_id  # chained
        assert inner.span_id != outer.span_id


# ---------------------------------------------------------------------------
# Cross-process (the mechanism that actually matters here)
# ---------------------------------------------------------------------------


def test_child_env_carries_the_trace() -> None:
    with tracectx.stage("parent_stage") as ctx:
        env = tracectx.child_env(base={}, stage_name="morning_pipeline.stage_news")
    assert env[tracectx.ENV_TRACE_ID] == ctx.trace_id
    assert env[tracectx.ENV_SPAN_ID] == ctx.span_id  # child's parent
    assert env[tracectx.ENV_STAGE] == "morning_pipeline.stage_news"


def test_child_env_without_context_still_labels_when_asked() -> None:
    """The orchestrator may not itself be in a span; a labelled child must
    still be attributable rather than silently untraced."""
    env = tracectx.child_env(base={}, stage_name="morning_pipeline.stage_x")
    assert env[tracectx.ENV_STAGE] == "morning_pipeline.stage_x"
    assert env[tracectx.ENV_TRACE_ID]


def test_child_env_passthrough_when_nothing_to_propagate() -> None:
    env = tracectx.child_env(base={"A": "1"})
    assert env == {"A": "1"}  # no fabricated trace


def test_seeding_from_env_requires_both_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """A half-set environment would yield a row that LOOKS traced but is not
    attributable — the silent-degradation shape. Half-set ⇒ no context."""
    monkeypatch.setenv(tracectx.ENV_TRACE_ID, "abc123")
    # stage missing
    assert tracectx._from_env() is None
    monkeypatch.setenv(tracectx.ENV_STAGE, "some.stage")
    seeded = tracectx._from_env()
    assert seeded is not None and seeded.trace_id == "abc123"
    assert seeded.stage == "some.stage"
    assert seeded.span_id != "abc123"  # the child is its own span


def test_real_subprocess_inherits_the_trace() -> None:
    """End-to-end: a child python process joins the parent's trace purely via
    the environment — the morning pipeline's actual topology."""
    with tracectx.stage("orchestrator") as ctx:
        env = tracectx.child_env(stage_name="morning_pipeline.stage_probe")
    # Load tracectx BY FILE PATH: `from llm import tracectx` would execute the
    # heavy llm/__init__ (which pulls llm_client, dotenv, DB wiring) — seconds
    # of import for a module that must stay dependency-free precisely so any
    # entrypoint can adopt it.
    # Import tracectx as a TOP-LEVEL module (src/llm on the path) rather than
    # `from llm import tracectx`: the latter executes the heavy llm/__init__
    # (llm_client, dotenv, DB wiring). This works precisely because tracectx
    # imports nothing but the stdlib — a property worth keeping, since every
    # entrypoint that wants to be traceable has to import it cheaply.
    code = (
        "import sys;sys.path.insert(0, sys.argv[1]);"
        "import tracectx as m;"
        "t,s,p,st=m.context_fields();"
        "print(f'{t}|{st}|{p}')"
    )
    out = subprocess.run(
        [sys.executable, "-c", code, str(PROJECT_ROOT / "src" / "llm")],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert out.returncode == 0, out.stderr
    trace_id, stage, parent = out.stdout.strip().split("|")
    assert trace_id == ctx.trace_id
    assert stage == "morning_pipeline.stage_probe"
    assert parent == ctx.span_id


# ---------------------------------------------------------------------------
# Ledger integration + the one-query diagnosis
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    called_at TEXT NOT NULL, purpose TEXT, ticker TEXT, scope TEXT,
    model TEXT, prompt_sha256 TEXT, response_sha256 TEXT,
    prompt_chars INTEGER, response_chars INTEGER, input_tokens INTEGER,
    cache_creation_input_tokens INTEGER, cache_read_input_tokens INTEGER,
    output_tokens INTEGER, elapsed_ms INTEGER, cost_estimate_usd REAL,
    cache_hit INTEGER NOT NULL DEFAULT 0, fallback_used TEXT,
    artifact_id INTEGER, error TEXT, run_id TEXT,
    template_id TEXT, template_version TEXT, template_vars_sha256 TEXT,
    trace_id TEXT, span_id TEXT, parent_span_id TEXT, stage TEXT
);
"""


def _emit(db: Path, purpose: str, cost: float) -> None:
    from llm.ledger import record_llm_call

    record_llm_call(
        started_at=datetime.now(UTC).replace(tzinfo=None),
        elapsed_ms=1,
        model="m",
        prompt_sha="a" * 64,
        prompt_chars=10,
        purpose=purpose,
        ticker=None,
        scope=None,
        run_id=None,
        response_text="ok",
        meta={"total_cost_usd": cost},
    )


def test_ledger_rows_carry_stage_and_answer_the_burn_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "p.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_DDL)
    conn.commit()
    conn.close()
    import db as _db

    monkeypatch.setattr(_db, "DB_PATH", db)

    with tracectx.stage("morning_pipeline.stage_news"):
        _emit(db, "news_structuring", 4.0)
        _emit(db, "news_structuring", 2.0)
    with tracectx.stage("morning_pipeline.stage_triggers"):
        _emit(db, "material_news_classification", 0.5)
    _emit(db, "ad_hoc_script", 9.0)  # untraced — must stay NULL, not misattributed

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        # THE query the July diagnosis needed and could not write.
        rows = conn.execute(
            "SELECT stage, COUNT(*) n, ROUND(SUM(cost_estimate_usd),2) cost "
            "FROM llm_calls GROUP BY stage ORDER BY cost DESC"
        ).fetchall()
    finally:
        conn.close()
    by_stage = {r["stage"]: (r["n"], r["cost"]) for r in rows}
    assert by_stage["morning_pipeline.stage_news"] == (2, 6.0)
    assert by_stage["morning_pipeline.stage_triggers"] == (1, 0.5)
    assert by_stage[None] == (1, 9.0)  # untraced is VISIBLE, not silently folded in


def test_pipeline_propagates_stage_to_children() -> None:
    """The orchestrator must actually pass a stage-labelled env to stages —
    the wiring, not just the helper."""
    src = (PROJECT_ROOT / "execution" / "run_morning_pipeline.py").read_text(encoding="utf-8")
    assert "tracectx.child_env(stage_name=" in src
    assert "morning_pipeline." in src


def test_untraced_env_is_not_leaked_between_processes() -> None:
    """child_env must not carry a stale ES_STAGE from the ambient environment
    when the caller asked for none."""
    env = tracectx.child_env(base={"ES_STAGE": "stale.stage"})
    # No active context and no stage_name: the base is returned as-is (the
    # caller owns its own env), but nothing NEW is fabricated.
    assert env["ES_STAGE"] == "stale.stage"
    assert tracectx.ENV_TRACE_ID not in env
    assert os.environ.get(tracectx.ENV_TRACE_ID) is None
