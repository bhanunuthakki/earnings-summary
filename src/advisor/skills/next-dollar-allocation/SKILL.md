---
name: next-dollar-allocation
description: Recommend the next capital-allocation move for the earnings-summary owner's portfolio, using current holdings, account taxes, saved goals and research. Use for next-dollar allocation, cash deployment, tax-aware rebalancing, or consolidating exploratory positions. Produces one preferred analyst plan using existing tools; does not execute trades.
---

# Next-dollar allocation

Run the analyst workflow already demonstrated in this project. Use existing data
readers, position reviews and comparison tools; a new optimizer or frontend is not
a prerequisite. Make the evidence collection and arithmetic reproducible, while
labeling the final choice as analyst judgment unless an actual validated optimizer
produced it. Do not promise that free-form reasoning always selects identical trades.

## Establish context without re-interviewing the owner

Resolve the earnings-summary checkout. Read its current host/data authority before
live access. The installed skill may be a symlink: resolve its real path before
locating the repository. Use [existing interfaces](references/interfaces.md) to
avoid rediscovering routes or accidentally starting a background job.

Load current capacity, positioning/sizing intent and account tax treatment. Carry
forward explicit preferences and permissions from the conversation and recorded
owner context. “Run my next-dollar allocation” uses those criteria; a current
override such as “cash only this time” narrows this run without rewriting durable
preferences. Do not infer a permanent reserve from a reported cash percentage.
Recover any missing session-specific decisions from the existing private owner
context or authorized Linear handoff. Personal goals, account data and recommendations
belong there, not in the public skill. Historical weights and dollar amounts are not
current inputs or permanent rules.

## Collect the decision inputs

1. Read the full available holdings/account snapshot, available cash and cash
   equivalents, reserved liquidity, totals and freshness. Include ETFs, mutual
   funds and small positions; the research roster or top-ten holdings pack is not
   the full book. Reconcile account and portfolio totals without double counting.
2. Read existing target bands and position-tax pre-analysis for likely buys and
   funding candidates. Reuse validated lots/tax estimates. Unknown treatment or
   basis is not zero tax; retirement sales are distinct from withdrawals. Consider
   supported lot elections and cross-account wash-sale exposure for taxable losses.
3. Read current company theses, valuation evidence and fund workups for candidates.
   Use current primary issuer/research sources for gaps and drift-sensitive claims.
   Do not require a corporate DCF or corporate thesis of an ETF. Examine underlying
   style, country, sector and currency exposures, including overlap with other funds.
4. Inspect usable full-book risk/comparison evidence. Retain as-of, coverage, horizon
   and methodology. Do not combine DCF price upside with annualized fund returns,
   or compare z-scores to Sharpe basis points as if they were the same unit.

On a failed read, use an available authorized source or state the specific gap;
avoid repeated unchanged attempts. Missing full-book data limits claims of a
complete ranking, but need not prevent a supported, explicitly scoped recommendation
such as moving an observed underweight holding toward recorded intent. Never turn
data failure into an investment recommendation to stay in cash.

## Form and compare feasible actions

Start with doing nothing and using available cash. When sales are permitted,
consider reducing concentrated/overlapping positions and closing exploratory
positions whose thesis or portfolio role no longer earns their place. Small size
alone is not a sale thesis. Compare sheltered and taxable funding after costs;
prefer the lower-friction route when investment outcomes are comparable, but allow
a taxable sale to win when its benefit justifies the tax.

Calculate dollar amounts and before/after weights with code or existing deterministic
helpers. Existing cash is already in NAV. With external contribution `E` and costs
actually paid `C`, post-trade NAV is `N + E - C`; retained contributions also enter
NAV. A position becomes `V - gross_sales + buys`. Retained tax reserves remain cash
inside reported NAV and reduce deployable cash, not NAV twice. Model accrued taxes
separately if comparing after-tax economic wealth. Purchases must be funded within
their identified accounts; do not assume transfers, loans or retirement withdrawals.

Distinguish cash-to-equity (adds equity risk and leaves other stock weights unchanged)
from equity-to-equity replacement. Evaluate the owner's equity participation,
concentration, overlapping business/style risks, liquidity and after-tax outcomes.
Check whether a plausible change in forecasts or market scenario reverses the
choice. Prefer a simpler plan or partial rebalance when the modeled advantage is
fragile or too small to justify costs. Existing target bands guide judgment; they
are not proof of a mathematically unique optimum.

## Deliver the advice, not a menu

Lead with one preferred plan and why it best serves the stated goals. Show the
proposed account, buy/sell/retain action, dollars, before/after weight and tax/cost
estimate where evidence supports them. State the funding sequence, cash left and
liquidity constraints. Give the incremental benefit of allowing sales versus the
cash-only case; keep rejected alternatives secondary. Explicitly address candidates
the owner named, including whether to increase, hold or reduce them.

Separate observed facts, calculations and analyst judgment. State what could change
the recommendation and the precise scope of any incomplete evidence. Do not invent
sale amounts, account funding, expected-return improvements or risk statistics to
fill the format. Save the analysis and a source/input manifest under the project's
configured private output authority when a durable run is requested; never commit
owner-specific analysis to the public repository. Retain: source references/as-of, effective criteria,
overrides, arithmetic, compared actions, preferred plan and evidence gaps. A prior
run is context, not a substitute for new holdings/prices.

Advice does not execute a trade or overwrite owner constraints. When the owner
later reports execution, reconcile it with the current canonical snapshot and
record confirmed versus pending achieved-weight evidence through existing workflows.
Do not mark executed or target-met from this recommendation.
