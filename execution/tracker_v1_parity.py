"""execution/tracker_v1_parity.py — dual-read parity harness for the v1 cutover.

Consolidation PRD §12 Phase 2 / §14: before ``PORTFOLIO_TRACKER_V1_READS``
defaults ON, every facade in ``integrations.portfolio_tracker_client`` must
read the same book through BOTH transports (legacy ``/api/portfolio/*`` and
typed ``/api/v1``) and agree within tolerance. This script performs that
dual read against a RUNNING tracker and emits one sanitized JSON report.

Sanitization contract (PRD §14): the report contains ONLY counts, set
symmetric-difference SIZES, as-of dates, booleans, availability reasons'
CLASS (never full error text), and RELATIVE deltas rounded to 6dp. Never an
absolute dollar value, balance, account name, or ticker symbol.

Output: one JSON object on stdout; structured JSON-line events on stderr
(repo Layer-3 rule: logs never mix with data). Read-only — no DB writes.

Exit codes: 0 = every compared section passes; 2 = at least one section
fails tolerance; 3 = both transports unavailable (nothing comparable).

CLI:
    python execution/tracker_v1_parity.py
    python execution/tracker_v1_parity.py --api-url http://127.0.0.1:8000 \
        --start-date 2026-01-01 --end-date 2026-07-24
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from integrations import portfolio_tracker_client as tc  # noqa: E402

# Counts and dates must match exactly. Value comparisons use a RELATIVE
# tolerance: both transports read the same server engine, so numbers are
# identical-by-construction up to float/Decimal representation — 1e-6
# relative absorbs that representation noise and nothing else. Units are
# compared like-for-like (percent stays percent, fraction stays fraction;
# both transports preserve the legacy dataclass conventions) so no
# percent-vs-fraction normalization is required or performed.
REL_TOL = 1e-6


def _log(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}), file=sys.stderr)


def _rel_delta(a: float | None, b: float | None) -> float | None:
    """Relative delta of two floats; ``None`` when either side is absent
    (presence mismatches are reported separately as booleans)."""
    if a is None or b is None:
        return None
    denom = max(abs(a), abs(b), 1e-12)
    return round(abs(a - b) / denom, 6)


def _within(delta: float | None) -> bool:
    return delta is None or delta <= REL_TOL


@dataclass
class SectionResult:
    section: str
    status: str  # pass | fail | unavailable_legacy | unavailable_v1 | unavailable_both
    details: dict[str, object] = field(default_factory=dict[str, object])

    def as_json(self) -> dict[str, object]:
        return {"section": self.section, "status": self.status, "details": self.details}


def _availability_status(legacy_ok: bool, v1_ok: bool) -> str | None:
    """The section status when at least one transport failed, else None."""
    if not legacy_ok and not v1_ok:
        return "unavailable_both"
    if not legacy_ok:
        return "unavailable_legacy"
    if not v1_ok:
        return "unavailable_v1"
    return None


# percent_of_portfolio: v1 serves it rounded to 4dp, so compare in absolute
# percentage POINTS (max representation error 0.00005pp), not relative terms
# — a relative delta explodes on tiny positions purely from that rounding.
PCT_ABS_TOL_PP = 0.001


def compare_live(legacy: tc.LivePortfolio, v1: tc.LivePortfolio) -> SectionResult:
    """Positions/tax-bucket/transaction-tail parity for fetch_live_portfolio.

    ``bucket_rel_deltas`` are INFORMATIONAL, not gating: lot-level tax
    classification legitimately differs by design — the v1 transport carries
    the server's ratified five-way mapping (owner-confirmed name heuristics,
    ``positions-v1.md`` SC-1) while the legacy transport re-infers treatment
    client-side and is known to misclassify (e.g. a bare "BrokerageLink"
    matches its "brokerage" name rule -> taxable, where the ratified mapping
    says pretax). A bucket delta is the v1 CORRECTION being visible, so it
    is reported for the cutover note but does not fail parity. Position
    counts, tickers, totals, and percents still gate."""
    status = _availability_status(legacy.available, v1.available)
    if status is not None:
        return SectionResult("live_portfolio", status)
    legacy_tickers = {p.ticker for p in legacy.positions if p.ticker}
    v1_tickers = {p.ticker for p in v1.positions if p.ticker}
    total_delta = _rel_delta(legacy.total_market_value, v1.total_market_value)
    bucket_deltas = {
        bucket: _rel_delta(legacy.by_tax_treatment.get(bucket), v1.by_tax_treatment.get(bucket))
        for bucket in tc.TAX_BUCKETS
    }
    pct_deltas: list[float] = []
    v1_by_ticker = {p.ticker: p for p in v1.positions if p.ticker}
    for p in legacy.positions:
        match = v1_by_ticker.get(p.ticker) if p.ticker else None
        if match is None or p.percent_of_portfolio is None or match.percent_of_portfolio is None:
            continue
        pct_deltas.append(round(abs(p.percent_of_portfolio - match.percent_of_portfolio), 6))
    max_pct_delta = max(pct_deltas) if pct_deltas else None
    details: dict[str, object] = {
        "legacy_positions": len(legacy.positions),
        "v1_positions": len(v1.positions),
        "ticker_set_diff": len(legacy_tickers ^ v1_tickers),
        "total_value_rel_delta": total_delta,
        "bucket_rel_deltas_informational": bucket_deltas,
        "max_percent_abs_delta_pp": max_pct_delta,
        "v1_as_of": v1.as_of,
        "v1_is_stale": v1.is_stale,
        "v1_is_partial": v1.is_partial,
    }
    ok = (
        len(legacy.positions) == len(v1.positions)
        and len(legacy_tickers ^ v1_tickers) == 0
        and _within(total_delta)
        and (max_pct_delta is None or max_pct_delta <= PCT_ABS_TOL_PP)
    )
    return SectionResult("live_portfolio", "pass" if ok else "fail", details)


def compare_history(
    legacy: list[tc.LiveTransaction] | None, v1: list[tc.LiveTransaction] | None
) -> SectionResult:
    """Transaction-history parity. The v1 read may be a strict SUPERSET: the
    legacy endpoint caps a call at 5,000 rows while v1 cursor pagination has
    no cap — more v1 rows than legacy rows is expected, fewer is a failure."""
    status = _availability_status(legacy is not None, v1 is not None)
    if status is not None:
        return SectionResult("transaction_history", status)
    assert legacy is not None and v1 is not None
    details: dict[str, object] = {
        "legacy_count": len(legacy),
        "v1_count": len(v1),
        "counts_equal": len(legacy) == len(v1),
        "v1_superset_allowed": len(v1) > len(legacy),
    }
    ok = len(v1) >= len(legacy)
    return SectionResult("transaction_history", "pass" if ok else "fail", details)


def compare_analytics(
    legacy: tc.PortfolioAnalytics, v1: tc.PortfolioAnalytics
) -> list[SectionResult]:
    """Per-section analytics parity: presence + a representative scalar per
    section (returns/ratios only — never dollar levels)."""
    results: list[SectionResult] = []

    def section(
        name: str, legacy_obj: object, v1_obj: object, deltas: dict[str, float | None]
    ) -> None:
        status = _availability_status(legacy_obj is not None, v1_obj is not None)
        if status is not None:
            results.append(SectionResult(f"analytics.{name}", status))
            return
        details: dict[str, object] = dict(deltas)
        ok = all(_within(d) for d in deltas.values())
        results.append(SectionResult(f"analytics.{name}", "pass" if ok else "fail", details))

    lp, vp = legacy.performance, v1.performance
    perf_deltas: dict[str, float | None] = {}
    if lp is not None and vp is not None:
        perf_deltas["points_count_delta"] = float(abs(len(lp.points) - len(vp.points)))
        last_l = lp.points[-1].portfolio_return_pct if lp.points else None
        last_v = vp.points[-1].portfolio_return_pct if vp.points else None
        perf_deltas["final_return_rel_delta"] = _rel_delta(last_l, last_v)
    section("performance", lp, vp, perf_deltas)

    la, va = legacy.position_alpha, v1.position_alpha
    alpha_deltas: dict[str, float | None] = {}
    if la is not None and va is not None:
        alpha_deltas["rows_count_delta"] = float(abs(len(la.rows) - len(va.rows)))
        alpha_deltas["total_alpha_rel_delta"] = _rel_delta(la.total_alpha, va.total_alpha)
    section("position_alpha", la, va, alpha_deltas)

    lpos, vpos = legacy.positioning, v1.positioning
    pos_deltas: dict[str, float | None] = {}
    if lpos is not None and vpos is not None:
        l_conc = lpos.concentration
        v_conc = vpos.concentration
        pos_deltas["hhi_rel_delta"] = _rel_delta(
            l_conc.hhi if l_conc else None, v_conc.hhi if v_conc else None
        )
        pos_deltas["num_positions_delta"] = (
            float(abs((l_conc.num_positions or 0) - (v_conc.num_positions or 0)))
            if l_conc and v_conc
            else None
        )
    section("positioning", lpos, vpos, pos_deltas)

    lb, vb = legacy.beta, v1.beta
    beta_deltas: dict[str, float | None] = {}
    if lb is not None and vb is not None:
        beta_deltas["beta_rel_delta"] = _rel_delta(lb.beta, vb.beta)
        beta_deltas["sharpe_rel_delta"] = _rel_delta(lb.sharpe, vb.sharpe)
    section("beta", lb, vb, beta_deltas)

    lpol, vpol = legacy.policy, v1.policy
    policy_deltas: dict[str, float | None] = {}
    if lpol is not None and vpol is not None:
        policy_deltas["total_pct_rel_delta"] = _rel_delta(lpol.total_pct, vpol.total_pct)
        policy_deltas["weights_count_delta"] = float(abs(len(lpol.weights) - len(vpol.weights)))
    section("policy", lpol, vpol, policy_deltas)

    return results


def run(api_url: str | None, start_date: str | None, end_date: str | None) -> int:
    prior = os.environ.get(tc._V1_READS_ENV)  # pyright: ignore[reportPrivateUsage]

    # The harness fires ~12 heavy recomputes back-to-back at a dev-grade
    # localhost server; the production 6s read budgets flake under that
    # serial load (measured 2026-07-24: alternating unavailable_* sections
    # across runs). A generous harness-only timeout isolates PARITY findings
    # from load findings — production budgets are exercised by the normal
    # render paths, not this script.
    harness_timeout = 30.0

    def read_with(
        flag: str,
    ) -> tuple[
        tc.LivePortfolio,
        list[tc.LiveTransaction] | None,
        tc.PortfolioAnalytics,
    ]:
        os.environ[tc._V1_READS_ENV] = flag  # pyright: ignore[reportPrivateUsage]
        live = tc.fetch_live_portfolio(api_url=api_url)
        history = tc.fetch_transaction_history(api_url=api_url, timeout=harness_timeout)
        analytics = tc.fetch_portfolio_analytics(
            api_url=api_url,
            timeout=harness_timeout,
            start_date=start_date,
            end_date=end_date,
        )
        return live, history, analytics

    try:
        _log("dual_read_start", api_url=api_url or "default")
        legacy_live, legacy_hist, legacy_analytics = read_with("0")
        v1_live, v1_hist, v1_analytics = read_with("1")
    finally:
        if prior is None:
            os.environ.pop(tc._V1_READS_ENV, None)  # pyright: ignore[reportPrivateUsage]
        else:
            os.environ[tc._V1_READS_ENV] = prior  # pyright: ignore[reportPrivateUsage]

    sections = [
        compare_live(legacy_live, v1_live),
        compare_history(legacy_hist, v1_hist),
        *compare_analytics(legacy_analytics, v1_analytics),
    ]
    statuses = {s.status for s in sections}
    if statuses == {"unavailable_both"}:
        overall, exit_code = "unavailable_both", 3
    elif "fail" in statuses:
        overall, exit_code = "fail", 2
    elif statuses & {"unavailable_legacy", "unavailable_v1"}:
        # A one-sided outage is not parity evidence either way; surface it
        # loudly but don't claim a pass.
        overall, exit_code = "fail", 2
    else:
        overall, exit_code = "pass", 0
    report = {
        "harness": "tracker_v1_parity",
        "overall": overall,
        "rel_tolerance": REL_TOL,
        "sections": [s.as_json() for s in sections],
    }
    print(json.dumps(report, indent=2))
    _log("dual_read_done", overall=overall)
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", type=str, default=None, help="tracker base URL override")
    parser.add_argument("--start-date", type=str, default=None, help="ISO analytics window start")
    parser.add_argument("--end-date", type=str, default=None, help="ISO analytics window end")
    args = parser.parse_args()
    return run(args.api_url, args.start_date, args.end_date)


if __name__ == "__main__":
    raise SystemExit(main())
