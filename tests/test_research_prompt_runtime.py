"""Regressions for evidence boundaries in investment prompt owners."""

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

import llm_client
from research.method_contract import ResearchMethod, ResearchMode
from synthesis.lenses import five_min_reread as five
from triggers import earnings_tone

_render_prompt = vars(earnings_tone)["_render_prompt"]


def test_stale_and_unknown_corpus_preserve_owner_rules() -> None:
    unknown = vars(llm_client)["_compute_staleness"]("2026-10-04", None)
    assert "Corpus is current" not in unknown[3]
    stale = vars(llm_client)["_compute_staleness"]("2026-10-04", "2025-01-01")
    prompt = vars(llm_client)["_build_pass_a_prompt"](
        "NU", {}, [], "2026-10-04", stale[2], stale[1], stale[3], ""
    )
    assert "REPLACE" not in stale[3]
    assert "vs. Break Threshold" in prompt
    assert "±X% uncertainty band" not in prompt
    assert "Research method" in prompt


def test_verdict_prompt_does_not_invent_allocation_or_sell_policy() -> None:
    prompt = vars(llm_client)["_build_pass_b_prompt"]("NU", {}, [], "2026-10-04", "", "", "", "")
    for forbidden in ("defaults to CUT/PASS", "≤8\u201310%", "3\u20135%", "N quarters minimum"):
        assert forbidden not in prompt
    assert "not a sell signal" in prompt
    assert "Research method" in prompt


def test_five_minute_missing_values_and_review_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(five, "load_dcf", Mock(return_value={"valuation_date": "2026-10-04"}))
    monkeypatch.setattr(five, "load_recent_summaries", Mock(return_value=[]))
    monkeypatch.setattr(five, "load_recent_insider_transactions", Mock(return_value=[]))
    monkeypatch.setattr(five, "load_predictions", Mock(return_value=[]))
    monkeypatch.setattr(five, "thesis_block", Mock(return_value="accepted rule"))
    ctx = vars(five)["_ctx_five_min_reread"]("NU", Path("/synthetic"))
    assert ctx is not None
    assert "$0" not in ctx.template_kwargs["dcf_summary"]
    assert "unavailable" in ctx.template_kwargs["dcf_summary"]
    assert "prior review is not supplied" in five.LENS.prompt_template
    assert "ADD <N%>" not in five.LENS.prompt_template


def test_tone_limits_absence_and_requires_matched_text() -> None:
    prompt = _render_prompt(
        ticker="NU",
        fiscal_period_type="Q2",
        fiscal_period="2026",
        thesis_anchor_block="",
        current_prepared_remarks="Speaker: CFO\nCosts improve",
        current_qa="",
        prior_transcripts=[],
    )
    assert "same speaker" in prompt
    assert "uncalibrated" in prompt
    assert "coverage is unknown" in prompt
    assert "Research method" in prompt


def test_summary_retains_middle_qa_and_stops_before_oversize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from research.method_contract import ResearchInputLimitError

    captured: list[str] = []

    def capture(prompt: str, **_: object) -> str:
        captured.append(prompt)
        return "note"

    monkeypatch.setattr(llm_client, "call_llm", capture)
    middle = "Analyst: two parts?\nCFO: first answer\nCEO: second answer\nAnalyst: follow-up"
    llm_client.generate_summary("a" * 35000 + middle + "z" * 35000, ticker="NU")
    assert middle in captured[0]
    assert "Research method" in captured[0]
    with pytest.raises(ResearchInputLimitError):
        llm_client.generate_summary("a" * 320000, ticker="NU")
    assert len(captured) == 1


def test_company_prompt_no_forced_precision_or_consensus() -> None:
    prompt = llm_client.build_company_description_prompt("NU", "bank", None, None, "", [], [], None)
    assert "MUST cite at least one specific number" not in prompt
    assert "TESTABLE quantified hypothesis" not in prompt
    assert "source_sha256" in prompt


def test_company_cache_changes_for_displayed_names_method_and_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, migrated_db: Callable[..., Path]
) -> None:
    import sqlite3
    from dataclasses import replace

    from compute import company_description as company

    db = migrated_db(tmp_path / "synthetic.db")
    conn = sqlite3.connect(db)
    profiles = {"description": "public supplied profile", "sector": "banking"}
    names = ["Brazil"]
    calls = Mock(
        return_value={
            "elevator_pitch": "draft",
            "business_overview": "",
            "revenue_model": "",
            "segments": [],
            "geographies": [],
        }
    )
    monkeypatch.setattr(company, "locate_annual_filing", Mock(return_value=(None, None)))
    monkeypatch.setattr(company, "load_canonical_narrative", Mock(return_value=None))
    monkeypatch.setattr(company, "load_profile", Mock(return_value=profiles))
    monkeypatch.setattr(company, "segment_names_from_db", Mock(return_value=names))
    for attr in (
        "_load_thesis",
        "_load_recent_earnings_md",
        "_load_recent_ir_docs_md",
        "load_ir_anchor",
    ):
        monkeypatch.setattr(company, attr, Mock(return_value=""))
    monkeypatch.setattr(company, "_call_llm", calls)
    try:
        company.extract_for_ticker("NU", tmp_path, conn)
        company.extract_for_ticker("NU", tmp_path, conn)
        assert calls.call_count == 1
        names.append("Mexico")
        company.extract_for_ticker("NU", tmp_path, conn)
        profiles["sector"] = "financial services"
        company.extract_for_ticker("NU", tmp_path, conn)
        original = llm_client.load_research_method

        def changed_method(mode: ResearchMode) -> ResearchMethod:
            return replace(original(mode), source_sha256="f" * 64)

        monkeypatch.setattr(llm_client, "load_research_method", changed_method)
        company.extract_for_ticker("NU", tmp_path, conn)
        assert calls.call_count == 4
    finally:
        conn.close()


def test_tone_cache_changes_when_body_changes_under_same_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, migrated_db: Callable[..., Path]
) -> None:
    from triggers import earnings_tone as tone

    db = migrated_db(tmp_path / "synthetic.db")
    calls = Mock(
        return_value={
            "summary": "comparison unavailable",
            "shifts": [],
            "no_material_shifts_detected": True,
        }
    )
    monkeypatch.setattr(tone, "_call_llm_with_retry", calls)
    args: dict[str, Any] = dict(
        ticker="NU",
        fiscal_period_type="Q2",
        fiscal_period="2026-06-30",
        thesis_anchor_block="",
        current_prepared_remarks="CEO: supplied wording",
        current_qa="",
        prior_transcripts=[],
        cache_inputs=["same-identity"],
        db_path=db,
    )
    trigger = tone.EarningsToneTrigger()
    vars(type(trigger))["_read_cached_or_call_llm"](trigger, **args)
    vars(type(trigger))["_read_cached_or_call_llm"](trigger, **args)
    assert calls.call_count == 1
    args["current_prepared_remarks"] = "CEO: corrected supplied wording"
    vars(type(trigger))["_read_cached_or_call_llm"](trigger, **args)
    assert calls.call_count == 2


def test_lens_exact_prompt_cache_and_explicit_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, migrated_db: Callable[..., Path]
) -> None:
    import importlib

    from db_paths import resolve_db_path

    shared = importlib.import_module("synthesis.lenses._shared")

    db = migrated_db(tmp_path / "synthetic.db")
    calls: list[str] = []
    seen: list[Path | None] = []
    data = {"text": "public observation"}

    def context(*_: object) -> Any:
        seen.append(resolve_db_path(None))
        return shared.LensContext("NU", data.copy(), ["stable"], [], [])

    lens = shared.Lens("five_min_reread", "claude-sonnet-4-6", "ticker", "{text}", context)

    def capture(prompt: str, **_: object) -> str:
        calls.append(prompt)
        return "brief"

    monkeypatch.setattr(shared, "call_llm", capture)
    monkeypatch.setattr(shared, "load_grounded_numbers", Mock(return_value=None))
    first = shared.run_lens(lens, ticker="NU", repo_root=tmp_path, db_path=db)
    again = shared.run_lens(lens, ticker="NU", repo_root=tmp_path, db_path=db)
    assert first is not None and again is not None and first.id == again.id
    assert len(calls) == 1
    assert isinstance(first.content_json, dict)
    assert first.content_json["rendered_prompt"] == calls[0]
    assert first.content_json["source_identity_status"] == "partial"
    data["text"] = "corrected public observation"
    updated = shared.run_lens(lens, ticker="NU", repo_root=tmp_path, db_path=db)
    assert updated is not None and updated.id != first.id
    assert len(calls) == 2
    assert seen == [db.resolve()] * 3
    assert not (tmp_path / "data/portfolio.db").exists()
