"""Render exact reviewed financial references without changing memo claims.

This attaches reader links only. The ordinary memo verifier still owns source,
canonical admission, snapshot membership and complete-output acceptance.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from report.models import CellSource
from research.decision_brief import (
    MemoContextReview,
    MemoContextReviewV2,
    MemoEvidenceError,
    memo_reader_blocks,
    verify_memo_claim_population,
)
from ui.source_chip import source_chip_html


def render_memo_financial_links(
    body_html: str, review: MemoContextReview | MemoContextReviewV2
) -> str:
    """Add shared source chips at each reviewed block's exact financial claim."""
    soup = BeautifulSoup(body_html, "html.parser")
    verify_memo_claim_population(soup, review)
    before = memo_reader_blocks(body_html)
    blocks = {block.block_id: block for block in before}
    for claim in review.claims:
        references = (
            *claim.financial_references,
            *(value.reference for value in claim.values),
            *(claim.calculation.operands if claim.calculation else ()),
        )
        if not references:
            continue
        elements = soup.select(blocks[claim.block_id].selector)
        if len(elements) != 1:
            raise MemoEvidenceError("memo_financial_link_block_ambiguous")
        element = elements[0]
        for reference in dict.fromkeys(references):
            chip = BeautifulSoup(
                source_chip_html(
                    CellSource(source="canonical", canonical_reference=reference), link_only=True
                ),
                "html.parser",
            ).find("a")
            if chip is None:
                raise MemoEvidenceError("memo_financial_link_unavailable")
            if element.select_one(f'a[href="{chip["href"]}"]') is None:
                element.append(chip)
    result = str(soup)
    if memo_reader_blocks(result) != before:
        raise MemoEvidenceError("memo_financial_links_changed_reader_claims")
    verify_memo_claim_population(soup, review)
    return result
