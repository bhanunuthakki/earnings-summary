---
name: micro-thesis-tracker
description: Use this skill when the user asks to run a monthly micro-thesis check, evaluate a specific holding's fundamentals, update the thesis ledger, or process a new earnings release / 10-Q / transcript for NU, MELI, NOW, VEEV, RBRK, WIX, NVO, GOOGL, META, FLKR, or BN. Triggers include phrases like "run the thesis tracker," "monthly review," "did [TICKER] earnings break the thesis," "update KPIs for [TICKER]," "I added docs for [TICKER]," "process [TICKER] earnings," or when the user uploads or drops an earnings transcript, 10-Q, or earnings presentation into a source folder. Source documents live in micro_thesis/sources/<TICKER>/ subfolders. Do NOT use for pure valuation/price-trigger questions (those are handled by the Code's existing threshold matrix) or for new-name diligence (use a separate initiation skill).
---

# Micro-Thesis Tracker

## Purpose
Monitor the Tier-1 KPIs for each concentrated satellite holding against the pre-defined micro-thesis. Output a Red/Yellow/Green verdict per holding, diff vs. prior period, and trigger Hold/Sell matrix review when a T1 metric breaks.

## When to use
- Monthly cadence review ("run the monthly")
- Single-name check post-earnings ("did NOW earnings hold up")
- When user uploads or drops a 10-Q, earnings release, transcript, or earnings presentation into a ticker's source folder
- When user asks "what's the thesis status on X"
- When user says "I added docs for [TICKER]" or "process [TICKER] earnings"

## Holdings coverage
Per-holding KPI specs live in `micro_thesis/holdings/<TICKER>.json`. Currently covered: NU, MELI, NOW, VEEV, RBRK, WIX, NVO, GOOGL, META, FLKR, BN.

Each JSON contains:
- `thesis`: one-sentence core thesis
- `tier_1_kpis`: thesis-breaking metrics (must have current value to produce verdict)
- `tier_2_kpis`: confirming metrics
- `tier_3_kpis`: context metrics
- `sources`: where each KPI is typically disclosed
- `break_conditions`: explicit numeric rules that flip verdict to Red

## Source document folders

Each holding has a drop folder at `micro_thesis/sources/<TICKER>/` for raw source documents. This is the primary data input mechanism — the user drops files here and invokes the skill.

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

**Accepted file types:** PDF, DOCX, TXT, HTML, CSV, XLSX, PPTX. No strict naming convention required — the user just needs to include the ticker and period somewhere in the filename for confirmation purposes.

**On invocation, always scan `micro_thesis/sources/<TICKER>/` first.** Sort files by modification date (newest first). Read each document and extract KPI values mapped in `holdings/<TICKER>.json`. When multiple documents cover the same period, cross-reference them (transcript commentary vs. 10-Q quantitative data vs. earnings deck visuals). Flag contradictions explicitly.

**Staleness rule:** If the newest file in a ticker's folder is >120 days old relative to the current date, warn the user that source docs may be stale and offer to supplement via web search.

**After processing:** Note which documents were consumed and their coverage. If a T1 KPI isn't found in any dropped document, flag it as a gap — don't silently fall through to web search without telling the user what's missing from their docs.

## Workflow

### Step 1 — Scope
Ask the user which mode:
- **Full monthly** (all 11 names)
- **Single name** (specify ticker)
- **Post-earnings** (specify ticker + provide/confirm source docs)

If scope is ambiguous, default to single-name with most recent earnings date.

### Step 2 — Data acquisition
For each holding in scope, acquire T1 KPI values in this priority order:

1. **Source folder documents** — scan `micro_thesis/sources/<TICKER>/` for all files. Read them newest-first. This is the highest-trust source. For each file processed, log what it contained and which KPIs it populated. If the user also uploaded documents directly in the conversation (not in the source folder), read those too and treat them at the same priority level.
2. **Web search** — for publicly reported metrics not found in source folder docs, search issuer IR pages, press releases, and earnings slide decks. Cite sources. Only fall through to this step after explicitly telling the user which T1 KPIs were NOT found in their dropped docs.
3. **Third-party datasets** — for the specific trackers listed below, attempt web fetch; if paywalled, ask user to provide:
   - NVO: IQVIA weekly Rx data (GLP-1 US volume)
   - FLKR: TrendForce DRAM/NAND contract prices
   - MELI/NU: Similarweb or SensorTower app rankings (optional, T2)
   - NOW/RBRK: Gartner MQ position (annual)
4. **Explicitly flag missing data** — never guess or interpolate. If a T1 KPI is unavailable, the verdict is "Incomplete" until the user provides it.

**When the source folder is empty:** If `micro_thesis/sources/<TICKER>/` has no files, tell the user: "No source docs found in micro_thesis/sources/[TICKER]/. Drop the transcript, 10-Q, and/or earnings deck there, or I'll work from web search only (qualitative signals will be weaker)."

**Document intake summary:** Before proceeding to evaluation, output a brief intake log:
```
📄 Sources ingested for [TICKER]:
- [filename] (mod: [date]) — covered: [list of KPIs extracted]
- [filename] (mod: [date]) — covered: [list of KPIs extracted]
⚠️ Not found in docs: [list of T1 KPIs still missing]
→ Supplementing via web search for: [list]
```

### Step 3 — Evaluation
For each T1 KPI:
- Record current value, prior quarter, YoY
- Apply `break_conditions` from the JSON
- Assign color: 🟢 Green (tracking thesis) / 🟡 Yellow (watch) / 🔴 Red (break)

Holding-level verdict:
- **Intact**: all T1 Green
- **Watch**: any T1 Yellow, no Red
- **Broken**: any T1 Red → triggers Hold/Sell matrix review

### Step 4 — Adversarial stress test
Before finalizing, run the internal dialectic per user's standing instruction:
- What's the bear case reading of these same numbers?
- Is there a composition/mix effect masking deterioration?
- Does management commentary contradict the quantitative print?

Only surface dialectic in output if the verdict is Watch or Broken, OR if user requests the debate ledger.

### Step 5 — Output format

```
## [TICKER] — [Verdict: Intact / Watch / Broken]
**Thesis:** [one-line]
**As of:** [date] | **Source:** [10-Q / transcript / press release + links]

| T1 KPI | Current | Prior Q | YoY | Status |
|---|---|---|---|---|
...

**Diff vs last review:** [what changed]
**Action:** [None / Monitor X next Q / Review Hold-Sell matrix]
```

For full monthly, prepend a summary table:

```
| Ticker | Verdict | Key Driver | Action |
```

## Output discipline
- Terse. No filler. Data-first.
- Never restate valuation triggers (those are in the Code, not this skill's scope).
- Flag if a metric the user specified is actually lagging/vanity — offer the leading alternative.
- If user asks for a holding not in coverage, offer to scaffold a new JSON.

## Updating KPI specs
When the user says "add [metric] to [ticker]" or "drop [metric]," edit the relevant `micro_thesis/holdings/<TICKER>.json` and confirm the change. Version the file with a `last_updated` field.
