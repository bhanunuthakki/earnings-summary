# Rubric: calibration_coach (v1)

Pass threshold: 0.70

Scope: one generated monthly calibration scorecard (the prose rendered by
`calibration_coach.render_scorecard_prose`) — the named recurring biases and the
period's behavioural experiment, synthesised from the owner's OWN track record
(graded decisions, conviction calibration, realized selection/sizing/timing
skill, closed-position lessons). This is the eval gate the directive (L8)
requires: a coach that misreads the owner's history is worse than silence, so
the synthesised prose is judged here BEFORE it reaches him; below threshold it
is suppressed and only the deterministic substrate renders.

The scorecard prose contains the deterministic context (overall hit-rate, trend
direction, realized-skill edge/leak) followed by the synthesised coach output
(the biases, each with its cited evidence and "tell", and the experiment). The
deterministic context IS the ground truth — judge whether the synthesised
biases and experiment are anchored to it, not whether the numbers themselves are
right (they are computed, not generated). **Grade the synthesised coach output
against the context it claims to rest on.**

Grading convention per facet: 1.0 = the bar is met across the output; 0.5 =
partially met (met for most but materially missed in places); 0.0 = broadly
missed. A scorecard that honestly names FEWER biases because the evidence
supports fewer is not penalised — padding to fill space is the miss, not
brevity. A scorecard with no synthesised biases at all is out of scope for this
rubric (nothing to gate).

## Facet: grounded_in_own_history — every bias is built from the owner's own numbers

Each named bias and the experiment must trace to a specific fact in the
deterministic context: a conviction-bucket hit-rate, the trend direction, a
realized-skill leg (selection/sizing/timing), a reversal count, or a named
closed position's own lesson. A bias asserting a pattern the context does not
evidence — generic market wisdom ("buy low, sell high"), or a claim about a
metric not present — is a hard miss for that bias. The cited "evidence" strings
must actually appear in or follow from the context, not be invented. Score 1.0
when every bias and the experiment are anchored to the owner's own data, 0.5
when most are but one floats unanchored, 0.0 when the output reads as generic
coaching detached from this owner's record.

## Facet: named_and_specific — biases are named patterns, not vague observations

Each bias must be a NAMED, specific recurring behaviour ("reverses winners
early", "oversizes high-multiple adds", "high conviction doesn't earn its
label") with a concrete description — not a vague platitude ("could be more
disciplined", "watch your risk"). The name should capture the pattern in the
owner's own terms. Score 1.0 when each bias is a sharply-named, specific
pattern, 0.5 when at least one is vague or could apply to any investor, 0.0 when
the "biases" are generic observations that name no actual pattern.

## Facet: falsifiable_and_actionable — the tells and experiment are observable and checkable

Each bias must carry a "tell" — an OBSERVABLE signal that the owner is repeating
it right now (e.g. "an add on a name already above your median weight at 5/5
conviction"), not an unfalsifiable mood. The behavioural experiment must end in
a falsifiable, numeric condition future data can satisfy or not, and must be a
PROCESS change (sizing/reversal/conviction discipline), not a stock pick. Score
1.0 when every tell is observable and the experiment is falsifiable and
behavioural, 0.5 when a tell is vague or the experiment is only loosely
checkable, 0.0 when the tells are unobservable or the experiment is not
falsifiable (or is a disguised buy/sell call).

## Facet: honest_calibration — the read respects the strength of the evidence

The coaching must be proportioned to the evidence: it must attack the leak the
data actually shows (e.g. if sizing is the most-negative realized leg, the
experiment should target sizing), and must not over-claim a confident pattern
from a thin or conflicting record. An output that confidently asserts a
sweeping bias the data only weakly supports, or that names a "leak" the skill
split contradicts, is the core miss. Honestly scoping a pattern to what the
evidence bears is full credit, not a hedge. Score 1.0 when the biases and
experiment match the evidence's actual shape and strength, 0.5 when the read is
somewhat over-confident relative to the data, 0.0 when it asserts conclusions
the owner's record cannot bear.
