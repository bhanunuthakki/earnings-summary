"""Capture Train 0 compatibility evidence as a typed JSON receipt."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quality.compatibility import (  # noqa: E402
    CompatibilityEvidenceError,
    capture_compatibility_evidence,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--baseline-revision", "--baseline", required=True)
    parser.add_argument("--out", "--output", type=Path)
    args = parser.parse_args(argv)
    try:
        receipt = capture_compatibility_evidence(args.repo_root, args.baseline_revision)
    except CompatibilityEvidenceError as exc:
        print(
            json.dumps(
                {"event": "compatibility_evidence_failed", "error": str(exc)}, sort_keys=True
            ),
            file=sys.stderr,
        )
        return 2
    payload = receipt.model_dump_json(indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
    elif len(payload.encode("utf-8")) > 100_000:
        output = (
            args.repo_root.resolve()
            / ".tmp"
            / "quality"
            / f"compatibility-{receipt.source_sha256[:24]}.json"
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
        sys.stdout.write(
            json.dumps(
                {
                    "hold": receipt.hold,
                    "output": output.relative_to(args.repo_root.resolve()).as_posix(),
                },
                sort_keys=True,
            )
            + "\n"
        )
    else:
        sys.stdout.write(payload)
    return 2 if receipt.hold else 0


if __name__ == "__main__":
    raise SystemExit(main())
