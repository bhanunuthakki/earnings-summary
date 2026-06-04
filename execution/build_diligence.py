"""Generate the per-ticker diligence template at micro_thesis/diligence/<TICKER>.md.

Phase 6 P2 entry point. Reads from the canonical corpus (FMP profile, metrics +
ratios views, form_10k risk_factors section) and pre-populates a markdown
template with data the system can fill in: industry snapshot, 5y growth +
margins, capital allocation history, raw 10-K risk factors excerpt, TTM
valuation multiples, and a "your turn" prompts plus a next-steps checklist.

The output is the gate for new-name evaluation: read this to decide if a
watchlist candidate warrants the deeper work of a thesis JSON + DCF
workbook + brief.

Usage:
    python execution/build_diligence.py --ticker AMD
    python execution/build_diligence.py --ticker AMD --force          # overwrite existing
    python execution/build_diligence.py --ticker AMD --repo-root /abs/path
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from io import StringIO
from pathlib import Path
from typing import cast

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    args = _parse_args()
    repo_root = args.repo_root.resolve()
    ticker = args.ticker.upper()

    out_path = repo_root / "micro_thesis" / "diligence" / f"{ticker}.md"
    if out_path.exists() and not args.force:
        sys.stderr.write(
            f"{out_path} already exists. Pass --force to overwrite, or edit in place.\n"
        )
        return 1

    db_path = repo_root / "data" / "portfolio.db"
    if not db_path.exists():
        sys.stderr.write(f"FATAL: no DB at {db_path}\n")
        return 2

    payload = _gather(ticker, repo_root, db_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_render(ticker, payload), encoding="utf-8")
    print(json.dumps({"ticker": ticker, "path": str(out_path)}, indent=2))
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--ticker", required=True, help="Ticker to build diligence template for")
    p.add_argument(
        "--repo-root",
        type=Path,
        default=PROJECT_ROOT,
        help="Repo root containing data/, micro_thesis/. Default: this repo.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing diligence file (default: refuse if file exists)",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Data assembly
# ---------------------------------------------------------------------------


def _gather(ticker: str, repo_root: Path, db_path: Path) -> dict[str, object]:
    """Pull every input the template renders from. Each source is best-effort —
    a missing piece (no FMP profile, no metrics row) becomes an explicit
    "Not available — run X" stub in the rendered output, not a hard failure.
    """
    fmp_dir = repo_root / "data" / "historical" / "fmp"
    profile = _load_profile(fmp_dir, ticker)
    peers = _load_peers(fmp_dir, ticker)
    risks_text, risks_year = _load_risk_factors(fmp_dir, ticker)
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        company_name = _company_name(conn, ticker)
        annual_metrics = _load_annual_rows(conn, ticker, "metrics", n=5)
        annual_ratios = _load_annual_rows(conn, ticker, "ratios", n=5)
        ttm_metrics = _load_ttm_row(conn, ticker, "metrics")
        ttm_ratios = _load_ttm_row(conn, ticker, "ratios")
    return {
        "company_name": company_name,
        "profile": profile,
        "peers": peers,
        "risks_text": risks_text,
        "risks_year": risks_year,
        "annual_metrics": annual_metrics,
        "annual_ratios": annual_ratios,
        "ttm_metrics": ttm_metrics,
        "ttm_ratios": ttm_ratios,
    }


def _company_name(conn: sqlite3.Connection, ticker: str) -> str | None:
    cur = conn.cursor()
    cur.execute("SELECT name FROM tracked_companies WHERE ticker = ? LIMIT 1", (ticker,))
    row = cur.fetchone()
    return str(row["name"]) if row and row["name"] else None


def _load_profile(fmp_dir: Path, ticker: str) -> dict[str, object] | None:
    path = fmp_dir / f"{ticker}_profile.json"
    if not path.exists():
        return None
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return cast("dict[str, object]", payload[0])
    if isinstance(payload, dict):
        return cast("dict[str, object]", payload)
    return None


def _load_peers(fmp_dir: Path, ticker: str) -> list[str]:
    path = fmp_dir / f"{ticker}_peers.json"
    if not path.exists():
        return []
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(payload, list):
        return [str(x) for x in payload if isinstance(x, str)]
    if isinstance(payload, dict):
        peers = payload.get("peersList") or payload.get("peers")
        if isinstance(peers, list):
            return [str(x) for x in peers if isinstance(x, str)]
    return []


def _load_risk_factors(fmp_dir: Path, ticker: str) -> tuple[str, int | None]:
    """Find the latest form_10k_<year>.json and pull its risk_factors text.

    Returns (text, year). Empty string + None if no matching file or no
    risk_factors key. The text is capped to ~12k chars — enough to convey
    the substance without dwarfing the rest of the template.
    """
    candidates = sorted(fmp_dir.glob(f"{ticker}_form_10k_*.json"), reverse=True)
    for path in candidates:
        try:
            payload: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, list) or not payload:
            continue
        rec = payload[0] if isinstance(payload[0], dict) else None
        if rec is None:
            continue
        year = _extract_year_from_filename(path.name)
        rec_dict = cast("dict[str, object]", rec)
        for key in ("item1a", "riskFactors", "risk_factors", "Item 1A"):
            raw = rec_dict.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()[:12_000], year
        break  # Only check the latest file
    return "", None


def _extract_year_from_filename(name: str) -> int | None:
    """e.g. 'META_form_10k_2025.json' → 2025."""
    for token in name.replace(".", "_").split("_"):
        if token.isdigit() and len(token) == 4 and 2000 <= int(token) <= 2099:
            return int(token)
    return None


def _load_annual_rows(
    conn: sqlite3.Connection, ticker: str, table: str, n: int
) -> list[dict[str, object]]:
    cur = conn.cursor()
    cur.execute(
        f"SELECT * FROM {table} WHERE ticker = ? AND fiscal_period_type = 'FY' "
        f"ORDER BY period_end DESC LIMIT ?",
        (ticker, n),
    )
    rows = [dict(r) for r in cur.fetchall()]
    rows.reverse()
    return rows


def _load_ttm_row(conn: sqlite3.Connection, ticker: str, table: str) -> dict[str, object] | None:
    cur = conn.cursor()
    cur.execute(
        f"SELECT * FROM {table} WHERE ticker = ? AND fiscal_period_type = 'TTM' "
        f"ORDER BY period_end DESC LIMIT 1",
        (ticker,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render(ticker: str, payload: dict[str, object]) -> str:
    out = StringIO()
    company = payload.get("company_name") or "—"
    out.write(f"# {ticker} Diligence — {company}\n\n")
    out.write(f"_Generated {date.today().isoformat()}._\n\n")
    out.write(
        "_Source corpus: FMP `profile.json` / `peers.json` / `form_10k_<year>.json`, "
        "and the DB `metrics` + `ratios` views. Empty sections indicate the source "
        "isn't ingested yet — run the relevant fetcher to fill them._\n\n"
    )

    _section_industry(out, ticker, payload)
    _section_growth_margins(out, payload)
    _section_capital_allocation(out, payload)
    _section_risk_factors(out, payload)
    _section_valuation_seed(out, payload)
    _section_next_steps(out, ticker)
    return out.getvalue()


def _section_industry(out: StringIO, ticker: str, payload: dict[str, object]) -> None:
    out.write("## §1 Industry snapshot\n\n")
    profile = payload.get("profile")
    if not isinstance(profile, dict):
        out.write(
            "_FMP profile not yet fetched — run "
            f"`python execution/save_fmp_data.py --ticker {ticker}` to populate._\n\n"
        )
    else:
        sector = profile.get("sector") or "—"
        industry = profile.get("industry") or "—"
        market_cap = profile.get("marketCap") or profile.get("mktCap")
        price = profile.get("price")
        out.write(f"- **Sector**: {sector}\n")
        out.write(f"- **Industry**: {industry}\n")
        if isinstance(market_cap, (int, float)):
            out.write(f"- **Market cap**: ${_compact_usd(float(market_cap))}\n")
        if isinstance(price, (int, float)):
            out.write(f"- **Current price**: ${float(price):,.2f}\n")
        out.write("\n")

    peers = payload.get("peers")
    if isinstance(peers, list) and peers:
        out.write(f"**Peers (FMP):** {', '.join(peers[:10])}\n\n")

    out.write(
        "> _Your turn:_ what's the structural setup of this industry? Cyclical or "
        "secular? Where do moats come from (scale, brand, switching costs, network "
        "effects, IP, regulatory)? Who's the marginal price-setter — incumbents "
        "or new entrants? Where is this name in the industry's lifecycle?\n\n"
    )


def _section_growth_margins(out: StringIO, payload: dict[str, object]) -> None:
    out.write("## §2 Growth + margin trajectory (5y annual)\n\n")
    metrics = payload.get("annual_metrics")
    ratios = payload.get("annual_ratios")
    if not isinstance(metrics, list) or not metrics:
        out.write(
            "_No FY rows in the metrics view — run `extract_facts.py --ticker <T>` "
            "after an FMP statements fetch._\n\n"
        )
        return
    if not isinstance(ratios, list):
        ratios = []
    ratios_by_year = {int(r["fiscal_year"]): r for r in ratios if isinstance(r, dict)}
    out.write("| FY | Revenue (USD M) | YoY | OI margin | FCF margin | ROE |\n")
    out.write("|---|---|---|---|---|---|\n")
    prev_revenue: float | None = None
    for row in metrics:
        if not isinstance(row, dict):
            continue
        year = row.get("fiscal_year")
        revenue = _maybe_float(row.get("revenue"))
        rev_m = revenue / 1_000_000 if revenue is not None else None
        yoy_pct = (revenue / prev_revenue - 1.0) if revenue is not None and prev_revenue else None
        prev_revenue = revenue if revenue is not None else prev_revenue
        ratio_row = ratios_by_year.get(int(year)) if isinstance(year, (int, float)) else None
        op_margin = _maybe_float(ratio_row.get("operating_margin")) if ratio_row else None
        fcf_margin = _maybe_float(ratio_row.get("fcf_margin")) if ratio_row else None
        roe = _maybe_float(ratio_row.get("roe")) if ratio_row else None
        out.write(
            f"| {year} | {_fmt_num(rev_m, 0)} | {_fmt_pct(yoy_pct)} | "
            f"{_fmt_pct(op_margin)} | {_fmt_pct(fcf_margin)} | {_fmt_pct(roe)} |\n"
        )
    out.write("\n")
    out.write(
        "> _Your turn:_ is the growth durable or cyclical-peak? Margin trajectory: "
        "expanding, stable, or compressing — and which line items drive the move "
        "(gross profit, opex leverage, mix)? Where's reinvestment going?\n\n"
    )


def _section_capital_allocation(out: StringIO, payload: dict[str, object]) -> None:
    out.write("## §3 Capital allocation history (5y)\n\n")
    metrics = payload.get("annual_metrics")
    if not isinstance(metrics, list) or not metrics:
        out.write("_No FY rows in metrics view._\n\n")
        return
    out.write("| FY | Capex | Capex / Rev | Dividends paid | Stock repurchased | Total debt |\n")
    out.write("|---|---|---|---|---|---|\n")
    ratios = payload.get("annual_ratios")
    ratios_by_year: dict[int, dict[str, object]] = {}
    if isinstance(ratios, list):
        for r in ratios:
            if isinstance(r, dict) and isinstance(r.get("fiscal_year"), (int, float)):
                ratios_by_year[int(r["fiscal_year"])] = r
    for row in metrics:
        if not isinstance(row, dict):
            continue
        year = row.get("fiscal_year")
        capex = _maybe_float(row.get("capex"))
        revenue = _maybe_float(row.get("revenue"))
        capex_intensity = (
            ratios_by_year.get(int(year), {}).get("capex_intensity")
            if isinstance(year, (int, float))
            else None
        )
        dividends = _maybe_float(row.get("dividends_paid"))
        repurchased = _maybe_float(row.get("stock_repurchased"))
        debt = _maybe_float(row.get("total_debt"))
        capex_m = capex / 1_000_000 if capex is not None else None
        div_m = dividends / 1_000_000 if dividends is not None else None
        rep_m = repurchased / 1_000_000 if repurchased is not None else None
        debt_m = debt / 1_000_000 if debt is not None else None
        ci = _maybe_float(capex_intensity)
        out.write(
            f"| {year} | {_fmt_num(capex_m, 0)} | {_fmt_pct(ci)} | "
            f"{_fmt_num(div_m, 0)} | {_fmt_num(rep_m, 0)} | {_fmt_num(debt_m, 0)} |\n"
        )
    out.write("\n")
    out.write(
        "> _Your turn:_ how does management deploy cash — reinvestment-led, "
        "return-led, or M&A-led? What's the track record on capital decisions over "
        "the cycle? Is debt being used productively or accumulating?\n\n"
    )


def _section_risk_factors(out: StringIO, payload: dict[str, object]) -> None:
    out.write("## §4 Key risks (from latest 10-K, raw excerpt)\n\n")
    text = payload.get("risks_text")
    year = payload.get("risks_year")
    if not isinstance(text, str) or not text:
        out.write(
            "_No `risk_factors` field found in FMP `form_10k_<year>.json` — confirm "
            "the latest 10-K JSON is fetched and parsed._\n\n"
        )
        return
    label = f"FY{year}" if year else "latest 10-K"
    out.write(
        f"_Excerpt from {label} (capped at 12k chars). Read the full filing for context._\n\n"
    )
    out.write("```\n")
    out.write(text)
    out.write("\n```\n\n")
    out.write(
        "> _Your turn:_ which risks here are real vs boilerplate? What's the "
        "bear-case failure mode that's NOT in this list? Which of the listed risks "
        "would actually break the investment thesis (vs cause a multiple compression "
        "that quickly mean-reverts)?\n\n"
    )


def _section_valuation_seed(out: StringIO, payload: dict[str, object]) -> None:
    out.write("## §5 Valuation seed (TTM-based)\n\n")
    profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else None
    ttm = payload.get("ttm_metrics") if isinstance(payload.get("ttm_metrics"), dict) else None
    ttm_ratios = payload.get("ttm_ratios") if isinstance(payload.get("ttm_ratios"), dict) else None
    if profile is None or ttm is None:
        out.write(
            "_Need both FMP profile (for market cap / price) and a TTM metrics row "
            "to compute multiples. Run the standard FMP statements fetch + "
            "`extract_facts.py`._\n\n"
        )
        return
    profile_d = cast("dict[str, object]", profile)
    ttm_d = cast("dict[str, object]", ttm)
    market_cap = _maybe_float(profile_d.get("marketCap") or profile_d.get("mktCap"))
    price = _maybe_float(profile_d.get("price"))
    revenue = _maybe_float(ttm_d.get("revenue"))
    op_income = _maybe_float(ttm_d.get("operating_income"))
    net_income = _maybe_float(ttm_d.get("net_income"))
    fcf = _maybe_float(ttm_d.get("free_cash_flow"))
    eps = _maybe_float(ttm_d.get("eps_diluted"))
    out.write(f"- **Current price**: {('$' + format(price, ',.2f')) if price else '—'}\n")
    if market_cap is not None:
        out.write(f"- **Market cap**: ${_compact_usd(market_cap)}\n")
    if revenue is not None:
        out.write(f"- **Revenue (TTM)**: ${_compact_usd(revenue)}\n")
    out.write("\n")
    out.write("| Multiple | Value |\n|---|---|\n")
    if market_cap is not None and net_income is not None and net_income > 0:
        out.write(f"| P/E (TTM) | {market_cap / net_income:.1f}x |\n")
    if market_cap is not None and fcf is not None and fcf > 0:
        out.write(f"| P/FCF (TTM) | {market_cap / fcf:.1f}x |\n")
    if market_cap is not None and revenue is not None and revenue > 0:
        out.write(f"| Market cap / Revenue (TTM) | {market_cap / revenue:.1f}x |\n")
    if market_cap is not None and op_income is not None and op_income > 0:
        out.write(f"| Market cap / OI (TTM) | {market_cap / op_income:.1f}x |\n")
    if price is not None and eps is not None and eps > 0:
        out.write(f"| P/EPS diluted (TTM) | {price / eps:.1f}x |\n")
    if isinstance(ttm_ratios, dict):
        roe = _maybe_float(ttm_ratios.get("roe"))
        if roe is not None:
            out.write(f"| ROE (TTM) | {roe * 100:.1f}% |\n")
    out.write("\n")
    out.write(
        "> _Your turn:_ at this multiple, what 5y growth + margin profile is the "
        "market pricing in? Refresh the redesigned DCF (`dcf/{ticker}.xlsx`) with "
        "`python execution/refresh_dcf.py --ticker {ticker}` to surface PV/share "
        "+ over-under % in the brief.\n\n"
    )


def _section_next_steps(out: StringIO, ticker: str) -> None:
    out.write("## §6 Next steps\n\n")
    out.write("- [ ] Read latest 10-K end-to-end (longer than the §4 excerpt above)\n")
    out.write(
        f"- [ ] Listen to latest 2 earnings calls (or run "
        f"`fetch_audio_transcripts.py --ticker {ticker} --year YYYY --quarter Q`)\n"
    )
    out.write(
        f"- [ ] Build the DCF at `dcf/{ticker}.xlsx` "
        f"(run `python execution/refresh_dcf.py --ticker {ticker}`)\n"
    )
    out.write(
        f"- [ ] Draft thesis at `micro_thesis/holdings/{ticker}.json` "
        f"(WACC, MoS bar, tier-1 KPIs, break_rules_hard)\n"
    )
    out.write(
        f"- [ ] Pressure-test the thesis: "
        f"`python execution/pressure_test_thesis.py --ticker {ticker}`\n"
    )
    out.write(
        f"- [ ] Check the initiation gate: "
        f"`python execution/check_initiation_gate.py --ticker {ticker}`\n"
    )
    out.write(
        "- [ ] Promote to portfolio when go-criteria are met (DCF MoS ≥ mos_bar, "
        "pressure-tested thesis, risk scan complete)\n\n"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _maybe_float(v: object) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _fmt_num(v: float | None, digits: int) -> str:
    if v is None:
        return "—"
    return f"{v:,.{digits}f}"


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v * 100:+.1f}%"


def _compact_usd(v: float) -> str:
    if abs(v) >= 1e9:
        return f"{v / 1e9:.1f}B"
    if abs(v) >= 1e6:
        return f"{v / 1e6:.0f}M"
    if abs(v) >= 1e3:
        return f"{v / 1e3:.0f}K"
    return f"{v:,.0f}"


if __name__ == "__main__":
    raise SystemExit(main())
