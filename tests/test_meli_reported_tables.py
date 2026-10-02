"""Isolated native-source publication tests using real migrated authorities."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from execution import publish_meli_reported_tables as cli
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    SourceObservation,
)
from provenance.fulltext_backfill import FullTextBackfillRequest, backfill_fulltext_evidence
from provenance.issuer_registry import (
    IdentifierAssertion,
    IdentifierResolution,
    IssuerRegistry,
    identifier_candidate_digest,
)
from provenance.meli_reported_tables import ReportedTableRequest, publish_meli_reported_tables
from provenance.reporting_entity_registry import ReportingEntityRegistry, SourceObligationRevision
from provenance.source_coverage import (
    CoverageAssessment,
    ExpectedDocument,
    SourceCoverageLedger,
    SourceInventorySnapshot,
)
from provenance.source_inventory_seal import (
    InventoryComponent,
    InventorySeal,
    SourceInventorySealStore,
    component_digest,
)
from sqlite_runtime import register_sqlite_integrity_functions
from tests.test_source_fact_repository import seed_foundation

FIXTURES = Path(__file__).parent / "fixtures" / "meli_reported_tables"
STAMP = datetime(2030, 1, 1, tzinfo=UTC)
END = datetime(2026, 6, 30, tzinfo=UTC)
START = datetime(2026, 1, 1, tzinfo=UTC)
URL = "https://www.sec.gov/Archives/edgar/data/1099590/000109959026000023/meli-20260630.htm"
ACCESSION = "0001099590-26-000023"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _seed(
    conn: sqlite3.Connection, root: Path, raw: bytes, *, document_accession: str = ACCESSION
) -> ReportedTableRequest:
    seed_foundation(conn)
    ledger = EvidenceLedger(conn)
    digest = _sha(raw)
    path = root / "source.html"
    path.write_bytes(raw)
    ledger.persist(
        ContentBlob(
            sha256=digest,
            byte_size=len(raw),
            media_type="text/html",
            storage_uri=path.as_uri(),
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id="table-source",
            idempotency_key="table-source",
            source_kind="sec_filing_document",
            source_url=URL,
            blob_sha256=digest,
            source_published_at=STAMP,
            filing_at=STAMP,
            accepted_at=STAMP,
            observed_at=STAMP,
            retrieved_at=STAMP,
            retrieval_config_sha256="a" * 64,
            collector_code_version="local-fixture.v1",
        )
    )
    ledger.persist(
        DocumentVersion(
            document_version_id="table-document",
            document_key="table-document-key",
            version_sequence=1,
            observation_id="table-source",
            blob_sha256=digest,
            issuer_id="issuer-1",
            ticker="MELI",
            document_type="filing",
            form_type="10-Q",
            accession_number=document_accession,
            period_start=None,
            period_end=END,
            language="en",
            recorded_at=STAMP,
        )
    )
    registry = IssuerRegistry(conn)
    assertion = IdentifierAssertion(
        assertion_id="table-cik",
        idempotency_key="table-cik",
        issuer_id="issuer-1",
        identifier_type="sec_cik",
        identifier_value="1099590",
        normalized_value="0001099590",
        authority="sec_registry",
        source_observation_id="table-source",
        effective_at=STAMP,
        knowledge_at=STAMP,
        recorded_at=STAMP,
    )
    registry.persist(assertion)
    registry.persist(
        IdentifierResolution(
            resolution_id="table-cik-selected",
            idempotency_key="table-cik-selected",
            resolution_key=assertion.resolution_key,
            revision=1,
            outcome="selected",
            selected_assertion_id=assertion.assertion_id,
            candidate_digest_sha256=identifier_candidate_digest((assertion,)),
            policy_name="synthetic_exact_cik",
            policy_version="1",
            policy_config_sha256="a" * 64,
            reason_code="exact_cik",
            reason_details=(("fixture", "local source-authentic excerpt"),),
            material_dissent=False,
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    )
    inventory_raw = b'{"fixture": "closed one-document inventory"}'
    inventory_path = root / "inventory.json"
    inventory_path.write_bytes(inventory_raw)
    ledger.persist(
        ContentBlob(
            sha256=_sha(inventory_raw),
            byte_size=len(inventory_raw),
            media_type="application/json",
            storage_uri=inventory_path.as_uri(),
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id="table-inventory-source",
            idempotency_key="table-inventory-source",
            source_kind="sec_submissions",
            source_url="https://data.sec.gov/submissions/CIK0001099590.json",
            blob_sha256=_sha(inventory_raw),
            source_published_at=None,
            filing_at=None,
            accepted_at=None,
            observed_at=STAMP,
            retrieved_at=STAMP,
            retrieval_config_sha256="a" * 64,
            collector_code_version="synthetic-fixture.v1",
        )
    )
    ReportingEntityRegistry(conn).persist(
        SourceObligationRevision(
            obligation_revision_id="table-obligation",
            idempotency_key="table-obligation",
            obligation_key="table-periodic",
            revision=1,
            issuer_id="issuer-1",
            authority_kind="sec_edgar",
            document_family="operating_company_periodic",
            obligation_state="required",
            completeness_rule="regulator_inventory",
            active_from=START,
            active_to=None,
            decision_kind="deterministic",
            reason_code="fixture_periodic",
            reason_details=(("fixture", "local"),),
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    )
    coverage = SourceCoverageLedger(conn)
    coverage.persist(
        SourceInventorySnapshot(
            snapshot_id="table-inventory",
            idempotency_key="table-inventory",
            inventory_key="table-inventory-key",
            revision=1,
            issuer_id="issuer-1",
            ticker="MELI",
            source_kind="sec_submissions",
            source_url="https://data.sec.gov/submissions/CIK0001099590.json",
            source_observation_id="table-inventory-source",
            outcome="succeeded",
            authoritative=True,
            retrieval_config_sha256="a" * 64,
            collector_code_version="synthetic-fixture.v1",
            started_at=STAMP,
            completed_at=STAMP,
            recorded_at=STAMP,
        )
    )
    coverage.persist(
        ExpectedDocument(
            expected_document_id="table-expected",
            idempotency_key="table-expected",
            snapshot_id="table-inventory",
            expected_document_key="table-document-key",
            issuer_id="issuer-1",
            ticker="MELI",
            source_kind="sec_filing",
            document_type="filing",
            form_type="10-Q",
            accession_number=ACCESSION,
            source_url=URL,
            primary_document="meli-20260630.htm",
            period_start=None,
            period_end=END,
            filing_at=STAMP,
            expected_at=None,
            expectation_basis="authoritative",
            recorded_at=STAMP,
        )
    )
    coverage.persist(
        CoverageAssessment(
            assessment_id="table-captured",
            idempotency_key="table-captured",
            expected_document_id="table-expected",
            revision=1,
            coverage_status="captured",
            document_version_id="table-document",
            extraction_run_id=None,
            manifest_id=None,
            index_run_id=None,
            reason_code="synthetic_capture",
            reason_details=(("fixture", "local hash checked"),),
            decision_kind="deterministic",
            policy_name="fixture",
            policy_version="1",
            policy_config_sha256="a" * 64,
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
            material_dissent=False,
        )
    )
    component = InventoryComponent(
        component_id="table-inventory-root",
        idempotency_key="table-inventory-root",
        snapshot_id="table-inventory",
        component_key="root",
        component_kind="primary",
        source_url="https://data.sec.gov/submissions/CIK0001099590.json",
        source_observation_id="table-inventory-source",
        outcome="succeeded",
        required=True,
        failure_reason=None,
        ordinal=0,
        recorded_at=STAMP,
    )
    seals = SourceInventorySealStore(conn)
    seals.persist(component)
    seals.persist(
        InventorySeal(
            snapshot_id="table-inventory",
            expected_component_count=1,
            component_digest_sha256=component_digest((component,)),
            completion_status="complete",
            sealed_at=STAMP,
        )
    )
    conn.commit()
    result = backfill_fulltext_evidence(
        conn,
        FullTextBackfillRequest(
            repo_root=root,
            source_lane="evidence_native",
            document_version_id="table-document",
            apply=True,
        ),
    )
    assert result.documents_extracted == 1
    run = conn.execute(
        "SELECT extraction_run_id FROM evidence_extraction_runs WHERE document_version_id='table-document'"
    ).fetchone()
    assert run is not None
    return ReportedTableRequest(
        inventory_key="table-inventory-key",
        accession_number=ACCESSION,
        document_version_id="table-document",
        fulltext_run_id=str(run[0]),
        content_roots=(root,),
        recorded_at=STAMP + timedelta(seconds=1),
    )


@pytest.fixture
def database(tmp_path: Path, migrated_db: Callable[..., Path]) -> Iterator[sqlite3.Connection]:
    path = migrated_db(tmp_path / "tables.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    register_sqlite_integrity_functions(conn)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def test_source_fixture_commitment() -> None:
    manifest = json.loads((FIXTURES / "provenance.json").read_text())
    assert _sha((FIXTURES / "h1-2026-snippet.html").read_bytes()) == manifest["snippet_sha256"]
    assert manifest["source_url"] == URL
    assert len(manifest["fragments"]) == 8


def test_real_authorities_dry_run_apply_replay(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    request = _seed(database, tmp_path, (FIXTURES / "h1-2026-snippet.html").read_bytes())
    before = database.total_changes
    dry = publish_meli_reported_tables(database, request)
    assert database.total_changes == before
    assert dry.source_population_complete
    assert [(p.metric, p.numeric_value, p.unit_key) for p in dry.population] == [
        ("nimal", "19.4", "percent"),
        ("available_cash_and_investments", "6751", "USD_millions"),
        ("total_debt_and_leases", "13176", "USD_millions"),
    ]
    assert dry.population[0].period_start == START
    assert dry.population[1].period_start is None
    applied = publish_meli_reported_tables(database, request.model_copy(update={"apply": True}))
    assert applied.canonical_admission == "not_performed"
    assert applied.captured_count == 3 and applied.rejected_count == 0
    assert applied.publication_id is not None and not applied.exact_replay
    repeated = publish_meli_reported_tables(
        database,
        request.model_copy(update={"apply": True, "recorded_at": STAMP + timedelta(days=1)}),
    )
    assert repeated.exact_replay and repeated.publication_id == applied.publication_id
    assert database.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
    assert (
        database.execute("SELECT count(*) FROM canonical_metric_definition_revisions").fetchone()[0]
        == 0
    )
    assert database.execute("SELECT count(*) FROM fact_observations_v2").fetchone()[0] == 3
    seal = database.execute(
        "SELECT expected_node_count,observed_node_count,reported_fact_count FROM fact_extraction_run_completeness_seals_v2"
    ).fetchone()
    assert tuple(seal) == (3, 3, 3)
    anchors = database.execute("SELECT source_locator_json FROM fact_observations_v2").fetchall()
    assert all(json.loads(row[0])["definition_node_ids"] for row in anchors)


def test_values_are_parsed_not_asserted(database: sqlite3.Connection, tmp_path: Path) -> None:
    raw = (
        (FIXTURES / "h1-2026-snippet.html")
        .read_bytes()
        .replace(b"19.4", b"18.7")
        .replace(b"6,751", b"6,700")
    )
    request = _seed(database, tmp_path, raw)
    result = publish_meli_reported_tables(database, request)
    assert result.source_population_complete
    assert result.population[0].numeric_value == "18.7"
    assert result.population[1].numeric_value == "6700"


@pytest.mark.parametrize(
    ("old", "new", "metric", "reason"),
    [
        (b"NIMAL </span>", b"NIMAL revised </span>", "nimal", "row_missing_or_ambiguous"),
        (
            b"total average gross loans receivable",
            b"total average net loans receivable",
            "nimal",
            "definition_missing_or_ambiguous",
        ),
        (
            b"Six Months Ended June 30,",
            b"Three Months Ended June 30,",
            "nimal",
            "unsupported_period_or_unit_columns",
        ),
        (
            b"Excludes time deposits, foreign debt securities",
            b"Includes time deposits, foreign debt securities",
            "available_cash_and_investments",
            "definition_missing_or_ambiguous",
        ),
        (
            b"Current Operating lease liabilities",
            b"Current Finance lease liabilities",
            "total_debt_and_leases",
            "debt_reconciliation_scope_missing",
        ),
    ],
)
def test_semantic_changes_get_closed_rejected_receipts(
    database: sqlite3.Connection, tmp_path: Path, old: bytes, new: bytes, metric: str, reason: str
) -> None:
    raw = (FIXTURES / "h1-2026-snippet.html").read_bytes()
    assert old in raw
    request = _seed(database, tmp_path, raw.replace(old, new))
    result = publish_meli_reported_tables(database, request.model_copy(update={"apply": True}))
    item = next(p for p in result.population if p.metric == metric)
    assert item.status == "rejected" and item.reason_code == reason
    assert not result.source_population_complete
    assert result.captured_count + result.rejected_count == 3
    seal = database.execute(
        "SELECT expected_node_count,observed_node_count,reported_fact_count FROM fact_extraction_run_completeness_seals_v2"
    ).fetchone()
    assert tuple(seal) == (3, 3, result.captured_count)


def test_tampered_bytes_fail_before_any_publication(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    request = _seed(database, tmp_path, (FIXTURES / "h1-2026-snippet.html").read_bytes())
    (tmp_path / "source.html").write_text("changed")
    before = database.total_changes
    with pytest.raises(ValueError, match="byte commitment"):
        publish_meli_reported_tables(database, request.model_copy(update={"apply": True}))
    assert database.total_changes == before


def test_native_target_ignores_corrupt_global_checkpoint(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    request = _seed(database, tmp_path, (FIXTURES / "h1-2026-snippet.html").read_bytes())
    state = tmp_path / ".tmp" / "fulltext-evidence-backfill" / "evidence-native-state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("deliberately invalid global checkpoint")
    result = backfill_fulltext_evidence(
        database,
        FullTextBackfillRequest(
            repo_root=tmp_path,
            source_lane="evidence_native",
            document_version_id=request.document_version_id,
            apply=True,
        ),
    )
    assert result.documents_considered == 1 and result.documents_skipped_covered == 1
    assert not result.has_more
    assert state.read_text() == "deliberately invalid global checkpoint"
    with pytest.raises(ValueError, match=r"missing|not found|unknown"):
        backfill_fulltext_evidence(
            database,
            FullTextBackfillRequest(
                repo_root=tmp_path, source_lane="evidence_native", document_version_id="absent"
            ),
        )
    with pytest.raises(ValueError, match="requires only"):
        backfill_fulltext_evidence(
            database,
            FullTextBackfillRequest(
                repo_root=tmp_path, document_version_id=request.document_version_id
            ),
        )


def test_cli_missing_database_is_unavailable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        cli.main(
            [
                "--db",
                str(tmp_path / "absent.db"),
                "--inventory-key",
                "i",
                "--accession",
                ACCESSION,
                "--document-version-id",
                "d",
                "--fulltext-run-id",
                "r",
                "--content-root",
                str(tmp_path),
            ]
        )
        == 3
    )
    assert json.loads(capsys.readouterr().out)["outcome"] == "unavailable"
    assert not (tmp_path / "absent.db").exists()


def test_native_coordinate_taint_is_rejected(database: sqlite3.Connection, tmp_path: Path) -> None:
    from provenance.evidence_ledger import EvidenceLocator, EvidenceNode

    request = _seed(database, tmp_path, (FIXTURES / "h1-2026-snippet.html").read_bytes())
    row = database.execute(
        "SELECT text,locator_json FROM evidence_nodes WHERE extraction_run_id=? AND node_kind='table_cell' LIMIT 1",
        (request.fulltext_run_id,),
    ).fetchone()
    locator = EvidenceLocator.model_validate_json(str(row[1]))
    forged = locator.model_copy(update={"table_column_index": 999})
    EvidenceLedger(database).persist(
        EvidenceNode(
            node_id="synthetic-tainted-node",
            evidence_key="synthetic-tainted-node",
            revision=1,
            extraction_run_id=request.fulltext_run_id,
            node_kind="table_cell",
            text=str(row[0]),
            locator=forged,
            recorded_at=STAMP,
        )
    )
    database.commit()
    before = database.total_changes
    with pytest.raises(ValueError, match="hierarchy does not replay"):
        publish_meli_reported_tables(database, request.model_copy(update={"apply": True}))
    assert database.total_changes == before


def test_ambiguous_duplicate_row_is_rejected(database: sqlite3.Connection, tmp_path: Path) -> None:
    source = (FIXTURES / "h1-2026-snippet.html").read_text()
    target = source.index("NIMAL </span>")
    start, end = source.rfind("<tr", 0, target), source.index("</tr>", target) + len("</tr>")
    raw = (source[:end] + source[start:end] + source[end:]).encode()
    request = _seed(database, tmp_path, raw)
    result = publish_meli_reported_tables(database, request)
    assert result.population[0].reason_code == "row_missing_or_ambiguous"
    assert result.captured_count == 2


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("document_version_id", "document-1", "current captured primary"),
        ("fulltext_run_id", "run-1", "qualified native HTML"),
        ("inventory_key", "absent-inventory", "sealed inventory"),
    ],
)
def test_missing_or_cross_document_authority_fails_closed(
    database: sqlite3.Connection,
    tmp_path: Path,
    field: str,
    value: str,
    reason: str,
) -> None:
    request = _seed(database, tmp_path, (FIXTURES / "h1-2026-snippet.html").read_bytes())
    before = database.total_changes
    with pytest.raises(ValueError, match=reason):
        publish_meli_reported_tables(
            database, request.model_copy(update={field: value, "apply": True})
        )
    assert database.total_changes == before


def test_publication_failure_rolls_back_scoped_nodes(
    database: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from provenance.source_fact_repository import SourceFactPublication, SourceFactRepository

    request = _seed(database, tmp_path, (FIXTURES / "h1-2026-snippet.html").read_bytes())

    def fail(_self: SourceFactRepository, _publication: SourceFactPublication) -> None:
        raise ValueError("injected publication failure")

    monkeypatch.setattr(SourceFactRepository, "publish", fail)
    with pytest.raises(ValueError, match="injected publication failure"):
        publish_meli_reported_tables(database, request.model_copy(update={"apply": True}))
    assert not database.in_transaction
    assert (
        database.execute(
            "SELECT count(*) FROM evidence_extraction_runs WHERE extractor_name='meli-reported-tables'"
        ).fetchone()[0]
        == 0
    )
    assert (
        database.execute(
            "SELECT count(*) FROM evidence_nodes WHERE node_id LIKE 'meli-table-node:%'"
        ).fetchone()[0]
        == 0
    )


def test_inventory_cannot_relabel_document_accession(
    database: sqlite3.Connection, tmp_path: Path
) -> None:
    request = _seed(
        database,
        tmp_path,
        (FIXTURES / "h1-2026-snippet.html").read_bytes(),
        document_accession="0001099590-25-000023",
    )
    before = database.total_changes
    with pytest.raises(ValueError, match="conflicts with inventory"):
        publish_meli_reported_tables(database, request.model_copy(update={"apply": True}))
    assert database.total_changes == before
    assert not database.in_transaction


def test_cli_runtime_guard_is_typed_unavailable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        raise RuntimeError("unsupported SQLite writer runtime")

    monkeypatch.setattr(cli, "connect_sqlite", unavailable)
    status = cli.main(
        [
            "--db",
            str(tmp_path / "isolated.db"),
            "--inventory-key",
            "i",
            "--accession",
            ACCESSION,
            "--document-version-id",
            "d",
            "--fulltext-run-id",
            "r",
            "--content-root",
            str(tmp_path),
            "--apply",
        ]
    )
    assert status == 3
    assert json.loads(capsys.readouterr().out) == {
        "outcome": "unavailable",
        "reason": "unsupported SQLite writer runtime",
    }
    assert not (tmp_path / "isolated.db").exists()
