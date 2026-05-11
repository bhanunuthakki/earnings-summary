"""One-shot: upgrade micro_thesis/holdings/<TICKER>.json files to schema v2.

Adds DCF-subsystem fields (wacc, mos_bar, dcf_defaults, segments,
operational_kpis), an empty break_rules_soft list, and a schema_version
marker. Existing fields (incl. break_rules) preserved untouched.

Idempotent: skips files where schema_version is already >= 2.

Per-ticker WACC / MoS / terminal-multiple seeds are baked in for the 11
portfolio + 1 watchlist names. All other holdings (index-promoted stubs)
get wacc=null, mos_bar=null, terminal_multiple=null, and a DCF_PARAMS_PENDING
flag indicating the values need user review.

Run from worktree root:

    python scratch/migrate_holdings_v2.py             # write all
    python scratch/migrate_holdings_v2.py --dry-run   # preview, no writes
    python scratch/migrate_holdings_v2.py --ticker META

Stderr: per-file action. Exits 0 on success.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
HOLDINGS_DIR = REPO_ROOT / "micro_thesis" / "holdings"

SCHEMA_VERSION = 2

# Per-user seed values — only the 11 portfolio + 1 watchlist names get explicit
# values. Other tickers get None and the DCF_PARAMS_PENDING flag.
_PORTFOLIO_SEEDS: dict[str, dict[str, float]] = {
    "META": {"wacc": 0.09, "mos_bar": 0.20, "terminal_multiple": 18.0},
    "GOOG": {"wacc": 0.09, "mos_bar": 0.20, "terminal_multiple": 18.0},
    "AMZN": {"wacc": 0.09, "mos_bar": 0.20, "terminal_multiple": 20.0},
    "BN": {"wacc": 0.09, "mos_bar": 0.20, "terminal_multiple": 15.0},
    "NU": {"wacc": 0.11, "mos_bar": 0.25, "terminal_multiple": 18.0},
    "MELI": {"wacc": 0.11, "mos_bar": 0.20, "terminal_multiple": 18.0},
    "NVO": {"wacc": 0.085, "mos_bar": 0.30, "terminal_multiple": 14.0},
    "RBRK": {"wacc": 0.12, "mos_bar": 0.30, "terminal_multiple": 22.0},
    "NOW": {"wacc": 0.095, "mos_bar": 0.20, "terminal_multiple": 22.0},
    "VEEV": {"wacc": 0.095, "mos_bar": 0.20, "terminal_multiple": 22.0},
    "WIX": {"wacc": 0.095, "mos_bar": 0.20, "terminal_multiple": 22.0},
    # Watchlist
    "LLY": {"wacc": 0.09, "mos_bar": 0.20, "terminal_multiple": 16.0},
}

_DEFAULT_FORECAST_YEARS = 5
_PENDING_FLAG = "DCF_PARAMS_PENDING"


def main() -> int:
    args = _parse_args()
    targets = _resolve_targets(args.ticker)

    upgraded = 0
    seeded_stubs = 0
    skipped_already_v2 = 0
    skipped_not_found = 0

    for path in targets:
        if not path.exists():
            sys.stderr.write(f"NOT_FOUND: {path.name}\n")
            skipped_not_found += 1
            continue
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if int(data.get("schema_version", 1)) >= SCHEMA_VERSION and not args.force:
            sys.stderr.write(f"SKIP (already v{SCHEMA_VERSION}): {path.name}\n")
            skipped_already_v2 += 1
            continue
        ticker = str(data.get("ticker", path.stem)).upper()
        upgraded_data = _upgrade(data, ticker)
        if not args.dry_run:
            # ensure_ascii=False keeps unicode literal so git diffs stay clean
            # and the files render readably in editors.
            with open(path, "w", encoding="utf-8") as f:
                json.dump(upgraded_data, f, indent=2, ensure_ascii=False)
                f.write("\n")
        is_seeded = ticker in _PORTFOLIO_SEEDS
        action = "UPGRADE" if is_seeded else "UPGRADE_STUB"
        sys.stderr.write(f"{action}: {path.name}\n")
        upgraded += 1
        if not is_seeded:
            seeded_stubs += 1

    sys.stderr.write(
        f"\nDone. upgraded={upgraded} "
        f"(of which {seeded_stubs} are stubs without WACC/MoS), "
        f"skipped_already_v2={skipped_already_v2}, "
        f"skipped_not_found={skipped_not_found}\n"
    )
    if args.dry_run:
        sys.stderr.write("(dry-run; no files written)\n")
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="Print actions without writing")
    p.add_argument("--ticker", help="Only process this ticker")
    p.add_argument(
        "--force",
        action="store_true",
        help="Re-process files already at schema_version >= 2 (e.g., to re-encode after a script fix)",
    )
    return p.parse_args()


def _resolve_targets(ticker: str | None) -> list[Path]:
    if ticker:
        return [HOLDINGS_DIR / f"{ticker.upper()}.json"]
    return sorted(HOLDINGS_DIR.glob("*.json"))


def _upgrade(data: dict[str, Any], ticker: str) -> dict[str, Any]:
    """Add v2 fields. Preserves all existing fields untouched.

    Existing `break_rules` field stays as-is — that is the hard-rules list.
    Adds `break_rules_soft` (empty by default) for predicate-evaluable soft
    rules to be authored later (Phase 4).
    """
    seed = _PORTFOLIO_SEEDS.get(ticker)
    out: dict[str, Any] = dict(data)

    out["schema_version"] = SCHEMA_VERSION
    out["last_updated"] = date.today().isoformat()

    if seed is not None:
        out["wacc"] = seed["wacc"]
        out["mos_bar"] = seed["mos_bar"]
        out["dcf_defaults"] = {
            "forecast_years": _DEFAULT_FORECAST_YEARS,
            "terminal_multiple": seed["terminal_multiple"],
        }
    else:
        out["wacc"] = None
        out["mos_bar"] = None
        out["dcf_defaults"] = {
            "forecast_years": _DEFAULT_FORECAST_YEARS,
            "terminal_multiple": None,
        }
        flags: list[str] = list(out.get("flags", []))
        if _PENDING_FLAG not in flags:
            flags.append(_PENDING_FLAG)
        out["flags"] = flags

    out.setdefault("segments", [])
    out.setdefault("operational_kpis", [])
    out.setdefault("break_rules_soft", [])

    return out


if __name__ == "__main__":
    raise SystemExit(main())
