"""Create a new offline reading copy; review visible analysis before sharing."""

from __future__ import annotations

import argparse
from pathlib import Path

from report.shareable import ShareableReportError, export_shareable_report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Existing standalone workspace HTML")
    parser.add_argument(
        "destination", type=Path, help="New output HTML; existing files are refused"
    )
    args = parser.parse_args()
    try:
        export_shareable_report(args.source, args.destination)
    except (OSError, ShareableReportError) as exc:
        parser.exit(1, f"Export failed ({type(exc).__name__}). No existing file was changed.\n")
    print("Read-only copy created. Review visible analysis for private information before sharing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
