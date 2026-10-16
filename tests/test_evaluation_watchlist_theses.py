"""Verify evaluation watchlist thesis persistence and programmatic surfacing.

Tests that TSM, LITE, CPNG, and ONON thesis research files exist, adhere to
BreakRule schema, register in entity_seed, and surface through both
ticker_command_center and the thesis report section.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from compute.thesis_evaluator import BreakRule
from entity_seed import all_known_tickers
from pipeline.ticker_command_center import build_ticker_command_center
from report.models import SectionStatus
from report.sections import thesis as thesis_section

EVALUATION_TICKERS = ("TSM", "LITE", "CPNG", "ONON")


def test_evaluation_tickers_known_in_entity_seed() -> None:
    known = all_known_tickers()
    for ticker in EVALUATION_TICKERS:
        assert ticker in known, f"{ticker} must be in all_known_tickers()"


def test_evaluation_theses_valid_schema() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    holdings_dir = repo_root / "micro_thesis" / "holdings"

    for ticker in EVALUATION_TICKERS:
        path = holdings_dir / f"{ticker}.json"
        assert path.is_file(), f"{path} must exist"
        raw: object = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(raw, dict), f"{path} must be a JSON object"
        payload = cast("dict[str, object]", raw)

        assert payload.get("ticker") == ticker
        assert payload.get("thesis"), f"{ticker} must have thesis text"
        assert payload.get("verdict") in ("Evaluation", "Watch")

        tier_1 = payload.get("tier_1_kpis")
        assert isinstance(tier_1, list), f"{ticker} tier_1_kpis must be a list"
        tier_1_list = cast("list[object]", tier_1)
        assert len(tier_1_list) >= 4, f"{ticker} tier-1 KPIs missing"

        break_rules = payload.get("break_rules")
        assert isinstance(break_rules, list), f"{ticker} break_rules must be a list"
        break_rules_list = cast("list[object]", break_rules)
        validated_rules = [BreakRule.model_validate(r) for r in break_rules_list]
        assert len(validated_rules) >= 2, f"{ticker} must have at least 2 break rules"


def test_evaluation_theses_surface_in_command_center() -> None:
    repo_root = Path(__file__).resolve().parents[1]

    for ticker in EVALUATION_TICKERS:
        tcc = build_ticker_command_center(repo_root, ticker)
        assert tcc.thesis.present is True, f"{ticker} thesis must be present in command center"
        assert tcc.thesis.thesis, f"{ticker} thesis text must not be empty"
        assert tcc.thesis.verdict in ("Evaluation", "Watch")
        assert len(tcc.thesis.tier1) >= 4
        assert len(tcc.thesis.break_rules) >= 2
        assert tcc.identity.name, f"{ticker} identity name must resolve"


def test_evaluation_theses_surface_in_report_section() -> None:
    repo_root = Path(__file__).resolve().parents[1]

    for ticker in EVALUATION_TICKERS:
        sec = thesis_section.build(ticker, repo_root)
        assert sec.status in (SectionStatus.OK, SectionStatus.PARTIAL)
        assert sec.thesis_full, f"{ticker} thesis_full must not be empty"
        tier_one_kpis = [row for row in sec.kpi_ledger if row.tier == "tier_1"]
        assert len(tier_one_kpis) >= 4, f"{ticker} kpi_ledger must have tier-1 rows"
