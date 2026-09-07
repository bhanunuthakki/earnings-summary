"""Deterministic quality-score oracle over immutable Git evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, Field, ValidationError, model_validator

from quality.admission_policy import (
    RUNTIME_POLICY_SHA256 as _RUNTIME_POLICY_SHA256,
)
from quality.admission_policy import (
    SOURCE_PATHS as _SOURCE_PATHS,
)
from quality.admission_policy import (
    ParsedSource as _ParsedSource,
)
from quality.admission_policy import (
    SourceName as _SourceName,
)
from quality.admission_policy import (
    evaluate_slot as _evaluate_slot,
)
from quality.admission_policy import (
    parse_source as _parse_source,
)
from quality.admission_policy import (
    required_paths as _required_paths,
)
from quality.admission_policy import (
    verify_registry as _verify_registry,
)
from quality.architecture import (
    COMPOSITION_ROOTS,
    ArchitectureMetrics,
    ArchitectureReceipt,
    StrictModel,
    architecture_regressions,
    build_architecture_receipt,
    compare_architecture,
)
from quality.evidence_path_policy import (
    admission_path_for as _admission_path_for,
)
from quality.evidence_path_policy import (
    is_canonical_generator_path as _is_canonical_generator_path,
)
from quality.evidence_path_policy import (
    is_canonical_receipt_path as _is_canonical_receipt_path,
)
from quality.git_env import clean_local_git_env

SCORE_RESULT_SCHEMA = "quality-score-result-v1"
SCORE_EVIDENCE_SCHEMA = "quality-score-evidence-v1"
ADMISSION_SCHEMA = "quality-score-admission/v1"
ADMISSION_GENERATOR_PATH = "src/quality/admission_policy.py"
DECLARATIVE_EXCEPTION_CAP = 3
DIAGNOSTIC_ITEM_CAP = 20
DIAGNOSTIC_TEXT_CAP = 200
_HEX40 = r"^[0-9a-f]{40}$"
_HEX64 = r"^[0-9a-f]{64}$"

EvidenceState = Literal["pass", "fail", "missing"]


class EvidenceEntry(StrictModel):
    receipt_path: str
    sha256: str = Field(pattern=_HEX64)
    bundle_commit: str = Field(pattern=_HEX40)

    @model_validator(mode="after")
    def _check_receipt(self) -> EvidenceEntry:
        if not _is_canonical_receipt_path(self.receipt_path):
            raise ValueError("noncanonical receipt path")
        object.__setattr__(self, "sha256", self.sha256.lower())
        object.__setattr__(self, "bundle_commit", self.bundle_commit.lower())
        return self


class AdmissionSource(StrictModel):
    path: str
    sha256: str = Field(pattern=_HEX64)
    schema_version: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _check_source(self) -> AdmissionSource:
        if not _is_canonical_receipt_path(self.path):
            raise ValueError("noncanonical source path")
        object.__setattr__(self, "sha256", self.sha256.lower())
        return self


class AdmissionReceipt(StrictModel):
    schema_version: Literal["quality-score-admission/v1"] = ADMISSION_SCHEMA
    kind: Literal["block", "hard_gate"]
    key: str = Field(min_length=1)
    state: Literal["pass", "fail"]
    subject_commit: str = Field(pattern=_HEX40)
    generator_path: str
    generator_sha256: str = Field(pattern=_HEX64)
    sources: tuple[AdmissionSource, ...] = Field(min_length=0)

    @model_validator(mode="after")
    def _check_admission(self) -> AdmissionReceipt:
        if not _is_canonical_generator_path(self.generator_path):
            raise ValueError("noncanonical generator path")
        paths = [s.path for s in self.sources]
        if len(set(paths)) != len(paths):
            raise ValueError("duplicate source paths")
        if self.state == "pass" and len(self.sources) == 0:
            raise ValueError("pass requires nonempty sources")
        object.__setattr__(self, "subject_commit", self.subject_commit.lower())
        object.__setattr__(self, "generator_sha256", self.generator_sha256.lower())
        return self


class ScoreEvidence(StrictModel):
    schema_version: Literal["quality-score-evidence-v1"]
    scoped_commit: str = Field(pattern=_HEX40)
    blocks: dict[str, EvidenceEntry] = Field(default_factory=dict, max_length=64)
    hard_gates: dict[str, EvidenceEntry] = Field(default_factory=dict, max_length=32)

    @model_validator(mode="after")
    def _bound_keys(self) -> ScoreEvidence:
        if any(len(key) > 100 for key in (*self.blocks, *self.hard_gates)):
            raise ValueError("evidence keys must be at most 100 characters")
        return self


class ScoreBlock(StrictModel):
    key: str
    label: str
    points: int = Field(gt=0)
    state: EvidenceState
    awarded: int = Field(ge=0)
    reason: str = Field(max_length=DIAGNOSTIC_TEXT_CAP)


class QualityScoreReceipt(StrictModel):
    schema_version: Literal["quality-score-result-v1"] = SCORE_RESULT_SCHEMA
    scoped_commit: str
    score_points: int = Field(ge=0, le=100)
    score_out_of_ten: str
    verdict: Literal["PASS", "FAIL", "HOLD"]
    blocks: tuple[ScoreBlock, ...]
    hard_gate_failures: tuple[str, ...]
    hard_gate_missing: tuple[str, ...]
    architecture_regressions: tuple[str, ...]
    architecture: ArchitectureReceipt

    @model_validator(mode="after")
    def points_match_blocks(self) -> QualityScoreReceipt:
        expected = list(SCORE_BLOCKS)
        if len(self.blocks) != len(expected):
            raise ValueError("score receipt block count mismatch")
        for got, (key, _label, points) in zip(self.blocks, expected, strict=True):
            if got.key != key or got.points != points:
                raise ValueError("score receipt registry key/order/weight mismatch")
        if sum(block.points for block in self.blocks) != 100:
            raise ValueError("score block weights must total exactly 100")
        if sum(block.awarded for block in self.blocks) != self.score_points:
            raise ValueError("awarded block points do not match score_points")
        return self


SCORE_BLOCKS: tuple[tuple[str, str, int], ...] = (
    ("elegance.cycles", "Elegance: cycles", 8),
    ("elegance.composition_roots", "Elegance: composition roots", 6),
    ("elegance.module_shape", "Elegance: module shape", 6),
    ("elegance.cohesive_typed_facades", "Elegance: cohesive typed facades", 5),
    ("maintainability.static_quality", "Maintainability: static quality", 8),
    ("maintainability.duplication", "Maintainability: duplication", 6),
    ("maintainability.authorities", "Maintainability: authorities", 5),
    ("maintainability.sustainable_tests", "Maintainability: sustainable tests", 3),
    ("maintainability.enforced_ratchets", "Maintainability: enforced ratchets", 3),
    ("efficiency.integrity_audit", "Efficiency: integrity audit", 10),
    ("efficiency.request_path", "Efficiency: request path", 6),
    ("efficiency.test_ci", "Efficiency: test/CI", 6),
    ("efficiency.dcf_disposition", "Efficiency: DCF disposition", 3),
    ("cleanup.lifecycle_inventory", "Cleanup: lifecycle inventory", 8),
    ("cleanup.reachability_oracle", "Cleanup: reachability oracle", 6),
    ("cleanup.deletion_proof", "Cleanup: deletion proof", 5),
    ("cleanup.schema_ownership", "Cleanup: schema ownership", 3),
    ("cleanup.reconstructability", "Cleanup: reconstructability", 3),
)
HARD_GATES: tuple[str, ...] = (
    "repository_gates",
    "active_static_zero",
    "compatibility_parity",
    "benchmark_contract",
    "database_authority",
    "deletion_evidence",
    "network_consolidation_safety",
    "owner_acceptance",
    "architecture_duplication_ratchets",
    "touched_reachability_closure",
)


def validate_registry() -> None:
    block_keys = [key for key, _label, _points in SCORE_BLOCKS]
    if len(SCORE_BLOCKS) != 18 or len(set(block_keys)) != 18:
        raise ValueError("registry must hold exactly 18 unique score blocks")
    if sum(points for _k, _l, points in SCORE_BLOCKS) != 100:
        raise ValueError("registry block weights must total exactly 100")
    if len(HARD_GATES) != 10 or len(set(HARD_GATES)) != 10:
        raise ValueError("registry must hold exactly 10 unique hard gates")
    if set(block_keys) & set(HARD_GATES):
        raise ValueError("registry block and gate keys must not overlap")


def _architecture_states(metrics: ArchitectureMetrics) -> dict[str, tuple[EvidenceState, str]]:
    cycle_ok = metrics.scc_count <= 3 and metrics.largest_scc <= 4
    composition_ok = metrics.max_internal_fan_out <= 25 and all(
        0 <= metrics.composition_root_loc.get(path, -1) <= limit
        for path, limit in COMPOSITION_ROOTS.items()
    )
    module_shape_ok = (
        metrics.modules_over_1000_loc <= 35
        and metrics.modules_at_least_3000_loc <= DECLARATIVE_EXCEPTION_CAP
    )
    facade_ok = not metrics.facade_violations
    return {
        "elegance.cycles": ("pass" if cycle_ok else "fail", "frozen SCC caps"),
        "elegance.composition_roots": (
            "pass" if composition_ok else "fail",
            "fan-out and composition-root LOC caps",
        ),
        "elegance.module_shape": ("pass" if module_shape_ok else "fail", "large-module caps"),
        "elegance.cohesive_typed_facades": (
            "pass" if facade_ok else "fail",
            "facade responsibility and annotation checks",
        ),
    }


def _bound(text: str, limit: int = DIAGNOSTIC_TEXT_CAP) -> str:
    return text[:limit]


def _bound_diagnostics(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    bounded = tuple(_bound(value) for value in values[:DIAGNOSTIC_ITEM_CAP])
    if len(values) <= DIAGNOSTIC_ITEM_CAP:
        return bounded
    summary = f"{len(values) - DIAGNOSTIC_ITEM_CAP + 1} additional diagnostics omitted"
    return (*bounded[:-1], summary)


def _reject_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate JSON key: {k}")
        out[k] = v
    return out


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=False,
        capture_output=True,
        env=clean_local_git_env(),
    )


def _resolve_exact_commit(repo_root: Path, value: str) -> str | None:
    proc = _git(repo_root, "rev-parse", "--verify", f"{value}^{{commit}}")
    if proc.returncode != 0:
        return None
    try:
        resolved = proc.stdout.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    return resolved if resolved == value.lower() else None


def _is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    return _git(repo_root, "merge-base", "--is-ancestor", ancestor, descendant).returncode == 0


def _evidence_only_diff(repo_root: Path, subject: str, bundle: str) -> tuple[str, ...] | None:
    proc = _git(
        repo_root,
        "diff",
        "--name-only",
        "--no-renames",
        "-z",
        f"{subject}..{bundle}",
        "--",
    )
    if proc.returncode != 0:
        return None
    if not proc.stdout:
        return ()
    if not proc.stdout.endswith(b"\0"):
        return None
    try:
        paths = tuple(path.decode("utf-8") for path in proc.stdout[:-1].split(b"\0"))
    except UnicodeDecodeError:
        return None
    if any(not path for path in paths) or len(set(paths)) != len(paths):
        return None
    return paths


def _git_show(repo_root: Path, revision: str, rel_path: str) -> bytes | None:
    if not (_is_canonical_receipt_path(rel_path) or _is_canonical_generator_path(rel_path)):
        return None
    proc = _git(repo_root, "show", f"{revision}:{rel_path}")
    return proc.stdout if proc.returncode == 0 else None


def _rebuild_claimed_architecture(
    repo_root: Path, claimed: ArchitectureReceipt
) -> tuple[ArchitectureReceipt | None, bool]:
    rebuilt: ArchitectureReceipt | None = None
    try:
        exact_commit = _resolve_exact_commit(repo_root, claimed.scoped_commit)
        if exact_commit is not None:
            rebuilt = build_architecture_receipt(repo_root, exact_commit)
    except (OSError, RuntimeError, ValueError):
        rebuilt = None
    matches = rebuilt is not None and (
        rebuilt.source_sha256 == claimed.source_sha256
        and rebuilt.metrics == claimed.metrics
        and rebuilt.scanner_sha256 == claimed.scanner_sha256
    )
    return rebuilt, matches


class _AdmissionVerifier:
    """Request-scoped cache for admission verification."""

    def __init__(self, repo_root: Path) -> None:
        self._repo_root = repo_root
        self._subjects: dict[str, str | None] = {}
        self._subject_errors: dict[str, str] = {}
        self._origin_done = False
        self._origin: str | None = None
        self._origin_error: str | None = None
        self._bundle_diffs: dict[tuple[str, str, str], tuple[str, ...]] = {}
        self._bundle_errors: dict[tuple[str, str, str], str] = {}
        self._blobs: dict[tuple[str, str], bytes | None] = {}

    def _subject_commit(self, scoped_commit: str) -> tuple[str | None, str | None]:
        if scoped_commit not in self._subjects:
            resolved = _resolve_exact_commit(self._repo_root, scoped_commit)
            self._subjects[scoped_commit] = resolved
            if resolved is None:
                self._subject_errors[scoped_commit] = "subject commit does not resolve exactly"
        err = self._subject_errors.get(scoped_commit)
        return self._subjects[scoped_commit], err

    def _origin_commit(self) -> tuple[str | None, str | None]:
        if not self._origin_done:
            self._origin_done = True
            proc = _git(
                self._repo_root,
                "rev-parse",
                "--verify",
                "refs/remotes/origin/main^{commit}",
            )
            if proc.returncode != 0:
                self._origin_error = "trusted origin/main is missing"
            else:
                try:
                    self._origin = proc.stdout.decode("utf-8").strip()
                except UnicodeDecodeError:
                    self._origin = None
                    self._origin_error = "trusted origin/main is unreadable"
        if self._origin is None:
            return None, self._origin_error or "trusted origin/main is missing"
        return self._origin, None

    def _bundle_diff(
        self, bundle_hex: str, subject: str, origin: str
    ) -> tuple[tuple[str, ...] | None, str | None]:
        key = (bundle_hex, subject, origin)
        if key in self._bundle_errors:
            return None, self._bundle_errors[key]
        if key in self._bundle_diffs:
            return self._bundle_diffs[key], None
        bundle = _resolve_exact_commit(self._repo_root, bundle_hex)
        if bundle is None:
            self._bundle_errors[key] = "bundle commit does not resolve exactly"
            return None, self._bundle_errors[key]
        if not _is_ancestor(self._repo_root, bundle, origin):
            self._bundle_errors[key] = "bundle is not an ancestor of origin/main"
            return None, self._bundle_errors[key]
        if not _is_ancestor(self._repo_root, subject, bundle):
            self._bundle_errors[key] = "subject is not an ancestor of bundle"
            return None, self._bundle_errors[key]
        changed = _evidence_only_diff(self._repo_root, subject, bundle)
        if changed is None:
            self._bundle_errors[key] = "evidence-only diff is unverifiable"
            return None, self._bundle_errors[key]
        if any(not _is_canonical_receipt_path(path) for path in changed):
            self._bundle_errors[key] = "bundle contains non-evidence files"
            return None, self._bundle_errors[key]
        self._bundle_diffs[key] = changed
        return changed, None

    def _show(self, revision: str, rel_path: str) -> bytes | None:
        key = (revision, rel_path)
        if key not in self._blobs:
            self._blobs[key] = _git_show(self._repo_root, revision, rel_path)
        return self._blobs[key]

    def verify(
        self,
        entry: EvidenceEntry,
        expected_kind: Literal["block", "hard_gate"],
        expected_key: str,
        scoped_commit: str,
    ) -> tuple[Literal["pass", "fail"] | None, str]:
        try:
            if not _verify_registry():
                return None, "admission registry mismatch"
            subject, subject_err = self._subject_commit(scoped_commit)
            if subject is None:
                return None, subject_err or "subject commit does not resolve exactly"
            origin, origin_err = self._origin_commit()
            if origin is None:
                return None, origin_err or "trusted origin/main is missing"
            changed, bundle_err = self._bundle_diff(entry.bundle_commit, subject, origin)
            if changed is None:
                if bundle_err == "bundle commit does not resolve exactly":
                    return None, bundle_err
                return None, bundle_err or "evidence-only diff is unverifiable"
            expected_path = _admission_path_for(expected_kind, expected_key)
            if entry.receipt_path != expected_path or entry.receipt_path not in changed:
                return None, "admission receipt path mismatch"
            raw = self._show(entry.bundle_commit.lower(), entry.receipt_path)
            if raw is None:
                return None, "admission receipt is missing in bundle"
            if hashlib.sha256(raw).hexdigest() != entry.sha256:
                return None, "admission receipt SHA-256 mismatch"
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return None, "admission receipt schema is invalid"
            try:
                payload: object = json.loads(text, object_pairs_hook=_reject_pairs)
            except ValueError:
                return None, "admission receipt schema is invalid"
            if not isinstance(payload, dict):
                return None, "admission receipt schema is invalid"
            payload = cast("dict[str, object]", payload)
            try:
                admission = AdmissionReceipt.model_validate(payload)
            except (UnicodeDecodeError, ValueError, ValidationError):
                return None, "admission receipt schema is invalid"
            if admission.kind != expected_kind:
                return None, "admission kind mismatch"
            if admission.key != expected_key:
                return None, "admission key mismatch"
            if admission.subject_commit != subject:
                return None, "admission subject mismatch"
            if admission.generator_path != ADMISSION_GENERATOR_PATH:
                return None, "admission generator is not the registered oracle"
            generator = self._show(subject, admission.generator_path)
            if generator is None:
                return None, "generator is missing at subject"
            _subject_gen = hashlib.sha256(generator).hexdigest()
            if _subject_gen != admission.generator_sha256:
                return None, "generator hash mismatch"
            if _RUNTIME_POLICY_SHA256.lower() != _subject_gen.lower():
                return None, "runtime admission policy differs from subject generator"
            for source in admission.sources:
                if source.path == entry.receipt_path:
                    return None, "admission receipt cannot cite itself"
                source_bytes = self._show(entry.bundle_commit.lower(), source.path)
                if source_bytes is None:
                    return None, "source receipt is missing in bundle"
                if hashlib.sha256(source_bytes).hexdigest() != source.sha256:
                    return None, "source receipt hash mismatch"
            try:
                req_paths = _required_paths(expected_key)
            except ValueError:
                return None, "admission key mismatch"
            path_to_source: dict[str, _SourceName] = {v: k for k, v in _SOURCE_PATHS.items()}
            parsed: dict[_SourceName, _ParsedSource] = {}
            for req_path in req_paths:
                blob = self._show(entry.bundle_commit.lower(), req_path)
                if blob is None:
                    continue
                req_name = path_to_source.get(req_path)
                if req_name is None:
                    return None, "source receipt is not registered"
                try:
                    item = _parse_source(req_name, blob, admission.subject_commit)
                except ValueError:
                    return None, "source receipt is not registered"
                if not item.typed_valid:
                    return None, "source receipt is typed-invalid"
                parsed[req_name] = item
            typed_paths: set[str] = {_SOURCE_PATHS[n] for n in parsed}
            expected_set = set(req_paths) & typed_paths
            claimed_set = {s.path for s in admission.sources}
            if claimed_set != expected_set:
                return None, "admission sources are forged"
            if tuple(sorted(claimed_set)) != tuple(s.path for s in admission.sources):
                return None, "admission sources are unsorted"
            for source in admission.sources:
                req_name2 = path_to_source.get(source.path)
                if req_name2 is None:
                    return None, "admission sources are forged"
                if req_name2 not in parsed:
                    return None, "admission sources are forged"
                live = parsed[req_name2]
                if live.sha256 != source.sha256:
                    return None, "source receipt hash mismatch"
                if live.schema_version != source.schema_version:
                    return None, "source receipt schema mismatch"
                if live.path != source.path:
                    return None, "source receipt path mismatch"
            try:
                expected_state = _evaluate_slot(expected_key, parsed)
            except ValueError:
                return None, "admission verification failed"
            if admission.state != expected_state:
                return None, "admission state is forged"
            return admission.state, "immutable admission verified"
        except (OSError, RuntimeError, ValueError):
            return None, "admission verification failed"


def score_quality(
    architecture: ArchitectureReceipt,
    evidence: ScoreEvidence | None,
    baseline: ArchitectureReceipt | None,
    repo_root: Path | None = None,
) -> QualityScoreReceipt:
    validate_registry()
    if not _verify_registry():
        raise ValueError("admission registry mismatch")
    root = repo_root or Path(__file__).resolve().parents[2]
    verifier = _AdmissionVerifier(root)
    trusted_architecture, subject_tree_matches = _rebuild_claimed_architecture(root, architecture)
    if trusted_architecture is None:
        architecture_states: dict[str, tuple[EvidenceState, str]] = {
            key: ("missing", "exact-subject architecture is unavailable")
            for key, _label, _points in SCORE_BLOCKS
            if key.startswith("elegance.")
        }
    else:
        architecture_states = _architecture_states(trusted_architecture.metrics)
    known_blocks = {key for key, _l, _p in SCORE_BLOCKS}
    blocks: list[ScoreBlock] = []
    for key, label, points in SCORE_BLOCKS:
        if key in architecture_states:
            state, reason = architecture_states[key]
            blocks.append(
                ScoreBlock(
                    key=key,
                    label=label,
                    points=points,
                    state=state,
                    awarded=points if state == "pass" else 0,
                    reason=_bound(reason),
                )
            )
            continue
        if evidence is None or key not in evidence.blocks:
            blocks.append(
                ScoreBlock(
                    key=key,
                    label=label,
                    points=points,
                    state="missing",
                    awarded=0,
                    reason="required evidence entry is absent",
                )
            )
            continue
        entry = evidence.blocks[key]
        if evidence.scoped_commit != architecture.scoped_commit:
            state = None
            reason = "evidence commit differs from architecture commit"
        else:
            state, reason = verifier.verify(entry, "block", key, architecture.scoped_commit)
        block_state: EvidenceState = state or "missing"
        blocks.append(
            ScoreBlock(
                key=key,
                label=label,
                points=points,
                state=block_state,
                awarded=points if state == "pass" else 0,
                reason=_bound(reason),
            )
        )
    hard_failures: list[str] = []
    hard_missing: list[str] = []
    if not subject_tree_matches:
        hard_missing.append("architecture_subject_tree")
    trusted_baseline: ArchitectureReceipt | None = None
    baseline_matches = baseline is None
    if baseline is not None:
        trusted_baseline, baseline_matches = _rebuild_claimed_architecture(root, baseline)
        if not baseline_matches:
            hard_missing.append("architecture_baseline")
    if evidence is None:
        hard_missing.append("all hard-gate evidence")
    else:
        for key in sorted(set(evidence.blocks) - known_blocks):
            hard_failures.append(f"unknown score block: {key}")
        for key in sorted(set(evidence.hard_gates) - set(HARD_GATES)):
            hard_failures.append(f"unknown hard gate: {key}")
        commit_ok = evidence.scoped_commit == architecture.scoped_commit
        for key in HARD_GATES:
            entry = evidence.hard_gates.get(key)
            if entry is None:
                hard_missing.append(key)
                continue
            if not commit_ok:
                hard_missing.append(key)
                continue
            state, _reason = verifier.verify(entry, "hard_gate", key, architecture.scoped_commit)
            if state is None:
                hard_missing.append(key)
            elif state == "fail":
                hard_failures.append(key)
    regressions: tuple[str, ...] = ()
    if (
        subject_tree_matches
        and trusted_architecture is not None
        and baseline_matches
        and trusted_baseline is not None
    ):
        regressions = architecture_regressions(
            trusted_architecture.metrics,
            trusted_baseline.metrics,
        )
    points = sum(block.awarded for block in blocks)
    missing = any(block.state == "missing" for block in blocks) or bool(hard_missing)
    failed = bool(hard_failures) or bool(regressions)
    verdict: Literal["PASS", "FAIL", "HOLD"]
    if missing:
        verdict = "HOLD"
    elif failed or points < 90:
        verdict = "FAIL"
    else:
        verdict = "PASS"
    return QualityScoreReceipt(
        scoped_commit=architecture.scoped_commit,
        score_points=points,
        score_out_of_ten=f"{points // 10}.{points % 10}",
        verdict=verdict,
        blocks=tuple(blocks),
        hard_gate_failures=_bound_diagnostics(sorted(hard_failures)),
        hard_gate_missing=_bound_diagnostics(sorted(hard_missing)),
        architecture_regressions=_bound_diagnostics(regressions),
        architecture=architecture,
    )


def _load_architecture(path: Path) -> ArchitectureReceipt:
    return ArchitectureReceipt.model_validate_json(path.read_text(encoding="utf-8"))


def _load_evidence(path: Path) -> ScoreEvidence:
    return ScoreEvidence.model_validate_json(path.read_text(encoding="utf-8"))


def _write_json(payload: BaseModel, output: Path | None) -> None:
    rendered = payload.model_dump_json(indent=2) + "\n"
    if output is None:
        sys.stdout.write(rendered)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(json.dumps({"output": str(output), "bytes": len(rendered)}) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--revision", default="WORKTREE", help="git revision or WORKTREE")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--evidence", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--architecture-only", action="store_true")
    mode.add_argument("--ratchet-only", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = args.repo_root.resolve()
    try:
        architecture = build_architecture_receipt(repo_root, args.revision)
        if args.architecture_only:
            _write_json(architecture, args.output)
            return 0
        baseline = _load_architecture(args.baseline) if args.baseline else None
        if args.ratchet_only:
            if baseline is None:
                raise ValueError("--ratchet-only requires --baseline")
            trusted_current, current_matches = _rebuild_claimed_architecture(
                repo_root, architecture
            )
            trusted_baseline, baseline_matches = _rebuild_claimed_architecture(repo_root, baseline)
            if trusted_current is None or not current_matches:
                raise ValueError("current receipt does not match its exact Git commit")
            if trusted_baseline is None or not baseline_matches:
                raise ValueError("baseline receipt does not match its exact Git commit")
            ratchet = compare_architecture(trusted_current, trusted_baseline)
            _write_json(ratchet, args.output)
            return 0 if ratchet.status == "PASS" else (2 if ratchet.status == "HOLD" else 1)
        if args.evidence is not None or baseline is not None:
            evidence = _load_evidence(args.evidence) if args.evidence else None
            receipt = score_quality(architecture, evidence, baseline, repo_root)
            _write_json(receipt, args.output)
            return 0 if receipt.verdict == "PASS" else (2 if receipt.verdict == "HOLD" else 1)
        _write_json(architecture, args.output)
        return 0
    except (OSError, RuntimeError, ValueError, ValidationError) as exc:
        sys.stderr.write(
            json.dumps(
                {"event": "architecture_measurement_failed", "error_type": type(exc).__name__}
            )
            + "\n"
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
