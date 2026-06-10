"""Customer-driven, three-stream platform valuation for Nu Holdings (NU).

The bank excess-return model treats NU as a maturing credit lender (ROE
compressing, loan-book growth fading, fee/float jammed into a thin "fee % of
book"). But NU's own Managerial P&L splits value creation into THREE streams and
~58% of gross profit is now non-credit:

    Credit Income  (lending spread, capital-intensive)
    Float Income   (the deposit franchise — interest on float, capital-light)
    Fee Income     (interchange, insurance, investments, marketplace, NuCel — capital-light)

So value NU the way it actually compounds — a customer-acquisition + monetization
platform:

    Revenue   = active customers x ARPAC x 12          (user growth x cross-sell)
    Gross profit = Revenue x GP margin, split Credit / Float / Fee (mix shifts to fee)
    Net income   = GP - Opex (operating leverage) - tax
    FCFE         = NI - growth in required capital (only the CREDIT book ties up
                   capital; the float/fee franchises are capital-light, so most of
                   NI is distributable -> the capital-light streams are credited)
    Equity value = PV(FCFE) + PV(terminal FCFE)   @ cost of equity

Primary = FCFE (credits the capital-light non-credit growth). Cross-checks:
residual income (full-retention floor — the bank-model lens) and an exit-P/E on
terminal NI. A reverse-solve shows what customer/ARPAC growth the market price
implies. Base case grounded in the Q4'25 deck; every driver is editable.

Env (like build_bank_dcf.py): DCF_TICKER, DCF_DEST, DCF_REPO_ROOT. Values in $M;
customers in millions, ARPAC in $/month. A Python mirror is the value-of-record
and matches the in-sheet formulas exactly.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, cast

import openpyxl
from openpyxl.styles import Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

REPO = Path(os.environ.get("DCF_REPO_ROOT") or Path(__file__).resolve().parents[1])
T = os.environ.get("DCF_TICKER", "NU")
DEST = Path(os.environ.get("DCF_DEST") or (REPO / "dcf" / f"{T}.xlsx"))

sys.path.insert(0, str(REPO / "src"))
try:  # persistence is best-effort -- the workbook builds without a DB
    from dcf import persist as persist_mod
    from dcf import valuation as valuation_mod
except ImportError:  # pragma: no cover
    persist_mod = None  # type: ignore[assignment]
    valuation_mod = None  # type: ignore[assignment]

YELLOW = PatternFill("solid", fgColor="FFF2CC")
BLUE_FONT = Font(color="1F4E79")
HEAD_FILL = PatternFill("solid", fgColor="1F2937")
HEAD_FONT = Font(color="FFFFFF", bold=True)
SUB_FONT = Font(bold=True, color="374151")
THIN = Side(style="thin", color="D1D5DB")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
PCT = "0.0%"
USD0 = "#,##0"
NUM1 = "0.0"
NUM2 = "0.00"
MULT = '0.0"x"'


@dataclass
class Assum:
    """Base case grounded in NU's Q4'25 deck. All $M; customers in millions,
    ARPAC in $/month. Rates fade linearly from `_near` (Y1) to `_term` (Y_n)."""

    # engine: customers x ARPAC (the user-growth + cross-sell drivers)
    cust0: float = 135.0  # total customers (M), Q1'26
    custg_near: float = 0.09  # customer growth Y1 (Brazil maturing + MX/CO/US compounding)
    custg_term: float = 0.03
    arpac0: float = 14.9  # ARPAC $/mo, calibrated so Y0 revenue ties to the ~$20B run-rate
    arpacg_near: float = 0.08  # ARPAC expansion Y1 (cross-sell deepening, cohort maturation)
    arpacg_term: float = 0.03
    activity: float = 0.83  # monthly-active / total
    # margins (gross profit = revenue x gp margin; opex = revenue x opex %)
    gpm_near: float = 0.392  # Y0 GP $7.84B / revenue $20B
    gpm_term: float = 0.42  # mix shift to high-margin fee/float + operating leverage
    opex_near: float = 0.131  # Y0 opex $2.61B / revenue $20B
    opex_term: float = 0.10  # operating leverage (efficiency improving)
    tax: float = 0.28  # normalized effective tax (Q4'25 ~31.6%; Q1'26 8.7% was a one-off)
    # gross-profit stream mix (Credit / Float; Fee = 1 - Credit - Float). Shows the
    # three streams; credit share declines, fee share rises (cross-sell).
    cr_near: float = 0.42
    cr_term: float = 0.37
    fl_near: float = 0.29
    fl_term: float = 0.30
    # capital: only the credit book ties up capital (fee/float capital-light)
    cb0: float = 31200.0  # credit book Y0 ($31.2B: cards $20.2B + loans $11.0B)
    cbg_near: float = 0.11
    cbg_term: float = 0.05
    cap_ratio: float = 0.12  # required equity / credit book
    # discounting / terminal
    eq0: float = 11330.0  # book equity Y0 ($11.33B, Q4'25) -- for the RI cross-check
    ke: float = 0.125  # cost of equity (LatAm)
    g_term: float = 0.07
    terminal_roe: float = 0.20  # sustainable ROE on RETAINED capital; terminal reinvestment = g/ROE
    exit_pe: float = 16.0  # exit P/E on terminal NI (cross-check)
    years: int = 10
    shares: float = 4907.0  # diluted shares (M)
    price: float = 12.29


def _interp(near: float, term: float, t: int, n: int) -> float:
    return near if n <= 1 else near + (term - near) * (t - 1) / (n - 1)


@dataclass
class Row:
    t: int
    cust: float
    arpac: float
    revenue: float
    gp: float
    gp_credit: float
    gp_float: float
    gp_fee: float
    ni: float
    cb: float
    reqcap: float
    fcfe: float
    equity: float
    ri: float
    df: float


@dataclass
class Mirror:
    rows: list[Row] = field(default_factory=list)
    pv_fcfe: float = 0.0
    pv_tv: float = 0.0
    value_fcfe: float = 0.0
    vps: float = 0.0
    value_ri: float = 0.0
    vps_ri: float = 0.0
    value_pe: float = 0.0
    vps_pe: float = 0.0
    roe_term: float = 0.0


def mirror(s: Assum) -> Mirror:
    ke, n = s.ke, s.years
    m = Mirror()
    cust_p, arpac_p, cb_p = s.cust0, s.arpac0, s.cb0
    reqcap_p = s.cap_ratio * s.cb0
    eq_p = s.eq0
    for t in range(1, n + 1):
        cust = cust_p * (1 + _interp(s.custg_near, s.custg_term, t, n))
        arpac = arpac_p * (1 + _interp(s.arpacg_near, s.arpacg_term, t, n))
        revenue = cust * s.activity * arpac * 12.0
        gp = revenue * _interp(s.gpm_near, s.gpm_term, t, n)
        cr = _interp(s.cr_near, s.cr_term, t, n)
        fl = _interp(s.fl_near, s.fl_term, t, n)
        gp_credit, gp_float, gp_fee = gp * cr, gp * fl, gp * (1 - cr - fl)
        opex = revenue * _interp(s.opex_near, s.opex_term, t, n)
        ni = (gp - opex) * (1 - s.tax)
        cb = cb_p * (1 + _interp(s.cbg_near, s.cbg_term, t, n))
        reqcap = s.cap_ratio * cb
        fcfe = ni - (reqcap - reqcap_p)
        eq = eq_p + ni  # full-retention path (for the residual-income cross-check)
        ri = ni - ke * eq_p
        df = 1 / (1 + ke) ** t
        m.rows.append(
            Row(
                t,
                cust,
                arpac,
                revenue,
                gp,
                gp_credit,
                gp_float,
                gp_fee,
                ni,
                cb,
                reqcap,
                fcfe,
                eq,
                ri,
                df,
            )
        )
        m.pv_fcfe += fcfe * df
        cust_p, arpac_p, cb_p, reqcap_p, eq_p = cust, arpac, cb, reqcap, eq
    last = m.rows[-1]
    df_n = last.df
    # FCFE terminal: sustainable Gordon. A g%-perpetual-growth business earning
    # ROE must reinvest g/ROE of earnings, so terminal FCFE = NI*(1+g)*(1-g/ROE).
    # (FCFE_n*(1+g) over-distributes -> an implausibly rich terminal multiple.)
    m.roe_term = s.terminal_roe
    ni_n1 = last.ni * (1 + s.g_term)
    tv = ni_n1 * (1 - s.g_term / s.terminal_roe) / (ke - s.g_term)
    m.pv_tv = tv * df_n
    m.value_fcfe = m.pv_fcfe + m.pv_tv
    m.vps = m.value_fcfe / s.shares
    # residual-income cross-check (full retention -> conservative floor / bank-model lens)
    pv_ri = sum(r.ri * r.df for r in m.rows)
    ri_roe = last.ni / last.equity  # end-equity ROE on the full-retention path
    cont_ri = (ri_roe - ke) * last.equity / (ke - s.g_term)
    m.value_ri = s.eq0 + pv_ri + cont_ri * df_n
    m.vps_ri = m.value_ri / s.shares
    # exit-P/E cross-check on terminal NI
    m.value_pe = last.ni * s.exit_pe * df_n + m.pv_fcfe
    m.vps_pe = m.value_pe / s.shares
    return m


def load_assumptions(ticker: str) -> Assum:
    """Assum defaults overridden by data/bank_assumptions/<T>_platform.json."""
    s = Assum()
    p = REPO / "data" / "bank_assumptions" / f"{ticker}_platform.json"
    if p.exists():
        try:
            ov: Any = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            ov = {}
        if isinstance(ov, dict):
            for k, v in cast("dict[str, Any]", ov).items():
                if hasattr(s, k) and isinstance(v, (int, float)):
                    setattr(s, k, v)
    prof = REPO / "data" / "historical" / "fmp" / f"{ticker}_profile.json"
    if prof.exists():
        try:
            d: Any = json.loads(prof.read_text(encoding="utf-8"))
            if isinstance(d, list):
                d = d[0] if d else {}
            if isinstance(d, dict) and d.get("price"):
                s.price = float(d["price"])
        except (OSError, json.JSONDecodeError, ValueError, KeyError):
            pass
    return s


# --------------------------------------------------------------------------- #
# workbook
# --------------------------------------------------------------------------- #
def _hdr(ws: Worksheet, cell: str, text: str) -> None:
    ws[cell] = text
    ws[cell].fill = HEAD_FILL
    ws[cell].font = HEAD_FONT


def _inp(ws: Worksheet, row: int, label: str, val: float, fmt: str) -> None:
    ws.cell(row=row, column=1, value=label).font = Font(color="6B7280")
    c = ws.cell(row=row, column=2, value=val)
    c.fill = YELLOW
    c.number_format = fmt
    c.border = BORDER


R = {
    "cust0": 3,
    "custgn": 4,
    "custgt": 5,
    "arpac0": 6,
    "arpacgn": 7,
    "arpacgt": 8,
    "activity": 9,
    "gpmn": 10,
    "gpmt": 11,
    "opexn": 12,
    "opext": 13,
    "tax": 14,
    "crn": 15,
    "crt": 16,
    "fln": 17,
    "flt": 18,
    "cb0": 19,
    "cbgn": 20,
    "cbgt": 21,
    "cap": 22,
    "eq0": 23,
    "ke": 24,
    "g": 25,
    "pe": 26,
    "years": 27,
    "sh": 28,
    "px": 29,
    "troe": 30,
    # outputs
    "val": 33,
    "vps": 34,
    "up": 35,
    "vpsri": 36,
    "vpspe": 37,
    "roet": 38,
}


def build(s: Assum, m: Mirror, dest: Path) -> None:
    wb = openpyxl.Workbook()
    dash = wb.active
    dash.title = "Dashboard"
    mod = wb.create_sheet("Model")
    val = wb.create_sheet("Valuation")
    scn = wb.create_sheet("Scenario")
    D = "Dashboard"

    # ---------- Dashboard ----------
    _hdr(dash, "A1", f"{T} - Customer-Driven Platform DCF | Dashboard")
    dash["A2"] = "Engine: customers x ARPAC -> revenue; Credit/Float/Fee gross profit"
    dash["A2"].font = SUB_FONT
    rows = [
        ("cust0", "Customers Y0 (M)", s.cust0, USD0),
        ("custgn", "Customer growth - near", s.custg_near, PCT),
        ("custgt", "Customer growth - terminal", s.custg_term, PCT),
        ("arpac0", "ARPAC Y0 ($/mo)", s.arpac0, NUM2),
        ("arpacgn", "ARPAC growth - near", s.arpacg_near, PCT),
        ("arpacgt", "ARPAC growth - terminal", s.arpacg_term, PCT),
        ("activity", "Activity rate", s.activity, PCT),
        ("gpmn", "Gross margin - near", s.gpm_near, PCT),
        ("gpmt", "Gross margin - terminal", s.gpm_term, PCT),
        ("opexn", "Opex % rev - near", s.opex_near, PCT),
        ("opext", "Opex % rev - terminal", s.opex_term, PCT),
        ("tax", "Tax rate", s.tax, PCT),
        ("crn", "Credit GP share - near", s.cr_near, PCT),
        ("crt", "Credit GP share - terminal", s.cr_term, PCT),
        ("fln", "Float GP share - near", s.fl_near, PCT),
        ("flt", "Float GP share - terminal", s.fl_term, PCT),
        ("cb0", "Credit book Y0 ($M)", s.cb0, USD0),
        ("cbgn", "Credit book growth - near", s.cbg_near, PCT),
        ("cbgt", "Credit book growth - terminal", s.cbg_term, PCT),
        ("cap", "Capital ratio (req eq / book)", s.cap_ratio, PCT),
        ("eq0", "Book equity Y0 ($M)", s.eq0, USD0),
        ("ke", "Cost of equity Ke", s.ke, PCT),
        ("g", "Terminal growth g", s.g_term, PCT),
        ("pe", "Exit P/E (cross-check)", s.exit_pe, MULT),
        ("years", "Forecast years", s.years, "0"),
        ("sh", "Diluted shares (M)", s.shares, USD0),
        ("px", "Current price ($)", s.price, NUM2),
        ("troe", "Terminal ROE (sustainable)", s.terminal_roe, PCT),
    ]
    for key, lab, v, fmt in rows:
        _inp(dash, R[key], lab, v, fmt)

    _hdr(dash, "A32", "OUTPUT")
    for rr, lab, ref, fmt in (
        (R["val"], "Equity value - FCFE ($M)", "Valuation!$B$8", USD0),
        (R["vps"], "Value per share - FCFE ($)", "Valuation!$B$9", NUM2),
        (R["up"], "Upside vs price", "Valuation!$B$10", PCT),
        (R["vpsri"], "Value/share - residual income", "Valuation!$B$12", NUM2),
        (R["vpspe"], "Value/share - exit P/E", "Valuation!$B$14", NUM2),
        (R["roet"], "Terminal ROE", "Valuation!$B$15", PCT),
    ):
        dash.cell(row=rr, column=1, value=lab).font = SUB_FONT
        oc = dash.cell(row=rr, column=2, value=f"={ref}")
        oc.number_format = fmt
        oc.font = Font(bold=True)
    dash.column_dimensions["A"].width = 34
    dash.column_dimensions["B"].width = 14

    # ---------- Model (formula-first engine) ----------
    _hdr(mod, "A1", f"{T} - Platform engine (formula-first; $M)")
    n = s.years
    col0 = 3  # column C = Y1

    def cl(i: int) -> str:
        return get_column_letter(i)

    labels = {
        4: "Year",
        5: "Customers (M)",
        6: "ARPAC ($/mo)",
        7: "Revenue",
        8: "Gross profit",
        9: "  Credit GP",
        10: "  Float GP",
        11: "  Fee GP",
        12: "Opex",
        13: "Net income",
        14: "Credit book",
        15: "Required capital",
        16: "FCFE",
        17: "Discount factor",
        18: "PV FCFE",
        19: "Book equity (retain)",
        20: "Residual income",
        21: "PV residual income",
    }
    for r, lab in labels.items():
        mod.cell(row=r, column=2, value=lab).font = Font(color="374151") if r != 4 else SUB_FONT

    def dref(key: str) -> str:  # Dashboard absolute cell ref for input `key`
        return f"{D}!$B${R[key]}"

    def interp_f(c: str, near_key: str, term_key: str) -> str:
        near, term = dref(near_key), dref(term_key)
        return f"{near}+({term}-{near})*({c}$4-1)/({dref('years')}-1)"

    for j in range(1, n + 1):
        c = cl(col0 + j - 1)
        p = cl(col0 + j - 2)  # prior column (j==1 references Dashboard Y0 instead)
        mod[f"{c}4"] = j
        if j == 1:
            mod[f"{c}5"] = f"={dref('cust0')}*(1+{interp_f(c, 'custgn', 'custgt')})"
            mod[f"{c}6"] = f"={dref('arpac0')}*(1+{interp_f(c, 'arpacgn', 'arpacgt')})"
            mod[f"{c}14"] = f"={dref('cb0')}*(1+{interp_f(c, 'cbgn', 'cbgt')})"
            mod[f"{c}19"] = f"={dref('eq0')}+{c}13"
            mod[f"{c}16"] = f"={c}13-({c}15-{dref('cap')}*{dref('cb0')})"
            mod[f"{c}20"] = f"={c}13-{dref('ke')}*{dref('eq0')}"
        else:
            mod[f"{c}5"] = f"={p}5*(1+{interp_f(c, 'custgn', 'custgt')})"
            mod[f"{c}6"] = f"={p}6*(1+{interp_f(c, 'arpacgn', 'arpacgt')})"
            mod[f"{c}14"] = f"={p}14*(1+{interp_f(c, 'cbgn', 'cbgt')})"
            mod[f"{c}19"] = f"={p}19+{c}13"
            mod[f"{c}16"] = f"={c}13-({c}15-{p}15)"
            mod[f"{c}20"] = f"={c}13-{dref('ke')}*{p}19"
        mod[f"{c}7"] = f"={c}5*{dref('activity')}*{c}6*12"
        mod[f"{c}8"] = f"={c}7*({interp_f(c, 'gpmn', 'gpmt')})"
        mod[f"{c}9"] = f"={c}8*({interp_f(c, 'crn', 'crt')})"
        mod[f"{c}10"] = f"={c}8*({interp_f(c, 'fln', 'flt')})"
        mod[f"{c}11"] = f"={c}8-{c}9-{c}10"
        mod[f"{c}12"] = f"={c}7*({interp_f(c, 'opexn', 'opext')})"
        mod[f"{c}13"] = f"=({c}8-{c}12)*(1-{dref('tax')})"
        mod[f"{c}15"] = f"={dref('cap')}*{c}14"
        mod[f"{c}17"] = f"=1/(1+{dref('ke')})^{c}4"
        mod[f"{c}18"] = f"={c}16*{c}17"
        mod[f"{c}21"] = f"={c}20*{c}17"
    for r in range(5, 22):
        for j in range(1, n + 1):
            cc = mod.cell(row=r, column=col0 + j - 1)
            cc.number_format = NUM2 if r in (6, 17) else (NUM1 if r == 5 else USD0)
    mod.column_dimensions["B"].width = 20

    # ---------- Valuation ----------
    _hdr(val, "A1", f"{T} - Valuation ($M)")
    cN = cl(col0 + n - 1)
    c1 = cl(col0)
    vrows = [
        ("PV of FCFE (yrs 1-N)", f"=SUM(Model!{c1}18:{cN}18)", USD0, 2),
        (
            "Terminal FCFE (sustainable)",
            f"=Model!{cN}13*(1+{D}!$B${R['g']})*(1-{D}!$B${R['g']}/{D}!$B${R['troe']})/({D}!$B${R['ke']}-{D}!$B${R['g']})",
            USD0,
            3,
        ),
        ("PV of terminal value", f"=B3*Model!{cN}17", USD0, 4),
        ("Terminal NI", f"=Model!{cN}13", USD0, 5),
        ("Terminal customers (M)", f"=Model!{cN}5", NUM1, 6),
        ("Terminal ARPAC ($/mo)", f"=Model!{cN}6", NUM2, 7),
        ("Equity value - FCFE", "=B2+B4", USD0, 8),
        ("Value per share - FCFE ($)", f"=B8/{D}!$B${R['sh']}", NUM2, 9),
        ("Upside vs price", f"=B9/{D}!$B${R['px']}-1", PCT, 10),
        (
            "Residual-income value (floor)",
            f"={D}!$B${R['eq0']}+SUM(Model!{c1}21:{cN}21)+((Model!{cN}13/Model!{cN}19)-{D}!$B${R['ke']})*Model!{cN}19/({D}!$B${R['ke']}-{D}!$B${R['g']})*Model!{cN}17",
            USD0,
            11,
        ),
        ("Value/share - residual income", f"=B11/{D}!$B${R['sh']}", NUM2, 12),
        ("Exit-P/E value", f"=Model!{cN}13*{D}!$B${R['pe']}*Model!{cN}17+B2", USD0, 13),
        ("Value/share - exit P/E", f"=B13/{D}!$B${R['sh']}", NUM2, 14),
        ("Terminal ROE (sustainable)", f"={D}!$B${R['troe']}", PCT, 15),
    ]
    for lab, formula, fmt, rr in vrows:
        val.cell(row=rr, column=1, value=lab).font = (
            SUB_FONT if rr in (8, 9) else Font(color="374151")
        )
        vc = val.cell(row=rr, column=2, value=formula)
        vc.number_format = fmt
        if rr in (8, 9):
            vc.font = Font(bold=True)
    val.column_dimensions["A"].width = 32
    val.column_dimensions["B"].width = 14

    # ---------- Scenario ----------
    _hdr(scn, "A1", "Scenarios & reverse-solve")
    scn["A2"], scn["B2"], scn["C2"], scn["D2"], scn["E2"] = (
        "Scenario",
        "Cust g (near)",
        "ARPAC g (near)",
        "GP margin (term)",
        "Value/share (FCFE)",
    )
    for cc in ("A2", "B2", "C2", "D2", "E2"):
        scn[cc].font = SUB_FONT
    import copy

    scenarios = [
        ("Bear", 0.05, 0.04, 0.38),
        ("Base", s.custg_near, s.arpacg_near, s.gpm_term),
        ("Bull", 0.12, 0.11, 0.45),
    ]
    r = 3
    for name, cg, ag, gm in scenarios:
        s2 = copy.copy(s)
        s2.custg_near, s2.arpacg_near, s2.gpm_term = cg, ag, gm
        v = mirror(s2).vps
        scn.cell(row=r, column=1, value=name).font = Font(bold=(name == "Base"), color="374151")
        for col, vv, f in zip(("B", "C", "D"), (cg, ag, gm), (PCT, PCT, PCT), strict=True):
            cc = scn[f"{col}{r}"]
            cc.value = vv
            cc.number_format = f
        ec = scn.cell(row=r, column=5, value=round(v, 2))
        ec.number_format = NUM2
        ec.font = Font(bold=(name == "Base"))
        r += 1
    r += 1
    _hdr(scn, f"A{r}", "REVERSE-SOLVE - what the price implies")
    r += 1
    implied_eq = s.price * s.shares
    rs = [
        ("Market equity value ($M)", f"{implied_eq:,.0f}"),
        ("Model FCFE equity value ($M)", f"{m.value_fcfe:,.0f}"),
        ("Model implies vs price", f"{m.vps / s.price - 1:+.0%}"),
        ("FCFE floor (resid. income) / sh", f"${m.vps_ri:.2f}"),
        ("Exit-P/E cross-check / sh", f"${m.vps_pe:.2f}"),
    ]
    for lab, txt in rs:
        scn.cell(row=r, column=1, value=lab).font = Font(color="374151")
        scn.cell(row=r, column=2, value=txt).font = Font(size=10, color="374151")
        r += 1
    scn.column_dimensions["A"].width = 36
    scn.column_dimensions["B"].width = 22

    dest.parent.mkdir(parents=True, exist_ok=True)
    wb.save(dest)


def persist_dcf_run(s: Assum, m: Mirror) -> bool:
    db = REPO / "data" / "portfolio.db"
    if persist_mod is None or valuation_mod is None or not db.exists() or not m.vps:
        return False
    # dcf_runs convention (migration 0024): (live - fair) / fair as a DECIMAL ratio.
    over_under = valuation_mod.over_under_pct(s.price, m.vps) if s.price and m.vps > 0 else None
    holdings = REPO / "micro_thesis" / "holdings" / f"{T}.json"
    mos: object = None
    if holdings.exists():
        try:
            mos = json.loads(holdings.read_text(encoding="utf-8")).get("mos_bar")
        except (OSError, json.JSONDecodeError):
            mos = None
    snap = json.dumps(
        {
            "model": "platform_dcf",
            "value_per_share_fcfe": m.vps,
            "value_per_share_residual_income": m.vps_ri,
            "value_per_share_exit_pe": m.vps_pe,
            "equity_value_fcfe_m": m.value_fcfe,
            "terminal_roe": m.roe_term,
            "terminal_customers_m": m.rows[-1].cust,
            "terminal_arpac": m.rows[-1].arpac,
            "ke": s.ke,
            "workbook": str(DEST),
        },
        indent=2,
    )
    row = persist_mod.DcfRunRow(
        ticker=T,
        valuation_date=date.today(),
        horizon_years=s.years,
        wacc=s.ke,
        npv=m.value_fcfe,
        npv_per_share=m.vps,
        shares_outstanding=s.shares * 1e6,
        currency="USD",
        live_price=s.price or None,
        live_price_at=None,
        over_under_pct=over_under,
        mos_bar_used=float(mos) if isinstance(mos, (int, float)) else None,
        assumption_snapshot_json=snap,
        notes=f"workbook={DEST.name} (customer-driven platform DCF)",
    )
    with sqlite3.connect(str(db)) as conn:
        persist_mod.upsert(conn, row)
    return True


def main() -> int:
    s = load_assumptions(T)
    m = mirror(s)
    build(s, m, DEST)
    persisted = persist_dcf_run(s, m) if os.environ.get("DCF_PERSIST", "1") == "1" else False
    up = (m.vps / s.price - 1) if s.price else 0.0
    last = m.rows[-1]
    print(
        f"RESULT\t{T}\tvalue/sh(FCFE)=${m.vps:.2f}\tprice=${s.price:.2f}\tupside={up:+.0%}"
        f"\tRI=${m.vps_ri:.2f}\texitPE=${m.vps_pe:.2f}\tROE_term={m.roe_term:.0%}"
        f"\tdcf_runs={'ok' if persisted else 'skip'}\t-> {DEST}"
    )
    print(f"{'Yr':>2} {'Cust':>6} {'ARPAC':>6} {'Rev':>7} {'GP':>7} {'NI':>7} {'FCFE':>7}")
    for r in m.rows:
        print(
            f"{r.t:>2} {r.cust:>6.0f} {r.arpac:>6.1f} {r.revenue:>7.0f} {r.gp:>7.0f} {r.ni:>7.0f} {r.fcfe:>7.0f}"
        )
    print(
        f"\nTerminal: {last.cust:.0f}M customers x ${last.arpac:.1f} ARPAC -> rev ${last.revenue / 1000:.0f}B, "
        f"NI ${last.ni / 1000:.1f}B, ROE {m.roe_term:.0%}"
    )
    print(
        f"FCFE ${m.vps:.2f} | RI floor ${m.vps_ri:.2f} | exit-PE ${m.vps_pe:.2f}  (vs ${s.price:.2f})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
