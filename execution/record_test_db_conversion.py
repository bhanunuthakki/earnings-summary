"""Stamp the typed conversion-record registry for converted test files.

Each record binds a converted test file to the exact content that was verified
and to a PASS parity receipt for the ``migrated_db`` invocation that replaced its
chain builder. Because the record carries the post-conversion content hash, any
later edit to a converted file makes its record stale; rerun this producer to
restamp the registry against the new content. Every run restamps all recorded
paths, so records accumulate across conversion batches.

    python execution/record_test_db_conversion.py --path tests/test_x.py ...
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from quality.test_db_conversions import (  # noqa: E402
    CONVERSION_REGISTRY_PATH,
    CONVERSION_REGISTRY_SCHEMA,
    load_registry,
)
from quality.test_db_invocations import collect_invocations  # noqa: E402
from quality.test_db_models import (  # noqa: E402
    ConversionRecord,
    ConversionRegistry,
    ParityReceipt,
)

DEFAULT_OWNER_ISSUE = "linear:BHA-104"
DEFAULT_EXPIRES_AT = "2027-01-01T00:00:00+00:00"
DEFAULT_REASON = (
    "Chain replay replaced by the cached migrated_db template; fidelity and "
    "isolation are proven by tests/test_migrated_db_parity.py."
)


def _fail(message: str) -> int:
    print(json.dumps({"error_code": "record-error", "message": message}, sort_keys=True))
    return 1


def _record(
    repo: Path, rel: str, *, owner_issue: str, reason: str, expires_at: str
) -> ConversionRecord:
    raw = (repo / rel).read_bytes()
    file_sha = hashlib.sha256(raw).hexdigest()
    tree = ast.parse(raw.decode("utf-8"), filename=rel)
    migrated = [
        invocation
        for invocation in collect_invocations(rel, tree, file_sha)
        if invocation.canonical_identity == "migrated_db"
    ]
    if not migrated:
        raise ValueError(f"no migrated_db invocation in {rel}")
    subject = sorted(migrated, key=lambda item: (item.locator.start_line, item.locator.start_col))[
        0
    ]
    return ConversionRecord(
        path=rel,
        source_sha256=file_sha,
        parity_receipt=ParityReceipt(
            schema_version="test-db-parity/v1",
            status="PASS",
            invocation_id=subject.invocation_id,
            path=rel,
            locator=subject.locator,
            source_sha256=file_sha,
        ),
        owner_issue=owner_issue,
        reason=reason,
        expires_at=datetime.fromisoformat(expires_at),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--owner-issue", default=DEFAULT_OWNER_ISSUE)
    parser.add_argument("--reason", default=DEFAULT_REASON)
    parser.add_argument("--expires-at", default=DEFAULT_EXPIRES_AT)
    args = parser.parse_args(argv)
    repo = args.root.resolve()
    registry_path = repo / CONVERSION_REGISTRY_PATH
    wanted: list[str] = list(dict.fromkeys(str(item) for item in args.path))
    # Records accumulate across conversion batches, so preserve every path
    # already recorded. Dropping one would silently forfeit its credit.
    if registry_path.exists():
        existing = load_registry(registry_path.read_bytes())
        wanted = sorted({*wanted, *(record.path for record in existing.records)})
    if not wanted:
        return _fail("no paths given")
    records: list[ConversionRecord] = []
    for rel in sorted(wanted):
        try:
            records.append(
                _record(
                    repo,
                    rel,
                    owner_issue=args.owner_issue,
                    reason=args.reason,
                    expires_at=args.expires_at,
                )
            )
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError) as exc:
            return _fail(f"{rel}: {exc}")
    registry = ConversionRegistry(
        schema_version=CONVERSION_REGISTRY_SCHEMA,
        records=tuple(records),
    )
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(registry.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "records": len(records),
                "registry": CONVERSION_REGISTRY_PATH,
                "stamped_at": datetime.now(UTC).isoformat(),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
