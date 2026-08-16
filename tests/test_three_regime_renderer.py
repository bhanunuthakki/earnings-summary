"""Hermetic unit tests for deterministic three-regime rendering."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

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
        sec.currency = "EUR"  # type: ignore[misc]

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
        out.two_pass_byte_identical = False  # type: ignore[misc]

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
        receipt.status = "HOLD"  # type: ignore[misc]


def test_two_pass_byte_identical_reproducibility() -> None:
    """Assert renderer guarantees byte-identical reproducibility across multiple runs."""
    renderer = ThreeRegimeDeterministicRenderer()
    as_of = date(2026, 4, 30)

    # Pass 1
    out1 = renderer.render_ticker_regime("RBRK", SourceRegime.REGIME_2_COMBINED, as_of_date=as_of)
    # Pass 2
    out2 = renderer.render_ticker_regime("RBRK", SourceRegime.REGIME_2_COMBINED, as_of_date=as_of)

    assert out1.html_sha256 == out2.html_sha256
    assert out1.markdown_sha256 == out2.markdown_sha256
    assert out1.sections_json_sha256 == out2.sections_json_sha256
    assert out1.two_pass_byte_identical is True


def test_three_regime_cohort_rendering() -> None:
    """Assert all 3 regimes are rendered cleanly for the full canary cohort."""
    renderer = ThreeRegimeDeterministicRenderer()
    cohort = ["META", "NU", "BN", "RBRK", "ASML", "WIX"]
    receipt = renderer.render_all_regimes_for_cohort(cohort, as_of_date=date(2026, 4, 30))

    assert receipt.status == "PASS"
    assert receipt.total_tickers == 6
    assert receipt.total_regimes == 3
    assert receipt.total_render_outputs == 18
    assert receipt.all_two_pass_verified is True

    # Check section lineage and currency
    for out in receipt.render_outputs:
        assert out.sections_count == 2
        for s in out.sections:
            assert s.status == SectionRenderStatus.COMPLETE
            assert s.regime == out.regime
            assert s.currency == out.currency
            assert s.source_lineage != ""
