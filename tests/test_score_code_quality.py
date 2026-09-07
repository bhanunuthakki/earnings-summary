from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from quality.architecture import (
    ArchitectureReceipt,
    analyze_sources,
    build_architecture_receipt,
)
from quality.evidence_path_policy import admission_path_for
from quality.git_env import clean_local_git_env
from quality.scoring import (
    ADMISSION_GENERATOR_PATH,
    ADMISSION_SCHEMA,
    HARD_GATES,
    SCORE_BLOCKS,
    AdmissionReceipt,
    EvidenceEntry,
    ScoreEvidence,
    score_quality,
    validate_registry,
)

GEN_PATH = ADMISSION_GENERATOR_PATH
GEN_BODY = (Path(__file__).resolve().parents[1] / ADMISSION_GENERATOR_PATH).read_text(
    encoding="utf-8"
)
SOURCE_REF = "docs/quality/upstream.json"
SOURCE_SCHEMA = "test-upstream/v1"
SOURCE_BODY = b'{"schema_version":"test-upstream/v1","status":"PASS"}\n'
SOURCE_SHA = hashlib.sha256(SOURCE_BODY).hexdigest()
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        env=clean_local_git_env(),
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _sources() -> dict[str, str]:
    return {
        "src/a.py": "def helper(value: int) -> int:\n    return value + 1\n",
        "execution/comments_server.py": "def main() -> None:\n    pass\n",
        "src/pipeline/portfolio_panel.py": "def render() -> str:\n    return 'ok'\n",
        GEN_PATH: GEN_BODY,
    }


class HermeticRepo:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.subject = ""
        self.bundle = ""
        self.gen_sha = hashlib.sha256(GEN_BODY.encode()).hexdigest()
        self.receipts: dict[str, bytes] = {}

    def commit_subject(self) -> None:
        _run(self.root, "init", "-q")
        _run(self.root, "config", "user.email", "t@example.com")
        _run(self.root, "config", "user.name", "t")
        for path, body in _sources().items():
            full = self.root / path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(body, encoding="utf-8")
        _run(self.root, "add", ".")
        _run(self.root, "commit", "-qm", "subject")
        self.subject = _run(self.root, "rev-parse", "HEAD")

    def admission(
        self, kind: str, key: str, state: str, subject: str | None = None
    ) -> dict[str, object]:
        subj = subject or self.subject
        if state == "fail":
            sources: list[dict[str, object]] = []
        else:
            sources = [
                {
                    "path": SOURCE_REF,
                    "sha256": SOURCE_SHA,
                    "schema_version": SOURCE_SCHEMA,
                }
            ]
        return {
            "schema_version": ADMISSION_SCHEMA,
            "kind": kind,
            "key": key,
            "state": state,
            "subject_commit": subj,
            "generator_path": GEN_PATH,
            "generator_sha256": self.gen_sha,
            "sources": sources,
        }

    def commit_bundle(
        self,
        payloads: dict[str, dict[str, object]],
        extra: dict[str, str] | None = None,
    ) -> None:
        source = self.root / SOURCE_REF
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(SOURCE_BODY)
        for rel, payload in payloads.items():
            full = self.root / rel
            full.parent.mkdir(parents=True, exist_ok=True)
            raw = json.dumps(payload, sort_keys=True).encode()
            full.write_text(raw.decode(), encoding="utf-8")
            self.receipts[rel] = raw
        for rel, body in (extra or {}).items():
            full = self.root / rel
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(body, encoding="utf-8")
        _run(self.root, "add", ".")
        _run(self.root, "commit", "-qm", "bundle")
        self.bundle = _run(self.root, "rev-parse", "HEAD")
        _run(self.root, "update-ref", "refs/remotes/origin/main", self.bundle)

    def evidence(
        self, states: dict[str, str] | None = None, gates: dict[str, str] | None = None
    ) -> ScoreEvidence:
        states = states or {}
        gates = gates or {}
        blocks: dict[str, EvidenceEntry] = {}
        for key, _label, _points in SCORE_BLOCKS:
            if key.startswith("elegance."):
                continue
            rel = admission_path_for("block", key)
            raw = self.receipts[rel]
            blocks[key] = EvidenceEntry(
                receipt_path=rel,
                sha256=hashlib.sha256(raw).hexdigest(),
                bundle_commit=self.bundle,
            )
            if key in states and states[key] == "fail":
                pass  # state lives inside receipt; rebuilt by caller variant
        hard: dict[str, EvidenceEntry] = {}
        for key in HARD_GATES:
            rel = admission_path_for("hard_gate", key)
            raw = self.receipts[rel]
            hard[key] = EvidenceEntry(
                receipt_path=rel, sha256=hashlib.sha256(raw).hexdigest(), bundle_commit=self.bundle
            )
        return ScoreEvidence(
            schema_version="quality-score-evidence-v1",
            scoped_commit=self.subject,
            blocks=blocks,
            hard_gates=hard,
        )


def _make_repo(
    tmp_path: Path, block_fails: set[str] | None = None, gate_fails: set[str] | None = None
) -> HermeticRepo:
    block_fails = block_fails or set()
    gate_fails = gate_fails or set()
    repo = HermeticRepo(tmp_path / "repo")
    repo.root.mkdir()
    repo.commit_subject()
    _commit_admission_bundle(repo, block_fails, gate_fails)
    return repo


def _commit_admission_bundle(
    repo: HermeticRepo,
    block_fails: set[str] | None = None,
    gate_fails: set[str] | None = None,
) -> None:
    block_fails = block_fails or set()
    gate_fails = gate_fails or set()
    payloads: dict[str, dict[str, object]] = {}
    for key, _l, _p in SCORE_BLOCKS:
        if key.startswith("elegance."):
            continue
        payloads[admission_path_for("block", key)] = repo.admission(
            "block", key, "fail" if key in block_fails else "pass"
        )
    for key in HARD_GATES:
        payloads[admission_path_for("hard_gate", key)] = repo.admission(
            "hard_gate", key, "fail" if key in gate_fails else "pass"
        )
    repo.commit_bundle(payloads)


def _replace_bundle_receipt(repo: HermeticRepo, path: str, payload: dict[str, object]) -> None:
    _run(repo.root, "checkout", "-q", repo.bundle)
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    (repo.root / path).write_bytes(raw)
    repo.receipts[path] = raw
    _run(repo.root, "add", path)
    _run(repo.root, "commit", "-qm", "replace admission")
    repo.bundle = _run(repo.root, "rev-parse", "HEAD")
    _run(repo.root, "update-ref", "refs/remotes/origin/main", repo.bundle)


def _arch(repo: HermeticRepo) -> ArchitectureReceipt:
    return build_architecture_receipt(repo.root, repo.subject)


def test_registry_counts_weights_gates() -> None:
    validate_registry()
    keys = [k for k, _l, _p in SCORE_BLOCKS]
    assert len(keys) == 18 and len(set(keys)) == 18
    assert sum(p for _k, _l, p in SCORE_BLOCKS) == 100
    assert len(HARD_GATES) == 10 and len(set(HARD_GATES)) == 10


def test_admission_model_rejects_bad_paths_hashes_duplicates() -> None:
    good = {
        "schema_version": ADMISSION_SCHEMA,
        "kind": "block",
        "key": "efficiency.test_ci",
        "state": "pass",
        "subject_commit": "ab" * 20,
        "generator_path": GEN_PATH,
        "generator_sha256": "cd" * 32,
        "sources": [
            {
                "path": SOURCE_REF,
                "sha256": "ef" * 32,
                "schema_version": SOURCE_SCHEMA,
            }
        ],
    }
    AdmissionReceipt.model_validate(good)
    with pytest.raises(ValidationError):
        AdmissionReceipt.model_validate({**good, "generator_path": "/tmp/evil.py"})
    with pytest.raises(ValidationError):
        AdmissionReceipt.model_validate({**good, "generator_sha256": "zz"})
    AdmissionReceipt.model_validate({**good, "state": "fail", "sources": []})
    with pytest.raises(ValidationError):
        AdmissionReceipt.model_validate({**good, "state": "pass", "sources": []})
    with pytest.raises(ValidationError):
        AdmissionReceipt.model_validate(
            {
                **good,
                "sources": [
                    {
                        "path": SOURCE_REF,
                        "sha256": "ef" * 32,
                        "schema_version": SOURCE_SCHEMA,
                    },
                    {
                        "path": SOURCE_REF,
                        "sha256": "ef" * 32,
                        "schema_version": SOURCE_SCHEMA,
                    },
                ],
            }
        )
    with pytest.raises(ValidationError):
        EvidenceEntry(receipt_path="/tmp/evil.json", sha256="ab" * 32, bundle_commit="ab" * 20)


def test_score_evidence_rejects_unbounded_keys() -> None:
    entry = EvidenceEntry(
        receipt_path="docs/quality/example.json",
        sha256="ab" * 32,
        bundle_commit="ab" * 20,
    )
    with pytest.raises(ValidationError):
        ScoreEvidence(
            schema_version="quality-score-evidence-v1",
            scoped_commit="ab" * 20,
            blocks={"x" * 101: entry},
        )


@pytest.mark.parametrize("mode", ["unregistered-generator", "self-source"])
def test_admission_rejects_unregistered_generator_and_self_source(
    tmp_path: Path, mode: str
) -> None:
    repo = _make_repo(tmp_path)
    architecture = _arch(repo)
    evidence = repo.evidence()
    key = "maintainability.static_quality"
    entry = evidence.blocks[key]
    payload = repo.admission("block", key, "pass")
    if mode == "unregistered-generator":
        payload["generator_path"] = "src/quality/not_the_oracle.py"
    else:
        payload["sources"] = [
            {
                "path": entry.receipt_path,
                "sha256": entry.sha256,
                "schema_version": ADMISSION_SCHEMA,
            }
        ]
    _replace_bundle_receipt(repo, entry.receipt_path, payload)
    replacement = EvidenceEntry(
        receipt_path=entry.receipt_path,
        sha256=hashlib.sha256(repo.receipts[entry.receipt_path]).hexdigest(),
        bundle_commit=repo.bundle,
    )
    result = score_quality(
        architecture,
        evidence.model_copy(update={"blocks": {**evidence.blocks, key: replacement}}),
        None,
        repo.root,
    )
    block = next(item for item in result.blocks if item.key == key)
    assert block.state == "missing"
    assert result.verdict == "HOLD"


def test_old_roadmap_path_cannot_substitute(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    architecture = _arch(repo)
    evidence = repo.evidence()
    key = "maintainability.static_quality"
    entry = evidence.blocks[key]
    payload = repo.admission("block", key, "pass")
    payload["generator_path"] = "src/quality/roadmap_reconciliation.py"
    payload["generator_sha256"] = hashlib.sha256(b"old-roadmap\n").hexdigest()
    _replace_bundle_receipt(repo, entry.receipt_path, payload)
    replacement = EvidenceEntry(
        receipt_path=entry.receipt_path,
        sha256=hashlib.sha256(repo.receipts[entry.receipt_path]).hexdigest(),
        bundle_commit=repo.bundle,
    )
    result = score_quality(
        architecture,
        evidence.model_copy(update={"blocks": {**evidence.blocks, key: replacement}}),
        None,
        repo.root,
    )
    assert next(item for item in result.blocks if item.key == key).state == "missing"
    assert result.verdict == "HOLD"


def test_adversarial_generic_source_awards_only_architecture(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    result = score_quality(_arch(repo), repo.evidence(), None, repo.root)
    assert result.score_points == 25 and result.verdict == "HOLD"
    non_arch = [b for b in result.blocks if not b.key.startswith("elegance.")]
    assert len(non_arch) == 14
    assert all(b.state == "missing" and b.awarded == 0 for b in non_arch)
    assert sum(b.awarded for b in result.blocks if b.key.startswith("elegance.")) == 25


def test_adversarial_generic_source_with_declared_fail_still_hold(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, block_fails={"efficiency.integrity_audit"})
    result = score_quality(_arch(repo), repo.evidence(), None, repo.root)
    assert result.score_points == 25 and result.verdict == "HOLD"
    failed = next(b for b in result.blocks if b.key == "efficiency.integrity_audit")
    assert failed.state == "fail" and failed.awarded == 0
    others = [b for b in result.blocks if not b.key.startswith("elegance.")]
    others = [b for b in others if b.key != "efficiency.integrity_audit"]
    assert all(b.state == "missing" and b.awarded == 0 for b in others)


def test_adversarial_generic_source_with_two_declared_fails_still_hold(
    tmp_path: Path,
) -> None:
    repo = _make_repo(
        tmp_path,
        block_fails={"maintainability.static_quality", "maintainability.sustainable_tests"},
    )
    r89 = score_quality(_arch(repo), repo.evidence(), None, repo.root)
    assert r89.score_points == 25 and r89.verdict == "HOLD"
    for key in ("maintainability.static_quality", "maintainability.sustainable_tests"):
        block = next(b for b in r89.blocks if b.key == key)
        assert block.state == "fail" and block.awarded == 0
    missing_passes = [
        b
        for b in r89.blocks
        if not b.key.startswith("elegance.")
        and b.key not in ("maintainability.static_quality", "maintainability.sustainable_tests")
    ]
    assert missing_passes and all(b.state == "missing" for b in missing_passes)


def test_ordinary_failed_block_zero(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, block_fails={"maintainability.static_quality"})
    result = score_quality(_arch(repo), repo.evidence(), None, repo.root)
    key = "maintainability.static_quality"
    block = next(b for b in result.blocks if b.key == key)
    assert block.state == "fail" and block.awarded == 0
    assert result.score_points == 25
    assert result.verdict == "HOLD"
    forged = [b for b in result.blocks if not b.key.startswith("elegance.") and b.key != key]
    assert forged and all(b.state == "missing" and b.awarded == 0 for b in forged)


def test_missing_evidence_hold(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    arch = _arch(repo)
    result = score_quality(arch, None, None, repo.root)
    assert result.verdict == "HOLD"
    ev = repo.evidence()
    ev2 = ev.model_copy(
        update={"blocks": {k: v for k, v in ev.blocks.items() if k != "efficiency.test_ci"}}
    )
    assert score_quality(arch, ev2, None, repo.root).verdict == "HOLD"


def test_hard_gate_fail(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, gate_fails={HARD_GATES[0]})
    result = score_quality(_arch(repo), repo.evidence(), None, repo.root)
    assert result.verdict == "HOLD" and HARD_GATES[0] in result.hard_gate_failures
    assert result.hard_gate_missing
    assert result.score_points == 25
    forged_blocks = [b for b in result.blocks if not b.key.startswith("elegance.")]
    assert forged_blocks and all(b.state == "missing" for b in forged_blocks)


def _hold_for_tamper(tmp_path: Path, mode: str) -> None:
    repo = _make_repo(tmp_path)
    arch = _arch(repo)
    ev = repo.evidence()
    key = "maintainability.static_quality"
    good = ev.blocks[key]
    if mode == "wrong-hash":
        bad = EvidenceEntry(
            receipt_path=good.receipt_path, sha256="ff" * 32, bundle_commit=good.bundle_commit
        )
    elif mode == "wrong-schema":
        rel = good.receipt_path
        raw = repo.receipts[rel]
        payload = json.loads(raw.decode())
        payload["schema_version"] = "bogus-v1"
        raw2 = json.dumps(payload, sort_keys=True).encode()
        _run(repo.root, "checkout", "-q", repo.bundle)
        (repo.root / rel).write_bytes(raw2)
        _run(repo.root, "add", ".")
        _run(repo.root, "commit", "-qm", "tamper")
        bundle2 = _run(repo.root, "rev-parse", "HEAD")
        _run(repo.root, "update-ref", "refs/remotes/origin/main", bundle2)
        bad = EvidenceEntry(
            receipt_path=rel, sha256=hashlib.sha256(raw2).hexdigest(), bundle_commit=bundle2
        )
    elif mode == "wrong-subject":
        rel = good.receipt_path
        raw = repo.receipts[rel]
        payload = json.loads(raw.decode())
        payload["subject_commit"] = "00" * 20
        raw2 = json.dumps(payload, sort_keys=True).encode()
        _run(repo.root, "checkout", "-q", repo.bundle)
        (repo.root / rel).write_bytes(raw2)
        _run(repo.root, "add", ".")
        _run(repo.root, "commit", "-qm", "tamper")
        bundle2 = _run(repo.root, "rev-parse", "HEAD")
        _run(repo.root, "update-ref", "refs/remotes/origin/main", bundle2)
        bad = EvidenceEntry(
            receipt_path=rel, sha256=hashlib.sha256(raw2).hexdigest(), bundle_commit=bundle2
        )
    elif mode == "wrong-generator":
        rel = good.receipt_path
        raw = repo.receipts[rel]
        payload = json.loads(raw.decode())
        payload["generator_sha256"] = "11" * 32
        raw2 = json.dumps(payload, sort_keys=True).encode()
        _run(repo.root, "checkout", "-q", repo.bundle)
        (repo.root / rel).write_bytes(raw2)
        _run(repo.root, "add", ".")
        _run(repo.root, "commit", "-qm", "tamper")
        bundle2 = _run(repo.root, "rev-parse", "HEAD")
        _run(repo.root, "update-ref", "refs/remotes/origin/main", bundle2)
        bad = EvidenceEntry(
            receipt_path=rel, sha256=hashlib.sha256(raw2).hexdigest(), bundle_commit=bundle2
        )
    elif mode == "non-evidence-bundle":
        _run(repo.root, "checkout", "-q", repo.bundle)
        (repo.root / "src/a.py").write_text("def other() -> int:\n    return 2\n", encoding="utf-8")
        _run(repo.root, "add", ".")
        _run(repo.root, "commit", "-qm", "non-evidence")
        bundle2 = _run(repo.root, "rev-parse", "HEAD")
        _run(repo.root, "update-ref", "refs/remotes/origin/main", bundle2)
        bad = EvidenceEntry(
            receipt_path=good.receipt_path,
            sha256=good.sha256,
            bundle_commit=bundle2,
        )
    elif mode == "whitespace-path-bundle":
        _run(repo.root, "checkout", "-q", repo.bundle)
        outside = repo.root / " docs/quality/outside.json"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text('{"schema_version":"outside/v1"}\n', encoding="utf-8")
        _run(repo.root, "add", ".")
        _run(repo.root, "commit", "-qm", "leading-space path")
        bundle2 = _run(repo.root, "rev-parse", "HEAD")
        _run(repo.root, "update-ref", "refs/remotes/origin/main", bundle2)
        bad = EvidenceEntry(
            receipt_path=good.receipt_path,
            sha256=good.sha256,
            bundle_commit=bundle2,
        )
    elif mode == "source-to-evidence-rename":
        _run(repo.root, "checkout", "-q", repo.bundle)
        _run(repo.root, "mv", "src/a.py", "docs/quality/moved-source.json")
        _run(repo.root, "commit", "-qm", "hide source deletion as evidence rename")
        bundle2 = _run(repo.root, "rev-parse", "HEAD")
        _run(repo.root, "update-ref", "refs/remotes/origin/main", bundle2)
        bad = EvidenceEntry(
            receipt_path=good.receipt_path,
            sha256=good.sha256,
            bundle_commit=bundle2,
        )
    elif mode == "untrusted-bundle":
        _run(repo.root, "update-ref", "refs/remotes/origin/main", repo.subject)
        bad = good
    else:
        raise AssertionError
    ev2 = ev.model_copy(update={"blocks": {**ev.blocks, key: bad}})
    result = score_quality(arch, ev2, None, repo.root)
    assert next(b for b in result.blocks if b.key == key).state == "missing"
    assert result.verdict == "HOLD"


@pytest.mark.parametrize(
    "mode",
    [
        "wrong-hash",
        "wrong-schema",
        "wrong-subject",
        "wrong-generator",
        "non-evidence-bundle",
        "whitespace-path-bundle",
        "source-to-evidence-rename",
        "untrusted-bundle",
    ],
)
def test_untrusted_receipt_hold(tmp_path: Path, mode: str) -> None:
    _hold_for_tamper(tmp_path, mode)


def test_receipt_path_must_match_canonical_admission_path(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    architecture = _arch(repo)
    evidence = repo.evidence()
    target = "maintainability.static_quality"
    control = "maintainability.duplication"
    before = score_quality(architecture, evidence, None, repo.root)
    control_before = next(item for item in before.blocks if item.key == control)
    swapped_path = admission_path_for("block", control)
    assert swapped_path != admission_path_for("block", target)
    tampered = evidence.blocks[target].model_copy(update={"receipt_path": swapped_path})
    after = score_quality(
        architecture,
        evidence.model_copy(update={"blocks": {**evidence.blocks, target: tampered}}),
        None,
        repo.root,
    )
    target_after = next(item for item in after.blocks if item.key == target)
    assert target_after.state == "missing"
    assert "admission receipt path mismatch" in target_after.reason
    assert after.verdict == "HOLD"
    control_after = next(item for item in after.blocks if item.key == control)
    assert (control_after.state, control_after.awarded) == (
        control_before.state,
        control_before.awarded,
    )


def test_noncanonical_path_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        EvidenceEntry(receipt_path="/tmp/evil.json", sha256="ab" * 32, bundle_commit="ab" * 20)
    with pytest.raises(ValidationError):
        EvidenceEntry(receipt_path="docs/other/x.json", sha256="ab" * 32, bundle_commit="ab" * 20)
    with pytest.raises(ValidationError):
        EvidenceEntry(
            receipt_path="docs/quality//x.json",
            sha256="ab" * 32,
            bundle_commit="ab" * 20,
        )


def test_unknown_keys_fail_closed(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    arch = _arch(repo)
    ev = repo.evidence()
    extra = EvidenceEntry(
        receipt_path="docs/quality/extra.json", sha256="ab" * 32, bundle_commit=repo.bundle
    )
    ev2 = ev.model_copy(update={"blocks": {**ev.blocks, "bogus.block": extra}})
    result2 = score_quality(arch, ev2, None, repo.root)
    assert result2.verdict == "HOLD"
    assert "unknown score block: bogus.block" in result2.hard_gate_failures
    ev3 = ev.model_copy(update={"hard_gates": {**ev.hard_gates, "bogus_gate": extra}})
    result3 = score_quality(arch, ev3, None, repo.root)
    assert result3.verdict == "HOLD"
    assert "unknown hard gate: bogus_gate" in result3.hard_gate_failures


def test_architecture_regression(tmp_path: Path) -> None:
    repo = HermeticRepo(tmp_path / "repo")
    repo.root.mkdir()
    repo.commit_subject()
    base = _arch(repo)
    (repo.root / "src/big.py").write_text(
        "\n".join(f"x{i}={i}" for i in range(1002)), encoding="utf-8"
    )
    _run(repo.root, "add", "src/big.py")
    _run(repo.root, "commit", "-qm", "grow subject")
    repo.subject = _run(repo.root, "rev-parse", "HEAD")
    _commit_admission_bundle(repo)
    grown = _arch(repo)
    result = score_quality(grown, repo.evidence(), base, repo.root)
    assert result.verdict == "HOLD" and result.architecture_regressions
    assert result.score_points == 25
    assert result.hard_gate_missing


def test_architecture_blocks_need_no_caller_pass(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    arch = _arch(repo)
    ev = repo.evidence()
    # Architecture elegance blocks are recomputed; evidence omits them entirely.
    assert not any(k.startswith("elegance.") for k in ev.blocks)
    result = score_quality(arch, ev, None, repo.root)
    assert result.score_points == 25
    assert result.verdict == "HOLD"
    assert all(b.state == "missing" for b in result.blocks if not b.key.startswith("elegance."))


def test_forged_architecture_metrics_hold_and_cannot_change_points(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    architecture = _arch(repo)
    forged_sources = _sources()
    forged_sources["src/oversized.py"] = "\n".join(f"x{i}={i}" for i in range(1002))
    forged = architecture.model_copy(update={"metrics": analyze_sources(forged_sources)})
    result = score_quality(forged, repo.evidence(), None, repo.root)
    assert result.score_points == 25
    assert result.verdict == "HOLD"
    assert "architecture_subject_tree" in result.hard_gate_missing


def test_forged_baseline_metrics_hold(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    architecture = _arch(repo)
    forged_sources = _sources()
    forged_sources["src/oversized.py"] = "\n".join(f"x{i}={i}" for i in range(1002))
    forged_baseline = architecture.model_copy(update={"metrics": analyze_sources(forged_sources)})
    result = score_quality(architecture, repo.evidence(), forged_baseline, repo.root)
    assert result.verdict == "HOLD"
    assert "architecture_baseline" in result.hard_gate_missing


def test_clean_worktree_can_match_exact_subject(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _run(repo.root, "checkout", "-q", repo.subject)
    architecture = build_architecture_receipt(repo.root, "WORKTREE")
    result = score_quality(architecture, repo.evidence(), None, repo.root)
    assert result.verdict == "HOLD"
    assert result.score_points == 25
    assert "architecture_subject_tree" not in result.hard_gate_missing


def test_dirty_worktree_cannot_claim_head_subject(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _run(repo.root, "checkout", "-q", repo.subject)
    (repo.root / "src/a.py").write_text("value = 7\n", encoding="utf-8")
    architecture = build_architecture_receipt(repo.root, "WORKTREE")
    result = score_quality(architecture, repo.evidence(), None, repo.root)
    assert result.verdict == "HOLD"
    assert "architecture_subject_tree" in result.hard_gate_missing


def test_shared_context_git_call_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path)
    arch = _arch(repo)
    ev = repo.evidence()
    import quality.scoring as mod

    calls = 0
    real_git = cast(Callable[..., subprocess.CompletedProcess[bytes]], getattr(mod, "_git"))

    def _counting(repo_root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
        nonlocal calls
        calls += 1
        return real_git(repo_root, *args)

    monkeypatch.setattr(mod, "_git", _counting)
    result = score_quality(arch, ev, None, repo.root)
    assert result.verdict == "HOLD" and result.score_points == 25
    assert calls <= 48, f"expected shared bundle/blob context, got {calls} git calls"


def test_admission_git_reads_ignore_inherited_repo_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "wrong-repository"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "wrong-worktree"))
    repo = _make_repo(tmp_path)
    architecture = _arch(repo)
    evidence = repo.evidence()
    result = score_quality(architecture, evidence, None, repo.root)
    assert result.verdict == "HOLD"
    assert result.score_points == 25


def test_no_database(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    assert list(repo.root.rglob("*.db")) == [] and list(repo.root.rglob("*.sqlite")) == []
    result = score_quality(_arch(repo), repo.evidence(), None, repo.root)
    assert result.verdict == "HOLD" and result.score_points == 25


def test_direct_cli_and_boundary_registration() -> None:
    proc = subprocess.run(
        [sys.executable, "execution/score_code_quality.py", "--help"],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert proc.returncode == 0
    data = json.loads(Path("config/architecture_boundaries.json").read_text())
    assert "execution/score_code_quality.py" in data["execution_sys_path_mutations"]


def test_ratchet_cli_rejects_forged_baseline(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    baseline = _arch(repo)
    forged_sources = _sources()
    forged_sources["src/oversized.py"] = "\n".join(f"x{i}={i}" for i in range(1002))
    forged = baseline.model_copy(update={"metrics": analyze_sources(forged_sources)})
    baseline_path = tmp_path / "forged-baseline.json"
    baseline_path.write_text(forged.model_dump_json(indent=2) + "\n", encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "execution/score_code_quality.py"),
            "--repo-root",
            str(repo.root),
            "--revision",
            repo.subject,
            "--ratchet-only",
            "--baseline",
            str(baseline_path),
        ],
        cwd=repo.root,
        capture_output=True,
        text=True,
        env=clean_local_git_env(),
    )
    assert proc.returncode == 2
    assert json.loads(proc.stderr) == {
        "event": "architecture_measurement_failed",
        "error_type": "ValueError",
    }


def test_ratchet_cli_rejects_dirty_worktree_claim(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    baseline = _arch(repo)
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(baseline.model_dump_json(indent=2) + "\n", encoding="utf-8")
    (repo.root / "src/a.py").write_text("value = 7\n", encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "execution/score_code_quality.py"),
            "--repo-root",
            str(repo.root),
            "--revision",
            "WORKTREE",
            "--ratchet-only",
            "--baseline",
            str(baseline_path),
        ],
        cwd=repo.root,
        capture_output=True,
        text=True,
        env=clean_local_git_env(),
    )
    assert proc.returncode == 2
    assert json.loads(proc.stderr) == {
        "event": "architecture_measurement_failed",
        "error_type": "ValueError",
    }


def test_runtime_policy_drift_hold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import quality.admission_policy as _policy
    import quality.scoring as _scoring

    repo = _make_repo(tmp_path)
    monkeypatch.setattr(_policy, "RUNTIME_POLICY_SHA256", "0" * 64)
    monkeypatch.setattr(_scoring, "_RUNTIME_POLICY_SHA256", "0" * 64)
    result = score_quality(_arch(repo), repo.evidence(), None, repo.root)
    assert result.verdict == "HOLD"
    assert result.score_points == 25
    assert all(b.state == "missing" for b in result.blocks if not b.key.startswith("elegance."))
