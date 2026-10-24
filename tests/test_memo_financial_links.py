"""Financial source links keep exact references and the existing claim population."""

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup

from research.decision_brief import (
    MemoContextReviewV2,
    MemoEvidenceError,
    ReviewedMemoClaimV2,
    memo_reader_blocks,
)
from research.memo_financial_links import render_memo_financial_links
from sources.report_financials import FinancialEvidenceReference


def test_links_keep_the_exact_reference_clocks_and_reader_blocks() -> None:
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    reference = FinancialEvidenceReference(
        ticker="SYNTH",
        concept="revenue",
        canonical_metric_cell_id="cell",
        observation_id="observation",
        canonical_resolution_revision_id="resolution",
        metric_definition_revision_id="definition",
        as_of=stamp,
        resolution_observed_through=stamp,
    )
    body = '<main><section><p><span class="cite">[1]</span> Revenue was $120m.</p></section></main>'
    (block,) = memo_reader_blocks(body)
    review = MemoContextReviewV2(
        artifact_id="synthetic",
        body_sha256="a" * 64,
        research_snapshot_id="snapshot",
        claims=(
            ReviewedMemoClaimV2(
                block_id=block.block_id,
                passage=block.text,
                kind="reported_fact",
                financial_references=(reference,),
                rationale="Exact synthetic financial reference.",
            ),
        ),
        reviewed_section_ids=("financials",),
        reviewer="synthetic-test",
        reviewed_at=stamp,
        rationale="Test source links without certifying the financial facts.",
    )
    rendered = render_memo_financial_links(body, review)
    (link,) = BeautifulSoup(rendered, "html.parser").select('a[href*="canonical-financial"]')
    href = str(link["href"])
    assert (
        FinancialEvidenceReference.model_validate_json(
            parse_qs(urlsplit(href).query)["reference"][0]
        )
        == reference
    )
    assert memo_reader_blocks(rendered) == memo_reader_blocks(body)
    assert render_memo_financial_links(rendered, review) == rendered
    with pytest.raises(MemoEvidenceError, match="memo_claim_block_mismatch"):
        render_memo_financial_links(body.replace("120", "999"), review)
