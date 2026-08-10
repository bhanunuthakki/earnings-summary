# Rubric: ask_advisory_answer (v1)

Pass threshold: 0.70

Scope: one real production Ask turn — an assistant answer from the `ask_turns`
table (the conversational advisory path, `src/ask/engine.py` →
`ask.narrative_transport.stream_llm_text`), replayed through the eval judge. Today only
citation/grounding HYGIENE is judged anywhere; nothing scores whether the
multi-turn advice is actually GOOD (sound reasoning, balanced risk/reward,
calibrated synthesis). This rubric closes that gap — it is a hard prerequisite
for trusting any LLM-composed coaching/standup bet that reads the owner's book.

The content under audit has three parts, clearly delimited: the CONVERSATION SO
FAR (prior turns, for context only), the ANSWER UNDER AUDIT (the assistant turn
being graded), and the EVIDENCE THE ANSWER CITED (the numbered sources the turn
attached, with their scored confidence). **Grade only the ANSWER UNDER AUDIT.**
The prior turns and the user's question set the bar the answer must clear; the
cited evidence is the only ground truth you have — the underlying book, filings,
and fact caches are NOT available to you, so judge whether claims are anchored
to the cited evidence rather than re-deriving the numbers yourself.

Grading convention per facet: 1.0 = the bar is met across the answer; 0.5 =
partially met (met for most of the answer but materially missed in places);
0.0 = broadly missed. A terse, correct answer is not penalized for brevity, and
length is never rewarded. An answer that correctly declines to over-reach (says
plainly what it cannot determine from the evidence) earns credit, not a miss.

## Facet: grounding_correctness — every specific claim is anchored to the cited evidence

Each specific, checkable claim in the answer (a figure, a dated event, a
quoted disclosure, a named driver) must be traceable to the EVIDENCE THE ANSWER
CITED — ideally via an inline `[n]` marker matching a numbered source. Figures
must match the evidence; numbers, dates, or events that appear nowhere in the
cited evidence are fabrications and a hard miss. An answer that makes a
specific quantitative claim with no anchoring source is a miss for that claim.
Score 1.0 when every specific claim is anchored and consistent with the
evidence, 0.5 when most are but a few float unanchored, 0.0 when the answer
asserts specific figures the cited evidence does not support. When the answer
makes no specific claims (a purely qualitative, correctly-hedged reply), grade
on whether what it does assert is consistent with the cited evidence.

## Facet: risk_reward_balance — the answer engages both sides, not just the bull case

When the answer takes a view or makes a recommendation-shaped statement, it
must engage the counter-case: the case for AND the case against, what would make
the view wrong, and (where relevant) the asymmetry between upside and downside.
An answer that cheerleads — all-upside, no named risk, no disconfirming
evidence — is a miss. A genuinely two-sided question answered one-sidedly is a
miss. Score 1.0 when the answer fairly weighs both sides and names the specific
disconfirming observable, 0.5 when one side is acknowledged only as a token
caveat, 0.0 when the answer is one-directional advocacy. A factual lookup that
warrants no risk framing (e.g. "what was Q3 revenue?") is full credit when it
answers cleanly — do not manufacture a counter-case the question didn't ask for.

## Facet: calibration_vs_evidence — confidence matches the strength of the evidence

The answer's stated conviction must track the evidence it actually has. Thin,
stale, or conflicting evidence must be flagged as such ("this rests on one
print", "the latest disclosure is N quarters old", "SEC and the filing
disagree") rather than papered over with confident prose. Low-confidence cited
sources (see the confidence on each numbered source) should be discounted, not
treated as settled fact. Over-claiming beyond what the evidence supports is the
core miss; honestly naming a data gap is full credit, not a hedge. Score 1.0
when conviction is proportioned to evidence strength and gaps are named, 0.5
when the answer is somewhat over-confident relative to its sources, 0.0 when it
asserts a firm conclusion the cited evidence cannot bear.

## Facet: followup_usefulness — the answer advances the analyst's decision

The answer must be responsive to the actual question, in a terse PM-to-PM
voice, and leave the analyst better positioned to act: it either answers
cleanly, or names the specific next thing to verify / watch / pull. A generic
recap, a dead-end "it depends" with no path forward, or an answer that ignores
what was asked are misses. When the conversation carried an open thread (a prior
question the answer should build on), the answer should advance it rather than
restart. Score 1.0 when the answer is directly responsive and ends with an
actionable read or a named next step, 0.5 when it answers but trails off
without a path forward, 0.0 when it is non-responsive or pure filler.
