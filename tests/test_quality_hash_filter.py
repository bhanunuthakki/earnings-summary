"""Regression tests for the narrow quality-evidence detect-secrets filter."""

import hashlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Protocol, cast

import pytest

ROOT = Path(__file__).resolve().parents[1]
FILTER_PATH = ROOT / ".github" / "scripts" / "quality_hash_filter.py"


class QualityHashFilter(Protocol):
    def is_quality_evidence_hash(self, filename: str, line: str) -> bool: ...


def _load_filter() -> QualityHashFilter:
    spec = importlib.util.spec_from_file_location("quality_hash_filter", FILTER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(getattr(module, "is_quality_evidence_hash", None))
    return cast(QualityHashFilter, module)


FILTER = _load_filter()
CANONICAL_PATHS = (
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
    "docs/quality/roadmap-reconciliation.json",
    "docs/quality/test-db-patterns-baseline.json",
)
HASH40_KEYS = ("subject_commit", "scoped_commit", "commit_hash", "revision", "subject_tree")
HASH64_KEYS = (
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
UNRECOGNIZED_MEMBER = "unrecognized_hash"


@pytest.mark.parametrize("filename", CANONICAL_PATHS)
@pytest.mark.parametrize("key", HASH40_KEYS)
def test_all_canonical_paths_accept_each_40_character_hash_key(filename: str, key: str) -> None:
    assert FILTER.is_quality_evidence_hash(filename, f'  "{key}": "{"a" * 40}",')


@pytest.mark.parametrize("filename", CANONICAL_PATHS)
@pytest.mark.parametrize("key", HASH64_KEYS)
def test_all_canonical_paths_accept_each_64_character_hash_key(filename: str, key: str) -> None:
    assert FILTER.is_quality_evidence_hash(filename, f'  "{key}": "{"b" * 64}",')


def test_windows_separator_is_normalized_only_on_native_windows() -> None:
    assert FILTER.is_quality_evidence_hash(
        "docs\\quality\\architecture-ratchet.json", f'"sha256": "{"a" * 64}"'
    ) is (os.sep == "\\")


def test_detect_secrets_eager_json_delimiter_normalization_is_accepted() -> None:
    assert FILTER.is_quality_evidence_hash(
        "docs/quality/architecture-ratchet.json", f'"sha256" = "{"a" * 64}"'
    )


@pytest.mark.parametrize("delimiter", (":", "="))
def test_roadmap_freeze_source_identity_accepts_only_complete_64_hex_member(
    delimiter: str,
) -> None:
    assert FILTER.is_quality_evidence_hash(
        "docs/quality/roadmap-freeze.json",
        f'  "source_identity" {delimiter} "{"a" * 64}",',
    )


@pytest.mark.parametrize(
    ("filename", "line"),
    (
        ("docs/quality/architecture-ratchet.json", f'"source_identity": "{"a" * 64}"'),
        ("docs/quality/roadmap-freeze.json", f'"source_identity": "{"a" * 40}"'),
        ("docs/quality/roadmap-freeze.json", f'"source_identity": "{"A" * 64}"'),
        ("docs/quality/roadmap-freeze.json", f'"source_identity": "{"a" * 64}x"'),
        ("docs/quality/roadmap-freeze.json", f'"unknown": "{"a" * 64}"'),
        (
            "docs/quality/roadmap-freeze.json",
            f'"provider_token": "ghp_{"a" * 60}"',
        ),
        (
            "docs/quality/roadmap-freeze.json",
            '"private_key": "-----BEGIN '
            + "PRIVATE KEY----- synthetic-fixture -----END "
            + 'PRIVATE KEY-----"',
        ),
    ),
)
def test_source_identity_filter_never_hides_other_paths_or_secret_shapes(
    filename: str, line: str
) -> None:
    assert not FILTER.is_quality_evidence_hash(filename, line)


@pytest.mark.parametrize(
    "filename",
    (
        "docs/quality/static-baseline.json",
        "other/docs/quality/architecture-ratchet.json",
        "/docs/quality/architecture-ratchet.json",
        "C:\\docs\\quality\\architecture-ratchet.json",
        "docs/quality/../quality/architecture-ratchet.json",
        "./docs/quality/architecture-ratchet.json",
    ),
)
def test_noncanonical_paths_are_not_filtered(filename: str) -> None:
    assert not FILTER.is_quality_evidence_hash(filename, f'"sha256": "{"a" * 64}"')


@pytest.mark.parametrize(
    "line",
    (
        '"' + UNRECOGNIZED_MEMBER + '": "' + "a" * 64 + '"',
        '"token": "' + "a" * 40 + '"',
        '"SHA256": "' + "a" * 64 + '"',
        '"sha256": "' + "A" * 64 + '"',
        '"sha256": "' + "a" * 63 + '"',
        '"sha256": "' + "a" * 64 + 'x"',
        'prefix "sha256": "' + "a" * 64 + '"',
        '"sha256": "' + "a" * 64 + '" suffix',
        '"sha256": "' + "a" * 64 + '", "' + UNRECOGNIZED_MEMBER + '": "secret"',
        '"sha256": "' + "a" * 64 + '" // generated',
        '"sha256" = "' + "a" * 64 + '" suffix',
        '"' + UNRECOGNIZED_MEMBER + '" = "' + "a" * 64 + '"',
    ),
)
def test_only_complete_exact_json_members_are_filtered(line: str) -> None:
    assert not FILTER.is_quality_evidence_hash("docs/quality/architecture-ratchet.json", line)


def _run_detect_secrets(
    tmp_path: Path, *filenames: str, filtered: bool
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-c",
        "from detect_secrets.pre_commit_hook import main; raise SystemExit(main())",
        "--no-verify",
    ]
    if filtered:
        command.extend(("-f", f"file://{FILTER_PATH}::is_quality_evidence_hash"))
    command.extend(filenames)
    return subprocess.run(command, cwd=tmp_path, text=True, capture_output=True, check=False)


def _json_document(member: str) -> str:
    return "{\n" + member + "\n}\n"


@pytest.mark.parametrize(
    "canonical_path",
    [
        "docs/quality/architecture-ratchet.json",
        "docs/quality/roadmap-freeze.json",
        "config/quality_roadmap_claims.json",
        "config/quality_roadmap_owners.json",
    ],
)
def test_detect_secrets_filter_drops_only_known_metadata_hashes(
    tmp_path: Path,
    canonical_path: str,
) -> None:
    known = tmp_path / canonical_path
    known.parent.mkdir(parents=True)
    digest = hashlib.sha256(b"public synthetic scanner fixture").hexdigest()
    known.write_text(_json_document(f'  "sha256": "{digest}"'), encoding="utf-8")
    unrelated = tmp_path / "other" / "docs" / "quality" / "architecture-ratchet.json"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text(_json_document(f'  "sha256": "{digest}"'), encoding="utf-8")
    synthetic_provider_value = "ghp" + "_" + digest[:36]
    private_marker = "-----BEGIN " + "PRIVATE " + "KEY-----"
    private_end_marker = "-----END " + "PRIVATE " + "KEY-----"

    assert _run_detect_secrets(tmp_path, canonical_path, filtered=False).returncode == 1
    assert _run_detect_secrets(tmp_path, canonical_path, filtered=True).returncode == 0
    for hostile_payload in (
        f'"{UNRECOGNIZED_MEMBER}": "{digest}"',
        f'"note": "{synthetic_provider_value}"',
        f'"private_key": "{private_marker} synthetic-fixture {private_end_marker}"',
    ):
        known.write_text(
            _json_document(f'  "sha256": "{digest}",\n  {hostile_payload}'),
            encoding="utf-8",
        )
        assert _run_detect_secrets(tmp_path, canonical_path, filtered=True).returncode == 1

    assert (
        _run_detect_secrets(
            tmp_path, "other/docs/quality/architecture-ratchet.json", filtered=True
        ).returncode
        == 1
    )
