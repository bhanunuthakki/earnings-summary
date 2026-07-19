"""Smoke tests for the src/llm/ subpackage layout.

llm_client.py was split into four submodules (anchors, fallback, cli, ledger)
in a pure refactor. These tests assert that:

1. Each submodule is importable on its own (no circular imports, no missing
   dependency lurking inside).
2. The expected public names land in the expected module (so a future code
   archaeologist can grep `from llm.cli import call_llm` and find it).
3. The top-level src/llm/__init__.py re-exports everything in __all__ and
   each re-exported name is the same object as the underlying submodule's
   definition (no accidental aliasing during the migration).
4. src/llm_client.py keeps re-exporting every previously-public name so
   the ~30 caller import sites continue to work without modification.

These tests intentionally do NOT exercise behavior — that is covered by
test_llm_client_budget_integration.py and the wider test suite. They guard
the module layout against regressions during future cleanups.
"""

from __future__ import annotations

import inspect


def test_anchors_module_exposes_expected_names() -> None:
    from llm import anchors

    for name in (
        "ANCHOR_BLOCK_CHAR_CAP",
        "IR_ANCHOR_CHAR_CAP",
        "compose_anchor_block",
        "load_bear_anchor",
        "load_ir_anchor",
        "load_thesis_anchor",
    ):
        assert hasattr(anchors, name), f"llm.anchors missing {name!r}"
    assert inspect.isfunction(anchors.load_thesis_anchor)
    assert inspect.isfunction(anchors.load_bear_anchor)
    assert inspect.isfunction(anchors.load_ir_anchor)
    assert inspect.isfunction(anchors.compose_anchor_block)


def test_fallback_module_exposes_expected_names() -> None:
    from llm import fallback

    for name in ("GEMINI_FALLBACK_MODEL", "is_fallback_disabled", "try_gemini_fallback"):
        assert hasattr(fallback, name), f"llm.fallback missing {name!r}"
    assert inspect.isfunction(fallback.is_fallback_disabled)
    assert inspect.isfunction(fallback.try_gemini_fallback)
    assert fallback.GEMINI_FALLBACK_MODEL.startswith("gemini")


def test_cli_module_exposes_expected_names() -> None:
    from llm import cli

    for name in (
        "DEFAULT_MODEL",
        "FAST_CLASSIFIER_MODEL",
        "LLM_MODELS",
        "DEFAULT_TIMEOUT_SECONDS",
        "CLAUDE_WEB_TIMEOUT_SECONDS",
        "CLAUDE_WEB_TOOLS",
        "LLMBudgetExceeded",
        "LLMSetupError",
        "is_hard_stop",
        "_model_for",
        "_verify_setup_once",
        "_enforce_budget_pre_call",
        "_call_claude",
        "call_llm",
        "call_llm_with_web",
    ):
        assert hasattr(cli, name), f"llm.cli missing {name!r}"
    assert inspect.isfunction(cli.call_llm)
    assert inspect.isfunction(cli.call_llm_with_web)
    assert inspect.isfunction(cli.is_hard_stop)
    assert issubclass(cli.LLMBudgetExceeded, RuntimeError)
    assert issubclass(cli.LLMSetupError, RuntimeError)
    assert isinstance(cli.LLM_MODELS, dict)
    assert "bear_case" in cli.LLM_MODELS  # spot-check a known purpose


def test_kpi_registry_auto_proposal_routes_to_opus() -> None:
    """The auto KPI-registry seeder's purpose resolves to Opus via the
    LLM_MODELS table, while the manual --propose purpose stays unregistered
    (Sonnet default) — the two modes diverge cleanly."""
    from llm import cli

    assert cli.LLM_MODELS["kpi_registry_auto_proposal"] == "claude-opus-4-8"
    # _model_for is module-private; tests reaching into it use the repo's
    # rule-scoped pyright pragma (see test_etf_instrument_mvp.py).
    resolve = cli._model_for  # pyright: ignore[reportPrivateUsage]
    assert resolve("kpi_registry_auto_proposal") == "claude-opus-4-8"
    # The manual seeder purpose is deliberately NOT registered -> Sonnet.
    assert "kpi_registry_proposal" not in cli.LLM_MODELS
    assert resolve("kpi_registry_proposal") == cli.DEFAULT_MODEL


def test_news_purpose_pins() -> None:
    """The news LLM purpose pins, post the 2026-07-19 review:
      * material_news_classification — the material-news trigger's per-headline
        materiality veto stays on Opus (judgment-heavy noise filtering, one
        batched cached call per ticker per run).
      * news_structuring — the WebSearch->rows fallback extractor was
        DOWNGRADED to the Sonnet default: 1,622 calls / $416 in 30 days (over
        half the monthly LLM bill) for a structure-extraction task with
        deterministic output-contract invariants; the golden set gates the pin.
    The recent-developments web brief stays on Sonnet (DEFAULT_MODEL), pinned
    explicitly so call_llm_with_web's purpose resolution keeps it there."""
    from llm import cli

    # _model_for is module-private; tests reaching into it use the repo's
    # rule-scoped pyright pragma (see test_etf_instrument_mvp.py).
    resolve = cli._model_for  # pyright: ignore[reportPrivateUsage]
    assert cli.LLM_MODELS["material_news_classification"] == "claude-opus-4-8"
    assert cli.LLM_MODELS["news_structuring"] == cli.DEFAULT_MODEL
    assert resolve("material_news_classification") == "claude-opus-4-8"
    assert resolve("news_structuring") == cli.DEFAULT_MODEL
    # Recent-developments is pinned to Sonnet, not Opus.
    assert cli.LLM_MODELS["recent_developments"] == cli.DEFAULT_MODEL
    assert resolve("recent_developments") == cli.DEFAULT_MODEL


def test_news_pins_do_not_disturb_existing_purposes() -> None:
    """Registering the news purposes must not flip any other prompt's model.
    Spot-check one purpose per tier plus the manual KPI seeder (deliberately
    unregistered -> Sonnet)."""
    from llm import cli

    resolve = cli._model_for  # pyright: ignore[reportPrivateUsage]
    # Sonnet (default) analytical writing — unchanged.
    assert resolve("bear_case") == cli.DEFAULT_MODEL
    assert resolve("transcript_summary") == cli.DEFAULT_MODEL
    # company_description moved Opus -> Sonnet in #444 (model-eval verdict).
    assert resolve("company_description") == cli.DEFAULT_MODEL
    # Pre-existing Opus pins — unchanged.
    assert resolve("valuation_basis") == "claude-opus-4-8"
    assert resolve("saydo_importance") == "claude-opus-4-8"
    assert resolve("kpi_registry_auto_proposal") == "claude-opus-4-8"
    # Gemini un-pinned 2026-07-02 (metered Gemini off the active pareto): these two
    # classifier purposes reverted from Gemini Flash (#538) back to Claude Haiku.
    assert resolve("intake_classifier") == cli.FAST_CLASSIFIER_MODEL
    assert resolve("transcript_metadata") == cli.FAST_CLASSIFIER_MODEL
    # The manual KPI-proposal purpose stays unregistered -> Sonnet.
    assert "kpi_registry_proposal" not in cli.LLM_MODELS
    assert resolve("kpi_registry_proposal") == cli.DEFAULT_MODEL


def test_ledger_module_exposes_expected_names() -> None:
    from llm import ledger

    for name in ("record_llm_call", "fallback_call_logged"):
        assert hasattr(ledger, name), f"llm.ledger missing {name!r}"
    assert inspect.isfunction(ledger.record_llm_call)
    assert inspect.isfunction(ledger.fallback_call_logged)


def test_llm_package_init_reexports_match_submodules() -> None:
    """llm.__init__ re-exports must be the SAME OBJECTS as the underlying
    submodule definitions — guards against an accidental `from llm.cli import
    call_llm as call_llm; call_llm = staticmethod(call_llm)` shadow."""
    import llm
    from llm import anchors, cli, fallback, ledger

    pairs = [
        (llm.load_thesis_anchor, anchors.load_thesis_anchor),
        (llm.load_bear_anchor, anchors.load_bear_anchor),
        (llm.load_ir_anchor, anchors.load_ir_anchor),
        (llm.compose_anchor_block, anchors.compose_anchor_block),
        (llm.call_llm, cli.call_llm),
        (llm.call_llm_with_web, cli.call_llm_with_web),
        (llm.LLMBudgetExceeded, cli.LLMBudgetExceeded),
        (llm.LLM_MODELS, cli.LLM_MODELS),
        (llm.try_gemini_fallback, fallback.try_gemini_fallback),
        (llm.is_fallback_disabled, fallback.is_fallback_disabled),
        (llm.record_llm_call, ledger.record_llm_call),
        (llm.fallback_call_logged, ledger.fallback_call_logged),
    ]
    for reexport, original in pairs:
        assert reexport is original, f"{reexport!r} is not {original!r}"


def test_llm_client_reexports_preserve_caller_imports() -> None:
    """The whole point of the split was zero caller churn. Every name the
    pre-split llm_client.py exposed (and that the codebase imports today)
    must still be importable from llm_client."""
    import llm_client

    expected_names = [
        # Public anchor builders
        "ANCHOR_BLOCK_CHAR_CAP",
        "IR_ANCHOR_CHAR_CAP",
        "compose_anchor_block",
        "load_bear_anchor",
        "load_ir_anchor",
        "load_thesis_anchor",
        # Public CLI entry points + budget
        "DEFAULT_MODEL",
        "FAST_CLASSIFIER_MODEL",
        "LLM_MODELS",
        "DEFAULT_TIMEOUT_SECONDS",
        "CLAUDE_WEB_TIMEOUT_SECONDS",
        "CLAUDE_WEB_TOOLS",
        "LLMBudgetExceeded",
        "call_llm",
        "call_llm_with_web",
        # Public fallback constants
        "GEMINI_FALLBACK_MODEL",
        # Public prompt-rendering helpers
        "JSON_FENCE_RE",
        "SCHEMA_LLM_REDACT_FIELDS",
        "INTAKE_TEXT_BUDGET",
        "STALE_CORPUS_THRESHOLD_DAYS",
        "VALUATION_MULTIPLE_CHOICES",
        # Private names imported by execution/* and src/compute/* (preserved
        # for compatibility with current callers — see the import grep
        # captured during the refactor).
        "_call_claude",
        # Module-level state the budget test monkeypatches
        "_setup_verified",
        "_claude_cli_path",
        # Per-purpose generators that still live in llm_client.py
        "classify_intake_document",
        "generate_bear_case",
        "generate_company_description",
        "generate_event_brief",
        "generate_pairwise_analysis",
        "generate_platform_diagram",
        "generate_press_release_summary",
        "generate_presentation_brief",
        "generate_qa_topics",
        "generate_recent_developments",
        "generate_saydo_filter",
        "generate_saydo_importance",
        "generate_strategic_analysis",
        "generate_summary",
        "generate_thesis_update",
        "generate_valuation_basis",
        "identify_transcript_metadata",
    ]
    for name in expected_names:
        assert hasattr(llm_client, name), f"llm_client missing {name!r}"
