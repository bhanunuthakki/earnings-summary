from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from provenance.filing_xbrl_extraction_ledger import (
    FilingXbrlExtractionDispositionRecord,
    FilingXbrlExtractionDispositionSeal,
    FilingXbrlExtractionLedger,
)
from provenance.filing_xbrl_fact_adapter import (
    FilingXbrlAdapterResult,
    FilingXbrlExtractionIdentity,
    FilingXbrlFactAdapter,
    FilingXbrlNormalizedOutput,
    FilingXbrlSubjectIdentity,
    NormalizedFilingXbrlFact,
)
from provenance.source_fact_repository import SourceFactRepository

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
PERIOD_END = STAMP - timedelta(days=30)
BASE_REVISION = "0213_decision_draft_provider_id"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _entry(
    ordinal: int,
    *,
    source_entry_sha256: str | None = None,
    concept_name: str = "Revenue",
    numeric_value: Decimal = Decimal("100"),
    period_kind: str = "duration",
    period_start: datetime | None = PERIOD_END - timedelta(days=365),
) -> NormalizedFilingXbrlFact:
    locator = {"path": f"/xbrl/{ordinal}"}
    locator_json = _canonical(locator)
    return NormalizedFilingXbrlFact.model_validate(
        {
            "ordinal": ordinal,
            "evidence_node_id": f"node-{ordinal}",
            "concept_namespace": "https://fasb.org/us-gaap/2026",
            "concept_name": concept_name,
            "taxonomy_name": "US GAAP",
            "source_taxonomy_version": "2026",
            "accounting_basis": "us_gaap",
            "consolidation_scope": "consolidated",
            "period_kind": period_kind,
            "period_start": period_start,
            "period_end": PERIOD_END,
            "fiscal_year": 2026,
            "fiscal_period": "FY",
            "unit_key": "iso4217:USD",
            "currency": "USD",
            "value_kind": "numeric",
            "numeric_value": numeric_value,
            "raw_lexical_value": str(numeric_value),
            "source_context_id": f"context-{ordinal}",
            "source_unit_id": f"unit-{ordinal}",
            "decimals": "-3",
            "source_locator": locator,
            "source_locator_sha256": _sha(locator_json),
            "source_entry_sha256": (source_entry_sha256 or _sha(f"entry-{ordinal}")),
            "effective_at": PERIOD_END,
            "knowledge_at": STAMP,
            "recorded_at": STAMP,
        }
    )


def _output(
    entries: tuple[NormalizedFilingXbrlFact, ...],
    *,
    extraction_run_id: str = "run-1",
    document_version_id: str = "document-1",
    extractor_config_sha256: str | None = None,
) -> FilingXbrlNormalizedOutput:
    extraction = FilingXbrlExtractionIdentity(
        document_version_id=document_version_id,
        extraction_run_id=extraction_run_id,
        extractor_name="filing-inline-xbrl",
        extractor_code_version="v3",
        extractor_config_sha256=(extractor_config_sha256 or _sha("extractor-config")),
        extraction_input_sha256=_sha("filing-bytes"),
        extraction_output_sha256="0" * 64,
        expected_evidence_node_count=len({entry.evidence_node_id for entry in entries}),
        knowledge_at=STAMP,
        recorded_at=STAMP,
    )
    return FilingXbrlNormalizedOutput.with_computed_digest(
        extraction=extraction,
        subject=FilingXbrlSubjectIdentity(
            reporting_entity_id="reporting-1",
            selected_subject_binding_revision_id="binding-1",
        ),
        entries=entries,
    )


def _insert_extraction_run(
    conn: sqlite3.Connection,
    output: FilingXbrlNormalizedOutput,
) -> None:
    extraction = output.extraction
    conn.execute(
        "INSERT INTO evidence_extraction_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            extraction.extraction_run_id,
            f"{extraction.extraction_run_id}-key",
            extraction.document_version_id,
            extraction.extraction_input_sha256,
            extraction.extractor_name,
            extraction.extractor_config_sha256,
            extraction.extractor_code_version,
            extraction.extraction_output_sha256,
            extraction.knowledge_at,
            extraction.recorded_at,
            "succeeded",
        ),
    )
    entries_by_node = {entry.evidence_node_id: entry for entry in output.entries}
    for sequence, entry in enumerate(entries_by_node.values(), start=1):
        conn.execute(
            "INSERT INTO evidence_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                entry.evidence_node_id,
                f"{extraction.extraction_run_id}-node-key-{sequence}",
                1,
                extraction.extraction_run_id,
                None,
                None,
                "table_cell",
                entry.raw_lexical_value,
                _canonical(entry.source_locator),
                entry.source_locator_sha256,
                entry.recorded_at,
            ),
        )


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _database(
    tmp_path: Path,
    output: FilingXbrlNormalizedOutput,
) -> sqlite3.Connection:
    path = tmp_path / "filing-xbrl-ledger.db"
    conn = sqlite3.connect(path)
    conn.executescript(
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
    conn.commit()
    conn.close()
    config = _config(path)
    command.stamp(config, BASE_REVISION)
    command.upgrade(config, "head")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
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
    blob_sha256 = output.extraction.extraction_input_sha256
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
    _insert_extraction_run(conn, output)
    conn.commit()
    return conn


def test_all_published_and_exact_replay(tmp_path: Path) -> None:
    output = _output((_entry(0), _entry(1, concept_name="Assets")))
    conn = _database(tmp_path, output)
    try:
        ledger = FilingXbrlExtractionLedger(conn)
        first = ledger.publish(output)
        replay = ledger.publish(output)

        assert not first.exact_replay
        assert replay.exact_replay
        assert (first.entry_count, first.published_count) == (2, 2)
        assert conn.execute(
            "SELECT disposition,input_ordinal "
            "FROM filing_xbrl_extraction_dispositions "
            "ORDER BY input_ordinal"
        ).fetchall() == [("published", 0), ("published", 1)]
        assert conn.execute(
            "SELECT COUNT(*) FROM filing_xbrl_extraction_disposition_seals"
        ).fetchone() == (1,)
    finally:
        conn.close()


def test_mixed_publish_and_invalid_graph_quarantine(tmp_path: Path) -> None:
    invalid = _entry(
        1,
        period_kind="instant",
        period_start=PERIOD_END - timedelta(days=1),
    )
    output = _output((_entry(0), invalid))
    conn = _database(tmp_path, output)
    try:
        receipt = FilingXbrlExtractionLedger(conn).publish(output)
        assert (
            receipt.entry_count,
            receipt.published_count,
            receipt.quarantined_count,
        ) == (2, 1, 1)
        assert conn.execute(
            "SELECT quarantine_reason_code "
            "FROM filing_xbrl_extraction_dispositions "
            "WHERE disposition = 'quarantined'"
        ).fetchone() == ("invalid_fact_graph",)
    finally:
        conn.close()


def test_exact_duplicate_links_to_one_auditable_primary(
    tmp_path: Path,
) -> None:
    shared_sha = _sha("shared-entry")
    first = _entry(0, source_entry_sha256=shared_sha)
    second = first.model_copy(update={"ordinal": 1})
    output = _output((first, second))
    conn = _database(tmp_path, output)
    try:
        receipt = FilingXbrlExtractionLedger(conn).publish(output)
        assert (
            receipt.published_count,
            receipt.duplicate_count,
            receipt.quarantined_count,
        ) == (1, 1, 0)
        assert conn.execute(
            "SELECT disposition,input_ordinal,primary_input_ordinal,"
            "observation_id FROM filing_xbrl_extraction_dispositions "
            "ORDER BY input_ordinal"
        ).fetchall() == [
            ("published", 0, None, receipt.publication_receipt.observation_ids[0]),
            ("duplicate", 1, 0, receipt.publication_receipt.observation_ids[0]),
        ]
        assert conn.execute(
            "SELECT COUNT(*) FROM source_fact_publication_members "
            "WHERE publication_id = ? "
            "AND record_kind = 'fact_observation'",
            (receipt.publication_receipt.publication_id,),
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT reported_fact_count "
            "FROM fact_extraction_run_completeness_seals_v2 "
            "WHERE extraction_run_id = ?",
            (output.extraction.extraction_run_id,),
        ).fetchone() == (1,)
        assert FilingXbrlExtractionLedger(conn).publish(output).exact_replay
    finally:
        conn.close()


def test_conflicting_source_identity_quarantines_entire_group(
    tmp_path: Path,
) -> None:
    shared_sha = _sha("shared-entry")
    output = _output(
        (
            _entry(0, source_entry_sha256=shared_sha),
            _entry(
                1,
                source_entry_sha256=shared_sha,
                concept_name="Assets",
            ),
        )
    )
    conn = _database(tmp_path, output)
    try:
        receipt = FilingXbrlExtractionLedger(conn).publish(output)
        assert (
            receipt.published_count,
            receipt.duplicate_count,
            receipt.quarantined_count,
        ) == (0, 0, 2)
        assert conn.execute(
            "SELECT DISTINCT quarantine_reason_code FROM filing_xbrl_extraction_dispositions"
        ).fetchall() == [("conflicting_source_entry_identity",)]
    finally:
        conn.close()


def test_zero_entry_run_is_explicitly_sealed(tmp_path: Path) -> None:
    output = _output(())
    conn = _database(tmp_path, output)
    try:
        receipt = FilingXbrlExtractionLedger(conn).publish(output)
        assert receipt.entry_count == 0
        assert receipt.disposition_set_sha256 == _sha("[]")
        assert conn.execute(
            "SELECT canonical_disposition_set_json,entry_count "
            "FROM filing_xbrl_extraction_disposition_seals"
        ).fetchone() == ("[]", 0)
    finally:
        conn.close()


def test_dangling_observation_and_entry_digest_tamper_are_rejected(
    tmp_path: Path,
) -> None:
    output = _output((_entry(0),))
    conn = _database(tmp_path, output)
    try:
        result = FilingXbrlFactAdapter().adapt(output)
        record = FilingXbrlExtractionLedger.build_disposition_records(
            output,
            result,
        )[0]
        repository = SourceFactRepository(conn)
        columns = tuple(FilingXbrlExtractionDispositionRecord.model_fields)
        statement = (
            "INSERT INTO filing_xbrl_extraction_dispositions ("
            + ",".join(columns)
            + ") VALUES ("
            + ",".join("?" for _ in columns)
            + ")"
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="lacks its exact run anchor",
        ):
            conn.execute(statement, record.database_values)

        repository.publish(result.publication)
        tampered = record.model_copy(update={"canonical_normalized_entry_json": "{}"})
        with pytest.raises(
            sqlite3.IntegrityError,
            match="commitment mismatch",
        ):
            conn.execute(statement, tampered.database_values)
    finally:
        conn.close()


class _BadSealLedger(FilingXbrlExtractionLedger):
    @staticmethod
    def _seal(
        output: FilingXbrlNormalizedOutput,
        result: FilingXbrlAdapterResult,
        records: tuple[FilingXbrlExtractionDispositionRecord, ...],
    ) -> FilingXbrlExtractionDispositionSeal:
        seal = FilingXbrlExtractionLedger._seal(
            output,
            result,
            records,
        )
        return seal.model_copy(update={"entry_count": seal.entry_count + 1})


class _ReorderedDispositionSetLedger(FilingXbrlExtractionLedger):
    @staticmethod
    def _seal(
        output: FilingXbrlNormalizedOutput,
        result: FilingXbrlAdapterResult,
        records: tuple[FilingXbrlExtractionDispositionRecord, ...],
    ) -> FilingXbrlExtractionDispositionSeal:
        seal = FilingXbrlExtractionLedger._seal(
            output,
            result,
            records,
        )
        payload = json.loads(seal.canonical_disposition_set_json)
        reordered_json = _canonical(list(reversed(payload)))
        return seal.model_copy(
            update={
                "canonical_disposition_set_json": reordered_json,
                "disposition_set_sha256": _sha(reordered_json),
            }
        )


class _CrossRunContaminatingAdapter(FilingXbrlFactAdapter):
    def __init__(self, foreign_result: FilingXbrlAdapterResult) -> None:
        self._foreign_result = foreign_result

    def adapt(
        self,
        output: FilingXbrlNormalizedOutput,
    ) -> FilingXbrlAdapterResult:
        result = super().adapt(output)
        contaminated = result.publication.model_copy(
            update={
                "reported_facts": (
                    *result.publication.reported_facts,
                    *self._foreign_result.publication.reported_facts,
                )
            }
        )
        return result.model_copy(update={"publication": contaminated})


class _DirectSealLedger(FilingXbrlExtractionLedger):
    def persist_result_seal(
        self,
        output: FilingXbrlNormalizedOutput,
        result: FilingXbrlAdapterResult,
    ) -> bool:
        records = self.build_disposition_records(output, result)
        for record in records:
            self._persist_disposition(record)
        return self._persist_seal(self._seal(output, result, records))


def test_final_seal_failure_rolls_back_publication_and_facts(
    tmp_path: Path,
) -> None:
    output = _output((_entry(0),))
    conn = _database(tmp_path, output)
    try:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="disposition seal mismatch",
        ):
            _BadSealLedger(conn).publish(output)
        for table in (
            "fact_cells_v2",
            "fact_observations_v2",
            "source_fact_publications",
            "filing_xbrl_extraction_dispositions",
            "filing_xbrl_extraction_disposition_seals",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
    finally:
        conn.close()


def test_reordered_canonical_disposition_set_fails_atomic_seal(
    tmp_path: Path,
) -> None:
    output = _output((_entry(0), _entry(1, concept_name="Assets")))
    conn = _database(tmp_path, output)
    try:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="disposition seal mismatch",
        ):
            _ReorderedDispositionSetLedger(conn).publish(output)
        assert conn.execute("SELECT COUNT(*) FROM source_fact_publications").fetchone() == (0,)
        assert conn.execute(
            "SELECT COUNT(*) FROM filing_xbrl_extraction_disposition_seals"
        ).fetchone() == (0,)
    finally:
        conn.close()


def _foreign_output() -> FilingXbrlNormalizedOutput:
    foreign_entry = _entry(
        0,
        concept_name="Assets",
        source_entry_sha256=_sha("foreign-entry"),
    ).model_copy(
        update={
            "evidence_node_id": "node-foreign",
            "source_context_id": "context-foreign",
        }
    )
    return _output(
        (foreign_entry,),
        extraction_run_id="run-foreign",
        extractor_config_sha256=_sha("foreign-extractor-config"),
    )


def test_cross_run_publication_contamination_is_rejected_by_database_seal(
    tmp_path: Path,
) -> None:
    output = _output((_entry(0),))
    foreign_output = _foreign_output()
    conn = _database(tmp_path, output)
    try:
        _insert_extraction_run(conn, foreign_output)
        adapter = FilingXbrlFactAdapter()
        foreign_result = adapter.adapt(foreign_output)
        SourceFactRepository(conn).publish(foreign_result.publication)

        result = adapter.adapt(output)
        contaminated_publication = result.publication.model_copy(
            update={
                "reported_facts": (
                    *result.publication.reported_facts,
                    *foreign_result.publication.reported_facts,
                )
            }
        )
        SourceFactRepository(conn).publish(contaminated_publication)
        ledger = _DirectSealLedger(conn)

        with pytest.raises(
            sqlite3.IntegrityError,
            match="disposition seal mismatch",
        ):
            ledger.persist_result_seal(output, result)
        assert conn.execute(
            "SELECT COUNT(*) FROM source_fact_publication_members "
            "WHERE publication_id = ? "
            "AND record_kind = 'fact_observation'",
            (contaminated_publication.publication_id,),
        ).fetchone() == (2,)
        assert conn.execute(
            "SELECT COUNT(*) "
            "FROM filing_xbrl_extraction_disposition_seals "
            "WHERE extraction_run_id = ?",
            (output.extraction.extraction_run_id,),
        ).fetchone() == (0,)
    finally:
        conn.close()


def test_typed_publisher_rejects_cross_run_result_before_writing(
    tmp_path: Path,
) -> None:
    output = _output((_entry(0),))
    foreign_output = _foreign_output()
    conn = _database(tmp_path, output)
    try:
        _insert_extraction_run(conn, foreign_output)
        foreign_result = FilingXbrlFactAdapter().adapt(foreign_output)
        SourceFactRepository(conn).publish(foreign_result.publication)

        ledger = FilingXbrlExtractionLedger(
            conn,
            adapter=_CrossRunContaminatingAdapter(foreign_result),
        )
        with pytest.raises(
            ValueError,
            match=("publication reported facts must exactly match published dispositions"),
        ):
            ledger.publish(output)
        assert conn.execute(
            "SELECT COUNT(*) FROM fact_reported_observation_anchors_v2 WHERE extraction_run_id = ?",
            (output.extraction.extraction_run_id,),
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT COUNT(*) FROM filing_xbrl_extraction_dispositions WHERE extraction_run_id = ?",
            (output.extraction.extraction_run_id,),
        ).fetchone() == (0,)
    finally:
        conn.close()


def test_gap_and_post_seal_mutation_fail_closed(tmp_path: Path) -> None:
    gap_output = _output((_entry(0), _entry(2)))
    gap_path = tmp_path / "gap"
    gap_path.mkdir()
    conn = _database(gap_path, gap_output)
    try:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="disposition seal mismatch",
        ):
            FilingXbrlExtractionLedger(conn).publish(gap_output)
        assert conn.execute("SELECT COUNT(*) FROM source_fact_publications").fetchone() == (0,)
    finally:
        conn.close()

    output = _output((_entry(0),))
    sealed_path = tmp_path / "sealed"
    sealed_path.mkdir()
    conn = _database(sealed_path, output)
    try:
        FilingXbrlExtractionLedger(conn).publish(output)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE filing_xbrl_extraction_dispositions SET input_ordinal = 5")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM filing_xbrl_extraction_disposition_seals")
        row = conn.execute("SELECT * FROM filing_xbrl_extraction_dispositions").fetchone()
        assert row is not None
        columns = tuple(
            item[1]
            for item in conn.execute("PRAGMA table_info(filing_xbrl_extraction_dispositions)")
        )
        values = dict(zip(columns, tuple(row), strict=True))
        values["disposition_id"] = "post-seal"
        values["idempotency_key"] = "post-seal"
        with pytest.raises(sqlite3.IntegrityError, match="already sealed"):
            conn.execute(
                "INSERT INTO filing_xbrl_extraction_dispositions ("
                + ",".join(columns)
                + ") VALUES ("
                + ",".join("?" for _ in columns)
                + ")",
                tuple(values[column] for column in columns),
            )
    finally:
        conn.close()
