# Rubric: behavior_distill (v1)

Grades one staged behavioral rule (an `owner_profile_facts` row, `category='behavioral'`,
`provenance='derived'`, always `status='proposed'`) distilled by
`synthesis.behavior_distill` from the owner's OWN graded `decisions` corpus. The judged
content is the row's `narrative` — the rule text plus the wrong/total tally computed from
its VALIDATED citations (decision ids that really exist and really carry the claimed
graded outcome; a hallucinated citation is dropped before this narrative is ever
assembled, so the tally here is trustworthy by construction — grade the RULE, not the
citation-validation machinery).

Pass threshold: 0.70

## Facet: falsifiable_and_specific — the rule is a real, checkable behavioral pattern
Score 1.0 when the rule names a specific decision-making tendency (e.g. "you sell winners
too early", "you skip the catalyst test on cheap names") stated as a direct second-person
instruction, not a vague truism ("be disciplined") or a restatement of one decision's
rationale.
Score 0.5 when specific but narrower than a generalizable pattern (reads like a note about
one name, not a repeating tendency).
Score 0.0 when generic, ungrounded, or unfalsifiable — nothing a future graded decision
could confirm or refute.

## Facet: evidence_grounded — the graded tally actually supports the claimed pattern
Score 1.0 when the wrong/total figures in the narrative plausibly support the stated rule
(a rule about a recurring mistake backed by a majority-wrong or clearly-lopsided tally; a
sparse-n hedge is honestly present when the denominator is small).
Score 0.5 when the tally is present but only weakly supports the rule as stated (e.g. a
near-even split asserted as a strong pattern with no hedge).
Score 0.0 when the narrative asserts a pattern the stated tally contradicts (e.g. "you
always X" backed by a majority-correct record), or omits the evidence entirely.

## Facet: second_person_actionable — reads as a direct instruction the owner can act on
Score 1.0 when the rule is phrased as a directive ("you sell winners too early — the ONLY
price-agnostic reason to trim ...") that a coaching surface could quote back verbatim, in
the voice of `_behavioral_rules`'s existing five seed rules.
Score 0.5 when the content is right but phrased as third-person observation ("the owner
tends to ...") rather than direct address.
Score 0.0 when it isn't actionable at all — description with no implied behavior change.

## Facet: non_redundant — distinct from the platform's other standing behavioral rules
Score 1.0 when the rule covers ground not already fully covered by the seed
`_behavioral_rules` set (sell-discipline, LEAP overlay, catalyst test, instrument
selection, framework-over-feeling) or by another currently-affirmed behavioral fact — a
genuinely new, evidence-backed pattern, or a materially sharper restatement of an existing
one (tighter evidence, better falsifier).
Score 0.5 when it substantially overlaps an existing rule without adding anything.
Score 0.0 when it is a verbatim or near-verbatim duplicate of a seed rule or another
staged fact, contributing zero new information.
