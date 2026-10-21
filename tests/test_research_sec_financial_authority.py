"""Native financial obligations follow captured SEC authority without new duties."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    SourceObservation,
)
from provenance.reporting_entity_registry import ReportingEntityRegistry, SourceObligationRevision
from provenance.research_snapshot import (
    DocumentProcessingPolicy,
    DocumentProcessingScope,
    derive_obligations,
)
from sqlite_runtime import register_sqlite_integrity_functions
from tests.test_source_fact_repository import seed_foundation

T0 = datetime(2032, 1, 1, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)
SEC_URL = "https://www.sec.gov/Archives/edgar/data/1691493/000169149326000001/nu-quarter.htm"


@pytest.fixture
def conn(tmp_path: Path, migrated_db: Callable[..., Path]) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "synthetic-source-authority.db"
    migrated_db(path)
    database = sqlite3.connect(path)
    database.execute("PRAGMA foreign_keys=ON")
    register_sqlite_integrity_functions(database)
    seed_foundation(database)
    yield database
    database.close()


def _source(
    conn: sqlite3.Connection,
    *,
    source_kind: str = "sec_filing",
    source_url: str = SEC_URL,
    form_type: str = "6-K",
    include_publisher: bool = True,
) -> None:
    ledger = EvidenceLedger(conn)
    raw = b"Synthetic financial statement source."
    digest = hashlib.sha256(raw).hexdigest()
    ledger.persist(
        ContentBlob(
            sha256=digest,
            byte_size=len(raw),
            media_type="text/plain",
            storage_uri="file:///synthetic-source.txt",
            recorded_at=T0,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id="authority-source",
            idempotency_key="authority-source",
            source_kind=source_kind,
            source_url=source_url,
            blob_sha256=digest,
            source_published_at=None,
            filing_at=None,
            accepted_at=None,
            observed_at=T0,
            retrieved_at=T0,
            retrieval_config_sha256="a" * 64,
            collector_code_version="synthetic-authority@1",
        )
    )
    ledger.persist(
        DocumentVersion(
            document_version_id="financial-source",
            document_key="financial-source",
            version_sequence=1,
            observation_id="authority-source",
            blob_sha256=digest,
            issuer_id="issuer-1",
            ticker="TEST",
            document_type="sec_financial_statements",
            form_type=form_type,
            language="en",
            recorded_at=T0,
        )
    )
    registry = ReportingEntityRegistry(conn)
    for authority, rule in (
        ("sec_edgar", "regulator_inventory"),
        ("issuer_publisher", "publisher_surface_exhaustion"),
    ):
        if authority == "issuer_publisher" and not include_publisher:
            continue
        registry.persist(
            SourceObligationRevision.model_validate(
                {
                    "obligation_revision_id": authority + "-financial-duty",
                    "idempotency_key": authority + "-financial-duty",
                    "obligation_key": "reporting-1:" + authority + ":issuer_financial_statements",
                    "revision": 1,
                    "issuer_id": "issuer-1",
                    "reporting_entity_id": "reporting-1",
                    "authority_kind": authority,
                    "document_family": "issuer_financial_statements",
                    "obligation_state": "required",
                    "completeness_rule": rule,
                    "active_from": T0,
                    "active_to": None,
                    "decision_kind": "deterministic",
                    "reason_code": "synthetic_authority",
                    "reason_details": (("source", "synthetic"),),
                    "effective_at": T0,
                    "knowledge_at": T0,
                    "recorded_at": T0,
                }
            )
        )


def _selected(conn: sqlite3.Connection) -> set[str]:
    obligations = derive_obligations(
        conn,
        DocumentProcessingScope(document_version_ids=("financial-source",)),
        T1,
        DocumentProcessingPolicy(policy_name="synthetic", policy_version="1"),
    )
    assert len(obligations) == 14
    return {item.source_obligation_revision_id for item in obligations}


def test_sec_six_k_financial_uses_existing_regulator_obligation(conn: sqlite3.Connection) -> None:
    _source(conn, include_publisher=False)
    duty_count = conn.execute("SELECT COUNT(*) FROM source_obligation_revisions").fetchone()[0]
    assert _selected(conn) == {"sec_edgar-financial-duty"}
    assert (
        conn.execute("SELECT COUNT(*) FROM source_obligation_revisions").fetchone()[0] == duty_count
    )


@pytest.mark.parametrize(
    "kind,url",
    [
        ("ir_document", "https://www.investidores.nu/financial-statements.pdf"),
        ("issuer_web", "https://issuer.example/financial-statements.pdf"),
        ("ir_document", SEC_URL),
        (
            "sec_filing",
            "https://www.sec.gov.evil.example/Archives/edgar/data/1691493/000169149326000001/nu.htm",
        ),
        ("sec_filing", SEC_URL.replace("https:", "http:")),
        ("sec_filing", SEC_URL.replace("www.sec.gov", "www.sec.gov:443")),
        ("sec_filing", SEC_URL.replace("www.sec.gov", "user@www.sec.gov")),
        ("sec_filing", SEC_URL + "?redirect=https://issuer.example"),
        ("sec_filing", SEC_URL + "#source"),
        ("sec_filing", SEC_URL.replace("nu-quarter.htm", "%2e%2e")),
        ("sec_filing", SEC_URL.replace("000169149326000001", "not-an-accession")),
    ],
)
def test_ir_legacy_and_noncanonical_sec_claims_keep_publisher_default(
    conn: sqlite3.Connection, kind: str, url: str
) -> None:
    _source(conn, source_kind=kind, source_url=url)
    assert _selected(conn) == {"issuer_publisher-financial-duty"}


def test_lookalike_host_cannot_use_sec_only_duty(conn: sqlite3.Connection) -> None:
    _source(
        conn,
        source_url=SEC_URL.replace("www.sec.gov", "www.sec.gov.example"),
        include_publisher=False,
    )
    with pytest.raises(ValueError, match="lacks an applicable source obligation"):
        _selected(conn)
    assert (
        conn.execute("SELECT COUNT(*) FROM document_processing_obligation_revisions").fetchone()[0]
        == 0
    )


@pytest.mark.parametrize("form", ["10-12B", "10-12B/A"])
def test_registration_behavior_stays_regulator_scoped(conn: sqlite3.Connection, form: str) -> None:
    _source(
        conn,
        source_kind="issuer_web",
        source_url="https://issuer.example/legacy.txt",
        form_type=form,
    )
    assert _selected(conn) == {"sec_edgar-financial-duty"}
