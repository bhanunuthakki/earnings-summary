"""Refresh a ticker's KPI series from its IR historical-data spreadsheet.

The issuer's own quarterly spreadsheet carries clean, audited KPI series. This
ingests them at IR_DOC tier so they supersede the lower-tier LLM brief/press
values in the report (the §3 chart + break-rule loaders prefer the
latest-ingested source per period).

Modes (one required):
  --file PATH    parse + ingest a local spreadsheet
  --url URL      download the spreadsheet, then parse + ingest
  --discover     resolve the current spreadsheet URL via the IR results-center
                 browser adapter, then download + parse + ingest

Examples:
  python execution/refresh_ir_kpis.py --ticker NU --discover --quarters 8
  python execution/refresh_ir_kpis.py --ticker NU --url "https://api.mziq.com/...?origin=2"
  python execution/refresh_ir_kpis.py --ticker NU --file data/ir_spreadsheets/NU/NU_Historical_Data_1Q26.xlsx
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from ir_pipeline.config import configured_tickers, get_config  # noqa: E402
from ir_pipeline.download import download_spreadsheet  # noqa: E402
from ir_pipeline.ingest import ingest_spreadsheet_kpis  # noqa: E402
from ir_pipeline.spreadsheet import parse_spreadsheet  # noqa: E402


def main() -> int:
    args = _parse_args()
    cfg = get_config(args.ticker)
    if cfg is None:
        print(
            f"No IR config for {args.ticker!r}. Configured: {configured_tickers()}",
            file=sys.stderr,
        )
        return 2

    repo_root = args.repo_root.resolve()
    if args.file is not None:
        path = args.file
    elif args.url is not None:
        path = download_spreadsheet(args.url, repo_root, args.ticker)
    else:  # --discover
        from ir_pipeline.discover import discover_spreadsheet_url

        try:
            url = discover_spreadsheet_url(cfg)
        except NotImplementedError as e:
            print(str(e), file=sys.stderr)
            return 3
        if url is None:
            print(f"Could not discover a spreadsheet URL for {args.ticker}.", file=sys.stderr)
            return 4
        path = download_spreadsheet(url, repo_root, args.ticker)

    parsed = parse_spreadsheet(path, cfg, max_quarters=args.quarters)
    db_path = repo_root / "data" / "portfolio.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        inserted, doc_id = ingest_spreadsheet_kpis(conn, args.ticker, cfg, parsed, path)
    finally:
        conn.close()

    print(
        json.dumps(
            {
                "ticker": args.ticker.upper(),
                "source_file": str(path),
                "doc_id": doc_id,
                "rows_inserted": inserted,
                "kpis": {k: len(v) for k, v in parsed.items()},
            },
            indent=2,
        )
    )
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--ticker", required=True, help="Ticker with an IR config (e.g. NU)")
    p.add_argument("--quarters", type=int, default=8, help="How many recent quarters to ingest")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", type=Path, help="Local spreadsheet to parse + ingest")
    src.add_argument("--url", help="Spreadsheet URL to download, then parse + ingest")
    src.add_argument(
        "--discover",
        action="store_true",
        help="Discover the current spreadsheet URL via the IR results-center browser adapter",
    )
    p.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
