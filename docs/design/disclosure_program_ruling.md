# Disclosure-Change Program — Keep / Cut Ruling

Ruled 2026-07-25 by the owner, after the P0-P5 build and the event study.
Supersedes the priority ordering in `disclosure_change_build_stack.md` — that
document describes what was BUILT; this one describes what is MAINTAINED and why.

## The ruling in one line

**On THIS 44-ticker book, this is deep-research tooling.** Keep the detection
and reading layer. Do not attempt to validate return prediction here.

## What this ruling does NOT say

It does **not** say narrative-change signals lack predictive power. The
academic literature establishes the opposite, on wide universes:

| Finding | Source | Universe |
|---|---|---|
| Filing-language change predicts returns; short changers / long non-changers up to **188bps/month** | Cohen, Malloy & Nguyen, *JF* 2020 | entire US filing universe, ~20 yrs |
| CFO deceptive-language model earns **-4% to -11%** annualized 4-factor alpha | Larcker & Zakolyukina, *JAR* 2012 | large/profitable US firms |
| Q&A non-answers predict analyst downgrades and **subsequent-quarter abnormal returns** | Gow, Larcker & Zakolyukina, *JAR* 2021 | broad panel |
| Risk-factor updates (esp. >100-word growth) predict **lower abnormal returns** and future negative earnings shocks | Filzen, *Acc. Horizons* 2015 | broad 10-Q panel |
| Abnormal tone predicts negative future earnings and a **delayed negative reaction** over 1-2 quarters | Huang, Teoh & Zhang, *TAR* 2014 | broad panel |
| Expected-but-missing disclosure: **-41bps** around the next earnings announcement | Zhou & Zhou, *JAR* 2020 | broad panel |
| Stopping quarterly EPS guidance: **-4.8%** 3-day CAR | Chen, Matsumoto & Rajgopal, *JAE* 2011 | 96 stoppers |

The signal class is real and replicated. Caveats that travel with it: post-
publication decay (McLean & Pontiff, *JF* 2016, ~50-58%), a formal critique of
Lazy Prices by Kent Daniel, and documented firm adaptation to machine readers
(Cao et al., NBER 27950).

## Why return prediction cannot be VALIDATED on this book

Not because the detectors are weak, and not because the literature is wrong —
because of the arithmetic of a 44-name universe.

1. **The cross-section is too small, structurally.** Cohen/Malloy/Nguyen
   established Lazy Prices on the entire universe of US filings over ~20 years.
   Cross-sectional anomalies are estimated by ranking thousands of firms against
   each other each period. This book has **44 tickers**. There is no
   cross-section to rank against, and adding periods (10-Qs) multiplies periods
   without widening it.
2. **Events cluster hard.** 5,954 detected events collapsed to **at most 109
   independent (ticker, arrival-date) draws**, decaying to 77 at a six-month
   horizon. Sub-buckets ran 20-26. Raw event counts massively overstate sample.
3. **The study measured its own invalidity.** "Drift" here = cumulative
   abnormal return vs SPY over a post-filing window. Events classified
   `substantive` drifted +8.8% at six months; `boilerplate_update` drifted
   +8.7%. If disclosure CONTENT drove returns those could not be identical —
   so the drift is a universe-composition effect (the tracked names rose in
   2024-2026), not a disclosure effect. After controlling for earnings
   surprise every bucket collapsed to ~0.

   **These numbers are a diagnostic of the study, not evidence about the
   signal class.** Do not cite them as if they refute the literature above;
   they only show this book cannot test it.
4. **No multiple-comparison correction was possible at this n.** 108 cells at
   nominal 95% would produce ~5 zero-crossings by chance; exactly 4 were found.
   Consistent with noise.

Separating (3) from a real effect requires a within-ticker placebo (same
tickers, non-event quarters). That is not built, and building it does not fix
(1) or (2).

## KEEP — maintained as research tooling

These earn their place by making filings faster to read with receipts. None of
them needs to predict returns to be worth the maintenance.

| Component | Why it stays |
|---|---|
| `filing_sections` store + coverage semantics (0198) | The substrate. Honest absence-reporting is what makes every consumer safe. |
| `filing_section_items` + item add/remove/reword (P0) | "Which risk factors did this issuer add and drop, verbatim" is direct research leverage. |
| Discontinued-metric detector (P1) | Four suppression stages; correctly quarantines ASC 842/606 transitions. "They stopped disclosing X, last value was Y" is exactly the Netflix-subscribers case worth catching. |
| Specificity / boilerplate classifier (P3) | Triage. 1,379 boilerplate vs 1,923 substantive is how a human avoids reading 5,954 diffs. |
| Cross-sectional detrending (P2) | Keeps magnitudes comparable across years. Cheap, already built. |
| Transcript speaker attribution (roic.ai DOM fix) | 38% -> 92.9% attribution. Infrastructure, independent of any signal on top. |
| Google Finance earnings deep link | One-click recorded audio + speaker-attributed transcript. Human access, not a quantitative signal. |

## CUT / PARK

| Component | Disposition |
|---|---|
| **P5 event study** | **Parked**, not deleted. Keep as a periodic sanity check; do not invest further until a within-ticker placebo exists. Do not cite any cell from it as a finding. |
| **P4 non-answer rate** | **Not shipped as a signal.** It moved AWAY from the ~11% literature baseline after attribution was fixed (26.3% -> 30.1%), and MELI worsened under strictly better data — which falsifies the attribution hypothesis. The pairing and period-dedup bugs are worth fixing for research value; the metric must not be presented as reproducing the published construct until it does. |
| **P6 alerting surfaces** | **Reframed.** Build against research value (read this filing faster), never as an alert implying a forecast the platform does not have. |
| **P7 vocal-affect signal** | **Out of reach**, unchanged. Text-only pipeline cannot recover it; the deep link gives human access only. Never proxy it from transcript disfluencies. |

## A number worth remembering

**5 concealment verdicts out of 5,954 events.** If the classifier is roughly
calibrated, that is the base rate for an issuer actually hiding something. It
argues the value here is not catching concealment — it is reading filings
faster, with receipts. Which is the deep-research goal, not the alpha one.

## If the goal is STOCK PICKING, the question changes

Alpha here means picking names, which means the signal has to run over a
universe wide enough to rank against — not over 44 names already owned. The
literature's effect sizes come from exactly that setting. So the real decision
is not "does this work" (it does, per §2) but **"do we widen the universe?"**

Three options, honestly costed:

1. **Screen a wide universe** (S&P 500 / Russell 1000). The detectors already
   run off cached EDGAR + companyfacts, which are FREE and unmetered — the
   binding cost is fetch time and storage, NOT FMP quota, since the narrative
   half never needed FMP. This is the only option that produces stock-picking
   alpha, and it is more tractable than assumed: the pipeline is universe-
   agnostic by construction.
2. **Stay at 44 names as research tooling** — the current ruling. Zero new
   cost, no stock-picking capability.
3. **Buy the signal** rather than build it. Not evaluated.

Whichever is chosen, any *validation* still needs a within-ticker placebo
(same names, non-event quarters) and an out-of-sample period not overlapping
2024-2026.

**Treat any return number computed on the 44-name book as unsupported —
including the ones in §3 above.**
