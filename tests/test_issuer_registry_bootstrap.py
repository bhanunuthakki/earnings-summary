"""Canonical issuer-registry bootstrap from the SEC company-ticker authority."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

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

HEAD = "0230_evidence_subject_bindings"
STAMP = datetime(2026, 7, 27, 21, 0, tzinfo=UTC)
SOURCE_URL = "https://www.sec.gov/files/company_tickers.json"


def _database(tmp_path: Path, migrated_db: Callable[..., Path]) -> Path:
    path = tmp_path / "bootstrap.db"
    migrated_db(path, stamp="0213_decision_draft_provider_id", archived=True, target=HEAD)
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


def test_dry_run_is_read_only_and_excludes_index_members(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = _database(tmp_path, migrated_db)
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
    migrated_db: Callable[..., Path],
) -> None:
    db_path = _database(tmp_path, migrated_db)
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

    assert first.records_created == 21
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
        "FROM source_obligation_revisions ORDER BY document_family,authority_kind"
    ).fetchall() == [
        ("sec_edgar", "continuous_disclosure", "required"),
        ("issuer_publisher", "issuer_earnings_materials", "required"),
        ("issuer_publisher", "issuer_financial_statements", "required"),
        ("sec_edgar", "issuer_financial_statements", "required"),
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
    migrated_db: Callable[..., Path],
) -> None:
    db_path = _database(tmp_path, migrated_db)
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
    assert conn.execute("SELECT COUNT(*) FROM source_obligation_revisions").fetchone() == (6,)
    conn.close()


def test_document_persistence_auto_binds_resolved_sec_cik_subject(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    db_path = _database(tmp_path, migrated_db)
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
    migrated_db: Callable[..., Path],
) -> None:
    db_path = _database(tmp_path, migrated_db)
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
    migrated_db: Callable[..., Path],
) -> None:
    db_path = _database(tmp_path, migrated_db)
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
    migrated_db: Callable[..., Path],
) -> None:
    db_path = _database(tmp_path, migrated_db)
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


def test_selected_ticker_scope_does_not_bootstrap_other_active_issuers(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = _database(tmp_path, migrated_db)
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO tracked_companies VALUES (?, 'bhanu', ?, ?, ?, NULL)",
        ((1, "ONON", "On", "watchlist"), (2, "OTHER", "Other", "portfolio")),
    )
    conn.commit()
    raw = _body(
        {"cik_str": 1858985, "ticker": "ONON", "title": "On Holding AG"},
        {"cik_str": 999, "ticker": "OTHER", "title": "Other Corp"},
    )
    request = BootstrapRequest(
        source_url=SOURCE_URL,
        blob_root=tmp_path / "blobs",
        apply=True,
        recorded_at=STAMP,
        ticker_scope=(" onon ",),
    )
    result = bootstrap_issuer_reporting_registry(conn, raw_body=raw, request=request)
    assert result.selected_tickers == ("ONON",)
    assert len(result.results) == 1
    assert result.results[0].normalized_cik == "0001858985"
    assert conn.execute("SELECT legal_name FROM issuer_profile_revisions").fetchall() == [
        ("On Holding AG",)
    ]
    replay = bootstrap_issuer_reporting_registry(conn, raw_body=raw, request=request)
    assert replay.records_created == 0
    conn.close()


def test_selected_ticker_scope_cannot_add_an_untracked_security(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> None:
    db_path = _database(tmp_path, migrated_db)
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    request = BootstrapRequest(
        source_url=SOURCE_URL,
        blob_root=tmp_path / "blobs",
        apply=False,
        recorded_at=STAMP,
        ticker_scope=("ONON",),
    )
    with pytest.raises(ValueError, match="ticker_scope_not_in_reporting_universe"):
        bootstrap_issuer_reporting_registry(
            conn,
            raw_body=_body({"cik_str": 1858985, "ticker": "ONON", "title": "On Holding AG"}),
            request=request,
        )
    assert conn.execute("SELECT COUNT(*) FROM issuer_entities").fetchone()[0] == 0
    conn.close()


def test_ordinary_ticker_bootstrap_binds_both_subject_keys(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    from provenance.reporting_entity_registry import ReportingEntityRegistry

    path = _database(tmp_path, migrated_db)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO tracked_companies VALUES (1, 'bhanu', 'ACME', 'Acme', 'portfolio', NULL)"
    )
    conn.commit()
    raw = _body({"cik_str": 123456, "ticker": "ACME", "title": "Acme Corporation"})
    first = bootstrap_issuer_reporting_registry(
        conn, raw_body=raw, request=_request(tmp_path, apply=True)
    )
    issuer = first.results[0].canonical_issuer_id
    assert issuer is not None
    registry = ReportingEntityRegistry(conn)
    for key in ("legacy-ticker:ACME", issuer):
        subject = registry.canonicalize_recorded_subject(key, knowledge_at=STAMP)
        assert subject.issuer_id == issuer
        assert subject.reporting_entity_id == "reporting:sec:0000123456"
        assert subject.material_dissent is False
    before = conn.execute("SELECT COUNT(*) FROM recorded_subject_binding_revisions").fetchone()
    replay = bootstrap_issuer_reporting_registry(
        conn, raw_body=raw, request=_request(tmp_path, apply=True)
    )
    assert replay.records_created == 0
    assert (
        conn.execute("SELECT COUNT(*) FROM recorded_subject_binding_revisions").fetchone() == before
    )
    conn.close()


@pytest.mark.parametrize("key_kind", ["legacy", "canonical"])
@pytest.mark.parametrize("conflict", ["issuer", "reporting_entity", "dissent", "unresolved"])
def test_ticker_bootstrap_preserves_conflicting_subject_resolution(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    key_kind: str,
    conflict: str,
) -> None:
    from provenance.reporting_entity_registry import (
        EvidenceSubjectBindingRevision,
        ReportingEntity,
        ReportingEntityRegistry,
    )

    path = _database(tmp_path, migrated_db)
    conn = sqlite3.connect(path)
    conn.executemany(
        "INSERT INTO tracked_companies VALUES (?, 'bhanu', ?, ?, 'portfolio', NULL)",
        [(1, "ACME", "Acme"), (2, "OTHER", "Other")],
    )
    conn.commit()
    raw = _body(
        {"cik_str": 123456, "ticker": "ACME", "title": "Acme Corporation"},
        {"cik_str": 654321, "ticker": "OTHER", "title": "Other Corporation"},
    )
    initial = bootstrap_issuer_reporting_registry(
        conn, raw_body=raw, request=_request(tmp_path, apply=True)
    )
    issuers = {r.ticker: r.canonical_issuer_id for r in initial.results}
    issuer = issuers["ACME"]
    assert issuer is not None
    key = "legacy-ticker:ACME" if key_kind == "legacy" else issuer
    previous = conn.execute(
        "SELECT binding_revision_id,revision FROM recorded_subject_binding_revisions "
        "WHERE recorded_issuer_id=? ORDER BY revision DESC LIMIT 1",
        (key,),
    ).fetchone()
    revision = 1 if previous is None else int(previous[1]) + 1
    if conflict == "reporting_entity":
        ReportingEntityRegistry(conn).persist(
            ReportingEntity(
                reporting_entity_id="reporting:alternate-acme",
                idempotency_key="reporting:alternate-acme",
                issuer_id=issuer,
                reporting_entity_kind="legal_registrant",
                display_name="Alternate Acme registrant",
                created_at=STAMP,
            )
        )
    ReportingEntityRegistry(conn).persist(
        EvidenceSubjectBindingRevision(
            binding_revision_id="test-conflict",
            idempotency_key="test-conflict",
            recorded_issuer_id=key,
            revision=revision,
            issuer_id=None
            if conflict == "unresolved"
            else issuers["OTHER"]
            if conflict == "issuer"
            else issuer,
            reporting_entity_id=(
                None
                if conflict == "unresolved"
                else "reporting:alternate-acme"
                if conflict == "reporting_entity"
                else "reporting:sec:0000654321"
                if conflict == "issuer"
                else "reporting:sec:0000123456"
            ),
            security_id=None,
            outcome="unresolved" if conflict == "unresolved" else "selected",
            decision_kind="manual",
            reason_code="test_explicit_resolution",
            reason_details=(("test", conflict),),
            material_dissent=conflict == "dissent",
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
            supersedes_binding_revision_id=None if previous is None else str(previous[0]),
        )
    )
    conn.commit()
    before = conn.total_changes
    for apply in (False, True):
        with pytest.raises(SecCompanyTickerContractError, match=r"subject.*conflict"):
            bootstrap_issuer_reporting_registry(
                conn, raw_body=raw, request=_request(tmp_path, apply=apply)
            )
        assert conn.total_changes == before
    assert conn.execute(
        "SELECT binding_revision_id FROM recorded_subject_binding_revisions "
        "WHERE recorded_issuer_id=? ORDER BY revision DESC LIMIT 1",
        (key,),
    ).fetchone() == ("test-conflict",)
    conn.close()


def test_subject_binding_constraint_failure_rolls_back_whole_bootstrap(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    path = _database(tmp_path, migrated_db)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO tracked_companies VALUES (1, 'bhanu', 'ACME', 'Acme', 'portfolio', NULL)"
    )
    conn.execute(
        "CREATE TRIGGER reject_canonical_subject BEFORE INSERT ON recorded_subject_binding_revisions "
        "WHEN NEW.recorded_issuer_id NOT LIKE 'legacy-ticker:%' "
        "BEGIN SELECT RAISE(ABORT, 'synthetic subject constraint'); END"
    )
    conn.commit()
    raw = _body({"cik_str": 123456, "ticker": "ACME", "title": "Acme Corporation"})
    with pytest.raises(sqlite3.IntegrityError, match="synthetic subject constraint"):
        bootstrap_issuer_reporting_registry(
            conn, raw_body=raw, request=_request(tmp_path, apply=True)
        )
    assert conn.in_transaction is False
    for table in (
        "issuer_entities",
        "reporting_entities",
        "recorded_subject_binding_revisions",
        "evidence_source_observations",
        "source_obligation_revisions",
    ):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
    conn.close()


def test_ticker_bootstrap_preserves_existing_selected_security(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    from provenance.issuer_registry import IssuerRegistry, Security
    from provenance.reporting_entity_registry import (
        EvidenceSubjectBindingRevision,
        ReportingEntityRegistry,
        SecurityReportingEntityRevision,
    )

    path = _database(tmp_path, migrated_db)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO tracked_companies VALUES (1, 'bhanu', 'ACME', 'Acme', 'portfolio', NULL)"
    )
    conn.commit()
    raw = _body({"cik_str": 123456, "ticker": "ACME", "title": "Acme Corporation"})
    initial = bootstrap_issuer_reporting_registry(
        conn, raw_body=raw, request=_request(tmp_path, apply=True)
    )
    issuer = initial.results[0].canonical_issuer_id
    assert issuer is not None
    IssuerRegistry(conn).persist(
        Security(
            security_id="security:acme",
            idempotency_key="security:acme",
            issuer_id=issuer,
            security_kind="common_stock",
            created_at=STAMP,
        )
    )
    registry = ReportingEntityRegistry(conn)
    registry.persist(
        SecurityReportingEntityRevision(
            relationship_revision_id="security-acme-reporting",
            idempotency_key="security-acme-reporting",
            relationship_key="security:acme:reports-through",
            revision=1,
            security_id="security:acme",
            reporting_entity_id="reporting:sec:0000123456",
            relationship_kind="reports_through",
            decision_kind="manual",
            reason_code="test_selected_security",
            reason_details=(("test", "existing reporting relationship"),),
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    )
    old = conn.execute(
        "SELECT binding_revision_id,revision FROM recorded_subject_binding_revisions "
        "WHERE recorded_issuer_id='legacy-ticker:ACME' ORDER BY revision DESC LIMIT 1"
    ).fetchone()
    assert old is not None
    registry.persist(
        EvidenceSubjectBindingRevision(
            binding_revision_id="selected-security",
            idempotency_key="selected-security",
            recorded_issuer_id="legacy-ticker:ACME",
            revision=int(old[1]) + 1,
            issuer_id=issuer,
            reporting_entity_id="reporting:sec:0000123456",
            security_id="security:acme",
            outcome="selected",
            decision_kind="manual",
            reason_code="test_selected_security",
            reason_details=(("test", "preserve selection"),),
            material_dissent=False,
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
            supersedes_binding_revision_id=str(old[0]),
        )
    )
    conn.commit()
    replay = bootstrap_issuer_reporting_registry(
        conn, raw_body=raw, request=_request(tmp_path, apply=True)
    )
    assert replay.records_created == 0
    assert (
        registry.canonicalize_recorded_subject("legacy-ticker:ACME", knowledge_at=STAMP).security_id
        == "security:acme"
    )
    conn.close()


def test_new_subject_on_retained_authority_uses_current_knowledge_clock(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
) -> None:
    from datetime import timedelta

    from provenance.reporting_entity_registry import ReportingEntityRegistry

    path = _database(tmp_path, migrated_db)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO tracked_companies VALUES (1, 'bhanu', 'ACME', 'Acme', 'portfolio', NULL)"
    )
    conn.commit()
    raw = _body(
        {"cik_str": 123456, "ticker": "ACME", "title": "Acme Corporation"},
        {"cik_str": 123456, "ticker": "ALIAS", "title": "Acme Corporation"},
    )
    first = bootstrap_issuer_reporting_registry(
        conn, raw_body=raw, request=_request(tmp_path, apply=True)
    )
    conn.execute(
        "INSERT INTO tracked_companies VALUES (2, 'bhanu', 'ALIAS', 'Acme', 'portfolio', NULL)"
    )
    conn.commit()
    later = STAMP + timedelta(days=1)
    applied = bootstrap_issuer_reporting_registry(
        conn,
        raw_body=raw,
        request=_request(tmp_path, apply=True, recorded_at=later).model_copy(
            update={"ticker_scope": ("ALIAS",)}
        ),
    )
    assert applied.source_observation_id == first.source_observation_id
    registry = ReportingEntityRegistry(conn)
    with pytest.raises(LookupError, match="no selected resolution"):
        registry.canonicalize_recorded_subject("legacy-ticker:ALIAS", knowledge_at=STAMP)
    assert (
        registry.canonicalize_recorded_subject("legacy-ticker:ALIAS", knowledge_at=later).issuer_id
        == first.results[0].canonical_issuer_id
    )
    conn.close()
