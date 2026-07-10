"""Generate an ETF's 'role in portfolio' one-pager (etf_role_synthesis).

Thin CLI over ``etf_role_synthesis.generate_role_synthesis``: assembles the
deterministic workup payload (profile, style loadings, look-through overlap +
country rollup, what-if rows, positioning target, book context), makes ONE
governed LLM call, validates against ``EtfRoleSynthesis``, and persists to
``llm_artifacts`` sha-keyed on the payload — a rerun with unchanged inputs is
a free no-op that never touches the LLM.

Usage:
    python execution/build_etf_workup.py --ticker AVDV
    python execution/build_etf_workup.py --ticker AVDV --force
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from etf_role_synthesis import generate_role_synthesis  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ticker", required=True, help="ETF ticker (e.g. AVDV)")
    ap.add_argument(
        "--force",
        action="store_true",
        help="Regenerate even when the workup inputs haven't changed",
    )
    ap.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    ap.add_argument("--db-path", type=Path, default=None)
    args = ap.parse_args()
    ticker = args.ticker.upper()
    repo_root: Path = args.repo_root.resolve()
    db_path: Path = args.db_path or repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        print(f"[etf-workup] DB not found: {db_path}", flush=True)
        return 1

    conn = sqlite3.connect(str(db_path))
    try:
        artifact_id, status = generate_role_synthesis(
            conn, repo_root, db_path, ticker, force=args.force
        )
    finally:
        conn.close()
    print(f"[etf-workup] {ticker} status={status} artifact_id={artifact_id}", flush=True)
    return 0 if not status.startswith("error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
