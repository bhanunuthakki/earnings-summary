"""Report what the ``filing_sections`` store holds for a ticker.

The read-side counterpart to ``execution/ingest_filing_sections.py``. Its job
is to make the store's honesty visible: which periods have sections, from
which source, which sources are absent and why, and where the two partitions
disagree. A period backed by only one source is labeled ``SINGLE-SOURCE``
rather than being rendered identically to a two-source period, because that
distinction is what a downstream language diff has to branch on.

Structured events to stderr, human/JSON report to stdout. Exit codes: 0 on
success, 1 hard stop (missing migration), 2 bad arguments.

Usage:
    python execution/filing_sections_report.py --ticker META
    python execution/filing_sections_report.py --ticker NU --json
    python execution/filing_sections_report.py --ticker WIX --timeline risk_factors
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402
from filings import store  # noqa: E402
from filings.models import FilingForm, HardStopError  # noqa: E402

_EXIT_HARD_STOP = 1
_EXIT_BAD_ARGS = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--ticker", type=str, required=True)
    parser.add_argument("--form", type=str, default=None, help="e.g. 10-K")
    parser.add_argument(
        "--timeline",
        type=str,
        default=None,
        help="Cross-form concept (e.g. risk_factors, mdna) to list chronologically",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    parser.add_argument("--db-path", type=str, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")
    log = logging.getLogger("filing_sections_report")

    form: FilingForm | None = None
    if args.form:
        try:
            form = FilingForm(args.form.strip().upper())
        except ValueError:
            log.error("unknown form %r; valid: %s", args.form, [f.value for f in FilingForm])
            return _EXIT_BAD_ARGS

    if args.db_path:
        db.set_db_path(args.db_path)
    conn = db.get_connection()
    ticker = args.ticker.strip().upper()

    try:
        try:
            availability = store.period_availability(conn, ticker, form=form)
            timeline = (
                store.section_timeline(conn, ticker, canonical_id=args.timeline)
                if args.timeline
                else []
            )
        except HardStopError as exc:
            log.error("hard stop: %s", exc)
            return _EXIT_HARD_STOP

        if args.json:
            json.dump(
                {
                    "ticker": ticker,
                    "periods": [
                        {
                            **a.model_dump(mode="json"),
                            "is_single_source": a.is_single_source,
                        }
                        for a in availability
                    ],
                    "timeline": [
                        {
                            "fiscal_year": s.fiscal_year,
                            "fiscal_period": s.fiscal_period.value,
                            "form": s.form.value,
                            "source": s.source.value,
                            "section_key": s.section_key_raw,
                            "char_len": s.char_len,
                            "text_sha256": s.text_sha256,
                        }
                        for s in timeline
                    ],
                },
                sys.stdout,
                indent=2,
                default=str,
            )
            sys.stdout.write("\n")
            return 0

        if not availability:
            print(f"{ticker}: no filing sections ingested yet.")
            print(
                "  run: python execution/ingest_filing_sections.py --tickers "
                f"{ticker} --sources fmp,exhibits"
            )
            return 0

        print(f"{ticker} — filing section coverage ({len(availability)} periods)")
        print()
        for a in availability:
            label = f"{a.form.value} FY{a.fiscal_year} {a.fiscal_period.value}"
            counts = ", ".join(f"{k}={v}" for k, v in sorted(a.section_counts.items())) or "none"
            flag = " [SINGLE-SOURCE]" if a.is_single_source else ""
            print(f"  {label:<24} {counts}{flag}")
            for source, reason in sorted(a.absent_sources.items()):
                print(f"      absent: {source} — {reason}")
            for mismatch in a.mismatches:
                print(f"      MISMATCH: {mismatch}")

        if timeline:
            print()
            print(f"timeline — {args.timeline} ({len(timeline)} versions, oldest first)")
            prior_hash: str | None = None
            for s in timeline:
                changed = (
                    ""
                    if prior_hash is None
                    else (" changed" if s.text_sha256 != prior_hash else " unchanged")
                )
                print(
                    f"  FY{s.fiscal_year} {s.fiscal_period.value} {s.form.value} "
                    f"({s.source.value}) {s.char_len:>8,} chars{changed}"
                )
                prior_hash = s.text_sha256
        elif args.timeline:
            print()
            print(f"timeline — no sections stored with canonical_id={args.timeline!r}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
