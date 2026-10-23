"""Enforce the frozen architecture and duplication ratchets.

The check is receipt-based and fail-closed, mirroring
``scripts/check_architecture_boundaries.py`` for the two growth seams that
seam-level allowlists cannot see:

- ``docs/quality/architecture-ratchet.json`` and
  ``docs/quality/duplicates-ratchet.json`` are committed measurement baselines;
  the current tree is re-measured and compared against them.  Monotonic
  growth — ``executable_modules``, ``total_noncomment_loc``, the LOC buckets,
  ``max_internal_fan_out``, SCC shapes, and duplicated LOC — fails with each
  regression named, so sprawl has to be an explicit, reviewed baseline refresh
  rather than silent drift.
- Scanner drift is never evidence: a ``scanner_sha256``/``scanner_hash``
  mismatch, a parse error, or an unreadable baseline holds the gate (exit 2)
  until the baseline is deliberately re-measured with the current scanner.
- ``quality.admission_policy`` is invoked directly, so the slot/source policy
  that consumes these receipts can no longer drift unverified.

Exit codes: 0 pass; 1 metric regression or admission-registry mismatch;
2 hold (the comparison cannot be trusted — the gate never guesses).
"""

from __future__ import annotations

import sys
from pathlib import Path

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quality.admission_policy import verify_registry
from quality.architecture import (
    ArchitectureReceipt,
    build_architecture_receipt,
    compare_architecture,
)
from quality.duplicates import (
    DuplicateInventory,
    build_inventory,
    compare_inventory,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURE_BASELINE = PROJECT_ROOT / "docs/quality/architecture-ratchet.json"
DUPLICATES_BASELINE = PROJECT_ROOT / "docs/quality/duplicates-ratchet.json"


def validate(
    root: Path | None = None,
    architecture_baseline: Path | None = None,
    duplicates_baseline: Path | None = None,
) -> tuple[list[str], bool]:
    """Return the ratchet gate's ``(messages, hold)`` verdict for ``root``.

    ``messages`` is empty only on a pass.  ``hold`` is true when the comparison
    cannot be trusted (scanner drift, parse errors, unreadable baselines);
    a hold is never treated as evidence in either direction.
    """
    repo_root = PROJECT_ROOT if root is None else root
    architecture_path = (
        ARCHITECTURE_BASELINE if architecture_baseline is None else architecture_baseline
    )
    duplicates_path = DUPLICATES_BASELINE if duplicates_baseline is None else duplicates_baseline
    try:
        architecture_current = build_architecture_receipt(repo_root, "WORKTREE")
        architecture_prior = ArchitectureReceipt.model_validate_json(
            architecture_path.read_text(encoding="utf-8")
        )
        architecture_ratchet = compare_architecture(architecture_current, architecture_prior)
        duplicates_current = build_inventory(repo_root, "WORKTREE")
        duplicates_prior = DuplicateInventory.model_validate_json(
            duplicates_path.read_text(encoding="utf-8")
        )
        duplicates_ratchet = compare_inventory(duplicates_current, duplicates_prior)
    except (OSError, ValueError, ValidationError) as exc:
        return [f"ratchet baseline is unreadable or unmeasurable: {exc}"], True

    if architecture_ratchet.status == "HOLD" or duplicates_ratchet.status == "HOLD":
        messages = [
            "ratchet evidence is untrusted: re-measure the baseline deliberately "
            "in the same reviewed change that touched the scanner"
        ]
        if architecture_ratchet.scanner_mismatch:
            messages.append("architecture scanner_sha256 drifted from the baseline")
        if duplicates_ratchet.scanner_mismatch:
            messages.append("duplicates scanner_hash drifted from the baseline")
        messages.extend(architecture_ratchet.regressions)
        messages.extend(duplicates_ratchet.regressions)
        return messages, True

    messages = [f"architecture: {item}" for item in architecture_ratchet.regressions]
    messages.extend(f"duplicates: {item}" for item in duplicates_ratchet.regressions)
    if not verify_registry():
        messages.append(
            "quality admission registry mismatch: SLOTS no longer matches "
            "EXPECTED_BLOCKS/EXPECTED_GATES (src/quality/admission_policy.py)"
        )
    return messages, False


def _print_messages(messages: list[str]) -> None:
    for message in messages:
        print(f"  - {message}", file=sys.stderr)


def main() -> int:
    messages, hold = validate()
    if hold:
        print("architecture-ratchets: HOLD", file=sys.stderr)
        _print_messages(messages)
        return 2
    if messages:
        print("architecture-ratchets: FAILED", file=sys.stderr)
        _print_messages(messages)
        return 1
    print("architecture-ratchets: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
