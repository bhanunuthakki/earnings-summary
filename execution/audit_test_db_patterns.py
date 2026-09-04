"""CLI wrapper for the test database pattern audit."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quality.test_db_patterns import audit_test_db_patterns  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    report = audit_test_db_patterns(args.root)
    payload = report.model_dump_json(indent=2)
    if len(payload.encode()) > 100_000:
        receipt = (
            args.root.resolve() / ".tmp" / "quality" / f"test-db-{report.source_sha256[:24]}.json"
        )
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(payload, encoding="utf-8")
        print(
            json.dumps(
                {
                    "status": report.status,
                    "receipt": receipt.relative_to(args.root.resolve()).as_posix(),
                    "bytes": len(payload.encode()),
                },
                sort_keys=True,
            )
        )
    else:
        print(payload)
    return 0 if report.status == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
