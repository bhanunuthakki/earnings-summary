"""Read-only common-drawdown evidence, optionally one paired synthetic proposal."""

from __future__ import annotations

import os
from pathlib import Path

try:
    from _lib import PROJECT_ROOT, command_parser
except ImportError:
    from execution._lib import PROJECT_ROOT, command_parser

from research.qualitative_stress import paired_wix_avdv_scenario, read_common_drawdown


def main(argv: list[str] | None = None) -> int:
    parser = command_parser(__doc__)
    parser.add_argument("--db-path", type=Path, default=os.environ.get("EARNINGS_SUMMARY_DB_PATH"))
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--paired-avdv-target",
        type=float,
        help="Explicit fractional synthetic target in unverified 0.045-0.05 band",
    )
    args = parser.parse_args(argv)
    if args.db_path is None or not args.db_path.is_file():
        parser.error("An existing explicit/configured database is required; no checkout fallback")
    try:
        result = (
            read_common_drawdown(args.db_path, args.repo_root)
            if args.paired_avdv_target is None
            else paired_wix_avdv_scenario(args.db_path, args.repo_root, args.paired_avdv_target)
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(result.model_dump_json(indent=2))
    return 0 if result.state in ("full", "partial") else 2


if __name__ == "__main__":
    raise SystemExit(main())
