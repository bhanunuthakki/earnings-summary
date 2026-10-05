# Review Runbook Template

Use this exact structure when producing output. One block per holding.
Apply the [research method](../src/advisor/skills/earnings-summary-investing/references/research-method.md)
within these sections. The accepted rules and deterministic evaluator remain
the threshold authority.

---

## [TICKER] — [🟢 Intact / 🟡 Watch / 🔴 Broken / Incomplete]

**Thesis:** [one line from micro_thesis/holdings/<TICKER>.json]
**As of:** [date] | **Period covered:** [Q_ FY__]
**Sources:** [list: 10-Q link, transcript link/uploaded, press release, third-party]

### Document intake
| Source document / locator | Published / fiscal period | KPIs covered / coverage limits |
|---|---|---|
| [identity + page/table/segment] | [date / period] | [list / gaps] |

⚠️ **Gaps:** [missing issuer package / fact / binding; public next step or unresolved]

### Tier 1 Scorecard
| KPI | Current | Prior Q | YoY | Break Condition | Status | Source |
|---|---|---|---|---|---|---|
| ... | ... | ... | ... | [from JSON] | 🟢/🟡/🔴 | [doc type, period, page/section] |

Every row must carry an inline source tag. Use `[not disclosed]` for any cell where the value is not in the available source documents — never guess.
Keep stale, incompatible or conflicting observations unresolved. Preserve any
confirmed breach; incomplete inputs do not produce an all-clear verdict.

### Diff vs prior review
- [what changed materially, including any management commentary shifts] — cite sources
- [material moat mechanism evidence/counter; complete-exchange unresolved question;
  comparable textual language change; affected pillar/assumption]. Omit immaterial
  additions. Missing prior review, Q&A coverage or matched passages stays explicit.

### Adversarial Loop — Thesis Verdict (REQUIRED, all verdicts)
- **Primary Thesis:** ... [Source: ...]
- **Strongest Counter:** ... [Source: ...]
- **Resolution:** ... — Net Conviction: High / Medium / Low. Specific observable that would flip the verdict: ...
- **Sensitivity:** supported scenario/calculation, or affected assumption and missing inputs.

### Adversarial Loop — Say-Do Attribution (REQUIRED when prior-period guidance exists)
- **Primary Thesis:** Execution vs. Exogenous read, with quoted prior guidance vs. current actual [Source: ...]
- **Strongest Counter:** ...
- **Resolution:** ... — Net Conviction: High / Medium / Low.
- **Sensitivity:** ...

### Adversarial Loop — Valuation / Trigger Distance (REQUIRED for any T1 within ~15% of break_condition, or any trigger that fired)
- **Primary Thesis:** ...
- **Strongest Counter:** false-positive risk / single-print artifact / mix effect / etc.
- **Resolution:** ... — Net Conviction: High / Medium / Low.
- **Sensitivity:** supported distance/scenario using comparable inputs; no invented percentage.

### Action
- [None / Monitor X into Q_/ Review Hold-Sell matrix / Deploy trigger check]
For each material follow-up, name its public source, next disclosure/event,
observable condition and thesis/model consequence. Sizing uses allocation.

### Data gaps
- [unresolved rule input or mechanism; available public check/proxy and limitation,
  or uncertainty that cannot be resolved publicly]. Do not require private data.

---

## Portfolio-level summary (full monthly only)

| Ticker | Verdict | Thesis driver status | Action |
|---|---|---|---|
| ... | ... | ... | ... |

**Broken theses requiring Hold/Sell review:** [list or "none"]
**Upcoming catalysts (next 45 days):** [earnings dates, trial readouts, regulatory]
