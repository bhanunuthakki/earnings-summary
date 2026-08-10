"""Index surviving workspace reports without rebuilding historical content."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from report.artifacts import CoverageRole, reconcile_legacy_workspace_reports  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402


def _coverage_roles(repo_root: Path) -> dict[str, CoverageRole]:
    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        return {}
    conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    try:
        rows = conn.execute(
            "SELECT ticker, list_type FROM tracked_companies "
            "WHERE list_type IN ('portfolio', 'evaluation')"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    roles: dict[str, CoverageRole] = {}
    for ticker, raw_role in rows:
        role_text = str(raw_role)
        if role_text == "portfolio":
            roles[str(ticker).upper()] = "portfolio"
        elif role_text == "evaluation":
            roles[str(ticker).upper()] = "evaluation"
    return roles


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    roles = _coverage_roles(repo_root)
    result = reconcile_legacy_workspace_reports(
        repo_root,
        coverage_role_for=lambda ticker: roles.get(ticker, "unknown"),
    )
    print(result.model_dump_json())
    print(
        json.dumps(
            {"event": "report_artifacts_reconciled", **result.model_dump()}, sort_keys=True
        ),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
