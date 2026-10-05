# Rubric: transcript_summary (v2)

Pass threshold: 0.70

Scope: one per-quarter earnings note (`.tmp/<TICKER>_Q<q>_<YYYY>_summary.txt`,
including the `_investor_update_summary.txt` variant) produced by
`llm_client.generate_summary`. The facets are distilled from that prompt's own
bar — "a senior buy-side analyst's quarterly note, not a Yahoo Finance recap"
(Stock-Market-Nerd-style per-quarter debriefs) — plus its EXPLICITLY FORBIDDEN
list, so the judge enforces the bar the prompt already demands.

Grading convention per facet: 1.0 = met across the note; 0.5 = partially met
(some sections clear the bar, others read templated/recappy); 0.0 = broadly
missed. Judge only the note itself — the workspace renders the financial
tables and Q&A roster separately, so do NOT reward restating them.

## Facet: analytical_takeaway — opens with a verdict, not a recap

The note must open with a 1-paragraph analytical takeaway that leads with
the verdict — the single most important thing this quarter as it bears on
the thesis — then the reasoning (~3–5 sentences). When the note shows
thesis/bear-anchor framing (named tier-1 KPIs or named failure modes), the
takeaway must engage it: name a KPI and how the print moves its distance to
break, or state whether the print confirms/refutes a named failure mode. An
opening that merely recaps headline results scores 0.

## Facet: no_template_boilerplate — flowing prose, none of the forbidden patterns

The forbidden list is hard: numbered template headers ("## 1. Executive
Summary" / "## 2. Financial Highlights"), re-listed headline financials
(Revenue / EPS / Op Margin dumps), generic "Key Drivers:" paraphrasing, a
"Strategic Initiatives:" bucket, product-launch laundry lists, or a
topic-by-topic Q&A restatement. The note should be flowing prose with at
most 3–4 self-chosen H3 subheads. Each forbidden pattern present drags this
facet toward 0.

## Facet: specificity — quantified deltas and named levers

Claims must identify the economic mechanism, supplied evidence and plausible alternative cause. Quantify comparable deltas when the inputs permit. Cite qualitative disclosures when numbers are absent. Distinguish secular, cyclical and one-off causes. Unsupported precision is a miss; explicit missing evidence is valid.

## Facet: forward_setup — a concrete next-quarter check

The note must end (or include) a concrete next-quarter setup: what to
expect in the next print given THIS quarter's signals, framed as a
1-quarter-out check — with specific watchable metrics and what reading
would confirm or break the picture. A generic "we'll watch margins" line
scores 0.5 at best; no forward section at all scores 0.

## Facet: grounded_numbers — every figure traceable, quotes verbatim

Every quantitative claim must be grounded in the transcript or derivable
from numbers in it — no invented figures (internal inconsistency or
impossible magnitudes are hard misses). Verbatim quotes, if present, appear
in blockquote format with speaker attribution and earn their place (signal
that paraphrase would lose); inline paraphrase must not masquerade as
quotation.

## Facet: exchange_and_context — bounded conclusions from available evidence

Preserve guidance, consensus and owner expectations as distinct dated bars.
Identify absent prior bars rather than reconstructing them. Classify an unanswered
component only from the complete relevant exchange, including later responses.
Unknown or selected transcript coverage must constrain avoidance/dropped-topic
claims. Compare language with cited matching speaker, topic and context. Separate
textual observation, management claim and analyst inference; do not infer motive.
Public next checks must name a source, observable and investment consequence.
