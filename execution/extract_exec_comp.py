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
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import db  # noqa: E402

from exec_comp_store import ExecCompPackage, PerformanceMetric, upsert_package  # noqa: E402
from filing_text_fetcher import fetch_latest_def14a_text  # noqa: E402
from llm_client import JSON_FENCE_RE, call_llm  # noqa: E402

log = logging.getLogger("extract_exec_comp")

# DEF 14A texts are ~150KB-500KB. Cap input for the prompt; the comp tables
# we care about typically appear in the first 60-80% of the doc.
_MAX_PROXY_CHARS = 180_000


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
    prompt = _PROMPT.format(ticker=ticker, fiscal_year=result.fiscal_year, proxy_text=proxy_text)

    try:
        raw = call_llm(
            prompt,
            purpose="exec_comp_extraction",
            ticker=ticker,
            model="claude-opus-4-7",
        ).strip()
    except Exception as exc:  # noqa: BLE001
        log.warning({"event": "exec_comp_llm_failed", "ticker": ticker, "error": str(exc)})
        return {"ticker": ticker, "status": "llm_failed", "n": 0}

    if raw.startswith("```"):
        raw = JSON_FENCE_RE.sub("", raw).strip()
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        log.warning({"event": "exec_comp_parse_failed", "ticker": ticker, "head": raw[:200]})
        return {"ticker": ticker, "status": "parse_failed", "n": 0}
    if not isinstance(decoded, dict):
        return {"ticker": ticker, "status": "not_an_object", "n": 0}

    d = cast("dict[str, object]", decoded)
    execs_raw = d.get("executives")
    if not isinstance(execs_raw, list):
        return {"ticker": ticker, "status": "no_executives", "n": 0}

    peer_group = d.get("peer_group") if isinstance(d.get("peer_group"), list) else []
    peer_group_list = [str(p) for p in cast("list[object]", peer_group) if isinstance(p, str)]
    cic_terms = d.get("cic_terms") if isinstance(d.get("cic_terms"), dict) else {}
    hedging = d.get("hedging_pledging_policy")
    hedging_str = str(hedging) if isinstance(hedging, str) else None
    ceo_pay_ratio = d.get("ceo_pay_ratio")
    ceo_pay_ratio_float = (
        float(ceo_pay_ratio)
        if isinstance(ceo_pay_ratio, (int, float)) and not isinstance(ceo_pay_ratio, bool)
        else None
    )

    db_path = repo_root / "data" / "portfolio.db"
    inserted = 0
    for exec_obj in execs_raw:
        if not isinstance(exec_obj, dict):
            continue
        e = cast("dict[str, object]", exec_obj)
        name = e.get("executive_name")
        if not isinstance(name, str) or not name.strip():
            continue
        metrics: list[PerformanceMetric] = []
        metrics_raw = e.get("performance_metrics")
        if isinstance(metrics_raw, list):
            for m in metrics_raw:
                if not isinstance(m, dict):
                    continue
                md = cast("dict[str, object]", m)
                metric_name = md.get("metric")
                weight = md.get("weight")
                if not isinstance(metric_name, str):
                    continue
                metrics.append(
                    PerformanceMetric(
                        metric=metric_name,
                        weight=float(weight) if isinstance(weight, (int, float)) else 0.0,
                        threshold=_f(md.get("threshold")),
                        target=_f(md.get("target")),
                        max=_f(md.get("max")),
                        actual=_f(md.get("actual")),
                        unit=str(md.get("unit", "")) if md.get("unit") else None,
                    )
                )
        pkg = ExecCompPackage(
            ticker=ticker,
            fiscal_year=result.fiscal_year,
            executive_name=name.strip(),
            role=str(e.get("role") or "").strip() or None,
            is_ceo=bool(e.get("is_ceo")),
            currency=str(e.get("currency") or "USD"),
            base_salary=_f(e.get("base_salary")),
            cash_bonus_target=_f(e.get("cash_bonus_target")),
            cash_bonus_actual=_f(e.get("cash_bonus_actual")),
            equity_grant_value=_f(e.get("equity_grant_value")),
            equity_grant_breakdown=cast("dict[str, object]", e.get("equity_grant_breakdown") or {}),
            other_comp=_f(e.get("other_comp")),
            total_comp_granted=_f(e.get("total_comp_granted")),
            total_comp_realized=_f(e.get("total_comp_realized")),
            performance_metrics=metrics,
            peer_group=peer_group_list,
            cic_terms=cast("dict[str, object]", cic_terms),
            hedging_pledging_policy=hedging_str,
            ceo_pay_ratio=ceo_pay_ratio_float,
            source_excerpt=None,
        )
        if upsert_package(pkg, db_path=db_path) is not None:
            inserted += 1

    return {
        "ticker": ticker,
        "fiscal_year": result.fiscal_year,
        "n_executives": len(execs_raw),
        "n_inserted": inserted,
        "status": "ok",
    }


def _f(v: object) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None


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
        conn = sqlite3.connect(str(args.repo_root / "data" / "portfolio.db"))
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
        total += int(outcome.get("n_inserted", 0))
    print(f"\nExec comp extraction done · {total} package rows across {len(tickers)} ticker(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
