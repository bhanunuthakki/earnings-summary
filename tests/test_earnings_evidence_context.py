"""Saved earnings context through current-schema source and trace owners."""

from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from pydantic import JsonValue

import earnings_readout
from llm_artifact_store import read_current
from provenance.evidence_links import BlobLocationObservation, EvidenceLinkLedger
from provenance.research_snapshot import ResearchSnapshotRequest
from search.heterogeneous_retrieval import (
    HeterogeneousRetrievalError,
    HeterogeneousRetrievalReceipt,
    HeterogeneousRetrievalRequest,
    NarrativeBundle,
    audit_research_snapshot_for_retrieval,
    retrieve_heterogeneous,
)
from tests.test_heterogeneous_retrieval import NOW
from tests.test_population_scoped_research_pipeline import (
    inherited_delta as inherited_delta,
)
from tests.test_population_scoped_research_pipeline import (
    scoped_research_pipeline as scoped_research_pipeline,
)
from tests.test_population_scoped_research_pipeline import (
    scoped_retrieval_trace as scoped_retrieval_trace,
)


@pytest.fixture
def retained_readout(
    scoped_retrieval_trace: tuple[sqlite3.Connection, HeterogeneousRetrievalReceipt],
    tmp_path: Path,
) -> Path:
    conn, receipt = scoped_retrieval_trace
    assert receipt.result_count > 0
    for document_id, ticker, period in (
        (100, "ACME", "2024-12-31"),
        (101, "ACME", "2023-12-31"),
        (102, "OTHER", "2024-12-31"),
    ):
        conn.execute(
            "INSERT OR IGNORE INTO tracked_companies(ticker,name,list_type) VALUES (?, 'Synthetic','evaluation')",
            (ticker,),
        )
        conn.execute(
            "INSERT INTO documents(id,ticker,source_type,doc_type,file_path,sha256,"
            "fetched_at,fetch_status,raw_bytes_size) VALUES (?,?,'synthetic',"
            "'transcript','synthetic-transcript',? ,?,'success',0)",
            (
                document_id,
                ticker,
                hashlib.sha256(str(document_id).encode()).hexdigest(),
                NOW.isoformat(),
            ),
        )
        conn.execute(
            "INSERT INTO transcripts(id,document_id,ticker,call_date,fiscal_period_type,"
            "period_end,recorded_at) VALUES (?,?,?,?,'Q4',?,?)",
            (document_id, document_id, ticker, NOW.isoformat(), period, NOW.isoformat()),
        )
    conn.commit()
    return tmp_path / "filing-xbrl-ledger.db"


@pytest.fixture
def model_prompts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    prompts: list[str] = []

    def model(prompt: str, **_kwargs: object) -> str:
        prompts.append(prompt)
        return "Synthetic bounded readout [1]."

    def budget(*_args: object, **_kwargs: object) -> None:
        return None

    def unexpected_current_context(*_args: object, **_kwargs: object) -> None:
        pytest.fail("saved trace mode read unrelated current context")

    monkeypatch.setattr(earnings_readout, "call_llm", model)
    monkeypatch.setattr(earnings_readout, "should_skip_for_budget", budget)
    monkeypatch.setattr(earnings_readout, "_context_blocks", unexpected_current_context)
    return prompts


def _generate(
    db: Path,
    root: Path,
    *,
    ticker: str = "ACME",
    period: str = "2024-12-31",
    cutoff: datetime = NOW,
    trace_id: str = "trace:current",
) -> earnings_readout.GenerateOutcome:
    return earnings_readout.generate_for_ticker(
        db,
        root,
        ticker,
        today=NOW.date(),
        period_end=period,
        fiscal_period_type="Q4",
        retrieval_trace_id=trace_id,
        knowledge_cutoff=cutoff,
    )


def test_real_retained_trace_generates_and_reuses_readout(
    retained_readout: Path, tmp_path: Path, model_prompts: list[str]
) -> None:
    result = _generate(retained_readout, tmp_path)
    assert result.status == earnings_readout.GENERATED
    assert _generate(retained_readout, tmp_path).status == earnings_readout.CACHE_HIT
    assert len(model_prompts) == 1
    artifact = read_current(
        ticker="ACME",
        purpose=earnings_readout.PURPOSE,
        fiscal_period="2024-12-31",
        db_path=retained_readout,
    )
    assert artifact is not None
    manifest = artifact.content_json
    assert isinstance(manifest, dict)
    manifest = cast(dict[str, JsonValue], manifest)
    assert manifest["grounding_status"] == "partial"
    retained = manifest["retained_evidence"]
    assert isinstance(retained, dict)
    assert retained["comparison_coverage"] == "not_established_for_requested_fiscal_period"
    assert retained["requested_fiscal_period_type"] == "Q4"
    facts = retained["fact_context"]
    assert isinstance(facts, list) and len(facts) == 1
    fact = facts[0]
    assert isinstance(fact, dict)
    assert fact["source_fiscal_period"] == "FY"
    assert fact["accounting_basis"] == "us_gaap"
    assert fact["consolidation_scope"] == "consolidated"
    entry = fact["projection_entry"]
    assert isinstance(entry, dict)
    assert isinstance(entry["period_start"], str)
    assert isinstance(entry["period_end"], str)
    assert entry["period_start"].startswith("2024-01-01")
    assert entry["period_end"].startswith("2024-12-31")
    assert entry["period_kind"] == "duration"
    assert entry["currency"] == "USD"
    assert entry["unit_key"] == "iso4217:USD"
    assert entry["metric_definition_revision_id"] == "metric:revenue:v1"
    assert "Annual facts remain annual context, never quarterly actuals" in model_prompts[0]
    assert '"source_fiscal_period":"FY"' in model_prompts[0]
    assert "[1]" in model_prompts[0]
    assert "Thesis update" in model_prompts[0]
    assert retained["research_snapshot_request_sha256"]


@pytest.mark.parametrize("mismatch", ("issuer", "period", "cutoff", "missing_trace"))
def test_real_retained_trace_rejects_mismatched_request_before_llm(
    retained_readout: Path, tmp_path: Path, model_prompts: list[str], mismatch: str
) -> None:
    with pytest.raises(earnings_readout.ReadoutUnavailableError) as error:
        _generate(
            retained_readout,
            tmp_path,
            ticker="OTHER" if mismatch == "issuer" else "ACME",
            period="2023-12-31" if mismatch == "period" else "2024-12-31",
            cutoff=NOW + timedelta(seconds=1) if mismatch == "cutoff" else NOW,
            trace_id="trace:absent" if mismatch == "missing_trace" else "trace:current",
        )
    assert not model_prompts
    if mismatch == "missing_trace":
        assert isinstance(error.value.__cause__, HeterogeneousRetrievalError)


@pytest.mark.parametrize("failure", ("mutated", "missing", "unsupported_uri", "outside_root"))
def test_real_retained_source_bytes_fail_closed_before_llm(
    retained_readout: Path,
    tmp_path: Path,
    model_prompts: list[str],
    failure: str,
    scoped_retrieval_trace: tuple[sqlite3.Connection, HeterogeneousRetrievalReceipt],
) -> None:
    blob = tmp_path / "data/evidence/blobs/filing.xhtml"
    if failure == "mutated":
        blob.write_bytes(b"x" * len(blob.read_bytes()))
    elif failure == "missing":
        blob.unlink()
    else:
        conn, _receipt = scoped_retrieval_trace
        raw = blob.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        uri = (
            "https://example.invalid/unsupported"
            if failure == "unsupported_uri"
            else (tmp_path / "outside.xhtml").as_uri()
        )
        EvidenceLinkLedger(conn).persist_location(
            BlobLocationObservation(
                location_observation_id="unsupported-location",
                idempotency_key="unsupported-location",
                blob_sha256=sha,
                storage_uri=uri,
                location_kind="local",
                availability_state="present",
                location_sequence=1,
                verified_at=NOW,
                verified_byte_size=len(raw),
                verified_sha256=sha,
                recorded_at=NOW,
            )
        )
        conn.commit()
    with pytest.raises(earnings_readout.ReadoutUnavailableError):
        _generate(retained_readout, tmp_path)
    assert not model_prompts


def test_real_trace_identity_change_invalidates_cache_without_text_change(
    retained_readout: Path,
    tmp_path: Path,
    model_prompts: list[str],
    scoped_retrieval_trace: tuple[sqlite3.Connection, HeterogeneousRetrievalReceipt],
) -> None:
    first = _generate(retained_readout, tmp_path)
    conn, receipt = scoped_retrieval_trace
    row = conn.execute(
        "SELECT request_json FROM research_snapshot_headers WHERE research_snapshot_id=?",
        (receipt.research_snapshot_id,),
    ).fetchone()
    request = ResearchSnapshotRequest.model_validate_json(str(row[0]))
    bundle = request.corpus_bundles[0]
    assert bundle.lexical_index_run_id is not None
    retrieve_heterogeneous(
        conn,
        HeterogeneousRetrievalRequest(
            trace_id="trace:repeated",
            idempotency_key="trace:repeated",
            research_snapshot_id=request.research_snapshot_id,
            fact_generation_id=request.canonical_fact_projection_run_id,
            narrative_bundles=(
                NarrativeBundle(
                    corpus_manifest_id=bundle.corpus_manifest_id,
                    lexical_index_run_id=bundle.lexical_index_run_id,
                ),
            ),
            query_text="Revenue 2024",
            cutoff_at=NOW,
            recorded_at=NOW,
        ),
    )
    conn.commit()
    second = _generate(retained_readout, tmp_path, trace_id="trace:repeated")
    assert first.status == second.status == earnings_readout.GENERATED
    assert first.artifact_id != second.artifact_id
    assert len(model_prompts) == 2
    assert model_prompts[0] == model_prompts[1]


def test_http_retained_request_generates_from_real_saved_trace(
    retained_readout: Path,
    tmp_path: Path,
    model_prompts: list[str],
) -> None:
    import comments_server

    with ThreadPoolExecutor(max_workers=1) as executor:
        client = comments_server.create_app(
            tmp_path, db_path=retained_readout, chat_executor=executor
        ).test_client()
        response = client.post(
            "/api/earnings-readout/generate",
            json={
                "ticker": "ACME",
                "period_end": "2024-12-31",
                "fiscal_period_type": "Q4",
                "retrieval_trace_id": "trace:current",
                "knowledge_cutoff": NOW.isoformat(),
            },
        )
        assert response.status_code == 200
        assert len(model_prompts) == 1
        missing = client.post(
            "/api/earnings-readout/generate",
            json={
                "ticker": "ACME",
                "period_end": "2024-12-31",
                "fiscal_period_type": "Q4",
                "retrieval_trace_id": "trace:absent",
                "knowledge_cutoff": NOW.isoformat(),
            },
        )
        assert missing.status_code == 404
        assert len(model_prompts) == 1


def test_cache_hit_still_requires_unchanged_source_bytes(
    retained_readout: Path,
    tmp_path: Path,
    model_prompts: list[str],
) -> None:
    first = _generate(retained_readout, tmp_path)
    blob = tmp_path / "data/evidence/blobs/filing.xhtml"
    blob.write_bytes(b"x" * len(blob.read_bytes()))
    with pytest.raises(earnings_readout.ReadoutUnavailableError):
        _generate(retained_readout, tmp_path)
    assert len(model_prompts) == 1
    artifact = read_current(
        ticker="ACME",
        purpose=earnings_readout.PURPOSE,
        fiscal_period="2024-12-31",
        db_path=retained_readout,
    )
    assert artifact is not None and artifact.id == first.artifact_id


def test_readout_retains_inherited_delta_fact_source_generation(
    retained_readout: Path,
    tmp_path: Path,
    model_prompts: list[str],
    inherited_delta: tuple[sqlite3.Connection, ResearchSnapshotRequest],
) -> None:
    conn, request = inherited_delta
    audit_research_snapshot_for_retrieval(conn, request.research_snapshot_id, audited_at=NOW)
    bundle = request.corpus_bundles[0]
    assert bundle.lexical_index_run_id is not None
    retrieve_heterogeneous(
        conn,
        HeterogeneousRetrievalRequest(
            trace_id="trace:delta-readout",
            idempotency_key="trace:delta-readout",
            research_snapshot_id=request.research_snapshot_id,
            fact_generation_id=request.canonical_fact_projection_run_id,
            narrative_bundles=(
                NarrativeBundle(
                    corpus_manifest_id=bundle.corpus_manifest_id,
                    lexical_index_run_id=bundle.lexical_index_run_id,
                ),
            ),
            query_text="Revenue 2024",
            cutoff_at=NOW,
            recorded_at=NOW,
        ),
    )
    conn.commit()
    assert (
        _generate(retained_readout, tmp_path, trace_id="trace:delta-readout").status
        == earnings_readout.GENERATED
    )
    assert len(model_prompts) == 1
    artifact = read_current(
        ticker="ACME",
        purpose=earnings_readout.PURPOSE,
        fiscal_period="2024-12-31",
        db_path=retained_readout,
    )
    assert artifact is not None
    manifest = artifact.content_json
    assert isinstance(manifest, dict)
    manifest = cast(dict[str, JsonValue], manifest)
    retained = manifest["retained_evidence"]
    assert isinstance(retained, dict)
    assert retained["fact_generation_id"] == "projection:unchanged-delta"
    facts = retained["fact_context"]
    assert isinstance(facts, list)
    fact = facts[0]
    assert isinstance(fact, dict)
    assert fact["source_generation_id"] == "projection:checkpoint"
