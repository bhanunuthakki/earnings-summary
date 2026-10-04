"""Sum-of-the-parts platform valuation for MercadoLibre (MELI).

A single FCFF DCF mis-frames MELI: ~44% of revenue is Fintech, and inside Fintech
the Mercado Pago credit book is a spread-lending business that consumes regulatory
capital and throws credit losses — an operating-FCFF model treats its receivable
growth as a working-capital drain and prices its earnings on an EBITDA multiple,
both wrong. So value MELI the way it is actually built — three parts, two methods:

    Commerce (3P marketplace + ads + 1P + logistics)   operating FCFF  @ WACC
    Fintech-payments (acquiring, float, fees)           operating FCFF  @ WACC
    Fintech-credit (Mercado Pago credit book)           excess-return / FCFE @ Ke

    Equity value = Operating EV (Commerce + Fintech-payments)
                 + Credit-book equity value (FCFE on the lending franchise)
                 + net non-operating cash
    per share    = Equity value / diluted shares

The credit book is valued on its own cost of equity (lending is riskier than the
capital-light operating franchises) and charged the growth in required capital it
consumes — the capital intensity an FCFF model can't see. The operating side fades
growth on the same CONVEX curve as the redesigned FCFF engine
(``dcf.redesign.GROWTH_FADE_CURVATURE``), so the two models speak the same language.

Historical defaults are retained solely for isolated draft calculations. Verified
refreshes use canonical reported fiscal operands and an explicitly selected,
dated reviewed forecast artifact; no historical seed is promoted as current.
Non-credit Fintech includes installment and investment income where admitted
source definitions place them outside the separate credit portfolio. Segment
operating margins remain explicit forecasts because MELI reports profit by
geography, not Commerce/Fintech.

Verified refresh requires DCF_MELI_ASSUMPTIONS_PATH and the configured database,
plus staged DCF_DEST/DCF_PROMOTE_DEST. The artifact owns the reviewed effective
discount rates; implicit cached CAPM recalculation is disabled in this route.
DCF_PERSIST=0 permits only a labeled, non-promoting .tmp draft. Values are $M;
shares are millions.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

import openpyxl
from openpyxl.styles import Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

# Import co-located package code (this script's repo), NOT REPO/src — REPO points
# at the DATA repo (which may be a different checkout, e.g. a worktree's data lives
# in the main repo), so resolving code from it would load a stale/foreign copy.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from db_paths import configured_db_path, require_db_path
from dcf import reverse_valuation as reverse_valuation_mod
from dcf.artifact_promotion import (
    ArtifactPromotion,
    live_path_from_env,
    promotion_from_env,
    run_dcf_entrypoint,
)
from dcf.input_evidence import (
    InputEvidenceError,
    ModelInputReceipt,
    ModelInputRequest,
)
from dcf.meli_inputs import (
    ASSUMPTION_KEYS,
    RECIPE,
    effective_numeric_inputs,
    prepare_meli_inputs,
    verify_meli_inputs,
)
from dcf.meli_model import Assum as Assum
from dcf.meli_model import Mirror as Mirror
from dcf.meli_model import mirror as mirror
from dcf.meli_model import validate_credit_terminal
from dcf.provenance import build_file_provenance, schema_supports_provenance
from dcf.specialized_price import (
    SpecializedPriceObservation,
    price_seed_source_files,
    resolve_specialized_price,
)
from sqlite_runtime import SQLiteConnectionRole, connect_sqlite

try:  # persistence is best-effort -- the workbook builds without a DB
    from dcf import persist as _persist_module
except ImportError:  # pragma: no cover
    persist_mod = None
else:
    persist_mod = _persist_module
try:  # global macro assumptions + country risk -- best-effort; in-code defaults else
    from dcf import country_risk as _country_risk_module
    from dcf import global_assumptions as _global_assumptions_module
except ImportError:  # pragma: no cover
    country_risk = None
    global_dcf = None
else:
    country_risk = _country_risk_module
    global_dcf = _global_assumptions_module

# Growth fade curvature — kept identical to the redesigned FCFF engine so the two
# models decelerate growth the same way (convex, front-loaded). Imported when the
# package is available; falls back to the same literal.
try:
    from dcf.redesign import GROWTH_FADE_CURVATURE as _CURVATURE
except ImportError:  # pragma: no cover
    _CURVATURE = 2.0
try:  # scenario emission (Monthly Red Team PR8) -- best-effort like persistence
    from dcf import redesign as _redesign_module
except ImportError:  # pragma: no cover
    redesign_mod = None
else:
    redesign_mod = _redesign_module

REPO = Path(os.environ.get("DCF_REPO_ROOT") or Path(__file__).resolve().parents[1])
T = os.environ.get("DCF_TICKER", "MELI")
DEST = Path(os.environ.get("DCF_DEST") or (REPO / "dcf" / f"{T}.xlsx"))

YELLOW = PatternFill("solid", fgColor="FFF2CC")
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


def reverse_valuation(s: Assum, m: Mirror) -> dict[str, object] | None:
    """Market-implied MELI operating multiple and credit-equity bridge residual."""
    if s.price <= 0 or m.vps <= 0:
        return None
    operating_exit_multiple = reverse_valuation_mod.solve_lever(
        lever_id="implied_operating_exit_multiple",
        label="Implied operating exit EBITDA multiple",
        unit="turns",
        base_value=s.op_exit_ebitda_mult,
        method="monotonic_bisection",
        price_at=lambda multiple: mirror(replace(s, op_exit_ebitda_mult=multiple)).vps,
        target_price=s.price,
        lower_bound=1.0,
        upper_bound=75.0,
    )
    market_equity_value = s.price * s.shares
    implied_credit_equity = market_equity_value - m.operating_ev - s.net_cash
    credit_residual = reverse_valuation_mod.residual_lever(
        lever_id="implied_credit_equity_value",
        label="Market-implied credit equity value",
        unit="usd_m",
        base_value=m.credit_equity_value,
        implied_value=implied_credit_equity,
        note="Market equity less modeled operating EV and net non-operating cash.",
    )
    return reverse_valuation_mod.ReverseValuation(
        archetype="meli_platform_sotp",
        price=s.price,
        base_value_per_share_usd=m.vps,
        valuation_scope="equity",
        levers=(operating_exit_multiple, credit_residual),
    ).to_snapshot_dict()


def load_assumptions(ticker: str, *, db_path: Path | None = None) -> Assum:
    """Assum defaults overridden by data/bank_assumptions/<T>_sotp.json, then the
    editable global tax + (opt-in) CAPM-derived discount rates with the
    revenue-weighted Damodaran country risk premium."""
    s = Assum()
    try:
        db = require_db_path(db_path)
    except (RuntimeError, OSError):
        if db_path is not None:
            raise
        db = None  # explicitly isolated draft: no checkout database fallback
    global_loaded = (
        global_dcf.load_with_provenance(db_path=db)
        if global_dcf is not None and db is not None
        else None
    )
    if global_loaded is not None:
        s.global_assumption_source = global_loaded.source_record
        s.tax = global_loaded.assumptions.tax_rate
    else:
        s.global_assumption_source = {
            "role": "global_dcf_assumptions",
            "status": "module_unavailable",
            "observed_at": None,
        }
    p = REPO / "data" / "bank_assumptions" / f"{ticker}_sotp.json"
    country_risk_overridden = False
    if p.exists():
        try:
            ov: Any = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            ov = {}
        if isinstance(ov, dict):
            for k, v in cast("dict[str, Any]", ov).items():
                if (
                    k not in {"global_assumption_source", "country_risk_source"}
                    and hasattr(s, k)
                    and isinstance(v, (int, float))
                ):
                    setattr(s, k, v)
                    if k == "price":
                        s.price_seed_source = "owner_assumptions"
                        s.price_seed_path = f"data/bank_assumptions/{ticker}_sotp.json"
                    if k == "country_risk_premium":
                        country_risk_overridden = True
    # Systematic country risk premium (revenue-weighted), filled only when the
    # owner did not explicitly set it. This makes an override a true authority
    # boundary: no geographic file is read or claimed as a model input.
    if country_risk is not None and not country_risk_overridden and not s.country_risk_premium:
        country_observation = country_risk.country_risk_observation(REPO, ticker)
        s.country_risk_premium = country_observation.premium
        if country_observation.source_record is not None:
            s.country_risk_source = country_observation.source_record
    # Opt-in: derive both discount rates from the editable global rf/ERP + CRP.
    if global_loaded is not None and s.derive_capm:
        s.wacc = (
            global_loaded.assumptions.risk_free_rate
            + s.beta_op * global_loaded.assumptions.equity_risk_premium
            + s.country_risk_premium
        )
        s.credit_ke = (
            global_loaded.assumptions.risk_free_rate
            + s.beta_credit * global_loaded.assumptions.equity_risk_premium
            + s.country_risk_premium
        )
    prof = REPO / "data" / "historical" / "fmp" / f"{ticker}_profile.json"
    if prof.exists():
        try:
            raw_profile: object = json.loads(prof.read_text(encoding="utf-8"))
            if isinstance(raw_profile, list):
                profiles = cast("list[object]", raw_profile)
                raw_profile = profiles[0] if profiles else {}
            if isinstance(raw_profile, dict):
                profile = cast("dict[str, object]", raw_profile)
                price = profile.get("price")
                if isinstance(price, (int, float, str)) and price:
                    s.price = float(price)
                    s.price_seed_source = "fmp_profile"
                    s.price_seed_path = f"data/historical/fmp/{ticker}_profile.json"
        except (OSError, json.JSONDecodeError, ValueError, KeyError):
            pass
    return s


# --------------------------------------------------------------------------- #
# Scenario emission (Monthly Red Team PR8).
#
# Mirrors the redesigned refresher's persisted ``scenarios`` block (bull/base/
# bear fair values + bear provenance) so MELI stops being invisible to every
# scenario consumer (``dcf.scenario_reward``, ``bear_lint``, tail stress). The
# shared 6-lever ``ScenarioDeltas`` vocabulary maps onto THIS SOTP's levers:
#
#   growth deltas   -> Commerce, Fintech-payments AND credit-book growth
#                      (near/terminal) — a demand shock hits all three engines
#   margin deltas   -> Commerce + Fintech-payments EBIT margins AND the credit
#                      book's NIMAL (near/terminal) — a price war compresses the
#                      operating segments while a credit cycle compresses NIMAL
#   exit multiple Δ -> the operating exit EV/EBITDA (turns, floored at 1x). The
#                      credit leg's Gordon terminal compresses through NIMAL/g,
#                      not through this multiple
#   terminal g Δ    -> credit terminal growth g (and the operating perpetuity
#                      cross-check g, cosmetic)
#
# Bear deltas: holdings ``bear_deltas`` when present (provenance "thesis"), else
# the generic BEAR_SEED (provenance "seed" — a labeled fallback). Legible over
# precise, per the red-team directive.
# --------------------------------------------------------------------------- #
def _load_holdings(ticker: str) -> dict[str, object] | None:
    path = REPO / "micro_thesis" / "holdings" / f"{ticker.upper()}.json"
    if not path.exists():
        return None
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return cast("dict[str, object]", data) if isinstance(data, dict) else None


def scenario_assumptions(s: Assum, deltas: Any) -> Assum:
    """``s`` shifted by one scenario's ``dcf.redesign.ScenarioDeltas`` under the
    documented lever mapping. Reject an inconsistent credit terminal rather
    than changing growth or ROE to make a scenario appear well-posed."""
    import copy

    s2 = copy.copy(s)
    for attr in ("comm_g_near", "fpay_g_near", "cbg_near"):
        setattr(s2, attr, getattr(s2, attr) + deltas.growth_near)
    for attr in ("comm_g_term", "fpay_g_term", "cbg_term"):
        setattr(s2, attr, getattr(s2, attr) + deltas.growth_term)
    for attr in ("comm_margin_near", "fpay_margin_near", "nimal_near"):
        setattr(s2, attr, getattr(s2, attr) + deltas.margin_near)
    for attr in ("comm_margin_term", "fpay_margin_term", "nimal_term"):
        setattr(s2, attr, getattr(s2, attr) + deltas.margin_term)
    s2.op_exit_ebitda_mult = max(1.0, s2.op_exit_ebitda_mult + deltas.exit_multiple)
    s2.credit_g_term += deltas.terminal_g
    s2.g_term += deltas.terminal_g
    validate_credit_terminal(asdict(s2))
    return s2


def scenarios_block(s: Assum, m: Mirror, holdings: dict[str, object] | None) -> dict[str, object]:
    """The ``scenarios`` payload for ``dcf_runs.assumption_snapshot_json`` —
    structurally identical to ``refresh_dcf._redesign_snapshot``'s block, so
    ``parse_scenario_fair_values`` / ``parse_scenario_bear_provenance`` read it
    unchanged. Requires ``redesign_mod`` (caller gates on it)."""
    import dataclasses as _dc

    if redesign_mod is None:
        raise RuntimeError("MELI scenario calculation requires the redesign module")
    bull_d = redesign_mod.BULL_SEED
    bear_d = redesign_mod.thesis_bear_seed(holdings)
    provenance = "thesis" if redesign_mod.parse_thesis_bear_deltas(holdings) is not None else "seed"
    bull_vps = mirror(scenario_assumptions(s, bull_d)).vps
    bear_vps = mirror(scenario_assumptions(s, bear_d)).vps
    return {
        "base": {"fair_value_per_share_usd": m.vps},
        "bull": {"fair_value_per_share_usd": bull_vps, "deltas": _dc.asdict(bull_d)},
        "bear": {
            "fair_value_per_share_usd": bear_vps,
            "deltas": _dc.asdict(bear_d),
            "provenance": provenance,
        },
    }


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


# Dashboard input rows (column B = the editable yellow cell).
R = {
    "comm_rev0": 3,
    "comm_gn": 4,
    "comm_gt": 5,
    "comm_mn": 6,
    "comm_mt": 7,
    "fpay_rev0": 8,
    "fpay_gn": 9,
    "fpay_gt": 10,
    "fpay_mn": 11,
    "fpay_mt": 12,
    "da": 13,
    "capxn": 14,
    "capxt": 15,
    "nwc": 16,
    "tax": 17,
    "opmult": 18,
    "wacc": 19,
    "cb0": 20,
    "cbgn": 21,
    "cbgt": 22,
    "niman": 23,
    "nimat": 24,
    "cropex": 25,
    "cap": 26,
    "cke": 27,
    "cgt": 28,
    "croe": 29,
    "netcash": 30,
    "years": 31,
    "sh": 32,
    "px": 33,
    "curv": 34,
    # outputs
    "opev": 37,
    "crev": 38,
    "eqv": 39,
    "vps": 40,
    "up": 41,
    "termrev": 42,
    "croet": 43,
}


def build(s: Assum, m: Mirror, dest: Path, holdings: dict[str, object] | None = None) -> None:
    """``holdings`` (Monthly Red Team PR10) is the already-loaded
    ``micro_thesis/holdings/<T>.json`` dict, threaded through so the Scenario
    sheet's Bear/Bull rows derive from the SAME ``scenario_assumptions()`` call
    ``scenarios_block`` uses for the persisted ``dcf_runs`` snapshot — the sheet
    can no longer show a bear the rest of the platform (bear_lint, tail stress,
    red-team evidence packs) disagrees with. ``None`` (no holdings on file / not
    passed) degrades to the generic ``BEAR_SEED`` fallback, same as
    ``scenarios_block``."""
    wb = openpyxl.Workbook()
    dash = wb.active
    if not isinstance(dash, Worksheet):
        raise RuntimeError("MELI workbook requires an active worksheet")
    dash.title = "Dashboard"
    mod = wb.create_sheet("Model")
    val = wb.create_sheet("Valuation")
    scn = wb.create_sheet("Scenario")
    D = "Dashboard"

    # ---------- Dashboard ----------
    _hdr(dash, "A1", f"{T} - Sum-of-the-Parts Platform DCF | Dashboard")
    dash["A2"] = (
        "Commerce + Fintech-payments (operating FCFF @ WACC) + credit book (excess-return @ Ke)"
    )
    dash["A2"].font = SUB_FONT
    rows = [
        ("comm_rev0", "Commerce revenue Y0 ($M)", s.comm_rev0, USD0),
        ("comm_gn", "Commerce growth - near", s.comm_g_near, PCT),
        ("comm_gt", "Commerce growth - terminal", s.comm_g_term, PCT),
        ("comm_mn", "Commerce EBIT margin - near", s.comm_margin_near, PCT),
        ("comm_mt", "Commerce EBIT margin - terminal", s.comm_margin_term, PCT),
        ("fpay_rev0", "Fintech-payments revenue Y0 ($M)", s.fpay_rev0, USD0),
        ("fpay_gn", "Fintech-pay growth - near", s.fpay_g_near, PCT),
        ("fpay_gt", "Fintech-pay growth - terminal", s.fpay_g_term, PCT),
        ("fpay_mn", "Fintech-pay EBIT margin - near", s.fpay_margin_near, PCT),
        ("fpay_mt", "Fintech-pay EBIT margin - terminal", s.fpay_margin_term, PCT),
        ("da", "D&A % of operating revenue", s.da_pct, PCT),
        ("capxn", "Capex % rev - near", s.capex_pct_near, PCT),
        ("capxt", "Capex % rev - terminal", s.capex_pct_term, PCT),
        ("nwc", "Working capital % of incr. rev", s.nwc_pct, PCT),
        ("tax", "Tax rate", s.tax, PCT),
        ("opmult", "Operating exit EV/EBITDA", s.op_exit_ebitda_mult, MULT),
        ("wacc", "WACC (operating discount)", s.wacc, PCT),
        ("cb0", "Credit book Y0 ($M)", s.cb0, USD0),
        ("cbgn", "Credit book growth - near", s.cbg_near, PCT),
        ("cbgt", "Credit book growth - terminal", s.cbg_term, PCT),
        ("niman", "NIMAL - near", s.nimal_near, PCT),
        ("nimat", "NIMAL - terminal", s.nimal_term, PCT),
        ("cropex", "Credit opex % of book", s.credit_opex_ratio, PCT),
        ("cap", "Capital ratio (req eq / book)", s.cap_ratio, PCT),
        ("cke", "Credit cost of equity Ke", s.credit_ke, PCT),
        ("cgt", "Credit terminal growth g", s.credit_g_term, PCT),
        ("croe", "Credit terminal ROE", s.credit_terminal_roe, PCT),
        ("netcash", "Net non-operating cash ($M)", s.net_cash, USD0),
        ("years", "Forecast years", s.years, "0"),
        ("sh", "Diluted shares (M)", s.shares, NUM1),
        ("px", "Current price ($)", s.price, NUM2),
        ("curv", "Growth fade curvature", _CURVATURE, NUM1),
    ]
    for key, lab, v, fmt in rows:
        _inp(dash, R[key], lab, v, fmt)

    _hdr(dash, "A36", "OUTPUT")
    for rr, lab, ref, fmt in (
        (R["opev"], "Operating EV ($M)", "Valuation!$B$5", USD0),
        (R["crev"], "Credit equity value ($M)", "Valuation!$B$9", USD0),
        (R["eqv"], "Equity value ($M)", "Valuation!$B$11", USD0),
        (R["vps"], "Value per share ($)", "Valuation!$B$12", NUM2),
        (R["up"], "Upside vs price", "Valuation!$B$13", PCT),
        (R["termrev"], "Terminal operating revenue ($M)", "Valuation!$B$14", USD0),
        (R["croet"], "Terminal credit ROE", "Valuation!$B$15", PCT),
    ):
        dash.cell(row=rr, column=1, value=lab).font = SUB_FONT
        oc = dash.cell(row=rr, column=2, value=f"={ref}")
        oc.number_format = fmt
        oc.font = Font(bold=True)
    dash.column_dimensions["A"].width = 36
    dash.column_dimensions["B"].width = 14

    # ---------- Model (formula-first engine) ----------
    _hdr(mod, "A1", f"{T} - SOTP engine (formula-first; $M)")
    n = s.years
    col0 = 3  # column C = Y1

    def cl(i: int) -> str:
        return get_column_letter(i)

    labels = {
        3: "Year",
        4: "Commerce revenue",
        5: "Fintech-pay revenue",
        6: "Operating revenue",
        7: "Operating EBIT",
        8: "  D&A",
        9: "  Capex",
        10: "Operating FCFF",
        11: "DF (WACC)",
        12: "PV operating FCFF",
        13: "Credit book",
        14: "Credit NI",
        15: "Required capital",
        16: "Credit FCFE",
        17: "DF (Ke)",
        18: "PV credit FCFE",
    }
    for r, lab in labels.items():
        mod.cell(row=r, column=2, value=lab).font = SUB_FONT if r == 3 else Font(color="374151")

    def dref(key: str) -> str:
        return f"{D}!$B${R[key]}"

    def fade_f(c: str, near_key: str, term_key: str) -> str:
        """Excel convex-fade formula mirroring _fade for the year in row 3 of col c."""
        near, term = dref(near_key), dref(term_key)
        yrs = dref("years")
        return f"{term}+({near}-{term})*(({yrs}-{c}$3)/({yrs}-1))^{dref('curv')}"

    def interp_f(c: str, near_key: str, term_key: str) -> str:
        near, term = dref(near_key), dref(term_key)
        return f"{near}+({term}-{near})*({c}$3-1)/({dref('years')}-1)"

    for j in range(1, n + 1):
        c = cl(col0 + j - 1)
        p = cl(col0 + j - 2)  # prior column (j==1 references Dashboard Y0)
        mod[f"{c}3"] = j
        if j == 1:
            mod[f"{c}4"] = f"={dref('comm_rev0')}*(1+{fade_f(c, 'comm_gn', 'comm_gt')})"
            mod[f"{c}5"] = f"={dref('fpay_rev0')}*(1+{fade_f(c, 'fpay_gn', 'fpay_gt')})"
            mod[f"{c}13"] = f"={dref('cb0')}*(1+{fade_f(c, 'cbgn', 'cbgt')})"
            prev_oprev = f"({dref('comm_rev0')}+{dref('fpay_rev0')})"
            prev_cb = dref("cb0")
            prev_reqcap = f"{dref('cap')}*{dref('cb0')}"
        else:
            mod[f"{c}4"] = f"={p}4*(1+{fade_f(c, 'comm_gn', 'comm_gt')})"
            mod[f"{c}5"] = f"={p}5*(1+{fade_f(c, 'fpay_gn', 'fpay_gt')})"
            mod[f"{c}13"] = f"={p}13*(1+{fade_f(c, 'cbgn', 'cbgt')})"
            prev_oprev = f"{p}6"
            prev_cb = f"{p}13"
            prev_reqcap = f"{p}15"
        mod[f"{c}6"] = f"={c}4+{c}5"
        mod[f"{c}7"] = (
            f"={c}4*({interp_f(c, 'comm_mn', 'comm_mt')})+{c}5*({interp_f(c, 'fpay_mn', 'fpay_mt')})"
        )
        mod[f"{c}8"] = f"={c}6*{dref('da')}"
        mod[f"{c}9"] = f"={c}6*({interp_f(c, 'capxn', 'capxt')})"
        mod[f"{c}10"] = f"={c}7*(1-{dref('tax')})+{c}8-{c}9-({c}6-{prev_oprev})*{dref('nwc')}"
        mod[f"{c}11"] = f"=1/(1+{dref('wacc')})^{c}3"
        mod[f"{c}12"] = f"={c}10*{c}11"
        # credit: NI on avg book at NIMAL net of opex; FCFE charges capital growth
        mod[f"{c}14"] = (
            f"=(({c}13+{prev_cb})/2)*(({interp_f(c, 'niman', 'nimat')})-{dref('cropex')})*(1-{dref('tax')})"
        )
        mod[f"{c}15"] = f"={dref('cap')}*{c}13"
        mod[f"{c}16"] = f"={c}14-({c}15-{prev_reqcap})"
        mod[f"{c}17"] = f"=1/(1+{dref('cke')})^{c}3"
        mod[f"{c}18"] = f"={c}16*{c}17"
    for r in range(4, 19):
        for j in range(1, n + 1):
            cc = mod.cell(row=r, column=col0 + j - 1)
            cc.number_format = NUM2 if r in (11, 17) else USD0
    mod.column_dimensions["B"].width = 20

    # ---------- Valuation (SOTP sum) ----------
    _hdr(val, "A1", f"{T} - Sum-of-the-Parts ($M)")
    cN = cl(col0 + n - 1)
    c1 = cl(col0)
    vrows = [
        ("PV operating FCFF (yrs 1-N)", f"=SUM(Model!{c1}12:{cN}12)", USD0, 2),
        (
            "Operating terminal EV (EV/EBITDA)",
            f"=(Model!{cN}7+Model!{cN}8)*{D}!$B${R['opmult']}",
            USD0,
            3,
        ),
        ("PV operating terminal", f"=B3*Model!{cN}11", USD0, 4),
        ("Operating EV", "=B2+B4", USD0, 5),
        ("PV credit FCFE (yrs 1-N)", f"=SUM(Model!{c1}18:{cN}18)", USD0, 6),
        (
            "Credit terminal value (sustainable)",
            f"=Model!{cN}14*(1+{D}!$B${R['cgt']})*(1-{D}!$B${R['cgt']}/{D}!$B${R['croe']})/({D}!$B${R['cke']}-{D}!$B${R['cgt']})",
            USD0,
            7,
        ),
        ("PV credit terminal", f"=B7*Model!{cN}17", USD0, 8),
        ("Credit equity value", "=B6+B8", USD0, 9),
        ("+ Net non-operating cash", f"={D}!$B${R['netcash']}", USD0, 10),
        ("Equity value", "=B5+B9+B10", USD0, 11),
        ("Value per share ($)", f"=B11/{D}!$B${R['sh']}", NUM2, 12),
        ("Upside vs price", f"=B12/{D}!$B${R['px']}-1", PCT, 13),
        ("Terminal operating revenue", f"=Model!{cN}6", USD0, 14),
        ("Terminal credit ROE", f"=Model!{cN}14/Model!{cN}15", PCT, 15),
    ]
    for lab, formula, fmt, rr in vrows:
        val.cell(row=rr, column=1, value=lab).font = (
            SUB_FONT if rr in (5, 9, 11, 12) else Font(color="374151")
        )
        vc = val.cell(row=rr, column=2, value=formula)
        vc.number_format = fmt
        if rr in (11, 12):
            vc.font = Font(bold=True)
    val.column_dimensions["A"].width = 36
    val.column_dimensions["B"].width = 14

    # ---------- Scenario ----------
    _hdr(scn, "A1", "Scenarios & sum-of-the-parts bridge")
    scn["A2"], scn["B2"], scn["C2"], scn["D2"], scn["E2"] = (
        "Scenario",
        "Commerce g (near)",
        "Comm margin (term)",
        "NIMAL (term)",
        "Value/share",
    )
    for cc in ("A2", "B2", "C2", "D2", "E2"):
        scn[cc].font = SUB_FONT
    r = 3
    if redesign_mod is not None:
        # Monthly Red Team PR10: Bear/Bull rows come from the SAME
        # scenario_assumptions() call scenarios_block() uses for the persisted
        # dcf_runs snapshot (bear deltas from holdings bear_deltas when present,
        # provenance "thesis"; else the generic BEAR_SEED, provenance "seed") —
        # this sheet and the snapshot every risk consumer reads (bear_lint,
        # tail stress, red-team evidence packs) can no longer disagree.
        bull_d = redesign_mod.BULL_SEED
        bear_d = redesign_mod.thesis_bear_seed(holdings)
        bear_provenance = (
            "thesis" if redesign_mod.parse_thesis_bear_deltas(holdings) is not None else "seed"
        )
        scenario_rows: list[tuple[str, Assum]] = [
            ("Bear", scenario_assumptions(s, bear_d)),
            ("Base", s),
            ("Bull", scenario_assumptions(s, bull_d)),
        ]
        for name, s2 in scenario_rows:
            v = mirror(s2).vps
            scn.cell(row=r, column=1, value=name).font = Font(bold=(name == "Base"), color="374151")
            for col, vv in zip(
                ("B", "C", "D"),
                (s2.comm_g_near, s2.comm_margin_term, s2.nimal_term),
                strict=True,
            ):
                cc = scn[f"{col}{r}"]
                cc.value = vv
                cc.number_format = PCT
            ec = scn.cell(row=r, column=5, value=round(v, 2))
            ec.number_format = NUM2
            ec.font = Font(bold=(name == "Base"))
            r += 1
        prov_label = (
            "bear from holdings bear_deltas (thesis)"
            if bear_provenance == "thesis"
            else "bear from generic BEAR_SEED (seed fallback -- no holdings bear_deltas on file)"
        )
        scn.cell(row=r, column=1, value="Bear provenance").font = Font(italic=True, color="6B7280")
        prov_cell = scn.cell(row=r, column=2, value=prov_label)
        prov_cell.font = Font(italic=True, color="6B7280", size=9)
        scn.merge_cells(start_row=r, start_column=2, end_row=r, end_column=5)
        r += 1
    else:  # pragma: no cover - import failure only, exercised by no test env
        # dcf.redesign unavailable: degrade LOUDLY (an empty/stale scenario row
        # is exactly the PR10 bug) rather than silently falling back to the old
        # hardcoded Bear/Bull levers.
        scn.cell(
            row=r, column=1, value="Scenarios unavailable (dcf.redesign import failed)"
        ).font = Font(italic=True, color="B91C1C")
        r += 1
    r += 1
    _hdr(scn, f"A{r}", "SUM-OF-THE-PARTS BRIDGE ($M)")
    r += 1
    bridge = [
        ("Operating EV (Commerce + Fintech-pay)", f"{m.operating_ev:,.0f}"),
        ("Credit-book equity value (FCFE @ Ke)", f"{m.credit_equity_value:,.0f}"),
        ("Net non-operating cash", f"{s.net_cash:,.0f}"),
        ("= Equity value", f"{m.equity_value:,.0f}"),
        ("Value per share", f"${m.vps:,.2f}  (vs ${s.price:,.2f}, {m.vps / s.price - 1:+.0%})"),
        ("Terminal credit ROE", f"{m.credit_terminal_roe:.0%}"),
        ("Terminal blended op margin", f"{m.terminal_blended_op_margin:.0%}"),
    ]
    for lab, txt in bridge:
        scn.cell(row=r, column=1, value=lab).font = Font(color="374151")
        scn.cell(row=r, column=2, value=txt).font = Font(size=10, color="374151")
        r += 1
    scn.column_dimensions["A"].width = 40
    scn.column_dimensions["B"].width = 26

    dest.parent.mkdir(parents=True, exist_ok=True)
    wb.save(dest)


def persist_dcf_run(
    s: Assum,
    m: Mirror,
    holdings: dict[str, object] | None = None,
    price_observation: SpecializedPriceObservation | None = None,
    *,
    artifact_promotion: ArtifactPromotion | None = None,
    db_path: Path | None = None,
    input_receipt: ModelInputReceipt | None = None,
    assumptions_path: Path | None = None,
) -> bool:
    """``holdings=None`` (the pre-PR10 2-arg call shape every test/caller uses)
    loads ``micro_thesis/holdings/<T>.json`` itself, same as before. ``main()``
    now passes the SAME dict ``build()``'s Scenario sheet used, so a mid-run
    file edit can never make the sheet and the persisted snapshot disagree —
    a ticker with genuinely no holdings JSON still resolves to ``None`` either
    way, so this collapses "not passed" and "no holdings on file" safely."""
    if input_receipt is None:
        raise InputEvidenceError("model_input_receipt_required")
    if assumptions_path is None or input_receipt.assumptions_source_path != str(
        assumptions_path.resolve()
    ):
        raise InputEvidenceError("explicit_assumptions_authority_required")
    try:
        current_source_hash = hashlib.sha256(assumptions_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise InputEvidenceError("assumptions_authority_unavailable") from exc
    if current_source_hash != input_receipt.assumptions_source_sha256:
        raise InputEvidenceError("assumptions_authority_changed_after_review")
    db = require_db_path(db_path)
    if persist_mod is None or not m.vps:
        return False
    recomputed = mirror(s)
    if recomputed != m:
        raise InputEvidenceError("model_output_input_mismatch")
    if holdings is None:
        holdings = _load_holdings(T)
    mos: object = holdings.get("mos_bar") if holdings else None
    observed_at = price_observation.observed_at if price_observation is not None else None
    price_source = (
        price_observation.source_name if price_observation is not None else "assumption_seed"
    )
    live_workbook = live_path_from_env(DEST)
    snap_payload: dict[str, object] = {
        "model": "meli_platform_sotp",
        "effective_model_inputs": effective_numeric_inputs(asdict(s)),
        "financial_period_end": input_receipt.request.financial_period_end.isoformat(),
        "value_per_share": m.vps,
        "operating_ev_m": m.operating_ev,
        "credit_equity_value_m": m.credit_equity_value,
        "equity_value_m": m.equity_value,
        "terminal_operating_revenue_m": m.op_terminal_revenue,
        "terminal_credit_roe": m.credit_terminal_roe,
        "terminal_blended_op_margin": m.terminal_blended_op_margin,
        "wacc": s.wacc,
        "credit_ke": s.credit_ke,
        "country_risk_premium": s.country_risk_premium,
        "workbook": str(live_workbook),
        "assumption_provenance": {
            "authority": str(assumptions_path.resolve()),
            "sha256": current_source_hash,
            "rates": "dated_reviewed_effective_vector",
            "workbook_capture": "unsupported",
            "sync_status": "not_applicable",
        },
    }
    if redesign_mod is not None:
        snap_payload["scenarios"] = scenarios_block(s, m, holdings)
    reverse = reverse_valuation(s, m)
    if reverse is not None:
        snap_payload["reverse_valuation"] = reverse
    snap = json.dumps(snap_payload, indent=2)
    provenance = build_file_provenance(
        ticker=T,
        repo_root=REPO,
        workbook_path=DEST,
        workbook_locator_path=live_workbook,
        engine_version="meli_platform_sotp_v1",
        effective_inputs=asdict(s),
        assumption_snapshot=snap_payload,
        live_price=s.price or None,
        live_price_at=observed_at,
        live_price_source=price_source,
        source_files=(
            (assumptions_path, "owner_assumptions"),
            (REPO / "micro_thesis" / "holdings" / f"{T}.json", "holding_policy"),
            *(
                price_seed_source_files(REPO, price_observation)
                if price_observation is not None
                else ()
            ),
        ),
        source_records=tuple(
            record for record in (s.global_assumption_source, s.country_risk_source) if record
        ),
        equity_direct_archetype="platform_sotp",
        model_input_receipt=input_receipt.model_dump(mode="json"),
    )
    row = persist_mod.DcfRunRow(
        ticker=T,
        valuation_date=date.today(),
        horizon_years=s.years,
        wacc=s.wacc,
        npv=m.equity_value,
        npv_per_share=m.vps,
        shares_outstanding=s.shares * 1e6,
        currency="USD",
        live_price=s.price or None,
        live_price_at=observed_at,
        mos_bar_used=float(mos) if isinstance(mos, (int, float)) else None,
        assumption_snapshot_json=snap,
        notes=f"workbook={live_workbook.name} (MELI sum-of-the-parts platform DCF)",
        provenance=provenance,
        calculated_at=datetime.now(UTC),
    )
    with connect_sqlite(str(db), role=SQLiteConnectionRole.WRITER, schema_preflight=True) as conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        verify_meli_inputs(
            conn,
            input_receipt,
            effective_inputs=effective_numeric_inputs(asdict(s)),
            as_of=datetime.now(UTC),
        )
        if not schema_supports_provenance(conn):
            raise InputEvidenceError("model_input_receipt_schema_unavailable")
        if artifact_promotion is None:
            return persist_mod.upsert(conn, row)
        return persist_mod.upsert(conn, row, artifact_promotion=artifact_promotion)


def load_verified_assumptions(
    ticker: str, *, db_path: Path, assumptions_path: Path, expected_sha256: str | None = None
) -> tuple[Assum, ModelInputReceipt]:
    """Read one explicit reviewed artifact; no repo/data or DB-directory fallback.

    This vector records effective forecast rates. Refreshing CAPM or other
    forecast choices requires a new dated review, not implicit cache loading.
    """
    if ticker != "MELI":
        raise InputEvidenceError("meli_input_recipe_ticker_mismatch")
    try:
        source_bytes = assumptions_path.read_bytes()
    except OSError as exc:
        raise InputEvidenceError("model_input_request_missing_or_invalid") from exc
    if expected_sha256 is not None and hashlib.sha256(source_bytes).hexdigest() != expected_sha256:
        raise InputEvidenceError("assumptions_authority_changed_after_dispatch")
    try:
        request_payload = json.loads(source_bytes)
        request = ModelInputRequest.model_validate(request_payload.get("input_evidence"))
    except (OSError, ValueError, AttributeError) as exc:
        raise InputEvidenceError("model_input_request_missing_or_invalid") from exc
    if request.recipe != RECIPE:
        raise InputEvidenceError("model_input_recipe_mismatch")
    s = Assum()
    for key, assumption in request.assumptions.items():
        if key not in ASSUMPTION_KEYS:
            raise InputEvidenceError("assumption_population_mismatch")
        setattr(
            s, key, int(assumption.value) if key in {"years", "derive_capm"} else assumption.value
        )
    # In verified builds no historical seed is offered as a current quote.
    s.price = 0.0
    s.price_seed_source = "unavailable"
    with connect_sqlite(require_db_path(db_path), role=SQLiteConnectionRole.READ_ONLY) as conn:
        conn.execute("BEGIN")
        values, receipt = prepare_meli_inputs(
            conn,
            request,
            effective_inputs=effective_numeric_inputs(asdict(s)),
            as_of=datetime.now(UTC),
        )
    for key, value in values.items():
        setattr(s, key, int(value) if key in {"years", "derive_capm"} else value)
    return s, receipt.model_copy(
        update={
            "assumptions_source_path": str(assumptions_path.resolve()),
            "assumptions_source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        }
    )


def _main_owned() -> int:
    draft = os.environ.get("DCF_PERSIST", "1") != "1"
    artifact_promotion = promotion_from_env(DEST)
    if draft:
        if artifact_promotion is not None or not DEST.resolve().is_relative_to(
            (REPO / ".tmp").resolve()
        ):
            raise InputEvidenceError("draft_destination_must_be_isolated_tmp")
    elif artifact_promotion is None or DEST.resolve() == live_path_from_env(DEST).resolve():
        raise InputEvidenceError("atomic_artifact_promotion_required_use_refresh_dcf")
    db_path = None if draft else require_db_path(configured_db_path(REPO))
    input_receipt: ModelInputReceipt | None = None
    assumptions_path: Path | None = None
    if draft:
        s = load_assumptions(T)
        sys.stderr.write(
            json.dumps(
                {
                    "event": "meli_isolated_draft",
                    "input_status": "unverified_defaults_or_assumptions",
                    "promotion_allowed": False,
                    "draft_input_source": str(
                        REPO / "data" / "bank_assumptions" / f"{T}_sotp.json"
                    ),
                }
            )
            + "\n"
        )
    else:
        assert db_path is not None
        raw_assumptions_path = os.environ.get("DCF_MELI_ASSUMPTIONS_PATH", "").strip()
        if not raw_assumptions_path:
            raise InputEvidenceError("explicit_assumptions_authority_required")
        assumptions_path = Path(raw_assumptions_path)
        s, input_receipt = load_verified_assumptions(
            T,
            db_path=db_path,
            assumptions_path=assumptions_path,
            expected_sha256=os.environ.get("DCF_MELI_ASSUMPTIONS_SHA256"),
        )
    price_observation = resolve_specialized_price(
        REPO,
        T,
        fallback_price=s.price,
        fallback_source_name=s.price_seed_source,
        fallback_source_path=s.price_seed_path,
    )
    s.price = price_observation.price
    m = mirror(s)
    # Loaded once and threaded through both the Scenario sheet (build) and the
    # persisted snapshot (persist_dcf_run) — PR10: one holdings read, one bear,
    # never two that could drift on a mid-run file edit.
    holdings = _load_holdings(T)
    build(s, m, DEST, holdings)
    if os.environ.get("DCF_PERSIST", "1") != "1":
        persisted = False
    elif artifact_promotion is not None:
        persisted = persist_dcf_run(
            s,
            m,
            holdings,
            price_observation,
            artifact_promotion=artifact_promotion,
            db_path=db_path,
            input_receipt=input_receipt,
            assumptions_path=assumptions_path,
        )
    else:
        persisted = persist_dcf_run(
            s,
            m,
            holdings,
            price_observation,
            db_path=db_path,
            input_receipt=input_receipt,
            assumptions_path=assumptions_path,
        )
    up = (m.vps / s.price - 1) if s.price else 0.0
    print(
        f"RESULT\t{T}\tvalue/sh=${m.vps:,.2f}\tprice=${s.price:,.2f}\tupside={up:+.0%}"
        f"\topEV=${m.operating_ev:,.0f}M\tcreditEq=${m.credit_equity_value:,.0f}M"
        f"\tWACC={s.wacc:.1%}\tcreditKe={s.credit_ke:.1%}\tdcf_runs={'ok' if persisted else 'skip'}\t-> {DEST}"
    )
    print(
        f"{'Yr':>2} {'CommRev':>8} {'PayRev':>7} {'OpEBIT':>7} {'OpFCFF':>7} {'Book':>7} {'CrNI':>6} {'CrFCFE':>7}"
    )
    for r in m.rows:
        print(
            f"{r.t:>2} {r.comm_rev:>8,.0f} {r.fpay_rev:>7,.0f} {r.op_ebit:>7,.0f} {r.op_fcff:>7,.0f} "
            f"{r.cb:>7,.0f} {r.credit_ni:>6,.0f} {r.credit_fcfe:>7,.0f}"
        )
    print(
        f"\nSOTP: Operating EV ${m.operating_ev / 1000:,.1f}B + Credit equity ${m.credit_equity_value / 1000:,.1f}B "
        f"+ net cash ${s.net_cash / 1000:,.1f}B = equity ${m.equity_value / 1000:,.1f}B"
    )
    print(
        f"Value/share ${m.vps:,.2f} vs ${s.price:,.2f} ({up:+.0%}) | terminal op rev "
        f"${m.op_terminal_revenue / 1000:,.0f}B, credit ROE {m.credit_terminal_roe:.0%}"
    )
    return 0


def main() -> int:
    return run_dcf_entrypoint(
        REPO,
        T,
        _main_owned,
        owner="build-meli-platform-dcf",
        require_database=os.environ.get("DCF_PERSIST", "1") == "1",
    )


if __name__ == "__main__":
    raise SystemExit(main())
