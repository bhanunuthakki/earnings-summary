# Rubric: advisor_next_dollar (v1)

Pass threshold: 0.75

Scope: one next-dollar allocation memo (an `advisor_memos` row with
kind='next_dollar') produced by `advisor.memos.generate_next_dollar_memo`.
The facets are distilled from `_NEXT_DOLLAR_PROMPT`'s own rules — most
importantly the advisor posture locked into the directive: the memo NEVER
instructs; evidence and framing only; the owner decides. This is the
highest-stakes prose purpose in the repo (it frames real allocation
decisions), hence the higher pass threshold.

Grading convention per facet: 1.0 = met across the memo; 0.5 = partially met;
0.0 = broadly missed. Judge only the memo text — the underlying book/candidate
data is not available to you, so grade citation DISCIPLINE (are claims
anchored to specific figures from the context blocks) rather than re-deriving
the numbers.

## Facet: no_directives — evidence and framing, never instructions

The memo must never instruct: no position sizes, no orders, no "you
should" / "buy X" / "trim Y" phrasing. The contract voice is "the case for
X is…, the case against is…, what to verify first is…". ANY directive
sentence is a hard miss — score this facet 0 if even one appears; 1.0 only
when the entire memo holds the evidence-and-framing posture.

## Facet: structure — the three contracted sections, in contract shape

The memo must contain exactly the three sections the prompt demands: "Where
the next dollar works hardest" (2–3 candidate destinations), "What the book
is telling you" (1–2 structural observations), and "Engaging your open
notes". Missing, renamed-beyond-recognition, or padded-out extra sections
are misses; minor heading-level drift is not.

## Facet: anchored_claims — every claim cites a number from the context

Voice is terse, PM-to-PM, and EVERY claim is anchored to a specific figure
from the memo's own context (valuation gap, sizing tension, alpha,
concentration percentage…). Unanchored vibes ("looks attractive here",
"valuation is reasonable") are misses. Genuine uncertainty must be flagged
specifically, not hedged with boilerplate.

## Facet: case_against — every candidate carries a real counter-case

Each candidate destination must include the case FOR, a substantive case
AGAINST (what would make this wrong — not a token caveat), and the one
thing to verify before acting. A candidate presented as all-upside is a
miss for this facet.

## Facet: notes_engagement — open notes advanced or explicitly declined

If the memo's context carried open notes / watch-items, the memo must
either advance at least one explicitly (naming the note) with the data at
hand, or state in one line that none can be advanced. Silence on the open
notes is a miss; a one-line "none can be advanced" is full credit when no
note is engageable.
