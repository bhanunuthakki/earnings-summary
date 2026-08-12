"""Offline planning and bounded SEC CompanyFacts evidence-binding capture."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import requests
from alembic.config import Config

from alembic import command
from pipeline import sec_xbrl
from pipeline.sec_xbrl import FetchedCompanyFacts
from provenance.issuer_registry import (
    IdentifierAssertion,
    IdentifierResolution,
    IssuerEntity,
    IssuerRegistry,
    LegacyIssuerBindingRevision,
    identifier_candidate_digest,
)
from provenance.sec_companyfacts_binding_backfill import (
    CompanyFactsBackfillHardStopError,
    CompanyFactsBindingBackfillRequest,
    backfill_sec_companyfacts_bindings,
)

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime(2026, 7, 27, 19, 0, 0, tzinfo=UTC)
SHA = hashlib.sha256(b"test-policy").hexdigest()


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _database(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "isolated-companyfacts.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            source_type TEXT NOT NULL,
            doc_type TEXT NOT NULL,
            period_start DATETIME,
            period_end DATETIME,
            file_path TEXT NOT NULL,
            sha256 TEXT NOT NULL UNIQUE,
            fetched_at DATETIME NOT NULL,
            fetch_status TEXT NOT NULL,
            http_code INTEGER,
            raw_bytes_size INTEGER NOT NULL,
            source_url TEXT,
            parent_document_id INTEGER,
            source_quality_tier TEXT NOT NULL,
            accession_number TEXT,
            filing_date TEXT
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
            source_doc_id INTEGER NOT NULL REFERENCES documents(id),
            confidence REAL NOT NULL DEFAULT 1.0,
            extracted_by TEXT,
            supersedes_id INTEGER,
            locator TEXT
        );
        CREATE UNIQUE INDEX uq_financial_facts_provenance
        ON financial_facts (
            ticker, period_end, fiscal_period_type, line_item, source_doc_id
        );
        """
    )
    conn.commit()
    conn.close()
    config = _config(path)
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, "0231_legacy_document_evidence_bindings")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _seed_identity(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    cik: str,
    issuer_id: str,
) -> None:
    registry = IssuerRegistry(conn)
    registry.persist(
        IssuerEntity(
            issuer_id=issuer_id,
            idempotency_key=issuer_id,
            entity_kind="operating_company",
            created_at=STAMP,
        )
    )
    registry.persist(
        LegacyIssuerBindingRevision(
            binding_revision_id=f"binding-{ticker.lower()}",
            idempotency_key=f"binding-{ticker.lower()}",
            recorded_issuer_id=f"legacy-ticker:{ticker}",
            revision=1,
            issuer_id=issuer_id,
            outcome="selected",
            decision_kind="deterministic",
            reason_code="test_fixture",
            reason_details=(("ticker", ticker),),
            material_dissent=False,
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    )
    assertion = IdentifierAssertion(
        assertion_id=f"assert-{ticker.lower()}-cik",
        idempotency_key=f"assert-{ticker.lower()}-cik",
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
            resolution_id=f"resolve-{ticker.lower()}-cik",
            idempotency_key=f"resolve-{ticker.lower()}-cik",
            resolution_key=f"sec_cik:{cik}",
            revision=1,
            outcome="selected",
            selected_assertion_id=assertion.assertion_id,
            candidate_digest_sha256=identifier_candidate_digest((assertion,)),
            policy_name="test",
            policy_version="1",
            policy_config_sha256=SHA,
            reason_code="test_fixture",
            reason_details=(("ticker", ticker),),
            material_dissent=False,
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    )
    conn.commit()


def _body(cik: str, *, value: int = 100) -> bytes:
    accession = f"{cik}-26-000001"
    return json.dumps(
        {
            "cik": int(cik),
            "entityName": f"Issuer {cik}",
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
                                    "val": value,
                                    "accn": accession,
                                    "fy": 2025,
                                    "fp": "FY",
                                    "form": "10-K",
                                    "filed": "2026-02-01",
                                    "frame": "CY2025",
                                }
                            ]
                        },
                    }
                }
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _request(
    tmp_path: Path,
    *,
    apply: bool,
    tickers: tuple[str, ...] = ("ACME",),
    task_id: str = "companyfacts-test",
) -> CompanyFactsBindingBackfillRequest:
    return CompanyFactsBindingBackfillRequest(
        blob_root=tmp_path / "immutable-companyfacts",
        checkpoint_root=tmp_path / "checkpoints",
        apply=apply,
        tickers=tickers,
        batch_size=10,
        task_id=task_id,
    )


def _fetcher(
    calls: list[str],
) -> Callable[..., FetchedCompanyFacts]:
    def fetch(cik: str, *, timeout: int = 30) -> FetchedCompanyFacts:
        assert timeout == 30
        calls.append(cik)
        return FetchedCompanyFacts(
            source_url=(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"),
            raw_body=_body(cik),
            observed_at=STAMP + timedelta(minutes=1),
            retrieved_at=STAMP + timedelta(minutes=1, seconds=1),
        )

    return fetch


def test_companyfacts_fetch_uses_dynamic_sec_identity_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, dict[str, str], int]] = []

    class Response:
        content = b"{}"

        @staticmethod
        def raise_for_status() -> None:
            return None

    def get(
        url: str,
        *,
        headers: dict[str, str],
        timeout: int,
    ) -> Response:
        seen.append((url, headers, timeout))
        return Response()

    monkeypatch.setenv(
        "EDGAR_USER_AGENT",
        "companyfacts-backfill-test test-owner@example.test",
    )
    monkeypatch.setattr(sec_xbrl.requests, "get", get)

    fetched = sec_xbrl.fetch_companyfacts("0000000001", timeout=17)

    assert fetched.raw_body == b"{}"
    assert seen == [
        (
            "https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json",
            {
                "User-Agent": "companyfacts-backfill-test test-owner@example.test",
                "Accept-Encoding": "gzip, deflate",
            },
            17,
        )
    ]


def test_dry_run_is_offline_read_only_and_plans_exact_registry_identity(
    tmp_path: Path,
) -> None:
    conn = _database(tmp_path)
    _seed_identity(
        conn,
        ticker="ACME",
        cik="0000000001",
        issuer_id="issuer-acme",
    )

    def forbidden_fetch(cik: str, *, timeout: int = 30) -> FetchedCompanyFacts:
        del cik
        raise AssertionError(f"dry run fetched with timeout={timeout}")

    try:
        result = backfill_sec_companyfacts_bindings(
            conn,
            _request(tmp_path, apply=False),
            fetcher=forbidden_fetch,
            now=lambda: STAMP + timedelta(hours=1),
        )

        assert result.mode == "dry_run"
        assert result.considered == 1
        assert result.items[0].normalized_cik == "0000000001"
        assert result.has_more is False
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert not (tmp_path / "immutable-companyfacts").exists()
        assert not (tmp_path / "checkpoints").exists()
    finally:
        conn.close()


def test_apply_spaces_requests_captures_only_evidence_and_resumes_idempotently(
    tmp_path: Path,
) -> None:
    conn = _database(tmp_path)
    _seed_identity(
        conn,
        ticker="ACME",
        cik="0000000001",
        issuer_id="issuer-acme",
    )
    _seed_identity(
        conn,
        ticker="BETA",
        cik="0000000002",
        issuer_id="issuer-beta",
    )
    calls: list[str] = []
    sleeps: list[float] = []
    request = _request(
        tmp_path,
        apply=True,
        tickers=("ACME", "BETA", "BLOCK"),
    )
    try:
        first = backfill_sec_companyfacts_bindings(
            conn,
            request,
            fetcher=_fetcher(calls),
            sleeper=sleeps.append,
            now=lambda: STAMP + timedelta(hours=1),
        )

        assert calls == ["0000000001", "0000000002"]
        assert sleeps == [0.15]
        assert first.considered == 2
        assert first.identity_blocked == 1
        blocked = next(item for item in first.items if item.outcome == "identity_blocked")
        assert blocked.ticker == "BLOCK"
        assert blocked.reason is not None
        assert "canonical issuer" in blocked.reason
        assert first.supported_accessions == 2
        assert first.documents_created == 2
        assert first.bindings_created == 2
        assert first.has_more is False
        assert conn.execute("SELECT COUNT(*) FROM financial_facts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
        stored_snapshots = conn.execute(
            "SELECT file_path, sha256 FROM documents ORDER BY ticker"
        ).fetchall()
        for stored_path, expected_sha in stored_snapshots:
            path = Path(str(stored_path))
            assert path.exists()
            assert path.is_file()
            assert path.is_relative_to((tmp_path / "immutable-companyfacts").resolve())
            assert hashlib.sha256(path.read_bytes()).hexdigest() == str(expected_sha)
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM legacy_document_evidence_binding_revisions"
            ).fetchone()[0]
            == 2
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            len(
                tuple(
                    path
                    for path in (tmp_path / "immutable-companyfacts").rglob("*.json")
                    if path.is_file()
                )
            )
            == 2
        )
        checkpoint = (tmp_path / "checkpoints" / "companyfacts-test" / "state.json").read_text(
            encoding="utf-8"
        )
        assert "BLOCK" not in checkpoint

        resumed = backfill_sec_companyfacts_bindings(
            conn,
            request,
            fetcher=_fetcher(calls),
            sleeper=sleeps.append,
            now=lambda: STAMP + timedelta(hours=2),
        )
        assert resumed.considered == 0
        assert calls == ["0000000001", "0000000002"]

        replay = backfill_sec_companyfacts_bindings(
            conn,
            request.model_copy(update={"task_id": "companyfacts-replay"}),
            fetcher=_fetcher(calls),
            sleeper=sleeps.append,
            now=lambda: STAMP + timedelta(hours=3),
        )
        assert replay.documents_created == 0
        assert replay.bindings_created == 0
        assert replay.bindings_unchanged == 2
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM legacy_document_evidence_binding_revisions"
            ).fetchone()[0]
            == 2
        )
    finally:
        conn.close()


def test_sec_403_is_hard_stop_without_database_or_checkpoint_mutation(
    tmp_path: Path,
) -> None:
    conn = _database(tmp_path)
    _seed_identity(
        conn,
        ticker="ACME",
        cik="0000000001",
        issuer_id="issuer-acme",
    )

    def forbidden(cik: str, *, timeout: int = 30) -> FetchedCompanyFacts:
        del cik, timeout
        response = requests.Response()
        response.status_code = 403
        raise requests.HTTPError("forbidden response body", response=response)

    try:
        with pytest.raises(CompanyFactsBackfillHardStopError, match="hard stop"):
            backfill_sec_companyfacts_bindings(
                conn,
                _request(tmp_path, apply=True),
                fetcher=forbidden,
                sleeper=lambda _seconds: None,
                now=lambda: STAMP + timedelta(hours=1),
            )

        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert not (tmp_path / "checkpoints").exists()
        assert not (tmp_path / "immutable-companyfacts").exists()
    finally:
        conn.close()
