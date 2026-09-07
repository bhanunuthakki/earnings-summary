"""Typed roadmap-reconciliation producer (R1 admission generator identity)."""

from __future__ import annotations

import hashlib
import json
import re
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
            "full suite seconds", r"full suite seconds: [\d.]+", None, "unavailable", _none, 1046.92
        ),
        RoadmapFact(
            "unreachable scripts", r"unreachable scripts: \d+", None, "unavailable", _none, 85
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


def fact_line_numbers(fact: RoadmapFact, lines: list[str]) -> tuple[int, ...]:
    try:
        rx = re.compile(fact.pattern)
    except re.error:
        return ()
    return tuple(i for i, line in enumerate(lines, 1) if rx.fullmatch(line.strip()))


def _roadmap_value_matches(fact: RoadmapFact, line: str) -> bool:
    expected = fact.provisional
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
            is_verified = observed == fact.provisional
            verdict = "verified" if is_verified else "corrected"
            eligible = True
            note = (
                "Fresh typed generator reproduces the provisional value."
                if is_verified
                else "Fresh typed generator corrects the provisional value."
            )
        ev = evidences.get(source) if source is not None else None
        prov = None
        if roadmap_ok and roadmap_raw is not None:
            nums = fact_line_numbers(fact, lines)
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
    raws: Mapping[SourceKey, bytes | None], roadmap_raw: bytes | None
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
    return digest.hexdigest()


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
            nums = fact_line_numbers(fact, lines)
            if len(nums) != 1:
                violations.append(f"roadmap locator is missing or ambiguous: {fact.name}")
            elif fact.provisional is not None and not _roadmap_value_matches(
                fact, lines[nums[0] - 1]
            ):
                violations.append(f"roadmap provisional value mismatch: {fact.name}")
    claims_roadmap_ok = roadmap_ok and not any(v.startswith("roadmap ") for v in violations)
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
        source_hash=deterministic_source_hash(raws, roadmap_raw),
        roadmap_source=Evidence(
            path=roadmap_rel,
            sha256=roadmap_hash,
            locator="baseline section",
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


__all__ = [
    "SOURCE_PATHS",
    "CurrentReceipts",
    "ReconciliationReceipt",
    "claim_manifest_hash",
    "reconcile",
    "roadmap_facts",
]
