"""Per-name DCF assumption pass (new redesign schema).

For a ticker, gather FMP + thesis context and ask the configured DCF model to set the
10-year DCF drivers the redesigned workbook consumes: per-segment growth
(near-term + terminal), the operating-margin path, capex intensity, terminal
basis/multiple, business-model applicability, and a narrative. Cache the result
under data/dcf_assumptions/<T>.json (key "redesign") so the renderer can read it.

Usage:  DCF_TICKER=AMZN python execution/refresh_dcf_assumptions.py
"""

from __future__ import annotations

import json
import math
import os
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import cast

from pydantic import TypeAdapter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from compute.segment_cache import apply_overrides
from db_paths import require_db_path
from dcf.artifact_promotion import DcfRecoveryError, hold_dcf_artifacts, unique_staged_path
from dcf.assumption_evidence import kpi_evidence
from dcf.assumption_prompt import build_prompt, call_dcf_assumptions, segment_guard
from dcf.fiscal_periods import detect_fy_periods
from llm.contracts import DcfAssumptionsPayload
from llm.structured import call_llm_structured
from runtime.job_runtime import JobAlreadyRunningError

REPO = Path(os.environ.get("DCF_REPO_ROOT") or Path(__file__).resolve().parents[1])
FMP = REPO / "data" / "historical" / "fmp"

DATABASE_PATH = require_db_path()
TICKER = os.environ.get("DCF_TICKER", "AMZN")


def load_cache_records(name: str) -> list[dict[str, object]]:
    path = FMP / name
    if not path.exists():
        return []
    payload: object = json.loads(path.read_text(encoding="utf-8"))
    # Some profile cache versions contain one object instead of an array.
    return TypeAdapter(list[dict[str, object]]).validate_python(
        [payload] if isinstance(payload, dict) else payload
    )


def to_millions(value: object) -> float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        return None
    return value / 1e6


def _format_millions(value: object) -> str:
    scaled = to_millions(value)
    return "?" if scaled is None else f"{scaled:,.0f}"


income_records = load_cache_records(f"{TICKER}_income_statement_quarterly.json")
cashflow_records = load_cache_records(f"{TICKER}_cash_flow_quarterly.json")
segment_records = apply_overrides(
    load_cache_records(f"{TICKER}_product_segments_quarterly.json"),
    ticker=TICKER,
    dim_type="product",
    db_path=str(DATABASE_PATH),
)
est = load_cache_records(f"{TICKER}_analyst_estimates_annual.json")
profile_records = load_cache_records(f"{TICKER}_profile.json")
company_profile: dict[str, object] = profile_records[0] if profile_records else {}


def index_fiscal_records(
    records: Sequence[dict[str, object]], *, segments: bool = False
) -> dict[tuple[int, str], dict[str, object]]:
    indexed: dict[tuple[int, str], dict[str, object]] = {}
    for record in records:
        period, fiscal_year = record.get("period"), record.get("fiscalYear")
        if not isinstance(fiscal_year, (str, int, float)):
            continue
        try:
            year = int(fiscal_year)
        except (TypeError, ValueError):
            continue
        if isinstance(period, str) and period.startswith("Q"):
            indexed[(year, period)] = (
                TypeAdapter(dict[str, object]).validate_python(record.get("data") or {})
                if segments
                else record
            )
    return indexed


income_by_period, cashflow_by_period, segments_by_period = (
    index_fiscal_records(income_records),
    index_fiscal_records(cashflow_records),
    index_fiscal_records(segment_records, segments=True),
)
PERIODS = detect_fy_periods(
    income_by_period
)  # ("Q1".."Q4") quarterly · ("Q2","Q4") semi-annual (BHP)
fys = sorted({year for year, _period in income_by_period})
full = [y for y in fys if all((y, p) in income_by_period for p in PERIODS)]
if not full:
    # No issuer-complete fiscal year (e.g. an IPO with < 2 years of history): the
    # redesign builder will SKIP this name, so don't spend a model call on it.
    print(f"SKIP\t{TICKER}\tno complete fiscal year yet\t(insufficient history for a DCF)")
    raise SystemExit(0)


def fiscal_year_total(
    records: Mapping[tuple[int, str], Mapping[str, object]], field: str, year: int
) -> float | None:
    values = [to_millions(records.get((year, period), {}).get(field)) for period in PERIODS]
    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


_REQUIRED_ACTUAL_FIELDS = {
    "income": ("revenue", "operatingIncome", "netIncome"),
    "cash_flow": ("capitalExpenditure", "depreciationAndAmortization"),
}


def _required_actual_issues() -> tuple[str, ...]:
    issues: list[str] = []
    sources = {"income": income_by_period, "cash_flow": cashflow_by_period}
    for year in full[-3:]:
        for period in PERIODS:
            for statement, fields in _REQUIRED_ACTUAL_FIELDS.items():
                record = sources[statement].get((year, period), {})
                for field in fields:
                    value = record.get(field)
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(float(value))
                    ):
                        issues.append(f"{statement}:FY{year}:{period}:{field}")
    return tuple(issues)


# --- context ---
lines = [
    f"Company: {company_profile.get('companyName', TICKER)} ({TICKER})",
    f"Sector / industry: {company_profile.get('sector', '?')} / {company_profile.get('industry', '?')}",
    f"Reported currency: {(income_by_period.get((full[-1], PERIODS[-1])) or income_records[0] if income_records else {}).get('reportedCurrency', '?') if income_records else '?'}",
    f"Country: {company_profile.get('country', '?')}  |  Beta: {company_profile.get('beta', '?')}  |  Current price (USD): {company_profile.get('price', '?')}",
    "",
]
lines.append("Fiscal-year actuals, up to five years (reporting currency, $M):")
for y in full[-5:]:
    rev = fiscal_year_total(income_by_period, "revenue", y)
    if rev is None or rev == 0:
        continue
    oi = fiscal_year_total(income_by_period, "operatingIncome", y)
    ni = fiscal_year_total(income_by_period, "netIncome", y)
    cap_raw = fiscal_year_total(cashflow_by_period, "capitalExpenditure", y)
    da = fiscal_year_total(cashflow_by_period, "depreciationAndAmortization", y)
    if oi is None or ni is None or cap_raw is None or da is None:
        continue
    cap = abs(cap_raw)
    lines.append(
        f"  FY{y}: revenue {rev:,.0f}  op-margin {oi / rev:.1%}  net-margin {ni / rev:.1%}  "
        f"capex {cap:,.0f} ({cap / rev:.1%} of rev)  D&A {da:,.0f}"
    )

# segments
seg_ann: defaultdict[int, defaultdict[str, float]] = defaultdict(lambda: defaultdict(float))
for (y, _period), d in segments_by_period.items():
    for k, v in (d or {}).items():
        if isinstance(v, (int, float)):
            seg_ann[y][k] += v / 1e6
seg_names: list[str] = []
if full and seg_ann.get(full[-1]):
    seg_names = sorted(seg_ann[full[-1]], key=lambda s: -seg_ann[full[-1]][s])
    lines.append("\nProduct segments (latest FY revenue, $M, and YoY growth):")
    for s in seg_names:
        cur, prev = seg_ann[full[-1]].get(s, 0), seg_ann[full[-2]].get(s, 0) if len(full) > 1 else 0
        g = (cur / prev - 1) if prev else None
        lines.append(f"  {s}: {cur:,.0f}" + (f"  ({g:+.1%} YoY)" if g is not None else ""))
else:
    lines.append(
        "\nNo usable product-segment data — model a single revenue line ('Total company')."
    )

# admitted company-reported KPIs (business drivers)
KPI_EVIDENCE = kpi_evidence(DATABASE_PATH, TICKER, repo_root=REPO)
if KPI_EVIDENCE.lines:
    lines.append(
        "\nCompany-reported KPIs admitted through the fact resolver "
        "(period end and value, oldest to newest):"
    )
    lines.extend(KPI_EVIDENCE.lines)
    if KPI_EVIDENCE.omitted:
        lines.append(f"  ({KPI_EVIDENCE.omitted} further admitted KPI series not shown.)")
else:
    lines.append("\nNo admitted company-reported KPI series for this ticker.")
if KPI_EVIDENCE.capture_gaps:
    lines.append(
        "KPIs defined for this ticker with NO admitted facts (capture gaps; do not "
        "treat them as reported): " + "; ".join(KPI_EVIDENCE.capture_gaps)
    )

# consensus
estimates_by_year: dict[int, dict[str, object]] = {}
for e in est:
    with suppress(TypeError, ValueError):
        estimates_by_year[int(str(e.get("date"))[:4])] = e
fwd = [y for y in sorted(estimates_by_year) if y >= (full[-1] + 1 if full else 2026)][:5]
if fwd:
    lines.append(
        "\nStreet consensus (FMP) forward estimates ($M, EPS per share). "
        "A comparison only, not a target:"
    )
    for y in fwd:
        e = estimates_by_year[y]
        lines.append(
            f"  {y}: revenue {_format_millions(e.get('revenueAvg'))}  "
            f"net-income {_format_millions(e.get('netIncomeAvg'))}  "
            f"EPS {e.get('epsAvg', '?')}"
        )

# thesis
for d in ("holdings", "evaluation"):
    tp = REPO / "micro_thesis" / d / f"{TICKER}.json"
    if tp.exists():
        th = json.loads(tp.read_text(encoding="utf-8"))
        blurb = th.get("thesis") or th.get("micro_thesis") or ""
        if blurb:
            lines.append(f"\nUser's investment thesis:\n  {str(blurb)[:600]}")
        break

CONTEXT = "\n".join(lines)
SEG_KEYS = seg_names if seg_names else ["Total company"]
PROMPT = build_prompt(TICKER, CONTEXT, SEG_KEYS)


def _segment_guard(value: object) -> tuple[bool, str]:
    return segment_guard(SEG_KEYS)(value)


def _call_dcf_assumptions() -> DcfAssumptionsPayload:
    issues = _required_actual_issues()
    if issues:
        raise RuntimeError(f"required_actual_unavailable: {','.join(issues)}")
    return call_dcf_assumptions(TICKER, CONTEXT, SEG_KEYS, call=call_llm_structured)


def main() -> int:
    issues = _required_actual_issues()
    if issues:
        print(
            f"UNAVAILABLE\t{TICKER}\trequired_actual_unavailable\t{','.join(issues)}",
            file=sys.stderr,
        )
        return 2
    result = _call_dcf_assumptions()
    data = cast("dict[str, object]", result.model_dump(mode="json"))
    data["_segment_keys"] = SEG_KEYS

    try:
        with hold_dcf_artifacts(REPO, TICKER, owner="dcf-assumptions", wait_s=0):
            cache_path = REPO / "data" / "dcf_assumptions" / f"{TICKER}.json"
            existing_raw: object = (
                json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
            )
            if not isinstance(existing_raw, dict):
                raise ValueError(f"{cache_path} must contain a JSON object")
            existing = cast("dict[str, object]", existing_raw)
            prior_redesign = existing.get("redesign")
            if isinstance(prior_redesign, dict) and "dcf_debt_scope" in prior_redesign:
                # Deterministic owner/governance input: the LLM schema neither selects
                # nor rewrites the liability perimeter used by the equity bridge.
                data["dcf_debt_scope"] = prior_redesign["dcf_debt_scope"]
            existing["redesign"] = data
            existing.setdefault("narrative", result.narrative)

            from dcf.assumptions_doc import baseline_from_assumption_refresh

            existing["opus_baseline"] = baseline_from_assumption_refresh(data)
            existing.pop("assumption_overrides", None)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            staged = unique_staged_path(cache_path, "assumptions-refresh")
            staged.write_text(json.dumps(existing, indent=2), encoding="utf-8")
            os.replace(staged, cache_path)
    except JobAlreadyRunningError:
        print(f"BLOCKED\t{TICKER}\tdcf_writer_busy", file=sys.stderr)
        return 75
    except DcfRecoveryError:
        print(f"BLOCKED\t{TICKER}\tdcf_recovery_required", file=sys.stderr)
        return 2
    suggestion = result.valuation_model_suggestion
    print(
        f"OK\t{TICKER}\tvaluation_model={result.valuation_model}\t"
        f"business={result.business_model}\tbasis={result.exit_basis}@{result.exit_multiple}\t"
        f"term_margin={result.terminal_op_margin}"
        + (f"\tSUGGESTS: {suggestion}" if result.valuation_model == "new" else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
