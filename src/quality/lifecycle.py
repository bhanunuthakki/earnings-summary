"""Build a complete, evidence-bound operational lifecycle inventory.

The inventory answers two separate questions: which operational surfaces exist,
and what lifecycle each surface currently has.  Candidate discovery is kept
independent from materialisation so an omitted record cannot make its own
coverage calculation pass.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Literal, NamedTuple, TypedDict

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from .reachability import GraphEdge, ReachabilityGraph, build_graph

SCHEMA_VERSION = "bha-119.v5"
MAX_STDOUT_BYTES = 100_000

Disposition = Literal[
    "scheduled",
    "service",
    "ui-reachable",
    "manual-supported",
    "internal-delegate",
    "dormant-until",
    "one-shot-completed",
    "compatibility-tombstone",
    "retire",
]
Surface = Literal[
    "python_module",
    "flask_route",
    "scheduled_task",
    "wrapper",
    "service",
    "reconstruction",
    "registry",
]


class LifecycleError(RuntimeError):
    """Raised when authoritative lifecycle evidence cannot be validated."""


class CandidateKey(NamedTuple):
    path: str
    kind: Surface
    identifier: str


class LifecycleEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    line: int
    kind: Surface
    identifier: str
    evidence: str
    fingerprint: str
    disposition: Disposition
    classification_basis: str
    rationale: str
    targets: tuple[str, ...] = ()
    methods: tuple[str, ...] = ()
    endpoint: str | None = None
    # These are deliberately explicit rather than inferred from names.  The
    # fields form the portable receipt consumed by the quality ratchet.
    owner_evidence: str | None = None
    invocation_evidence: str | None = None
    incoming_edge: str | None = None
    sealed_completion_evidence: str | None = None
    tombstone_consumer: str | None = None
    tombstone_expiry: str | None = None
    dormant_owner: str | None = None
    dormant_activation: str | None = None
    dormant_review: str | None = None
    dormant_policy_evidence: str | None = None
    retirement_evidence: tuple[str, ...] = ()

    @property
    def key(self) -> CandidateKey:
        return CandidateKey(self.path, self.kind, self.identifier)

    @model_validator(mode="after")
    def validate_disposition_evidence(self) -> LifecycleEntry:
        if self.disposition == "manual-supported" and (
            not self.owner_evidence
            or not self.owner_evidence.startswith(("canonical:", "runbook:"))
            or not self.invocation_evidence
        ):
            raise ValueError(
                "manual-supported requires canonical/runbook owner and invocation evidence"
            )
        if self.disposition == "internal-delegate" and not self.incoming_edge:
            raise ValueError("internal-delegate requires incoming typed edge evidence")
        if self.disposition == "one-shot-completed" and (
            not self.sealed_completion_evidence
            or not self.sealed_completion_evidence.startswith("sealed:")
        ):
            raise ValueError("one-shot-completed requires sealed completion evidence")
        if self.disposition == "compatibility-tombstone":
            if not self.tombstone_consumer or not self.tombstone_expiry:
                raise ValueError("compatibility-tombstone requires consumer and expiry")
            if self.tombstone_consumer.lower() in {"none", "unknown", "n/a"}:
                raise ValueError("compatibility-tombstone requires a named consumer")
            _require_current_iso_date(self.tombstone_expiry, field="tombstone_expiry")
        if self.disposition == "dormant-until" and not all(
            (
                self.dormant_owner,
                self.dormant_activation,
                self.dormant_review,
                self.dormant_policy_evidence,
            )
        ):
            raise ValueError(
                "dormant-until requires owner, activation, review, and policy evidence"
            )
        if self.disposition == "dormant-until":
            assert self.dormant_review is not None
            _require_current_iso_date(self.dormant_review, field="dormant_review")
        if self.disposition == "retire":
            required = {
                "no-incoming-runtime-edges",
                "no-route-or-ui-surface",
                "no-scheduler-or-service-owner",
                "no-registry-or-reconstruction-contract",
                "behavioral-suite-pass",
            }
            observed = {item.split(":", 1)[0] for item in self.retirement_evidence}
            if not required.issubset(observed):
                raise ValueError("retire requires all five deletion-proof evidence classes")
        return self


class _LifecycleFields(TypedDict, total=False):
    owner_evidence: str | None
    invocation_evidence: str | None
    incoming_edge: str | None
    sealed_completion_evidence: str | None
    tombstone_consumer: str | None
    tombstone_expiry: str | None
    dormant_owner: str | None
    dormant_activation: str | None
    dormant_review: str | None
    dormant_policy_evidence: str | None
    retirement_evidence: tuple[str, ...]


class LifecycleInventory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    status: Literal["PASS", "HOLD"]
    entries: tuple[LifecycleEntry, ...]
    counts: dict[str, int]
    surface_counts: dict[str, int]
    tracked_tree_hash: str
    revision: str
    worktree_dirty: bool
    reachability_graph_hash: str
    graph_parser: dict[str, str]
    coverage: dict[str, int]
    omissions: tuple[str, ...]
    extras: tuple[str, ...]
    violations: tuple[str, ...]


class ScheduledTaskRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    task_name: str
    xml: str
    wrapper: str


class ScheduledTaskManifest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: int
    tasks: tuple[ScheduledTaskRecord, ...]


class DormantPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["bha-119.dormant-policy/v1"]
    owner_evidence: str
    authorization_evidence: str
    activation_evidence: str
    review_on: str
    path_prefixes: tuple[str, ...]
    exact_paths: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_policy(self) -> DormantPolicy:
        if not self.owner_evidence.startswith("linear:"):
            raise ValueError("dormant policy owner must be a Linear issue")
        if not self.authorization_evidence.strip() or not self.activation_evidence.strip():
            raise ValueError("dormant policy requires authorization and activation evidence")
        _require_current_iso_date(self.review_on, field="review_on")
        if not self.path_prefixes and not self.exact_paths:
            raise ValueError("dormant policy scope is empty")
        return self

    def covers(self, path: str) -> bool:
        return path in self.exact_paths or path.startswith(self.path_prefixes)


def _require_current_iso_date(value: str, *, field: str) -> None:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date") from exc
    if parsed < date.today():
        raise ValueError(f"{field} is expired")


_PYTHON_ROOTS = ("execution/", "cron/", "scripts/", ".github/scripts/")
_SOURCE_ROOT = "src/"
_WRAPPER_SUFFIXES = (".bat", ".cmd", ".ps1", ".sh")
_CLI_TEXT = re.compile(r"if\s+__name__\s*==|ArgumentParser\s*\(|typer\.", re.MULTILINE)
_ROUTE_TEXT = re.compile(
    r"^\s*@\w+(?:\.\w+)*\.(?:route|get|post|put|patch|delete)\s*\(", re.MULTILINE
)
_PY_TARGET = re.compile(
    r"(?:python(?:3)?|py)(?:\.exe)?\s+(?:-u\s+)?(?:-m\s+)?([\w./\\-]+(?:\.py)?)", re.IGNORECASE
)
_OPERATIONAL_PY_PATH = re.compile(
    r"\b((?:execution|cron)[\\/][A-Za-z0-9_./\\-]+\.py)\b", re.IGNORECASE
)
_WRAPPER_REFERENCE = re.compile(
    r"\b(cron[\\/][A-Za-z0-9_.\\/-]+\.(?:bat|cmd|ps1|sh))\b", re.IGNORECASE
)
_ONE_SHOT_TOKENS = frozenset(
    {"backfill", "bootstrap", "migrate", "migration", "upgrade", "seed", "populate"}
)
_HTTP_DECORATORS = frozenset({"route", "get", "post", "put", "patch", "delete"})
REGISTRY_AUTHORITIES: tuple[str, ...] = (
    "src/ask/engine.py",
    "src/ask/sealed_retrieval.py",
    "src/compute/metrics_engine/registry.py",
    "src/dispatch_registry.py",
    "src/document_table_extractor.py",
    "src/etf_sources/issuer_registry.py",
    "src/evals/run_registry.py",
    "src/ir_pipeline/authority_capture.py",
    "src/ir_pipeline/home_authority_registry.py",
    "src/ir_uploads.py",
    "src/issuer_registry.py",
    "src/llm/cli.py",
    "src/llm/prompt_registry.py",
    "src/macro_regime_playbook.py",
    "src/operations/registry.py",
    "src/pipeline/operations_panel.py",
    "src/pipeline/source_policy.py",
    "src/provenance/integrity_audit.py",
    "src/provenance/issuer_registry.py",
    "src/provenance/reporting_entity_registry.py",
    "src/quality/performance.py",
    "src/runtime/service_registry.py",
    "src/sources/registry.py",
    "src/triggers/registry.py",
    "src/ui/design_registry.py",
    "src/user_state/registry.py",
)
_SERVICE_TARGETS: dict[str, tuple[str, ...]] = {
    "es-dashboard": ("start_comments_server.bat", "execution/comments_server.py"),
    "es-poller": ("cron/run_capture_poller.bat", "execution/capture_poller.py"),
}


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _worktree_paths(root: Path) -> list[str]:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LifecycleError(f"cannot enumerate worktree: {exc}") from exc
    return sorted(path for path in result.stdout.decode().split("\0") if path)


def _source_line(root: Path, path: str, line: int) -> str:
    lines = _read_text(root / path).splitlines()
    if line < 1 or line > len(lines):
        raise LifecycleError(f"invalid evidence line {path}:{line}")
    return lines[line - 1].strip()


def _fingerprint(path: str, line: int, evidence: str) -> str:
    payload = f"{path}:{line}:{evidence.strip()}".encode()
    return hashlib.sha256(payload).hexdigest()


def _first_cli_line(text: str) -> int | None:
    match = _CLI_TEXT.search(text)
    return text.count("\n", 0, match.start()) + 1 if match else None


def _has_main_guard(text: str) -> bool:
    """Return whether a module exposes a Python ``-m``/script entrypoint."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        # Keep malformed source in the candidate universe so parse failures do
        # not disappear from the lifecycle receipt.
        return bool(re.search(r"if\s+__name__\s*==\s*['\"]__main__['\"]", text))
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            continue
        comparison = node.test
        if len(comparison.ops) != 1 or not isinstance(comparison.ops[0], ast.Eq):
            continue
        if len(comparison.comparators) != 1:
            continue
        left, right = comparison.left, comparison.comparators[0]
        if (
            isinstance(left, ast.Name)
            and left.id == "__name__"
            and isinstance(right, ast.Constant)
            and right.value == "__main__"
        ) or (
            isinstance(right, ast.Name)
            and right.id == "__name__"
            and isinstance(left, ast.Constant)
            and left.value == "__main__"
        ):
            return True
    return False


def _is_python_candidate(path: str, text: str) -> bool:
    if not path.endswith(".py") or not path.startswith(_PYTHON_ROOTS):
        return path.startswith(_SOURCE_ROOT) and path.endswith(".py") and _has_main_guard(text)
    return True


def _is_wrapper(path: str) -> bool:
    return (
        path == "Makefile"
        or path.endswith(_WRAPPER_SUFFIXES)
        or (path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")))
    )


def _graph_bytes(graph: ReachabilityGraph) -> bytes:
    return (graph.model_dump_json(indent=2) + "\n").encode()


def _load_graph(root: Path, *, allow_missing_graph: bool = False) -> tuple[ReachabilityGraph, str]:
    graph_path = root / ".tmp/quality/reachability-check.json"
    if not graph_path.is_file():
        if not allow_missing_graph:
            raise LifecycleError("typed reachability graph is missing")
        graph = build_graph(root)
        if graph.hold:
            raise LifecycleError("fresh current-worktree reachability graph is on HOLD")
        return graph, hashlib.sha256(_graph_bytes(graph)).hexdigest()
    try:
        graph = ReachabilityGraph.model_validate_json(graph_path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise LifecycleError(f"invalid typed reachability graph: {exc}") from exc
    if graph.hold:
        raise LifecycleError("typed reachability graph is on HOLD")
    fresh = build_graph(root)
    if fresh.hold:
        raise LifecycleError("fresh current-worktree reachability graph is on HOLD")
    if graph.model_dump(mode="json") != fresh.model_dump(mode="json"):
        raise LifecycleError("typed reachability graph is stale for the current worktree")
    return graph, hashlib.sha256(graph_path.read_bytes()).hexdigest()


def _load_task_manifest(root: Path) -> ScheduledTaskManifest:
    path = root / "cron/task_manifest.json"
    if not path.is_file():
        raise LifecycleError("canonical scheduled-task manifest is missing")
    try:
        return ScheduledTaskManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise LifecycleError(f"invalid scheduled-task manifest: {exc}") from exc


def _load_dormant_policy(root: Path) -> tuple[DormantPolicy, str]:
    relative = "docs/quality/lifecycle-dormant-policy.json"
    path = root / relative
    if not path.is_file():
        raise LifecycleError("dormant lifecycle policy is missing")
    try:
        raw = path.read_bytes()
        policy = DormantPolicy.model_validate_json(raw)
    except (OSError, ValidationError, ValueError) as exc:
        raise LifecycleError(f"invalid dormant lifecycle policy: {exc}") from exc
    return policy, f"policy:{relative}#{hashlib.sha256(raw).hexdigest()}"


def _literal(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _decorator_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _route_entries(root: Path, path: str, text: str, violations: list[str]) -> list[LifecycleEntry]:
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as exc:
        if _ROUTE_TEXT.search(text):
            violations.append(f"route syntax could not be parsed: {path}:{exc.lineno or 1}")
        return []
    result: list[LifecycleEntry] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            name = _decorator_name(decorator.func)
            if name not in _HTTP_DECORATORS:
                continue
            rule = _literal(decorator.args[0]) if decorator.args else None
            if rule is None:
                violations.append(f"non-literal route rule: {path}:{decorator.lineno}")
                continue
            methods: tuple[str, ...]
            if name == "route":
                method_values: list[str] = []
                for keyword in decorator.keywords:
                    if keyword.arg != "methods" or not isinstance(
                        keyword.value, (ast.List, ast.Tuple)
                    ):
                        continue
                    method_values = [
                        value
                        for item in keyword.value.elts
                        if (value := _literal(item)) is not None
                    ]
                methods = tuple(sorted(value.upper() for value in (method_values or ["GET"])))
            else:
                methods = (name.upper(),)
            evidence = _source_line(root, path, decorator.lineno)
            identifier = f"{','.join(methods)} {rule} {node.name}"
            result.append(
                LifecycleEntry(
                    path=path,
                    line=decorator.lineno,
                    kind="flask_route",
                    identifier=identifier,
                    evidence=evidence,
                    fingerprint=_fingerprint(path, decorator.lineno, evidence),
                    disposition="ui-reachable",
                    classification_basis="production_route",
                    rationale="Literal production Flask route exposed through an HTTP decorator.",
                    targets=(rule,),
                    methods=methods,
                    endpoint=node.name,
                )
            )
    return result


def _managed_service_entries(
    root: Path, path: str, text: str, violations: list[str]
) -> list[LifecycleEntry]:
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as exc:
        if "ManagedService(" in text:
            violations.append(
                f"managed service registry could not be parsed: {path}:{exc.lineno or 1}"
            )
        return []
    entries: list[LifecycleEntry] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        function_name = function.id if isinstance(function, ast.Name) else _decorator_name(function)
        if function_name != "ManagedService":
            continue
        values = {item.arg: _literal(item.value) for item in node.keywords if item.arg}
        name = values.get("name")
        if not name:
            violations.append(f"managed service has non-literal name: {path}:{node.lineno}")
            continue
        evidence = _source_line(root, path, node.lineno)
        entries.append(
            LifecycleEntry(
                path=path,
                line=node.lineno,
                kind="service",
                identifier=name,
                evidence=evidence,
                fingerprint=_fingerprint(path, node.lineno, evidence),
                disposition="service",
                classification_basis="managed_service_registry",
                rationale="Canonical ManagedService declaration owns this runtime service.",
                targets=_SERVICE_TARGETS.get(name, (name,)),
            )
        )
    return entries


def _scheduled_targets(
    root: Path, paths: Iterable[str], tasks: Iterable[ScheduledTaskRecord]
) -> tuple[set[str], set[str]]:
    """Follow canonical task wrappers to their Python and wrapper targets."""
    available = set(paths)
    wrappers = {f"cron/{task.wrapper}" for task in tasks}
    queue = list(wrappers)
    python_targets: set[str] = set()
    while queue:
        path = queue.pop()
        candidate = Path(path)
        resolved_root = root.resolve()
        resolved = (resolved_root / candidate).resolve()
        if (
            candidate.is_absolute()
            or not resolved.is_relative_to(resolved_root)
            or path not in available
            or not resolved.is_file()
        ):
            continue
        raw_text = _read_text(resolved)
        executable_lines: list[str] = []
        for line in raw_text.splitlines():
            stripped = line.lstrip()
            lowered = stripped.lower()
            if lowered.startswith(("#", "::", "rem ")):
                continue
            executable_lines.append(line)
        normalized = "\n".join(executable_lines).replace("\\", "/")
        for match in _OPERATIONAL_PY_PATH.finditer(normalized):
            python_targets.add(match.group(1).replace("\\", "/"))
        for match in _PY_TARGET.finditer(normalized):
            target = match.group(1).replace("\\", "/")
            if target.startswith(("execution/", "cron/")):
                python_targets.add(target)
        for match in _WRAPPER_REFERENCE.finditer(normalized):
            target = match.group(1).replace("\\", "/")
            if target not in wrappers:
                wrappers.add(target)
                queue.append(target)
    return python_targets, wrappers


def _registry_symbols(text: str, path: str) -> tuple[str, ...]:
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as exc:
        raise LifecycleError(
            f"registry authority could not be parsed: {path}:{exc.lineno or 1}"
        ) from exc
    symbols: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                symbols.add(node.name)
        elif isinstance(node, ast.Assign):
            symbols.update(
                target.id
                for target in node.targets
                if isinstance(target, ast.Name)
                and (
                    not target.id.startswith("_")
                    or "REGISTRY" in target.id
                    or target.id == "LLM_MODELS"
                )
            )
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and (
                not node.target.id.startswith("_")
                or "REGISTRY" in node.target.id
                or node.target.id == "LLM_MODELS"
            )
        ):
            symbols.add(node.target.id)
    return tuple(sorted(symbols))


def _registry_entry(
    root: Path,
    path: str,
    dormant_policy: DormantPolicy,
    dormant_policy_evidence: str,
    incoming_edge: str | None = None,
) -> LifecycleEntry:
    text = _read_text(root / path)
    lines = text.splitlines()
    line = next((index for index, value in enumerate(lines, 1) if value.strip()), 1)
    evidence = _source_line(root, path, line)
    active = incoming_edge is not None
    return LifecycleEntry(
        path=path,
        line=line,
        kind="registry",
        identifier=path,
        evidence=evidence,
        fingerprint=_fingerprint(path, line, evidence),
        disposition="internal-delegate" if active else "dormant-until",
        classification_basis=("typed_incoming_edge" if active else "time_bounded_owner_review"),
        rationale=(
            "A verified typed edge retains this catalogued registry authority."
            if active
            else "Unreferenced registry authority is retained for owner review, not claimed live."
        ),
        targets=_registry_symbols(text, path),
        incoming_edge=incoming_edge,
        dormant_owner=None if active else dormant_policy.owner_evidence,
        dormant_activation=None if active else dormant_policy.activation_evidence,
        dormant_review=None if active else dormant_policy.review_on,
        dormant_policy_evidence=None if active else dormant_policy_evidence,
    )


def _python_disposition(
    path: str,
    text: str,
    scheduled: set[str],
    service_targets: set[str],
    incoming_edge: str | None,
) -> Disposition:
    if path in service_targets:
        return "service"
    if path in scheduled:
        return "scheduled"
    if re.search(r"lifecycle:\s*tombstone\b", text, re.IGNORECASE):
        return "compatibility-tombstone"
    if re.search(r"lifecycle:\s*dormant\b", text, re.IGNORECASE):
        return "dormant-until"
    tokens = set(re.split(r"[^a-z0-9]+", path.lower()))
    if tokens & _ONE_SHOT_TOKENS and re.search(r"lifecycle:\s*completion=sealed:", text, re.I):
        return "one-shot-completed"
    if re.search(r"lifecycle:\s*owner=(?:canonical|runbook):\S+", text, re.I) and re.search(
        r"lifecycle:\s*invocation=\S+", text, re.I
    ):
        return "manual-supported"
    if incoming_edge is not None:
        return "internal-delegate"
    return "dormant-until"


def _python_rationale(disposition: Disposition, text: str) -> tuple[str, str]:
    if disposition == "internal-delegate":
        return "typed_incoming_edge", "A verified typed edge retains this internal delegate."
    if disposition == "service":
        return "managed_service_target", "The managed-service registry launches this entrypoint."
    if disposition == "scheduled":
        return (
            "scheduled_wrapper_reference",
            "A canonical cron/task wrapper launches this entrypoint.",
        )
    if disposition in {"compatibility-tombstone", "dormant-until"}:
        if disposition == "dormant-until" and "lifecycle: dormant" not in text.lower():
            return (
                "time_bounded_owner_review",
                "Unlinked operation is retained but dormant under the BHA-119 owner mandate until explicit activation or review.",
            )
        return (
            "explicit_lifecycle_annotation",
            f"Source contains an explicit {disposition} lifecycle annotation.",
        )
    if disposition == "one-shot-completed":
        return (
            "one_shot_operation_name",
            "Executable name identifies a migration, backfill, bootstrap, seed, or population operation.",
        )
    return (
        "unlinked_cli_surface",
        "Top-level CLI has no scheduler or managed-service linkage and remains operator-invoked.",
    )


_EVIDENCE = re.compile(
    r"(?im)^\s*#\s*lifecycle:\s*(owner|invocation|completion|consumer|expiry|activation|review)\s*=\s*(\S+)\s*$"
)


def _lifecycle_evidence(text: str) -> dict[str, str]:
    """Read only explicit, line-oriented lifecycle receipts from source."""
    values: dict[str, str] = {}
    for key, value in _EVIDENCE.findall(text):
        if key in values:
            raise LifecycleError(f"duplicate lifecycle evidence field: {key}")
        values[key] = value
    return values


def _owned_file(root: Path, relative: str, *, evidence: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute():
        raise LifecycleError(f"{evidence} path must be repository-relative: {relative}")
    resolved_root = root.resolve()
    resolved = (resolved_root / candidate).resolve()
    if not resolved.is_relative_to(resolved_root) or not resolved.is_file():
        raise LifecycleError(f"{evidence} is not a repository file: {relative}")
    return resolved


def _validate_directive_owner(root: Path, owner: str) -> None:
    owner_class, owner_path = owner.split(":", 1)
    _owned_file(root, owner_path, evidence="manual lifecycle owner")
    manifest_path = root / "directives/directive_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entry = manifest["directives"][owner_path]
        actual_class = entry["class"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise LifecycleError(
            f"manual lifecycle owner is absent from directive manifest: {owner_path}"
        ) from exc
    if actual_class != owner_class:
        raise LifecycleError(
            f"manual lifecycle owner class mismatch: expected {owner_class}, found {actual_class}"
        )


def lifecycle_evidence_fields(
    *,
    path: str,
    text: str,
    disposition: Disposition,
    incoming_edge: str | None = None,
    root: Path | None = None,
    dormant_policy: DormantPolicy | None = None,
    dormant_policy_evidence: str | None = None,
) -> _LifecycleFields:
    values = _lifecycle_evidence(text)
    owner = values.get("owner")
    invocation = values.get("invocation")
    completion = values.get("completion")
    if disposition == "manual-supported" and (
        not owner or not owner.startswith(("canonical:", "runbook:")) or not invocation
    ):
        raise LifecycleError(
            f"manual lifecycle requires canonical/runbook owner and invocation evidence: {path}"
        )
    if owner and root is not None and owner.startswith(("canonical:", "runbook:")):
        _validate_directive_owner(root, owner)
    if disposition == "internal-delegate" and incoming_edge is None:
        raise LifecycleError(f"internal lifecycle requires a verified incoming typed edge: {path}")
    if disposition == "one-shot-completed" and (
        not completion or not completion.startswith("sealed:")
    ):
        raise LifecycleError(f"one-shot lifecycle requires sealed completion evidence: {path}")
    if completion and root is not None and completion.startswith("sealed:"):
        receipt_path = completion.split(":", 1)[1]
        receipt_file = _owned_file(
            root, receipt_path, evidence="one-shot sealed completion receipt"
        )
        try:
            receipt_payload = json.loads(receipt_file.read_text(encoding="utf-8"))
            receipt_status = receipt_payload.get("status")
            receipt_sealed = receipt_payload.get("sealed")
        except (OSError, AttributeError, json.JSONDecodeError) as exc:
            raise LifecycleError(
                f"one-shot completion receipt is not typed JSON: {receipt_path}"
            ) from exc
        if (
            receipt_status not in {"PASS", "complete", "completed", "sealed"}
            and receipt_sealed is not True
        ):
            raise LifecycleError(f"one-shot completion receipt is not terminal: {receipt_path}")
    if disposition == "compatibility-tombstone" and (
        not values.get("consumer") or not values.get("expiry")
    ):
        raise LifecycleError(f"tombstone lifecycle requires consumer and expiry evidence: {path}")
    if disposition == "compatibility-tombstone":
        if values["consumer"].lower() in {"none", "unknown", "n/a"}:
            raise LifecycleError(f"tombstone lifecycle requires a named consumer: {path}")
        try:
            _require_current_iso_date(values["expiry"], field="expiry")
        except ValueError as exc:
            raise LifecycleError(f"invalid tombstone lifecycle evidence for {path}: {exc}") from exc
    if disposition == "dormant-until" and not all(
        values.get(key) for key in ("owner", "activation", "review")
    ):
        if "lifecycle: dormant" in text.lower():
            raise LifecycleError(
                f"dormant lifecycle requires owner, activation, and review evidence: {path}"
            )
        if dormant_policy is None or not dormant_policy.covers(path):
            raise LifecycleError(f"dormant lifecycle lacks explicit policy coverage: {path}")
        values.update(
            owner=dormant_policy.owner_evidence,
            activation=dormant_policy.activation_evidence,
            review=dormant_policy.review_on,
        )
    elif disposition == "dormant-until":
        explicit_owner = values["owner"]
        if not explicit_owner.startswith(("linear:", "canonical:", "runbook:")):
            raise LifecycleError(f"dormant lifecycle owner is not authoritative: {path}")
        if (
            dormant_policy is not None
            and explicit_owner.startswith("linear:")
            and explicit_owner != dormant_policy.owner_evidence
        ):
            raise LifecycleError(f"dormant lifecycle owner conflicts with policy: {path}")
        if root is not None and explicit_owner.startswith(("canonical:", "runbook:")):
            _validate_directive_owner(root, explicit_owner)
        dormant_policy_evidence = f"source:{path}"
    if disposition == "dormant-until":
        try:
            _require_current_iso_date(values["review"], field="review")
        except ValueError as exc:
            raise LifecycleError(f"invalid dormant lifecycle evidence for {path}: {exc}") from exc
    return {
        "owner_evidence": owner,
        "invocation_evidence": invocation,
        "incoming_edge": incoming_edge,
        "sealed_completion_evidence": completion,
        "tombstone_consumer": values.get("consumer"),
        "tombstone_expiry": values.get("expiry"),
        "dormant_owner": values.get("owner"),
        "dormant_activation": values.get("activation"),
        "dormant_review": values.get("review"),
        "dormant_policy_evidence": dormant_policy_evidence,
    }


def _graph_entry(root: Path, edge: GraphEdge) -> LifecycleEntry:
    if edge.line is None:
        raise LifecycleError(f"{edge.kind} edge lacks source line: {edge.source} -> {edge.target}")
    evidence = _source_line(root, edge.source, edge.line)
    kind: Surface = "reconstruction" if edge.kind == "reconstruction" else "registry"
    return LifecycleEntry(
        path=edge.source,
        line=edge.line,
        kind=kind,
        identifier=f"{edge.target}@{edge.line}",
        evidence=evidence,
        fingerprint=_fingerprint(edge.source, edge.line, evidence),
        disposition="internal-delegate",
        classification_basis="typed_reachability_edge",
        rationale=f"Typed {edge.kind} edge retains this operational dependency.",
        targets=(edge.target,),
        incoming_edge=f"{edge.source}:{edge.line}:{edge.kind}",
    )


_RUNTIME_EDGE_SOURCE_PREFIXES = ("src/", "execution/", "cron/", "scripts/", ".github/")


def _is_runtime_edge_source(path: str) -> bool:
    return (
        path.startswith(_RUNTIME_EDGE_SOURCE_PREFIXES)
        or path == "Makefile"
        or path.endswith((*_WRAPPER_SUFFIXES, ".service"))
    )


def _runtime_incoming_edge(graph: ReachabilityGraph, path: str) -> str | None:
    edge = next(
        (
            candidate
            for candidate in graph.edges
            if candidate.target.replace("\\", "/") == path
            and candidate.line is not None
            and not candidate.unknown
            and _is_runtime_edge_source(candidate.source)
            and (
                candidate.kind not in {"unknown", "directive", "reconstruction"}
                or (
                    candidate.kind == "unknown"
                    and candidate.reviewed_disposition == "internal_python_target"
                )
            )
        ),
        None,
    )
    if edge is None:
        return None
    return f"{edge.source}:{edge.line}:{edge.kind}"


def _expected_candidates(
    root: Path,
    paths: list[str],
    graph: ReachabilityGraph,
    task_by_xml: dict[str, ScheduledTaskRecord],
) -> tuple[set[CandidateKey], list[str]]:
    """Find candidates with deliberately broad text/graph scans."""
    expected: set[CandidateKey] = set()
    violations: list[str] = []
    for path in paths:
        absolute = root / path
        if not absolute.is_file():
            continue
        text = _read_text(absolute)
        if _is_python_candidate(path, text):
            expected.add(CandidateKey(path, "python_module", path))
        if path.endswith(".py") and path.startswith(("src/", "execution/", "cron/", "scripts/")):
            for match in _ROUTE_TEXT.finditer(text):
                decorator_at = text.find("@", match.start(), match.end())
                line = text.count("\n", 0, decorator_at) + 1
                try:
                    parsed = ast.parse(text, filename=path)
                except SyntaxError:
                    violations.append(f"route candidate is unparseable: {path}:{line}")
                    continue
                identities = {
                    (
                        _literal(decorator.args[0]),
                        node.name,
                        _decorator_name(decorator.func),
                        tuple(
                            sorted(
                                value.upper()
                                for keyword in decorator.keywords
                                if keyword.arg == "methods"
                                and isinstance(keyword.value, (ast.List, ast.Tuple))
                                for item in keyword.value.elts
                                if (value := _literal(item)) is not None
                            )
                        ),
                    )
                    for node in ast.walk(parsed)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    for decorator in node.decorator_list
                    if isinstance(decorator, ast.Call)
                    and decorator.lineno == line
                    and _decorator_name(decorator.func) in _HTTP_DECORATORS
                    and decorator.args
                }
                literal_identities = {
                    f"{','.join(methods or (('GET' if decorator_name == 'route' else (decorator_name or 'route').upper()),))} {rule} {endpoint}"
                    for rule, endpoint, decorator_name, methods in identities
                    if rule is not None
                }
                if not literal_identities:
                    violations.append(f"route candidate lacks literal identity: {path}:{line}")
                expected.update(
                    CandidateKey(path, "flask_route", identity) for identity in literal_identities
                )
        if path.endswith(".task.xml"):
            task = task_by_xml.get(Path(path).name)
            identifier = task.task_name if task else path
            expected.add(CandidateKey(path, "scheduled_task", identifier))
        if _is_wrapper(path):
            expected.add(CandidateKey(path, "wrapper", path))
        if path.endswith(".service"):
            expected.add(CandidateKey(path, "service", path))
        if "ManagedService(" in text and path == "src/runtime/service_registry.py":
            for match in re.finditer(r"name\s*=\s*['\"]([^'\"]+)['\"]", text):
                expected.add(CandidateKey(path, "service", match.group(1)))
    for edge in graph.edges:
        if edge.kind in {"reconstruction", "registry"} and not edge.unknown:
            edge_surface: Surface = (
                "reconstruction" if edge.kind == "reconstruction" else "registry"
            )
            expected.add(CandidateKey(edge.source, edge_surface, f"{edge.target}@{edge.line}"))
    discovered_registries = {
        path
        for path in paths
        if path.startswith("src/")
        and (
            Path(path).name == "registry.py"
            or Path(path).name.endswith("_registry.py")
            or path == "src/llm/cli.py"
            or re.search(
                r"(?m)^[A-Z_][A-Z0-9_]*REGISTRY[A-Z0-9_]*(?:\s*:[^=]+)?\s*=",
                _read_text(root / path),
            )
        )
    }
    catalogued_registries = set(REGISTRY_AUTHORITIES)
    if uncatalogued := sorted(discovered_registries - catalogued_registries):
        violations.append(f"uncatalogued registry authorities: {', '.join(uncatalogued)}")
    if missing_registries := sorted(catalogued_registries - set(paths)):
        violations.append(
            f"catalogued registry authorities missing: {', '.join(missing_registries)}"
        )
    expected.update(
        CandidateKey(path, "registry", path) for path in catalogued_registries if path in paths
    )
    return expected, violations


def _tree_hash(root: Path, paths: Iterable[str], graph_hash: str) -> str:
    digest = hashlib.sha256()
    for path in sorted(set(paths)):
        absolute = root / path
        if not absolute.is_file():
            continue
        digest.update(path.encode())
        digest.update(b"\0")
        digest.update(str(absolute.stat().st_mode).encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(absolute.read_bytes()).digest())
    digest.update(b".tmp/quality/reachability-check.json\0")
    digest.update(bytes.fromhex(graph_hash))
    return digest.hexdigest()


def _revision_identity(root: Path) -> tuple[str, bool]:
    try:
        revision = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LifecycleError(f"cannot resolve revision identity: {exc}") from exc
    return revision, dirty


def build_inventory(root: Path, *, allow_missing_graph: bool = False) -> LifecycleInventory:
    root = root.resolve()
    paths = _worktree_paths(root)
    graph, graph_hash = _load_graph(root, allow_missing_graph=allow_missing_graph)
    task_manifest = _load_task_manifest(root)
    dormant_policy, dormant_policy_evidence = _load_dormant_policy(root)
    task_by_xml = {task.xml: task for task in task_manifest.tasks}
    duplicate_task_names = len(task_manifest.tasks) - len(
        {task.task_name for task in task_manifest.tasks}
    )
    duplicate_task_xml = len(task_manifest.tasks) - len(task_by_xml)
    expected, violations = _expected_candidates(root, paths, graph, task_by_xml)
    operational_prefixes = ("src/", "execution/", "cron/", "scripts/", ".github/")
    for diagnostic in graph.diagnostics:
        if diagnostic.path.startswith(operational_prefixes):
            violations.append(
                f"operational reachability {diagnostic.kind}: {diagnostic.path}: {diagnostic.message}"
            )
    xml_files = {Path(path).name for path in paths if path.endswith(".task.xml")}
    manifest_xml = set(task_by_xml)
    if orphan_xml := sorted(xml_files - manifest_xml):
        violations.append(f"scheduled task XML absent from manifest: {', '.join(orphan_xml)}")
    if missing_xml := sorted(manifest_xml - xml_files):
        violations.append(f"scheduled task manifest XML missing: {', '.join(missing_xml)}")
    missing_wrappers = sorted(
        task.wrapper for task in task_manifest.tasks if f"cron/{task.wrapper}" not in paths
    )
    if missing_wrappers:
        violations.append(f"scheduled task wrappers missing: {', '.join(missing_wrappers)}")
    if duplicate_task_names:
        violations.append(f"duplicate scheduled task names: {duplicate_task_names}")
    if duplicate_task_xml:
        violations.append(f"duplicate scheduled task XML entries: {duplicate_task_xml}")
    scheduled, scheduled_wrappers = _scheduled_targets(root, paths, task_manifest.tasks)
    service_registry_path = root / "src/runtime/service_registry.py"
    service_registry_text = (
        _read_text(service_registry_path) if service_registry_path.is_file() else ""
    )
    declared_service_names = {
        match.group(1)
        for match in re.finditer(r"name\s*=\s*['\"]([^'\"]+)['\"]", service_registry_text)
    }
    service_targets = {
        target for name in declared_service_names for target in _SERVICE_TARGETS.get(name, ())
    }
    if missing_service_targets := sorted(service_targets - set(paths)):
        violations.append(f"managed service targets missing: {', '.join(missing_service_targets)}")
    scheduled.update(target for target in service_targets if target.endswith(".py"))
    scheduled_wrappers.update(
        target for target in service_targets if target.endswith(_WRAPPER_SUFFIXES)
    )
    entries: list[LifecycleEntry] = []

    for path in paths:
        absolute = root / path
        if not absolute.is_file():
            continue
        text = _read_text(absolute)
        if _is_python_candidate(path, text):
            line = _first_cli_line(text) or 1
            evidence = _source_line(root, path, line)
            incoming = _runtime_incoming_edge(graph, path)
            disposition = _python_disposition(path, text, scheduled, service_targets, incoming)
            basis, rationale = _python_rationale(disposition, text)
            try:
                lifecycle_fields = lifecycle_evidence_fields(
                    path=path,
                    text=text,
                    disposition=disposition,
                    incoming_edge=incoming,
                    root=root,
                    dormant_policy=dormant_policy,
                    dormant_policy_evidence=dormant_policy_evidence,
                )
            except LifecycleError as exc:
                violations.append(str(exc))
                continue
            entries.append(
                LifecycleEntry(
                    path=path,
                    line=line,
                    kind="python_module",
                    identifier=path,
                    evidence=evidence,
                    fingerprint=_fingerprint(path, line, evidence),
                    disposition=disposition,
                    classification_basis=basis,
                    rationale=rationale,
                    targets=(path,),
                    **lifecycle_fields,
                )
            )
        if path.endswith(".py") and path.startswith(("src/", "execution/", "cron/", "scripts/")):
            entries.extend(_route_entries(root, path, text, violations))
        if path.endswith(".task.xml"):
            first_line = _read_text(absolute).splitlines()
            evidence = first_line[0].strip() if first_line else path
            task = task_by_xml.get(Path(path).name)
            identifier = task.task_name if task else path
            targets = (f"cron/{task.wrapper}",) if task else ()
            entries.append(
                LifecycleEntry(
                    path=path,
                    line=1,
                    kind="scheduled_task",
                    identifier=identifier,
                    evidence=evidence,
                    fingerprint=_fingerprint(path, 1, evidence),
                    disposition="scheduled",
                    classification_basis="scheduled_task_manifest",
                    rationale="Canonical task manifest and matching XML declare this scheduled task.",
                    targets=targets,
                )
            )
        if _is_wrapper(path):
            lines = text.splitlines()
            evidence = lines[0].strip() if lines else path
            disposition: Disposition = (
                "scheduled"
                if path in scheduled_wrappers or path.startswith(".github/workflows/")
                else "dormant-until"
            )
            if disposition == "dormant-until" and not dormant_policy.covers(path):
                violations.append(f"dormant lifecycle policy does not cover wrapper: {path}")
                continue
            entries.append(
                LifecycleEntry(
                    path=path,
                    line=1,
                    kind="wrapper",
                    identifier=path,
                    evidence=evidence,
                    fingerprint=_fingerprint(path, 1, evidence),
                    disposition=disposition,
                    classification_basis=(
                        "scheduler_owned_wrapper"
                        if disposition == "scheduled"
                        else "operator_wrapper"
                    ),
                    rationale=(
                        "Cron or CI owns this executable wrapper."
                        if disposition == "scheduled"
                        else "Executable wrapper is not linked from the canonical task scheduler."
                    ),
                    dormant_owner=(
                        None if disposition == "scheduled" else dormant_policy.owner_evidence
                    ),
                    dormant_activation=(
                        None if disposition == "scheduled" else dormant_policy.activation_evidence
                    ),
                    dormant_review=(
                        None if disposition == "scheduled" else dormant_policy.review_on
                    ),
                    dormant_policy_evidence=(
                        None if disposition == "scheduled" else dormant_policy_evidence
                    ),
                )
            )
        if path.endswith(".service"):
            lines = text.splitlines()
            evidence = lines[0].strip() if lines else path
            entries.append(
                LifecycleEntry(
                    path=path,
                    line=1,
                    kind="service",
                    identifier=path,
                    evidence=evidence,
                    fingerprint=_fingerprint(path, 1, evidence),
                    disposition="service",
                    classification_basis="service_unit_file",
                    rationale="Service unit file declares a continuously managed operation.",
                    targets=(path,),
                )
            )
        if "ManagedService(" in text and path == "src/runtime/service_registry.py":
            entries.extend(_managed_service_entries(root, path, text, violations))

    for edge in graph.edges:
        if edge.kind in {"reconstruction", "registry"} and not edge.unknown:
            try:
                entries.append(_graph_entry(root, edge))
            except LifecycleError as exc:
                violations.append(str(exc))
    for path in REGISTRY_AUTHORITIES:
        if path in paths:
            try:
                incoming = _runtime_incoming_edge(graph, path)
                entries.append(
                    _registry_entry(
                        root,
                        path,
                        dormant_policy,
                        dormant_policy_evidence,
                        incoming,
                    )
                )
            except LifecycleError as exc:
                violations.append(str(exc))

    entries.sort(key=lambda entry: (entry.path, entry.line, entry.kind, entry.identifier))
    actual = {entry.key for entry in entries}
    duplicate_count = len(entries) - len(actual)
    omissions = tuple(sorted(":".join(key) for key in expected - actual))
    extras = tuple(sorted(":".join(key) for key in actual - expected))
    if duplicate_count:
        violations.append(f"duplicate lifecycle identities: {duplicate_count}")
    if omissions:
        violations.append(f"inventory omissions: {len(omissions)}")
    if extras:
        violations.append(f"inventory extras: {len(extras)}")
    if not entries:
        violations.append("no lifecycle evidence discovered")

    disposition_names = (
        "scheduled",
        "service",
        "ui-reachable",
        "manual-supported",
        "internal-delegate",
        "one-shot-completed",
        "compatibility-tombstone",
        "dormant-until",
        "retire",
    )
    surface_names = (
        "python_module",
        "flask_route",
        "scheduled_task",
        "wrapper",
        "service",
        "reconstruction",
        "registry",
    )
    authority_paths = {entry.path for entry in entries}
    authority_paths.add("cron/task_manifest.json")
    revision, worktree_dirty = _revision_identity(root)
    return LifecycleInventory(
        status="HOLD" if violations else "PASS",
        entries=tuple(entries),
        counts={
            name: sum(entry.disposition == name for entry in entries) for name in disposition_names
        },
        surface_counts={
            name: sum(entry.kind == name for entry in entries) for name in surface_names
        },
        tracked_tree_hash=_tree_hash(root, authority_paths, graph_hash),
        revision=revision,
        worktree_dirty=worktree_dirty,
        reachability_graph_hash=graph_hash,
        graph_parser=graph.parser,
        coverage={
            "candidates": len(expected),
            "inventoried": len(actual),
            "omissions": len(omissions),
            "extras": len(extras),
            "duplicates": duplicate_count,
        },
        omissions=omissions,
        extras=extras,
        violations=tuple(sorted(set(violations))),
    )


def validate_inventory(root: Path, persisted: LifecycleInventory) -> tuple[str, ...]:
    """Return exact drift from a persisted receipt; an empty tuple is valid."""
    current = build_inventory(root)
    violations: list[str] = []
    if persisted.schema_version != SCHEMA_VERSION:
        violations.append(f"schema changed: {persisted.schema_version} != {SCHEMA_VERSION}")
    if persisted.tracked_tree_hash != current.tracked_tree_hash:
        violations.append("tracked content fingerprint is stale")
    if persisted.reachability_graph_hash != current.reachability_graph_hash:
        violations.append("reachability graph fingerprint is stale")
    if persisted.status != "PASS" or current.status != "PASS":
        violations.append("persisted and current lifecycle inventories must both PASS")
    volatile = {"revision", "worktree_dirty"}
    if persisted.model_dump(mode="json", exclude=volatile) != current.model_dump(
        mode="json", exclude=volatile
    ):
        violations.append("persisted lifecycle semantics differ from the current inventory")
    return tuple(sorted(set(violations)))


def load_inventory(path: Path) -> LifecycleInventory:
    try:
        return LifecycleInventory.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, json.JSONDecodeError) as exc:
        raise LifecycleError(f"invalid lifecycle inventory {path}: {exc}") from exc
