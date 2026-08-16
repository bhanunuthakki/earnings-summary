"""Compute the §Valuation tab payload: Opus-picked multiple + current value + 8Q history.

Pipeline:
  1. Locate FMP `key_metrics_quarterly.json` (the source of truth for the
     standard EV multiples + market cap) and `analyst_estimates_*` (NTM lines).
  2. Build a tiny financial profile + estimates-availability summary for the LLM.
  3. Call `generate_valuation_basis` to pick ONE multiple from the allowed set.
  4. Compute the numeric value for the chosen multiple + 8Q history (where the
     FMP-disclosed series supports it) so the renderer can show a sparkline +
     rich/cheap verdict vs the trailing median.
  5. Cache the whole payload at `data/valuation_basis/<T>.json` keyed by
     (thesis_sha + latest_period_end + analyst_estimates_sha) so a fresh
     quarterly print or thesis revision invalidates.

Source-of-truth rules (per project memory):
- Quantitative multiples: FMP `key_metrics_quarterly.json`
- NTM estimates: FMP `analyst_estimates_annual.json` (forward FY1)
- Market cap / price: FMP `historical_market_cap.json` for current spot
- Thesis text: `micro_thesis/holdings/<T>.json`

The renderer is a pure consumer of this output — no display logic lives here.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

from llm_client import JSON_FENCE_RE, VALUATION_MULTIPLE_CHOICES, generate_valuation_basis


@dataclass
class ValuationHistPoint:
    period_end: str  # ISO date
    value: float | None


@dataclass
class ValuationBasisResult:
    ticker: str
    multiple_name: str | None = None
    rationale: str | None = None
    target_band: str | None = None
    notes: str | None = None
    current_value: float | None = None
    current_value_display: str | None = None
    current_period_end: str | None = None  # ISO date
    history: list[ValuationHistPoint] = field(default_factory=list)
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

# Bump when the computed payload shape changes (new derived fields) so existing
# caches recompute on their next --enable-llm build even though the underlying
# inputs (thesis / profile / estimates) are unchanged. v2 added the PEG ratio.
_CACHE_VERSION = "v2"


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
        decoded = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    return cast("dict[str, object]", decoded)


def extract_for_ticker(
    ticker: str,
    repo_root: Path,
    db_conn: sqlite3.Connection,  # accepted for parity with other compute layers — unused here
    refresh: bool = False,
) -> ValuationBasisResult:
    """End-to-end: pick multiple via Opus → compute current value + history.

    Falls back gracefully when inputs are missing (no FMP key_metrics → empty
    result with skipped_reason; LLM call fails → cache miss propagates).
    """
    _ = db_conn  # signature parity hook
    ticker = ticker.upper()
    cache_path = _cache_path(repo_root, ticker)

    key_metrics = _load_quarterly(repo_root, ticker, "key_metrics_quarterly.json")
    if not key_metrics:
        return ValuationBasisResult(
            ticker=ticker,
            skipped_reason=f"no key_metrics_quarterly.json for {ticker}",
        )

    income_q = _load_quarterly(repo_root, ticker, "income_statement_quarterly.json")
    balance_q = _load_quarterly(repo_root, ticker, "balance_sheet_quarterly.json")
    analyst_annual = _load_list(repo_root, ticker, "analyst_estimates_annual.json")
    thesis_text = _load_thesis(repo_root, ticker)
    sector, industry = _load_sector_industry(repo_root, ticker)

    financial_profile = _financial_profile_md(key_metrics, income_q)
    estimates_md = _available_estimates_md(analyst_annual)

    # Per-ticker override: holdings JSON can pin the multiple via
    # `valuation_multiple_override` (must match a VALUATION_MULTIPLE_CHOICES
    # entry). Skips the LLM call entirely when present. Use case: the
    # analyst has already decided NTM P/E is the right lens for a
    # particular name and doesn't want Opus second-guessing it.
    override = _load_multiple_override(repo_root, ticker)

    inputs_sha = hashlib.sha256(
        (
            _CACHE_VERSION
            + "\x00"
            + thesis_text
            + "\x00"
            + financial_profile
            + "\x00"
            + estimates_md
            + "\x00"
            + (override or "")
        ).encode("utf-8")
    ).hexdigest()

    if not refresh and cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("cache_sha256") == inputs_sha:
            cached.pop("history", None)
            hist_raw = json.loads(cache_path.read_text(encoding="utf-8")).get("history") or []
            history = [ValuationHistPoint(**h) for h in hist_raw]
            return ValuationBasisResult(**cached, history=history)

    if override is not None:
        # Skip the LLM — analyst-pinned multiple.
        parsed: dict[str, object] = {
            "multiple": override,
            "rationale": (
                "Analyst-pinned multiple via holdings JSON "
                "`valuation_multiple_override`. Skipping Opus selection."
            ),
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

    current_value, current_pe, history = _compute_series(
        multiple_name, key_metrics, analyst_annual, balance_q, income_q
    )
    hist_values = [h.value for h in history if h.value is not None]
    hist_min = min(hist_values) if hist_values else None
    hist_max = max(hist_values) if hist_values else None
    hist_median = sorted(hist_values)[len(hist_values) // 2] if hist_values else None
    rich_cheap = _rich_cheap_verdict(current_value, hist_median, hist_min, hist_max)
    peg_ratio, peg_growth_pct = _compute_peg(multiple_name, current_value, analyst_annual, income_q)

    result = ValuationBasisResult(
        ticker=ticker,
        multiple_name=multiple_name,
        rationale=_str_or_none(parsed.get("rationale")),
        target_band=_str_or_none(parsed.get("target_band")),
        notes=_str_or_none(parsed.get("notes")),
        current_value=current_value,
        current_value_display=_format_value(current_value, multiple_name),
        current_period_end=current_pe,
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


def load(repo_root: Path, ticker: str) -> ValuationBasisResult | None:
    """Read the cached valuation basis, or None if absent."""
    path = _cache_path(repo_root, ticker)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    history_raw = payload.pop("history", []) or []
    history = [ValuationHistPoint(**h) for h in history_raw]
    return ValuationBasisResult(**payload, history=history)


# ---------------------------------------------------------------------------
# Sources / IO helpers
# ---------------------------------------------------------------------------


def _cache_path(repo_root: Path, ticker: str) -> Path:
    return repo_root / "data" / "valuation_basis" / f"{ticker.upper()}.json"


def _write_cache(path: Path, result: ValuationBasisResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(result), indent=2, default=str), encoding="utf-8")


def _load_quarterly(repo_root: Path, ticker: str, filename: str) -> list[dict[str, object]]:
    """Load a list-shape FMP quarterly endpoint, newest-first ordered."""
    path = repo_root / "data" / "historical" / "fmp" / f"{ticker.upper()}_{filename}"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    rows = [cast("dict[str, object]", r) for r in raw if isinstance(r, dict)]
    rows.sort(key=lambda r: str(r.get("date") or ""), reverse=True)
    return rows


def _load_list(repo_root: Path, ticker: str, filename: str) -> list[dict[str, object]]:
    return _load_quarterly(repo_root, ticker, filename)


def _load_multiple_override(repo_root: Path, ticker: str) -> str | None:
    """Read `valuation_multiple_override` from holdings JSON if present.

    Returns None when the field is missing OR when the value isn't in
    VALUATION_MULTIPLE_CHOICES (caller falls back to LLM selection)."""
    path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    if not path.exists():
        return None
    try:
        payload = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None
    v = payload.get("valuation_multiple_override")
    if isinstance(v, str) and v.strip() in VALUATION_MULTIPLE_CHOICES:
        return v.strip()
    return None


def _load_thesis(repo_root: Path, ticker: str) -> str:
    path = repo_root / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    if not path.exists():
        return ""
    try:
        payload = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return ""
    for key in ("thesis", "thesis_full", "thesis_one_liner"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _load_sector_industry(repo_root: Path, ticker: str) -> tuple[str | None, str | None]:
    """Pull sector/industry from FMP profile.json."""
    path = repo_root / "data" / "historical" / "fmp" / f"{ticker.upper()}_profile.json"
    if not path.exists():
        return (None, None)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return (None, None)
    if isinstance(raw, list):
        raw = raw[0] if raw else {}
    if not isinstance(raw, dict):
        return (None, None)
    r = cast("dict[str, object]", raw)
    sector = r.get("sector")
    industry = r.get("industry")
    return (
        sector.strip() if isinstance(sector, str) and sector.strip() else None,
        industry.strip() if isinstance(industry, str) and industry.strip() else None,
    )


# ---------------------------------------------------------------------------
# Prompt-input formatting
# ---------------------------------------------------------------------------


def _financial_profile_md(
    key_metrics: list[dict[str, object]],
    income_q: list[dict[str, object]],
) -> str:
    """Compact summary of recent quarterly financial shape so the LLM can
    judge which multiple is appropriate. Keep it tight — 1-2 KB max."""
    out: list[str] = []
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
        out.append(f"  - Revenue: ${rev / 1e9:.2f}B" if rev else "  - Revenue: n/a")
        if yoy is not None:
            out.append(f"  - Revenue YoY: {yoy:+.1f}%")
        if op_margin is not None:
            out.append(f"  - Operating margin: {op_margin:.1f}%")
        if net_inc is not None:
            out.append(f"  - Net income: ${net_inc / 1e9:.2f}B")
        if eps is not None:
            out.append(f"  - EPS: ${eps:.2f}")
    if key_metrics:
        latest_km = key_metrics[0]
        ev = _float(latest_km.get("enterpriseValue"))
        mcap = _float(latest_km.get("marketCap"))
        if mcap:
            out.append(f"  - Market cap: ${mcap / 1e9:.1f}B")
        if ev:
            out.append(f"  - Enterprise value: ${ev / 1e9:.1f}B")
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


def _available_estimates_md(annual_estimates: list[dict[str, object]]) -> str:
    """Tell the LLM which NTM lines are computable."""
    if not annual_estimates:
        return "(no analyst estimates on file — NTM multiples not computable)"
    # The endpoint is sorted DESC by date; find the first row whose date is in the future.
    today_iso = date.today().isoformat()
    future = [r for r in annual_estimates if str(r.get("date") or "") > today_iso]
    if not future:
        return "(all analyst estimates are historical — NTM multiples not computable)"
    nxt = future[-1]  # closest forward year
    lines = [f"- NTM (FY ending {nxt.get('date')}): analyst consensus available for:"]
    for label, field_name in (
        ("Revenue", "revenueAvg"),
        ("EBITDA", "ebitdaAvg"),
        ("EBIT", "ebitAvg"),
        ("EPS", "epsAvg"),
        ("Net income", "netIncomeAvg"),
    ):
        v = _float(nxt.get(field_name))
        if v is not None:
            unit = "$" + (
                f"{v / 1e9:.2f}B"
                if abs(v) > 1e9
                else f"{v / 1e6:.0f}M"
                if abs(v) > 1e6
                else f"{v:.2f}"
            )
            if label == "EPS":
                unit = f"${v:.2f}"
            lines.append(f"  - {label}: {unit}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Multiple computation
# ---------------------------------------------------------------------------


def _compute_series(
    multiple_name: str | None,
    key_metrics: list[dict[str, object]],
    analyst_annual: list[dict[str, object]],
    balance_q: list[dict[str, object]] | None = None,
    income_q: list[dict[str, object]] | None = None,
) -> tuple[float | None, str | None, list[ValuationHistPoint]]:
    """Return (current_value, current_period_end_iso, 8Q history)."""
    if multiple_name is None or not key_metrics:
        return (None, None, [])

    if multiple_name in _NTM_MULTIPLES:
        return _compute_ntm_multiple(multiple_name, key_metrics, analyst_annual, income_q)

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


def _compute_ntm_multiple(
    multiple_name: str,
    key_metrics: list[dict[str, object]],
    analyst_annual: list[dict[str, object]],
    income_q: list[dict[str, object]] | None,
) -> tuple[float | None, str | None, list[ValuationHistPoint]]:
    """NTM multiples = current EV (or market cap) / forward-year analyst consensus.

    For historical points: we don't archive prior-period consensus snapshots,
    so we use ACTUALLY-REALIZED 4-quarter-forward figures as the proxy. That
    is, for period Q with date D, "NTM-at-time-of-Q" history = (price-at-Q) /
    (sum of the 4 quarterly EPS values that followed Q in reality). This is
    backward-looking truth, not real-time estimates, but it's a cleaner
    historical anchor than the trailing-LTM proxy (which double-counts old
    earnings).

    Returns (current_value, current_period_end, 12Q history with realized-forward proxy).
    """
    if not key_metrics:
        return (None, None, [])
    latest = key_metrics[0]
    ev = _float(latest.get("enterpriseValue"))
    mcap = _float(latest.get("marketCap"))
    today_iso = date.today().isoformat()
    future = [r for r in analyst_annual if str(r.get("date") or "") > today_iso]
    nxt = future[-1] if future else None

    current_value: float | None = None
    if nxt is not None:
        if multiple_name == "EV/NTM Revenue" and ev:
            rev = _float(nxt.get("revenueAvg"))
            current_value = (ev / rev) if rev and rev > 0 else None
        elif multiple_name == "EV/NTM EBITDA" and ev:
            ebitda = _float(nxt.get("ebitdaAvg"))
            current_value = (ev / ebitda) if ebitda and ebitda > 0 else None
        elif multiple_name == "P/E (NTM)" and mcap:
            ni = _float(nxt.get("netIncomeAvg"))
            if ni and ni > 0:
                current_value = mcap / ni

    # Build per-period income lookup (oldest-first) so we can sum 4 quarters
    # forward from any historical point.
    income_by_date: list[tuple[str, dict[str, object]]] = []
    if income_q:
        income_by_date = sorted(
            ((str(r.get("date") or ""), r) for r in income_q if r.get("date")),
            key=lambda x: x[0],
        )

    # For each of the last 12 key_metrics quarters, compute the "NTM-as-of-then"
    # value using realized 4-quarter-forward financials. When the 4 forward
    # quarters aren't all on file (e.g. for the most recent 3 periods), fall
    # back to the LTM key_metrics multiple.
    ltm_field = {
        "EV/NTM Revenue": "evToSales",
        "EV/NTM EBITDA": "evToEBITDA",
        "P/E (NTM)": "peRatio",
    }[multiple_name]
    history: list[ValuationHistPoint] = []
    for row in key_metrics[:12]:
        pe = str(row.get("date") or "")
        v = _compute_realized_forward_value(multiple_name, row, income_by_date)
        if v is None:
            # Fall back to LTM proxy if forward window isn't available.
            v = _float(row.get(ltm_field))
            if v is not None and v <= 0:
                v = None
        history.append(ValuationHistPoint(period_end=pe, value=v))
    history.reverse()
    current_pe = str(latest.get("date") or "")
    return (current_value, current_pe, history)


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
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if not s:
            return None
        try:
            return float(s)
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
