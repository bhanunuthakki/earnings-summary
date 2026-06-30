"""Phase-0 seed step: load the owner's seed corpus into The Ledger and report.

This is the consumer the seed PR was built for. It loads
data/ledger_seed/seed.json into typed Ledger objects, validates the shape, and
(optionally) runs the coaching lens for a name so you can see the thought-partner
replay the owner's own words back in context.

Usage:
  python -m execution.seed_themes
  python -m execution.seed_themes --advise NVDA --action sell
  python -m execution.seed_themes --advise MELI --action buy
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ledger.coach import LedgerCoach
from ledger.models import Ledger

DEFAULT_SEED = Path(__file__).resolve().parents[1] / "data" / "ledger_seed" / "seed.json"


def _print_coaching(coach: LedgerCoach, ticker: str, action: str | None) -> None:
    c = coach.advise(ticker, action)
    print(f"\nCoaching for {c.ticker} (action={c.action or 'n/a'}):")
    if c.is_empty():
        print("  (no seeded context for this name yet)")
        return
    for theme in c.themes:
        print(f"  theme    : {theme.title}")
    for musing in c.ticker_musings:
        print(f"  musing   : {musing.body[:140]}")
    for flag in c.behavioral_flags:
        print(f"  PATTERN! : {flag.body[:140]}")
    for falsifier in c.falsifiers:
        print(f"  falsifier: {falsifier[:140]}")
    for note in c.notes:
        print(f"  note     : {note}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load and inspect The Ledger seed.")
    parser.add_argument("--seed", default=str(DEFAULT_SEED), help="path to seed.json")
    parser.add_argument("--advise", help="ticker to run the coaching lens on")
    parser.add_argument("--action", help="contemplated action (buy/add/trim/sell/...)")
    args = parser.parse_args(argv)

    ledger = Ledger.from_seed(args.seed)
    print(
        f"The Ledger seed loaded (as_of {ledger.as_of}): "
        f"{len(ledger.decisions)} decisions, "
        f"{len(ledger.musings)} musings, "
        f"{len(ledger.themes)} themes."
    )
    print("\nThemes:")
    for theme in ledger.themes:
        print(f"  - {theme.slug:38s} {', '.join(theme.tickers)}")

    if args.advise:
        _print_coaching(LedgerCoach(ledger), args.advise, args.action)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
