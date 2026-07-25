# Disclosure-Change Build Stack — priority-ordered

Derived from the 2026-07-24/25 session: the Phase 0 section store (migration 0198),
the literature review (`disclosure_change_signals.md`), and the empirical calibration
runs against the real book. Schema for P0/P1 lands in migration 0200.

**The organizing finding — everything below is ranked by it:**

> **Additions and removals of INDIVIDUAL disclosure items, within SPECIFIC sections,
> are the signal. Total length and whole-document similarity are the weakest,
> noisiest version of the measure and are actively misleading alone.**

Three independent lines of evidence converged on this:

- **Item-level beats document-level.** Lyle/Riedl/Siano (TAR 2023) find individual
  risk-factor additions *and* removals both move the variance risk premium — power
  a document-level cosine score does not have. Campbell et al. (RAST 2014) and
  Kravet & Muslu (RAST 2013) work at the same granularity.
- **Specific sections carry it.** Cohen/Malloy/Nguyen (JF 2020) localize the return
  signal to executive-team language, litigation language and Item 1A — not the
  filing as a whole.
- **Length is confounded by a market-wide trend.** Median 10-K length roughly doubled
  1996-2013, driven mostly by three mandated topics (Dyer/Lang/Stice-Lawrence, JAE
  2017). Any length- or raw-similarity-based measure must be detrended against the
  same-year cross-section or it manufactures signal.

And the empirical finding from this book: **naive detection is ~90% noise** (175
"discontinued metrics" for META alone, including PP&E, which it still reports). The
noise dies deterministically and for free — never send it to a model.

---

## P0 — Item-level add/remove/reword engine

The core build. Splits stored sections into atomic disclosure items, aligns them
across periods, and emits typed events.

- Split `filing_sections` rows into `filing_section_items`: individual risk factors
  (reuse `filing_text_fetcher.split_risk_factors`' heading heuristic — do not
  reinvent), MD&A sub-headings, 20-F Item 3.D sub-items.
- Align items across consecutive periods: exact `body_sha256` → unchanged; heading
  `match_key` → candidate pair; content similarity → reworded; unmatched in prior →
  **added**; unmatched in current → **removed**.
- Emit `item_added` / `item_removed` / `item_reworded` into `disclosure_events`.
- **Removals are first-class, not an afterthought** — they are the harder-won half
  of the signal and the half a naive diff tends to drop.
- Zero LLM in this phase. It is deterministic text alignment.

## P1 — Discontinued-metric detector

Fully specified in `disclosure_change_signals.md` §3. Deterministic candidate
generation from cached companyfacts, then deterministic suppression, then a single
cheap per-ticker triage call over **tag names only, never documents**.

Three noise sources, all killable in plain code (measured this session):
axis conflation, per-tag irregular cadence, and **taxonomy relabels** (MELI's
`PaymentsToAcquirePropertyPlantAndEquipment` → `PaymentsToAcquireProductiveAssets`
is a pure rename with zero information content — the loudest false positive).

Model **concealment vs. maturity as competing priors**; never encode
"stopped reporting = bearish" (Chen/Matsumoto/Rajgopal -4.8% CAR, but ~40% of
COVID-era guidance stoppers never restarted and performed well — Call/Melessa/Volant).

## P2 — Cross-sectional detrending

Benchmark every similarity/length measure against the **same-year cross-sectional
distribution** across the tracked book, per Lazy Prices' quintile approach — never a
fixed historical baseline. Without this, P0/P1 magnitudes are not comparable across
years. Cheap to add once P0 emits events; expensive to retrofit into consumers later.

## P3 — Boilerplate vs. substance classifier

Hope/Hu/Lu (RAST 2016) "Specificity" is the template — separate generic risk language
from firm-specific disclosure. This is the LLM layer that turns P0's mechanical
add/remove events into `boilerplate_update` vs. `new_risk`. Runs on diff hunks only,
gated by the `text_sha256` skip already stored in `filing_sections`.

Concrete threshold worth encoding: risk section growth >100 words QoQ predicts lower
abnormal returns and more future negative earnings shocks (Filzen 2015).

## P4 — Transcript longitudinal tracking

Same architecture, different artifact.

- **Split prepared remarks from Q&A and score them separately** — Q&A is measurably
  more informative (Matsumoto/Pronk/Roelofsen, TAR 2011). Never one whole-call blob.
- **Non-answer rate** against the stable **~11%** baseline (Gow/Larcker/Zakolyukina,
  JAR 2021) — stable across time and industries, so deviation is the signal. Predicts
  analyst downgrades and subsequent-quarter abnormal returns.
- **Topic disappearance**: a KPI management stops discussing. Unvalidated in the
  literature — build it, but label it novel.
- Residualize tone against fundamentals before calling it a tone change
  (Huang/Teoh/Zhang, TAR 2014); control for earnings surprise or you are re-deriving
  a number you already have.
- CFO and CEO language are **not interchangeable** — only CFO language was tradeable
  (Larcker/Zakolyukina, JAR 2012).

## P5 — Own event study

KPI discontinuation has **no published effect size**. Once P0/P1 have emitted events
across the book, measure forward returns on our own detected events. This closes a
genuine literature gap rather than re-implementing a known result — and it is the
only honest way to size the signal.

## P6 — Surfaces

Feed confirmed events into the thesis machinery as break-rule / KPI-tracking inputs
(not stances), per the ticker-beliefs ruling. Consequence-first, receipts attached,
batched. Only after P0-P2 produce events worth acting on.

## P7 — Transcript audio (SPIKE, not a build)

Vocal affect predicts fundamentals *incremental to the words spoken* (Mayew &
Venkatachalam, JF 2012), so a text-only pipeline is structurally blind to it. Wiring
browser-based audio into the transcripts section is therefore a real capability gain,
not a nicety.

**Blocked on scoping, deliberately.** Unverified: what the intended audio source
exposes (embeddable player? URL per call? API?), whether per-call audio can be
resolved for this roster at all, and its terms of use. Do not build an integration
against an assumed interface. Spike first: confirm the surface, confirm coverage for
2-3 roster names, then scope. UI work follows `directives/design_language.md` and must
pass `tests/test_ui_controls.py`.

Explicitly out of scope until then: inferring vocal affect from transcript
disfluencies ("um"/"uh"). That substitution is unvalidated and would be a fabricated
signal.

---

## Cross-cutting rules for every item above

1. **Deterministic first, LLM for judgment only.** ~90% of candidates die in plain
   code. An LLM that sees raw candidates is both expensive and wrong.
2. **Never send documents.** Tag names, headings, diff hunks. The `text_sha256`
   already stored per section is the single biggest token lever.
3. **Batch per ticker, cascade models** — cheap triage, expensive interpretation only
   on survivors.
4. **Every event carries a verbatim quote.** No receipts, no surface.
5. **Absence is never silent** — the coverage semantics from 0198 apply: a detector
   that could not run says so, with a reason code.
6. **Expect decay.** Firms adapt their language to the machines reading it (Cao et al.,
   NBER 27950). Re-validate; do not trust a 2012 coefficient in 2026.
