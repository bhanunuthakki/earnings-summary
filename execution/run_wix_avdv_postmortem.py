#!/usr/bin/env python3
"""Report the unavailable WIX/AVDV postmortem without opening any database."""

from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report missing evidence for WIX lifecycle closure and AVDV postmortem"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Retained for compatibility; cannot override missing evidence",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Report missing evidence without database access"
    )
    args = parser.parse_args()
    print(
        json.dumps(
            {
                "status": "hold",
                "reason": "postmortem_evidence_unavailable",
                "message": (
                    "Refreshed holdings, verified execution details, frozen decision/horizon "
                    "and owner-reviewed attribution are not wired. No database was opened "
                    "and no lifecycle or owner record was changed."
                ),
                "dry_run": args.dry_run,
                "force": args.force,
            },
            sort_keys=True,
        )
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
