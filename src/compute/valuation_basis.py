"""Valuation selection and explicitly labelled provider-basis calculations.

Reported financial rows remain an unmigrated provider snapshot. Annual consensus
is FY1, never true NTM; realized-forward and LTM history cannot grade that basis.
All effective inputs and unavailable dispositions are retained in cache v3.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from llm_client import JSON_FENCE_RE, VALUATION_MULTIPLE_CHOICES, generate_valuation_basis
from report.render_clock import render_today
from sources.adapters import EstimateMetric, FiscalPeriodType
from sources.discovery_financial_inputs import comparable_quarter_window
from sources.discovery_financials import FinancialConcept
from sources.valuation_inputs import ValuationInputs, read_valuation_inputs


@dataclass
class ValuationHistPoint:
    period_end: str  # ISO date
    value: float | None
    basis: str = "unverified_legacy"
    method: str = "unverified_legacy"


@dataclass
class ValuationBasisResult:
    ticker: str
    multiple_name: str | None = None
    requested_multiple: str | None = None
    current_basis: str = "unavailable"
    current_method: str = "unavailable"
    comparison_unavailable_reason: str | None = None
    current_unavailable_reason: str | None = None
    source_context: dict[str, object] = field(default_factory=dict[str, object])
    rationale: str | None = None
    target_band: str | None = None
    notes: str | None = None
    current_value: float | None = None
    current_value_display: str | None = None
    current_period_end: str | None = None  # market observation / provider ratio date
    estimate_target_period_end: str | None = None
    history: list[ValuationHistPoint] = field(default_factory=list[ValuationHistPoint])
    historical_min: float | None = None
    historical_max: float | None = None
    historical_median: float | None = None
    rich_cheap_verdict: str | None = None
    # PEG = P/E(NTM) ÷ forward EPS growth%. Populated ONLY when the chosen
    # multiple is the earnings multiple P/E (NTM) AND forward EPS growth is
    # positive — None for book-value / EV / FCF multiples and unprofitable or
    # negative-growth names (see `_compute_peg`). `peg_growth_pct` is the
    # forward EPS growth rate used as the denominator, retained for display.
    peg_ratio: float | None = None
    peg_growth_pct: float | None = None
    cache_sha256: str | None = None
    extracted_at: str | None = None
    skipped_reason: str | None = None


# Map LLM-picked multiple → (FMP key_metrics field name OR computation strategy)
# "ntm" entries trigger a manual compute using analyst_estimates forward FY1.
_LTM_KEY_METRICS_FIELDS: dict[str, str] = {
    "EV/LTM Revenue": "evToSales",
    "EV/LTM EBITDA": "evToEBITDA",
    "P/E (LTM)": "peRatio",  # may be null in key_metrics; we fallback compute
    "P/B": "priceToBookRatio",
    "P/TBV": "priceToTangibleAssetsRatio",
    "P/FCF": "priceToFreeCashFlowsRatio",
    "EV/FCF": "evToFreeCashFlow",
}

_NTM_MULTIPLES: frozenset[str] = frozenset({"EV/NTM Revenue", "EV/NTM EBITDA", "P/E (NTM)"})

# Bump the source-manifest schema with changes to these calculation semantics.
# Unlabelled legacy caches must be rebuilt before any value is rendered.
_CACHE_VERSION = "v5"


def _coerce_multiple_payload(raw: str) -> dict[str, object] | None:
    """Parse the multiple-selection LLM response into a JSON object.

    Returns the decoded ``dict`` on success, or ``None`` when the response is
    empty, fenced-but-empty, non-JSON prose, truncated, or valid JSON that is
    not an object (e.g. a bare list or string). Returning ``None`` rather than
    raising lets ``extract_for_ticker`` degrade to a ``skipped_reason`` result
    so a transient malformed response can't surface an unhandled error out of
    the compute layer. The ``isinstance(decoded, dict)`` check in particular
    closes a latent ``AttributeError`` on the downstream ``parsed.get(...)``
    when the model returned a non-object shape. Mirrors the defensive parse in
    ``report.sections.bear_case`` / ``report.sections.earnings``.
    """
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = JSON_FENCE_RE.sub("", cleaned).strip()
    try:
        decoded: object = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    return cast("dict[str, object]", decoded)


def extract_for_ticker(
    ticker: str,
    repo_root: Path,
    db_conn: sqlite3.Connection,
    refresh: bool = False,
) -> ValuationBasisResult:
    """End-to-end: pick multiple via Opus → compute current value + history.

    Falls back gracefully when inputs are missing (no FMP key_metrics → empty
    result with skipped_reason; LLM call fails → cache miss propagates).
    """
    ticker = ticker.upper()
    cache_path = _cache_path(repo_root, ticker)
    inputs = read_valuation_inputs(repo_root, ticker, as_of=render_today(), conn=db_conn)
    key_metrics, income_q = inputs.key_metrics, inputs.income
    if not key_metrics and inputs.thesis.get("valuation_multiple_override") not in (
        "P/E (LTM)",
        "P/FCF",
    ):
        return ValuationBasisResult(
            ticker=ticker,
            skipped_reason="provider_key_metrics_unavailable",
            source_context=inputs.manifest,
        )
    thesis_text = next(
        (
            str(inputs.thesis[key])
            for key in ("thesis", "thesis_full", "thesis_one_liner")
            if isinstance(inputs.thesis.get(key), str) and inputs.thesis[key]
        ),
        "",
    )
    sector, industry = (
        _str_or_none(inputs.profile.get("sector")),
        _str_or_none(inputs.profile.get("industry")),
    )
    financial_profile = _financial_profile_md(key_metrics, income_q)
    estimates_md = _dated_estimates_md(inputs)
    override = _str_or_none(inputs.thesis.get("valuation_multiple_override"))
    if override not in VALUATION_MULTIPLE_CHOICES:
        override = None
    inputs_sha = inputs.fingerprint

    if not refresh and cache_path.exists():
        decoded_cache: object = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(decoded_cache, dict):
            raise ValueError("expected JSON object for valuation_basis cache")
        cached = cast("dict[str, Any]", decoded_cache)
        if cached.get("cache_sha256") == inputs_sha:
            hist_raw: Any = cached.pop("history", None) or []
            history = [ValuationHistPoint(**h) for h in hist_raw]
            return ValuationBasisResult(**cached, history=history)

    if override is not None:
        # Skip the LLM — analyst-pinned multiple.
        parsed: dict[str, object] = {
            "multiple": override,
            "rationale": "Owner-selected valuation multiple.",
            "target_band": "",
            "notes": "",
        }
    else:
        raw = generate_valuation_basis(
            ticker=ticker,
            sector=sector,
            industry=industry,
            thesis_text=thesis_text,
            financial_profile_md=financial_profile,
            available_estimates_md=estimates_md,
        )
        parsed_opt = _coerce_multiple_payload(raw)
        if parsed_opt is None:
            # Empty / non-JSON / valid-JSON-but-not-an-object response. Degrade
            # to a skipped_reason (the §Valuation section maps this to a loud
            # MISSING_DATA banner carrying a --refresh fix command) rather than
            # raising — a transient malformed multiple-selection response must
            # not surface an unhandled error out of the compute layer. The
            # non-object case in particular previously AttributeError'd on the
            # `parsed.get(...)` calls below.
            return ValuationBasisResult(
                ticker=ticker,
                skipped_reason=(
                    "LLM returned an empty, non-JSON, or non-object multiple-selection response"
                ),
                cache_sha256=inputs_sha,
            )
        parsed = parsed_opt

    multiple_name = _str_or_none(parsed.get("multiple"))
    if multiple_name not in VALUATION_MULTIPLE_CHOICES:
        # LLM picked something out-of-set — fall back to a sector default rather
        # than 500-erroring the whole render.
        multiple_name = _sector_fallback(sector, industry)

    requested_multiple = multiple_name
    current_value, current_pe, history, basis, method, unavailable = _labelled_series(
        multiple_name, inputs
    )
    if multiple_name in _NTM_MULTIPLES:
        actual_horizon = (
            "annual estimate; horizon unavailable"
            if basis == "unsupported_annual_estimate_horizon"
            else "FY1 estimate"
        )
        multiple_name = multiple_name.replace("NTM", actual_horizon)
    comparable = [
        point.value
        for point in history
        if point.value is not None and point.basis == basis and point.method == method
    ]
    hist_min = min(comparable) if comparable else None
    hist_max = max(comparable) if comparable else None
    hist_median = sorted(comparable)[len(comparable) // 2] if comparable else None
    rich_cheap = _rich_cheap_verdict(current_value, hist_median, hist_min, hist_max)
    comparison_reason = (
        None
        if rich_cheap is not None
        else "no_comparable_same_basis_history"
        if not comparable
        else "current_value_unavailable"
    )
    peg_ratio, peg_growth_pct = _dated_peg(requested_multiple, current_value, inputs)

    target_period_end = current_pe if requested_multiple in _NTM_MULTIPLES else None
    if requested_multiple in _NTM_MULTIPLES:
        current_pe = (
            inputs.market.captured_at.date().isoformat() if inputs.market.captured_at else None
        )
    result = ValuationBasisResult(
        ticker=ticker,
        multiple_name=multiple_name,
        requested_multiple=requested_multiple,
        current_basis=basis,
        current_method=method,
        current_unavailable_reason=unavailable,
        comparison_unavailable_reason=comparison_reason,
        source_context={
            **inputs.manifest,
            "requested_multiple": requested_multiple,
            "actual_display_label": multiple_name,
            "current_unavailable_reason": unavailable,
            "estimate_target_horizon_limit_days": 385,
            "estimate_target_horizon_scope": "upper_bound_only_not_verified_issuer_calendar_or_complete_curve",
            "calculation_version": _CACHE_VERSION,
        },
        rationale=_str_or_none(parsed.get("rationale")),
        target_band=_str_or_none(parsed.get("target_band")),
        notes=_str_or_none(parsed.get("notes")),
        current_value=current_value,
        current_value_display=_format_value(current_value, multiple_name),
        current_period_end=current_pe,
        estimate_target_period_end=target_period_end,
        history=history,
        historical_min=hist_min,
        historical_max=hist_max,
        historical_median=hist_median,
        rich_cheap_verdict=rich_cheap,
        peg_ratio=peg_ratio,
        peg_growth_pct=peg_growth_pct,
        cache_sha256=inputs_sha,
        extracted_at=datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )
    _write_cache(cache_path, result)
    return result


def _dated_estimates_md(inputs: ValuationInputs) -> str:
    if inputs.estimate_unavailable_reason:
        return f"FY1 estimate unavailable: {inputs.estimate_unavailable_reason}; true NTM is not implemented."
    lines = ["Dated annual FY1 estimates (not next twelve months):"]
    for estimate in inputs.estimates:
        if estimate.target_period_end.date() > render_today():
            lines.append(
                f"- {estimate.metric.value}: {estimate.estimated_avg} {estimate.currency.value}; FY ending {estimate.target_period_end.date()}; observed {estimate.observation_date.isoformat()}"
            )
    return "\n".join(lines)


def _labelled_series(
    requested: str | None, inputs: ValuationInputs
) -> tuple[float | None, str | None, list[ValuationHistPoint], str, str, str | None]:
    if requested not in _NTM_MULTIPLES:
        current, period, history = _compute_series(requested, inputs.key_metrics, inputs.balance)
        field_name = _LTM_KEY_METRICS_FIELDS.get(requested or "", "unknown")
        by_date = {str(row.get("date") or ""): row for row in inputs.key_metrics}
        for point in history:
            provider_value = _float(by_date[point.period_end].get(field_name))
            point.basis = "provider_reported_ratio_unmigrated"
            point.method = f"provider_field:{field_name}"
            if (provider_value is None or provider_value <= 0) and point.value is not None:
                point.basis = "legacy_calculated_book_multiple_unmigrated"
                point.method = f"legacy_book_formula:{requested}"
        if requested in {"P/E (LTM)", "P/FCF"}:
            concept: FinancialConcept = (
                "net_income" if requested == "P/E (LTM)" else "free_cash_flow"
            )
            window = comparable_quarter_window(inputs.canonical_financials, concept, 4)
            method = f"captured_market_cap / sum_4_contiguous_quarter_{concept}"
            period = (
                inputs.market.captured_at.date().isoformat() if inputs.market.captured_at else None
            )
            current = None
            reason: str | None = None
            if isinstance(window, str):
                reason = window
            elif sum((item.value for item in window), Decimal(0)) <= 0:
                reason = "nonpositive_canonical_ltm_denominator"
            elif window[0].unit not in {"actual", window[0].currency}:
                reason = "canonical_ltm_currency_scale_unavailable"
            elif inputs.market.status != "available" or inputs.market.market_cap is None:
                reason = "current_captured_market_cap_unavailable"
            elif inputs.market.currency != window[0].currency:
                reason = "canonical_ltm_market_currency_mismatch"
            else:
                current = float(
                    inputs.market.market_cap / sum((item.value for item in window), Decimal(0))
                )
            return current, period, history, "canonical_reported_ltm", method, reason
        basis = history[-1].basis if history else "unavailable"
        method = history[-1].method if history else "unavailable"
        return (
            current,
            period,
            history,
            basis,
            method,
            None if current is not None else "provider_ratio_inputs_unavailable",
        )
    metric = {
        "P/E (NTM)": EstimateMetric.NET_INCOME,
        "EV/NTM Revenue": EstimateMetric.REVENUE,
        "EV/NTM EBITDA": EstimateMetric.EBITDA,
    }[requested]
    candidates = sorted(
        (
            item
            for item in inputs.estimates
            if item.metric == metric
            and item.fiscal_period == FiscalPeriodType.FY
            and item.target_period_end.date() > render_today()
        ),
        key=lambda item: item.target_period_end,
    )
    selected = candidates[0] if candidates else None
    current: float | None = None
    reason = inputs.estimate_unavailable_reason
    period = selected.target_period_end.date().isoformat() if selected else None
    unsupported_horizon = (
        selected is not None and (selected.target_period_end.date() - render_today()).days > 385
    )
    if unsupported_horizon:
        reason = "fy1_target_horizon_unavailable"
    if reason is None:
        if selected is None or selected.estimated_avg <= 0:
            reason = "positive_fy1_denominator_unavailable"
        elif requested != "P/E (NTM)":
            reason = "enterprise_value_definition_and_capture_unavailable"
        elif inputs.market.status != "available" or inputs.market.market_cap is None:
            reason = "current_captured_market_cap_unavailable"
        elif inputs.market.currency != selected.currency.value:
            reason = "estimate_market_currency_mismatch"
        else:
            current = float(inputs.market.market_cap / selected.estimated_avg)
    income = sorted(
        ((str(row.get("date") or ""), row) for row in inputs.income if row.get("date")),
        key=lambda pair: pair[0],
    )
    ltm = {"P/E (NTM)": "peRatio", "EV/NTM Revenue": "evToSales", "EV/NTM EBITDA": "evToEBITDA"}[
        requested
    ]
    history: list[ValuationHistPoint] = []
    for row in reversed(inputs.key_metrics[:12]):
        period_end = str(row.get("date") or "")
        # Retain the old calculation solely as an explicitly hindsight-based
        # numerical shadow; it is never the historical consensus comparator.
        value = _compute_realized_forward_value(requested, row, income)
        basis, method = "realized_forward_proxy_unmigrated", "legacy_following_four_records"
        if value is None:
            value = _float(row.get(ltm))
            value = value if value is not None and value > 0 else None
            basis, method = "provider_ltm_proxy_unmigrated", f"provider_field:{ltm}"
        history.append(ValuationHistPoint(period_end, value, basis, method))
    return (
        current,
        period,
        history,
        "unsupported_annual_estimate_horizon" if unsupported_horizon else "fy1_estimate",
        "captured_market_cap / dated_fy1_net_income"
        if requested == "P/E (NTM)"
        else "unavailable_ev_fy1",
        reason,
    )


def _dated_peg(
    requested: str | None, current: float | None, inputs: ValuationInputs
) -> tuple[float | None, float | None]:
    if (
        requested != "P/E (NTM)"
        or current is None
        or current <= 0
        or inputs.estimate_unavailable_reason
    ):
        return None, None
    eps = sorted(
        (
            item
            for item in inputs.estimates
            if item.metric == EstimateMetric.EPS
            and item.fiscal_period == FiscalPeriodType.FY
            and item.target_period_end.date() > render_today()
        ),
        key=lambda item: item.target_period_end,
    )
    income = sorted(
        (
            item
            for item in inputs.estimates
            if item.metric == EstimateMetric.NET_INCOME
            and item.fiscal_period == FiscalPeriodType.FY
            and item.target_period_end.date() > render_today()
        ),
        key=lambda item: item.target_period_end,
    )
    if (
        len(eps) < 2
        or not income
        or eps[0].target_period_end != income[0].target_period_end
        or eps[0].estimated_avg <= 0
    ):
        return None, None
    if (
        eps[0].currency != eps[1].currency
        or eps[0].observation_date != eps[1].observation_date
        or not 335 <= (eps[1].target_period_end - eps[0].target_period_end).days <= 395
    ):
        return None, None
    growth = float((eps[1].estimated_avg / eps[0].estimated_avg - 1) * 100)
    return (current / growth, growth) if growth > 0 else (None, None)


def load(
    repo_root: Path, ticker: str, *, conn: sqlite3.Connection | None = None
) -> ValuationBasisResult | None:
    """Read only a current, labelled cache; validation never invokes the picker."""
    path = _cache_path(repo_root, ticker)
    if not path.exists():
        return None
    decoded: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("expected JSON object for valuation_basis cache")
    payload = cast("dict[str, Any]", decoded)
    context = payload.get("source_context")
    if not isinstance(context, dict) or context.get("calculation_version") != _CACHE_VERSION:
        return ValuationBasisResult(
            ticker=ticker, skipped_reason="valuation_cache_rebuild_required_unlabelled_legacy_basis"
        )
    if conn is None:
        return ValuationBasisResult(
            ticker=ticker, skipped_reason="valuation_cache_validation_database_unavailable"
        )
    inputs = read_valuation_inputs(repo_root, ticker, as_of=render_today(), conn=conn)
    if payload.get("cache_sha256") != inputs.fingerprint:
        return ValuationBasisResult(
            ticker=ticker, skipped_reason="valuation_cache_rebuild_required_input_identity_changed"
        )
    history_raw: Any = payload.pop("history", []) or []
    return ValuationBasisResult(
        **payload, history=[ValuationHistPoint(**point) for point in history_raw]
    )


# ---------------------------------------------------------------------------
# Sources / IO helpers
# ---------------------------------------------------------------------------


def _cache_path(repo_root: Path, ticker: str) -> Path:
    return repo_root / "data" / "valuation_basis" / f"{ticker.upper()}.json"


def _write_cache(path: Path, result: ValuationBasisResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(result), indent=2, default=str), encoding="utf-8")


# ---------------------------------------------------------------------------
# Prompt-input formatting
# ---------------------------------------------------------------------------


def _financial_profile_md(
    key_metrics: list[dict[str, object]],
    income_q: list[dict[str, object]],
) -> str:
    """Compact summary of recent quarterly financial shape so the LLM can
    judge which multiple is appropriate. Keep it tight — 1-2 KB max."""
    out: list[str] = [
        "Unmigrated provider financial context; native source units, currency not inferred."
    ]
    if income_q:
        latest = income_q[0]
        prior_yr = income_q[4] if len(income_q) > 4 else None
        rev = _float(latest.get("revenue"))
        op_inc = _float(latest.get("operatingIncome"))
        net_inc = _float(latest.get("netIncome"))
        eps = _float(latest.get("eps"))
        op_margin = (op_inc / rev * 100) if rev and op_inc is not None else None
        yoy = None
        if prior_yr and rev:
            prior_rev = _float(prior_yr.get("revenue"))
            if prior_rev:
                yoy = (rev - prior_rev) / prior_rev * 100
        out.append(f"- Latest quarter ({latest.get('date')}):")
        out.append(f"  - Revenue: {rev / 1e9:.2f}B" if rev else "  - Revenue: n/a")
        if yoy is not None:
            out.append(f"  - Revenue YoY: {yoy:+.1f}%")
        if op_margin is not None:
            out.append(f"  - Operating margin: {op_margin:.1f}%")
        if net_inc is not None:
            out.append(f"  - Net income: {net_inc / 1e9:.2f}B")
        if eps is not None:
            out.append(f"  - EPS: {eps:.2f}")
    if key_metrics:
        latest_km = key_metrics[0]
        ev = _float(latest_km.get("enterpriseValue"))
        mcap = _float(latest_km.get("marketCap"))
        if mcap:
            out.append(f"  - Market cap: {mcap / 1e9:.1f}B")
        if ev:
            out.append(f"  - Enterprise value: {ev / 1e9:.1f}B")
        # Current LTM multiples (whatever FMP populated)
        for label, field_name in (
            ("EV/LTM Revenue", "evToSales"),
            ("EV/LTM EBITDA", "evToEBITDA"),
            ("P/B", "priceToBookRatio"),
            ("P/E (LTM)", "peRatio"),
            ("P/FCF", "priceToFreeCashFlowsRatio"),
        ):
            v = _float(latest_km.get(field_name))
            if v is not None and v > 0:
                out.append(f"  - {label}: {v:.1f}x")
    return "\n".join(out) if out else "(no financial profile available)"


# ---------------------------------------------------------------------------
# Multiple computation
# ---------------------------------------------------------------------------


def _compute_series(
    multiple_name: str | None,
    key_metrics: list[dict[str, object]],
    balance_q: list[dict[str, object]] | None = None,
) -> tuple[float | None, str | None, list[ValuationHistPoint]]:
    """Return (current_value, current_period_end_iso, 8Q history)."""
    if multiple_name is None or not key_metrics:
        return (None, None, [])

    field_name = _LTM_KEY_METRICS_FIELDS.get(multiple_name)
    if field_name is None:
        return (None, None, [])

    # Build per-period lookup from balance sheet for manual P/B + P/TBV
    # fallback when key_metrics has nulls (FMP often misses these for non-US
    # listings / fintechs / recent IPOs).
    balance_by_date: dict[str, dict[str, object]] = {}
    if balance_q:
        for row in balance_q:
            d = str(row.get("date") or "")
            if d:
                balance_by_date[d] = row

    history: list[ValuationHistPoint] = []
    for row in key_metrics[:12]:  # 12 quarters back, oldest-last in display
        pe = str(row.get("date") or "")
        v = _float(row.get(field_name))
        if v is not None and v <= 0:
            v = None
        # Manual fallback for P/B and P/TBV from balance sheet.
        if v is None and multiple_name in ("P/B", "P/TBV"):
            v = _manual_book_multiple(multiple_name, row, balance_by_date.get(pe))
        history.append(ValuationHistPoint(period_end=pe, value=v))
    history.reverse()  # oldest-first for the sparkline
    current = history[-1].value if history else None
    current_pe = history[-1].period_end if history else None
    return (current, current_pe, history)


def _manual_book_multiple(
    multiple_name: str,
    key_metrics_row: dict[str, object],
    balance_row: dict[str, object] | None,
) -> float | None:
    """Compute P/B = marketCap / totalStockholdersEquity, or P/TBV =
    marketCap / (totalStockholdersEquity - goodwill - intangibles).

    Falls back to None when balance sheet doesn't disclose the inputs.
    """
    if balance_row is None:
        return None
    mcap = _float(key_metrics_row.get("marketCap"))
    equity = _float(balance_row.get("totalStockholdersEquity")) or _float(
        balance_row.get("totalEquity")
    )
    if not mcap or not equity or equity <= 0:
        return None
    if multiple_name == "P/B":
        return mcap / equity
    # P/TBV: subtract goodwill + intangibles from equity to get tangible book
    goodwill = _float(balance_row.get("goodwill")) or 0.0
    intangibles = _float(balance_row.get("intangibleAssets")) or 0.0
    tangible_book = equity - goodwill - intangibles
    if tangible_book <= 0:
        return None
    return mcap / tangible_book


def _compute_realized_forward_value(
    multiple_name: str,
    snapshot_km_row: dict[str, object],
    income_by_date_asc: list[tuple[str, dict[str, object]]],
) -> float | None:
    """Numerator = market cap / EV at snapshot date. Denominator = sum of
    the 4 quarterly fundamentals (revenue / EBITDA / net income) that
    actually happened in the 12 months AFTER the snapshot date. Returns
    None when we don't have 4 forward quarters on file."""
    snapshot_date = str(snapshot_km_row.get("date") or "")
    if not snapshot_date:
        return None

    # Find the 4 quarterly periods strictly AFTER the snapshot date.
    forward = [r for d, r in income_by_date_asc if d > snapshot_date][:4]
    if len(forward) < 4:
        return None

    income_field = {
        "EV/NTM Revenue": "revenue",
        "EV/NTM EBITDA": "ebitda",
        "P/E (NTM)": "netIncome",
    }[multiple_name]
    forward_values = [_float(r.get(income_field)) for r in forward]
    if any(v is None for v in forward_values):
        return None
    forward_sum = sum(v for v in forward_values if v is not None)
    if forward_sum <= 0:
        return None

    if multiple_name in ("EV/NTM Revenue", "EV/NTM EBITDA"):
        numerator = _float(snapshot_km_row.get("enterpriseValue"))
    else:  # P/E (NTM)
        numerator = _float(snapshot_km_row.get("marketCap"))
    if not numerator or numerator <= 0:
        return None
    return numerator / forward_sum


def _compute_peg(
    multiple_name: str | None,
    current_value: float | None,
    analyst_annual: list[dict[str, object]],
    income_q: list[dict[str, object]] | None,
) -> tuple[float | None, float | None]:
    """PEG = P/E(NTM) ÷ forward-EPS-growth%. Returns (peg_ratio, peg_growth_pct).

    Applicability — "PEG across the board, except where it doesn't make sense":
    only an *earnings* multiple over a *positive* forward growth rate yields a
    meaningful PEG. We gate on the chosen multiple being P/E (NTM) (so the
    numerator `current_value` is literally the forward P/E, not a P/B / EV /
    FCF multiple) AND a positive forward EPS growth rate. Returns (None, None)
    for book-value-valued banks (P/B, P/TBV), EV/EBITDA, EV/Revenue, the FCF
    multiples, and unprofitable / negative-growth names — i.e. PEG is simply
    omitted where it would mislead.
    """
    if multiple_name != "P/E (NTM)" or not current_value or current_value <= 0:
        return (None, None)
    growth_pct = _compute_forward_eps_growth(analyst_annual, income_q)
    if growth_pct is None or growth_pct <= 0:
        return (None, None)
    return (current_value / growth_pct, growth_pct)


def _compute_forward_eps_growth(
    analyst_annual: list[dict[str, object]],
    income_q: list[dict[str, object]] | None,
) -> float | None:
    """Forward EPS growth % used as the PEG denominator.

    Primary (forward-over-forward): year-over-year growth of the forward annual
    EPS consensus, measured from the P/E(NTM) basis year (the closest forward
    fiscal year — the same row `_compute_ntm_multiple` divides into) to the
    following year, i.e. ``future[-2].epsAvg`` vs ``future[-1].epsAvg``. This is
    the growth the forward multiple is paying for.

    Fallback (only one forward year of consensus on file): grow the closest
    forward-year EPS estimate over trailing-twelve-month actual EPS, keeping the
    span ~1 year so the rate stays comparable.

    Returns None when neither path has the inputs or the base EPS is <= 0 (a
    growth rate off a zero / loss-making base isn't meaningful — those names are
    PEG-ineligible anyway).
    """
    # `analyst_annual` is sorted newest-first by `_load_quarterly`; the filtered
    # `future` list preserves that order, so future[-1] is the closest forward
    # fiscal year and future[-2] the year after it.
    today_iso = date.today().isoformat()
    future = [r for r in analyst_annual if str(r.get("date") or "") > today_iso]
    if len(future) >= 2:
        next_eps = _float(future[-2].get("epsAvg"))
        base_eps = _float(future[-1].get("epsAvg"))
    elif len(future) == 1:
        next_eps = _float(future[-1].get("epsAvg"))
        base_eps = _ltm_eps(income_q)
    else:
        return None
    if next_eps is None or base_eps is None or base_eps <= 0:
        return None
    return (next_eps - base_eps) / base_eps * 100.0


def _ltm_eps(income_q: list[dict[str, object]] | None) -> float | None:
    """Trailing-twelve-month EPS = sum of the latest 4 quarterly EPS values.

    `income_q` is newest-first (see `_load_quarterly`). Returns None unless all
    four trailing quarters disclose an EPS figure."""
    if not income_q:
        return None
    eps_vals = [_float(r.get("eps")) for r in income_q[:4]]
    if len(eps_vals) < 4 or any(v is None for v in eps_vals):
        return None
    return sum(v for v in eps_vals if v is not None)


def _sector_fallback(sector: str | None, industry: str | None) -> str:
    """Pick a sector-default multiple when the LLM returns an out-of-set value."""
    blob = f"{sector or ''} {industry or ''}".lower()
    if any(t in blob for t in ("bank", "diversified financial", "insurance", "asset management")):
        return "P/B"
    if any(t in blob for t in ("software", "internet", "technology services")):
        return "EV/LTM Revenue"
    if any(t in blob for t in ("oil", "gas", "metal", "mining", "materials")):
        return "EV/LTM EBITDA"
    return "P/E (LTM)"


def _rich_cheap_verdict(
    current: float | None,
    median: float | None,
    minv: float | None,
    maxv: float | None,
) -> str | None:
    """One-line read of current vs trailing band, e.g. 'rich vs 8Q median 12.4x'."""
    if current is None or median is None:
        return None
    drift_pct = (current - median) / median * 100 if median else None
    if drift_pct is None:
        return None
    if abs(drift_pct) <= 5:
        tone = "in-line with"
    elif drift_pct > 0:
        tone = "rich vs"
    else:
        tone = "cheap vs"
    bits = [f"{tone} trailing median {median:.1f}x"]
    if minv is not None and maxv is not None:
        bits.append(f"(range {minv:.1f}-{maxv:.1f}x)")
    return " ".join(bits)


# ---------------------------------------------------------------------------
# Small parsing helpers
# ---------------------------------------------------------------------------


def _float(v: object) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if not s:
            return None
        try:
            value = float(s)
            return value if math.isfinite(value) else None
        except ValueError:
            return None
    return None


def _str_or_none(v: object) -> str | None:
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def _format_value(v: float | None, multiple_name: str | None) -> str | None:
    if v is None:
        return None
    if multiple_name and (
        "P/E" in multiple_name or "EV/" in multiple_name or "P/" in multiple_name
    ):
        return f"{v:.1f}x"
    return f"{v:.2f}"


# Public re-exports for the section builder.
__all__ = [
    "VALUATION_MULTIPLE_CHOICES",
    "ValuationBasisResult",
    "ValuationHistPoint",
    "extract_for_ticker",
    "load",
]
