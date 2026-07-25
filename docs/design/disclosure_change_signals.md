# Disclosure-Change & Language-Change Signals — Literature Review and Detector Design

Status: research complete, detector designed and empirically calibrated, NOT yet built.
Companion to `filing_longitudinal_language.md` (Phase 0 store, shipped). Written 2026-07-24.

Two questions drove this: (1) what does the academic literature actually establish about
disclosure and language change as a return signal, and (2) how do we detect a company
quietly dropping a metric, cheaply enough to run over the whole book every quarter.

## 1. What the literature establishes

### 1.1 Textual change predicts returns — but the design details are load-bearing

**Cohen, Malloy & Nguyen, "Lazy Prices", *Journal of Finance* 75(3), 2020.** The canonical
result. Shorting firms that CHANGED their filing language and going long non-changers earns
up to **188 bps/month**. Direction matters: **change is bad news.** Four similarity measures
(cosine, Jaccard, minimum edit distance, simple), portfolios on quintiles of the *prior
year's* cross-sectional distribution. Signal concentrates in **executive-team language,
litigation language, and Item 1A**. Crucially there is **no abnormal return around the filing
date** — the drift accrues over subsequent months, which they read as investor inattention.
Changes also predict lower future earnings and higher bankruptcy incidence.

Caveats we should carry, not bury: Kent Daniel formally discussed/critiqued the paper at the
2016 Red Rock conference; McLean & Pontiff (JF 2016) find published anomalies decay ~50-58%
out-of-sample. The 188bps is an in-sample, pre-publication number. Treat it as evidence the
*direction* is real, not as a target.

### 1.2 Item-level beats document-level

**Lyle, Riedl & Siano, *The Accounting Review* 98(6), 2023** detect **individual risk-factor
additions and removals** and find both directions reduce uncertainty about firm risk —
item-level change carries information a document-level cosine score misses. **Campbell et al.,
*RAST* 19, 2014** and **Kravet & Muslu, *RAST* 18, 2013** likewise work at risk-factor
granularity; Kravet & Muslu find YoY *increases* in risk-sentence count predict higher
post-filing volatility and more dispersed analyst revisions.

**Filzen, *Accounting Horizons* 29(4), 2015** is the sharpest operational finding: in 10-Qs,
firms that update risk factors have significantly lower abnormal returns around the filing,
lower future unexpected earnings, and more future extreme negative earnings shocks — and the
effect concentrates where **the risk section grows by more than ~100 words** versus the prior
quarter. That is a directly implementable threshold.

This validates the Phase 0 architecture: we store sections, not documents, and align them
across periods. Do per-hunk classification, not a single similarity number.

### 1.3 Raw similarity must be detrended or it lies

**Dyer, Lang & Stice-Lawrence, *JAE* 64(2-3), 2017**: median 10-K length **doubled from
~23,000 to ~50,000 words** 1996-2013, with boilerplate and redundancy rising almost
monotonically. Three topics of 150 — fair value, internal controls, risk factors — explain
nearly all the growth, much of it driven by FASB/SEC mandates rather than firm behavior.

Consequence for us: an un-detrended similarity score manufactures a spurious "everyone changes
more each year" trend. Benchmark each firm's YoY similarity against the **same-year
cross-sectional distribution** (Lazy Prices' own approach), never a fixed historical baseline.

### 1.4 Separating boilerplate from substance

**Hope, Hu & Lu, *RAST* 21(4), 2016** build an explicit **Specificity** measure separating
generic risk language from firm-specific disclosure; market reaction to the 10-K is more
positive when specificity is higher. This is a better template for our
`boilerplate_update vs. new_risk` classifier than Brown & Tucker's implicit argument that
cosine similarity is inherently robust to padding. **Brown & Tucker, *JAR* 49(2), 2011**
remains the reference for the MD&A modification score itself — and reports its own decay:
modification scores fell through the 2000s as MD&A length rose, and the price reaction
weakened with them.

### 1.5 Discontinued disclosure — a genuine literature gap

**No peer-reviewed paper studies "a firm stops disclosing an operating KPI it previously
reported" as its own object with an event-study effect size.** Both research passes converged
on this independently. The nearest analogs:

- **Chen, Matsumoto & Rajgopal, *JAE* 51(1-2), 2011, "Is Silence Golden?"** — 96 firms that
  stopped quarterly EPS guidance: **−4.8% three-day abnormal return**. Stoppers had poor
  trailing performance *before* stopping. After stopping, analyst dispersion rises and
  accuracy falls.
- **Zhou & Zhou, *JAR* 58(3), 2020, "The Dog that Did Not Bark"** — firms that fail to issue
  *expected* guidance: **−41 bps around the NEXT quarterly earnings announcement**, attributed
  to limited attention and short-sale constraints. This is the methodological template we
  want: model an expected-disclosure baseline per firm, treat deviation from it as the signal.
- **Call, Melessa & Volant (2024 WP)** — a vital counterweight: using COVID as a natural
  experiment (when the penalty for stopping guidance lapsed), **~40% of firms that suspended
  quarterly guidance never restarted**, and those permanent stoppers showed subsequently
  *positive* performance. They were "stuck" issuing guidance only because of the stopping
  penalty.

**Therefore: do not encode "stopped reporting = bearish".** Model two competing priors —
*concealment* (hiding deterioration) versus *maturity/reframing* (the metric stopped being the
right KPI; Apple dropping unit sales is the canonical example, and it outperformed after).
Distinguishing them is a judgment call on context, which is exactly what an LLM is for.

Regulatory note: **SEC Release 33-10751 (Jan 2020)** requires disclosure when a KPI's
*calculation or presentation* changes period-over-period, but is **silent on ceasing to
disclose a KPI altogether** — confirmed across several independent law-firm summaries. A real
regulatory blind spot, which is part of why the signal may persist.

### 1.6 Segment realignment

**Floros, Johnson & Zhao, *JAR* 63(2), 2025** — when SFAS 131 forced more segment disclosure,
affected firms **increased redaction in material contracts**, concentrated in firms with
greater profitability divergence across segments. Firms route around forced transparency into
adjacent, less-scrutinized channels. **Berger & Hann** establish the two concealment motives
(proprietary cost vs. agency cost) behind segment aggregation.

### 1.7 Transcripts

- **Q&A carries more signal than prepared remarks** — Matsumoto, Pronk & Roelofsen, *TAR*
  86(4), 2011. Score the sections separately; never one whole-call blob.
- **Gow, Larcker & Zakolyukina, *JAR* 59(4), 2021, "Non-Answers during Conference Calls"** —
  **~11% of analyst questions get an explicit non-answer, and the rate is stable over time and
  across industries**, which makes it an excellent normalization baseline. Non-answers rise
  with negatively-toned questions and with performance questions when performance is poor.
  Analysts downgrade after high-non-answer calls; lack of spontaneity is negatively associated
  with both the immediate reaction and **subsequent-quarter abnormal returns**.
- **Hassan, Hollander, van Lent & Tahoun, *QJE* 134(4), 2019** — for topic-share measures,
  only ~1% of variation is aggregate and ~9% sector×time; **over 90% is firm-level, and ~70%
  of that is within-firm-over-time**. This is the strongest single validation of the premise
  that change, not level, is where the signal lives. Their measure correlates 0.80 with the
  Baker-Bloom-Davis EPU index with a ~2.5% false-positive rate.
- **Huang, Teoh & Zhang, *TAR* 89(3), 2014** — use **abnormal** tone (residual after
  controlling for fundamentals), not raw tone. ABTONE predicts negative future earnings and a
  delayed negative reaction over the following 1-2 quarters.
- **Larcker & Zakolyukina, *JAR* 50(2), 2012** — deception markers; the **CFO** language model
  produced a significant −4% to −11% annualized four-factor alpha, the CEO model did not.
- **Frankel, Jennings & Lee, *Management Science* 68(7), 2022** — ML sentiment beats
  Loughran-McDonald dictionaries at conference-call dates, by a bigger margin than LM beat
  generic dictionaries. Supports using an LLM over word lists here.
- **Mayew & Venkatachalam, *JF* 67(1), 2012** — vocal affect predicts performance, but it is
  an **audio** signal. We are text-only: explicitly out of reach. Do not proxy it from "um"
  counts without separate validation.
- **Cao, Jiang, Yang & Zhang, NBER WP 27950** — firms measurably change their language to suit
  the algorithms reading them. Any tone signal we build is training data for tomorrow's IR
  department. Expect decay; re-validate published coefficients on recent data.

**Unvalidated by the literature** (build if we want, but do not claim support): KPI
disappearance on calls, analyst-roster change, executive-speaker change, analyst repeating a
question.

## 2. Empirical calibration of the discontinued-metric detector

Run against the real cached companyfacts (113 tickers) before any LLM involvement.

**Naive detection is ~90% noise.** A first pass ("tag reported ≥6 periods, absent from the last
4") produced **175 candidates for META alone**, including `PropertyPlantAndEquipmentNet` —
which META obviously still reports. Three distinct noise sources, each killable deterministically
and therefore for free:

| Noise source | Example | Fix |
|---|---|---|
| **Axis conflation** — annual-only tags judged against a quarterly period axis | META's lease tags "last reported 2024 Q3" | Split the period axis by form family; judge each tag on its own axis |
| **Irregular cadence** — many tags legitimately appear in only some periods | VEEV's PP&E detail tags | Calibrate against the tag's OWN historical max gap; flag only when silence exceeds what it ever tolerated |
| **Taxonomy relabels** — the disclosure continues under a new tag | MELI's `PaymentsToAcquirePropertyPlantAndEquipment` (stops 2024 Q3) → `PaymentsToAcquireProductiveAssets` (runs through 2026 Q1) | Look for a tag starting within ±2 periods of the stop with comparable magnitude; suppress |

The relabel case is the important one: it is a **pure XBRL rename with zero information
content**, and it would have been the loudest false positive in the book.

## 3. Detector design

Deterministic core, LLM only for judgment — per the repo's 3-layer rule, and it is also what
makes this cheap.

**Stage 0 — candidate generation (zero tokens).** Per ticker, per tag: axis-aware,
gap-calibrated absence detection over cached companyfacts, plus FMP R-file section-stem
lifecycle and `filing_sections` stem lifecycle. This is the Zhou & Zhou "expected disclosure
baseline" made concrete.

**Stage 1 — deterministic suppression (zero tokens).** Relabel detection; drop tags with too
little history; drop where the whole filing is missing (a coverage gap, not a disclosure
event — `filing_section_coverage` already distinguishes these).

**Stage 2 — relevance triage (one cheap call per ticker).** The LLM sees **tag names, labels,
last value and period — never a document**. ~40 candidates ≈ 600 input tokens. It answers one
question: is this a business-meaningful metric an investor would notice losing (users, ARR,
backlog, segment revenue, churn), or is it accounting plumbing (`AccruedVacationCurrent`)?
Haiku-class.

**Stage 3 — interpretation (only for survivors, few per ticker).** Pull the narrow context:
the section text around the metric's last appearance, the same section in the current period,
and the thesis block. Ask for the concealment-vs-maturity judgment (§1.5) with verbatim quote
receipts. Sonnet-class.

**Token-efficiency levers, in order of value:**
1. Deterministic suppression before any call — kills ~90% of candidates for free.
2. **`text_sha256` skip** — sections whose hash is unchanged are never diffed. Already stored
   in `filing_sections`; this is the single biggest lever for the language-change side.
3. Send diff hunks, never whole sections.
4. Batch per ticker, not per candidate.
5. Two-stage model cascade (cheap triage → expensive interpretation on survivors only).

**Signal construction, per the literature:** detrend similarity against the same-year
cross-sectional distribution (§1.3); score item-level adds/removes, not document cosine
(§1.2); flag risk-section growth >100 words QoQ (§1.2); residualize tone against fundamentals
before calling it a tone change (§1.7); control for earnings surprise before claiming
incremental content (§1.7).

## 4. Honest limits

- KPI-discontinuation has **no published effect size** to calibrate against. If we want a
  number, we have to run our own event study on events we detect. That is a genuine gap we
  could close, not a known result we are re-implementing.
- Lazy Prices' 188bps is in-sample and pre-publication, and was formally critiqued.
- Every tone/language signal faces documented adaptation decay (Cao et al.).
- Vocal cues are out of reach text-only.
- Cross-source period disagreement is real on off-calendar-FYE filers: the Phase 0
  reconciliation flagged AVGO, DHR, LITE and NVDA, where EDGAR and FMP bucket the same 10-K
  into different fiscal years. Those periods must not be diffed cross-source until resolved.
