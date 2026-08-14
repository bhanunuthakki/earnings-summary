from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from models.documents import DocType, SourceType
from provenance.source_regime import (
    AdmissionEvidence,
    AsOfRule,
    DegradationBehavior,
    DomainSourcePolicy,
    EvidenceAuthority,
    EvidenceLifecycle,
    ParentEvidenceReference,
    SourceDomain,
    SourceRegime,
    SourceRegimeContract,
    TransformationLineage,
    classification_for_source_type,
    contract_for,
    contract_sha256,
    receipt_identity,
)

_PUBLISHED = datetime(2026, 8, 1, tzinfo=UTC)
_INGESTED = _PUBLISHED + timedelta(hours=1)
_SEALED = _INGESTED + timedelta(hours=1)
_CUTOFF = _SEALED + timedelta(hours=1)
_SHA = "a" * 64


def _call_untyped(
    function: Callable[..., object],
    *args: object,
    **kwargs: object,
) -> object:
    return function(*args, **kwargs)


def _evidence(
    source_type: SourceType,
    document_type: DocType,
    *,
    lineage: TransformationLineage | None = None,
    published_at: datetime | None = _PUBLISHED,
    ingested_at: datetime = _INGESTED,
    sealed_at: datetime | None = _SEALED,
) -> AdmissionEvidence:
    return AdmissionEvidence(
        source_type=source_type,
        document_type=document_type,
        source_document_id="document-version-1",
        observation_or_projection_version="observation-1",
        currency="USD",
        fiscal_period="FY2026Q2",
        published_at=published_at,
        ingested_at=ingested_at,
        sealed_at=sealed_at,
        transformation_lineage=lineage,
    )


def _lineage(
    source_type: SourceType = SourceType.IR_DOC,
    document_type: DocType = DocType.IR_PRESENTATION,
) -> TransformationLineage:
    return TransformationLineage(
        parents=(
            ParentEvidenceReference(
                source_type=source_type,
                document_type=document_type,
                source_document_id="parent-document-1",
                observation_id="parent-observation-1",
                resolution_revision_id="parent-resolution-1",
                published_at=_PUBLISHED,
                ingested_at=_INGESTED,
            ),
        ),
        formula_id="adjusted-ebitda",
        formula_version="1",
        formula_definition_sha256=_SHA,
        formula_config_sha256=_SHA,
    )


def _admits(
    regime: SourceRegime,
    domain: SourceDomain,
    source_type: SourceType,
    document_type: DocType,
    *,
    lineage: TransformationLineage | None = None,
) -> bool:
    return contract_for(regime).admits(
        domain=domain,
        evidence=_evidence(source_type, document_type, lineage=lineage),
        cutoff=_CUTOFF,
    )


def test_every_document_source_type_has_a_closed_classification() -> None:
    classifications = [classification_for_source_type(source_type) for source_type in SourceType]

    assert {item.lifecycle for item in classifications} == set(EvidenceLifecycle)
    assert {item.authority for item in classifications} == {*EvidenceAuthority, None}


def test_official_primary_keeps_vendor_carve_outs_explicit() -> None:
    assert _admits(
        SourceRegime.OFFICIAL_PRIMARY,
        SourceDomain.REPORTED_FACT,
        SourceType.SEC_XBRL,
        DocType.SEC_COMPANYFACTS_SNAPSHOT,
    )
    assert _admits(
        SourceRegime.OFFICIAL_PRIMARY,
        SourceDomain.REPORTED_FACT,
        SourceType.IR_DOC,
        DocType.IR_PRESS_RELEASE,
    )
    with pytest.raises(ValueError, match="excluded"):
        _admits(
            SourceRegime.OFFICIAL_PRIMARY,
            SourceDomain.REPORTED_FACT,
            SourceType.FMP,
            DocType.FMP_INCOME_STATEMENT,
        )
    assert _admits(
        SourceRegime.OFFICIAL_PRIMARY,
        SourceDomain.ESTIMATE,
        SourceType.FMP,
        DocType.FMP_ANALYST_ESTIMATES,
    )
    assert _admits(
        SourceRegime.OFFICIAL_PRIMARY,
        SourceDomain.PRICE,
        SourceType.FMP,
        DocType.FMP_HISTORICAL_PRICE,
    )


def test_vendor_only_does_not_silently_admit_official_reported_facts() -> None:
    assert _admits(
        SourceRegime.NORMALIZED_VENDOR_ONLY,
        SourceDomain.REPORTED_FACT,
        SourceType.FMP,
        DocType.FMP_INCOME_STATEMENT,
    )
    with pytest.raises(ValueError, match="excluded"):
        _admits(
            SourceRegime.NORMALIZED_VENDOR_ONLY,
            SourceDomain.REPORTED_FACT,
            SourceType.SEC_XBRL,
            DocType.SEC_COMPANYFACTS_SNAPSHOT,
        )


def test_combined_precedence_distinguishes_issuer_from_third_party() -> None:
    assert contract_for(SourceRegime.COMBINED).precedence(SourceDomain.REPORTED_FACT) == (
        EvidenceAuthority.REGULATOR,
        EvidenceAuthority.ISSUER,
        EvidenceAuthority.THIRD_PARTY,
    )


def test_manual_sources_are_control_plane_inputs_not_reported_facts() -> None:
    assert _admits(
        SourceRegime.COMBINED,
        SourceDomain.OWNER_STATE,
        SourceType.MANUAL_ENTRY,
        DocType.ANALYST_COMMENT,
    )
    assert _admits(
        SourceRegime.COMBINED,
        SourceDomain.MANUAL_OVERRIDE,
        SourceType.MANUAL_CSV,
        DocType.ANALYST_COMMENT,
    )
    with pytest.raises(ValueError, match="excluded"):
        _admits(
            SourceRegime.COMBINED,
            SourceDomain.REPORTED_FACT,
            SourceType.MANUAL_ENTRY,
            DocType.ANALYST_COMMENT,
        )


def test_temporal_rules_are_enforced_at_the_admission_boundary() -> None:
    contract = contract_for(SourceRegime.COMBINED)
    after_cutoff = _CUTOFF + timedelta(seconds=1)

    with pytest.raises(ValueError, match="published by cutoff"):
        contract.admits(
            domain=SourceDomain.REPORTED_FACT,
            evidence=_evidence(
                SourceType.SEC_XBRL,
                DocType.SEC_COMPANYFACTS_SNAPSHOT,
                published_at=after_cutoff,
                ingested_at=after_cutoff,
                sealed_at=after_cutoff,
            ),
            cutoff=_CUTOFF,
        )
    with pytest.raises(ValueError, match="observed by cutoff"):
        contract.admits(
            domain=SourceDomain.PRICE,
            evidence=_evidence(
                SourceType.FMP,
                DocType.FMP_HISTORICAL_PRICE,
                ingested_at=after_cutoff,
                sealed_at=after_cutoff,
            ),
            cutoff=_CUTOFF,
        )
    with pytest.raises(ValueError, match="sealed by cutoff"):
        contract.admits(
            domain=SourceDomain.OWNER_STATE,
            evidence=_evidence(
                SourceType.MANUAL_ENTRY,
                DocType.ANALYST_COMMENT,
                sealed_at=after_cutoff,
            ),
            cutoff=_CUTOFF,
        )


def test_temporal_and_degradation_rules_are_explicit() -> None:
    contract = contract_for(SourceRegime.COMBINED)

    assert contract.policy_for(SourceDomain.REPORTED_FACT).as_of_rule is (
        AsOfRule.PUBLISHED_BY_CUTOFF
    )
    assert contract.policy_for(SourceDomain.PRICE).as_of_rule is AsOfRule.OBSERVED_BY_CUTOFF
    assert contract.policy_for(SourceDomain.OWNER_STATE).degradation is (
        DegradationBehavior.OWNER_INPUT_REQUIRED
    )


def test_dcf_and_foreign_interim_inputs_have_regime_specific_policy() -> None:
    official = contract_for(SourceRegime.OFFICIAL_PRIMARY)
    assert official.dcf_input_domains == (
        SourceDomain.REPORTED_FACT,
        SourceDomain.ESTIMATE,
        SourceDomain.PRICE,
        SourceDomain.OWNER_STATE,
        SourceDomain.MANUAL_OVERRIDE,
        SourceDomain.DERIVED_FACT,
    )
    assert _admits(
        SourceRegime.OFFICIAL_PRIMARY,
        SourceDomain.FOREIGN_INTERIM,
        SourceType.IR_DOC,
        DocType.IR_INVESTOR_UPDATE,
    )
    with pytest.raises(ValueError, match="excluded"):
        _admits(
            SourceRegime.OFFICIAL_PRIMARY,
            SourceDomain.FOREIGN_INTERIM,
            SourceType.FMP,
            DocType.FMP_INCOME_STATEMENT,
        )
    assert _admits(
        SourceRegime.NORMALIZED_VENDOR_ONLY,
        SourceDomain.FOREIGN_INTERIM,
        SourceType.FMP,
        DocType.FMP_INCOME_STATEMENT,
    )


def test_derived_admission_requires_sealed_persisted_lineage() -> None:
    contract = contract_for(SourceRegime.OFFICIAL_PRIMARY)
    orphan = _evidence(SourceType.LLM_EXTRACTED, DocType.ANALYST_COMMENT)

    with pytest.raises(ValueError, match="sealed transformation lineage"):
        contract.admits(domain=SourceDomain.COMPANY_KPI, evidence=orphan, cutoff=_CUTOFF)
    assert contract.admits(
        domain=SourceDomain.COMPANY_KPI,
        evidence=_evidence(
            SourceType.LLM_EXTRACTED,
            DocType.ANALYST_COMMENT,
            lineage=_lineage(),
        ),
        cutoff=_CUTOFF,
    )
    with pytest.raises(ValueError, match="excluded parent"):
        contract.admits(
            domain=SourceDomain.COMPANY_KPI,
            evidence=_evidence(
                SourceType.LLM_EXTRACTED,
                DocType.ANALYST_COMMENT,
                lineage=_lineage(
                    SourceType.FMP,
                    DocType.FMP_KEY_METRICS,
                ),
            ),
            cutoff=_CUTOFF,
        )


def test_non_derived_source_rejects_fake_transformation_lineage() -> None:
    with pytest.raises(ValueError, match="must not declare transformation lineage"):
        contract_for(SourceRegime.COMBINED).admits(
            domain=SourceDomain.REPORTED_FACT,
            evidence=_evidence(
                SourceType.SEC_XBRL,
                DocType.SEC_COMPANYFACTS_SNAPSHOT,
                lineage=_lineage(),
            ),
            cutoff=_CUTOFF,
        )


def test_source_and_document_allowlists_close_cross_domain_leaks() -> None:
    with pytest.raises(ValueError, match="excluded"):
        _admits(
            SourceRegime.OFFICIAL_PRIMARY,
            SourceDomain.REPORTED_FACT,
            SourceType.TRANSCRIPT_AUDIO,
            DocType.EARNINGS_CALL_AUDIO,
        )
    with pytest.raises(ValueError, match="excluded"):
        _admits(
            SourceRegime.NORMALIZED_VENDOR_ONLY,
            SourceDomain.TRANSCRIPT,
            SourceType.FMP,
            DocType.FMP_OTHER,
        )
    with pytest.raises(ValueError, match="excluded"):
        _admits(
            SourceRegime.COMBINED,
            SourceDomain.PRICE,
            SourceType.FMP,
            DocType.FMP_ANALYST_ESTIMATES,
        )


def test_unknown_and_unvalidated_runtime_values_fail_closed() -> None:
    with pytest.raises(ValidationError):
        AdmissionEvidence.model_validate(
            {
                **_evidence(
                    SourceType.SEC_XBRL,
                    DocType.SEC_COMPANYFACTS_SNAPSHOT,
                ).model_dump(),
                "source_type": "unknown_provider",
            }
        )
    with pytest.raises(ValidationError):
        _call_untyped(contract_for, "combined")
    with pytest.raises(ValidationError):
        _call_untyped(
            contract_for(SourceRegime.COMBINED).admits,
            domain=SourceDomain.REPORTED_FACT,
            evidence={"source_type": "sec_xbrl"},
            cutoff=_CUTOFF,
        )


def test_contract_digest_is_stable_regime_specific_and_binds_classification() -> None:
    combined = contract_for(SourceRegime.COMBINED)

    assert contract_sha256(combined) == contract_sha256(contract_for(SourceRegime.COMBINED))
    assert len(contract_sha256(combined)) == 64
    assert contract_sha256(combined) != contract_sha256(contract_for(SourceRegime.OFFICIAL_PRIMARY))
    assert receipt_identity(combined).model_dump(mode="json") == {
        "schema_version": "source-regime-contract@2",
        "regime": "combined",
        "contract_sha256": contract_sha256(combined),
    }


def test_contract_is_deeply_immutable_and_constructor_is_closed() -> None:
    contract = contract_for(SourceRegime.COMBINED)

    with pytest.raises(ValidationError):
        contract.policies[0].allow_derived = True
    with pytest.raises(AttributeError):
        getattr(contract.policies, "__setitem__")
    with pytest.raises(ValidationError, match="at least 1 item"):
        SourceRegimeContract(
            regime=SourceRegime.COMBINED,
            policies=(),
            dcf_input_domains=(),
        )


def test_policy_constructor_rejects_unbound_source_grants() -> None:
    valid_policy = contract_for(SourceRegime.COMBINED).policy_for(SourceDomain.PRICE)

    with pytest.raises(ValidationError, match="authority is absent"):
        DomainSourcePolicy(
            domain=valid_policy.domain,
            authority_precedence=(EvidenceAuthority.REGULATOR,),
            source_grants=valid_policy.source_grants,
            allow_provisional=valid_policy.allow_provisional,
            allow_derived=valid_policy.allow_derived,
            as_of_rule=valid_policy.as_of_rule,
            degradation=valid_policy.degradation,
        )


def test_foreign_filer_evidence_does_not_need_provider_specific_policy() -> None:
    assert _admits(
        SourceRegime.COMBINED,
        SourceDomain.FILING,
        SourceType.SEC_XBRL,
        DocType.SEC_20F,
    )
    assert _admits(
        SourceRegime.COMBINED,
        SourceDomain.FOREIGN_INTERIM,
        SourceType.IR_DOC,
        DocType.IR_SUPPLEMENT,
    )
    assert _admits(
        SourceRegime.COMBINED,
        SourceDomain.TRANSCRIPT,
        SourceType.TRANSCRIPT_AUDIO,
        DocType.EARNINGS_CALL_AUDIO,
    )
