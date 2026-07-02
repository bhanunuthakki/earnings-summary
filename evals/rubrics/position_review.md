# Rubric: position_review (v1)

Grades a persisted position-review verdict (the `/review` service's `position_review`
advisor-memo) on whether the trim / hold / add call is sound, respects the owner's
documented decision-making, is specifically grounded, and picks a fitting expression.
The memo body carries the verdict, the reason, the behavioral-check line, the suggested
expression, and the grounded facts (weight, break-rule status, valuation ladder verdict,
concentration flag) it was built from — grade the call against those facts.

Pass threshold: 0.70

## Facet: verdict_soundness — the trim/hold/add call follows from the grounded facts
Score 1.0 when the verdict is entailed by the facts: HOLD on an intact, non-over-valued,
non-oversized name; TRIM/SELL only when a falsifier tripped, the valuation ladder says
trim/sell, or the weight is above band / a flagged single-name concentration; ADD only
with a genuine discount and catalyst.
Score 0.5 when defensible but only weakly supported by the stated facts.
Score 0.0 when it contradicts the facts — most importantly a price-only TRIM/SELL on a
name the facts show is intact, not over-valued, and not oversized.

## Facet: behavioral_calibration — respects the owner's documented patterns
Score 1.0 when the behavioral-check names the specific pattern at risk
(sell-winners-too-early, catalyst-test, or instrument-selection) and the verdict honors
it — never recommending a price-only trim of a healthy, correctly-sized winner, and never
an add into weakness without a near-term catalyst and a priced-in floor.
Score 0.5 when the calibration is acknowledged generically but not tied to this decision.
Score 0.0 when it ignores or inverts the owner's calibration (e.g. counsels the
sell-winners mistake, or a catalyst-less add).

## Facet: grounding — the reason names a specific driver, not a vibe
Score 1.0 when the reason cites the concrete driver: which break-rule/threshold, which
DCF ladder step (over/under vs the mos bar), or which sizing band / weight figure.
Score 0.5 when partially specific.
Score 0.0 when generic ("it's had a strong run", "still a great company") with no grounded
driver a reader could check.

## Facet: expression_fit — the suggested expression matches the situation
Score 1.0 when the suggested expression fits: trim-to-target for an oversized weight;
LEAP-overlay only for an undersized or previously-exited high-conviction name (NOT an
already-full, fully-valued line); encode-thesis-first for an unencoded name; do-nothing
for an intact, fairly-valued, correctly-sized hold.
Score 0.5 when plausible but not the best-fit expression.
Score 0.0 when the expression contradicts the verdict or the owner's instrument-selection
discipline (e.g. a LEAP on an already-full, richly-valued position).
