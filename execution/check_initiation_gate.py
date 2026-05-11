"""Check the initiation gate for a candidate ticker.

P2 (new-name evaluation) ends with a go/no-go decision. This CLI evaluates
three gates from the design (Q13b):

  1. DCF margin-of-safety — over_under_pct < -mos_bar
                            (per-ticker mos_bar in holdings JSON; system default 0.20)
  2. Thesis pressure-tested — at least one pressure-test JSON exists for the
                              ticker in .tmp/pressure_tests/
  3. Risk-factor scan complete — diligence markdown exists with the §4 risk
                                 excerpt populated

Exits 0 with GO when all three pass; 1 with NO-GO + per-gate reasons otherwise.

Usage:
    python execution/check_initiation_gate.py --ticker AMD
    python execution/check_initiation_gate.py --ticker AMD --mos-default 0.25
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_MOS_BAR = 0.20  # 20% — matches the design's portfolio default
PRESSURE_TEST_MAX_AGE_DAYS = 90  # pressure-test stale after a quarter


def main() -> int:
    args = _parse_args()
    repo_root = args.repo_root.resolve()
    ticker = args.ticker.upper()

    holdings = _load_holdings(repo_root, ticker)
    mos_bar = _mos_bar(holdings, args.mos_default)

    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        sys.stderr.write(f"FATAL: no DB at {db_path}\n")
        return 2

    gates: list[dict[str, object]] = [
        _gate_dcf(ticker, db_path, mos_bar),
        _gate_pressure_test(repo_root, ticker),
        _gate_diligence(repo_root, ticker),
    ]
    overall_pass = all(g["passed"] for g in gates)
    overall = {
        "ticker": ticker,
        "decision": "GO" if overall_pass else "NO-GO",
        "mos_bar_used": mos_bar,
        "gates": gates,
    }
    print(json.dumps(overall, indent=2))
    return 0 if overall_pass else 1


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--ticker", required=True, help="Candidate ticker to evaluate")
    p.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/, micro_thesis/. Default: this repo.",
    )
    p.add_argument(
        "--mos-default",
        type=float,
        default=DEFAULT_MOS_BAR,
        help=(
            f"Margin-of-safety bar to use when the holdings JSON doesn't supply one. "
            f"Decimal (0.20 = 20%%). Default {DEFAULT_MOS_BAR}."
        ),
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def _gate_dcf(ticker: str, db_path: Path, mos_bar: float) -> dict[str, object]:
    """Pass when the most recent dcf_runs row shows over_under_pct < -mos_bar."""
    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT over_under_pct, valuation_date, live_price, npv_per_share "
            "FROM dcf_runs WHERE ticker = ? LIMIT 1",
            (ticker,),
        )
        row = cur.fetchone()
    if row is None:
        return {
            "name": "dcf_mos",
            "passed": False,
            "reason": (
                "No dcf_runs row for this ticker. "
                f"Run: python execution/refresh_dcf.py --ticker {ticker}"
            ),
        }
    over_under = row[0]
    if over_under is None:
        return {
            "name": "dcf_mos",
            "passed": False,
            "reason": (
                "dcf_runs row exists but over_under_pct is NULL. "
                "Likely no live price was available — confirm FMP profile.json is fetched."
            ),
        }
    passed = over_under < -mos_bar
    return {
        "name": "dcf_mos",
        "passed": passed,
        "over_under_pct": over_under,
        "mos_bar": mos_bar,
        "valuation_date": row[1],
        "reason": (f"over_under {over_under * 100:+.1f}% (threshold: < {-mos_bar * 100:.1f}%)"),
    }


def _gate_pressure_test(repo_root: Path, ticker: str) -> dict[str, object]:
    """Pass when at least one recent pressure-test JSON exists for the ticker."""
    audit_dir = repo_root / ".tmp" / "pressure_tests"
    if not audit_dir.exists():
        return {
            "name": "pressure_test",
            "passed": False,
            "reason": (
                "No .tmp/pressure_tests/ directory. "
                f"Run: python execution/pressure_test_thesis.py --ticker {ticker}"
            ),
        }
    matches = sorted(audit_dir.glob(f"{ticker}_*.json"), reverse=True)
    if not matches:
        return {
            "name": "pressure_test",
            "passed": False,
            "reason": (
                f"No pressure-test JSON found for {ticker}. "
                f"Run: python execution/pressure_test_thesis.py --ticker {ticker}"
            ),
        }
    latest = matches[0]
    age_days = _age_days(latest)
    if age_days > PRESSURE_TEST_MAX_AGE_DAYS:
        return {
            "name": "pressure_test",
            "passed": False,
            "latest_path": str(latest),
            "age_days": age_days,
            "reason": (
                f"Latest pressure-test is {age_days}d old (max {PRESSURE_TEST_MAX_AGE_DAYS}d). "
                f"Re-run: python execution/pressure_test_thesis.py --ticker {ticker}"
            ),
        }
    return {
        "name": "pressure_test",
        "passed": True,
        "latest_path": str(latest),
        "age_days": age_days,
        "reason": f"Latest pressure-test {age_days}d old",
    }


def _gate_diligence(repo_root: Path, ticker: str) -> dict[str, object]:
    """Pass when a diligence markdown exists with the §4 risk excerpt populated."""
    path = repo_root / "micro_thesis" / "diligence" / f"{ticker}.md"
    if not path.exists():
        return {
            "name": "diligence_scan",
            "passed": False,
            "reason": (
                f"No diligence file at {path.relative_to(repo_root)}. "
                f"Run: python execution/build_diligence.py --ticker {ticker}"
            ),
        }
    text = path.read_text(encoding="utf-8")
    # Heuristic: passes if the file has the §4 section AND the section isn't
    # the "no risk_factors field found" stub
    has_section = "§4 Key risks" in text
    has_content = "No `risk_factors` field found" not in text
    passed = has_section and has_content
    return {
        "name": "diligence_scan",
        "passed": passed,
        "path": str(path),
        "reason": (
            "Diligence §4 risk excerpt populated"
            if passed
            else (
                "Diligence file exists but §4 risk excerpt is empty/stub. "
                "Ensure the latest 10-K is fetched (FMP form_10k_<year>.json with risk_factors field)."
            )
        ),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_holdings(repo_root: Path, ticker: str) -> dict[str, object] | None:
    path = repo_root / "micro_thesis" / "holdings" / f"{ticker}.json"
    if not path.exists():
        return None
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return cast("dict[str, object]", data)


def _mos_bar(holdings: dict[str, object] | None, default: float) -> float:
    if holdings is None:
        return default
    raw = holdings.get("mos_bar")
    if isinstance(raw, (int, float)):
        return float(raw)
    return default


def _age_days(path: Path) -> int:
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    return (datetime.now(UTC) - mtime).days


if __name__ == "__main__":
    raise SystemExit(main())
