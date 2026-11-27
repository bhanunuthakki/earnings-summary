"""Narrow detect-secrets filter for retained test and fixture digest metadata."""

import os
import re

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
_GROUNDED_SEARCH_DIGEST_ASSIGNMENT = re.compile(
    r'[ \t]*expected_bundle_sha256[ \t]*=[ \t]*"[0-9a-f]{64}"[ \t]*(?:\r?\n)?\Z'
)
_IR_AUTHORITY_DIGEST_ASSIGNMENT = re.compile(
    r'[ \t]*expected_ir_sha256[ \t]*=[ \t]*"[0-9a-f]{64}"[ \t]*(?:\r?\n)?\Z'
)
_REPORT_CONTEXT_DIGEST_ASSIGNMENT = re.compile(
    r'[ \t]*GOLDEN_V1_(?:MODEL_|EVIDENCE_)?SHA256[ \t]*=[ \t]*"[0-9a-f]{64}"[ \t]*(?:\r?\n)?\Z'
)


def is_quality_evidence_hash(filename: str, line: str) -> bool:
    """Return whether one exact allowed line contains non-secret digest metadata."""
    canonical_path = filename.replace(os.sep, "/")
    if canonical_path in _MELI_SOURCE_FIXTURES:
        return _MELI_SOURCE_DIGEST_MEMBER.fullmatch(line) is not None
    if canonical_path == _INVESTING_REVIEW_BASELINE:
        return _INVESTING_REVIEW_DIGEST_MEMBER.fullmatch(line) is not None
    if canonical_path == "tests/test_grounded_search_foundation.py":
        return _GROUNDED_SEARCH_DIGEST_ASSIGNMENT.fullmatch(line) is not None
    if canonical_path == "tests/test_ir_authority_capture.py":
        return _IR_AUTHORITY_DIGEST_ASSIGNMENT.fullmatch(line) is not None
    if canonical_path == "tests/test_report_generation_context.py":
        return _REPORT_CONTEXT_DIGEST_ASSIGNMENT.fullmatch(line) is not None
    return False
