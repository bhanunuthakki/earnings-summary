"""Former SEC tickers retain historical evidence without ending issuer identity."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from execution.bootstrap_sec_former_ticker import normalize_cik
from provenance.issuer_registry_bootstrap import (
    SecCompanyTickerContractError,
    SecFormerTickerRequest,
    bootstrap_sec_former_ticker,
    parse_sec_former_ticker_identity,
)

ROOT = Path(__file__).resolve().parents[1]
HEAD = "0230_evidence_subject_bindings"
STAMP = datetime(2026, 8, 27, 20, 45, tzinfo=UTC)
CIK = "0001009759"
SUBMISSIONS_URL = f"https://data.sec.gov/submissions/CIK{CIK}.json"
TRANSITION_URL = (
    "https://www.sec.gov/Archives/edgar/data/1009759/000110465926080974/tm2619464d3_8k.htm"
)


def _submissions(*, current_ticker: str = "CEPL") -> bytes:
    return json.dumps(
        {
            "cik": CIK,
            "name": "Capstone Energy Plus, Inc.",
            "entityType": "operating",
            "tickers": [current_ticker],
        },
        separators=(",", ":"),
    ).encode()


def _transition(*, former_ticker: str = "CGEH") -> bytes:
    return (
        "<html><body><p>CIK 0001009759</p><p>The Company's common stock, "
        f"which had previously been quoted on the OTC Markets under the symbol {former_ticker}, "
        "is expected to begin trading on the Nasdaq Global Market under the ticker symbol CEPL "
        "on July 8, 2026.</p></body></html>"
    ).encode()


def _database(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "former-ticker.db"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, HEAD)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _request(tmp_path: Path, *, apply: bool) -> SecFormerTickerRequest:
    return SecFormerTickerRequest(
        former_ticker="CGEH",
        successor_ticker="CEPL",
        normalized_cik=CIK,
        transition_date=date(2026, 7, 8),
        submissions_source_url=SUBMISSIONS_URL,
        submissions_raw_body=_submissions(),
        transition_source_url=TRANSITION_URL,
        transition_raw_body=_transition(),
        blob_root=tmp_path / "blobs",
        apply=apply,
        recorded_at=STAMP,
    )


def test_former_ticker_parser_requires_current_successor_and_transition_proof() -> None:
    identity = parse_sec_former_ticker_identity(
        _submissions(),
        _transition(),
        normalized_cik=CIK,
        former_ticker="CGEH",
        successor_ticker="CEPL",
        transition_date=date(2026, 7, 8),
        transition_source_url=TRANSITION_URL,
    )
    assert identity.transition_accession == "0001104659-26-080974"
    assert identity.legal_name == "Capstone Energy Plus, Inc."
    with pytest.raises(SecCompanyTickerContractError, match="successor ticker"):
        parse_sec_former_ticker_identity(
            _submissions(current_ticker="OTHER"),
            _transition(),
            normalized_cik=CIK,
            former_ticker="CGEH",
            successor_ticker="CEPL",
            transition_date=date(2026, 7, 8),
            transition_source_url=TRANSITION_URL,
        )
    with pytest.raises(SecCompanyTickerContractError, match="does not prove"):
        parse_sec_former_ticker_identity(
            _submissions(),
            _transition(former_ticker="OTHER"),
            normalized_cik=CIK,
            former_ticker="CGEH",
            successor_ticker="CEPL",
            transition_date=date(2026, 7, 8),
            transition_source_url=TRANSITION_URL,
        )


def test_former_ticker_cli_normalizes_short_sec_cik() -> None:
    assert normalize_cik("1009759") == CIK
    assert normalize_cik(f" {CIK} ") == CIK
    with pytest.raises(ValueError, match="ten decimal digits"):
        normalize_cik("not-a-cik")


def test_former_ticker_bootstrap_is_append_only_and_exactly_replayable(
    tmp_path: Path,
) -> None:
    conn = _database(tmp_path)
    dry_run = bootstrap_sec_former_ticker(conn, request=_request(tmp_path, apply=False))
    first = bootstrap_sec_former_ticker(conn, request=_request(tmp_path, apply=True))
    second = bootstrap_sec_former_ticker(conn, request=_request(tmp_path, apply=True))

    assert dry_run.mode == "dry_run"
    assert first.records_created > 0
    assert second.records_created == 0
    assert conn.execute(
        "SELECT status, filing_regime FROM v_issuer_profiles_current"
    ).fetchone() == ("active", "SEC")
    assert conn.execute(
        "SELECT outcome, reason_code FROM v_legacy_issuer_bindings_current "
        "WHERE recorded_issuer_id = 'legacy-ticker:CGEH'"
    ).fetchone() == ("selected", "sec_former_ticker_transition_selected")
    assert conn.execute(
        "SELECT reporting_entity_id, security_id "
        "FROM v_recorded_subject_bindings_current "
        "WHERE recorded_issuer_id = 'legacy-ticker:CGEH'"
    ).fetchone() == (f"reporting:sec:{CIK}", None)
    assert conn.execute(
        "SELECT inclusion_state, require_sec, require_ir, require_earnings "
        "FROM v_issuer_reporting_scope_current"
    ).fetchone() == ("discovery", 0, 0, 0)
    assert conn.execute("SELECT COUNT(*) FROM source_obligation_revisions").fetchone() == (0,)
    assert conn.execute(
        "SELECT source_kind FROM evidence_source_observations ORDER BY source_kind"
    ).fetchall() == [("sec_filing",), ("sec_submissions",)]
    assert conn.execute(
        "SELECT media_type FROM evidence_content_blobs ORDER BY media_type"
    ).fetchall() == [("application/json",), ("text/html",)]
    conn.close()
