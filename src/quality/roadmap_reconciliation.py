"""Reconcile every provisional roadmap baseline against fresh typed receipts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Literal, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .architecture import ArchitectureReceipt, build_architecture_receipt
from .duplicates import DuplicateInventory, build_inventory
from .reachability import ReachabilityGraph, build_graph
from .static_quality import StaticQualityInventory, inventory
from .test_db_patterns import TestDbAudit, audit_test_db_patterns

Verdict = Literal["verified", "corrected", "rejected"]
ClaimValue = int | float | str | bool
ReceiptModel = TypeVar("ReceiptModel", bound=BaseModel)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    sha256: str
    locator: str


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    provisional_expected: ClaimValue | None
    observed: ClaimValue | None
    verdict: Verdict
    scored_eligible: bool
    evidence: Evidence | None
    provisional_evidence: Evidence | None
    note: str


class ReconciliationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "bha-121.v6"
    status: Literal["PASS", "HOLD"]
    claims: tuple[Claim, ...]
    scored_claims: int
    rejected_claims: int
    source_hash: str
    roadmap_source: Evidence
    claim_manifest_sha256: str
    violations: tuple[str, ...] = Field(default_factory=tuple)


@dataclass(frozen=True)
class CurrentReceipts:
    """Fresh generator results used to admit checked-in evidence."""

    architecture: ArchitectureReceipt
    duplicates: DuplicateInventory
    static: StaticQualityInventory
    test_db: TestDbAudit
    reachability: ReachabilityGraph


_ROADMAP_NAME = "quality-9plus-roadmap.md"
_ROADMAP_SHA256 = "b1fcd67d60783085faddb67e045e28d0b654a3ae2052dec2d4aea0da418bad1c"  # pragma: allowlist secret -- artifact digest
_ROADMAP_BASELINE_LINES = frozenset((*range(15, 37), *range(40, 44)))


@dataclass(frozen=True)
class _RoadmapFact:
    name: str
    pattern: str
    source: str | None
    locator: str
    extractor: Callable[[CurrentReceipts], ClaimValue | None]
    expected: ClaimValue | None = None


def _roadmap_path(root: Path) -> Path | None:
    candidates = (
        root / ".tmp" / _ROADMAP_NAME,
        root.parent / _ROADMAP_NAME,
    )
    return next((path for path in candidates if path.is_file()), None)


def _evidence_path(root: Path, path: Path | None) -> str:
    if path is None:
        return f".tmp/{_ROADMAP_NAME}"
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _fact_line_numbers(fact: _RoadmapFact, lines: list[str]) -> tuple[int, ...]:
    return tuple(
        number
        for number, line in enumerate(lines, 1)
        if number in _ROADMAP_BASELINE_LINES and re.search(fact.pattern, line)
    )


def _roadmap_facts() -> tuple[_RoadmapFact, ...]:
    def none(_current: CurrentReceipts) -> ClaimValue | None:
        return None

    return (
        _RoadmapFact(
            "production module count",
            r"1,291 Python modules and 554,615 LOC",
            "architecture",
            "$.metrics.executable_modules",
            lambda c: c.architecture.metrics.executable_modules,
            1291,
        ),
        _RoadmapFact(
            "production noncomment LOC",
            r"1,291 Python modules and 554,615 LOC",
            "architecture",
            "$.metrics.total_noncomment_loc",
            lambda c: c.architecture.metrics.total_noncomment_loc,
        ),
        _RoadmapFact(
            "maximum internal fan-out",
            r"Provisional static import topology: 4,725 internal edges",
            "architecture",
            "$.metrics.max_internal_fan_out",
            lambda c: c.architecture.metrics.max_internal_fan_out,
        ),
        _RoadmapFact(
            "modules >=1,000 LOC",
            r"119 files >=1,000 LOC",
            "architecture",
            "$.metrics.modules_over_1000_loc",
            lambda c: c.architecture.metrics.modules_over_1000_loc,
        ),
        _RoadmapFact(
            "modules >=2,000 LOC",
            r"26 >=2,000 LOC",
            "architecture",
            "$.metrics.modules_over_2000_loc",
            lambda c: c.architecture.metrics.modules_over_2000_loc,
        ),
        _RoadmapFact(
            "modules >=3,000 LOC",
            r"7 >=3,000 LOC",
            "architecture",
            "$.metrics.modules_at_least_3000_loc",
            lambda c: c.architecture.metrics.modules_at_least_3000_loc,
        ),
        _RoadmapFact("module LOC p95", r"p95 is 1,411 LOC", None, "unavailable", none),
        _RoadmapFact(
            "integrity_audit LOC", r"integrity_audit\.py`: 6,337 LOC", None, "unavailable", none
        ),
        _RoadmapFact(
            "integrity_audit functions",
            r"integrity_audit\.py`: 6,337 LOC, 77 functions",
            None,
            "unavailable",
            none,
        ),
        _RoadmapFact(
            "integrity_audit SQL sites",
            r"integrity_audit\.py`: 6,337 LOC, 77 functions, 59 SQL",
            None,
            "unavailable",
            none,
        ),
        _RoadmapFact(
            "comments_server LOC", r"comments_server\.py`: 5,824 LOC", None, "unavailable", none
        ),
        _RoadmapFact(
            "comments_server functions",
            r"comments_server\.py`: 5,824 LOC, 184 functions",
            None,
            "unavailable",
            none,
        ),
        _RoadmapFact(
            "comments_server routes", r"184 functions, 124 routes", None, "unavailable", none
        ),
        _RoadmapFact(
            "comments_server imports",
            r"124 routes in the root file, 89 imports",
            None,
            "unavailable",
            none,
        ),
        _RoadmapFact(
            "SCC internal edges",
            r"4,725 internal edges",
            "architecture",
            "$.definitions.internal_edges",
            lambda c: sum(module.internal_fan_out for module in c.architecture.metrics.modules),
        ),
        _RoadmapFact(
            "SCC count",
            r"4,725 internal edges and 16 strongly connected",
            "architecture",
            "$.metrics.scc_count",
            lambda c: c.architecture.metrics.scc_count,
        ),
        _RoadmapFact(
            "SCC modules",
            r"16 strongly connected components spanning 77 modules",
            "architecture",
            "$.metrics.scc_module_count",
            lambda c: c.architecture.metrics.scc_module_count,
        ),
        _RoadmapFact(
            "largest SCC",
            r"largest SCCs contain 24 and 16 modules",
            "architecture",
            "$.metrics.largest_scc",
            lambda c: c.architecture.metrics.largest_scc,
        ),
        _RoadmapFact(
            "exact duplicate groups",
            r"140 duplicate groups",
            "duplicates",
            "$.exact_totals.groups",
            lambda c: c.duplicates.exact_totals.groups,
        ),
        _RoadmapFact(
            "exact duplicate functions",
            r"140 duplicate groups covering 397 functions",
            "duplicates",
            "$.exact_totals.participating_functions",
            lambda c: c.duplicates.exact_totals.participating_functions,
        ),
        _RoadmapFact(
            "near-miss duplicate groups",
            r"Provisional exact AST-body inventory: 140 duplicate groups",
            "duplicates",
            "$.near_miss_totals.groups",
            lambda c: c.duplicates.near_miss_totals.groups,
        ),
        _RoadmapFact(
            "near-miss duplicated LOC",
            r"Provisional exact AST-body inventory: 140 duplicate groups",
            "duplicates",
            "$.near_miss_totals.duplicated_loc",
            lambda c: c.duplicates.near_miss_totals.duplicated_loc,
        ),
        _RoadmapFact(
            "exact duplicated LOC",
            r"140 duplicate groups covering 397 functions",
            "duplicates",
            "$.exact_totals.duplicated_loc",
            lambda c: c.duplicates.exact_totals.duplicated_loc,
        ),
        _RoadmapFact(
            "duplicate coverage",
            r"140 duplicate groups covering 397 functions",
            None,
            "unavailable",
            none,
        ),
        _RoadmapFact("passed tests", r"14,043 passed", None, "unavailable", none),
        _RoadmapFact("skipped tests", r"62 skipped", None, "unavailable", none),
        _RoadmapFact("full suite seconds", r"1,046\.92 seconds", None, "unavailable", none),
        _RoadmapFact(
            "Ruff diagnostics",
            r"found 2 whole-tree Ruff errors",
            "static",
            "$.diagnostics[tool=ruff].count",
            lambda c: _diagnostic_count(c.static, "ruff"),
        ),
        _RoadmapFact(
            "Ruff format files",
            r"61 files needing Ruff formatting",
            "static",
            "$.diagnostics[tool=ruff-format].count",
            lambda c: _diagnostic_count(c.static, "ruff-format"),
        ),
        _RoadmapFact(
            "strict Pyright diagnostics",
            r"reported 27,924",
            "static",
            "$.diagnostics[tool=pyright].count",
            lambda c: _diagnostic_count(c.static, "pyright"),
        ),
        _RoadmapFact(
            "Pyright omitted files", r"omit 312 tracked Python files", None, "unavailable", none
        ),
        _RoadmapFact(
            "type ignore directives",
            r"253 `# type: ignore`",
            "static",
            "$.suppressions_by_file[# type: ignore]",
            lambda c: sum(
                v.get("# type: ignore", 0) for v in c.static.suppressions_by_file.values()
            ),
        ),
        _RoadmapFact(
            "pyright ignore directives",
            r"335 `# pyright: ignore`",
            "static",
            "$.suppressions_by_file[# pyright: ignore]",
            lambda c: sum(
                v.get("# pyright: ignore", 0) for v in c.static.suppressions_by_file.values()
            ),
        ),
        _RoadmapFact(
            "suppression files",
            r"across 207 files",
            "static",
            "$.suppressions_by_file",
            lambda c: len(c.static.suppressions_by_file),
        ),
        _RoadmapFact(
            "test files",
            r"1,092 test files",
            "test_db",
            "$.tracked_test_files",
            lambda c: len(c.test_db.tracked_test_files),
        ),
        _RoadmapFact(
            "test files with direct command.upgrade",
            r"172 files still contain direct `command\.upgrade`",
            "test_db",
            "$.database_builders[*].evidence contains call:command.upgrade",
            lambda c: _count_upgrade_builders(c.test_db),
        ),
        _RoadmapFact(
            "migrated_db files",
            r"146 already use `migrated_db`",
            "test_db",
            "$.database_builders[*].evidence contains call:migrated_db",
            lambda c: _count_evidence_builders(c.test_db, "call:migrated_db"),
        ),
        _RoadmapFact(
            "hand-written DDL files",
            r"550 contain hand-written DDL",
            "test_db",
            "$.database_builders[*].evidence contains sql:",
            lambda c: _count_evidence_builders(c.test_db, "sql:"),
        ),
        _RoadmapFact("direct migration seconds", r"18-56 seconds", None, "unavailable", none),
        _RoadmapFact("cached template milliseconds", r"13\.5 ms", None, "unavailable", none),
        _RoadmapFact("CSS census seconds", r"44\.83 seconds", None, "unavailable", none),
        _RoadmapFact("architecture/UI group seconds", r"76\.47 seconds", None, "unavailable", none),
        _RoadmapFact("emitter AST parses", r"6,454 AST parses", None, "unavailable", none),
        _RoadmapFact("emitter AST walks", r"32\.45M AST walks", None, "unavailable", none),
        _RoadmapFact("emitter calls", r"517\.7M calls", None, "unavailable", none),
        _RoadmapFact(
            "integrity query families",
            r"six verified data-proportional query families",
            None,
            "unavailable",
            none,
        ),
        _RoadmapFact(
            "renderer db_path-only count", r"48 top-level renderers", None, "unavailable", none
        ),
        _RoadmapFact(
            "connect_sqlite sites",
            r"76 static `connect_sqlite` sites across 42 files",
            None,
            "unavailable",
            none,
        ),
        _RoadmapFact(
            "connect_sqlite files",
            r"76 static `connect_sqlite` sites across 42 files",
            None,
            "unavailable",
            none,
        ),
        _RoadmapFact(
            "executable entrypoints",
            r"375 executable `__main__` entrypoints",
            None,
            "unavailable",
            none,
        ),
        _RoadmapFact("Flask endpoints", r"181 Flask endpoints", None, "unavailable", none),
        _RoadmapFact("scheduled tasks", r"46 scheduled tasks", None, "unavailable", none),
        _RoadmapFact("wrappers", r"47 wrappers", None, "unavailable", none),
        _RoadmapFact("managed services", r"2 managed services", None, "unavailable", none),
        _RoadmapFact(
            "reconstruction entrypoints",
            r"29 reconstruction entrypoints",
            None,
            "unavailable",
            none,
        ),
        _RoadmapFact("unreachable scripts", r"85 executable scripts", None, "unavailable", none),
        _RoadmapFact("unreachable queue LOC", r"totaling 14,291 LOC", None, "unavailable", none),
        _RoadmapFact(
            "Muse elegance grade", r"Fresh Muse grade: elegance 6", None, "unavailable", none
        ),
        _RoadmapFact(
            "Muse maintainability grade",
            r"Fresh Muse grade: elegance 6, maintainability 5",
            None,
            "unavailable",
            none,
        ),
        _RoadmapFact(
            "Muse runtime efficiency grade", r"runtime efficiency 6", None, "unavailable", none
        ),
        _RoadmapFact(
            "Muse cleanup readiness grade", r"cleanup readiness 6", None, "unavailable", none
        ),
        _RoadmapFact("Muse overall grade", r"overall 6", None, "unavailable", none),
        _RoadmapFact(
            "portfolio_panel LOC",
            r"portfolio_panel\.py`: 3,602 LOC, 92 functions",
            None,
            "unavailable",
            none,
        ),
        _RoadmapFact(
            "portfolio_panel functions",
            r"portfolio_panel\.py`: 3,602 LOC, 92 functions",
            None,
            "unavailable",
            none,
        ),
        _RoadmapFact(
            "work_os_shell LOC", r"work_os_shell\.py`: 3,308 LOC", None, "unavailable", none
        ),
        _RoadmapFact(
            "issuer_registry_bootstrap LOC",
            r"issuer_registry_bootstrap\.py`: 3,159 LOC",
            None,
            "unavailable",
            none,
        ),
        _RoadmapFact("gc_recovery LOC", r"gc_recovery\.py`: 3,125 LOC", None, "unavailable", none),
        _RoadmapFact(
            "conformance_scan LOC", r"conformance_scan\.py`: 3,098 LOC", None, "unavailable", none
        ),
        _RoadmapFact(
            "largest SCC second size",
            r"largest SCCs contain 24 and 16 modules",
            "architecture",
            "$.metrics.strongly_connected_components[1]",
            lambda c: (
                sorted(
                    (
                        len(component)
                        for component in c.architecture.metrics.strongly_connected_components
                    ),
                    reverse=True,
                )[1]
                if len(c.architecture.metrics.strongly_connected_components) > 1
                else 0
            ),
        ),
        _RoadmapFact(
            "duplicate logging configurators",
            r"11 logging configurators",
            None,
            "unavailable",
            none,
        ),
        _RoadmapFact("duplicate file hashes", r"12 file hashes", None, "unavailable", none),
        _RoadmapFact(
            "duplicate savepoint helpers", r"6 savepoint helpers", None, "unavailable", none
        ),
        _RoadmapFact(
            "duplicate Form 10-K locators", r"4 Form 10-K locators", None, "unavailable", none
        ),
        _RoadmapFact(
            "canonical_json definitions",
            r"46 public/private `canonical_json` definitions",
            None,
            "unavailable",
            none,
        ),
        _RoadmapFact(
            "_db_time definitions", r"11 `_db_time` definitions", None, "unavailable", none
        ),
        _RoadmapFact(
            "file-hash definitions", r"9 file-hash definitions", None, "unavailable", none
        ),
        _RoadmapFact("full suite minutes", r"\(17m26s\)", None, "unavailable", none),
        _RoadmapFact("full suite display seconds", r"\(17m26s\)", None, "unavailable", none),
        _RoadmapFact(
            "immutable migration formatted files", r"50 of those 61", None, "unavailable", none
        ),
        _RoadmapFact("documented Pyright diagnostics", r"roughly 3,070", None, "unavailable", none),
        _RoadmapFact("Sol Pyright diagnostics", r"found 3,271", None, "unavailable", none),
        _RoadmapFact(
            "Pyright files scanned", r"27,924 over 2,380 files", None, "unavailable", none
        ),
        _RoadmapFact("migration replay lower bound", r"18-56 seconds", None, "unavailable", none),
        _RoadmapFact("migration replay upper bound", r"18-56 seconds", None, "unavailable", none),
        _RoadmapFact("emitter AST walks M", r"32\.45M AST walks", None, "unavailable", none),
        _RoadmapFact("emitter calls M", r"517\.7M calls", None, "unavailable", none),
        _RoadmapFact(
            "comments_server root routes", r"124 routes in the root file", None, "unavailable", none
        ),
        _RoadmapFact(
            "build_redesigned_dcf correction LOC",
            r"build_redesigned_dcf\.py` exists, is live, and is 2,369 LOC",
            None,
            "unavailable",
            none,
        ),
        _RoadmapFact(
            "direct-builder ratchet correction",
            r"returns 172 direct-builder files and has a cap of 172",
            "test_db",
            "$.database_builders[*].evidence contains call:command.upgrade",
            lambda c: _count_upgrade_builders(c.test_db),
        ),
        _RoadmapFact(
            "theme_synth live-edge correction",
            r"`src/synthesis/theme_synth\.py`.*are live through edges",
            "reachability",
            "$.edges[target=src/synthesis/theme_synth.py]",
            lambda c: _has_runtime_incoming(c.reachability, "src/synthesis/theme_synth.py"),
        ),
        _RoadmapFact(
            "filings.boilerplate_classify live-edge correction",
            r"filings\.boilerplate_classify.*are live through edges",
            "reachability",
            "$.edges[target=src/filings/boilerplate_classify.py]",
            lambda c: _has_runtime_incoming(c.reachability, "src/filings/boilerplate_classify.py"),
        ),
        _RoadmapFact(
            "filings.cross_sectional_detrend live-edge correction",
            r"filings\.cross_sectional_detrend.*are live through edges",
            "reachability",
            "$.edges[target=src/filings/cross_sectional_detrend.py]",
            lambda c: _has_runtime_incoming(
                c.reachability, "src/filings/cross_sectional_detrend.py"
            ),
        ),
        _RoadmapFact(
            "ask.turn_cache live-edge correction",
            r"ask\.turn_cache.*are live through edges",
            "reachability",
            "$.edges[target=src/ask/turn_cache.py]",
            lambda c: _has_runtime_incoming(c.reachability, "src/ask/turn_cache.py"),
        ),
        _RoadmapFact(
            "etf_sources.vanguard live-edge correction",
            r"etf_sources\.vanguard.*are live through edges",
            "reachability",
            "$.edges[target=src/etf_sources/vanguard.py]",
            lambda c: _has_runtime_incoming(c.reachability, "src/etf_sources/vanguard.py"),
        ),
        _RoadmapFact(
            "refetch_aggregator absence correction",
            r"`execution/refetch_aggregator\.py` does not exist",
            "architecture",
            "$.metrics.modules[path=execution/refetch_aggregator.py]",
            lambda c: not _has_module(c.architecture, "execution/refetch_aggregator.py"),
        ),
        _RoadmapFact(
            "refetch_aggregator_transcripts existence correction",
            r"relevant file is `execution/refetch_aggregator_transcripts\.py`",
            "architecture",
            "$.metrics.modules[path=execution/refetch_aggregator_transcripts.py]",
            lambda c: _has_module(c.architecture, "execution/refetch_aggregator_transcripts.py"),
        ),
    )


class ReceiptSource(Generic[ReceiptModel]):
    """A checked-in receipt admitted only when it equals a fresh typed result."""

    def __init__(
        self,
        root: Path,
        relative: str,
        model: type[ReceiptModel],
        current: ReceiptModel,
        *,
        ignored_identity_fields: set[str],
        status: Callable[[ReceiptModel], bool] = lambda _receipt: True,
        normalize: Callable[[ReceiptModel], object] | None = None,
    ) -> None:
        self.relative = relative
        self.path = root / relative
        self.raw: bytes | None = self.path.read_bytes() if self.path.is_file() else None
        self.receipt: ReceiptModel | None = None
        self.rejection: str | None = None
        if self.raw is None:
            self.rejection = "receipt is missing"
            return
        try:
            parsed = model.model_validate_json(self.raw)
        except ValidationError:
            self.rejection = "receipt failed its typed schema"
            return
        if not status(parsed):
            self.rejection = "receipt status, violations, or parse errors are not admissible"
            return
        stored = parsed.model_dump(exclude=ignored_identity_fields)
        fresh = current.model_dump(exclude=ignored_identity_fields)
        if normalize is not None:
            stored = normalize(parsed)
            fresh = normalize(current)
        if stored != fresh:
            self.rejection = "receipt does not exactly reproduce the fresh generator result"
            return
        self.receipt = parsed

    @property
    def admitted(self) -> bool:
        return self.receipt is not None

    def evidence(self, locator: str) -> Evidence | None:
        if self.raw is None:
            return None
        return Evidence(
            path=self.relative,
            sha256=hashlib.sha256(self.raw).hexdigest(),
            locator=locator,
        )


def _fresh_receipts(root: Path) -> CurrentReceipts:
    return CurrentReceipts(
        architecture=build_architecture_receipt(root, "WORKTREE"),
        duplicates=build_inventory(root, "WORKTREE"),
        static=inventory(root),
        test_db=audit_test_db_patterns(root),
        reachability=build_graph(root),
    )


def _count_upgrade_builders(receipt: TestDbAudit) -> int:
    return sum("call:command.upgrade" in builder.evidence for builder in receipt.database_builders)


def _count_evidence_builders(receipt: TestDbAudit, prefix: str) -> int:
    return sum(
        any(item.startswith(prefix) for item in builder.evidence)
        for builder in receipt.database_builders
    )


def _has_module(receipt: ArchitectureReceipt, path: str) -> bool:
    return any(module.path == path for module in receipt.metrics.modules)


def _has_runtime_incoming(receipt: ReachabilityGraph, target: str) -> bool:
    excluded_sources = ("tests/", "instruction_tests/", "docs/", "directives/")
    return any(
        edge.target.replace("\\", "/") == target
        and edge.line is not None
        and not edge.unknown
        and edge.kind not in {"unknown", "directive", "reconstruction"}
        and not edge.source.startswith(excluded_sources)
        for edge in receipt.edges
    )


def _diagnostic_count(receipt: StaticQualityInventory, tool: str) -> int | None:
    matches = [item.count for item in receipt.diagnostics if item.tool == tool]
    return matches[0] if len(matches) == 1 else None


def _static_semantics(receipt: StaticQualityInventory) -> dict[str, object]:
    payload = receipt.model_dump(exclude={"scoped_commit", "repo_root"})
    diagnostics = payload.get("diagnostics")
    if isinstance(diagnostics, list):
        for raw in cast(list[object], diagnostics):
            if isinstance(raw, dict):
                cast(dict[object, object], raw).pop("receipt_path", None)
    return payload


def reconcile(
    root: Path, *, current_receipts: CurrentReceipts | None = None
) -> ReconciliationReceipt:
    root = root.resolve()
    current = current_receipts or _fresh_receipts(root)
    architecture = ReceiptSource(
        root,
        "docs/quality/architecture-ratchet.json",
        ArchitectureReceipt,
        current.architecture,
        ignored_identity_fields={"scoped_commit"},
        status=lambda receipt: receipt.scoped_revision == "WORKTREE",
    )
    duplicates = ReceiptSource(
        root,
        "docs/quality/duplicates-ratchet.json",
        DuplicateInventory,
        current.duplicates,
        ignored_identity_fields={"commit_hash"},
        status=lambda receipt: (
            receipt.schema_version == "1"
            and receipt.scoped_revision == "WORKTREE"
            and not receipt.parse_errors
        ),
    )
    static = ReceiptSource(
        root,
        "docs/quality/static-baseline.json",
        StaticQualityInventory,
        current.static,
        ignored_identity_fields={"scoped_commit"},
        status=lambda receipt: (
            receipt.schema_version == "bha-120.v2"
            and receipt.status == "PASS"
            and not receipt.violations
            and sorted(item.tool for item in receipt.diagnostics)
            == ["pyright", "ruff", "ruff-format", "source-ignore-comments"]
        ),
        normalize=_static_semantics,
    )
    test_db = ReceiptSource(
        root,
        "docs/quality/test-db-patterns-baseline.json",
        TestDbAudit,
        current.test_db,
        ignored_identity_fields={"scoped_commit"},
        status=lambda receipt: receipt.status == "PASS" and not receipt.violations,
    )
    reachability = ReceiptSource(
        root,
        ".tmp/quality/reachability-check.json",
        ReachabilityGraph,
        current.reachability,
        ignored_identity_fields=set(),
        status=lambda receipt: not receipt.hold,
    )
    sources = (architecture, duplicates, static, test_db, reachability)
    claims: list[Claim] = []

    roadmap_path = _roadmap_path(root)
    try:
        roadmap_raw = roadmap_path.read_bytes() if roadmap_path else None
    except OSError:
        roadmap_raw = None
    roadmap_hash = hashlib.sha256(roadmap_raw).hexdigest() if roadmap_raw else None
    roadmap_decode_error = False
    try:
        roadmap_lines = roadmap_raw.decode("utf-8").splitlines() if roadmap_raw else []
    except UnicodeDecodeError:
        roadmap_lines = []
        roadmap_decode_error = True
    roadmap_admitted = (
        roadmap_raw is not None and not roadmap_decode_error and roadmap_hash == _ROADMAP_SHA256
    )
    roadmap_evidence = Evidence(
        path=_evidence_path(root, roadmap_path),
        sha256=roadmap_hash or "0" * 64,
        locator="baseline section",
    )

    def add(
        fact: _RoadmapFact,
        expected: ClaimValue | None,
        source: ReceiptSource[ArchitectureReceipt]
        | ReceiptSource[DuplicateInventory]
        | ReceiptSource[StaticQualityInventory]
        | ReceiptSource[TestDbAudit]
        | ReceiptSource[ReachabilityGraph]
        | None,
    ) -> None:
        observed = fact.extractor(current) if source is not None and source.admitted else None
        if not roadmap_admitted:
            observed = None
        if expected is None:
            verdict = "rejected"
            eligible = False
            note = "The provisional roadmap does not state a value for this derived metric."
        elif observed is None:
            verdict: Verdict = "rejected"
            eligible = False
            if not roadmap_admitted:
                note = "Roadmap source is missing or tampered."
            elif source is not None and source.rejection:
                note = source.rejection
            else:
                note = "No admissible typed generator receipt exists for this roadmap fact."
        else:
            verdict = "verified" if observed == expected else "corrected"
            eligible = True
            note = (
                "Fresh typed generator reproduces the provisional value."
                if verdict == "verified"
                else "Fresh typed generator corrects the provisional value."
            )
        claims.append(
            Claim(
                name=fact.name,
                provisional_expected=expected,
                observed=observed,
                verdict=verdict,
                scored_eligible=eligible,
                evidence=source.evidence(fact.locator) if source else None,
                provisional_evidence=Evidence(
                    path=roadmap_evidence.path,
                    sha256=roadmap_evidence.sha256,
                    locator=f"line {next(iter(_fact_line_numbers(fact, roadmap_lines)), 0)}",
                )
                if roadmap_raw
                else None,
                note=note,
            )
        )

    source_map = {
        "architecture": architecture,
        "duplicates": duplicates,
        "static": static,
        "test_db": test_db,
        "reachability": reachability,
    }
    provisional = {
        "production module count": 1291,
        "production noncomment LOC": 554615,
        "maximum internal fan-out": None,
        "modules >=1,000 LOC": 119,
        "modules >=2,000 LOC": 26,
        "modules >=3,000 LOC": 7,
        "module LOC p95": 1411,
        "integrity_audit LOC": 6337,
        "integrity_audit functions": 77,
        "integrity_audit SQL sites": 59,
        "comments_server LOC": 5824,
        "comments_server functions": 184,
        "comments_server routes": 124,
        "comments_server imports": 89,
        "SCC internal edges": 4725,
        "SCC count": 16,
        "SCC modules": 77,
        "largest SCC": 24,
        "largest SCC second size": 16,
        "exact duplicate groups": 140,
        "exact duplicate functions": 397,
        "near-miss duplicate groups": None,
        "near-miss duplicated LOC": None,
        "passed tests": 14043,
        "skipped tests": 62,
        "full suite seconds": 1046.92,
        "full suite minutes": 17,
        "full suite display seconds": 26,
        "Ruff diagnostics": 2,
        "Ruff format files": 61,
        "strict Pyright diagnostics": 27924,
        "Pyright omitted files": 312,
        "type ignore directives": 253,
        "pyright ignore directives": 335,
        "suppression files": 207,
        "test files": 1092,
        "test files with direct command.upgrade": 172,
        "migrated_db files": 146,
        "hand-written DDL files": 550,
        "CSS census seconds": 44.83,
        "architecture/UI group seconds": 76.47,
        "emitter AST parses": 6454,
        "emitter AST walks": 32450000,
        "emitter calls": 517700000,
        "integrity query families": 6,
        "renderer db_path-only count": 48,
        "connect_sqlite sites": 76,
        "connect_sqlite files": 42,
        "executable entrypoints": 375,
        "Flask endpoints": 181,
        "scheduled tasks": 46,
        "wrappers": 47,
        "managed services": 2,
        "reconstruction entrypoints": 29,
        "unreachable scripts": 85,
        "unreachable queue LOC": 14291,
        "Muse elegance grade": 6,
        "Muse maintainability grade": 5,
        "Muse runtime efficiency grade": 6,
        "Muse cleanup readiness grade": 6,
        "Muse overall grade": 6,
        "portfolio_panel LOC": 3602,
        "portfolio_panel functions": 92,
        "work_os_shell LOC": 3308,
        "issuer_registry_bootstrap LOC": 3159,
        "gc_recovery LOC": 3125,
        "conformance_scan LOC": 3098,
        "duplicate logging configurators": 11,
        "duplicate file hashes": 12,
        "duplicate savepoint helpers": 6,
        "duplicate Form 10-K locators": 4,
        "canonical_json definitions": 46,
        "_db_time definitions": 11,
        "file-hash definitions": 9,
        "immutable migration formatted files": 50,
        "documented Pyright diagnostics": 3070,
        "Sol Pyright diagnostics": 3271,
        "Pyright files scanned": 2380,
        "build_redesigned_dcf correction LOC": 2369,
        "direct-builder ratchet correction": 172,
        "theme_synth live-edge correction": True,
        "filings.boilerplate_classify live-edge correction": True,
        "filings.cross_sectional_detrend live-edge correction": True,
        "ask.turn_cache live-edge correction": True,
        "etf_sources.vanguard live-edge correction": True,
        "refetch_aggregator absence correction": True,
        "refetch_aggregator_transcripts existence correction": True,
    }
    for fact in _roadmap_facts():
        add(
            fact,
            provisional.get(fact.name, fact.expected),
            source_map.get(fact.source) if fact.source is not None else None,
        )

    manifest_payload = json.dumps(
        [
            {
                "expected": provisional.get(fact.name, fact.expected),
                "locator": fact.locator,
                "name": fact.name,
                "pattern": fact.pattern,
                "source": fact.source,
            }
            for fact in _roadmap_facts()
        ],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    violations = [
        f"inadmissible source receipt {source.relative}: {source.rejection}"
        for source in sources
        if not source.admitted
    ]
    for fact in _roadmap_facts():
        if len(_fact_line_numbers(fact, roadmap_lines)) != 1:
            violations.append(f"roadmap locator is missing or ambiguous: {fact.name}")
    actual_names = [claim.name for claim in claims]
    if len(actual_names) != len(set(actual_names)):
        violations.append("roadmap claim set is incomplete or duplicated")
    if roadmap_raw is None:
        violations.append("roadmap source is missing")
    elif roadmap_hash != _ROADMAP_SHA256:
        violations.append("roadmap source hash is not the registered baseline")
    if roadmap_decode_error:
        violations.append("roadmap source is not valid UTF-8")
    for claim in claims:
        eligible_shape = (
            claim.verdict in {"verified", "corrected"}
            and claim.observed is not None
            and claim.evidence is not None
            and claim.provisional_evidence is not None
        )
        if claim.scored_eligible != eligible_shape:
            violations.append(f"claim eligibility is inconsistent: {claim.name}")

    digest = hashlib.sha256()
    for source in sources:
        if source.raw is not None:
            digest.update(source.relative.encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(source.raw).digest())
        else:
            digest.update(source.relative.encode())
            digest.update(b"\0MISSING")
    digest.update(b"roadmap\0")
    digest.update(hashlib.sha256(roadmap_raw).digest() if roadmap_raw else b"MISSING")
    return ReconciliationReceipt(
        status="HOLD" if violations else "PASS",
        claims=tuple(claims),
        scored_claims=sum(claim.scored_eligible for claim in claims),
        rejected_claims=sum(claim.verdict == "rejected" for claim in claims),
        source_hash=digest.hexdigest(),
        roadmap_source=roadmap_evidence,
        claim_manifest_sha256=hashlib.sha256(manifest_payload).hexdigest(),
        violations=tuple(violations),
    )
