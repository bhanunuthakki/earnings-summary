"""Narrow detect-secrets filter for generated quality-evidence hashes.

This suppresses scanner false positives for schema-defined digest members only.
detect-secrets may normalize a JSON colon to an equals sign during its eager
second scan; that exact equivalent is accepted solely for the same JSON paths.
This filter neither validates evidence integrity nor authorizes credentials or
other secret-looking content.
"""

import os
import re

_CANONICAL_PATHS = frozenset(
    {
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
        "docs/quality/roadmap-reconciliation.json",
        "docs/quality/test-db-patterns-baseline.json",
    }
)

_HASH40_KEYS = "|".join(("subject_commit", "scoped_commit", "commit_hash", "revision"))
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
    )
)
_HASH40_MEMBER = re.compile(
    rf'[ \t]*"(?:{_HASH40_KEYS})"[ \t]*(?::|=)[ \t]*"[0-9a-f]{{40}}"[ \t]*,?[ \t]*(?:\r?\n)?\Z'
)
_HASH64_MEMBER = re.compile(
    rf'[ \t]*"(?:{_HASH64_KEYS})"[ \t]*(?::|=)[ \t]*"[0-9a-f]{{64}}"[ \t]*,?[ \t]*(?:\r?\n)?\Z'
)


def is_quality_evidence_hash(filename: str, line: str) -> bool:
    """Return whether one exact allowed JSON member is generated digest metadata."""

    canonical_path = filename.replace(os.sep, "/")
    if canonical_path not in _CANONICAL_PATHS:
        return False
    return _HASH40_MEMBER.fullmatch(line) is not None or _HASH64_MEMBER.fullmatch(line) is not None
