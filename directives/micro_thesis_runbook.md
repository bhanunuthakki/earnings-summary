# Review Runbook Template

Use this exact structure when producing output. One block per holding.

---

## [TICKER] — [🟢 Intact / 🟡 Watch / 🔴 Broken]

**Thesis:** [one line from micro_thesis/holdings/<TICKER>.json]
**As of:** [date] | **Period covered:** [Q_ FY__]
**Sources:** [list: 10-Q link, transcript link/uploaded, press release, third-party]

### Document intake
| File | Modified | KPIs covered |
|---|---|---|
| [filename from micro_thesis/sources/<TICKER>/] | [date] | [list] |

⚠️ **Gaps:** [T1 KPIs not found in dropped docs — supplemented via web / still missing]

### Tier 1 Scorecard
| KPI | Current | Prior Q | YoY | Break Condition | Status | Source |
|---|---|---|---|---|---|---|
| ... | ... | ... | ... | [from JSON] | 🟢/🟡/🔴 | [doc type, period, page/section] |

Every row must carry an inline source tag. Use `[not disclosed]` for any cell where the value is not in the available source documents — never guess.

### Diff vs prior review
- [what changed materially, including any management commentary shifts] — cite sources

### Adversarial Loop — Thesis Verdict (REQUIRED, all verdicts)
- **Primary Thesis:** ... [Source: ...]
- **Strongest Counter:** ... [Source: ...]
- **Resolution:** ... — Net Conviction: High / Medium / Low. Specific observable that would flip the verdict: ...
- **Sensitivity:** if primary read is wrong by ±X%, ...

### Adversarial Loop — Say-Do Attribution (REQUIRED when prior-period guidance exists)
- **Primary Thesis:** Execution vs. Exogenous read, with quoted prior guidance vs. current actual [Source: ...]
- **Strongest Counter:** ...
- **Resolution:** ... — Net Conviction: High / Medium / Low.
- **Sensitivity:** ...

### Adversarial Loop — Valuation / Trigger Distance (REQUIRED for any T1 within ~15% of break_condition, or any trigger that fired)
- **Primary Thesis:** ...
- **Strongest Counter:** false-positive risk / single-print artifact / mix effect / etc.
- **Resolution:** ... — Net Conviction: High / Medium / Low.
- **Sensitivity:** distance to threshold under ±X% scenarios.

### Action
- [None / Monitor X into Q_/ Review Hold-Sell matrix / Deploy trigger check]

### Data gaps
- [any T1 metric you couldn't source — what's needed from user]

---

## Portfolio-level summary (full monthly only)

| Ticker | Verdict | Thesis driver status | Action |
|---|---|---|---|
| ... | ... | ... | ... |

**Broken theses requiring Hold/Sell review:** [list or "none"]
**Upcoming catalysts (next 45 days):** [earnings dates, trial readouts, regulatory]
