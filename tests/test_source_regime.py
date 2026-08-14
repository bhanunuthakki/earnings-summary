from __future__ import annotations

import pytest
from pydantic import ValidationError

from models.documents import SourceType
from provenance.source_regime import (
    AsOfRule,
    DegradationBehavior,
    EvidenceAuthority,
    EvidenceLifecycle,
    SourceDomain,
    SourceRegime,
    classification_for_source_type,
    contract_for,
    contract_sha256,
)


def test_every_document_source_type_has_a_closed_classification() -> None:
    classifications = [classification_for_source_type(source_type) for source_type in SourceType]

    assert {item.lifecycle for item in classifications} == set(EvidenceLifecycle)
    assert {item.authority for item in classifications} == {
        *EvidenceAuthority,
        None,
    }


def test_official_primary_keeps_vendor_carve_outs_explicit() -> None:
    contract = contract_for(SourceRegime.OFFICIAL_PRIMARY)

    assert contract.admits(domain=SourceDomain.REPORTED_FACT, source_type=SourceType.SEC_XBRL)
    assert contract.admits(domain=SourceDomain.REPORTED_FACT, source_type=SourceType.IR_DOC)
    with pytest.raises(ValueError, match="excluded"):
        contract.admits(domain=SourceDomain.REPORTED_FACT, source_type=SourceType.FMP)
    assert contract.admits(domain=SourceDomain.ESTIMATE, source_type=SourceType.FMP)
    assert contract.admits(domain=SourceDomain.PRICE, source_type=SourceType.FMP)


def test_vendor_only_does_not_silently_admit_official_reported_facts() -> None:
    contract = contract_for(SourceRegime.NORMALIZED_VENDOR_ONLY)

    assert contract.admits(domain=SourceDomain.REPORTED_FACT, source_type=SourceType.FMP)
    with pytest.raises(ValueError, match="excluded"):
        contract.admits(domain=SourceDomain.REPORTED_FACT, source_type=SourceType.SEC_XBRL)
    with pytest.raises(ValueError, match="excluded"):
        contract.admits(domain=SourceDomain.REPORTED_FACT, source_type=SourceType.IR_DOC)


def test_combined_precedence_distinguishes_issuer_from_third_party() -> None:
    contract = contract_for(SourceRegime.COMBINED)

    assert contract.precedence(SourceDomain.REPORTED_FACT) == (
        EvidenceAuthority.REGULATOR,
        EvidenceAuthority.ISSUER,
        EvidenceAuthority.THIRD_PARTY,
    )


def test_manual_sources_are_control_plane_inputs_not_reported_facts() -> None:
    contract = contract_for(SourceRegime.COMBINED)

    assert contract.admits(domain=SourceDomain.OWNER_STATE, source_type=SourceType.MANUAL_ENTRY)
    assert contract.admits(
        domain=SourceDomain.MANUAL_OVERRIDE,
        source_type=SourceType.MANUAL_CSV,
    )
    with pytest.raises(ValueError, match="excluded"):
        contract.admits(domain=SourceDomain.REPORTED_FACT, source_type=SourceType.MANUAL_ENTRY)


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
    vendor = contract_for(SourceRegime.NORMALIZED_VENDOR_ONLY)

    assert official.dcf_input_domains == (
        SourceDomain.REPORTED_FACT,
        SourceDomain.ESTIMATE,
        SourceDomain.PRICE,
        SourceDomain.OWNER_STATE,
        SourceDomain.MANUAL_OVERRIDE,
        SourceDomain.DERIVED_FACT,
    )
    with pytest.raises(ValueError, match="excluded"):
        official.admits(domain=SourceDomain.REPORTED_FACT, source_type=SourceType.FMP)
    assert official.admits(domain=SourceDomain.ESTIMATE, source_type=SourceType.FMP)
    assert official.admits(domain=SourceDomain.PRICE, source_type=SourceType.FMP)
    assert official.admits(domain=SourceDomain.OWNER_STATE, source_type=SourceType.MANUAL_ENTRY)
    assert official.admits(domain=SourceDomain.FOREIGN_INTERIM, source_type=SourceType.IR_DOC)
    with pytest.raises(ValueError, match="excluded"):
        official.admits(domain=SourceDomain.FOREIGN_INTERIM, source_type=SourceType.FMP)
    assert vendor.admits(domain=SourceDomain.FOREIGN_INTERIM, source_type=SourceType.FMP)
    with pytest.raises(ValueError, match="excluded"):
        vendor.admits(domain=SourceDomain.FOREIGN_INTERIM, source_type=SourceType.IR_DOC)


def test_sole_admission_api_rejects_orphan_and_excluded_parent_lineage() -> None:
    contract = contract_for(SourceRegime.OFFICIAL_PRIMARY)

    with pytest.raises(ValueError, match="primary parent"):
        contract.admits(
            domain=SourceDomain.COMPANY_KPI,
            source_type=SourceType.LLM_EXTRACTED,
        )
    assert contract.admits(
        domain=SourceDomain.COMPANY_KPI,
        source_type=SourceType.LLM_EXTRACTED,
        parent_source_types=(SourceType.IR_DOC,),
    )
    with pytest.raises(ValueError, match="excluded parent source"):
        contract.admits(
            domain=SourceDomain.COMPANY_KPI,
            source_type=SourceType.LLM_EXTRACTED,
            parent_source_types=(SourceType.FMP,),
        )


def test_official_primary_rejects_vendor_derived_fact_lineage() -> None:
    contract = contract_for(SourceRegime.OFFICIAL_PRIMARY)

    with pytest.raises(ValueError, match="excluded parent source"):
        contract.admits(
            domain=SourceDomain.DERIVED_FACT,
            source_type=SourceType.LLM_EXTRACTED,
            parent_source_types=(SourceType.FMP,),
        )


def test_non_derived_source_rejects_fake_parent_lineage() -> None:
    contract = contract_for(SourceRegime.COMBINED)

    with pytest.raises(ValueError, match="must not declare derived parents"):
        contract.admits(
            domain=SourceDomain.REPORTED_FACT,
            source_type=SourceType.SEC_XBRL,
            parent_source_types=(SourceType.IR_DOC,),
        )


def test_contract_digest_is_stable_and_regime_specific() -> None:
    combined = contract_for(SourceRegime.COMBINED)

    assert contract_sha256(combined) == contract_sha256(contract_for(SourceRegime.COMBINED))
    assert len(contract_sha256(combined)) == 64
    assert contract_sha256(combined) != contract_sha256(contract_for(SourceRegime.OFFICIAL_PRIMARY))


def test_contract_is_deeply_immutable() -> None:
    contract = contract_for(SourceRegime.COMBINED)

    with pytest.raises(ValidationError):
        contract.policies[0].allow_derived = True
    with pytest.raises(AttributeError):
        getattr(contract.policies, "__setitem__")


def test_foreign_filer_evidence_does_not_need_provider_specific_policy() -> None:
    contract = contract_for(SourceRegime.COMBINED)

    assert contract.admits(domain=SourceDomain.FILING, source_type=SourceType.SEC_XBRL)
    assert contract.admits(domain=SourceDomain.FOREIGN_INTERIM, source_type=SourceType.IR_DOC)
    assert contract.admits(
        domain=SourceDomain.TRANSCRIPT,
        source_type=SourceType.TRANSCRIPT_AUDIO,
    )
