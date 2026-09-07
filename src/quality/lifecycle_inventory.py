"""Strict inventory build and validation."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import date
from pathlib import Path

from pydantic import ValidationError

from quality.git_env import clean_local_git_env
from scheduler_manifest import TaskManifest, load_manifest

from .lifecycle_discovery import (
    SERVICE_REGISTRY,
    CandidateKey,
    expected_candidates,
    first_cli_line,
    is_python_candidate,
    is_wrapper,
    lifecycle_evidence_fields,
    python_disposition,
    python_rationale,
    registry_symbols,
    route_entries,
    runtime_incoming_edge,
    scheduled_targets,
    service_entries,
)
from .lifecycle_models import (
    DormantPolicy,
    LifecycleEntry,
    LifecycleError,
    LifecycleInventory,
    fingerprint,
    read_text,
    reject_duplicate_keys,
    resolve_repo_file,
    source_line,
)
from .reachability import ReachabilityCollectionError, ReachabilityGraph, build_graph

DISPOSITIONS = (
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
SURFACES = (
    "python_module",
    "flask_route",
    "scheduled_task",
    "wrapper",
    "service",
    "reconstruction",
    "registry",
)
GRAPH_REL = ".tmp/quality/reachability-check.json"
VIOLATION_CAP = 50
TASK_MANIFEST_REL = "cron/task_manifest.json"
DIRECTIVE_MANIFEST_REL = "directives/directive_manifest.json"
DORMANT_POLICY_REL = "docs/quality/lifecycle-dormant-policy.json"
LIFECYCLE_STATIC_INPUTS: tuple[str, ...] = (
    TASK_MANIFEST_REL,
    DIRECTIVE_MANIFEST_REL,
    DORMANT_POLICY_REL,
)


def _worktree_paths(root: Path) -> list[str]:
    try:
        r = subprocess.run(
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
            env=clean_local_git_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LifecycleError(f"cannot enumerate worktree: {exc}") from exc
    return sorted(p for p in r.stdout.decode().split("\0") if p)


def is_protected_lifecycle_output(root: Path, output: Path, report: LifecycleInventory) -> bool:
    """Return whether an output aliases producer input or unsafe in-repo state."""
    root_resolved = root.resolve()
    lexical = output if output.is_absolute() else Path.cwd() / output
    try:
        resolved_output = lexical.resolve()
    except OSError:
        return True
    protected = set(LIFECYCLE_STATIC_INPUTS)
    protected.add(GRAPH_REL)
    try:
        protected.update(_worktree_paths(root_resolved))
    except LifecycleError:
        return True
    for entry in report.entries:
        if entry.sealed_completion_evidence is not None:
            prefix, separator, target = entry.sealed_completion_evidence.partition(":")
            if separator and prefix == "sealed" and target:
                protected.add(target)
    for rel in protected:
        candidate = Path(rel)
        if rel == "" or candidate.is_absolute() or ".." in candidate.parts:
            continue
        protected_path = root_resolved / candidate
        try:
            if protected_path.resolve() == resolved_output:
                return True
        except OSError:
            continue
        try:
            if protected_path.samefile(lexical):
                return True
        except OSError:
            continue
    return resolved_output.is_relative_to(root_resolved) and not resolved_output.is_relative_to(
        root_resolved / ".tmp"
    )


def _load_graph(root: Path) -> tuple[ReachabilityGraph, str]:
    try:
        gp = resolve_repo_file(root, GRAPH_REL, label="typed reachability graph")
    except LifecycleError as exc:
        raise LifecycleError(f"typed reachability graph is missing: {exc}") from exc
    try:
        raw = gp.read_bytes()
        graph_payload: object = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
        graph = ReachabilityGraph.model_validate(graph_payload)
    except (OSError, ValidationError, ValueError, UnicodeDecodeError) as exc:
        raise LifecycleError(f"invalid typed reachability graph: {exc}") from exc
    if graph.hold:
        raise LifecycleError("typed reachability graph is on HOLD")
    try:
        fresh = build_graph(root)
    except ReachabilityCollectionError as exc:
        raise LifecycleError(f"typed reachability collection failed: {exc}") from exc
    if (
        graph.source_manifest_sha256 != fresh.source_manifest_sha256
        or graph.scanner_sha256 != fresh.scanner_sha256
        or graph.parser != fresh.parser
    ):
        raise LifecycleError("typed reachability graph is stale for the current worktree")
    if fresh.hold:
        raise LifecycleError("fresh current-worktree reachability graph is on HOLD")
    if graph.model_dump(mode="json") != fresh.model_dump(mode="json"):
        raise LifecycleError("typed reachability graph is stale for the current worktree")
    return graph, hashlib.sha256(raw).hexdigest()


def load_task_manifest(root: Path) -> TaskManifest:
    try:
        p = resolve_repo_file(root, TASK_MANIFEST_REL, label="scheduled-task manifest")
    except LifecycleError as exc:
        raise LifecycleError(f"canonical scheduled-task manifest is missing: {exc}") from exc
    try:
        return load_manifest(p)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise LifecycleError(f"invalid scheduled-task manifest: {exc}") from exc


def _load_policy(root: Path) -> tuple[DormantPolicy, str]:
    rel = DORMANT_POLICY_REL
    try:
        p = resolve_repo_file(root, rel, label="dormant lifecycle policy")
    except LifecycleError as exc:
        raise LifecycleError(f"dormant lifecycle policy is missing: {exc}") from exc
    try:
        raw = p.read_bytes()
        payload: object = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
        pol = DormantPolicy.model_validate(payload)
    except (OSError, ValidationError, ValueError, UnicodeDecodeError) as exc:
        raise LifecycleError(f"invalid dormant lifecycle policy: {exc}") from exc
    return pol, f"policy:{rel}#{hashlib.sha256(raw).hexdigest()}"


def _tree_hash(
    root: Path,
    authority: set[str],
    graph_hash: str,
    sealed_snapshots: dict[str, bytes],
) -> str:
    d = hashlib.sha256()
    for p in sorted(authority):
        if p in sealed_snapshots:
            d.update(p.encode() + b"\0" + hashlib.sha256(sealed_snapshots[p]).digest())
            continue
        f = root / p
        if not f.is_file():
            continue
        d.update(p.encode() + b"\0" + hashlib.sha256(f.read_bytes()).digest())
    d.update(GRAPH_REL.encode() + b"\0" + bytes.fromhex(graph_hash))
    return d.hexdigest()


def _revision(root: Path) -> tuple[str, bool]:
    try:
        rev = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            env=clean_local_git_env(),
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
                env=clean_local_git_env(),
            ).stdout.strip()
        )
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        raise LifecycleError(f"cannot resolve revision identity: {exc}") from exc
    return rev, dirty


def build_inventory(root: Path) -> LifecycleInventory:
    root = root.resolve()
    start_rev, start_dirty = _revision(root)
    paths = _worktree_paths(root)
    pset = set(paths)
    graph, gh = _load_graph(root)
    manifest = load_task_manifest(root)
    policy, policy_ev = _load_policy(root)
    task_by_xml = {t.xml: t for t in manifest.tasks}
    violations: list[str] = []
    try:
        policy_expired = date.fromisoformat(policy.review_on) < date.today()
    except ValueError:
        policy_expired = False
    if policy_expired:
        violations.append(f"dormant lifecycle policy is expired: {policy.review_on}")
    if len({t.task_name for t in manifest.tasks}) != len(manifest.tasks):
        violations.append("duplicate scheduled task names")
    if len(task_by_xml) != len(manifest.tasks):
        violations.append("duplicate scheduled task XML entries")
    expected, ev, uncatalogued = expected_candidates(root, paths, graph, task_by_xml)
    violations.extend(ev)
    for dg in graph.diagnostics:
        if dg.path.startswith(("src/", "execution/", "cron/", "scripts/", ".github/")):
            violations.append(f"operational reachability {dg.kind}: {dg.path}: {dg.message}")
    for e in graph.unknown_edges:
        if (
            e.source.startswith(("src/", "execution/", "cron/", "scripts/", ".github/"))
            or e.source in ("Makefile",)
            or e.source.endswith((".bat", ".cmd", ".ps1", ".sh", ".service"))
        ):
            violations.append(f"operational reachability unknown: {e.source}:{e.line}: {e.kind}")
    xmls = {Path(p).name for p in paths if p.endswith(".task.xml")}
    if orph := sorted(xmls - set(task_by_xml)):
        violations.append(f"scheduled task XML absent from manifest: {', '.join(orph)}")
    if miss := sorted(set(task_by_xml) - xmls):
        violations.append(f"scheduled task manifest XML missing: {', '.join(miss)}")
    if mw := sorted(t.wrapper for t in manifest.tasks if f"cron/{t.wrapper}" not in pset):
        violations.append(f"scheduled task wrappers missing: {', '.join(mw)}")
    sched_py, sched_wrap = scheduled_targets(
        root, pset, {f"cron/{t.wrapper}" for t in manifest.tasks}
    )
    entries: list[LifecycleEntry] = []
    sealed_snapshots: dict[str, bytes] = {}
    for path in paths:
        f = root / path
        if not f.is_file():
            continue
        text = read_text(f)
        if is_python_candidate(path, text):
            line = first_cli_line(text)
            evd = source_line(root, path, line)
            inc = runtime_incoming_edge(graph, path)
            disp = python_disposition(path, text, sched_py, inc)
            basis, rat = python_rationale(disp)
            try:
                fields = lifecycle_evidence_fields(
                    path=path,
                    text=text,
                    disposition=disp,
                    incoming_edge=inc,
                    root=root,
                    dormant_policy=policy,
                    dormant_policy_evidence=policy_ev,
                    sealed_snapshots=sealed_snapshots,
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
                    evidence=evd,
                    fingerprint=fingerprint(path, line, evd),
                    disposition=disp,
                    classification_basis=basis,
                    rationale=rat,
                    targets=(path,),
                    owner_evidence=fields["owner_evidence"],
                    invocation_evidence=fields["invocation_evidence"],
                    incoming_edge=fields["incoming_edge"],
                    sealed_completion_evidence=fields["sealed_completion_evidence"],
                    tombstone_consumer=fields["tombstone_consumer"],
                    tombstone_expiry=fields["tombstone_expiry"],
                    dormant_owner=fields["dormant_owner"],
                    dormant_activation=fields["dormant_activation"],
                    dormant_review=fields["dormant_review"],
                    dormant_policy_evidence=fields["dormant_policy_evidence"],
                )
            )
        if path.endswith(".py") and path.startswith(("src/", "execution/", "cron/", "scripts/")):
            entries.extend(route_entries(root, path, text, violations))
        if path.endswith(".task.xml"):
            ls = read_text(f).splitlines()
            evd = ls[0].strip() if ls else path
            t = task_by_xml.get(Path(path).name)
            entries.append(
                LifecycleEntry(
                    path=path,
                    line=1,
                    kind="scheduled_task",
                    identifier=t.task_name if t else path,
                    evidence=evd,
                    fingerprint=fingerprint(path, 1, evd),
                    disposition="scheduled",
                    classification_basis="scheduled_task_manifest",
                    rationale="Task manifest declares scheduled task.",
                    targets=(f"cron/{t.wrapper}",) if t else (),
                )
            )
        if is_wrapper(path):
            ls = text.splitlines()
            evd = ls[0].strip() if ls else path
            sched = path in sched_wrap
            if not sched and policy_expired:
                violations.append(f"expired dormant policy cannot classify wrapper: {path}")
                continue
            if not sched and not policy.covers(path):
                violations.append(f"dormant lifecycle policy does not cover wrapper: {path}")
                continue
            entries.append(
                LifecycleEntry(
                    path=path,
                    line=1,
                    kind="wrapper",
                    identifier=path,
                    evidence=evd,
                    fingerprint=fingerprint(path, 1, evd),
                    disposition="scheduled" if sched else "dormant-until",
                    classification_basis="scheduler_owned_wrapper" if sched else "operator_wrapper",
                    rationale="Scheduler-owned wrapper." if sched else "Unlinked wrapper dormant.",
                    dormant_owner=None if sched else policy.owner_evidence,
                    dormant_activation=None if sched else policy.activation_evidence,
                    dormant_review=None if sched else policy.review_on,
                    dormant_policy_evidence=None if sched else policy_ev,
                )
            )
        if path.endswith(".service"):
            ls = text.splitlines()
            evd = ls[0].strip() if ls else path
            entries.append(
                LifecycleEntry(
                    path=path,
                    line=1,
                    kind="service",
                    identifier=path,
                    evidence=evd,
                    fingerprint=fingerprint(path, 1, evd),
                    disposition="service",
                    classification_basis="service_unit_file",
                    rationale="Service unit file.",
                    targets=(path,),
                )
            )
        if path == SERVICE_REGISTRY:
            entries.extend(service_entries(root, path, text, violations))
    for e in graph.edges:
        if e.kind in {"reconstruction", "registry"} and not e.unknown and e.line is not None:
            try:
                evd = source_line(root, e.source, e.line)
            except LifecycleError as exc:
                violations.append(str(exc))
                continue
            entries.append(
                LifecycleEntry(
                    path=e.source,
                    line=e.line,
                    kind="reconstruction" if e.kind == "reconstruction" else "registry",
                    identifier=f"{e.target}@{e.line}",
                    evidence=evd,
                    fingerprint=fingerprint(e.source, e.line, evd),
                    disposition="internal-delegate",
                    classification_basis="typed_reachability_edge",
                    rationale=f"Typed {e.kind} edge.",
                    targets=(e.target,),
                    incoming_edge=f"{e.source}:{e.line}:{e.kind}",
                )
            )
    from .lifecycle_discovery import REGISTRY_AUTHORITIES

    for p in (*REGISTRY_AUTHORITIES, *uncatalogued):
        if p in pset:
            is_uncatalogued = p not in REGISTRY_AUTHORITIES
            try:
                text = read_text(root / p)
                line = next((i for i, v in enumerate(text.splitlines(), 1) if v.strip()), 1)
                evd = source_line(root, p, line)
                targets = registry_symbols(text, p)
            except LifecycleError as exc:
                violations.append(str(exc))
                continue
            inc = runtime_incoming_edge(graph, p)
            active = inc is not None
            if not active and policy_expired:
                violations.append(f"expired dormant policy cannot classify registry: {p}")
                continue
            if not active and not policy.covers(p):
                violations.append(f"dormant lifecycle policy does not cover registry: {p}")
            if is_uncatalogued:
                basis = "uncatalogued_registry"
                rationale = "Uncatalogued registry retained."
            elif active:
                basis = "typed_incoming_edge"
                rationale = "Registry retained."
            else:
                basis = "time_bounded_owner_review"
                rationale = "Registry retained."
            entries.append(
                LifecycleEntry(
                    path=p,
                    line=line,
                    kind="registry",
                    identifier=p,
                    evidence=evd,
                    fingerprint=fingerprint(p, line, evd),
                    disposition="internal-delegate" if active else "dormant-until",
                    classification_basis=basis,
                    rationale=rationale,
                    targets=targets,
                    incoming_edge=inc,
                    dormant_owner=None if active else policy.owner_evidence,
                    dormant_activation=None if active else policy.activation_evidence,
                    dormant_review=None if active else policy.review_on,
                    dormant_policy_evidence=None if active else policy_ev,
                )
            )
    entries.sort(key=lambda e: (e.path, e.line, e.kind, e.identifier))
    actual = {CandidateKey(e.path, e.kind, e.identifier) for e in entries}
    dups = len(entries) - len(actual)
    omis = tuple(sorted(":".join(k) for k in set(expected) - actual))
    extr = tuple(sorted(":".join(k) for k in actual - set(expected)))
    if dups:
        violations.append(f"duplicate lifecycle identities: {dups}")
    if omis:
        violations.append(f"inventory omissions: {len(omis)}")
    if extr:
        violations.append(f"inventory extras: {len(extr)}")
    if not entries:
        violations.append("no lifecycle evidence discovered")
    sealed_refs = {
        e.sealed_completion_evidence.split(":", 1)[1]
        for e in entries
        if e.sealed_completion_evidence is not None
        and e.sealed_completion_evidence.startswith("sealed:")
    }
    auth = (
        {e.path for e in entries}
        | {
            "cron/task_manifest.json",
            "directives/directive_manifest.json",
            "docs/quality/lifecycle-dormant-policy.json",
        }
        | sealed_refs
    )
    tree_hash = _tree_hash(root, auth, gh, sealed_snapshots)
    for rel, raw in sealed_snapshots.items():
        try:
            after_path = resolve_repo_file(root, rel, label="one-shot sealed completion receipt")
            receipt_hash_after = hashlib.sha256(after_path.read_bytes()).digest()
        except (LifecycleError, OSError):
            receipt_hash_after = b""
        if receipt_hash_after != hashlib.sha256(raw).digest():
            violations.append(
                f"sealed completion receipt changed during lifecycle collection: {rel}"
            )
    try:
        graph_after = resolve_repo_file(root, GRAPH_REL, label="typed reachability graph")
        graph_hash_after = hashlib.sha256(graph_after.read_bytes()).hexdigest()
    except (LifecycleError, OSError):
        graph_hash_after = ""
    if graph_hash_after != gh:
        violations.append("typed reachability graph changed during lifecycle collection")
    rev, dirty = _revision(root)
    if start_rev != rev:
        violations.append(f"git revision changed during build: {start_rev} != {rev}")
    if start_dirty != dirty:
        violations.append(
            f"git worktree cleanliness changed during build: {start_dirty} != {dirty}"
        )
    if start_dirty or dirty:
        violations.append("worktree is dirty: lifecycle evidence is not admissible")
    if graph.subject_commit != rev:
        violations.append(
            f"reachability graph subject does not match inventory revision: {graph.subject_commit} != {rev}"
        )
    v = tuple(sorted(set(violations))[:VIOLATION_CAP])
    return LifecycleInventory(
        status="HOLD" if v else "PASS",
        entries=tuple(entries),
        counts={n: sum(e.disposition == n for e in entries) for n in DISPOSITIONS},
        surface_counts={n: sum(e.kind == n for e in entries) for n in SURFACES},
        tracked_tree_hash=tree_hash,
        revision=rev,
        worktree_dirty=dirty,
        reachability_graph_hash=gh,
        graph_parser=dict(graph.parser),
        coverage={
            "candidates": len(expected),
            "inventoried": len(actual),
            "omissions": len(omis),
            "extras": len(extr),
            "duplicates": dups,
        },
        omissions=omis,
        extras=extr,
        violations=v,
    )


def load_inventory(path: Path) -> LifecycleInventory:
    try:
        payload: object = json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
        return LifecycleInventory.model_validate(payload)
    except (OSError, ValidationError, ValueError, UnicodeDecodeError) as exc:
        raise LifecycleError(f"invalid lifecycle inventory {path}: {exc}") from exc


def validate_inventory(root: Path, persisted: LifecycleInventory) -> tuple[str, ...]:
    current = build_inventory(root)
    out: list[str] = []
    from .lifecycle_models import SCHEMA_VERSION

    if persisted.schema_version != SCHEMA_VERSION:
        out.append(f"schema changed: {persisted.schema_version} != {SCHEMA_VERSION}")
    if persisted.tracked_tree_hash != current.tracked_tree_hash:
        out.append("tracked content fingerprint is stale")
    if persisted.reachability_graph_hash != current.reachability_graph_hash:
        out.append("reachability graph fingerprint is stale")
    if persisted.status != "PASS" or current.status != "PASS":
        out.append("persisted and current lifecycle inventories must both PASS")
    if persisted.revision != current.revision:
        out.append(f"revision identity mismatch: {persisted.revision} != {current.revision}")
    if persisted.worktree_dirty or current.worktree_dirty:
        out.append("lifecycle inventory worktree identity is not clean")
    if persisted.model_dump(
        mode="json", exclude={"revision", "worktree_dirty"}
    ) != current.model_dump(mode="json", exclude={"revision", "worktree_dirty"}):
        out.append("persisted lifecycle semantics differ from the current inventory")
    return tuple(sorted(set(out)))
