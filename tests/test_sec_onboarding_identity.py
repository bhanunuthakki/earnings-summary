"""Exact SEC metadata closes new-company identity gaps without FMP or a thesis."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from argparse import Namespace
from collections.abc import Callable, Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from execution import onboard_ticker
from pipeline import sec_onboarding_identity, sec_xbrl
from pipeline.sec_onboarding_identity import IdentityStatus, ensure_sec_onboarding_identity
from provenance.issuer_registry_bootstrap import (
    SEC_COMPANY_TICKERS_URL,
    BootstrapRequest,
    bootstrap_issuer_reporting_registry,
)

STAMP = datetime(2026, 10, 1, tzinfo=UTC)
CIK = "0001234567"
SUBMISSIONS_URL = f"https://data.sec.gov/submissions/CIK{CIK}.json"
COVER_URL = "https://www.sec.gov/Archives/edgar/data/1234567/000123456726000001/new.htm"


@pytest.fixture
def database(
    tmp_path: Path, migrated_db: Callable[..., Path]
) -> Generator[sqlite3.Connection, None, None]:
    path = migrated_db(tmp_path / "identity.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO tracked_companies (user_id,ticker,name,list_type) VALUES "
        "('bhanu','NEW','New Issuer','evaluation')"
    )
    conn.execute(
        "INSERT INTO tracked_companies (user_id,ticker,name,list_type) VALUES "
        "('bhanu','OTHER','Other Issuer','portfolio')"
    )
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def _sources(
    *,
    title: str = "Class A Common Stock, $0.01 par value",
    entity: str = "operating",
    fye: str = "0930",
    forms: tuple[str, ...] = ("10-Q",),
) -> dict[str, bytes]:
    return {
        SEC_COMPANY_TICKERS_URL: json.dumps(
            {
                "0": {"cik_str": 1234567, "ticker": "NEW", "title": "New Issuer"},
                "1": {"cik_str": 7654321, "ticker": "OTHER", "title": "Other Issuer"},
            }
        ).encode(),
        SUBMISSIONS_URL: json.dumps(
            {
                "cik": "1234567",
                "name": "New Issuer",
                "entityType": entity,
                "tickers": ["NEW"],
                "fiscalYearEnd": fye,
                "filings": {
                    "recent": {
                        "form": list(forms),
                        "accessionNumber": ["0001234567-26-000001"] * len(forms),
                        "primaryDocument": ["new.htm"] * len(forms),
                        "filingDate": ["2026-08-01"] * len(forms),
                    }
                },
            }
        ).encode(),
        COVER_URL: (
            '<html xmlns:dei="http://xbrl.sec.gov/dei/2026" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL">'
            '<ix:nonNumeric name="dei:EntityCentralIndexKey" contextRef="issuer">0001234567</ix:nonNumeric>'
            '<ix:nonNumeric name="dei:TradingSymbol" contextRef="security">NEW</ix:nonNumeric>'
            f'<ix:nonNumeric name="dei:Security12bTitle" contextRef="security">{title}</ix:nonNumeric>'
            "</html>"
        ).encode(),
    }


sec_identity_sources = _sources


def test_bounded_bootstrap_never_writes_unrelated_bindings(
    database: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    sources = _sources()
    result = bootstrap_issuer_reporting_registry(
        database,
        raw_body=sources[SEC_COMPANY_TICKERS_URL],
        request=BootstrapRequest(
            source_url=SEC_COMPANY_TICKERS_URL,
            blob_root=tmp_path / "blobs",
            apply=True,
            recorded_at=STAMP,
            ticker_scope=("NEW",),
        ),
    )
    assert result.selected_tickers == ("NEW",)
    assert (
        database.execute(
            "SELECT recorded_issuer_id FROM legacy_issuer_binding_revisions"
        ).fetchall()[0][0]
        == "legacy-ticker:NEW"
    )
    assert (
        database.execute("SELECT COUNT(*) FROM legacy_issuer_binding_revisions").fetchone()[0] == 1
    )
    assert database.execute("SELECT COUNT(*) FROM issuer_entities").fetchone()[0] == 1
    with pytest.raises(ValueError, match="active membership"):
        bootstrap_issuer_reporting_registry(
            database,
            raw_body=sources[SEC_COMPANY_TICKERS_URL],
            request=BootstrapRequest(
                source_url=SEC_COMPANY_TICKERS_URL,
                blob_root=tmp_path / "blobs",
                apply=True,
                recorded_at=STAMP,
                ticker_scope=("ABSENT",),
            ),
        )


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Class A Common Stock, $0.01 par value", "equity"),
        ("American Depositary Shares, each representing ordinary shares", "adr"),
    ],
)
def test_exact_security_and_non_december_fiscal_metadata_support_companyfacts(
    database: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    title: str,
    expected: str,
) -> None:
    sources = _sources(title=title)
    requests: list[str] = []

    def fetch(url: str) -> bytes:
        requests.append(url)
        return sources[url]

    identity = ensure_sec_onboarding_identity(
        database,
        ticker="NEW",
        project_root=tmp_path,
        knowledge_at=STAMP,
        fetch=fetch,
    )
    assert identity.status is IdentityStatus.READY
    assert identity.instrument_type == expected
    assert identity.fiscal_year_end == "09-30"
    assert identity.filing_regime == "10-K"
    assert requests == [SEC_COMPANY_TICKERS_URL, SUBMISSIONS_URL, COVER_URL]
    assert tuple(
        database.execute(
            "SELECT instrument_type,filing_regime,fiscal_year_end FROM tracked_companies WHERE ticker='NEW'"
        ).fetchone()
    ) == (expected, "10-K", "09-30")
    assert (
        database.execute(
            "SELECT COUNT(*) FROM legacy_issuer_binding_revisions WHERE recorded_issuer_id='legacy-ticker:OTHER'"
        ).fetchone()[0]
        == 0
    )
    for source in identity.sources:
        path = tmp_path / "data/evidence/blobs" / source.source_sha256[:2] / source.source_sha256
        assert path.read_bytes() == sources[source.source_url]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == source.source_sha256
    raw_facts = json.dumps(
        {
            "cik": 1234567,
            "entityName": "New Issuer",
            "facts": {
                "us-gaap": {
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {
                        "label": "Revenue",
                        "description": "Revenue",
                        "units": {
                            "USD": [
                                {
                                    "start": "2026-04-01",
                                    "end": "2026-06-30",
                                    "val": 100000000,
                                    "accn": "0001234567-26-000001",
                                    "fy": 2026,
                                    "fp": "Q3",
                                    "form": "10-Q",
                                    "filed": "2026-08-01",
                                    "frame": "CY2026Q2",
                                }
                            ]
                        },
                    },
                }
            },
        }
    ).encode()

    def companyfacts(cik: str) -> sec_xbrl.FetchedCompanyFacts:
        assert cik == CIK
        return sec_xbrl.FetchedCompanyFacts(
            source_url=f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json",
            raw_body=raw_facts,
            observed_at=STAMP,
            retrieved_at=STAMP,
        )

    monkeypatch.setattr(sec_xbrl, "fetch_companyfacts", companyfacts)
    result = cast(
        Callable[..., onboard_ticker.StageResult], getattr(onboard_ticker, "_run_sec_ingestion")
    )(
        database,
        ticker="NEW",
        project_root=tmp_path,
        run_id="identity-integration",
        skip=False,
    )
    assert result.rows_processed == 1
    assert database.execute("SELECT COUNT(*) FROM fact_observation_revisions").fetchone()[0] == 1
    assert not (tmp_path / "micro_thesis/holdings/NEW.json").exists()


@pytest.mark.parametrize(
    "change",
    [
        "missing_fye",
        "investment",
        "unproven_security",
        "different_context",
        "cik_conflict",
        "stored_conflict",
    ],
)
def test_missing_or_conflicting_source_metadata_never_defaults_to_equity(
    database: sqlite3.Connection,
    tmp_path: Path,
    change: str,
) -> None:
    sources = _sources()
    if change == "missing_fye":
        sources = _sources(fye="")
    elif change == "investment":
        sources = _sources(entity="investment")
    elif change == "unproven_security":
        sources = _sources(title="Preferred stock")
    elif change == "different_context":
        sources[COVER_URL] = sources[COVER_URL].replace(
            b'Security12bTitle" contextRef="security"', b'Security12bTitle" contextRef="different"'
        )
    elif change == "cik_conflict":
        sources[SUBMISSIONS_URL] = sources[SUBMISSIONS_URL].replace(
            b'"cik": "1234567"', b'"cik": "7654321"'
        )
    elif change == "stored_conflict":
        database.execute("UPDATE tracked_companies SET fiscal_year_end='12-31' WHERE ticker='NEW'")
        database.commit()
    result = ensure_sec_onboarding_identity(
        database,
        ticker="NEW",
        project_root=tmp_path,
        knowledge_at=STAMP,
        fetch=sources.__getitem__,
    )
    assert result.status in {
        IdentityStatus.MISSING_SOURCE_METADATA,
        IdentityStatus.IDENTITY_CONFLICT,
    }
    assert (
        database.execute(
            "SELECT instrument_type FROM tracked_companies WHERE ticker='NEW'"
        ).fetchone()[0]
        is None
    )
    if change == "stored_conflict":
        assert (
            database.execute(
                "SELECT fiscal_year_end FROM tracked_companies WHERE ticker='NEW'"
            ).fetchone()[0]
            == "12-31"
        )


def test_metadata_denial_prevents_http(database: sqlite3.Connection, tmp_path: Path) -> None:
    database.execute("UPDATE tracked_companies SET list_type='none' WHERE ticker='NEW'")
    database.commit()

    def forbidden(_url: str) -> bytes:
        raise AssertionError("HTTP must not run for inactive collection policy")

    result = ensure_sec_onboarding_identity(
        database,
        ticker="NEW",
        project_root=tmp_path,
        knowledge_at=STAMP,
        fetch=forbidden,
    )
    assert result.status is IdentityStatus.POLICY_DENIED


def test_actual_onboarding_closes_identity_and_admits_sec_without_fmp(
    database: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = _sources()
    monkeypatch.setattr(sec_onboarding_identity, "_fetch_bytes", sources.__getitem__)
    database_path = Path(database.execute("PRAGMA database_list").fetchone()[2])
    monkeypatch.setattr(onboard_ticker, "_DB_PATH", database_path)
    monkeypatch.setattr(onboard_ticker, "_STATE_ROOT", tmp_path)
    monkeypatch.setattr(onboard_ticker, "_HOLDINGS_DIR", tmp_path / "micro_thesis/holdings")

    def transcripts(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {}

    monkeypatch.setattr(onboard_ticker, "stage_pending_issuer_transcripts", transcripts)
    raw = json.dumps(
        {
            "cik": 1234567,
            "entityName": "New Issuer",
            "facts": {
                "us-gaap": {
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {
                        "label": "Revenue",
                        "description": "Revenue",
                        "units": {
                            "USD": [
                                {
                                    "start": "2026-04-01",
                                    "end": "2026-06-30",
                                    "val": 100000000,
                                    "accn": "0001234567-26-000001",
                                    "fy": 2026,
                                    "fp": "Q3",
                                    "form": "10-Q",
                                    "filed": "2026-08-01",
                                    "frame": "CY2026Q2",
                                }
                            ]
                        },
                    },
                }
            },
        }
    ).encode()
    calls: list[str] = []

    def companyfacts(cik: str) -> sec_xbrl.FetchedCompanyFacts:
        calls.append(cik)
        stamp = datetime.now(UTC)
        return sec_xbrl.FetchedCompanyFacts(
            source_url=f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json",
            raw_body=raw,
            observed_at=stamp,
            retrieved_at=stamp,
        )

    monkeypatch.setattr(sec_xbrl, "fetch_companyfacts", companyfacts)
    args = Namespace(
        ticker="NEW",
        industry_template=None,
        instrument=None,
        skip_fmp=True,
        skip_sec=False,
        skip_transcripts=True,
        skip_ir=True,
        skip_saydo=True,
        force_saydo=False,
    )
    assert cast(Callable[[Namespace], int], getattr(onboard_ticker, "_onboard"))(args) == 0
    assert calls == [CIK]
    assert database.execute("SELECT COUNT(*) FROM fact_observation_revisions").fetchone()[0] == 1
    assert (
        database.execute(
            "SELECT fiscal_year_end FROM tracked_companies WHERE ticker='NEW'"
        ).fetchone()[0]
        == "09-30"
    )
    assert not (tmp_path / "micro_thesis/holdings/NEW.json").exists()


def test_new_alias_binding_preserves_existing_fiscal_metadata(
    database: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    sources = _sources()
    ready = ensure_sec_onboarding_identity(
        database,
        ticker="NEW",
        project_root=tmp_path,
        knowledge_at=STAMP,
        fetch=sources.__getitem__,
    )
    assert ready.status is IdentityStatus.READY
    raw = json.dumps({"0": {"cik_str": 1234567, "ticker": "OTHER", "title": "New Issuer"}}).encode()
    bootstrap_issuer_reporting_registry(
        database,
        raw_body=raw,
        request=BootstrapRequest(
            source_url=SEC_COMPANY_TICKERS_URL,
            blob_root=tmp_path / "data/evidence/blobs",
            apply=True,
            recorded_at=STAMP,
            ticker_scope=("OTHER",),
        ),
    )
    profile = database.execute(
        "SELECT fiscal_year_end,filing_regime FROM issuer_profile_revisions ORDER BY revision DESC LIMIT 1"
    ).fetchone()
    assert tuple(profile) == ("09-30", "10-K")
