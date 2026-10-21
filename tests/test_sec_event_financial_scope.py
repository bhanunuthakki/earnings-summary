"""Native 8-K numeric event spans preserve contractual scope and legacy review hashes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    SourceObservation,
)
from provenance.fact_read_model import FactReadModel
from provenance.fulltext_backfill import FullTextBackfillRequest, backfill_fulltext_evidence
from provenance.issuer_registry import (
    IdentifierAssertion,
    IdentifierResolution,
    IssuerRegistry,
    identifier_candidate_digest,
)
from provenance.reviewed_sec_financial_tables import (
    ReviewedSecFinancialFact,
    ReviewedSecFinancialRequest,
    ReviewedSecNumericFragment,
    assert_sec_source_identity,
    publish_reviewed_sec_financial_tables,
)
from sqlite_runtime import register_sqlite_integrity_functions
from tests.test_reviewed_sec_financial_tables import END
from tests.test_reviewed_sec_financial_tables import URL as LEGACY_URL
from tests.test_source_fact_repository import seed_foundation

STAMP = datetime(2030, 1, 1, tzinfo=UTC)
CIK = "0001099590"
ACCESSION = "0001099590-26-000099"
URL = "https://www.sec.gov/Archives/edgar/data/1099590/000109959026000099/fixture.htm"
RAW = b"""<html><body><p>Synthetic fixture: On September 14, 2026 the issuer closed USD 1,000,000,000 principal notes.</p><p>Synthetic fixture: Under the September 9, 2026 contract the underwriter purchase price is 97.364 percent of principal before other expenses.</p></body></html>"""


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _request(conn: sqlite3.Connection, root: Path) -> ReviewedSecFinancialRequest:
    seed_foundation(conn)
    path = root / "event.htm"
    path.write_bytes(RAW)
    ledger = EvidenceLedger(conn)
    digest = _sha(RAW)
    ledger.persist(
        ContentBlob(
            sha256=digest,
            byte_size=len(RAW),
            media_type="text/html",
            storage_uri=path.as_uri(),
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id="event-source",
            idempotency_key="event-source",
            source_kind="sec_filing",
            source_url=URL,
            blob_sha256=digest,
            source_published_at=None,
            filing_at=STAMP,
            accepted_at=STAMP,
            observed_at=STAMP,
            retrieved_at=STAMP,
            retrieval_config_sha256="a" * 64,
            collector_code_version="synthetic-test.v1",
        )
    )
    ledger.persist(
        DocumentVersion(
            document_version_id="event-doc",
            document_key="event-doc",
            version_sequence=1,
            observation_id="event-source",
            blob_sha256=digest,
            issuer_id="issuer-1",
            ticker="MELI",
            document_type="filing",
            form_type="8-K",
            accession_number=ACCESSION,
            period_end=datetime(2026, 9, 14, tzinfo=UTC),
            language="en",
            recorded_at=STAMP,
        )
    )
    registry = IssuerRegistry(conn)
    assertion = IdentifierAssertion(
        assertion_id="event-cik",
        idempotency_key="event-cik",
        issuer_id="issuer-1",
        identifier_type="sec_cik",
        identifier_value=CIK,
        normalized_value=CIK,
        authority="sec_registry",
        source_observation_id="event-source",
        effective_at=STAMP,
        knowledge_at=STAMP,
        recorded_at=STAMP,
    )
    registry.persist(assertion)
    registry.persist(
        IdentifierResolution(
            resolution_id="event-resolution",
            idempotency_key="event-resolution",
            resolution_key=assertion.resolution_key,
            revision=1,
            outcome="selected",
            selected_assertion_id=assertion.assertion_id,
            candidate_digest_sha256=identifier_candidate_digest((assertion,)),
            policy_name="synthetic-test",
            policy_version="1",
            policy_config_sha256="a" * 64,
            reason_code="test",
            reason_details=(("fixture", "synthetic"),),
            material_dissent=False,
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    )
    conn.commit()
    backfill_fulltext_evidence(
        conn,
        FullTextBackfillRequest(
            repo_root=root,
            source_lane="evidence_native",
            document_version_id="event-doc",
            apply=True,
        ),
    )
    run = str(
        conn.execute(
            "SELECT extraction_run_id FROM evidence_extraction_runs WHERE document_version_id='event-doc'"
        ).fetchone()[0]
    )
    nodes = conn.execute(
        "SELECT node_id,text,locator_sha256,locator_json FROM evidence_nodes "
        "WHERE extraction_run_id=? AND node_kind='passage'",
        (run,),
    ).fetchall()
    facts: list[ReviewedSecFinancialFact] = []
    for key, token, day, unit, currency in (
        ("principal", "1,000,000,000", 14, "USD", "USD"),
        ("purchase_fraction", "97.364", 9, "percent", None),
    ):
        matches = [row for row in nodes if token in str(row["text"])]
        row = min(matches, key=lambda item: len(str(item["text"])))
        text = str(row["text"])
        locator = json.loads(str(row["locator_json"]))
        start = int(locator["char_start"]) + text.index(token)
        facts.append(
            ReviewedSecFinancialFact(
                fact_key=key,
                concept_name=key,
                document_version_id="event-doc",
                fulltext_run_id=run,
                source_kind="sec_8k",
                source_doc_sha256=digest,
                accession_number=ACCESSION,
                sec_cik=CIK,
                value_node_id=str(row["node_id"]),
                value_locator_sha256=str(row["locator_sha256"]),
                raw_lexical_value=token,
                value_capture_kind="passage_numeric_span",
                value_fragments=(
                    ReviewedSecNumericFragment(
                        node_id=str(row["node_id"]),
                        char_start=start,
                        char_end=start + len(token),
                        raw_text=token,
                    ),
                ),
                unit_key=unit,
                currency=currency,
                period_end=datetime(2026, 9, day, tzinfo=UTC),
                fiscal_period="OTHER",
                accounting_basis="other",
                consolidation_scope="other",
                definition_text=text,
                definition_node_ids=(str(row["node_id"]),),
                context_node_ids=(str(row["node_id"]),),
            )
        )
    draft = ReviewedSecFinancialRequest.model_construct(
        ticker="MELI",
        reviewed_by="synthetic-reviewer",
        reviewed_at=STAMP + timedelta(seconds=1),
        review_evidence="Synthetic contractual event span review",
        expected_fact_keys=tuple(f.fact_key for f in facts),
        facts=tuple(facts),
        rejections=(),
        content_roots=(root,),
        recorded_at=STAMP + timedelta(seconds=1),
        review_sha256="0" * 64,
        apply=False,
    )
    return ReviewedSecFinancialRequest.model_validate(
        {
            **draft.model_dump(mode="json"),
            "review_sha256": _sha(draft.canonical_review_json.encode()),
        }
    )


@pytest.mark.parametrize(
    "source_currency",
    [
        "USD",
        "U.S. Dollars",
        "US dollars",
        "United States dollars",
        "U.S. dollar",
        "US dollar",
        "United States dollar",
    ],
)
def test_native_8k_numeric_spans_publish_other_scope_with_exact_source(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    source_currency: str,
) -> None:
    monkeypatch.setattr(
        "tests.test_sec_event_financial_scope.RAW", RAW.replace(b"USD", source_currency.encode())
    )
    conn = sqlite3.connect(migrated_db(tmp_path / "event.db"))
    conn.row_factory = sqlite3.Row
    register_sqlite_integrity_functions(conn)
    request = _request(conn, tmp_path)
    dry = publish_reviewed_sec_financial_tables(conn, request)
    apply = request.model_copy(update={"apply": True, "expected_plan_sha256": dry.plan_sha256})
    result = publish_reviewed_sec_financial_tables(conn, apply)
    assert result.captured_count == 2
    values: dict[str, tuple[str, str]] = {}
    for key, observation_id in result.observation_ids:
        bundle = FactReadModel(conn).provenance_bundle(observation_id, cutoff=request.reviewed_at)
        assert bundle.cell.accounting_basis == bundle.cell.consolidation_scope == "other"
        assert bundle.cell.fiscal_period == "OTHER"
        assert bundle.observation.observation_kind == "reported"
        assert bundle.evidence is not None
        definition = bundle.evidence.source_locator.root["definition"]
        assert isinstance(definition, dict)
        assert definition["consolidation_scope"] == "other"
        values[key] = (str(bundle.observation.decimal_value), bundle.cell.unit_key)
    assert values == {"principal": ("1000000000", "USD"), "purchase_fraction": ("0.97364", "pure")}
    assert publish_reviewed_sec_financial_tables(conn, apply).exact_replay
    conn.close()


@pytest.mark.parametrize("source_currency", ["$", "AUD", "CAD", "USDX", "XUSD", "U.S. dollarsX"])
def test_usd_witness_rejects_dollar_symbol_and_other_currency_tokens(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    source_currency: str,
) -> None:
    monkeypatch.setattr(
        "tests.test_sec_event_financial_scope.RAW", RAW.replace(b"USD", source_currency.encode())
    )
    conn = sqlite3.connect(migrated_db(tmp_path / "event.db"))
    conn.row_factory = sqlite3.Row
    register_sqlite_integrity_functions(conn)
    try:
        request = _request(conn, tmp_path)
        with pytest.raises(ValueError, match="unit has no exact source context witness"):
            publish_reviewed_sec_financial_tables(conn, request)
    finally:
        conn.close()


def test_default_consolidated_scope_does_not_change_legacy_review_encoding() -> None:
    fact = ReviewedSecFinancialFact(
        fact_key="cash",
        concept_name="Cash",
        document_version_id="doc",
        fulltext_run_id="run",
        source_kind="sec_6k",
        source_doc_sha256="0" * 64,
        accession_number="0001858985-26-000018",
        sec_cik="0001858985",
        value_node_id="node",
        value_locator_sha256="0" * 64,
        raw_lexical_value="2.5",
        unit_key="CHF_millions",
        currency="CHF",
        period_end=END,
        fiscal_period="Q2",
        accounting_basis="ifrs",
        definition_text="Cash",
        definition_node_ids=("definition",),
        context_node_ids=("context",),
    )
    assert "consolidation_scope" not in fact.model_dump(mode="json")
    encoded = fact.model_dump_json()
    assert ReviewedSecFinancialFact.model_validate_json(encoded).model_dump_json() == encoded
    assert fact.normalization()[0] == "CHF"
    assert_sec_source_identity(
        source_kind="sec_6k",
        form_type="6-K",
        accession_number=fact.accession_number,
        sec_cik=fact.sec_cik,
        source_url=LEGACY_URL,
    )


def test_8k_kind_does_not_waive_form_or_issuer_identity() -> None:
    assert_sec_source_identity(
        source_kind="sec_8k",
        form_type="8-K",
        accession_number=ACCESSION,
        sec_cik=CIK,
        source_url=URL,
    )
    for form, cik in (("6-K", CIK), ("8-K", "0001858985")):
        with pytest.raises(ValueError):
            assert_sec_source_identity(
                source_kind="sec_8k",
                form_type=form,
                accession_number=ACCESSION,
                sec_cik=cik,
                source_url=URL,
            )


def test_10q_kind_requires_exact_native_form_cik_and_accession() -> None:
    accession = "0001099590-26-000023"
    url = "https://www.sec.gov/Archives/edgar/data/1099590/000109959026000023/meli-20260630.htm"
    assert_sec_source_identity(
        source_kind="sec_10q",
        form_type="10-Q",
        accession_number=accession,
        sec_cik=CIK,
        source_url=url,
    )
    for form, cik, candidate_accession in (
        ("8-K", CIK, accession),
        ("10-Q", "0001691493", accession),
        ("10-Q", CIK, "0001099590-26-000024"),
    ):
        with pytest.raises(ValueError):
            assert_sec_source_identity(
                source_kind="sec_10q",
                form_type=form,
                accession_number=candidate_accession,
                sec_cik=cik,
                source_url=url,
            )
