"""MELI consumers reuse verified publications, never a prior call's admission."""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime

import pytest

from dcf import input_evidence, meli_inputs
from dcf.meli_input_preview import preview_meli_inputs
from provenance import fact_read_model
from provenance.canonical_fact_resolution import CanonicalFactResolutionEngine
from provenance.fact_read_model import (
    FactAdmissionError,
    FactReadModel,
    ObservationUnavailableError,
)
from provenance.source_fact_publication import VerifiedSourceFactPublication
from tests.test_meli_input_evidence import END, NOW
from tests.test_meli_input_evidence import shared_revenue_inputs as shared_revenue_inputs
from tests.test_meli_input_evidence import source_context as source_context


def _count_verifications(monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    original = fact_read_model.verify_source_fact_publication
    calls: Counter[str] = Counter()

    def counted(
        connection: sqlite3.Connection,
        *,
        publication_id: str,
        cutoff: datetime,
        observed_through: datetime | None = None,
    ) -> VerifiedSourceFactPublication:
        calls[publication_id] += 1
        return original(
            connection,
            publication_id=publication_id,
            cutoff=cutoff,
            observed_through=observed_through,
        )

    monkeypatch.setattr(fact_read_model, "verify_source_fact_publication", counted)
    return calls


def test_preview_batches_native_publication_checks_per_independent_pass(
    shared_revenue_inputs: tuple[sqlite3.Connection, input_evidence.ModelInputRequest],
    source_context: input_evidence.SourceReadContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Source coverage is mocked by this fixture; this counter measures only
    # native fact-reader publication checks, not complete snapshot verification.
    conn, request = shared_revenue_inputs
    calls = _count_verifications(monkeypatch)
    conn.execute("PRAGMA query_only=ON")
    changes = conn.total_changes
    for invocation in (1, 2):
        result = preview_meli_inputs(
            conn,
            research_snapshot_id=request.research_snapshot_id,
            financial_period_end=END,
            as_of=NOW,
            source_context=source_context,
        )
        assert result.facts == request.facts
        assert result.state == "reported_inputs_verified_not_model_ready"
        # Candidate selection and the independent final model-input verifier
        # each verify once. The next preview must verify again.
        assert calls and set(calls.values()) == {2 * invocation}, calls
        assert conn.total_changes == changes


def test_model_input_verifier_batches_the_exact_required_observations(
    shared_revenue_inputs: tuple[sqlite3.Connection, input_evidence.ModelInputRequest],
    source_context: input_evidence.SourceReadContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Source coverage is mocked by this fixture; this counter measures only
    # native fact-reader publication checks, not complete snapshot verification.
    conn, request = shared_revenue_inputs
    calls = _count_verifications(monkeypatch)
    receipt = input_evidence.verify_model_inputs(
        conn,
        request,
        recipe=meli_inputs.RECIPE,
        requirements=meli_inputs.requirements_for(END),
        effective_inputs={key: value.value for key, value in request.assumptions.items()},
        assumption_keys=meli_inputs.ASSUMPTION_KEYS,
        as_of=NOW,
        source_context=source_context,
    )
    assert len(receipt.inputs) == 28
    assert calls and set(calls.values()) == {1}, calls


def test_required_batch_preserves_unavailable_observation_failure(
    shared_revenue_inputs: tuple[sqlite3.Connection, input_evidence.ModelInputRequest],
) -> None:
    conn, request = shared_revenue_inputs
    reader = FactReadModel(conn)
    observation = next(iter(request.facts.values())).observation_id
    with pytest.raises(ObservationUnavailableError) as caught:
        reader.required_provenance_bundles((observation, "not-a-retained-observation"), cutoff=NOW)
    assert caught.value.observation_id == "not-a-retained-observation"


def test_canonical_batch_preserves_order_duplicates_missing_and_transaction(
    shared_revenue_inputs: tuple[sqlite3.Connection, input_evidence.ModelInputRequest],
) -> None:
    conn, request = shared_revenue_inputs
    refs = tuple(request.facts.values())
    cell_ids = (
        refs[1].canonical_metric_cell_id,
        "missing-cell",
        refs[0].canonical_metric_cell_id,
        refs[1].canonical_metric_cell_id,
    )
    resolver = CanonicalFactResolutionEngine(conn)
    transaction, changes, factory = conn.in_transaction, conn.total_changes, conn.row_factory
    reads = resolver.read_as_known(cell_ids, NOW)
    assert tuple(read.canonical_metric_cell_id for read in reads) == cell_ids
    assert reads[1].resolution is None and reads[1].bundle is None
    assert reads[0] == reads[3]
    assert reads[0].bundle is not None
    assert reads[0].bundle.observation.observation_id == refs[1].observation_id
    assert reads[2].resolution == resolver.as_known(refs[0].canonical_metric_cell_id, NOW)
    assert (conn.in_transaction, conn.total_changes, conn.row_factory) == (
        transaction,
        changes,
        factory,
    )


def test_canonical_batch_rechecks_source_after_a_prior_success(
    shared_revenue_inputs: tuple[sqlite3.Connection, input_evidence.ModelInputRequest],
) -> None:
    conn, request = shared_revenue_inputs
    ref = next(iter(request.facts.values()))
    resolver = CanonicalFactResolutionEngine(conn)
    assert resolver.read_as_known((ref.canonical_metric_cell_id,), NOW)[0].bundle is not None
    # Fault injection in a disposable native fixture. Retained append-only
    # records are never altered by production readers.
    conn.execute("DROP TRIGGER trg_source_fact_publication_members_append_only")
    conn.execute(
        "UPDATE source_fact_publication_members SET record_commitment_sha256=? "
        "WHERE publication_id='publication-meli' AND record_kind='extraction_seal'",
        ("a" * 64,),
    )
    conn.commit()
    with pytest.raises(FactAdmissionError) as caught:
        resolver.read_as_known((ref.canonical_metric_cell_id,), NOW)
    assert caught.value.reason_code == "publication_member_tampered"
