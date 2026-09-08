# Whole-portfolio capital allocation

Status: skill-first scope corrected by the owner, September 8, 2026.
The reusable [next-dollar-allocation skill](../../src/advisor/skills/next-dollar-allocation/SKILL.md)
packages the demonstrated analyst sequence using existing readers and calculations.
It is installed locally for agent invocation. No new optimizer or frontend is needed
to use it. App routing and frontend integration remain later roadmap work.

Roadmap: [BHA-150](https://linear.app/bhanu-personal/issue/BHA-150/next-dollar-recommend-one-tax-aware-whole-portfolio-plan-across-cash),
High priority. BHA-149 limits complete quantitative coverage; it does not prevent
using the skill for evidence-supported, explicitly scoped advice.

The owner explicitly approved this extension and requested a current-research pass.
The [allocation-method review](../research/allocation_method_review_2026-09-08.md)
records that pass, its primary sources, applicability and remaining evidence gaps.
The larger engine design below is retained as a possible future extension, not a
prerequisite or a commitment to build an optimizer now. Its deterministic-selection
and UI acceptance gates apply only if those components are implemented. The skill
makes evidence collection and arithmetic repeatable; final advice remains analyst
judgment. No executed trade or unique optimal target is established by this work.

## Intended answer

“Given my current portfolio, accounts, taxes, goals and current research, what should
I do with capital now?” should produce one preferred, account-funded plan. It must
consider deploying existing cash, new contributions, sales, and consolidating small
exploratory positions when the owner's current permissions allow them. Recommendations
never execute broker orders. Load investment goals, risk constraints, liquidity needs
and account permissions from private owner context at run time. A reported cash
percentage is a question trigger, not automatically an approved permanent cash floor.

## Existing capabilities and gaps

Private portfolio observations and session-specific targets are retained in the
owner's existing private context and Linear handoff. Public documentation contains
only reusable methodology and code findings. Incomplete aggregate classification or
partial position reads cannot establish a complete same-snapshot full-book model.

### Reusable implementation

| Existing owner | What is reusable | What is missing for this question |
| --- | --- | --- |
| `allocation/model.py` | Return, marginal-risk and macro factor calculations | Relative cross-sectional scores and softmax shares are not optimized portfolio weights; the documented book excludes funds/cash |
| `allocation/recommendation.py` | Evidence gate, bounded candidate frontier, concentration and affirmed human-capital caps | Only add/retain plans; external-new-cash funding; no sales, tax/account allocation or full-book joint optimization |
| `allocation/recommendation_artifact.py` and `recommendation_schema.py` | Governed artifact, validation, source references, fallback and dispositions | LLM selects the preferred plan; same evidence need not produce the same choice; cash-only schema cannot express sell legs or account funding |
| `allocation/what_if.py` | Portfolio risk comparison and new-cash/pro-rata scenarios | Pro-rata reallocation is not an implementable account/lot-specific funding plan |
| `advisor/context.py::screen_swap_candidates` | Existing holding-versus-candidate valuation screen | DCF-margin screen selects the highest-upside candidate, not a joint after-tax portfolio plan |
| `advisor/position_tax.py` | Sheltered/taxable separation, validated reconstructed lots, approximate tax range, holding-period and wash-sale checks | Not integrated into next-dollar plan construction or ranking |
| `integrations/portfolio_tracker_v1.py` | Stable security and account identity, positions, tax treatment, coverage and freshness | Allocation consumer must retain all positions and same-snapshot joins |
| `integrations/portfolio_allocation.py` | Reconciled typed allocation projection | BHA-149: known fund/cash metadata gaps and aggregate unexplained remainder |
| `positioning/target.py`, `owner_profile/store.py`, sizing intents | Existing versioned goals, constraints, targets and provenance | Resolve these once into a typed decision request; preserve explicit owner vs analyst-assumption distinction |
| `ask/packs.py` | Holdings/tax context, current allocation artifact | Whole-book holdings pack truncates to ten rows; allocation pack retrieves an old artifact rather than running a complete funding analysis |
| `pipeline/allocation_recommendation_panel.py` | Preferred-plan presentation, details, compare, saved intent | Form asks only for new cash and horizon; no sale/account/ETF plan visibility |
| Work OS thresholds and Copilot | Existing next-dollar doorway and conversation | Must invoke/read the same allocation result, with full-book coverage rather than research-roster-only hydration |

The prior Performance redesign deliberately removed the Next Dollar card from that
page (BHA-79). Keep its accepted information hierarchy. Add a compact doorway in the
existing Portfolio Copilot/threshold workflow; do not restore a duplicate dashboard.

## Possible future engine contract and execution path

Extend the current recommendation entry point and artifact rather than keeping the
legacy cash allocator and a separate rebalance answer. Use an explicit typed request
for funding mode (`existing_cash`, `external_contribution`, or `rebalance`), permitted
sales, account restrictions, liquidity reserve, horizon, current target revision,
and analyst assumptions with provenance. A conversational request is compiled into
that contract through the existing governed structured route. After that boundary,
calculation and selection are deterministic. No keyword router or model-produced
trade arithmetic.

Run these stages in order and retain their receipts:

1. **Reconcile the book.** Use every included account and security, including money
   markets, ETFs, mutual funds and small exploratory holdings. Reconcile positions,
   account totals, cash representations and NAV; expose unresolved securities by
   identity and reason. Cash-equivalent exposure is distinct from settled purchasing
   power and reserved cash. Never renormalize an incompletely modeled subset.
2. **Resolve needs.** Read current owner capacity, account tax treatment, sizing
   intents and positioning targets. Record the revision and source of every binding
   constraint. Reuse earlier explicit sale authorization. Missing assumptions are
   individually visible; proposed recommendations need not be reapproved merely to
   calculate them, but cannot silently become durable owner facts.
3. **Admit the universe.** Include held stocks/funds and relevant evaluated
   diversifiers. AVDV is an explicit candidate, not a preselected winner. Use the
   existing ETF workup/price/exposure evidence for funds; corporate DCF/KPI gates
   cannot be required of an ETF. Distinguish fund identity/classification from
   adequacy of the evidence needed to recommend a particular weight.
4. **Build comparable plans.** Always include do-nothing and cash-only deployment.
   With sales allowed, also construct funded sheltered-account consolidations,
   concentrated-position trims, and taxable alternatives. Compare AVDV unchanged,
   increased and replaced by the best admitted substitute. Model correlated
   company/business exposure, broad equity beta and style/geographic exposure
   across the entire book. A new company name does not prove diversification.
5. **Price account-specific funding.** Allocate buys to identified accounts using
   their own cash and net sale proceeds. Prefer a sheltered sale only when its
   economic result is competitive. Do not assume free transfers between accounts,
   retirement withdrawals, fractional-share capability, or an unsupported tax-lot
   election. Apply actual transaction costs and current tax assumptions. Use the
   existing validated lot reconstruction; otherwise show and rank conservatively
   against its approximate tax range. Unknown tax treatment is not sheltered.
   Cross-account wash-sale risks remain visible; do not count unvalidated loss
   deductions as spendable tax savings.
6. **Select one plan.** Apply hard constraints first. Compare after-tax expected
   outcomes over the same explicit horizon and common risk/scenario assumptions.
   Do not compare DCF upside directly with annualized ETF returns, or marginal-vol
   z-scores with Sharpe basis points. Reward improved compensated exposure, penalize
   concentrated/shared risks, taxes, turnover and unnecessary position count using
   an explicit versioned objective. Evaluate uncertainty in return estimates and
   covariance, plus growth-led rally, growth repricing, recession and inflation
   scenarios. Prefer a simpler, lower-tax plan when the modeled advantage is within
   uncertainty. Use stable tie-breaking; the model may explain but cannot select a
   different winner or invent trades. “Best among evaluated feasible plans under
   these assumptions” is the supported claim; a global optimum requires proof.
7. **Return and retain the decision.** Save the same typed artifact and complete
   input/source manifest for UI, Ask and other existing consumers. Read/render must
   not launch providers or recompute a plan. Generation is an explicit action with
   stage status, retry/resumption and an operational receipt. Saving a provisional
   intent remains separate from changing owner constraints and from trading.

## Funding arithmetic

Let `N` be current full NAV, already including cash; `E` external contributions;
`S_i` gross sales; `B_i` buys; and `C` costs and taxes actually paid from the portfolio.
Post-plan reported NAV is `N + E - C`. Retained tax reserves remain portfolio cash
and reduce deployment capacity, not reported NAV. Accrued tax liabilities belong
in a separately labeled after-tax wealth comparison. For a position with value `V_i`,
its post-plan value is `V_i - S_i + B_i`. Account purchasing power is opening
available cash plus that account's external contribution and sales, less buys and
account-funded costs/reserves. Every account must reconcile without implicit loans
or transfers. Economic tax liabilities funded outside the account still reduce
whole-wealth outcomes and must have a named funding source.

Deploying $5,000 already inside a $100,000 portfolio into a $10,000 position results
in 15% weight before costs. Contributing $5,000 from outside produces 14.2857%.
Retained external contributions still enter NAV. The current allocator instead
increases the denominator only by deployed dollars, so partial deployment also
needs a regression. Never manufacture higher diversification by double-counting
cash or silently normalizing modeled equities to 100%.

## Research-reviewed construction requirements

- Use a constrained robust optimization formulation with return/covariance
  uncertainty and explicit costs. Keep estimates, owner constraints and analyst
  preferences separately versioned. Do not market a particular solver or a historical
  maximum-Sharpe portfolio as inherently superior. Begin with the existing long-only,
  unlevered funding boundary unless current owner authority explicitly permits more.
- Compare against do-nothing, simple target-band/cash-flow rebalancing and a
  diversification-focused challenger. Use walk-forward observations and realistic
  tax/cost assumptions; prevent future data or tuned-on-test parameters from leaking
  into the comparison. Fixed replay seeds, canonical ordering, pinned method versions
  and solver tolerances are part of reproducibility.
- Use an explicit no-trade region and permit partial rebalancing. Small estimated
  improvements inside forecast uncertainty do not justify turnover. Calibrate the
  trade threshold for this book; do not import institutional fund rebalancing bands.
- Incorporate supported tax-lot choice, not only reconstructed FIFO. Reuse FIFO when
  it reflects the actual supported method, but distinguish it from tax-optimal lot
  selection. Account for tax deferral, potential near-term holding-period changes
  and cross-account replacement purchases. No automatic claim of usable loss offsets.
- Preserve conventional gross-NAV reporting and label any tax-adjusted household
  wealth calculation separately; traditional retirement and Roth dollars are not
  automatically equal in after-tax spending value. Expose assumptions rather than
  silently discounting account balances or changing reported holdings weights.
- Show economic fund look-through, not just a distinct ticker or country bucket.
  AVDV's dated issuer mix includes energy/materials and country concentration; count
  these alongside VDE and direct equities. Unknown fund exposure stays quantified.
- Distinguish replacing cash from replacing an equity: at fixed NAV a cash-funded
  AVDV buy leaves existing company weights unchanged and usually increases equity
  beta. A sale-funded buy can reduce the sold exposure. Neither a historical
  correlation nor an ETF label proves lower absolute portfolio volatility.
- Retain a simple policy baseline if the sophisticated model fails stability or
  after-cost comparison. A feasible heuristic must report its scope and gap to any
  available bound; do not label it a proved global optimum.

## Output and frontend visibility

Lead with **Recommended plan** and a short explanation of why it beats staying put.
Show an ordered action table: account/tax bucket, buy/sell/retain, security, dollar
amount, before/after book weight, estimated tax/cost, and reason. Include:

- AVDV: current and proposed weight, funding source, what it displaces, expected
  benefit and what would reverse the recommendation.
- VDE: increase/hold/reduce, with energy-equity exposure distinguished from a
  guaranteed inflation hedge.
- Net deployable cash and reserves after all legs; no account overspending.
- Before/after concentration, shared software/growth exposure, equity beta,
  expected-return range and modeled stress outcomes, each with coverage/as-of.
- A compact cash-only counterfactual: how much allowing sales improves the outcome
  after taxes and costs. Other plans remain in expandable evidence, not a menu
  substituting for advice.
- Any held candidate excluded from modeling, the exact missing evidence and whether
  it prevents ranking, sizing or execution. A data-blocked plan is not an investment
  recommendation to retain cash and is not an all-clear.

Show the effective criteria and source revisions in Details. Let the owner revise
criteria and rerun; do not repeatedly ask them to restate goals already recorded.
Use the existing registered panel/control/overlay families and next-dollar doorway.

## Required acceptance evidence

Use representative synthetic portfolio/account fixtures and approved live read-only
evidence after separately authorized activation:

1. Identical snapshot, criteria, candidate evidence and engine version reproduce
   identical trades, ranking and semantic input hash. Observation wall time is
   metadata, not a source of cache misses or a ranking input.
2. Known money-market holdings are identified and reconciled; unresolved ETF
   geography does not make known cash disappear. Every small holding is evaluated.
3. Existing cash, external cash (including retained contribution), partial buys,
   taxes, reserves and sale-funded buys satisfy the equations above. Weights and
   account ledgers reconcile, including fees and rounding.
4. A taxed appreciated position and an economically equivalent sheltered position
   produce different funding choices. A materially better taxable sale can still
   win after taxes. Unknown basis/treatment and wash-sale risk are explicit.
5. An AVDV increase can win while maintaining the configured beta/risk constraints;
   an already sufficient AVDV weight or weaker after-tax case can cause hold/reduce.
   The same scenario works when AVDV has no corporate DCF.
6. Consolidating small exploratory positions is evaluated against their economic
   value and overlap, not triggered mechanically by small size alone.
7. Return uncertainty, insufficient risk-history coverage, stale/missing accounts,
   blocked classification and infeasible constraints cannot yield a confident
   optimized result. No equal-weight fallback masquerades as this owner's book.
8. UI and Ask show the same plan, exact assumption revisions, coverage and stale
   state. The model cannot change the selected trades. Refresh and saved-intent
   actions preserve existing idempotency and authority boundaries.
9. Browser evidence covers the preferred plan, cash-only comparison, criteria
   inspection, loading, data-blocked/error/retry, stale result and supported widths.

## Delivery sequence

1. **Now:** package the existing analyst workflow as the locally installed
   `$next-dollar-allocation` skill. Natural-language invocation: “Run my next-dollar
   allocation.” Reuse current goals, accounts, permitted sales and existing tools;
   produce one preferred plan with arithmetic and evidence limits.
2. **Data repair:** retain BHA-149 for the classification and full-book coverage
   defects. Expose gaps precisely while allowing supported scoped advice now.
3. **Later:** connect the same workflow to the app's existing Copilot/threshold
   doorway and retained result display. Frontend work stays on the roadmap.
4. **Only if justified by demonstrated limitations:** extend typed funding artifacts
   or implement a quantitative optimizer. The future-engine gates above govern that
   work; they do not block the analyst skill.

The skill installation changes agent instructions only. The app runtime, live roster,
portfolio state and existing allocation endpoint are unchanged.
