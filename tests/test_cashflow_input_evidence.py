"""Real sealed source facts, reviewed admission, model save, and reader replay.

Snapshot/inventory verification is isolated here, as in the MELI recipe suite.
Separate coverage tests retain that authority's complete and current checks.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Callable, Generator, Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from dcf import cashflow_refresh
from dcf import input_evidence as evidence
from dcf.cashflow_inputs import (
    RECIPE,
    calculate_actuals,
    model_output,
    prepare_cashflow_inputs,
    requirements_for,
)
from dcf.cashflow_refresh import PreparedCashflowDcfRequest, prepare_cashflow_dcf
from dcf.input_recipes import model_engine, prepare_model_inputs, verify_model_input_receipt
from dcf.readiness import load_valuation_readiness
from provenance.canonical_fact_resolution import CanonicalFactResolutionEngine
from provenance.fact_read_model import FactReadModel
from provenance.financial_statement_admission import (
    FinancialStatementAdmissionRequest,
    FinancialStatementConceptReview,
    FinancialStatementContextReview,
    ReviewedFinancialStatementRole,
    admit_reviewed_financial_statements,
)
from provenance.metric_ontology import MetricOntology
from provenance.research_snapshot import ResearchSnapshotRequest
from tests.test_report_canonical_financials import STAMP, seed_table
from tests.test_source_fact_repository import seed_foundation

CLOCK = STAMP + timedelta(seconds=2)
END = date(2025, 12, 31)
CONCEPTS = (
    "operating_cash_flow",
    "capital_expenditure",
    "stock_based_compensation",
    "cash_and_equivalents",
    "total_financial_debt",
    "shares_outstanding",
)
SOURCE_NAMES: dict[str, str] = dict(
    zip(
        CONCEPTS,
        (
            "NetCashProvidedByUsedInOperatingActivities",
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "ShareBasedCompensation",
            "CashAndCashEquivalentsAtCarryingValue",
            "TotalFinancialDebt",
            "CommonStockSharesOutstanding",
        ),
        strict=True,
    )
)


@pytest.fixture
def source_context(tmp_path: Path) -> evidence.SourceReadContext:
    """Explicit isolated byte authority; the database path grants no file access."""
    return evidence.SourceReadContext.for_sec_state_root(tmp_path / "source-state")


@pytest.fixture
def database(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    source_context: evidence.SourceReadContext,
) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(migrated_db(tmp_path / "cashflow.db"))
    conn.execute("PRAGMA foreign_keys = ON")
    seed_foundation(conn, source_path=source_context.content_roots[0] / "fixture.json")
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def _request(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    *,
    source_context: evidence.SourceReadContext,
    debt_source_name: str = "TotalFinancialDebt",
) -> evidence.ModelInputRequest:
    def now(_zone: object) -> datetime:
        return CLOCK

    monkeypatch.setattr(cashflow_refresh, "datetime", SimpleNamespace(now=now))
    source_names: dict[str, str] = dict(SOURCE_NAMES)
    source_names["total_financial_debt"] = debt_source_name
    sources = seed_table(
        conn,
        [
            (
                source_names[concept],
                "2025-01-01" if index < 3 else "",
                END.isoformat(),
                "FY",
                str(value),
                "USD" if index < 5 else "shares",
            )
            for index, (concept, value) in enumerate(
                zip(
                    CONCEPTS,
                    (100_000_000, 10_000_000, 5_000_000, 30_000_000, 60_000_000, 10_000_000),
                    strict=True,
                )
            )
        ],
        concept_namespace="https://fasb.org/us-gaap/2026",
        consolidation_scope="other",
        currencies={5: None},
    )
    wording = "Combined carve-out financial statements"
    locator = '{"path":"/statement/heading"}'
    conn.execute(
        "INSERT INTO evidence_extraction_runs SELECT 'cashflow-heading','cashflow-heading',document_version_id,input_sha256,'cashflow-heading',extractor_config_sha256,extractor_code_version,output_sha256,started_at,completed_at,outcome FROM evidence_extraction_runs WHERE extraction_run_id='report-run'"
    )
    conn.execute(
        "INSERT INTO evidence_nodes VALUES ('cashflow-heading','cashflow-heading',1,'cashflow-heading',NULL,NULL,'section',?,?,?,?)",
        (wording, locator, hashlib.sha256(locator.encode()).hexdigest(), STAMP),
    )
    doc_sha = str(
        conn.execute(
            "SELECT blob_sha256 FROM evidence_document_versions WHERE document_version_id='report-document'"
        ).fetchone()[0]
    )
    ontology, reader = MetricOntology(conn), FactReadModel(conn)
    debt_wording = "Total financial debt includes all current and noncurrent financial borrowing."
    debt_locator = '{"path":"/statement/total-financial-debt"}'
    conn.execute(
        "INSERT INTO evidence_nodes VALUES ('debt-role','debt-role',1,'cashflow-heading',NULL,NULL,'section',?,?,?,?)",
        (debt_wording, debt_locator, hashlib.sha256(debt_locator.encode()).hexdigest(), STAMP),
    )
    roles: list[ReviewedFinancialStatementRole] = []
    for concept, source in zip(CONCEPTS, sources, strict=True):
        bundle = reader.provenance_bundle(source.observation.observation_id, cutoff=STAMP)
        binding = ontology.binding_as_known(bundle.observation.observation_id, STAMP)
        assert binding is not None
        metric = str(
            conn.execute(
                "SELECT metric_id FROM canonical_metric_cells WHERE canonical_metric_cell_id=?",
                (binding.canonical_metric_cell_id,),
            ).fetchone()[0]
        )
        definition = ontology.metric_definition_as_known(metric, STAMP)
        assert definition is not None
        roles.append(
            ReviewedFinancialStatementRole.model_validate(
                {
                    "observation_id": bundle.observation.observation_id,
                    "observation_payload_sha256": bundle.observation_payload_sha256,
                    "expected_definition_revision_id": definition.metric_definition_revision_id,
                    "concept": concept,
                    "concept_review": FinancialStatementConceptReview(
                        concept_namespace="https://fasb.org/us-gaap/2026",
                        concept_name=source_names[concept],
                        canonical_concept="total_financial_debt",
                        evidence_node_id="debt-role",
                        evidence_locator_sha256=hashlib.sha256(debt_locator.encode()).hexdigest(),
                        source_wording=debt_wording,
                        reviewer="synthetic-reviewer",
                        reviewed_at=STAMP,
                        rationale="Exact synthetic total-debt row explicitly includes both current and noncurrent borrowing.",
                    )
                    if concept == "total_financial_debt"
                    else None,
                    "context": FinancialStatementContextReview(
                        document_version_id="report-document",
                        document_sha256=doc_sha,
                        issuer_id="issuer-1",
                        reporting_entity_id="reporting-1",
                        evidence_node_id="cashflow-heading",
                        evidence_locator_sha256=hashlib.sha256(locator.encode()).hexdigest(),
                        source_wording=wording,
                        accounting_basis="us_gaap",
                        consolidation_scope="other",
                        source_scope_label="combined_carve_out",
                        period_start=bundle.cell.period_start,
                        period_end=bundle.cell.period_end,
                        fiscal_year=2025,
                        fiscal_period="FY",
                        reviewer="synthetic-reviewer",
                        reviewed_at=STAMP,
                        rationale="Actual synthetic source heading names this exact combined statement scope.",
                    ),
                }
            )
        )
    admit_reviewed_financial_statements(
        conn,
        FinancialStatementAdmissionRequest(
            roles=tuple(roles), recorded_at=STAMP + timedelta(seconds=1), apply=True
        ),
    )
    context: dict[str, object] = {
        "economic_method": "nonfinancial_operating_company",
        "reporting_regime": "sec_domestic_10k_10q",
        "currency": "USD",
        "accounting_basis": "us_gaap",
        "source_scope_label": "combined_carve_out",
        "consolidation_scope": "other",
        "point_scope_label": "combined_carve_out",
        "point_consolidation_scope": "other",
        "flow_periods": [{"role": "fy", "start": "2025-01-01", "end": END.isoformat()}],
        "balance_date": END.isoformat(),
        "shares_date": END.isoformat(),
        "cash_interest_and_tax": "included_in_operating_cash_flow",
        "capex_basis": "positive_cash_outflow",
        "sbc_basis": "reported_operating_cashflow_addback",
        "shares_basis": "period_end_common_shares_plus_analyst_dilution",
        "net_new_borrowing": "zero",
        "method_reviewer": "synthetic-analyst",
        "method_rationale": "US-GAAP operating cash includes interest and tax; debt retained as leverage and no new borrowing assumed.",
    }
    assumptions = {
        key: evidence.AssumptionBasis(
            value=value,
            attribution="analyst",
            rationale="Synthetic explicitly reviewed analyst input.",
        )
        for key, value in {
            "growth": 0.04,
            "cost_of_equity": 0.1,
            "terminal_growth": 0.02,
            "years": 5,
            "cash_reserve": 10,
            "cashflow_normalization": 3,
            "incremental_capex": 2,
            "dilution_shares": 1,
        }.items()
    }
    facts: dict[str, evidence.FactBinding] = {}
    for concept, source in zip(CONCEPTS, sources, strict=True):
        observation = source.observation
        binding = ontology.binding_as_known(observation.observation_id, CLOCK)
        assert binding is not None and binding.canonical_metric_cell_id is not None
        metric = str(
            conn.execute(
                "SELECT metric_id FROM canonical_metric_cells WHERE canonical_metric_cell_id=?",
                (binding.canonical_metric_cell_id,),
            ).fetchone()[0]
        )
        definition = ontology.metric_definition_as_known(metric, CLOCK)
        resolution = CanonicalFactResolutionEngine(conn).as_known(
            binding.canonical_metric_cell_id, CLOCK
        )
        assert definition is not None and resolution is not None
        key = (
            f"{concept}_fy"
            if concept in CONCEPTS[:3]
            else {
                "cash_and_equivalents": "reported_cash",
                "total_financial_debt": "reported_debt",
                "shares_outstanding": "reported_shares",
            }[concept]
        )
        facts[key] = evidence.FactBinding(
            canonical_metric_cell_id=binding.canonical_metric_cell_id,
            metric_id=metric,
            metric_definition_revision_id=definition.metric_definition_revision_id,
            canonical_resolution_revision_id=resolution.canonical_resolution_revision_id,
            observation_id=observation.observation_id,
            observation_payload_sha256=reader.provenance_bundle(
                observation.observation_id, cutoff=CLOCK
            ).observation_payload_sha256,
        )
    snapshot_id = str(
        conn.execute(
            "SELECT resolution_snapshot_id FROM canonical_fact_resolution_snapshot_members WHERE canonical_metric_cell_id=?",
            (next(iter(facts.values())).canonical_metric_cell_id,),
        ).fetchone()[0]
    )
    snapshot = ResearchSnapshotRequest.model_validate(
        {
            "research_snapshot_id": "synthetic-snapshot",
            "idempotency_key": "synthetic-snapshot",
            "research_universe": {
                "issuer_id": "issuer-1",
                "reporting_entity_ids": ["reporting-1"],
                "document_version_ids": ["report-document"],
                "source_obligation_revision_ids": ["synthetic-obligation"],
            },
            "processing_snapshot_ids": ["synthetic-processing"],
            "corpus_bundles": [
                {
                    "corpus_manifest_id": "synthetic-manifest",
                    "lexical_index_run_id": "synthetic-lexical",
                }
            ],
            "source_fact_publication_ids": ["report-publication"],
            "ontology_snapshot_id": "synthetic-ontology",
            "canonical_fact_resolution_snapshot_id": snapshot_id,
            "canonical_fact_projection_run_id": "synthetic-projection",
            "cutoff_at": CLOCK,
            "recorded_at": CLOCK,
        }
    )

    def coverage(
        _conn: sqlite3.Connection, _request: evidence.ModelInputRequest, _cutoff: datetime
    ) -> tuple[ResearchSnapshotRequest, str, tuple[str, ...]]:
        return snapshot, "b" * 64, ("synthetic-inventory",)

    monkeypatch.setattr(evidence, "verify_source_coverage", coverage)
    request = evidence.ModelInputRequest(
        recipe=RECIPE,
        ticker="SYNTH",
        research_snapshot_id="synthetic-snapshot",
        financial_period_end=END,
        facts=facts,
        assumptions=assumptions,
        recipe_context=context,
    )
    proposed = {key: item.value for key, item in assumptions.items()}
    proof = evidence.verify_model_inputs(
        conn,
        request,
        recipe=RECIPE,
        requirements=requirements_for(request),
        effective_inputs=proposed,
        assumption_keys=frozenset(assumptions),
        as_of=CLOCK,
        source_context=source_context,
    )
    actuals, _ = calculate_actuals(proof)
    complete = {
        **proposed,
        **{
            key: actuals[key]
            for key in ("owner_cashflow", "reported_cash", "reported_debt", "reported_shares")
        },
    }
    drivers = {
        "cashflow_normalization": (actuals["reported_fcf_after_sbc"], actuals["owner_cashflow"]),
        "cash_reserve": (actuals["reported_cash"], complete["cash_reserve"]),
        "dilution_shares": (
            actuals["reported_shares"],
            complete["reported_shares"] + complete["dilution_shares"],
        ),
    }
    review = evidence.AssumptionReview(
        reviewed_at=CLOCK,
        reviewer="synthetic-analyst",
        effective_inputs_sha256=evidence.canonical_digest(complete),
        actuals_sha256=evidence.canonical_digest(actuals),
        drivers={
            key: evidence.DriverReview(
                observed=observed,
                forecast=forecast,
                variance=forecast - observed,
                rationale="Explicit variance review with synthetic source basis.",
            )
            for key, (observed, forecast) in drivers.items()
        },
    )
    conn.commit()
    return request.model_copy(update={"assumption_review": review})


seed_cashflow_model_inputs = _request


def test_generic_source_to_versioned_save_and_readiness_replay(
    database: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_context: evidence.SourceReadContext,
) -> None:
    request = _request(database, monkeypatch, source_context=source_context)
    effective, receipt = prepare_cashflow_inputs(
        database,
        request,
        effective_inputs={key: item.value for key, item in request.assumptions.items()},
        as_of=CLOCK,
        source_context=source_context,
    )
    assert effective["owner_cashflow"] == 86
    assert receipt.calculations[3].key == "reported_fcf_after_sbc"
    expected = model_output(effective)
    price = expected["vps"]
    assert isinstance(price, (int, float))
    prepared = PreparedCashflowDcfRequest(
        model_inputs=request,
        effective_inputs=effective,
        as_of=CLOCK,
        valuation_date=CLOCK.date(),
        market_price=float(price),
        market_observed_at=CLOCK,
        market_source="synthetic-current-observation",
    )
    path, artifact = tmp_path / "prepared.json", tmp_path / "calculation.json"
    path.write_text(prepared.model_dump_json())
    os.utime(path, (CLOCK.timestamp(), CLOCK.timestamp()))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    planned = prepare_cashflow_dcf(
        database,
        prepared,
        request_path=path,
        expected_request_sha256=digest,
        repo_root=tmp_path,
        artifact_path=artifact,
        source_context=source_context,
    )
    assert planned.mode == "dry_run" and not artifact.exists()
    with pytest.raises(
        evidence.InputEvidenceError, match="prepared_cashflow_request_bytes_mismatch"
    ):
        prepare_cashflow_dcf(
            database,
            prepared,
            request_path=path,
            expected_request_sha256="0" * 64,
            repo_root=tmp_path,
            artifact_path=artifact,
            source_context=source_context,
            apply=True,
        )
    assert not artifact.exists()
    assert database.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == 0
    result = prepare_cashflow_dcf(
        database,
        prepared,
        request_path=path,
        expected_request_sha256=digest,
        repo_root=tmp_path,
        artifact_path=artifact,
        source_context=source_context,
        apply=True,
    )
    assert result.version_created and artifact.is_file()
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == result.artifact_sha256
    readiness = load_valuation_readiness(
        database, "SYNTH", as_of=CLOCK + timedelta(seconds=1), source_context=source_context
    )
    assert readiness.financial_input_completeness == "verified", readiness.reason_codes
    assert readiness.reason_codes == ("scenario_acceptance_unverified",)
    database.execute("UPDATE dcf_runs SET npv=npv+1000 WHERE ticker='SYNTH'")
    wrong = load_valuation_readiness(
        database, "SYNTH", as_of=CLOCK + timedelta(seconds=1), source_context=source_context
    )
    assert "persisted_model_output_replay_mismatch" in wrong.reason_codes


@pytest.mark.parametrize(
    "change", ["population", "unit", "assumption", "scope", "method", "ticker", "calculated"]
)
def test_generic_inputs_fail_closed(
    database: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    source_context: evidence.SourceReadContext,
) -> None:
    request = _request(database, monkeypatch, source_context=source_context)
    effective = {key: item.value for key, item in request.assumptions.items()}
    if change == "population":
        request = request.model_copy(
            update={
                "facts": {
                    key: value for key, value in request.facts.items() if key != "reported_debt"
                }
            }
        )
    elif change == "unit":
        facts = dict(request.facts)
        facts["reported_cash"] = facts["reported_shares"]
        request = request.model_copy(update={"facts": facts})
    elif change == "assumption":
        effective["growth"] = 0.08
    elif change in {"scope", "method"}:
        assert request.recipe_context is not None
        context = dict(request.recipe_context)
        context["source_scope_label" if change == "scope" else "economic_method"] = (
            "consolidated" if change == "scope" else "bank"
        )
        request = request.model_copy(update={"recipe_context": context})
    elif change == "calculated":
        effective, _ = prepare_cashflow_inputs(
            database,
            request,
            effective_inputs=effective,
            as_of=CLOCK,
            source_context=source_context,
        )
        effective["owner_cashflow"] += 1
    else:
        request = request.model_copy(update={"ticker": "OTHER"})

        # The snapshot issuer still owns these source facts; source coverage must reject OTHER.
        def wrong_ticker(
            _conn: sqlite3.Connection, _request: evidence.ModelInputRequest, _cutoff: datetime
        ) -> tuple[ResearchSnapshotRequest, str, tuple[str, ...]]:
            raise evidence.InputEvidenceError("source_inventory_not_authoritative_complete")

        monkeypatch.setattr(evidence, "verify_source_coverage", wrong_ticker)
    with pytest.raises((ValueError, RuntimeError)):
        prepare_cashflow_inputs(
            database,
            request,
            effective_inputs=effective,
            as_of=CLOCK,
            source_context=source_context,
        )


def test_unknown_recipe_has_no_dispatch() -> None:
    with pytest.raises(evidence.InputEvidenceError, match="input_recipe_unsupported"):
        model_engine("bank.v1")


def test_reported_debt_component_cannot_be_reviewed_as_the_total(
    database: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    source_context: evidence.SourceReadContext,
) -> None:
    with pytest.raises(ValueError, match="financial_aggregate_component_is_not_reported_total"):
        _request(
            database,
            monkeypatch,
            source_context=source_context,
            debt_source_name="LongTermDebtCurrent",
        )
    assert (
        database.execute(
            "SELECT COUNT(*) FROM canonical_metric_definition_revisions WHERE json_extract(scope_constraints_json,'$.financial_statement_concept') IS NOT NULL"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize("change", ["context", "missing", "bytes", "outside_root"])
def test_cashflow_dispatch_rechecks_current_source_bytes(
    database: sqlite3.Connection,
    source_context: evidence.SourceReadContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    request = _request(database, monkeypatch, source_context=source_context)
    effective, receipt = prepare_model_inputs(
        database,
        request,
        effective_inputs={key: item.value for key, item in request.assumptions.items()},
        as_of=CLOCK,
        source_context=source_context,
    )
    assert receipt.schema_version == "dcf_model_inputs.v3"
    assert receipt.source_integrity == "present_bytes_verified"
    assert len(receipt.raw_documents) == 1
    assert receipt.raw_documents[0].blob_sha256 == hashlib.sha256(b"filing bytes").hexdigest()
    assert receipt.raw_documents[0].byte_size == 12
    source_path = source_context.content_roots[0] / "fixture.json"
    context: evidence.SourceReadContext | None = source_context
    reason = "model_input_source_context_unavailable"
    if change == "context":
        context = None
    elif change == "missing":
        source_path.unlink()
        reason = "model_input_source_bytes_unavailable"
    elif change == "bytes":
        source_path.write_bytes(b"filing wrong")
        reason = "model_input_source_digest_or_size_mismatch"
    else:
        context = evidence.SourceReadContext.for_sec_state_root(tmp_path / "unapproved")
        reason = "model_input_source_location_unapproved"
    with pytest.raises(evidence.InputEvidenceError, match=reason):
        verify_model_input_receipt(
            database, receipt, effective_inputs=effective, as_of=CLOCK, source_context=context
        )


@pytest.mark.parametrize("change", ["fresh_clock", "storage_identity", "legacy"])
def test_cashflow_receipt_preserves_physical_commitments(
    database: sqlite3.Connection,
    source_context: evidence.SourceReadContext,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    request = _request(database, monkeypatch, source_context=source_context)
    effective, receipt = prepare_model_inputs(
        database,
        request,
        effective_inputs={key: item.value for key, item in request.assumptions.items()},
        as_of=CLOCK,
        source_context=source_context,
    )
    if change == "legacy":
        modified = receipt.model_copy(
            update={
                "schema_version": "dcf_model_inputs.v2",
                "source_integrity": "unverified",
                "raw_documents": (),
            }
        )
        reason = "model_input_source_legacy_receipt_unverified"
    else:
        witness = receipt.raw_documents[0]
        updates = (
            {"verified_at": witness.verified_at + timedelta(seconds=1)}
            if change == "fresh_clock"
            else {"storage_uri_sha256": "0" * 64}
        )
        modified = receipt.model_copy(
            update={"raw_documents": (witness.model_copy(update=updates),)}
        )
        reason = "model_input_receipt_mismatch"
    if change == "fresh_clock":
        verified = verify_model_input_receipt(
            database,
            modified,
            effective_inputs=effective,
            as_of=CLOCK,
            source_context=source_context,
        )
        assert (
            verified.raw_documents[0].storage_uri_sha256
            == receipt.raw_documents[0].storage_uri_sha256
        )
    else:
        with pytest.raises(evidence.InputEvidenceError, match=reason):
            verify_model_input_receipt(
                database,
                modified,
                effective_inputs=effective,
                as_of=CLOCK,
                source_context=source_context,
            )


def test_cashflow_transaction_rechecks_bytes_before_retention(
    database: sqlite3.Connection,
    tmp_path: Path,
    source_context: evidence.SourceReadContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(database, monkeypatch, source_context=source_context)
    prepared = PreparedCashflowDcfRequest(
        model_inputs=request,
        effective_inputs={key: item.value for key, item in request.assumptions.items()},
        as_of=CLOCK,
        valuation_date=CLOCK.date(),
        market_price=10,
        market_observed_at=CLOCK,
        market_source="synthetic-current-observation",
    )
    path, artifact = tmp_path / "prepared.json", tmp_path / "calculation.json"
    path.write_text(prepared.model_dump_json())
    os.utime(path, (CLOCK.timestamp(), CLOCK.timestamp()))
    calls = 0

    def mutate_at_transaction_recheck(
        conn: sqlite3.Connection,
        input_request: evidence.ModelInputRequest,
        *,
        effective_inputs: Mapping[str, float],
        as_of: datetime,
        source_context: evidence.SourceReadContext | None = None,
    ) -> tuple[dict[str, float], evidence.ModelInputReceipt]:
        nonlocal calls
        calls += 1
        if calls == 2:
            assert conn.in_transaction
            assert source_context is not None
            (source_context.content_roots[0] / "fixture.json").write_bytes(b"filing wrong")
        return prepare_cashflow_inputs(
            conn,
            input_request,
            effective_inputs=effective_inputs,
            as_of=as_of,
            source_context=source_context,
        )

    monkeypatch.setattr(cashflow_refresh, "prepare_cashflow_inputs", mutate_at_transaction_recheck)
    with pytest.raises(
        evidence.InputEvidenceError, match="model_input_source_digest_or_size_mismatch"
    ):
        prepare_cashflow_dcf(
            database,
            prepared,
            request_path=path,
            expected_request_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            repo_root=tmp_path,
            artifact_path=artifact,
            apply=True,
            source_context=source_context,
        )
    assert calls == 2
    assert not database.in_transaction
    assert database.execute("SELECT COUNT(*) FROM dcf_runs").fetchone()[0] == 0
    assert not artifact.exists()
    assert not list(tmp_path.glob(".calculation.json.*.staged"))
