# Rubric: earnings_themes_split (v1)

Pass threshold: 0.70

Scope: one `data/earnings_themes/<TICKER>.json` artifact — the structured
cross-quarter theme rollup (`prepared_themes[]`, `qa_themes[]`) produced by
`llm_client.extract_qa_vs_prepared_themes`. Each list contains theme objects
with `theme_name`, `mentions_per_quarter`, and `evidence[]`. The facets below
score the quality of the split and the content of each side.

Grading convention per facet: 1.0 = the bar is met throughout the artifact;
0.5 = met on one side but materially missed on the other, or met inconsistently
across themes; 0.0 = the bar is broadly missed. Judge only the artifact —
no outside knowledge of the company, no penalty for thin inputs.

## Facet: prepared_qa_separation — themes sit on the right side of the split

Themes in `prepared_themes` must reflect management's chosen narrative:
guidance reaffirmations, strategic priorities, product milestones, announced
initiatives. Themes in `qa_themes` must reflect what ANALYSTS drove: follow-up
pressure, risks management avoided volunteering, valuation probes. A theme that
belongs to one side but appears on the other scores 0 for this facet. Score 1.0
when both lists are cleanly separated, 0.5 when one side has 1-2 misclassified
themes, 0.0 when the split is essentially random.

## Facet: theme_distinctiveness — themes are distinct, non-overlapping concepts

Each theme must name a specific, bounded topic — not a catch-all ("financial
performance", "guidance") that could absorb half the transcript. Themes within
the same list must not overlap (two themes that cover the same subject are a
dedup failure). Score 1.0 when every theme on both sides is specific and
non-overlapping, 0.5 when 1-2 themes are vague or overlap, 0.0 when the lists
are dominated by catch-alls or duplicates.

## Facet: cross_quarter_grounding — mentions_per_quarter reflects real coverage

`mentions_per_quarter` should show realistic non-zero counts only for periods
actually covered by the input transcripts; a theme that appears in only one
quarter should not have entries spread uniformly across all quarters. Score 1.0
when the per-quarter distribution is plausible given a multi-quarter earnings
window, 0.5 when a few entries seem inflated or misattributed, 0.0 when the
distribution is uniform across all quarters regardless of content.

## Facet: evidence_specificity — evidence citations are grounded and specific

Each `evidence[]` entry must include a speaker name, a period label, and a
concrete excerpt — a paraphrase so vague it could apply to any quarter ("we
are focused on growth") is a miss. Score 1.0 when all evidence entries are
specific and attributable, 0.5 when some entries are vague paraphrases, 0.0
when evidence is absent or entirely generic.

## Facet: theme_name_clarity — theme names are self-explanatory noun phrases

Each `theme_name` must be a concise noun phrase (4-8 words) that names the
concept precisely enough that a reader unfamiliar with the transcript can infer
the topic. Verb-heavy or vague labels ("Discussed margins", "Growth topics")
are misses. Score 1.0 when all names meet the bar, 0.5 when several are
borderline, 0.0 when most names are unclear.
