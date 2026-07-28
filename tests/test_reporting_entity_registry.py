"""Reporting entities, securities, and source obligations are distinct identities."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from alembic.config import Config

from alembic import command
from provenance.evidence_ledger import (
    ContentBlob,
    DocumentVersion,
    EvidenceLedger,
    SourceObservation,
)
from provenance.integrity_audit import AuditOptions, audit_connection
from provenance.issuer_registry import (
    IssuerEntity,
    IssuerRegistry,
    LegacyIssuerBindingRevision,
    ReportingScopeRevision,
    Security,
)
from provenance.reporting_entity_registry import (
    EvidenceSubjectBindingRevision,
    ReportingEntity,
    ReportingEntityIdentifierAssertion,
    ReportingEntityIdentifierResolution,
    ReportingEntityRegistry,
    SecurityIdentifierAssertion,
    SecurityIdentifierResolution,
    SecurityReportingEntityRevision,
    SourceObligationRevision,
    reporting_identifier_candidate_digest,
    security_identifier_candidate_digest,
)

ROOT = Path(__file__).resolve().parents[1]
HEAD = "0230_evidence_subject_bindings"
STAMP = datetime(2026, 7, 27, 21, 0, tzinfo=UTC)
SHA = "a" * 64


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _conn(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "reporting-entity-registry.db"
    config = _config(path)
    command.stamp(config, "0213_decision_draft_provider_id")
    command.upgrade(config, HEAD)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    payload = b'{"registrant":"American Century ETF Trust"}'
    digest = hashlib.sha256(payload).hexdigest()
    evidence_path = tmp_path / digest
    evidence_path.write_bytes(payload)
    ledger = EvidenceLedger(conn)
    ledger.persist(
        ContentBlob(
            sha256=digest,
            byte_size=len(payload),
            media_type="application/json",
            storage_uri=evidence_path.resolve().as_uri(),
            recorded_at=STAMP,
        )
    )
    ledger.persist(
        SourceObservation(
            observation_id="obs-sec-series",
            idempotency_key="obs-sec-series",
            source_kind="sec_filing_package",
            source_url="https://www.sec.gov/Archives/edgar/data/1710607/index.json",
            blob_sha256=digest,
            source_published_at=None,
            filing_at=None,
            accepted_at=None,
            observed_at=STAMP,
            retrieved_at=STAMP,
            retrieval_config_sha256=SHA,
            collector_code_version="reporting-entity-fixture@1",
        )
    )
    return conn


def _resolution(
    assertion: ReportingEntityIdentifierAssertion,
) -> ReportingEntityIdentifierResolution:
    return ReportingEntityIdentifierResolution(
        resolution_id=f"resolve-{assertion.assertion_id}",
        idempotency_key=f"resolve-{assertion.assertion_id}",
        resolution_key=assertion.resolution_key,
        revision=1,
        outcome="selected",
        selected_assertion_id=assertion.assertion_id,
        candidate_digest_sha256=reporting_identifier_candidate_digest((assertion,)),
        policy_name="regulator_exact_match",
        policy_version="1",
        policy_config_sha256=SHA,
        reason_code="unique_regulator_identifier",
        reason_details=(("source_observation_id", "obs-sec-series"),),
        material_dissent=False,
        effective_at=STAMP,
        knowledge_at=STAMP,
        recorded_at=STAMP,
    )


def test_fund_series_do_not_collapse_when_they_share_one_legal_registrant(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    issuer_registry = IssuerRegistry(conn)
    issuer_registry.persist(
        IssuerEntity(
            issuer_id="issuer-american-century-etf-trust",
            idempotency_key="issuer-american-century-etf-trust",
            entity_kind="fund",
            created_at=STAMP,
        )
    )
    for security_id in ("security-avdv", "security-avuv"):
        issuer_registry.persist(
            Security(
                security_id=security_id,
                idempotency_key=security_id,
                issuer_id="issuer-american-century-etf-trust",
                security_kind="fund_share",
                share_class=None,
                created_at=STAMP,
            )
        )

    registry = ReportingEntityRegistry(conn)
    registrant = ReportingEntity(
        reporting_entity_id="reporting-sec-0001710607",
        idempotency_key="reporting-sec-0001710607",
        issuer_id="issuer-american-century-etf-trust",
        reporting_entity_kind="legal_registrant",
        display_name="American Century ETF Trust",
        created_at=STAMP,
    )
    avdv = ReportingEntity(
        reporting_entity_id="reporting-sec-series-s000066457",
        idempotency_key="reporting-sec-series-s000066457",
        issuer_id="issuer-american-century-etf-trust",
        reporting_entity_kind="fund_series",
        display_name="Avantis International Small Cap Value ETF",
        created_at=STAMP,
    )
    avuv = ReportingEntity(
        reporting_entity_id="reporting-sec-series-s000066459",
        idempotency_key="reporting-sec-series-s000066459",
        issuer_id="issuer-american-century-etf-trust",
        reporting_entity_kind="fund_series",
        display_name="Avantis U.S. Small Cap Value ETF",
        created_at=STAMP,
    )
    for entity in (registrant, avdv, avuv):
        assert registry.persist(entity).created

    assertions = (
        ReportingEntityIdentifierAssertion(
            assertion_id="assert-trust-cik",
            idempotency_key="assert-trust-cik",
            reporting_entity_id=registrant.reporting_entity_id,
            identifier_type="sec_cik",
            identifier_value="1710607",
            normalized_value="0001710607",
            authority="sec_registry",
            source_observation_id="obs-sec-series",
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        ),
        ReportingEntityIdentifierAssertion(
            assertion_id="assert-avdv-series",
            idempotency_key="assert-avdv-series",
            reporting_entity_id=avdv.reporting_entity_id,
            identifier_type="sec_series_id",
            identifier_value="S000066457",
            normalized_value="S000066457",
            authority="sec_registry",
            source_observation_id="obs-sec-series",
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        ),
        ReportingEntityIdentifierAssertion(
            assertion_id="assert-avuv-series",
            idempotency_key="assert-avuv-series",
            reporting_entity_id=avuv.reporting_entity_id,
            identifier_type="sec_series_id",
            identifier_value="S000066459",
            normalized_value="S000066459",
            authority="sec_registry",
            source_observation_id="obs-sec-series",
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        ),
    )
    for assertion in assertions:
        registry.persist(assertion)
        registry.persist(_resolution(assertion))

    security_assertions = (
        SecurityIdentifierAssertion(
            assertion_id="assert-avdv-class",
            idempotency_key="assert-avdv-class",
            security_id="security-avdv",
            identifier_type="sec_class_contract_id",
            identifier_value="C000214352",
            normalized_value="C000214352",
            authority="sec_registry",
            source_observation_id="obs-sec-series",
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        ),
        SecurityIdentifierAssertion(
            assertion_id="assert-avuv-class",
            idempotency_key="assert-avuv-class",
            security_id="security-avuv",
            identifier_type="sec_class_contract_id",
            identifier_value="C000214354",
            normalized_value="C000214354",
            authority="sec_registry",
            source_observation_id="obs-sec-series",
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        ),
    )
    for assertion in security_assertions:
        registry.persist(assertion)
        registry.persist(
            SecurityIdentifierResolution(
                resolution_id=f"resolve-{assertion.assertion_id}",
                idempotency_key=f"resolve-{assertion.assertion_id}",
                resolution_key=assertion.resolution_key,
                revision=1,
                outcome="selected",
                selected_assertion_id=assertion.assertion_id,
                candidate_digest_sha256=security_identifier_candidate_digest((assertion,)),
                policy_name="regulator_exact_match",
                policy_version="1",
                policy_config_sha256=SHA,
                reason_code="unique_regulator_identifier",
                reason_details=(("source_observation_id", "obs-sec-series"),),
                material_dissent=False,
                effective_at=STAMP,
                knowledge_at=STAMP,
                recorded_at=STAMP,
            )
        )

    for relation_id, security_id, entity in (
        ("relation-avdv", "security-avdv", avdv),
        ("relation-avuv", "security-avuv", avuv),
    ):
        registry.persist(
            SecurityReportingEntityRevision(
                relationship_revision_id=relation_id,
                idempotency_key=relation_id,
                relationship_key=f"{security_id}:reports-through",
                revision=1,
                security_id=security_id,
                reporting_entity_id=entity.reporting_entity_id,
                relationship_kind="reports_through",
                decision_kind="deterministic",
                reason_code="sec_series_class_mapping",
                reason_details=(("source_observation_id", "obs-sec-series"),),
                effective_at=STAMP,
                knowledge_at=STAMP,
                recorded_at=STAMP,
            )
        )

    source_blob_sha = str(
        conn.execute(
            "SELECT blob_sha256 FROM evidence_source_observations "
            "WHERE observation_id = 'obs-sec-series'"
        ).fetchone()[0]
    )
    EvidenceLedger(conn).persist(
        DocumentVersion(
            document_version_id="document-avdv-profile",
            document_key="legacy-ticker:AVDV:profile",
            version_sequence=1,
            observation_id="obs-sec-series",
            blob_sha256=source_blob_sha,
            issuer_id="legacy-ticker:AVDV",
            ticker="AVDV",
            document_type="profile",
            form_type="profile",
            language="en",
            recorded_at=STAMP,
        )
    )
    issuer_registry.persist(
        LegacyIssuerBindingRevision(
            binding_revision_id="legacy-binding-avdv-1",
            idempotency_key="legacy-binding-avdv-1",
            recorded_issuer_id="legacy-ticker:AVDV",
            revision=1,
            issuer_id="issuer-american-century-etf-trust",
            outcome="selected",
            decision_kind="deterministic",
            reason_code="sec_series_class_mapping",
            reason_details=(("source_observation_id", "obs-sec-series"),),
            material_dissent=False,
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    )
    ambiguous_codes = {
        finding.code
        for finding in audit_connection(
            conn,
            AuditOptions(deep_sqlite_checks=False),
        ).findings
    }
    assert "EVIDENCE_SUBJECT_BINDING_AMBIGUOUS" in ambiguous_codes
    registry.persist(
        EvidenceSubjectBindingRevision(
            binding_revision_id="subject-binding-avdv-1",
            idempotency_key="subject-binding-avdv-1",
            recorded_issuer_id="legacy-ticker:AVDV",
            revision=1,
            issuer_id="issuer-american-century-etf-trust",
            reporting_entity_id=avdv.reporting_entity_id,
            security_id="security-avdv",
            outcome="selected",
            decision_kind="deterministic",
            reason_code="sec_series_class_mapping",
            reason_details=(("source_observation_id", "obs-sec-series"),),
            material_dissent=False,
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    )
    registry.persist(
        SourceObligationRevision(
            obligation_revision_id="obligation-avdv-nport-1",
            idempotency_key="obligation-avdv-nport-1",
            obligation_key="reporting-sec-series-s000066457:sec-investment-company",
            revision=1,
            issuer_id="issuer-american-century-etf-trust",
            reporting_entity_id=avdv.reporting_entity_id,
            authority_kind="sec_edgar",
            document_family="investment_company_periodic",
            obligation_state="required",
            completeness_rule="regulator_inventory",
            active_from=STAMP,
            active_to=None,
            decision_kind="deterministic",
            reason_code="registered_investment_company_series",
            reason_details=(("sec_series_id", "S000066457"),),
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    )
    conn.commit()

    resolved_avdv = registry.resolve_reporting_identifier(
        "sec_series_id", "s000066457", knowledge_at=STAMP
    )
    resolved_avuv = registry.resolve_reporting_identifier(
        "sec_series_id", "S000066459", knowledge_at=STAMP
    )
    assert resolved_avdv.reporting_entity_id != resolved_avuv.reporting_entity_id
    assert (
        registry.resolve_security_identifier(
            "sec_class_contract_id", "c000214352", knowledge_at=STAMP
        ).security_id
        == "security-avdv"
    )
    subject = registry.canonicalize_recorded_subject(
        "legacy-ticker:AVDV",
        knowledge_at=STAMP,
    )
    assert subject.reporting_entity_id == avdv.reporting_entity_id
    assert subject.security_id == "security-avdv"
    assert conn.execute(
        "SELECT issuer_id, reporting_entity_id, security_id "
        "FROM v_evidence_document_versions_canonical "
        "WHERE document_version_id = 'document-avdv-profile'"
    ).fetchone() == (
        "issuer-american-century-etf-trust",
        avdv.reporting_entity_id,
        "security-avdv",
    )
    resolved_codes = {
        finding.code
        for finding in audit_connection(
            conn,
            AuditOptions(deep_sqlite_checks=False),
        ).findings
    }
    assert "EVIDENCE_SUBJECT_BINDING_AMBIGUOUS" not in resolved_codes
    obligations = registry.source_obligations(
        issuer_id="issuer-american-century-etf-trust",
        knowledge_at=STAMP,
    )
    assert [(item.reporting_entity_id, item.document_family) for item in obligations] == [
        ("reporting-sec-series-s000066457", "investment_company_periodic")
    ]
    assert registry.persist(registrant).created is False
    try:
        conn.execute(
            "DELETE FROM reporting_entities WHERE reporting_entity_id = ?",
            (avdv.reporting_entity_id,),
        )
    except sqlite3.IntegrityError as exc:
        assert "append-only" in str(exc)
    else:
        raise AssertionError("reporting entities must be append-only")
    conn.close()


def test_audit_blocks_active_scope_without_reporting_identity_or_obligations(
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path)
    registry = IssuerRegistry(conn)
    registry.persist(
        IssuerEntity(
            issuer_id="issuer-missing-reporting-boundary",
            idempotency_key="issuer-missing-reporting-boundary",
            entity_kind="operating_company",
            created_at=STAMP,
        )
    )
    registry.persist(
        ReportingScopeRevision(
            scope_revision_id="scope-missing-reporting-boundary",
            idempotency_key="scope-missing-reporting-boundary",
            scope_key="investor-research",
            issuer_id="issuer-missing-reporting-boundary",
            revision=1,
            inclusion_state="core",
            history_policy="all_available",
            require_sec=True,
            require_ir=True,
            require_earnings=True,
            decision_kind="deterministic",
            reason_code="portfolio_reporting_scope",
            reason_details=(("list_types", "portfolio"),),
            effective_at=STAMP,
            knowledge_at=STAMP,
            recorded_at=STAMP,
        )
    )
    conn.commit()

    summary = audit_connection(
        conn,
        AuditOptions(deep_sqlite_checks=False),
    )
    codes = {finding.code for finding in summary.findings}

    assert "REPORTING_ENTITY_REGISTRY_UNINITIALIZED" in codes
    assert "SCOPE_SOURCE_OBLIGATION_MISSING" in codes
    conn.close()
