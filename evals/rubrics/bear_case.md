# Rubric: bear_case (v1)

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

## Facet: non_consensus — at least two failure modes are genuinely non-consensus

At least TWO failure modes must argue something sell-side coverage does not
broadly flag — a systematically underweighted risk, an organizational
blind-spot, a framing inversion — rather than restating widely-discussed
concerns. Score 1.0 when two or more clear the bar, 0.5 when exactly one
does, 0.0 when none do. The `most_underweighted` paragraph counts as
evidence of intent but does not substitute for the failure modes themselves.

## Facet: evidence_citation — evidence_in_data cites concrete numbers with periods

Every `evidence_in_data` must cite a specific number or trend FROM the
inputs, with the value AND the time period ("FCF dropped from $24.6B Q4'25
to $5.3B Q2'25"). Vague paraphrasing ("growth is decelerating", "margins
are under pressure") without a value-and-period citation is a miss for
that failure mode.

## Facet: quantified_impact — quantitative_impact shows a replicable math chain

Every `quantitative_impact` must link the failure mode to a specific
revenue / margin / FCF / NPV-per-share delta with the reasoning chain shown —
the reader should be able to plug the numbers into a model and replicate the
scenario. A bare directional claim ("this would hurt margins") or a number
with no derivation is a miss for that failure mode.

## Facet: refutation_criteria — falsifiable, disclosure-anchored refutation paths

Every `refutation_criteria` must state what management would have to
disclose or demonstrate over the next 2–4 quarters to neutralize the
hypothesis — specific and falsifiable (a named metric, disclosure, or
event), not "if results improve". Every `leading_indicator` must likewise
be a numerical / disclosed metric observable in the next 1–2 prints.

## Facet: grounding_discipline — no fabricated numbers, out-of-scope risks parked properly

Quantitative claims must be consistent with being derived from the stated
inputs (internally consistent units, periods, and magnitudes — a fabricated
or impossible figure is a hard miss). Risks not derivable from the inputs
must be parked in `out_of_scope_flags` (1–3 entries, each with a one-line
reason) instead of being smuggled into failure modes as unevidenced claims.
