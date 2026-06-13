# Rubric: peer_selection (v1)

Pass threshold: 0.70

Scope: one ``peer_selection`` artifact — the JSON list of ``{ticker, name, why}``
objects produced by ``compute.peer_selection.suggest_peers`` and cached to
``data/peer_selection/{TICKER}.json``. The facets below are distilled from the
generating prompt's own bar: *business-model comparability* over sector/cap
proximity (directives/peer_selection_llm.md).

Grading convention per facet: 1.0 = the bar is met across all returned peers;
0.5 = met for most but materially missed for one or two; 0.0 = the bar is
broadly missed. Judge only what is in the artifact — no penalty for analysis
the inputs couldn't support.

## Facet: business_model_match — peers share the subject's business model, not just its sector

Each returned peer must be comparable on unit economics, customer type,
revenue model, or competitive dynamics — NOT picked merely because it is in
the same GICS sector or has a similar market cap. A fintech whose primary
revenue is consumer credit should return consumer digital lenders, not
diversified banking conglomerates. An enterprise SaaS company should return
SaaS peers with similar land-and-expand mechanics, not application-software
index constituents. Score 0 if the list is dominated by sector/cap proxies.

## Facet: specificity — the ``why`` field is one concrete sentence

Every ``why`` must name a specific shared mechanic, market, or dynamic — not
a generic label like "same sector" or "comparable size". A ``why`` that could
be copy-pasted unchanged onto any competitor in a broad industry scores 0.
A ``why`` that names a shared product type, customer segment, regulatory
framework, or growth vector scores 1.

## Facet: cross_boundary — at least one peer crosses the obvious sector or geography boundary

The owner's flagged failure mode is FMP returning homogeneous sector/cap peers.
At least one returned peer should cross the obvious boundary: either a
different GICS sector that is genuinely comparable, or a foreign-listed name
that shares the real business model. Score 0 if every returned peer is in
exactly the same sector and geography as the subject.

## Facet: coverage — 4-10 peers returned

Too few peers (< 4) means the model hedged or filtered aggressively; too many
(> 10) violates the prompt's instruction. Score 0 outside this band;
1.0 within it.
