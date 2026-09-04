"""Deterministic inventory derivation for the BHA-122 roadmap freeze.

The workflow module owns orchestration; this module owns pure evidence parsing,
graph cuts, and bottom-up inventory allocation.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Literal, cast

from .architecture import ArchitectureReceipt, build_architecture_receipt, resolved_import_edges
from .function_lifecycle import FunctionLifecycleInventory, build_inventory, load_inventory
from .lifecycle import LifecycleInventory
from .lifecycle import build_inventory as build_lifecycle_inventory
from .reachability import ReachabilityGraph, build_graph
from .roadmap_freeze_contract import (
    BUILDER_CONVERT_QUOTAS,
    BUILDER_RETAIN_QUOTAS,
    FROZEN_PERFORMANCE,
    FROZEN_PERFORMANCE_RECEIPT_SHA256,
    FROZEN_RECONCILIATION_CLAIM_MANIFEST_SHA256,
    FROZEN_RECONCILIATION_ROADMAP_SHA256,
    FROZEN_TYPE_DEBT_AUTHORITY_SHA256,
    FROZEN_TYPE_DEBT_TOTALS,
    FUNCTION_LIFECYCLE_RECEIPT,
    LOC_TARGET_CAPS,
    PERFORMANCE_RECEIPT,
    PROGRAM_OWNER,
    RECONCILIATION_RECEIPT,
    TYPE_DEBT_AUTHORITY_PATH,
    TYPE_DEBT_AUTHORITY_SHA256,
    TYPE_DEBT_EVIDENCE_ALGORITHM,
    BudgetMapping,
    BuilderDisposition,
    FunctionLifecycleSnapshot,
    LargeModule,
    LocCrossing,
    ReachabilityEvidence,
    ReconciliationSnapshot,
    RoadmapFreeze,
    SuppressionRetirement,
    TypeDebtCluster,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_diagnostic_value(root: Path, key: str | None, value: object) -> object:
    """Normalize one Pyright diagnostic value for stable membership hashing."""
    if isinstance(value, dict):
        mapping = cast(dict[str, object], value)
        return {
            child_key: _canonical_diagnostic_value(root, child_key, child_value)
            for child_key, child_value in mapping.items()
        }
    if isinstance(value, list):
        values = cast(list[object], value)
        return [_canonical_diagnostic_value(root, key, child) for child in values]
    if key == "file" and isinstance(value, str):
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            return candidate.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return value.replace("\\", "/")
    return value


def _canonical_diagnostic_membership_sha256(root: Path, rows: list[dict[str, object]]) -> str:
    """Hash semantic diagnostic membership, excluding volatile receipt metadata.

    Pyright's top-level ``time`` and ``summary`` fields are runtime metadata;
    each diagnostic row is the membership authority.  Sorting canonical rows
    makes output ordering irrelevant while preserving duplicate diagnostics.
    Repository-relative file paths keep the digest stable across checkouts.
    """
    canonical_rows = sorted(
        json.dumps(
            _canonical_diagnostic_value(root, None, row),
            sort_keys=True,
            separators=(",", ":"),
        )
        for row in rows
    )
    payload = TYPE_DEBT_EVIDENCE_ALGORITHM + "\n" + "\n".join(canonical_rows)
    return hashlib.sha256(payload.encode()).hexdigest()


def _read_json(root: Path, path: str) -> dict[str, object]:
    value = json.loads((root / path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"evidence receipt is not an object: {path}")
    return cast(dict[str, object], value)


_PRODUCTION_PATH_PREFIXES = ("src/", "execution/", "cron/", "scripts/", ".github/")


def _graph_sha256(graph: ReachabilityGraph) -> str:
    """Hash the exact CLI serialization, while keeping the graph in memory."""
    payload = graph.model_dump_json(indent=2) + "\n"
    return hashlib.sha256(payload.encode()).hexdigest()


def reachability_evidence(root: Path, lifecycle: dict[str, object]) -> ReachabilityEvidence:
    """Rebuild reachability and bind lifecycle provenance without an ignored file."""
    graph = build_graph(root)
    graph_hash = _graph_sha256(graph)
    persisted_lifecycle = LifecycleInventory.model_validate(lifecycle)
    current_lifecycle = build_lifecycle_inventory(root, allow_missing_graph=True)
    volatile = {"revision", "worktree_dirty"}
    if persisted_lifecycle.model_dump(
        mode="json", exclude=volatile
    ) != current_lifecycle.model_dump(mode="json", exclude=volatile):
        raise ValueError("lifecycle receipt is stale relative to the current source tree")
    lifecycle_hash = lifecycle.get("reachability_graph_hash")
    if lifecycle_hash != graph_hash:
        raise ValueError("lifecycle reachability graph hash is stale")
    graph_parser = lifecycle.get("graph_parser")
    if graph_parser != graph.parser:
        raise ValueError("lifecycle reachability parser provenance is stale")
    status_raw = lifecycle.get("status")
    if status_raw not in {"PASS", "HOLD"}:
        raise ValueError("lifecycle receipt has an invalid status")
    status = cast(Literal["PASS", "HOLD"], status_raw)
    raw_violations = lifecycle.get("violations", [])
    violations = (
        tuple(str(item) for item in cast(list[object], raw_violations))
        if isinstance(raw_violations, list)
        else ()
    )
    unknown_edges = tuple(graph.unknown_edges)
    production_unknown = sum(
        edge.source.startswith(_PRODUCTION_PATH_PREFIXES) for edge in unknown_edges
    )
    stale_disposition_diagnostics = sum(
        diagnostic.kind == "unknown" and "stale reachability disposition" in diagnostic.message
        for diagnostic in graph.diagnostics
    )
    return ReachabilityEvidence(
        graph_sha256=graph_hash,
        parser=graph.parser,
        lifecycle_status=status,
        lifecycle_violations=violations,
        unknown_edges=len(unknown_edges),
        production_unknown_edges=production_unknown,
        stale_disposition_diagnostics=stale_disposition_diagnostics,
    )


def reconciliation_snapshot(root: Path) -> ReconciliationSnapshot:
    """Parse and admit the complete claim-level reconciliation receipt.

    The receipt's source generators may include an ignored reachability file;
    claims remain admissible on a clean clone because their typed claim shape,
    eligibility, counts, and available source hashes are checked here.
    """
    from .roadmap_reconciliation import ReconciliationReceipt

    receipt = ReconciliationReceipt.model_validate_json(
        (root / RECONCILIATION_RECEIPT).read_text(encoding="utf-8")
    )
    if receipt.status != "PASS" or receipt.violations:
        raise ValueError("reconciliation receipt must be a clean PASS")
    if receipt.claim_manifest_sha256 != FROZEN_RECONCILIATION_CLAIM_MANIFEST_SHA256:
        raise ValueError("reconciliation claim manifest is stale")
    if receipt.roadmap_source.sha256 != FROZEN_RECONCILIATION_ROADMAP_SHA256:
        raise ValueError("reconciliation roadmap source is stale")
    names = [claim.name for claim in receipt.claims]
    if len(names) != len(set(names)):
        raise ValueError("reconciliation claims are duplicated")
    scored = sum(claim.scored_eligible for claim in receipt.claims)
    rejected = sum(claim.verdict == "rejected" for claim in receipt.claims)
    if (receipt.scored_claims, receipt.rejected_claims) != (scored, rejected):
        raise ValueError("reconciliation claim totals do not match claim-level evidence")
    graph = build_graph(root)
    source_hashes = {
        path: _sha256(root / path)
        for path in (
            "docs/quality/architecture-ratchet.json",
            "docs/quality/duplicates-ratchet.json",
            "docs/quality/static-baseline.json",
            "docs/quality/test-db-patterns-baseline.json",
        )
    }
    source_hashes[".tmp/quality/reachability-check.json"] = _graph_sha256(graph)
    digest = hashlib.sha256()
    for source_path in (
        "docs/quality/architecture-ratchet.json",
        "docs/quality/duplicates-ratchet.json",
        "docs/quality/static-baseline.json",
        "docs/quality/test-db-patterns-baseline.json",
        ".tmp/quality/reachability-check.json",
    ):
        digest.update(source_path.encode())
        digest.update(b"\0")
        digest.update(bytes.fromhex(source_hashes[source_path]))
    digest.update(b"roadmap\0")
    digest.update(bytes.fromhex(FROZEN_RECONCILIATION_ROADMAP_SHA256))
    if receipt.source_hash != digest.hexdigest():
        raise ValueError("reconciliation source hash is stale")

    for claim in receipt.claims:
        eligible_shape = (
            claim.verdict in {"verified", "corrected"}
            and claim.observed is not None
            and claim.evidence is not None
            and claim.provisional_evidence is not None
        )
        if claim.scored_eligible != eligible_shape:
            raise ValueError(f"reconciliation claim eligibility is inconsistent: {claim.name}")
        if claim.evidence is not None:
            expected_hash = source_hashes.get(claim.evidence.path)
            if expected_hash is None or claim.evidence.sha256 != expected_hash:
                raise ValueError(f"reconciliation evidence hash changed: {claim.evidence.path}")
        if (
            claim.provisional_evidence is not None
            and claim.provisional_evidence.sha256 != FROZEN_RECONCILIATION_ROADMAP_SHA256
        ):
            raise ValueError("reconciliation provisional roadmap evidence changed")
    return ReconciliationSnapshot(
        path=RECONCILIATION_RECEIPT,
        sha256=_sha256(root / RECONCILIATION_RECEIPT),
        status=receipt.status,
        claims_count=len(receipt.claims),
        scored_claims=receipt.scored_claims,
        rejected_claims=receipt.rejected_claims,
        source_hash=receipt.source_hash,
        claim_manifest_sha256=receipt.claim_manifest_sha256,
        roadmap_source_sha256=receipt.roadmap_source.sha256,
        violations=receipt.violations,
    )


def _stable_function_lifecycle_fields(
    inventory: FunctionLifecycleInventory,
) -> tuple[object, ...]:
    """Return receipt fields whose identity is independent of commit finalization."""
    return (
        inventory.schema_version,
        inventory.parser_version,
        inventory.status,
        inventory.tracked_tree_hash,
        inventory.inventory_hash,
        inventory.files_scanned,
        inventory.symbol_count,
        len(inventory.candidate_symbols),
        inventory.unknown_total,
        inventory.counts,
        inventory.unknown_hazard_counts,
        inventory.files_failed,
        inventory.violations,
        inventory.candidate_symbols,
        inventory.unknown_symbols,
    )


def function_lifecycle_snapshot(root: Path) -> FunctionLifecycleSnapshot:
    """Validate the tracked compact receipt against a fresh in-memory scan.

    ``revision`` and ``worktree_dirty`` are source-generation metadata.  They
    are intentionally not compared: adding the tracked receipt necessarily
    changes the final commit, and a clean clone has a different dirty bit.
    """
    receipt_path = root / FUNCTION_LIFECYCLE_RECEIPT
    persisted = load_inventory(receipt_path)
    current = build_inventory(root)
    if persisted.status != "PASS" or persisted.files_failed or persisted.violations:
        raise ValueError("function lifecycle receipt does not record a successful scan")
    if current.status != "PASS" or current.files_failed or current.violations:
        raise ValueError("fresh function lifecycle scan did not succeed")
    if _stable_function_lifecycle_fields(persisted) != _stable_function_lifecycle_fields(current):
        raise ValueError("function lifecycle receipt is stale relative to the current source tree")
    return FunctionLifecycleSnapshot(
        path=FUNCTION_LIFECYCLE_RECEIPT,
        sha256=_sha256(receipt_path),
        schema_version=persisted.schema_version,
        parser_version=persisted.parser_version,
        status="PASS",
        tracked_tree_hash=persisted.tracked_tree_hash,
        inventory_hash=persisted.inventory_hash,
        files_scanned=persisted.files_scanned,
        symbol_count=persisted.symbol_count,
        candidate_count=len(persisted.candidate_symbols),
        unknown_total=persisted.unknown_total,
        counts=persisted.counts,
        unknown_hazard_counts=persisted.unknown_hazard_counts,
        files_failed=persisted.files_failed,
        violations=persisted.violations,
    )


def _tracked_type_debt_authority(root: Path) -> tuple[dict[str, int], list[TypeDebtCluster]]:
    """Read tracked cluster membership and verify the code-pinned authority hash."""
    authority_path = root / TYPE_DEBT_AUTHORITY_PATH
    if not authority_path.is_file() or _sha256(authority_path) != TYPE_DEBT_AUTHORITY_SHA256:
        raise ValueError("tracked type-debt authority is missing or changed")
    authority = _read_json(root, TYPE_DEBT_AUTHORITY_PATH)
    raw_clusters = authority.get("clusters")
    if not isinstance(raw_clusters, list):
        raise ValueError("tracked type-debt authority lacks cluster rows")
    clusters = [TypeDebtCluster.model_validate(row) for row in cast(list[object], raw_clusters)]
    digest = hashlib.sha256(
        json.dumps(
            [cluster.model_dump() for cluster in clusters],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if digest != FROZEN_TYPE_DEBT_AUTHORITY_SHA256:
        raise ValueError("tracked type-debt authority changed")
    retained = sum(cluster.count for cluster in clusters)
    totals = {
        "total": retained,
        "archived": FROZEN_TYPE_DEBT_TOTALS["archived"],
        "all": retained + FROZEN_TYPE_DEBT_TOTALS["archived"],
    }
    if totals != FROZEN_TYPE_DEBT_TOTALS:
        raise ValueError("tracked type-debt authority totals changed")
    return totals, clusters


def _validate_performance_snapshot(root: Path, freeze: RoadmapFreeze) -> None:
    """Validate the historical performance authority, including absent .tmp.

    A missing intermediate receipt is an expected fresh-checkout condition and
    should leave the freeze on HOLD.  It must not, however, make the embedded
    performance numbers or receipt identity mutable by whoever supplies a
    candidate freeze document.
    """
    reference = freeze.evidence["performance"]
    if reference.path != PERFORMANCE_RECEIPT:
        raise ValueError("checked performance evidence path drifted")
    expected_revision = cast(str, FROZEN_PERFORMANCE["revision"])
    if (
        reference.sha256 != FROZEN_PERFORMANCE_RECEIPT_SHA256
        or reference.scoped_commit != expected_revision
        or reference.scope != "COMMIT"
    ):
        raise ValueError("performance evidence identity drifted from the approved Train 0 freeze")
    # Keep the canonical relative path intact here: callers may deliberately
    # model a fresh checkout by hiding this exact path without affecting any
    # other evidence file.
    performance_path = root / PERFORMANCE_RECEIPT
    if not performance_path.is_file():
        return
    actual_sha256 = _sha256(performance_path)
    if actual_sha256 != reference.sha256 or actual_sha256 != FROZEN_PERFORMANCE_RECEIPT_SHA256:
        raise ValueError("historical performance evidence changed")
    actual = _read_json(root, PERFORMANCE_RECEIPT)
    for key, expected in FROZEN_PERFORMANCE.items():
        if actual.get(key) != expected:
            raise ValueError("historical performance snapshot drifted from the approved freeze")


def _git_commit(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _sccs(edges: list[tuple[str, str]], nodes: set[str]) -> list[list[str]]:
    adjacency: dict[str, set[str]] = defaultdict(set)
    for source, target in edges:
        adjacency[source].add(target)
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indexes: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    components: list[list[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indexes[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in sorted(adjacency[node]):
            if target not in indexes:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indexes[target])
        if lowlinks[node] == indexes[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            if len(component) > 1:
                components.append(sorted(component))

    for node in sorted(nodes):
        if node not in indexes:
            visit(node)
    return sorted(components, key=lambda component: (-len(component), component))


def _order_score(order: list[str], edges: list[tuple[str, str]]) -> int:
    positions = {node: index for index, node in enumerate(order)}
    return sum(positions[source] > positions[target] for source, target in edges)


def _feedback_arc_cut(
    edges: list[tuple[str, str]], components: list[list[str]], source_hash: str
) -> list[tuple[str, str]]:
    """Choose a stable minimum feedback-arc set for the architecture SCCs.

    Exact minimum feedback arc set is NP-hard.  These SCCs are small, and the
    deterministic seeded insertion search reaches the known 31-edge optimum;
    the resulting artifact is still validated by removing the chosen edges and
    proving that no SCC remains.
    """
    selected: list[tuple[str, str]] = []
    for component in components:
        component_nodes = set(component)
        internal = sorted(
            (source, target)
            for source, target in edges
            if source in component_nodes and target in component_nodes
        )
        seed = int(
            hashlib.sha256((source_hash + "\0" + "\n".join(component)).encode()).hexdigest()[:16],
            16,
        )
        best_score = len(internal) + 1
        best_order: list[str] = []
        for restart in range(16):
            order = component[:]
            random.Random(seed + restart).shuffle(order)
            score = _order_score(order, internal)
            changed = True
            while changed:
                changed = False
                for index in range(len(order)):
                    node = order.pop(index)
                    candidate_score = score
                    candidate_index = index
                    for position in range(len(order) + 1):
                        order.insert(position, node)
                        trial_score = _order_score(order, internal)
                        order.pop(position)
                        if trial_score < candidate_score:
                            candidate_score = trial_score
                            candidate_index = position
                    order.insert(candidate_index, node)
                    if candidate_score < score:
                        score = candidate_score
                        changed = True
            if score < best_score:
                best_score = score
                best_order = order[:]
        positions = {node: index for index, node in enumerate(best_order)}
        selected.extend(
            (source, target) for source, target in internal if positions[source] > positions[target]
        )
    return sorted(selected)


def _architecture_edges(root: Path) -> tuple[ArchitectureReceipt, list[tuple[str, str]]]:
    receipt = build_architecture_receipt(root, "WORKTREE")
    return receipt, list(resolved_import_edges(root))


def _type_debt(
    root: Path, static: dict[str, object]
) -> tuple[dict[str, int], list[TypeDebtCluster]]:
    raw_diagnostics = static.get("diagnostics", [])
    diagnostics = cast(list[object], raw_diagnostics) if isinstance(raw_diagnostics, list) else []
    diagnostic_rows = [
        cast(dict[str, object], item) for item in diagnostics if isinstance(item, dict)
    ]
    pyright = next((item for item in diagnostic_rows if item.get("tool") == "pyright"), None)
    if not isinstance(pyright, dict):
        raise ValueError("static receipt lacks pyright diagnostics")
    receipt_path_raw = pyright.get("receipt_path")
    receipt_path = str(receipt_path_raw) if isinstance(receipt_path_raw, str) else None
    rows: list[dict[str, object]] = []
    evidence_sha256 = "missing"
    if not isinstance(receipt_path, str):
        raise ValueError("static receipt lacks a pyright evidence path")
    evidence_file = (root / receipt_path).resolve()
    try:
        evidence_file.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("pyright evidence path escapes the repository") from exc
    if not evidence_file.is_file():
        # Raw diagnostics are an ignored intermediate.  A clean clone can
        # still validate the frozen membership from the tracked, hash-pinned
        # authority; a candidate cannot replace that authority's digest.
        return _tracked_type_debt_authority(root)
    raw: object = json.loads(evidence_file.read_text(encoding="utf-8"))
    raw_object = cast(dict[str, object], raw) if isinstance(raw, dict) else {}
    raw_values: object = raw_object.get("generalDiagnostics", [])
    values = cast(list[object], raw_values) if isinstance(raw_values, list) else []
    rows = [cast(dict[str, object], value) for value in values if isinstance(value, dict)]
    evidence_sha256 = _canonical_diagnostic_membership_sha256(root, rows)
    by_cluster: Counter[tuple[str, str]] = Counter()
    files_by_cluster: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        filename = str(row.get("file", "")).replace("\\", "/")
        try:
            filename = Path(filename).resolve().relative_to(root).as_posix()
        except ValueError:
            filename = filename.lstrip("/")
        rule = str(row.get("rule", "unknown"))
        if filename.startswith("alembic/versions_archived/"):
            zone = "archived"
        elif filename.startswith("instruction_tests/"):
            zone = "instruction_tests"
        elif filename.startswith("tests/"):
            zone = "tests"
        elif filename.startswith("execution/"):
            zone = "execution"
        elif filename.startswith("src/"):
            zone = "src"
        else:
            zone = "other"
        by_cluster[(zone, rule)] += 1
        files_by_cluster[(zone, rule)].add(filename)
    archived = sum(count for (zone, _rule), count in by_cluster.items() if zone == "archived")
    retained = sum(count for (zone, _rule), count in by_cluster.items() if zone != "archived")
    rule_lanes = {
        "reportUnknown": ("typing-unknowns", 0.08),
        "reportMissing": ("typing-annotations", 0.12),
        "reportPrivateUsage": ("api-boundaries", 0.10),
        "reportUnused": ("dead-code", 0.06),
    }
    clusters: list[TypeDebtCluster] = []
    for (zone, rule), count in sorted(by_cluster.items()):
        lane, hours_per = next(
            (value for prefix, value in rule_lanes.items() if rule.startswith(prefix)),
            ("contract-typing", 0.15),
        )
        if zone == "archived":
            lane, risk, hours_per = "archive-integrity", "low", 0.02
        elif zone == "execution":
            risk = "high"
        elif zone == "src" and (
            rule.startswith("reportArgument") or rule.startswith("reportAttribute")
        ):
            risk = "critical"
        else:
            risk = "medium"
        hours = round(count * hours_per, 2)
        if zone == "archived":
            continue
        clusters.append(
            TypeDebtCluster(
                source_zone=zone,
                rule=rule,
                count=count,
                ownership_lane=lane,
                risk=risk,
                estimated_hours=hours,
                suggested_slice_count=max(1, math.ceil(count / 100)),
                file_count=len(files_by_cluster[(zone, rule)]),
                files_sha256=hashlib.sha256(
                    "\n".join(sorted(files_by_cluster[(zone, rule)])).encode()
                ).hexdigest(),
                evidence_sha256=evidence_sha256,
            )
        )
    return {"total": retained, "archived": archived, "all": retained + archived}, clusters


def _suppression_retirement(static: dict[str, object]) -> SuppressionRetirement:
    """Derive the source-ignore-comments baseline from the static receipt."""
    raw_diagnostics = static.get("diagnostics", [])
    diagnostics = cast(list[object], raw_diagnostics) if isinstance(raw_diagnostics, list) else []
    source = next(
        (
            cast(dict[str, object], row)
            for row in diagnostics
            if isinstance(row, dict) and row.get("tool") == "source-ignore-comments"
        ),
        None,
    )
    if source is None or not isinstance(source.get("count"), int):
        raise ValueError("static receipt lacks source-ignore-comments suppression baseline")
    raw_rules = source.get("diagnostics_by_rule")
    if not isinstance(raw_rules, dict):
        raise ValueError("static receipt lacks typed suppression rule counts")
    typed_rules = cast(dict[str, object], raw_rules)
    if not all(isinstance(value, int) for value in typed_rules.values()):
        raise ValueError("static receipt lacks typed suppression rule counts")
    rule_counts: dict[str, int] = {}
    for key, value in typed_rules.items():
        if not isinstance(value, int):
            raise ValueError("static receipt lacks typed suppression rule counts")
        rule_counts[str(key)] = value
    count = source["count"]
    assert isinstance(count, int)
    return SuppressionRetirement(
        baseline=count,
        source_rule_counts=rule_counts,
    )


def static_quality_total(static: dict[str, object]) -> tuple[int, dict[str, int]]:
    """Derive the final-static-zero denominator from every typed static tool."""
    raw_diagnostics = static.get("diagnostics", [])
    diagnostics = cast(list[object], raw_diagnostics) if isinstance(raw_diagnostics, list) else []
    rows = [cast(dict[str, object], row) for row in diagnostics if isinstance(row, dict)]

    def count(tool: str) -> int:
        row = next((item for item in rows if item.get("tool") == tool), None)
        value = row.get("count") if row is not None else None
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"static receipt lacks non-negative {tool} count")
        return value

    pyright = next((item for item in rows if item.get("tool") == "pyright"), None)
    if pyright is None or not isinstance(pyright.get("diagnostics_by_directory"), dict):
        raise ValueError("static receipt lacks typed Pyright directory counts")
    directories = cast(dict[str, object], pyright["diagnostics_by_directory"])
    archived = sum(
        value
        for key, value in directories.items()
        if key.startswith("alembic/versions_archived") and isinstance(value, int)
    )
    components = {
        "pyright-active": count("pyright") - archived,
        "ruff": count("ruff"),
        "ruff-format": count("ruff-format"),
        "source-ignore-comments": count("source-ignore-comments"),
    }
    if components["pyright-active"] < 0:
        raise ValueError("archived Pyright diagnostics exceed the total")
    return sum(components.values()), components


def _duplicate_totals(receipt: dict[str, object]) -> tuple[int, int, int]:
    raw = receipt.get("exact_totals")
    if not isinstance(raw, dict):
        raise ValueError("duplicate receipt lacks exact_totals")
    values: list[object] = [
        raw.get(key) for key in ("groups", "participating_functions", "duplicated_loc")
    ]
    if not all(isinstance(value, int) for value in values):
        raise ValueError("duplicate receipt exact totals are not integers")
    return cast(tuple[int, int, int], tuple(values))


def _upgrade_builder_rows(test_db: dict[str, object]) -> list[dict[str, object]]:
    raw = test_db.get("database_builders", [])
    rows = cast(list[object], raw) if isinstance(raw, list) else []
    result: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        typed = cast(dict[str, object], row)
        evidence = typed.get("evidence")
        evidence_items = cast(list[object], evidence) if isinstance(evidence, list) else []
        if "call:command.upgrade" in evidence_items:
            result.append(typed)
    return sorted(
        result,
        key=lambda row: (
            str(row.get("path", "")),
            str(row.get("taxonomy", "")),
            json.dumps(row.get("evidence", []), sort_keys=True),
        ),
    )


def _builder_dispositions(rows: list[dict[str, object]]) -> tuple[BuilderDisposition, ...]:
    by_taxonomy: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        by_taxonomy[str(row.get("taxonomy", "unknown"))].append(row)
    expected_taxonomies = set(BUILDER_RETAIN_QUOTAS) | set(BUILDER_CONVERT_QUOTAS)
    if set(by_taxonomy) != expected_taxonomies:
        raise ValueError("migration builder taxonomies drifted from the approved cohort")
    result: list[BuilderDisposition] = []
    for taxonomy in sorted(by_taxonomy):
        ranked = sorted(
            by_taxonomy[taxonomy],
            key=lambda row: (-_builder_semantic_score(row), str(row.get("path", ""))),
        )
        retain_count = BUILDER_RETAIN_QUOTAS.get(taxonomy, 0)
        convert_count = BUILDER_CONVERT_QUOTAS.get(taxonomy, 0)
        if len(ranked) != retain_count + convert_count:
            raise ValueError(f"migration builder taxonomy count drifted: {taxonomy}")
        for index, row in enumerate(ranked):
            disposition = "retain_candidate" if index < retain_count else "convert_candidate"
            evidence_items = row.get("evidence", [])
            evidence_values = (
                cast(list[object], evidence_items) if isinstance(evidence_items, list) else []
            )
            evidence = tuple(str(item) for item in evidence_values)
            score = _builder_semantic_score(row)
            rationale = f"{taxonomy} semantic score {score} at rank {index + 1}/{len(ranked)}; " + (
                "historical/downgrade or schema exercise is preserved"
                if disposition == "retain_candidate"
                else "setup-only or lower-evidence path is converted"
            )
            result.append(
                BuilderDisposition(
                    path=str(row.get("path", "")),
                    taxonomy=taxonomy,
                    disposition=disposition,
                    owner=PROGRAM_OWNER,
                    selection_basis=(
                        f"semantic evidence score {score}, rank {index + 1} within {taxonomy}; "
                        "path is only a deterministic tie-break"
                    ),
                    selection_rank=index + 1,
                    evidence=evidence,
                    rationale=rationale,
                    exception_code=(
                        f"retain:{taxonomy}:{index + 1}"
                        if disposition == "retain_candidate"
                        else None
                    ),
                )
            )
    return tuple(sorted(result, key=lambda row: (row.path, row.taxonomy)))


def _builder_semantic_score(row: dict[str, object]) -> int:
    """Rank migration fixtures by observed behavior, not pathname order."""
    evidence = row.get("evidence", [])
    evidence_values = cast(list[object], evidence) if isinstance(evidence, list) else []
    items = {str(item) for item in evidence_values}
    weights = {
        "call:command.downgrade": 8,
        "sql:create trigger": 6,
        "sql:alter table": 5,
        "sql:create table": 3,
        "call:conn.executescript": 2,
        "call:legacy.executescript": 2,
        "call:database.executescript": 2,
        "call:seed.executescript": 2,
        "call:migrated_db": 1,
    }
    return sum(weight for marker, weight in weights.items() if marker in items)


def _selected_crossings(
    large_modules: tuple[LargeModule, ...], mandatory_paths: tuple[str, ...]
) -> tuple[LocCrossing, ...]:
    modules = {module.path: module.noncomment_loc for module in large_modules}
    ordered = (
        mandatory_paths
        + tuple(module.path for module in large_modules if module.path not in mandatory_paths)[
            : 56 - len(mandatory_paths)
        ]
    )
    return tuple(
        LocCrossing(
            path=path,
            baseline_loc=modules[path],
            target_cap=LOC_TARGET_CAPS.get(path, 1000),
            selection_basis=(
                "mandatory composition-root cap"
                if path in LOC_TARGET_CAPS
                else "largest non-comment module crossing, stable LOC order"
            ),
        )
        for path in ordered
    )


def _budget_mappings(
    cuts: list[tuple[str, str]],
    crossings: tuple[LocCrossing, ...],
    clusters: list[TypeDebtCluster],
    builders: tuple[BuilderDisposition, ...],
    suppression: SuppressionRetirement | None = None,
    function_lifecycle: FunctionLifecycleSnapshot | None = None,
    static_total: tuple[int, dict[str, int]] | None = None,
) -> tuple[BudgetMapping, ...]:
    """Allocate every measured item into the approved bottom-up PR matrix."""
    mappings: list[BudgetMapping] = []
    for index, (source, target) in enumerate(sorted(cuts), start=1):
        mappings.append(
            BudgetMapping(
                item_kind="scc_cut",
                item_id=f"{source}->{target}",
                slice_key="architecture-boundaries",
                work_package=f"architecture-scc-{index:02d}",
                units=1,
                estimated_prs=1,
                evidence=("architecture-ratchet:scc-cut",),
            )
        )
    crossing_order = sorted(
        range(len(crossings)),
        key=lambda index: (
            -(crossings[index].baseline_loc - crossings[index].target_cap),
            crossings[index].path,
        ),
    )
    crossing_packages = {index: rank + 1 for rank, index in enumerate(crossing_order)}
    for index, crossing in enumerate(crossings):
        mappings.append(
            BudgetMapping(
                item_kind="loc_crossing",
                item_id=crossing.path,
                slice_key="architecture-boundaries",
                work_package=f"architecture-loc-{crossing_packages[index]:02d}",
                units=1,
                estimated_prs=1 if crossing_packages[index] <= 37 else 0,
                evidence=(f"architecture-ratchet:loc:{crossing.baseline_loc}",),
            )
        )
    risk_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    cluster_order = sorted(
        range(len(clusters)),
        key=lambda index: (
            risk_rank[clusters[index].risk],
            -clusters[index].count,
            clusters[index].source_zone,
            clusters[index].rule,
        ),
    )
    cluster_packages = {
        index: (rank * 20 // len(clusters)) + 1 for rank, index in enumerate(cluster_order)
    }
    first_cluster_in_package = {
        package: min(index for index, value in cluster_packages.items() if value == package)
        for package in set(cluster_packages.values())
    }
    for index, cluster in enumerate(clusters):
        package = cluster_packages[index]
        mappings.append(
            BudgetMapping(
                item_kind="type_cluster",
                item_id=f"{cluster.source_zone}:{cluster.rule}",
                slice_key="active-type-debt",
                work_package=f"typing-cluster-{package:02d}",
                units=cluster.count,
                estimated_prs=1 if index == first_cluster_in_package[package] else 0,
                evidence=(cluster.evidence_sha256,),
            )
        )
    converted = [row for row in builders if row.disposition == "convert_candidate"]
    converted_order = sorted(
        converted,
        key=lambda row: (
            row.taxonomy,
            -_builder_semantic_score({"evidence": list(row.evidence)}),
            row.path,
        ),
    )
    conversion_package_by_key = {
        (row.path, row.taxonomy): (index * 22 // len(converted_order)) + 1
        for index, row in enumerate(converted_order)
    }
    first_builder_in_package = {
        package: min(
            index
            for index, row in enumerate(builders)
            if row.disposition == "convert_candidate"
            and conversion_package_by_key[(row.path, row.taxonomy)] == package
        )
        for package in range(1, 23)
    }
    for index, builder in enumerate(builders):
        package = (
            conversion_package_by_key[(builder.path, builder.taxonomy)]
            if builder.disposition == "convert_candidate"
            else 0
        )
        mappings.append(
            BudgetMapping(
                item_kind="builder",
                item_id=f"{builder.path}|{builder.taxonomy}",
                slice_key="migration-test-ci",
                work_package=(
                    f"migration-convert-{package:02d}" if package else "migration-retain-exception"
                ),
                units=1,
                estimated_prs=1 if package and index == first_builder_in_package[package] else 0,
                evidence=builder.evidence,
            )
        )
    anchors = {
        "integrity-audit": 9,
        "duplicate-authorities": 11,
        "lifecycle-pruning": 12,
        "final-static-zero": 12,
        "quality-closure": 2,
    }
    for slice_key, prs in anchors.items():
        mappings.append(
            BudgetMapping(
                item_kind="slice_anchor",
                item_id=f"anchor:{slice_key}",
                slice_key=slice_key,
                work_package=f"{slice_key}-anchor",
                units=1,
                estimated_prs=prs,
                evidence=(f"approved-estimate-matrix:{slice_key}",),
                candidate_count=(
                    function_lifecycle.candidate_count
                    if slice_key == "lifecycle-pruning" and function_lifecycle is not None
                    else 0
                ),
            )
        )
    if suppression is not None:
        mappings.append(
            BudgetMapping(
                item_kind="suppression",
                item_id="source-ignore-comments",
                slice_key="final-static-zero",
                work_package="suppression-retirement",
                units=suppression.baseline,
                estimated_prs=0,
                evidence=tuple(
                    f"source-ignore-comments:{rule}={count}"
                    for rule, count in sorted(suppression.source_rule_counts.items())
                ),
            )
        )
    if static_total is not None:
        _total, components = static_total
        for name in ("pyright-active", "ruff", "ruff-format"):
            mappings.append(
                BudgetMapping(
                    item_kind="static_quality",
                    item_id=name,
                    slice_key="final-static-zero",
                    work_package=f"static-quality-{name}",
                    units=components[name],
                    estimated_prs=0,
                    evidence=(f"{name}={components[name]}",),
                )
            )
    return tuple(mappings)


# Stable public names for the workflow's composition imports.  The original
# private names remain the implementation seams used by freeze tests.
architecture_edges = _architecture_edges
budget_mappings = _budget_mappings
builder_dispositions = _builder_dispositions
builder_semantic_score = _builder_semantic_score
duplicate_totals = _duplicate_totals
feedback_arc_cut = _feedback_arc_cut
git_commit = _git_commit
order_score = _order_score
read_json = _read_json
reconciliation = reconciliation_snapshot
reachability = reachability_evidence
function_lifecycle = function_lifecycle_snapshot
sccs = _sccs
selected_crossings = _selected_crossings
sha256 = _sha256
tracked_type_debt_authority = _tracked_type_debt_authority
type_debt = _type_debt
static_quality = static_quality_total
suppression_retirement = _suppression_retirement
upgrade_builder_rows = _upgrade_builder_rows
validate_performance_snapshot = _validate_performance_snapshot
