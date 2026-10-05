"""Selected retained runs use current migrated storage and shared acceptance owners."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import TypeAlias, cast

import pytest

from filings.inline_xbrl_processor import (
    InlineXbrlProcessorResult,
    ProcessorBundleManifest,
    ProcessorPackageMember,
    package_member_set_sha256,
)
from provenance.evidence_ledger import EvidenceLedger
from provenance.filing_xbrl_extraction_ledger import (
    FilingXbrlExtractionDispositionRecord,
    FilingXbrlExtractionLedger,
)
from provenance.filing_xbrl_fact_adapter import (
    FilingXbrlNormalizationRejection,
    FilingXbrlNormalizedOutput,
)
from provenance.integrity_audit import (
    AuditOptions,
    FilingXbrlRunVerificationLimits,
    audit_connection,
    verify_filing_xbrl_run,
)
from provenance.issuer_registry import (
    IdentifierAssertion,
    IdentifierResolution,
    IssuerRegistry,
    identifier_candidate_digest,
)
from provenance.sec_filing_xbrl_ingest import (
    filing_xbrl_evidence_node_id,
    persist_filing_xbrl_evidence_nodes,
    persist_filing_xbrl_input_closure,
    persist_filing_xbrl_processor_artifact,
    persist_filing_xbrl_raw_commitments,
)
from provenance.source_fact_stream import register_source_fact_stream_functions
from tests.test_filing_xbrl_extraction_ledger import (
    STAMP,
    filing_xbrl_entry,
    filing_xbrl_ledger_database,
    filing_xbrl_output,
    insert_filing_xbrl_extraction_run,
)
from tests.test_inline_xbrl_processor import measured_processor_output_fixture

ROOT = Path(__file__).resolve().parents[1]
RunFixture: TypeAlias = tuple[
    FilingXbrlNormalizedOutput,
    ProcessorBundleManifest,
    InlineXbrlProcessorResult,
    tuple[ProcessorPackageMember, ...],
]


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _fixture_output(
    tmp_path: Path,
    run_id: str,
    *,
    legacy: bool = False,
    rejected: bool = False,
) -> RunFixture:
    """Reuse processor source fixtures; this does not approve a runtime artifact."""
    body = measured_processor_output_fixture(tmp_path)
    manifest_body = json.loads((ROOT / "config/filing_xbrl_processor_bundle.json").read_bytes())
    execution = manifest_body["execution"]
    if not legacy:
        manifest_body["bridge_protocol_version"] = "filing-xbrl-bridge.v2"
        unit_bytes = (ROOT / "src/filings/xbrl_units.py").read_bytes()
        unit_sha = hashlib.sha256(unit_bytes).hexdigest()
        manifest_body["build_provenance"]["unit_source_sha256"] = unit_sha
        execution["runtime_members"].append(
            {
                "relative_path": "earnings_summary_xbrl_units.py",
                "blob_sha256": unit_sha,
                "byte_size": len(unit_bytes),
            }
        )
    execution["runtime_artifact_sha256"] = _sha(execution["runtime_members"])
    manifest = ProcessorBundleManifest.model_validate(manifest_body)
    entry = filing_xbrl_entry(0)
    base = filing_xbrl_output((entry,), extraction_run_id=run_id)
    member = ProcessorPackageMember(
        member_ordinal=0,
        member_role="primary_document",
        document_version_id="document-1",
        source_url="https://www.sec.gov/Archives/example/filing.xhtml",
        local_path=tmp_path / "filing.xhtml",
        blob_sha256=base.extraction.extraction_input_sha256,
        byte_size=len("filing-bytes"),
        media_type="application/xhtml+xml",
    )
    facts = cast(list[dict[str, object]], body["facts"])
    fact = facts[0]
    raw = cast(dict[str, object], fact["canonical_raw_fact"])
    if legacy:
        raw.pop("unit_measures")
    else:
        cast(dict[str, object], raw["unit_measures"])["denominator"] = []
    fact["package_member_blob_sha256"] = member.blob_sha256
    locator = cast(dict[str, object], fact["source_locator"])
    locator["source_ref"] = locator["xbrl_package_member"] = member.source_url
    fact["source_locator_sha256"] = _sha(locator)
    fact["raw_fact_sha256"] = _sha(raw)
    fact["source_entry_sha256"] = _sha(
        {
            key: fact[key]
            for key in (
                "accession_number",
                "observed_cik",
                "package_member_blob_sha256",
                "package_member_ordinal",
                "raw_fact_sha256",
                "source_locator_sha256",
            )
        }
    )
    note = {"text": "retained fixture footnote"}
    fact["footnotes"] = [
        {"footnote_ordinal": 0, "canonical_footnote": note, "footnote_sha256": _sha(note)}
    ]
    body["footnote_count"] = 1
    body["footnote_set_sha256"] = _sha(
        [
            {
                "canonical_footnote": note,
                "footnote_ordinal": 0,
                "footnote_sha256": _sha(note),
                "input_ordinal": 0,
            }
        ]
    )
    normalized = cast(dict[str, object], fact["normalized_fact"])
    normalized["unit_key"] = "USD"
    body["bridge_protocol_version"] = manifest.bridge_protocol_version
    body["raw_fact_set_sha256"] = _sha(
        [
            {
                key: fact[key]
                for key in (
                    "input_ordinal",
                    "raw_fact_sha256",
                    "source_entry_sha256",
                    "source_locator_sha256",
                )
            }
        ]
    )
    body["runtime_artifact_sha256"] = execution["runtime_artifact_sha256"]
    body["package_member_set_sha256"] = package_member_set_sha256((member,))
    evidence = cast(dict[str, object], body["execution_evidence"])
    evidence["runtime_artifact_sha256"] = body["runtime_artifact_sha256"]
    evidence["package_member_set_sha256"] = body["package_member_set_sha256"]
    if rejected:
        fact.update(
            normalization_outcome="rejected",
            normalized_fact=None,
            rejection_reason_code="fixture_rejection",
            rejection_detail="offline fixture",
        )
    processor = InlineXbrlProcessorResult.model_validate(body)
    entry = entry.model_copy(
        update={
            "evidence_node_id": filing_xbrl_evidence_node_id(run_id, 0),
            "unit_key": "USD",
            "source_unit_id": "u17",
            "numeric_value": Decimal("10"),
            "raw_lexical_value": "10",
            "source_locator": processor.facts[0].source_locator,
            "source_locator_sha256": processor.facts[0].source_locator_sha256,
            "source_entry_sha256": processor.facts[0].source_entry_sha256,
        }
    )
    rejection = FilingXbrlNormalizationRejection(
        ordinal=0,
        evidence_node_id=entry.evidence_node_id,
        canonical_raw_fact_json=_canonical(processor.facts[0].canonical_raw_fact),
        raw_fact_sha256=processor.facts[0].raw_fact_sha256,
        source_entry_sha256=processor.facts[0].source_entry_sha256,
        source_locator_sha256=processor.facts[0].source_locator_sha256,
        reason_code="fixture_rejection",
        detail="offline fixture",
        knowledge_at=STAMP,
        recorded_at=STAMP,
    )
    output = FilingXbrlNormalizedOutput.with_computed_digest(
        extraction=base.extraction.model_copy(
            update={
                "extractor_name": "filing-native-xbrl",
                "extractor_code_version": f"fixture-{run_id}",
            }
        ),
        subject=base.subject,
        entries=() if rejected else (entry,),
        rejections=(rejection,) if rejected else (),
    )
    return output, manifest, processor, (member,)


def _publish(conn: sqlite3.Connection, fixture: RunFixture) -> None:
    register_source_fact_stream_functions(conn)
    output, manifest, processor, members = fixture
    if not conn.execute(
        "SELECT 1 FROM evidence_nodes WHERE extraction_run_id=? LIMIT 1",
        (output.extraction.extraction_run_id,),
    ).fetchone():
        persist_filing_xbrl_evidence_nodes(
            EvidenceLedger(conn), output.extraction.extraction_run_id, processor, STAMP
        )
    artifact = persist_filing_xbrl_processor_artifact(conn, manifest, processor, STAMP)
    persist_filing_xbrl_input_closure(
        conn,
        run_id=output.extraction.extraction_run_id,
        processor_artifact_id=artifact,
        members=members,
        processor=processor,
        accession_number="0000000001-26-000001",
        expected_cik="0000000001",
        issuer_id="issuer-1",
        recorded_at=STAMP,
    )
    persist_filing_xbrl_raw_commitments(
        conn,
        run_id=output.extraction.extraction_run_id,
        facts=processor.facts,
        recorded_at=STAMP,
    )
    FilingXbrlExtractionLedger(conn).publish(output)


def _bind_cik(conn: sqlite3.Connection) -> None:
    assertion = IdentifierAssertion(
        assertion_id="cik-1",
        idempotency_key="cik-1",
        issuer_id="issuer-1",
        identifier_type="sec_cik",
        identifier_value="0000000001",
        normalized_value="0000000001",
        authority="manual",
        effective_at=STAMP,
        knowledge_at=STAMP,
        recorded_at=STAMP,
    )
    registry = IssuerRegistry(conn)
    registry.persist(assertion)
    registry.persist(
        IdentifierResolution(
            resolution_id="cik-resolution",
            idempotency_key="cik-resolution",
            resolution_key=assertion.resolution_key,
            revision=1,
            outcome="selected",
            selected_assertion_id=assertion.assertion_id,
            candidate_digest_sha256=identifier_candidate_digest((assertion,)),
            policy_name="fixture",
            policy_version="1",
            policy_config_sha256="a" * 64,
            material_dissent=False,
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
            reason_code="fixture",
            reason_details=(("source", "synthetic"),),
        )
    )


@pytest.fixture
def retained_run(
    tmp_path: Path, migrated_db: Callable[[Path], Path]
) -> Iterator[sqlite3.Connection]:
    fixture = _fixture_output(tmp_path, "run-1")
    conn = filing_xbrl_ledger_database(tmp_path, fixture[0], migrated_db)
    _bind_cik(conn)
    _publish(conn, fixture)
    conn.commit()
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _allow_corruption(conn: sqlite3.Connection, table: str) -> None:
    """Fault injection only: remove immutable guards from the disposable fixture."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)
    )
    for (name,) in list(rows):
        conn.execute('DROP TRIGGER "' + str(name).replace('"', '""') + '"')


def test_full_run_is_read_only_and_reconstructs_v2_units(retained_run: sqlite3.Connection) -> None:
    before = retained_run.total_changes
    retained_run.execute("PRAGMA query_only=ON")
    receipt = verify_filing_xbrl_run(retained_run, extraction_run_id="run-1")
    assert (receipt.raw_fact_count, receipt.disposition_count, receipt.normalized_fact_count) == (
        1,
        1,
        1,
    )
    assert receipt.unit_semantics == "verified_v2"
    assert receipt.footnote_count == 1
    assert retained_run.total_changes == before


def test_unrelated_corrupt_run_is_excluded_but_global_audit_retains_detection(
    retained_run: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    fixture = _fixture_output(tmp_path, "run-2")
    insert_filing_xbrl_extraction_run(retained_run, fixture[0])
    _publish(retained_run, fixture)
    _allow_corruption(retained_run, "filing_xbrl_extraction_input_seals")
    retained_run.execute(
        "UPDATE filing_xbrl_extraction_input_seals SET member_set_sha256=? WHERE extraction_run_id='run-2'",
        ("f" * 64,),
    )
    assert (
        verify_filing_xbrl_run(retained_run, extraction_run_id="run-1").unit_semantics
        == "verified_v2"
    )
    with pytest.raises(ValueError, match=r"integrity|unqualified"):
        verify_filing_xbrl_run(retained_run, extraction_run_id="run-2")
    codes = {
        item.code
        for item in audit_connection(retained_run, AuditOptions(deep_sqlite_checks=False)).findings
    }
    assert "FILING_XBRL_RESULT_COMMITMENT_DIGEST_MISMATCH" in codes


@pytest.mark.parametrize(
    "table",
    [
        "filing_xbrl_extraction_input_seals",
        "filing_xbrl_processor_artifacts",
        "filing_xbrl_extraction_disposition_seals",
        "evidence_extraction_runs",
        "filing_xbrl_raw_fact_commitments",
        "filing_xbrl_extraction_dispositions",
    ],
)
def test_missing_retained_population_fails_closed(
    retained_run: sqlite3.Connection, table: str
) -> None:
    # Fault injection is restricted to this migrated disposable fixture.
    retained_run.rollback()
    retained_run.execute("PRAGMA foreign_keys=OFF")
    _allow_corruption(retained_run, table)
    retained_run.execute(f"DELETE FROM {table}")  # nosec B608 -- closed parameterized test table set
    with pytest.raises(ValueError, match=r"missing|incomplete|unqualified"):
        verify_filing_xbrl_run(retained_run, extraction_run_id="run-1")


class _RunIdSubclass(str):
    pass


@pytest.mark.parametrize("run_id", ["", "x" * 129, _RunIdSubclass("run-1")])
def test_exact_builtin_run_id_boundary(retained_run: sqlite3.Connection, run_id: str) -> None:
    with pytest.raises(ValueError, match="exact run ID"):
        verify_filing_xbrl_run(retained_run, extraction_run_id=run_id)


def test_absent_schema_run_and_read_transaction_fail_closed(
    retained_run: sqlite3.Connection,
) -> None:
    with pytest.raises(ValueError, match="missing"):
        verify_filing_xbrl_run(retained_run, extraction_run_id="absent")
    retained_run.rollback()
    with pytest.raises(ValueError, match="read transaction"):
        verify_filing_xbrl_run(retained_run, extraction_run_id="run-1")
    retained_run.execute("BEGIN")
    retained_run.execute("DROP TABLE filing_xbrl_footnote_commitments")
    with pytest.raises(ValueError, match="schema is missing"):
        verify_filing_xbrl_run(retained_run, extraction_run_id="run-1")


@pytest.mark.parametrize(
    "limit",
    [
        {"raw_facts": 0},
        {"total_rows": 1},
        {"individual_row_bytes": 1},
        {"total_text_bytes": 1},
    ],
)
def test_population_overflow_refuses_complete_replay(
    retained_run: sqlite3.Connection, limit: dict[str, int]
) -> None:
    with pytest.raises(ValueError, match="limit exceeded"):
        verify_filing_xbrl_run(
            retained_run, extraction_run_id="run-1", limits=FilingXbrlRunVerificationLimits(**limit)
        )


@pytest.mark.parametrize(
    "field,value", [("unit_key", "EUR"), ("currency", "EUR"), ("source_unit_id", "wrong")]
)
def test_sealed_normalized_unit_change_fails_owning_rule(
    retained_run: sqlite3.Connection,
    field: str,
    value: str,
) -> None:
    # Reseal the synthetic normalized entry with the existing disposition owner:
    # unit semantics must fail even when all commitment hashes are self-consistent.
    _allow_corruption(retained_run, "filing_xbrl_extraction_dispositions")
    _allow_corruption(retained_run, "filing_xbrl_extraction_disposition_seals")
    retained_run.row_factory = sqlite3.Row
    row = retained_run.execute("SELECT * FROM filing_xbrl_extraction_dispositions").fetchone()
    assert row is not None
    record = dict(row)
    normalized = json.loads(record["canonical_normalized_entry_json"])
    normalized[field] = value
    record["canonical_normalized_entry_json"] = _canonical(normalized)
    record["normalized_entry_sha256"] = hashlib.sha256(
        record["canonical_normalized_entry_json"].encode()
    ).hexdigest()
    normalized.pop("ordinal")
    record["normalized_entry_identity_sha256"] = _sha(normalized)
    disposition = json.loads(record["canonical_disposition_json"])
    disposition["normalized_entry_sha256"] = record["normalized_entry_sha256"]
    disposition["normalized_entry_identity_sha256"] = record["normalized_entry_identity_sha256"]
    record["canonical_disposition_json"] = _canonical(disposition)
    record["disposition_sha256"] = hashlib.sha256(
        record["canonical_disposition_json"].encode()
    ).hexdigest()
    updated = FilingXbrlExtractionDispositionRecord.model_validate(record)
    retained_run.execute(
        "UPDATE filing_xbrl_extraction_dispositions SET canonical_normalized_entry_json=?,normalized_entry_sha256=?,normalized_entry_identity_sha256=?,canonical_disposition_json=?,disposition_sha256=?",
        (
            updated.canonical_normalized_entry_json,
            updated.normalized_entry_sha256,
            updated.normalized_entry_identity_sha256,
            updated.canonical_disposition_json,
            updated.disposition_sha256,
        ),
    )
    canonical_set = _canonical([disposition])
    retained_run.execute(
        "UPDATE filing_xbrl_extraction_disposition_seals SET canonical_disposition_set_json=?,disposition_set_sha256=?",
        (canonical_set, hashlib.sha256(canonical_set.encode()).hexdigest()),
    )
    with pytest.raises(ValueError, match="unit"):
        verify_filing_xbrl_run(retained_run, extraction_run_id="run-1")


@pytest.mark.parametrize("legacy,rejected", [(True, False), (False, True)])
def test_legacy_is_not_unit_proof_and_quarantine_is_in_complete_population(
    tmp_path: Path,
    migrated_db: Callable[[Path], Path],
    legacy: bool,
    rejected: bool,
) -> None:
    fixture = _fixture_output(tmp_path, "run-1", legacy=legacy, rejected=rejected)
    conn = filing_xbrl_ledger_database(tmp_path, fixture[0], migrated_db)
    try:
        _bind_cik(conn)
        _publish(conn, fixture)
        conn.commit()
        conn.execute("BEGIN")
        receipt = verify_filing_xbrl_run(conn, extraction_run_id="run-1")
        assert receipt.raw_fact_count == receipt.disposition_count == 1
        assert receipt.normalized_fact_count == (0 if rejected else 1)
        assert receipt.unit_semantics == ("not_proven_legacy_v1" if legacy else "verified_v2")
        if rejected:
            assert conn.execute(
                "SELECT disposition FROM filing_xbrl_extraction_dispositions"
            ).fetchone() == ("quarantined",)
    finally:
        conn.rollback()
        conn.close()


@pytest.mark.parametrize(
    "table,assignment",
    [
        ("filing_xbrl_raw_fact_commitments", "canonical_raw_fact_json='{}'"),
        ("filing_xbrl_extraction_input_members", "canonical_member_json='{}'"),
        ("filing_xbrl_footnote_commitments", "canonical_footnote_json='{}'"),
        ("evidence_nodes", "recorded_at='2026-07-28T12:00:00+00:00'"),
    ],
)
def test_selected_digest_and_clock_corruption_fail_closed(
    retained_run: sqlite3.Connection,
    table: str,
    assignment: str,
) -> None:
    _allow_corruption(retained_run, table)
    retained_run.execute(f"UPDATE {table} SET {assignment}")  # nosec B608 -- closed test fault set
    with pytest.raises(ValueError, match=r"integrity|atomic"):
        verify_filing_xbrl_run(retained_run, extraction_run_id="run-1")
