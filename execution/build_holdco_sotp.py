"""Formula-first sum-of-the-parts / NAV model for a capital-allocator holdco
(BN = Brookfield Corporation, first instance).

A holdco that consolidates non-recourse subsidiary debt + large minority interest
can't be valued on consolidated earnings or an FCFF DCF — the value is the NAV of
the parts BN's shareholders actually own. This builds the SOTP, corrected per an
independent Opus review:

  ① Asset management  = BAM total FRE × FRE multiple × BN ownership %
                        (captures fees + BAM's 2/3 of FUTURE carry)
  ② Carried interest  = BN-RETAINED ONLY (100% legacy accrued + 1/3 future),
                        net of the employee pool + a realization haircut, after tax
                        (NEVER × the 73% BAM ratio — that double-counts BAM's 2/3)
  ③ Insurance (BWS)   = BWS distributable earnings × a DE multiple   (NOT a bank
                        ROE-excess-return model — a spread insurer's value isn't ROE-on-equity)
  ④ Invested capital  = listed affiliates at market × ownership + private/RE × (1 − haircut)
  ⑤ − Corporate       = recourse debt + preferred + PV(overhead).  Does NOT subtract
                        the ~$250B non-recourse asset-level debt (it's inside ④/①/③).

  SOTP equity = ① + ② + ③ + ④ − ⑤ ;  ÷ diluted shares (incl. BNT exchangeables).

The holdco discount is an OUTPUT (price-to-NAV gap = the thesis), not an input — only
PV(corporate overhead) is deducted. A scenario block (base / moderate / worst) and a
reverse-solve (what the market implies for carry + private real estate at the current
price) are the most useful artifacts. DE-capitalization is a sanity BAND, not an
independent cross-check (DE already sums the same four buckets).

Env (like build_bank_dcf.py): DCF_TICKER, DCF_DEST, DCF_REPO_ROOT. Values in $B.
A Python value-of-record mirrors the in-sheet formulas exactly (openpyxl can't
evaluate offline) — verify with the `formulas` lib.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import openpyxl
from openpyxl.styles import Border, Font, PatternFill, Side
from openpyxl.worksheet.worksheet import Worksheet

REPO = Path(os.environ.get("DCF_REPO_ROOT") or Path(__file__).resolve().parents[1])
T = os.environ.get("DCF_TICKER", "BN")
DEST = Path(os.environ.get("DCF_DEST") or (REPO / "dcf" / f"{T}.xlsx"))

sys.path.insert(0, str(REPO / "src"))
try:  # persistence is best-effort — the workbook builds without a DB
    from dcf import persist as persist_mod
except ImportError:  # pragma: no cover
    persist_mod = None  # type: ignore[assignment]

YELLOW = PatternFill("solid", fgColor="FFF2CC")
HEAD_FILL = PatternFill("solid", fgColor="1F2937")
HEAD_FONT = Font(color="FFFFFF", bold=True)
SUB_FONT = Font(bold=True, color="374151")
THIN = Side(style="thin", color="D1D5DB")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
USDB = '#,##0.0,"B"'  # value already in $B
USD2 = "0.00"
PCT = "0.0%"
MULT = '0.0"x"'


@dataclass
class Sotp:
    """All inputs in $B unless noted. Defaults seeded to reproduce an independent
    SOTP bracket (worst ~$44 / moderate ~$57 / base ~$70); CALIBRATE against BN's
    quarterly supplemental."""

    # ① asset management (fee business)
    bam_fre: float = 4.0  # BAM total annualized fee-related earnings
    fre_mult: float = 20.0  # peers ~14-24x; BN's lower-fee mix → ~18-22x
    bn_own: float = 0.73  # BN's economic interest in BAM
    # ② carried interest — BN-retained only
    carry_accrued_gross: float = 11.8  # accumulated unrealized (gross, legacy → 100% BN)
    carry_pool: float = 0.22  # employee carry pool deduction
    carry_haircut: float = 0.40  # realization/timing haircut on accrued
    carry_future_annual: float = 2.4  # future carry generated per year (gross)
    carry_bn_future_share: float = 0.333  # BN keeps 1/3 of post-spin carry (BAM gets 2/3)
    carry_future_mult: float = 5.0  # risk-haircut capitalization of BN-share future carry
    carry_tax: float = 0.18  # cash tax on realized carry
    # ③ insurance (Brookfield Wealth Solutions) — DE multiple, NOT a bank model
    bws_de: float = 1.7  # BWS distributable earnings (LTM)
    bws_mult: float = 13.0  # ~12-15x DE (market/mgmt convention)
    # ④ invested capital
    ic_listed: float = 45.0  # BEP/BEPC, BIP/BIPC, BBU at market × BN ownership
    ic_private: float = 35.0  # real estate (BPG) + private at IFRS/appraised
    ic_re_haircut: float = 0.30  # haircut on private/RE (office-heavy RE is the contested line)
    # ⑤ corporate (subtract)
    corp_recourse_debt: float = 12.0  # recourse corporate debt ONLY (not the $250B non-recourse)
    corp_preferred: float = 4.1
    corp_overhead_pv: float = 6.0  # PV of corporate G&A = the real "holdco cost"
    # discount / market
    ke: float = (
        0.10  # blended cost of equity (β-1.85 CAPM ~13-14% is too punitive on the fee annuity)
    )
    shares_m: float = 2367.0  # diluted, incl. BNT exchangeables
    price: float = 45.06
    plan_value: float = 68.0  # management's published plan value/share


def _am(s: Sotp) -> float:
    return s.bam_fre * s.fre_mult * s.bn_own


def _carry(s: Sotp) -> float:
    accrued = s.carry_accrued_gross * (1 - s.carry_pool) * (1 - s.carry_haircut)
    future = (
        s.carry_future_annual * s.carry_bn_future_share * (1 - s.carry_pool) * s.carry_future_mult
    )
    return (accrued + future) * (1 - s.carry_tax)


def _bws(s: Sotp) -> float:
    return s.bws_de * s.bws_mult


def _ic(s: Sotp) -> float:
    return s.ic_listed + s.ic_private * (1 - s.ic_re_haircut)


def _corp(s: Sotp) -> float:
    return s.corp_recourse_debt + s.corp_preferred + s.corp_overhead_pv


def value(s: Sotp) -> tuple[float, float]:
    """(SOTP equity $B, value per share $)."""
    eq = _am(s) + _carry(s) + _bws(s) + _ic(s) - _corp(s)
    return eq, eq * 1000.0 / s.shares_m


def persist_dcf_run(
    eq: float, vps: float, price: float, ke: float, snapshot: dict[str, object]
) -> bool:
    """Best-effort upsert into dcf_runs so the brief's valuation panel reads the
    SOTP value/share. Shape-agnostic (BN or BRK). No-op without the DB / persist module."""
    db = REPO / "data" / "portfolio.db"
    if persist_mod is None or not db.exists() or not vps:
        return False
    over_under = round((vps / price - 1) * 100, 2) if price else None
    holdings = REPO / "micro_thesis" / "holdings" / f"{T}.json"
    mos: object = None
    if holdings.exists():
        try:
            mos = json.loads(holdings.read_text(encoding="utf-8")).get("mos_bar")
        except (OSError, json.JSONDecodeError):
            mos = None
    row = persist_mod.DcfRunRow(
        ticker=T,
        valuation_date=date.today(),
        horizon_years=0,
        wacc=ke,
        npv=eq * 1000.0,
        npv_per_share=vps,
        shares_outstanding=eq * 1e9 / vps,  # back out shares from equity/value-per-share
        currency="USD",
        live_price=price or None,
        live_price_at=None,
        over_under_pct=over_under,
        mos_bar_used=float(mos) if isinstance(mos, (int, float)) else None,
        assumption_snapshot_json=json.dumps({**snapshot, "workbook": str(DEST)}, indent=2),
        notes=f"workbook={DEST.name} ({snapshot.get('model', 'holdco SOTP')})",
    )
    with sqlite3.connect(str(db)) as conn:
        persist_mod.upsert(conn, row)
    return True


def _run_bn() -> int:
    s = _load(T)
    eq, vps = value(s)
    build(s, DEST)
    snap: dict[str, object] = {
        "model": "holdco_sotp",
        "ke": s.ke,
        "sotp_equity_b": eq,
        "value_per_share_usd": vps,
        "plan_value": s.plan_value,
        "asset_mgmt_b": _am(s),
        "carry_b": _carry(s),
        "insurance_b": _bws(s),
        "invested_capital_b": _ic(s),
        "corporate_b": -_corp(s),
    }
    persisted = persist_dcf_run(eq, vps, s.price, s.ke, snap)
    # scenarios
    base = _scn(s, carry_haircut=0.0, ic_re_haircut=0.0)
    worst = _scn(s, carry_zero=True, ic_private=0.0)
    # reverse-solve: what the market implies for carry + private RE at the price
    implied_eq = s.price * s.shares_m / 1000.0
    floor = _am(s) + _bws(s) + s.ic_listed - _corp(s)  # AM + BWS + listed only − corp
    implied_carry_re = implied_eq - floor
    model_carry_re = _carry(s) + s.ic_private * (1 - s.ic_re_haircut)
    print(
        f"RESULT\t{T}\tSOTP/sh=${vps:.2f}\tprice=${s.price:.2f}\tupside={vps / s.price - 1:+.0%}"
        f"\tvs plan ${s.plan_value:.0f}={vps / s.plan_value - 1:+.0%}"
        f"\tdcf_runs={'ok' if persisted else 'skip'}\t-> {DEST}"
    )
    print(f"  (1) Asset mgmt (FRE x{s.fre_mult:.0f} x {s.bn_own:.0%})... ${_am(s):6.1f}B")
    print(f"  (2) Carry (BN-retained, net pool/haircut/tax) ${_carry(s):6.1f}B")
    print(f"  (3) Insurance BWS (DE x{s.bws_mult:.0f})............ ${_bws(s):6.1f}B")
    print(f"  (4) Invested capital (listed+private-haircut). ${_ic(s):6.1f}B")
    print(f"  (5) - Corporate (recourse debt+pref+overhead). ${-_corp(s):6.1f}B")
    print(f"  = SOTP equity ................................ ${eq:6.1f}B  -> ${vps:.2f}/sh")
    print(
        f"\n  Scenarios: worst ${worst:.2f} (carry+RE=0) | moderate ${vps:.2f} | base ${base:.2f}"
    )
    print(
        f"  REVERSE-SOLVE: at ${s.price:.2f}, market implies ${implied_carry_re:.1f}B for "
        f"carry+private-RE vs ${model_carry_re:.1f}B modeled -- the thesis is in that gap."
    )
    return 0


def _scn(s: Sotp, **over: object) -> float:
    import copy

    s2 = copy.copy(s)
    carry_zero = bool(over.pop("carry_zero", False))
    for k, v in over.items():
        setattr(s2, k, v)
    if carry_zero:
        s2.carry_accrued_gross = 0.0
        s2.carry_future_annual = 0.0
    return value(s2)[1]


def _load(ticker: str) -> Sotp:
    """Seed price/shares from FMP if present; everything else uses calibrated defaults."""
    s = Sotp()
    prof = REPO / "data" / "historical" / "fmp" / f"{ticker}_profile.json"
    if prof.exists():
        try:
            d = json.loads(prof.read_text(encoding="utf-8"))
            if isinstance(d, list):
                d = d[0] if d else {}
            if isinstance(d, dict) and d.get("price"):
                s.price = float(d["price"])
        except (OSError, json.JSONDecodeError, ValueError, KeyError):
            pass
    return s


# --------------------------------------------------------------------------- #
def _hdr(ws: Worksheet, cell: str, text: str) -> None:
    ws[cell] = text
    ws[cell].fill = HEAD_FILL
    ws[cell].font = HEAD_FONT


def _inp(ws: Worksheet, row: int, label: str, val: float, fmt: str = USDB) -> None:
    ws.cell(row=row, column=1, value=label).font = Font(color="6B7280")
    c = ws.cell(row=row, column=2, value=val)
    c.fill = YELLOW
    c.number_format = fmt
    c.border = BORDER


# Dashboard input rows (canonical cells the SOTP sheet references)
R = {
    "bam_fre": 3,
    "fre_mult": 4,
    "bn_own": 5,
    "c_accr": 7,
    "c_pool": 8,
    "c_hair": 9,
    "c_fut": 10,
    "c_futshr": 11,
    "c_futmult": 12,
    "c_tax": 13,
    "bws_de": 15,
    "bws_mult": 16,
    "ic_listed": 18,
    "ic_priv": 19,
    "ic_hair": 20,
    "corp_debt": 22,
    "corp_pref": 23,
    "corp_oh": 24,
    "ke": 26,
    "shares": 27,
    "price": 28,
    "plan": 29,
}


def build(s: Sotp, dest: Path) -> None:
    wb = openpyxl.Workbook()
    dash = wb.active
    dash.title = "Dashboard"
    sotp = wb.create_sheet("SOTP")
    scen = wb.create_sheet("Scenarios")
    D = "Dashboard"

    _hdr(dash, "A1", f"{T} — Holdco Sum-of-the-Parts · Dashboard ($B)")
    dash["A2"] = "① Asset management (fee business)"
    dash["A2"].font = SUB_FONT
    _inp(dash, R["bam_fre"], "BAM total FRE", s.bam_fre)
    _inp(dash, R["fre_mult"], "FRE multiple", s.fre_mult, MULT)
    _inp(dash, R["bn_own"], "BN ownership of BAM", s.bn_own, PCT)
    dash["A6"] = "② Carried interest — BN-RETAINED ONLY (never ×73%)"
    dash["A6"].font = SUB_FONT
    _inp(dash, R["c_accr"], "Accrued carry (gross, legacy=100% BN)", s.carry_accrued_gross)
    _inp(dash, R["c_pool"], "Employee carry pool", s.carry_pool, PCT)
    _inp(dash, R["c_hair"], "Realization/timing haircut", s.carry_haircut, PCT)
    _inp(dash, R["c_fut"], "Future carry / yr (gross)", s.carry_future_annual)
    _inp(dash, R["c_futshr"], "BN share of future carry (1/3)", s.carry_bn_future_share, PCT)
    _inp(dash, R["c_futmult"], "Future-carry capitalization", s.carry_future_mult, MULT)
    _inp(dash, R["c_tax"], "Cash tax on carry", s.carry_tax, PCT)
    dash["A14"] = "③ Insurance (BWS) — DE multiple"
    dash["A14"].font = SUB_FONT
    _inp(dash, R["bws_de"], "BWS distributable earnings", s.bws_de)
    _inp(dash, R["bws_mult"], "BWS DE multiple", s.bws_mult, MULT)
    dash["A17"] = "④ Invested capital"
    dash["A17"].font = SUB_FONT
    _inp(dash, R["ic_listed"], "Listed affiliates @ market × own", s.ic_listed)
    _inp(dash, R["ic_priv"], "Private + real estate (IFRS)", s.ic_private)
    _inp(dash, R["ic_hair"], "Private/RE haircut", s.ic_re_haircut, PCT)
    dash["A21"] = "⑤ Corporate (subtract)"
    dash["A21"].font = SUB_FONT
    _inp(dash, R["corp_debt"], "Recourse corporate debt", s.corp_recourse_debt)
    _inp(dash, R["corp_pref"], "Preferred equity", s.corp_preferred)
    _inp(dash, R["corp_oh"], "PV corporate overhead", s.corp_overhead_pv)
    dash["A25"] = "Discount / market"
    dash["A25"].font = SUB_FONT
    _inp(dash, R["ke"], "Blended cost of equity Ke", s.ke, PCT)
    _inp(dash, R["shares"], "Diluted shares (M, incl. exchangeables)", s.shares_m, "#,##0")
    _inp(dash, R["price"], "Current price ($)", s.price, USD2)
    _inp(dash, R["plan"], "Management plan value ($)", s.plan_value, USD2)
    dash.column_dimensions["A"].width = 40
    dash.column_dimensions["B"].width = 13

    # ---- SOTP build (formula-first off the Dashboard inputs) ----
    _hdr(sotp, "A1", f"{T} — Sum-of-the-Parts build ($B)")

    def b(r: int) -> str:
        return f"{D}!$B${r}"

    rows = [
        ("① Asset management", f"={b(R['bam_fre'])}*{b(R['fre_mult'])}*{b(R['bn_own'])}"),
        (
            "② Carried interest (BN-retained)",
            f"=({b(R['c_accr'])}*(1-{b(R['c_pool'])})*(1-{b(R['c_hair'])})"
            f"+{b(R['c_fut'])}*{b(R['c_futshr'])}*(1-{b(R['c_pool'])})*{b(R['c_futmult'])})*(1-{b(R['c_tax'])})",
        ),
        ("③ Insurance (BWS)", f"={b(R['bws_de'])}*{b(R['bws_mult'])}"),
        ("④ Invested capital", f"={b(R['ic_listed'])}+{b(R['ic_priv'])}*(1-{b(R['ic_hair'])})"),
        ("⑤ − Corporate", f"=-({b(R['corp_debt'])}+{b(R['corp_pref'])}+{b(R['corp_oh'])})"),
        ("= SOTP equity value", "=SUM(B3:B7)"),
        ("÷ shares → value / share ($)", f"=B8*1000/{b(R['shares'])}"),
        ("Upside vs price", f"=B9/{b(R['price'])}-1"),
        ("Discount to plan value", f"=B9/{b(R['plan'])}-1"),
        (
            "Market-implied $ for carry+private-RE",
            f"={b(R['price'])}*{b(R['shares'])}/1000-({b(R['bam_fre'])}*{b(R['fre_mult'])}*{b(R['bn_own'])}"
            f"+{b(R['bws_de'])}*{b(R['bws_mult'])}+{b(R['ic_listed'])}-({b(R['corp_debt'])}+{b(R['corp_pref'])}+{b(R['corp_oh'])}))",
        ),
        ("Modeled $ for carry+private-RE", f"=B4+{b(R['ic_priv'])}*(1-{b(R['ic_hair'])})"),
    ]
    rr = 3
    for label, formula in rows:
        sotp.cell(row=rr, column=1, value=label).font = (
            SUB_FONT if "SOTP equity" in label or "value / share" in label else Font(color="374151")
        )
        c = sotp.cell(row=rr, column=2, value=formula)
        c.number_format = (
            USD2
            if ("share" in label or "$)" in label)
            else (PCT if "vs price" in label or "Discount" in label else USDB)
        )
        if "value / share" in label:
            c.font = Font(bold=True)
        rr += 1
    sotp.column_dimensions["A"].width = 40
    sotp.column_dimensions["B"].width = 13

    # ---- Scenarios (note: the bracket is computed by the Python mirror; this
    #      sheet documents the structure so the user can re-derive in-sheet) ----
    _hdr(scen, "A1", "Scenarios & the thesis")
    notes = [
        "Worst case  (carry = 0, private/RE = 0): only AM + BWS + listed − corporate.",
        "Moderate    (defaults): realization/timing haircut on carry + RE haircut.",
        "Base case   (no haircuts): full carry + full private/RE marks.",
        "",
        "THE THESIS: the current price ≈ the WORST case — i.e. the market is paying",
        "almost nothing for the carried-interest stack and the private real estate.",
        "The reverse-solve on the SOTP sheet quantifies exactly how little.",
        "",
        "Holdco discount is an OUTPUT (price-to-NAV gap), not an input — only PV of",
        "corporate overhead is deducted. DE-capitalization is a sanity band, not an",
        "independent cross-check (DE already sums the same four buckets).",
    ]
    for i, n in enumerate(notes, start=3):
        scen.cell(row=i, column=1, value=n).font = Font(color="374151")
    scen.column_dimensions["A"].width = 78

    dest.parent.mkdir(parents=True, exist_ok=True)
    wb.save(dest)


# =========================================================================== #
# Berkshire shape — the two-column SOTP (investments + operating earnings).
# A capital-allocator holdco like BN, but a totally different decomposition:
# its value is the marked investment portfolio + the capitalized operating
# businesses, with the insurance float as free leverage embedded in the
# portfolio. Buffett's own framing. Kept separate from the BN path so the live
# BN model is untouched.
# =========================================================================== #
@dataclass
class BrkSotp:
    """Berkshire two-column SOTP ($B). Calibrate vs the 10-K."""

    eq_bonds: float = 336.4  # long-term investments (marketable equities + fixed income) @ market
    cash_tbills: float = 373.3  # cash + short-term Treasuries
    op_earn: float = 31.0  # after-tax operating earnings EX insurance investment income (already in the portfolio)
    op_mult: float = (
        13.0  # capitalization of the controlled operating businesses (BNSF/BHE/MSR + underwriting)
    )
    dtl: float = 87.0  # deferred tax on unrealized investment gains
    dtl_hair: float = 0.50  # fraction of the DTL to deduct (PV / probability of the future tax)
    corp: float = 0.0  # net corporate adjustments (parent debt is small; sub debt is non-recourse)
    shares_m: float = 2160.0  # B-equivalent shares
    price: float = 488.30


def _brk_value(s: BrkSotp) -> tuple[float, float]:
    investments = s.eq_bonds + s.cash_tbills
    eq = investments + s.op_earn * s.op_mult - s.dtl * s.dtl_hair - s.corp
    return eq, eq * 1000.0 / s.shares_m


def _brk_load() -> BrkSotp:
    """Seed the marked investments, deferred tax, shares and price from FMP so the
    big balance-sheet items auto-update; operating earnings stays a calibrated default."""
    s = BrkSotp()
    fmp = REPO / "data" / "historical" / "fmp"

    def _rows(name):
        p = fmp / name
        if not p.exists():
            return []
        raw = json.loads(p.read_text(encoding="utf-8"))
        return raw.get("historical", [raw]) if isinstance(raw, dict) else raw

    try:
        bal = _rows(f"{T}_balance_sheet_annual.json")
        if bal:
            b = bal[0]
            s.cash_tbills = float(b.get("cashAndShortTermInvestments") or s.cash_tbills * 1e9) / 1e9
            s.eq_bonds = float(b.get("longTermInvestments") or s.eq_bonds * 1e9) / 1e9
            s.dtl = float(b.get("deferredTaxLiabilitiesNonCurrent") or s.dtl * 1e9) / 1e9
        inc = _rows(f"{T}_income_statement_annual.json")
        if inc:
            s.shares_m = float(inc[0].get("weightedAverageShsOutDil") or s.shares_m * 1e6) / 1e6
        prof = json.loads((fmp / f"{T}_profile.json").read_text(encoding="utf-8"))
        if isinstance(prof, list):
            prof = prof[0] if prof else {}
        if isinstance(prof, dict) and prof.get("price"):
            s.price = float(prof["price"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
        pass
    return s


_RB = {
    "eq_bonds": 3,
    "cash_tbills": 4,
    "op_earn": 6,
    "op_mult": 7,
    "dtl": 9,
    "dtl_hair": 10,
    "corp": 12,
    "shares": 14,
    "price": 15,
}


def _brk_build(s: BrkSotp, dest: Path) -> None:
    wb = openpyxl.Workbook()
    dash = wb.active
    dash.title = "Dashboard"
    sotp = wb.create_sheet("SOTP")
    notes_ws = wb.create_sheet("Method")
    D = "Dashboard"

    _hdr(dash, "A1", f"{T} — Berkshire two-column SOTP · Dashboard ($B)")
    dash["A2"] = "① Investment portfolio (marked to market)"
    dash["A2"].font = SUB_FONT
    _inp(dash, _RB["eq_bonds"], "Equities + fixed income @ market", s.eq_bonds)
    _inp(dash, _RB["cash_tbills"], "Cash + Treasury bills", s.cash_tbills)
    dash["A5"] = "② Operating businesses (ex insurance investment income)"
    dash["A5"].font = SUB_FONT
    _inp(dash, _RB["op_earn"], "After-tax operating earnings (ex inv. income)", s.op_earn)
    _inp(dash, _RB["op_mult"], "Operating multiple", s.op_mult, MULT)
    dash["A8"] = "③ Deferred tax + corporate (subtract)"
    dash["A8"].font = SUB_FONT
    _inp(dash, _RB["dtl"], "Deferred tax on unrealized gains", s.dtl)
    _inp(dash, _RB["dtl_hair"], "DTL haircut (PV/probability)", s.dtl_hair, PCT)
    _inp(dash, _RB["corp"], "Net corporate / other", s.corp)
    dash["A13"] = "Market"
    dash["A13"].font = SUB_FONT
    _inp(dash, _RB["shares"], "Shares (M, B-equivalent)", s.shares_m, "#,##0")
    _inp(dash, _RB["price"], "Current price ($)", s.price, USD2)
    dash.column_dimensions["A"].width = 44
    dash.column_dimensions["B"].width = 13

    _hdr(sotp, "A1", f"{T} — Two-column SOTP build ($B)")

    def b(r: int) -> str:
        return f"{D}!$B${r}"

    rows = [
        ("① Investments @ market", f"={b(_RB['eq_bonds'])}+{b(_RB['cash_tbills'])}"),
        ("② Operating businesses", f"={b(_RB['op_earn'])}*{b(_RB['op_mult'])}"),
        ("③ − Deferred tax on gains", f"=-{b(_RB['dtl'])}*{b(_RB['dtl_hair'])}"),
        ("④ − Corporate / other", f"=-{b(_RB['corp'])}"),
        ("= SOTP equity value", "=SUM(B3:B6)"),
        ("÷ shares → value / share ($)", f"=B7*1000/{b(_RB['shares'])}"),
        ("Upside vs price", f"=B8/{b(_RB['price'])}-1"),
        (
            "col-1: investments / share ($)",
            f"=({b(_RB['eq_bonds'])}+{b(_RB['cash_tbills'])})*1000/{b(_RB['shares'])}",
        ),
        (
            "col-2: operating value / share ($)",
            f"={b(_RB['op_earn'])}*{b(_RB['op_mult'])}*1000/{b(_RB['shares'])}",
        ),
    ]
    for i, (label, formula) in enumerate(rows, start=3):
        headline = label.startswith("= SOTP") or label.startswith("÷ shares")
        sotp.cell(row=i, column=1, value=label).font = (
            Font(bold=True) if headline else Font(color="374151")
        )
        c = sotp.cell(row=i, column=2, value=formula)
        c.number_format = USD2 if "share" in label else (PCT if "vs price" in label else USDB)
        if headline:
            c.font = Font(bold=True)
    sotp.column_dimensions["A"].width = 38
    sotp.column_dimensions["B"].width = 13

    _hdr(notes_ws, "A1", "Method — Buffett's two columns")
    for i, n in enumerate(
        [
            "① Investments are MARKED to market (public) — this captures the insurance",
            "   float's value: the float funds the portfolio at ~zero cost (free leverage).",
            "② Operating earnings EXCLUDE insurance investment income — that income is",
            "   already counted by marking the portfolio in ①. Capitalizing it too would",
            "   double-count. Capitalize only the controlled-business earnings (BNSF, BHE,",
            "   MSR, Pilot + insurance UNDERWRITING).",
            "③ Deferred tax on unrealized gains is a real but deferred/contingent claim —",
            "   haircut it (Berkshire rarely liquidates), don't take it at full face.",
            "No holdco discount input: Berkshire trades ~AT its two-column value, so the",
            "price-to-SOTP gap is the output. Swing variables: the operating multiple and",
            "the DTL haircut.",
        ],
        start=3,
    ):
        notes_ws.cell(row=i, column=1, value=n).font = Font(color="374151")
    notes_ws.column_dimensions["A"].width = 80

    dest.parent.mkdir(parents=True, exist_ok=True)
    wb.save(dest)


def _run_brk() -> int:
    s = _brk_load()
    eq, vps = _brk_value(s)
    _brk_build(s, DEST)
    investments = s.eq_bonds + s.cash_tbills
    op_value = s.op_earn * s.op_mult
    dtl_ded = s.dtl * s.dtl_hair
    snap: dict[str, object] = {
        "model": "holdco_sotp_brk",
        "investments_b": investments,
        "operating_value_b": op_value,
        "dtl_deduction_b": dtl_ded,
        "corporate_b": -s.corp,
        "sotp_equity_b": eq,
        "value_per_share_usd": vps,
    }
    persisted = persist_dcf_run(eq, vps, s.price, 1.0 / s.op_mult, snap)
    print(
        f"RESULT\t{T}\tSOTP/sh=${vps:.2f}\tprice=${s.price:.2f}\tupside={vps / s.price - 1:+.0%}"
        f"\tdcf_runs={'ok' if persisted else 'skip'}\t-> {DEST}"
    )
    print(
        f"  (1) Investments @ market (eq/bonds {s.eq_bonds:.0f} + cash/T-bills {s.cash_tbills:.0f}) ${investments:7.1f}B"
    )
    print(
        f"  (2) Operating businesses (op-earn {s.op_earn:.0f} x {s.op_mult:.0f}x)........ ${op_value:7.1f}B"
    )
    print(
        f"  (3) - Deferred tax on gains ({s.dtl:.0f} x {s.dtl_hair:.0%})............ ${-dtl_ded:7.1f}B"
    )
    print(f"  (4) - Corporate / other................................. ${-s.corp:7.1f}B")
    print(
        f"  = SOTP equity ......................................... ${eq:7.1f}B  -> ${vps:.2f}/sh"
    )
    print(
        f"\n  Two columns: investments ${investments * 1000 / s.shares_m:.0f}/sh + operating "
        f"${op_value * 1000 / s.shares_m:.0f}/sh (less DTL) -- Berkshire trades ~AT intrinsic (no holdco discount)."
    )
    return 0


_BRK_TICKERS = {"BRK-B", "BRK-A", "BRK.B", "BRK"}


def main() -> int:
    return _run_brk() if T.upper() in _BRK_TICKERS else _run_bn()


if __name__ == "__main__":
    raise SystemExit(main())
