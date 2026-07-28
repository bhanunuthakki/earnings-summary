"""SEC fund identities retain registrant, series, and share-class boundaries."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from provenance.issuer_registry_bootstrap import (
    SecCompanyTickerContractError,
    SecFundBootstrapRequest,
    SecFundRegistrantEvidence,
    bootstrap_sec_fund_registry,
    parse_sec_mutual_fund_tickers,
)
from provenance.reporting_entity_registry import ReportingEntityRegistry

ROOT = Path(__file__).resolve().parents[1]
HEAD = "0230_evidence_subject_bindings"
STAMP = datetime(2026, 7, 27, 23, 0, tzinfo=UTC)
SOURCE_URL = "https://www.sec.gov/files/company_tickers_mf.json"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _database(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "fund-registry.db"
    config = _config(path)
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, HEAD)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
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
    conn.executemany(
        "INSERT INTO tracked_companies VALUES (?, 'bhanu', ?, ?, ?, NULL)",
        (
            (1, "AVDV", "Avantis International Small Cap Value ETF", "portfolio"),
            (2, "AVUV", "Avantis U.S. Small Cap Value ETF", "watchlist"),
            (3, "ACME", "Acme Corporation", "portfolio"),
        ),
    )
    conn.commit()
    return conn


def _fund_body() -> bytes:
    return json.dumps(
        {
            "fields": ["cik", "seriesId", "classId", "symbol"],
            "data": [
                [1710607, "S000066457", "C000214352", "AVDV"],
                [1710607, "S000066459", "C000214354", "AVUV"],
                [857489, "S000005786", "C000015902", "VWO"],
                [2078265, "S000104044", "C000274642", ""],
            ],
        },
        separators=(",", ":"),
    ).encode()


def _request(tmp_path: Path, *, apply: bool) -> SecFundBootstrapRequest:
    registrant = json.dumps(
        {
            "cik": "0001710607",
            "name": "AMERICAN CENTURY ETF TRUST",
            "entityType": "investment",
        },
        separators=(",", ":"),
    ).encode()
    return SecFundBootstrapRequest(
        source_url=SOURCE_URL,
        registrants=(
            SecFundRegistrantEvidence(
                normalized_cik="0001710607",
                source_url=("https://data.sec.gov/submissions/CIK0001710607.json"),
                raw_body=registrant,
            ),
        ),
        blob_root=tmp_path / "blobs",
        apply=apply,
        recorded_at=STAMP,
    )


def test_fund_registry_contract_is_closed() -> None:
    assert [item.symbol for item in parse_sec_mutual_fund_tickers(_fund_body())] == [
        "AVDV",
        "AVUV",
        "VWO",
        "",
    ]
    with pytest.raises(SecCompanyTickerContractError):
        parse_sec_mutual_fund_tickers(b'{"fields":["cik","seriesId","classId"],"data":[]}')


def test_bootstrap_keeps_two_series_under_one_legal_trust_and_replays_exactly(
    tmp_path: Path,
) -> None:
    conn = _database(tmp_path)
    raw_body = _fund_body()

    dry_run = bootstrap_sec_fund_registry(
        conn,
        raw_body=raw_body,
        request=_request(tmp_path, apply=False),
    )
    first = bootstrap_sec_fund_registry(
        conn,
        raw_body=raw_body,
        request=_request(tmp_path, apply=True),
    )
    second = bootstrap_sec_fund_registry(
        conn,
        raw_body=raw_body,
        request=_request(tmp_path, apply=True),
    )

    assert dry_run.selected_tickers == ("AVDV", "AVUV")
    assert [item.outcome for item in dry_run.results] == ["selected", "selected"]
    assert first.records_created > 0
    assert second.records_created == 0
    assert conn.execute("SELECT COUNT(*) FROM issuer_entities").fetchone() == (1,)
    assert conn.execute(
        "SELECT reporting_entity_kind, COUNT(*) FROM reporting_entities "
        "GROUP BY reporting_entity_kind ORDER BY reporting_entity_kind"
    ).fetchall() == [
        ("fund_series", 2),
        ("legal_registrant", 1),
    ]
    assert conn.execute("SELECT COUNT(*) FROM securities").fetchone() == (2,)
    assert conn.execute(
        "SELECT require_sec, require_ir, require_earnings FROM v_issuer_reporting_scope_current"
    ).fetchone() == (1, 1, 0)
    assert conn.execute(
        "SELECT document_family, obligation_state, COUNT(*) "
        "FROM v_source_obligations_current "
        "GROUP BY document_family, obligation_state "
        "ORDER BY document_family"
    ).fetchall() == [
        ("investment_company_periodic", "required", 2),
        ("issuer_earnings_materials", "not_applicable", 2),
        ("issuer_financial_statements", "required", 2),
        ("issuer_presentations", "optional", 2),
    ]
    subjects = conn.execute(
        "SELECT recorded_issuer_id, issuer_id, reporting_entity_id, security_id "
        "FROM v_recorded_subject_bindings_current ORDER BY recorded_issuer_id"
    ).fetchall()
    assert len({str(row[1]) for row in subjects}) == 1
    assert len({str(row[2]) for row in subjects}) == 2
    assert len({str(row[3]) for row in subjects}) == 2
    registry = ReportingEntityRegistry(conn)
    assert (
        registry.canonicalize_recorded_subject(
            "legacy-ticker:AVDV",
            knowledge_at=STAMP,
        ).reporting_entity_id
        != registry.canonicalize_recorded_subject(
            "legacy-ticker:AVUV",
            knowledge_at=STAMP,
        ).reporting_entity_id
    )
    assert conn.execute(
        "SELECT source_kind, COUNT(*) FROM evidence_source_observations "
        "GROUP BY source_kind ORDER BY source_kind"
    ).fetchall() == [
        ("sec_mutual_fund_tickers", 1),
        ("sec_submissions", 1),
    ]
    conn.close()
