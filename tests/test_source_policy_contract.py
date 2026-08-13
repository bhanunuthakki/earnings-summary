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
    SOURCE_POLICY_CONFIG,
    AdapterKey,
    ArtifactKind,
    AuthorizationReason,
    CollectionMode,
    CollectionSource,
    CollectionTarget,
    FmpIssuerRules,
    IrEndpointRule,
    IrIssuerRules,
    IssuerAcquisitionPolicy,
    NameRule,
    SecIssuerRules,
    build_issuer_registry,
    decision_for,
    issuer_policy,
    mode_for_role,
    select_collection_targets,
)


def test_typed_collection_selector_orders_by_priority_and_requires_explicit_evaluation() -> None:
    selection = select_collection_targets(
        (
            CollectionTarget(ticker="IDX", coverage_role=ListType.INDEX_MEMBER),
            CollectionTarget(ticker="EVAL", coverage_role=ListType.EVALUATION),
            CollectionTarget(ticker="ASKED", coverage_role=ListType.EVALUATION, requested=True),
            CollectionTarget(ticker="PORT", coverage_role=ListType.PORTFOLIO),
            CollectionTarget(ticker="WATCH", coverage_role=ListType.WATCHLIST),
        ),
        source=CollectionSource.IR,
        artifact_kind=ArtifactKind.IR_DOCUMENT,
    )

    assert [item.target.ticker for item in selection.allowed] == ["PORT", "ASKED"]
    assert [item.decision.reason for item in selection.denied] == [
        AuthorizationReason.REQUEST_REQUIRED,
        AuthorizationReason.COVERAGE_DEPTH_DENIED,
        AuthorizationReason.COVERAGE_DEPTH_DENIED,
    ]


def test_reported_quarter_bound_is_typed_and_carried_by_collection_decisions() -> None:
    bound = SOURCE_POLICY_CONFIG.reported_quarter_window

    assert bound.max_quarters == 5
    assert (
        decision_for(
            ListType.PORTFOLIO,
            CollectionSource.IR,
            ArtifactKind.IR_DOCUMENT,
            requested=False,
        ).reported_quarter_window
        == bound
    )
    assert (
        decision_for(
            ListType.EVALUATION,
            CollectionSource.TRANSCRIPT,
            ArtifactKind.TEXT_TRANSCRIPT,
            requested=True,
        ).reported_quarter_window
        == bound
    )
    assert (
        decision_for(
            ListType.PORTFOLIO,
            CollectionSource.SEC,
            ArtifactKind.COMPANY_FACTS,
            requested=False,
        ).reported_quarter_window
        is None
    )


def test_operator_docs_match_the_stored_role_and_temporal_policy() -> None:
    docs = "\n".join(
        (PROJECT_ROOT / relative).read_text(encoding="utf-8")
        for relative in (
            "directives/edgar_pipeline.md",
            "directives/backfill_transcripts.md",
            "directives/fetch_ir_documents.md",
            "cron/SETUP_WINDOWS_SCHEDULER.md",
            "README.md",
        )
    )

    assert "portfolio is automatic" in docs
    assert "evaluation requires" in docs
    assert "fail closed" in docs
    assert "canonical last 5 reported" in docs
    assert "last 6 fiscal quarters" not in docs
    assert "covering the last 8 quarters" not in docs


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
    rubrik_golden = "".join(
        (
            "7c87233926cca937",  # pragma: allowlist secret
            "1cb89e719708aef6",  # pragma: allowlist secret
            "a60ccf58d6b7397d",  # pragma: allowlist secret
            "dda4282f8c25a875",  # pragma: allowlist secret
        )
    )
    wix_golden = "".join(
        (
            "fa0a55b4c509ef71",  # pragma: allowlist secret
            "16d9d60c67492fd6",  # pragma: allowlist secret
            "299b11054953748c",  # pragma: allowlist secret
            "c0da3b2335091e0f",  # pragma: allowlist secret
        )
    )
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
            approved_endpoints=(
                IrEndpointRule(host="issuer.example", exact_paths=("/investors",)),
            ),
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


@pytest.mark.parametrize(
    "host",
    ["  issuer.example", ".issuer.example", "issuer.example.", "issuer..example", "127.0.0.1"],
)
def test_ir_endpoint_rule_rejects_noncanonical_hosts(host: str) -> None:
    with pytest.raises(ValidationError):
        IrEndpointRule(host=host, exact_paths=("/investors",))


@pytest.mark.parametrize(
    "path",
    ["/../secret", "//investors", "/%2e%2e/secret", "/%252e%252e/secret"],
)
def test_ir_endpoint_rule_rejects_noncanonical_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        IrEndpointRule(host="issuer.example", exact_paths=(path,))
