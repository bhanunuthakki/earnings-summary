"""Canonical monetary sums do not force bank stocks through report-table aliases."""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest
from bs4 import BeautifulSoup

from provenance.canonical_fact_resolution import (
    CanonicalFactResolutionEngine,
    ResolutionPolicy,
    ResolutionSnapshotScope,
)
from provenance.population_metric_ontology import admit_exact_reported_observations
from provenance.research_snapshot import ResearchSnapshotRequest
from research import decision_brief as briefs
from search.canonical_fact_projection import read_canonical_facts_by_id
from tests.test_meli_input_evidence import seal_model_projection_context
from tests.test_report_canonical_financials import STAMP, seed_table
from tests.test_report_canonical_financials import database as database


def fixture(
    conn: sqlite3.Connection, *, mismatch: str | None = None
) -> tuple[ResearchSnapshotRequest, tuple[briefs.MemoObservationReference, ...]]:
    conn.row_factory = sqlite3.Row
    facts = seed_table(
        conn,
        [
            ("card_allowance", "", "2026-03-31", "Q1", "-4165671000", "USD"),
            (
                "loan_allowance",
                "2026-01-01" if mismatch == "duration" else "",
                "2026-03-31",
                "Q1",
                "-1935573000",
                "USD",
            ),
        ],
        populate=False,
        concept_namespace="reviewed-sec:0000000001",
        accounting_basis="management" if mismatch == "basis" else "ifrs",
        currencies={1: "BRL"} if mismatch == "currency" else None,
    )
    admit_exact_reported_observations(
        conn,
        tuple(f.observation.observation_id for f in facts),
        knowledge_cutoff=STAMP,
        operation_recorded_at=STAMP,
        apply=True,
    )
    conn.commit()
    snapshot = ResearchSnapshotRequest.model_validate(
        {
            "research_snapshot_id": "sum-unit-boundary",
            "idempotency_key": "sum-unit-boundary",
            "research_universe": {
                "issuer_id": "issuer-1",
                "reporting_entity_ids": ["reporting-1"],
                "document_version_ids": ["report-document"],
                "source_obligation_revision_ids": ["obligation"],
            },
            "processing_snapshot_ids": ["not-a-completeness-claim"],
            "corpus_bundles": [
                {
                    "corpus_manifest_id": "not-a-completeness-claim",
                    "lexical_index_run_id": "not-a-completeness-claim",
                }
            ],
            "source_fact_publication_ids": ["report-publication"],
            "ontology_snapshot_id": "sum-ontology",
            "canonical_fact_resolution_snapshot_id": "sum-resolution",
            "canonical_fact_projection_run_id": "sum-projection",
            "cutoff_at": STAMP,
            "recorded_at": STAMP,
        }
    )
    resolver = CanonicalFactResolutionEngine(conn)
    for row in conn.execute(
        "SELECT canonical_metric_cell_id FROM canonical_metric_cells"
    ).fetchall():
        resolver.resolve(
            str(row[0]),
            STAMP,
            ResolutionPolicy(name="synthetic", version="1", config={}),
            recorded_at=STAMP,
        )
    resolver.seal_snapshot(
        snapshot.canonical_fact_resolution_snapshot_id,
        STAMP,
        STAMP,
        ResolutionSnapshotScope(issuer_id="issuer-1", reporting_entity_ids=("reporting-1",)),
    )
    conn.commit()
    seal_model_projection_context(conn, snapshot)
    rows = conn.execute(
        "SELECT canonical_metric_cell_id FROM canonical_metric_cells ORDER BY canonical_metric_cell_id"
    ).fetchall()
    hits = read_canonical_facts_by_id(
        conn,
        generation_id=snapshot.canonical_fact_projection_run_id,
        canonical_metric_cell_ids=tuple(str(r[0]) for r in rows),
    )
    by_observation = {str(h.lineage["selected_observation_id"]): h for h in hits if h is not None}
    refs: list[briefs.MemoObservationReference] = []
    for f in facts:
        h = by_observation[f.observation.observation_id]
        refs.append(
            briefs.MemoObservationReference(
                observation_id=f.observation.observation_id,
                canonical_metric_cell_id=h.canonical_metric_cell_id,
                canonical_resolution_revision_id=str(h.lineage["canonical_resolution_revision_id"]),
                metric_definition_revision_id=str(h.lineage["metric_definition_revision_id"]),
            )
        )
    return snapshot, tuple(refs)


def test_bank_stock_sum_is_signed_scaled_and_bound_to_exact_snapshot(
    database: sqlite3.Connection,
) -> None:
    snapshot, refs = fixture(database)
    assert briefs.read_memo_observation_sum(database, refs, snapshot) == Decimal("-6101.244")
    calculation = briefs.MemoObservationSum(
        operands=refs, displayed_value="-6101.2", display_format="number1"
    )
    markup = "<p>Combined signed allowances in USD millions: -6101.2.</p>"
    block = briefs.memo_reader_blocks(markup)[0]
    claim = briefs.ReviewedMemoClaimV2(
        block_id=block.block_id,
        passage=block.text,
        kind="calculation",
        observation_sum=calculation,
        rationale="Exact separately admitted stocks share currency, basis, scope and period; not a managerial total.",
    )
    review = briefs.MemoContextReviewV2(
        artifact_id="test",
        body_sha256="a" * 64,
        research_snapshot_id="sum-unit-boundary",
        claims=(claim,),
        reviewed_section_ids=("financials",),
        reviewer="synthetic",
        reviewed_at=STAMP,
        rationale="Unit boundary only; this fixture does not certify a complete source corpus.",
    )
    briefs.verify_memo_claim_population(BeautifulSoup(markup, "html.parser"), review)
    briefs.verify_memo_observation_sum(database, calculation, snapshot)
    with pytest.raises(briefs.MemoEvidenceError, match="value_mismatch"):
        briefs.verify_memo_observation_sum(
            database, calculation.model_copy(update={"displayed_value": "6101.2"}), snapshot
        )
    with pytest.raises(briefs.MemoEvidenceError, match="identity"):
        briefs.read_memo_observation_sum(
            database,
            (refs[0].model_copy(update={"observation_id": "not-selected"}), refs[1]),
            snapshot,
        )
    with pytest.raises(briefs.MemoEvidenceError, match="identity"):
        briefs.read_memo_observation_sum(
            database,
            (refs[0].model_copy(update={"metric_definition_revision_id": "invented"}), refs[1]),
            snapshot,
        )
    with pytest.raises(briefs.MemoEvidenceError, match="duplicate"):
        briefs.read_memo_observation_sum(database, (refs[0], refs[0]), snapshot)


@pytest.mark.parametrize("mismatch", ["currency", "duration", "basis"])
def test_sum_rejects_currency_and_stock_flow_mixing(
    database: sqlite3.Connection, mismatch: str
) -> None:
    snapshot, refs = fixture(database, mismatch=mismatch)
    with pytest.raises(briefs.MemoEvidenceError, match="scope"):
        briefs.read_memo_observation_sum(database, refs, snapshot)


@pytest.mark.parametrize(
    "field,value",
    [
        ("ontology_snapshot_id", "other-ontology"),
        ("canonical_fact_resolution_snapshot_id", "other-resolution"),
        ("document_version_ids", ("unselected-document",)),
    ],
)
def test_sum_rejects_unselected_snapshot_context(
    database: sqlite3.Connection, field: str, value: object
) -> None:
    snapshot, refs = fixture(database)
    if field == "document_version_ids":
        snapshot = snapshot.model_copy(
            update={
                "research_universe": snapshot.research_universe.model_copy(update={field: value})
            }
        )
    else:
        snapshot = snapshot.model_copy(update={field: value})
    with pytest.raises(briefs.MemoEvidenceError, match=r"identity|scope"):
        briefs.read_memo_observation_sum(database, refs, snapshot)


def test_whole_claim_gate_replays_sum_and_rejects_reader_extra_numbers(
    database: sqlite3.Connection,
) -> None:
    snapshot, refs = fixture(database)
    markup = "<p>Combined signed allowances in USD millions: -6101.2.</p>"
    block = briefs.memo_reader_blocks(markup)[0]
    calculation = briefs.MemoObservationSum(
        operands=refs, displayed_value="-6101.2", display_format="number1"
    )
    claim = briefs.ReviewedMemoClaimV2(
        block_id=block.block_id,
        passage=block.text,
        kind="calculation",
        observation_sum=calculation,
        rationale="Exact signed source stocks; only native canonical projection operands are used.",
    )
    review = briefs.MemoContextReviewV2(
        artifact_id="test",
        body_sha256="a" * 64,
        research_snapshot_id=snapshot.research_snapshot_id,
        claims=(claim,),
        reviewed_section_ids=("financials",),
        reviewer="synthetic",
        reviewed_at=STAMP,
        rationale="Actual claim verifier wiring; not issuer or research-corpus qualification.",
    )

    def verify(current: briefs.MemoContextReviewV2) -> tuple[frozenset[str], frozenset[str]]:
        return briefs.verify_memo_claims(
            database,
            review=current,
            ticker="SYNTH",
            snapshot=snapshot,
            snapshots={"primary": (snapshot, "SYNTH")},
            supporting_contexts={},
            used_contexts=set(),
            cutoff=STAMP,
            verified_model=None,
            earnings=None,
        )

    assert verify(review) == (frozenset((block.block_id,)), frozenset())
    wrong = calculation.model_copy(update={"displayed_value": "6101.2"})
    with pytest.raises(briefs.MemoEvidenceError, match="value_mismatch"):
        verify(
            review.model_copy(
                update={"claims": (claim.model_copy(update={"observation_sum": wrong}),)}
            )
        )
    markup = "<p>Combined signed allowances in USD millions: -6101.2 in 2026.</p>"
    extra = briefs.memo_reader_blocks(markup)[0]
    changed = claim.model_copy(update={"block_id": extra.block_id, "passage": extra.text})
    with pytest.raises(briefs.MemoEvidenceError, match="numeric_population"):
        briefs.verify_memo_claim_population(
            BeautifulSoup(markup, "html.parser"), review.model_copy(update={"claims": (changed,)})
        )
