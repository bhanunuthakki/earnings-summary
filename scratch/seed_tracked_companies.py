"""One-shot seed: register 87 tickers into tracked_companies after data wipe.

Bypasses db.track_company() (which spawns onboard_ticker.py per insert) to avoid
fanning out 87 detached FMP-fetch subprocesses. Instead does raw INSERTs with
the right list_type per the user's classification:

  - portfolio (11): AMZN, GOOG, META, MELI, NU, NVO, NOW, WIX, RBRK, VEEV, BN
  - evaluation (15): ABNB, BHP, BKNG, CGEH, DLO, FCX, FIGR, NTDOY, NTRA, SOFI,
                     TEM, TMO, UBER, WGS, LLY
  - watchlist (61): the remaining holdings/ JSONs

Name comes from micro_thesis/holdings/<T>.json. fiscal_year_end and
instrument_type stay NULL — they get populated by the FMP-doc-index pass after
save_fmp_data.py writes profile.json files. sec_validated stays 0 — gets set
by SEC XBRL pass if the ticker is in CIK_MAP.

Idempotent: re-running upserts on (user_id, ticker).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOLDINGS_DIR = PROJECT_ROOT / "micro_thesis" / "holdings"
DB_PATH = PROJECT_ROOT / "data" / "portfolio.db"

PORTFOLIO: set[str] = {
    "AMZN", "GOOG", "META", "MELI", "NU", "NVO", "NOW", "WIX",
    "RBRK", "VEEV", "BN",
}
EVALUATION: set[str] = {
    "ABNB", "BHP", "BKNG", "CGEH", "DLO", "FCX", "FIGR", "NTDOY",
    "NTRA", "SOFI", "TEM", "TMO", "UBER", "WGS", "LLY",
}


def _classify(ticker: str) -> str:
    if ticker in PORTFOLIO:
        return "portfolio"
    if ticker in EVALUATION:
        return "evaluation"
    return "watchlist"


def main() -> int:
    if not HOLDINGS_DIR.is_dir():
        print(f"FATAL: holdings dir not found at {HOLDINGS_DIR}", file=sys.stderr)
        return 1

    rows: list[tuple[str, str, str]] = []
    for path in sorted(HOLDINGS_DIR.glob("*.json")):
        ticker = path.stem.upper()
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"  SKIP {ticker}: bad JSON ({e})", file=sys.stderr)
            continue
        name = (blob.get("name") or ticker).strip()
        list_type = _classify(ticker)
        rows.append((ticker, name, list_type))

    counts: dict[str, int] = {"portfolio": 0, "evaluation": 0, "watchlist": 0}
    for _, _, lt in rows:
        counts[lt] += 1
    print(f"Plan: {len(rows)} tickers — {counts}")

    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        now = datetime.now()
        cur.executemany(
            """
            INSERT INTO tracked_companies
                (user_id, ticker, name, list_type, added_at,
                 sec_validated, brief_dirty, fmp_data_saved)
            VALUES (1, ?, ?, ?, ?, 0, 0, 0)
            ON CONFLICT(user_id, ticker) DO UPDATE SET
                list_type = excluded.list_type,
                name      = excluded.name,
                archived_at = NULL
            """,
            [(t, n, lt, now) for (t, n, lt) in rows],
        )
        conn.commit()
        cur.execute(
            "SELECT list_type, COUNT(*) FROM tracked_companies GROUP BY list_type ORDER BY list_type"
        )
        print("Result by list_type:")
        for lt, n in cur.fetchall():
            print(f"  {lt}: {n}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
