# Forecast-assumption method

Use when a task builds or revises the forecast assumptions of a valuation model
or a multi-year estimate. This method owns how an assumption is derived, recorded
and tested. [Analysis paths](analysis-paths.md) own the valuation mode and the
[research method](research-method.md) owns evidence and judgment mechanics.
The shared calculator `src/dcf/driver_model.py` owns the record fields, the
ranking, the joint stress and the identity check. This method adds no database
table, acquisition route or write permission. It does not change the owner's
model inputs; a proposed assumption stays a proposal until the owner or the
model's existing review path accepts it.

Apply it to the positions where the forecast can change a decision. A model whose
growth path is fitted to consensus is a consensus-fade estimate. Label it as such;
do not present it as an independent forecast.

## Start from the decision question

1. State the question that the model must answer, in one sentence. Use the
   current investor debate or the thesis line under test, for example "how much
   does share loss reduce revenue". A model without a question is a table.
2. Put the effort where the result is decided. Model the largest and most
   disputed line in detail. Give a small or stable line one growth rate and one
   margin, and say that it does not change the result.
3. Form the view from the drivers first. Compare it with consensus and with the
   latest run-rate afterwards. Do not fit the drivers to consensus.

## Build the driver tree

1. State each revenue line as a product of business drivers, for example
   volume × share × units per customer × price. Stop at a driver that has a public
   observable, or record that none exists.
2. Give a cost its own line when two opposing drivers move it, for example usage
   per unit against cost per unit. Do not hide it in one margin ramp.
3. Choose each path shape from the mechanism: an adoption curve with a stated
   ceiling, a fade to a mature rate, a contractual rate. Give the reason for the
   ceiling or end value. A straight line needs a reason too.
4. A parameter that stays constant can cancel out of a growth index. Shock its
   change through time, not its level.
5. For a product with no revenue history, separate the fixed cost, the variable
   cost as a share of revenue, and the start date. State the years of loss.
6. A contract or a rule limits some paths, for example a revenue-share rate.
   Use it as a bound on the margin path and cite it.
7. Do not extend a recent favourable trend when its cause weakens. If margin
   grew with fast revenue growth, a slower forecast needs a slower margin gain.
8. When two effects oppose and the evidence does not show which is larger, use
   a value near neutral, state both effects and set confidence to low.

## Find the anchor

Use this source order for each driver. Record the class that supplied the value.

1. **Reported fact:** issuer filings and releases through the shared readers.
   Check the stored KPI facts, management-indicator observations and IR documents
   for call and presentation metrics before any other source.
2. **Management statement:** a dated, cited claim. It proves what was said.
3. **Calculation:** derived from the classes above. Show the formula and inputs.
4. **External estimate:** third-party data, a sell-side or independent analyst
   estimate, or an analog company. The owner permits these (decision of
   2026-11-16) for drivers the issuer does not disclose and as cross-checks.
5. **Judgment:** the model builder's assumption. Say so.

For an undisclosed cost structure, an analog company with reported costs is an
external estimate. State why the analog fits and where it does not.

Rules for an external estimate: record the publisher, date, what was measured,
the method if known and the stated or estimated error. Prefer the direction of a
series over its level when the error is large. Compare two independent sources
when available. It never replaces a company-reported value and never joins a
canonical reported series. Short attributed quotes only; do not store the text.

If a metric that the issuer reports, or that a KPI definition expects, is absent
from the stores, report the capture gap and its owner. A direct read of the filing
can continue the analysis. State that the value did not pass the resolver, and
do not call the output decision-grade.

## Record each material assumption

Keep one record for each driver that can move the result. Use the
`DriverAssumption` record of the shared calculator. An empty field, an external
estimate without publisher, measure and error, or a one-sided shock range stops
the calculation. Cited prose is sufficient only for a quick answer with no model.

| Field | Content |
| --- | --- |
| Driver and role | Its position in the formula |
| Base path | Value, shape and end state |
| Anchor | Class, source, date and locator; reported history beside the value |
| Counter-argument | The strongest reason the value is wrong, and its direction |
| Confidence | High, medium or low, with the reason |
| Shock range | The adverse and favourable values, and why that range is reasonable |
| Output effect | Change in the decision output for each shock |
| Public observable | Source, disclosure and cadence that will test it, or "none" |

When the assumption differs from its reported history or from consensus, state
the difference and the reason. Name each material item that is not modeled and
the direction of its effect.

## Check the model before use

- **Identity:** the driver product must reproduce reported history within
  rounding, for each period with disclosed drivers. Explain a larger gap.
- **Base year:** an estimated base year states its reported part, its estimated
  part and the rule for the estimate.
- **Run-rate:** compare the first forecast year with the latest reported period.
  A sharp change in growth or margin needs a named cause.
- **Reconciliation:** when two facts appear to conflict, find the driver value
  that makes both true and test that it is plausible.
- **Residual:** a value backed out as "what is left" is a calculation that
  absorbs every other error. Label it and give it low confidence.
- **Implied quantities:** convert the base case into quantities that a reader
  can judge: units handled against today, revenue per unit against today, a
  cost as a share of the revenue it serves. Report the ones that look extreme.
- **Cross-check:** compare outputs with an independent estimate when one exists.
  Report an unexplained gap; do not tune the model to remove it.

Use code or the existing model calculations for all arithmetic.

Then apply the [draft valuation sense checks](research-method.md#sense-check-draft-valuations)
for returns on capital, terminal economics, the bridge to consensus and price,
and the value-chain cross-check. This method does not repeat them.

## Rank the drivers and stress them together

Write the model as one pure function from a parameter set to the decision
output. Give that function and the records to `rank_drivers` in the shared
calculator. It shocks one driver at a time across its recorded range, ranks the
drivers, marks those that move the output by at least 3% as material and moves
the material drivers together in each direction. Report each effect in currency
and percent, for both directions; effects can be asymmetric. Say which lines do
not matter. For a model with a price output, use `solve_monotonic` in
`src/dcf/reverse_valuation.py` to find the value of the top driver that the
market price implies.

`grade_blockers` lists the reasons that the result is not decision-grade: a
reported fact read outside the resolver, a material driver that rests on an
external estimate or on judgment, a material driver with no public observable,
a model older than the latest report, and a consensus-fitted path. Show the
list. An empty list does not grant the grade.

## Connect the assumptions to earnings

The public observables of the top drivers are inputs to "Numbers to check the
moment they print" in the pre-earnings brief. After a report, name each assumption
that the new evidence confirms, weakens or replaces. State the model's as-of date
and latest reported period with every output. A model older than the latest
report is stale for the drivers that the report tested; say which ones.

## Delivery

Lead with the question, the conclusion, the ranked drivers and the joint
stress. Keep the records and source detail behind them; `render_markdown` in
the shared calculator gives this order. State the confidence of the conclusion
in plain words. If the main driver has low confidence, say that the result is
one reasonable path and not a point estimate. Keep the model script, its
parameter set and the report together under `output/research/<TICKER>/`. Separate reported fact, management claim,
external estimate, calculation and judgment in the text. Missing evidence limits
the conclusion. The delivery checks of the research method still apply.
