from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from models.companies import ListType
from pipeline.source_policy import (
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
    StoredIdentityStatus,
    authorize_collection_target_in_connection,
    build_issuer_registry,
    decision_for,
    issuer_policy,
    mode_for_role,
    select_collection_targets,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_typed_collection_selector_orders_all_active_research_roles_by_priority() -> None:
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

    assert [item.target.ticker for item in selection.allowed] == ["PORT", "ASKED", "EVAL", "WATCH"]
    assert [item.decision.reason for item in selection.denied] == [
        AuthorizationReason.COVERAGE_DEPTH_DENIED,
    ]


@pytest.mark.parametrize("role", [ListType.PORTFOLIO, ListType.EVALUATION, ListType.WATCHLIST])
@pytest.mark.parametrize(
    ("source", "artifact"),
    [
        (CollectionSource.SEC, ArtifactKind.COMPANY_FACTS),
        (CollectionSource.SEC, ArtifactKind.FILING_PACKAGE),
        (CollectionSource.SEC, ArtifactKind.FILING_SECTION),
        (CollectionSource.IR, ArtifactKind.IR_DOCUMENT),
        (CollectionSource.FMP, ArtifactKind.FINANCIAL_FACT),
        (CollectionSource.TRANSCRIPT, ArtifactKind.TEXT_TRANSCRIPT),
    ],
)
def test_active_research_roles_receive_automatic_full_collection(
    role: ListType, source: CollectionSource, artifact: ArtifactKind
) -> None:
    decision = decision_for(role, source, artifact, requested=False)
    assert decision.allowed
    assert decision.mode is CollectionMode.AUTOMATIC_FULL
    assert decision.reason is AuthorizationReason.AUTOMATIC


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

    assert "portfolio, evaluation, and watchlist" in docs
    assert "automatic full" in docs
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


def test_source_authorization_preserves_excluded_roles_and_artifacts() -> None:
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
        is AuthorizationReason.AUTOMATIC
    )
    assert decision_for(
        ListType.EVALUATION,
        CollectionSource.IR,
        ArtifactKind.IR_DOCUMENT,
        requested=True,
    ).allowed
    for role in (ListType.INDEX_MEMBER, ListType.NONE, ListType.ETF):
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
            "02fcede6699925be",  # pragma: allowlist secret
            "c9393b618a12d0e0",  # pragma: allowlist secret
            "1a8c3bb4f832c33c",  # pragma: allowlist secret
            "4d5294f3de390a05",  # pragma: allowlist secret
        )
    )
    wix_golden = "".join(
        (
            "89b52dfa720258bc",  # pragma: allowlist secret
            "2413899d8e6c5cd9",  # pragma: allowlist secret
            "3e36e99a4b354680",  # pragma: allowlist secret
            "676d95dd6a73b6b4",  # pragma: allowlist secret
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


@pytest.mark.parametrize("instrument", ["equity", "adr", "etf", None, "invalid"])
@pytest.mark.parametrize(
    ("source", "artifact"),
    [
        (CollectionSource.SEC, ArtifactKind.FILING_PACKAGE),
        (CollectionSource.IR, ArtifactKind.IR_DOCUMENT),
        (CollectionSource.TRANSCRIPT, ArtifactKind.TEXT_TRANSCRIPT),
    ],
)
def test_stored_instrument_bounds_automatic_research_collection(
    instrument: str | None, source: CollectionSource, artifact: ArtifactKind
) -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.execute(
            "CREATE TABLE tracked_companies "
            "(ticker TEXT, list_type TEXT, archived_at TEXT, instrument_type TEXT)"
        )
        conn.execute(
            "INSERT INTO tracked_companies VALUES ('TEST', 'evaluation', NULL, ?)", (instrument,)
        )
        result = authorize_collection_target_in_connection(
            conn, "TEST", requested=False, source=source, artifact_kind=artifact
        )
        assert result.allowed is (instrument in ("equity", "adr"))
        if instrument == "etf":
            assert result.status is StoredIdentityStatus.INSTRUMENT_NOT_APPLICABLE
        elif instrument not in ("equity", "adr"):
            assert result.status is StoredIdentityStatus.INSTRUMENT_UNAVAILABLE
        metadata = authorize_collection_target_in_connection(
            conn, "TEST", requested=False, source=source, artifact_kind=ArtifactKind.METADATA
        )
        assert metadata.allowed


def test_missing_instrument_schema_fails_closed_for_corporate_acquisition() -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.execute(
            "CREATE TABLE tracked_companies (ticker TEXT, list_type TEXT, archived_at TEXT)"
        )
        conn.execute("INSERT INTO tracked_companies VALUES ('TEST', 'watchlist', NULL)")
        result = authorize_collection_target_in_connection(
            conn,
            "TEST",
            requested=False,
            source=CollectionSource.SEC,
            artifact_kind=ArtifactKind.FILING_PACKAGE,
        )
        assert result.status is StoredIdentityStatus.INSTRUMENT_UNAVAILABLE
        assert not result.allowed
