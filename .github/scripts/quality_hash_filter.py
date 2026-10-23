"""Narrow detect-secrets filter for generated quality-evidence hashes.

This suppresses scanner false positives for schema-defined digest members and
one named compatibility-test digest assignment.
detect-secrets may normalize a JSON colon to an equals sign during its eager
second scan; that exact equivalent is accepted solely for the same JSON paths.
This filter neither validates evidence integrity nor authorizes credentials or
other secret-looking content.
"""

import os
import re

_CANONICAL_PATHS = frozenset(
    {
        "config/quality_roadmap_claims.json",
        "config/quality_roadmap_owners.json",
        "docs/quality/roadmap-freeze.json",
        "docs/quality/admission-block-cleanup-deletion_proof.json",
        "docs/quality/admission-block-cleanup-lifecycle_inventory.json",
        "docs/quality/admission-block-cleanup-reachability_oracle.json",
        "docs/quality/admission-block-cleanup-reconstructability.json",
        "docs/quality/admission-block-cleanup-schema_ownership.json",
        "docs/quality/admission-block-efficiency-dcf_disposition.json",
        "docs/quality/admission-block-efficiency-integrity_audit.json",
        "docs/quality/admission-block-efficiency-request_path.json",
        "docs/quality/admission-block-efficiency-test_ci.json",
        "docs/quality/admission-block-maintainability-authorities.json",
        "docs/quality/admission-block-maintainability-duplication.json",
        "docs/quality/admission-block-maintainability-enforced_ratchets.json",
        "docs/quality/admission-block-maintainability-static_quality.json",
        "docs/quality/admission-block-maintainability-sustainable_tests.json",
        "docs/quality/admission-gate-active_static_zero.json",
        "docs/quality/admission-gate-architecture_duplication_ratchets.json",
        "docs/quality/admission-gate-benchmark_contract.json",
        "docs/quality/admission-gate-compatibility_parity.json",
        "docs/quality/admission-gate-database_authority.json",
        "docs/quality/admission-gate-deletion_evidence.json",
        "docs/quality/admission-gate-network_consolidation_safety.json",
        "docs/quality/admission-gate-owner_acceptance.json",
        "docs/quality/admission-gate-repository_gates.json",
        "docs/quality/admission-gate-touched_reachability_closure.json",
        "docs/quality/architecture-ratchet.json",
        "docs/quality/duplicates-ratchet.json",
        "docs/quality/lifecycle-inventory.json",
        "docs/quality/performance-baseline.json",
        "docs/quality/reachability-check.json",
        "docs/quality/pipeline-completion-announcement-review-20261023.json",
        "docs/quality/pipeline-completion-bulk-review-20261023.json",
        "docs/quality/pipeline-completion-exchange-review-20261023.json",
        "docs/quality/pipeline-completion-final-delta-review-20261023.json",
        "docs/quality/pipeline-completion-growth-20261023.json",
        "docs/quality/pipeline-completion-pdf-review-20261023.json",
        "docs/quality/pipeline-completion-plan-review-20261022.json",
        "docs/quality/pipeline-completion-rich-review-20261023.json",
        "docs/quality/pipeline-completion-transcript-review-20261023.json",
        "docs/quality/roadmap-reconciliation.json",
        "docs/quality/test-db-conversions.json",
        "docs/quality/test-db-patterns-baseline.json",
    }
)

_HASH40_KEYS = "|".join(
    ("subject_commit", "scoped_commit", "commit_hash", "revision", "subject_tree")
)
_HASH64_KEYS = "|".join(
    (
        "generator_sha256",
        "sha256",
        "scanner_sha256",
        "source_sha256",
        "source_hash",
        "scanner_hash",
        "normalized_hash",
        "fingerprint",
        "tracked_tree_hash",
        "reachability_graph_hash",
        "dispositions_sha256",
        "config_sha256",
        "output_sha256",
        "source_manifest_sha256",
        "content_sha256",
        "claim_manifest_sha256",
        "plan_sha256",
        "owner_snapshot_sha256",
        "scope_sha256",
    )
)
_HASH40_MEMBER = re.compile(
    rf'[ \t]*"(?:{_HASH40_KEYS})"[ \t]*(?::|=)[ \t]*"[0-9a-f]{{40}}"[ \t]*,?[ \t]*(?:\r?\n)?\Z'
)
_HASH64_MEMBER = re.compile(
    rf'[ \t]*"(?:{_HASH64_KEYS})"[ \t]*(?::|=)[ \t]*"[0-9a-f]{{64}}"[ \t]*,?[ \t]*(?:\r?\n)?\Z'
)
_ROADMAP_FREEZE_SOURCE_IDENTITY_MEMBER = re.compile(
    r'[ \t]*"source_identity"[ \t]*(?::|=)[ \t]*"[0-9a-f]{64}"[ \t]*,?[ \t]*(?:\r?\n)?\Z'
)
_MELI_SOURCE_FIXTURES = frozenset(
    {
        "tests/fixtures/meli_reported_tables/provenance.json",
        "tests/fixtures/meli_reported_statements/provenance.json",
    }
)
_MELI_SOURCE_DIGEST_MEMBER = re.compile(
    r'[ \t]*"(?:source_sha256|snippet_sha256|sha256)"[ \t]*(?::|=)[ \t]*"[0-9a-f]{64}"[ \t]*,?[ \t]*(?:\r?\n)?\Z'
)
_INVESTING_REVIEW_BASELINE = (
    "src/advisor/skills/earnings-summary-investing/references/reviewed-sources.json"
)
_INVESTING_REVIEW_DIGEST_MEMBER = re.compile(
    r'[ \t]*"sha256"[ \t]*(?::|=)[ \t]*"[0-9a-f]{64}"[ \t]*,?[ \t]*(?:\r?\n)?\Z'
)
_CANONICAL_HELPERS_COMMIT_MEMBER = re.compile(
    r'[ \t]*"(?:generated_commit|source_commit|capture_commit)"[ \t]*(?::|=)[ \t]*"[0-9a-f]{40}"[ \t]*,?[ \t]*(?:\r?\n)?\Z'
)
_GROUNDED_SEARCH_DIGEST_ASSIGNMENT = re.compile(
    r'[ \t]*expected_bundle_sha256[ \t]*=[ \t]*"[0-9a-f]{64}"[ \t]*(?:\r?\n)?\Z'
)
_FILING_XBRL_HASH_KEYS = {
    "docs/quality/filing-xbrl-architecture-review.json": {"integrated_main": 40},
    "docs/quality/filing-xbrl-v2-candidate.json": {
        "supersedes_candidate_commit": 40,
        "execution/build_filing_xbrl_processor_bundle.py": 64,
        "execution/filing_xbrl_bridge.py": 64,
        "execution/filing_xbrl_appcontainer_launcher.cs": 64,
        "execution/filing_xbrl_appcontainer_launcher.py": 64,
        "src/runtime/windows_appcontainer.py": 64,
        "src/runtime/windows_appcontainer_supervisor.py": 64,
        "src/filings/xbrl_units.py": 64,
        "runtime_artifact_sha256": 64,
        "sandbox_launcher_sha256": 64,
        "manifest_sha256": 64,
        "manifest_artifact_sha256": 64,
    },
    "docs/quality/filing-xbrl-v2-approval-proposal.json": {
        "manifest_sha256": 64,
        "manifest_artifact_sha256": 64,
        "runtime_artifact_sha256": 64,
        "sandbox_launcher_sha256": 64,
        "bridge_source_sha256": 64,
        "launcher_source_sha256": 64,
        "unit_source_sha256": 64,
        "support_source_sha256": 64,
    },
}
_FILING_XBRL_HASH_MEMBER = re.compile(
    r'[ \t]*"(?P<key>[^"\r\n]+)"[ \t]*(?::|=)[ \t]*"(?P<digest>[0-9a-f]+)"[ \t]*,?[ \t]*(?:\r?\n)?\Z'
)


def is_quality_evidence_hash(filename: str, line: str) -> bool:
    """Return whether one exact allowed line contains non-secret digest metadata."""

    canonical_path = filename.replace(os.sep, "/")
    if canonical_path in _FILING_XBRL_HASH_KEYS:
        member = _FILING_XBRL_HASH_MEMBER.fullmatch(line)
        return member is not None and len(member["digest"]) == _FILING_XBRL_HASH_KEYS[
            canonical_path
        ].get(member["key"])
    if canonical_path in _MELI_SOURCE_FIXTURES:
        return _MELI_SOURCE_DIGEST_MEMBER.fullmatch(line) is not None
    if canonical_path == _INVESTING_REVIEW_BASELINE:
        return _INVESTING_REVIEW_DIGEST_MEMBER.fullmatch(line) is not None
    if canonical_path == "docs/quality/canonical-helpers.json":
        return _CANONICAL_HELPERS_COMMIT_MEMBER.fullmatch(line) is not None
    if canonical_path == "tests/test_grounded_search_foundation.py":
        return _GROUNDED_SEARCH_DIGEST_ASSIGNMENT.fullmatch(line) is not None
    if canonical_path not in _CANONICAL_PATHS:
        return False
    if _HASH40_MEMBER.fullmatch(line) is not None or _HASH64_MEMBER.fullmatch(line) is not None:
        return True
    return (
        canonical_path == "docs/quality/roadmap-freeze.json"
        and _ROADMAP_FREEZE_SOURCE_IDENTITY_MEMBER.fullmatch(line) is not None
    )
