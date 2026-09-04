from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from src.quality.architecture import ArchitectureReceipt
from src.quality.duplicates import DuplicateInventory
from src.quality.reachability import ReachabilityGraph
from src.quality.roadmap_reconciliation import CurrentReceipts, reconcile
from src.quality.static_quality import StaticQualityInventory
from src.quality.test_db_patterns import TestDbAudit as DbAuditReceipt

PROJECT_ROOT = Path(__file__).resolve().parents[1]
Receipt = TypeVar("Receipt", bound=BaseModel)


def _load(name: str, model: type[Receipt]) -> Receipt:
    return model.model_validate_json((PROJECT_ROOT / "docs/quality" / name).read_bytes())


def _seed(tmp_path: Path, *, omit: str | None = None) -> CurrentReceipts:
    receipts = CurrentReceipts(
        architecture=_load("architecture-ratchet.json", ArchitectureReceipt),
        duplicates=_load("duplicates-ratchet.json", DuplicateInventory),
        static=_load("static-baseline.json", StaticQualityInventory),
        test_db=_load("test-db-patterns-baseline.json", DbAuditReceipt),
        reachability=ReachabilityGraph.model_validate_json(
            (PROJECT_ROOT / ".tmp/quality/reachability-check.json").read_bytes()
        ),
    )
    values = {
        "architecture-ratchet.json": receipts.architecture,
        "duplicates-ratchet.json": receipts.duplicates,
        "static-baseline.json": receipts.static,
        "test-db-patterns-baseline.json": receipts.test_db,
    }
    for name, value in values.items():
        if name == omit:
            continue
        path = tmp_path / "docs/quality" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value.model_dump_json(indent=2) + "\n", encoding="utf-8")
    graph_path = tmp_path / ".tmp/quality/reachability-check.json"
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    graph_path.write_text(receipts.reachability.model_dump_json(indent=2) + "\n", encoding="utf-8")
    roadmap = tmp_path / ".tmp" / "quality-9plus-roadmap.md"
    roadmap.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(PROJECT_ROOT.parent / "quality-9plus-roadmap.md", roadmap)
    return receipts


def _rewrite(tmp_path: Path, name: str, mutate: Callable[[dict[str, object]], None]) -> None:
    path = tmp_path / "docs/quality" / name
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _wrong_schema(value: dict[str, object]) -> None:
    value["schema_version"] = "wrong"


def _parse_error(value: dict[str, object]) -> None:
    value["parse_errors"] = ["bad"]


def _hold(value: dict[str, object]) -> None:
    value["status"] = "HOLD"


def _wrong_source_hash(value: dict[str, object]) -> None:
    value["source_hash"] = "0" * 64


def test_unsupported_claims_are_rejected_without_blocking_pass(tmp_path: Path) -> None:
    current = _seed(tmp_path)
    result = reconcile(tmp_path, current_receipts=current)
    timing = next(claim for claim in result.claims if claim.name == "full suite seconds")
    queue = next(claim for claim in result.claims if claim.name == "unreachable scripts")
    assert timing.verdict == queue.verdict == "rejected"
    assert not timing.scored_eligible and not queue.scored_eligible
    assert result.status == "PASS"
    assert len({claim.name for claim in result.claims}) == len(result.claims)
    assert len(result.claims) == 96
    assert {
        "production noncomment LOC",
        "maximum internal fan-out",
        "largest SCC second size",
        "near-miss duplicate groups",
        "test files with direct command.upgrade",
        "full suite display seconds",
        "Pyright omitted files",
        "unreachable queue LOC",
        "direct-builder ratchet correction",
        "theme_synth live-edge correction",
        "refetch_aggregator absence correction",
    } <= {claim.name for claim in result.claims}
    assert all(
        claim.provisional_evidence is not None
        and claim.provisional_evidence.sha256 == result.roadmap_source.sha256
        and claim.provisional_evidence.locator != "line 0"
        for claim in result.claims
    )


def test_typed_current_receipts_correct_stale_provisional_values(tmp_path: Path) -> None:
    current = _seed(tmp_path)
    result = reconcile(tmp_path, current_receipts=current)
    module = next(claim for claim in result.claims if claim.name == "production module count")
    migration = next(claim for claim in result.claims if claim.name == "migrated_db files")
    ddl = next(claim for claim in result.claims if claim.name == "hand-written DDL files")
    assert module.verdict == "corrected"
    assert module.observed == current.architecture.metrics.executable_modules
    assert migration.observed == sum(
        "call:migrated_db" in builder.evidence for builder in current.test_db.database_builders
    )
    assert ddl.observed == sum(
        any(item.startswith("sql:") for item in builder.evidence)
        for builder in current.test_db.database_builders
    )
    assert module.evidence is not None


def test_partial_or_tampered_receipt_holds_and_cannot_score(tmp_path: Path) -> None:
    current = _seed(tmp_path)
    path = tmp_path / "docs/quality/architecture-ratchet.json"
    path.write_text('{"metrics":{"executable_modules":1311}}', encoding="utf-8")
    result = reconcile(tmp_path, current_receipts=current)
    architecture_claims = result.claims[:3]
    assert result.status == "HOLD"
    assert all(claim.verdict == "rejected" for claim in architecture_claims)
    assert not any(claim.scored_eligible for claim in architecture_claims)
    assert "typed schema" in result.violations[0]


def test_wrong_schema_status_parse_errors_and_hash_are_inadmissible(tmp_path: Path) -> None:
    mutators: tuple[tuple[str, Callable[[dict[str, object]], None]], ...] = (
        ("duplicates-ratchet.json", _wrong_schema),
        ("duplicates-ratchet.json", _parse_error),
        ("static-baseline.json", _hold),
        ("static-baseline.json", _wrong_source_hash),
    )
    for index, (name, mutate) in enumerate(mutators):
        case = tmp_path / str(index)
        current = _seed(case)
        _rewrite(case, name, mutate)
        result = reconcile(case, current_receipts=current)
        assert result.status == "HOLD"
        assert any(name in violation for violation in result.violations)


def test_missing_required_receipt_holds(tmp_path: Path) -> None:
    current = _seed(tmp_path, omit="test-db-patterns-baseline.json")
    result = reconcile(tmp_path, current_receipts=current)
    assert result.status == "HOLD"
    migration = next(claim for claim in result.claims if claim.name == "migrated_db files")
    assert migration.verdict == "rejected" and not migration.scored_eligible


def test_commit_and_local_receipt_paths_do_not_invalidate_semantics(tmp_path: Path) -> None:
    current = _seed(tmp_path)
    _rewrite(
        tmp_path,
        "architecture-ratchet.json",
        lambda value: value.__setitem__("scoped_commit", "prior-commit"),
    )
    _rewrite(
        tmp_path,
        "static-baseline.json",
        lambda value: value.__setitem__("repo_root", "/different/checkout"),
    )
    result = reconcile(tmp_path, current_receipts=current)
    assert result.status == "PASS"


def test_reconciliation_hash_is_deterministic(tmp_path: Path) -> None:
    current = _seed(tmp_path)
    assert (
        reconcile(tmp_path, current_receipts=current).source_hash
        == reconcile(tmp_path, current_receipts=current).source_hash
    )


def test_roadmap_omission_or_tampering_holds_and_is_unscored(tmp_path: Path) -> None:
    current = _seed(tmp_path)
    roadmap = tmp_path / ".tmp" / "quality-9plus-roadmap.md"
    roadmap.write_text(
        roadmap.read_text(encoding="utf-8").replace("554,615 LOC", "554,614 LOC"), encoding="utf-8"
    )
    result = reconcile(tmp_path, current_receipts=current)
    module = next(claim for claim in result.claims if claim.name == "production noncomment LOC")
    assert result.status == "HOLD"
    assert module.verdict == "rejected" and not module.scored_eligible
    assert "roadmap source hash" in " ".join(result.violations)


def test_invalid_utf8_roadmap_holds_instead_of_crashing(tmp_path: Path) -> None:
    current = _seed(tmp_path)
    (tmp_path / ".tmp/quality-9plus-roadmap.md").write_bytes(b"\xff")
    result = reconcile(tmp_path, current_receipts=current)
    assert result.status == "HOLD"
    assert "roadmap source is not valid UTF-8" in result.violations
    assert not any(claim.scored_eligible for claim in result.claims)


def test_named_audit_corrections_are_reconciled(tmp_path: Path) -> None:
    current = _seed(tmp_path)
    result = reconcile(tmp_path, current_receipts=current)
    names = {
        "theme_synth live-edge correction",
        "filings.boilerplate_classify live-edge correction",
        "filings.cross_sectional_detrend live-edge correction",
        "ask.turn_cache live-edge correction",
        "etf_sources.vanguard live-edge correction",
        "refetch_aggregator absence correction",
        "refetch_aggregator_transcripts existence correction",
    }
    corrections = [claim for claim in result.claims if claim.name in names]
    assert len(corrections) == len(names)
    assert all(claim.observed is True and claim.scored_eligible for claim in corrections)
