"""Regression tests for the narrow quality-evidence detect-secrets filter."""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from typing import Protocol, cast
from urllib.parse import urlparse

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
    "docs/quality/portfolio-coverage-growth-20261023.json",
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
        relative_filter = FILTER_PATH.relative_to(ROOT)
        local_filter = tmp_path / relative_filter
        local_filter.parent.mkdir(parents=True, exist_ok=True)
        local_filter.write_bytes(FILTER_PATH.read_bytes())
        command.extend(("-f", f"file://{relative_filter.as_posix()}::is_quality_evidence_hash"))
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
        "docs/quality/pipeline-completion-exchange-review-20261023.json",
        "docs/quality/pipeline-completion-bulk-review-20261023.json",
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


MELI_SOURCE_FIXTURES = (
    "tests/fixtures/meli_reported_tables/provenance.json",
    "tests/fixtures/meli_reported_statements/provenance.json",
)
SCANNER_FIELD_NAME = "api_" + "key"


@pytest.mark.parametrize("key", ("source_sha256", "snippet_sha256", "sha256"))
@pytest.mark.parametrize("delimiter", (":", "="))
@pytest.mark.parametrize("filename", MELI_SOURCE_FIXTURES)
def test_meli_public_source_fixture_accepts_only_exact_digest_members(
    key: str, delimiter: str, filename: str
) -> None:
    digest = hashlib.sha256(b"public SEC filing evidence").hexdigest()
    assert FILTER.is_quality_evidence_hash(filename, f'  "{key}" {delimiter} "{digest}",')


@pytest.mark.parametrize(
    ("path_prefix", "line"),
    (
        ("other/", f'"sha256": "{"a" * 64}"'),
        ("./", f'"sha256": "{"a" * 64}"'),
        ("", f'"source_manifest_sha256": "{"a" * 64}"'),
        ("", f'"sha256": "{"A" * 64}"'),
        ("", f'"sha256": "{"a" * 63}"'),
        ("", f'"sha256": "{"a" * 64}x"'),
        ("", f'"{SCANNER_FIELD_NAME}": "ghp_{"a" * 36}"'),
        (
            "",
            '"private_key": "-----BEGIN '
            "PRIVATE KEY----- synthetic-fixture -----END "
            'PRIVATE KEY-----"',
        ),
        ("", f'"sha256": "{"a" * 64}", "token": "secret"'),
    ),
)
@pytest.mark.parametrize("filename", MELI_SOURCE_FIXTURES)
def test_meli_public_source_fixture_rejects_other_paths_and_secret_shapes(
    filename: str, path_prefix: str, line: str
) -> None:
    assert not FILTER.is_quality_evidence_hash(path_prefix + filename, line)


@pytest.mark.parametrize("filename", MELI_SOURCE_FIXTURES)
def test_detect_secrets_scans_meli_fixture_without_masking_actual_secrets(
    tmp_path: Path,
    filename: str,
) -> None:
    fixture = tmp_path / filename
    fixture.parent.mkdir(parents=True)
    original = (ROOT / filename).read_text(encoding="utf-8")
    fixture.write_text(original, encoding="utf-8")
    assert _run_detect_secrets(tmp_path, filename, filtered=False).returncode == 1
    assert _run_detect_secrets(tmp_path, filename, filtered=True).returncode == 0
    with_secret = json.loads(original)
    with_secret[SCANNER_FIELD_NAME] = "ghp_" + "a" * 36
    fixture.write_text(json.dumps(with_secret, indent=2) + "\n", encoding="utf-8")
    assert _run_detect_secrets(tmp_path, filename, filtered=True).returncode == 1


@pytest.mark.parametrize(
    ("case", "expected_returncode"),
    (("annotated", 0), ("without_annotation", 1), ("extra_credential", 1)),
)
def test_native_public_digest_annotation_keeps_other_credentials_detectable(
    tmp_path: Path, case: str, expected_returncode: int
) -> None:
    filename = "tests/test_meli_reported_statements.py"
    source = (ROOT / filename).read_text(encoding="utf-8")
    annotation = "  # pragma: allowlist secret"
    assert source.count(annotation) == 1
    if case == "without_annotation":
        source = source.replace(annotation, "")
    elif case == "extra_credential":
        source += f'\n{SCANNER_FIELD_NAME} = "ghp_{"a" * 36}"\n'
    fixture = tmp_path / filename
    fixture.parent.mkdir(parents=True)
    fixture.write_text(source, encoding="utf-8")
    returncode = _run_detect_secrets(tmp_path, filename, filtered=True).returncode
    assert returncode == expected_returncode


INVESTING_REVIEW_BASELINE = (
    "src/advisor/skills/earnings-summary-investing/references/reviewed-sources.json"
)


@pytest.mark.parametrize("delimiter", (":", "="))
def test_investing_review_accepts_only_complete_lowercase_sha256_members(delimiter: str) -> None:
    digest = hashlib.sha256(b"public investing workflow source").hexdigest()
    assert FILTER.is_quality_evidence_hash(
        INVESTING_REVIEW_BASELINE, f'  "sha256" {delimiter} "{digest}",\n'
    )


@pytest.mark.parametrize(
    ("filename", "line"),
    (
        ("other/" + INVESTING_REVIEW_BASELINE, f'"sha256": "{"a" * 64}"'),
        ("./" + INVESTING_REVIEW_BASELINE, f'"sha256": "{"a" * 64}"'),
        (INVESTING_REVIEW_BASELINE, f'"source_sha256": "{"a" * 64}"'),
        (INVESTING_REVIEW_BASELINE, f'"SHA256": "{"a" * 64}"'),
        (INVESTING_REVIEW_BASELINE, f'"sha256": "{"A" * 64}"'),
        (INVESTING_REVIEW_BASELINE, f'"sha256": "{"a" * 63}"'),
        (INVESTING_REVIEW_BASELINE, f'"sha256": "{"a" * 65}"'),
        (INVESTING_REVIEW_BASELINE, f'"sha256": "{"a" * 64}", "token": "secret"'),
        (INVESTING_REVIEW_BASELINE, f'"sha256": "{"a" * 64}" // generated'),
        (INVESTING_REVIEW_BASELINE, f'"{SCANNER_FIELD_NAME}": "ghp_{"a" * 36}"'),
    ),
)
def test_investing_review_rejects_other_paths_fields_and_secret_shapes(
    filename: str, line: str
) -> None:
    assert not FILTER.is_quality_evidence_hash(filename, line)


def test_detect_secrets_scans_investing_baseline_without_masking_other_fields(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / INVESTING_REVIEW_BASELINE
    baseline.parent.mkdir(parents=True)
    original = (ROOT / INVESTING_REVIEW_BASELINE).read_text(encoding="utf-8")
    baseline.write_text(original, encoding="utf-8")
    assert _run_detect_secrets(tmp_path, INVESTING_REVIEW_BASELINE, filtered=False).returncode == 1
    assert _run_detect_secrets(tmp_path, INVESTING_REVIEW_BASELINE, filtered=True).returncode == 0

    digest = hashlib.sha256(b"public synthetic investing scanner fixture").hexdigest()
    for key, value in (
        (SCANNER_FIELD_NAME, "ghp_" + digest[:36]),
        (UNRECOGNIZED_MEMBER, digest),
    ):
        payload = json.loads(original)
        payload[key] = value
        baseline.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        assert (
            _run_detect_secrets(tmp_path, INVESTING_REVIEW_BASELINE, filtered=True).returncode == 1
        )

    unrelated = "other/" + INVESTING_REVIEW_BASELINE
    other = tmp_path / unrelated
    other.parent.mkdir(parents=True)
    other.write_text(original, encoding="utf-8")
    assert _run_detect_secrets(tmp_path, unrelated, filtered=True).returncode == 1


CANONICAL_HELPERS_BASELINE = "docs/quality/canonical-helpers.json"
GROUNDED_SEARCH_ORACLE = "tests/test_grounded_search_foundation.py"


@pytest.mark.parametrize("key", ("generated_commit", "source_commit", "capture_commit"))
@pytest.mark.parametrize("delimiter", (":", "="))
def test_reconciliation_hashes_are_path_and_schema_specific(key: str, delimiter: str) -> None:
    digest = hashlib.sha1(b"public reconciliation commit", usedforsecurity=False).hexdigest()
    line = f'  "{key}" {delimiter} "{digest}",\n'
    assert FILTER.is_quality_evidence_hash(CANONICAL_HELPERS_BASELINE, line)
    for filename in (
        "docs/quality/architecture-ratchet.json",
        "other/" + CANONICAL_HELPERS_BASELINE,
        "./" + CANONICAL_HELPERS_BASELINE,
    ):
        assert not FILTER.is_quality_evidence_hash(filename, line)
    assert not FILTER.is_quality_evidence_hash(
        CANONICAL_HELPERS_BASELINE, f'"sha256": "{"a" * 64}"'
    )


def test_reconciliation_oracle_accepts_only_its_complete_named_assignment() -> None:
    digest = hashlib.sha256(b"public historical Ask bundle oracle").hexdigest()
    assignment = f'    expected_bundle_sha256 = "{digest}"\n'
    assert FILTER.is_quality_evidence_hash(GROUNDED_SEARCH_ORACLE, assignment)
    for filename in (
        "tests/test_other.py",
        "other/" + GROUNDED_SEARCH_ORACLE,
        "./" + GROUNDED_SEARCH_ORACLE,
    ):
        assert not FILTER.is_quality_evidence_hash(filename, assignment)
    for line in (
        assignment.replace("expected_bundle_sha256", "other_bundle_sha256"),
        assignment.replace(digest, digest.upper()),
        assignment.replace(digest, digest[:-1]),
        assignment.rstrip() + "; token = 'secret'",
        assignment.rstrip() + " # fixture",
        '"source_commit": "' + "a" * 40 + '"',
    ):
        assert not FILTER.is_quality_evidence_hash(GROUNDED_SEARCH_ORACLE, line)


@pytest.mark.parametrize("filename", (CANONICAL_HELPERS_BASELINE, GROUNDED_SEARCH_ORACLE))
def test_actual_detector_preserves_adjacent_secrets_at_reconciliation_paths(
    tmp_path: Path, filename: str
) -> None:
    digest = hashlib.sha256(b"public deterministic reconciliation scanner fixture").hexdigest()
    if filename == CANONICAL_HELPERS_BASELINE:
        original = _json_document(
            f'  "generated_commit": "{digest[:40]}",\n  "source_commit": "{digest[-40:]}"'
        )
        hostile = original.replace("\n}", f',\n  "{SCANNER_FIELD_NAME}": "ghp_{digest[:36]}"\n}}')
    else:
        original = f'expected_bundle_sha256 = "{digest}"\n'
        hostile = original + f'{SCANNER_FIELD_NAME} = "ghp_{digest[:36]}"\n'
    fixture = tmp_path / filename
    fixture.parent.mkdir(parents=True)
    fixture.write_text(original, encoding="utf-8")
    assert _run_detect_secrets(tmp_path, filename, filtered=False).returncode == 1
    assert _run_detect_secrets(tmp_path, filename, filtered=True).returncode == 0
    fixture.write_text(hostile, encoding="utf-8")
    assert _run_detect_secrets(tmp_path, filename, filtered=True).returncode == 1
    unrelated = "other/" + filename
    other = tmp_path / unrelated
    other.parent.mkdir(parents=True)
    other.write_text(original, encoding="utf-8")
    assert _run_detect_secrets(tmp_path, unrelated, filtered=True).returncode == 1


FILING_XBRL_HASH_KEYS = {
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


@pytest.mark.parametrize(
    ("filename", "key", "length"),
    [
        (filename, key, length)
        for filename, keys in FILING_XBRL_HASH_KEYS.items()
        for key, length in keys.items()
    ],
)
@pytest.mark.parametrize("delimiter", (":", "="))
def test_filing_xbrl_hashes_require_exact_path_key_and_value(
    filename: str, key: str, length: int, delimiter: str
) -> None:
    digest = hashlib.sha256(b"public filing XBRL scanner fixture").hexdigest()[:length]
    line = f'  "{key}" {delimiter} "{digest}",\n'
    assert FILTER.is_quality_evidence_hash(filename, line)
    for prefix in ("other/", "./", "/", "docs/../"):
        assert not FILTER.is_quality_evidence_hash(prefix + filename, line)
    for other, keys in FILING_XBRL_HASH_KEYS.items():
        if key not in keys:
            assert not FILTER.is_quality_evidence_hash(other, line)
    for value in (digest.upper(), digest[:-1], digest + "a", "g" + digest[1:]):
        assert not FILTER.is_quality_evidence_hash(filename, f'"{key}" {delimiter} "{value}"')
    for malformed in (
        line.replace(key, "unknown_sha256"),
        line.replace(key, "execution/other.py"),
        line.replace(key, SCANNER_FIELD_NAME),
        line.replace(digest, "ghp_" + digest[:36]),
        "prefix " + line,
        line.rstrip() + ' "token": "secret"',
        line.rstrip() + " // generated",
    ):
        assert not FILTER.is_quality_evidence_hash(filename, malformed)


@pytest.mark.parametrize("filename", FILING_XBRL_HASH_KEYS)
def test_actual_detector_keeps_secrets_visible_in_filing_xbrl_evidence(
    tmp_path: Path, filename: str
) -> None:
    original = (ROOT / filename).read_text(encoding="utf-8")
    fixture = tmp_path / filename
    fixture.parent.mkdir(parents=True)
    fixture.write_text(original, encoding="utf-8")
    assert _run_detect_secrets(tmp_path, filename, filtered=False).returncode == 1
    assert _run_detect_secrets(tmp_path, filename, filtered=True).returncode == 0
    digest = hashlib.sha256(b"public adjacent XBRL scanner fixture").hexdigest()
    for key, value in (
        (SCANNER_FIELD_NAME, "ghp_" + digest[:36]),
        (UNRECOGNIZED_MEMBER, digest),
        ("private_key", "-----BEGIN " + "PRIVATE KEY----- synthetic-fixture"),
    ):
        payload = json.loads(original)
        payload[key] = value
        fixture.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        assert _run_detect_secrets(tmp_path, filename, filtered=True).returncode == 1


def test_detect_secrets_helper_uses_exact_filter_bytes_and_drive_free_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    def capture(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", capture)
    _run_detect_secrets(tmp_path, "docs/quality/architecture-ratchet.json", filtered=True)
    reference = commands[0][commands[0].index("-f") + 1]
    filename, function = reference.removeprefix("file://").split("::")
    assert function == "is_quality_evidence_hash"
    assert filename == FILTER_PATH.relative_to(ROOT).as_posix()
    assert not Path(filename).is_absolute()
    assert not PureWindowsPath(filename).drive
    assert not PureWindowsPath(filename).is_absolute()
    assert len(urlparse(reference).path.split("::")) == 2
    assert (tmp_path / filename).read_bytes() == FILTER_PATH.read_bytes()
    # Detector1.5 validates the URL path before it imports the file. A drive
    # colon in the authority consumes the function separator instead.
    absolute_windows = f"file://{PureWindowsPath('C:/repo/filter.py')}::is_quality_evidence_hash"
    assert len(urlparse(absolute_windows).path.split("::")) != 2


PDF_IDENTITY_GROWTH = "docs/quality/pdf-6212-architecture-growth-20261023.json"
PDF_IDENTITY_HASH_KEYS = {
    "scoped_commit": 40,
    "baseline_commit": 40,
    "current_source_sha256": 64,
    "baseline_source_sha256": 64,
}


@pytest.mark.parametrize(("key", "length"), PDF_IDENTITY_HASH_KEYS.items())
@pytest.mark.parametrize("delimiter", (":", "="))
def test_pdf_identity_growth_accepts_only_exact_digest_members(
    key: str, length: int, delimiter: str
) -> None:
    digest = hashlib.sha256(b"public PDF identity quality fixture").hexdigest()[:length]
    line = f'  "{key}" {delimiter} "{digest}",\n'
    assert FILTER.is_quality_evidence_hash(PDF_IDENTITY_GROWTH, line)
    for path in ("other/" + PDF_IDENTITY_GROWTH, "./" + PDF_IDENTITY_GROWTH):
        assert not FILTER.is_quality_evidence_hash(path, line)
    for malformed in (
        line.replace(key, "unrecognized_hash"),
        line.replace(key, SCANNER_FIELD_NAME),
        line.replace(digest, digest.upper()),
        line.replace(digest, digest[:-1]),
        line.rstrip() + ' "token": "secret"',
    ):
        assert not FILTER.is_quality_evidence_hash(PDF_IDENTITY_GROWTH, malformed)


def test_actual_detector_preserves_secrets_adjacent_to_pdf_identity_hashes(tmp_path: Path) -> None:
    fixture = tmp_path / PDF_IDENTITY_GROWTH
    fixture.parent.mkdir(parents=True)
    original = (ROOT / PDF_IDENTITY_GROWTH).read_text(encoding="utf-8")
    fixture.write_text(original, encoding="utf-8")
    assert _run_detect_secrets(tmp_path, PDF_IDENTITY_GROWTH, filtered=False).returncode == 1
    assert _run_detect_secrets(tmp_path, PDF_IDENTITY_GROWTH, filtered=True).returncode == 0
    digest = hashlib.sha256(b"public adjacent PDF scanner fixture").hexdigest()
    for key, value in ((SCANNER_FIELD_NAME, "ghp_" + digest[:36]), (UNRECOGNIZED_MEMBER, digest)):
        payload = json.loads(original)
        payload[key] = value
        fixture.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        assert _run_detect_secrets(tmp_path, PDF_IDENTITY_GROWTH, filtered=True).returncode == 1
