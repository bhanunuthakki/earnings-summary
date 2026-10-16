"""Present source bytes are separate from sealed financial and knowledge clocks."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dcf import input_evidence as evidence
from dcf import meli_inputs
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    SourceObservation,
)
from provenance.issuer_registry import IssuerEntity, IssuerRegistry
from tests.test_meli_input_evidence import real_inputs as real_inputs
from tests.test_meli_input_evidence import source_context as source_context

STAMP = datetime(2026, 7, 31, tzinfo=UTC)
RAW = b"<html><body>Exact retained issuer financial source</body></html>"


@pytest.fixture
def captured_source(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> Iterator[tuple[sqlite3.Connection, Path, Path]]:
    conn = sqlite3.connect(migrated_db(tmp_path / "physical-source.db"))
    root = tmp_path / "source-state" / "data" / "evidence" / "blobs"
    root.mkdir(parents=True)
    path = root / "source.html"
    path.write_bytes(RAW)
    sha = hashlib.sha256(RAW).hexdigest()
    IssuerRegistry(conn).persist(
        IssuerEntity(
            issuer_id="physical-issuer",
            idempotency_key="physical-issuer",
            entity_kind="operating_company",
            created_at=STAMP,
        )
    )
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=sha,
            byte_size=len(RAW),
            media_type="text/html",
            storage_uri=path.as_uri(),
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id="physical-observation",
            idempotency_key="physical-observation",
            source_kind="sec_filing",
            source_url="https://www.sec.gov/Archives/edgar/data/1858985/000185898526000018/source.htm",
            blob_sha256=sha,
            source_published_at=None,
            filing_at=None,
            accepted_at=None,
            observed_at=STAMP,
            retrieved_at=STAMP,
            retrieval_config_sha256="a" * 64,
            collector_code_version="isolated-source-fixture.v1",
        )
    )
    ledger.persist(
        DocumentVersion(
            document_version_id="physical-document",
            document_key="physical-document",
            version_sequence=1,
            observation_id="physical-observation",
            blob_sha256=sha,
            issuer_id="physical-issuer",
            ticker="ONON",
            document_type="financial_statement",
            form_type="6-K",
            accession_number="0001858985-26-000018",
            period_end=datetime(2026, 6, 30, tzinfo=UTC),
            language="en",
            recorded_at=STAMP,
        )
    )
    conn.commit()
    try:
        yield conn, root, path
    finally:
        conn.close()


def verified_input() -> evidence.VerifiedInput:
    return evidence.VerifiedInput(
        key="reported_cash",
        value=10,
        reference=evidence.FactBinding(
            canonical_metric_cell_id="synthetic-cell",
            metric_id="synthetic-metric",
            metric_definition_revision_id="synthetic-definition",
            canonical_resolution_revision_id="synthetic-resolution",
            observation_id="synthetic-fact-observation",
            observation_payload_sha256="b" * 64,
        ),
        period_start=None,
        period_end=STAMP.date(),
        document_version_id="physical-document",
        reporting_entity_id="synthetic-reporting",
        unit_key="currency:CHF",
        currency="CHF",
        observation_kind="reported",
        knowledge_at=STAMP,
        recorded_at=STAMP,
    )


def test_present_bytes_replay_preserves_financial_clock(
    captured_source: tuple[sqlite3.Connection, Path, Path],
) -> None:
    conn, root, _path = captured_source
    context = evidence.SourceReadContext(content_roots=(root,))
    fact = verified_input()
    first = evidence.verify_input_source_bytes(conn, (fact,), context)
    second = evidence.verify_input_source_bytes(conn, (fact,), context)
    assert len(first) == len(second) == 1
    assert first[0].blob_sha256 == second[0].blob_sha256 == hashlib.sha256(RAW).hexdigest()
    assert first[0].byte_size == len(RAW)
    assert first[0].verified_at >= fact.knowledge_at
    assert second[0].verified_at >= first[0].verified_at
    assert fact.knowledge_at == fact.recorded_at == STAMP
    assert not conn.in_transaction
    assert conn.row_factory is None


@pytest.mark.parametrize(
    "failure", ["missing", "tampered", "size", "wrong_root", "symlink", "hardlink", "byte_limit"]
)
def test_unavailable_or_untrusted_source_refuses(
    captured_source: tuple[sqlite3.Connection, Path, Path], tmp_path: Path, failure: str
) -> None:
    conn, root, path = captured_source
    context = evidence.SourceReadContext(content_roots=(root,))
    if failure == "missing":
        path.unlink()
    elif failure == "tampered":
        path.write_bytes(b"X" * len(RAW))
    elif failure == "size":
        path.write_bytes(RAW + b"X")
    elif failure == "wrong_root":
        other = tmp_path / "different-approved-root"
        other.mkdir()
        context = evidence.SourceReadContext(content_roots=(other,))
    elif failure == "symlink":
        other = tmp_path / "outside.html"
        other.write_bytes(RAW)
        path.unlink()
        path.symlink_to(other)
    elif failure == "hardlink":
        os.link(path, root / "unapproved-alias.html")
    else:
        context = evidence.SourceReadContext(content_roots=(root,), max_document_bytes=len(RAW) - 1)
    with pytest.raises(evidence.InputEvidenceError, match="model_input_source_"):
        evidence.verify_input_source_bytes(conn, (verified_input(),), context)


def test_no_implicit_source_authority(
    captured_source: tuple[sqlite3.Connection, Path, Path],
) -> None:
    conn, _root, _path = captured_source
    with pytest.raises(evidence.InputEvidenceError, match="context_unavailable"):
        evidence.verify_input_source_bytes(conn, (verified_input(),), None)


def test_one_blob_still_has_one_witness_per_document(
    captured_source: tuple[sqlite3.Connection, Path, Path],
) -> None:
    conn, root, _path = captured_source
    fact = verified_input()
    witnesses = evidence.verify_input_source_bytes(
        conn,
        (fact, fact.model_copy(update={"key": "second-coordinate"})),
        evidence.SourceReadContext(content_roots=(root,)),
    )
    assert len(witnesses) == 1


def test_current_recipe_requires_explicit_present_byte_authority(
    real_inputs: tuple[sqlite3.Connection, evidence.ModelInputRequest],
) -> None:
    conn, request = real_inputs
    assert request.assumption_review is not None
    with pytest.raises(evidence.InputEvidenceError, match="model_input_source_context_unavailable"):
        evidence.verify_model_inputs(
            conn,
            request,
            recipe=meli_inputs.RECIPE,
            requirements=meli_inputs.requirements_for(request.financial_period_end),
            effective_inputs={key: item.value for key, item in request.assumptions.items()},
            assumption_keys=meli_inputs.ASSUMPTION_KEYS,
            as_of=request.assumption_review.reviewed_at,
        )


@pytest.mark.parametrize("failure", ["absent_authority", "tampered", "verified"])
def test_governed_reward_consumers_require_present_bytes_and_keep_generic_behavior(
    captured_source: tuple[sqlite3.Connection, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Isolate the consumer gate; source reads are real, financial approval is synthetic."""
    import risk_reward
    from allocation import model as allocation_model
    from dcf.readiness import ValuationReadiness

    conn, root, source = captured_source
    db = tmp_path / "physical-source.db"
    for ticker in ("ONON", "MELI", "META"):
        conn.execute(
            "INSERT INTO dcf_runs (ticker,valuation_date,horizon_years,revenue_growths_json,"
            "fcf_margin,wacc,terminal_growth,npv,npv_per_share,live_price,live_price_at,created_at) "
            "VALUES (?,? ,5,'[]',.1,.1,.03,200,200,100,?,?)",
            (ticker, STAMP.date().isoformat(), STAMP.isoformat(), STAMP.isoformat()),
        )
    conn.commit()
    if failure == "tampered":
        source.write_bytes(b"X" * len(RAW))
    context = (
        None if failure == "absent_authority" else evidence.SourceReadContext(content_roots=(root,))
    )
    calls: list[str] = []

    def qualified_financial_fixture(
        database: sqlite3.Connection,
        ticker: str,
        *,
        as_of: datetime,
        source_context: evidence.SourceReadContext | None = None,
    ) -> ValuationReadiness:
        assert ticker in {"MELI", "ONON"}, "Generic consumer policy must remain unchanged"
        calls.append(ticker)
        try:
            witnesses = evidence.verify_input_source_bytes(
                database, (verified_input(),), source_context
            )
        except evidence.InputEvidenceError as exc:
            return ValuationReadiness(
                ticker=ticker, evaluated_at=as_of.isoformat(), reason_codes=(str(exc),)
            )
        return ValuationReadiness(
            ticker=ticker,
            evaluated_at=as_of.isoformat(),
            ready=True,
            status="ready",
            source_integrity="present_bytes_verified",
            raw_document_verifications=witnesses,
        )

    monkeypatch.setattr(allocation_model, "load_valuation_readiness", qualified_financial_fixture)
    monkeypatch.setattr(risk_reward, "load_valuation_readiness", qualified_financial_fixture)
    upside = getattr(allocation_model, "_dcf_upside")(
        db, ["MELI", "ONON", "META"], as_of=STAMP, source_context=context
    )
    rewards = getattr(risk_reward, "_dcf_reward_legs")(
        db, ["MELI", "ONON", "META"], STAMP.date(), as_of=STAMP, source_context=context
    )
    assert upside["META"][0] == 1
    assert rewards["META"].expected_return == 1
    for ticker in ("MELI", "ONON"):
        if failure == "verified":
            assert upside[ticker][0] == 1
            assert rewards[ticker].expected_return == 1
        else:
            assert ticker not in upside
            assert rewards[ticker].expected_return is None
            assert "model_input_source_" in (rewards[ticker].confidence_reason or "")
    assert sorted(calls) == ["MELI", "MELI", "ONON", "ONON"]
    assert not conn.in_transaction


def shared_document_inputs(conn: sqlite3.Connection) -> tuple[evidence.VerifiedInput, ...]:
    """Two real captured versions share bytes; location variants are isolated below."""
    EvidenceLedger(conn).persist(
        DocumentVersion(
            document_version_id="physical-document-2",
            document_key="physical-document-2",
            version_sequence=1,
            observation_id="physical-observation",
            blob_sha256=hashlib.sha256(RAW).hexdigest(),
            issuer_id="physical-issuer",
            ticker="ONON",
            document_type="financial_statement",
            form_type="6-K",
            accession_number="0001858985-26-000018",
            period_end=STAMP,
            language="en",
            recorded_at=STAMP,
        )
    )
    conn.commit()
    return (
        verified_input(),
        verified_input().model_copy(
            update={"key": "second", "document_version_id": "physical-document-2"}
        ),
    )


@pytest.mark.parametrize("location", ["outside", "symlink", "distinct", "total_cap"])
def test_shared_blob_requires_each_candidate_location_and_its_own_uri_commitment(
    captured_source: tuple[sqlite3.Connection, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    location: str,
) -> None:
    """Use native version IDs and actual files; only location projection varies."""
    conn, root, _first = captured_source
    inputs = shared_document_inputs(conn)
    other = (tmp_path if location == "outside" else root) / "second.html"
    if location == "symlink":
        outside = tmp_path / "outside.html"
        outside.write_bytes(RAW)
        other.symlink_to(outside)
    else:
        other.write_bytes(RAW)
    select = evidence.select_evidence_native_candidates_by_id

    def projected_candidates(
        database: sqlite3.Connection,
        *,
        document_version_ids: tuple[str, ...],
        include_legacy: bool = False,
    ):
        candidates = select(
            database, document_version_ids=document_version_ids, include_legacy=include_legacy
        )
        return [
            item.model_copy(update={"storage_uri": other.as_uri()})
            if item.document_version_id == "physical-document-2"
            else item
            for item in candidates
        ]

    monkeypatch.setattr(evidence, "select_evidence_native_candidates_by_id", projected_candidates)
    context = evidence.SourceReadContext(
        content_roots=(root,), max_total_bytes=2 * len(RAW) - (1 if location == "total_cap" else 0)
    )
    if location == "distinct":
        witnesses = evidence.verify_input_source_bytes(conn, inputs, context)
        assert len(witnesses) == 2
        assert witnesses[0].storage_uri_sha256 != witnesses[1].storage_uri_sha256
        assert (
            witnesses[1].storage_uri_sha256 == hashlib.sha256(other.as_uri().encode()).hexdigest()
        )
    else:
        with pytest.raises(evidence.InputEvidenceError, match="model_input_source_"):
            evidence.verify_input_source_bytes(conn, inputs, context)


@pytest.mark.parametrize("population", ["missing", "duplicate", "foreign"])
def test_native_candidate_population_mismatch_refuses_before_source_reads(
    captured_source: tuple[sqlite3.Connection, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    population: str,
) -> None:
    conn, root, _path = captured_source
    inputs = shared_document_inputs(conn)
    select = evidence.select_evidence_native_candidates_by_id

    def projected_candidates(
        database: sqlite3.Connection,
        *,
        document_version_ids: tuple[str, ...],
        include_legacy: bool = False,
    ):
        candidates = select(
            database, document_version_ids=document_version_ids, include_legacy=include_legacy
        )
        if population == "missing":
            return candidates[:1]
        if population == "duplicate":
            return [candidates[0], candidates[0]]
        return [
            *candidates,
            candidates[0].model_copy(update={"document_version_id": "foreign-document"}),
        ]

    def forbidden_read(*args: object, **kwargs: object):
        raise AssertionError("Population mismatch must refuse before source bytes open")

    monkeypatch.setattr(evidence, "select_evidence_native_candidates_by_id", projected_candidates)
    monkeypatch.setattr(evidence, "read_stable_artifact", forbidden_read)
    with pytest.raises(evidence.InputEvidenceError, match="model_input_source_population_mismatch"):
        evidence.verify_input_source_bytes(
            conn, inputs, evidence.SourceReadContext(content_roots=(root,))
        )
