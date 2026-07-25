# Rubric: senior_partner_brief (v1)

Pass threshold: 0.70

Scope: one Senior Partner Brief artifact (`llm_artifacts` row,
purpose='senior_partner_brief', scope='portfolio') produced by
`advisor.senior_partner_brief.compose_brief`. The graded text is the
artifact's `content_md` — the five ordered §9.1 sections rendered as markdown
(personal_investment_partner_prd.md §2.3/§9.1/§10.5).

The hard structural invariants (at most 3 action_requested items in a normal
week, an active week's excess requires a populated `active_week_explanation`,
closed disposition vocabulary, no invented source_refs) are already enforced
in code (`advisor.senior_partner_brief.SeniorPartnerBrief.validate_notification_policy`
plus the deterministic post-pass in `compose_brief`) BEFORE an artifact is
ever persisted — grade the QUALITY of the judgment and prose here, not those
hard invariants.

Grading convention per facet: 1.0 = met across the artifact; 0.5 = partially
met; 0.0 = broadly missed. Judge only the artifact text — the underlying
deterministic inputs (recommendation, risk snapshot, cards, decision journal)
are not available to you, so grade citation DISCIPLINE (are claims anchored
to specific figures/facts already present in the text) rather than
re-deriving the numbers.

## Facet: prioritization — the attention budget is respected

Section 2 (highest-priority portfolio decision) and section 3 (best use of
incremental capital) name the SINGLE most important item in each lane rather
than a diffuse list. Action-requesting items read as genuinely the most
consequential open items on the book, not whatever happened to be freshest.

## Facet: materiality — every item earns its place

Each section-1 "what changed" item states why the change matters to a
decision, not just that something moved. Context-only and
no-action-warranted items are labeled as such rather than dressed up as
action items to look more useful.

## Facet: integration — portfolio, research, and history in one voice

The brief visibly draws on more than one input class (e.g. the recommendation
AND the risk posture AND decision history), synthesizing across them rather
than restating one source's own summary verbatim. Section 5 (a prior Owner
Decision worth revisiting) connects a PAST decision's stated advice/falsifier
to what has happened since, not just a generic "revisit this" prompt.

## Facet: actionable_but_humble — advice, never an executed decision

Every action_requested item states a concrete next step; every item states
its uncertainty or disconfirming case honestly rather than projecting false
confidence. No numeric probability dressed up as prose (PRD §2.3 house
style: qualitative confidence language, "This is my read because...").
Section 4 (an assumption or behavioral pattern worth challenging) reads as a
genuine challenge grounded in evidence, not a vague nag.

## Facet: no_duplication — five distinct sections, not one idea repeated

The five sections are substantively distinct — no section merely restates
another section's content in different words. An item appearing in "what
changed" that also drives the "highest-priority decision" is explicitly
linked rather than silently duplicated as two separate items.

## Facet: action_budget_respected — the brief reads calm under a normal week

Even before the deterministic <=3 cap is checked in code, the PROSE itself
should read as an owner-scaled advisory brief (PRD §1's ~5hr/week attention
budget), not an institutional-process checklist demanding many actions. An
active week's `active_week_explanation` (when populated) genuinely explains
why MORE CONTEXT is visible this week — never that more PINGS were sent (PRD
§9.1: "an active week increases visible context, not ping frequency").

## Facet: notification_policy_respected — the brief, not a nag

The overall tone matches "the brief owns delivery; the tenet-2 machinery owns
detection" (PRD §3.3/§9.1) — items feel like a synthesized weekly briefing,
never a rehash of every individual alert/ping that fed it. Suppressed/
digest-lane items that resurface here read as deliberately surfaced, not as
noise leaking through.
