"""Typed roadmap-reconciliation producer (R1 admission generator identity)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from quality.architecture import ArchitectureReceipt, build_architecture_receipt
from quality.duplicates import DuplicateInventory, build_inventory
from quality.git_env import clean_local_git_env
from quality.reachability import ReachabilityGraph, build_graph
from quality.roadmap_source import (
    ROADMAP_CLAIM_MAP_PATH,
    RoadmapClaimBinding,
    RoadmapSourceError,
    load_roadmap_claim_map_with_raw,
    parse_roadmap_claim_map,
)
from quality.static_quality import StaticQualityInventory, inventory
from quality.test_db_patterns import TestDbAudit, audit_test_db_patterns

ClaimValue = int | float | str | bool
SourceKey = Literal["architecture", "duplicates", "static", "test_db", "reachability"]
Verdict = Literal["verified", "corrected", "rejected"]

JsonValue = dict[str, "JsonValue"] | list["JsonValue"] | str | int | float | bool | None
_RUNTIME_PREFIXES = ("src/", "execution/", "cron/", "scripts/", ".github/")
_RUNTIME_SUFFIXES = (".bat", ".cmd", ".ps1", ".sh", ".service")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Evidence(Strict):
    path: str = Field(min_length=1, max_length=300)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    locator: str = Field(min_length=1, max_length=300)


class Claim(Strict):
    name: str = Field(min_length=1, max_length=200)
    provisional_expected: ClaimValue | None = None
    observed: ClaimValue | None = None
    verdict: Verdict
    scored_eligible: bool
    evidence: Evidence | None = None
    provisional_evidence: Evidence | None = None
    note: str = Field(max_length=300)

    @model_validator(mode="after")
    def verified_claim_has_exact_value_type(self) -> Claim:
        if self.verdict == "verified" and (
            self.provisional_expected is None
            or self.observed is None
            or type(self.provisional_expected) is not type(self.observed)
            or self.provisional_expected != self.observed
        ):
            raise ValueError("verified claim requires an exact typed observed value")
        return self


class CurrentReceipts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    architecture: ArchitectureReceipt
    duplicates: DuplicateInventory
    static: StaticQualityInventory
    test_db: TestDbAudit
    reachability: ReachabilityGraph


class ReconciliationReceipt(Strict):
    schema_version: Literal["roadmap-reconciliation-v1"] = "roadmap-reconciliation-v1"
    status: Literal["PASS", "HOLD"]
    claims: tuple[Claim, ...] = Field(min_length=1)
    scored_claims: int = Field(ge=0)
    rejected_claims: int = Field(ge=0)
    subject_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    worktree_dirty: bool | None = None
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    roadmap_source: Evidence | None = None
    claim_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    violations: tuple[str, ...] = Field(default_factory=tuple, max_length=20)

    @model_validator(mode="after")
    def pass_requires_clean_subject(self) -> ReconciliationReceipt:
        if self.status == "PASS" and (
            self.subject_commit is None or self.worktree_dirty is not False
        ):
            raise ValueError("PASS reconciliation requires an exact clean Git subject")
        return self


SOURCE_PATHS: dict[SourceKey, str] = {
    "architecture": "docs/quality/architecture-ratchet.json",
    "duplicates": "docs/quality/duplicates-ratchet.json",
    "static": "docs/quality/static-baseline.json",
    "test_db": "docs/quality/test-db-patterns-baseline.json",
    "reachability": ".tmp/quality/reachability-check.json",
}
VOLATILE_FIELDS: dict[str, set[str]] = {
    "architecture": {"scoped_commit", "scoped_revision"},
    "duplicates": {"commit_hash", "scoped_revision"},
    "static": {"scoped_commit", "repo_root", "receipt_identity"},
    "test_db": {"scoped_commit"},
    "reachability": {"subject_commit"},
}
ROADMAP_CANDIDATES = ("docs/quality/quality-9plus-roadmap.md",)
VIOLATION_CAP = 20
VIOLATION_TEXT_CAP = 200
_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_TEST_SUBJECT_COMMIT = "a" * 40

StagedKey = Literal["architecture", "duplicates", "static", "test_db", "reachability", "roadmap"]
STAGED_KEYS: tuple[StagedKey, ...] = (
    "architecture",
    "duplicates",
    "static",
    "test_db",
    "reachability",
    "roadmap",
)
_STAGED_JSON_KEYS: tuple[SourceKey, ...] = (
    "architecture",
    "duplicates",
    "static",
    "test_db",
    "reachability",
)


class StagedManifestEntry(Strict):
    path: str = Field(min_length=1, max_length=300)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class StagedManifestModel(Strict):
    architecture: StagedManifestEntry
    duplicates: StagedManifestEntry
    static: StagedManifestEntry
    test_db: StagedManifestEntry
    reachability: StagedManifestEntry
    roadmap: StagedManifestEntry
    roadmap_claims: StagedManifestEntry | None = None


class StagedManifestError(ValueError):
    pass


@dataclass(frozen=True)
class _StagedFileSnapshot:
    resolved: Path
    data: bytes
    dev: int
    ino: int


@dataclass(frozen=True)
class _StagedLoad:
    staged_root: Path | None
    entries: dict[StagedKey, StagedManifestEntry]
    snapshots: dict[StagedKey, _StagedFileSnapshot]
    manifest_data: bytes | None
    manifest_dev: int | None
    manifest_ino: int | None
    violations: tuple[str, ...]
    roadmap_claims_entry: StagedManifestEntry | None = None
    roadmap_claims_snapshot: _StagedFileSnapshot | None = None


def _is_hex40(value: str | None) -> bool:
    return isinstance(value, str) and _HEX40_RE.fullmatch(value) is not None


def _git_state(root: Path) -> tuple[str | None, bool | None]:
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD^{commit}"],
            capture_output=True,
            text=True,
            check=True,
            env=clean_local_git_env(),
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return None, None
    try:
        porcelain = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            check=True,
            env=clean_local_git_env(),
        ).stdout
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return head, None
    return head, bool(porcelain.strip())


def _git_state_violations(
    *states: tuple[str | None, bool | None],
) -> tuple[str, ...]:
    violations: list[str] = []
    if any(subject is None or dirty is None for subject, dirty in states):
        violations.append("git identity is unavailable")
    if any(subject is not None and not _is_hex40(subject) for subject, _dirty in states):
        violations.append("git HEAD subject is not a 40-hex commit")
    if states and states[0][1] is True:
        violations.append("git worktree is dirty before collection")
    if any(dirty is True for _subject, dirty in states[1:]):
        violations.append("git worktree is dirty after collection")
    if states and any(subject != states[0][0] for subject, _dirty in states[1:]):
        violations.append("git HEAD changed during collection")
    if states and any(dirty != states[0][1] for _subject, dirty in states[1:]):
        violations.append("git worktree state changed during collection")
    return tuple(violations)


def _safe_source_path(root: Path, rel: str) -> Path | None:
    """Return the lexical source path only when it is free of symlinks."""
    lexical = root / rel
    try:
        resolved = lexical.resolve()
    except OSError:
        return None
    if resolved != lexical:
        return None
    try:
        if resolved.relative_to(root) != Path(rel):
            return None
    except ValueError:
        return None
    try:
        if lexical.is_symlink():
            return None
        if not lexical.is_file():
            return None
    except OSError:
        return None
    return lexical


def _ignored_source_snapshot_violations(
    root: Path, receipt: ReconciliationReceipt
) -> tuple[str, ...]:
    expected = {
        claim.evidence.path: claim.evidence.sha256
        for claim in receipt.claims
        if claim.evidence is not None
    }
    violations: list[str] = []
    for rel in SOURCE_PATHS.values():
        if not rel.startswith(".tmp/") or rel not in expected:
            continue
        safe = _safe_source_path(root, rel)
        try:
            if safe is None:
                raise OSError("ignored source receipt is unavailable")
            current_hash = hashlib.sha256(safe.read_bytes()).hexdigest()
        except OSError:
            current_hash = ""
        if current_hash != expected[rel]:
            violations.append(f"ignored source receipt changed during collection: {rel}")
    return tuple(violations)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate object key: {key}")
        seen[key] = value
    return seen


def _validate_unique_json_keys(raw: bytes) -> None:
    json.loads(raw, object_pairs_hook=_reject_duplicate_keys)


@dataclass(frozen=True)
class RoadmapFact:
    name: str
    pattern: str
    source: SourceKey | None
    locator: str
    extractor: Callable[[CurrentReceipts], ClaimValue | None]
    provisional: ClaimValue | None = None


def _diag_count(inv: StaticQualityInventory, tool: str) -> int | None:
    hits = [int(d.count) for d in inv.diagnostics if d.tool == tool]
    return hits[0] if len(hits) == 1 else None


def _evidence_count(audit: TestDbAudit, prefix: str) -> int:
    total = 0
    for b in audit.database_builders:
        if any(str(e).startswith(prefix) for e in b.evidence):
            total += 1
    return total


def _has_edge(graph: ReachabilityGraph, target: str) -> bool:
    for edge in graph.edges:
        if edge.target != target or edge.unknown or edge.line is None:
            continue
        if edge.source.startswith(("tests/", "instruction_tests/")):
            continue
        if not (
            edge.source.startswith(_RUNTIME_PREFIXES)
            or edge.source == "Makefile"
            or edge.source.endswith(_RUNTIME_SUFFIXES)
        ):
            continue
        if edge.kind in {"directive", "reconstruction"}:
            continue
        if edge.kind == "unknown" and edge.reviewed_disposition != "internal_python_target":
            continue
        return True
    return False


def _has_module(arch: ArchitectureReceipt, path: str) -> bool:
    return any(m.path == path for m in arch.metrics.modules)


def _none(_c: CurrentReceipts) -> ClaimValue | None:
    return None


def roadmap_facts() -> tuple[RoadmapFact, ...]:
    return (
        RoadmapFact(
            "production module count",
            r"production modules: \d+",
            "architecture",
            "$.metrics.executable_modules",
            lambda c: c.architecture.metrics.executable_modules,
            1291,
        ),
        RoadmapFact(
            "production noncomment LOC",
            r"noncomment LOC: \d+",
            "architecture",
            "$.metrics.total_noncomment_loc",
            lambda c: c.architecture.metrics.total_noncomment_loc,
            554615,
        ),
        RoadmapFact(
            "scc count",
            r"scc count: \d+",
            "architecture",
            "$.metrics.scc_count",
            lambda c: c.architecture.metrics.scc_count,
            16,
        ),
        RoadmapFact(
            "largest scc",
            r"largest scc: \d+",
            "architecture",
            "$.metrics.largest_scc",
            lambda c: c.architecture.metrics.largest_scc,
            24,
        ),
        RoadmapFact(
            "exact duplicate groups",
            r"exact duplicate groups: \d+",
            "duplicates",
            "$.exact_totals.groups",
            lambda c: c.duplicates.exact_totals.groups,
            140,
        ),
        RoadmapFact(
            "exact duplicate functions",
            r"exact duplicate functions: \d+",
            "duplicates",
            "$.exact_totals.participating_functions",
            lambda c: c.duplicates.exact_totals.participating_functions,
            397,
        ),
        RoadmapFact(
            "ruff diagnostics",
            r"ruff diagnostics: \d+",
            "static",
            "$.diagnostics[tool=ruff].count",
            lambda c: _diag_count(c.static, "ruff"),
            2,
        ),
        RoadmapFact(
            "pyright diagnostics",
            r"pyright diagnostics: \d+",
            "static",
            "$.diagnostics[tool=pyright].count",
            lambda c: _diag_count(c.static, "pyright"),
            27924,
        ),
        RoadmapFact(
            "test files",
            r"test files: \d+",
            "test_db",
            "$.tracked_test_files",
            lambda c: len(c.test_db.tracked_test_files),
            1092,
        ),
        RoadmapFact(
            "upgrade builders",
            r"upgrade builders: \d+",
            "test_db",
            "$.database_builders[*].evidence contains call:upgrade",
            lambda c: _evidence_count(c.test_db, "call:upgrade"),
            172,
        ),
        RoadmapFact(
            "migrated builders",
            r"migrated builders: \d+",
            "test_db",
            "$.database_builders[*].evidence contains call:migrated_db",
            lambda c: _evidence_count(c.test_db, "call:migrated_db"),
            146,
        ),
        RoadmapFact(
            "ddl builders",
            r"ddl builders: \d+",
            "test_db",
            "$.database_builders[*].evidence contains sql:",
            lambda c: _evidence_count(c.test_db, "sql:"),
            550,
        ),
        RoadmapFact(
            "theme live edge",
            r"theme_synth live edge: (true|false)",
            "reachability",
            "$.edges[target=src/synthesis/theme_synth.py]",
            lambda c: _has_edge(c.reachability, "src/synthesis/theme_synth.py"),
            True,
        ),
        RoadmapFact(
            "refetch absence",
            r"refetch_aggregator absent: (true|false)",
            "architecture",
            "$.metrics.modules[path=execution/refetch_aggregator.py]",
            lambda c: not _has_module(c.architecture, "execution/refetch_aggregator.py"),
            True,
        ),
        RoadmapFact(
            "full suite seconds",
            r"full suite seconds: [\d.]+",
            None,
            "historical receipt only",
            _none,
            1046.92,
        ),
        RoadmapFact(
            "unreachable scripts",
            r"unreachable scripts: \d+",
            None,
            "provisional queue only",
            _none,
            85,
        ),
    )


def claim_manifest_hash() -> str:
    payload = json.dumps(
        [
            {
                "name": f.name,
                "pattern": f.pattern,
                "source": f.source,
                "locator": f.locator,
                "expected": f.provisional,
            }
            for f in roadmap_facts()
        ],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def find_roadmap(root: Path) -> Path | None:
    for rel in ROADMAP_CANDIDATES:
        p = root / rel
        try:
            if p.is_file() and not p.is_symlink():
                return p
        except OSError:
            continue
    return None


def load_roadmap(root: Path) -> tuple[bytes | None, list[str], bool, str]:
    path = find_roadmap(root)
    if path is None:
        return None, [], False, "docs/quality/quality-9plus-roadmap.md"
    try:
        raw = path.read_bytes()
    except OSError:
        return (
            None,
            [],
            False,
            path.relative_to(root).as_posix() if path.is_absolute() else str(path),
        )
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        rel = path.as_posix()
    try:
        return raw, raw.decode("utf-8").splitlines(), False, rel
    except UnicodeDecodeError:
        return raw, [], True, rel


def fact_line_numbers(
    fact: RoadmapFact,
    lines: list[str],
    bindings: Mapping[str, RoadmapClaimBinding] | None = None,
) -> tuple[int, ...]:
    if bindings is not None:
        binding = bindings.get(fact.name)
        if (
            binding is None
            or binding.source_key != fact.source
            or binding.extractor_locator != fact.locator
            or binding.value != fact.provisional
            or binding.source_line > len(lines)
        ):
            return ()
        return (
            (binding.source_line,) if lines[binding.source_line - 1] == binding.source_quote else ()
        )
    try:
        rx = re.compile(fact.pattern)
    except re.error:
        return ()
    return tuple(i for i, line in enumerate(lines, 1) if rx.fullmatch(line.strip()))


def roadmap_value_matches(
    fact: RoadmapFact, line: str, binding: RoadmapClaimBinding | None = None
) -> bool:
    if binding is not None:
        return (
            binding.source_key == fact.source
            and binding.extractor_locator == fact.locator
            and type(binding.value) is type(fact.provisional)
            and binding.value == fact.provisional
        )
    expected = binding.value if binding is not None else fact.provisional
    if expected is None or ":" not in line:
        return False
    raw = line.rsplit(":", 1)[1].strip()
    if isinstance(expected, bool):
        lowered = raw.lower()
        return lowered in {"true", "false"} and (lowered == "true") == expected
    if isinstance(expected, int):
        return re.fullmatch(r"[+-]?\d+", raw) is not None and int(raw) == expected
    if isinstance(expected, float):
        if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", raw) is None:
            return False
        try:
            return Decimal(raw) == Decimal(str(expected))
        except InvalidOperation:
            return False
    return raw == expected


def bound_violations(items: list[str]) -> tuple[str, ...]:
    out = [v[:VIOLATION_TEXT_CAP] for v in items[:VIOLATION_CAP]]
    if len(items) > VIOLATION_CAP:
        out[-1] = f"{len(items) - VIOLATION_CAP + 1} additional violations omitted"
    return tuple(out)


def build_claims(
    current: CurrentReceipts,
    admitted: Mapping[SourceKey, bool],
    evidences: Mapping[SourceKey, Evidence | None],
    roadmap_ok: bool,
    roadmap_raw: bytes | None,
    roadmap_rel: str,
    roadmap_hash: str,
    lines: list[str],
    rejections: Mapping[SourceKey, str],
    bindings: Mapping[str, RoadmapClaimBinding] | None = None,
) -> list[Claim]:
    claims: list[Claim] = []
    for fact in roadmap_facts():
        observed: ClaimValue | None = None
        source: SourceKey | None = fact.source
        if roadmap_ok and source is not None and admitted.get(source, False):
            try:
                observed = fact.extractor(current)
            except (ValueError, AttributeError, TypeError):
                observed = None
        if fact.provisional is None or source is None:
            verdict: Verdict = "rejected"
            eligible = False
            note = "Provisional roadmap states no scorable value for this metric."
        elif observed is None:
            verdict = "rejected"
            eligible = False
            if not roadmap_ok:
                note = "Roadmap source is missing or tampered."
            elif rejections.get(source):
                note = rejections[source][:200]
            else:
                note = "No admissible typed generator receipt exists for this roadmap fact."
        else:
            is_verified = type(observed) is type(fact.provisional) and observed == fact.provisional
            verdict = "verified" if is_verified else "corrected"
            eligible = True
            note = (
                "Fresh typed generator reproduces the provisional value."
                if is_verified
                else "Fresh typed generator corrects the provisional value."
            )
        binding = bindings.get(fact.name) if bindings is not None else None
        if binding is not None:
            note = (
                f"{note} Unverified map annotation (display only): "
                f"{binding.qualifier}; {binding.metric_definition}."
            )
        ev = evidences.get(source) if source is not None else None
        prov = None
        if roadmap_ok and roadmap_raw is not None:
            nums = fact_line_numbers(fact, lines, bindings)
            if len(nums) == 1:
                prov = Evidence(
                    path=roadmap_rel,
                    sha256=roadmap_hash,
                    locator=f"line {nums[0]}",
                )
        claims.append(
            Claim(
                name=fact.name,
                provisional_expected=fact.provisional,
                observed=observed,
                verdict=verdict,
                scored_eligible=eligible,
                evidence=ev,
                provisional_evidence=prov,
                note=note,
            )
        )
    return claims


def _strip_receipt_path(payload: JsonValue) -> JsonValue:
    if isinstance(payload, dict):
        narrowed = cast(dict[str, JsonValue], payload)
        return {k: _strip_receipt_path(v) for k, v in narrowed.items() if k != "receipt_path"}
    if isinstance(payload, list):
        return [_strip_receipt_path(v) for v in payload]
    if isinstance(payload, (str, int, float)) or payload is None:
        return payload
    raise TypeError(f"unsupported JSON value: {type(payload).__name__}")


def _normalized(model: BaseModel, key: SourceKey) -> JsonValue:
    raw: object = model.model_dump(exclude=VOLATILE_FIELDS.get(key, set()))
    assert isinstance(raw, dict)
    payload: JsonValue = cast(JsonValue, raw)
    if key == "static":
        payload = _strip_receipt_path(payload)
    return payload


def _status_ok(key: SourceKey, parsed: BaseModel) -> str | None:
    if key == "architecture" and isinstance(parsed, ArchitectureReceipt):
        # ArchitectureReceipt has no status/closure field by design;
        # exact fresh reproduction in _admit_one is its admission gate.
        return None
    if key == "duplicates" and isinstance(parsed, DuplicateInventory):
        if parsed.parse_errors:
            return "receipt parse errors are not admissible"
    elif key == "static" and isinstance(parsed, StaticQualityInventory):
        if parsed.status != "PASS":
            return "receipt status is not PASS"
        if parsed.violations:
            return "receipt violations are not admissible"
    elif key == "test_db" and isinstance(parsed, TestDbAudit):
        if parsed.collection_status != "COMPLETE":
            return "receipt collection is not COMPLETE"
        if parsed.raw_audit_status != "PASS":
            return "receipt audit status is not PASS"
        if parsed.violations:
            return "receipt violations are not admissible"
    elif key == "reachability" and isinstance(parsed, ReachabilityGraph):
        if parsed.collection_status != "COMPLETE":
            return "receipt collection is not COMPLETE"
        if parsed.closure_status != "PASS" or parsed.hold:
            return "receipt closure is not PASS"
    return None


def _admit_one(
    root: Path, key: SourceKey, current: BaseModel
) -> tuple[bool, Evidence | None, str, bytes | None]:
    rel = SOURCE_PATHS[key]
    safe = _safe_source_path(root, rel)
    if safe is None:
        return False, None, "receipt is missing", None
    try:
        raw = safe.read_bytes()
    except OSError:
        return False, None, "receipt is missing", None
    model: type[BaseModel] = {
        "architecture": ArchitectureReceipt,
        "duplicates": DuplicateInventory,
        "static": StaticQualityInventory,
        "test_db": TestDbAudit,
        "reachability": ReachabilityGraph,
    }[key]
    try:
        _validate_unique_json_keys(raw)
        parsed = model.model_validate_json(raw)
    except (ValidationError, ValueError, UnicodeDecodeError):
        return False, None, "receipt failed its typed schema", raw
    problem = _status_ok(key, parsed)
    if problem is not None:
        return False, None, problem, raw
    try:
        if _normalized(parsed, key) != _normalized(current, key):
            return False, None, "receipt does not exactly reproduce the fresh generator result", raw
    except (ValueError, TypeError):
        return False, None, "receipt comparison failed", raw
    locators = {
        "architecture": "$.metrics",
        "duplicates": "$.exact_totals",
        "static": "$.diagnostics",
        "test_db": "$.database_builders",
        "reachability": "$.edges",
    }
    return (
        True,
        Evidence(path=rel, sha256=hashlib.sha256(raw).hexdigest(), locator=locators[key]),
        "",
        raw,
    )


def admit_sources(
    root: Path, current: CurrentReceipts
) -> tuple[
    dict[SourceKey, bool],
    dict[SourceKey, Evidence | None],
    dict[SourceKey, str],
    dict[SourceKey, bytes | None],
]:
    cur: dict[SourceKey, BaseModel] = {
        "architecture": current.architecture,
        "duplicates": current.duplicates,
        "static": current.static,
        "test_db": current.test_db,
        "reachability": current.reachability,
    }
    admitted: dict[SourceKey, bool] = {}
    evidences: dict[SourceKey, Evidence | None] = {}
    rejections: dict[SourceKey, str] = {}
    raws: dict[SourceKey, bytes | None] = {}
    for key in SOURCE_PATHS:
        ok, ev, rej, raw = _admit_one(root, key, cur[key])
        admitted[key] = ok
        evidences[key] = ev
        if not ok:
            rejections[key] = rej
        raws[key] = raw
    return admitted, evidences, rejections, raws


def deterministic_source_hash(
    raws: Mapping[SourceKey, bytes | None],
    roadmap_raw: bytes | None,
    claim_map_raw: bytes | None = None,
) -> str:
    digest = hashlib.sha256()
    for key in sorted(SOURCE_PATHS):
        rel = SOURCE_PATHS[key]
        digest.update(rel.encode() + b"\0")
        raw = raws.get(key)
        if raw is not None:
            digest.update(hashlib.sha256(raw).digest())
        else:
            digest.update(b"MISSING")
        digest.update(b"\0")
    digest.update(b"roadmap\0")
    digest.update(hashlib.sha256(roadmap_raw).digest() if roadmap_raw is not None else b"MISSING")
    digest.update(b"roadmap-claim-map\0")
    digest.update(
        hashlib.sha256(claim_map_raw).digest() if claim_map_raw is not None else b"MISSING"
    )
    return digest.hexdigest()


def _roadmap_bindings(
    root: Path, roadmap_raw: bytes | None
) -> tuple[dict[str, RoadmapClaimBinding] | None, bytes | None, str | None]:
    if roadmap_raw is None:
        return None, None, "roadmap source is missing"
    # Existing hermetic callers retain their explicit legacy one-line fixtures.
    # A checked-in map, once present, is mandatory and never falls back on failure.
    map_lexical = root / ROADMAP_CLAIM_MAP_PATH
    if not os.path.lexists(map_lexical):
        return None, None, None
    try:
        if _safe_source_path(root, ROADMAP_CLAIM_MAP_PATH) is None:
            raise RoadmapSourceError("roadmap claim map is unsafe")
        claim_map, raw = load_roadmap_claim_map_with_raw(root)
    except (OSError, RoadmapSourceError):
        return None, None, "roadmap claim map is invalid"
    if hashlib.sha256(roadmap_raw).hexdigest() != claim_map.document.sha256:
        return None, raw, "roadmap source does not match approved claim map"
    return {binding.name: binding for binding in claim_map.claims}, raw, None


def _subject_tracks_roadmap_claim_map(root: Path, subject_commit: str | None) -> bool:
    if not _is_hex40(subject_commit):
        return False
    try:
        return (
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "cat-file",
                    "-e",
                    f"{subject_commit}:{ROADMAP_CLAIM_MAP_PATH}",
                ],
                capture_output=True,
                check=False,
                env=clean_local_git_env(),
            ).returncode
            == 0
        )
    except OSError:
        return False


def _fresh_receipts(root: Path) -> CurrentReceipts:
    return CurrentReceipts(
        architecture=build_architecture_receipt(root, "WORKTREE"),
        duplicates=build_inventory(root, "WORKTREE"),
        static=inventory(root),
        test_db=audit_test_db_patterns(root),
        reachability=build_graph(root),
    )


def reconcile_with_receipts_for_testing(
    root: Path, current: CurrentReceipts
) -> ReconciliationReceipt:
    """Explicitly synthetic hermetic test seam with fixed clean Git identity."""
    return _build_receipt(root.resolve(), current, _TEST_SUBJECT_COMMIT, False, ())


def _build_receipt(
    root: Path,
    current: CurrentReceipts,
    subject_commit: str | None,
    worktree_dirty: bool | None,
    extra_violations: tuple[str, ...] = (),
) -> ReconciliationReceipt:
    violations: list[str] = []
    if not _is_hex40(subject_commit):
        violations.append("git HEAD subject is unavailable or invalid")
    if worktree_dirty is None:
        violations.append("git worktree state is unavailable")
    elif worktree_dirty:
        violations.append("git worktree is dirty")
    violations.extend(extra_violations)
    admitted, evidences, rejections, raws = admit_sources(root, current)
    roadmap_raw, lines, decode_error, roadmap_rel = load_roadmap(root)
    roadmap_hash = hashlib.sha256(roadmap_raw).hexdigest() if roadmap_raw is not None else ""
    roadmap_ok = roadmap_raw is not None and not decode_error
    bindings, claim_map_raw, binding_error = _roadmap_bindings(root, roadmap_raw)
    if binding_error is not None:
        violations.append(binding_error)
    for key in SOURCE_PATHS:
        if not admitted.get(key, False):
            violations.append(
                f"inadmissible source receipt {SOURCE_PATHS[key]}: {rejections.get(key, 'missing')}"
            )
    if roadmap_raw is None:
        violations.append("roadmap source is missing")
    elif decode_error:
        violations.append("roadmap source is not valid UTF-8")
    else:
        for fact in roadmap_facts():
            binding = bindings.get(fact.name) if bindings is not None else None
            nums = fact_line_numbers(fact, lines, bindings)
            if len(nums) != 1:
                violations.append(f"roadmap locator is missing or ambiguous: {fact.name}")
            elif fact.provisional is not None and not roadmap_value_matches(
                fact, lines[nums[0] - 1], binding
            ):
                violations.append(f"roadmap provisional value mismatch: {fact.name}")
    claims_roadmap_ok = (
        roadmap_ok
        and binding_error is None
        and not any(v.startswith("roadmap ") for v in violations)
    )
    claims = build_claims(
        current,
        admitted,
        evidences,
        claims_roadmap_ok,
        roadmap_raw,
        roadmap_rel,
        roadmap_hash,
        lines,
        rejections,
        bindings,
    )
    names = [c.name for c in claims]
    if len(set(names)) != len(names) or len(names) != len(roadmap_facts()):
        violations.append("roadmap claim set is incomplete or duplicated")
    for claim in claims:
        ok_shape = (
            claim.verdict in ("verified", "corrected")
            and claim.observed is not None
            and claim.evidence is not None
            and claim.provisional_evidence is not None
        )
        if claim.scored_eligible != ok_shape:
            violations.append(f"claim eligibility is inconsistent: {claim.name}")
        if claim.verdict == "rejected" and claim.scored_eligible:
            violations.append(f"rejected claim must be unscored: {claim.name}")
    bounded = bound_violations(sorted(set(violations)))
    status = "PASS" if not bounded else "HOLD"
    scored = sum(1 for c in claims if c.scored_eligible)
    rejected = sum(1 for c in claims if c.verdict == "rejected")
    if scored + rejected != len(claims):
        bounded = bound_violations([*bounded, "claim counts are inconsistent"])
        status = "HOLD"
    return ReconciliationReceipt(
        status=status,
        claims=tuple(claims),
        scored_claims=scored,
        rejected_claims=rejected,
        subject_commit=subject_commit,
        worktree_dirty=worktree_dirty,
        source_hash=deterministic_source_hash(raws, roadmap_raw, claim_map_raw),
        roadmap_source=Evidence(
            path=roadmap_rel,
            sha256=roadmap_hash,
            locator="baseline section; Linear document identity externally unverified",
        )
        if claims_roadmap_ok
        else None,
        claim_manifest_sha256=claim_manifest_hash(),
        violations=bounded,
    )


def reconcile(root: Path) -> ReconciliationReceipt:
    """Reconcile fresh typed evidence against a stable, clean Git subject."""
    resolved = root.resolve()
    before = _git_state(resolved)
    current = _fresh_receipts(resolved)
    middle = _git_state(resolved)
    middle_subject = middle[0] if _is_hex40(middle[0]) else None
    draft = _build_receipt(
        resolved,
        current,
        middle_subject,
        middle[1],
        _git_state_violations(before, middle),
    )
    final = _git_state(resolved)
    collection_violations = (
        *_git_state_violations(before, middle, final),
        *_ignored_source_snapshot_violations(resolved, draft),
    )
    if not collection_violations:
        return draft
    combined = bound_violations(sorted(set(draft.violations) | set(collection_violations)))
    final_subject = final[0] if _is_hex40(final[0]) else None
    return ReconciliationReceipt(
        status="HOLD",
        claims=draft.claims,
        scored_claims=draft.scored_claims,
        rejected_claims=draft.rejected_claims,
        subject_commit=final_subject,
        worktree_dirty=final[1],
        source_hash=draft.source_hash,
        roadmap_source=draft.roadmap_source,
        claim_manifest_sha256=draft.claim_manifest_sha256,
        violations=combined,
    )


def _lstat_regular(path: Path) -> os.stat_result | None:
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode):
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    if st.st_nlink != 1:
        return None
    return st


def _staged_commit_of(key: SourceKey, parsed: BaseModel) -> str | None:
    if key == "architecture" and isinstance(parsed, ArchitectureReceipt):
        return parsed.scoped_commit
    if key == "duplicates" and isinstance(parsed, DuplicateInventory):
        return parsed.commit_hash
    if key == "static" and isinstance(parsed, StaticQualityInventory):
        return parsed.scoped_commit
    if key == "test_db" and isinstance(parsed, TestDbAudit):
        return parsed.scoped_commit
    if key == "reachability" and isinstance(parsed, ReachabilityGraph):
        return parsed.subject_commit
    return None


def _is_ignored_subject_tmp_staging(subject: Path, staged_root: Path) -> bool:
    """Allow only the collector's lexical ignored `.tmp` staging subtree."""

    try:
        relative = staged_root.relative_to(subject)
    except ValueError:
        return False
    if len(relative.parts) < 2 or relative.parts[0] != ".tmp":
        return False
    candidate = subject
    try:
        for part in relative.parts:
            candidate = candidate / part
            if candidate.is_symlink():
                return False
        if staged_root.resolve() != staged_root:
            return False
    except OSError:
        return False
    try:
        return (
            subprocess.run(
                ["git", "-C", str(subject), "check-ignore", "-q", "--", relative.as_posix()],
                capture_output=True,
                check=False,
                env=clean_local_git_env(),
            ).returncode
            == 0
        )
    except OSError:
        return False


def _load_staged_for_subject(subject_resolved: Path, manifest_path: Path) -> _StagedLoad:
    violations: list[str] = []
    entries: dict[StagedKey, StagedManifestEntry] = {}
    snapshots: dict[StagedKey, _StagedFileSnapshot] = {}
    manifest_data: bytes | None = None
    manifest_dev: int | None = None
    manifest_ino: int | None = None
    try:
        manifest_lexical = manifest_path
        if manifest_lexical.is_symlink():
            violations.append("staged manifest is invalid")
            return _StagedLoad(None, entries, snapshots, None, None, None, tuple(violations))
        manifest_stat = _lstat_regular(manifest_lexical)
        if manifest_stat is None:
            violations.append("staged manifest is unavailable")
            return _StagedLoad(None, entries, snapshots, None, None, None, tuple(violations))
        try:
            raw_manifest = manifest_lexical.read_bytes()
        except OSError:
            violations.append("staged manifest is unavailable")
            return _StagedLoad(None, entries, snapshots, None, None, None, tuple(violations))
        manifest_data = raw_manifest
        manifest_dev = manifest_stat.st_dev
        manifest_ino = manifest_stat.st_ino
    except OSError:
        violations.append("staged manifest is unavailable")
        return _StagedLoad(None, entries, snapshots, None, None, None, tuple(violations))
    try:
        _validate_unique_json_keys(manifest_data)
    except (ValueError, UnicodeDecodeError):
        violations.append("staged manifest has duplicate keys")
        return _StagedLoad(
            None, entries, snapshots, manifest_data, manifest_dev, manifest_ino, tuple(violations)
        )
    try:
        model = StagedManifestModel.model_validate_json(manifest_data)
    except (ValidationError, ValueError, UnicodeDecodeError):
        violations.append("staged manifest shape is invalid")
        return _StagedLoad(
            None, entries, snapshots, manifest_data, manifest_dev, manifest_ino, tuple(violations)
        )
    parsed_entries: dict[StagedKey, StagedManifestEntry] = {
        "architecture": model.architecture,
        "duplicates": model.duplicates,
        "static": model.static,
        "test_db": model.test_db,
        "reachability": model.reachability,
        "roadmap": model.roadmap,
    }
    entries = parsed_entries
    manifest_root_lexical = Path(os.path.abspath(manifest_path.parent))
    try:
        manifest_inside_subject = manifest_root_lexical.is_relative_to(subject_resolved)
    except OSError:
        violations.append("staged manifest is invalid")
        return _StagedLoad(
            None, entries, snapshots, manifest_data, manifest_dev, manifest_ino, tuple(violations)
        )
    if manifest_inside_subject and not _is_ignored_subject_tmp_staging(
        subject_resolved, manifest_root_lexical
    ):
        violations.append("staged root overlaps subject")
        return _StagedLoad(
            manifest_root_lexical,
            entries,
            snapshots,
            manifest_data,
            manifest_dev,
            manifest_ino,
            tuple(violations),
        )
    try:
        staged_root = manifest_root_lexical.resolve()
    except OSError:
        violations.append("staged manifest is invalid")
        return _StagedLoad(
            None, entries, snapshots, manifest_data, manifest_dev, manifest_ino, tuple(violations)
        )
    if staged_root == subject_resolved:
        violations.append("staged root overlaps subject")
        return _StagedLoad(
            staged_root,
            entries,
            snapshots,
            manifest_data,
            manifest_dev,
            manifest_ino,
            tuple(violations),
        )
    try:
        nested_allowed = _is_ignored_subject_tmp_staging(subject_resolved, manifest_root_lexical)
        if (
            staged_root.is_relative_to(subject_resolved) and not nested_allowed
        ) or subject_resolved.is_relative_to(staged_root):
            violations.append("staged root overlaps subject")
            return _StagedLoad(
                staged_root,
                entries,
                snapshots,
                manifest_data,
                manifest_dev,
                manifest_ino,
                tuple(violations),
            )
    except OSError:
        violations.append("staged manifest is invalid")
        return _StagedLoad(
            staged_root,
            entries,
            snapshots,
            manifest_data,
            manifest_dev,
            manifest_ino,
            tuple(violations),
        )
    declared_paths = [entries[k].path for k in STAGED_KEYS]
    if len(set(declared_paths)) != len(declared_paths):
        violations.append("staged inputs share the same path")
    for key in STAGED_KEYS:
        rel = entries[key].path
        rel_path = Path(rel)
        if rel_path.is_absolute():
            violations.append(f"staged input path escapes staged root: {key}")
            continue
        if any(part in ("..",) for part in rel_path.parts):
            violations.append(f"staged input path escapes staged root: {key}")
            continue
        lexical = staged_root / rel_path
        try:
            if lexical.is_symlink():
                violations.append(f"staged input is not a regular file: {key}")
                continue
            resolved = lexical.resolve()
        except OSError:
            violations.append(f"staged input is unavailable: {key}")
            continue
        if resolved != lexical:
            violations.append(f"staged input is not a regular file: {key}")
            continue
        try:
            resolved.relative_to(staged_root)
        except ValueError:
            violations.append(f"staged input path escapes staged root: {key}")
            continue
        try:
            if resolved.is_relative_to(subject_resolved) and not nested_allowed:
                violations.append(f"staged input overlaps subject: {key}")
                continue
        except OSError:
            violations.append(f"staged input is unavailable: {key}")
            continue
        st = _lstat_regular(lexical)
        if st is None:
            violations.append(f"staged input is not a regular file: {key}")
            continue
        try:
            data = lexical.read_bytes()
        except OSError:
            violations.append(f"staged input is unavailable: {key}")
            continue
        try:
            restat = os.lstat(lexical)
        except OSError:
            violations.append(f"staged input changed during collection: {key}")
            continue
        if (restat.st_dev, restat.st_ino) != (st.st_dev, st.st_ino):
            violations.append(f"staged input changed during collection: {key}")
            continue
        if stat.S_ISLNK(restat.st_mode) or not stat.S_ISREG(restat.st_mode):
            violations.append(f"staged input is not a regular file: {key}")
            continue
        snapshots[key] = _StagedFileSnapshot(
            resolved=resolved, data=data, dev=st.st_dev, ino=st.st_ino
        )
    if len(snapshots) == len(STAGED_KEYS):
        seen_resolved = {snapshots[k].resolved for k in STAGED_KEYS}
        if len(seen_resolved) != len(STAGED_KEYS):
            violations.append("staged inputs share the same file")
        seen_ino = {(snapshots[k].dev, snapshots[k].ino) for k in STAGED_KEYS}
        if len(seen_ino) != len(STAGED_KEYS):
            violations.append("staged inputs share the same file")
    claims_entry = model.roadmap_claims
    claims_snapshot: _StagedFileSnapshot | None = None
    if claims_entry is not None:
        rel_path = Path(claims_entry.path)
        lexical = staged_root / rel_path
        if rel_path.is_absolute() or ".." in rel_path.parts or lexical.is_symlink():
            violations.append("staged roadmap claims path is invalid")
        else:
            try:
                resolved = lexical.resolve()
                st = _lstat_regular(lexical)
                if resolved != lexical or st is None or not resolved.is_relative_to(staged_root):
                    raise OSError
                data = lexical.read_bytes()
                restat = os.lstat(lexical)
                if (restat.st_dev, restat.st_ino) != (st.st_dev, st.st_ino):
                    raise OSError
                claims_snapshot = _StagedFileSnapshot(resolved, data, st.st_dev, st.st_ino)
            except OSError:
                violations.append("staged roadmap claims is unavailable or unsafe")
    if claims_snapshot is not None:
        all_snapshots = [*snapshots.values(), claims_snapshot]
        if len({snapshot.resolved for snapshot in all_snapshots}) != len(all_snapshots):
            violations.append("staged inputs share the same file")
        if len({(snapshot.dev, snapshot.ino) for snapshot in all_snapshots}) != len(all_snapshots):
            violations.append("staged inputs share the same file")
    return _StagedLoad(
        staged_root,
        entries,
        snapshots,
        manifest_data,
        manifest_dev,
        manifest_ino,
        tuple(violations),
        claims_entry,
        claims_snapshot,
    )


def _admit_staged_one(
    key: SourceKey,
    raw: bytes,
    current: BaseModel,
    subject_commit: str | None,
) -> tuple[bool, Evidence | None, str, BaseModel | None]:
    model: type[BaseModel] = {
        "architecture": ArchitectureReceipt,
        "duplicates": DuplicateInventory,
        "static": StaticQualityInventory,
        "test_db": TestDbAudit,
        "reachability": ReachabilityGraph,
    }[key]
    try:
        _validate_unique_json_keys(raw)
        parsed = model.model_validate_json(raw)
    except (ValidationError, ValueError, UnicodeDecodeError):
        return False, None, "receipt failed its typed schema", None
    problem = _status_ok(key, parsed)
    if problem is not None:
        return False, None, problem, parsed
    embedded = _staged_commit_of(key, parsed)
    if subject_commit is None or not _is_hex40(subject_commit):
        return False, None, "staged receipt commit does not match subject", parsed
    if embedded != subject_commit:
        return False, None, "staged receipt commit does not match subject", parsed
    try:
        if _normalized(parsed, key) != _normalized(current, key):
            return (
                False,
                None,
                "receipt does not exactly reproduce the fresh generator result",
                parsed,
            )
    except (ValueError, TypeError):
        return False, None, "receipt comparison failed", parsed
    return True, None, "", parsed


def _build_staged_receipt(
    subject_resolved: Path,
    current: CurrentReceipts,
    subject_commit: str | None,
    worktree_dirty: bool | None,
    manifest_path: Path,
    extra_violations: tuple[str, ...] = (),
) -> tuple[ReconciliationReceipt, _StagedLoad]:
    violations: list[str] = []
    if not _is_hex40(subject_commit):
        violations.append("git HEAD subject is unavailable or invalid")
    if worktree_dirty is None:
        violations.append("git worktree state is unavailable")
    elif worktree_dirty:
        violations.append("git worktree is dirty")
    violations.extend(extra_violations)
    load = _load_staged_for_subject(subject_resolved, manifest_path)
    violations.extend(load.violations)
    cur: dict[SourceKey, BaseModel] = {
        "architecture": current.architecture,
        "duplicates": current.duplicates,
        "static": current.static,
        "test_db": current.test_db,
        "reachability": current.reachability,
    }
    locators = {
        "architecture": "$.metrics",
        "duplicates": "$.exact_totals",
        "static": "$.diagnostics",
        "test_db": "$.database_builders",
        "reachability": "$.edges",
    }
    admitted: dict[SourceKey, bool] = {}
    evidences: dict[SourceKey, Evidence | None] = {}
    rejections: dict[SourceKey, str] = {}
    raws: dict[SourceKey, bytes | None] = {}
    for key in _STAGED_JSON_KEYS:
        snap = load.snapshots.get(cast(StagedKey, key))
        entry = load.entries.get(cast(StagedKey, key))
        if snap is None or entry is None:
            admitted[key] = False
            evidences[key] = None
            if key not in rejections:
                rejections[key] = "staged receipt is missing"
            raws[key] = None
            continue
        actual_sha = hashlib.sha256(snap.data).hexdigest()
        if actual_sha != entry.sha256:
            admitted[key] = False
            evidences[key] = None
            rejections[key] = "staged receipt hash mismatch"
            raws[key] = snap.data
            violations.append(f"inadmissible staged receipt {key}: hash mismatch")
            continue
        ok, _, rej, _parsed = _admit_staged_one(key, snap.data, cur[key], subject_commit)
        raws[key] = snap.data
        if ok:
            admitted[key] = True
            evidences[key] = Evidence(path=entry.path, sha256=actual_sha, locator=locators[key])
        else:
            admitted[key] = False
            evidences[key] = None
            rejections[key] = rej
            violations.append(f"inadmissible staged receipt {key}: {rej}")
    roadmap_raw: bytes | None = None
    roadmap_lines: list[str] = []
    roadmap_decode_error = False
    roadmap_rel = ""
    roadmap_hash = ""
    roadmap_hash_mismatch = False
    roadmap_entry = load.entries.get("roadmap")
    roadmap_snap = load.snapshots.get("roadmap")
    if roadmap_snap is None or roadmap_entry is None:
        violations.append("staged roadmap is missing")
    else:
        roadmap_raw = roadmap_snap.data
        roadmap_rel = roadmap_entry.path
        actual_roadmap_sha = hashlib.sha256(roadmap_raw).hexdigest()
        if actual_roadmap_sha != roadmap_entry.sha256:
            violations.append("inadmissible staged roadmap: hash mismatch")
            roadmap_hash_mismatch = True
            roadmap_raw = roadmap_snap.data
            roadmap_lines = []
            roadmap_decode_error = False
            roadmap_hash = actual_roadmap_sha
        else:
            roadmap_hash = actual_roadmap_sha
            try:
                roadmap_lines = roadmap_raw.decode("utf-8").splitlines()
                roadmap_decode_error = False
            except UnicodeDecodeError:
                roadmap_lines = []
                roadmap_decode_error = True
                violations.append("roadmap source is not valid UTF-8")
    roadmap_ok = (
        roadmap_raw is not None
        and not roadmap_decode_error
        and roadmap_entry is not None
        and not roadmap_hash_mismatch
    )
    bindings: dict[str, RoadmapClaimBinding] | None = None
    claim_map_raw: bytes | None = None
    binding_error: str | None = None
    claims_entry = load.roadmap_claims_entry
    claims_snapshot = load.roadmap_claims_snapshot
    if claims_entry is not None:
        if (
            claims_snapshot is None
            or hashlib.sha256(claims_snapshot.data).hexdigest() != claims_entry.sha256
        ):
            binding_error = "staged roadmap claims is missing or has a hash mismatch"
        elif roadmap_raw is None:
            binding_error = "roadmap source is missing"
        else:
            try:
                claim_map = parse_roadmap_claim_map(claims_snapshot.data, roadmap_raw)
                bindings = {binding.name: binding for binding in claim_map.claims}
                claim_map_raw = claims_snapshot.data
            except RoadmapSourceError:
                binding_error = "staged roadmap claims is invalid"
    elif _subject_tracks_roadmap_claim_map(subject_resolved, subject_commit):
        binding_error = "staged roadmap claims is missing"
    if binding_error is not None:
        violations.append(binding_error)
    if roadmap_ok and roadmap_raw is not None:
        for fact in roadmap_facts():
            binding = bindings.get(fact.name) if bindings is not None else None
            nums = fact_line_numbers(fact, roadmap_lines, bindings)
            if len(nums) != 1:
                violations.append(f"roadmap locator is missing or ambiguous: {fact.name}")
            elif fact.provisional is not None and not roadmap_value_matches(
                fact, roadmap_lines[nums[0] - 1], binding
            ):
                violations.append(f"roadmap provisional value mismatch: {fact.name}")
    elif (
        (roadmap_raw is None or roadmap_decode_error)
        and not any(v.startswith("roadmap source") for v in violations)
        and not any(
            v.startswith("staged roadmap") or v.startswith("inadmissible staged roadmap")
            for v in violations
        )
    ):
        violations.append("roadmap source is missing")
    claims_roadmap_ok = (
        bool(roadmap_ok)
        and binding_error is None
        and not any(v.startswith("roadmap ") for v in violations)
    )
    claims = build_claims(
        current,
        admitted,
        evidences,
        claims_roadmap_ok,
        roadmap_raw,
        roadmap_rel,
        roadmap_hash,
        roadmap_lines,
        rejections,
        bindings,
    )
    names = [c.name for c in claims]
    if len(set(names)) != len(names) or len(names) != len(roadmap_facts()):
        violations.append("roadmap claim set is incomplete or duplicated")
    for claim in claims:
        ok_shape = (
            claim.verdict in ("verified", "corrected")
            and claim.observed is not None
            and claim.evidence is not None
            and claim.provisional_evidence is not None
        )
        if claim.scored_eligible != ok_shape:
            violations.append(f"claim eligibility is inconsistent: {claim.name}")
        if claim.verdict == "rejected" and claim.scored_eligible:
            violations.append(f"rejected claim must be unscored: {claim.name}")
    bounded = bound_violations(sorted(set(violations)))
    status: Literal["PASS", "HOLD"] = "PASS" if not bounded else "HOLD"
    scored = sum(1 for c in claims if c.scored_eligible)
    rejected = sum(1 for c in claims if c.verdict == "rejected")
    if scored + rejected != len(claims):
        bounded = bound_violations([*bounded, "claim counts are inconsistent"])
        status = "HOLD"
    receipt = ReconciliationReceipt(
        status=status,
        claims=tuple(claims),
        scored_claims=scored,
        rejected_claims=rejected,
        subject_commit=subject_commit,
        worktree_dirty=worktree_dirty,
        source_hash=deterministic_source_hash(raws, roadmap_raw, claim_map_raw),
        roadmap_source=Evidence(
            path=roadmap_rel,
            sha256=roadmap_hash,
            locator="baseline section; Linear document identity externally unverified",
        )
        if claims_roadmap_ok and roadmap_hash != ""
        else None,
        claim_manifest_sha256=claim_manifest_hash(),
        violations=bounded,
    )
    return receipt, load


def _verify_staged_stable(load: _StagedLoad, manifest_path: Path) -> tuple[str, ...]:
    problems: list[str] = []
    if load.manifest_data is None or load.manifest_dev is None or load.manifest_ino is None:
        return tuple(problems)
    try:
        st = os.lstat(manifest_path)
        if (st.st_dev, st.st_ino) != (load.manifest_dev, load.manifest_ino):
            problems.append("staged manifest changed during collection")
            return tuple(problems)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            problems.append("staged manifest changed during collection")
            return tuple(problems)
        current_manifest = manifest_path.read_bytes()
        if current_manifest != load.manifest_data:
            problems.append("staged manifest changed during collection")
    except OSError:
        problems.append("staged manifest changed during collection")
        return tuple(problems)
    for key in STAGED_KEYS:
        snap = load.snapshots.get(key)
        if snap is None:
            continue
        try:
            if snap.resolved.is_symlink():
                problems.append(f"staged input changed during collection: {key}")
                continue
            st2 = os.lstat(snap.resolved)
            if (st2.st_dev, st2.st_ino) != (snap.dev, snap.ino):
                problems.append(f"staged input changed during collection: {key}")
                continue
            if stat.S_ISLNK(st2.st_mode) or not stat.S_ISREG(st2.st_mode) or st2.st_nlink != 1:
                problems.append(f"staged input changed during collection: {key}")
                continue
            current_data = snap.resolved.read_bytes()
            if current_data != snap.data:
                problems.append(f"staged input changed during collection: {key}")
        except OSError:
            problems.append(f"staged input changed during collection: {key}")
    claims_snapshot = load.roadmap_claims_snapshot
    if claims_snapshot is not None:
        try:
            if claims_snapshot.resolved.is_symlink():
                problems.append("staged roadmap claims changed during collection")
            else:
                st2 = os.lstat(claims_snapshot.resolved)
                if (
                    (st2.st_dev, st2.st_ino)
                    != (
                        claims_snapshot.dev,
                        claims_snapshot.ino,
                    )
                    or not stat.S_ISREG(st2.st_mode)
                    or st2.st_nlink != 1
                    or claims_snapshot.resolved.read_bytes() != claims_snapshot.data
                ):
                    problems.append("staged roadmap claims changed during collection")
        except OSError:
            problems.append("staged roadmap claims changed during collection")
    return tuple(problems)


def reconcile_staged_subject(
    subject_root: Path, staged_manifest_path: Path
) -> ReconciliationReceipt:
    """Reconcile fresh subject measurements against hash-bound staged inputs."""
    subject_resolved = subject_root.resolve()
    if not subject_resolved.is_dir():
        raise StagedManifestError("invalid subject root")
    manifest_resolved = staged_manifest_path
    before = _git_state(subject_resolved)
    current = _fresh_receipts(subject_resolved)
    middle = _git_state(subject_resolved)
    middle_subject = middle[0] if _is_hex40(middle[0]) else None
    draft, load = _build_staged_receipt(
        subject_resolved,
        current,
        middle_subject,
        middle[1],
        manifest_resolved,
        _git_state_violations(before, middle),
    )
    final = _git_state(subject_resolved)
    stable_problems = _verify_staged_stable(load, manifest_resolved)
    collection_violations = (
        *_git_state_violations(before, middle, final),
        *stable_problems,
    )
    if not collection_violations:
        return draft
    combined = bound_violations(sorted(set(draft.violations) | set(collection_violations)))
    final_subject = final[0] if _is_hex40(final[0]) else None
    return ReconciliationReceipt(
        status="HOLD",
        claims=draft.claims,
        scored_claims=draft.scored_claims,
        rejected_claims=draft.rejected_claims,
        subject_commit=final_subject,
        worktree_dirty=final[1],
        source_hash=draft.source_hash,
        roadmap_source=draft.roadmap_source,
        claim_manifest_sha256=draft.claim_manifest_sha256,
        violations=combined,
    )


__all__ = [
    "SOURCE_PATHS",
    "STAGED_KEYS",
    "CurrentReceipts",
    "ReconciliationReceipt",
    "StagedManifestEntry",
    "StagedManifestError",
    "claim_manifest_hash",
    "reconcile",
    "reconcile_staged_subject",
    "roadmap_facts",
    "roadmap_value_matches",
]
