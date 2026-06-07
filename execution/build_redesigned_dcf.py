"""Build a Damodaran/MBI-style, formula-driven DCF workbook for one ticker.

Nine sheets: Cover, Dashboard (the control surface), Color Code, WACC, Model,
Financials, Consensus, Valuation, Monte Carlo. Everything derived is an
Excel/Sheets formula; only blue=hardcoded actuals and yellow=assumptions are
literals (green=cross-sheet link, orange=moved-off-consensus). Default terminal =
exit multiple with perpetuity as a cross-check; terminal method + exit basis are
data-validation dropdowns; the Monte Carlo is a live in-sheet simulation. Per-name
Opus assumptions (data/dcf_assumptions/<T>.json["redesign"], from
dcf_opus_assumptions.py) override the consensus-anchored defaults; names flagged
dcf_applicable=false (banks/insurers/asset-managers) are skipped.

Env-driven so a driver can fan out over tickers:
  DCF_TICKER     ticker to build (default AMZN)
  DCF_DEST       output .xlsx path (default dcf/<T>_redesign.xlsx)
  DCF_NAME       display-name override (default from the FMP profile)
  DCF_REPO_ROOT  repo root holding data/ (default: this file's repo root)
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

REPO = Path(os.environ.get("DCF_REPO_ROOT") or Path(__file__).resolve().parents[1])
FMP = REPO / "data" / "historical" / "fmp"

T = os.environ.get("DCF_TICKER", "AMZN")
_pfd = (
    json.loads((FMP / f"{T}_profile.json").read_text(encoding="utf-8"))
    if (FMP / f"{T}_profile.json").exists()
    else [{}]
)
_pfd = (
    (_pfd[0] if isinstance(_pfd, list) and _pfd else _pfd) if isinstance(_pfd, (list, dict)) else {}
)
NAME = (
    os.environ.get("DCF_NAME") or (_pfd.get("companyName") if isinstance(_pfd, dict) else None) or T
)
DEST = Path(os.environ.get("DCF_DEST", str(REPO / "dcf" / f"{T}_redesign.xlsx")))
QUARTERS = 28  # ~7y of quarterly history
N_ACTUAL_FY = 5  # actual FY columns on the Model
N_FC = 10  # forecast years

# ----------------------------------------------------------------------------- styles
BLUE = Font(color="0000CC")
BLUEB = Font(color="0000CC", bold=True)
GREEN = Font(color="008000")
GREENB = Font(color="008000", bold=True)
BLK = Font()
BOLD = Font(bold=True)
TITLE = Font(bold=True, size=15)
BIG = Font(bold=True, size=16, color="1F4E78")
SUB = Font(italic=True, color="666666")
SEC = Font(bold=True, color="FFFFFF")
SECFILL = PatternFill("solid", fgColor="1F4E78")
YEL = PatternFill("solid", fgColor="FFFF00")
ACTH = PatternFill("solid", fgColor="DDEBF7")
FCH = PatternFill("solid", fgColor="FFF2CC")
RIGHT = Alignment(horizontal="right")
WRAP = Alignment(wrap_text=True, vertical="top")
USD, PCT, MULT, PXS, NUM3 = "#,##0", "0.0%", '0.0"x"', '"$"#,##0.00', "0.000"


def put(ws, r, c, v, *, fmt=None, kind="f", bold=False):
    """kind: act=blue hardcode, in=yellow input, f=formula (auto-green if cross-sheet)."""
    cell = ws.cell(r, c, v)
    if fmt:
        cell.number_format = fmt
    if kind == "act":
        cell.font = BLUEB if bold else BLUE
    elif kind == "in":
        cell.font = BLUE
        cell.fill = YEL
    else:
        link = isinstance(v, str) and v.startswith("=") and "!" in v
        if link:
            cell.font = GREENB if bold else GREEN
        else:
            cell.font = BOLD if bold else BLK
    return cell


def band(ws, r, text, ncol):
    for c in range(1, ncol + 1):
        ws.cell(r, c).fill = SECFILL
    ws.cell(r, 1, "  " + text).font = SEC


def ie(expr):
    """Wrap a ratio/growth formula so a blank/zero denominator shows blank,
    not #DIV/0! (Google Sheets is stricter than the offline engine)."""
    return f'=IFERROR({expr},"")'


# ----------------------------------------------------------------------------- data
def load(stmt):
    p = FMP / f"{T}_{stmt}_quarterly.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


inc, bal, cf = load("income_statement"), load("balance_sheet"), load("cash_flow")


def _loadjson(name):
    p = FMP / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


prod_seg = _loadjson(f"{T}_product_segments_quarterly.json")
geo_seg = _loadjson(f"{T}_geo_segments_quarterly.json")
est = _loadjson(f"{T}_analyst_estimates_annual.json")
prof = _loadjson(f"{T}_profile.json")
prof = prof[0] if isinstance(prof, list) else prof


def idx(records, segmode=False):
    out = {}
    for r in records:
        per = r.get("period")
        try:
            yr = int(r.get("fiscalYear"))
        except (TypeError, ValueError):
            continue
        if isinstance(per, str) and per.startswith("Q"):
            out[(yr, per)] = r.get("data") if segmode else r
    return out


inc_i, bal_i, cf_i = idx(inc), idx(bal), idx(cf)
pseg_i, gseg_i = idx(prod_seg, True), idx(geo_seg, True)
keys = sorted(inc_i, reverse=True)[:QUARTERS]
keys.reverse()  # oldest -> newest
qlabels = [f"{p} {y}" for (y, p) in keys]
NQ = len(keys)

fy_cols = defaultdict(list)
for pos, (y, p) in enumerate(keys):
    fy_cols[y].append(2 + pos)
_full = sorted(y for y, cs in fy_cols.items() if len(cs) == 4)
if not _full:
    # Too little FMP history to anchor a forecast — e.g. a name that IPO'd in the
    # last few quarters, for which FMP returns no/partial quarterly statements
    # (FRVO). Emit a clean SKIP like the dcf_applicable=false path below instead
    # of indexing into an empty list.
    _reason = "no quarterly FMP history" if not keys else "no complete fiscal year yet"
    print(f"SKIP\t{T}\t{_reason}\t(insufficient history for a DCF)")
    raise SystemExit(0)
full_fys = [_full[-1]]  # consecutive run ending at the latest full FY (no gap years)
for _y in reversed(_full[:-1]):
    if _y == full_fys[0] - 1:
        full_fys.insert(0, _y)
    else:
        break
full_fys = full_fys[-N_ACTUAL_FY:]
FC_YEARS = list(range(full_fys[-1] + 1, full_fys[-1] + 1 + N_FC))

# current reporting structure = segments present in the latest quarter (drops
# AMZN's pre-2019 legacy segment names that only populate old columns).
_latest_p = pseg_i.get(keys[-1]) or {}
_latest_g = gseg_i.get(keys[-1]) or {}
PROD = sorted(
    (s for s, v in _latest_p.items() if isinstance(v, (int, float))), key=lambda s: -_latest_p[s]
)
GEO = sorted(
    (s for s, v in _latest_g.items() if isinstance(v, (int, float))), key=lambda s: -_latest_g[s]
)
# Fallback for names without usable FMP product-segment data: model one revenue
# line (the whole company) instead of a per-segment build. Trigger on <2 segments
# in the latest quarter, OR when the reported segments carry no revenue for the
# base fiscal year: FMP sometimes drops segment disclosure for a stretch of years
# (e.g. LITE reports Components/Systems only for older + the newest quarters,
# leaving FY2025 at zero), which would otherwise collapse the whole forecast to 0.
_base_fy = full_fys[-1]
_seg_base_total = sum(
    v
    for p in ("Q1", "Q2", "Q3", "Q4")
    for v in ((pseg_i.get((_base_fy, p)) or {}).get(s) for s in PROD)
    if isinstance(v, (int, float))
)
SINGLE_SEG = len(PROD) < 2 or _seg_base_total <= 0
if SINGLE_SEG:
    PROD = ["Total company"]


def m(v):
    return v / 1e6 if isinstance(v, (int, float)) else None


def fy_sum_raw(records_i, field, y):
    tot = 0.0
    for p in ("Q1", "Q2", "Q3", "Q4"):
        r = records_i.get((y, p))
        v = (r or {}).get(field)
        if isinstance(v, (int, float)):
            tot += v / 1e6
    return tot


ly = full_fys[-1]
rev_ly = fy_sum_raw(inc_i, "revenue", ly)
if rev_ly <= 0:
    # Base-year revenue missing/zero (broken or empty FMP income data): every ratio
    # below divides by it. SKIP cleanly rather than crash through the ratio block.
    print(f"SKIP\t{T}\tbase-year revenue is zero\t(insufficient data for a DCF)")
    raise SystemExit(0)
ratios_ly = {
    "cogs": fy_sum_raw(inc_i, "costOfRevenue", ly) / rev_ly,
    "rnd": fy_sum_raw(inc_i, "researchAndDevelopmentExpenses", ly) / rev_ly,
    "sga": fy_sum_raw(inc_i, "sellingGeneralAndAdministrativeExpenses", ly) / rev_ly,
    "da": fy_sum_raw(cf_i, "depreciationAndAmortization", ly) / rev_ly,
    "sbc": fy_sum_raw(cf_i, "stockBasedCompensation", ly) / rev_ly,
}
oi_ly = fy_sum_raw(inc_i, "operatingIncome", ly)
other_ly = (
    rev_ly
    - fy_sum_raw(inc_i, "costOfRevenue", ly)
    - fy_sum_raw(inc_i, "researchAndDevelopmentExpenses", ly)
    - fy_sum_raw(inc_i, "sellingGeneralAndAdministrativeExpenses", ly)
    - oi_ly
) / rev_ly


def fade(a, b, n=N_FC):
    return [a + (b - a) * i / (n - 1) for i in range(n)]


# product-segment annual actuals (for growth defaults)
seg_ann = defaultdict(lambda: defaultdict(float))
if SINGLE_SEG:
    for y in [full_fys[0] - 1, *full_fys]:
        seg_ann[y]["Total company"] = fy_sum_raw(inc_i, "revenue", y)
else:
    for (y, p), d in pseg_i.items():
        for k, v in (d or {}).items():
            if isinstance(v, (int, float)):
                seg_ann[y][k] += v / 1e6
TAX, EXIT_MULT, TG = 0.24, 12.0, 0.045

# Default assumptions START AT CONSENSUS (user edits from there). Anchor revenue
# to consensus revenueAvg and the margin path to consensus net income (FMP's ebit
# estimate is unreliable for AMZN: it reports NI > EBIT). Beyond the consensus
# horizon, fade growth to a terminal rate and hold the margin.
est_by_year = {}
for e in est:
    try:
        est_by_year[int(str(e.get("date"))[:4])] = e
    except (TypeError, ValueError):
        pass
cons_rev = {
    y: m(est_by_year[y].get("revenueAvg"))
    for y in FC_YEARS
    if (est_by_year.get(y) or {}).get("revenueAvg")
}
cons_ni = {
    y: m(est_by_year[y].get("netIncomeAvg"))
    for y in FC_YEARS
    if (est_by_year.get(y) or {}).get("netIncomeAvg")
}
cons_years = sorted(cons_rev)
ncons = max(2, len(cons_years))


def _term(s):
    return 0.07 if s in ("Amazon Web Services", "Advertising Services") else 0.05


momentum = {}
for s in PROD:
    g0 = (seg_ann[ly][s] / seg_ann[ly - 1][s] - 1) if seg_ann[ly - 1].get(s) else 0.06
    momentum[s] = fade(max(min(g0, 0.30), -0.05), _term(s))

# revenue: per-segment momentum + a uniform delta each consensus year so the
# segment total lands on consensus; fade to terminal beyond the consensus horizon.
seg_growth = {s: [0.0] * N_FC for s in PROD}
seg_prev = {s: seg_ann[ly][s] for s in PROD}
for j, y in enumerate(FC_YEARS):
    if y in cons_rev:
        mg = {s: momentum[s][j] for s in PROD}
        denom = sum(seg_prev.values())
        delta = (
            (cons_rev[y] - sum(seg_prev[s] * (1 + mg[s]) for s in PROD)) / denom if denom else 0.0
        )
        for s in PROD:
            gg = mg[s] + delta
            seg_growth[s][j] = gg
            seg_prev[s] *= 1 + gg
    else:
        steps = max(1, (N_FC - 1) - (ncons - 1))
        for s in PROD:
            g_start = seg_growth[s][ncons - 1] if ncons else momentum[s][j]
            gg = g_start + (_term(s) - g_start) * ((j - (ncons - 1)) / steps if ncons else 1.0)
            seg_growth[s][j] = gg
            seg_prev[s] *= 1 + gg

# margin: COGS/R&D/SG&A held at last-actual %; Other-opex is the plug set so
# NOPAT (= EBIT x (1-tax)) lands on consensus net income each consensus year.
cogs_pct = [ratios_ly["cogs"]] * N_FC
rnd_pct = [ratios_ly["rnd"]] * N_FC
sga_pct = [ratios_ly["sga"]] * N_FC
other_pct = [0.0] * N_FC
oim_list = []
target_oim = oi_ly / rev_ly
for j, y in enumerate(FC_YEARS):
    if y in cons_ni and y in cons_rev:
        target_oim = (cons_ni[y] / (1 - TAX)) / cons_rev[y]
    oim_list.append(target_oim)
    other_pct[j] = max(
        1 - ratios_ly["cogs"] - ratios_ly["rnd"] - ratios_ly["sga"] - target_oim, 0.02
    )
da_pct = fade(ratios_ly["da"], ratios_ly["da"])
sbc_pct = fade(ratios_ly["sbc"], ratios_ly["sbc"] * 0.6)
# Capex anchored to Amazon's announced ~$200B 2026 capex (Jassy, Q4'25 call —
# vs Street's ~$147B), then the capex/D&A ratio converges to ~1.05x as the AI
# build-out matures (Damodaran reinvestment normalization).
# 2026 capex: AMZN guided ~$200B; everyone else defaults to last-year capex +10%.
_capex_ly_M = abs(fy_sum_raw(cf_i, "capitalExpenditure", ly)) or (rev_ly * 0.05)
CAPEX_2026_M = 200_000.0 if T == "AMZN" else _capex_ly_M * 1.10
_da_2026 = (cons_rev.get(FC_YEARS[0], rev_ly) * ratios_ly["da"]) or 1.0
cda0 = CAPEX_2026_M / _da_2026
capex_da = fade(cda0, 1.05)
nwc_pct = [0.005] * N_FC

cache = REPO / "data" / "dcf_assumptions" / f"{T}.json"
narr = json.loads(cache.read_text(encoding="utf-8")).get("narrative", "") if cache.exists() else ""
try:
    con = sqlite3.connect(str(REPO / "data" / "portfolio.db"))
    row = con.execute("SELECT live_price FROM dcf_runs WHERE ticker=?", (T,)).fetchone()
    price = (row[0] if row and row[0] else None) or prof.get("price") or 255.0
    con.close()
except Exception:
    price = prof.get("price") or 255.0

# ----------------------------------------------------------------------------- Monte Carlo
# A reduced-form model (single revenue CAGR, linear margin ramp) calibrated so the
# base case reproduces the full workbook value, then perturbed over Opus-set driver
# distributions. Static snapshot — recomputed each build.
import numpy as np  # noqa: E402

latest = keys[-1]
cash_now = m(bal_i[latest].get("cashAndShortTermInvestments")) or 0.0
debt_now = m(bal_i[latest].get("longTermDebt")) or 0.0
shares_now = m(inc_i[latest].get("weightedAverageShsOutDil")) or 1.0
beta = prof.get("beta") or 1.3
ke = 0.043 + beta * 0.045
mktcap = price * shares_now
we = mktcap / (mktcap + debt_now) if (mktcap + debt_now) else 1.0
wacc0 = we * ke + (1 - we) * 0.045 * (1 - TAX)


# Dashboard control defaults: per-segment growth collapses to 2 points (near-term
# + terminal); margin to 2 points. The workbook interpolates these, so the Python
# mirror interpolates identically — keeping full_value == the in-sheet value.
g1_def = {s: sum(seg_growth[s][:ncons]) / ncons for s in PROD}  # avg over consensus window
gT_def = {s: seg_growth[s][-1] for s in PROD}
margin_near_def, margin_term_def = oim_list[0], oim_list[-1]

# --- Opus per-name override (if the Opus assumption pass has run for this name) ---
OPUS_BASIS, OPUS_METHOD = "EV/EBITDA", "Exit multiple"
CURRENCY = (inc[0].get("reportedCurrency") if inc else None) or "USD"
FX = {
    "USD": 1.0,
    "DKK": 0.145,
    "EUR": 1.08,
    "GBP": 1.27,
    "CAD": 0.73,
    "BRL": 0.18,
    "ILS": 0.27,
    "INR": 0.012,
    "JPY": 0.0067,
    "CHF": 1.12,
    "SEK": 0.095,
}.get(CURRENCY, 1.0)
_opus = (
    json.loads(cache.read_text(encoding="utf-8")).get("redesign") if cache.exists() else None
) or {}
if _opus.get("dcf_applicable") is False:
    print(f"SKIP\t{T}\t{_opus.get('business_model')}\t(FCFF DCF not the right tool)")
    raise SystemExit(0)
if _opus.get("segments"):
    _sg = _opus["segments"]
    g1_def = {s: _sg.get(s, {}).get("near_term_growth", g1_def[s]) for s in PROD}
    gT_def = {s: _sg.get(s, {}).get("terminal_growth", gT_def[s]) for s in PROD}
    margin_near_def = _opus.get("near_term_op_margin", margin_near_def)
    margin_term_def = _opus.get("terminal_op_margin", margin_term_def)
    TAX = _opus.get("tax_rate", TAX)
    EXIT_MULT = float(_opus.get("exit_multiple", EXIT_MULT))
    TG = _opus.get("terminal_growth_g", TG)
    OPUS_BASIS = _opus.get("exit_basis", OPUS_BASIS)
    # Keep exit-multiple as the default method (user preference); Opus's perpetuity
    # pick stays available as the cross-check, not the headline.
    narr = _opus.get("narrative") or narr
    if _opus.get("capex_pct_revenue_2026"):
        CAPEX_2026_M = _opus["capex_pct_revenue_2026"] * (
            cons_rev.get(FC_YEARS[0], rev_ly) or rev_ly
        )
    cda0 = CAPEX_2026_M / _da_2026
    capex_da = fade(cda0, float(_opus.get("terminal_capex_da", 1.05)))


def _seg_g(s, j):  # hold near-term ~3y, then fade to terminal (tracks consensus plateau)
    return g1_def[s] + (gT_def[s] - g1_def[s]) * max(0, j - 2) / (N_FC - 3)


def _oim(j):  # ramp to terminal by the end of the consensus window, then hold
    return margin_near_def + (margin_term_def - margin_near_def) * min(1.0, j / (ncons - 1))


def _project():
    seg = {s: seg_ann[ly][s] for s in PROD}
    prev = sum(seg.values())
    rev, oi, da, vf = [], [], [], []
    for j in range(N_FC):
        for s in PROD:
            seg[s] *= 1 + _seg_g(s, j)
        rr = sum(seg.values())
        ebit = rr * _oim(j)
        nop = ebit * (1 - TAX)
        d, cx, nw = rr * da_pct[j], rr * da_pct[j] * capex_da[j], (rr - prev) * nwc_pct[j]
        rev.append(rr)
        oi.append(ebit)
        da.append(d)
        vf.append(nop + d - cx - nw)
        prev = rr
    return rev, oi, da, vf


rev_p, oi_p, da_p, vf_p = _project()
# mirror the workbook's SELECTED terminal (Opus may pick perpetuity), then FX -> USD
_tmetric = {
    "EV/EBITDA": oi_p[-1] + da_p[-1],
    "EV/Sales": rev_p[-1],
    "EV/EBIT": oi_p[-1],
    "EV/FCF": vf_p[-1],
}.get(OPUS_BASIS, oi_p[-1] + da_p[-1])
_tv_exit = _tmetric * EXIT_MULT
_tv_perp = vf_p[-1] * (1 + TG) / (wacc0 - TG) if wacc0 > TG else _tv_exit
_tv = _tv_perp if OPUS_METHOD == "Perpetuity" else _tv_exit
full_value = (
    (
        sum(vf_p[t] / (1 + wacc0) ** (t + 1) for t in range(N_FC))
        + _tv / (1 + wacc0) ** N_FC
        + cash_now
        - debt_now
    )
    / shares_now
    * FX
)

ann_rev = [fy_sum_raw(inc_i, "revenue", y) for y in full_fys]
ann_g = [ann_rev[i] / ann_rev[i - 1] - 1 for i in range(1, len(ann_rev)) if ann_rev[i - 1]]
ann_oim = []
for _y in full_fys:
    _rev = fy_sum_raw(inc_i, "revenue", _y)
    if _rev:
        ann_oim.append(fy_sum_raw(inc_i, "operatingIncome", _y) / _rev)
cagr0 = (rev_p[-1] / rev_ly) ** (1 / N_FC) - 1
mT0 = oi_p[-1] / rev_p[-1] if rev_p[-1] else margin_term_def
SIG = {
    "cagr": float(min(max(np.std(ann_g) * 0.5, 0.012), 0.03)),
    "margin": float(min(max(np.std(ann_oim), 0.015), 0.03)),
    "wacc": 0.005,
    "exit": 1.5,
}


def _reduced(cagrs, mTs, waccs, exits):
    t = np.arange(1, N_FC + 1)
    R = rev_ly * (1 + cagrs[:, None]) ** t[None, :]
    mm = ann_oim[-1] + (mTs[:, None] - ann_oim[-1]) * t[None, :] / N_FC
    ebit = R * mm
    nop = ebit * (1 - TAX)
    d = R * ratios_ly["da"]
    cda = cda0 + (1.05 - cda0) * t / N_FC
    nw = (R - rev_ly * (1 + cagrs[:, None]) ** (t[None, :] - 1)) * 0.005
    vf = nop + d - d * cda[None, :] - nw
    pv = (vf / (1 + waccs[:, None]) ** t[None, :]).sum(axis=1)
    return (
        pv + (ebit[:, -1] + d[:, -1]) * exits / (1 + waccs) ** N_FC + cash_now - debt_now
    ) / shares_now


base_red = float(
    _reduced(np.array([cagr0]), np.array([mT0]), np.array([wacc0]), np.array([EXIT_MULT]))[0]
)
kcal = full_value / base_red if base_red else 1.0
rng = np.random.default_rng(42)
NMC = 10000
mc_vals = (
    _reduced(
        rng.normal(cagr0, SIG["cagr"], NMC),
        rng.normal(mT0, SIG["margin"], NMC),
        np.clip(rng.normal(wacc0, SIG["wacc"], NMC), 0.05, 0.16),
        np.clip(rng.normal(EXIT_MULT, SIG["exit"], NMC), 5, 24),
    )
    * kcal
)
PCTS = [5, 10, 25, 50, 75, 90, 95]
mc_res = {
    "mean": float(mc_vals.mean()),
    "median": float(np.median(mc_vals)),
    "std": float(mc_vals.std()),
    "min": float(mc_vals.min()),
    "max": float(mc_vals.max()),
    "pcts": {p: float(np.percentile(mc_vals, p)) for p in PCTS},
    "p_under": float((mc_vals > price).mean()),
    "p_up20": float((mc_vals > price * 1.2).mean()),
}
_hc, _he = np.histogram(mc_vals, bins=18)
mc_hist = [(float(_he[i]), float(_he[i + 1]), int(_hc[i])) for i in range(len(_hc))]

# ----------------------------------------------------------------------------- Dashboard
# Single control surface: the ~handful of cells that move the answer live here at
# fixed addresses; Model/WACC/Valuation/MC reference them. Per-segment growth is
# collapsed to 2 inputs (near-term + terminal), interpolated by formula.
SEG_ROW0 = 20  # first segment row on the Dashboard
DB = {
    "g1": lambda i: f"Dashboard!$B${SEG_ROW0 + i}",
    "gT": lambda i: f"Dashboard!$C${SEG_ROW0 + i}",
    "ref": lambda i: f"Dashboard!$D${SEG_ROW0 + i}",
    "margin_near": "Dashboard!$B$29",
    "margin_term": "Dashboard!$B$30",
    "tax": "Dashboard!$B$31",
    "capex26": "Dashboard!$B$34",
    "capex_term_da": "Dashboard!$B$35",
    "rf": "Dashboard!$B$38",
    "erp": "Dashboard!$B$39",
    "beta": "Dashboard!$B$40",
    "kd": "Dashboard!$B$41",
    "method": "Dashboard!$B$43",
    "basis": "Dashboard!$B$44",
    "mult": "Dashboard!$B$45",
    "tg": "Dashboard!$B$46",
    "price": "Dashboard!$B$48",
}

# ----------------------------------------------------------------------------- workbook
wb = openpyxl.Workbook()
wb.remove(wb.active)

# ===== Financials (rich quarterly history + segments + ratios) =====
fs = wb.create_sheet("Financials")
fs.sheet_view.showGridLines = False
fs.column_dimensions["A"].width = 34
for i in range(NQ):
    fs.column_dimensions[get_column_letter(2 + i)].width = 10
put(fs, 1, 1, "Line Item ($M)", bold=True)
for i, lab in enumerate(qlabels):
    put(fs, 1, 2 + i, lab, bold=True).alignment = RIGHT
fs.freeze_panes = "B2"

frow = 2
fin_row = {}
pseg_row = {}


def col(i):
    return get_column_letter(2 + i)


def write_qrow(label, getter, *, kind="act", fmt=USD):
    global frow
    put(fs, frow, 1, label)
    for i, key in enumerate(keys):
        v = getter(i, key)
        if v is not None:
            put(fs, frow, 2 + i, v, fmt=fmt, kind=kind)
    frow += 1
    return frow - 1


def write_yoy(target_row):
    global frow
    put(fs, frow, 1, "    % YoY")
    for i in range(NQ):
        if i >= 4:
            put(fs, frow, 2 + i, ie(f"{col(i)}{target_row}/{col(i - 4)}{target_row}-1"), fmt=PCT)
    frow += 1


def write_pct(target_row, base_row, label="    % of revenue"):
    global frow
    put(fs, frow, 1, label)
    for i in range(NQ):
        put(fs, frow, 2 + i, ie(f"{col(i)}{target_row}/{col(i)}{base_row}"), fmt=PCT)
    frow += 1


band(fs, frow, "INCOME STATEMENT", NQ + 1)
frow += 1
rev_r = write_qrow("Revenue", lambda i, k: m(inc_i.get(k, {}).get("revenue")))
fin_row["Revenue"] = rev_r
write_yoy(rev_r)
for lab, fld in [
    ("Cost of Revenue", "costOfRevenue"),
    ("R&D Expense", "researchAndDevelopmentExpenses"),
    ("SG&A Expense", "sellingGeneralAndAdministrativeExpenses"),
]:
    rr = write_qrow(lab, lambda i, k, f=fld: m(inc_i.get(k, {}).get(f)))
    fin_row[lab] = rr
    write_pct(rr, rev_r)
gp_r = write_qrow("Gross Profit", lambda i, k: m(inc_i.get(k, {}).get("grossProfit")))
write_pct(gp_r, rev_r, "    gross margin")
oi_r = write_qrow("Operating Income", lambda i, k: m(inc_i.get(k, {}).get("operatingIncome")))
fin_row["Operating Income"] = oi_r
write_pct(oi_r, rev_r, "    operating margin")
ni_r = write_qrow("Net Income", lambda i, k: m(inc_i.get(k, {}).get("netIncome")))
fin_row["Net Income"] = ni_r
write_pct(ni_r, rev_r, "    net margin")
fin_row["Diluted Shares (M)"] = write_qrow(
    "Diluted Shares (M)", lambda i, k: m(inc_i.get(k, {}).get("weightedAverageShsOutDil"))
)

if not SINGLE_SEG:
    band(fs, frow, "REVENUE BY SEGMENT — PRODUCT (% YoY, % of revenue)", NQ + 1)
    frow += 1
    for s in PROD:
        rr = write_qrow(s, lambda i, k, seg=s: m((pseg_i.get(k) or {}).get(seg)))
        pseg_row[s] = rr
        write_yoy(rr)
        write_pct(rr, rev_r)

if GEO:
    band(fs, frow, "REVENUE BY SEGMENT — GEOGRAPHY (% YoY, % of revenue)", NQ + 1)
    frow += 1
    for s in GEO:
        rr = write_qrow(s, lambda i, k, seg=s: m((gseg_i.get(k) or {}).get(seg)))
        write_yoy(rr)
        write_pct(rr, rev_r)

band(fs, frow, "BALANCE SHEET", NQ + 1)
frow += 1
for lab, fld in [
    ("Cash & ST Investments", "cashAndShortTermInvestments"),
    ("Total Current Assets", "totalCurrentAssets"),
    ("PP&E (net)", "propertyPlantEquipmentNet"),
    ("Total Assets", "totalAssets"),
    ("Total Current Liabilities", "totalCurrentLiabilities"),
    ("Long-term Debt", "longTermDebt"),
    ("Total Equity", "totalStockholdersEquity"),
]:
    fin_row[lab] = write_qrow(lab, lambda i, k, f=fld: m(bal_i.get(k, {}).get(f)))

band(fs, frow, "CASH FLOW", NQ + 1)
frow += 1
for lab, fld in [
    ("D&A", "depreciationAndAmortization"),
    ("Stock-based Compensation", "stockBasedCompensation"),
    ("Change in Working Capital", "changeInWorkingCapital"),
    ("Operating Cash Flow", "operatingCashFlow"),
    ("Capex", "capitalExpenditure"),
    ("Free Cash Flow", "freeCashFlow"),
]:
    fin_row[lab] = write_qrow(lab, lambda i, k, f=fld: m(cf_i.get(k, {}).get(f)))
put(
    fs, frow + 1, 1, "Blue = hardcoded actuals from FMP filings. Same-sheet ratios are formulas."
).font = SUB
LAST = col(NQ - 1)  # latest quarter column


def fref(label, y):
    cs = sorted(fy_cols[y])
    r = fin_row[label]
    return f"SUM(Financials!{get_column_letter(cs[0])}{r}:{get_column_letter(cs[-1])}{r})"


def pseg_fy(seg, y):
    if SINGLE_SEG:
        return fref("Revenue", y)  # single revenue line = total company
    cs = sorted(fy_cols[y])
    r = pseg_row[seg]
    return f"SUM(Financials!{get_column_letter(cs[0])}{r}:{get_column_letter(cs[-1])}{r})"


# ===== WACC calculator =====
wc = wb.create_sheet("WACC")
wc.sheet_view.showGridLines = False
wc.column_dimensions["A"].width = 34
wc.column_dimensions["B"].width = 14
put(wc, 1, 1, "WACC Calculator", bold=True).font = TITLE
wrows = [
    ("Risk-free rate (10Y)", f"={DB['rf']}", PCT, "f"),
    ("Equity risk premium", f"={DB['erp']}", PCT, "f"),
    ("Beta (levered)", f"={DB['beta']}", NUM3, "f"),
    ("Cost of equity (CAPM)", "=B2+B4*B3", PCT, "f"),
    ("Pre-tax cost of debt", f"={DB['kd']}", PCT, "f"),
    ("Tax rate", f"={DB['tax']}", PCT, "f"),
    ("After-tax cost of debt", "=B6*(1-B7)", PCT, "f"),
    (
        "Market cap (E)",
        f"={DB['price']}*Financials!{LAST}{fin_row['Diluted Shares (M)']}",
        USD,
        "f",
    ),
    ("Total debt (D)", f"=Financials!{LAST}{fin_row['Long-term Debt']}", USD, "f"),
    ("Equity weight", "=B9/(B9+B10)", PCT, "f"),
    ("Debt weight", "=B10/(B9+B10)", PCT, "f"),
]
for i, (lab, v, fmt, kind) in enumerate(wrows):
    put(wc, 2 + i, 1, lab)
    put(wc, 2 + i, 2, v, fmt=fmt, kind=kind)
WACC_ROW = 2 + len(wrows) + 1
put(wc, WACC_ROW, 1, "WACC", bold=True)
put(wc, WACC_ROW, 2, "=B11*B5+B12*B8", fmt=PCT, bold=True)
put(
    wc,
    WACC_ROW + 2,
    1,
    "Yellow = edit. CAPM cost of equity; market-value weights. Feeds Valuation!WACC.",
).font = SUB

# ===== Model =====
md = wb.create_sheet("Model")
md.sheet_view.showGridLines = False
md.column_dimensions["A"].width = 34
ALL = full_fys + FC_YEARS
yc = {y: 2 + i for i, y in enumerate(ALL)}
fcj = {y: i for i, y in enumerate(FC_YEARS)}


def mc(y):
    return get_column_letter(yc[y])


put(md, 1, 1, "$M unless noted", bold=True)
for y in ALL:
    c = put(md, 1, yc[y], f"FY{y}{'A' if y in full_fys else 'E'}", bold=True)
    c.alignment = RIGHT
    c.fill = ACTH if y in full_fys else FCH
    md.column_dimensions[mc(y)].width = 10
md.freeze_panes = "B2"

r = 3
band(
    md,
    r,
    "REVENUE BY SEGMENT (yellow = growth assumption; actuals link to Financials)",
    len(ALL) + 1,
)
r += 1
seg_rev_row = {}
for i, s in enumerate(PROD):
    seg_rev_row[s] = r
    put(md, r, 1, s)
    for y in full_fys:
        put(md, r, yc[y], f"={pseg_fy(s, y)}", fmt=USD)  # green link
    for y in FC_YEARS:
        put(md, r, yc[y], f"={mc(y - 1)}{r}*(1+{mc(y)}{r + 1})", fmt=USD)
    r += 1
    put(md, r, 1, "    growth % (interpolated from Dashboard)")
    for y in full_fys[1:]:
        put(md, r, yc[y], ie(f"{mc(y)}{r - 1}/{mc(y - 1)}{r - 1}-1"), fmt=PCT)
    for y in FC_YEARS:  # hold near-term ~3y then fade to terminal (green link to Dashboard)
        put(
            md,
            r,
            yc[y],
            f"={DB['g1'](i)}+({DB['gT'](i)}-{DB['g1'](i)})*MAX(0,{fcj[y]}-2)/{N_FC - 3}",
            fmt=PCT,
        )
    r += 1
rev_row = r
put(md, r, 1, "Total revenue", bold=True)
for y in ALL:
    cells = "+".join(f"{mc(y)}{seg_rev_row[s]}" for s in PROD)
    put(md, r, yc[y], f"={cells}", fmt=USD, bold=True)
r += 1
put(md, r, 1, "    growth %")
for y in ALL[1:]:
    put(md, r, yc[y], ie(f"{mc(y)}{rev_row}/{mc(y - 1)}{rev_row}-1"), fmt=PCT)
r += 2

band(md, r, "COST STRUCTURE (yellow = % of revenue)", len(ALL) + 1)
r += 1
cost_rows = {}
for lab, fin_lab, ser in [
    ("Cost of revenue", "Cost of Revenue", cogs_pct),
    ("R&D / Technology", "R&D Expense", rnd_pct),
    ("SG&A", "SG&A Expense", sga_pct),
]:
    cost_rows[lab] = r
    put(md, r, 1, lab)
    for y in full_fys:
        put(md, r, yc[y], f"={fref(fin_lab, y)}", fmt=USD)
    for y in FC_YEARS:
        put(md, r, yc[y], f"={mc(y)}{rev_row}*{mc(y)}{r + 1}", fmt=USD)
    r += 1
    put(md, r, 1, "    % of revenue")
    for y in full_fys:
        put(md, r, yc[y], ie(f"{mc(y)}{r - 1}/{mc(y)}{rev_row}"), fmt=PCT)
    for y in FC_YEARS:
        put(md, r, yc[y], ser[fcj[y]], fmt=PCT, kind="in")
    r += 1
other_row = r
put(md, r, 1, "Other opex (fulfilment, etc.)")
for y in full_fys:
    parts = "+".join(f"{mc(y)}{cost_rows[c]}" for c in cost_rows)
    put(md, r, yc[y], f"={mc(y)}{rev_row}-({parts})-{fref('Operating Income', y)}", fmt=USD)
for y in FC_YEARS:  # plug so operating margin = Dashboard near->terminal ramp (green link)
    j = fcj[y]
    parts = "+".join(f"{mc(y)}{cost_rows[c]}" for c in cost_rows)
    ramp = f"({DB['margin_near']}+({DB['margin_term']}-{DB['margin_near']})*MIN(1,{j}/{ncons - 1}))"
    put(md, r, yc[y], f"={mc(y)}{rev_row}*(1-{ramp})-({parts})", fmt=USD)
r += 1
put(md, r, 1, "    % of revenue")
for y in ALL:
    put(md, r, yc[y], ie(f"{mc(y)}{other_row}/{mc(y)}{rev_row}"), fmt=PCT)
r += 2
oi_row = r
put(md, r, 1, "Operating income (EBIT)", bold=True)
for y in ALL:
    parts = "+".join(f"{mc(y)}{cost_rows[c]}" for c in cost_rows)
    put(md, r, yc[y], f"={mc(y)}{rev_row}-({parts})-{mc(y)}{other_row}", fmt=USD, bold=True)
r += 1
put(md, r, 1, "    operating margin")
for y in ALL:
    put(md, r, yc[y], ie(f"{mc(y)}{oi_row}/{mc(y)}{rev_row}"), fmt=PCT)
r += 2

band(md, r, "FCF BRIDGE (NOPAT + D&A + SBC - dNWC - Capex)", len(ALL) + 1)
r += 1
nopat_row = r
put(md, r, 1, "NOPAT = EBIT x (1 - tax)")
for y in ALL:
    put(md, r, yc[y], f"={mc(y)}{oi_row}*(1-Valuation!$B$5)", fmt=USD)
r += 1
da_row = r
put(md, r, 1, "+ D&A")
for y in full_fys:
    put(md, r, yc[y], f"={fref('D&A', y)}", fmt=USD)
for y in FC_YEARS:
    put(md, r, yc[y], f"={mc(y)}{rev_row}*{da_pct[fcj[y]]:.4f}", fmt=USD)
r += 1
sbc_row = r
put(md, r, 1, "+ SBC (non-cash)")
for y in full_fys:
    put(md, r, yc[y], f"={fref('Stock-based Compensation', y)}", fmt=USD)
for y in FC_YEARS:
    put(md, r, yc[y], f"={mc(y)}{rev_row}*{sbc_pct[fcj[y]]:.4f}", fmt=USD)
r += 1
nwc_row = r
put(md, r, 1, "- dNWC")
for y in FC_YEARS:
    put(md, r, yc[y], f"=({mc(y)}{rev_row}-{mc(y - 1)}{rev_row})*{nwc_pct[fcj[y]]:.4f}", fmt=USD)
r += 1
capex_row = r
put(md, r, 1, "- Capex")
for y in full_fys:
    put(md, r, yc[y], f"=-{fref('Capex', y)}", fmt=USD)
for y in FC_YEARS:
    put(md, r, yc[y], f"={mc(y)}{da_row}*{mc(y)}{r + 1}", fmt=USD)
r += 1
put(md, r, 1, "    Capex / D&A  (from Dashboard: $cap'26 -> terminal)")
for y in full_fys:
    put(md, r, yc[y], ie(f"{mc(y)}{capex_row}/{mc(y)}{da_row}"), fmt=MULT)
_da26 = f"{mc(FC_YEARS[0])}{da_row}"  # D&A in the first forecast year
for y in FC_YEARS:  # 2026 ratio = $200B / D&A; fades to the Dashboard terminal ratio
    j = fcj[y]
    r0 = f"({DB['capex26']}/{_da26})"
    put(md, r, yc[y], f"={r0}+({DB['capex_term_da']}-{r0})*{j}/{N_FC - 1}", fmt=MULT)
r += 2
fcff_row = r
put(md, r, 1, "FCFF (firm)", bold=True)
for y in ALL:
    put(
        md,
        r,
        yc[y],
        f"={mc(y)}{nopat_row}+{mc(y)}{da_row}+{mc(y)}{sbc_row}-{mc(y)}{nwc_row}-{mc(y)}{capex_row}",
        fmt=USD,
        bold=True,
    )
r += 1
valfcf_row = r
put(md, r, 1, "Valuation FCF (FCFF - SBC)", bold=True)
for y in ALL:
    put(md, r, yc[y], f"={mc(y)}{fcff_row}-{mc(y)}{sbc_row}", fmt=USD, bold=True)
r += 2

# --- Returns on capital & efficiency (level + incremental) ---
band(
    md,
    r,
    "RETURNS ON CAPITAL & EFFICIENCY  (is new revenue/capital earning its keep?)",
    len(ALL) + 1,
)
r += 1
EQ_F, DB_F, CA_F = (
    fin_row["Total Equity"],
    fin_row["Long-term Debt"],
    fin_row["Cash & ST Investments"],
)


def q4(y):
    return get_column_letter(sorted(fy_cols[y])[-1])


ic_row = r
put(md, r, 1, "Invested capital (debt + equity - cash)")
for y in full_fys:
    put(
        md,
        r,
        yc[y],
        f"=Financials!{q4(y)}{EQ_F}+Financials!{q4(y)}{DB_F}-Financials!{q4(y)}{CA_F}",
        fmt=USD,
    )
for y in FC_YEARS:
    put(
        md,
        r,
        yc[y],
        f"={mc(y - 1)}{ic_row}+{mc(y)}{capex_row}-{mc(y)}{da_row}+{mc(y)}{nwc_row}",
        fmt=USD,
    )
r += 1
eq_row = r
put(md, r, 1, "Book equity (year-end)")
for y in full_fys:
    put(md, r, yc[y], f"=Financials!{q4(y)}{EQ_F}", fmt=USD)
for y in FC_YEARS:
    put(md, r, yc[y], f"={mc(y - 1)}{eq_row}+{mc(y)}{nopat_row}", fmt=USD)
r += 1
roic_row = r
put(md, r, 1, "  ROIC (NOPAT / beg. invested capital)", bold=True)
for y in ALL:
    if (y - 1) in yc:
        put(md, r, yc[y], ie(f"{mc(y)}{nopat_row}/{mc(y - 1)}{ic_row}"), fmt=PCT)
r += 1
put(md, r, 1, "  ROE (earnings / beg. equity)", bold=True)
for y in ALL:
    if (y - 1) in yc:
        earn = fref("Net Income", y) if y in full_fys else f"{mc(y)}{nopat_row}"
        put(md, r, yc[y], ie(f"{earn}/{mc(y - 1)}{eq_row}"), fmt=PCT)
r += 1
put(md, r, 1, "  ROIC - WACC spread (value created)")
for y in ALL:
    if (y - 1) in yc:
        put(md, r, yc[y], ie(f"{mc(y)}{roic_row}-WACC!$B${WACC_ROW}"), fmt=PCT)
r += 1
put(md, r, 1, "  Incremental op margin (ΔEBIT / ΔRev)")
for y in ALL:
    if (y - 1) in yc:
        put(
            md,
            r,
            yc[y],
            ie(f"({mc(y)}{oi_row}-{mc(y - 1)}{oi_row})/({mc(y)}{rev_row}-{mc(y - 1)}{rev_row})"),
            fmt=PCT,
        )
r += 1
put(md, r, 1, "  ROIIC (ΔNOPAT / ΔInvested capital)", bold=True)
for y in ALL:
    if (y - 1) in yc:
        put(
            md,
            r,
            yc[y],
            ie(
                f"({mc(y)}{nopat_row}-{mc(y - 1)}{nopat_row})/({mc(y)}{ic_row}-{mc(y - 1)}{ic_row})"
            ),
            fmt=PCT,
        )
r += 1
put(md, r, 1, "  Sales-to-capital (ΔRev / ΔInvested capital)")
for y in ALL:
    if (y - 1) in yc:
        put(
            md,
            r,
            yc[y],
            ie(f"({mc(y)}{rev_row}-{mc(y - 1)}{rev_row})/({mc(y)}{ic_row}-{mc(y - 1)}{ic_row})"),
            fmt='0.00"x"',
        )
r += 1
put(md, r, 1, "  Reinvestment rate (ΔInvested capital / NOPAT)")
for y in ALL:
    if (y - 1) in yc:
        put(md, r, yc[y], ie(f"({mc(y)}{ic_row}-{mc(y - 1)}{ic_row})/{mc(y)}{nopat_row}"), fmt=PCT)
put(md, r + 2, 1, "Blue/green = actual  ·  Yellow = assumption  ·  Black = formula").font = SUB

# ===== Consensus =====
cs = wb.create_sheet("Consensus")
cs.sheet_view.showGridLines = False
cs.column_dimensions["A"].width = 34
est_by_year = {}
for e in est:
    try:
        est_by_year[int(str(e.get("date"))[:4])] = e
    except (TypeError, ValueError):
        pass
CYEARS = [y for y in FC_YEARS if y in est_by_year][:6]
put(cs, 1, 1, "Full consensus check — model vs Street (FMP estimates, $M)", bold=True).font = TITLE
put(cs, 2, 1, "Fiscal year", bold=True)
for j, y in enumerate(CYEARS):
    put(cs, 2, 2 + j, y, bold=True).alignment = RIGHT
    cs.column_dimensions[get_column_letter(2 + j)].width = 12

DIFMT = "+0.0%;(0.0%)"
shares_ref = f"Financials!{LAST}{fin_row['Diluted Shares (M)']}"
metrics = [
    ("Revenue", "revenueAvg", lambda y: f"=Model!{mc(y)}{rev_row}", USD),
    ("EBITDA*", "ebitdaAvg", lambda y: f"=Model!{mc(y)}{oi_row}+Model!{mc(y)}{da_row}", USD),
    ("EBIT*", "ebitAvg", lambda y: f"=Model!{mc(y)}{oi_row}", USD),
    ("SG&A", "sgaExpenseAvg", lambda y: f"=Model!{mc(y)}{cost_rows['SG&A']}", USD),
    ("Net income (NOPAT)", "netIncomeAvg", lambda y: f"=Model!{mc(y)}{nopat_row}", USD),
    ("EPS", "epsAvg", lambda y: f"=Model!{mc(y)}{nopat_row}/{shares_ref}", PXS),
]
band(cs, 3, "EVERY AVAILABLE FMP CONSENSUS FIELD (consensus / model / % diff)", len(CYEARS) + 1)
r = 4
for lab, fld, mexpr, fmt in metrics:
    put(cs, r, 1, f"{lab} — consensus", bold=True)
    for j, y in enumerate(CYEARS):
        v = m(est_by_year[y].get(fld)) if fmt != PXS else est_by_year[y].get(fld)
        if isinstance(v, (int, float)):
            put(cs, r, 2 + j, v, fmt=fmt, kind="act")
    crow = r
    r += 1
    put(cs, r, 1, "  model")
    for j, y in enumerate(CYEARS):
        put(cs, r, 2 + j, mexpr(y), fmt=fmt)
    r += 1
    put(cs, r, 1, "  vs consensus")
    for j in range(len(CYEARS)):
        cl = get_column_letter(2 + j)
        put(cs, r, 2 + j, ie(f"{cl}{r - 1}/{cl}{crow}-1"), fmt=DIFMT)
    r += 2
put(
    cs,
    r,
    1,
    "Defaults anchored to consensus revenue + net income (≈0% diff). Other rows show where the model sits vs Street.",
).font = SUB
put(
    cs,
    r + 1,
    1,
    "*FMP's EBITDA/EBIT estimates look unreliable for AMZN (it reports NI > EBIT) — don't over-read those two.",
).font = SUB

# ===== Valuation =====
vs = wb.create_sheet("Valuation")
vs.sheet_view.showGridLines = False
vs.column_dimensions["A"].width = 36
vs.column_dimensions["B"].width = 14
for i in range(N_FC):
    vs.column_dimensions[get_column_letter(3 + i)].width = 9
put(vs, 1, 1, f"{NAME} — DCF Valuation", bold=True).font = TITLE
vin = [  # all controlled from the Dashboard (green links); edit there, not here
    ("Current price", f"={DB['price']}", PXS, "f"),
    ("WACC", f"=WACC!B{WACC_ROW}", PCT, "f"),
    ("Terminal growth (g)", f"={DB['tg']}", PCT, "f"),
    ("Tax rate", f"={DB['tax']}", PCT, "f"),
    ("Terminal method", f"={DB['method']}", None, "f"),
    ("Exit basis", f"={DB['basis']}", None, "f"),
    ("Exit multiple", f"={DB['mult']}", MULT, "f"),
]
for i, (lab, v, fmt, kind) in enumerate(vin):
    put(vs, 2 + i, 1, lab)
    put(vs, 2 + i, 2, v, fmt=fmt, kind=kind)
B = {
    "price": "$B$2",
    "wacc": "$B$3",
    "g": "$B$4",
    "tax": "$B$5",
    "method": "$B$6",
    "basis": "$B$7",
    "mult": "$B$8",
}

w0 = 11
put(vs, w0, 1, "Forecast year", bold=True)
for j, y in enumerate(FC_YEARS):
    put(vs, w0, 3 + j, y, bold=True).alignment = RIGHT


def vc(j):
    return get_column_letter(3 + j)


put(vs, w0 + 1, 1, "FCFF ($M)")
for j, y in enumerate(FC_YEARS):
    put(vs, w0 + 1, 3 + j, f"=Model!{mc(y)}{valfcf_row}", fmt=USD)
put(vs, w0 + 2, 1, "PV factor")
for j in range(N_FC):
    put(vs, w0 + 2, 3 + j, f"=1/(1+{B['wacc']})^{j + 1}", fmt=NUM3)
put(vs, w0 + 3, 1, "PV of FCFF ($M)")
for j in range(N_FC):
    put(vs, w0 + 3, 3 + j, f"={vc(j)}{w0 + 1}*{vc(j)}{w0 + 2}", fmt=USD)
sumpv = w0 + 5
put(vs, sumpv, 1, "Sum PV of FCFF", bold=True)
put(vs, sumpv, 2, f"=SUM({vc(0)}{w0 + 3}:{vc(N_FC - 1)}{w0 + 3})", fmt=USD, bold=True)

ty = mc(FC_YEARS[-1])
tr = sumpv + 2
band(vs, tr, "TERMINAL VALUE (exit multiple = default; perpetuity = cross-check)", 3)
put(vs, tr + 1, 1, "Terminal-year FCFF")
put(vs, tr + 1, 2, f"={vc(N_FC - 1)}{w0 + 1}", fmt=USD)
put(vs, tr + 2, 1, "Terminal revenue")
put(vs, tr + 2, 2, f"=Model!{ty}{rev_row}", fmt=USD)
put(vs, tr + 3, 1, "Terminal EBIT")
put(vs, tr + 3, 2, f"=Model!{ty}{oi_row}", fmt=USD)
put(vs, tr + 4, 1, "Terminal EBITDA")
put(vs, tr + 4, 2, f"=Model!{ty}{oi_row}+Model!{ty}{da_row}", fmt=USD)
put(vs, tr + 5, 1, "Terminal metric (per exit basis)")
put(
    vs,
    tr + 5,
    2,
    f'=IF({B["basis"]}="EV/EBITDA",B{tr + 4},IF({B["basis"]}="EV/Sales",B{tr + 2},IF({B["basis"]}="EV/EBIT",B{tr + 3},B{tr + 1})))',
    fmt=USD,
)
put(vs, tr + 6, 1, "TV (exit) = metric x multiple")
put(vs, tr + 6, 2, f"=B{tr + 5}*{B['mult']}", fmt=USD)
put(vs, tr + 7, 1, "PV of TV — exit multiple")
put(vs, tr + 7, 2, f"=B{tr + 6}/(1+{B['wacc']})^{N_FC}", fmt=USD)
put(vs, tr + 8, 1, "TV (perpetuity) = FCFF(1+g)/(WACC-g)")
put(vs, tr + 8, 2, f"=B{tr + 1}*(1+{B['g']})/({B['wacc']}-{B['g']})", fmt=USD)
put(vs, tr + 9, 1, "PV of TV — perpetuity")
put(vs, tr + 9, 2, f"=B{tr + 8}/(1+{B['wacc']})^{N_FC}", fmt=USD)
put(vs, tr + 10, 1, "PV of TV — SELECTED", bold=True)
put(vs, tr + 10, 2, f'=IF({B["method"]}="Perpetuity",B{tr + 9},B{tr + 7})', fmt=USD, bold=True)

br = tr + 12
band(vs, br, "EQUITY BRIDGE -> VALUE / SHARE", 3)
put(vs, br + 1, 1, "Operating value (enterprise value)", bold=True)
put(vs, br + 1, 2, f"=B{sumpv}+B{tr + 10}", fmt=USD, bold=True)
put(vs, br + 2, 1, "+ Cash & ST investments")
put(vs, br + 2, 2, f"=Financials!{LAST}{fin_row['Cash & ST Investments']}", fmt=USD)
put(vs, br + 3, 1, "- Long-term debt")
put(vs, br + 3, 2, f"=-Financials!{LAST}{fin_row['Long-term Debt']}", fmt=USD)
put(vs, br + 4, 1, "Equity value", bold=True)
put(vs, br + 4, 2, f"=B{br + 1}+B{br + 2}+B{br + 3}", fmt=USD, bold=True)
put(vs, br + 5, 1, "Diluted shares (M)")
put(vs, br + 5, 2, f"=Financials!{LAST}{fin_row['Diluted Shares (M)']}", fmt=USD)
vps_row = br + 6
put(vs, vps_row, 1, "VALUE / SHARE", bold=True)
put(
    vs, vps_row, 2, f"=B{br + 4}/B{br + 5}*{FX}", fmt=PXS
).font = BIG  # *FX -> USD (1.0 for USD names)

ck = vps_row + 2
band(vs, ck, "CROSS-CHECKS & RETURN", 3)
put(vs, ck + 1, 1, "Terminal weight (% of EV)")
put(vs, ck + 1, 2, f"=B{tr + 10}/B{br + 1}", fmt=PCT)
put(vs, ck + 2, 1, "Implied perpetuity g (from exit multiple)")
put(vs, ck + 2, 2, f"=(B{tr + 6}*{B['wacc']}-B{tr + 1})/(B{tr + 6}+B{tr + 1})", fmt=PCT)
put(vs, ck + 3, 1, "Implied exit EV/EBITDA (from perpetuity)")
put(vs, ck + 3, 2, f"=B{tr + 8}/B{tr + 4}", fmt=MULT)
put(vs, ck + 4, 1, "Upside / (downside) to fair value")
put(vs, ck + 4, 2, f"=B{vps_row}/{B['price']}-1", fmt="+0%;(0%)")
irr = ck + 6
put(vs, irr, 1, "Owner cashflow / share")
put(vs, irr, 2, f"=-{B['price']}", fmt=PXS)
for j, y in enumerate(FC_YEARS):
    extra = f"+B{vps_row}" if j == N_FC - 1 else ""
    put(vs, irr, 3 + j, f"={vc(j)}{w0 + 1}/B{br + 5}{extra}", fmt="0.00")
put(vs, irr + 1, 1, "IRR (buy at price, hold 10y)", bold=True)
put(vs, irr + 1, 2, f"=IRR(B{irr}:{vc(N_FC - 1)}{irr})", fmt=PCT, bold=True)

# ===== Monte Carlo (LIVE — recalculates in-sheet on every edit) =====
from openpyxl.chart import BarChart, Reference  # noqa: E402

NTRIAL = 1000
HARR = "{1;2;3;4;5;6;7;8;9;10}"  # year vector for the in-cell SUMPRODUCT
mcs = wb.create_sheet("Monte Carlo")
mcs.sheet_view.showGridLines = False
mcs.column_dimensions["A"].width = 30
for ltr in "BCD":
    mcs.column_dimensions[ltr].width = 13
put(mcs, 1, 1, "Monte Carlo simulation (live)", bold=True).font = TITLE
put(
    mcs,
    2,
    1,
    f"{NTRIAL:,} trials, recomputed in-sheet on every edit. Edit the yellow driver distributions to re-roll. Engine grid is to the right (cols H+).",
).font = SUB

band(mcs, 4, "DRIVER DISTRIBUTIONS (yellow = edit; Opus-set defaults)", 4)
for j, h in enumerate(["Driver", "Dist", "Mean", "Std dev"]):
    put(mcs, 5, 1 + j, h, bold=True)
_cagr_link = f"=(Model!{mc(FC_YEARS[-1])}{rev_row}/Model!{mc(full_fys[-1])}{rev_row})^(1/{N_FC})-1"
for i, (lab, mu, sd, fmt) in enumerate(
    [
        ("Revenue 10y CAGR", _cagr_link, SIG["cagr"], PCT),
        ("Terminal operating margin", f"={DB['margin_term']}", SIG["margin"], PCT),
        ("WACC", f"=WACC!B{WACC_ROW}", SIG["wacc"], PCT),
        ("Exit EV/EBITDA multiple", f"={DB['mult']}", SIG["exit"], MULT),
    ]
):
    put(mcs, 6 + i, 1, lab)
    put(mcs, 6 + i, 2, "Normal")
    put(mcs, 6 + i, 3, mu, fmt=fmt, kind="f")  # mean links to model/Dashboard (auto-tracks)
    put(mcs, 6 + i, 4, sd, fmt=fmt, kind="in")  # std stays editable

VR = "$O$2:$O$" + str(NTRIAL + 1)
PR = "Valuation!$B$2"
band(mcs, 11, "SIMULATION OUTPUT — value / share (live)", 4)
for i, (lab, f) in enumerate(
    [
        ("Mean", f"=AVERAGE({VR})"),
        ("Median (P50)", f"=MEDIAN({VR})"),
        ("Std dev", f"=STDEV({VR})"),
        ("Min", f"=MIN({VR})"),
        ("Max", f"=MAX({VR})"),
        ("Current price", f"={PR}"),
        ("Base case (full model)", f"=Valuation!B{vps_row}"),
    ]
):
    put(mcs, 12 + i, 1, lab)
    put(mcs, 12 + i, 2, f, fmt=PXS, bold=(lab == "Median (P50)"))
MINC, MAXC = "$B$15", "$B$16"
MC_PUNDER_ROW = 20
put(mcs, 20, 1, "P(undervalued at current price)", bold=True)
put(mcs, 20, 2, f'=COUNTIF({VR},">"&{PR})/{NTRIAL}', fmt=PCT, bold=True)
put(mcs, 21, 1, "P(>20% upside)")
put(mcs, 21, 2, f'=COUNTIF({VR},">"&{PR}*1.2)/{NTRIAL}', fmt=PCT)
band(mcs, 23, "PERCENTILES (live)", 4)
for i, p in enumerate(PCTS):
    put(mcs, 24 + i, 1, f"P{p}")
    put(mcs, 24 + i, 2, f"=PERCENTILE({VR},{p / 100})", fmt=PXS)
NB, hb = 15, 32
band(mcs, hb, "VALUE DISTRIBUTION (live histogram)", 4)
put(mcs, hb + 1, 1, "Bin midpoint ($/sh)", bold=True)
put(mcs, hb + 1, 2, "Count", bold=True)
for i in range(NB):
    lo = f"({MINC}+{i}*({MAXC}-{MINC})/{NB})"
    hi = f"({MINC}+{i + 1}*({MAXC}-{MINC})/{NB})"
    put(mcs, hb + 2 + i, 1, f"={MINC}+{i + 0.5}*({MAXC}-{MINC})/{NB}", fmt='"$"#,##0')
    put(
        mcs,
        hb + 2 + i,
        2,
        f'=COUNTIFS({VR},">="&{lo},{VR},"<"&{hi})' if i < NB - 1 else f'=COUNTIF({VR},">="&{lo})',
    )
chart = BarChart()
chart.type, chart.legend = "col", None
chart.title = f"Value / share — {NTRIAL:,} trials"
chart.height, chart.width = 8.5, 15
chart.add_data(
    Reference(mcs, min_col=2, min_row=hb + 2, max_row=hb + 1 + NB), titles_from_data=False
)
chart.set_categories(Reference(mcs, min_col=1, min_row=hb + 2, max_row=hb + 1 + NB))
mcs.add_chart(chart, "A50")

# --- engine: constants (H:I) + trial grid (K:O), referenced by the dashboard ---
mcs.column_dimensions["G"].width = 3
mcs.column_dimensions["H"].width = 20
put(mcs, 1, 8, "SIMULATION ENGINE — leave as-is", bold=True)
for i, (lab, v) in enumerate(
    [
        ("R0 (base revenue)", f"=Model!{mc(full_fys[-1])}{rev_row}"),
        ("m0 (base op margin)", f"=Model!{mc(full_fys[-1])}{oi_row + 1}"),
        ("da (D&A / revenue)", ratios_ly["da"]),
        ("cda0 (2026 capex/D&A)", cda0),
        ("nwc (% incr revenue)", 0.005),
        ("tax", "=Valuation!$B$5"),
        ("k (calibration)", kcal),
        ("cash", f"=Financials!{LAST}{fin_row['Cash & ST Investments']}"),
        ("debt", f"=Financials!{LAST}{fin_row['Long-term Debt']}"),
        ("shares", f"=Financials!{LAST}{fin_row['Diluted Shares (M)']}"),
    ]
):
    put(mcs, 2 + i, 8, lab)
    put(mcs, 2 + i, 9, v, fmt=(NUM3 if isinstance(v, float) else None))
for j, h in enumerate(["cagr", "term margin", "wacc", "exit", "value/share"]):
    put(mcs, 1, 11 + j, h, bold=True)


def val_formula(rs):
    return (
        "=$I$8*(SUMPRODUCT(($I$2*(1+K"
        + rs
        + ")^"
        + HARR
        + ")*(($I$3+(L"
        + rs
        + "-$I$3)*"
        + HARR
        + "/10)*(1-$I$7)+$I$4-$I$4*($I$5+(1.05-$I$5)*"
        + HARR
        + "/10)-(K"
        + rs
        + "/(1+K"
        + rs
        + "))*$I$6)/(1+M"
        + rs
        + ")^"
        + HARR
        + ")+$I$2*(1+K"
        + rs
        + ")^10*(L"
        + rs
        + "+$I$4)*N"
        + rs
        + "/(1+M"
        + rs
        + ")^10+$I$9-$I$10)/$I$11"
    )


for rr in range(2, NTRIAL + 2):
    rs = str(rr)
    put(mcs, rr, 11, "=MAX(-0.05,NORMINV(RAND(),$C$6,$D$6))", fmt=PCT)
    put(mcs, rr, 12, "=MAX(0.02,NORMINV(RAND(),$C$7,$D$7))", fmt=PCT)
    put(mcs, rr, 13, "=MIN(0.16,MAX(0.05,NORMINV(RAND(),$C$8,$D$8)))", fmt=PCT)
    put(mcs, rr, 14, "=MIN(24,MAX(5,NORMINV(RAND(),$C$9,$D$9)))", fmt=MULT)
    put(mcs, rr, 15, val_formula(rs), fmt=PXS)
# hide the simulation engine (constants H:I + 1,000-row trial grid K:O)
for ltr in "HIJKLMNO":
    mcs.column_dimensions[ltr].hidden = True

# ===== Dashboard (the front door — all primary controls + sanity checks) =====
from openpyxl.formatting.rule import FormulaRule  # noqa: E402

dsh = wb.create_sheet("Dashboard")
dsh.sheet_view.showGridLines = False
for ltr, w in [("A", 30), ("B", 13), ("C", 13), ("D", 14), ("E", 46)]:
    dsh.column_dimensions[ltr].width = w
put(dsh, 1, 1, f"{NAME} ({T}) — Control Dashboard", bold=True).font = TITLE
put(
    dsh,
    2,
    1,
    "Edit the yellow cells; everything recomputes. Orange = a driver you've moved off consensus.",
).font = SUB

band(dsh, 4, "VERDICT", 5)
put(dsh, 5, 1, "Fair value / share", bold=True)
put(dsh, 5, 2, f"=Valuation!B{vps_row}", fmt=PXS).font = BIG
put(dsh, 6, 1, "Current price")
put(dsh, 6, 2, f"={DB['price']}", fmt=PXS)
put(dsh, 7, 1, "Upside / (downside)")
put(dsh, 7, 2, f"=Valuation!B{ck + 4}", fmt="+0%;(0%)")
put(dsh, 8, 1, "Verdict", bold=True)
put(dsh, 8, 2, f'=IF(Valuation!B{vps_row}>{DB["price"]},"Undervalued","Overvalued")', bold=True)
put(dsh, 9, 1, "Monte Carlo P(undervalued)")
put(dsh, 9, 2, f"='Monte Carlo'!B{MC_PUNDER_ROW}", fmt=PCT)

band(dsh, 11, "GUARDRAIL SIGNALS (are my assumptions sane?)", 5)
for j, h in enumerate(["Signal", "Value", "Flag", "Healthy"]):
    put(dsh, 12, 1 + j, h, bold=True)
roic26 = f"Model!{mc(FC_YEARS[0])}{roic_row}"
tw, ig, pu = f"Valuation!B{ck + 1}", f"Valuation!B{ck + 2}", f"'Monte Carlo'!B{MC_PUNDER_ROW}"
guards = [
    ("Terminal weight (% of EV)", f"={tw}", PCT, f'=IF({tw}>0.9,"! high","ok")', "< 90%"),
    (
        "Implied g vs exit multiple",
        f"={ig}",
        PCT,
        f'=IF({ig}>{DB["tg"]}+0.02,"! rich","ok")',
        "near term g",
    ),
    (
        "ROIC - WACC (2026)",
        f"={roic26}-WACC!B{WACC_ROW}",
        PCT,
        f'=IF({roic26}-WACC!B{WACC_ROW}<0,"! destroys","creates")',
        "> 0",
    ),
    (
        "Revenue vs consensus (2026)",
        "=Consensus!B6",
        PCT,
        '=IF(ABS(Consensus!B6)>0.05,"! off-Street","on")',
        "+/- 5%",
    ),
    ("MC P(undervalued)", f"={pu}", PCT, f'=IF({pu}<0.5,"! coin-flip","ok")', "context"),
]
for i, (lab, vf, fmt, flag, hr) in enumerate(guards):
    put(dsh, 13 + i, 1, lab)
    put(dsh, 13 + i, 2, vf, fmt=fmt)
    put(dsh, 13 + i, 3, flag, bold=True)
    put(dsh, 13 + i, 4, hr).font = SUB

band(dsh, 18, "REVENUE DRIVERS — near-term & terminal growth per segment", 5)
for j, h in enumerate(["Segment", "Near-term", "Terminal", "Consensus", "Thesis claim"]):
    put(dsh, 19, 1 + j, h, bold=True)
seg_notes = {
    "Amazon Web Services": "AI/cloud reaccel + capacity adds",
    "Advertising Services": "ad load + retail media",
    "Online Stores": "share gains, GMV growth",
    "Third-Party Seller Services": "3P mix + fee/ad take",
    "Subscription Services": "Prime price + member adds",
    "Physical Stores": "grocery/format expansion",
    "Other Services": "misc / emerging",
}
for i, s in enumerate(PROD):
    rr = SEG_ROW0 + i
    put(dsh, rr, 1, s)
    put(dsh, rr, 2, g1_def[s], fmt=PCT, kind="in")
    put(dsh, rr, 3, gT_def[s], fmt=PCT, kind="in")
    put(dsh, rr, 4, g1_def[s], fmt=PCT, kind="act")
    put(dsh, rr, 5, seg_notes.get(s, "")).font = SUB

band(dsh, 28, "PROFITABILITY & TAX", 5)
put(dsh, 29, 1, "Near-term operating margin")
put(dsh, 29, 2, margin_near_def, fmt=PCT, kind="in")
put(dsh, 29, 4, margin_near_def, fmt=PCT, kind="act")
put(dsh, 29, 5, "consensus-implied; ramps to terminal").font = SUB
put(dsh, 30, 1, "Terminal operating margin")
put(dsh, 30, 2, margin_term_def, fmt=PCT, kind="in")
put(dsh, 30, 5, "AI/ads mix + operating leverage").font = SUB
put(dsh, 31, 1, "Tax rate")
put(dsh, 31, 2, TAX, fmt=PCT, kind="in")

band(dsh, 33, "CAPITAL INTENSITY", 5)
put(dsh, 34, 1, "2026 capex ($M)")
put(dsh, 34, 2, CAPEX_2026_M, fmt=USD, kind="in")
put(dsh, 34, 4, CAPEX_2026_M, fmt=USD, kind="act")
put(
    dsh,
    34,
    5,
    "Amazon guided ~$200B (AI/datacenters)"
    if T == "AMZN"
    else "~last-year capex +10%; set to guidance",
).font = SUB
put(dsh, 35, 1, "Terminal capex / D&A")
put(dsh, 35, 2, 1.05, fmt=MULT, kind="in")
put(dsh, 35, 5, "converges to ~1.0x as build-out matures").font = SUB

band(dsh, 37, "DISCOUNT RATE & TERMINAL", 5)
put(dsh, 38, 1, "Risk-free rate (10Y)")
put(dsh, 38, 2, 0.043, fmt=PCT, kind="in")
put(dsh, 39, 1, "Equity risk premium")
put(dsh, 39, 2, 0.045, fmt=PCT, kind="in")
put(dsh, 40, 1, "Beta (levered)")
put(dsh, 40, 2, round(prof.get("beta") or 1.3, 3), fmt=NUM3, kind="in")
put(dsh, 40, 5, "FMP raw beta; Damodaran bottom-up ~1.2").font = SUB
put(dsh, 41, 1, "Pre-tax cost of debt")
put(dsh, 41, 2, 0.045, fmt=PCT, kind="in")
put(dsh, 42, 1, "WACC (computed)", bold=True)
put(dsh, 42, 2, f"=WACC!B{WACC_ROW}", fmt=PCT, bold=True)
put(dsh, 43, 1, "Terminal method")
put(dsh, 43, 2, OPUS_METHOD, kind="in")
put(dsh, 44, 1, "Exit basis")
put(dsh, 44, 2, OPUS_BASIS, kind="in")
put(dsh, 45, 1, "Exit multiple")
put(dsh, 45, 2, EXIT_MULT, fmt=MULT, kind="in")
put(dsh, 46, 1, "Terminal growth (g)")
put(dsh, 46, 2, TG, fmt=PCT, kind="in")
put(dsh, 48, 1, "Current price")
put(dsh, 48, 2, price, fmt=PXS, kind="in")

ddm = DataValidation(type="list", formula1='"Perpetuity,Exit multiple"', allow_blank=False)
ddb = DataValidation(type="list", formula1='"EV/EBITDA,EV/Sales,EV/EBIT,EV/FCF"', allow_blank=False)
dsh.add_data_validation(ddm)
dsh.add_data_validation(ddb)
ddm.add("B43")
ddb.add("B44")
ORANGE = PatternFill("solid", fgColor="FFD9A0")
dsh.conditional_formatting.add(
    f"B{SEG_ROW0}:B{SEG_ROW0 + len(PROD) - 1}",
    FormulaRule(formula=[f"$B{SEG_ROW0}<>$D{SEG_ROW0}"], fill=ORANGE),
)
dsh.conditional_formatting.add("B29", FormulaRule(formula=["$B29<>$D29"], fill=ORANGE))
dsh.conditional_formatting.add("B34", FormulaRule(formula=["$B34<>$D34"], fill=ORANGE))

# ===== Color Code =====
cc = wb.create_sheet("Color Code")
cc.sheet_view.showGridLines = False
cc.column_dimensions["A"].width = 16
cc.column_dimensions["B"].width = 74
put(cc, 1, 1, "Color", bold=True)
put(cc, 1, 2, "Meaning", bold=True)
put(cc, 2, 1, "Black")
put(cc, 2, 2, "Formula-driven (same-sheet)")
put(cc, 3, 1, "Blue").font = BLUE
put(cc, 3, 2, "Hard-coded actuals (from FMP filings)")
yc2 = put(cc, 4, 1, "Yellow")
yc2.fill = YEL
yc2.font = BLUE
put(cc, 4, 2, "An assumption you can change — edit these to drive the model")
put(cc, 5, 1, "Green").font = GREEN
put(cc, 5, 2, "A formula that links to another sheet")
oc = put(cc, 6, 1, "Orange")
oc.fill = PatternFill("solid", fgColor="FFD9A0")
put(cc, 6, 2, "(Dashboard) an assumption you've moved off consensus — your active bet")
put(cc, 7, 1, "How to use", bold=True)
put(
    cc,
    7,
    2,
    "Edit yellow cells (Model assumptions, WACC drivers, Valuation toggles). Everything else recomputes.",
)

# ===== Cover =====
cv = wb.create_sheet("Cover")
cv.sheet_view.showGridLines = False
cv.column_dimensions["A"].width = 28
cv.column_dimensions["B"].width = 48
put(cv, 1, 1, f"{NAME} ({T})").font = TITLE
put(cv, 2, 1, "DCF valuation — exit-multiple terminal · defaults anchored to consensus").font = SUB
for i, (k, v) in enumerate(
    [
        ("Last updated", date.today().isoformat()),
        ("Data pulled (FMP)", qlabels[-1]),
        ("Assumptions by", f"Opus 4.8 — {date.today().isoformat()}"),
    ]
):
    put(cv, 4 + i, 1, k, bold=True)
    put(cv, 4 + i, 2, v)
band(cv, 8, "VALUATION SUMMARY", 2)
put(cv, 10, 1, "Fair value / share", bold=True)
put(cv, 10, 2, f"=Valuation!B{vps_row}", fmt=PXS).font = BIG
put(cv, 11, 1, "Current price")
put(cv, 11, 2, "=Valuation!$B$2", fmt=PXS)
put(cv, 12, 1, "Upside / (downside)")
put(cv, 12, 2, f"=Valuation!B{ck + 4}", fmt="+0%;(0%)")
put(cv, 13, 1, "Verdict", bold=True)
put(cv, 13, 2, f'=IF(Valuation!B{vps_row}>Valuation!$B$2,"Undervalued","Overvalued")', bold=True)
put(cv, 14, 1, "Monte Carlo P(undervalued)")
put(cv, 14, 2, f"='Monte Carlo'!B{MC_PUNDER_ROW}", fmt=PCT)
band(cv, 15, "KEY ASSUMPTIONS", 2)
for i, (k, v, fmt) in enumerate(
    [
        ("WACC (from WACC tab)", f"=WACC!B{WACC_ROW}", PCT),
        ("Terminal method", "=Valuation!$B$6", None),
        ("Exit basis / multiple", "=Valuation!$B$7", None),
        ("Exit multiple", "=Valuation!$B$8", MULT),
        ("Terminal growth (g)", "=Valuation!$B$4", PCT),
        ("Terminal weight", f"=Valuation!B{ck + 1}", PCT),
        ("Forecast horizon", f"{N_FC} years", None),
    ]
):
    put(cv, 17 + i, 1, k)
    put(cv, 17 + i, 2, v, fmt=fmt)
band(cv, 25, "THE STORY (Opus)", 2)
put(cv, 27, 1, narr or "(Opus narrative)").alignment = WRAP
cv.merge_cells("A27:B43")

order = [
    "Cover",
    "Dashboard",
    "Color Code",
    "WACC",
    "Model",
    "Financials",
    "Consensus",
    "Valuation",
    "Monte Carlo",
]
for pos, name in enumerate(order):
    wb.move_sheet(name, -(wb.sheetnames.index(name) - pos))

DEST.parent.mkdir(parents=True, exist_ok=True)
wb.save(str(DEST))
_up = (full_value / price - 1) if price else 0.0
_seg = "single" if SINGLE_SEG else str(len(PROD))
print(f"RESULT\t{T}\t{full_value:.2f}\t{price:.2f}\t{_up:+.3f}\t{_seg}\t{DEST}")
print("consensus years:", CYEARS)
