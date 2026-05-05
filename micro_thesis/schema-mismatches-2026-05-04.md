# Schema-vs-Disclosure Mismatches — Portfolio Audit

**Date:** 2026-05-04
**Scope:** 7 holdings with source documents (RBRK, VEEV, NOW, NU, MELI, NVO, BN)
**Method:** 7 parallel session-mode subagents read each ticker's `holdings/<TICKER>.json` against the extracted text from its source corpus, validated KPI names + break-condition thresholds against actual disclosure conventions.

---

## Executive Summary

**7 of 7 holdings have at least one schema-vs-disclosure mismatch.** No holding's schema is currently usable as-is for an automated-trigger system without producing false positives or missing the actual signal.

| Ticker | Tier-1 KPIs Clean | Break Conditions Clean | Severity |
|---|---|---|---|
| **MELI** | 4/4 | 3/4 | LOW — single fix (NPL threshold) |
| **NU** | 3/5 | 2/5 | MEDIUM — methodology change + threshold recalibration |
| **NOW** | 1/3 | 1/3 | MEDIUM — break already mechanically triggered |
| **RBRK** | 2/4 | 1/4 | MEDIUM — Total ARR not disclosed; NRR floor unmeasurable |
| **VEEV** | 5/7 | 5/7 | MEDIUM — total-sub break would trigger on company's own guide |
| **BN** | 3/6 | 3/6 | MEDIUM — disclosure cadence + carry waterfall miscalibration |
| **NVO** | 1/4 | 1/5 | **HIGH — schema reflects broken thesis, full rewrite warranted** |

**Most common failure modes:**
1. **Permanently-on/off thresholds** — break condition mechanically true regardless of issuer performance (MELI NPL, NOW Sub Rev cc, NVO breaks)
2. **Metric not disclosed** — schema asks for a KPI the issuer never publishes (RBRK Total ARR, NOW Pro+ SKU, NVO Wegovy standalone, BN insurance yield on float, VEEV gross retention quarterly)
3. **Disclosure cadence mismatch** — schema assumes quarterly but issuer reports annually/TTM (BN carry per Q vs. European waterfall; BN flagship-vintage now lives at BAM)
4. **Mid-year methodology shift** — comparability breaks within the schema's measurement window (NU NPL geo consolidation Q4'25)
5. **Stale thesis framing** — schema premises the original investment thesis even when the issuer's regime has materially changed (NVO supply-constrained-growth → share-defender)

---

## Per-Ticker Findings

### RBRK
2 of 4 KPIs match cleanly.

| Schema KPI | Disclosed? | Action |
|---|---|---|
| Subscription ARR Growth | ✅ | Keep (currently 34% YoY, decelerating from 39%) |
| Total ARR Growth | ❌ | Replace with **Cloud ARR Growth** (48% YoY — the higher-signal cloud-transition metric) |
| Subscription Revenue Growth | ✅ Partial | Keep but specify "**normalized** subscription revenue growth" (~$70M FY26 material-rights tailwind) |
| Net Revenue Retention Rate | ⚠️ | Rename to **Average Subscription Dollar-Based NRR** (issuer terminology); note disclosure is floor-only ">120%" |

| Schema Break | Realistic? | Action |
|---|---|---|
| Subscription ARR growth < 25% YoY | ✅ | Keep (real trip-wire; currently 34%, decelerating) |
| Total ARR growth < 15% YoY | ❌ | Re-baseline as **Cloud ARR growth < 30% YoY** |
| NRR < 115% | ❌ | Issuer only discloses ">120%" floor — re-baseline as "NRR floor disclosure drops below >120% bucket" |
| Sub revenue growth < 25% for 2Q | ⚠️ | Specify "**normalized**" to neutralize material-rights noise |

**Critical fix:** Total ARR Growth is unmeasurable.

---

### VEEV
5 of 7 KPIs match with naming tweaks.

| Schema KPI | Action |
|---|---|
| R&D Cloud subscription revenue growth | Rename → **R&D and Quality Solutions** subscription revenue growth |
| Commercial Cloud subscription revenue growth | Rename → **Commercial Solutions** subscription revenue growth |
| Vault CRM named migration wins (top-20 pharma) | Keep (10 of 20 committed; expects ~14) |
| Subscription revenue growth (total) | Keep |
| Gross retention | ❌ Not disclosed quarterly — demote to Tier-2 or annual-only |
| Operating margin (non-GAAP) | Keep |
| Top-20 pharma customer product attach count | Rephrase as "Top-20 biopharma per-product adoption counts" (per-product, not aggregate) |

| Schema Break | Action |
|---|---|
| Total subscription growth < 15% for 2Q | ❌ **Would trip on VEEV's own FY27 guide of ~13%** — re-baseline to **<12%** or "two Qs below guide midpoint" |
| Gross retention < 95% | ❌ Move to annual review or remove (not in quarterly disclosure) |
| Top-20 attach count flat YoY | ⚠️ Re-baseline as "no top-20 product-area additions across Vault CRM/QMS/Submissions/Link in 2 Qs" |

**Critical fix:** Total-sub break threshold mechanically triggers on management's own forward guide.

---

### NOW
1 of 3 Tier-1 KPIs matches; the schema is partly already-broken.

| Schema KPI | Action |
|---|---|
| Subscription Revenue Growth (CC) | Keep metric — **but baseline value 24.5% in schema is stale** (now ~19% CC) |
| cRPO Growth | Keep |
| Pro+ SKU Upsell Rate | ❌ Replace with **Now Assist >$1M ACV customer growth (YoY)** |

| Schema Break | Action |
|---|---|
| Subscription Revenue Growth (CC) < 20% | ❌ **Already triggered** (Q4'25 19.5% CC, Q1'26 19% CC, FY26 guide 19.5–20% CC) yet schema marks Green. Re-baseline to **<17% CC** |
| cRPO Growth < 18% | ✅ Keep (Q1'26 21% CC, Q2'26 guide 19.5% CC — reasonable buffer) |
| Pro+ adoption deceleration | ❌ Unmeasurable; replace with **Now Assist >$1M ACV growth < 50% YoY** |

**Critical fix:** Sub Rev (CC) <20% is mechanically triggered by management's own guide — schema is silently in a broken state.

---

### NU
3 of 5 KPIs match cleanly. Methodology shift in Q4'25 is the structural issue.

| Schema KPI | Action |
|---|---|
| Revenue Growth (FXN) | Keep (Q1 +40%, Q2 +33%, Q3 +30%, Q4 +30%) |
| Customer Count | Keep (118.6M → 131M across 2025) |
| Monthly ARPAC | Keep ($11.2 → $15.0) |
| NPL 90+ Day Ratio | ⚠️ **Bifurcate into NPL 90+ Brazil consumer (Q1-Q3 6.5-6.8%) AND NPL 90+ Group (Q4'25 4.1%)** — comparability breaks at Q4'25 when geography mix consolidated |
| Cost-to-Serve per Active Customer | Keep |

| Schema Break | Action |
|---|---|
| NPL 90d+ > 7% | ⚠️ Geography-dependent — re-baseline as either "Brazil consumer NPL 90+ > 8%" OR "Group NPL 90+ > 6%" (pick one based on what the bifurcated KPI tracks) |
| ARPAC declining QoQ for 2Q | ✅ Keep |
| FXN Revenue growth < 25% YoY | ❌ Too tight given $5B/Q revenue base — re-baseline to **<20% YoY FXN** |
| Customer net adds < 2M per Q for 2Q | ❌ Mechanically safe (Brazil saturation at 62% adult pop) — re-baseline as **<2.5M** OR replace with "Mexico active customer growth < 30% YoY" |
| Cost-to-serve rising QoQ without commensurate ARPAC expansion | ❌ Permanently triggered — replace with "**ARPAC/CTS ratio falling for 2 consecutive quarters**" |

**Critical fix:** NPL 90+ comparability break at Q4'25 from methodology change must be addressed before automated triggers run.

---

### MELI
4 of 4 KPIs map cleanly. Single critical fix.

| Schema KPI | Action |
|---|---|
| Revenue Growth (FXN) | Keep (Q1-Q4'25 35-40% FXN) |
| GMV Growth (FXN) | Keep (Q1-Q4'25 29-35% FXN) |
| TPV Growth (FXN) | Keep (Q1-Q4'25 53-72% FXN) |
| Operating Margin | Keep |

| Schema Break | Action |
|---|---|
| FXN Revenue growth < 20% YoY for 2Q | ✅ Keep |
| FXN GMV growth < 15% YoY | ✅ Keep |
| OpMgn contracts >400bps YoY without reinvestment rationale | ⚠️ Tighten to "without reinvestment rationale **AND no FXN revenue re-acceleration**" |
| Credit NPL 90d+ > 8% of portfolio | ❌ **Permanently triggered** (FY25 prints 17.5-18.5% structurally) — re-baseline as **"15-90d early-bucket NPL rises >150bps QoQ for 2 consecutive quarters"** OR "NIMAL compression >300bps YoY" |

**Critical fix:** NPL 90d+ >8% is the canonical permanently-on threshold — must be respec'd to a flow metric or directional change.

---

### NVO — HIGH SEVERITY (full thesis rewrite warranted)
Only 1 of 4 KPIs (Operating Margin) maps cleanly. 4 of 5 break conditions already triggered or near-triggered.

| Schema KPI | Action |
|---|---|
| Revenue Growth (CER) | Keep (but FY25 +10% — well below original thesis) |
| GLP-1 Sales Growth (CER) | ❌ Issuer reports as TWO lines: **GLP-1 Diabetes** and **Obesity care**. Replace with both |
| Wegovy Sales Growth | ❌ **Wegovy not separately disclosed** (only Obesity care franchise total) — replace with "Obesity care Sales Growth (CER)" + "**NVO US AOM TRx market share vs tirzepatide**" |
| Operating Margin | Keep |

| Schema Break | Action |
|---|---|
| CER Revenue growth < 15% YoY for 2Q | ❌ **Already triggered** (Q3 +15%, FY +10%, FY26 guide -5% to -13%). Re-baseline as "CER sales growth < -15% (worse than guide low end)" |
| GLP-1 franchise growth (CER) < 20% YoY | ❌ Triggered AND metric not disclosed as single line — remove; replace with "**Obesity care CER growth < 0% YoY**" |
| Wegovy growth < 15% YoY (demand signal) | ❌ Wegovy not disclosed — replace with "**NVO US AOM TRx share loses >500bps to tirzepatide over 2 quarters**" |
| OM contracts >300bps YoY without manufacturing explanation | ⚠️ Already contracted ~290bps; near-triggered. Re-baseline as "OM < 38%" or ">500bps contraction ex-restructuring" |
| Clinical trial failure on oral sema or CagriSema | ✅ Keep, narrow to "CagriSema + next-gen amycretin/cagrisema readouts" (oral sema already approved) |

**Critical fix: full thesis rewrite to "GLP-1 share defender + pipeline option" framing** — replace supply-constrained-growth metrics with share-defense metrics (US AOM TRx share, Obesity care CER growth, self-pay price). The original 20%+ supply-constrained-demand thesis is broken.

---

### BN
3 of 6 KPIs match cleanly. Disclosure cadence + entity rename are the structural issues.

| Schema KPI | Action |
|---|---|
| DE per share growth YoY | Keep |
| Fee-Related Earnings growth | Keep |
| Fee-bearing capital growth | Keep |
| Fundraising pace (flagship vintages) | ⚠️ **BN no longer publishes per-flagship subscription levels** (lives at BAM) — replace with "Annual capital raised vs. $200B/yr inflow target" |
| Insurance (BNRE) annualized earnings + assets | ⚠️ Keep but **rename BNRE → BWS** (entity rebranded) |
| Realizations / monetizations | ⚠️ Keep metric; re-baseline cadence (see below) |

| Schema Break | Action |
|---|---|
| DE per share <15% YoY for 2Q | ⚠️ Per-Q DE is lumpy from carry timing — re-baseline as "<15% on **TTM basis** for 2 consec Qs" |
| FRE <15% YoY | ✅ Keep |
| FBC flat or declining | ✅ Keep |
| Flagship fund undersubscribed | ⚠️ Re-baseline as "annual inflows materially below $200B run-rate" |
| Insurance earnings yield on float <9% | ❌ **BN discloses ROE not yield on float** — replace with "BWS ROE <12% for 2 Qs" |
| <$1B realized carry per Q for 2Q | ❌ **European waterfall makes per-Q floor a false-positive generator** ($6B over 3 yrs ≈ $2B/yr ≈ $0.5B/Q avg). Re-baseline as **"<$2B realized carry on TTM basis AND no advancement in carry-eligible capital toward recognition"** |

**Critical fix:** Carry break threshold is the most acutely miscalibrated — a per-Q $1B floor is mechanically a false-positive on European waterfall recognition.

---

## Cross-Cutting Patterns

### Pattern 1: Issuer disclosure ≠ schema vocabulary
The schemas were drafted with idealized KPI names that don't match how issuers actually label their disclosures:
- RBRK schema: "Total ARR" → issuer: "Subscription ARR" + "Cloud ARR"
- NOW schema: "Pro+ SKU Upsell Rate" → issuer: "Now Assist >$1M ACV cohort growth"
- NVO schema: "Wegovy Sales Growth" → issuer: only "Obesity care" franchise
- BN schema: "BNRE" → issuer: rebranded to "BWS"
- VEEV schema: "R&D Cloud" → issuer: "R&D and Quality Solutions"

**Fix pattern:** rename schema KPI to match issuer terminology verbatim. The KPI scorecard becomes self-evidently sourceable.

### Pattern 2: Permanently-on/off thresholds
Break thresholds set without reference to the metric's natural range:
- MELI NPL 90d+ >8% (issuer prints 17-18% structurally) — **always on**
- RBRK NRR <115% (issuer only discloses ">120%" floor) — **never measurable**
- NOW Sub Rev cc <20% (currently 19%, guide 19.5-20%) — **already on**
- VEEV total-sub <15% (FY27 guide ~13%) — **about to trigger on management's own guide**
- BN carry <$1B per Q (European waterfall ~$0.5B/Q avg) — **always on**
- NVO multiple — **already on, structural regime change**

**Fix pattern:** for any threshold set without baselining against 8+ quarters of historical disclosure, recalibrate to a value that gives meaningful signal (typically 1–2 standard deviations from trailing trend, or below management's own forward guide floor).

### Pattern 3: Disclosure cadence mismatch
Per-quarter thresholds applied to metrics the issuer reports annually or TTM:
- BN carry recognition (European waterfall — recognize over fund life, not quarterly)
- BN flagship vintage subscription (per-fund close, not per-quarter)
- VEEV gross retention (annual disclosure)
- NU customer-segment growth (Brazil saturated; Mexico growth would be the leading indicator)

**Fix pattern:** explicit cadence in the threshold definition ("on TTM basis", "on annual basis") and a corresponding adjustment to the alert logic.

### Pattern 4: Methodology shifts mid-window
NU is the cleanest example — Q4'25 consolidated NPL geography mix, breaking comparability with Q1-Q3'25 prints. The schema doesn't acknowledge this; an automated trigger would silently fail.

**Fix pattern:** when an issuer announces a methodology change, the corresponding schema KPI must be bifurcated (old basis + new basis tracked separately for one full year) before the new basis becomes the primary trigger.

### Pattern 5: Stale thesis framing
NVO is the canonical case — the schema premises the 2023-era "supply-constrained-growth" thesis. The 2026 reality is "share-defender + pipeline option," and the schema's growth thresholds are all mechanically broken.

**Fix pattern:** when an Adversarial Loop on Thesis Status produces a Net Conviction = Low or repeatedly-triggered breaks, queue a thesis-rewrite review independent of the per-quarter tracker run. The thesis itself, not the metrics, is what's wrong.

---

## Recommended Action Sequencing

1. **Immediate (apply now):** patch the 7 holding JSONs against the per-ticker fix tables above. Order of acuity:
   1. **NVO** — full thesis rewrite (treat as a fresh thesis-initiation, not a schema patch)
   2. **NOW** — fix the mechanically-triggered Sub Rev break
   3. **MELI** — fix the permanently-on NPL break
   4. **BN** — fix carry waterfall + entity rename + cadence
   5. **NU** — bifurcate NPL into Brazil/Group
   6. **RBRK** — replace Total ARR with Cloud ARR
   7. **VEEV** — re-baseline total-sub break + remove gross retention from quarterly

2. **Next session:** re-run the 7 thesis trackers using the patched schemas to verify that scorecards now produce meaningful Status flags (no `[schema mismatch — see Schema Hygiene]` rows).

3. **Pipeline-side:** ensure `generate_thesis_update`'s new "Schema Hygiene" required output section actually catches any future drift between schema and disclosure. (This was added in the Step 1 prompt-tightening pass.)

4. **Ongoing:** when the user adds a new holding to the portfolio, run this validation pass once before the first thesis tracker — drafting a schema in isolation and only catching mismatches via tracker output is wasteful.

---

## Methodology notes

- 7 parallel session-mode subagents, each handed only its ticker's JSON + extracted text. No web supplementation.
- Two minor fidelity issues observed:
  - MELI agent reported it could not read the 10-K extraction (the file does exist; possibly a tool encoding issue). Decks alone covered all KPIs so the validation is still sound for MELI.
  - NVO Wegovy standalone sales — the Annual Report does not break this out at the franchise level the agent could see; if a more granular cut exists in the Outlook section appendix, the validation may understate disclosure.
- The schema-mismatch rate (7/7 holdings) is high enough to suggest the original schema-drafting process did not include a "validate against actual issuer disclosure" step. The new `validate_holdings_schema.py` recommendation in the prior QA notes is now even more clearly justified — but for one-off remediation, this session-mode pass is sufficient to drive the patch.
