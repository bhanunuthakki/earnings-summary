"""Canonical issuer-registry bootstrap from the SEC company-ticker authority."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    SourceObservation,
)
from provenance.issuer_registry_bootstrap import (
    BootstrapRequest,
    SecCompanyTickerContractError,
    bootstrap_issuer_reporting_registry,
    fetch_sec_company_tickers,
    parse_sec_company_tickers,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

ROOT = Path(__file__).resolve().parents[1]
HEAD = "0230_evidence_subject_bindings"
STAMP = datetime(2026, 7, 27, 21, 0, tzinfo=UTC)
SOURCE_URL = "https://www.sec.gov/files/company_tickers.json"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "bootstrap.db"
    config = _config(path)
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, HEAD)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE tracked_companies (
            id INTEGER PRIMARY KEY,
            user_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            name TEXT NOT NULL,
            list_type TEXT NOT NULL,
            archived_at TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()
    return path


def _body(*entries: dict[str, object]) -> bytes:
    return json.dumps(
        {str(index): entry for index, entry in enumerate(entries)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _request(
    tmp_path: Path,
    *,
    apply: bool,
    recorded_at: datetime = STAMP,
) -> BootstrapRequest:
    return BootstrapRequest(
        source_url=SOURCE_URL,
        blob_root=tmp_path / "blobs",
        apply=apply,
        recorded_at=recorded_at,
    )


def test_dry_run_is_read_only_and_excludes_index_members(tmp_path: Path) -> None:
    db_path = _database(tmp_path)
    writer = sqlite3.connect(db_path)
    writer.executemany(
        "INSERT INTO tracked_companies VALUES (?, 'bhanu', ?, ?, ?, NULL)",
        (
            (1, "ACME", "Acme", "portfolio"),
            (2, "MON", "Monitor", "watchlist"),
            (3, "EVAL", "Evaluation", "evaluation"),
            (4, "INDEX", "Index constituent", "index_member"),
        ),
    )
    writer.commit()
    writer.close()
    raw = _body(
        {"cik_str": 123, "ticker": "ACME", "title": "Acme Corporation"},
        {"cik_str": 456, "ticker": "MON", "title": "Monitor Incorporated"},
        {"cik_str": 789, "ticker": "EVAL", "title": "Evaluation Ltd."},
        {"cik_str": 999, "ticker": "INDEX", "title": "Index Member Inc."},
    )

    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    result = bootstrap_issuer_reporting_registry(
        conn,
        raw_body=raw,
        request=_request(tmp_path, apply=False),
    )
    conn.close()

    assert result.mode == "dry_run"
    assert result.selected_tickers == ("ACME", "EVAL", "MON")
    assert result.excluded_index_member_count == 1
    assert {item.ticker: item.inclusion_state for item in result.results} == {
        "ACME": "core",
        "EVAL": "monitored",
        "MON": "monitored",
    }
    assert result.records_created == 0
    check = sqlite3.connect(db_path)
    assert check.execute("SELECT COUNT(*) FROM issuer_entities").fetchone() == (0,)
    assert check.execute("SELECT COUNT(*) FROM evidence_content_blobs").fetchone() == (0,)
    check.close()
    assert not (tmp_path / "blobs").exists()


def test_apply_captures_evidence_and_exact_replay_creates_nothing(
    tmp_path: Path,
) -> None:
    db_path = _database(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO tracked_companies VALUES (1, 'bhanu', 'ACME', 'Acme', 'portfolio', NULL)"
    )
    conn.commit()
    raw = _body({"cik_str": 123456, "ticker": "ACME", "title": "Acme Corporation"})

    first = bootstrap_issuer_reporting_registry(
        conn,
        raw_body=raw,
        request=_request(tmp_path, apply=True),
    )
    conn.commit()
    second = bootstrap_issuer_reporting_registry(
        conn,
        raw_body=raw,
        request=_request(tmp_path, apply=True),
    )
    conn.commit()

    assert first.records_created == 18
    assert second.records_created == 0
    row = conn.execute(
        "SELECT i.issuer_id, i.normalized_value, r.outcome "
        "FROM issuer_identifier_assertions AS i "
        "JOIN issuer_identifier_resolution_outcomes AS r "
        "ON r.selected_assertion_id = i.assertion_id"
    ).fetchone()
    assert row is not None
    assert row[1:] == ("0000123456", "selected")
    issuer_id = str(row[0])
    assert "ACME" not in issuer_id
    assert "123456" not in issuer_id
    assert conn.execute(
        "SELECT status, source_url FROM issuer_authority_surface_revisions"
    ).fetchone() == (
        "verified",
        "https://data.sec.gov/submissions/CIK0000123456.json",
    )
    assert conn.execute(
        "SELECT inclusion_state, history_policy FROM issuer_reporting_scope_revisions"
    ).fetchone() == ("core", "all_available")
    assert conn.execute("SELECT reporting_entity_kind FROM reporting_entities").fetchone() == (
        "legal_registrant",
    )
    assert conn.execute(
        "SELECT identifier_type, normalized_value FROM reporting_entity_identifier_assertions"
    ).fetchone() == ("sec_cik", "0000123456")
    assert conn.execute(
        "SELECT authority_kind, document_family, obligation_state "
        "FROM source_obligation_revisions ORDER BY document_family"
    ).fetchall() == [
        ("sec_edgar", "continuous_disclosure", "required"),
        ("issuer_publisher", "issuer_earnings_materials", "required"),
        ("issuer_publisher", "issuer_financial_statements", "required"),
        ("issuer_publisher", "issuer_presentations", "required"),
        ("sec_edgar", "operating_company_periodic", "required"),
    ]
    assert conn.execute(
        "SELECT recorded_issuer_id, outcome FROM legacy_issuer_binding_revisions"
    ).fetchone() == ("legacy-ticker:ACME", "selected")
    assert conn.execute("SELECT COUNT(*) FROM evidence_content_blobs").fetchone() == (1,)
    assert conn.execute(
        "SELECT availability_state, verified_sha256 FROM evidence_blob_location_observations"
    ).fetchone() == ("present", first.source_sha256)
    assert conn.execute("SELECT source_kind FROM evidence_source_observations").fetchone() == (
        "sec_company_tickers",
    )
    blob_files = tuple(path for path in (tmp_path / "blobs").rglob("*") if path.is_file())
    assert len(blob_files) == 1
    assert blob_files[0].read_bytes() == raw
    conn.close()


def test_new_source_observation_reuses_immutable_reporting_entity(
    tmp_path: Path,
) -> None:
    db_path = _database(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO tracked_companies VALUES (1, 'bhanu', 'ACME', 'Acme', 'portfolio', NULL)"
    )
    conn.commit()
    first_raw = _body({"cik_str": 123456, "ticker": "ACME", "title": "Acme Corporation"})
    second_raw = _body(
        {"cik_str": 123456, "ticker": "ACME", "title": "Acme Corporation"},
        {"cik_str": 999999, "ticker": "OTHER", "title": "Other Corporation"},
    )

    first = bootstrap_issuer_reporting_registry(
        conn,
        raw_body=first_raw,
        request=_request(tmp_path, apply=True),
    )
    conn.commit()
    second = bootstrap_issuer_reporting_registry(
        conn,
        raw_body=second_raw,
        request=_request(
            tmp_path,
            apply=True,
            recorded_at=datetime(2026, 7, 28, 21, 0, tzinfo=UTC),
        ),
    )
    conn.commit()

    assert second.source_sha256 != first.source_sha256
    assert second.records_created > 0
    assert conn.execute(
        "SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM reporting_entities"
    ).fetchone() == (1, STAMP.isoformat(" "), STAMP.isoformat(" "))
    assert conn.execute(
        "SELECT revision, supersedes_resolution_id IS NOT NULL "
        "FROM reporting_entity_identifier_resolution_outcomes ORDER BY revision"
    ).fetchall() == [(1, 0), (2, 1)]
    assert conn.execute("SELECT COUNT(*) FROM source_obligation_revisions").fetchone() == (5,)
    conn.close()


def test_document_persistence_auto_binds_resolved_sec_cik_subject(
    tmp_path: Path,
) -> None:
    db_path = _database(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO tracked_companies VALUES (1, 'bhanu', 'ACME', 'Acme', 'portfolio', NULL)"
    )
    conn.commit()
    raw = _body({"cik_str": 123456, "ticker": "ACME", "title": "Acme Corporation"})
    bootstrap_issuer_reporting_registry(
        conn,
        raw_body=raw,
        request=_request(tmp_path, apply=True),
    )

    body = b"issuer release"
    digest = hashlib.sha256(body).hexdigest()
    blob_path = tmp_path / digest
    blob_path.write_bytes(body)
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=digest,
            byte_size=len(body),
            media_type="text/html",
            storage_uri=blob_path.resolve().as_uri(),
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id="issuer-release-observation",
            idempotency_key="issuer-release-observation",
            source_kind="issuer_release",
            source_url="https://ir.example.test/release",
            blob_sha256=digest,
            source_published_at=STAMP,
            filing_at=None,
            accepted_at=None,
            observed_at=STAMP,
            retrieved_at=STAMP,
            retrieval_config_sha256="c" * 64,
            collector_code_version="fixture@1",
        )
    )
    ledger.persist(
        DocumentVersion(
            document_version_id="issuer-release-document",
            document_key="issuer-release-document",
            version_sequence=1,
            observation_id="issuer-release-observation",
            blob_sha256=digest,
            issuer_id="sec-cik-0000123456",
            ticker="ACME",
            document_type="earnings_release",
            form_type="IR",
            language="en",
            recorded_at=STAMP,
        )
    )
    conn.commit()

    assert conn.execute(
        "SELECT canonical_issuer_id, outcome, reason_code "
        "FROM v_legacy_issuer_bindings_current "
        "WHERE recorded_issuer_id = 'sec-cik-0000123456'"
    ).fetchone() == (
        conn.execute(
            "SELECT issuer_id FROM v_issuer_identifiers_canonical "
            "WHERE normalized_value = '0000123456'"
        ).fetchone()[0],
        "selected",
        "unique_sec_cik_selected",
    )
    conn.close()


def test_bootstrap_reconciles_existing_sec_cik_evidence_subject(
    tmp_path: Path,
) -> None:
    db_path = _database(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO tracked_companies VALUES (1, 'bhanu', 'ACME', 'Acme', 'portfolio', NULL)"
    )
    conn.execute(
        "INSERT INTO evidence_document_versions "
        "(document_version_id, document_key, version_sequence, observation_id, "
        "blob_sha256, issuer_id, ticker, document_type, form_type, language, recorded_at) "
        "VALUES ('orphan-document', 'orphan-document', 1, 'orphan-observation', "
        "?, 'sec-cik-0000123456', 'ACME', 'earnings_release', 'IR', 'en', ?)",
        ("d" * 64, STAMP),
    )
    conn.commit()

    raw = _body({"cik_str": 123456, "ticker": "ACME", "title": "Acme Corporation"})
    bootstrap_issuer_reporting_registry(
        conn,
        raw_body=raw,
        request=_request(tmp_path, apply=True),
    )
    conn.commit()

    assert conn.execute(
        "SELECT outcome FROM v_legacy_issuer_bindings_current "
        "WHERE recorded_issuer_id = 'sec-cik-0000123456'"
    ).fetchone() == ("selected",)
    conn.close()


def test_duplicate_and_missing_sec_tickers_remain_explicitly_unresolved(
    tmp_path: Path,
) -> None:
    db_path = _database(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO tracked_companies VALUES (?, 'bhanu', ?, ?, ?, NULL)",
        (
            (1, "DUP", "Duplicate", "portfolio"),
            (2, "MISS", "Missing", "evaluation"),
        ),
    )
    conn.commit()
    raw = _body(
        {"cik_str": 1, "ticker": "DUP", "title": "Duplicate One"},
        {"cik_str": 2, "ticker": "DUP", "title": "Duplicate Two"},
    )

    result = bootstrap_issuer_reporting_registry(
        conn,
        raw_body=raw,
        request=_request(tmp_path, apply=True),
    )
    conn.commit()

    assert {item.ticker: item.outcome for item in result.results} == {
        "DUP": "unresolved_duplicate",
        "MISS": "unresolved_missing",
    }
    assert conn.execute("SELECT COUNT(*) FROM issuer_entities").fetchone() == (0,)
    assert conn.execute(
        "SELECT recorded_issuer_id, outcome, reason_code "
        "FROM legacy_issuer_binding_revisions ORDER BY recorded_issuer_id"
    ).fetchall() == [
        ("legacy-ticker:DUP", "unresolved", "duplicate_sec_ticker"),
        ("legacy-ticker:MISS", "unresolved", "sec_ticker_missing"),
    ]
    conn.close()


def test_evidence_only_ticker_is_bound_without_entering_research_scope(
    tmp_path: Path,
) -> None:
    db_path = _database(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO tracked_companies VALUES (?, 'bhanu', ?, ?, ?, NULL)",
        (
            (1, "ACME", "Acme", "portfolio"),
            (2, "HIST", "Historical index constituent", "index_member"),
        ),
    )
    body = b"historical filing"
    digest = hashlib.sha256(body).hexdigest()
    blob_path = tmp_path / digest
    blob_path.write_bytes(body)
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=digest,
            byte_size=len(body),
            media_type="text/html",
            storage_uri=blob_path.resolve().as_uri(),
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id="historical-observation",
            idempotency_key="historical-observation",
            source_kind="sec_filing",
            source_url="https://www.sec.gov/Archives/historical.htm",
            blob_sha256=digest,
            source_published_at=STAMP,
            filing_at=STAMP,
            accepted_at=STAMP,
            observed_at=STAMP,
            retrieved_at=STAMP,
            retrieval_config_sha256="b" * 64,
            collector_code_version="fixture@1",
        )
    )
    ledger.persist(
        DocumentVersion(
            document_version_id="historical-document",
            document_key="historical-document",
            version_sequence=1,
            observation_id="historical-observation",
            blob_sha256=digest,
            issuer_id="legacy-ticker:HIST",
            ticker="HIST",
            document_type="filing",
            form_type="10-K",
            language="en",
            recorded_at=STAMP,
        )
    )
    conn.commit()

    raw = _body(
        {"cik_str": 123, "ticker": "ACME", "title": "Acme Corporation"},
        {"cik_str": 456, "ticker": "HIST", "title": "Historical Corporation"},
    )
    result = bootstrap_issuer_reporting_registry(
        conn,
        raw_body=raw,
        request=_request(tmp_path, apply=True),
    )
    conn.commit()

    by_ticker = {item.ticker: item for item in result.results}
    assert by_ticker["HIST"].inclusion_state == "historical"
    assert conn.execute(
        "SELECT outcome FROM v_legacy_issuer_bindings_current "
        "WHERE recorded_issuer_id = 'legacy-ticker:HIST'"
    ).fetchone() == ("selected",)
    historical_issuer = by_ticker["HIST"].canonical_issuer_id
    assert historical_issuer is not None
    assert conn.execute(
        "SELECT COUNT(*) FROM issuer_reporting_scope_revisions WHERE issuer_id = ?",
        (historical_issuer,),
    ).fetchone() == (0,)
    assert conn.execute(
        "SELECT issuer_id FROM v_evidence_document_versions_canonical "
        "WHERE document_version_id = 'historical-document'"
    ).fetchone() == (historical_issuer,)
    conn.close()


def test_company_ticker_entries_are_closed_and_cik_is_validated() -> None:
    with pytest.raises(SecCompanyTickerContractError):
        parse_sec_company_tickers(
            _body(
                {
                    "cik_str": 123,
                    "ticker": "ACME",
                    "title": "Acme",
                    "unexpected": True,
                }
            )
        )
    with pytest.raises(SecCompanyTickerContractError):
        parse_sec_company_tickers(
            _body({"cik_str": "not-a-cik", "ticker": "ACME", "title": "Acme"})
        )


def test_sec_authority_fetch_is_exactly_one_request() -> None:
    class _Response:
        status_code = 200
        content = b"{}"

    class _Session:
        def __init__(self) -> None:
            self.calls = 0

        def get(
            self,
            url: str,
            *,
            headers: dict[str, str],
            timeout: tuple[int, int],
        ) -> _Response:
            assert url == SOURCE_URL
            assert headers["User-Agent"] == "Analyst analyst@example.test"
            assert timeout == (10, 60)
            self.calls += 1
            return _Response()

    session = _Session()
    assert (
        fetch_sec_company_tickers(
            session,
            source_url=SOURCE_URL,
            user_agent="Analyst analyst@example.test",
        )
        == b"{}"
    )
    assert session.calls == 1
