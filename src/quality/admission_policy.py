"""Fail-closed admission policy over typed collector receipts."""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from quality.architecture import ArchitectureReceipt
from quality.duplicates import DuplicateInventory
from quality.lifecycle_models import LifecycleInventory
from quality.performance_models import PerformanceReceipt
from quality.reachability import ReachabilityGraph
from quality.roadmap_reconciliation import ReconciliationReceipt
from quality.static_quality import StaticQualityInventory
from quality.test_db_models import TestDbAudit

SourceName = Literal[
    "architecture",
    "duplicates",
    "static",
    "test_db",
    "reachability",
    "lifecycle",
    "reconciliation",
    "performance",
]
SlotKind = Literal["block", "hard_gate"]
Verdict = Literal["pass", "fail"]
AdmissionRule = Literal[
    "unadmitted",
    "lifecycle_complete",
    "reachability_closed",
]

__all__ = [
    "ARCHITECTURE_PATH",
    "DUPLICATES_PATH",
    "EXPECTED_BLOCKS",
    "EXPECTED_GATES",
    "LIFECYCLE_PATH",
    "PERFORMANCE_PATH",
    "REACHABILITY_PATH",
    "RECONCILIATION_PATH",
    "RUNTIME_POLICY_SHA256",
    "SLOTS",
    "SOURCE_PATHS",
    "SOURCE_SCHEMAS",
    "STATIC_PATH",
    "TEST_DB_PATH",
    "AdmissionRule",
    "ParsedSource",
    "SlotSpec",
    "evaluate_slot",
    "parse_source",
    "required_paths",
    "verify_registry",
]

RUNTIME_POLICY_SHA256: str = hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()


ARCHITECTURE_PATH = "docs/quality/architecture-ratchet.json"
DUPLICATES_PATH = "docs/quality/duplicates-ratchet.json"
STATIC_PATH = "docs/quality/static-baseline.json"
TEST_DB_PATH = "docs/quality/test-db-patterns-baseline.json"
REACHABILITY_PATH = "docs/quality/reachability-check.json"
LIFECYCLE_PATH = "docs/quality/lifecycle-inventory.json"
RECONCILIATION_PATH = "docs/quality/roadmap-reconciliation.json"
PERFORMANCE_PATH = "docs/quality/performance-baseline.json"
SOURCE_PATHS: dict[SourceName, str] = {
    "architecture": ARCHITECTURE_PATH,
    "duplicates": DUPLICATES_PATH,
    "static": STATIC_PATH,
    "test_db": TEST_DB_PATH,
    "reachability": REACHABILITY_PATH,
    "lifecycle": LIFECYCLE_PATH,
    "reconciliation": RECONCILIATION_PATH,
    "performance": PERFORMANCE_PATH,
}
SOURCE_SCHEMAS: dict[SourceName, str] = {
    "architecture": "architecture-measurement-v1",
    "duplicates": "1",
    "static": "bha-120.v3",
    "test_db": "test-db-patterns/v1",
    "reachability": "operational-reachability-raw/v1",
    "lifecycle": "operational-lifecycle-inventory/v1",
    "reconciliation": "roadmap-reconciliation-v1",
    "performance": "performance-baseline/v1",
}


class ParsedSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source: SourceName
    path: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    typed_valid: bool
    semantic_pass: bool
    error: str = Field(max_length=300)


class SlotSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    key: str = Field(min_length=1)
    kind: SlotKind
    sources: tuple[SourceName, ...] = Field(min_length=1)
    rule: AdmissionRule


SLOTS: dict[str, SlotSpec] = {
    "maintainability.static_quality": SlotSpec(
        key="maintainability.static_quality",
        kind="block",
        sources=("static",),
        rule="unadmitted",
    ),
    "maintainability.duplication": SlotSpec(
        key="maintainability.duplication",
        kind="block",
        sources=("duplicates",),
        rule="unadmitted",
    ),
    "maintainability.authorities": SlotSpec(
        key="maintainability.authorities",
        kind="block",
        sources=("reconciliation",),
        rule="unadmitted",
    ),
    "maintainability.sustainable_tests": SlotSpec(
        key="maintainability.sustainable_tests",
        kind="block",
        sources=("test_db",),
        rule="unadmitted",
    ),
    "maintainability.enforced_ratchets": SlotSpec(
        key="maintainability.enforced_ratchets",
        kind="block",
        sources=(
            "architecture",
            "duplicates",
        ),
        rule="unadmitted",
    ),
    "efficiency.integrity_audit": SlotSpec(
        key="efficiency.integrity_audit",
        kind="block",
        sources=("reconciliation",),
        rule="unadmitted",
    ),
    "efficiency.request_path": SlotSpec(
        key="efficiency.request_path",
        kind="block",
        sources=("reachability",),
        rule="unadmitted",
    ),
    "efficiency.test_ci": SlotSpec(
        key="efficiency.test_ci", kind="block", sources=("test_db",), rule="unadmitted"
    ),
    "efficiency.dcf_disposition": SlotSpec(
        key="efficiency.dcf_disposition",
        kind="block",
        sources=("performance",),
        rule="unadmitted",
    ),
    "cleanup.lifecycle_inventory": SlotSpec(
        key="cleanup.lifecycle_inventory",
        kind="block",
        sources=("lifecycle",),
        rule="lifecycle_complete",
    ),
    "cleanup.reachability_oracle": SlotSpec(
        key="cleanup.reachability_oracle",
        kind="block",
        sources=("reachability",),
        rule="reachability_closed",
    ),
    "cleanup.deletion_proof": SlotSpec(
        key="cleanup.deletion_proof",
        kind="block",
        sources=("lifecycle", "reachability"),
        rule="unadmitted",
    ),
    "cleanup.schema_ownership": SlotSpec(
        key="cleanup.schema_ownership", kind="block", sources=("static",), rule="unadmitted"
    ),
    "cleanup.reconstructability": SlotSpec(
        key="cleanup.reconstructability",
        kind="block",
        sources=("reconciliation",),
        rule="unadmitted",
    ),
    "repository_gates": SlotSpec(
        key="repository_gates",
        kind="hard_gate",
        sources=("reconciliation",),
        rule="unadmitted",
    ),
    "active_static_zero": SlotSpec(
        key="active_static_zero",
        kind="hard_gate",
        sources=("static",),
        rule="unadmitted",
    ),
    "compatibility_parity": SlotSpec(
        key="compatibility_parity",
        kind="hard_gate",
        sources=("test_db",),
        rule="unadmitted",
    ),
    "benchmark_contract": SlotSpec(
        key="benchmark_contract",
        kind="hard_gate",
        sources=("performance",),
        rule="unadmitted",
    ),
    "database_authority": SlotSpec(
        key="database_authority", kind="hard_gate", sources=("test_db",), rule="unadmitted"
    ),
    "deletion_evidence": SlotSpec(
        key="deletion_evidence", kind="hard_gate", sources=("lifecycle",), rule="unadmitted"
    ),
    "network_consolidation_safety": SlotSpec(
        key="network_consolidation_safety",
        kind="hard_gate",
        sources=("reachability",),
        rule="unadmitted",
    ),
    "owner_acceptance": SlotSpec(
        key="owner_acceptance",
        kind="hard_gate",
        sources=("reconciliation",),
        rule="unadmitted",
    ),
    "architecture_duplication_ratchets": SlotSpec(
        key="architecture_duplication_ratchets",
        kind="hard_gate",
        sources=("architecture", "duplicates"),
        rule="unadmitted",
    ),
    "touched_reachability_closure": SlotSpec(
        key="touched_reachability_closure",
        kind="hard_gate",
        sources=("reachability",),
        rule="reachability_closed",
    ),
}
EXPECTED_BLOCKS: tuple[str, ...] = (
    "maintainability.static_quality",
    "maintainability.duplication",
    "maintainability.authorities",
    "maintainability.sustainable_tests",
    "maintainability.enforced_ratchets",
    "efficiency.integrity_audit",
    "efficiency.request_path",
    "efficiency.test_ci",
    "efficiency.dcf_disposition",
    "cleanup.lifecycle_inventory",
    "cleanup.reachability_oracle",
    "cleanup.deletion_proof",
    "cleanup.schema_ownership",
    "cleanup.reconstructability",
)
EXPECTED_GATES: tuple[str, ...] = (
    "repository_gates",
    "active_static_zero",
    "compatibility_parity",
    "benchmark_contract",
    "database_authority",
    "deletion_evidence",
    "network_consolidation_safety",
    "owner_acceptance",
    "architecture_duplication_ratchets",
    "touched_reachability_closure",
)


def _reject_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate JSON key: {k}")
        out[k] = v
    return out


def _decode_object(raw: bytes) -> dict[str, object]:
    text = raw.decode("utf-8")
    payload: object = json.loads(text, object_pairs_hook=_reject_pairs)
    if not isinstance(payload, dict):
        raise ValueError("source must be a JSON object")
    return cast("dict[str, object]", payload)


def _lifecycle_semantic(m: LifecycleInventory) -> bool:
    return (
        m.status == "PASS"
        and len(m.violations) == 0
        and len(m.omissions) == 0
        and len(m.extras) == 0
    )


def _reachability_semantic(m: ReachabilityGraph) -> bool:
    return (
        m.collection_status == "COMPLETE"
        and m.closure_status == "PASS"
        and m.hold is False
        and len(m.unresolved) == 0
        and len(m.unknown_edges) == 0
        and len(m.closure_reasons) == 0
        and len(m.collection_reasons) == 0
    )


def _check_schema(source: SourceName, payload: dict[str, object]) -> str:
    raw_schema = payload.get("schema_version")
    if not isinstance(raw_schema, str):
        raise ValueError("missing schema_version")
    expected = SOURCE_SCHEMAS[source]
    if raw_schema != expected:
        raise ValueError(f"wrong schema: {raw_schema}")
    return raw_schema


def _spec_for(slot: str) -> SlotSpec:
    spec = SLOTS.get(slot)
    if spec is None:
        raise ValueError(f"unknown slot: {slot}")
    return spec


def parse_source(source: SourceName, raw: bytes, expected_subject: str) -> ParsedSource:
    if source not in SOURCE_PATHS or source not in SOURCE_SCHEMAS:
        raise ValueError(f"unknown source: {source}")
    if re.fullmatch(r"[0-9a-f]{40}", expected_subject) is None:
        raise ValueError("invalid expected subject")
    digest = hashlib.sha256(raw).hexdigest()
    path = SOURCE_PATHS[source]
    try:
        payload = _decode_object(raw)
        schema = _check_schema(source, payload)
        typed_valid = False
        semantic_pass = False
        if source == "architecture":
            model_a = ArchitectureReceipt.model_validate_json(raw)
            if model_a.scoped_commit != expected_subject:
                raise ValueError("subject mismatch")
            typed_valid = True
            semantic_pass = False
        elif source == "duplicates":
            model_d = DuplicateInventory.model_validate_json(raw)
            if model_d.commit_hash != expected_subject:
                raise ValueError("subject mismatch")
            typed_valid = True
            semantic_pass = False
        elif source == "static":
            model_s = StaticQualityInventory.model_validate_json(raw)
            if model_s.scoped_commit != expected_subject:
                raise ValueError("subject mismatch")
            typed_valid = True
            semantic_pass = False
        elif source == "test_db":
            model_t = TestDbAudit.model_validate_json(raw)
            if model_t.scoped_commit != expected_subject:
                raise ValueError("subject mismatch")
            typed_valid = True
            semantic_pass = False
        elif source == "reachability":
            model_r = ReachabilityGraph.model_validate_json(raw)
            if model_r.subject_commit != expected_subject:
                raise ValueError("subject mismatch")
            typed_valid = True
            semantic_pass = _reachability_semantic(model_r)
        elif source == "lifecycle":
            model_l = LifecycleInventory.model_validate_json(raw)
            if model_l.revision != expected_subject:
                raise ValueError("subject mismatch")
            typed_valid = True
            semantic_pass = _lifecycle_semantic(model_l)
        elif source == "reconciliation":
            model_c = ReconciliationReceipt.model_validate_json(raw)
            if model_c.subject_commit is None or model_c.subject_commit != expected_subject:
                raise ValueError("subject mismatch")
            typed_valid = True
            semantic_pass = model_c.status == "PASS"
        else:
            model_p = PerformanceReceipt.model_validate_json(raw)
            if model_p.revision != expected_subject:
                raise ValueError("subject mismatch")
            typed_valid = True
            semantic_pass = False
        err = "" if (typed_valid and semantic_pass) else "fail-closed"
        return ParsedSource(
            source=source,
            path=path,
            schema_version=schema,
            sha256=digest,
            typed_valid=typed_valid,
            semantic_pass=semantic_pass,
            error=err[:300],
        )
    except (UnicodeDecodeError, ValueError) as exc:
        return ParsedSource(
            source=source,
            path=path,
            schema_version=SOURCE_SCHEMAS[source],
            sha256=digest,
            typed_valid=False,
            semantic_pass=False,
            error=str(exc)[:300],
        )


def required_paths(slot: str) -> tuple[str, ...]:
    spec = _spec_for(slot)
    return tuple(sorted(SOURCE_PATHS[s] for s in spec.sources))


def evaluate_slot(slot: str, parsed: dict[SourceName, ParsedSource]) -> Verdict:
    spec = _spec_for(slot)
    for name in spec.sources:
        item = parsed.get(name)
        if item is None:
            return "fail"
        if item.source != name:
            return "fail"
        if not item.typed_valid:
            return "fail"
        if not item.semantic_pass:
            return "fail"
        if item.path != SOURCE_PATHS[name] or item.schema_version != SOURCE_SCHEMAS[name]:
            return "fail"
    if spec.rule == "lifecycle_complete":
        return "pass"
    if spec.rule == "reachability_closed":
        return "pass"
    return "fail"


def verify_registry() -> bool:
    if set(SLOTS) != set([*EXPECTED_BLOCKS, *EXPECTED_GATES]):
        return False
    if len(SLOTS) != 24:
        return False
    if len(set(SOURCE_PATHS.values())) != len(SOURCE_PATHS):
        return False
    if len(set(SOURCE_SCHEMAS.values())) != len(SOURCE_SCHEMAS):
        return False
    if tuple(sorted(SOURCE_PATHS)) != tuple(sorted(SOURCE_SCHEMAS)):
        return False
    for key in EXPECTED_BLOCKS:
        spec = SLOTS[key]
        if spec.key != key:
            return False
        if spec.kind != "block":
            return False
        if not spec.sources or spec.sources != tuple(sorted(spec.sources)):
            return False
        if not spec.rule or spec.rule.strip() != spec.rule:
            return False
        for name in spec.sources:
            if name not in SOURCE_PATHS or name not in SOURCE_SCHEMAS:
                return False
    for key in EXPECTED_GATES:
        spec = SLOTS[key]
        if spec.key != key:
            return False
        if spec.kind != "hard_gate":
            return False
        if not spec.sources or spec.sources != tuple(sorted(spec.sources)):
            return False
        if not spec.rule or spec.rule.strip() != spec.rule:
            return False
        for name in spec.sources:
            if name not in SOURCE_PATHS or name not in SOURCE_SCHEMAS:
                return False
    expected_rule_sources: dict[AdmissionRule, tuple[SourceName, ...] | None] = {
        "lifecycle_complete": ("lifecycle",),
        "reachability_closed": ("reachability",),
        "unadmitted": None,
    }
    expected_rule_keys: dict[AdmissionRule, frozenset[str]] = {
        "lifecycle_complete": frozenset({"cleanup.lifecycle_inventory"}),
        "reachability_closed": frozenset(
            {"cleanup.reachability_oracle", "touched_reachability_closure"}
        ),
        "unadmitted": frozenset(k for k, s in SLOTS.items() if s.rule == "unadmitted"),
    }
    if len(expected_rule_keys["unadmitted"]) != 21:
        return False
    if set(expected_rule_keys) != {
        "unadmitted",
        "lifecycle_complete",
        "reachability_closed",
    }:
        return False
    actual_keys: dict[AdmissionRule, set[str]] = {
        "unadmitted": set(),
        "lifecycle_complete": set(),
        "reachability_closed": set(),
    }
    for key, spec in SLOTS.items():
        actual_keys[spec.rule].add(key)
        expected_sources = expected_rule_sources[spec.rule]
        if expected_sources is not None and spec.sources != expected_sources:
            return False
    return all(
        frozenset(actual_keys[rule]) == expected for rule, expected in expected_rule_keys.items()
    )
