"""Candidate discovery and per-entry evidence helpers."""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple, TypedDict, cast

from scheduler_manifest import TaskSpec

from .lifecycle_models import (
    Disposition,
    DormantPolicy,
    LifecycleEntry,
    LifecycleError,
    Surface,
    fingerprint,
    read_text,
    reject_duplicate_keys,
    require_current_iso_date,
    resolve_repo_file,
    source_line,
)
from .reachability import ReachabilityGraph

REGISTRY_AUTHORITIES: tuple[str, ...] = (
    "src/ask/engine.py",
    "src/dispatch_registry.py",
    "src/llm/cli.py",
    "src/llm/prompt_registry.py",
    "src/operations/registry.py",
    "src/runtime/service_registry.py",
    "src/sources/registry.py",
    "src/triggers/registry.py",
)
PYTHON_ROOTS = ("execution/", "cron/", "scripts/", ".github/scripts/")
SRC_ROOT = "src/"
RUNTIME_PREFIXES = ("src/", "execution/", "cron/", "scripts/", ".github/")
WRAPPER_SUFFIXES = (".bat", ".cmd", ".ps1", ".sh")
SERVICE_REGISTRY = "src/runtime/service_registry.py"
_CLI = re.compile(r"if\s+__name__\s*==|ArgumentParser\s*\(|typer\.", re.MULTILINE)
_HTTP = frozenset({"route", "get", "post", "put", "patch", "delete"})
_OP_PY = re.compile(r"\b((?:execution|cron)[\\/][A-Za-z0-9_./\\-]+\.py)\b", re.IGNORECASE)
_WRAP_REF = re.compile(r"\b(cron[\\/][A-Za-z0-9_.\\/-]+\.(?:bat|cmd|ps1|sh))\b", re.IGNORECASE)
_COMMAND_TOKEN = re.compile(r'^\s*(?:call\s+|&\s*)?(?:"([^"]+)"|(\S+))', re.IGNORECASE)
_ARG_TOKEN = re.compile(r'"([^"]*)"|(\S+)')
_ONE_SHOT = frozenset(
    {"backfill", "bootstrap", "migrate", "migration", "upgrade", "seed", "populate"}
)
_EV = re.compile(
    r"(?im)^\s*#\s*lifecycle:\s*(owner|invocation|completion|consumer|expiry|activation|review)\s*=\s*(\S+)\s*$"
)
_REG_ASSIGN = re.compile(r"(?m)^[A-Z_][A-Z0-9_]*REGISTRY[A-Z0-9_]*(?:\s*:[^=]+)?\s*=")


class LifecycleEvidenceFields(TypedDict):
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


class CandidateKey(NamedTuple):
    path: str
    kind: Surface
    identifier: str


def has_main_guard(text: str) -> bool:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return bool(re.search(r"if\s+__name__\s*==\s*['\"]__main__['\"]", text))
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            continue
        if len(node.test.ops) != 1 or not isinstance(node.test.ops[0], ast.Eq):
            continue
        if len(node.test.comparators) != 1:
            continue
        left, right = node.test.left, node.test.comparators[0]
        if (
            isinstance(left, ast.Name)
            and left.id == "__name__"
            and isinstance(right, ast.Constant)
            and right.value == "__main__"
        ):
            return True
        if (
            isinstance(right, ast.Name)
            and right.id == "__name__"
            and isinstance(left, ast.Constant)
            and left.value == "__main__"
        ):
            return True
    return False


def is_python_candidate(path: str, text: str) -> bool:
    if not path.endswith(".py"):
        return False
    if path.startswith(PYTHON_ROOTS):
        return True
    if path.startswith(SRC_ROOT):
        return has_main_guard(text)
    return False


def is_wrapper(path: str) -> bool:
    return (
        path == "Makefile"
        or path.endswith(WRAPPER_SUFFIXES)
        or (path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")))
    )


def _literal(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _parse_route_decorator(
    dec: ast.Call, func_name: str, path: str, violations: list[str]
) -> tuple[str, tuple[str, ...], str] | None:
    """Fail-closed Flask route parsing shared by inventory and candidates."""
    name = dec.func.attr if isinstance(dec.func, ast.Attribute) else None
    if name not in _HTTP:
        return None
    if any(kw.arg is None for kw in dec.keywords):
        violations.append(
            f"non-literal route options include keyword unpacking: {path}:{dec.lineno}"
        )
        return None
    rule = _literal(dec.args[0]) if dec.args else None
    if rule is None:
        violations.append(f"non-literal route rule lacks literal identity: {path}:{dec.lineno}")
        return None
    endpoint = func_name
    endpoint_kws = [kw for kw in dec.keywords if kw.arg == "endpoint"]
    if endpoint_kws:
        if len(endpoint_kws) != 1:
            violations.append(f"non-literal route endpoint: {path}:{dec.lineno}")
            return None
        override = _literal(endpoint_kws[0].value)
        if override is None or override.strip() == "":
            violations.append(f"non-literal route endpoint: {path}:{dec.lineno}")
            return None
        endpoint = override
    methods: tuple[str, ...]
    if name == "route":
        method_kws = [kw for kw in dec.keywords if kw.arg == "methods"]
        if not method_kws:
            methods = ("GET",)
        else:
            if len(method_kws) != 1:
                violations.append(f"non-literal route methods: {path}:{dec.lineno}")
                return None
            raw = method_kws[0].value
            if not isinstance(raw, (ast.List, ast.Tuple)) or not raw.elts:
                violations.append(f"non-literal route methods: {path}:{dec.lineno}")
                return None
            literals: list[str] = []
            for elt in raw.elts:
                val = _literal(elt)
                if val is None or val.strip() == "":
                    violations.append(f"non-literal route methods: {path}:{dec.lineno}")
                    return None
                literals.append(val)
            methods = tuple(sorted(v.strip().upper() for v in literals))
    else:
        methods = (str(name).upper(),)
    return (rule, methods, endpoint)


def route_entries(root: Path, path: str, text: str, violations: list[str]) -> list[LifecycleEntry]:
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as exc:
        violations.append(f"route syntax could not be parsed: {path}:{exc.lineno or 1}")
        return []
    out: list[LifecycleEntry] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            parsed = _parse_route_decorator(dec, node.name, path, violations)
            if parsed is None:
                continue
            rule, methods, endpoint = parsed
            ev = source_line(root, path, dec.lineno)
            ident = f"{','.join(methods)} {rule} {endpoint}"
            out.append(
                LifecycleEntry(
                    path=path,
                    line=dec.lineno,
                    kind="flask_route",
                    identifier=ident,
                    evidence=ev,
                    fingerprint=fingerprint(path, dec.lineno, ev),
                    disposition="ui-reachable",
                    classification_basis="production_route",
                    rationale="Literal Flask route.",
                    targets=(rule,),
                    methods=methods,
                    endpoint=endpoint,
                )
            )
    return out


def _managed_service_names(text: str, path: str, violations: list[str]) -> list[tuple[str, int]]:
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as exc:
        violations.append(f"managed service registry could not be parsed: {path}:{exc.lineno or 1}")
        return []
    spots: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else (node.func.attr if isinstance(node.func, ast.Attribute) else "")
        )
        if fn != "ManagedService":
            continue
        vals = {k.arg: _literal(k.value) for k in node.keywords if k.arg}
        name = vals.get("name")
        if not name:
            violations.append(f"managed service has non-literal name: {path}:{node.lineno}")
            continue
        spots.append((name, node.lineno))
    return spots


def service_entries(
    root: Path, path: str, text: str, violations: list[str]
) -> list[LifecycleEntry]:
    out: list[LifecycleEntry] = []
    for name, lineno in _managed_service_names(text, path, violations):
        ev = source_line(root, path, lineno)
        out.append(
            LifecycleEntry(
                path=path,
                line=lineno,
                kind="service",
                identifier=name,
                evidence=ev,
                fingerprint=fingerprint(path, lineno, ev),
                disposition="service",
                classification_basis="managed_service_registry",
                rationale="ManagedService logical identity.",
                targets=(name,),
            )
        )
    return out


def registry_symbols(text: str, path: str) -> tuple[str, ...]:
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as exc:
        raise LifecycleError(
            f"registry authority could not be parsed: {path}:{exc.lineno or 1}"
        ) from exc
    syms: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                syms.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and (not t.id.startswith("_") or "REGISTRY" in t.id):
                    syms.add(t.id)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and (not node.target.id.startswith("_") or "REGISTRY" in node.target.id)
        ):
            syms.add(node.target.id)
    return tuple(sorted(syms))


def is_runtime_source(path: str) -> bool:
    return (
        path.startswith(RUNTIME_PREFIXES)
        or path == "Makefile"
        or path.endswith((*WRAPPER_SUFFIXES, ".service"))
    )


def runtime_incoming_edge(graph: ReachabilityGraph, path: str) -> str | None:
    norm = path.replace("\\", "/")
    for c in graph.edges:
        if c.target.replace("\\", "/") != norm or c.line is None or c.unknown:
            continue
        if c.source.startswith(("tests/", "instruction_tests/")):
            continue
        if not is_runtime_source(c.source):
            continue
        if c.kind in {"directive", "reconstruction"}:
            continue
        if c.kind == "unknown" and c.reviewed_disposition != "internal_python_target":
            continue
        return f"{c.source}:{c.line}:{c.kind}"
    return None


def _normalize_scheduled_python_target(raw: str) -> str | None:
    if raw.startswith(("execution/", "cron/")):
        return raw if raw.endswith(".py") else f"{raw}.py"
    if raw.startswith(("execution.", "cron.")):
        base = raw[:-3] if raw.endswith(".py") else raw
        parts = base.split(".")
        if any(not part for part in parts):
            return None
        norm = "/".join(parts) + ".py"
        if norm.startswith(("execution/", "cron/")):
            return norm
    return None


def _command_arguments(text: str) -> tuple[str, ...]:
    return tuple((match.group(1) or match.group(2) or "") for match in _ARG_TOKEN.finditer(text))


def _operational_python_path(token: str) -> str | None:
    match = _OP_PY.search(token)
    if match is None:
        return None
    return _normalize_scheduled_python_target(match.group(1).replace("\\", "/"))


def _python_command_targets(arguments: tuple[str, ...]) -> set[str]:
    for index, argument in enumerate(arguments):
        lowered = argument.lower()
        if lowered in {"-c", "--command"}:
            return set()
        if lowered == "-m":
            if index + 1 >= len(arguments):
                return set()
            target = _normalize_scheduled_python_target(arguments[index + 1].replace("\\", "/"))
            return {target} if target is not None else set()
        if lowered.startswith("-m") and len(argument) > 2:
            target = _normalize_scheduled_python_target(argument[2:].replace("\\", "/"))
            return {target} if target is not None else set()
        if argument.startswith("-"):
            continue
        target = _operational_python_path(argument)
        if target is None:
            return set()
        targets = {target}
        if target == "execution/sqlite_bootstrap.py" and index + 1 < len(arguments):
            forwarded = _operational_python_path(arguments[index + 1])
            if forwarded is not None:
                targets.add(forwarded)
        return targets
    return set()


def _scheduled_command_targets(line: str) -> tuple[set[str], str | None]:
    stripped = line.strip()
    lowered = stripped.lstrip("@").lower()
    if not stripped or lowered.startswith(
        ("#", "::", "rem ", ":", "echo ", "echo.", "echo(", "set ", "if ", "goto ", "exit ", "for ")
    ):
        return set(), None
    normalized = stripped.lstrip("@").replace("\\", "/")
    command_match = _COMMAND_TOKEN.match(normalized)
    if command_match is None:
        return set(), None
    command = (command_match.group(1) or command_match.group(2) or "").replace("\\", "/")
    wrapper_match = _WRAP_REF.search(command)
    wrapper = wrapper_match.group(1).replace("\\", "/") if wrapper_match else None
    leaf = command.rsplit("/", 1)[-1].strip("%$").lower()
    python_launcher = (
        re.fullmatch(r"(?:python(?:_exe|3(?:\.\d+)?)?(?:\.exe)?|py(?:\.exe)?)", leaf) is not None
    )
    if wrapper is None and not python_launcher:
        return set(), None
    arguments = _command_arguments(normalized[command_match.end() :])
    targets: set[str] = set()
    if wrapper == "cron/run_python.bat":
        targets = _python_command_targets(arguments[2:])
    if python_launcher:
        targets = _python_command_targets(arguments)
    return targets, wrapper


def scheduled_targets(root: Path, paths: set[str], wrappers: set[str]) -> tuple[set[str], set[str]]:
    py: set[str] = set()
    seen = set(wrappers)
    queue = list(wrappers)
    while queue:
        p = queue.pop()
        if p not in paths:
            continue
        f = root / p
        if not f.is_file():
            continue
        for line in read_text(f).splitlines():
            targets, wrapper = _scheduled_command_targets(line)
            py.update(targets)
            if wrapper is not None and wrapper not in seen:
                seen.add(wrapper)
                queue.append(wrapper)
    return py, seen


def python_disposition(
    path: str, text: str, scheduled: set[str], incoming: str | None
) -> Disposition:
    if path in scheduled:
        return "scheduled"
    if re.search(r"lifecycle:\s*tombstone\b", text, re.I):
        return "compatibility-tombstone"
    if re.search(r"lifecycle:\s*dormant\b", text, re.I):
        return "dormant-until"
    toks = set(re.split(r"[^a-z0-9]+", path.lower()))
    if toks & _ONE_SHOT and re.search(r"lifecycle:\s*completion=sealed:", text, re.I):
        return "one-shot-completed"
    if re.search(r"lifecycle:\s*owner=(?:canonical|runbook):\S+", text, re.I) and re.search(
        r"lifecycle:\s*invocation=\S+", text, re.I
    ):
        return "manual-supported"
    if incoming is not None:
        return "internal-delegate"
    return "dormant-until"


def python_rationale(d: Disposition) -> tuple[str, str]:
    return {
        "internal-delegate": ("typed_incoming_edge", "Verified typed edge retains delegate."),
        "scheduled": ("scheduled_wrapper_reference", "Canonical wrapper launches entrypoint."),
        "compatibility-tombstone": (
            "explicit_lifecycle_annotation",
            "Explicit tombstone annotation.",
        ),
        "dormant-until": (
            "time_bounded_owner_review",
            "Unlinked surface dormant under owner review.",
        ),
        "one-shot-completed": (
            "one_shot_operation_name",
            "One-shot migration/backfill/seed operation.",
        ),
        "manual-supported": ("unlinked_cli_surface", "Operator-invoked CLI with runbook owner."),
    }[d]


def parse_lifecycle_evidence(text: str) -> dict[str, str]:
    vals: dict[str, str] = {}
    for k, v in _EV.findall(text):
        if k in vals:
            raise LifecycleError(f"duplicate lifecycle evidence field: {k}")
        vals[k] = v
    return vals


def owned_file(root: Path, rel: str, evidence: str) -> Path:
    return resolve_repo_file(root, rel, label=evidence)


def validate_directive_owner(root: Path, owner: str) -> None:
    cls, p = owner.split(":", 1)
    directive_path = Path(p)
    if (
        directive_path.is_absolute()
        or len(directive_path.parts) < 2
        or directive_path.parts[0] != "directives"
        or ".." in directive_path.parts
    ):
        raise LifecycleError(f"manual lifecycle owner must reference directives/: {p}")
    owned_file(root, p, evidence="manual lifecycle owner")
    try:
        raw_manifest: object = json.loads(
            (root / "directives/directive_manifest.json").read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except LifecycleError:
        raise
    except (OSError, ValueError) as exc:
        raise LifecycleError("directive manifest is not valid typed JSON") from exc
    if not isinstance(raw_manifest, dict):
        raise LifecycleError("directive manifest is not an object")
    manifest = cast(dict[str, object], raw_manifest)
    raw_directives = manifest.get("directives")
    if not isinstance(raw_directives, dict):
        raise LifecycleError("directive manifest lacks a directives object")
    directives = cast(dict[str, object], raw_directives)
    key = directive_path.relative_to("directives").as_posix()
    raw_record = directives.get(key)
    if not isinstance(raw_record, dict):
        raise LifecycleError(f"manual lifecycle owner absent from manifest: {p}")
    record = cast(dict[str, object], raw_record)
    actual = record.get("class")
    if not isinstance(actual, str):
        raise LifecycleError(f"manual lifecycle owner has no typed class: {p}")
    if actual != cls:
        raise LifecycleError(
            f"manual lifecycle owner class mismatch: expected {cls}, found {actual}"
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
    sealed_snapshots: dict[str, bytes] | None = None,
) -> LifecycleEvidenceFields:
    vals = parse_lifecycle_evidence(text)
    if disposition == "manual-supported" and (
        not vals.get("owner")
        or not vals.get("owner", "").startswith(("canonical:", "runbook:"))
        or not vals.get("invocation")
    ):
        raise LifecycleError(
            f"manual lifecycle requires canonical/runbook owner and invocation evidence: {path}"
        )
    owner_val = vals.get("owner")
    if owner_val and root is not None and owner_val.startswith(("canonical:", "runbook:")):
        validate_directive_owner(root, owner_val)
    if disposition == "internal-delegate" and incoming_edge is None:
        raise LifecycleError(f"internal lifecycle requires a verified incoming typed edge: {path}")
    comp = vals.get("completion")
    if disposition == "one-shot-completed" and (not comp or not comp.startswith("sealed:")):
        raise LifecycleError(f"one-shot lifecycle requires sealed completion evidence: {path}")
    if comp and root is not None and comp.startswith("sealed:"):
        rp = comp.split(":", 1)[1]
        f = owned_file(root, rp, evidence="one-shot sealed completion receipt")
        try:
            raw_receipt = f.read_bytes()
            raw_pay: object = json.loads(
                raw_receipt.decode("utf-8"),
                object_pairs_hook=reject_duplicate_keys,
            )
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise LifecycleError(f"one-shot completion receipt is not typed JSON: {rp}") from exc
        if not isinstance(raw_pay, dict):
            raise LifecycleError(f"one-shot completion receipt is not an object: {rp}")
        pay = cast(dict[str, object], raw_pay)
        status = pay.get("status")
        if "status" in pay and not isinstance(status, str):
            raise LifecycleError(f"one-shot completion receipt is not terminal: {rp}")
        terminal = status in {"PASS", "complete", "completed", "sealed"} or (
            "status" not in pay and pay.get("sealed") is True
        )
        if not terminal:
            raise LifecycleError(f"one-shot completion receipt is not terminal: {rp}")
        for issue_key in ("violations", "parse_errors"):
            if issue_key in pay:
                issues = pay[issue_key]
                if not isinstance(issues, list) or issues:
                    raise LifecycleError(f"one-shot completion receipt has unresolved issues: {rp}")
        if sealed_snapshots is not None:
            sealed_snapshots[rp] = raw_receipt
    if disposition == "compatibility-tombstone":
        if not vals.get("consumer") or not vals.get("expiry"):
            raise LifecycleError(
                f"tombstone lifecycle requires consumer and expiry evidence: {path}"
            )
        if vals["consumer"].lower() in {"none", "unknown", "n/a"}:
            raise LifecycleError(f"tombstone lifecycle requires a named consumer: {path}")
        try:
            require_current_iso_date(vals["expiry"], field="expiry")
        except ValueError as exc:
            raise LifecycleError(f"invalid tombstone lifecycle evidence for {path}: {exc}") from exc
    if disposition == "dormant-until":
        has_direct = all(vals.get(k) for k in ("owner", "activation", "review"))
        if not has_direct:
            if re.search(r"lifecycle:\s*dormant\b", text, re.I):
                raise LifecycleError(
                    f"dormant lifecycle requires owner, activation, and review evidence: {path}"
                )
            if dormant_policy is None or not dormant_policy.covers(path):
                raise LifecycleError(f"dormant lifecycle lacks explicit policy coverage: {path}")
            if dormant_policy_evidence is None:
                raise LifecycleError(f"dormant lifecycle lacks policy evidence: {path}")
            vals.update(
                owner=dormant_policy.owner_evidence,
                activation=dormant_policy.activation_evidence,
                review=dormant_policy.review_on,
            )
        else:
            o = vals["owner"]
            if not o.startswith(("linear:", "canonical:", "runbook:")):
                raise LifecycleError(f"dormant lifecycle owner is not authoritative: {path}")
            if (
                dormant_policy is not None
                and o.startswith("linear:")
                and o != dormant_policy.owner_evidence
            ):
                raise LifecycleError(f"dormant lifecycle owner conflicts with policy: {path}")
            if root is not None and o.startswith(("canonical:", "runbook:")):
                validate_directive_owner(root, o)
            if dormant_policy is None or not dormant_policy.covers(path):
                raise LifecycleError(f"dormant lifecycle lacks explicit policy coverage: {path}")
            if dormant_policy_evidence is None:
                raise LifecycleError(f"dormant lifecycle lacks policy evidence: {path}")
        try:
            require_current_iso_date(vals["review"], field="review")
        except ValueError as exc:
            raise LifecycleError(f"invalid dormant lifecycle evidence for {path}: {exc}") from exc
    is_dormant = disposition == "dormant-until"
    is_tomb = disposition == "compatibility-tombstone"
    return {
        "owner_evidence": vals.get("owner"),
        "invocation_evidence": vals.get("invocation")
        if disposition == "manual-supported"
        else None,
        "incoming_edge": incoming_edge,
        "sealed_completion_evidence": vals.get("completion")
        if disposition == "one-shot-completed"
        else None,
        "tombstone_consumer": vals.get("consumer") if is_tomb else None,
        "tombstone_expiry": vals.get("expiry") if is_tomb else None,
        "dormant_owner": vals.get("owner") if is_dormant else None,
        "dormant_activation": vals.get("activation") if is_dormant else None,
        "dormant_review": vals.get("review") if is_dormant else None,
        "dormant_policy_evidence": dormant_policy_evidence if is_dormant else None,
    }


def first_cli_line(text: str) -> int:
    m = _CLI.search(text)
    return text.count("\n", 0, m.start()) + 1 if m else 1


def expected_candidates(
    root: Path,
    paths: list[str],
    graph: ReachabilityGraph,
    task_by_xml: Mapping[str, TaskSpec],
) -> tuple[set[CandidateKey], list[str], tuple[str, ...]]:
    expected: set[CandidateKey] = set()
    violations: list[str] = []
    for path in paths:
        f = root / path
        if not f.is_file():
            continue
        text = read_text(f)
        if is_python_candidate(path, text):
            expected.add(CandidateKey(path, "python_module", path))
        if path.endswith(".py") and path.startswith(("src/", "execution/", "cron/", "scripts/")):
            try:
                tree = ast.parse(text, filename=path)
            except SyntaxError:
                tree = None
            if tree is not None:
                for node in ast.walk(tree):
                    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    for dec in node.decorator_list:
                        if not isinstance(dec, ast.Call):
                            continue
                        parsed = _parse_route_decorator(dec, node.name, path, violations)
                        if parsed is None:
                            continue
                        rule, methods, endpoint = parsed
                        meth = ",".join(methods)
                        expected.add(CandidateKey(path, "flask_route", f"{meth} {rule} {endpoint}"))
        if path.endswith(".task.xml"):
            t = task_by_xml.get(Path(path).name)
            ident = t.task_name if t is not None else path
            expected.add(CandidateKey(path, "scheduled_task", ident))
        if is_wrapper(path):
            expected.add(CandidateKey(path, "wrapper", path))
        if path.endswith(".service"):
            expected.add(CandidateKey(path, "service", path))
        if path == SERVICE_REGISTRY:
            for name, _ln in _managed_service_names(text, path, violations):
                expected.add(CandidateKey(path, "service", name))
    for e in graph.edges:
        if e.kind in {"reconstruction", "registry"} and not e.unknown and e.line is not None:
            expected.add(
                CandidateKey(
                    e.source,
                    "reconstruction" if e.kind == "reconstruction" else "registry",
                    f"{e.target}@{e.line}",
                )
            )
    discovered = {
        p
        for p in paths
        if p.startswith("src/")
        and (
            Path(p).name == "registry.py"
            or Path(p).name.endswith("_registry.py")
            or _REG_ASSIGN.search(read_text(root / p))
        )
    }
    uncat = sorted(discovered - set(REGISTRY_AUTHORITIES))
    if uncat:
        violations.append(f"uncatalogued registry authorities: {', '.join(uncat)}")
    expected.update(CandidateKey(p, "registry", p) for p in REGISTRY_AUTHORITIES if p in paths)
    expected.update(CandidateKey(p, "registry", p) for p in uncat)
    return expected, violations, tuple(uncat)
