"""execution/run_scenario.py — drive the macro-scenario lenses.

Loads a Scenario from src/macro_scenarios.py and runs either the
single-ticker `macro_scenario` lens or the portfolio-wide
`portfolio_macro_stress` lens. Output is cached via llm_artifacts so
re-running the same (scenario, ticker) is free.

CLI:
    # Single-ticker read-through
    python execution/run_scenario.py --scenario fed_cuts_50bps --ticker AMZN

    # Portfolio-wide stress digest
    python execution/run_scenario.py --scenario recession_2026 --portfolio

    # List available scenarios
    python execution/run_scenario.py --list

    # Force regeneration (bypass cache)
    python execution/run_scenario.py --scenario oil_to_50 --ticker NU --force
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from macro_scenarios import SCENARIOS, all_scenario_ids  # noqa: E402
from macro_scenarios import get as get_scenario  # noqa: E402
from synthesis_lenses import (  # noqa: E402
    run_macro_scenario_lens,
    run_portfolio_macro_stress_lens,
)

log = logging.getLogger("run_scenario")


def _sync_db_path(repo_root: Path) -> None:
    import db

    db.PROJECT_ROOT = str(repo_root)
    db.DATA_DIR = str(repo_root / "data")
    db.DB_PATH = str(repo_root / "data" / "portfolio.db")
    db.FMP_DIR = str(repo_root / "data" / "historical" / "fmp")


def _print_scenarios() -> None:
    print("Available scenarios:")
    for sid in all_scenario_ids():
        s = SCENARIOS[sid]
        print(f"  {sid:24s} {s.title}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", help="Scenario id (see --list).")
    parser.add_argument("--list", action="store_true", help="List available scenarios and exit.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--ticker", help="Single ticker to evaluate.")
    group.add_argument("--portfolio", action="store_true", help="Portfolio-wide stress digest.")
    parser.add_argument("--force", action="store_true", help="Bypass artifact cache.")
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    repo_root = args.repo_root.resolve()
    _sync_db_path(repo_root)

    if args.list:
        _print_scenarios()
        return 0
    if not args.scenario:
        print("Error: --scenario required (use --list to see options).", file=sys.stderr)
        return 2
    scenario = get_scenario(args.scenario)
    if scenario is None:
        print(f"Unknown scenario: {args.scenario}", file=sys.stderr)
        print("Use --list to see options.", file=sys.stderr)
        return 2

    if args.portfolio:
        artifact = run_portfolio_macro_stress_lens(
            scenario_obj=scenario, repo_root=repo_root, force=args.force
        )
        scope_label = "portfolio"
    else:
        if not args.ticker:
            print("Error: either --ticker or --portfolio required.", file=sys.stderr)
            return 2
        artifact = run_macro_scenario_lens(
            scenario_obj=scenario,
            ticker=args.ticker,
            repo_root=repo_root,
            force=args.force,
        )
        scope_label = args.ticker.upper()

    if artifact is None:
        print(
            f"[FAIL] {scenario.id} x {scope_label} -- no artifact produced "
            "(check sensitivities are populated, DCF run exists, and FMP/LLM keys are set).",
            file=sys.stderr,
        )
        return 1
    # Windows cp1252 stdout can't encode Unicode dashes/minus signs the LLM
    # likes to use. Bypass the codec by writing UTF-8 bytes directly.
    body = artifact.content_md or "(no content)"
    header = f"[OK] {scenario.id} x {scope_label} -- artifact id={artifact.id}\n\n"
    try:
        sys.stdout.buffer.write((header + body + "\n").encode("utf-8"))
    except AttributeError:
        print(header + body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
