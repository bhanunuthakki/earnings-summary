"""Ingest pre-collected SEDI insider transaction dumps.

For each Canadian-domiciled holding (BN, BAM, BEPC, BIPC, ENB, TRP, FNV,
HBM, IVN, TECK, WPM), drop a JSON dump from SEDI at
`data/sedi_dumps/<TICKER>_sedi.json` and run this command.

The dump format is documented in `src/sedi_adapter.py:ingest_sedi_dump`.
A working SEDI scraper / Playwright pipeline can produce these dumps
on demand; until that lands, this is the manual ingest path.

Usage:
    python execution/ingest_sedi_dumps.py            # ingest all dumps
    python execution/ingest_sedi_dumps.py --ticker BN
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402

from sedi_adapter import ingest_sedi_dump  # noqa: E402

log = logging.getLogger("ingest_sedi")

# The Canadian-domiciled holdings (anything we expect to find SEDI data for).
SEDI_TARGETS = ["BN", "BAM", "BEPC", "BIPC", "ENB", "TRP", "FNV", "HBM", "IVN", "TECK", "WPM"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ticker", default=None, help="Single ticker (default: all SEDI_TARGETS with a dump)."
    )
    parser.add_argument(
        "--dumps-dir",
        type=Path,
        default=None,
        help="Override dump directory (default: <repo>/data/sedi_dumps).",
    )
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    db.PROJECT_ROOT = str(args.repo_root)
    db.DATA_DIR = str(args.repo_root / "data")
    db.DB_PATH = str(args.repo_root / "data" / "portfolio.db")
    db.FMP_DIR = str(args.repo_root / "data" / "historical" / "fmp")

    dumps_dir = args.dumps_dir or (args.repo_root / "data" / "sedi_dumps")
    if not dumps_dir.exists():
        print(
            f"No SEDI dumps directory at {dumps_dir}. Drop SEDI JSON dumps there "
            f"named <TICKER>_sedi.json. Schema documented in src/sedi_adapter.py."
        )
        return 0

    targets = [args.ticker.upper()] if args.ticker else SEDI_TARGETS
    total = 0
    for ticker in targets:
        dump_path = dumps_dir / f"{ticker}_sedi.json"
        if not dump_path.exists():
            log.info({"event": "no_dump", "ticker": ticker, "path": str(dump_path)})
            continue
        n = ingest_sedi_dump(
            dump_path=dump_path,
            ticker=ticker,
            db_path=args.repo_root / "data" / "portfolio.db",
        )
        total += n
    print(f"\nIngested {total} SEDI rows across {len(targets)} candidate ticker(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
