"""Tests for the Slice 3 /review chat command + deterministic rendering.

The command is the instant, no-LLM surface: it routes through the ask engine's
command path and returns the grounded pre-analysis + a mechanical trim/hold read.
Tests cover the mechanical-read branches, the chat rendering, and the command
dispatch + at-price parsing (through the public run_chat_command, so no private
symbols are imported). No LLM; build_pre_analysis is stubbed where a DB would be.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from advisor import position_review as pr
from advisor.position_review import (
    PreAnalysis,
    mechanical_read,
    parse_review_command,
    render_pre_analysis_chat,
    render_pre_analysis_plain,
    render_tax_lines,
    review_reply_text,
)
from advisor.position_tax import PositionTaxView, TrimTaxEstimate, unavailable_tax_view
from ask.commands import COMMAND_PREFIXES, run_chat_command
from capture.matcher import DISTINCTIVE_ALIASES, build_roster_index
from dispatch_registry import Job, Registry

_PRE_DEFAULT = PreAnalysis(
    ticker="RBRK",
    weight_pct=10.0,
    weight_source="materialized",
    market_value_usd=None,
    unrealized_pnl_usd=None,
    target_weight_pct=None,
    target_band=None,
    weight_vs_band="no_band",
    conviction_1_5=None,
    concentration_flag=False,
    thesis_present=True,
    verdict_label="Intact",
    key_driver="Net new subscription ARR cadence",
    break_rule_status="intact",
    tripped_rules=(),
    dcf_gap_pct=-10.0,
    npv_per_share=91.09,
    dcf_live_price=80.35,
    dcf_date="2026-06-15",
    at_price=None,
    mos_bar=0.30,
    valuation_verdict="fair",
    conviction_encoded=True,
    has_stance=True,
    has_decision_note=True,
    is_index_instrument=False,
)


def _pre(**overrides: object) -> PreAnalysis:
    return replace(_PRE_DEFAULT, **overrides)


# --------------------------------------------------------------------------- #
# mechanical_read — the deterministic verdict hint (mirrors the guard)
# --------------------------------------------------------------------------- #


def test_mechanical_read_hold_on_intact_fair_non_oversized() -> None:
    assert mechanical_read(_pre()).startswith("HOLD")


def test_mechanical_read_flags_breach() -> None:
    assert "BREACHED" in mechanical_read(_pre(break_rule_status="breach"))


def test_mechanical_read_flags_overvalued() -> None:
    assert "Over-valued" in mechanical_read(_pre(valuation_verdict="sell"))


def test_mechanical_read_flags_oversized_concentration() -> None:
    assert "OVERSIZED" in mechanical_read(_pre(concentration_flag=True))


def test_mechanical_read_encode_first_for_index() -> None:
    read = mechanical_read(_pre(thesis_present=False, is_index_instrument=True))
    assert "encode" in read.lower() and "sleeve" in read.lower()


# --------------------------------------------------------------------------- #
# render_pre_analysis_chat
# --------------------------------------------------------------------------- #


def test_render_chat_contains_grounded_facts_and_cli_pointer() -> None:
    out = render_pre_analysis_chat(_pre(at_price=82.0))
    assert "**RBRK**" in out
    assert "Mechanical read:" in out
    assert "ladder: fair" in out
    assert "review_position.py RBRK --at-price 82" in out


# --------------------------------------------------------------------------- #
# render_pre_analysis_plain — the Telegram coach-voice variant (PR10)
# --------------------------------------------------------------------------- #


def test_render_plain_has_no_markdown_markers_or_broken_cli_pointer() -> None:
    out = render_pre_analysis_plain(_pre(at_price=82.0))
    assert "**" not in out
    assert "`" not in out
    assert "python execution" not in out


def test_render_plain_carries_the_same_key_facts_as_chat() -> None:
    """Parity check: both renderers must agree on the substantive facts — the
    plain variant only drops Markdown chrome and the CLI pointer."""
    pre = _pre(at_price=82.0)
    chat = render_pre_analysis_chat(pre)
    plain = render_pre_analysis_plain(pre)
    assert pre.ticker in plain
    assert "10.0%" in plain and "10.0%" in chat  # weight line
    assert "91.09" in plain and "91.09" in chat  # fair value
    assert "Mechanical read:" in plain
    assert "ladder: fair" in plain


def test_render_plain_ends_with_a_web_pointer_not_a_cli_command() -> None:
    out = render_pre_analysis_plain(_pre())
    assert "Full calibrated review: from the desk (Holding -> Review)." in out


def test_render_plain_includes_tax_block_when_present() -> None:
    out = render_pre_analysis_plain(_pre(tax=_TAX_VIEW))
    assert "- Tax: taxable 60% of position" in out
    assert out.index("- Tax:") < out.index("- Mechanical read:")


def test_render_plain_tax_block_is_ascii_and_markdown_free() -> None:
    """The shared tax renderer must honor the plain-ASCII contract on Telegram:
    no `_footnote_` italics (they'd arrive as literal underscores with no
    parse_mode) and no non-ASCII → / · separators."""
    out = render_pre_analysis_plain(_pre(tax=_TAX_VIEW))
    assert "→" not in out and "·" not in out
    # the MAGI footnote renders WITHOUT the _italic_ markdown wrappers
    assert "Tax estimate assumes 2026 MFJ" in out
    assert "_Tax estimate" not in out
    # the ST->LT wait line uses an ASCII arrow, not the Markdown-chat glyph
    assert "(ST->LT)" in out and "(ST→LT)" not in out


# --------------------------------------------------------------------------- #
# review_reply_text(plain=...) — the shared Telegram/web dispatch seam
# --------------------------------------------------------------------------- #


def test_review_reply_text_plain_renders_via_plain_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake(*_a: object, **_k: object) -> PreAnalysis:
        return _pre(at_price=82.0)

    monkeypatch.setattr(pr, "build_pre_analysis", _fake)
    reply = review_reply_text(Path("."), "/review RBRK at $82", plain=True)
    assert "**" not in reply and "`" not in reply
    assert "RBRK" in reply


def test_review_reply_text_default_stays_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(*_a: object, **_k: object) -> PreAnalysis:
        return _pre(at_price=82.0)

    monkeypatch.setattr(pr, "build_pre_analysis", _fake)
    reply = review_reply_text(Path("."), "/review RBRK at $82")
    assert "**RBRK**" in reply


def test_review_reply_text_plain_usage_message_is_ascii() -> None:
    reply = review_reply_text(Path("."), "/review", plain=True)
    assert "Usage:" in reply
    assert "`" not in reply and "—" not in reply


# --------------------------------------------------------------------------- #
# name/alias resolution — "/review Rubrik" must review RBRK, not "RUBRIK"
# --------------------------------------------------------------------------- #


def _roster() -> object:
    """A roster with the tracked symbols + the distinctive-name alias seed —
    the same shape ``load_roster`` builds in prod."""
    return build_roster_index(symbols=["RBRK", "NU", "NVO"], phrases=DISTINCTIVE_ALIASES)


def test_parse_resolves_company_name_to_ticker() -> None:
    roster = _roster()
    assert parse_review_command("/review Rubrik", roster=roster) == ("RBRK", None)
    # case-insensitive, and the "at $X" clause still parses alongside the name
    assert parse_review_command("/review rubrik at $70", roster=roster) == ("RBRK", 70.0)
    assert parse_review_command("/review Nubank", roster=roster) == ("NU", None)


def test_parse_resolves_multiword_company_name() -> None:
    roster = build_roster_index(symbols=["NVO"], phrases=DISTINCTIVE_ALIASES)
    # a two-word name survives the "at $X" strip and resolves as a phrase
    assert parse_review_command("/review Novo Nordisk at $120", roster=roster) == ("NVO", 120.0)


def test_parse_bare_symbol_and_symbol_alias_still_resolve() -> None:
    roster = _roster()
    assert parse_review_command("/review RBRK", roster=roster) == ("RBRK", None)
    assert parse_review_command("/review $RBRK at $70", roster=roster) == ("RBRK", 70.0)
    # symbol-alias fallback still applies even when the roster has no name match
    assert parse_review_command("/review googl", roster=roster) == ("GOOG", None)


def test_parse_without_roster_uses_token_as_typed() -> None:
    # No roster (resolution unavailable) → the token is used as-typed, uppercased
    # — the pre-resolution behavior, so an un-rostered name still reviews.
    assert parse_review_command("/review RBRK at $82") == ("RBRK", 82.0)
    assert parse_review_command("/review Rubrik") == ("RUBRIK", None)


def test_review_reply_text_resolves_company_name_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole Telegram/web seam: '/review Rubrik' must build the review for
    RBRK, not the literal 'RUBRIK'."""
    seen: dict[str, object] = {}

    def _fake(_repo: object, ticker: str, **_k: object) -> PreAnalysis:
        seen["ticker"] = ticker
        return _pre()

    monkeypatch.setattr(pr, "build_pre_analysis", _fake)
    monkeypatch.setattr(
        pr,
        "_load_review_roster",
        lambda _rr: build_roster_index(symbols=["RBRK"], phrases=DISTINCTIVE_ALIASES),
    )
    out = review_reply_text(Path("."), "/review Rubrik", plain=True)
    assert seen["ticker"] == "RBRK"
    assert "RBRK" in out


def test_review_command_background_job_uses_resolved_ticker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The background full-verdict kickoff must schedule the RESOLVED ticker, so
    '/review Rubrik' reviews AND grades RBRK — never a dangling 'RUBRIK' job."""
    started: dict[str, object] = {}

    def _fake(*_a: object, **_k: object) -> PreAnalysis:
        return _pre()

    class _Capturing(_NonSpawningRegistry):
        def start(
            self,
            *,
            ticker: str,
            kind: str,
            argv: list[str],
            spawn: bool = True,
            cwd: str | None = None,
        ) -> Job:
            started["ticker"] = ticker
            started["argv"] = argv
            return super().start(ticker=ticker, kind=kind, argv=argv, spawn=spawn, cwd=cwd)

    monkeypatch.setattr(pr, "build_pre_analysis", _fake)
    monkeypatch.setattr(
        pr,
        "_load_review_roster",
        lambda _rr: build_roster_index(symbols=["RBRK"], phrases=DISTINCTIVE_ALIASES),
    )
    run_chat_command(Path("."), "/review Rubrik at $70", _Capturing())
    assert started["ticker"] == "RBRK"
    argv = cast("list[str]", started["argv"])
    assert "RBRK" in argv and "--at-price" in argv and "70.0" in argv


# --------------------------------------------------------------------------- #
# render_tax_lines — the tax block shared by chat, CLI, and memo
# --------------------------------------------------------------------------- #

_TAX_TRIM = TrimTaxEstimate(
    trim_fraction=0.2,
    trim_rationale="to top of band (4.8%)",
    trim_shares=100.0,
    trim_usd=30_000.0,
    taxable_shares_consumed=100.0,
    realized_gain_usd=15_000.0,
    st_gain_usd=4_000.0,
    lt_gain_usd=11_000.0,
    tax_low_usd=4_895.0,
    tax_high_usd=4_895.0,
    days_until_lt=62,
    wait_savings_usd=680.0,
    wash_sale_risk=True,
)
_TAX_VIEW = PositionTaxView(
    available=True,
    reason=None,
    approximate=False,
    approx_reasons=(),
    eval_price=300.0,
    taxable_mv_usd=90_000.0,
    taxable_pct_of_position=60.0,
    sheltered_mv_usd=60_000.0,
    sheltered_note="40% of the position sits in Roth/HSA — trim there first",
    st_unrealized_usd=12_000.0,
    lt_unrealized_usd=48_000.0,
    trim=_TAX_TRIM,
    footnote="Tax estimate assumes 2026 MFJ, $450-500k MAGI, CA resident.",
)


def test_render_tax_lines_exact_mode_is_dense_and_complete() -> None:
    text = "\n".join(render_tax_lines(_TAX_VIEW))
    assert "- Tax: taxable 60% of position" in text
    assert "embedded gain ST $12,000 / LT $48,000" in text
    assert "Proposed trim (to top of band (4.8%)): ~$30,000" in text
    assert "est. tax ~$4,895" in text
    assert "waiting 62d (ST→LT) saves ~$680" in text
    assert "wash-sale risk" in text
    assert "Placement: 40% of the position sits in Roth/HSA" in text
    # The MAGI assumption footnote keeps the estimate auditable.
    assert "$450-500k MAGI" in text


def test_render_tax_lines_unavailable_is_one_honest_line() -> None:
    lines = render_tax_lines(unavailable_tax_view("tracker offline (ConnectionError)"))
    assert lines == ["- Tax: unavailable (tracker offline (ConnectionError))"]


def test_render_tax_lines_approximate_shows_range_and_reasons() -> None:
    from dataclasses import replace as dc_replace

    view = dc_replace(
        _TAX_VIEW,
        approximate=True,
        approx_reasons=("transaction history unavailable from the tracker",),
        st_unrealized_usd=None,
        lt_unrealized_usd=None,
        trim=dc_replace(
            _TAX_TRIM,
            st_gain_usd=None,
            lt_gain_usd=None,
            tax_low_usd=1_200.0,
            tax_high_usd=2_000.0,
            days_until_lt=None,
            wait_savings_usd=None,
            wash_sale_risk=False,
        ),
    )
    text = "\n".join(render_tax_lines(view))
    assert "- Tax (approx):" in text
    assert "term split unknown" in text
    assert "est. tax ~$1,200-$2,000 (term unknown)" in text
    assert "Tax approx because: transaction history unavailable" in text
    assert "waiting" not in text


def test_render_tax_lines_none_is_empty_and_chat_still_renders() -> None:
    assert render_tax_lines(None) == []
    assert "Tax" not in render_pre_analysis_chat(_pre())  # pre-tax-stage object


def test_render_chat_includes_tax_block_when_present() -> None:
    out = render_pre_analysis_chat(_pre(tax=_TAX_VIEW))
    assert "- Tax: taxable 60% of position" in out
    # Tax lines sit above the mechanical read, before the CLI pointer.
    assert out.index("- Tax:") < out.index("- Mechanical read:")


# --------------------------------------------------------------------------- #
# command routing + dispatch (public run_chat_command)
# --------------------------------------------------------------------------- #


def test_review_is_a_routed_command_prefix() -> None:
    assert "/review" in COMMAND_PREFIXES
    # mirrors ask.engine.route_turn's exact check
    assert "/review RBRK at $70".lower().startswith(COMMAND_PREFIXES)


def test_review_command_usage_without_ticker() -> None:
    reply = run_chat_command(Path("."), "/review", cast("Registry", None))
    assert reply is not None and "Usage:" in reply


def test_review_command_dispatches_and_renders(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(*_a: object, **_k: object) -> PreAnalysis:
        return _pre(at_price=82.0)

    monkeypatch.setattr(pr, "build_pre_analysis", _fake)
    reply = run_chat_command(Path("."), "/review RBRK at $82", cast("Registry", None))
    assert reply is not None
    assert "**RBRK**" in reply and "Mechanical read:" in reply


def test_review_command_parses_at_price_through_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def _fake(*_a: object, **kwargs: object) -> PreAnalysis:
        seen["at_price"] = kwargs.get("at_price")
        return _pre()

    monkeypatch.setattr(pr, "build_pre_analysis", _fake)
    run_chat_command(Path("."), "/review FLKR at $70", cast("Registry", None))
    assert seen["at_price"] == 70.0
    run_chat_command(Path("."), "/review RBRK", cast("Registry", None))
    assert seen["at_price"] is None


# --------------------------------------------------------------------------- #
# /review background full-verdict job (Bug 2: production caller wiring)
# --------------------------------------------------------------------------- #


class _NonSpawningRegistry(Registry):
    """Records `start()` calls without forking a real subprocess."""

    def start(
        self,
        *,
        ticker: str,
        kind: str,
        argv: list[str],
        spawn: bool = True,
        cwd: str | None = None,
    ) -> Job:
        return super().start(ticker=ticker, kind=kind, argv=argv, spawn=False, cwd=cwd)


def test_review_command_starts_background_verdict_job_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake(*_a: object, **_k: object) -> PreAnalysis:
        return _pre(at_price=82.0)

    monkeypatch.setattr(pr, "build_pre_analysis", _fake)
    registry = _NonSpawningRegistry()
    reply = run_chat_command(Path("."), "/review RBRK at $82", registry)
    assert reply is not None
    # The instant pre-analysis still renders...
    assert "**RBRK**" in reply and "Mechanical read:" in reply
    # ...and the full verdict was kicked off in the background.
    assert "background" in reply.lower() and "job_" in reply
    jobs = registry.list_jobs()
    assert (
        len(jobs) == 1
        and jobs[0]["ticker"] == "RBRK"
        and jobs[0]["kind"] == ("position-review-verdict")
    )


def test_review_command_background_job_argv_carries_verdict_and_at_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: dict[str, object] = {}

    def _fake(*_a: object, **_k: object) -> PreAnalysis:
        return _pre()

    class _Capturing(_NonSpawningRegistry):
        def start(
            self,
            *,
            ticker: str,
            kind: str,
            argv: list[str],
            spawn: bool = True,
            cwd: str | None = None,
        ) -> Job:
            started["ticker"] = ticker
            started["argv"] = argv
            return super().start(ticker=ticker, kind=kind, argv=argv, spawn=spawn, cwd=cwd)

    monkeypatch.setattr(pr, "build_pre_analysis", _fake)
    run_chat_command(Path("/repo"), "/review FLKR at $70", _Capturing())
    argv = cast("list[str]", started["argv"])
    assert started["ticker"] == "FLKR"
    assert argv[1].endswith("review_position.py")
    assert "FLKR" in argv and "--verdict" in argv
    assert "--at-price" in argv and "70.0" in argv


def test_review_command_skips_background_job_without_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A surface with no job registry (e.g. the Telegram poller, which is a
    standalone loop with no Flask action/SSE plumbing to spawn against)
    degrades to the instant reply only — no crash, no dangling reference."""

    def _fake(*_a: object, **_k: object) -> PreAnalysis:
        return _pre()

    monkeypatch.setattr(pr, "build_pre_analysis", _fake)
    reply = run_chat_command(Path("."), "/review RBRK", None)
    assert reply is not None
    assert "background" not in reply.lower()


def test_review_command_respects_full_verdict_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake(*_a: object, **_k: object) -> PreAnalysis:
        return _pre()

    monkeypatch.setattr(pr, "build_pre_analysis", _fake)
    monkeypatch.setenv("REVIEW_FULL_VERDICT", "0")
    registry = _NonSpawningRegistry()
    reply = run_chat_command(Path("."), "/review RBRK", registry)
    assert reply is not None
    assert "background" not in reply.lower()
    assert registry.list_jobs() == []


def test_review_command_conflict_degrades_to_one_line_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ticker already under review (single-flight slot busy) must not break
    the instant reply — the background-kickoff failure degrades to a short
    note, same as every other RegistryConflict caller in this codebase."""
    from dispatch_registry import RegistryConflict

    def _fake(*_a: object, **_k: object) -> PreAnalysis:
        return _pre()

    class _Busy(_NonSpawningRegistry):
        def start(
            self,
            *,
            ticker: str,
            kind: str,
            argv: list[str],
            spawn: bool = True,
            cwd: str | None = None,
        ) -> Job:
            raise RegistryConflict("busy")

    monkeypatch.setattr(pr, "build_pre_analysis", _fake)
    reply = run_chat_command(Path("."), "/review RBRK", _Busy())
    assert reply is not None
    assert "already running" in reply.lower()
    assert "**RBRK**" in reply  # the instant pre-analysis still rendered
