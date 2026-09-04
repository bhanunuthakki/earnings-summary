"""Compare two raw test/CI performance receipts and emit a typed HOLD/INVALID result."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.quality.test_ci_pairing import (  # noqa: E402
    evaluate_test_ci_pair,
    write_pairing_receipt,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, nargs="+", required=True)
    parser.add_argument("--current", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path)
    return parser


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"cannot read receipt {path}: {type(exc).__name__}") from None


def main() -> int:
    args = _parser().parse_args()
    from src.quality.test_ci_pairing import aggregate_test_ci_pairs

    if len(args.baseline) == 1 and len(args.current) == 1:
        receipt = evaluate_test_ci_pair(_read(args.baseline[0]), _read(args.current[0]))
    else:
        receipt = aggregate_test_ci_pairs(
            [json.loads(_read(path)) for path in args.baseline],
            [json.loads(_read(path)) for path in args.current],
        )
    payload = json.dumps(receipt.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    if args.output is None:
        sys.stdout.write(payload)
    else:
        write_pairing_receipt(receipt, args.output)
    return 0 if receipt.status == "PASS" else 1 if receipt.status == "INVALID" else 2


if __name__ == "__main__":
    raise SystemExit(main())
