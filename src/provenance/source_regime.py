"""Provider-neutral source-regime contracts for reproducible research builds.

``SourceType`` records the concrete origin currently persisted by the app. A
source regime instead governs which evidence authorities and lifecycle states
may supply each semantic domain. Provider identity remains attribution; it is
never the downstream selection contract.

This module is read-only policy. It does not mutate the database, choose a fact
winner, or build a filtered projection. Projectors must use ``admits`` as the
single fail-closed admission API and retain observation/resolution lineage.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from models.documents import SourceType


class EvidenceAuthority(StrEnum):
    """Authority responsible for the underlying evidence."""

    REGULATOR = "regulator"
    ISSUER = "issuer"
    THIRD_PARTY = "third_party"
    OWNER = "owner"


class EvidenceLifecycle(StrEnum):
    """Whether evidence is primary, provisional, or transformed."""

    PRIMARY = "primary"
    PROVISIONAL = "provisional"
    DERIVED = "derived"


class SourceDomain(StrEnum):
    """Semantic input domains whose source policies differ materially."""

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
    """Stable regime identities used by offline projections and receipts."""

    OFFICIAL_PRIMARY = "official_primary"
    NORMALIZED_VENDOR_ONLY = "normalized_vendor_only"
    COMBINED = "combined"


class AsOfRule(StrEnum):
    """Temporal admission rule a projection must enforce for a domain."""

    PUBLISHED_BY_CUTOFF = "published_by_cutoff"
    OBSERVED_BY_CUTOFF = "observed_by_cutoff"
    SEALED_AT_CUTOFF = "sealed_at_cutoff"


class DegradationBehavior(StrEnum):
    """Fail-closed behavior when no allowed input is available."""

    UNAVAILABLE = "unavailable"
    EXPLICIT_NOT_APPLICABLE = "explicit_not_applicable"
    OWNER_INPUT_REQUIRED = "owner_input_required"


class SourceClassification(BaseModel):
    """Provider-neutral classification of one persisted source type."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    authority: EvidenceAuthority | None
    lifecycle: EvidenceLifecycle


_CLASSIFICATION_BY_TYPE: dict[SourceType, SourceClassification] = {
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
    # A transformation inherits authority from its validated primary parents.
    SourceType.LLM_EXTRACTED: SourceClassification(
        authority=None,
        lifecycle=EvidenceLifecycle.DERIVED,
    ),
}

if set(_CLASSIFICATION_BY_TYPE) != set(SourceType):
    missing = sorted(source.value for source in set(SourceType) - set(_CLASSIFICATION_BY_TYPE))
    extra = sorted(source.value for source in set(_CLASSIFICATION_BY_TYPE) - set(SourceType))
    raise RuntimeError(f"source classification is not closed: missing={missing}, extra={extra}")


def classification_for_source_type(source_type: SourceType) -> SourceClassification:
    """Return the immutable provider-neutral classification for a source type."""

    return _CLASSIFICATION_BY_TYPE[source_type]


class DomainSourcePolicy(BaseModel):
    """Deeply immutable admission policy for one semantic domain."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    domain: SourceDomain
    authority_precedence: tuple[EvidenceAuthority, ...]
    allow_provisional: bool = False
    allow_derived: bool = False
    as_of_rule: AsOfRule
    degradation: DegradationBehavior


class SourceRegimeContract(BaseModel):
    """Closed, deeply immutable policy for one reproducible build regime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "source-regime-contract@1"
    regime: SourceRegime
    policies: tuple[DomainSourcePolicy, ...]
    dcf_input_domains: tuple[SourceDomain, ...]

    def policy_for(self, domain: SourceDomain) -> DomainSourcePolicy:
        for policy in self.policies:
            if policy.domain is domain:
                return policy
        raise ValueError(f"regime {self.regime.value} has no policy for {domain.value}")

    def precedence(self, domain: SourceDomain) -> tuple[EvidenceAuthority, ...]:
        return self.policy_for(domain).authority_precedence

    def admits(
        self,
        *,
        domain: SourceDomain,
        source_type: SourceType,
        parent_source_types: tuple[SourceType, ...] = (),
    ) -> bool:
        """Validate one observation and its lineage, then return ``True``.

        There is intentionally no public family-only admission helper. Derived
        evidence cannot be admitted without parents, and non-derived evidence
        cannot claim transformation parents.
        """

        policy = self.policy_for(domain)
        classification = classification_for_source_type(source_type)

        if classification.lifecycle is not EvidenceLifecycle.DERIVED:
            if parent_source_types:
                raise ValueError("non-derived source must not declare derived parents")
            authority = classification.authority
            if authority not in policy.authority_precedence:
                raise ValueError(
                    f"source {source_type.value} is excluded from {domain.value} "
                    f"under {self.regime.value}"
                )
            if (
                classification.lifecycle is EvidenceLifecycle.PROVISIONAL
                and not policy.allow_provisional
            ):
                raise ValueError(
                    f"provisional source {source_type.value} is excluded from "
                    f"{domain.value} under {self.regime.value}"
                )
            return True

        if not policy.allow_derived:
            raise ValueError(
                f"derived source {source_type.value} is excluded from {domain.value} "
                f"under {self.regime.value}"
            )
        if not parent_source_types:
            raise ValueError("derived source requires at least one primary parent")

        for parent_source_type in parent_source_types:
            parent = classification_for_source_type(parent_source_type)
            if parent.lifecycle is EvidenceLifecycle.DERIVED:
                raise ValueError("derived source cannot use another derived primary parent")
            if parent.authority not in policy.authority_precedence:
                raise ValueError(
                    f"excluded parent source {parent_source_type.value} for "
                    f"{domain.value} under {self.regime.value}"
                )
            if parent.lifecycle is EvidenceLifecycle.PROVISIONAL and not policy.allow_provisional:
                raise ValueError(
                    f"excluded provisional parent {parent_source_type.value} for "
                    f"{domain.value} under {self.regime.value}"
                )
        return True


def _policy(
    domain: SourceDomain,
    *authorities: EvidenceAuthority,
    allow_provisional: bool = False,
    allow_derived: bool = False,
    as_of_rule: AsOfRule = AsOfRule.PUBLISHED_BY_CUTOFF,
    degradation: DegradationBehavior = DegradationBehavior.UNAVAILABLE,
) -> DomainSourcePolicy:
    if len(set(authorities)) != len(authorities):
        raise ValueError("authority precedence cannot contain duplicates")
    return DomainSourcePolicy(
        domain=domain,
        authority_precedence=authorities,
        allow_provisional=allow_provisional,
        allow_derived=allow_derived,
        as_of_rule=as_of_rule,
        degradation=degradation,
    )


_REGULATOR = EvidenceAuthority.REGULATOR
_ISSUER = EvidenceAuthority.ISSUER
_VENDOR = EvidenceAuthority.THIRD_PARTY
_OWNER = EvidenceAuthority.OWNER

_DCF_INPUT_DOMAINS = (
    SourceDomain.REPORTED_FACT,
    SourceDomain.ESTIMATE,
    SourceDomain.PRICE,
    SourceDomain.OWNER_STATE,
    SourceDomain.MANUAL_OVERRIDE,
    SourceDomain.DERIVED_FACT,
)


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


_CONTRACTS: dict[SourceRegime, SourceRegimeContract] = {
    SourceRegime.OFFICIAL_PRIMARY: SourceRegimeContract(
        regime=SourceRegime.OFFICIAL_PRIMARY,
        dcf_input_domains=_DCF_INPUT_DOMAINS,
        policies=(
            _policy(SourceDomain.REPORTED_FACT, _REGULATOR, _ISSUER, allow_provisional=True),
            _policy(SourceDomain.COMPANY_KPI, _REGULATOR, _ISSUER, allow_derived=True),
            _policy(SourceDomain.SEGMENT, _REGULATOR, _ISSUER, allow_derived=True),
            _policy(SourceDomain.FILING, _REGULATOR, _ISSUER, allow_provisional=True),
            # Consensus estimates and total-return prices are explicit external
            # carve-outs; they do not relax reported-fact policy.
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
            _policy(SourceDomain.TRANSCRIPT, _ISSUER, allow_derived=True),
            *_owner_policies(),
            _policy(
                SourceDomain.FOREIGN_INTERIM,
                _REGULATOR,
                _ISSUER,
                allow_derived=True,
            ),
            _policy(
                SourceDomain.DERIVED_FACT,
                _REGULATOR,
                _ISSUER,
                allow_provisional=True,
                allow_derived=True,
            ),
        ),
    ),
    SourceRegime.NORMALIZED_VENDOR_ONLY: SourceRegimeContract(
        regime=SourceRegime.NORMALIZED_VENDOR_ONLY,
        dcf_input_domains=_DCF_INPUT_DOMAINS,
        policies=(
            _policy(SourceDomain.REPORTED_FACT, _VENDOR),
            _policy(SourceDomain.COMPANY_KPI, _VENDOR, allow_derived=True),
            _policy(SourceDomain.SEGMENT, _VENDOR, allow_derived=True),
            _policy(SourceDomain.FILING, _VENDOR),
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
            _policy(SourceDomain.TRANSCRIPT, _VENDOR, allow_derived=True),
            *_owner_policies(),
            _policy(SourceDomain.FOREIGN_INTERIM, _VENDOR, allow_derived=True),
            _policy(SourceDomain.DERIVED_FACT, _VENDOR, allow_derived=True),
        ),
    ),
    SourceRegime.COMBINED: SourceRegimeContract(
        regime=SourceRegime.COMBINED,
        dcf_input_domains=_DCF_INPUT_DOMAINS,
        policies=(
            _policy(
                SourceDomain.REPORTED_FACT,
                _REGULATOR,
                _ISSUER,
                _VENDOR,
                allow_provisional=True,
            ),
            _policy(
                SourceDomain.COMPANY_KPI,
                _REGULATOR,
                _ISSUER,
                _VENDOR,
                allow_derived=True,
            ),
            _policy(
                SourceDomain.SEGMENT,
                _REGULATOR,
                _ISSUER,
                _VENDOR,
                allow_derived=True,
            ),
            _policy(
                SourceDomain.FILING,
                _REGULATOR,
                _ISSUER,
                _VENDOR,
                allow_provisional=True,
            ),
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
            _policy(SourceDomain.TRANSCRIPT, _ISSUER, _VENDOR, allow_derived=True),
            *_owner_policies(),
            _policy(
                SourceDomain.FOREIGN_INTERIM,
                _REGULATOR,
                _ISSUER,
                _VENDOR,
                allow_derived=True,
            ),
            _policy(
                SourceDomain.DERIVED_FACT,
                _REGULATOR,
                _ISSUER,
                _VENDOR,
                allow_provisional=True,
                allow_derived=True,
            ),
        ),
    ),
}

_EXPECTED_DOMAINS = set(SourceDomain)
for _regime, _contract in _CONTRACTS.items():
    _domains = tuple(policy.domain for policy in _contract.policies)
    if len(_domains) != len(set(_domains)):
        raise RuntimeError(f"source-regime contract {_regime.value} has duplicate domains")
    if set(_domains) != _EXPECTED_DOMAINS:
        missing = sorted(domain.value for domain in _EXPECTED_DOMAINS - set(_domains))
        extra = sorted(domain.value for domain in set(_domains) - _EXPECTED_DOMAINS)
        raise RuntimeError(
            f"source-regime contract {_regime.value} is not closed: "
            f"missing={missing}, extra={extra}"
        )


def contract_for(regime: SourceRegime) -> SourceRegimeContract:
    """Return the registered deeply immutable contract for ``regime``."""

    return _CONTRACTS[regime]


def contract_sha256(contract: SourceRegimeContract) -> str:
    """Return a stable digest suitable for projections and artifact receipts."""

    payload = json.dumps(
        contract.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
