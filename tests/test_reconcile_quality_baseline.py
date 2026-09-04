from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from src.quality.architecture import ArchitectureReceipt
from src.quality.duplicates import DuplicateInventory
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
    timing = next(claim for claim in result.claims if claim.name == "full local suite seconds")
    queue = next(
        claim for claim in result.claims if claim.name == "unreachable executable-script queue"
    )
    assert timing.verdict == queue.verdict == "rejected"
    assert not timing.scored_eligible and not queue.scored_eligible
    assert result.status == "PASS"
    assert len({claim.name for claim in result.claims}) == len(result.claims) == 12


def test_typed_current_receipts_correct_stale_provisional_values(tmp_path: Path) -> None:
    current = _seed(tmp_path)
    result = reconcile(tmp_path, current_receipts=current)
    module = next(claim for claim in result.claims if claim.name == "executable module count")
    migration = next(
        claim for claim in result.claims if claim.name == "test files with direct command.upgrade"
    )
    assert module.verdict == "corrected"
    assert module.observed == current.architecture.metrics.executable_modules
    assert migration.observed == sum(
        "call:command.upgrade" in builder.evidence for builder in current.test_db.database_builders
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
    migration = next(
        claim for claim in result.claims if claim.name == "test files with direct command.upgrade"
    )
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
