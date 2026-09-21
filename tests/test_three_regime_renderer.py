"""Hermetic unit tests for deterministic three-regime rendering."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from evals.regime_backtest import SourceRegime, StratumCohort
from pipeline.three_regime_renderer import (
    RenderedSectionPayload,
    SectionRenderStatus,
    SingleRegimeRenderOutput,
    ThreeRegimeDeterministicRenderer,
    ThreeRegimeRenderReceipt,
)


def test_three_regime_renderer_models_frozen_immutability() -> None:
    """Assert rendered section, single regime output, and receipt models reject mutations and extra fields."""
    sec = RenderedSectionPayload(
        section_id="overview",
        section_name="Overview",
        regime=SourceRegime.REGIME_2_COMBINED,
        status=SectionRenderStatus.COMPLETE,
        source_lineage="CANONICAL",
        currency="USD",
        fiscal_period="FY2025",
        metrics={"revenue": Decimal("1000")},
        content_html="<section>HTML</section>",
        content_markdown="## Markdown",
    )
    with pytest.raises(ValidationError):
        setattr(sec, "currency", "EUR")

    out = SingleRegimeRenderOutput(
        ticker="RBRK",
        regime=SourceRegime.REGIME_2_COMBINED,
        stratum=StratumCohort.STRATUM_10K_OPERATING,
        as_of_date=date(2026, 4, 30),
        currency="USD",
        html_sha256="0" * 64,
        markdown_sha256="0" * 64,
        sections_json_sha256="0" * 64,
        sections_count=1,
        two_pass_byte_identical=True,
        sections=(sec,),
    )
    with pytest.raises(ValidationError):
        setattr(out, "two_pass_byte_identical", False)

    receipt = ThreeRegimeRenderReceipt(
        run_id="run_1",
        as_of_date=date(2026, 4, 30),
        total_tickers=1,
        total_regimes=1,
        total_render_outputs=1,
        all_two_pass_verified=True,
        status="PASS",
        render_outputs=(out,),
        verified_at=datetime.now(UTC),
    )
    with pytest.raises(ValidationError):
        setattr(receipt, "status", "HOLD")


@pytest.mark.parametrize("tickers", [["META", "NU"], ["NO_SUCH_ISSUER"], []])
def test_missing_sealed_inputs_cannot_certify_render(tickers: list[str]) -> None:
    receipt = ThreeRegimeDeterministicRenderer().render_all_regimes_for_cohort(tickers)
    assert receipt.status == "HOLD"
    assert receipt.render_outputs == ()
    assert receipt.total_render_outputs == 0
    assert receipt.all_two_pass_verified is False


def test_single_render_refuses_unbound_financial_values() -> None:
    with pytest.raises(ValueError, match="sealed source inputs"):
        ThreeRegimeDeterministicRenderer().render_ticker_regime(
            "NO_SUCH_ISSUER", SourceRegime.REGIME_2_COMBINED
        )


def test_operational_cli_exits_nonzero_with_hold_receipt(tmp_path: Path) -> None:
    receipt_path = tmp_path / "receipt.json"
    result = subprocess.run(
        [
            sys.executable,
            "execution/render_three_regimes.py",
            "--output-receipt",
            str(receipt_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    receipt = json.loads(receipt_path.read_text())
    assert receipt["status"] == "HOLD"
    assert receipt["reason_codes"]
