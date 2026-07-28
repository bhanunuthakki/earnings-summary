from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from provenance.filing_xbrl_extraction_ledger import (
    FilingXbrlExtractionLedger,
)
from provenance.filing_xbrl_fact_adapter import (
    DuplicateFilingXbrlDisposition,
    FilingXbrlDimension,
    FilingXbrlExtractionIdentity,
    FilingXbrlFactAdapter,
    FilingXbrlNormalizedOutput,
    FilingXbrlSubjectIdentity,
    NormalizedFilingXbrlFact,
    PublishedFilingXbrlDisposition,
    QuarantinedFilingXbrlDisposition,
)
from provenance.source_fact_repository import SourceFactRepository

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
PERIOD_END = STAMP - timedelta(days=30)
REVISION = "0246_source_fact_publication_stream"
BASE_REVISION = "0213_decision_draft_provider_id"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _locator(path: str) -> tuple[dict[str, str], str]:
    value = {"path": path}
    canonical = f'{{"path":"{path}"}}'
    return value, _sha(canonical)


def _dimension(
    axis: str,
    *,
    application: str = "explicit",
    typed: bool = False,
) -> FilingXbrlDimension:
    if typed:
        return FilingXbrlDimension.model_validate(
            {
                "application": application,
                "axis_namespace": "https://xbrl.example/dimensions",
                "axis_name": axis,
                "member_kind": "typed",
                "typed_member_value": {
                    "lexical": "EMEA",
                    "type": "xbrli:stringItemType",
                },
            }
        )
    return FilingXbrlDimension.model_validate(
        {
            "application": application,
            "axis_namespace": "https://xbrl.example/dimensions",
            "axis_name": axis,
            "member_kind": "explicit",
            "explicit_member_namespace": "https://xbrl.example/members",
            "explicit_member_name": f"{axis}Member",
        }
    )


def _entry(
    ordinal: int,
    *,
    concept_name: str = "Revenue",
    value_kind: str = "numeric",
    numeric_value: Decimal = Decimal("100.000"),
    source_entry_sha256: str | None = None,
    evidence_node_id: str | None = None,
    dimensions: tuple[FilingXbrlDimension, ...] = (),
    source_taxonomy_version: str = "2026",
    include_source_context: bool = True,
    include_source_unit: bool = True,
    knowledge_at: datetime = STAMP,
    recorded_at: datetime = STAMP,
) -> NormalizedFilingXbrlFact:
    path = f"/html/body/xbrl/{ordinal}"
    source_locator, source_locator_sha256 = _locator(path)
    raw = {
        "ordinal": ordinal,
        "evidence_node_id": evidence_node_id or f"node-{ordinal}",
        "concept_namespace": "https://fasb.org/us-gaap/2026",
        "concept_name": concept_name,
        "taxonomy_name": "US GAAP",
        "source_taxonomy_version": source_taxonomy_version,
        "accounting_basis": "us_gaap",
        "consolidation_scope": "consolidated",
        "period_kind": "duration",
        "period_start": PERIOD_END - timedelta(days=365),
        "period_end": PERIOD_END,
        "fiscal_year": 2026,
        "fiscal_period": "FY",
        "dimensions": dimensions,
        "unit_key": "iso4217:USD",
        "currency": "usd",
        "value_kind": value_kind,
        "numeric_value": numeric_value if value_kind == "numeric" else None,
        "text_value": "profitable" if value_kind == "text" else None,
        "is_nil": value_kind == "nil",
        "raw_lexical_value": (
            "100.000"
            if value_kind == "numeric"
            else ("profitable" if value_kind == "text" else None)
        ),
        "source_context_id": (f"context-{ordinal}" if include_source_context else None),
        "source_unit_id": (
            f"unit-{ordinal}" if value_kind == "numeric" and include_source_unit else None
        ),
        "decimals": "-3" if value_kind == "numeric" else None,
        "precision": None,
        "source_locator": source_locator,
        "source_locator_sha256": source_locator_sha256,
        "source_entry_sha256": (source_entry_sha256 or _sha(f"source-entry-{ordinal}")),
        "effective_at": PERIOD_END,
        "knowledge_at": knowledge_at,
        "recorded_at": recorded_at,
    }
    return NormalizedFilingXbrlFact.model_validate(raw)


def _output(
    entries: tuple[NormalizedFilingXbrlFact, ...],
    *,
    expected_evidence_node_count: int | None = None,
    document_version_id: str = "document-1",
    extraction_run_id: str = "run-1",
    extraction_input: str = "filing-bytes",
    knowledge_at: datetime = STAMP,
    recorded_at: datetime = STAMP,
) -> FilingXbrlNormalizedOutput:
    extraction = FilingXbrlExtractionIdentity(
        document_version_id=document_version_id,
        extraction_run_id=extraction_run_id,
        extractor_name="filing-inline-xbrl",
        extractor_code_version="v3",
        extractor_config_sha256=_sha("extractor-config"),
        extraction_input_sha256=_sha(extraction_input),
        extraction_output_sha256="0" * 64,
        expected_evidence_node_count=(
            len(entries) if expected_evidence_node_count is None else expected_evidence_node_count
        ),
        knowledge_at=knowledge_at,
        recorded_at=recorded_at,
    )
    subject = FilingXbrlSubjectIdentity(
        reporting_entity_id="reporting-1",
        selected_subject_binding_revision_id="binding-1",
    )
    return FilingXbrlNormalizedOutput.with_computed_digest(
        extraction=extraction,
        subject=subject,
        entries=entries,
    )


def test_all_fact_kinds_have_one_publish_disposition() -> None:
    result = FilingXbrlFactAdapter().adapt(
        _output(
            (
                _entry(0, value_kind="numeric"),
                _entry(1, concept_name="BusinessDescription", value_kind="text"),
                _entry(2, concept_name="UndisclosedMetric", value_kind="nil"),
            )
        )
    )

    assert result.total_count == 3
    assert result.published_count == 3
    assert result.duplicate_count == 0
    assert result.quarantined_count == 0
    assert all(isinstance(item, PublishedFilingXbrlDisposition) for item in result.dispositions)
    observations = tuple(item.observation for item in result.publication.reported_facts)
    assert tuple(item.value_kind for item in observations) == (
        "numeric",
        "text",
        "nil",
    )
    assert observations[0].numeric_value == "100"
    assert observations[1].text_value == "profitable"
    assert observations[2].is_nil


def test_every_xbrl_fact_requires_a_source_context() -> None:
    with pytest.raises(
        ValueError,
        match="every XBRL fact requires source_context_id",
    ):
        _entry(
            0,
            value_kind="text",
            include_source_context=False,
        )


def test_numeric_xbrl_fact_requires_a_source_unit() -> None:
    with pytest.raises(
        ValueError,
        match="numeric XBRL facts require source_unit_id",
    ):
        _entry(0, include_source_unit=False)


def test_dimension_order_and_default_application_normalize_identity() -> None:
    product = _dimension("ProductAxis")
    geography = _dimension("GeographyAxis", application="defaulted", typed=True)
    first = FilingXbrlFactAdapter().adapt(_output((_entry(0, dimensions=(product, geography)),)))
    second = FilingXbrlFactAdapter().adapt(_output((_entry(0, dimensions=(geography, product)),)))

    first_cell = first.publication.reported_facts[0].cell
    second_cell = second.publication.reported_facts[0].cell
    assert first_cell.fact_cell_id == second_cell.fact_cell_id
    assert first_cell.dimensions == second_cell.dimensions
    assert tuple(item.axis_name for item in first_cell.dimensions) == (
        "GeographyAxis",
        "ProductAxis",
    )
    assert first_cell.dimensions[0].typed_member_value is not None


def test_adapter_ids_and_publication_are_deterministic() -> None:
    output = _output((_entry(0),))
    first = FilingXbrlFactAdapter().adapt(output)
    second = FilingXbrlFactAdapter().adapt(output)

    assert first == second
    assert first.publication.publication_id.startswith("xbrl-publication:")
    assert first.published_fact_cell_ids[0].startswith("xbrl-cell:")
    assert first.published_observation_ids[0].startswith("xbrl-observation:")


def test_normalized_output_rejects_uncommitted_caller_hash() -> None:
    output = _output((_entry(0),))
    with pytest.raises(
        ValueError,
        match="must match the canonical normalized",
    ):
        FilingXbrlNormalizedOutput(
            extraction=output.extraction.model_copy(
                update={"extraction_output_sha256": _sha("arbitrary")}
            ),
            subject=output.subject,
            entries=output.entries,
        )


def test_exact_duplicate_source_entry_links_to_published_primary() -> None:
    duplicate_hash = _sha("duplicate")
    original = _entry(0, source_entry_sha256=duplicate_hash)
    duplicate = original.model_copy(update={"ordinal": 1})
    result = FilingXbrlFactAdapter().adapt(_output((original, duplicate)))

    assert result.published_count == 1
    assert result.duplicate_count == 1
    assert result.quarantined_count == 0
    assert isinstance(
        result.dispositions[0],
        PublishedFilingXbrlDisposition,
    )
    duplicate = result.dispositions[1]
    assert isinstance(duplicate, DuplicateFilingXbrlDisposition)
    assert duplicate.primary_ordinal == 0
    assert duplicate.observation_id == result.dispositions[0].observation_id
    assert len(result.publication.reported_facts) == 1


def test_conflicting_duplicate_source_entry_fails_closed() -> None:
    duplicate_hash = _sha("conflict")
    revenue = _entry(0, source_entry_sha256=duplicate_hash)
    assets = _entry(
        1,
        concept_name="Assets",
        source_entry_sha256=duplicate_hash,
    )
    result = FilingXbrlFactAdapter().adapt(_output((revenue, assets)))

    assert result.published_count == 0
    assert result.duplicate_count == 0
    assert result.quarantined_count == 2
    assert all(
        isinstance(item, QuarantinedFilingXbrlDisposition)
        and item.reason == "conflicting_source_entry_identity"
        for item in result.dispositions
    )


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


@pytest.fixture
def repository_conn(
    tmp_path: Path,
) -> Generator[sqlite3.Connection, None, None]:
    path = tmp_path / "filing-xbrl-adapter.db"
    database = sqlite3.connect(path)
    database.executescript(
        """
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY,
            source_doc_id INTEGER NOT NULL
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY,
            source_doc_id INTEGER NOT NULL
        );
        """
    )
    database.commit()
    database.close()
    config = _config(path)
    command.stamp(config, BASE_REVISION)
    command.upgrade(config, REVISION)
    database = sqlite3.connect(path)
    database.execute("PRAGMA foreign_keys = ON")
    try:
        yield database
    finally:
        database.close()


def _seed_repository_foundation(
    conn: sqlite3.Connection,
    extraction_output_sha256: str,
) -> None:
    conn.execute(
        "INSERT INTO issuer_entities VALUES (?,?,?,?)",
        ("issuer-1", "issuer-key-1", "operating_company", STAMP),
    )
    conn.execute(
        "INSERT INTO reporting_entities VALUES (?,?,?,?,?,?)",
        (
            "reporting-1",
            "reporting-key-1",
            "issuer-1",
            "legal_registrant",
            "Issuer One",
            STAMP,
        ),
    )
    blob_sha256 = _sha("filing-bytes")
    conn.execute(
        "INSERT INTO evidence_content_blobs VALUES (?,?,?,?,?)",
        (
            blob_sha256,
            len("filing-bytes"),
            "application/xhtml+xml",
            "file:///filing.xhtml",
            STAMP,
        ),
    )
    conn.execute(
        "INSERT INTO evidence_source_observations "
        "(observation_id,idempotency_key,source_kind,source_url,blob_sha256,"
        "source_published_at,filing_at,accepted_at,observed_at,retrieved_at,"
        "retrieval_config_sha256,collector_code_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "source-1",
            "source-key-1",
            "sec_filing",
            "https://www.sec.gov/Archives/example/filing.xhtml",
            blob_sha256,
            STAMP,
            STAMP,
            STAMP,
            STAMP,
            STAMP,
            _sha("retrieval"),
            "test-v1",
        ),
    )
    conn.execute(
        "INSERT INTO evidence_document_versions "
        "(document_version_id,document_key,version_sequence,observation_id,"
        "blob_sha256,issuer_id,ticker,document_type,form_type,accession_number,"
        "exhibit_id,period_start,period_end,as_of_at,language,"
        "replaces_document_version_id,legacy_document_id,recorded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "document-1",
            "document-key-1",
            1,
            "source-1",
            blob_sha256,
            "issuer-1",
            None,
            "regulatory_filing",
            "10-K",
            "0000000001-26-000001",
            None,
            PERIOD_END - timedelta(days=365),
            PERIOD_END,
            PERIOD_END,
            "en",
            None,
            None,
            STAMP,
        ),
    )
    conn.execute(
        "INSERT INTO recorded_subject_binding_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "binding-1",
            "binding-key-1",
            "issuer-1",
            1,
            "issuer-1",
            "reporting-1",
            None,
            "selected",
            "deterministic",
            "exact_subject",
            "{}",
            0,
            STAMP,
            STAMP,
            STAMP,
            None,
        ),
    )
    conn.execute(
        "INSERT INTO evidence_extraction_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "run-1",
            "run-key-1",
            "document-1",
            blob_sha256,
            "filing-inline-xbrl",
            _sha("extractor-config"),
            "v3",
            extraction_output_sha256,
            STAMP,
            STAMP,
            "succeeded",
        ),
    )
    source_locator, source_locator_sha256 = _locator("/html/body/xbrl/0")
    conn.execute(
        "INSERT INTO evidence_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "node-0",
            "node-key-0",
            1,
            "run-1",
            None,
            None,
            "table_cell",
            "100.000",
            '{"path":"/html/body/xbrl/0"}',
            source_locator_sha256,
            STAMP,
        ),
    )
    assert source_locator == {"path": "/html/body/xbrl/0"}


def _seed_second_filing(
    conn: sqlite3.Connection,
    recorded_at: datetime,
    extraction_output_sha256: str,
) -> None:
    blob_sha256 = _sha("filing-2-bytes")
    conn.execute(
        "INSERT INTO evidence_content_blobs VALUES (?,?,?,?,?)",
        (
            blob_sha256,
            len("filing-2-bytes"),
            "application/xhtml+xml",
            "file:///filing-2.xhtml",
            recorded_at,
        ),
    )
    conn.execute(
        "INSERT INTO evidence_source_observations "
        "(observation_id,idempotency_key,source_kind,source_url,blob_sha256,"
        "source_published_at,filing_at,accepted_at,observed_at,retrieved_at,"
        "retrieval_config_sha256,collector_code_version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "source-2",
            "source-key-2",
            "sec_filing",
            "https://www.sec.gov/Archives/example/filing-2.xhtml",
            blob_sha256,
            recorded_at,
            recorded_at,
            recorded_at,
            recorded_at,
            recorded_at,
            _sha("retrieval"),
            "test-v1",
        ),
    )
    conn.execute(
        "INSERT INTO evidence_document_versions "
        "(document_version_id,document_key,version_sequence,observation_id,"
        "blob_sha256,issuer_id,ticker,document_type,form_type,accession_number,"
        "exhibit_id,period_start,period_end,as_of_at,language,"
        "replaces_document_version_id,legacy_document_id,recorded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "document-2",
            "document-key-2",
            1,
            "source-2",
            blob_sha256,
            "issuer-1",
            None,
            "regulatory_filing",
            "10-K/A",
            "0000000001-26-000002",
            None,
            PERIOD_END - timedelta(days=365),
            PERIOD_END,
            PERIOD_END,
            "en",
            None,
            None,
            recorded_at,
        ),
    )
    conn.execute(
        "INSERT INTO evidence_extraction_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "run-2",
            "run-key-2",
            "document-2",
            blob_sha256,
            "filing-inline-xbrl",
            _sha("extractor-config"),
            "v3",
            extraction_output_sha256,
            recorded_at,
            recorded_at,
            "succeeded",
        ),
    )
    _, source_locator_sha256 = _locator("/html/body/xbrl/0")
    conn.execute(
        "INSERT INTO evidence_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "node-1",
            "node-key-1",
            1,
            "run-2",
            None,
            None,
            "table_cell",
            "125.000",
            '{"path":"/html/body/xbrl/0"}',
            source_locator_sha256,
            recorded_at,
        ),
    )
    conn.commit()


def test_publication_persists_through_source_fact_repository(
    repository_conn: sqlite3.Connection,
) -> None:
    output = _output(
        (_entry(0),),
        expected_evidence_node_count=1,
    )
    result = FilingXbrlFactAdapter().adapt(output)
    _seed_repository_foundation(
        repository_conn,
        output.extraction.extraction_output_sha256,
    )
    repository = SourceFactRepository(repository_conn)
    first = repository.publish(result.publication)
    second = repository.publish(result.publication)

    assert first.observation_ids == result.published_observation_ids
    assert not first.exact_replay
    assert second.exact_replay
    assert repository_conn.execute(
        "SELECT observed_node_count,reported_fact_count "
        "FROM fact_extraction_run_completeness_seals_v2"
    ).fetchone() == (1, 1)
    assert repository_conn.execute(
        "SELECT COUNT(*) FROM v_fact_reported_anchors_selected_v2"
    ).fetchone() == (1,)


def test_same_semantic_cell_reuses_dimension_graph_across_filings(
    repository_conn: sqlite3.Connection,
) -> None:
    later = STAMP + timedelta(days=1)
    dimension = _dimension("ProductAxis")
    first_output = _output((_entry(0, dimensions=(dimension,)),))
    first = FilingXbrlFactAdapter().adapt(first_output)
    second_output = _output(
        (
            _entry(
                0,
                numeric_value=Decimal("125.000"),
                evidence_node_id="node-1",
                dimensions=(dimension,),
                source_taxonomy_version="2027",
                knowledge_at=later,
                recorded_at=later,
            ),
        ),
        document_version_id="document-2",
        extraction_run_id="run-2",
        extraction_input="filing-2-bytes",
        knowledge_at=later,
        recorded_at=later,
    )
    second = FilingXbrlFactAdapter().adapt(second_output)

    first_fact = first.publication.reported_facts[0]
    second_fact = second.publication.reported_facts[0]
    assert first_fact.cell.fact_cell_id == second_fact.cell.fact_cell_id
    assert tuple(item.dimension_id for item in first_fact.cell.dimensions) == tuple(
        item.dimension_id for item in second_fact.cell.dimensions
    )
    assert first_fact.observation.observation_id != second_fact.observation.observation_id
    assert first_fact.observation.numeric_value == "100"
    assert second_fact.observation.numeric_value == "125"
    assert first_fact.observation.source_taxonomy_version == "2026"
    assert second_fact.observation.source_taxonomy_version == "2027"

    _seed_repository_foundation(
        repository_conn,
        first_output.extraction.extraction_output_sha256,
    )
    _seed_second_filing(
        repository_conn,
        later,
        second_output.extraction.extraction_output_sha256,
    )
    ledger = FilingXbrlExtractionLedger(repository_conn)
    ledger.publish(first_output)
    ledger.publish(second_output)

    assert repository_conn.execute("SELECT COUNT(*) FROM fact_cells_v2").fetchone() == (1,)
    assert repository_conn.execute(
        "SELECT COUNT(*) FROM fact_dimensions_normalized_v2"
    ).fetchone() == (1,)
    assert repository_conn.execute("SELECT COUNT(*) FROM fact_observations_v2").fetchone() == (2,)
    assert repository_conn.execute(
        "SELECT COUNT(*) FROM filing_xbrl_extraction_disposition_seals"
    ).fetchone() == (2,)
