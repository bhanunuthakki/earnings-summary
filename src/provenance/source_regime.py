"""Provider-neutral source-regime contracts for reproducible research builds.

The contract in this module is policy only. It neither mutates persistence nor
selects a winning fact. Callers present a complete evidence envelope to the
single fail-closed ``admits`` boundary before building an offline projection.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator, validate_call

from models.documents import DocType, SourceType


class EvidenceAuthority(StrEnum):
    REGULATOR = "regulator"
    ISSUER = "issuer"
    THIRD_PARTY = "third_party"
    OWNER = "owner"


class EvidenceLifecycle(StrEnum):
    PRIMARY = "primary"
    PROVISIONAL = "provisional"
    DERIVED = "derived"


class SourceDomain(StrEnum):
    REPORTED_FACT = "reported_fact"
    COMPANY_KPI = "company_kpi"
    SEGMENT = "segment"
    FILING = "filing"
    ESTIMATE = "estimate"
    PRICE = "price"
    TRANSCRIPT = "transcript"
    OWNER_STATE = "owner_state"
    MANUAL_OVERRIDE = "manual_override"
    FOREIGN_INTERIM = "foreign_interim"
    DERIVED_FACT = "derived_fact"


class SourceRegime(StrEnum):
    OFFICIAL_PRIMARY = "official_primary"
    NORMALIZED_VENDOR_ONLY = "normalized_vendor_only"
    COMBINED = "combined"


class AsOfRule(StrEnum):
    PUBLISHED_BY_CUTOFF = "published_by_cutoff"
    OBSERVED_BY_CUTOFF = "observed_by_cutoff"
    SEALED_AT_CUTOFF = "sealed_at_cutoff"


class DegradationBehavior(StrEnum):
    UNAVAILABLE = "unavailable"
    EXPLICIT_NOT_APPLICABLE = "explicit_not_applicable"
    OWNER_INPUT_REQUIRED = "owner_input_required"


_STRICT_FROZEN = ConfigDict(extra="forbid", frozen=True, strict=True)


class SourceClassification(BaseModel):
    model_config = _STRICT_FROZEN

    authority: EvidenceAuthority | None
    lifecycle: EvidenceLifecycle


_CLASSIFICATION_BY_TYPE = MappingProxyType(
    {
        SourceType.SEC_XBRL: SourceClassification(
            authority=EvidenceAuthority.REGULATOR,
            lifecycle=EvidenceLifecycle.PRIMARY,
        ),
        SourceType.SEC_S1: SourceClassification(
            authority=EvidenceAuthority.REGULATOR,
            lifecycle=EvidenceLifecycle.PROVISIONAL,
        ),
        SourceType.IR_DOC: SourceClassification(
            authority=EvidenceAuthority.ISSUER,
            lifecycle=EvidenceLifecycle.PRIMARY,
        ),
        SourceType.TRANSCRIPT_AUDIO: SourceClassification(
            authority=EvidenceAuthority.ISSUER,
            lifecycle=EvidenceLifecycle.PRIMARY,
        ),
        SourceType.FMP: SourceClassification(
            authority=EvidenceAuthority.THIRD_PARTY,
            lifecycle=EvidenceLifecycle.PRIMARY,
        ),
        SourceType.MANUAL_CSV: SourceClassification(
            authority=EvidenceAuthority.OWNER,
            lifecycle=EvidenceLifecycle.PRIMARY,
        ),
        SourceType.MANUAL_ENTRY: SourceClassification(
            authority=EvidenceAuthority.OWNER,
            lifecycle=EvidenceLifecycle.PRIMARY,
        ),
        SourceType.LLM_EXTRACTED: SourceClassification(
            authority=None,
            lifecycle=EvidenceLifecycle.DERIVED,
        ),
    }
)

if set(_CLASSIFICATION_BY_TYPE) != set(SourceType):
    missing = sorted(source.value for source in set(SourceType) - set(_CLASSIFICATION_BY_TYPE))
    extra = sorted(source.value for source in set(_CLASSIFICATION_BY_TYPE) - set(SourceType))
    raise RuntimeError(f"source classification is not closed: missing={missing}, extra={extra}")


@validate_call(config=_STRICT_FROZEN)
def classification_for_source_type(source_type: SourceType) -> SourceClassification:
    try:
        return _CLASSIFICATION_BY_TYPE[source_type]
    except KeyError as exc:
        raise ValueError(f"unclassified source type: {source_type!r}") from exc


class SourceGrant(BaseModel):
    """One explicit source/document-kind allowlist entry."""

    model_config = _STRICT_FROZEN

    source_type: SourceType
    document_types: tuple[DocType, ...] = Field(min_length=1)

    @field_validator("document_types")
    @classmethod
    def _unique_document_types(cls, value: tuple[DocType, ...]) -> tuple[DocType, ...]:
        if len(value) != len(set(value)):
            raise ValueError("document grant cannot contain duplicates")
        return value


class ParentEvidenceReference(BaseModel):
    """Persisted parent identity needed to reproduce a derived observation."""

    model_config = _STRICT_FROZEN

    source_type: SourceType
    document_type: DocType
    source_document_id: str = Field(min_length=1, max_length=128)
    observation_id: str = Field(min_length=1, max_length=128)
    resolution_revision_id: str = Field(min_length=1, max_length=128)
    published_at: datetime
    ingested_at: datetime

    @model_validator(mode="after")
    def _valid_clocks(self) -> Self:
        _require_aware(self.published_at, field="published_at")
        _require_aware(self.ingested_at, field="ingested_at")
        if self.ingested_at < self.published_at:
            raise ValueError("parent ingested_at must not precede published_at")
        return self


class TransformationLineage(BaseModel):
    """Immutable derivation seal carried by transformed evidence."""

    model_config = _STRICT_FROZEN

    parents: tuple[ParentEvidenceReference, ...] = Field(min_length=1)
    formula_id: str = Field(min_length=1, max_length=128)
    formula_version: str = Field(min_length=1, max_length=128)
    formula_definition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    formula_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _unique_parent_observations(self) -> Self:
        observation_ids = tuple(parent.observation_id for parent in self.parents)
        if len(observation_ids) != len(set(observation_ids)):
            raise ValueError("derived lineage cannot repeat a parent observation")
        return self


class AdmissionEvidence(BaseModel):
    """Complete provenance envelope evaluated by a source regime."""

    model_config = _STRICT_FROZEN

    source_type: SourceType
    document_type: DocType
    source_document_id: str = Field(min_length=1, max_length=128)
    observation_or_projection_version: str = Field(min_length=1, max_length=128)
    currency: str | None
    fiscal_period: str | None
    published_at: datetime | None
    ingested_at: datetime
    sealed_at: datetime | None
    transformation_lineage: TransformationLineage | None

    @field_validator("currency")
    @classmethod
    def _canonical_currency(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) != 3 or value != value.upper():
            raise ValueError("currency must be an uppercase three-letter code")
        return value

    @model_validator(mode="after")
    def _valid_clocks(self) -> Self:
        _require_aware(self.ingested_at, field="ingested_at")
        if self.published_at is not None:
            _require_aware(self.published_at, field="published_at")
            if self.ingested_at < self.published_at:
                raise ValueError("ingested_at must not precede published_at")
        if self.sealed_at is not None:
            _require_aware(self.sealed_at, field="sealed_at")
            if self.sealed_at < self.ingested_at:
                raise ValueError("sealed_at must not precede ingested_at")
        return self


def _require_aware(value: datetime, *, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


class DomainSourcePolicy(BaseModel):
    model_config = _STRICT_FROZEN

    domain: SourceDomain
    authority_precedence: tuple[EvidenceAuthority, ...]
    source_grants: tuple[SourceGrant, ...]
    allow_provisional: bool = False
    allow_derived: bool = False
    as_of_rule: AsOfRule
    degradation: DegradationBehavior

    @model_validator(mode="after")
    def _closed_grants(self) -> Self:
        if len(self.authority_precedence) != len(set(self.authority_precedence)):
            raise ValueError("authority precedence cannot contain duplicates")
        identities = tuple(grant.source_type for grant in self.source_grants)
        if len(identities) != len(set(identities)):
            raise ValueError("source grants cannot repeat a source type")
        for grant in self.source_grants:
            classification = classification_for_source_type(grant.source_type)
            if classification.lifecycle is EvidenceLifecycle.DERIVED:
                if not self.allow_derived:
                    raise ValueError("derived grant requires allow_derived")
            elif classification.authority not in self.authority_precedence:
                raise ValueError("source grant authority is absent from precedence")
        return self


_DCF_INPUT_DOMAINS = (
    SourceDomain.REPORTED_FACT,
    SourceDomain.ESTIMATE,
    SourceDomain.PRICE,
    SourceDomain.OWNER_STATE,
    SourceDomain.MANUAL_OVERRIDE,
    SourceDomain.DERIVED_FACT,
)


class SourceRegimeContract(BaseModel):
    model_config = _STRICT_FROZEN

    schema_version: Literal["source-regime-contract@2"] = "source-regime-contract@2"
    regime: SourceRegime
    policies: tuple[DomainSourcePolicy, ...] = Field(min_length=1)
    dcf_input_domains: tuple[SourceDomain, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _complete_contract(self) -> Self:
        domains = tuple(policy.domain for policy in self.policies)
        if len(domains) != len(set(domains)) or set(domains) != set(SourceDomain):
            raise ValueError("contract must contain exactly one policy for every source domain")
        if self.dcf_input_domains != _DCF_INPUT_DOMAINS:
            raise ValueError("contract must declare the canonical DCF input domains")
        return self

    @validate_call(config=_STRICT_FROZEN)
    def policy_for(self, domain: SourceDomain) -> DomainSourcePolicy:
        for policy in self.policies:
            if policy.domain is domain:
                return policy
        raise ValueError(f"regime {self.regime.value} has no policy for {domain.value}")

    def precedence(self, domain: SourceDomain) -> tuple[EvidenceAuthority, ...]:
        return self.policy_for(domain).authority_precedence

    @validate_call(config=_STRICT_FROZEN)
    def admits(
        self,
        *,
        domain: SourceDomain,
        evidence: AdmissionEvidence,
        cutoff: datetime,
    ) -> bool:
        """Fail closed unless evidence, clocks, document kind, and lineage comply."""

        _require_aware(cutoff, field="cutoff")
        policy = self.policy_for(domain)
        classification = classification_for_source_type(evidence.source_type)
        grant = next(
            (
                item
                for item in policy.source_grants
                if item.source_type is evidence.source_type
                and evidence.document_type in item.document_types
            ),
            None,
        )
        if grant is None:
            raise ValueError(
                f"source/document {evidence.source_type.value}/{evidence.document_type.value} "
                f"is excluded from {domain.value} under {self.regime.value}"
            )
        _enforce_cutoff(policy.as_of_rule, evidence=evidence, cutoff=cutoff)

        if classification.lifecycle is not EvidenceLifecycle.DERIVED:
            if evidence.transformation_lineage is not None:
                raise ValueError("non-derived source must not declare transformation lineage")
            if classification.authority not in policy.authority_precedence:
                raise ValueError("source authority is excluded by policy")
            if (
                classification.lifecycle is EvidenceLifecycle.PROVISIONAL
                and not policy.allow_provisional
            ):
                raise ValueError("provisional source is excluded by policy")
            return True

        if not policy.allow_derived or evidence.transformation_lineage is None:
            raise ValueError("derived source requires sealed transformation lineage")
        for parent in evidence.transformation_lineage.parents:
            parent_classification = classification_for_source_type(parent.source_type)
            if parent_classification.lifecycle is EvidenceLifecycle.DERIVED:
                raise ValueError("derived source cannot use another derived primary parent")
            parent_grant = next(
                (
                    item
                    for item in policy.source_grants
                    if item.source_type is parent.source_type
                    and parent.document_type in item.document_types
                ),
                None,
            )
            if parent_grant is None:
                raise ValueError("derived source has an excluded parent source/document")
            _enforce_parent_cutoff(policy.as_of_rule, parent=parent, cutoff=cutoff)
        return True


class SourceRegimeReceiptIdentity(BaseModel):
    """Typed fields projection and build receipts embed without importing policy."""

    model_config = _STRICT_FROZEN

    schema_version: Literal["source-regime-contract@2"]
    regime: SourceRegime
    contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _enforce_cutoff(
    rule: AsOfRule,
    *,
    evidence: AdmissionEvidence,
    cutoff: datetime,
) -> None:
    if rule is AsOfRule.PUBLISHED_BY_CUTOFF:
        if evidence.published_at is None or evidence.published_at > cutoff:
            raise ValueError("evidence was not published by cutoff")
    elif rule is AsOfRule.OBSERVED_BY_CUTOFF:
        if evidence.ingested_at > cutoff:
            raise ValueError("evidence was not observed by cutoff")
    elif evidence.sealed_at is None or evidence.sealed_at > cutoff:
        raise ValueError("evidence was not sealed by cutoff")


def _enforce_parent_cutoff(
    rule: AsOfRule,
    *,
    parent: ParentEvidenceReference,
    cutoff: datetime,
) -> None:
    parent_clock = (
        parent.ingested_at if rule is AsOfRule.OBSERVED_BY_CUTOFF else parent.published_at
    )
    if parent_clock > cutoff:
        raise ValueError("derived parent evidence was unavailable at cutoff")


_SEC_FILINGS = (
    DocType.SEC_10K,
    DocType.SEC_10Q,
    DocType.SEC_20F,
    DocType.SEC_40F,
    DocType.SEC_8K,
    DocType.SEC_6K,
)
_IR_FINANCIAL = (
    DocType.IR_PRESS_RELEASE,
    DocType.IR_PRESENTATION,
    DocType.IR_SUPPLEMENT,
    DocType.IR_INVESTOR_UPDATE,
)
_FMP_REPORTED = (
    DocType.FMP_INCOME_STATEMENT,
    DocType.FMP_BALANCE_SHEET,
    DocType.FMP_CASHFLOW,
    DocType.FMP_AS_REPORTED_INCOME,
    DocType.FMP_AS_REPORTED_BALANCE,
    DocType.FMP_AS_REPORTED_CASHFLOW,
    DocType.FMP_AS_REPORTED_FINANCIAL,
)
_DOCS_BY_DOMAIN_SOURCE: dict[tuple[SourceDomain, SourceType], tuple[DocType, ...]] = {
    (SourceDomain.REPORTED_FACT, SourceType.SEC_XBRL): (
        DocType.SEC_COMPANYFACTS_SNAPSHOT,
        *_SEC_FILINGS,
    ),
    (SourceDomain.REPORTED_FACT, SourceType.SEC_S1): (DocType.SEC_S1,),
    (SourceDomain.REPORTED_FACT, SourceType.IR_DOC): _IR_FINANCIAL,
    (SourceDomain.REPORTED_FACT, SourceType.FMP): _FMP_REPORTED,
    (SourceDomain.COMPANY_KPI, SourceType.SEC_XBRL): (
        DocType.SEC_COMPANYFACTS_SNAPSHOT,
        *_SEC_FILINGS,
    ),
    (SourceDomain.COMPANY_KPI, SourceType.IR_DOC): _IR_FINANCIAL,
    (SourceDomain.COMPANY_KPI, SourceType.FMP): (
        DocType.FMP_KEY_METRICS,
        DocType.FMP_FINANCIAL_RATIOS,
        DocType.FMP_FINANCIAL_GROWTH,
        DocType.FMP_OWNER_EARNINGS,
        DocType.FMP_ENTERPRISE_VALUES,
    ),
    (SourceDomain.SEGMENT, SourceType.SEC_XBRL): (
        DocType.SEC_COMPANYFACTS_SNAPSHOT,
        *_SEC_FILINGS,
    ),
    (SourceDomain.SEGMENT, SourceType.IR_DOC): _IR_FINANCIAL,
    (SourceDomain.SEGMENT, SourceType.FMP): (
        DocType.FMP_SEGMENT_PRODUCT,
        DocType.FMP_SEGMENT_GEOGRAPHIC,
    ),
    (SourceDomain.FILING, SourceType.SEC_XBRL): _SEC_FILINGS,
    (SourceDomain.FILING, SourceType.SEC_S1): (DocType.SEC_S1,),
    (SourceDomain.FILING, SourceType.FMP): (
        DocType.FMP_10K_JSON,
        DocType.FMP_10Q_JSON,
        DocType.FMP_FINANCIAL_REPORTS_DATES,
    ),
    (SourceDomain.ESTIMATE, SourceType.FMP): (
        DocType.FMP_ANALYST_ESTIMATES,
        DocType.FMP_EARNINGS_CALENDAR,
        DocType.FMP_PRICE_TARGET_CONSENSUS,
    ),
    (SourceDomain.PRICE, SourceType.FMP): (
        DocType.FMP_HISTORICAL_PRICE,
        DocType.FMP_HISTORICAL_MARKET_CAP,
    ),
    (SourceDomain.TRANSCRIPT, SourceType.IR_DOC): (
        DocType.IR_TRANSCRIPT,
        DocType.EARNINGS_CALL_TRANSCRIPT,
    ),
    (SourceDomain.TRANSCRIPT, SourceType.TRANSCRIPT_AUDIO): (DocType.EARNINGS_CALL_AUDIO,),
    (SourceDomain.OWNER_STATE, SourceType.MANUAL_ENTRY): (DocType.ANALYST_COMMENT,),
    (SourceDomain.MANUAL_OVERRIDE, SourceType.MANUAL_CSV): (DocType.ANALYST_COMMENT,),
    (SourceDomain.MANUAL_OVERRIDE, SourceType.MANUAL_ENTRY): (DocType.ANALYST_COMMENT,),
    (SourceDomain.FOREIGN_INTERIM, SourceType.SEC_XBRL): (
        DocType.SEC_6K,
        DocType.SEC_20F,
        DocType.SEC_40F,
    ),
    (SourceDomain.FOREIGN_INTERIM, SourceType.IR_DOC): _IR_FINANCIAL,
    (SourceDomain.FOREIGN_INTERIM, SourceType.FMP): _FMP_REPORTED,
    (SourceDomain.DERIVED_FACT, SourceType.SEC_XBRL): (
        DocType.SEC_COMPANYFACTS_SNAPSHOT,
        *_SEC_FILINGS,
    ),
    (SourceDomain.DERIVED_FACT, SourceType.SEC_S1): (DocType.SEC_S1,),
    (SourceDomain.DERIVED_FACT, SourceType.IR_DOC): _IR_FINANCIAL,
    (SourceDomain.DERIVED_FACT, SourceType.FMP): (
        *_FMP_REPORTED,
        DocType.FMP_DCF,
        DocType.FMP_DCF_LEVERED,
    ),
}


def _grants(
    domain: SourceDomain,
    authorities: tuple[EvidenceAuthority, ...],
    *,
    allow_derived: bool,
) -> tuple[SourceGrant, ...]:
    grants = [
        SourceGrant(source_type=source_type, document_types=document_types)
        for (candidate_domain, source_type), document_types in _DOCS_BY_DOMAIN_SOURCE.items()
        if candidate_domain is domain
        and classification_for_source_type(source_type).authority in authorities
    ]
    if allow_derived:
        grants.append(
            SourceGrant(
                source_type=SourceType.LLM_EXTRACTED,
                document_types=(DocType.ANALYST_COMMENT,),
            )
        )
    return tuple(grants)


def _policy(
    domain: SourceDomain,
    *authorities: EvidenceAuthority,
    allow_provisional: bool = False,
    allow_derived: bool = False,
    as_of_rule: AsOfRule = AsOfRule.PUBLISHED_BY_CUTOFF,
    degradation: DegradationBehavior = DegradationBehavior.UNAVAILABLE,
) -> DomainSourcePolicy:
    return DomainSourcePolicy(
        domain=domain,
        authority_precedence=authorities,
        source_grants=_grants(domain, authorities, allow_derived=allow_derived),
        allow_provisional=allow_provisional,
        allow_derived=allow_derived,
        as_of_rule=as_of_rule,
        degradation=degradation,
    )


_REGULATOR = EvidenceAuthority.REGULATOR
_ISSUER = EvidenceAuthority.ISSUER
_VENDOR = EvidenceAuthority.THIRD_PARTY
_OWNER = EvidenceAuthority.OWNER


def _owner_policies() -> tuple[DomainSourcePolicy, DomainSourcePolicy]:
    return (
        _policy(
            SourceDomain.OWNER_STATE,
            _OWNER,
            as_of_rule=AsOfRule.SEALED_AT_CUTOFF,
            degradation=DegradationBehavior.OWNER_INPUT_REQUIRED,
        ),
        _policy(
            SourceDomain.MANUAL_OVERRIDE,
            _OWNER,
            as_of_rule=AsOfRule.SEALED_AT_CUTOFF,
            degradation=DegradationBehavior.EXPLICIT_NOT_APPLICABLE,
        ),
    )


def _contract(
    regime: SourceRegime, authorities: tuple[EvidenceAuthority, ...]
) -> SourceRegimeContract:
    official = _REGULATOR in authorities or _ISSUER in authorities
    return SourceRegimeContract(
        regime=regime,
        dcf_input_domains=_DCF_INPUT_DOMAINS,
        policies=(
            _policy(
                SourceDomain.REPORTED_FACT,
                *authorities,
                allow_provisional=official,
            ),
            _policy(SourceDomain.COMPANY_KPI, *authorities, allow_derived=True),
            _policy(SourceDomain.SEGMENT, *authorities, allow_derived=True),
            _policy(SourceDomain.FILING, *authorities, allow_provisional=official),
            _policy(
                SourceDomain.ESTIMATE,
                _VENDOR,
                as_of_rule=AsOfRule.OBSERVED_BY_CUTOFF,
            ),
            _policy(
                SourceDomain.PRICE,
                _VENDOR,
                as_of_rule=AsOfRule.OBSERVED_BY_CUTOFF,
            ),
            _policy(SourceDomain.TRANSCRIPT, *authorities, allow_derived=True),
            *_owner_policies(),
            _policy(SourceDomain.FOREIGN_INTERIM, *authorities, allow_derived=True),
            _policy(
                SourceDomain.DERIVED_FACT,
                *authorities,
                allow_provisional=official,
                allow_derived=True,
            ),
        ),
    )


_CONTRACTS = MappingProxyType(
    {
        SourceRegime.OFFICIAL_PRIMARY: _contract(
            SourceRegime.OFFICIAL_PRIMARY,
            (_REGULATOR, _ISSUER),
        ),
        SourceRegime.NORMALIZED_VENDOR_ONLY: _contract(
            SourceRegime.NORMALIZED_VENDOR_ONLY,
            (_VENDOR,),
        ),
        SourceRegime.COMBINED: _contract(
            SourceRegime.COMBINED,
            (_REGULATOR, _ISSUER, _VENDOR),
        ),
    }
)


@validate_call(config=_STRICT_FROZEN)
def contract_for(regime: SourceRegime) -> SourceRegimeContract:
    try:
        return _CONTRACTS[regime]
    except KeyError as exc:
        raise ValueError(f"unknown source regime: {regime!r}") from exc


@validate_call(config=_STRICT_FROZEN)
def contract_sha256(contract: SourceRegimeContract) -> str:
    """Hash the contract and immutable classification registry it depends on."""

    payload = {
        "contract": contract.model_dump(mode="json"),
        "source_classifications": {
            source_type.value: _CLASSIFICATION_BY_TYPE[source_type].model_dump(mode="json")
            for source_type in sorted(SourceType, key=lambda item: item.value)
        },
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def receipt_identity(contract: SourceRegimeContract) -> SourceRegimeReceiptIdentity:
    """Return the immutable contract identity to copy into downstream receipts."""

    return SourceRegimeReceiptIdentity(
        schema_version=contract.schema_version,
        regime=contract.regime,
        contract_sha256=contract_sha256(contract),
    )
