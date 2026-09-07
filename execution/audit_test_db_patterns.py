"""CLI wrapper for the test database pattern audit."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from quality.test_db_patterns import (  # noqa: E402
    InvocationConversion,
    audit_test_db_patterns,
)

OUTPUT_INLINE_LIMIT = 100_000


def _fail(message: str) -> int:
    payload = json.dumps({"error_code": "delivery-error", "message": message}, sort_keys=True)
    print(payload, file=sys.stderr)
    return 1


def _write_text(target: Path, payload: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload, encoding="utf-8")


def _parse_expires(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            raise ValueError("invalid-expires")
        return parsed
    raise ValueError("invalid-expires")


def _load_conversions(raw: object) -> tuple[InvocationConversion, ...]:
    if not isinstance(raw, list):
        raise ValueError("invalid-dispositions")
    entries = cast(list[object], raw)
    loaded: list[InvocationConversion] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("invalid-dispositions")
        entry_dict = cast(dict[object, object], entry)
        locator_raw = entry_dict.get("locator")
        if not isinstance(locator_raw, dict):
            raise ValueError("invalid-dispositions")
        locator_dict = cast(dict[object, object], locator_raw)
        expires_raw = entry_dict.get("expires_at")
        expires = _parse_expires(expires_raw)
        invocation_raw = entry_dict.get("invocation_id")
        path_raw = entry_dict.get("path")
        sha_raw = entry_dict.get("source_sha256")
        receipt_raw = entry_dict.get("parity_receipt", "")
        issue_raw = entry_dict.get("owner_issue", "")
        reason_raw = entry_dict.get("reason", "")
        if not isinstance(invocation_raw, str) or not isinstance(path_raw, str):
            raise ValueError("invalid-dispositions")
        if not isinstance(sha_raw, str):
            raise ValueError("invalid-dispositions")
        if (
            not isinstance(receipt_raw, str)
            or not isinstance(issue_raw, str)
            or not isinstance(reason_raw, str)
        ):
            raise ValueError("invalid-dispositions")
        start_line = locator_dict.get("start_line")
        start_col = locator_dict.get("start_col")
        end_line = locator_dict.get("end_line")
        end_col = locator_dict.get("end_col")
        if (
            not isinstance(start_line, int)
            or not isinstance(start_col, int)
            or not isinstance(end_line, int)
            or not isinstance(end_col, int)
        ):
            raise ValueError("invalid-dispositions")
        loaded.append(
            InvocationConversion.model_validate(
                {
                    "invocation_id": invocation_raw,
                    "path": path_raw,
                    "locator": {
                        "start_line": start_line,
                        "start_col": start_col,
                        "end_line": end_line,
                        "end_col": end_col,
                    },
                    "source_sha256": sha_raw,
                    "parity_receipt": receipt_raw,
                    "owner_issue": issue_raw,
                    "reason": reason_raw,
                    "expires_at": expires,
                }
            )
        )
    return tuple(loaded)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--dispositions", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        repo = args.root.resolve()
    except OSError:
        return _fail("invalid-root")
    try:
        dispositions: tuple[InvocationConversion, ...] = tuple()
        if args.dispositions is not None:
            disp_path = args.dispositions
            if not disp_path.is_absolute():
                disp_path = repo / disp_path
            try:
                resolved_disp = disp_path.resolve()
                resolved_disp.relative_to(repo)
                text = resolved_disp.read_text(encoding="utf-8")
                raw: object = json.loads(text)
                dispositions = _load_conversions(raw)
            except Exception:
                return _fail("invalid-dispositions")
        report = audit_test_db_patterns(repo, dispositions)
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
