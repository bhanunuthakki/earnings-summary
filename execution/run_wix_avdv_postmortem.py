#!/usr/bin/env python3
"""Layer-3 CLI: Run WIX Lifecycle Closure & AVDV Alternative Postmortem (BHA-49).

Executes deterministic evaluation and idempotent persistence of the WIX exit postmortem.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pipeline.queries import open_db  # noqa: E402
from synthesis.wix_avdv_postmortem import (  # noqa: E402
    evaluate_wix_avdv_postmortem,
    persist_wix_avdv_postmortem,
)

log = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate and close WIX lifecycle with AVDV alternative postmortem"
    )
    parser.add_argument(
        "--force", action="store_true", help="Force overwrite of existing postmortem notes"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Evaluate without committing changes to database"
    )
    args = parser.parse_args()

    db_path = PROJECT_ROOT / "data" / "portfolio.db"
    conn = open_db(db_path)

    sys.stderr.write(
        json.dumps(
            {
                "event": "evaluating_wix_avdv_postmortem",
                "force": args.force,
                "dry_run": args.dry_run,
            }
        )
        + "\n"
    )

    try:
        result = evaluate_wix_avdv_postmortem(conn)
        sys.stderr.write(
            json.dumps(
                {
                    "event": "evaluated",
                    "ticker": result.ticker,
                    "outcome_vs_thesis": result.outcome_vs_thesis,
                    "avdv_status": result.avdv_status,
                }
            )
            + "\n"
        )

        if not args.dry_run:
            persist_wix_avdv_postmortem(conn, result, force=args.force)
            conn.commit()
            sys.stderr.write(
                json.dumps({"event": "persisted", "position_entry_id": result.position_entry_id})
                + "\n"
            )

        # Summary JSON to stdout
        output = {
            "status": "ok",
            "dry_run": args.dry_run,
            "postmortem": result.model_dump(),
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))
        return 0
    except Exception as e:
        sys.stderr.write(json.dumps({"event": "failed", "error": str(e)}) + "\n")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
