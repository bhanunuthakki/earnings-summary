"""Reconcile ``data/issuer_registry.json`` against active ``tracked_companies``.

The IR-document categorizer keys ticker/calendar/name detection off an issuer
registry. Curated-in-code entries (``ir_uploads.ISSUER_REGISTRY``) handle the
tricky fiscal calendars; every other active tracked ticker is maintained
automatically in a JSON store, kept current by the onboard/remove triggers in
``db.py``. This command is the drift-safety net behind those triggers: it makes
the store exactly reflect the current active list — adding entries for tickers
that were added out-of-band (e.g. a raw-SQL ``list_type`` flip the trigger never
saw) and dropping entries for tickers that left the list. ``manual_override``
rows are never touched.

Run it after a bulk list change, or on a schedule next to the daily refresh.

Usage:
    python execution/sync_issuer_registry.py
    python execution/sync_issuer_registry.py --repo-root /abs/path/to/repo
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import issuer_registry  # noqa: E402  (path set above)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root holding data/issuer_registry.json + data/portfolio.db. Default: this repo.",
    )
    args = parser.parse_args(argv)
    summary = issuer_registry.sync_all(args.repo_root.resolve())
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
