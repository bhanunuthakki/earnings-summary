# Allocation-method review — September 8, 2026

## Decision

The owner clarified that immediate delivery is a reusable analyst skill using the
existing components, with frontend integration later. The
[installed skill](../../src/advisor/skills/next-dollar-allocation/SKILL.md) applies the
review's tax, uncertainty, funding and exposure checks without requiring a new engine.
If a quantitative optimizer is later justified, constrained tax-aware construction
with explicit forecast uncertainty and reproducible account-funded trades is a
supported direction. “State of the art” does not establish one universally best
optimizer or make a forecast precise. Future engine gates remain in the
[design reference](../architecture/capital_allocation_workflow.md); they are not
prerequisites for giving the analyst advice already demonstrated in this session.

Individual recommendations, account information and execution follow-up remain in
private owner context. This public review describes the reusable method; it does
not establish a particular portfolio target or quantified improvement in Sharpe ratio.

## What the review changes

### Funding determines the risk effect

Cash-to-AVDV adds equity exposure. It does not reduce the NAV weights of existing
software holdings or concentrated companies. Sale-to-AVDV replaces a different
risky exposure and can change those concentrations. Compare both explicitly; do
not call the cash-funded trade a demonstrated reduction in absolute portfolio risk.

Before costs, for a transfer of fraction `d` of NAV, the change in portfolio beta is
`d × (beta_destination − beta_funding_asset)`, using a common benchmark and estimation
window. Cash-to-equity normally increases beta. Whether an equity-funded switch
reduces volatility depends on the full covariance matrix, not the destination's
standalone volatility or a ticker's domicile. This is portfolio arithmetic and
analyst interpretation, not an estimated beta for the owner's current book.

### AVDV is a deliberate style allocation, not neutral international exposure

The issuer's June 30, 2026 fact sheet describes developed ex-U.S. small companies
with value and profitability tilts. It reports 1,717 holdings, a 0.36% expense ratio,
33.00% Japan, 22.71% materials and 10.05% energy. These dated holdings support the
case for different equity drivers, while revealing country/cyclical exposure that
an “international ETF” label hides. Fund-level sector weights must be multiplied by
portfolio weights and combined with other funds or direct holdings. An international
small-value fund and an energy fund can overlap economically even without sharing
securities. These data do not establish a suitable allocation for any particular
investor. [Avantis fact sheet,
June 30, 2026](https://res.avantisinvestors.com/docs/avantis-international-small-cap-value-avdv-etf-fact-sheet.pdf).

### Use robust construction, not unconstrained historical Sharpe maximization

Boyd and coauthors' practical framework explicitly incorporates trading constraints,
costs and uncertain expected returns/covariance. Its uncertainty penalties reduce
the incentive to exploit small, fragile estimated advantages. Apply this principle
at `allocation/recommendation.py`: version the objective and uncertainty set,
retain hard liquidity/account constraints, and compare feasible outcomes. Require
stability to plausible forecast changes, realistic cost assumptions and different
historical windows. A risk-only or simple target-band portfolio is a useful
challenger; it is not automatically the owner's preferred allocation. No algorithm
name—Markowitz, Black–Litterman, risk parity or machine learning—substitutes for
validation against the actual objective. [Markowitz Portfolio Construction at
Seventy, Journal of Portfolio Management, July 2024](https://web.stanford.edu/~boyd/papers/markowitz.html),
especially sections 3.5 and 4.4 of the author's current PDF.

### Integrate taxes into the trade decision

Moehle and coauthors model expected return, risk, transaction costs and tax liability
together, producing asset buys and individual tax-lot sales. Their tax-aware problem
can be nonconvex, so an implementation must distinguish a feasible heuristic from
a proved optimum. For this project, `advisor/position_tax.py` supplies existing
tax evidence, while the allocation builder owns funding and selection. Prefer
economically equivalent sheltered rebalances without treating every taxable sale
as prohibited. Model near-term tax drag and the value of deferral; do not subtract
one-time gains tax from annual return without a common horizon. [Tax-Aware Portfolio
Construction via Convex Optimization, JOTA, May 2021](https://web.stanford.edu/~boyd/papers/tax_aware_portfolio.html).

Validated FIFO reconstruction is not proof that FIFO is the best available tax-lot
election. Compare specific lots only if broker capabilities, source records and
required identification are supported; otherwise state the actual method. IRS
Publication 550 explains specific identification and the wash-sale window, including
IRA/Roth replacement purchases and spouse purchases. A blanket monthly-trading
assumption from a research model is insufficient for this household. Future buys
and reinvestments must remain part of the check. Do not treat a sheltered sale as
a tax-free withdrawal, or a tax-loss benefit as assured spendable cash. [IRS
Publication 550, 2025 edition, current page accessed September 8, 2026](https://www.irs.gov/publications/p550).

### Allow economically sensible inaction and partial rebalancing

Vanguard's August 2026 implementation discussion supports tolerance bands and
balancing drift against trading costs. Its institutional target-date thresholds
and savings estimates should not be copied into an individual's concentrated
stock portfolio. Adopt the principle: compare no trade, cash-flow-funded correction
and partial/full rebalance; do not force a trade for a negligible estimated gain.
Use target bands rather than a falsely precise mandated weight when the evidence
supports a range of economically similar outcomes. [Delivering on
design, Vanguard, August 14, 2026](https://workplace.vanguard.com/insights-and-research/perspective/delivering-on-design-disciplined-implementation-in-index-based-target-date-funds.html).

### Valuation informs a horizon, not a market-timing certainty

AQR's January 2026 assumptions distinguish five-to-ten-year expected returns from
short-horizon timing and explicitly show wide estimation uncertainty. Those
year-end-2025 inputs are not September spot valuations or an AVDV forecast. They
support using valuation-aware, horizon-consistent ranges rather than extrapolating
the recent software rebound or claiming international stocks must outperform next.
Evaluate participation in equity markets against the recorded investor objective;
a valuation forecast alone does not determine a portfolio's appropriate risk level. [AQR
2026 Capital Market Assumptions, January 14, 2026](https://www.aqr.com/-/media/AQR/Documents/Alternative-Thinking/AQR-Alternative-Thinking---2026-Capital-Market-Assumptions.pdf?sc_lang=en).

## Implementation choices and evidence status

All sources above were accessed September 8, 2026. Publications are dated explicitly;
webpage crawl dates are not treated as new research publication dates.

| Area / owning seam | Decision | Evidence status / remaining limit |
| --- | --- | --- |
| Selection / `allocation/recommendation.py` | One deterministic winner under an explicit robust objective | Supported methodology; objective calibration and representative results still required |
| Return inputs / factor model and ETF workups | Common horizon, benchmark, currency and total-return convention; separate forecasts from evidence | No approved complete same-snapshot current return/covariance input set yet |
| Funding / next-dollar frontier | Distinguish existing cash, external contribution and sale proceeds | Deterministic arithmetic specified; implementation and regression tests pending |
| Taxes / `advisor/position_tax.py` | Account and supported lot selection, deferral sensitivity, no assumed loss benefit | Existing capabilities inspected; per-run account coverage and current tax assumptions require verification |
| Exposure / tracker securities and ETF profiles | Economic look-through plus gross NAV and tax-adjusted wealth views clearly labeled | AVDV example verified against issuer sheet; whole-book classification remains BHA-149 |
| Owner criteria / positioning and capacity | Load current participation, risk, liquidity and sale permissions from private context | Do not infer a new beta target, risk-aversion coefficient or cash floor |
| Trading policy / artifact and frontend | No-trade region, partial correction, same result in Ask/UI | Supported principle; individual thresholds require calibrated policy, not copied institutional constants |
| Validation / tests and research evals | Replay, sensitivity, walk-forward comparisons, tax/account conservation | No out-of-sample superiority claimed; simplified target/cash-flow baseline must remain competitive |

## Execution follow-up

A recommendation does not establish that a trade occurred. When the owner reports
execution, reconcile the trade and account against a fresh canonical holdings
snapshot, then show achieved weight against recorded intent. Do not mark executed,
target-met or account-funded before that evidence arrives. Monitoring requires its
own request; it is not part of invoking this skill.
