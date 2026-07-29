"""Foreign issuers preserve regulator, ordinary-share, and ADR boundaries."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config
from pydantic import ValidationError

from alembic import command
from execution.resolve_foreign_identity_blockers import (
    build_known_request,
    known_source_specs,
)
from provenance.foreign_issuer_bootstrap import (
    AuthoritySurfaceClaim,
    ForeignIssuerBootstrapRequest,
    ForeignIssuerSource,
    IssuerIdentifierClaim,
    ListingClaim,
    ReportingIdentifierClaim,
    SecurityClaim,
    SecurityIdentifierClaim,
    SourceObligationClaim,
    bootstrap_foreign_issuer,
)
from provenance.issuer_registry import IssuerRegistry
from provenance.reporting_entity_registry import ReportingEntityRegistry

ROOT = Path(__file__).resolve().parents[1]
HEAD = "0231_legacy_document_evidence_bindings"
STAMP = datetime(2026, 7, 28, 1, 0, tzinfo=UTC)


def _database(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "foreign-issuer.db"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, HEAD)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _source(key: str, url: str, body: bytes) -> ForeignIssuerSource:
    return ForeignIssuerSource(
        source_key=key,
        source_kind="issuer_identity_authority",
        source_url=url,
        media_type="text/html",
        raw_body=body,
    )


def _nintendo_request(tmp_path: Path, *, apply: bool) -> ForeignIssuerBootstrapRequest:
    corporate = _source(
        "nintendo-corporate",
        "https://www.nintendo.co.jp/corporate/en/outline/index.html",
        b"<html>Nintendo Co., Ltd. Kyoto Japan</html>",
    )
    stock = _source(
        "nintendo-stock",
        "https://www.nintendo.co.jp/ir/en/stock/information/index.html",
        b"<html>Stock code 7974 Tokyo Stock Exchange Prime Market</html>",
    )
    citi = _source(
        "citi-adr",
        "https://depositaryreceipts.citi.com/adr/guides/pgm_d.aspx?cusip=654445303",
        (
            b"<html>Nintendo Co., Ltd. NTDOY JP3756600007 "
            b"US6544453037 654445303 1:4 unsponsored</html>"
        ),
    )
    return ForeignIssuerBootstrapRequest(
        ticker="NTDOY",
        issuer_id="issuer:publisher:nintendo",
        legal_name="Nintendo Co., Ltd.",
        domicile_country="JP",
        filing_regime="Japan FIEA",
        profile_source_key="nintendo-corporate",
        reporting_entity_id="reporting:publisher:nintendo-consolidated",
        reporting_entity_kind="foreign_reporting_entity",
        reporting_entity_display_name="Nintendo Co., Ltd. consolidated group",
        sources=(corporate, stock, citi),
        issuer_identifiers=(),
        reporting_identifiers=(),
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
                        authority="issuer_publisher",
                        source_key="citi-adr",
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
                source_key="citi-adr",
                identifiers=(
                    SecurityIdentifierClaim(
                        identifier_type="isin",
                        identifier_value="US6544453037",
                        authority="issuer_publisher",
                        source_key="citi-adr",
                    ),
                    SecurityIdentifierClaim(
                        identifier_type="cusip",
                        identifier_value="654445303",
                        authority="issuer_publisher",
                        source_key="citi-adr",
                    ),
                ),
                listings=(
                    ListingClaim(
                        market_mic="OTCM",
                        ticker="NTDOY",
                        currency="USD",
                        authority="issuer_publisher",
                        source_key="citi-adr",
                    ),
                ),
            ),
        ),
        subject_security_id="security:isin:US6544453037",
        authority_surfaces=(
            AuthoritySurfaceClaim(
                surface_key="ir-home",
                surface_kind="ir_home",
                source_url="https://www.nintendo.co.jp/ir/en/index.html",
                authority_level="publisher",
                source_key="nintendo-stock",
            ),
            AuthoritySurfaceClaim(
                surface_key="ir-financials",
                surface_kind="ir_financials",
                source_url="https://www.nintendo.co.jp/ir/en/finance/index.html",
                authority_level="publisher",
                source_key="nintendo-stock",
            ),
        ),
        obligations=(
            SourceObligationClaim(
                authority_kind="edinet",
                document_family="annual_securities_report",
                obligation_state="required",
                completeness_rule="regulator_inventory",
                source_key="nintendo-corporate",
            ),
            SourceObligationClaim(
                authority_kind="issuer_publisher",
                document_family="issuer_financial_statements",
                obligation_state="required",
                completeness_rule="publisher_surface_exhaustion",
                source_key="nintendo-stock",
            ),
        ),
        inclusion_state="monitored",
        require_sec=False,
        require_ir=True,
        require_earnings=True,
        blob_root=tmp_path / "blobs",
        apply=apply,
        recorded_at=STAMP,
    )


def _ivn_request(tmp_path: Path, *, apply: bool) -> ForeignIssuerBootstrapRequest:
    faq = _source(
        "ivn-faq",
        "https://www.ivanhoemines.com/investors/investor-faqs/",
        (
            b"<html>Ivanhoe Mines Ltd. SEDAR original Ivanhoe changed its "
            b"name to Turquoise Hill Resources in August 2012</html>"
        ),
    )
    sedar = _source(
        "sedar-profile",
        "https://www.sedarplus.ca/csa-party/records/document.html?id=ivanhoe",
        b"<html>Ivanhoe Mines Ltd. profile 000033595</html>",
    )
    isin = _source(
        "ivn-isin",
        "https://www.ivanhoemines.com/investors/investor-faqs/faq/what-are-ivanhoe-mines-isin-numbers/",
        b"<html>Canadian ISIN CA46579R1047 US ISIN US46579R2031</html>",
    )
    return ForeignIssuerBootstrapRequest(
        ticker="IVN",
        issuer_id="issuer:sedar:000033595",
        legal_name="Ivanhoe Mines Ltd.",
        domicile_country="CA",
        filing_regime="Canadian continuous disclosure",
        profile_source_key="sedar-profile",
        reporting_entity_id="reporting:sedar:000033595",
        reporting_entity_kind="foreign_reporting_entity",
        reporting_entity_display_name="Ivanhoe Mines Ltd.",
        sources=(faq, sedar, isin),
        issuer_identifiers=(
            IssuerIdentifierClaim(
                identifier_type="sedar_profile",
                identifier_value="000033595",
                authority="regulator",
                source_key="sedar-profile",
            ),
        ),
        reporting_identifiers=(
            ReportingIdentifierClaim(
                identifier_type="sedar_profile",
                identifier_value="000033595",
                authority="regulator",
                source_key="sedar-profile",
            ),
        ),
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
                        source_key="ivn-faq",
                    ),
                ),
            ),
        ),
        subject_security_id="security:isin:CA46579R1047",
        authority_surfaces=(
            AuthoritySurfaceClaim(
                surface_key="sedar-plus",
                surface_kind="other",
                source_url="https://www.sedarplus.ca/",
                authority_level="regulator",
                source_key="sedar-profile",
            ),
        ),
        obligations=(),
        inclusion_state="discovery",
        require_sec=False,
        require_ir=False,
        require_earnings=False,
        blob_root=tmp_path / "blobs",
        apply=apply,
        recorded_at=STAMP,
    )


def test_nintendo_adr_stays_distinct_from_ordinary_and_replays_exactly(
    tmp_path: Path,
) -> None:
    conn = _database(tmp_path)
    request = _nintendo_request(tmp_path, apply=True)

    dry_run = bootstrap_foreign_issuer(
        conn,
        request=request.model_copy(update={"apply": False}),
    )
    first = bootstrap_foreign_issuer(conn, request=request)
    second = bootstrap_foreign_issuer(conn, request=request)
    refreshed = bootstrap_foreign_issuer(
        conn,
        request=request.model_copy(
            update={
                "sources": tuple(
                    source.model_copy(update={"raw_body": source.raw_body + b" "})
                    for source in request.sources
                ),
                "recorded_at": STAMP + timedelta(hours=1),
            }
        ),
    )

    assert dry_run.mode == "dry_run"
    assert first.records_created > 0
    assert second.records_created == 0
    assert refreshed.records_created == 9
    assert conn.execute(
        "SELECT security_kind, COUNT(*) FROM securities "
        "GROUP BY security_kind ORDER BY security_kind"
    ).fetchall() == [("adr", 1), ("common_stock", 1)]
    assert (
        IssuerRegistry(conn).resolve_listing("OTCM", "NTDOY", knowledge_at=STAMP).security_id
        == "security:isin:US6544453037"
    )
    assert (
        IssuerRegistry(conn).resolve_listing("XTKS", "7974", knowledge_at=STAMP).security_id
        == "security:isin:JP3756600007"
    )
    subject = ReportingEntityRegistry(conn).canonicalize_recorded_subject(
        "legacy-ticker:NTDOY",
        knowledge_at=STAMP,
    )
    assert subject.issuer_id == "issuer:publisher:nintendo"
    assert subject.security_id == "security:isin:US6544453037"
    assert conn.execute(
        "SELECT security_id, relationship_kind "
        "FROM v_security_reporting_entities_current ORDER BY security_id"
    ).fetchall() == [
        ("security:isin:JP3756600007", "reports_through"),
        ("security:isin:US6544453037", "depositary_receipt_for"),
    ]
    assert conn.execute(
        "SELECT outcome FROM v_legacy_issuer_bindings_current "
        "WHERE recorded_issuer_id = 'legacy-ticker:NTDOY'"
    ).fetchone() == ("selected",)
    assert conn.execute("SELECT COUNT(*) FROM evidence_source_observations").fetchone() == (6,)
    assert conn.execute("SELECT COUNT(*) FROM issuer_authority_surface_revisions").fetchone() == (
        2,
    )
    assert conn.execute("SELECT COUNT(*) FROM security_identifier_assertions").fetchone() == (3,)
    assert conn.execute("SELECT COUNT(*) FROM security_listing_assertions").fetchone() == (2,)
    conn.close()


def test_ivn_uses_sedar_identity_and_never_imports_sec_predecessor(
    tmp_path: Path,
) -> None:
    conn = _database(tmp_path)
    result = bootstrap_foreign_issuer(
        conn,
        request=_ivn_request(tmp_path, apply=True),
    )

    assert result.canonical_issuer_id == "issuer:sedar:000033595"
    assert (
        IssuerRegistry(conn)
        .resolve_identifier(
            "sedar_profile",
            "000033595",
            knowledge_at=STAMP,
        )
        .issuer_id
        == "issuer:sedar:000033595"
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM issuer_identifier_assertions WHERE identifier_type = 'sec_cik'"
    ).fetchone() == (0,)
    assert conn.execute(
        "SELECT issuer_id, reporting_entity_id, security_id "
        "FROM v_recorded_subject_bindings_current "
        "WHERE recorded_issuer_id = 'legacy-ticker:IVN'"
    ).fetchone() == (
        "issuer:sedar:000033595",
        "reporting:sedar:000033595",
        "security:isin:CA46579R1047",
    )
    assert conn.execute(
        "SELECT inclusion_state, require_sec, require_ir, require_earnings "
        "FROM v_issuer_reporting_scope_current"
    ).fetchone() == ("discovery", 0, 0, 0)
    conn.close()


def test_bundle_rejects_missing_sources_and_unowned_subject_security(
    tmp_path: Path,
) -> None:
    request = _nintendo_request(tmp_path, apply=False)
    with pytest.raises(ValidationError):
        ForeignIssuerBootstrapRequest.model_validate(
            {
                **request.model_dump(),
                "profile_source_key": "missing-source",
            }
        )
    with pytest.raises(ValidationError):
        ForeignIssuerBootstrapRequest.model_validate(
            {
                **request.model_dump(),
                "subject_security_id": "security:other-company",
            }
        )


def test_known_resolution_profiles_keep_foreign_security_boundaries(
    tmp_path: Path,
) -> None:
    nintendo_specs = known_source_specs("NTDOY")
    nintendo = build_known_request(
        ticker="NTDOY",
        raw_bodies={spec.source_key: b"<html>authority evidence</html>" for spec in nintendo_specs},
        db_blob_root=tmp_path / "blobs",
        apply=False,
        recorded_at=STAMP,
    )
    ivn_specs = known_source_specs("IVN")
    ivn = build_known_request(
        ticker="IVN",
        raw_bodies={spec.source_key: b"<html>authority evidence</html>" for spec in ivn_specs},
        db_blob_root=tmp_path / "blobs",
        apply=False,
        recorded_at=STAMP,
    )

    assert {security.security_kind for security in nintendo.securities} == {
        "adr",
        "common_stock",
    }
    assert nintendo.subject_security_id == "security:isin:US6544453037"
    assert all(
        identifier.identifier_type != "sec_cik" for identifier in nintendo.issuer_identifiers
    )
    assert ivn.issuer_id == "issuer:publisher:ivanhoe-mines"
    assert ivn.issuer_identifiers == ()
    assert ivn.require_sec is False
