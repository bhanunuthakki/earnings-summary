# Rubric: incremental_dollar_recommendation (v1)

Pass threshold: 0.75

Scope: one governed Incremental Dollar Recommendation artifact
(`llm_artifacts` row, purpose='incremental_dollar_recommendation',
scope='portfolio') produced by
`allocation.recommendation_artifact.generate_recommendation`. The graded text
is the artifact's `content_md` — the preferred plan plus its humility fields
(central_hypothesis, personalization_why, main_unknowns,
disconfirming_evidence, confidence_basis), rendered as markdown
(personal_investment_partner_prd.md §2.3/§7.4/§10.5).

This is a high-stakes allocation-judgment purpose (it frames a real
new-cash decision), hence the pass threshold above the repo default. The
deterministic frontier-grounding checks (allocations sum to cash, tickers
are frontier-eligible, zones present at >=12%, no naked numeric
probabilities) are already enforced in code
(`allocation.recommendation_schema.IncrementalDollarRecommendation.
validate_against_frontier`) BEFORE an artifact is ever persisted — grade the
QUALITY of the reasoning and prose here, not those hard invariants.

Grading convention per facet: 1.0 = met across the artifact; 0.5 = partially
met; 0.0 = broadly missed. Judge only the artifact text — the underlying
frontier/book data is not available to you, so grade citation DISCIPLINE
(are claims anchored to specific figures/facts already present in the text)
rather than re-deriving the numbers.

## Facet: usefulness — a decision the owner can actually act on

The preferred plan states a clear status (deploy_all / deploy_partial /
retain_all / defer) and concrete next step, in plain language, without
hedging so heavily the owner can't tell what is actually being recommended.
Vague "it depends" framing with no operative content is a miss.

## Facet: personalization — grounded in THIS owner, THIS book, THIS cash

`personalization_why` must connect the recommendation to specifics of the
book or owner context already in the artifact (a stated concentration zone,
an owner-context fact, a specific dollar amount) — not a generic essay that
could apply to any investor with any amount of cash.

## Facet: probabilistic_humility — genuine uncertainty, no invented odds

`main_unknowns`, `disconfirming_evidence`, and `confidence_basis` must read
as genuinely considered, specific risks to the stated plan — not boilerplate
("markets could go down") and not a numeric probability dressed up as
prose. `confidence_verbal` must be consistent with the tone of
`confidence_basis` (e.g. "high" confidence paired with a confidence_basis
that reads deeply uncertain is a miss).

## Facet: evidence_grounding — claims trace to a cited fact or source

Every material claim in `central_hypothesis` and `supporting_evidence`
should be traceable to a specific fact, figure, or freshness key already
present in the artifact text — not an unanchored vibe ("this looks like a
good opportunity").

## Facet: alternative_quality — the alternative/diversifier plans earn their place

When `best_alternative` or `best_diversifier` are populated, the text should
explain WHY each is a genuine alternative (different risk/return trade-off,
different diversification benefit) rather than a token second option with no
distinguishing rationale.

## Facet: disconfirming_case — a real case against the preferred plan

`disconfirming_evidence` must present a substantive case against the
preferred plan (what would make this wrong), not a token caveat. A
recommendation presented as all-upside is a miss for this facet.

## Facet: frontier_consistency — the prose matches the numbers it presents

The allocation dollar amounts, resulting weights, and tickers named in the
prose must be internally consistent with each other (e.g. the plan
narrative doesn't say "put $2,000 into NU" while the allocation table shows
a different name or amount). Judge only cross-text consistency within the
artifact — the hard frontier-grounding check already ran in code.

## Facet: no_overreach — advice voice, never an executed-trade or institutional-process voice

The artifact must read as ADVICE for a solo owner to act on (or not), never
as if a trade has already executed, a committee approved it, or the system
is instructing rather than informing. PRD §2.3 house style
("This is my preferred plan.", "The main reason I could be wrong is...")
should be recognizable in tone even if not verbatim.
