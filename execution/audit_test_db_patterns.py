"""CLI wrapper for the test database pattern audit."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from quality.test_db_patterns import audit_test_db_patterns  # noqa: E402

OUTPUT_INLINE_LIMIT = 100_000


def _fail(message: str) -> int:
    payload = json.dumps({"error_code": "delivery-error", "message": message}, sort_keys=True)
    print(payload, file=sys.stderr)
    return 1


def _write_text(target: Path, payload: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        repo = args.root.resolve()
    except OSError:
        return _fail("invalid-root")
    try:
        report = audit_test_db_patterns(repo)
    except Exception:
        return _fail("audit-failed")
    try:
        payload = report.model_dump_json(indent=2) + "\n"
        encoded = payload.encode("utf-8")
    except Exception:
        return _fail("audit-failed")
    try:
        if args.output is not None:
            out = args.output
            if not out.is_absolute():
                out = repo / out
            try:
                resolved = out.resolve()
                resolved.relative_to(repo)
            except (OSError, ValueError):
                return _fail("invalid-output-path")
            _write_text(resolved, payload)
            return (
                0
                if report.collection_status == "COMPLETE" and report.raw_audit_status == "PASS"
                else 2
            )
        if len(encoded) <= OUTPUT_INLINE_LIMIT:
            print(payload)
            return (
                0
                if report.collection_status == "COMPLETE" and report.raw_audit_status == "PASS"
                else 2
            )
        name = f"test-db-{report.source_sha256[:24]}.json"
        receipt = repo / ".tmp" / "quality" / name
        try:
            resolved_receipt = receipt.resolve()
            resolved_receipt.relative_to(repo)
        except (OSError, ValueError):
            return _fail("invalid-output-path")
        _write_text(resolved_receipt, payload)
        summary = json.dumps(
            {
                "admission_status": report.admission_status,
                "bytes": len(encoded),
                "collection_status": report.collection_status,
                "raw_audit_status": report.raw_audit_status,
                "receipt": resolved_receipt.relative_to(repo).as_posix(),
            },
            sort_keys=True,
        )
        print(summary)
        return (
            0 if report.collection_status == "COMPLETE" and report.raw_audit_status == "PASS" else 2
        )
    except OSError:
        return _fail("write-failed")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
