"""Offline parity and fail-closed tests for the CompanyFacts fact matcher."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

import provenance.sec_companyfacts_capture as companyfacts_capture
from execution.match_legacy_companyfacts_evidence import main as cli_main
from provenance.sec_companyfacts_capture import CompanyFactsPayload
from provenance.sec_companyfacts_fact_matcher import (
    CompanyFactsFactMatcherRequest,
    match_legacy_companyfacts_evidence,
)

STAMP = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)
ACCESSION = "0000000001-26-000001"
OTHER_ACCESSION = "0000000001-26-000002"
MATCH_COLUMNS = """
    match_revision_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    fact_table TEXT NOT NULL,
    fact_row_id INTEGER NOT NULL,
    issuer_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    fact_payload_json TEXT NOT NULL,
    fact_payload_fingerprint_sha256 TEXT NOT NULL,
    original_locator_json TEXT,
    original_locator_sha256 TEXT,
    relocated_locator_json TEXT,
    relocated_locator_sha256 TEXT,
    legacy_binding_revision_id TEXT NOT NULL,
    legacy_binding_revision INTEGER NOT NULL,
    binding_scope_content_sha256 TEXT NOT NULL,
    evidence_node_id TEXT NOT NULL,
    matched_entry_sha256 TEXT,
    candidate_manifest_json TEXT NOT NULL,
    candidate_manifest_sha256 TEXT NOT NULL,
    candidate_count INTEGER NOT NULL,
    matched_candidate_count INTEGER NOT NULL,
    issuer_check TEXT NOT NULL,
    context_check TEXT NOT NULL,
    unit_check TEXT NOT NULL,
    sign_check TEXT NOT NULL,
    fiscal_period_check TEXT NOT NULL,
    value_check TEXT NOT NULL,
    matcher_name TEXT NOT NULL,
    matcher_version TEXT NOT NULL,
    matcher_config_sha256 TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    reason_details_json TEXT NOT NULL,
    effective_at DATETIME NOT NULL,
    knowledge_at DATETIME NOT NULL,
    recorded_at DATETIME NOT NULL,
    supersedes_match_revision_id TEXT
"""


def _entry(
    *,
    value: int | float = 100,
    accession: str = ACCESSION,
    end: str = "2026-06-30",
    start: str | None = "2026-04-01",
    fp: str | None = "Q2",
    form: str = "10-Q",
    frame: str | None = None,
) -> dict[str, object]:
    return {
        "end": end,
        "val": value,
        "accn": accession,
        "fy": 2026,
        "fp": fp,
        "form": form,
        "filed": "2026-07-20",
        "start": start,
        "frame": frame,
    }


def _payload(
    *,
    facts: dict[str, dict[str, dict[str, object]]] | None = None,
) -> bytes:
    body = {
        "cik": 1,
        "entityName": "Acme Corp",
        "facts": facts
        or {
            "us-gaap": {
                "Revenues": {
                    "label": "Revenue",
                    "description": "Revenue",
                    "units": {"USD": [_entry()]},
                }
            }
        },
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        f"""
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            source_type TEXT NOT NULL,
            doc_type TEXT NOT NULL,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            fetched_at DATETIME NOT NULL,
            fetch_status TEXT NOT NULL,
            raw_bytes_size INTEGER NOT NULL,
            source_url TEXT,
            source_quality_tier TEXT,
            accession_number TEXT
        );
        CREATE TABLE financial_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_end DATETIME NOT NULL,
            fiscal_period_type TEXT NOT NULL,
            line_item TEXT NOT NULL,
            value NUMERIC NOT NULL,
            currency TEXT,
            unit TEXT NOT NULL,
            source_doc_id INTEGER NOT NULL,
            confidence REAL NOT NULL,
            extracted_by TEXT,
            supersedes_id INTEGER,
            locator TEXT
        );
        CREATE TABLE kpi_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            period_end DATETIME NOT NULL,
            fiscal_period_type TEXT NOT NULL,
            kpi_definition_id INTEGER NOT NULL,
            value NUMERIC NOT NULL,
            unit TEXT NOT NULL,
            source_doc_id INTEGER NOT NULL,
            confidence REAL NOT NULL,
            extracted_by TEXT,
            supersedes_id INTEGER,
            locator TEXT,
            source_excerpt TEXT,
            computed_from TEXT,
            formula_id INTEGER,
            formula_version INTEGER
        );
        CREATE TABLE evidence_content_blobs (
            sha256 TEXT PRIMARY KEY,
            byte_size INTEGER NOT NULL,
            media_type TEXT NOT NULL,
            storage_uri TEXT NOT NULL,
            recorded_at DATETIME NOT NULL
        );
        CREATE TABLE evidence_source_observations (
            observation_id TEXT PRIMARY KEY,
            blob_sha256 TEXT NOT NULL,
            retrieved_at DATETIME NOT NULL
        );
        CREATE TABLE evidence_document_versions (
            document_version_id TEXT PRIMARY KEY,
            observation_id TEXT NOT NULL,
            blob_sha256 TEXT NOT NULL,
            issuer_id TEXT NOT NULL,
            document_type TEXT NOT NULL
        );
        CREATE TABLE evidence_blob_location_observations (
            location_observation_id TEXT PRIMARY KEY,
            blob_sha256 TEXT NOT NULL,
            storage_uri TEXT NOT NULL,
            availability_state TEXT NOT NULL,
            verified_sha256 TEXT,
            location_sequence INTEGER NOT NULL
        );
        CREATE TABLE legacy_document_evidence_binding_revisions (
            binding_revision_id TEXT PRIMARY KEY,
            legacy_document_id INTEGER NOT NULL,
            revision INTEGER NOT NULL,
            document_version_id TEXT NOT NULL,
            evidence_node_id TEXT NOT NULL,
            scope_locator_json TEXT NOT NULL,
            scope_content_sha256 TEXT NOT NULL,
            effective_at DATETIME NOT NULL,
            knowledge_at DATETIME NOT NULL
        );
        CREATE TABLE issuer_identifier_assertions (
            issuer_id TEXT NOT NULL,
            identifier_type TEXT NOT NULL,
            normalized_value TEXT NOT NULL
        );
        CREATE TABLE legacy_fact_evidence_match_revisions ({MATCH_COLUMNS});
        CREATE VIEW v_evidence_blob_locations_current AS
        SELECT *, 1 AS verified_byte_size, recorded_at AS verified_at,
               'local' AS location_kind, location_observation_id AS idempotency_key,
               NULL AS supersedes_location_observation_id
        FROM (
            SELECT location.*, '{STAMP.isoformat()}' AS recorded_at
            FROM evidence_blob_location_observations AS location
        );
        CREATE VIEW v_evidence_document_versions_canonical AS
        SELECT version.document_version_id, version.observation_id,
               version.blob_sha256, version.issuer_id, version.document_type
        FROM evidence_document_versions AS version;
        CREATE VIEW v_issuer_identifiers_canonical AS
        SELECT * FROM issuer_identifier_assertions;
        CREATE VIEW v_legacy_document_evidence_bindings_current AS
        SELECT binding.*
        FROM legacy_document_evidence_binding_revisions AS binding
        WHERE NOT EXISTS (
            SELECT 1 FROM legacy_document_evidence_binding_revisions AS newer
            WHERE newer.legacy_document_id = binding.legacy_document_id
              AND newer.revision > binding.revision
        );
        CREATE VIEW v_legacy_fact_evidence_matches_current AS
        SELECT match.*
        FROM legacy_fact_evidence_match_revisions AS match
        WHERE NOT EXISTS (
            SELECT 1 FROM legacy_fact_evidence_match_revisions AS newer
            WHERE newer.fact_table = match.fact_table
              AND newer.fact_row_id = match.fact_row_id
              AND newer.revision > match.revision
        );
        """
    )


def _seed(
    tmp_path: Path,
    raw_body: bytes,
    *,
    line_item: str = "revenue",
    fact_value: str = "100",
    currency: str | None = "USD",
    unit: str = "actual",
    period_end: str = "2026-06-30",
    fiscal_period: str = "Q2",
    locator: str | None = ('{"json_path":"facts.us-gaap.Revenues.units.USD[0]"}'),
    aggregate_identity: bool = False,
) -> tuple[sqlite3.Connection, Path]:
    db_path = tmp_path / "matcher.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _schema(conn)
    payload = CompanyFactsPayload.model_validate_json(raw_body)
    accession_scopes = cast(
        "dict[str, bytes]",
        vars(companyfacts_capture)["_accession_scopes"](payload),
    )
    scope_sha = hashlib.sha256(accession_scopes[ACCESSION]).hexdigest()
    blob_sha = hashlib.sha256(raw_body).hexdigest()
    blob_root = tmp_path / "blobs"
    blob_path = blob_root / blob_sha[:2] / f"{blob_sha}.json"
    blob_path.parent.mkdir(parents=True)
    blob_path.write_bytes(raw_body)
    conn.execute(
        "INSERT INTO documents VALUES "
        "(1, 'ACME', 'sec_xbrl', ?, 'companyfacts.json', ?, ?, "
        "'ok', ?, 'https://data.sec.gov/companyfacts', 'sec_official', ?)",
        (
            "sec_companyfacts_snapshot" if aggregate_identity else "sec_10q",
            blob_sha if aggregate_identity else hashlib.sha256(ACCESSION.encode()).hexdigest(),
            STAMP,
            len(raw_body),
            None if aggregate_identity else ACCESSION,
        ),
    )
    conn.execute(
        "INSERT INTO evidence_content_blobs VALUES (?, ?, 'application/json', ?, ?)",
        (blob_sha, len(raw_body), blob_path.as_uri(), STAMP),
    )
    conn.execute(
        "INSERT INTO evidence_source_observations VALUES ('source-1', ?, ?)",
        (blob_sha, STAMP),
    )
    conn.execute(
        "INSERT INTO evidence_document_versions VALUES "
        "('document-1', 'source-1', ?, 'issuer-acme', "
        "'companyfacts_snapshot')",
        (blob_sha,),
    )
    conn.execute(
        "INSERT INTO evidence_blob_location_observations VALUES "
        "('location-1', ?, ?, 'present', ?, 1)",
        (blob_sha, blob_path.as_uri(), blob_sha),
    )
    conn.execute(
        "INSERT INTO legacy_document_evidence_binding_revisions VALUES "
        "('binding-1', 1, 1, 'document-1', 'node-1', ?, ?, ?, ?)",
        (
            json.dumps(
                {"source_ref": "https://data.sec.gov/companyfacts"}
                if aggregate_identity
                else {"accession_number": ACCESSION}
            ),
            blob_sha if aggregate_identity else scope_sha,
            STAMP,
            STAMP,
        ),
    )
    conn.execute(
        "INSERT INTO issuer_identifier_assertions VALUES ('issuer-acme', 'sec_cik', '0000000001')"
    )
    conn.execute(
        "INSERT INTO financial_facts "
        "(ticker, period_end, fiscal_period_type, line_item, value, currency, "
        "unit, source_doc_id, confidence, extracted_by, locator) "
        "VALUES ('ACME', ?, ?, ?, ?, ?, ?, 1, 0.99, 'sec_xbrl', ?)",
        (
            period_end,
            fiscal_period,
            line_item,
            fact_value,
            currency,
            unit,
            locator,
        ),
    )
    conn.commit()
    return conn, blob_root


def test_aggregate_snapshot_matches_fact_locator_without_filing_document(
    tmp_path: Path,
) -> None:
    locator = json.dumps(
        {
            "accession_number": ACCESSION,
            "json_path": "facts.us-gaap.Revenues.units.USD[0]",
        }
    )
    raw = _payload()
    conn, blob_root = _seed(
        tmp_path,
        raw,
        locator=locator,
        aggregate_identity=True,
    )
    try:
        summary = match_legacy_companyfacts_evidence(
            conn,
            _request(tmp_path, blob_root, apply=True),
            now=lambda: STAMP,
        )

        assert summary.accepted == 1
        document = conn.execute(
            "SELECT doc_type, accession_number, sha256 FROM documents"
        ).fetchone()
        assert tuple(document) == (
            "sec_companyfacts_snapshot",
            None,
            hashlib.sha256(raw).hexdigest(),
        )
        binding = conn.execute(
            "SELECT scope_locator_json, scope_content_sha256 "
            "FROM legacy_document_evidence_binding_revisions"
        ).fetchone()
        assert "accession_number" not in json.loads(str(binding[0]))
        assert str(binding[1]) == hashlib.sha256(raw).hexdigest()
    finally:
        conn.close()


def test_legacy_accession_binding_remains_readable_with_enriched_fact_locator(
    tmp_path: Path,
) -> None:
    locator = json.dumps(
        {
            "accession_number": ACCESSION,
            "json_path": "facts.us-gaap.Revenues.units.USD[0]",
        }
    )
    conn, blob_root = _seed(tmp_path, _payload(), locator=locator)
    try:
        summary = match_legacy_companyfacts_evidence(
            conn,
            _request(tmp_path, blob_root, apply=True),
            now=lambda: STAMP,
        )

        assert summary.accepted == 1
        match = conn.execute(
            "SELECT outcome, relocated_locator_json FROM legacy_fact_evidence_match_revisions"
        ).fetchone()
        assert str(match[0]) == "accepted"
        relocated = json.loads(str(match[1]))
        assert relocated["accession_number"] == ACCESSION
        assert relocated["json_path"] == "facts.us-gaap.Revenues.units.USD[0]"
    finally:
        conn.close()


def _request(tmp_path: Path, blob_root: Path, *, apply: bool) -> CompanyFactsFactMatcherRequest:
    return CompanyFactsFactMatcherRequest(
        blob_root=blob_root,
        checkpoint_root=tmp_path / "checkpoints",
        apply=apply,
        batch_size=20,
        fact_tables=("financial_facts",),
    )


@pytest.mark.parametrize(
    ("locator", "entry_index"),
    [
        ('{"json_path":"facts.us-gaap.Revenues.units.USD[0]"}', 0),
        ('{"json_path":"facts.us-gaap.Revenues.units.USD[0]"}', 1),
        (None, 1),
    ],
)
def test_accepts_exact_relocated_and_missing_locator(
    tmp_path: Path,
    locator: str | None,
    entry_index: int,
) -> None:
    entries = (
        [_entry()] if entry_index == 0 else [_entry(value=50, end="2026-03-31", fp="Q1"), _entry()]
    )
    raw = _payload(
        facts={
            "us-gaap": {
                "Revenues": {
                    "label": "Revenue",
                    "description": "Revenue",
                    "units": {"USD": entries},
                }
            }
        }
    )
    conn, blob_root = _seed(tmp_path, raw, locator=locator)
    try:
        summary = match_legacy_companyfacts_evidence(
            conn,
            _request(tmp_path, blob_root, apply=True),
            now=lambda: STAMP,
        )
        assert summary.accepted == 1
        stored = conn.execute(
            "SELECT outcome, relocated_locator_json, candidate_count "
            "FROM legacy_fact_evidence_match_revisions"
        ).fetchone()
        assert stored["outcome"] == "accepted"
        relocated = json.loads(stored["relocated_locator_json"])
        assert relocated["entry_index"] == entry_index
        assert stored["candidate_count"] == len(entries)
    finally:
        conn.close()


def test_sign_inversion_uses_sec_xbrl_ladder(tmp_path: Path) -> None:
    raw = _payload(
        facts={
            "us-gaap": {
                "PaymentsToAcquirePropertyPlantAndEquipment": {
                    "label": "Capex",
                    "description": "Capex",
                    "units": {"USD": [_entry(value=75)]},
                }
            }
        }
    )
    conn, blob_root = _seed(
        tmp_path,
        raw,
        line_item="capital_expenditure",
        fact_value="-75",
        locator=None,
    )
    try:
        summary = match_legacy_companyfacts_evidence(
            conn, _request(tmp_path, blob_root, apply=True), now=lambda: STAMP
        )
        assert summary.accepted == 1
    finally:
        conn.close()


@pytest.mark.parametrize("fiscal_period", ["FY", "Q4"])
def test_instant_fye_dual_labels(tmp_path: Path, fiscal_period: str) -> None:
    raw = _payload(
        facts={
            "us-gaap": {
                "Assets": {
                    "label": "Assets",
                    "description": "Assets",
                    "units": {
                        "USD": [
                            _entry(
                                end="2025-12-31",
                                start=None,
                                fp="FY",
                                form="10-K",
                            )
                        ]
                    },
                }
            }
        }
    )
    conn, blob_root = _seed(
        tmp_path,
        raw,
        line_item="total_assets",
        period_end="2025-12-31",
        fiscal_period=fiscal_period,
        locator=None,
    )
    try:
        summary = match_legacy_companyfacts_evidence(
            conn, _request(tmp_path, blob_root, apply=True), now=lambda: STAMP
        )
        assert summary.accepted == 1
    finally:
        conn.close()


def test_duplicate_identical_entries_remain_ambiguous(tmp_path: Path) -> None:
    duplicate = _entry()
    raw = _payload(
        facts={
            "us-gaap": {
                "Revenues": {
                    "label": "Revenue",
                    "description": "Revenue",
                    "units": {"USD": [duplicate, dict(duplicate)]},
                }
            }
        }
    )
    conn, blob_root = _seed(tmp_path, raw, locator=None)
    try:
        summary = match_legacy_companyfacts_evidence(
            conn, _request(tmp_path, blob_root, apply=True), now=lambda: STAMP
        )
        assert summary.terminal == 1
        row = conn.execute(
            "SELECT reason_code, candidate_count, matched_candidate_count, "
            "issuer_check, context_check, unit_check, sign_check, "
            "fiscal_period_check, value_check "
            "FROM legacy_fact_evidence_match_revisions"
        ).fetchone()
        assert row["reason_code"] == "ambiguous_companyfacts_candidates"
        assert row["candidate_count"] == 2
        assert row["matched_candidate_count"] == 2
        assert set(tuple(row)[3:]) == {"pass"}
    finally:
        conn.close()


def test_zero_match_is_terminal_with_failed_check(tmp_path: Path) -> None:
    conn, blob_root = _seed(tmp_path, _payload(), fact_value="999", locator=None)
    try:
        summary = match_legacy_companyfacts_evidence(
            conn, _request(tmp_path, blob_root, apply=True), now=lambda: STAMP
        )
        assert summary.terminal == 1
        row = conn.execute(
            "SELECT reason_code, value_check, reason_details_json "
            "FROM legacy_fact_evidence_match_revisions"
        ).fetchone()
        assert row["reason_code"] == "no_exact_companyfacts_match"
        assert row["value_check"] == "fail"
        assert "value_check" in json.loads(row["reason_details_json"])["failed_checks"]
    finally:
        conn.close()


def test_derived_kpi_is_terminal_without_document_match(tmp_path: Path) -> None:
    conn, blob_root = _seed(tmp_path, _payload())
    conn.execute("DELETE FROM financial_facts")
    conn.execute(
        "INSERT INTO kpi_facts "
        "(ticker, period_end, fiscal_period_type, kpi_definition_id, value, unit, "
        "source_doc_id, confidence, extracted_by, computed_from, formula_id, "
        "formula_version) VALUES "
        "('ACME', '2026-06-30', 'Q2', 1, 25, 'percent', 1, 0.9, "
        "'metrics_engine_derived', 'revenue,cost', 1, 1)"
    )
    conn.commit()
    request = CompanyFactsFactMatcherRequest(
        blob_root=blob_root,
        checkpoint_root=tmp_path / "checkpoints",
        apply=True,
        fact_tables=("kpi_facts",),
    )
    try:
        summary = match_legacy_companyfacts_evidence(conn, request, now=lambda: STAMP)
        assert summary.terminal == 1
        row = conn.execute(
            "SELECT outcome, reason_code, relocated_locator_json "
            "FROM legacy_fact_evidence_match_revisions"
        ).fetchone()
        assert tuple(row) == (
            "terminal",
            "derived_kpi_not_document_matchable",
            None,
        )
        replay = match_legacy_companyfacts_evidence(conn, request, now=lambda: STAMP)
        assert replay.considered == 0
    finally:
        conn.close()


def test_corrupt_blob_is_retryable_and_reconsidered(tmp_path: Path) -> None:
    raw = _payload()
    conn, blob_root = _seed(tmp_path, raw)
    blob_path = next(blob_root.rglob("*.json"))
    blob_path.write_bytes(b"corrupt")
    try:
        first = match_legacy_companyfacts_evidence(
            conn, _request(tmp_path, blob_root, apply=True), now=lambda: STAMP
        )
        assert first.retryable == 1
        blob_path.write_bytes(raw)
        second = match_legacy_companyfacts_evidence(
            conn, _request(tmp_path, blob_root, apply=True), now=lambda: STAMP
        )
        assert second.accepted == 1
        assert (
            conn.execute("SELECT COUNT(*) FROM legacy_fact_evidence_match_revisions").fetchone()[0]
            == 2
        )
    finally:
        conn.close()


def test_unseen_rows_are_not_starved_by_retryable_prefix(tmp_path: Path) -> None:
    raw = _payload()
    conn, blob_root = _seed(tmp_path, raw)
    conn.execute(
        "INSERT INTO financial_facts "
        "(ticker, period_end, fiscal_period_type, line_item, value, currency, "
        "unit, source_doc_id, confidence, extracted_by, locator) "
        "SELECT ticker, period_end, fiscal_period_type, line_item, value, "
        "currency, unit, source_doc_id, confidence, extracted_by, locator "
        "FROM financial_facts WHERE id = 1"
    )
    conn.commit()
    next(blob_root.rglob("*.json")).write_bytes(b"corrupt")
    request = _request(
        tmp_path,
        blob_root,
        apply=True,
    ).model_copy(update={"batch_size": 1})
    try:
        first = match_legacy_companyfacts_evidence(conn, request, now=lambda: STAMP)
        second = match_legacy_companyfacts_evidence(conn, request, now=lambda: STAMP)
        assert first.items[0].fact_row_id == 1
        assert first.retryable == 1
        assert second.items[0].fact_row_id == 2
        assert second.retryable == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM legacy_fact_evidence_match_revisions WHERE fact_row_id = 1"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_dry_run_is_read_only_and_apply_replay_is_current(tmp_path: Path) -> None:
    conn, blob_root = _seed(tmp_path, _payload())
    try:
        dry = match_legacy_companyfacts_evidence(
            conn, _request(tmp_path, blob_root, apply=False), now=lambda: STAMP
        )
        assert dry.considered == 1
        assert dry.accepted == 1
        assert dry.items[0].reason_code == "unique_companyfacts_entry"
        assert dry.revisions_created == 0
        assert dry.revisions_replayed == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM legacy_fact_evidence_match_revisions").fetchone()[0]
            == 0
        )
        first = match_legacy_companyfacts_evidence(
            conn, _request(tmp_path, blob_root, apply=True), now=lambda: STAMP
        )
        second = match_legacy_companyfacts_evidence(
            conn, _request(tmp_path, blob_root, apply=True), now=lambda: STAMP
        )
        assert first.revisions_created == 1
        assert second.already_current is None
        assert second.considered == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM legacy_fact_evidence_match_revisions").fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_lower_rung_and_nonmodal_currency_cannot_match(tmp_path: Path) -> None:
    raw = _payload(
        facts={
            "us-gaap": {
                "Revenues": {
                    "label": "Revenue",
                    "description": "Revenue",
                    "units": {"USD": [_entry(value=50)]},
                },
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "label": "Revenue",
                    "description": "Revenue",
                    "units": {
                        "USD": [_entry()],
                        "EUR": [
                            _entry(value=100),
                            _entry(value=100, accession=OTHER_ACCESSION),
                        ],
                    },
                },
            }
        }
    )
    conn, blob_root = _seed(tmp_path, raw, locator=None)
    try:
        summary = match_legacy_companyfacts_evidence(
            conn, _request(tmp_path, blob_root, apply=True), now=lambda: STAMP
        )
        assert summary.terminal == 1
    finally:
        conn.close()


def test_same_document_pick_key_collapses_looser_context(tmp_path: Path) -> None:
    raw = _payload(
        facts={
            "us-gaap": {
                "Revenues": {
                    "label": "Revenue",
                    "description": "Revenue",
                    "units": {
                        "USD": [
                            _entry(value=50, start="2026-01-01"),
                            _entry(value=100, start="2026-04-01"),
                        ]
                    },
                }
            }
        }
    )
    conn, blob_root = _seed(tmp_path, raw, locator=None)
    try:
        summary = match_legacy_companyfacts_evidence(
            conn, _request(tmp_path, blob_root, apply=True), now=lambda: STAMP
        )
        assert summary.accepted == 1
        row = conn.execute(
            "SELECT candidate_count, matched_candidate_count "
            "FROM legacy_fact_evidence_match_revisions"
        ).fetchone()
        assert tuple(row) == (2, 1)
    finally:
        conn.close()


def test_cli_rejects_live_database() -> None:
    assert (
        cli_main(
            [
                "--db",
                str(Path("data") / "portfolio.db"),
                "--blob-root",
                "unused-blobs",
                "--checkpoint-root",
                "unused-checkpoints",
                "--apply",
            ]
        )
        == 2
    )
