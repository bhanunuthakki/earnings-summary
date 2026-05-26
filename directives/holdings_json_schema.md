# Holdings JSON schema (v2)

Each `micro_thesis/holdings/<TICKER>.json` is the single source of truth for
that name's thesis, KPIs, hard break rules, and soft watch signals. The
file lives in source control; the evaluator + report renderers consume it
through `src/compute/thesis_evaluator.py` and `src/report/sections/thesis.py`.

This doc pins the shape so authors don't need to read code to know what fields
mean. When a field's semantics change, edit here in the same PR.

## Top-level fields

```jsonc
{
  "ticker": "VEEV",                       // required, uppercase
  "name": "Veeva Systems",                // required, display name
  "last_updated": "2026-05-10",           // ISO date; populated on every edit
  "thesis": "...",                        // one-paragraph core thesis
  "verdict": "Pending|Intact|Watch|Broken",
  "verdict_color": "gray|green|yellow|red",
  "key_driver": "...",                    // short tagline
  "chart_priorities": [...],              // metric names to chart in §4

  "tier_1_kpis": [...],                   // see "KPI entries" below
  "tier_2_kpis": [...],
  "tier_3_kpis": [...],

  "competitive_watchlist": [...],
  "thesis_breakers_qualitative": [...],   // free-text breakers, narrative only

  "break_rules": [...],                   // hard universal tripwires (see below)
  "business_model_rules": [...],          // hard per-ticker breakers (see below)
  "break_rules_soft": [...],              // predicate-style YELLOW signals (see below)

  "schema_version": 2,
  "wacc": 0.095,
  "mos_bar": 0.2,
  "dcf_defaults": {"forecast_years": 5, "terminal_multiple": 22.0},
  "segments": [...],
  "operational_kpis": [...],
  "valuation_multiple_override": "P/E (NTM)"  // optional
}
```

## Hard break rules — `break_rules` and `business_model_rules`

Both arrays carry the same shape; the array placement decides the **tier**:

- `break_rules` → tier=`universal` (catastrophic tripwires shared across holdings)
- `business_model_rules` → tier=`business_model` (per-ticker, calibrated)

```jsonc
{
  "rule_id": "veev_total_rev_below_12",   // unique within file, snake_case
  "kpi_name": "Total revenue YoY growth", // must match kpi_definitions.name
  "comparator": "lt|le|gt|ge|eq",
  "threshold": 12,
  "unit": "percent|ratio|bps|actual|count|...",
  "consecutive_periods": 2,               // 1..12
  "narrative": "..."                      // human-readable explanation
}
```

Status semantics:

- **BREACH** — all of the last `consecutive_periods` observations match the rule.
- **WARN**   — some but not all of the last `consecutive_periods` match.
- **OK**     — none match.

Rollup: holding-level status is the worst rule status across both arrays.
`BREACH` wins. See `src/compute/thesis_evaluator.py:_rollup_status`.

## Soft rules — `break_rules_soft`

Predicate-style watch signals. Each rule either fires (YELLOW) or doesn't
(GREEN) — they never escalate the holding to BREACH on their own. When any
soft rule fires AND no hard rule breaches, the rollup goes to WARN.

```jsonc
{
  "name": "growth_decel_2q",              // unique-ish, used as the display label
  "predicate": {
    "type": "series_decel|series_below|series_above|ratio_breach|compound",
    "params": { ... }                     // shape depends on type, see below
  },
  "evidence_template": "Revenue YoY decel {first_bps} → {second_bps}bps"
}
```

`evidence_template` is Python `str.format`-style. The keys available depend on
the predicate type (listed below). When the template is omitted or fails to
render (missing key), the evaluator falls back to a generated description so
the brief always has *some* evidence text.

### Predicate types

#### `series_decel`

Fires when YoY growth deceleration ≥ `threshold_bps` for `periods`
consecutive quarters. Deceleration at quarter Q = `YoY(Q-1) - YoY(Q)` in bps
(positive = slowing).

```jsonc
{"type": "series_decel", "params": {
  "metric": "revenue",                    // financial_facts.line_item OR kpi name
  "source": "financial",                  // optional; "financial" (default) | "kpi"
  "periods": 2,                           // consecutive Q with decel ≥ threshold
  "threshold_bps": 200                    // 200bps = 2.00pp decel per Q
}}
```

Evidence template keys: `metric`, `periods`, `threshold_bps`, `first_bps`,
`second_bps`, `last_yoy_pct`, `prior_yoy_pct`, `decel_series_bps`,
`last_period`.

Needs ≥ `periods + 5` quarters of underlying data; otherwise GREEN with
"insufficient data" evidence.

#### `series_below` / `series_above`

Fires when the metric is `< threshold` (or `>`) for `periods` consecutive
quarters.

```jsonc
{"type": "series_below", "params": {
  "metric": "Non-GAAP operating margin",
  "source": "kpi",
  "threshold": 38,
  "periods": 2
}}
```

Evidence keys: `metric`, `threshold`, `periods`, `direction`, `last_value`,
`values`, `last_period`.

#### `ratio_breach`

Numerator / denominator vs threshold for N quarters. Threshold expressed as a
fraction (`0.15` for 15%) so the JSON matches what the analyst writes
("fcf/revenue < 15% for 2Q").

```jsonc
{"type": "ratio_breach", "params": {
  "numerator": "free_cash_flow",
  "denominator": "revenue",
  "threshold": 0.15,
  "direction": "below",                   // "below" | "above"
  "periods": 2                            // optional, default 1
}}
```

`numerator` and `denominator` are either bare strings (default to
`source: "financial"`) or `{name, source}` dicts:

```jsonc
"numerator": {"name": "AWS Operating Income", "source": "financial"}
```

Evidence keys: `numerator`, `denominator`, `threshold`, `threshold_pct`,
`direction`, `periods`, `last_ratio`, `last_ratio_pct`, `ratios_pct`,
`last_period`.

#### `compound`

Boolean over child predicates. Use `op: "and"` when a soft rule needs
multiple conditions both met; `op: "or"` when either condition suffices.

```jsonc
{"type": "compound", "params": {
  "op": "and",
  "predicates": [
    {"type": "series_decel", "params": {"metric": "revenue", "periods": 2, "threshold_bps": 200}},
    {"type": "series_below", "params": {"metric": "Operating margin (GAAP)", "source": "kpi", "threshold": 25, "periods": 2}}
  ]
}}
```

Evidence keys: `op`, `children` (list of `{type, fired, description}`).

### Error handling

Soft-rule evaluation is best-effort: any single rule that throws (missing
param, invalid source, schema typo) is logged + emitted as GREEN with the
error embedded in `evidence`. The thesis evaluator pipeline must not crash
on a malformed soft rule.

## Persistence

The evaluator writes hard-rule results to `thesis_evaluations.rule_evaluations_json`
and soft-rule results to `thesis_evaluations.soft_rule_results_json` (added
in alembic migration `0053_thesis_evaluations_soft_rules`). Older rows
without the soft column read as no soft rules evaluated, which the §2
renderer treats as a silent default rather than a missing-data warning.
