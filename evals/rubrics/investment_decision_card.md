# Rubric: investment_decision_card (v1)

Pass threshold: 0.70

Scope: one Investment Decision Card artifact (`llm_artifacts` row,
purpose='investment_decision_card', scope='ticker') produced by
`research.investment_decision_card.generate_card`. The graded text is the
artifact's `content_md` — the seven §8.1 sections rendered as markdown
(personal_investment_partner_prd.md §2.3/§8.1/§10.5).

The hard structural invariants (required sections present, company/security/
portfolio distinctness, no `decision_ready=true` when a blocking input is
missing, source IDs resolve, no disposition mutation) are already enforced
in code (`research.investment_decision_card.InvestmentDecisionCard.
validate_grounding` plus the deterministic evidence_readiness overwrite in
`generate_card`) BEFORE an artifact is ever persisted — grade the QUALITY of
the judgment and prose here, not those hard invariants.

Grading convention per facet: 1.0 = met across the artifact; 0.5 = partially
met; 0.0 = broadly missed. Judge only the artifact text — the underlying
deterministic inputs (thesis, DCF, bear case, candidate fit) are not
available to you, so grade citation DISCIPLINE (are claims anchored to
specific figures/facts already present in the text) rather than re-deriving
the numbers.

## Facet: hypothesis_clarity — the company hypothesis is a real, falsifiable claim

The directional thesis and operating mechanism state a specific, checkable
claim about how the business creates value and what would confirm or refute
it — not a generic "good company, good management" essay that could apply to
any security. Key KPIs, confirming evidence, and disconfirming evidence
should be concrete and specific to this name.

## Facet: priced_in_reasoning — a genuine read of what the market already assumes

`appears_priced_in` must reason about what the CURRENT price/valuation
implies the market already believes, grounded in the valuation range stated
in the artifact — not a bare "it looks cheap" or "it looks expensive" with no
connection to the stated valuation figures.

## Facet: opposing_case — the disconfirming case is substantive, not a token caveat

`bear_hypothesis` must present a real, well-reasoned case against the
thesis — specific mechanisms by which the company hypothesis could be wrong,
not generic risk boilerplate ("competition could increase", "the market could
decline"). `evidence_that_would_confirm_it` and `next_proof_point` should be
concrete and checkable, not vague.

## Facet: decision_usefulness — the owner can act on this without more work

Taken together, the seven sections should let the owner form a view on
whether to pass/watch/research further/promote WITHOUT needing to re-derive
the analysis themselves. `expected_role` and `candidate_fit_summary` connect
the security to a specific role in the book (not a generic "would add
diversification" with no specifics), and the suggested disposition follows
sensibly from the rest of the card rather than reading as an afterthought.

## Facet: uncertainty_honesty — genuine, specific uncertainty, no invented confidence

`justification` and `what_would_change_it` must read as genuinely considered
uncertainty specific to this name's situation — not boilerplate ("markets are
uncertain") and not a numeric probability dressed up as prose.
`confidence_verbal` must be consistent with the tone of `justification` (e.g.
"high" confidence paired with a justification that reads deeply uncertain is
a miss). The card should read as ADVICE for a solo owner to weigh, never as
an executed decision or institutional-process voice (PRD §2.3 house style).
