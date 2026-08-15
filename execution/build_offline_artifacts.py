"""CLI entrypoint for sealed offline research artifact builds.

Executes side-effect-free offline report compilation with strict dependency
preflight validation, input manifest freezing, and two-pass determinism verification.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pipeline.offline_build_boundary import OfflineBuildBoundary  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Execute sealed offline research artifact build.")
    parser.add_argument("--ticker", required=True, help="Target equity ticker (e.g. WIX, RBRK, GOOG)")
    parser.add_argument(
        "--as-of-date",
        type=date.fromisoformat,
        default=date.today(),
        help="Fixed historical as-of date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Target output directory (defaults to .tmp/offline_builds/<TICKER>/<DATE>)",
    )
    parser.add_argument(
        "--verify-determinism",
        action="store_true",
        default=True,
        help="Assert byte-identical outputs across two consecutive render passes",
    )
    parser.add_argument("--json", action="store_true", help="Print build receipt JSON to stdout")

    args = parser.parse_args()
    ticker = str(args.ticker).upper().strip()
    as_of_date: date = args.as_of_date
    date_str = as_of_date.isoformat()

    output_dir: Path = args.output_dir or (PROJECT_ROOT / ".tmp" / "offline_builds" / ticker / date_str)

    boundary = OfflineBuildBoundary(repo_root=PROJECT_ROOT)

    try:
        receipt = boundary.execute_sealed_build(
            ticker=ticker,
            as_of_date=as_of_date,
            output_dir=output_dir,
            verify_determinism=args.verify_determinism,
        )
    except Exception as e:
        sys.stderr.write(f"Error during offline build for {ticker}: {e}\n")
        sys.exit(1)

    if args.json:
        print(receipt.model_dump_json(indent=2))
    else:
        print("=== Sealed Offline Build: [PASS] ===")
        print(f"  Ticker:        {receipt.ticker}")
        print(f"  As-Of Date:    {receipt.as_of_date}")
        print(f"  Build ID:      {receipt.build_id}")
        print(f"  Manifest SHA:  {receipt.input_manifest_hash[:16]}...")
        print(f"  HTML SHA:      {receipt.html_sha256[:16]}...")
        print(f"  Markdown SHA:  {receipt.md_sha256[:16]}...")
        print(f"  JSON SHA:      {receipt.sections_sha256[:16]}...")
        print(f"  Determinism:   {'VERIFIED' if receipt.deterministic_two_pass_verified else 'SKIPPED'}")
        print(f"  Duration:      {receipt.duration_ms:.1f}ms")
        print(f"  Artifacts in:  {output_dir}")


if __name__ == "__main__":
    main()
