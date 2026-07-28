"""Delisted SEC issuers remain searchable without creating future duties."""

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
    SecHistoricalIssuerRequest,
    bootstrap_sec_historical_issuer,
    parse_sec_historical_issuer_identity,
)

ROOT = Path(__file__).resolve().parents[1]
HEAD = "0230_evidence_subject_bindings"
STAMP = datetime(2026, 7, 27, 23, 30, tzinfo=UTC)
CIK = "0001699838"
SOURCE_URL = f"https://data.sec.gov/submissions/CIK{CIK}.json"


def _body(*, current_tickers: list[str] | None = None) -> bytes:
    return json.dumps(
        {
            "cik": CIK,
            "name": "Confluent, Inc.",
            "entityType": "operating",
            "tickers": current_tickers or [],
            "filings": {
                "recent": {
                    "form": ["10-K", "15-12G"],
                    "filingDate": ["2026-02-20", "2026-03-27"],
                    "accessionNumber": [
                        "0001699838-26-000010",
                        "0000950142-26-000892",
                    ],
                    "primaryDocument": [
                        "confluent-20251231.htm",
                        "eh260757581_1512g.htm",
                    ],
                }
            },
        },
        separators=(",", ":"),
    ).encode()


def _database(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "historical-issuer.db"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, HEAD)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _request(tmp_path: Path, *, apply: bool) -> SecHistoricalIssuerRequest:
    return SecHistoricalIssuerRequest(
        ticker="CFLT",
        normalized_cik=CIK,
        source_url=SOURCE_URL,
        raw_body=_body(),
        blob_root=tmp_path / "blobs",
        apply=apply,
        recorded_at=STAMP,
    )


def test_historical_parser_requires_ticker_retirement_and_form15() -> None:
    identity = parse_sec_historical_issuer_identity(
        _body(),
        normalized_cik=CIK,
        ticker="CFLT",
    )
    assert identity.termination_form == "15-12G"
    assert identity.termination_date.isoformat() == "2026-03-27"
    with pytest.raises(SecCompanyTickerContractError):
        parse_sec_historical_issuer_identity(
            _body(current_tickers=["CFLT"]),
            normalized_cik=CIK,
            ticker="CFLT",
        )


def test_historical_bootstrap_is_append_only_and_exactly_replayable(
    tmp_path: Path,
) -> None:
    conn = _database(tmp_path)
    dry_run = bootstrap_sec_historical_issuer(
        conn,
        request=_request(tmp_path, apply=False),
    )
    first = bootstrap_sec_historical_issuer(
        conn,
        request=_request(tmp_path, apply=True),
    )
    second = bootstrap_sec_historical_issuer(
        conn,
        request=_request(tmp_path, apply=True),
    )

    assert dry_run.mode == "dry_run"
    assert first.records_created > 0
    assert second.records_created == 0
    assert conn.execute(
        "SELECT status, filing_regime FROM v_issuer_profiles_current"
    ).fetchone() == ("inactive", "SEC")
    assert conn.execute(
        "SELECT outcome, reason_code FROM v_legacy_issuer_bindings_current "
        "WHERE recorded_issuer_id = 'legacy-ticker:CFLT'"
    ).fetchone() == ("selected", "sec_form15_historical_identity_selected")
    assert conn.execute(
        "SELECT reporting_entity_id, security_id "
        "FROM v_recorded_subject_bindings_current "
        "WHERE recorded_issuer_id = 'legacy-ticker:CFLT'"
    ).fetchone() == (f"reporting:sec:{CIK}", None)
    assert conn.execute(
        "SELECT inclusion_state, require_sec, require_ir, require_earnings "
        "FROM v_issuer_reporting_scope_current"
    ).fetchone() == ("discovery", 0, 0, 0)
    assert conn.execute("SELECT COUNT(*) FROM source_obligation_revisions").fetchone() == (0,)
    assert conn.execute("SELECT source_kind FROM evidence_source_observations").fetchone() == (
        "sec_submissions",
    )
    conn.close()
