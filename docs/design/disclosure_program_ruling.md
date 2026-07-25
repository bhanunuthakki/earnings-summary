# Disclosure-Change Program — What We Track and Why

Ruled 2026-07-25 by the owner, after the P0-P5 build. Supersedes the priority
ordering in `disclosure_change_build_stack.md` — that document records what was
BUILT; this one records what is MAINTAINED and on what evidence.

## The ruling in one line

**Track the signals robust research supports, and weigh them probabilistically
alongside everything else.** In-sample validation on this book is NOT the gate
and never was.

## The standard of evidence — read this first

An earlier draft ran an event study on the 44-ticker book, found it
underpowered, and concluded the program was "research tooling, not alpha."
**That reasoning was wrong at the root.** It treated failure to replicate in a
44-name owned basket as evidence against the signal class, when it was only
evidence that the sample cannot test it. No investment process holds its inputs
to the standard of re-deriving published effects in its own portfolio.

The correct standard: **robust external research is the evidence.** If a
replicated, peer-reviewed finding says a disclosure or tone change carries
predictive information, then surfacing that change is a justified input —
because investing is probabilistic and inputs are weighed, not proven. The
pipeline's job is to make the signal visible, honest and receipted; not to
independently re-derive an effect on a sample too small to do it.

## The evidence base

| Finding | Source | Setting |
|---|---|---|
| Filing-language change predicts returns; short changers / long non-changers **up to 188bps/month** | Cohen, Malloy & Nguyen, *JF* 2020 | entire US filing universe, ~20 yrs |
| CFO deceptive-language model earns **-4% to -11%** annualized 4-factor alpha (CEO language: nothing) | Larcker & Zakolyukina, *JAR* 2012 | large US firms |
| Q&A non-answers predict analyst downgrades and **subsequent-quarter abnormal returns**; ~11% base rate, stable across time and industry | Gow, Larcker & Zakolyukina, *JAR* 2021 | broad panel |
| Risk-factor updates — especially **>100-word QoQ growth** — predict lower abnormal returns and future negative earnings shocks | Filzen, *Acc. Horizons* 2015 | broad 10-Q panel |
| **Abnormal** tone (residualized on fundamentals) predicts negative future earnings and a delayed negative reaction over 1-2 quarters | Huang, Teoh & Zhang, *TAR* 2014 | broad panel |
| Expected-but-missing disclosure: **-41bps** around the next earnings announcement | Zhou & Zhou, *JAR* 2020 | broad panel |
| Stopping quarterly EPS guidance: **-4.8%** 3-day CAR | Chen, Matsumoto & Rajgopal, *JAE* 2011 | 96 stoppers |
| Individual risk-factor **additions AND removals** both move the variance risk premium | Lyle, Riedl & Siano, *TAR* 2023 | broad panel |

Caveats that travel with all of it, and should temper the weight — not the
decision to track: post-publication decay of ~50-58% (McLean & Pontiff, *JF*
2016), a formal critique of Lazy Prices by Kent Daniel, and documented firm
adaptation to machine readers (Cao et al., NBER 27950).

Two structural properties shape what we build:

* **The signal is DRIFT, not announcement.** Cohen/Malloy/Nguyen found NO
  abnormal return around the filing date; it accrues over subsequent months.
  These are not day-of alerts — they stay relevant for weeks.
* **It localizes to SPECIFIC sections** (Item 1A, litigation, executive-team
  language), not whole documents. This is why item-level is the right
  granularity and whole-document length is the weakest measure.

## KEEP — tracked as literature-supported research inputs

| Component | Evidence it serves |
|---|---|
| `filing_sections` store + coverage semantics (0198) | Substrate. Honest absence-reporting is what makes every consumer safe. |
| Item add/remove/reword (P0) | Lyle/Riedl/Siano — both directions informative at item level. |
| Discontinued-metric detector (P1) | Zhou/Zhou (expected-but-missing) and Chen/Matsumoto/Rajgopal (cessation). Four suppression stages; quarantines ASC 842/606 transitions. |
| Specificity / boilerplate classifier (P3) | Hope/Hu/Lu specificity; carries the Filzen >100-word threshold. Also triage — 1,379 boilerplate vs 1,923 substantive is how a human avoids reading 5,954 diffs. |
| Cross-sectional detrending (P2) | Dyer/Lang/Stice-Lawrence — length inflation is market-wide; magnitudes must be same-period-relative. |
| Transcript speaker attribution | Prerequisite for Gow/Larcker/Zakolyukina and Larcker/Zakolyukina, both of which are speaker-role-specific. 38% -> 92.9%. |
| Earnings-audio deep link | Human access to recorded call + attributed transcript. Not a quantitative signal. |

## GAPS — literature-supported, not yet tracked

These are the highest-value additions, because published evidence backs them
and we do not surface them at all:

1. **Guidance withdrawal / expected-but-missing disclosure.** Zhou/Zhou's
   -41bps and Chen/Matsumoto/Rajgopal's -4.8% are among the best-evidenced
   effects in this whole space, and P1 detects discontinued XBRL METRICS but
   not withdrawn GUIDANCE. Closest thing to a direct build.
2. **Abnormal tone (ABTONE), residualized on fundamentals** — Huang/Teoh/Zhang.
   Raw tone mostly re-derives the earnings surprise; the residual is the signal.
3. **CFO-vs-CEO language separation** — Larcker/Zakolyukina found the CFO model
   tradeable and the CEO model not. Pooling management speech discards the
   finding. Now feasible given the attribution fix.
4. **Document-level YoY similarity, detrended** — the actual Lazy Prices
   construct. Deliberately skipped as "the weak measure," which was right for
   ranking but wrong for omitting: it is the single most replicated finding here.

## PARK / DO NOT SHIP

| Component | Disposition |
|---|---|
| **P5 event study** | **Do not repeat on this book.** Not because it failed — because in-sample validation was never the standard and 44 owned names cannot supply one. Keep the code; do not cite any cell from it as a finding either way. |
| **P4 non-answer rate** | **Do not ship until it reproduces the construct.** This is a CORRECTNESS bar, not a validation bar: our measure reads 30.1% against a baseline the literature finds stable at ~11%, and it moved further away after attribution was fixed. That means OUR DETECTOR is wrong, not that the signal is. Fix the period-dedup and Q&A-pairing bugs, re-measure, then ship. |
| **P7 vocal affect** | **Out of reach.** Text-only cannot recover it (Mayew/Venkatachalam is an acoustic effect). Never proxy it from transcript disfluencies. |

## Surfaces

Surface these as **weighted inputs with receipts**, never as forecasts or
day-of alerts — the drift structure of the evidence means they inform a view
over weeks, not a trade on the filing date. Feed into the thesis machinery as
break-rule / KPI inputs, per the ticker-beliefs ruling.

## Scope note

The narrative detectors run off EDGAR text and SEC companyfacts, which are
**free and unmetered** — FMP only ever supplied the R-file partition. The
pipeline is universe-agnostic by construction, so widening beyond the tracked
book is bounded by fetch time and storage rather than quota. Whether to do that
is an open owner decision, not a prerequisite for anything above.
