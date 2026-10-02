"""Read persisted valuation evidence without fetching data or changing state.

Run via execution/sqlite_bootstrap.py with an explicit --db-path and --ticker.
A blocked receipt exits 2; unavailable authority exits 3. Neither is permission
for a provider retry, an assumption change, a model promotion, or a trade.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from db_paths import require_db_path
from dcf.readiness import load_valuation_readiness
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path)
    parser.add_argument("--ticker", required=True)
    args = parser.parse_args(argv)
    try:
        db_path = require_db_path(args.db_path)
    except (RuntimeError, FileNotFoundError):
        print(
            json.dumps({"status": "unavailable", "reason_code": "database_authority_unavailable"})
        )
        return 3
    try:
        conn = connect_sqlite(db_path, role=SQLiteConnectionRole.READ_ONLY)
    except (sqlite3.Error, OSError, RuntimeError):
        print(json.dumps({"status": "unavailable", "reason_code": "database_open_failed"}))
        return 3
    try:
        receipt = load_valuation_readiness(conn, args.ticker, as_of=datetime.now(UTC))
    finally:
        conn.close()
    print(json.dumps(receipt.model_dump(mode="json"), indent=2, sort_keys=True))
    return 0 if receipt.ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
