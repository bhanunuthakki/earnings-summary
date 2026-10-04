"""Memo grade cannot be inferred from rendering, chips or legacy facts."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from collections.abc import Callable, Generator
from datetime import date
from pathlib import Path
from typing import cast

import pytest
from bs4 import BeautifulSoup

from provenance.canonical_fact_resolution import (
    CanonicalFactResolutionEngine,
    ResolutionSnapshotScope,
)
from provenance.metric_ontology import MetricOntology, OntologySnapshot
from provenance.research_snapshot import ResearchSnapshotRequest
from report.artifacts import (
    RenderedReportBody,
    ReportInteractionManifest,
    ReportSectionRef,
    persist_report_artifact,
)
from report.models import CellSource
from research.decision_brief import (
    MemoCalculation,
    MemoContextReview,
    MemoEvidenceError,
    ReviewedMemoClaim,
    assess_decision_brief,
    memo_reader_blocks,
    persist_decision_brief_readiness,
    verify_memo_calculation,
    verify_memo_claim_population,
    verify_memo_section_population,
    verify_memo_snapshot_node,
    verify_memo_snapshot_reference,
)
from research.decision_brief_workflow import (
    DecisionBriefPreparationRequest,
    prepare_decision_brief,
)
from runtime.job_runtime import run_captured_application_child
from sources.report_financials import FinancialEvidenceReference, read_financial_table
from tests import test_report_canonical_financials as canonical
from ui.source_chip import source_chip_html, source_hover_title

STAMP = canonical.STAMP


@pytest.fixture
def database(tmp_path: Path, migrated_db: Callable[..., Path]) -> Generator[sqlite3.Connection]:
    factory = cast(
        Callable[..., Generator[sqlite3.Connection]], getattr(canonical.database, "__wrapped__")
    )
    yield from factory(tmp_path, migrated_db)


def _report(root: Path, markup: str):
    body = RenderedReportBody.from_html(
        ticker="SYNTH",
        report_date=date(2026, 1, 1),
        body_html=f'<main data-report-body="v1"><section id="financials">{markup}</section></main>',
        sections=(
            ReportSectionRef(section_id="financials", label="Financials", group_id="financials"),
        ),
        interaction_manifest=ReportInteractionManifest(),
    )
    standalone = root / "standalone.html"
    standalone.write_text(body.body_html)
    return persist_report_artifact(
        repo_root=root,
        body=body,
        standalone_path=standalone,
        generated_at=STAMP,
        coverage_role="evaluation",
        title="SYNTH evaluation",
    )


def _reference(conn: sqlite3.Connection) -> FinancialEvidenceReference:
    canonical.seed_table(conn, [("revenue", "2025-10-01", "2025-12-31", "Q4", "120000000", "USD")])
    cell = read_financial_table(conn, "SYNTH", as_of=STAMP).cells[0]
    assert cell.provenance is not None
    assert cell.canonical_resolution_revision_id is not None
    assert cell.metric_definition_revision_id is not None
    return FinancialEvidenceReference(
        ticker="SYNTH",
        concept=cell.concept,
        canonical_metric_cell_id=cell.canonical_metric_cell_id,
        observation_id=cell.provenance.observation.observation_id,
        canonical_resolution_revision_id=cell.canonical_resolution_revision_id,
        metric_definition_revision_id=cell.metric_definition_revision_id,
        as_of=STAMP,
    )


def test_successful_render_does_not_certify_memo(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    report = _report(tmp_path, "An analyst draft without sealed claim evidence.")
    receipt = assess_decision_brief(database, repo_root=tmp_path, artifact=report, as_of=STAMP)
    assert not receipt.decision_grade
    assert receipt.owner_thesis_required is False
    assert "memo_claim_context_review_missing" in receipt.reason_codes
    assert "memo_canonical_financial_references_missing" in receipt.reason_codes
    assert any(reason.startswith("valuation_") for reason in receipt.reason_codes)
    path = persist_decision_brief_readiness(tmp_path, receipt)
    assert persist_decision_brief_readiness(tmp_path, receipt) == path
    assert hashlib.sha256(path.read_bytes()).hexdigest() == path.stem


@pytest.mark.parametrize(
    "display,expected_count,reason",
    [("120.0", 1, None), ("121.0", 0, "memo_reader_value_mismatch")],
)
def test_exact_admitted_chip_does_not_hide_wrong_display_value(
    database: sqlite3.Connection,
    tmp_path: Path,
    display: str,
    expected_count: int,
    reason: str | None,
) -> None:
    reference = _reference(database)
    chip = source_chip_html(
        CellSource(source="sec_official", canonical_reference=reference), link_only=True
    )
    report = _report(tmp_path, f'<table><tr><td class="num">{display}{chip}</td></tr></table>')
    receipt = assess_decision_brief(database, repo_root=tmp_path, artifact=report, as_of=STAMP)
    assert receipt.financial_reference_count == expected_count
    assert not receipt.decision_grade
    if reason:
        assert reason in receipt.reason_codes
    else:
        assert "memo_financial_reference_not_admitted" not in receipt.reason_codes


def test_retained_reader_body_tamper_fails_before_grade(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    report = _report(tmp_path, "A retained analyst draft.")
    assert report.body_path
    (tmp_path / report.body_path).write_text("changed after generation")
    receipt = assess_decision_brief(database, repo_root=tmp_path, artifact=report, as_of=STAMP)
    assert "memo_body_missing_or_changed" in receipt.reason_codes
    assert not receipt.decision_grade


def test_readiness_preserves_caller_transaction(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    report = _report(tmp_path, "A retained analyst draft.")
    database.commit()
    assess_decision_brief(database, repo_root=tmp_path, artifact=report, as_of=STAMP)
    assert not database.in_transaction
    database.execute("BEGIN")
    assess_decision_brief(database, repo_root=tmp_path, artifact=report, as_of=STAMP)
    assert database.in_transaction
    database.rollback()


def test_source_scope_remains_visible_in_shared_chip() -> None:
    source = CellSource(source="sec_official", source_scope_label="combined_carve_out")
    assert "combined carve out" in source_hover_title(source)
    assert "scope: combined carve out" in source_chip_html(source)


def _review(
    markup: str, *, only_first: bool = False, kind: str = "analyst_inference"
) -> MemoContextReview:
    blocks = memo_reader_blocks(markup)
    claims = tuple(
        ReviewedMemoClaim.model_validate(
            {
                "block_id": block.block_id,
                "passage": block.text,
                "kind": kind,
                "rationale": "Synthetic complete block review for this exact reader body.",
            }
        )
        for block in (blocks[:1] if only_first else blocks)
    )
    return MemoContextReview(
        artifact_id="synthetic",
        body_sha256="a" * 64,
        research_snapshot_id="synthetic",
        claims=claims,
        reviewed_section_ids=("company",),
        reviewer="synthetic-reviewer",
        reviewed_at=STAMP,
        rationale="Every exact text block is independently disposed in this synthetic test.",
    )


def test_one_reviewed_claim_cannot_cover_second_unreviewed_claim() -> None:
    markup = "<section><p>Analyst inference: first conclusion.</p><div>A second financial assertion outside a paragraph.</div></section>"
    with pytest.raises(MemoEvidenceError, match="memo_claim_population_incomplete"):
        verify_memo_claim_population(
            BeautifulSoup(markup, "html.parser"), _review(markup, only_first=True)
        )


def test_full_inference_population_requires_reader_classification() -> None:
    markup = "<section><p>Analyst inference: an attributed conclusion.</p><div>Analyst inference: another attributed conclusion.</div></section>"
    verify_memo_claim_population(BeautifulSoup(markup, "html.parser"), _review(markup))
    changed = markup.replace("Analyst inference: ", "")
    with pytest.raises(MemoEvidenceError, match="memo_inference_not_distinguished_in_reader"):
        verify_memo_claim_population(BeautifulSoup(changed, "html.parser"), _review(changed))


def test_substantive_prose_cannot_be_disposed_as_presentation() -> None:
    markup = "<p>Revenue grew 20 percent.</p>"
    with pytest.raises(MemoEvidenceError, match="memo_substantive_block_marked_presentation"):
        verify_memo_claim_population(
            BeautifulSoup(markup, "html.parser"), _review(markup, kind="presentation")
        )


def test_source_chip_text_is_separate_from_claim_population() -> None:
    markup = '<p>Analyst inference: conclusion.<a class="src-chip">SEC</a></p>'
    assert [block.text for block in memo_reader_blocks(markup)] == [
        "Analyst inference: conclusion."
    ]


def test_nonrendered_comments_do_not_become_reader_claims() -> None:
    markup = "<p>Analyst inference: conclusion.<!-- internal template marker --></p>"
    assert [block.text for block in memo_reader_blocks(markup)] == [
        "Analyst inference: conclusion."
    ]


def test_calculation_reconstructs_result_from_admitted_operands(
    database: sqlite3.Connection,
) -> None:
    reference = _reference(database)
    valid = MemoCalculation(
        operation="difference",
        operands=(reference, reference),
        displayed_value="0.0",
        display_format="number1",
    )
    verify_memo_calculation(database, valid)
    with pytest.raises(MemoEvidenceError, match="memo_calculation_value_mismatch"):
        verify_memo_calculation(database, valid.model_copy(update={"displayed_value": "25.0"}))


@pytest.mark.parametrize(
    "passage",
    [
        "The calculated result is 99.0.",
        "The calculated result is 0.0 in 2025.",
        "The calculated result is 0.0 and again 0.0.",
        "The calculated result is 0.0%.",
    ],
)
def test_calculation_receipt_cannot_certify_unbound_reader_numbers(
    database: sqlite3.Connection, passage: str
) -> None:
    reference = _reference(database)
    calculation = MemoCalculation(
        operation="difference",
        operands=(reference, reference),
        displayed_value="0.0",
        display_format="number1",
    )
    markup = f"<p>{passage}</p>"
    review = _review(markup)
    claim = review.claims[0].model_copy(update={"kind": "calculation", "calculation": calculation})
    with pytest.raises(
        MemoEvidenceError,
        match=r"memo_reported_numeric_population_unverified|memo_reported_value_not_in_passage",
    ):
        verify_memo_claim_population(
            BeautifulSoup(markup, "html.parser"), review.model_copy(update={"claims": (claim,)})
        )


def test_calculation_receipt_binds_actual_reader_number(database: sqlite3.Connection) -> None:
    reference = _reference(database)
    calculation = MemoCalculation(
        operation="difference",
        operands=(reference, reference),
        displayed_value="0.0",
        display_format="number1",
    )
    markup = "<p>The calculated result is 0.0.</p>"
    review = _review(markup)
    claim = review.claims[0].model_copy(update={"kind": "calculation", "calculation": calculation})
    verify_memo_calculation(database, calculation)
    verify_memo_claim_population(
        BeautifulSoup(markup, "html.parser"), review.model_copy(update={"claims": (claim,)})
    )


def test_succeeded_node_on_same_document_is_not_exact_processing_membership(
    database: sqlite3.Connection,
) -> None:
    _reference(database)
    snapshot = ResearchSnapshotRequest.model_validate(
        {
            "research_snapshot_id": "synthetic",
            "idempotency_key": "synthetic",
            "research_universe": {
                "issuer_id": "issuer-1",
                "reporting_entity_ids": ["reporting-1"],
                "document_version_ids": ["report-document"],
                "source_obligation_revision_ids": ["synthetic"],
            },
            "processing_snapshot_ids": ["unrelated-processing"],
            "corpus_bundles": [
                {"corpus_manifest_id": "synthetic", "lexical_index_run_id": "synthetic"}
            ],
            "source_fact_publication_ids": ["report-publication"],
            "ontology_snapshot_id": "synthetic",
            "canonical_fact_resolution_snapshot_id": "synthetic",
            "canonical_fact_projection_run_id": "synthetic",
            "cutoff_at": STAMP,
            "recorded_at": STAMP,
        }
    )
    node = database.execute(
        "SELECT node_id FROM evidence_nodes WHERE extraction_run_id='report-run' LIMIT 1"
    ).fetchone()
    assert node is not None
    with pytest.raises(MemoEvidenceError, match="memo_claim_outside_exact_processing_snapshot"):
        verify_memo_snapshot_node(database, str(node[0]), snapshot, STAMP)


def test_requested_memo_child_capture_uses_owned_runtime(tmp_path: Path) -> None:
    result = run_captured_application_child(
        [
            sys.executable,
            "-c",
            "import sys;print('{\"status\":\"complete\"}');print('diagnostic',file=sys.stderr);sys.exit(3)",
        ],
        cwd=tmp_path,
        timeout_seconds=10,
    )
    assert result.returncode == 3
    assert result.stdout.strip() == '{"status":"complete"}'
    assert result.stderr.strip() == "diagnostic"


def test_prepare_plan_uses_explicit_code_state_and_database_without_running_children(
    tmp_path: Path,
) -> None:
    code = tmp_path / "code"
    state = tmp_path / "state"
    database = tmp_path / "authority" / "isolated.sqlite"
    code.mkdir()
    state.mkdir()

    def refuse(_command: tuple[str, ...], _state: Path):
        raise AssertionError("A plan must not run a child or fetch sources")

    result = prepare_decision_brief(
        DecisionBriefPreparationRequest(
            ticker="NEW", code_root=code, repo_root=state, database=database, skip_fmp=True
        ),
        runner=refuse,
    )
    assert result.status == "planned"
    first = result.stages[0].command
    assert str(code / "execution/onboard_ticker.py") in first
    assert str(database) in first
    assert str(state) in first
    assert "--skip-fmp" in first
    assert "--skip-sec" not in first
    assert result.stages[-1].stage == "full_memo"
    assert result.readiness is None


def test_factual_heading_cannot_be_disposed_as_presentation() -> None:
    markup = "<h2>Revenue doubled to $999m</h2>"
    with pytest.raises(MemoEvidenceError, match="memo_substantive_block_marked_presentation"):
        verify_memo_claim_population(
            BeautifulSoup(markup, "html.parser"), _review(markup, kind="presentation")
        )


def test_hidden_inference_attribute_does_not_classify_visible_reader() -> None:
    markup = '<p data-claim-kind="analyst_inference">Revenue will grow 50 percent.</p>'
    with pytest.raises(MemoEvidenceError, match="memo_inference_not_distinguished_in_reader"):
        verify_memo_claim_population(BeautifulSoup(markup, "html.parser"), _review(markup))


def test_manifest_sections_cannot_certify_empty_reader_sections() -> None:
    sections = ("company", "synthesis", "financials", "bear", "valuation", "sources")
    markup = "".join(f'<section id="{section}"></section>' for section in sections)
    with pytest.raises(MemoEvidenceError, match="memo_required_section_body_empty"):
        verify_memo_section_population(BeautifulSoup(markup, "html.parser"), sections)


@pytest.mark.parametrize("identity", ["../escaped", "/absolute", "..", "C:escape", "a\\b"])
def test_artifact_directory_identity_refuses_unsafe_components(
    database: sqlite3.Connection, tmp_path: Path, identity: str
) -> None:
    report = _report(tmp_path, "An analyst draft.")
    with pytest.raises(ValueError, match="single safe directory components"):
        type(report).model_validate({**report.model_dump(), "artifact_id": identity})


def test_unreadable_retained_body_returns_degraded_receipt(
    database: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _report(tmp_path, "An analyst draft.")

    def refuse(_path: Path) -> bytes:
        raise PermissionError("synthetic denied file")

    monkeypatch.setattr(Path, "read_bytes", refuse)
    receipt = assess_decision_brief(database, repo_root=tmp_path, artifact=report, as_of=STAMP)
    assert not receipt.decision_grade
    assert receipt.reason_codes == ("memo_retained_evidence_unreadable_or_invalid",)


def test_financial_reference_requires_exact_ontology_and_resolution_members(
    database: sqlite3.Connection,
) -> None:
    reference = _reference(database)
    MetricOntology(database).seal_snapshot(
        OntologySnapshot(
            ontology_snapshot_id="memo-ontology",
            idempotency_key="memo-ontology",
            cutoff_at=STAMP,
            recorded_at=STAMP,
        )
    )
    CanonicalFactResolutionEngine(database).seal_snapshot(
        "memo-resolution",
        STAMP,
        STAMP,
        ResolutionSnapshotScope(issuer_id="issuer-1", reporting_entity_ids=("reporting-1",)),
    )
    snapshot = ResearchSnapshotRequest.model_validate(
        {
            "research_snapshot_id": "synthetic",
            "idempotency_key": "synthetic",
            "research_universe": {
                "issuer_id": "issuer-1",
                "reporting_entity_ids": ["reporting-1"],
                "document_version_ids": ["report-document"],
                "source_obligation_revision_ids": ["synthetic"],
            },
            "processing_snapshot_ids": ["synthetic"],
            "corpus_bundles": [
                {"corpus_manifest_id": "synthetic", "lexical_index_run_id": "synthetic"}
            ],
            "source_fact_publication_ids": ["report-publication"],
            "ontology_snapshot_id": "memo-ontology",
            "canonical_fact_resolution_snapshot_id": "memo-resolution",
            "canonical_fact_projection_run_id": "synthetic",
            "cutoff_at": STAMP,
            "recorded_at": STAMP,
        }
    )
    verify_memo_snapshot_reference(database, reference, snapshot, "SYNTH", STAMP)
    for field in ("ontology_snapshot_id", "canonical_fact_resolution_snapshot_id"):
        with pytest.raises(
            MemoEvidenceError, match="memo_financial_evidence_outside_exact_snapshot"
        ):
            verify_memo_snapshot_reference(
                database,
                reference,
                snapshot.model_copy(update={field: "unrelated"}),
                "SYNTH",
                STAMP,
            )
