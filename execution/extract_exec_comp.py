"""Extract executive compensation packages from DEF 14A proxy filings.

DEF 14A (proxy statement) contains the high-signal compensation
disclosures: NEO summary comp table, grants of plan-based awards,
performance metrics + targets, peer group, CIC terms, hedging policy,
CEO pay ratio.

Pipeline:
  1. fetch_latest_def14a_text(ticker) — pulls + caches proxy text from SEC.
  2. Single Opus call with the full text and a structured-output schema.
  3. Parse JSON response into ExecCompPackage rows + write to exec_comp_packages.

Opus is the right model here — proxies are dense, multi-table, with
performance-metric structures that need careful preservation. A single
proxy run costs ~$0.50-1.50.

Usage:
    python execution/extract_exec_comp.py --ticker GOOG
    python execution/extract_exec_comp.py --all-portfolio
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import cast

from pydantic import BaseModel, Field, TypeAdapter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402
from exec_comp_store import ExecCompPackage, PerformanceMetric, upsert_package  # noqa: E402
from filing_text_fetcher import fetch_latest_def14a_text  # noqa: E402
from llm.structured import StructuredParseError, call_llm_structured  # noqa: E402
from llm.untrusted import spotlight  # noqa: E402
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite  # noqa: E402

log = logging.getLogger("extract_exec_comp")

# DEF 14A texts are ~150KB-500KB. Cap input for the prompt; the comp tables
# we care about typically appear in the first 60-80% of the doc.
_MAX_PROXY_CHARS = 180_000


class _MetricExtraction(BaseModel):
    metric: str
    weight: float | None = None
    threshold: float | None = None
    target: float | None = None
    max: float | None = None
    actual: float | None = None
    unit: str | None = None


class _ExecutiveExtraction(BaseModel):
    executive_name: str
    role: str | None = None
    is_ceo: bool = False
    currency: str | None = None
    base_salary: float | None = None
    cash_bonus_target: float | None = None
    cash_bonus_actual: float | None = None
    equity_grant_value: float | None = None
    equity_grant_breakdown: dict[str, float | None] = Field(default_factory=dict)
    other_comp: float | None = None
    total_comp_granted: float | None = None
    total_comp_realized: float | None = None
    performance_metrics: list[_MetricExtraction] = Field(default_factory=list[_MetricExtraction])


class _ExecCompExtraction(BaseModel):
    executives: list[_ExecutiveExtraction]
    peer_group: list[str] = Field(default_factory=list[str])
    cic_terms: dict[str, object] = Field(default_factory=dict[str, object])
    hedging_pledging_policy: str | None = None
    ceo_pay_ratio: float | None = None


_PROMPT = """You are extracting executive compensation disclosures from {ticker}'s
fiscal year {fiscal_year} DEF 14A proxy statement.

Find the Summary Compensation Table, Grants of Plan-Based Awards table,
Outstanding Equity Awards table, the CD&A narrative, the Peer Group, CIC
terms, and the CEO Pay Ratio. Emit a JSON object describing the NEO
compensation packages for this fiscal year.

OUTPUT SCHEMA (strict JSON, no commentary):
{{
  "executives": [
    {{
      "executive_name": "<full name>",
      "role": "Chief Executive Officer" | "CFO" | "COO" | ...,
      "is_ceo": true/false,
      "currency": "USD",
      "base_salary": <number, in dollars>,
      "cash_bonus_target": <number, dollars>,
      "cash_bonus_actual": <number, dollars>,
      "equity_grant_value": <number, dollars at grant-date fair value>,
      "equity_grant_breakdown": {{
        "rsu_value": <number>,
        "psu_value": <number>,
        "options_value": <number>,
        "psu_target_shares": <number>
      }},
      "other_comp": <number>,
      "total_comp_granted": <number, "Total" column>,
      "total_comp_realized": <number, when separately disclosed (Realized Pay table)>,
      "performance_metrics": [
        {{
          "metric": "Revenue growth" | "Cloud OI margin" | "TSR vs S&P 100" | ...,
          "weight": <0..1>,
          "threshold": <number, payout threshold>,
          "target": <number, target>,
          "max": <number, max payout level>,
          "actual": <number, achieved when disclosed>,
          "unit": "pct" | "usd_m" | "ratio" | ...
        }}
      ]
    }}
  ],
  "peer_group": ["<list of peer company names or tickers used for comp benchmarking>"],
  "cic_terms": {{
    "ceo_cash_severance_multiple": <number e.g. 2.0>,
    "ceo_equity_vesting_treatment": "<one-line description>",
    "single_trigger": true/false,
    "double_trigger": true/false
  }},
  "hedging_pledging_policy": "<one-line summary>",
  "ceo_pay_ratio": <number, CEO comp / median employee comp>
}}

Rules:
  - Return ONLY the JSON object. No markdown fence. No commentary.
  - NEO list typically includes CEO, CFO, and 3 other top officers (5 total).
  - If a number isn't disclosed, use null (not 0).
  - performance_metrics may be empty if the filing only discloses cash bonus
    + time-vesting equity (no performance plan).
  - PRIORITIZE accuracy on: total_comp_granted, performance_metrics targets,
    peer_group, and ceo_pay_ratio. These drive the alignment lens downstream.

INPUT (DEF 14A text, may be truncated):
{proxy_text}
"""


def extract_for_ticker(
    *,
    ticker: str,
    repo_root: Path,
    user_agent: str,
    fiscal_year: int | None = None,
) -> dict[str, object]:
    cache_dir = repo_root / "data" / "sec_text"
    result = fetch_latest_def14a_text(
        ticker=ticker, user_agent=user_agent, cache_dir=cache_dir, fiscal_year=fiscal_year
    )
    if result is None or not result.text:
        return {"ticker": ticker, "status": "no_def14a", "n": 0}

    proxy_text = result.text[:_MAX_PROXY_CHARS]
    prompt = _PROMPT.format(
        ticker=ticker,
        fiscal_year=result.fiscal_year,
        proxy_text=spotlight(proxy_text, source="SEC DEF 14A proxy filing text"),
    )

    try:
        validated = cast(
            "_ExecCompExtraction",
            call_llm_structured(
                prompt,
                purpose="exec_comp_extraction",
                ticker=ticker,
                scope="executive_compensation",
                schema=TypeAdapter(_ExecCompExtraction),
            ),
        )
    except StructuredParseError as exc:
        log.warning({"event": "exec_comp_structured_failed", "ticker": ticker, "error": str(exc)})
        return {"ticker": ticker, "status": "structured_failed", "n": 0}
    except Exception as exc:
        log.warning({"event": "exec_comp_llm_failed", "ticker": ticker, "error": str(exc)})
        return {"ticker": ticker, "status": "llm_failed", "n": 0}
    db_path = repo_root / "data" / "portfolio.db"
    inserted = 0
    for executive in validated.executives:
        if not executive.executive_name.strip():
            continue
        metrics = [
            PerformanceMetric(
                metric=metric.metric,
                weight=metric.weight or 0.0,
                threshold=metric.threshold,
                target=metric.target,
                max=metric.max,
                actual=metric.actual,
                unit=metric.unit,
            )
            for metric in executive.performance_metrics
        ]
        pkg = ExecCompPackage(
            ticker=ticker,
            fiscal_year=result.fiscal_year,
            executive_name=executive.executive_name.strip(),
            role=executive.role.strip() if executive.role else None,
            is_ceo=executive.is_ceo,
            currency=executive.currency or "USD",
            base_salary=executive.base_salary,
            cash_bonus_target=executive.cash_bonus_target,
            cash_bonus_actual=executive.cash_bonus_actual,
            equity_grant_value=executive.equity_grant_value,
            equity_grant_breakdown=cast("dict[str, object]", executive.equity_grant_breakdown),
            other_comp=executive.other_comp,
            total_comp_granted=executive.total_comp_granted,
            total_comp_realized=executive.total_comp_realized,
            performance_metrics=metrics,
            peer_group=validated.peer_group,
            cic_terms=validated.cic_terms,
            hedging_pledging_policy=validated.hedging_pledging_policy,
            ceo_pay_ratio=validated.ceo_pay_ratio,
            source_excerpt=None,
        )
        if upsert_package(pkg, db_path=db_path) is not None:
            inserted += 1

    return {
        "ticker": ticker,
        "fiscal_year": result.fiscal_year,
        "n_executives": len(validated.executives),
        "n_inserted": inserted,
        "status": "ok",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--ticker", help="Single ticker.")
    g.add_argument("--all-portfolio", action="store_true", help="Run for portfolio holdings.")
    parser.add_argument("--fiscal-year", type=int, default=None)
    parser.add_argument("--repo-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument(
        "--user-agent",
        default=os.environ.get(
            "EDGAR_USER_AGENT", "earnings-summary research/0.1 (analyst@example.com)"
        ),
        help="SEC EDGAR User-Agent. Set EDGAR_USER_AGENT in env (recommended).",
    )
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

    if args.ticker:
        tickers = [args.ticker.upper()]
    else:
        conn = connect_sqlite(
            str(args.repo_root / "data" / "portfolio.db"), role=SQLiteConnectionRole.READ_ONLY
        )
        tickers = [
            r[0]
            for r in conn.execute(
                "SELECT ticker FROM tracked_companies WHERE archived_at IS NULL AND list_type = 'portfolio' ORDER BY ticker"
            )
        ]
        conn.close()

    total = 0
    for t in tickers:
        outcome = extract_for_ticker(
            ticker=t,
            repo_root=args.repo_root,
            user_agent=args.user_agent,
            fiscal_year=args.fiscal_year,
        )
        log.info(outcome)
        n_inserted = outcome.get("n_inserted", 0)
        if isinstance(n_inserted, int) and not isinstance(n_inserted, bool):
            total += n_inserted
    print(f"\nExec comp extraction done · {total} package rows across {len(tickers)} ticker(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
