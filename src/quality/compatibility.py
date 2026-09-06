"""Deterministic compatibility evidence for the Train 0 quality baseline."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import re
import subprocess
import tarfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
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
    implementation_artifacts: list[str]
    verification_artifacts: list[str]
    artifact_sha256: str
    extracted: list[EvidenceRecord]


class EvidenceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    identifier: str
    value: str


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


def _tracked_paths(root: Path, patterns: Sequence[str]) -> set[str]:
    try:
        result = subprocess.run(  # reachability: external-process
            ["git", "-C", str(root), "ls-files", "-z", "--", *patterns],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CompatibilityEvidenceError(
            "git evidence command failed: ls-files -z -- " + " ".join(patterns)
        ) from exc
    try:
        paths = {item.decode("utf-8") for item in result.stdout.split(b"\0") if item}
    except UnicodeDecodeError as exc:
        raise CompatibilityEvidenceError("tracked compatibility path is not UTF-8") from exc
    if any(
        PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts for path in paths
    ):
        raise CompatibilityEvidenceError("tracked compatibility path escapes the repository")
    return paths


def _read_bytes(root: Path, relative: str) -> bytes:
    path = _artifact_path(root, relative)
    try:
        return path.read_bytes()
    except (OSError, RuntimeError) as exc:
        raise CompatibilityEvidenceError(
            f"unable to read compatibility artifact: {relative}"
        ) from exc


def _read_text(root: Path, relative: str) -> str:
    path = _artifact_path(root, relative)
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CompatibilityEvidenceError(
            f"unable to read compatibility artifact: {relative}"
        ) from exc


def _artifact_path(root: Path, relative: str) -> Path:
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise CompatibilityEvidenceError(
            f"unable to read compatibility artifact: {relative}"
        ) from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise CompatibilityEvidenceError(
            f"compatibility artifact escapes the repository: {relative}"
        )
    return candidate


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
        collections = [
            payload_dict[key]
            for key in ("cases", "goldens", "examples", "items")
            if key in payload_dict
        ]
        if len(collections) == 1 and isinstance(collections[0], list):
            return len(cast(list[object], collections[0]))
    return 0


def _goldens(root: Path) -> tuple[list[GoldenReceipt], list[tuple[str, bytes]]]:
    paths = sorted(_tracked_paths(root, ("evals/golden/*.json",)))
    if not paths:
        raise CompatibilityEvidenceError("missing legacy-route golden evidence under evals/golden")
    receipts: list[GoldenReceipt] = []
    raw_items: list[tuple[str, bytes]] = []
    for relative in paths:
        raw = _read_bytes(root, relative)
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
    current_paths = _tracked_paths(root, ("src/*.py", "execution/*.py"))
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
        current = _read_bytes(root, path) if path in current_paths else None
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


def _extract_records(root: Path, name: str, artifacts: list[str]) -> list[EvidenceRecord]:
    records: list[EvidenceRecord] = []
    if name == "flask_url_method_endpoint_map":
        relative = "execution/comments_server.py"
        if (root / relative).exists():
            try:
                tree = ast.parse(_read_text(root, relative))
                for node in ast.walk(tree):
                    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    for decorator in node.decorator_list:
                        if not isinstance(decorator, ast.Call) or not isinstance(
                            decorator.func, ast.Attribute
                        ):
                            continue
                        if decorator.func.attr != "route" or not decorator.args:
                            continue
                        rule = (
                            decorator.args[0].value
                            if isinstance(decorator.args[0], ast.Constant)
                            else None
                        )
                        if not isinstance(rule, str):
                            continue
                        methods = next(
                            (
                                keyword.value
                                for keyword in decorator.keywords
                                if keyword.arg == "methods"
                            ),
                            None,
                        )
                        method_names = ["GET"]
                        if isinstance(methods, (ast.List, ast.Tuple)):
                            parsed_methods = [
                                item.value
                                for item in methods.elts
                                if isinstance(item, ast.Constant) and isinstance(item.value, str)
                            ]
                            if parsed_methods:
                                method_names = sorted(parsed_methods)
                        records.append(
                            EvidenceRecord(
                                kind="flask_route",
                                identifier=rule,
                                value=f"{','.join(method_names)}:{node.name}",
                            )
                        )
            except (SyntaxError, ValueError):
                return []
    elif name == "public_import_surfaces":
        for relative in artifacts:
            if not relative.endswith("__init__.py"):
                continue
            try:
                tree = ast.parse(_read_text(root, relative))
            except (SyntaxError, ValueError):
                continue
            for node in tree.body:
                if (
                    isinstance(node, ast.Assign)
                    and isinstance(node.value, (ast.List, ast.Tuple))
                    and any(
                        isinstance(target, ast.Name) and target.id == "__all__"
                        for target in node.targets
                    )
                ):
                    for item in node.value.elts:
                        if isinstance(item, ast.Constant) and isinstance(item.value, str):
                            records.append(
                                EvidenceRecord(
                                    kind="public_export", identifier=relative, value=item.value
                                )
                            )
    elif name == "population_dry_run_apply_receipts":
        for relative in artifacts:
            if relative.startswith("execution/"):
                text = _read_text(root, relative)
                if "--apply" in text:
                    records.append(
                        EvidenceRecord(kind="population_mode", identifier=relative, value="apply")
                    )
                # The canonical evaluator defaults request.apply to false and
                # emits mode='dry_run'; either spelling proves that branch.
                if "--dry-run" in text or ("request.apply" in text and "dry_run" in text):
                    records.append(
                        EvidenceRecord(kind="population_mode", identifier=relative, value="dry_run")
                    )
    elif name == "dcf_formula_cell_receipts":
        for relative in artifacts:
            text = _read_text(root, relative)
            for line in text.splitlines():
                if "formula" in line.lower() or "cell(" in line.lower() or "!$" in line:
                    records.append(
                        EvidenceRecord(
                            kind="dcf_contract", identifier=relative, value=line.strip()[:240]
                        )
                    )
    elif name == "integrity_serialized_ordering":
        for relative in artifacts:
            text = _read_text(root, relative)
            for line in text.splitlines():
                if any(token in line for token in ("ORDER", "_FIELDS", "to_json", "serialize")):
                    records.append(
                        EvidenceRecord(
                            kind="ordering_contract", identifier=relative, value=line.strip()[:240]
                        )
                    )
    elif name == "report_dashboard_goldens":
        for relative in artifacts:
            if relative.startswith(("tests/golden/", "evals/")):
                raw = _read_bytes(root, relative)
                records.append(
                    EvidenceRecord(
                        kind="golden", identifier=relative, value=f"sha256:{_sha256(raw)}"
                    )
                )
    return sorted(records, key=lambda record: (record.kind, record.identifier, record.value))


def _category(root: Path, name: str, patterns: tuple[str, ...]) -> EvidenceCategory:
    artifacts = sorted(_tracked_paths(root, patterns))
    payload = [(path, _read_bytes(root, path)) for path in artifacts]
    implementation_artifacts = [
        path for path in artifacts if path.startswith(("src/", "execution/"))
    ]
    verification_artifacts = [path for path in artifacts if path.startswith(("tests/", "evals/"))]
    implementation = bool(implementation_artifacts)
    verification = bool(verification_artifacts)
    extracted = _extract_records(root, name, artifacts)
    missing: list[str] = []
    if not implementation:
        missing.append("implementation artifact")
    if not verification:
        missing.append("behavior-pinning test/golden artifact")
    if not extracted:
        missing.append("extracted contract")
    if name == "population_dry_run_apply_receipts":
        modes = {record.value for record in extracted if record.kind == "population_mode"}
        missing.extend(
            f"population {mode} mode" for mode in ("dry_run", "apply") if mode not in modes
        )
    status: Literal["PASS", "HOLD"] = "PASS" if not missing else "HOLD"
    reason = (
        "all required evidence conditions present"
        if status == "PASS"
        else "missing " + ", ".join(missing)
    )
    return EvidenceCategory(
        name=name,
        status=status,
        reason=reason,
        artifacts=artifacts,
        implementation_artifacts=implementation_artifacts,
        verification_artifacts=verification_artifacts,
        artifact_sha256=_aggregate(payload),
        extracted=extracted,
    )


def capture_compatibility_evidence(
    root: str | Path, baseline_revision: str
) -> CompatibilityEvidence:
    repo_root = Path(root).resolve()
    requested_baseline = baseline_revision.strip()
    if not requested_baseline:
        raise CompatibilityEvidenceError("baseline revision is required")
    current_revision = _run(repo_root, ["rev-parse", "HEAD"]).strip()
    resolved_baseline = _run(
        repo_root,
        ["rev-parse", "--verify", "--end-of-options", f"{requested_baseline}^{{commit}}"],
    ).strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", resolved_baseline):
        raise CompatibilityEvidenceError("baseline revision did not resolve to a commit")
    golden, golden_items = _goldens(repo_root)
    parity, current_items, baseline_items = _entrypoints(repo_root, resolved_baseline)
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
        baseline_revision=resolved_baseline.lower(),
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
