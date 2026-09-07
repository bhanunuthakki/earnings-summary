"""BHA-147 exact-subject evidence collector (Phase A) and docs-only assembler (Phase B).

Phase A observes one immutable clean exact Git subject, brackets 40-hex
HEAD/tree/cleanliness before and after, and hash-binds raw producer bytes in
ignored staging without claiming bundle identity.

Phase B consumes only the hash-bound manifest plus staged bytes, byte-preserves
sources into an explicit canonical allowlist, and mints deterministic
``quality-score-admission/v1`` receipts for exactly the 14 non-architecture
score blocks and 10 hard gates. Score admission stays owned by
``src/quality/scoring.py``; this module never scores.
"""

from __future__ import annotations

from quality.evidence_bundle_assemble import assemble_bundle
from quality.evidence_bundle_collect import (
    collect_evidence,
    default_artifact_specs,
    load_collection_manifest,
)
from quality.evidence_bundle_io import verify_staged_bytes
from quality.evidence_bundle_models import (
    ALLOWED_SOURCE_PATHS,
    ArtifactRecord,
    ArtifactSpec,
    BundleAssembly,
    CollectionManifest,
    SubjectSnapshot,
    admission_path_for,
    allowed_bundle_paths,
    bound_violations,
    non_architecture_blocks,
)
from quality.evidence_bundle_validate import record_score_evidence, validate_bundle_diff
from quality.scoring import (
    ADMISSION_GENERATOR_PATH,
    HARD_GATES,
    SCORE_BLOCKS,
)

__all__ = (
    "ADMISSION_GENERATOR_PATH",
    "ALLOWED_SOURCE_PATHS",
    "HARD_GATES",
    "SCORE_BLOCKS",
    "ArtifactRecord",
    "ArtifactSpec",
    "BundleAssembly",
    "CollectionManifest",
    "SubjectSnapshot",
    "admission_path_for",
    "allowed_bundle_paths",
    "assemble_bundle",
    "bound_violations",
    "collect_evidence",
    "default_artifact_specs",
    "load_collection_manifest",
    "non_architecture_blocks",
    "record_score_evidence",
    "validate_bundle_diff",
    "verify_staged_bytes",
)
