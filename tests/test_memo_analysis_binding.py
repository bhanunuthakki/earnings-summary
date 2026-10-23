"""Analyst prose may state figures only when each binds to cited support."""

from __future__ import annotations

import sqlite3

import pytest

from research.decision_brief import MemoContextReviewV2, ReviewedMemoClaimV2, memo_reader_blocks
from research.memo_claim_support import (
    MemoClaimSupportError,
    MemoDerivedFigure,
    derived_figure_display,
    readable_source_text,
    verify_memo_analysis_text,
    verify_reported_memo_claim,
)

CALL = "Risk-Adjusted  NIM came in at 9.5 percent, down 100 basis points from 10.5 percent."
SPLIT = ("S", "&atilde;", "o Paulo, June 1, 2026 ", "&#8211;", " Nu appoints a CFO")
TABLE = "CET1 ratio ␟ &#9;11.3%&#9; ␟ &nbsp; ␟ &#9;13.0%&#9;"


def excerpt(text: str, *nodes: str) -> tuple[str, tuple[str, ...]]:
    return text, nodes


def test_readable_text_decodes_entities_separators_and_ligatures() -> None:
    assert (
        readable_source_text("".join(SPLIT)) == "São Paulo, June 1, 2026 \u2013 Nu appoints a CFO"
    )
    assert readable_source_text(TABLE) == "CET1 ratio | 11.3% | | 13.0%"
    assert readable_source_text("the \ufb01rst quarter") == "the first quarter"


def test_bound_figures_and_calendar_references_pass() -> None:
    verify_memo_analysis_text(
        "In Q1 2026 risk-adjusted NIM fell 100 basis points to 9.5%; CET1 fell to 11.3%.",
        (
            excerpt("came in at 9.5 percent, down 100 basis points", CALL),
            excerpt("CET1 ratio | 11.3%", TABLE),
        ),
    )


def test_unbound_figure_fails_closed() -> None:
    with pytest.raises(MemoClaimSupportError, match="memo_analysis_number_unbound"):
        verify_memo_analysis_text(
            "Risk-adjusted NIM fell to 9.5% and should reach 11% next year.",
            (excerpt("came in at 9.5 percent", CALL),),
        )
    with pytest.raises(MemoClaimSupportError, match="memo_analysis_number_unbound"):
        verify_memo_analysis_text("Net income should grow 25%.", ())


def test_excerpt_must_be_verbatim_in_its_nodes() -> None:
    verify_memo_analysis_text("Announced in São Paulo.", (excerpt("São Paulo, June 1", *SPLIT),))
    with pytest.raises(MemoClaimSupportError, match="memo_excerpt_not_in_source"):
        verify_memo_analysis_text("NIM fell.", (excerpt("NIM collapsed to 9.5 percent", CALL),))


def test_quoted_wording_must_occur_in_a_cited_excerpt() -> None:
    cited = (excerpt("down 100 basis points from 10.5 percent", CALL),)
    verify_memo_analysis_text(
        "Management said it was “down 100 basis points from 10.5 percent”.", cited
    )
    with pytest.raises(MemoClaimSupportError, match="memo_quoted_span_not_in_excerpt"):
        verify_memo_analysis_text("Management called it “a temporary seasonal dip”.", cited)


def test_derived_figures_replay_from_bound_operands() -> None:
    figure = MemoDerivedFigure(
        expression="ni*(1-hi/100)/(1-q1/100)",
        operands={"ni": "871.4", "hi": "20", "q1": "8.7"},
        displayed_value="764",
        decimals=0,
    )
    assert derived_figure_display(figure) == "764"
    guide = excerpt(
        "the 8.7 percent rate converges toward 15 to 20 percent",
        "the 8.7 percent rate converges toward 15 to 20 percent",
    )
    verify_memo_analysis_text(
        "Normalized net income is about $764m.", (guide,), ("871.4",), (figure,)
    )
    with pytest.raises(MemoClaimSupportError, match="memo_derived_operand_unbound"):
        verify_memo_analysis_text("Normalized net income is about $764m.", (guide,), (), (figure,))
    wrong = figure.model_copy(update={"displayed_value": "800"})
    with pytest.raises(MemoClaimSupportError, match="memo_derived_value_mismatch"):
        verify_memo_analysis_text(
            "Normalized net income is about $800m.", (guide,), ("871.4",), (wrong,)
        )


def test_whole_node_reconstruction_accepts_the_readable_form() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE evidence_nodes (node_id TEXT PRIMARY KEY, text TEXT)")
    conn.executemany(
        "INSERT INTO evidence_nodes VALUES (?, ?)",
        [(f"node-{index}", text) for index, text in enumerate(SPLIT)],
    )
    ids = tuple(f"node-{index}" for index in range(len(SPLIT)))
    verify_reported_memo_claim(conn, "São Paulo, June 1, 2026 \u2013 Nu appoints a CFO", ids)
    with pytest.raises(MemoClaimSupportError, match="memo_reported_passage_not_reconstructed"):
        verify_reported_memo_claim(conn, "São Paulo, June 2, 2026 \u2013 Nu appoints a CFO", ids)


def test_reader_blocks_drop_cite_chips_but_keep_cited_values() -> None:
    body = (
        '<section data-voice="analyst"><p>NIM fell to <span class="cite-wrap">'
        '<span class="cite-val">9.5%</span><a class="cite-mark">3</a>'
        '<span class="cite-pop">Q1 2026 earnings call, page 6</span></span> in Q1.</p></section>'
    )
    (block,) = memo_reader_blocks(body)
    assert block.text == "NIM fell to 9.5% in Q1."
    assert block.visibly_inferred


def test_empty_new_claim_fields_keep_earlier_serialization() -> None:
    claim = ReviewedMemoClaimV2(
        block_id="0" * 64,
        passage="Revenue rose.",
        kind="analyst_inference",
        rationale="Unchanged earlier claim serialization check.",
    )
    assert "excerpts" not in claim.model_dump_json()
    assert "derived_values" not in claim.model_dump_json()


def test_owner_thesis_must_be_hash_pinned_and_quoted_verbatim() -> None:
    import hashlib

    from research.decision_brief import MemoOwnerThesis

    text = "Thesis breaks if CET1 falls below 13%."
    thesis = MemoOwnerThesis(
        ticker="NU", revision="r", sha256=hashlib.sha256(text.encode()).hexdigest(), text=text
    )
    verify_memo_analysis_text("Your floor is 13%.", (("CET1 falls below 13%", (thesis.text,)),))
    with pytest.raises(ValueError, match="owner_thesis_hash_mismatch"):
        MemoOwnerThesis(ticker="NU", revision="r", sha256="0" * 64, text=text)


def test_analyst_prose_may_carry_model_values_but_facts_may_not() -> None:
    from bs4 import BeautifulSoup

    from research.decision_brief import (
        MemoContextReviewV2,
        MemoEvidenceError,
        verify_memo_claim_population,
    )

    value = {
        "model": "meli_platform_sotp",
        "source": "scenario",
        "key": "vps",
        "scenario": "base",
        "period": None,
        "wacc_delta": None,
        "credit_ke_delta": None,
        "unit": "USD_per_share",
        "displayed_unit": "USD_per_share",
        "display_format": "number2",
        "rounding": "half_even",
        "displayed_value": "1767.72",
    }
    body = '<section data-voice="analyst"><p>Base value is $1767.72 per share.</p></section>'
    (block,) = memo_reader_blocks(body)

    def review(kind: str) -> MemoContextReviewV2:
        claim = ReviewedMemoClaimV2.model_validate(
            {
                "block_id": block.block_id,
                "passage": block.text,
                "kind": kind,
                "model_values": [value],
                "rationale": "Model output stated in analyst prose for this test.",
            }
        )
        return MemoContextReviewV2.model_construct(claims=(claim,))

    verify_memo_claim_population(BeautifulSoup(body, "html.parser"), review("analyst_inference"))
    with pytest.raises(MemoEvidenceError, match="memo_calculation_kind_mismatch"):
        verify_memo_claim_population(BeautifulSoup(body, "html.parser"), review("reported_fact"))


def _owner_calculation_review(
    key: str = "wacc", value: str = "0.11", displayed: str = "11", *, kind: str = "calculation"
) -> MemoContextReviewV2:
    import hashlib
    from datetime import UTC, datetime

    from research.decision_brief import MemoContextReviewV2

    text = f'{{"{key}": {value}}}'
    return MemoContextReviewV2.model_validate(
        {
            "artifact_id": "owner-calculation",
            "body_sha256": "a" * 64,
            "research_snapshot_id": "synthetic",
            "reviewed_section_ids": ["assumptions"],
            "reviewer": "synthetic-test",
            "reviewed_at": datetime(2026, 1, 1, tzinfo=UTC),
            "rationale": "Verify owner assumptions without treating them as issuer facts.",
            "owner_thesis": {
                "ticker": "MELI",
                "revision": "test",
                "text": text,
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
            },
            "claims": [
                {
                    "block_id": "b" * 64,
                    "passage": f"Your {key}, percent: {value} \u00d7 100 = {displayed}",
                    "kind": kind,
                    "owner_thesis_excerpts": [f'"{key}": {value}'],
                    "derived_values": [
                        {
                            "expression": "x*100",
                            "operands": {"x": value},
                            "displayed_value": displayed,
                            "decimals": 0,
                        }
                    ],
                    "rationale": "Convert the pinned owner assumption to percent.",
                }
            ],
        }
    )


def _verify_owner_calculation(
    review: MemoContextReviewV2,
) -> tuple[frozenset[str], frozenset[str]]:
    from datetime import UTC, datetime

    from provenance.research_snapshot import (
        CorpusProjectionBundle,
        ResearchSnapshotRequest,
        ResearchUniverse,
    )
    from research.decision_brief import verify_memo_claims

    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    snapshot = ResearchSnapshotRequest(
        research_snapshot_id="synthetic",
        idempotency_key="synthetic",
        research_universe=ResearchUniverse(
            issuer_id="issuer",
            reporting_entity_ids=("entity",),
            document_version_ids=("document",),
            source_obligation_revision_ids=("obligation",),
        ),
        processing_snapshot_ids=("processing",),
        corpus_bundles=(
            CorpusProjectionBundle(corpus_manifest_id="corpus", lexical_index_run_id="lexical"),
        ),
        source_fact_publication_ids=(),
        ontology_snapshot_id="ontology",
        canonical_fact_resolution_snapshot_id="resolution",
        canonical_fact_projection_run_id="projection",
        cutoff_at=stamp,
        recorded_at=stamp,
    )
    with sqlite3.connect(":memory:") as conn:
        return verify_memo_claims(
            conn,
            review=review,
            snapshot=snapshot,
            ticker="MELI",
            snapshots={},
            supporting_contexts={},
            used_contexts=set(),
            cutoff=stamp,
            verified_model=None,
            earnings=None,
        )


@pytest.mark.parametrize(
    ("key", "value", "displayed"), [("wacc", "0.11", "11"), ("mos_bar", "0.2", "20")]
)
def test_owner_only_calculation_reaches_the_existing_arithmetic_verifier(
    key: str, value: str, displayed: str
) -> None:
    assert _verify_owner_calculation(_owner_calculation_review(key, value, displayed)) == (
        frozenset(("b" * 64,)),
        frozenset(),
    )


def test_owner_only_calculation_rejects_wrong_result_and_changed_excerpt() -> None:
    from research.decision_brief import MemoContextReviewV2, MemoEvidenceError

    with pytest.raises(MemoEvidenceError, match="memo_derived_value_mismatch"):
        _verify_owner_calculation(_owner_calculation_review(displayed="12"))
    payload = _owner_calculation_review().model_dump()
    payload["claims"][0]["owner_thesis_excerpts"] = ['"wacc": 0.12']
    with pytest.raises(MemoEvidenceError, match="memo_excerpt_not_in_source"):
        _verify_owner_calculation(MemoContextReviewV2.model_validate(payload))


@pytest.mark.parametrize("kind", ["reported_fact", "management_claim"])
def test_owner_assumption_does_not_support_a_reported_claim(kind: str) -> None:
    from research.decision_brief import MemoEvidenceError

    with pytest.raises(MemoEvidenceError, match="memo_reported_claim_support_missing"):
        _verify_owner_calculation(_owner_calculation_review(kind=kind))


def test_owner_only_calculation_requires_a_pinned_thesis_and_a_derived_value() -> None:
    from research.decision_brief import MemoContextReviewV2, MemoEvidenceError

    payload = _owner_calculation_review().model_dump()
    payload["owner_thesis"] = None
    with pytest.raises(ValueError, match="owner_thesis_excerpt_without_pinned_thesis"):
        MemoContextReviewV2.model_validate(payload)
    payload = _owner_calculation_review().model_dump()
    payload["claims"][0]["derived_values"] = []
    with pytest.raises(MemoEvidenceError, match="memo_reported_claim_support_missing"):
        _verify_owner_calculation(MemoContextReviewV2.model_validate(payload))
