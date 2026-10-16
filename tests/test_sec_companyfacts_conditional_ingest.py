"""Conditional SEC CompanyFacts ingest: skips, receipts, resumption, probe hoist.

Fixture-first validation for the conditional fetch contract (VAL-PIPE-001 /
002 / 008):

- an unchanged payload performs zero downloads, zero pydantic re-parses, and
  zero fact re-walks on the second run, proved by transport/parse/walk
  counters plus the per-ticker completion receipt and the skip log receipt;
- a changed payload reprocesses exactly that ticker;
- an interrupted run resumes from per-ticker completion receipts without
  redoing completed tickers (the failed ticker commits no receipt);
- the per-fact PRAGMA table_info probes are hoisted to once per ingest;
- the write transaction covers only the bounded persistence batch — the
  parse and the fact-walk selection run before it opens.

No real network: every transport is a fake; replay uses the
``local_payload`` seam (exact retained bytes, zero transport calls).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from execution import fetch_sec_xbrl as fetch_sec_xbrl_execution
from pipeline import sec_xbrl
from pipeline.restatement_detector import FinancialFactSchemaProfile
from pipeline.sec_xbrl import (
    CompanyFactsNotModified,
    CompanyFactsSelection,
    CompanyFactsValidators,
    FactAdmissionTimingPhase,
    FetchedCompanyFacts,
    IngestStats,
    fetch_receipts_table_present,
    ingest_for_ticker,
    load_companyfacts_fetch_receipt,
)
from provenance.issuer_registry import (
    IdentifierAssertion,
    IdentifierResolution,
    IssuerEntity,
    IssuerRegistry,
    identifier_candidate_digest,
)

STAMP = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)
CIK_ACME = "0000000001"
CIK_BECO = "0000000002"
ACCESSION_ONE = "0000000001-26-000001"
ACCESSION_TWO = "0000000001-26-000002"
ACCESSION_THREE = "0000000001-26-000003"
RETRIEVAL_LATER = STAMP + timedelta(hours=1)


def _database(
    tmp_path: Path,
    migrated_db: Callable[..., Path],
    name: str,
) -> sqlite3.Connection:
    path = tmp_path / name
    assert callable(migrated_db)
    migrated_db(path, target="head")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _seed_issuer(conn: sqlite3.Connection, *, issuer_id: str, cik: str) -> None:
    registry = IssuerRegistry(conn)
    registry.persist(
        IssuerEntity(
            issuer_id=issuer_id,
            idempotency_key=issuer_id,
            entity_kind="operating_company",
            created_at=STAMP,
        )
    )
    assertion = IdentifierAssertion(
        assertion_id=f"assertion-{cik}",
        idempotency_key=f"assertion-{cik}",
        issuer_id=issuer_id,
        identifier_type="sec_cik",
        identifier_value=cik,
        normalized_value=cik,
        authority="manual",
        source_observation_id=None,
        effective_at=STAMP,
        knowledge_at=STAMP,
        recorded_at=STAMP,
    )
    registry.persist(assertion)
    registry.persist(
        IdentifierResolution(
            resolution_id=f"resolution-{cik}",
            idempotency_key=f"resolution-{cik}",
            resolution_key=f"sec_cik:{cik}",
            revision=1,
            outcome="selected",
            selected_assertion_id=assertion.assertion_id,
            candidate_digest_sha256=identifier_candidate_digest((assertion,)),
            policy_name="test",
            policy_version="1",
            policy_config_sha256=hashlib.sha256(b"test-policy").hexdigest(),
            material_dissent=False,
            reason_code="manual_test_identity",
            reason_details=(("source", "test"),),
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
            supersedes_resolution_id=None,
        )
    )
    conn.commit()


def _tracked(conn: sqlite3.Connection, ticker: str) -> None:
    conn.execute(
        "INSERT INTO tracked_companies (ticker, name, list_type) VALUES (?, ?, 'evaluation')",
        (ticker, ticker.title()),
    )
    conn.commit()


def _body(
    *,
    cik: int,
    second_value: int = 200,
    second_accession: str = ACCESSION_TWO,
) -> bytes:
    payload = {
        "cik": cik,
        "entityName": f"Corporation {cik}",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "label": "Revenue",
                    "description": "Revenue recognized.",
                    "units": {
                        "USD": [
                            {
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "val": 100,
                                "accn": ACCESSION_ONE,
                                "fy": 2025,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2026-02-01",
                                "frame": "CY2025",
                            },
                            {
                                "start": "2026-04-01",
                                "end": "2026-06-30",
                                "val": second_value,
                                "accn": second_accession,
                                "fy": 2026,
                                "fp": "Q2",
                                "form": "10-Q",
                                "filed": "2026-07-20",
                                "frame": "CY2026Q2",
                            },
                        ]
                    },
                }
            }
        },
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _fetched(
    body: bytes,
    *,
    cik: str,
    retrieved_at: datetime = STAMP,
    etag: str | None = None,
    last_modified: str | None = None,
) -> FetchedCompanyFacts:
    return FetchedCompanyFacts(
        source_url=f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json",
        raw_body=body,
        observed_at=retrieved_at - timedelta(seconds=1),
        retrieved_at=retrieved_at,
        etag=etag,
        last_modified=last_modified,
    )


def _seed_ciks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sec_xbrl.CIK_MAP, "ACME", CIK_ACME)
    monkeypatch.setitem(sec_xbrl.CIK_MAP, "BECO", CIK_BECO)


class _CallCounter:
    """Wraps one public seam, counting calls and forwarding to the real one."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        module: object,
        name: str,
        original: Callable[..., object],
    ) -> None:
        self.calls = 0
        self.target = original
        monkeypatch.setattr(module, name, self)

    def __call__(self, *args: object, **kwargs: object) -> object:
        self.calls += 1
        return self.target(*args, **kwargs)


class _TransactionObserver:
    """Wraps one public seam, recording the ingest connection's txn state."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        module: object,
        name: str,
        original: Callable[..., object],
        *,
        conn: sqlite3.Connection,
    ) -> None:
        self.in_transaction: list[bool] = []
        self._conn = conn
        self.target = original
        monkeypatch.setattr(module, name, self)

    def __call__(self, *args: object, **kwargs: object) -> object:
        self.in_transaction.append(bool(self._conn.in_transaction))
        return self.target(*args, **kwargs)


def _receipt_row(conn: sqlite3.Connection, ticker: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT ticker, normalized_cik, payload_sha256, etag, last_modified, run_id "
        "FROM sec_companyfacts_fetch_receipts WHERE ticker = ?",
        (ticker,),
    ).fetchone()


def _fact_rows(conn: sqlite3.Connection, ticker: str) -> list[tuple[int, int | float, str, str]]:
    return [
        (
            int(row[0]),
            int(row[1]) if isinstance(row[1], int) else float(row[1]),
            str(row[2]),
            str(row[3]),
        )
        for row in conn.execute(
            "SELECT id, value, period_end, line_item FROM financial_facts "
            "WHERE ticker = ? ORDER BY id",
            (ticker,),
        ).fetchall()
    ]


def test_unchanged_payload_replay_does_zero_downloads_parses_and_walks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> None:
    """VAL-PIPE-001: identical payload on the second run skips everything.

    The second run replays the exact retained bytes through the
    ``local_payload`` seam: the transport raises if touched, the parse
    counter and the fact-walk counter stay flat, no fact row changes, and
    the completion receipt proves the skip against the same digest.
    """
    conn = _database(tmp_path, migrated_db, "conditional-replay.db")
    _seed_issuer(conn, issuer_id="issuer-acme", cik=CIK_ACME)
    _tracked(conn, "ACME")
    _seed_ciks(monkeypatch)
    body = _body(cik=1)
    digest = hashlib.sha256(body).hexdigest()

    def _forbidden_transport(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("unchanged replay must not touch the transport")

    monkeypatch.setattr(sec_xbrl, "fetch_companyfacts", _forbidden_transport)
    parse_calls = _CallCounter(
        monkeypatch, sec_xbrl, "parse_companyfacts_body", sec_xbrl.parse_companyfacts_body
    )
    walk_calls = _CallCounter(
        monkeypatch,
        sec_xbrl,
        "select_facts_from_companyfacts",
        sec_xbrl.select_facts_from_companyfacts,
    )

    first = ingest_for_ticker(
        conn,
        ticker="ACME",
        project_root=tmp_path,
        local_payload=_fetched(body, cik=CIK_ACME, etag='W/"v1"'),
    )
    facts_before = _fact_rows(conn, "ACME")
    observations_before = conn.execute(
        "SELECT COUNT(*) FROM evidence_source_observations"
    ).fetchone()[0]
    assert first.disposition == "ingested"
    assert first.accessions_inserted == 2
    assert first.facts_inserted == 2
    assert parse_calls.calls == 1
    assert walk_calls.calls == 1

    second = ingest_for_ticker(
        conn,
        ticker="ACME",
        project_root=tmp_path,
        run_id="retry-run",
        local_payload=_fetched(body, cik=CIK_ACME, retrieved_at=RETRIEVAL_LATER),
    )
    assert second.disposition == "skipped_unchanged"
    assert second.skip_reason == "digest_match"
    assert second.payload_sha256 == digest
    assert second.accessions_inserted == 0
    assert second.facts_inserted == 0
    assert parse_calls.calls == 1, "unchanged payload must not re-parse"
    assert walk_calls.calls == 1, "unchanged payload must not re-walk"
    assert _fact_rows(conn, "ACME") == facts_before
    # The unchanged retrieval is a positive capture-boundary observation.
    assert (
        conn.execute("SELECT COUNT(*) FROM evidence_source_observations").fetchone()[0]
        == observations_before + 1
    )
    receipt = _receipt_row(conn, "ACME")
    assert receipt is not None
    assert receipt["payload_sha256"] == digest
    assert receipt["normalized_cik"] == CIK_ACME
    # A replay without server validators keeps the first run's stored ones.
    assert receipt["etag"] == 'W/"v1"'
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_conditional_get_returns_not_modified_and_skips_without_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> None:
    """VAL-PIPE-001: a 304 answers the conditional GET; no bytes are parsed."""
    conn = _database(tmp_path, migrated_db, "conditional-304.db")
    _seed_issuer(conn, issuer_id="issuer-acme", cik=CIK_ACME)
    _tracked(conn, "ACME")
    _seed_ciks(monkeypatch)
    body = _body(cik=1)
    digest = hashlib.sha256(body).hexdigest()
    transport_calls: list[CompanyFactsValidators | None] = []

    def _conditional_fetch(
        cik: str,
        *,
        validators: CompanyFactsValidators | None = None,
    ) -> FetchedCompanyFacts | CompanyFactsNotModified:
        transport_calls.append(validators)
        if len(transport_calls) == 1:
            return _fetched(
                body,
                cik=cik,
                etag='W/"v1"',
                last_modified="Mon, 27 Jul 2026 12:00:00 GMT",
            )
        assert validators is not None and validators.present, (
            "the second GET must be conditional on the receipt's validators"
        )
        return CompanyFactsNotModified(
            source_url=f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json",
            observed_at=RETRIEVAL_LATER - timedelta(seconds=1),
            retrieved_at=RETRIEVAL_LATER,
        )

    monkeypatch.setattr(sec_xbrl, "fetch_companyfacts", _conditional_fetch)
    parse_calls = _CallCounter(
        monkeypatch, sec_xbrl, "parse_companyfacts_body", sec_xbrl.parse_companyfacts_body
    )
    walk_calls = _CallCounter(
        monkeypatch,
        sec_xbrl,
        "select_facts_from_companyfacts",
        sec_xbrl.select_facts_from_companyfacts,
    )
    try:
        first = ingest_for_ticker(conn, ticker="ACME", project_root=tmp_path)
        assert first.disposition == "ingested"
        assert parse_calls.calls == 1
        receipt = load_companyfacts_fetch_receipt(conn, ticker="ACME")
        assert receipt is not None
        assert receipt.validators == CompanyFactsValidators(
            etag='W/"v1"',
            last_modified="Mon, 27 Jul 2026 12:00:00 GMT",
        )

        facts_before = _fact_rows(conn, "ACME")
        second = ingest_for_ticker(
            conn,
            ticker="ACME",
            project_root=tmp_path,
            run_id="retry-run",
        )
        assert second.disposition == "skipped_unchanged"
        assert second.skip_reason == "not_modified"
        assert second.payload_sha256 == digest
        assert second.accessions_inserted == 0
        assert second.facts_inserted == 0
        assert len(transport_calls) == 2
        assert parse_calls.calls == 1, "a 304 must not reach the parser"
        assert walk_calls.calls == 1, "a 304 must not re-walk facts"
        assert _fact_rows(conn, "ACME") == facts_before
    finally:
        conn.close()


def test_changed_payload_reprocesses_exactly_that_ticker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> None:
    """VAL-PIPE-001: a mutated payload reprocesses only its own ticker."""
    conn = _database(tmp_path, migrated_db, "conditional-mutated.db")
    _seed_issuer(conn, issuer_id="issuer-acme", cik=CIK_ACME)
    _seed_issuer(conn, issuer_id="issuer-beco", cik=CIK_BECO)
    _tracked(conn, "ACME")
    _tracked(conn, "BECO")
    _seed_ciks(monkeypatch)
    acme_body = _body(cik=1)
    beco_body = _body(cik=2)
    responses = iter(
        (
            _fetched(acme_body, cik=CIK_ACME),
            _fetched(beco_body, cik=CIK_BECO),
        )
    )

    def _fetch(_cik: str) -> FetchedCompanyFacts:
        return next(responses)

    monkeypatch.setattr(sec_xbrl, "fetch_companyfacts", _fetch)
    try:
        assert ingest_for_ticker(conn, ticker="ACME", project_root=tmp_path).facts_inserted == 2
        assert ingest_for_ticker(conn, ticker="BECO", project_root=tmp_path).facts_inserted == 2
        acme_facts_before = _fact_rows(conn, "ACME")
        beco_facts_before = _fact_rows(conn, "BECO")

        # BECO files an amendment: same period, corrected value, new accession.
        amended_body = _body(cik=2, second_value=201, second_accession=ACCESSION_THREE)
        retry = {
            "ACME": _fetched(acme_body, cik=CIK_ACME, retrieved_at=RETRIEVAL_LATER),
            "BECO": _fetched(amended_body, cik=CIK_BECO, retrieved_at=RETRIEVAL_LATER),
        }
        acme_stats = ingest_for_ticker(
            conn,
            ticker="ACME",
            project_root=tmp_path,
            run_id="retry-run",
            local_payload=retry["ACME"],
        )
        beco_stats = ingest_for_ticker(
            conn,
            ticker="BECO",
            project_root=tmp_path,
            run_id="retry-run",
            local_payload=retry["BECO"],
        )

        assert acme_stats.disposition == "skipped_unchanged"
        assert acme_stats.skip_reason == "digest_match"
        assert acme_stats.facts_inserted == 0
        assert _fact_rows(conn, "ACME") == acme_facts_before, (
            "an unchanged ticker must gain no fact rows"
        )

        assert beco_stats.disposition == "ingested"
        # The changed aggregate payload gets a new snapshot document, so its
        # full fact set re-registers under it (existing aggregate-snapshot
        # behavior); each re-registered observation supersedes its prior
        # current counterpart instead of erasing it.
        assert beco_stats.facts_inserted == 2
        beco_after = _fact_rows(conn, "BECO")
        assert beco_after != beco_facts_before
        assert [int(row[1]) for row in beco_after] == [100, 200, 100, 201]
        supersede_links = conn.execute(
            "SELECT value, supersedes_id FROM financial_facts WHERE ticker = 'BECO' "
            "AND supersedes_id IS NOT NULL ORDER BY id"
        ).fetchall()
        assert [(int(row[0]), int(row[1])) for row in supersede_links] == [
            (100, int(beco_facts_before[0][0])),
            (201, int(beco_facts_before[1][0])),
        ]
        beco_receipt = _receipt_row(conn, "BECO")
        assert beco_receipt is not None
        assert beco_receipt["payload_sha256"] == hashlib.sha256(amended_body).hexdigest()
        acme_receipt = _receipt_row(conn, "ACME")
        assert acme_receipt is not None
        assert acme_receipt["payload_sha256"] == hashlib.sha256(acme_body).hexdigest()
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_interrupted_run_resumes_from_receipts_without_redoing_completed_tickers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> None:
    """VAL-PIPE-002: receipts mark the completed prefix; retry heals the rest."""
    conn = _database(tmp_path, migrated_db, "conditional-resume.db")
    _seed_issuer(conn, issuer_id="issuer-acme", cik=CIK_ACME)
    _seed_issuer(conn, issuer_id="issuer-beco", cik=CIK_BECO)
    _tracked(conn, "ACME")
    _tracked(conn, "BECO")
    _seed_ciks(monkeypatch)
    acme_body = _body(cik=1)
    beco_body = _body(cik=2)
    fail_beco = True
    real_persist = sec_xbrl.persist_facts_from_companyfacts

    def _interrupting_persist(
        conn: sqlite3.Connection,
        selection: CompanyFactsSelection,
        *,
        ticker: str,
        accession_to_doc_id: Mapping[str, int],
        run_id: str = "sec_xbrl_ingest",
        snapshot_root: Path | None = None,
        schema_profile: FinancialFactSchemaProfile | None = None,
        timing_sink: Callable[[FactAdmissionTimingPhase, int], None] | None = None,
        timing_phase_started: Callable[[FactAdmissionTimingPhase], None] | None = None,
        monotonic_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> int:
        if fail_beco and ticker == "BECO":
            raise RuntimeError("forced interruption after selection")
        return real_persist(
            conn,
            selection,
            ticker=ticker,
            accession_to_doc_id=accession_to_doc_id,
            run_id=run_id,
            snapshot_root=snapshot_root,
            schema_profile=schema_profile,
            timing_sink=timing_sink,
            timing_phase_started=timing_phase_started,
            monotonic_ns=monotonic_ns,
        )

    def _fetch(cik: str) -> FetchedCompanyFacts:
        if cik == CIK_ACME:
            return _fetched(acme_body, cik=CIK_ACME)
        return _fetched(beco_body, cik=CIK_BECO)

    monkeypatch.setattr(sec_xbrl, "persist_facts_from_companyfacts", _interrupting_persist)
    monkeypatch.setattr(sec_xbrl, "fetch_companyfacts", _fetch)
    try:
        ingest_for_ticker(conn, ticker="ACME", project_root=tmp_path)
        with pytest.raises(RuntimeError, match="forced interruption"):
            ingest_for_ticker(conn, ticker="BECO", project_root=tmp_path)

        # The interrupted ticker committed nothing: no snapshot document, no
        # facts, and — the resumption contract — no completion receipt.
        assert _receipt_row(conn, "ACME") is not None
        assert _receipt_row(conn, "BECO") is None
        assert _fact_rows(conn, "BECO") == []
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM documents WHERE ticker = 'BECO' "
                "AND doc_type = 'sec_companyfacts_snapshot'"
            ).fetchone()[0]
            == 0
        )

        # Retry pass over the full ticker set: only the incomplete ticker
        # re-runs the walk; the completed one is skipped by receipt.
        fail_beco = False
        walk_calls = _CallCounter(
            monkeypatch,
            sec_xbrl,
            "select_facts_from_companyfacts",
            sec_xbrl.select_facts_from_companyfacts,
        )
        parse_calls = _CallCounter(
            monkeypatch, sec_xbrl, "parse_companyfacts_body", sec_xbrl.parse_companyfacts_body
        )
        acme_stats = ingest_for_ticker(
            conn,
            ticker="ACME",
            project_root=tmp_path,
            run_id="retry-run",
            local_payload=_fetched(acme_body, cik=CIK_ACME, retrieved_at=RETRIEVAL_LATER),
        )
        beco_stats = ingest_for_ticker(
            conn,
            ticker="BECO",
            project_root=tmp_path,
            run_id="retry-run",
            local_payload=_fetched(beco_body, cik=CIK_BECO, retrieved_at=RETRIEVAL_LATER),
        )
        assert acme_stats.disposition == "skipped_unchanged"
        assert acme_stats.skip_reason == "digest_match"
        assert beco_stats.disposition == "ingested"
        assert beco_stats.accessions_inserted == 2
        assert beco_stats.facts_inserted == 2
        assert walk_calls.calls == 1, "retry must re-walk only the incomplete ticker"
        assert parse_calls.calls == 1, "retry must re-parse only the incomplete ticker"
        assert len(_fact_rows(conn, "BECO")) == 2
        assert _receipt_row(conn, "BECO") is not None
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_schema_probes_run_once_per_ingest_not_once_per_fact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> None:
    """VAL-PIPE-008: the PRAGMA table_info profile is hoisted per ingest."""
    conn = _database(tmp_path, migrated_db, "conditional-probe.db")
    _seed_issuer(conn, issuer_id="issuer-acme", cik=CIK_ACME)
    _tracked(conn, "ACME")
    _seed_ciks(monkeypatch)
    body = _body(cik=1)
    amended_body = _body(cik=1, second_value=201, second_accession=ACCESSION_THREE)
    responses = iter((_fetched(body, cik=CIK_ACME), _fetched(amended_body, cik=CIK_ACME)))

    def _fetch(_cik: str) -> FetchedCompanyFacts:
        return next(responses)

    monkeypatch.setattr(sec_xbrl, "fetch_companyfacts", _fetch)
    probe_calls = _CallCounter(
        monkeypatch, sec_xbrl, "probe_financial_fact_schema", sec_xbrl.probe_financial_fact_schema
    )
    try:
        first = ingest_for_ticker(conn, ticker="ACME", project_root=tmp_path)
        assert first.facts_inserted == 2
        assert probe_calls.calls == 1, "one probe per ingest, not one per fact"

        second = ingest_for_ticker(conn, ticker="ACME", project_root=tmp_path)
        assert second.disposition == "ingested"
        assert probe_calls.calls == 2, "each ingest probes once, still never per fact"
    finally:
        conn.close()


def test_parse_and_fact_walk_run_outside_the_write_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> None:
    """VAL-PIPE-008: the write transaction covers only the write batch."""
    conn = _database(tmp_path, migrated_db, "conditional-transaction.db")
    _seed_issuer(conn, issuer_id="issuer-acme", cik=CIK_ACME)
    _tracked(conn, "ACME")
    _seed_ciks(monkeypatch)

    def _fetch(_cik: str) -> FetchedCompanyFacts:
        return _fetched(_body(cik=1), cik=CIK_ACME)

    monkeypatch.setattr(sec_xbrl, "fetch_companyfacts", _fetch)
    parse_observer = _TransactionObserver(
        monkeypatch,
        sec_xbrl,
        "parse_companyfacts_body",
        sec_xbrl.parse_companyfacts_body,
        conn=conn,
    )
    walk_observer = _TransactionObserver(
        monkeypatch,
        sec_xbrl,
        "select_facts_from_companyfacts",
        sec_xbrl.select_facts_from_companyfacts,
        conn=conn,
    )
    persist_observer = _TransactionObserver(
        monkeypatch,
        sec_xbrl,
        "persist_facts_from_companyfacts",
        sec_xbrl.persist_facts_from_companyfacts,
        conn=conn,
    )
    try:
        stats = ingest_for_ticker(conn, ticker="ACME", project_root=tmp_path)
        assert stats.disposition == "ingested"
        assert parse_observer.in_transaction == [False], (
            "the pydantic parse must run before the write transaction opens"
        )
        assert walk_observer.in_transaction == [False], (
            "the fact walk must run before the write transaction opens"
        )
        assert persist_observer.in_transaction == [True], (
            "the bounded write batch is what the transaction covers"
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_replay_without_receipts_table_reprocesses_without_extractor_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
) -> None:
    """Schema tolerance: no 0050 table means no validators, never a broken ingest."""
    conn = _database(tmp_path, migrated_db, "conditional-legacy.db")
    conn.execute("DROP TABLE sec_companyfacts_fetch_receipts")
    conn.commit()
    _seed_issuer(conn, issuer_id="issuer-acme", cik=CIK_ACME)
    _tracked(conn, "ACME")
    _seed_ciks(monkeypatch)
    body = _body(cik=1)

    def _fetch(_cik: str) -> FetchedCompanyFacts:
        return _fetched(body, cik=CIK_ACME)

    monkeypatch.setattr(sec_xbrl, "fetch_companyfacts", _fetch)
    try:
        assert ingest_for_ticker(conn, ticker="ACME", project_root=tmp_path).disposition == (
            "ingested"
        )
        assert fetch_receipts_table_present(conn) is False
        assert load_companyfacts_fetch_receipt(conn, ticker="ACME") is None

        replay = ingest_for_ticker(
            conn,
            ticker="ACME",
            project_root=tmp_path,
            local_payload=_fetched(body, cik=CIK_ACME, retrieved_at=RETRIEVAL_LATER),
        )
        assert replay.disposition == "ingested"
        assert replay.skip_reason is None
        assert replay.facts_inserted == 0
    finally:
        conn.close()


def test_driver_reports_skip_rows_events_and_summary_without_staleness_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The driver turns skip stats into explicit receipts, never staleness."""
    conn = sqlite3.connect(":memory:")

    def _open_db(_path: Path) -> sqlite3.Connection:
        return conn

    def _tickers(_args: object, _conn: sqlite3.Connection) -> list[str]:
        return ["NU"]

    def _begin_run(*_args: object, **_kwargs: object) -> str:
        return "retry-run"

    def _end_run(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(fetch_sec_xbrl_execution, "open_db", _open_db)
    monkeypatch.setattr(fetch_sec_xbrl_execution, "_resolve_tickers", _tickers)
    monkeypatch.setattr(fetch_sec_xbrl_execution, "start_run", _begin_run)
    monkeypatch.setattr(fetch_sec_xbrl_execution, "end_run", _end_run)

    def _unexpected_staleness(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("an unchanged skip is never silent staleness")

    monkeypatch.setattr(
        fetch_sec_xbrl_execution,
        "handle_silent_staleness",
        _unexpected_staleness,
    )

    def _skip_ingest(
        _conn: sqlite3.Connection,
        *,
        ticker: str,
        project_root: object,
        run_id: str,
        timing_sink: object,
    ) -> IngestStats:
        del project_root, run_id, timing_sink
        return IngestStats(
            accessions_inserted=0,
            facts_inserted=0,
            disposition="skipped_unchanged",
            skip_reason="not_modified",
            payload_sha256=hashlib.sha256(b"nu-payload").hexdigest(),
        )

    monkeypatch.setattr(fetch_sec_xbrl_execution, "ingest_for_ticker", _skip_ingest)
    database = tmp_path / "driver-fixture.db"
    database.touch()
    monkeypatch.setattr(sys, "argv", ["fetch_sec_xbrl.py", "--db", str(database), "--ticker", "NU"])

    assert fetch_sec_xbrl_execution.main() == 0
    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert summary["skipped_unchanged"] == 1
    assert summary["total_accessions_registered"] == 0
    assert summary["total_facts_inserted"] == 0
    assert summary["failed"] == 0
    row = summary["rows"][0]
    assert row["ticker"] == "NU"
    assert row["disposition"] == "skipped_unchanged"
    assert row["skip_reason"] == "not_modified"
    assert row["payload_sha256"] == hashlib.sha256(b"nu-payload").hexdigest()
    assert "silent_staleness" not in row
    assert "brief_dirty_forced" not in row

    events = [json.loads(line) for line in captured.err.splitlines()]
    assert events == [
        {
            "event": "sec_companyfacts_unchanged_skip",
            "run_id": "retry-run",
            "ticker": "NU",
            "disposition": "skipped_unchanged",
            "reason": "not_modified",
            "payload_sha256": hashlib.sha256(b"nu-payload").hexdigest(),
            "accessions_registered": 0,
            "facts_inserted": 0,
        }
    ]


@pytest.mark.parametrize("transport", ["digest", "304", "legacy_identity"])
def test_changed_extractor_reprocesses_identical_payload_before_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Callable[..., Path],
    transport: str,
) -> None:
    conn = _database(tmp_path, migrated_db, "changed-extractor.db")
    _seed_issuer(conn, issuer_id="issuer-acme", cik=CIK_ACME)
    _tracked(conn, "ACME")
    _seed_ciks(monkeypatch)
    body = _body(cik=1)
    first = _fetched(body, cik=CIK_ACME, etag='W/"v1"')
    calls: list[CompanyFactsValidators | None] = []

    def fetch(
        cik: str, *, validators: CompanyFactsValidators | None = None
    ) -> FetchedCompanyFacts | CompanyFactsNotModified:
        calls.append(validators)
        if validators is not None:
            return CompanyFactsNotModified(
                source_url=f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json",
                observed_at=RETRIEVAL_LATER,
                retrieved_at=RETRIEVAL_LATER,
            )
        return _fetched(body, cik=cik, retrieved_at=RETRIEVAL_LATER)

    try:
        ingest_for_ticker(conn, ticker="ACME", project_root=tmp_path, local_payload=first)
        original = load_companyfacts_fetch_receipt(conn, ticker="ACME")
        assert original is not None
        assert original.extractor_sha256 == sec_xbrl.companyfacts_extractor_sha256()
        if transport == "legacy_identity":
            conn.execute("UPDATE sec_companyfacts_fetch_receipts SET extractor_sha256=NULL")
            conn.commit()
        else:
            monkeypatch.setattr(sec_xbrl, "TAG_LADDERS", tuple(reversed(sec_xbrl.TAG_LADDERS)))
            assert sec_xbrl.companyfacts_extractor_sha256() != original.extractor_sha256
        parses = _CallCounter(
            monkeypatch, sec_xbrl, "parse_companyfacts_body", sec_xbrl.parse_companyfacts_body
        )
        walks = _CallCounter(
            monkeypatch,
            sec_xbrl,
            "select_facts_from_companyfacts",
            sec_xbrl.select_facts_from_companyfacts,
        )
        monkeypatch.setattr(sec_xbrl, "fetch_companyfacts", fetch)
        replay = ingest_for_ticker(
            conn,
            ticker="ACME",
            project_root=tmp_path,
            local_payload=None
            if transport == "304"
            else _fetched(body, cik=CIK_ACME, retrieved_at=RETRIEVAL_LATER),
        )
        assert replay.disposition == "ingested"
        assert replay.skip_reason is None
        assert parses.calls == 1
        assert walks.calls == 1
        if transport == "304":
            assert len(calls) == 2
            assert calls[0] is not None
            assert calls[1] is None
        updated = load_companyfacts_fetch_receipt(conn, ticker="ACME")
        assert updated is not None
        assert updated.extractor_sha256 == sec_xbrl.companyfacts_extractor_sha256()
        stable = ingest_for_ticker(
            conn,
            ticker="ACME",
            project_root=tmp_path,
            local_payload=_fetched(
                body, cik=CIK_ACME, retrieved_at=RETRIEVAL_LATER + timedelta(hours=1)
            ),
        )
        assert stable.disposition == "skipped_unchanged"
        assert parses.calls == 1
        assert walks.calls == 1
    finally:
        conn.close()
