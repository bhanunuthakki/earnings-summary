---
name: micro-thesis-tracker
description: Micro-thesis monitoring mechanics for holdings in the canonical portfolio roster, including monthly reviews and earnings or filing updates. The project-owned earnings-summary-investing skill routes valuation, new-company evaluation and governed thesis amendments.
---

# Micro-Thesis Tracker

## Purpose
Monitor the Tier-1 KPIs for each concentrated satellite holding against the pre-defined micro-thesis. Output a Red/Yellow/Green verdict per holding, diff vs. prior period, and trigger Hold/Sell matrix review when a T1 metric breaks.

This runbook supplies mechanics under the approved holdings schema. Use the
project-owned [investing skill](../src/advisor/skills/earnings-summary-investing/SKILL.md)
for routing and its [research method](../src/advisor/skills/earnings-summary-investing/references/research-method.md)
for public moat checks, transcript analysis and investment implications.
The method does not replace accepted KPI rules or the deterministic evaluator.

## When to use
- Monthly cadence review ("run the monthly")
- Single-name check post-earnings ("did NOW earnings hold up")
- When user uploads or drops a 10-Q, earnings release, transcript, or earnings presentation into a ticker's source folder
- When user asks "what's the thesis status on X"
- When user says "I added docs for [TICKER]" or "process [TICKER] earnings"

## Holdings coverage
Resolve scope from the current canonical portfolio roster. Per-holding KPI specs
live in `micro_thesis/holdings/<TICKER>.json`; file presence does not prove membership
or approval. Evaluation hypotheses use the investing skill's evaluation path.

Each JSON contains:
- `thesis`: one-sentence core thesis
- `tier_1_kpis`: thesis-breaking metrics (must have current value to produce verdict)
- `tier_2_kpis`: confirming metrics
- `tier_3_kpis`: context metrics
- `sources`: where each KPI is typically disclosed
- `break_conditions`: explicit numeric rules that flip verdict to Red

## Source document folders

Each holding has an optional drop folder at `micro_thesis/sources/<TICKER>/`
for owner-supplied documents. Use it alongside admitted project evidence.

```
micro_thesis/sources/
├── NOW/
│   ├── NOW_Q1_2026_transcript.pdf
│   ├── NOW_Q1_2026_10Q.pdf
│   └── NOW_Q1_2026_earnings_deck.pptx
├── NU/
│   └── NU_Q4_2025_transcript.pdf
└── ...
```

**Accepted file types:** PDF, DOCX, TXT, HTML, CSV, XLSX, PPTX. Confirm issuer and
fiscal identity from the document and intake receipt, not its filename.

Check admitted facts, transcripts, IR documents and relevant owner-supplied files
through the investing routes. Cross-reference same-period evidence and retain
conflicts. File modification time is an intake detail, not source authority,
fiscal identity or a reason to supersede an observation.

**Freshness:** Compare the covered fiscal periods, disclosure cadence and requested
cutoff with the latest expected issuer package. A recently modified old filing
is still old evidence. Report missing current packages through typed receipts.

**After processing:** Retain document identities, locators and coverage. Keep
acquisition gaps separate from extraction or semantic-admission gaps.

## Workflow

### Step 1 — Scope
Use the owner's requested monthly, single-name or post-earnings scope. Resolve
the current roster and exact fiscal period. Ask only when a missing scope choice
would change the work. Do not select an arbitrary ticker or require an upload
when supported public acquisition can supply the evidence.

### Step 2 — Data acquisition
1. Read admitted T1 values and their provenance through the existing resolver.
   Compare source definition, unit, scope, basis and fiscal period with the rule.
2. For gaps, use the issuer acquisition/intake routes for filings, earnings
   releases, slides and transcripts. Generic search discovers issuer sources;
   it does not replace company-reported facts. An empty drop folder is not an
   instruction to stop or ask the owner for files.
3. Optional public third-party evidence can add context with its coverage and
   limits. Do not require paywalled or private data as investor homework or
   silently substitute a proxy into an accepted T1 rule.
4. Keep missing, stale, conflicting and definition-incompatible inputs unresolved.
   State the missing source or binding and the next supported public step.
   Preserve confirmed breaches even when other inputs are unavailable.

**Document intake summary:** Before proceeding to evaluation, output a brief intake log:
```
📄 Sources ingested for [TICKER]:
- [document identity, publication date, fiscal period, locator] — covered: [KPIs]
⚠️ Gaps: [missing package / fact / semantic binding]
→ Public acquisition or unresolved evidence: [next step]
```

### Step 3 — Evaluation
For each T1 KPI:
- Record current value, prior quarter, YoY
- Use the approved rule version and deterministic evaluator through the investing
  route. Do not replace thresholds with management targets or a qualitative view.
- Assign color: 🟢 Green (tracking thesis) / 🟡 Yellow (watch) / 🔴 Red (break)

Holding-level verdict:
- **Intact**: all T1 Green
- **Watch**: any T1 Yellow, no Red
- **Broken**: any T1 Red → triggers Hold/Sell matrix review
- **Incomplete**: an essential rule input is unresolved and no breach is confirmed.
  Show coverage with any confirmed result. Missing input is not a Green result.

These are report dispositions, not permission to persist or override evaluator
state. Qualitative moat/transcript findings can request review without changing
an accepted numeric rule.

### Step 4 — Adversarial stress test (REQUIRED, always surfaced)

The adversarial loop is not optional and is not gated on verdict color. It runs every time, on three specific surfaces, and the structured output appears in the final report.

**Surfaces requiring the loop:**
1. **Overall thesis verdict** — even Intact/Green verdicts must articulate the bear read. A "Green with no counter" is under-examined.
2. **Say-Do attribution** — when comparable dated prior guidance exists, test
   execution against external causes such as macro, FX, supply or one-offs.
   Otherwise state that the comparison is unavailable; do not manufacture a miss.
3. **Valuation triggers / break-conditions** — for any T1 KPI within ~15% of its `break_condition` threshold, run the loop on whether the trigger is genuinely about to fire vs. a noisy single-print artifact. Also stress-test any trigger that *did* fire — false-positive risk matters.

**Loop structure (use these exact field names):**

```
Primary Thesis     : The asserted reading + the strongest supporting evidence,
                     with inline source citations [Source: doc, period, page/section].
Strongest Counter  : The most credible challenge — alternative reading of the same
                     data, contradicting datapoint, mix/composition effect, base-rate
                     argument, or management-credibility caveat. Cite sources.
Resolution         : How the two sides reconcile + Net Conviction (High / Medium / Low),
                     AND the specific observable that would flip the verdict (e.g.
                     "two consecutive quarters of GMV growth <12%" or "RPO bookings
                     coverage drops below 1.0x").
Sensitivity        : Supported calculation/scenario for the affected assumption
                     or trigger. If required inputs are absent, name the assumption
                     to review and the missing input; do not invent ±X% or fair value.
```

**Discipline:** A counter you cannot articulate is a gap in the analysis, not a sign of conviction. Push harder. If the only counter is "macro could deteriorate," reject it as too generic and find a name-specific one.

Stress-test inputs as well as conclusions: if the source documents are sparse or stale, treat the conviction as Low even when the surface read looks Green.

Conviction is an analyst assessment, not a calibrated probability. Apply the
research method to material moat evidence and Q&A/language findings. A missing
public mechanism test is unproven evidence, not an automatic thesis break.

### Step 5 — Output format

```
## [TICKER] — [Verdict: Intact / Watch / Broken / Incomplete]
**Thesis:** [one-line]
**As of:** [date] | **Sources:** [10-Q / transcript / press release + links]

| T1 KPI | Current | Prior Q | YoY | Break Threshold | Status | Source |
|---|---|---|---|---|---|---|
...
(every row carries an inline source tag: doc type, period, page/section)

**Diff vs last review:** [what changed materially, with sources]
[Include material moat evidence, complete-exchange unanswered components and
comparable language changes here; label absent baselines or partial coverage.]

**Adversarial Loop — Thesis Verdict**
- Primary Thesis: ...
- Strongest Counter: ...
- Resolution (Net Conviction: H/M/L): ...  ← include the specific observable that would flip the verdict
- Sensitivity: ...

**Adversarial Loop — Say-Do Attribution**
[When comparable dated prior guidance exists; otherwise state unavailable.]
- Primary Thesis: [Execution vs. Exogenous read with quoted guidance vs. actual]
- Strongest Counter: ...
- Resolution (Net Conviction: H/M/L): ...
- Sensitivity: ...

**Adversarial Loop — Valuation / Break-Condition Trigger Distance**
(Required for any T1 within ~15% of break_condition, and any trigger that fired)
- Primary Thesis: ...
- Strongest Counter: ...
- Resolution (Net Conviction: H/M/L): ...
- Sensitivity: ...

**Action:** [None / Monitor X next Q / Review Hold-Sell matrix / Deploy trigger check]
```

For full monthly, prepend a summary table:

```
| Ticker | Verdict | Key Driver | Action |
```

## Output discipline
- Terse. No filler. Data-first.
- Reference approved triggers when they explain the result; do not rewrite them.
- Flag if a metric the user specified is actually lagging/vanity — offer the leading alternative.
- Missing coverage routes through existing onboarding/evaluation mechanics;
  do not make a new accepted thesis from a scaffold.

## Updating KPI specs
Use the investing skill's governed thesis-revision path. Prepare the cited
amendment and distinguish metric definition, narrative and break-rule changes.
Persist only when the specific approval and supported versioned writer are
available. A report recommendation does not authorize an ad hoc JSON/SQL edit.
