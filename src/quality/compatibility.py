"""Deterministic compatibility evidence for the Train 0 quality baseline."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field


class CompatibilityEvidenceError(RuntimeError):
    """Evidence cannot be trusted or is incomplete."""


class GoldenReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    sha256: str
    cases: int = Field(ge=1)


class EntrypointParity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    current_sha256: str | None
    baseline_sha256: str | None
    status: Literal["unchanged", "changed", "added", "removed"]


class EvidenceCategory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    status: Literal["PASS", "HOLD"]
    reason: str
    artifacts: list[str]
    artifact_sha256: str


class CompatibilityEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "quality-compatibility/v1"
    baseline_revision: str
    current_revision: str
    source_sha256: str
    baseline_sha256: str
    legacy_route_golden: list[GoldenReceipt]
    entrypoint_parity: list[EntrypointParity]
    golden_count: int = Field(ge=1)
    entrypoint_count: int = Field(ge=1)
    categories: list[EvidenceCategory]
    checklist_sha256: str
    hold: bool


REQUIRED_CATEGORIES: tuple[str, ...] = (
    "flask_url_method_endpoint_map",
    "integrity_serialized_ordering",
    "public_import_surfaces",
    "dcf_formula_cell_receipts",
    "population_dry_run_apply_receipts",
    "report_dashboard_goldens",
)
CHECKLIST = "\n".join(
    f"{name}: checked-in implementation and test/golden evidence required"
    for name in REQUIRED_CATEGORIES
)


def _run(root: Path, args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, check=True, text=True
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CompatibilityEvidenceError(f"git evidence command failed: {' '.join(args)}") from exc
    return result.stdout


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _aggregate(items: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for path, content in sorted(items):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
    return digest.hexdigest()


def _case_count(payload: object) -> int:
    if isinstance(payload, list):
        return len(cast(list[object], payload))
    if isinstance(payload, dict):
        payload_dict = cast(dict[str, object], payload)
        for key in ("cases", "goldens", "examples", "items"):
            value: object = payload_dict.get(key)
            if isinstance(value, list):
                return len(cast(list[object], value))
        return 1
    return 0


def _goldens(root: Path) -> tuple[list[GoldenReceipt], list[tuple[str, bytes]]]:
    paths = sorted(root.glob("evals/golden/*.json"))
    if not paths:
        raise CompatibilityEvidenceError("missing legacy-route golden evidence under evals/golden")
    receipts: list[GoldenReceipt] = []
    raw_items: list[tuple[str, bytes]] = []
    for path in paths:
        relative = path.relative_to(root).as_posix()
        raw = path.read_bytes()
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CompatibilityEvidenceError(f"invalid golden evidence: {relative}") from exc
        cases = _case_count(payload)
        if cases < 1:
            raise CompatibilityEvidenceError(f"golden evidence has no cases: {relative}")
        receipts.append(GoldenReceipt(path=relative, sha256=_sha256(raw), cases=cases))
        raw_items.append((relative, raw))
    return receipts, raw_items


def _entrypoints(
    root: Path, revision: str
) -> tuple[list[EntrypointParity], list[tuple[str, bytes]], list[tuple[str, bytes]]]:
    current_paths = {
        path
        for path in _run(root, ["ls-files", "--", "src/*.py", "execution/*.py"]).splitlines()
        if path
    }
    if not current_paths:
        raise CompatibilityEvidenceError("missing tracked src/ or execution/ Python entrypoints")
    try:
        archive = subprocess.run(
            ["git", "-C", str(root), "archive", revision, "--", "src", "execution"],
            capture_output=True,
            check=True,
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
            baseline_data: dict[str, bytes] = {}
            for member in tar.getmembers():
                if not member.isfile() or not member.name.endswith(".py"):
                    continue
                extracted = tar.extractfile(member)
                if extracted is not None:
                    baseline_data[member.name] = extracted.read()
    except (OSError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        raise CompatibilityEvidenceError("unable to batch-read baseline entrypoints") from exc
    baseline_paths = set(baseline_data)
    records: list[EntrypointParity] = []
    current_items: list[tuple[str, bytes]] = []
    baseline_items: list[tuple[str, bytes]] = []
    for path in sorted(current_paths | baseline_paths):
        current = (root / path).read_bytes() if path in current_paths else None
        baseline = baseline_data.get(path)
        current_hash = _sha256(current) if current is not None else None
        baseline_hash = _sha256(baseline) if baseline is not None else None
        if current is not None:
            current_items.append((path, current))
        if baseline is not None:
            baseline_items.append((path, baseline))
        status: Literal["unchanged", "changed", "added", "removed"]
        if current is None:
            status = "removed"
        elif baseline is None:
            status = "added"
        elif current_hash == baseline_hash:
            status = "unchanged"
        else:
            status = "changed"
        records.append(
            EntrypointParity(
                path=path, current_sha256=current_hash, baseline_sha256=baseline_hash, status=status
            )
        )
    return records, current_items, baseline_items


def _category(root: Path, name: str, patterns: tuple[str, ...]) -> EvidenceCategory:
    artifacts = sorted(
        {
            path.relative_to(root).as_posix()
            for pattern in patterns
            for path in root.glob(pattern)
            if path.is_file()
        }
    )
    payload = [(path, (root / path).read_bytes()) for path in artifacts]
    implementation = any(path.startswith(("src/", "execution/")) for path in artifacts)
    verification = any(path.startswith(("tests/", "evals/")) for path in artifacts)
    status: Literal["PASS", "HOLD"] = "PASS" if implementation and verification else "HOLD"
    reason = (
        "checked-in implementation and test/golden evidence present"
        if status == "PASS"
        else "missing checked-in implementation or test/golden evidence"
    )
    return EvidenceCategory(
        name=name,
        status=status,
        reason=reason,
        artifacts=artifacts,
        artifact_sha256=_aggregate(payload),
    )


def capture_compatibility_evidence(
    root: str | Path, baseline_revision: str
) -> CompatibilityEvidence:
    repo_root = Path(root).resolve()
    if not baseline_revision.strip():
        raise CompatibilityEvidenceError("baseline revision is required")
    current_revision = _run(repo_root, ["rev-parse", "HEAD"]).strip()
    _run(repo_root, ["rev-parse", "--verify", baseline_revision])
    golden, golden_items = _goldens(repo_root)
    parity, current_items, baseline_items = _entrypoints(repo_root, baseline_revision)
    categories = [
        _category(
            repo_root,
            "flask_url_method_endpoint_map",
            ("execution/comments_server.py", "tests/test_comments_server*.py"),
        ),
        _category(
            repo_root,
            "integrity_serialized_ordering",
            (
                "src/**/*integrity*.py",
                "src/models/facts.py",
                "tests/test_*integrity*.py",
                "tests/test_calendar_integrity.py",
            ),
        ),
        _category(
            repo_root,
            "public_import_surfaces",
            ("src/**/__init__.py", "execution/__init__.py", "tests/test_*import*.py"),
        ),
        _category(
            repo_root, "dcf_formula_cell_receipts", ("execution/*dcf*.py", "tests/test_*dcf*.py")
        ),
        _category(
            repo_root,
            "population_dry_run_apply_receipts",
            ("execution/*population*.py", "tests/test_*population*.py"),
        ),
        _category(
            repo_root,
            "report_dashboard_goldens",
            (
                "src/report/**/*.py",
                "src/dashboard/**/*.py",
                "src/pipeline/*dashboard*.py",
                "tests/golden/workspace/**/*",
                "tests/test_workspace_golden.py",
                "tests/test_dashboard*.py",
            ),
        ),
    ]
    return CompatibilityEvidence(
        baseline_revision=baseline_revision,
        current_revision=current_revision,
        source_sha256=_aggregate([*current_items, *golden_items]),
        baseline_sha256=_aggregate(baseline_items),
        legacy_route_golden=golden,
        entrypoint_parity=parity,
        golden_count=len(golden),
        entrypoint_count=len(parity),
        categories=categories,
        checklist_sha256=_sha256(CHECKLIST.encode("utf-8")),
        hold=any(category.status == "HOLD" for category in categories),
    )
