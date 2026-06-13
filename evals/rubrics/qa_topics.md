# Rubric: qa_topics (v1)

Pass threshold: 0.70

Scope: one entry from `data/qa_topics/<TICKER>.json` — the JSON array of
Q&A topic labels produced by `llm_client.generate_qa_topics` for a single
earnings quarter. Each item has `id`, `topic` (4-7 word noun phrase), and
`tag` (one of: INFRA, CLOUD, SEARCH, MARGIN, CAPEX, AGENT, LEGAL,
OTHER BETS, CONSUMER, Q&A). The facets below score the label set as a whole.

Grading convention per facet: 1.0 = the bar is met across the label set;
0.5 = met for most items but materially missed for several; 0.0 = the bar
is broadly missed. Judge only the artifact — no outside knowledge of the
company or transcript beyond what the content implies.

## Facet: substance_capture — topics name real analytical substance

Each `topic` must name a concrete question or concern that analysts raised —
specific enough to surface what was contested or uncertain in the call. Vague
labels ("Business update", "Guidance discussion", "General Q&A") or labels
that restate a segment name without adding analytical content are misses.
Score 1.0 when every topic names real substance, 0.5 when several are vague
catch-alls, 0.0 when the list is dominated by uninformative labels.

## Facet: tag_accuracy — tags correctly classify each topic's domain

Each `tag` must correctly map the topic to its domain. INFRA = data center /
hardware supply; CLOUD = hyperscaler revenue; SEARCH = core search ad product;
MARGIN = cost structure / profitability; CAPEX = capital expenditure plans;
AGENT = AI agents / AI products; LEGAL = regulatory / antitrust / compliance;
OTHER BETS = non-core bets; CONSUMER = consumer-facing product lines; Q&A =
use only when no other tag applies. Misclassification (e.g., a capex question
tagged MARGIN) is a miss per item. Score 1.0 when all tags are correct, 0.5
when a few are debatable, 0.0 when systematic misclassification is present.

## Facet: phrase_quality — topic phrases are concise, well-formed noun phrases

Each `topic` must be a 4-7 word noun phrase — specific, grammatically sound,
no verbs as the main predicate, no sentence fragments. Phrases that are too
short ("margin pressure"), too long (>10 words), or sentence-like ("Will
margins expand next year?") are misses. Score 1.0 when all phrases meet the
bar, 0.5 when several fall outside the 4-7 word target or are grammatically
awkward, 0.0 when most phrases are malformed.

## Facet: coverage_breadth — the label set covers the full Q&A session

The label set as a whole should cover the major analytical threads of the Q&A
session — not all from the same tag cluster. A set that has 8 MARGIN tags and
nothing else, or covers only 2 of 10 question blocks, fails this facet. Score
1.0 when the set spans 3+ distinct tags and covers the material threads, 0.5
when coverage is somewhat narrow (1-2 tags dominate) but not egregiously so,
0.0 when the set is obviously incomplete or all from one domain.
