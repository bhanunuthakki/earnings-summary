"""CLI entrypoint for dual-read reader parity verification.

Verifies that downstream readers consuming provider-neutral adapters produce
byte-and-count identical observations compared to legacy direct JSON reads.
Emits structured JSON parity receipts to .tmp/reader_parity_receipt.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sources.readers import DualReadShadowingVerifier, ParityStatus  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify dual-read parity across data readers.")
    parser.add_argument(
        "--tickers",
        nargs="+",
        default=["WIX", "RBRK"],
        help="List of tickers to verify (default: WIX RBRK)",
    )
    parser.add_argument(
        "--output-receipt",
        type=Path,
        default=PROJECT_ROOT / ".tmp" / "reader_parity_receipt.json",
        help="Output receipt path (default: .tmp/reader_parity_receipt.json)",
    )
    parser.add_argument("--json", action="store_true", help="Print receipt JSON to stdout")
    parser.add_argument(
        "--strict",
        action="store_true",
        default=False,
        help="Require 100% verified matches (fail closed with exit code 2 on PARTIAL status)",
    )

    args = parser.parse_args()
    tickers: list[str] = [t.upper().strip() for t in args.tickers]
    output_receipt: Path = args.output_receipt

    verifier = DualReadShadowingVerifier(repo_root=PROJECT_ROOT)
    receipts: list[dict[str, Any]] = []

    for t in tickers:
        price_receipt = verifier.verify_price_parity(t)
        est_receipt = verifier.verify_estimates_parity(t)
        seg_geo_receipt = verifier.verify_segments_parity(t, dim_type="geography")
        seg_prod_receipt = verifier.verify_segments_parity(t, dim_type="product")
        filing_receipt = verifier.verify_filing_sections_parity(t, form="10-K")

        for r in (price_receipt, est_receipt, seg_geo_receipt, seg_prod_receipt, filing_receipt):
            receipts.append(r.model_dump(mode="json"))

    verified_matches = sum(1 for r in receipts if r["status"] == ParityStatus.VERIFIED_MATCH.value)
    divergences = sum(1 for r in receipts if r["status"] == ParityStatus.VERIFIED_DIVERGENCE.value)
    indeterminates = sum(1 for r in receipts if r["status"] == ParityStatus.INDETERMINATE_UNAVAILABLE.value)

    if divergences > 0 or verified_matches == 0:
        overall_status = "FAIL"
    elif indeterminates > 0:
        overall_status = "PARTIAL"
    else:
        overall_status = "PASS"

    summary = {
        "status": overall_status,
        "verified_at": datetime.now(UTC).isoformat(),
        "total_checks": len(receipts),
        "verified_matches": verified_matches,
        "divergences": divergences,
        "indeterminate_unavailable": indeterminates,
        "tickers": tickers,
        "receipts": receipts,
    }

    output_receipt.parent.mkdir(parents=True, exist_ok=True)
    output_receipt.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(
            f"Reader parity check complete. Status: {summary['status']} "
            f"({verified_matches} verified matches, {divergences} divergences, {indeterminates} unavailable)"
        )
        print(f"Receipt written to: {output_receipt}")

    if overall_status == "FAIL":
        sys.exit(1)
    elif overall_status == "PARTIAL" and args.strict:
        print("Strict mode enabled: failing on PARTIAL status.")
        sys.exit(2)


if __name__ == "__main__":
    main()
