# Rubric: bear_case (v2)

Pass threshold: 0.70

Scope: one `data/bear_case/<TICKER>.json` artifact — the structured bear case
(`failure_modes[]`, `most_underweighted`, `out_of_scope_flags[]`) produced by
`llm_client.generate_bear_case`. The facets below are distilled from that
prompt's own BAR rules; the judge scores the OUTPUT against the bar the prompt
already demands, so a low score means the model under-delivered, not that the
goalposts moved. (directives/llm_evals_plan.md §3 PR 2.)

Grading convention per facet: 1.0 = the bar is met across the whole artifact;
0.5 = met in some failure modes but materially missed in others; 0.0 = the bar
is broadly missed. Judge only what is in the artifact — no outside knowledge
of the company, no penalty for analysis the inputs couldn't support.

## Facet: ticker_specificity — failure modes name THIS business's mechanics

Every `failure_mode.hypothesis` must be specific to the company's business
model — its pricing, unit economics, regulatory exposure, capex profile,
channel concentration, switching-cost economics, named segments or products.
Generic risks ("revenue could decelerate", "macro could weaken",
"competition intensifies") are automatic misses for that failure mode. A
hypothesis that could be pasted into any other ticker's bear case scores 0
for this facet.

## Facet: contrary_case — substantive company-specific counter-evidence

Test the strongest plausible failure mechanisms against supplied support and
counter-evidence. Do not claim a risk is non-consensus without a dated consensus
reference. Explicitly unavailable consensus earns no penalty. Unsupported claims
about what the market believes are misses.

## Facet: evidence_citation — evidence_in_data cites concrete numbers with periods

Every `evidence_in_data` must identify supplied evidence with period and locator.
Use figures when supplied and comparable. A specific cited qualitative disclosure
is valid evidence. Missing evidence must remain explicit; never invent a number
to satisfy this facet.

## Facet: quantified_impact — quantitative_impact shows a replicable math chain

Where model inputs permit a calculation, show the replicable assumption and
math chain with comparable units. Otherwise name the affected model assumption
and missing inputs. Explicit inability to quantify is valid; invented valuation
or magnitude is a miss.

## Facet: refutation_criteria — falsifiable, disclosure-anchored refutation paths

Every `refutation_criteria` must state what management would have to
disclose or demonstrate over the next 2–4 quarters to neutralize the
hypothesis — specific and falsifiable (a named metric, disclosure, or
event), not "if results improve". Every `leading_indicator` must likewise
name a public source, observable condition, expected event and investment consequence. A specific qualitative disclosure check is valid.

## Facet: grounding_discipline — no fabricated numbers, out-of-scope risks parked properly

Quantitative claims must be consistent with being derived from the stated
inputs (internally consistent units, periods, and magnitudes — a fabricated
or impossible figure is a hard miss). Risks not derivable from the inputs
must be parked in `out_of_scope_flags` (1–3 entries, each with a one-line
reason) instead of being smuggled into failure modes as unevidenced claims.
