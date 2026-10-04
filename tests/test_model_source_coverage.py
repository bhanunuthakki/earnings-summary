"""Latest-period proof must include foreign filings without guessing 6-K meaning."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import cast

import pytest

from dcf import input_evidence as evidence
from provenance.research_snapshot import ResearchSnapshotRequest

NOW = datetime(2026, 10, 1, 20, tzinfo=UTC)


def coverage_db(monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE research_snapshot_headers (research_snapshot_id TEXT,request_json TEXT);
        CREATE TABLE search_manifest_source_inventories (manifest_id TEXT,snapshot_id TEXT);
        CREATE TABLE source_inventory_snapshots (
            snapshot_id TEXT,inventory_key TEXT,revision INTEGER,issuer_id TEXT,ticker TEXT,
            source_kind TEXT,outcome TEXT,authoritative INTEGER,completed_at TEXT,recorded_at TEXT);
        CREATE TABLE expected_documents (
            expected_document_id TEXT,snapshot_id TEXT,issuer_id TEXT,ticker TEXT,
            source_kind TEXT,document_type TEXT,form_type TEXT,accession_number TEXT,
            period_end TEXT,expectation_basis TEXT,recorded_at TEXT,source_url TEXT,filing_at TEXT);
        CREATE TABLE source_coverage_assessments (
            expected_document_id TEXT,revision INTEGER,coverage_status TEXT,
            document_version_id TEXT,reason_details_json TEXT,knowledge_at TEXT,recorded_at TEXT);
        CREATE TABLE evidence_document_versions (
            document_version_id TEXT,observation_id TEXT,blob_sha256 TEXT,document_type TEXT,
            period_end TEXT,accession_number TEXT,form_type TEXT,issuer_id TEXT,recorded_at TEXT);
        CREATE TABLE evidence_source_observations (
            observation_id TEXT,source_url TEXT,blob_sha256 TEXT,source_kind TEXT,
            collector_code_version TEXT,retrieved_at TEXT);
        CREATE TABLE source_inventory_components (
            snapshot_id TEXT,component_key TEXT,source_observation_id TEXT,
            required INTEGER,outcome TEXT,recorded_at TEXT);
        CREATE TABLE expected_document_obligation_bindings (
            expected_document_id TEXT,source_obligation_revision_id TEXT,issuer_id TEXT,
            reporting_entity_id TEXT,document_family TEXT,canonical_binding_json TEXT,
            binding_sha256 TEXT,knowledge_at TEXT,recorded_at TEXT);
    """)
    snapshot = ResearchSnapshotRequest.model_validate(
        {
            "research_snapshot_id": "snapshot",
            "idempotency_key": "snapshot",
            "research_universe": {
                "issuer_id": "issuer-1",
                "reporting_entity_ids": ["reporting-1"],
                "document_version_ids": ["document-1"],
                "source_obligation_revision_ids": ["obligation"],
            },
            "processing_snapshot_ids": ["processing"],
            "corpus_bundles": [
                {"corpus_manifest_id": "manifest", "lexical_index_run_id": "lexical"}
            ],
            "source_fact_publication_ids": ["publication"],
            "ontology_snapshot_id": "ontology",
            "canonical_fact_resolution_snapshot_id": "resolution-snapshot",
            "canonical_fact_projection_run_id": "projection",
            "cutoff_at": NOW,
            "recorded_at": NOW,
        }
    )
    conn.execute(
        "INSERT INTO research_snapshot_headers VALUES ('snapshot',?)", (snapshot.model_dump_json(),)
    )
    conn.execute("INSERT INTO search_manifest_source_inventories VALUES ('manifest','inventory')")
    conn.execute(
        "INSERT INTO source_inventory_snapshots VALUES ('inventory','sec',1,'issuer-1','ONON',"
        "'sec_submissions','succeeded',1,?,?)",
        (NOW.isoformat(), NOW.isoformat()),
    )

    def verify_snapshot(*args: object) -> SimpleNamespace:
        return SimpleNamespace(member_set_sha256="b" * 64)

    monkeypatch.setattr(evidence, "verify_research_snapshot", verify_snapshot)
    return conn


def add_document(
    conn: sqlite3.Connection,
    identity: str,
    form: str,
    period: str | None,
    *,
    kind: str = "filing",
    family: str = "operating_company_periodic",
    source_kind: str = "ir_document",
    filing_at: str | None = NOW.isoformat(),
) -> None:
    conn.execute(
        "INSERT INTO expected_documents VALUES (?,'inventory','issuer-1','ONON',"
        "?,?,?,?,?,'authoritative',?,?,?)",
        (
            identity,
            source_kind,
            kind,
            form,
            identity,
            period,
            NOW.isoformat(),
            "https://www.sec.gov/example",
            filing_at,
        ),
    )
    payload = {
        "document_family": family,
        "expected_document_id": identity,
        "issuer_id": "issuer-1",
        "reporting_entity_id": "reporting-1",
        "source_obligation_revision_id": "obligation",
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    conn.execute(
        "INSERT INTO expected_document_obligation_bindings VALUES (?, 'obligation','issuer-1',"
        "'reporting-1',?,?,?, ?,?)",
        (
            identity,
            family,
            encoded,
            hashlib.sha256(encoded.encode()).hexdigest(),
            NOW.isoformat(),
            NOW.isoformat(),
        ),
    )


def request(period: str) -> evidence.ModelInputRequest:
    return evidence.ModelInputRequest(
        recipe="onon-cash-rent-sbc-inputs/v1",
        ticker="ONON",
        research_snapshot_id="snapshot",
        financial_period_end=date.fromisoformat(period),
        facts={},
        assumptions={},
    )


@pytest.mark.parametrize(
    "form", ["10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A"]
)
def test_foreign_and_domestic_periodic_forms_name_the_financial_anchor(
    monkeypatch: pytest.MonkeyPatch, form: str
) -> None:
    conn = coverage_db(monkeypatch)
    add_document(conn, "annual", form, "2025-12-31")
    assert evidence.verify_source_coverage(conn, request("2025-12-31"), NOW)[2] == ("inventory",)


def test_financial_6k_and_nonfinancial_6k_have_distinct_governed_meaning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = coverage_db(monkeypatch)
    add_document(conn, "annual", "20-F", "2025-12-31")
    add_document(
        conn,
        "earnings",
        "6-K",
        "2026-06-30",
        kind="financial_statement",
        family="issuer_financial_statements",
    )
    add_document(
        conn,
        "investor-day",
        "6-K",
        "2026-09-22",
        kind="investor_presentation",
        family="issuer_presentations",
    )
    assert evidence.verify_source_coverage(conn, request("2026-06-30"), NOW)[2] == ("inventory",)


def test_native_financial_sec_exhibit_qualifies_under_its_sec_source_duty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = coverage_db(monkeypatch)
    add_document(
        conn,
        "earnings",
        "6-K",
        "2026-06-30",
        kind="financial_statement",
        family="continuous_disclosure",
        source_kind="sec_filing",
    )
    add_native_capture(conn)
    assert evidence.verify_source_coverage(conn, request("2026-06-30"), NOW)[2] == ("inventory",)


def add_native_capture(conn: sqlite3.Connection, *, imported: bool = True) -> None:
    fields = {
        "subject_review_sha256": "c" * 64,
        "subject_source_sha256": "d" * 64,
        "subject_reviewer": "analyst",
        "subject_reviewed_at": NOW.isoformat(),
        "subject_heading_selector": "h2",
        "subject_heading_text": "Condensed consolidated statements as of June 30, 2026",
        "subject_period_end_text": "June 30, 2026",
        "subject_rationale": "Reported financial statements identify the interim period.",
    }
    details = {
        ("imported_detail_" if imported else "") + key: value for key, value in fields.items()
    }
    conn.execute(
        "INSERT INTO source_coverage_assessments VALUES ('earnings',1,'extracted','document-1',?,?,?)",
        (json.dumps(details), NOW.isoformat(), NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO evidence_document_versions VALUES ('document-1','observation',?,'financial_statement',"
        "'2026-06-30','earnings','6-K','issuer-1',?)",
        ("d" * 64, NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO evidence_source_observations VALUES ('observation','https://www.sec.gov/example',?,'sec_filing','capture/v1',?)",
        ("d" * 64, NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO evidence_source_observations VALUES ('review','https://www.sec.gov/review',?,'sec_package_subject_review','sec_package_subject_review.v1',?)",
        ("c" * 64, NOW.isoformat()),
    )
    conn.execute(
        "INSERT INTO source_inventory_components VALUES ('inventory','reviewed-package-subjects','review',1,'succeeded',?)",
        (NOW.isoformat(),),
    )


@pytest.mark.parametrize(
    "damage",
    [
        "unclassified",
        "unbound",
        "wrong-family",
        "wrong-issuer",
        "wrong-entity",
        "outside-snapshot",
        "tampered-hash",
        "future-binding",
        "candidate-only",
    ],
)
def test_6k_qualification_requires_exact_current_governed_authority(
    monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    conn = coverage_db(monkeypatch)
    add_document(
        conn,
        "earnings",
        "6-K",
        "2026-06-30",
        kind="financial_statement",
        family="issuer_financial_statements",
    )
    if damage == "unclassified":
        conn.execute("UPDATE expected_documents SET document_type='filing'")
    elif damage == "unbound":
        conn.execute("DELETE FROM expected_document_obligation_bindings")
    elif damage == "wrong-family":
        conn.execute(
            "UPDATE expected_document_obligation_bindings SET document_family='continuous_disclosure'"
        )
    elif damage == "wrong-issuer":
        conn.execute("UPDATE expected_document_obligation_bindings SET issuer_id='other'")
    elif damage == "wrong-entity":
        conn.execute("UPDATE expected_document_obligation_bindings SET reporting_entity_id='other'")
    elif damage == "outside-snapshot":
        conn.execute(
            "UPDATE expected_document_obligation_bindings SET source_obligation_revision_id='other'"
        )
    elif damage == "tampered-hash":
        conn.execute(
            "UPDATE expected_document_obligation_bindings SET binding_sha256=?", ("a" * 64,)
        )
    elif damage == "future-binding":
        conn.execute("UPDATE expected_document_obligation_bindings SET recorded_at='2026-10-02'")
    else:
        conn.execute("UPDATE expected_documents SET expectation_basis='publisher_candidate'")
    with pytest.raises(evidence.InputEvidenceError, match="financial_reporting"):
        evidence.verify_source_coverage(conn, request("2026-06-30"), NOW)


def test_unknown_6k_cannot_silently_certify_an_older_annual_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = coverage_db(monkeypatch)
    add_document(conn, "annual", "20-F", "2025-12-31")
    add_document(conn, "unclassified", "6-K", "2026-06-30", family="continuous_disclosure")
    with pytest.raises(evidence.InputEvidenceError, match="financial_reporting"):
        evidence.verify_source_coverage(conn, request("2025-12-31"), NOW)


@pytest.mark.parametrize("damage", ["partial", "stale", "superseded"])
def test_foreign_source_coverage_preserves_inventory_guards(
    monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    conn = coverage_db(monkeypatch)
    add_document(conn, "annual", "20-F", "2025-12-31")
    if damage == "partial":
        conn.execute("UPDATE source_inventory_snapshots SET outcome='partial'")
    elif damage == "stale":
        conn.execute("UPDATE source_inventory_snapshots SET completed_at='2026-09-28'")
    else:
        conn.execute(
            "INSERT INTO source_inventory_snapshots SELECT 'new',inventory_key,2,issuer_id,ticker,source_kind,outcome,authoritative,completed_at,recorded_at FROM source_inventory_snapshots"
        )
    with pytest.raises(evidence.InputEvidenceError, match="source_inventory"):
        evidence.verify_source_coverage(conn, request("2025-12-31"), NOW)


@pytest.mark.parametrize(
    "damage",
    [
        "missing-review",
        "changed-capture",
        "changed-observation",
        "wrong-period",
        "wrong-url",
        "review-after-capture",
        "not-captured",
        "conflicting-details",
    ],
)
def test_native_sec_classification_requires_exact_reviewed_capture(
    monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    conn = coverage_db(monkeypatch)
    add_document(
        conn,
        "earnings",
        "6-K",
        "2026-06-30",
        kind="financial_statement",
        family="continuous_disclosure",
        source_kind="sec_filing",
    )
    add_native_capture(conn)
    if damage == "missing-review":
        conn.execute("UPDATE source_coverage_assessments SET reason_details_json='{}'")
    elif damage == "changed-capture":
        conn.execute("UPDATE evidence_document_versions SET blob_sha256=?", ("e" * 64,))
    elif damage == "changed-observation":
        conn.execute("UPDATE evidence_source_observations SET blob_sha256=?", ("e" * 64,))
    elif damage == "wrong-period":
        conn.execute("UPDATE evidence_document_versions SET period_end='2026-03-31'")
    elif damage == "wrong-url":
        conn.execute(
            "UPDATE evidence_source_observations SET source_url='https://www.sec.gov/other'"
        )
    elif damage == "not-captured":
        conn.execute("UPDATE source_coverage_assessments SET coverage_status='expected'")
    else:
        details = json.loads(
            conn.execute("SELECT reason_details_json FROM source_coverage_assessments").fetchone()[
                0
            ]
        )
        if damage == "review-after-capture":
            details["imported_detail_subject_reviewed_at"] = "2026-10-02T20:00:00+00:00"
        else:
            details["subject_source_sha256"] = "e" * 64
        conn.execute(
            "UPDATE source_coverage_assessments SET reason_details_json=?", (json.dumps(details),)
        )
    with pytest.raises(evidence.InputEvidenceError, match="financial_reporting_subject"):
        evidence.verify_source_coverage(conn, request("2026-06-30"), NOW)


def test_sec_cover_uses_reviewed_same_accession_financial_companion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = coverage_db(monkeypatch)
    add_document(
        conn, "cover", "6-K", None, family="continuous_disclosure", source_kind="sec_filing"
    )
    conn.execute(
        "UPDATE expected_documents SET accession_number='earnings' WHERE expected_document_id='cover'"
    )
    add_document(
        conn,
        "earnings",
        "6-K",
        "2026-06-30",
        kind="financial_statement",
        family="continuous_disclosure",
        source_kind="sec_filing",
    )
    add_native_capture(conn)
    assert evidence.verify_source_coverage(conn, request("2026-06-30"), NOW)[2] == ("inventory",)


@pytest.mark.parametrize("kind", ["investor_presentation", "investor_update"])
def test_reviewed_native_presentation_does_not_advance_financial_anchor(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    conn = coverage_db(monkeypatch)
    add_document(conn, "annual", "20-F", "2025-12-31")
    add_document(
        conn,
        "earnings",
        "6-K",
        None,
        kind=kind,
        family="continuous_disclosure",
        source_kind="sec_filing",
    )
    add_native_capture(conn, imported=False)
    conn.execute("UPDATE evidence_document_versions SET document_type=?,period_end=NULL", (kind,))
    assert evidence.verify_source_coverage(conn, request("2025-12-31"), NOW)[2] == ("inventory",)


def test_6k_companion_is_classified_across_complete_inventory_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = coverage_db(monkeypatch)
    add_document(
        conn, "cover", "6-K", None, family="continuous_disclosure", source_kind="sec_filing"
    )
    conn.execute(
        "UPDATE expected_documents SET accession_number='earnings' WHERE expected_document_id='cover'"
    )
    add_document(
        conn,
        "earnings",
        "6-K",
        "2026-06-30",
        kind="financial_statement",
        family="issuer_financial_statements",
    )
    conn.execute(
        "INSERT INTO search_manifest_source_inventories VALUES ('manifest','ir-inventory')"
    )
    conn.execute(
        "INSERT INTO source_inventory_snapshots SELECT 'ir-inventory','ir',1,issuer_id,ticker,'ir_archive',outcome,authoritative,completed_at,recorded_at FROM source_inventory_snapshots WHERE snapshot_id='inventory'"
    )
    conn.execute(
        "UPDATE expected_documents SET snapshot_id='ir-inventory' WHERE expected_document_id='earnings'"
    )
    assert evidence.verify_source_coverage(conn, request("2026-06-30"), NOW)[2] == (
        "inventory",
        "ir-inventory",
    )


@pytest.mark.parametrize(
    "damage", ["missing", "hash", "optional", "wrong-kind", "wrong-collector", "future"]
)
def test_review_requires_immutable_required_inventory_component(
    monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    conn = coverage_db(monkeypatch)
    add_document(
        conn,
        "earnings",
        "6-K",
        "2026-06-30",
        kind="financial_statement",
        family="continuous_disclosure",
        source_kind="sec_filing",
    )
    add_native_capture(conn)
    if damage == "missing":
        conn.execute("DELETE FROM source_inventory_components")
    elif damage == "optional":
        conn.execute("UPDATE source_inventory_components SET required=0")
    elif damage == "hash":
        conn.execute(
            "UPDATE evidence_source_observations SET blob_sha256=? WHERE observation_id='review'",
            ("e" * 64,),
        )
    elif damage == "wrong-kind":
        conn.execute(
            "UPDATE evidence_source_observations SET source_kind='manual' WHERE observation_id='review'"
        )
    elif damage == "wrong-collector":
        conn.execute(
            "UPDATE evidence_source_observations SET collector_code_version='unreviewed' WHERE observation_id='review'"
        )
    else:
        conn.execute(
            "UPDATE evidence_source_observations SET retrieved_at='2026-10-02' WHERE observation_id='review'"
        )
    with pytest.raises(evidence.InputEvidenceError, match="review_blob_mismatch"):
        evidence.verify_source_coverage(conn, request("2026-06-30"), NOW)


@pytest.mark.parametrize(
    "damage",
    [
        None,
        "label-only",
        "changed-capture",
        "changed-review-blob",
        "unavailable-capture",
        "candidate",
    ],
)
def test_population_processing_uses_the_same_native_capture_witness(
    monkeypatch: pytest.MonkeyPatch, damage: str | None
) -> None:
    from provenance import population_document_processing as population

    conn = coverage_db(monkeypatch)
    add_document(
        conn,
        "earnings",
        "6-K",
        "2026-06-30",
        kind="financial_statement",
        family="continuous_disclosure",
        source_kind="sec_filing",
    )
    add_native_capture(conn)
    conn.executescript("""
        ALTER TABLE expected_documents ADD COLUMN expected_document_key TEXT;
        CREATE TABLE expected_document_lifecycle_revisions (
            inventory_key TEXT,expected_document_key TEXT,expected_document_id TEXT,
            status TEXT,revision INTEGER,knowledge_at TEXT,recorded_at TEXT);
        CREATE VIEW v_evidence_document_versions_canonical AS
            SELECT document.*, 'reporting-1' AS reporting_entity_id FROM evidence_document_versions document;
        CREATE VIEW v_source_inventory_sealed_complete AS SELECT snapshot_id FROM source_inventory_snapshots;
    """)
    conn.execute("UPDATE expected_documents SET expected_document_key='logical-earnings'")
    conn.execute(
        "INSERT INTO expected_document_lifecycle_revisions VALUES ('sec','logical-earnings','earnings','expected',1,?,?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    if damage == "label-only":
        conn.execute("UPDATE source_coverage_assessments SET reason_details_json='{}'")
    elif damage == "changed-capture":
        conn.execute("UPDATE evidence_document_versions SET blob_sha256=?", ("e" * 64,))
    elif damage == "changed-review-blob":
        conn.execute(
            "UPDATE evidence_source_observations SET blob_sha256=? WHERE observation_id='review'",
            ("e" * 64,),
        )
    elif damage == "unavailable-capture":
        conn.execute(
            "UPDATE source_coverage_assessments SET coverage_status='available',document_version_id=NULL"
        )
    elif damage == "candidate":
        conn.execute("UPDATE expected_documents SET expectation_basis='publisher_candidate'")
    conn.row_factory = sqlite3.Row
    target = population.__dict__["_document_scope"]
    assert callable(target)
    scope = cast(
        Callable[
            [sqlite3.Connection, datetime, datetime],
            tuple[
                tuple[population.ReportingDocumentDecision, ...], dict[str, tuple[str, ...]], int
            ],
        ],
        target,
    )
    decisions, documents, _ = scope(conn, NOW, NOW)
    assert len(decisions) == 1
    if damage is None:
        assert decisions[0].outcome == "governed_reporting"
        assert documents == {"issuer-1": ("document-1",)}
    else:
        assert decisions[0].outcome == "unresolved"
        assert decisions[0].reason_code.startswith("financial_reporting_subject")
        assert documents == {}


@pytest.mark.parametrize("filing_at", ["2024-06-01", "2025-12-30", "2025-12-31"])
def test_unknown_historical_sec_6k_cannot_postdate_a_proven_annual_anchor(
    monkeypatch: pytest.MonkeyPatch, filing_at: str
) -> None:
    conn = coverage_db(monkeypatch)
    add_document(conn, "annual", "20-F", "2025-12-31")
    add_document(
        conn,
        "historical",
        "6-K",
        None,
        source_kind="sec_filing",
        family="continuous_disclosure",
        filing_at=filing_at,
    )
    assert evidence.verify_source_coverage(conn, request("2025-12-31"), NOW)[2] == ("inventory",)


def test_verified_financial_6k_can_supply_the_dated_discovery_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = coverage_db(monkeypatch)
    add_document(conn, "annual", "20-F", "2025-12-31")
    add_document(
        conn,
        "historical",
        "6-K",
        None,
        source_kind="sec_filing",
        family="continuous_disclosure",
        filing_at="2026-06-15",
    )
    add_document(
        conn,
        "earnings",
        "6-K",
        "2026-06-30",
        kind="financial_statement",
        family="continuous_disclosure",
        source_kind="sec_filing",
        filing_at="2026-08-12",
    )
    add_native_capture(conn)
    assert evidence.verify_source_coverage(conn, request("2026-06-30"), NOW)[2] == ("inventory",)


@pytest.mark.parametrize("filing_at", [None, "invalid", "2026-01-01", "2026-10-02"])
def test_unknown_missing_invalid_or_recent_sec_6k_remains_blocked(
    monkeypatch: pytest.MonkeyPatch, filing_at: str | None
) -> None:
    conn = coverage_db(monkeypatch)
    add_document(conn, "annual", "20-F", "2025-12-31")
    add_document(
        conn,
        "unknown",
        "6-K",
        None,
        source_kind="sec_filing",
        family="continuous_disclosure",
        filing_at=filing_at,
    )
    with pytest.raises(evidence.InputEvidenceError, match="financial_reporting"):
        evidence.verify_source_coverage(conn, request("2025-12-31"), NOW)


def test_requested_anchor_alone_cannot_supply_the_historical_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = coverage_db(monkeypatch)
    add_document(conn, "annual", "20-F", "2025-12-31")
    add_document(
        conn,
        "unknown",
        "6-K",
        None,
        source_kind="sec_filing",
        family="continuous_disclosure",
        filing_at="2026-06-15",
    )
    with pytest.raises(evidence.InputEvidenceError, match="classification_unavailable"):
        evidence.verify_source_coverage(conn, request("2026-06-30"), NOW)


def test_classified_future_of_filing_period_is_rejected_before_old_package_exclusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = coverage_db(monkeypatch)
    add_document(conn, "annual", "20-F", "2025-12-31")
    add_document(
        conn,
        "financial",
        "6-K",
        "2026-06-30",
        kind="financial_statement",
        family="issuer_financial_statements",
        filing_at="2025-12-01",
    )
    with pytest.raises(
        evidence.InputEvidenceError, match="financial_reporting_period_after_filing"
    ):
        evidence.verify_source_coverage(conn, request("2025-12-31"), NOW)


@pytest.mark.parametrize("filing_at", [None, "invalid", "2025-12-30", "2026-10-02"])
def test_periodic_bound_requires_sane_reported_period_and_filing_date(
    monkeypatch: pytest.MonkeyPatch, filing_at: str | None
) -> None:
    conn = coverage_db(monkeypatch)
    add_document(conn, "annual", "20-F", "2025-12-31", filing_at=filing_at)
    with pytest.raises(evidence.InputEvidenceError, match="financial_reporting"):
        evidence.verify_source_coverage(conn, request("2025-12-31"), NOW)


def test_unknown_ir_package_cannot_use_native_sec_filing_date_exclusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = coverage_db(monkeypatch)
    add_document(conn, "annual", "20-F", "2025-12-31")
    add_document(conn, "unknown", "6-K", None, filing_at="2025-12-30")
    with pytest.raises(evidence.InputEvidenceError, match="classification_unavailable"):
        evidence.verify_source_coverage(conn, request("2025-12-31"), NOW)


def test_historical_dated_bound_does_not_weaken_expected_issuer_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = coverage_db(monkeypatch)
    add_document(conn, "annual", "20-F", "2025-12-31")
    add_document(conn, "unknown", "6-K", None, source_kind="sec_filing", filing_at="2025-12-30")
    conn.execute(
        "UPDATE expected_documents SET issuer_id='other' WHERE expected_document_id='unknown'"
    )
    with pytest.raises(evidence.InputEvidenceError, match="authority_invalid"):
        evidence.verify_source_coverage(conn, request("2025-12-31"), NOW)
