"""P0 prompt registry (llm_quality_program_2026_07.md): template validation,
strict rendering, auto-versioning, the RenderedPrompt str-subclass contract,
the ledger lift, and the scenario_prior byte-identity migration gate."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from llm.prompt_registry import (
    REGISTRY,
    PromptTemplate,
    RenderedPrompt,
    attribute_plain_prompt,
    register,
    template_meta,
)

# ---------------------------------------------------------------------------
# Template validation — a drifted declaration must never register
# ---------------------------------------------------------------------------


def test_declared_variables_must_match_body_slots_exactly() -> None:
    with pytest.raises(ValueError, match="present-but-undeclared"):
        PromptTemplate("t.bad1", "Analyze {ticker} in {period}.", ("ticker",))
    with pytest.raises(ValueError, match="declared-but-absent"):
        PromptTemplate("t.bad2", "Analyze {ticker}.", ("ticker", "period"))


def test_positional_slots_rejected() -> None:
    with pytest.raises(ValueError, match="positional"):
        PromptTemplate("t.pos", "Analyze {}.", ())


def test_brace_escapes_are_not_slots() -> None:
    t = PromptTemplate("t.json", 'Return {{"k": "{v}"}}.', ("v",))
    assert t.render(v="x") == 'Return {"k": "x"}.'


def test_version_is_body_hash_and_changes_on_edit() -> None:
    a = PromptTemplate("t.v1", "Body {x}.", ("x",))
    b = PromptTemplate("t.v2", "Body {x}.", ("x",))
    c = PromptTemplate("t.v3", "Body {x}!", ("x",))
    assert a.version == b.version  # identity is the BODY, not the id
    assert a.version != c.version  # any edit is automatically a new version
    assert len(a.version) == 12


# ---------------------------------------------------------------------------
# Strict rendering — both directions
# ---------------------------------------------------------------------------


def test_render_missing_and_unexpected_variables_both_raise() -> None:
    t = PromptTemplate("t.strict", "A {x} B {y}.", ("x", "y"))
    with pytest.raises(ValueError, match="missing"):
        t.render(x="1")
    with pytest.raises(ValueError, match="unexpected"):
        t.render(x="1", y="2", z="3")


def test_rendered_prompt_is_a_string_with_identity() -> None:
    t = PromptTemplate("t.id", "Hello {name}.", ("name",))
    r = t.render(name="NU")
    assert isinstance(r, str) and r == "Hello NU."
    assert isinstance(r, RenderedPrompt)
    assert r.template_id == "t.id" and r.template_version == t.version
    # Same vars -> same sha; different vars -> different sha.
    assert t.render(name="NU").vars_sha256 == r.vars_sha256
    assert t.render(name="MELI").vars_sha256 != r.vars_sha256
    # str operations do not lose the base contract the transport relies on.
    assert len(r) == len("Hello NU.")


def test_template_meta_lift() -> None:
    t = PromptTemplate("t.meta", "X {a}.", ("a",))
    r = t.render(a="1")
    assert template_meta(r) == ("t.meta", t.version, r.vars_sha256)
    assert template_meta("a plain unmigrated prompt") == (None, None, None)
    assert template_meta(None) == (None, None, None)


def test_plain_prompt_gets_central_purpose_version_and_body_hash() -> None:
    import hashlib

    prompt = "raw production prompt"
    attributed = attribute_plain_prompt(prompt, purpose="bear_case")

    assert attributed == prompt
    assert isinstance(attributed, RenderedPrompt)
    assert attributed.template_id == "purpose:bear_case"
    assert attributed.template_version == "v2"
    assert attributed.vars_sha256 == hashlib.sha256(prompt.encode()).hexdigest()


def test_central_attribution_preserves_registered_rendered_prompt() -> None:
    rendered = PromptTemplate("t.keep", "Hi {name}", ("name",)).render(name="NU")
    assert attribute_plain_prompt(rendered, purpose="bear_case") is rendered


def test_call_llm_attributes_plain_prompt_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import llm.cli as cli

    observed: list[str] = []

    def fake_claude(prompt: str, **_kwargs: object) -> str:
        observed.append(prompt)
        return "ok"

    monkeypatch.setattr(cli, "_call_claude", fake_claude)
    result = cli.call_llm(
        "raw prompt",
        purpose="bear_case",
        model="claude-opus-4-8",
        backend="claude",
        force_budget_bypass=True,
    )

    assert result == "ok"
    assert len(observed) == 1 and isinstance(observed[0], RenderedPrompt)
    attributed = observed[0]
    assert isinstance(attributed, RenderedPrompt)
    assert attributed.template_id == "purpose:bear_case"
    assert attributed.template_version == "v2"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_idempotent_and_conflict_loud() -> None:
    t = PromptTemplate("t.reg.unique", "Body {x}.", ("x",))
    try:
        assert register(t) is t
        assert register(PromptTemplate("t.reg.unique", "Body {x}.", ("x",))) is t  # no-op
        with pytest.raises(ValueError, match="different body"):
            register(PromptTemplate("t.reg.unique", "Other {x}.", ("x",)))
    finally:
        REGISTRY.pop("t.reg.unique", None)


# ---------------------------------------------------------------------------
# Ledger integration — the row carries template identity (mig 0205)
# ---------------------------------------------------------------------------

_LLM_CALLS_DDL = """
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


def _record(db: Path, prompt: object) -> sqlite3.Row:
    import llm_call_ledger
    from llm.ledger import record_llm_call

    # The available-columns cache is keyed by db path and lives for the
    # process; tests build several DBs with different schemas.
    optional_cache = cast(
        "dict[str, frozenset[str]]", getattr(llm_call_ledger, "_OPTIONAL_COLUMN_CACHE")
    )
    optional_cache.pop(str(db), None)

    record_llm_call(
        started_at=datetime.now(UTC).replace(tzinfo=None),
        elapsed_ms=5,
        model="claude-test",
        prompt_sha="ab" * 32,
        prompt_chars=len(str(prompt)),
        purpose="scenario_prior",
        ticker="NU",
        scope=None,
        run_id=None,
        response_text="ok",
        prompt=prompt,
    )
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM llm_calls ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    assert row is not None
    return row


def test_ledger_row_carries_template_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "p.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_LLM_CALLS_DDL)
    conn.commit()
    conn.close()
    import db as _db

    monkeypatch.setattr(_db, "DB_PATH", db)

    t = PromptTemplate("t.ledger", "Analyze {ticker}.", ("ticker",))
    rendered = t.render(ticker="NU")
    row = _record(db, rendered)
    assert row["template_id"] == "t.ledger"
    assert row["template_version"] == t.version
    assert row["template_vars_sha256"] == rendered.vars_sha256

    plain = _record(db, "raw lower-level prompt")
    assert plain["template_id"] is None  # direct ledger bypass stays honest


def test_ledger_pre_migration_db_still_lands_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Against a pre-0205 DB the row must still land (legacy columns) with a
    LOUD warning — dropping telemetry silently is the silent-degradation class."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        _LLM_CALLS_DDL.replace(
            ",\n    template_id TEXT, template_version TEXT, template_vars_sha256 TEXT", ""
        )
    )
    conn.commit()
    conn.close()
    import db as _db

    monkeypatch.setattr(_db, "DB_PATH", db)
    t = PromptTemplate("t.old", "X {a}.", ("a",))
    import logging

    with caplog.at_level(logging.WARNING):
        row = _record(db, t.render(a="1"))
    assert row["purpose"] == "scenario_prior"  # the row landed
    assert any("optional_cols_missing" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# The scenario_prior migration gate — byte identity with the legacy f-string
# ---------------------------------------------------------------------------


def test_scenario_prior_template_byte_identity() -> None:
    """P0 gate (directive): the registry render must be byte-identical to the
    legacy inline .format for the migrated purpose."""
    import dcf.scenario_prior as scenario_prior

    ticker, anchor = "nu", "=== ANCHORS ===\nthesis text {not a slot} — 100% & more"
    # The anchor block may contain braces-like text; the legacy path formatted
    # the TEMPLATE (not the anchor), so both paths must treat anchor verbatim.
    legacy_template = cast("str", getattr(scenario_prior, "_PROMPT"))
    legacy = legacy_template.format(ticker=ticker.upper(), anchor_block=anchor)
    rendered = scenario_prior.build_prompt(ticker, anchor)
    assert rendered == legacy
    assert isinstance(rendered, RenderedPrompt)
    assert rendered.template_id == "scenario_prior.weights"


def test_scenario_prior_registered() -> None:
    assert "scenario_prior.weights" in REGISTRY


def test_every_backend_forwards_the_prompt_object() -> None:
    """P0's template identity rides on the RenderedPrompt str-SUBCLASS, so any
    ledger write that doesn't forward the prompt OBJECT silently records NULLs.

    Found live 2026-07-25: the Claude path was wired but the OpenRouter and
    Gemini backends were not — precisely the backends the new cross-family
    judges use, so judged calls lost their template attribution. Asserted as
    source structure because the alternative is four live backend calls.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for rel in (
        "src/llm/cli.py",
        "src/llm/openrouter_backend.py",
        "src/llm/gemini_backend.py",
        "src/llm/ledger.py",
    ):
        src = (root / rel).read_text(encoding="utf-8")
        for m in re.finditer(r"record_llm_call\(", src):
            start = m.end()
            depth = 0
            k = start
            for k in range(start, len(src)):
                if src[k] == "(":
                    depth += 1
                elif src[k] == ")":
                    if depth == 0:
                        break
                    depth -= 1
            call = src[start:k]
            # Real call sites always pass started_at=; a bare "record_llm_call("
            # inside a docstring or comment does not.
            if "started_at=" not in call:
                continue
            line = src[: m.start()].count("\n") + 1
            assert "prompt=" in call, (
                f"{rel}:{line} record_llm_call() does not forward prompt= — "
                "template identity will be NULL for this path"
            )


def test_str_conversion_drops_identity_by_design() -> None:
    """Documents the sharp edge: str()/concatenation returns a plain str and
    loses the metadata. Callers must pass the RenderedPrompt THROUGH, not a
    stringified copy (this is why the check above asserts on the call site)."""
    t = PromptTemplate("t.sharp", "A {x}.", ("x",))
    r = t.render(x="1")
    assert template_meta(r)[0] == "t.sharp"
    assert template_meta(str(r)) == (None, None, None)
    assert template_meta(r + "") == (None, None, None)
