"""Resolve the known IVN and NTDOY non-SEC identity blockers.

This is a deliberately narrow migration entrypoint.  The reusable write
boundary lives in ``provenance.foreign_issuer_bootstrap``; this script owns the
two reviewed authority bundles and their public-source retrievals.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field
from pypdf import PdfReader

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from log_redact import redact  # noqa: E402
from provenance.foreign_issuer_bootstrap import (  # noqa: E402
    AuthoritySurfaceClaim,
    ForeignIssuerBootstrapRequest,
    ForeignIssuerBootstrapResult,
    ForeignIssuerSource,
    ListingClaim,
    ReportingIdentifierClaim,
    SecurityClaim,
    SecurityIdentifierClaim,
    SourceObligationClaim,
    bootstrap_foreign_issuer,
)
from runtime.job_runtime import JobLock  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

KnownTicker = Literal["IVN", "NTDOY"]
_TIMEOUT_SECONDS = 45


class KnownSourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_key: str = Field(min_length=1, max_length=128)
    source_kind: str = Field(min_length=1, max_length=64)
    source_url: str = Field(min_length=1)
    media_type: str = Field(min_length=1, max_length=255)


_NINTENDO_SOURCES = (
    KnownSourceSpec(
        source_key="nintendo-corporate",
        source_kind="issuer_publisher_identity",
        source_url="https://www.nintendo.co.jp/corporate/en/outline/index.html",
        media_type="text/html",
    ),
    KnownSourceSpec(
        source_key="nintendo-stock",
        source_kind="issuer_publisher_security",
        source_url="https://www.nintendo.co.jp/ir/en/stock/information/index.html",
        media_type="text/html",
    ),
    KnownSourceSpec(
        source_key="nintendo-ir-home",
        source_kind="issuer_publisher_authority",
        source_url="https://www.nintendo.co.jp/ir/en/index.html",
        media_type="text/html",
    ),
    KnownSourceSpec(
        source_key="nintendo-ir-financials",
        source_kind="issuer_publisher_authority",
        source_url="https://www.nintendo.co.jp/ir/en/finance/index.html",
        media_type="text/html",
    ),
    KnownSourceSpec(
        source_key="nintendo-edinet-annual",
        source_kind="edinet_filing",
        source_url=("https://disclosure2dl.edinet-fsa.go.jp/searchdocument/pdf/S100QY8A.pdf"),
        media_type="application/pdf",
    ),
    KnownSourceSpec(
        source_key="citi-ntdoy-adr",
        source_kind="depositary_receipt_authority",
        source_url=(
            "https://depositaryreceipts.citi.com/adr/guides/pgm_d.aspx"
            "?cusip=654445303&pageId=15&subpageid=106"
        ),
        media_type="text/html",
    ),
)

_IVN_SOURCES = (
    KnownSourceSpec(
        source_key="ivn-faq",
        source_kind="issuer_publisher_identity",
        source_url="https://www.ivanhoemines.com/investors/investor-faqs/",
        media_type="text/html",
    ),
    KnownSourceSpec(
        source_key="ivn-legal",
        source_kind="issuer_publisher_security",
        source_url="https://www.ivanhoemines.com/legal-notice/",
        media_type="text/html",
    ),
    KnownSourceSpec(
        source_key="ivn-isin",
        source_kind="issuer_publisher_security",
        source_url=(
            "https://www.ivanhoemines.com/investors/investor-faqs/faq/"
            "what-are-ivanhoe-mines-isin-numbers/"
        ),
        media_type="text/html",
    ),
    KnownSourceSpec(
        source_key="ivn-ir-home",
        source_kind="issuer_publisher_authority",
        source_url="https://www.ivanhoemines.com/investors/investor-hub/",
        media_type="text/html",
    ),
)

_SOURCE_MARKERS: dict[str, tuple[str, ...]] = {
    "ivn-faq": ("Ivanhoe Mines", "Turquoise Hill"),
    "ivn-legal": ("Ivanhoe Mines", "IVN", "IVPAF"),
    "ivn-isin": ("CA46579R1047", "US46579R2031"),
    "ivn-ir-home": ("Investor Hub", "Ivanhoe Mines"),
    "nintendo-corporate": ("Nintendo Co., Ltd.",),
    "nintendo-stock": ("7974", "Tokyo Stock Exchange"),
    "nintendo-ir-home": ("Nintendo Co., Ltd.", "Investor Relations"),
    "nintendo-ir-financials": ("Financial Data",),
    "nintendo-edinet-annual": ("E02367", "Nintendo Co., Ltd."),
    "citi-ntdoy-adr": (
        "NTDOY",
        "US6544453037",
        "JP3756600007",
        "654445303",
        "Unsponsored",
    ),
}


def known_source_specs(ticker: str) -> tuple[KnownSourceSpec, ...]:
    normalized = ticker.strip().upper()
    if normalized == "IVN":
        return _IVN_SOURCES
    if normalized == "NTDOY":
        return _NINTENDO_SOURCES
    raise ValueError(f"unsupported known foreign identity: {normalized}")


def build_known_request(
    *,
    ticker: str,
    raw_bodies: Mapping[str, bytes],
    db_blob_root: Path,
    apply: bool,
    recorded_at: datetime,
) -> ForeignIssuerBootstrapRequest:
    normalized = ticker.strip().upper()
    specs = known_source_specs(normalized)
    expected = {spec.source_key for spec in specs}
    supplied = set(raw_bodies)
    if supplied != expected:
        raise ValueError(
            "source body set does not match reviewed bundle: "
            f"missing={sorted(expected - supplied)}, extra={sorted(supplied - expected)}"
        )
    sources = tuple(
        ForeignIssuerSource(
            source_key=spec.source_key,
            source_kind=spec.source_kind,
            source_url=spec.source_url,
            media_type=spec.media_type,
            raw_body=raw_bodies[spec.source_key],
        )
        for spec in specs
    )
    if normalized == "IVN":
        return _ivn_request(
            sources=sources,
            blob_root=db_blob_root,
            apply=apply,
            recorded_at=recorded_at,
        )
    return _nintendo_request(
        sources=sources,
        blob_root=db_blob_root,
        apply=apply,
        recorded_at=recorded_at,
    )


def _ivn_request(
    *,
    sources: tuple[ForeignIssuerSource, ...],
    blob_root: Path,
    apply: bool,
    recorded_at: datetime,
) -> ForeignIssuerBootstrapRequest:
    return ForeignIssuerBootstrapRequest(
        ticker="IVN",
        issuer_id="issuer:publisher:ivanhoe-mines",
        legal_name="Ivanhoe Mines Ltd.",
        domicile_country="CA",
        filing_regime="Canadian continuous disclosure",
        profile_source_key="ivn-faq",
        reporting_entity_id="reporting:publisher:ivanhoe-mines",
        reporting_entity_kind="foreign_reporting_entity",
        reporting_entity_display_name="Ivanhoe Mines Ltd.",
        sources=sources,
        issuer_identifiers=(),
        reporting_identifiers=(),
        securities=(
            SecurityClaim(
                security_id="security:isin:CA46579R1047",
                security_kind="common_stock",
                share_class="Common shares",
                relationship_kind="reports_through",
                source_key="ivn-isin",
                identifiers=(
                    SecurityIdentifierClaim(
                        identifier_type="isin",
                        identifier_value="CA46579R1047",
                        authority="issuer_publisher",
                        source_key="ivn-isin",
                    ),
                ),
                listings=(
                    ListingClaim(
                        market_mic="XTSE",
                        ticker="IVN",
                        currency="CAD",
                        authority="issuer_publisher",
                        source_key="ivn-legal",
                    ),
                ),
            ),
        ),
        subject_security_id="security:isin:CA46579R1047",
        authority_surfaces=(
            AuthoritySurfaceClaim(
                surface_key="ir-home",
                surface_kind="ir_home",
                source_url="https://www.ivanhoemines.com/investors/investor-hub/",
                authority_level="publisher",
                source_key="ivn-ir-home",
            ),
        ),
        obligations=(),
        inclusion_state="discovery",
        require_sec=False,
        require_ir=False,
        require_earnings=False,
        blob_root=blob_root,
        apply=apply,
        recorded_at=recorded_at,
    )


def _nintendo_request(
    *,
    sources: tuple[ForeignIssuerSource, ...],
    blob_root: Path,
    apply: bool,
    recorded_at: datetime,
) -> ForeignIssuerBootstrapRequest:
    return ForeignIssuerBootstrapRequest(
        ticker="NTDOY",
        issuer_id="issuer:publisher:nintendo",
        legal_name="Nintendo Co., Ltd.",
        domicile_country="JP",
        filing_regime="Japan FIEA",
        profile_source_key="nintendo-corporate",
        reporting_entity_id="reporting:edinet:E02367",
        reporting_entity_kind="foreign_reporting_entity",
        reporting_entity_display_name="Nintendo Co., Ltd. consolidated group",
        sources=sources,
        issuer_identifiers=(),
        reporting_identifiers=(
            ReportingIdentifierClaim(
                identifier_type="edinet_code",
                identifier_value="E02367",
                authority="regulator",
                source_key="nintendo-edinet-annual",
            ),
        ),
        securities=(
            SecurityClaim(
                security_id="security:isin:JP3756600007",
                security_kind="common_stock",
                share_class="Ordinary shares",
                relationship_kind="reports_through",
                source_key="nintendo-stock",
                identifiers=(
                    SecurityIdentifierClaim(
                        identifier_type="isin",
                        identifier_value="JP3756600007",
                        authority="imported",
                        source_key="citi-ntdoy-adr",
                    ),
                ),
                listings=(
                    ListingClaim(
                        market_mic="XTKS",
                        ticker="7974",
                        currency="JPY",
                        authority="issuer_publisher",
                        source_key="nintendo-stock",
                    ),
                ),
            ),
            SecurityClaim(
                security_id="security:isin:US6544453037",
                security_kind="adr",
                share_class="Unsponsored ADR, 1 ordinary share : 4 ADRs",
                relationship_kind="depositary_receipt_for",
                source_key="citi-ntdoy-adr",
                identifiers=(
                    SecurityIdentifierClaim(
                        identifier_type="isin",
                        identifier_value="US6544453037",
                        authority="imported",
                        source_key="citi-ntdoy-adr",
                    ),
                    SecurityIdentifierClaim(
                        identifier_type="cusip",
                        identifier_value="654445303",
                        authority="imported",
                        source_key="citi-ntdoy-adr",
                    ),
                ),
                listings=(
                    ListingClaim(
                        market_mic="OTCM",
                        ticker="NTDOY",
                        currency="USD",
                        authority="imported",
                        source_key="citi-ntdoy-adr",
                    ),
                ),
            ),
        ),
        subject_security_id="security:isin:US6544453037",
        authority_surfaces=(
            AuthoritySurfaceClaim(
                surface_key="edinet-filings",
                surface_kind="other",
                source_url="https://disclosure2.edinet-fsa.go.jp/",
                authority_level="regulator",
                source_key="nintendo-edinet-annual",
            ),
            AuthoritySurfaceClaim(
                surface_key="ir-home",
                surface_kind="ir_home",
                source_url="https://www.nintendo.co.jp/ir/en/index.html",
                authority_level="publisher",
                source_key="nintendo-ir-home",
            ),
            AuthoritySurfaceClaim(
                surface_key="ir-financials",
                surface_kind="ir_financials",
                source_url="https://www.nintendo.co.jp/ir/en/finance/index.html",
                authority_level="publisher",
                source_key="nintendo-ir-financials",
            ),
        ),
        obligations=(
            SourceObligationClaim(
                authority_kind="edinet",
                document_family="annual_securities_report",
                obligation_state="required",
                completeness_rule="regulator_inventory",
                source_key="nintendo-edinet-annual",
            ),
            SourceObligationClaim(
                authority_kind="issuer_publisher",
                document_family="issuer_financial_statements",
                obligation_state="required",
                completeness_rule="publisher_surface_exhaustion",
                source_key="nintendo-ir-financials",
            ),
            SourceObligationClaim(
                authority_kind="issuer_publisher",
                document_family="issuer_presentations",
                obligation_state="required",
                completeness_rule="publisher_surface_exhaustion",
                source_key="nintendo-ir-home",
            ),
            SourceObligationClaim(
                authority_kind="issuer_publisher",
                document_family="issuer_earnings_materials",
                obligation_state="required",
                completeness_rule="publisher_surface_exhaustion",
                source_key="nintendo-ir-home",
            ),
        ),
        inclusion_state="monitored",
        require_sec=False,
        require_ir=True,
        require_earnings=True,
        blob_root=blob_root,
        apply=apply,
        recorded_at=recorded_at,
    )


def _fetch_sources(
    session: requests.Session,
    ticker: KnownTicker,
) -> dict[str, bytes]:
    bodies: dict[str, bytes] = {}
    headers = {
        "User-Agent": (
            "earnings-summary/1.0 local investor research (contact: data-provenance-local)"
        )
    }
    for spec in known_source_specs(ticker):
        response = session.get(
            spec.source_url,
            headers=headers,
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        if not response.content:
            raise ValueError(f"empty authority response for {spec.source_key}")
        _validate_source_contract(spec, response.content)
        bodies[spec.source_key] = response.content
        _event(
            "foreign_identity_source_fetched",
            ticker=ticker,
            source_key=spec.source_key,
            byte_size=len(response.content),
            sha256=hashlib_sha256(response.content),
        )
    return bodies


def _validate_source_contract(spec: KnownSourceSpec, raw_body: bytes) -> None:
    if spec.media_type == "application/pdf":
        if not raw_body.startswith(b"%PDF-"):
            raise ValueError(f"{spec.source_key} did not return a PDF")
        page_text = PdfReader(io.BytesIO(raw_body)).pages[0].extract_text() or ""
        normalized_text = " ".join(page_text.split())
    else:
        normalized_text = " ".join(
            BeautifulSoup(raw_body, "html.parser").get_text(" ", strip=True).split()
        )
    missing = tuple(
        marker
        for marker in _SOURCE_MARKERS[spec.source_key]
        if marker.casefold() not in normalized_text.casefold()
    )
    if missing:
        raise ValueError(
            f"{spec.source_key} failed reviewed identity contract; missing markers={missing}"
        )


def hashlib_sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _event(event: str, **fields: object) -> None:
    sys.stderr.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument(
        "--ticker",
        choices=("IVN", "NTDOY", "ALL"),
        default="ALL",
    )
    parser.add_argument(
        "--blob-root",
        type=Path,
        default=PROJECT_ROOT / "data" / "evidence" / "blobs",
    )
    parser.add_argument("--apply", action="store_true")
    return parser


def _run_one(
    *,
    db: Path,
    ticker: KnownTicker,
    raw_bodies: Mapping[str, bytes],
    blob_root: Path,
    apply: bool,
    recorded_at: datetime,
) -> ForeignIssuerBootstrapResult:
    role = SQLiteConnectionRole.WRITER if apply else SQLiteConnectionRole.READ_ONLY
    conn = connect_sqlite(db, role=role, schema_preflight=apply)
    try:
        request = build_known_request(
            ticker=ticker,
            raw_bodies=raw_bodies,
            db_blob_root=blob_root,
            apply=apply,
            recorded_at=recorded_at,
        )
        return bootstrap_foreign_issuer(conn, request=request)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    tickers: tuple[KnownTicker, ...] = ("IVN", "NTDOY") if args.ticker == "ALL" else (args.ticker,)
    try:
        with requests.Session() as session:
            bodies = {ticker: _fetch_sources(session, ticker) for ticker in tickers}
        recorded_at = datetime.now(UTC)
        results: list[ForeignIssuerBootstrapResult] = []
        if args.apply:
            with JobLock(
                PROJECT_ROOT,
                "foreign-identity-blocker-resolution",
                [
                    f"sqlite:{args.db.resolve()}",
                    f"evidence-blobs:{args.blob_root.resolve()}",
                ],
            ):
                for ticker in tickers:
                    results.append(
                        _run_one(
                            db=args.db,
                            ticker=ticker,
                            raw_bodies=bodies[ticker],
                            blob_root=args.blob_root,
                            apply=True,
                            recorded_at=recorded_at,
                        )
                    )
        else:
            for ticker in tickers:
                results.append(
                    _run_one(
                        db=args.db,
                        ticker=ticker,
                        raw_bodies=bodies[ticker],
                        blob_root=args.blob_root,
                        apply=False,
                        recorded_at=recorded_at,
                    )
                )
    except Exception as exc:
        _event(
            "foreign_identity_resolution_failed",
            error_type=type(exc).__name__,
            error=redact(exc),
        )
        return 1
    sys.stdout.write(
        json.dumps(
            [result.model_dump(mode="json") for result in results],
            sort_keys=True,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
