# Disclosure-Change Program — Keep / Cut Ruling

Ruled 2026-07-25 by the owner, after the P0-P5 build and the event study.
Supersedes the priority ordering in `disclosure_change_build_stack.md` — that
document describes what was BUILT; this one describes what is MAINTAINED and why.

## The ruling in one line

**This is a deep-research program, not an alpha program.** Keep the detection
and reading tooling. Stop investing in return prediction on this book.

## Why alpha was ruled out

Not because the detectors are weak — because of the arithmetic of the universe.

1. **The cross-section is too small, structurally.** Cohen/Malloy/Nguyen
   established Lazy Prices on the entire universe of US filings over ~20 years.
   Cross-sectional anomalies are estimated by ranking thousands of firms against
   each other each period. This book has **44 tickers**. There is no
   cross-section to rank against, and adding periods (10-Qs) multiplies periods
   without widening it.
2. **Events cluster hard.** 5,954 detected events collapsed to **at most 109
   independent (ticker, arrival-date) draws**, decaying to 77 at a six-month
   horizon. Sub-buckets ran 20-26. Raw event counts massively overstate sample.
3. **The decisive evidence — a confound no sample size fixes.** In the event
   study, `verdict=substantive` (+8.8%) and `verdict=boilerplate_update`
   (+8.7%) showed near-identical six-month drift. If disclosure CONTENT drove
   returns, those two could not move together. The pattern is a
   universe-composition effect — the ~40 tracked names outperformed in
   2024-2026 — not an event-study finding. After controlling for earnings
   surprise, every bucket collapsed to approximately zero.
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

## What would reopen the alpha question

Only these, and (1) is the binding constraint:

1. A materially wider cross-section (hundreds to thousands of issuers), which
   this platform is not designed for and which FMP/EDGAR quota would not support.
2. A within-ticker placebo design separating universe composition from event
   effects.
3. An out-of-sample period not overlapping 2024-2026.

Absent those, treat any return number computed on this book as unsupported.
