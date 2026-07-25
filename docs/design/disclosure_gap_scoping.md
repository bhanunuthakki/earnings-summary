# Disclosure Gap Scoping — the four literature-supported signals we don't track yet

Status: scoping only. **No code, no migration, no detector in this doc or this PR.**
Written 2026-07-25, against `docs/design/disclosure_program_ruling.md`'s GAPS list
(same date, PR #1028) — read that doc's "standard of evidence" section first, because
it governs every claim below: **published, peer-reviewed research is the evidence
that a signal is worth tracking. In-sample backtesting on this 44-ticker book is
explicitly NOT the gate**, and nothing in this doc proposes one. Where a "falsify
this early" check appears below, it is a plausibility/correctness check on our own
detector code (did the boundary-quality bug reappear, does the residual explain any
variance at all), never a return-prediction test on the owned basket.

The organizing question this doc answers, per gap: **how far does the existing
`src/filings` / `src/transcripts` construct already carry this, and what is the
genuinely new delta?** Two real generalizations fall out of asking that question
honestly (Gap 1 and, more loosely, Gap 4); the other two gaps share substrate without
unifying into one detector. See "The unification picture" near the end.

## The four gaps, in the ruling's priority order

1. Guidance withdrawal / expected-but-missing disclosure
2. Abnormal tone (ABTONE), residualized on fundamentals
3. CFO-vs-CEO language separation
4. Detrended document-level YoY similarity (the Lazy Prices construct itself)

---

## Gap 1 — Guidance withdrawal / expected-but-missing disclosure

**Evidence.** Zhou & Zhou, *JAR* 58(3), 2020, "The Dog that Did Not Bark": firms that
fail to issue *expected* guidance see **-41bps around the next quarterly earnings
announcement**, modeled as a per-firm expected-disclosure baseline with deviation as
the signal. Chen, Matsumoto & Rajgopal, *JAE* 51(1-2), 2011, "Is Silence Golden?": 96
firms that stopped quarterly EPS guidance saw **-4.8% three-day CAR**; stoppers had
poor trailing performance *before* stopping, and analyst dispersion rose / accuracy
fell afterward. Counterweight that must travel with both: Call, Melessa & Volant
(2024 WP) — using COVID as a natural experiment, **~40% of firms that suspended
guidance never restarted**, and those permanent stoppers subsequently
*outperformed* — they were "stuck" guiding only because of the stopping penalty, not
because guiding was informative. **Do not encode "stopped = bearish."**

### How far the existing construct already carries it

This is the best-evidenced gap and also the one where the platform already has,
almost verbatim, the right shaped engine — just applied to a different subject.

`src/filings/metric_lifecycle.py` is not really "the discontinued-XBRL-metric
detector." Read structurally, it is a general **"expected periodic disclosure,
judged against the issuer's own cadence" engine**, where the subject happens to be an
`(taxonomy, tag)` pair today:

- **Stage 0** (`_stage0_candidates` / `compute_tag_lifecycle`) computes, per subject,
  its own `historical_max_gap` (the largest silence it has ever tolerated between two
  of its own appearances) and flags only when `current_silence` exceeds that
  precedent — this **is** Zhou & Zhou's "expected disclosure baseline," built from the
  company's own filing history, not a universal rule or wall-clock "today."
- **Stage 1** (`find_relabel_target`) suppresses candidates whose disclosure
  continues under a differently-named vehicle with comparable magnitude and label
  overlap — the guidance analog is a company that swaps a quarterly EPS point
  estimate for a full-year revenue range: same underlying practice, different
  packaging, zero information content as a "withdrawal."
- **Stage "1.5"** (`build_standard_transition_corpus` /
  `STANDARD_TRANSITION_MIN_OTHER_TICKERS`) reclassifies a candidate as a mechanical,
  industry-wide transition when enough *other* cached tickers stopped the same tag in
  the same window — this is exactly the shape of the COVID guidance-suspension
  confound Call/Melessa/Volant describe: a shared macro event, not a company decision.
- **Stage 2/3** (`src/filings/metric_triage.py`'s `Relevance` / `LifecyclePrior`
  enums) already asks the subject-agnostic question this gap needs: is this
  business-meaningful, and does stopping look like *concealment* or
  *maturity/reframing* or *unclear* — explicitly never defaulting to "bearish."

**Say it plainly, as the task asks: guidance withdrawal and P1's discontinued-metric
detector are one detector with two subject types, not two detectors.** The
recommendation is to genericize Stage 0/1/1.5 to operate over an abstract per-ticker
subject series (`ticker`, `subject_namespace`, `subject_id`, per-period
presence/value) instead of a `TagSeries`-shaped input, and feed it a second
population (guidance candidates) alongside the first (XBRL tags) it already serves.
That is a refactor-and-extend of existing code, not a new module.

**What genuinely does not exist yet:** a candidate-generation stage that defines
"guidance was disclosed this period" as a fact per (ticker, subject, period). Two
candidate sources, neither wired today:

- `src/compute/say_do.py` + `management_commitments` (migration 0017) +
  `execution/extract_commitments_from_transcript.py` already extract forward-looking
  numeric commitments per transcript segment (`kpi_name`, `comparator`,
  `target_value`, `period_target`) — structurally adjacent to "guidance," but **not
  the same construct**: a commitment can be any ad hoc KPI target management floats
  once, not necessarily a recurring, formal guidance practice. Using this as the
  candidate feed requires first checking that a ticker's own `historical_max_gap`
  over `management_commitments` actually stabilizes (i.e., there IS an established
  cadence to violate) before treating any gap in it as a withdrawal signal.
- A second, complementary and cheaper lane: explicit-language detection. If a
  company states outright — in the MD&A or press release — "we are suspending/
  withdrawing our previously issued guidance," that is just an `item_removed` /
  `item_reworded` event on a "guidance"/"outlook" sub-heading, which **P0's existing
  item-diff engine already detects for free** if MD&A splitting captures that
  sub-heading (unverified — needs a quick corpus check, not new code).

### Cheapest deterministic core / LLM surface

Zero LLM for candidate generation and the three suppression stages — pure reuse of
`metric_lifecycle`'s math generalized to a non-XBRL series. LLM surface is the
**same two Stage 2/3 questions `metric_triage` already asks** (`Relevance`,
`LifecyclePrior`), batched per ticker, names/values only, never documents — Haiku-class,
matching the existing precedent exactly.

### Known measurement traps

- **Don't encode "stopped = bearish."** Chen/Matsumoto/Rajgopal's -4.8% CAR and
  Call/Melessa/Volant's 40%-never-restarted-and-outperformed finding are both real;
  `verdict` must stay `unclassified` until Stage 2/3 judges it, exactly as
  `metric_lifecycle` already does for XBRL tags.
- **Relabeling, not withdrawal**, when guidance changes form (EPS point → revenue
  range) — needs the same magnitude/label-overlap gate as Stage 1, adapted to
  guidance vocabulary.
- **Macro-wide guidance suspensions (COVID) are a structural break**, not a
  per-company signal — the Stage 1.5 cross-sectional gate must run before shipping
  anything, or the confound Call/Melessa/Volant describes reappears wholesale.
- **Pipeline-coverage gaps are not company behavior.** `management_commitments`
  extraction is LLM-driven and, per its own docstring, session-triggered rather than
  a guaranteed-every-quarter batch job. An absence of extracted commitments can mean
  "extraction was never attempted this quarter," not "no guidance was given." This
  needs its own coverage/absence semantics (mirroring `filing_section_coverage`)
  before any Stage 0 gap-calibration is trustworthy — this is the single biggest
  correctness risk for this gap, bigger than anything in the literature caveats.

### Size and falsification

**Size: M.** Depends on (a) an audit of whether `management_commitments` extraction
actually runs per-ticker-per-quarter with usable coverage — discovery work, not
build; (b) whether MD&A splitting already captures a guidance/outlook sub-heading —
a quick corpus check, not new code; (c) genericizing `metric_lifecycle`'s Stage 0/1/1.5
to a subject-series abstraction — a refactor, not a rewrite.

**What would make this WRONG, so a future implementer can catch it early:**
- Detected "stops" cluster around known extraction-script downtime or backfill
  boundaries rather than real earnings calls — that's a pipeline artifact, not
  guidance behavior.
- A ticker's own `historical_max_gap` over guidance candidates never stabilizes
  (every quarter's "guidance" looks structurally different) — there is no real
  cadence to violate, and every "stop" is noise from an ad hoc-commitment feed that
  was never a formal guidance practice to begin with.
- Flagged withdrawals cluster overwhelmingly in one macro window (2020Q1-2021) and
  the cross-sectional suppression gate was skipped — the confound this gate exists
  to catch went unshipped.

---

## Gap 2 — Abnormal tone (ABTONE), residualized on fundamentals

**Evidence.** Huang, Teoh & Zhang, *TAR* 89(3), 2014: **abnormal** tone — the
residual after controlling for fundamentals — predicts negative future earnings and
a delayed negative reaction over the following 1-2 quarters. **Raw tone mostly
re-derives the earnings surprise; the residual is the signal**, per the ruling.

### How far the existing construct already carries it

This gap has the richest existing substrate of the four, and it is important to be
precise about exactly what exists versus what is still missing, because it is easy to
mistake the former for the latter.

Already shipped, in `src/transcripts/`:

- `transcript_judgment.judge_call` produces a per-speaker tone **score** (-1.0..+1.0)
  via one batched, cached LLM call per transcript, tracked **by name**, never pooled.
- `transcripts.longitudinal.tone_shift_is_abnormal(tone_delta, eps_surprise_pct)`
  already joins `nearest_earnings_surprise` (the `earnings_surprises` table) and
  flags a tone delta as material.
- `execution/detect_transcript_disclosure_events.py` already writes
  `transcript_tone_shift_abnormal` rows into `disclosure_events`.

But `tone_shift_is_abnormal`'s own docstring is explicit and correct: it is **"A
TRANSPARENT PROXY for Huang/Teoh/Zhang's ABTONE residual — NOT a fitted OLS
regression."** It flags a delta as abnormal via two simple, transparent rules
(opposite-signed vs. the earnings surprise, or a large move despite a small
surprise) — not an actual residual from regressing tone on fundamentals. There is
also a second, wholly separate raw-tone mechanism already live,
`src/triggers/earnings_tone.py`, which diffs a new transcript against the prior 4
quarters via a single LLM call with no fundamentals control at all — precisely the
raw-tone-re-derives-the-surprise trap the literature warns against, already present
in the codebase as a different, pre-existing signal.

So: the gap is real, but it is narrower than "build tone tracking from scratch." The
expensive part — an LLM producing a per-speaker tone score, cached — is already built
and paid for. What's missing is the actual **residualization**: fitting
`tone_score ~ eps_surprise_pct + revenue_yoy (+ margin delta)` and using the residual,
not the two-rule heuristic, as the abnormal-tone signal.

**What genuinely does not exist yet:** a deterministic regression/residual step over
the already-cached tone scores, joined to a fundamentals feed (revenue/margin growth
per period — should already be queryable from `kpi_facts` or equivalent). Cross-
sectional (per-quarter, across the tracked book — mirroring P2's "benchmark against
the same-period cross-section" pattern already used for item-diff materiality) is
more feasible near-term than a per-ticker time-series panel, since most tickers do
not yet have enough quarters on file for a within-ticker regression to have any power.

### Cheapest deterministic core / LLM surface

**Zero new LLM calls.** The tone score is already generated and cached
(`transcript_judgment.judge_call`); computing a residual against fundamentals is pure
Layer-3 arithmetic on data that already exists. This is the cheapest of the four gaps
in LLM-token terms specifically because the expensive half is sunk cost.

### Known measurement traps

- Raw tone re-derives the earnings surprise — the existing `tone_shift_is_abnormal`
  proxy must not be relabeled as "ABTONE" in any surface copy; it should stay
  explicitly named as the heuristic it is, alongside (not replaced by) a real
  residual once one exists.
- Cao, Jiang, Yang & Zhang (NBER 27950): firms adapt language to the algorithms
  reading them — expect decay, re-validate the fundamentals regression periodically
  rather than treating a fitted coefficient as permanent.
- A cross-sectional regression across ~40-44 names per quarter has very little
  statistical power. It is fine as a **within-book deviation flag** (an outlier
  reading), never as a claim that the coefficient is "significant" — echoing the
  ruling's own standard-of-evidence point: published literature is the evidence the
  effect exists; our own regression is a book-specific normalization step (same
  spirit as P2 detrending), not a re-validation of Huang/Teoh/Zhang.
- The underlying tone score inherits known transcript-coverage gaps: aggregator
  transcripts are Q&A-only (no prepared remarks) for ~97% of the book, per
  `transcripts.longitudinal`'s own docstring — the residual step inherits any noise
  already present in that scoring.

### Size and falsification

**Size: S.** Depends mainly on whether a fundamentals join is already queryable
per (ticker, period) — likely yes; the LLM half is sunk cost.

**What would make this WRONG:**
- The fitted residual correlates almost perfectly with the existing two-rule
  proxy already in production — residualization added nothing real.
- The fundamentals regression explains ~0% of tone-score variance — either the
  fundamentals feed or the tone scores are the wrong inputs, not evidence tone is
  "already abnormal by default."
- Flagged residuals cluster by `parse_coverage` value (e.g. always fire for
  `reparsed_aggregator` calls, never `segmented` ones) — that is a parsing-coverage
  artifact, not a tone signal.

---

## Gap 3 — CFO-vs-CEO language separation

**Evidence.** Larcker & Zakolyukina, *JAR* 50(2), 2012: a deception-marker language
model built from **CFO** speech earned a significant **-4% to -11% annualized
four-factor alpha**; the identical model built from **CEO** speech did not. Pooling
management speech into one bucket discards the finding entirely.

### How far the existing construct already carries it

The attribution substrate is already built and is the reason the ruling calls this
"now feasible":

- `transcripts.longitudinal.classify_roles` already classifies every speaker **by
  name** into MANAGEMENT/ANALYST/OPERATOR, and tone/non-answer measures are already
  tracked per individual named speaker, never pooled — the module docstring states
  this explicitly is "so the CEO-vs-CFO non-interchangeability finding is respected
  even without a resolved title."
- `resolve_exec_role(conn, ticker, name)` already joins a MANAGEMENT speaker name
  against `exec_comp_packages` (DEF 14A NEO names/roles) to resolve CEO/CFO — but
  `exec_comp_packages` is **empty for the entire book** as of this writing, so today
  every speaker resolves to `exec_role=None` ("unresolved role") in practice; the
  emitted `transcript_tone_shift_abnormal` events already carry this fallback label.

So per-name isolation exists; role RESOLUTION at scale does not. That is the actual
minimum delta, and it is smaller than it sounds:

**What genuinely does not exist yet:**
1. **A populated `exec_comp_packages`** (DEF 14A-sourced, structured extraction — an
   existing-pattern backfill, not a novel detector), OR a cheaper partial win: the
   ~3% of transcripts ingested from FactSet CallStreet PDFs
   (`compute.transcript_ingest`'s "segmented" path) carry a "CORPORATE PARTICIPANTS"
   roster block with stated titles ("John Smith — Chief Financial Officer") —
   **`transcripts.longitudinal.strip_document_artifacts` currently treats this exact
   block as pure noise and deletes it**, rather than parsing it for name→title before
   stripping. That is a genuinely cheap, zero-new-fetch win for the ~3% "segmented"
   subset; the aggregator-sourced ~97% never carries this line at all (prepared
   remarks, where it would live, are excluded from that source by design), so
   `exec_comp_packages` remains the wider-coverage path.
2. **A real CFO-specific language feature set.** This is the part that must not be
   glossed over: Larcker/Zakolyukina's construct is a **deception-marker** model
   (hedging language, distancing pronouns, extreme-positive-emotion markers, specific
   psycholinguistic categories) — a genuinely different construct from the existing
   -1..+1 sentiment tone score in `transcript_judgment`. Reusing the sentiment score
   as-is and calling it "the CFO signal" would be citing the wrong construct entirely.
   The per-name isolation plumbing is necessary but not sufficient; the actual
   marker set is new LLM/dictionary judgment work, not a re-derivation of tone.

### Cheapest deterministic core / LLM surface

Role resolution itself needs no LLM: the roster-block parse is a regex/line
heuristic (same genre as `_looks_like_risk_heading`), and DEF 14A→
`exec_comp_packages` extraction is structured-document extraction, not novel
judgment. An LLM is only a narrow fallback when a title is stated in ambiguous free
prose a regex can't parse cleanly. The deception-marker feature set, once role is
resolved, is the one part of this whole doc that is a genuinely new LLM judgment
layer rather than a reuse — batched per speaker's accumulated Q&A answer text,
never per question, matching the repo's existing batching discipline.

### Known measurement traps

- **Do not conflate the existing sentiment tone score with the deception-marker
  construct.** They are different measures from different papers; the per-name
  isolation is shared infrastructure, the feature set is not.
- **CFO/CEO roles rotate.** A "CFO series" must be tied to the resolved
  person-at-the-time, not a fixed name — an executive transition (promotion, new
  hire) must not silently blend a CFO's language history with a successor's or a
  CEO's.
- **Small-N per ticker.** Most tickers have exactly one person occupying "CFO" for
  most of the available history — a within-ticker series of a single individual's
  language over at best ~10-20 quarters. State this honestly; it limits what can be
  claimed in-book, though (per the ruling) the external literature remains the
  evidence gate regardless.
- Some issuers (smaller-cap, founder-led) blur the CFO/principal-accounting-officer
  or CEO/finance-chair lines — the clean CFO-vs-CEO separation Larcker/Zakolyukina
  studied may simply not apply structurally to every name in this book, which is a
  reason some tickers are out of scope, not a detector bug.

### Size and falsification

**Size: M.** Depends mostly on (a) the DEF 14A `exec_comp_packages` backfill, likely
cheap since it is a known structured filing type with a table already waiting, and
(b) building the deception-marker feature set, which is the bulk of the size.

**What would make this WRONG:**
- CFO and CEO scores are indistinguishable across the WHOLE book once role is
  resolved — check the resolution rate first: if most speakers are still
  "unresolved," this is a resolution failure (comparing noise to noise), not
  evidence the finding doesn't hold.
- The deception-marker feature set is just the existing sentiment tone score
  relabeled — that is citing the wrong construct, not implementing this gap.
- A ticker's "CFO series" silently spans an executive transition — any observed
  language shift is a personnel change, not a language-quality signal.

---

## Gap 4 — Detrended document-level YoY similarity (the Lazy Prices construct itself)

**Evidence.** Cohen, Malloy & Nguyen, *JF* 75(3), 2020, "Lazy Prices": shorting firms
that changed their filing language and going long non-changers earns **up to
188bps/month**; four similarity measures (cosine, Jaccard, minimum edit distance,
simple); portfolios on **quintiles of the prior year's cross-sectional
distribution**; signal concentrates in exec-team/litigation/Item 1A language; **no
abnormal return around the filing date** — the effect is drift over subsequent
months, read as investor inattention. Caveats that travel with it, already logged in
the literature review: Kent Daniel's formal discussion/critique of the paper, and
McLean & Pontiff's ~50-58% post-publication anomaly decay.

This is the gap the earlier build deliberately deprioritized as "the weak measure"
(item-level beats document-level per Lyle/Riedl/Siano, Campbell et al., Kravet &
Muslu) — correctly, for ranking purposes, but wrongly for omitting it altogether: it
is the single most replicated finding in this whole literature. The task here is to
track it honestly as a coarser, secondary measure, not to let it compete with or
replace the item-level P0 events for the same period.

### How far the existing construct already carries it

- `filings.item_diff._similarity` (token-set Jaccard) is **already one of
  Cohen/Malloy/Nguyen's four similarity measures**, already validated and in
  production — just computed at item grain, for the reworded-item fallback pass, with
  its complement (`materiality = 1 - similarity`) already stored per `item_reworded`
  event.
- `filing_sections` (migration 0198) already stores the full section text
  (`text`, `text_sha256`, `char_len`) per (ticker, source, form, period,
  `canonical_id`/`section_stem`) — a whole-canonical-section similarity score (e.g.
  the entire `risk_factors` text, or the entire `mdna` text, concatenated across its
  items in order) between consecutive periods is a direct read over
  `store.section_timeline`, no new fetch, no new extraction.
- Cohen/Malloy/Nguyen's OWN unit is actually closer to whole-disclosure
  (Item 1A as a whole, litigation language as a whole) than to individual risk-factor
  bullets — so the right grain to reuse here is `canonical_id`-level whole-section
  text, one level up from what P0/`item_diff` computes today, not a competing
  measure at the same grain.
- The cross-sectional detrending this measure absolutely requires (same-year
  quintiles, per Cohen/Malloy/Nguyen's own methodology, and per this repo's own
  build-stack P2 spec) does not exist yet for similarity scores, but the **pattern**
  is already proven: `metric_lifecycle.build_standard_transition_corpus` already
  sweeps the whole cached book once per run and compares each candidate against a
  corpus-wide histogram — structurally the same "build once, compare each ticker
  against it" shape a same-year quintile ranking needs, just over a continuous score
  instead of a boolean stop-count.

**Say it plainly, as the task asks:** this does NOT unify with Gap 1's detector —
it is a continuous magnitude over stored text, not a presence/absence judgment against
an issuer's own cadence, so it does not fit the `SubjectSeries` abstraction proposed
for Gap 1. What IT reuses is the **corpus-sweep pattern**, not the detector itself.

**What genuinely does not exist yet:**
1. Whole-canonical-section similarity computed between consecutive periods (a
   direct reuse of `item_diff._similarity`/`_tokens`, applied to concatenated
   section text instead of individual item bodies).
2. A cross-sectional ranking/quintile step: for each (form, canonical_id, period),
   compute this score for every tracked ticker with both periods stored, then rank
   each ticker's own score against that same-period distribution — new code, but a
   direct structural clone of `build_standard_transition_corpus`'s sweep-once-per-run
   pattern.
3. A new `disclosure_events.event_type` (e.g. `document_similarity_low`) — fits the
   existing schema with **zero migration**: `subject` = canonical_id/section_stem,
   `materiality` = the detrended score, no new columns required. `verdict` is less
   applicable here than for Gaps 1-3 (Cohen/Malloy/Nguyen's finding is
   unconditionally "change is bad news," not a concealment-vs-maturity judgment call)
   — a fixed informational verdict is a reasonable default, worth a small design
   decision but not a schema question.

### Cheapest deterministic core / LLM surface

**Fully deterministic, zero LLM at any stage.** Token-set similarity is arithmetic
over already-stored text; cross-sectional ranking is arithmetic over already-computed
scores. This is, narrowly, the cheapest gap of the four to build — no new fetch, no
new judgment call, almost pure aggregation over what P0/0198 already persist. Any
LLM step (e.g., explaining *why* a low-similarity ticker changed, quoting the
biggest diff hunks) is optional polish layered on afterward, not required to emit
the raw signal.

### Known measurement traps

- **Must detrend against the same-year cross-section, never a fixed historical
  baseline** — Dyer, Lang & Stice-Lawrence (*JAE* 64, 2017) found median 10-K length
  roughly doubled 1996-2013; an un-detrended score manufactures a spurious
  "everyone's changing more every year" trend. This is the single most emphasized
  trap in the whole literature review for this exact measure, and it is the one gap
  in this doc where skipping it isn't a minor omission — it inverts the finding.
- **No abnormal return around the filing date.** This is drift over subsequent
  months. A surface must never present this as a day-of alert — the ruling's
  "Surfaces" rule already applies generally, but here it is uniquely load-bearing
  because it is literally the structural property the founding paper measured.
- **This is deliberately the WEAKEST version of the measure**, per three independent
  lines of evidence already in this repo's own literature review (Lyle/Riedl/Siano,
  Campbell et al., Kravet & Muslu). It must ship labeled as coarser and secondary to
  item-level P0 events for the same period, never as a replacement or as carrying
  more information than they do.
- **Section-boundary quality risk, acutely.** `filing_sections`/`item_diff` already
  document that a bad splitting boundary (the pre-2026-07-25 header-matching
  defects) inflates any change metric computed over it without warning. A
  whole-section aggregate inherits this risk MORE severely than item-level does — one
  bad boundary corrupts the entire aggregate, not just one item. Gate on
  `filing_section_coverage.reason_code` / `slice_boundaries_suspect` before trusting
  a score, exactly as already documented for item-level diffs.
- Kent Daniel's critique and McLean & Pontiff's ~50-58% decay apply with full force
  here specifically, since this is the paper actually being operationalized, not a
  secondary citation.

### Size and falsification

**Size: S/M.** Depends on how many tickers/periods already have both consecutive
periods of a given `canonical_id` stored (should be most of the book, since P0 has
already run); the cross-sectional sweep is new code but structurally proven already.

**What would make this WRONG:**
- Un-detrended scores show a monotonic "more change every year" trend across the
  WHOLE cross-section, not just individual tickers — the length-inflation confound
  reappeared; detrending either wasn't wired or wasn't enough (the three specific
  mandated topics Dyer/Lang/Stice-Lawrence name may need separate exclusion).
- Low-similarity-quintile tickers correlate almost 1:1 with tickers carrying
  `slice_boundaries_suspect` flags rather than any real business signal — boundary
  noise masquerading as a disclosure-change signal, the same failure mode already
  documented for item-level diffs.
- The cross-sectional rank for the SAME ticker with essentially unchanged text
  swings quarter to quarter — the ~40-44-name same-year cross-section is too
  small/volatile to rank meaningfully at this book's size, and a coarser bucket (a
  raw z-score, or terciles instead of quintiles) is needed instead.

---

## The unification picture

Asked honestly, the four gaps resolve into **two shared substrates and one shared
pattern**, not four independent builds:

| Gap | Shares detector engine with | Shares a pattern (not the detector) with |
|---|---|---|
| 1. Guidance withdrawal | **P1's `metric_lifecycle` engine**, generalized to a new subject (one detector, two subject types — per the task's own framing) | — |
| 2. Abnormal tone | — | P4's per-speaker tone-scoring substrate (`transcript_judgment`, shared with Gap 3) |
| 3. CFO-vs-CEO separation | — | P4's per-speaker isolation substrate (`transcripts.longitudinal.classify_roles`, shared with Gap 2) |
| 4. Document-level similarity | — | P1's corpus-sweep pattern (`build_standard_transition_corpus`'s "sweep once, compare against a corpus" shape), reused for cross-sectional detrending, not for detection itself |

Gap 1 is the one genuine "these are secretly the same detector" finding this
exercise turned up, exactly as the task asked to look for: `metric_lifecycle`'s
Stage 0 (own-cadence gap calibration), Stage 1 (relabel suppression), and Stage 1.5
(cross-sectional mechanical-transition suppression) all transfer to a guidance-subject
series with no new *engine*, only a new *candidate feed* (guidance disclosure
presence, not yet computed) and a generic `SubjectSeries` abstraction in place of the
current `TagSeries`-shaped input.

Gaps 2 and 3 are correctly a second axis — a per-speaker language-quality question,
not a periodic-disclosure presence/absence question — and share P4's already-shipped
substrate (`transcripts.longitudinal` + `transcript_judgment`) without unifying with
each other: Gap 2's delta is a residual-fitting step on top of the existing tone
score; Gap 3's delta is role resolution plus an entirely new feature set (the
deception-marker construct is not the sentiment-tone construct, and must not be
conflated).

Gap 4 is a third axis again — a continuous aggregate magnitude over stored text, not
an expected-vs-actual disclosure judgment — so it does not fold into Gap 1's
`SubjectSeries` abstraction. It DOES reuse Gap 1's substrate in a narrower sense: the
corpus-sweep *pattern* `build_standard_transition_corpus` already proves out, applied
to a continuous similarity score instead of a boolean stop-count.

**Net new modules required across all four gaps: zero at the substrate level.**
Every gap's delta is either (a) genericizing an existing engine to a new subject or
score type, (b) a thin new stage on top of an already-shipped substrate, or (c) a
one-time backfill of an existing, currently-empty table (`exec_comp_packages`).

## Sizing summary

| Gap | Size | Primary size driver |
|---|---|---|
| 1. Guidance withdrawal | M | Coverage audit of `management_commitments` extraction + genericizing `metric_lifecycle`'s subject-series abstraction |
| 2. Abnormal tone | S | Fundamentals join + a regression/residual step over already-cached, already-paid-for LLM tone scores |
| 3. CFO-vs-CEO separation | M | `exec_comp_packages` backfill (cheap) + a genuinely new deception-marker feature set (the bulk of the size) |
| 4. Document-level similarity | S/M | Cross-sectional corpus-sweep step, new but structurally proven by `build_standard_transition_corpus` |

## Explicit non-goals for this doc

- No new `src/filings` or `src/transcripts` module, no migration, no detector script
  — scoping only, per the task.
- No in-sample event study proposed as a validation gate for any of the four gaps —
  the program ruling already settled that question; published research is the
  evidence, this book is not statistically powered to re-derive it, and re-litigating
  that here would repeat the exact error the ruling corrected.
- No claim that any of the four gaps is "done" once built — every one of them
  inherits `filing_section_coverage`'s absence-is-never-silent discipline and the
  repo's verdict-not-implied-by-event-type rule; none should ship emitting a
  directional claim the underlying paper itself does not support unconditionally
  (only Gap 4's "change is bad news" is close to unconditional in the literature;
  Gaps 1-3 all require an explicit unclear/unresolved default).
