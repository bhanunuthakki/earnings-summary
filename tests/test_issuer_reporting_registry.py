"""Canonical issuer identity, authority-surface, and reporting-scope contracts."""

from __future__ import annotations

import hashlib
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
from provenance.inventory_identity import (
    InventoryIdentityError,
    resolve_ir_inventory_subject,
    resolve_sec_inventory_subject,
)
from provenance.issuer_registry import (
    AuthoritySurfaceRevision,
    IdentifierAssertion,
    IdentifierResolution,
    IssuerEntity,
    IssuerProfileRevision,
    IssuerRegistry,
    LegacyIssuerBindingRevision,
    ListingAssertion,
    ListingResolution,
    ReportingScopeRevision,
    Security,
    UnresolvedIssuerIdentityError,
    identifier_candidate_digest,
    listing_candidate_digest,
)

ROOT = Path(__file__).resolve().parents[1]
PRIOR = "0226_fact_cutover_performance_indexes"
HEAD = "0227_issuer_reporting_registry"
STAMP = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)
SHA = "a" * 64


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _conn(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "issuer-registry.db"
    config = _config(path)
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, HEAD)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _source_observation(conn: sqlite3.Connection, tmp_path: Path) -> None:
    body = b'{"ticker":"ACME","cik_str":123456}'
    digest = hashlib.sha256(body).hexdigest()
    path = tmp_path / digest
    path.write_bytes(body)
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=digest,
            byte_size=len(body),
            media_type="application/json",
            storage_uri=path.resolve().as_uri(),
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id="obs-sec-tickers",
            idempotency_key="obs-sec-tickers",
            source_kind="sec_company_tickers",
            source_url="https://www.sec.gov/files/company_tickers.json",
            blob_sha256=digest,
            source_published_at=None,
            filing_at=None,
            accepted_at=None,
            observed_at=STAMP,
            retrieved_at=STAMP,
            retrieval_config_sha256=SHA,
            collector_code_version="issuer-registry-fixture@1",
        )
    )
    conn.commit()


def _seed_entity(registry: IssuerRegistry) -> None:
    assert registry.persist(
        IssuerEntity(
            issuer_id="issuer-acme",
            idempotency_key="issuer-acme",
            entity_kind="operating_company",
            created_at=STAMP,
        )
    ).created
    assert registry.persist(
        IssuerProfileRevision(
            profile_revision_id="profile-acme-1",
            idempotency_key="profile-acme-1",
            issuer_id="issuer-acme",
            revision=1,
            legal_name="Acme Corporation",
            domicile_country="US",
            filing_regime="10-K",
            fiscal_year_end="12-31",
            status="active",
            decision_kind="imported",
            reason_code="tracked_company_seed",
            reason_details=(("ticker", "ACME"),),
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    ).created


def test_conflicting_identifier_assertions_require_explicit_resolution(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    _source_observation(conn, tmp_path)
    registry = IssuerRegistry(conn)
    _seed_entity(registry)
    registry.persist(
        IssuerEntity(
            issuer_id="issuer-other",
            idempotency_key="issuer-other",
            entity_kind="operating_company",
            created_at=STAMP,
        )
    )
    assertions = (
        IdentifierAssertion(
            assertion_id="assert-cik-acme",
            idempotency_key="assert-cik-acme",
            issuer_id="issuer-acme",
            identifier_type="sec_cik",
            identifier_value="123456",
            normalized_value="0000123456",
            authority="sec_registry",
            source_observation_id="obs-sec-tickers",
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        ),
        IdentifierAssertion(
            assertion_id="assert-cik-other",
            idempotency_key="assert-cik-other",
            issuer_id="issuer-other",
            identifier_type="sec_cik",
            identifier_value="0000123456",
            normalized_value="0000123456",
            authority="imported",
            source_observation_id="obs-sec-tickers",
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        ),
    )
    for assertion in assertions:
        registry.persist(assertion)

    with pytest.raises(UnresolvedIssuerIdentityError):
        registry.resolve_identifier(
            "sec_cik",
            "123456",
            knowledge_at=STAMP,
        )
    assert conn.execute("SELECT COUNT(*) FROM v_issuer_identifiers_canonical").fetchone() == (0,)
    resolution = IdentifierResolution(
        resolution_id="resolve-cik-acme-1",
        idempotency_key="resolve-cik-acme-1",
        resolution_key="sec_cik:0000123456",
        revision=1,
        outcome="selected",
        selected_assertion_id="assert-cik-acme",
        candidate_digest_sha256=identifier_candidate_digest(assertions),
        policy_name="authority_rank_then_recency",
        policy_version="1",
        policy_config_sha256=SHA,
        reason_code="sec_registry_preferred",
        reason_details=(("dissenting_assertion_id", "assert-cik-other"),),
        material_dissent=True,
        effective_at=STAMP,
        knowledge_at=STAMP,
        recorded_at=STAMP,
    )
    registry.persist(resolution)
    assert conn.execute(
        "SELECT issuer_id, normalized_value, material_dissent FROM v_issuer_identifiers_canonical"
    ).fetchone() == ("issuer-acme", "0000123456", 1)
    resolved = registry.resolve_identifier(
        "sec_cik",
        "123456",
        knowledge_at=STAMP,
    )
    assert resolved.issuer_id == "issuer-acme"
    assert resolved.legal_name == "Acme Corporation"
    assert resolved.material_dissent
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "DELETE FROM issuer_identifier_assertions WHERE assertion_id = ?",
            ("assert-cik-other",),
        )
    conn.close()


def test_verified_authority_surface_is_evidence_backed_and_revisioned(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    registry = IssuerRegistry(conn)
    _seed_entity(registry)
    with pytest.raises(ValueError, match="source evidence"):
        registry.persist(
            AuthoritySurfaceRevision(
                surface_revision_id="surface-ir-1",
                idempotency_key="surface-ir-1",
                issuer_id="issuer-acme",
                surface_key="ir-archive",
                revision=1,
                surface_kind="ir_archive",
                source_url="https://ir.acme.test/archive",
                status="verified",
                authority_level="publisher",
                source_observation_id=None,
                verification_method="publisher_archive",
                effective_at=STAMP,
                knowledge_at=STAMP,
                recorded_at=STAMP,
            )
        )
    _source_observation(conn, tmp_path)
    assert registry.persist(
        AuthoritySurfaceRevision(
            surface_revision_id="surface-ir-1",
            idempotency_key="surface-ir-1",
            issuer_id="issuer-acme",
            surface_key="ir-archive",
            revision=1,
            surface_kind="ir_archive",
            source_url="https://ir.acme.test/archive",
            status="verified",
            authority_level="publisher",
            source_observation_id="obs-sec-tickers",
            verification_method="publisher_archive",
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    ).created
    assert conn.execute(
        "SELECT source_url FROM v_issuer_authority_surfaces_current"
    ).fetchone() == ("https://ir.acme.test/archive",)
    surfaces = registry.source_authority(
        "issuer-acme",
        "ir_archive",
        knowledge_at=STAMP,
    )
    assert tuple(surface.source_url for surface in surfaces) == ("https://ir.acme.test/archive",)
    conn.close()


def test_security_listing_is_distinct_from_reporting_issuer_and_resolved(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    _source_observation(conn, tmp_path)
    registry = IssuerRegistry(conn)
    _seed_entity(registry)
    registry.persist(
        Security(
            security_id="security-acme-class-a",
            idempotency_key="security-acme-class-a",
            issuer_id="issuer-acme",
            security_kind="common_stock",
            share_class="Class A",
            created_at=STAMP,
        )
    )
    assertions = (
        ListingAssertion(
            assertion_id="listing-acme-xnas",
            idempotency_key="listing-acme-xnas",
            security_id="security-acme-class-a",
            market_mic="XNAS",
            ticker="ACME",
            normalized_ticker="ACME",
            currency="USD",
            status="listed",
            authority="exchange_registry",
            source_observation_id="obs-sec-tickers",
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        ),
    )
    registry.persist(assertions[0])
    registry.persist(
        ListingResolution(
            resolution_id="listing-resolution-acme-1",
            idempotency_key="listing-resolution-acme-1",
            resolution_key="listing:XNAS:ACME",
            revision=1,
            outcome="selected",
            selected_assertion_id="listing-acme-xnas",
            candidate_digest_sha256=listing_candidate_digest(assertions),
            policy_name="exchange_authority",
            policy_version="1",
            policy_config_sha256=SHA,
            reason_code="exchange_registry_selected",
            reason_details=(("source_observation_id", "obs-sec-tickers"),),
            material_dissent=False,
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    )
    assert conn.execute(
        "SELECT issuer_id, security_id, market_mic, normalized_ticker "
        "FROM v_security_listings_canonical"
    ).fetchone() == (
        "issuer-acme",
        "security-acme-class-a",
        "XNAS",
        "ACME",
    )
    listing = registry.resolve_listing("xnas", "acme", knowledge_at=STAMP)
    assert listing.issuer_id == "issuer-acme"
    assert listing.security_id == "security-acme-class-a"
    assert listing.market_mic == "XNAS"
    conn.close()


def test_legacy_binding_canonicalizes_without_rewriting_evidence(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    _source_observation(conn, tmp_path)
    registry = IssuerRegistry(conn)
    _seed_entity(registry)
    assert registry.persist(
        LegacyIssuerBindingRevision(
            binding_revision_id="legacy-binding-acme-1",
            idempotency_key="legacy-binding-acme-1",
            recorded_issuer_id="legacy-ticker:ACME",
            revision=1,
            issuer_id="issuer-acme",
            outcome="selected",
            decision_kind="manual",
            reason_code="verified_identity_bridge",
            reason_details=(("legacy_ticker", "ACME"),),
            material_dissent=False,
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    ).created
    assert conn.execute(
        "SELECT recorded_issuer_id, canonical_issuer_id FROM v_legacy_issuer_bindings_current"
    ).fetchone() == ("legacy-ticker:ACME", "issuer-acme")
    canonical = registry.canonicalize_recorded_issuer(
        "legacy-ticker:ACME",
        knowledge_at=STAMP,
    )
    assert canonical.issuer_id == "issuer-acme"
    EvidenceLedger(conn).persist(
        DocumentVersion(
            document_version_id="legacy-document-acme",
            document_key="legacy-document-acme",
            version_sequence=1,
            observation_id="obs-sec-tickers",
            blob_sha256=conn.execute(
                "SELECT blob_sha256 FROM evidence_source_observations "
                "WHERE observation_id = 'obs-sec-tickers'"
            ).fetchone()[0],
            issuer_id="legacy-ticker:ACME",
            ticker="ACME",
            document_type="filing",
            form_type="10-K",
            language="en",
            recorded_at=STAMP,
        )
    )
    assert conn.execute(
        "SELECT recorded_issuer_id, issuer_id FROM v_evidence_document_versions_canonical"
    ).fetchone() == ("legacy-ticker:ACME", "issuer-acme")
    conn.close()


def test_reporting_scope_separates_research_from_discovery_and_bounds_history(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    registry = IssuerRegistry(conn)
    _seed_entity(registry)
    assert registry.persist(
        ReportingScopeRevision(
            scope_revision_id="scope-acme-1",
            idempotency_key="scope-acme-1",
            scope_key="investor-research",
            issuer_id="issuer-acme",
            revision=1,
            inclusion_state="core",
            history_policy="since_date",
            history_start=datetime(2015, 1, 1, tzinfo=UTC),
            latest_years=None,
            require_sec=True,
            require_ir=True,
            require_earnings=True,
            decision_kind="manual",
            reason_code="portfolio_holding",
            reason_details=(("list_type", "portfolio"),),
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    ).created
    assert conn.execute(
        "SELECT inclusion_state, history_policy, require_sec, require_ir, require_earnings "
        "FROM v_issuer_reporting_scope_current"
    ).fetchone() == ("core", "since_date", 1, 1, 1)
    conn.close()


def test_inventory_subject_rejects_cross_issuer_and_unverified_source_urls(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    _source_observation(conn, tmp_path)
    registry = IssuerRegistry(conn)
    _seed_entity(registry)
    assertion = IdentifierAssertion(
        assertion_id="assert-cik-acme",
        idempotency_key="assert-cik-acme",
        issuer_id="issuer-acme",
        identifier_type="sec_cik",
        identifier_value="123456",
        normalized_value="0000123456",
        authority="sec_registry",
        source_observation_id="obs-sec-tickers",
        effective_at=STAMP,
        knowledge_at=STAMP,
        recorded_at=STAMP,
    )
    registry.persist(assertion)
    registry.persist(
        IdentifierResolution(
            resolution_id="resolve-cik-acme",
            idempotency_key="resolve-cik-acme",
            resolution_key="sec_cik:0000123456",
            revision=1,
            outcome="selected",
            selected_assertion_id=assertion.assertion_id,
            candidate_digest_sha256=identifier_candidate_digest((assertion,)),
            policy_name="sec_registry",
            policy_version="1",
            policy_config_sha256=SHA,
            reason_code="sec_registry_selected",
            reason_details=(("assertion_id", assertion.assertion_id),),
            material_dissent=False,
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    )
    registry.persist(
        LegacyIssuerBindingRevision(
            binding_revision_id="binding-acme",
            idempotency_key="binding-acme",
            recorded_issuer_id="legacy-ticker:ACME",
            revision=1,
            issuer_id="issuer-acme",
            outcome="selected",
            decision_kind="deterministic",
            reason_code="sec_ticker_mapping",
            reason_details=(("ticker", "ACME"),),
            material_dissent=False,
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    )
    for key, kind, url in (
        (
            "sec-submissions",
            "sec_submissions",
            "https://data.sec.gov/submissions/CIK0000123456.json",
        ),
        ("ir-home", "ir_home", "https://ir.acme.test/"),
    ):
        registry.persist(
            AuthoritySurfaceRevision(
                surface_revision_id=f"surface-{key}",
                idempotency_key=f"surface-{key}",
                issuer_id="issuer-acme",
                surface_key=key,
                revision=1,
                surface_kind=kind,
                source_url=url,
                status="verified",
                authority_level="regulator" if kind == "sec_submissions" else "publisher",
                source_observation_id="obs-sec-tickers",
                verification_method="fixture",
                effective_at=STAMP,
                knowledge_at=STAMP,
                recorded_at=STAMP,
            )
        )

    sec = resolve_sec_inventory_subject(
        conn,
        ticker="ACME",
        cik="123456",
        knowledge_at=STAMP,
    )
    assert sec.issuer_id == "issuer-acme"
    ir = resolve_ir_inventory_subject(
        conn,
        issuer_id="issuer-acme",
        ticker="ACME",
        ir_url="https://ir.acme.test/",
        knowledge_at=STAMP,
    )
    assert ir.issuer_id == "issuer-acme"
    with pytest.raises(InventoryIdentityError, match="ticker"):
        resolve_sec_inventory_subject(
            conn,
            ticker="WRONG",
            cik="123456",
            knowledge_at=STAMP,
        )
    with pytest.raises(InventoryIdentityError, match="authority"):
        resolve_ir_inventory_subject(
            conn,
            issuer_id="issuer-acme",
            ticker="ACME",
            ir_url="https://lookalike.test/",
            knowledge_at=STAMP,
        )
    conn.close()


def test_migration_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "round-trip.db"
    config = _config(path)
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, PRIOR)
    command.upgrade(config, HEAD)
    command.downgrade(config, PRIOR)
    conn = sqlite3.connect(path)
    try:
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='issuer_entities'"
            ).fetchone()
            is None
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
