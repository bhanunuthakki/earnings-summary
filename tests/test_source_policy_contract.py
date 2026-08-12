from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from models.companies import ListType  # noqa: E402
from pipeline.source_policy import (  # noqa: E402
    DISPLAY_ROLE_ORDER,
    AdapterKey,
    ArtifactKind,
    AuthorizationReason,
    CollectionMode,
    CollectionSource,
    FmpIssuerRules,
    IrIssuerRules,
    IssuerAcquisitionPolicy,
    NameRule,
    SecIssuerRules,
    build_issuer_registry,
    decision_for,
    issuer_policy,
    mode_for_role,
)


def test_coverage_policy_order_and_unknowns_are_fail_closed() -> None:
    assert set(ListType) == {
        ListType.PORTFOLIO,
        ListType.EVALUATION,
        ListType.WATCHLIST,
        ListType.INDEX_MEMBER,
        ListType.NONE,
        ListType.ETF,
    }
    assert DISPLAY_ROLE_ORDER == (
        ListType.PORTFOLIO,
        ListType.EVALUATION,
        ListType.WATCHLIST,
        ListType.INDEX_MEMBER,
    )
    assert mode_for_role(ListType.PORTFOLIO) is CollectionMode.AUTOMATIC_FULL
    with pytest.raises(ValueError, match="unknown coverage role"):
        mode_for_role("priority")
    with pytest.raises(ValueError, match="unknown collection source"):
        decision_for(ListType.PORTFOLIO, "web", ArtifactKind.METADATA, requested=False)
    with pytest.raises(ValueError, match="unknown artifact kind"):
        decision_for(ListType.PORTFOLIO, CollectionSource.SEC, "all", requested=False)


def test_source_authorization_never_elevates_lower_priority_roles() -> None:
    assert (
        decision_for(
            ListType.PORTFOLIO,
            CollectionSource.SEC,
            ArtifactKind.FILING_PACKAGE,
            requested=False,
        ).reason
        is AuthorizationReason.AUTOMATIC
    )
    assert (
        decision_for(
            ListType.EVALUATION,
            CollectionSource.IR,
            ArtifactKind.IR_DOCUMENT,
            requested=False,
        ).reason
        is AuthorizationReason.REQUEST_REQUIRED
    )
    assert decision_for(
        ListType.EVALUATION,
        CollectionSource.IR,
        ArtifactKind.IR_DOCUMENT,
        requested=True,
    ).allowed
    for role in (ListType.WATCHLIST, ListType.INDEX_MEMBER, ListType.NONE, ListType.ETF):
        assert not decision_for(
            role,
            CollectionSource.IR,
            ArtifactKind.IR_DOCUMENT,
            requested=True,
        ).allowed
    assert decision_for(
        ListType.INDEX_MEMBER,
        CollectionSource.FMP,
        ArtifactKind.FINANCIAL_FACT,
        requested=False,
    ).allowed
    for source in CollectionSource:
        assert not decision_for(
            ListType.INDEX_MEMBER,
            source,
            ArtifactKind.METADATA,
            requested=True,
        ).allowed
    assert not decision_for(
        ListType.PORTFOLIO,
        CollectionSource.TRANSCRIPT,
        ArtifactKind.WEBCAST,
        requested=True,
    ).allowed


def test_policy_is_deeply_immutable_and_hashes_are_golden() -> None:
    rubrik = issuer_policy("RBRK")
    wix = issuer_policy("WIX")
    original_hash = rubrik.policy_sha256
    with pytest.raises(ValidationError):
        rubrik.sec.relevant_sections[0].sections += ()
    with pytest.raises(ValidationError):
        rubrik.fmp.endpoint_aliases += (NameRule(source_name="old", canonical_name="new"),)
    assert issuer_policy("rbrk").policy_sha256 == original_hash
    rubrik_golden = "542c3d70f3599e0ffdeeb14fa2005271225f3a0ed2c389d912ac566cbfb7781e"  # pragma: allowlist secret
    wix_golden = "112015facabe16776da9d4e64cd1f05c64d16eee4d7e95034d96121e4b6cc082"  # pragma: allowlist secret
    assert rubrik.policy_sha256 == rubrik_golden
    assert wix.policy_sha256 == wix_golden


def _policy(issuer_id: str, *aliases: str) -> IssuerAcquisitionPolicy:
    return IssuerAcquisitionPolicy(
        issuer_id=issuer_id,
        ticker_aliases=aliases,
        sec=SecIssuerRules(filing_forms=()),
        ir=IrIssuerRules(
            authority_url="https://issuer.example/investors",
            adapter_key=AdapterKey.RUBRIK_QUARTER_TABLE,
            publisher_host_suffixes=("issuer.example",),
            fiscal_year_end="12-31",
            admitted_doc_types=(),
        ),
    )


@pytest.mark.parametrize(
    "policies",
    [
        (_policy("issuer-a", "AAA"), _policy("ISSUER-A", "BBB")),
        (_policy("issuer-a", "AAA"), _policy("issuer-b", "aaa")),
        (_policy("issuer-a", "issuer-b"), _policy("ISSUER-B", "BBB")),
        (_policy("ISSUER-B", "BBB"), _policy("issuer-a", "issuer-b")),
    ],
)
def test_registry_rejects_duplicate_and_cross_namespace_identifiers(
    policies: tuple[IssuerAcquisitionPolicy, IssuerAcquisitionPolicy],
) -> None:
    with pytest.raises(ValueError, match="duplicate or ambiguous issuer identifier"):
        build_issuer_registry(policies)


def test_rule_changes_and_invalid_shapes_are_detected() -> None:
    rubrik = issuer_policy("RBRK")
    changed = rubrik.model_copy(
        update={
            "fmp": FmpIssuerRules(
                label_overrides=(NameRule(source_name="sales", canonical_name="revenue"),)
            )
        }
    )
    assert changed.policy_sha256 != rubrik.policy_sha256
    with pytest.raises(ValueError, match="unknown issuer acquisition policy"):
        issuer_policy("UNKNOWN")
    with pytest.raises(ValidationError):
        FmpIssuerRules.model_validate({"label_overrides": {"sales": "revenue"}})
