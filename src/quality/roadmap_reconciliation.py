"""Reconcile every provisional roadmap baseline against fresh typed receipts."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Literal, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .architecture import ArchitectureReceipt, build_architecture_receipt
from .duplicates import DuplicateInventory, build_inventory
from .static_quality import StaticQualityInventory, inventory
from .test_db_patterns import TestDbAudit, audit_test_db_patterns

Verdict = Literal["verified", "corrected", "rejected"]
Number = int | float
ReceiptModel = TypeVar("ReceiptModel", bound=BaseModel)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    sha256: str
    locator: str


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    provisional_expected: Number | None
    observed: Number | None
    verdict: Verdict
    scored_eligible: bool
    evidence: Evidence | None
    note: str


class ReconciliationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "bha-121.v4"
    status: Literal["PASS", "HOLD"]
    claims: tuple[Claim, ...]
    scored_claims: int
    rejected_claims: int
    source_hash: str
    violations: tuple[str, ...] = Field(default_factory=tuple)


@dataclass(frozen=True)
class CurrentReceipts:
    """Fresh generator results used to admit checked-in evidence."""

    architecture: ArchitectureReceipt
    duplicates: DuplicateInventory
    static: StaticQualityInventory
    test_db: TestDbAudit


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
    )


def _count_upgrade_builders(receipt: TestDbAudit) -> int:
    return sum("call:command.upgrade" in builder.evidence for builder in receipt.database_builders)


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
            receipt.schema_version == "bha-120.v1"
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
    sources = (architecture, duplicates, static, test_db)
    claims: list[Claim] = []

    def add(
        name: str,
        expected: Number | None,
        source: (
            ReceiptSource[ArchitectureReceipt]
            | ReceiptSource[DuplicateInventory]
            | ReceiptSource[StaticQualityInventory]
            | ReceiptSource[TestDbAudit]
            | None
        ),
        locator: str,
        extractor: Callable[[], Number | None],
        *,
        rejection_note: str = "Required typed receipt is missing, stale, or inadmissible.",
    ) -> None:
        observed = extractor() if source is not None and source.admitted else None
        if observed is None:
            verdict: Verdict = "rejected"
            eligible = False
            note = source.rejection if source and source.rejection else rejection_note
        else:
            verdict = "verified" if expected is None or observed == expected else "corrected"
            eligible = True
            note = (
                "Fresh typed generator reproduces the provisional value."
                if verdict == "verified"
                else "Fresh typed generator corrects the provisional value."
            )
        claims.append(
            Claim(
                name=name,
                provisional_expected=expected,
                observed=observed,
                verdict=verdict,
                scored_eligible=eligible,
                evidence=source.evidence(locator) if source else None,
                note=note,
            )
        )

    add(
        "executable module count",
        1291,
        architecture,
        "$.metrics.executable_modules",
        lambda: architecture.receipt.metrics.executable_modules if architecture.receipt else None,
    )
    add(
        "strongly connected component count",
        16,
        architecture,
        "$.metrics.scc_count",
        lambda: architecture.receipt.metrics.scc_count if architecture.receipt else None,
    )
    add(
        "maximum internal fan-out",
        156,
        architecture,
        "$.metrics.max_internal_fan_out",
        lambda: architecture.receipt.metrics.max_internal_fan_out if architecture.receipt else None,
    )
    add(
        "exact duplicate groups",
        140,
        duplicates,
        "$.exact_totals.groups",
        lambda: duplicates.receipt.exact_totals.groups if duplicates.receipt else None,
    )
    add(
        "near-miss duplicate groups",
        None,
        duplicates,
        "$.near_miss_totals.groups",
        lambda: duplicates.receipt.near_miss_totals.groups if duplicates.receipt else None,
    )
    add(
        "full local suite seconds",
        1046.92,
        None,
        "unavailable",
        lambda: None,
        rejection_note="The reported 1046.92-second run has no retained typed timing receipt; the claim is rejected and cannot score.",
    )
    add(
        "test files with direct command.upgrade",
        172,
        test_db,
        "$.database_builders[*].evidence contains call:command.upgrade",
        lambda: _count_upgrade_builders(test_db.receipt) if test_db.receipt else None,
    )
    add(
        "Ruff diagnostics",
        2,
        static,
        "$.diagnostics[tool=ruff].count",
        lambda: _diagnostic_count(static.receipt, "ruff") if static.receipt else None,
    )
    add(
        "Ruff format files",
        61,
        static,
        "$.diagnostics[tool=ruff-format].count",
        lambda: _diagnostic_count(static.receipt, "ruff-format") if static.receipt else None,
    )
    add(
        "strict Pyright diagnostics",
        27924,
        static,
        "$.diagnostics[tool=pyright].count",
        lambda: _diagnostic_count(static.receipt, "pyright") if static.receipt else None,
    )
    add(
        "type-suppression directives",
        588,
        static,
        "$.diagnostics[tool=source-ignore-comments].count",
        lambda: (
            _diagnostic_count(static.receipt, "source-ignore-comments") if static.receipt else None
        ),
    )
    add(
        "unreachable executable-script queue",
        85,
        None,
        "unavailable",
        lambda: None,
        rejection_note="The provisional 85-script scan has no retained generator/receipt and is not equivalent to reachability unknown edges; it is rejected, not reused.",
    )

    violations = [
        f"inadmissible source receipt {source.relative}: {source.rejection}"
        for source in sources
        if not source.admitted
    ]
    expected_names = {
        "executable module count",
        "strongly connected component count",
        "maximum internal fan-out",
        "exact duplicate groups",
        "near-miss duplicate groups",
        "full local suite seconds",
        "test files with direct command.upgrade",
        "Ruff diagnostics",
        "Ruff format files",
        "strict Pyright diagnostics",
        "type-suppression directives",
        "unreachable executable-script queue",
    }
    actual_names = [claim.name for claim in claims]
    if set(actual_names) != expected_names or len(actual_names) != len(expected_names):
        violations.append("roadmap claim set is incomplete or duplicated")
    for claim in claims:
        eligible_shape = (
            claim.verdict in {"verified", "corrected"}
            and claim.observed is not None
            and claim.evidence is not None
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
    return ReconciliationReceipt(
        status="HOLD" if violations else "PASS",
        claims=tuple(claims),
        scored_claims=sum(claim.scored_eligible for claim in claims),
        rejected_claims=sum(claim.verdict == "rejected" for claim in claims),
        source_hash=digest.hexdigest(),
        violations=tuple(violations),
    )
