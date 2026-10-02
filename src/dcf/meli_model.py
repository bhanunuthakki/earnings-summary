"""MELI numerical owner: original operating FCFF + credit FCFE formulas.

Defaults are historical draft seeds, never evidence of current financial facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dcf.redesign import GROWTH_FADE_CURVATURE as _CURVATURE


@dataclass
class Assum:
    """Base case grounded in MELI's FY2025 10-K segment note. All $M; shares in
    millions. Growth fades CONVEXLY near→terminal; margins/NIMAL ramp linearly."""

    # ---- Commerce (3P marketplace + ads + 1P + logistics) — operating FCFF ----
    comm_rev0: float = 16294.0  # FY2025 Commerce net revenue
    comm_g_near: float = 0.22  # near-term growth (GMV + ads + 1P), fades convexly
    comm_g_term: float = 0.05
    comm_margin_near: float = 0.12  # EBIT margin Y1 (compressed: free-shipping, 1P, AI invest)
    comm_margin_term: float = 0.20  # mature margin (logistics density + ads mix)

    # ---- Fintech-payments (acquiring, float, fees) — operating FCFF, capital-light ----
    fpay_rev0: float = 6741.0  # FY2025 Fintech ex-credit (services $6,678M + product $63M)
    fpay_g_near: float = 0.26
    fpay_g_term: float = 0.06
    fpay_margin_near: float = 0.16
    fpay_margin_term: float = 0.26  # capital-light take-rate + float economics

    # ---- shared operating drivers ----
    da_pct: float = 0.04  # D&A as % of operating revenue
    capex_pct_near: float = 0.055  # logistics build-out heavy early
    capex_pct_term: float = 0.035
    nwc_pct: float = 0.02  # working-capital draw on incremental operating revenue
    tax: float = 0.28
    op_exit_ebitda_mult: float = 12.0  # terminal EV/EBITDA on the operating block
    wacc: float = 0.135  # operating discount rate (set from CAPM+CRP when derive_capm)

    # ---- Fintech-credit (Mercado Pago credit book) — excess-return / FCFE ----
    cb0: float = 13000.0  # credit portfolio Y0 ($M, ~FY2025 end; Q1'26 $14.6B)
    cbg_near: float = 0.34  # book growth (credit card + consumer), fades convexly
    cbg_term: float = 0.08
    nimal_near: float = 0.178  # net interest margin AFTER losses (Q1'26 17.8%)
    nimal_term: float = 0.150  # PM floor; card-mix compresses the spread
    credit_opex_ratio: float = 0.05  # credit-specific opex (origination/servicing) as % of book
    cap_ratio: float = 0.15  # required equity / credit book
    credit_ke: float = 0.16  # cost of equity for the lending franchise (riskier)
    credit_g_term: float = 0.08
    credit_terminal_roe: float = 0.25  # sustainable ROE on the credit book

    # ---- bridge / discounting ----
    net_cash: float = 0.0  # verified recipe computes this; editable only in degraded drafts
    # Reviewed allocations of the issuer-reported liquidity/debt pools ($M).
    credit_cash_allocation: float = 0.0
    operating_cash_reserve: float = 0.0
    credit_funding_debt_allocation: float = 0.0
    g_term: float = 0.045  # operating perpetuity-cross-check growth (~risk-free)
    years: int = 10
    shares: float = 50.697  # diluted shares (M)
    price: float = 1684.0

    # ---- opt-in CAPM discount rates from the editable global rf/ERP + country CRP ----
    # When derive_capm != 0, wacc and credit_ke are recomputed from rf + beta*erp + crp
    # so a dashboard macro change flows through. Off by default (explicit scalars win).
    beta_op: float = 1.30
    beta_credit: float = 1.55
    country_risk_premium: float = 0.0  # filled from dcf.country_risk at load when 0
    derive_capm: int = 1
    global_assumption_source: dict[str, object] = field(
        default_factory=lambda: dict[str, object](), repr=False
    )
    country_risk_source: dict[str, object] = field(
        default_factory=lambda: dict[str, object](), repr=False
    )
    price_seed_source: str = field(default="model_seed", repr=False)
    price_seed_path: str | None = field(default=None, repr=False)


def _interp(near: float, term: float, t: int, n: int) -> float:
    """Linear ramp from ``near`` (year 1) to ``term`` (year n)."""
    return near if n <= 1 else near + (term - near) * (t - 1) / (n - 1)


def _fade(near: float, term: float, t: int, n: int) -> float:
    """Convex growth fade: ``term + (near-term)*((n-t)/(n-1))**curvature``.

    Year 1 = near, year n = term, front-loaded deceleration — identical in shape
    to ``dcf.redesign``'s fade so the SOTP operating block and the FCFF engine
    decelerate the same way.
    """
    if n <= 1:
        return near
    frac = ((n - t) / (n - 1)) ** _CURVATURE
    return term + (near - term) * frac


@dataclass
class Row:
    t: int
    # operating
    comm_rev: float
    fpay_rev: float
    op_rev: float
    op_ebit: float
    op_da: float
    op_capex: float
    op_fcff: float
    # credit
    cb: float
    credit_ni: float
    reqcap: float
    credit_fcfe: float
    # discounting
    df_op: float
    df_cr: float


@dataclass
class Mirror:
    rows: list[Row] = field(default_factory=lambda: list[Row]())
    pv_op_fcff: float = 0.0
    op_terminal_ev: float = 0.0
    pv_op_terminal: float = 0.0
    operating_ev: float = 0.0
    pv_credit_fcfe: float = 0.0
    credit_terminal: float = 0.0
    pv_credit_terminal: float = 0.0
    credit_equity_value: float = 0.0
    equity_value: float = 0.0
    vps: float = 0.0
    # cross-checks
    op_terminal_revenue: float = 0.0
    credit_terminal_roe: float = 0.0
    terminal_blended_op_margin: float = 0.0


def mirror(s: Assum) -> Mirror:
    n = s.years
    m = Mirror()
    comm_p, fpay_p, op_rev_p = s.comm_rev0, s.fpay_rev0, s.comm_rev0 + s.fpay_rev0
    cb_p = s.cb0
    reqcap_p = s.cap_ratio * s.cb0
    for t in range(1, n + 1):
        # --- operating: Commerce + Fintech-payments -> FCFF ---
        comm_rev = comm_p * (1 + _fade(s.comm_g_near, s.comm_g_term, t, n))
        fpay_rev = fpay_p * (1 + _fade(s.fpay_g_near, s.fpay_g_term, t, n))
        op_rev = comm_rev + fpay_rev
        op_ebit = comm_rev * _interp(
            s.comm_margin_near, s.comm_margin_term, t, n
        ) + fpay_rev * _interp(s.fpay_margin_near, s.fpay_margin_term, t, n)
        op_da = op_rev * s.da_pct
        op_capex = op_rev * _interp(s.capex_pct_near, s.capex_pct_term, t, n)
        op_dnwc = (op_rev - op_rev_p) * s.nwc_pct
        op_fcff = op_ebit * (1 - s.tax) + op_da - op_capex - op_dnwc
        df_op = 1 / (1 + s.wacc) ** t

        # --- credit book: NIMAL spread, capital-charged -> FCFE ---
        cb = cb_p * (1 + _fade(s.cbg_near, s.cbg_term, t, n))
        avg_book = (cb + cb_p) / 2.0
        nimal = _interp(s.nimal_near, s.nimal_term, t, n)
        credit_pretax = avg_book * (nimal - s.credit_opex_ratio)
        credit_ni = credit_pretax * (1 - s.tax)
        reqcap = s.cap_ratio * cb
        credit_fcfe = credit_ni - (reqcap - reqcap_p)
        df_cr = 1 / (1 + s.credit_ke) ** t

        m.rows.append(
            Row(
                t,
                comm_rev,
                fpay_rev,
                op_rev,
                op_ebit,
                op_da,
                op_capex,
                op_fcff,
                cb,
                credit_ni,
                reqcap,
                credit_fcfe,
                df_op,
                df_cr,
            )
        )
        m.pv_op_fcff += op_fcff * df_op
        m.pv_credit_fcfe += credit_fcfe * df_cr
        comm_p, fpay_p, op_rev_p, cb_p, reqcap_p = comm_rev, fpay_rev, op_rev, cb, reqcap

    last = m.rows[-1]
    # operating terminal: EV/EBITDA exit on terminal operating EBITDA
    m.op_terminal_ev = (last.op_ebit + last.op_da) * s.op_exit_ebitda_mult
    m.pv_op_terminal = m.op_terminal_ev * last.df_op
    m.operating_ev = m.pv_op_fcff + m.pv_op_terminal

    # credit terminal: sustainable Gordon on credit NI (reinvest g/ROE)
    ni_n1 = last.credit_ni * (1 + s.credit_g_term)
    m.credit_terminal = (
        ni_n1 * (1 - s.credit_g_term / s.credit_terminal_roe) / (s.credit_ke - s.credit_g_term)
    )
    m.pv_credit_terminal = m.credit_terminal * last.df_cr
    m.credit_equity_value = m.pv_credit_fcfe + m.pv_credit_terminal

    m.equity_value = m.operating_ev + s.net_cash + m.credit_equity_value
    m.vps = m.equity_value / s.shares if s.shares else 0.0
    m.op_terminal_revenue = last.op_rev
    m.credit_terminal_roe = (last.credit_ni / last.reqcap) if last.reqcap else 0.0
    m.terminal_blended_op_margin = (last.op_ebit / last.op_rev) if last.op_rev else 0.0
    return m
