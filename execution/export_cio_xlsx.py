"""Export the Personal-CIO substrate (alerts / queued actions / thesis ledger)
to an .xlsx workbook — the cross-ticker spreadsheet companion to the per-ticker
financial report (report/renderers/workbook.py).

Usage:
    python execution/export_cio_xlsx.py
    python execution/export_cio_xlsx.py --out tmp/cio.xlsx --user-id bhanu
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dashboard.cio_export import export_cio_workbook  # noqa: E402
from identity import DEFAULT_USER_ID  # noqa: E402


def _default_out(repo_root: Path) -> Path:
    return repo_root / "data" / "dashboard" / "cio_export.xlsx"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output .xlsx path. Defaults to data/dashboard/cio_export.xlsx.",
    )
    parser.add_argument(
        "--user-id",
        default=DEFAULT_USER_ID,
        help="user_id to scope the export to. Defaults to $CIO_USER_ID or 'bhanu'.",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Override the portfolio DB path (defaults to <repo>/data/portfolio.db).",
    )
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args()

    repo_root: Path = args.repo_root
    db_path: Path | None = args.db_path
    if db_path is None:
        candidate = repo_root / "data" / "portfolio.db"
        db_path = candidate if candidate.exists() else None
    out_path: Path = args.out if args.out is not None else _default_out(repo_root)

    written = export_cio_workbook(out_path, user_id=args.user_id, db_path=db_path)
    print(f"Wrote CIO workbook -> {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
