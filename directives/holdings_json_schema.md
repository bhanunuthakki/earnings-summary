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

  "competitive_watchlist": [...],         // pinned rival names/tickers (peer-comp +3 boost)
  "peer_exclude": [...],                   // optional; rivals to drop from the peer panel (S5)
  "peers_section_override": {...},         // optional; re-evaluable hide-unless-quality rule (S5)
  "thesis_breakers_qualitative": [...],   // free-text breakers, narrative only

  "break_rules": [...],                   // hard universal tripwires (see below)
  "business_model_rules": [...],          // hard per-ticker breakers (see below)
  "break_rules_soft": [...],              // predicate-style YELLOW signals (see below)
  "bear_deltas": {...},                   // optional; thesis-calibrated DCF bear scenario (see below)

  "schema_version": 2,
  "wacc": 0.095,
  "mos_bar": 0.2,
  // dcf_defaults.gsheet_id is written by `dcf_sheets.py export` (the DCF ⇄ Google
  // Sheets round-trip) and read back by `import`; see directives/dcf_gsheets_setup.md.
  "dcf_defaults": {"forecast_years": 5, "terminal_multiple": 22.0, "gsheet_id": "1AbC...optional"},
  "segments": [...],
  "operational_kpis": [...],
  "valuation_multiple_override": "P/E (NTM)",   // optional

  // Recently-IPO'd issuer fields (all optional; omit for mature names)
  "recently_ipod": true,                        // true within ~12mo of IPO
  "ipo_date": "2026-05-13",                     // first trading day
  "data_anchor": "s1",                          // "s1" | "10k" (default 10k)
  "s1_accession": "0001234567-26-000123",       // SEC accession, no slashes
  "s1_url": "https://www.sec.gov/Archives/...html",
  "s1_filing_date": "2026-05-11",
  "s1_cache_path": "data/sec_text/FRVO_s1_2026.txt"
}
```

## Recently-IPO'd issuer fields

For tickers added inside their first ~12 months of public-company status, the
10-K narrative source-of-truth doesn't exist yet — there's only the S-1 (and
its amendments) and possibly a single 10-Q. Setting `recently_ipod: true` plus
`data_anchor: "s1"` tells the consumers that anchor on 10-K narrative
(`src/compute/company_description.py`, the bear-case anchor, TAM/risk
extractors) to fall back to `data/sec_text/<TICKER>_s1_<FY>.txt`.

Authoring workflow for a fresh IPO:

1. Add the ticker to `tracked_companies` with `list_type='evaluation'`.
2. Author `micro_thesis/holdings/<TICKER>.json` with the IPO fields set.
3. Run `python execution/fetch_sec_s1.py --ticker <TICKER>` to populate the
   `data/sec_text/<TICKER>_s1_<FY>.txt` cache.
4. Onboard normally: `python execution/onboard_ticker.py --ticker <TICKER>`.
   FMP fundamental coverage will be sparse but the script tolerates it; the
   narrative layer reads from the S-1 cache instead via
   `filing_text_fetcher.load_canonical_narrative`.

The flag is informational — it doesn't change rule evaluation or DCF math.
It only switches the narrative source. Once a 10-K is filed, flip
`data_anchor` to `"10k"` (or drop both fields).

## Peer-curation fields (S5)

The comparable-company panel is owner-steerable through the `curate_peers`
comment intent (see `directives/report_comments_and_chat.md`). Three fields
feed `src/report/sections/p3_data.py::load_peer_comp`:

- `competitive_watchlist` — pinned rival names (existing). Each name gives a
  matching pool peer a +3 "named rival" score. A pin written as a bare
  **ticker** ("HOOD") that the upstream FMP pool omits is INJECTED into the
  pool so the explicit pin still renders.
- `peer_exclude` — optional list of tickers and/or names to DROP from the
  shown set, however well they'd otherwise score. The one curation the
  watchlist can't express.
- `peers_section_override` — optional, re-evaluable "remove this section
  unless better peers" condition. Shape:
  ```jsonc
  {
    "action": "hide",                 // only "hide" today
    "condition": "peers_quality",
    "require_named": true,            // count only watchlist-vouched peers
    "require_metrics": true,          // …that carry ≥1 computed TTM multiple
    "min_quality_peers": 2,           // hide while fewer than this qualify
    "rationale": "<owner's words>",
    "source_comment_id": "cmt_…",
    "created_at": "2026-06-13T…"
  }
  ```
  `load_peer_comp` re-checks it every build (`evaluate_peers_override`): the
  panel hides while too few credible comps qualify and returns on its own once
  enough are pinned — the condition is acted on, not just recorded.

## `bear_deltas` — thesis-calibrated DCF bear scenario (Monthly Red Team Phase 1)

The DCF's bear scenario defaults to `BEAR_SEED` (`src/dcf/redesign.py`) — a
generic -3pt near-term growth / -1pt margin / -2x exit-multiple offset applied
to EVERY name, regardless of what would actually break its thesis. The
2026-07 adversarial review found this produces bear cases that sit AT or
ABOVE the live price for several names (a "bear" with no downside) — the
`bear_lint` module (`src/bear_lint.py`) flags these as `not_a_bear` /
`shallow`.

`bear_deltas` lets the analyst name a thesis-specific bear instead of the
generic seed. It is a fallback default only — an owner's workbook Dashboard
edit (the Bear column's yellow cells) still wins unconditionally once one is
on file; this only changes what a FRESH scenario build seeds the Bear column
with before any owner edit exists.

```jsonc
{
  "bear_deltas": {
    "growth_delta_pp": -8.0,       // shifts BOTH near- and terminal-segment growth (pp)
    "margin_delta_pp": -3.0,       // shifts BOTH near- and terminal operating margin (pp)
    "exit_multiple_delta": -6.0,   // shifts the exit multiple (turns)
    "terminal_g_delta_pp": -0.5,   // optional; shifts terminal growth g (pp)
    "note": "NIMAL floor breach — the thesis-break bear, not a mild dip"
  }
}
```

All four numeric levers are optional; an unset lever falls back to
`BEAR_SEED`'s value for that lever, so a thesis only needs to name the
specific break rather than re-derive the whole six-lever range. `note` is
free text, not consumed by any reader — it documents WHY for the next reader.

Provenance travels with every persisted bear scenario
(`dcf_runs.assumption_snapshot_json.scenarios.bear.provenance`, read via
`dcf.scenario_reward.parse_scenario_bear_provenance`): `"seed"` (untouched
`BEAR_SEED`), `"thesis"` (this block), or `"owner"` (a hand-edited workbook
cell) — `bear_lint` surfaces a `seed`-provenance bear on a portfolio name even
when it happens to clear the realism floor, since a generic offset producing
a plausible number by coincidence still isn't a thesis read.

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

Predicate-style watch signals. Each rule evaluates to one of three statuses:

- **GREEN** — evaluated, didn't fire.
- **YELLOW** — evaluated, fired.
- **UNRESOLVED** — couldn't be evaluated (insufficient data, a malformed
  predicate, or a data-quality guard tripped on a `derived: "delta"` metric).
  Never collapsed into GREEN — `directives/monthly_red_team.md` Phase 1's
  "Prose-rule encoding" contract requires a rule leg with no data to stay
  visible, not silently read as "checked, all clear". The §2 renderer shows
  UNRESOLVED in the same amber tone as YELLOW, labeled distinctly.

Soft rules never escalate the holding to BREACH on their own. When any soft
rule is YELLOW **or** UNRESOLVED, and no hard rule breaches, the rollup goes
to WARN — an unresolved soft rule is itself a signal ("this needs attention,
the data can't confirm or deny it"), not silence.

```jsonc
{
  "name": "growth_decel_2q",              // unique-ish, used as the display label
  "predicate": {
    "type": "series_decel|series_below|series_above|ratio_breach|compound|trajectory",
    "params": { ... }                     // shape depends on type, see below
  },
  "evidence_template": "Revenue YoY decel {first_bps} → {second_bps}bps"
}
```

`evidence_template` is Python `str.format`-style. The keys available depend on
the predicate type (listed below). When the template is omitted or fails to
render (missing key), the evaluator falls back to a generated description so
the brief always has *some* evidence text.

### `derived: "delta"` — consecutive-print differences

Any metric spec (`metric` on `series_decel`/`series_below`/`series_above`,
`kpi_name` on `trajectory`, `numerator`/`denominator` on `ratio_breach`)
accepts an optional `"derived": "delta"` key alongside `"source"`. It swaps
the raw level series for consecutive-print DIFFERENCES — e.g. NU's "net
adds" is `delta(Total customers)`, not the level itself. Omitted or
`"level"` (the default) uses the raw series unchanged.

A cumulative series feeding a `delta` can't be trusted blindly: a decrease
(the series should be non-decreasing) or a >1000× jump between adjacent
prints (a raw-count row landing inside a millions-scale series — the exact
"Total customers" def-641 corruption the 2026-07 red-team audit found) means
the delta would be garbage. Either condition trips a data-quality guard and
the rule reports **UNRESOLVED** with the reason named, rather than compute a
nonsense delta. See `execution/fix_kpi_series.py` for the persist-time
counterpart that catches this at the source.

### Predicate types

#### `series_decel`

Fires when YoY growth deceleration ≥ `threshold_bps` for `periods`
consecutive quarters. Deceleration at quarter Q = `YoY(Q-1) - YoY(Q)` in bps
(positive = slowing).

```jsonc
{"type": "series_decel", "params": {
  "metric": "revenue",                    // financial_facts.line_item OR kpi name
  "source": "financial",                  // optional; "financial" (default) | "kpi"
  "derived": "level",                     // optional; "level" (default) | "delta"
  "periods": 2,                           // consecutive Q with decel ≥ threshold
  "threshold_bps": 200                    // 200bps = 2.00pp decel per Q
}}
```

Evidence template keys: `metric`, `periods`, `threshold_bps`, `first_bps`,
`second_bps`, `last_yoy_pct`, `prior_yoy_pct`, `decel_series_bps`,
`last_period`.

Needs ≥ `periods + 5` quarters of underlying data; otherwise UNRESOLVED with
"insufficient data" evidence.

#### `series_below` / `series_above`

Fires when the metric is `< threshold` (or `>`) for `periods` consecutive
quarters.

```jsonc
{"type": "series_below", "params": {
  "metric": "Non-GAAP operating margin",
  "source": "kpi",
  "derived": "level",                     // optional; "level" (default) | "delta"
  "threshold": 38,
  "periods": 2
}}
```

Evidence keys: `metric`, `threshold`, `periods`, `direction`, `last_value`,
`values`, `last_period`.

Fewer than `periods` observations, or a `derived: "delta"` data-quality guard
trip → UNRESOLVED, never a silent GREEN.

#### `trajectory`

Linear-fits the last `lookback_prints` observations and projects whether the
trend crosses `threshold` within `horizon_prints` future prints. This is the
Phase 1 "Trajectory WARN" contract: a rule can be OK today by the hard-rule
threshold and still be visibly gliding toward it (MELI's NIMAL: 22.7% →
17.8% YoY, −490bps/yr, toward the 15%-floor `meli_nimal_below_15` hard rule —
plain OK today, but the trajectory rule surfaces the approach).

```jsonc
{"type": "trajectory", "params": {
  "kpi_name": "NIMAL (net interest margin after losses)",
  "source": "kpi",                        // optional; "kpi" (default for trajectory) | "financial"
  "derived": "level",                     // optional; "level" (default) | "delta"
  "comparator": "lt",                     // lt | le | gt | ge — direction that counts as "crossed"
  "threshold": 15,
  "lookback_prints": 4,                   // optional, default 4, minimum 3
  "horizon_prints": 2                     // optional, default 2, minimum 1
}}
```

Evidence keys: `kpi_name`, `threshold`, `comparator`, `lookback_prints`,
`horizon_prints`, `slope_per_period`, `last_value`, `last_period`,
`trip_period`, `trip_h`, `trip_value`, `already_violating`.

Fewer than `lookback_prints` observations, or a `derived: "delta"`
data-quality guard trip → UNRESOLVED ("thin" data), never a silent
non-fire. A flat or receding trend (never projected to cross within
`horizon_prints`) → GREEN. A trend projected to cross → YELLOW, with
`trip_period` naming the quarter (e.g. `"Q1'27"`).

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
`source: "financial"`, `derived: "level"`) or `{name, source, derived}`
dicts:

```jsonc
"numerator": {"name": "AWS Operating Income", "source": "financial", "derived": "level"}
```

Evidence keys: `numerator`, `denominator`, `threshold`, `threshold_pct`,
`direction`, `periods`, `last_ratio`, `last_ratio_pct`, `ratios_pct`,
`last_period`.

#### `compound`

Three-valued (Kleene) boolean over child predicates. Use `op: "and"` when a
soft rule needs multiple conditions both met; `op: "or"` when either
condition suffices. Each child predicate evaluates to fired / not-fired /
**unresolved** (see the tri-state status above), and the compound combines
them without ever laundering an unresolved child into a plain fired/clear
verdict:

- `AND`: any definite `False` child wins (→ not fired) even with an
  unresolved sibling; else any unresolved child → the compound is
  **UNRESOLVED**; else all `True` → fired.
- `OR`: any definite `True` child wins (→ fired) even with an unresolved
  sibling; else any unresolved child → the compound is **UNRESOLVED**; else
  all `False` → not fired.

This is how NU's "net adds <5M/Q AND Brazil flagship penetration declining
QoQ" tripwire — previously thesis prose only, one leg already lit, zero
panel signal — now renders: the penetration leg has no data (def 639, zero
rows) → UNRESOLVED, so even with the net-adds leg firing the compound reads
**UNRESOLVED** (visible amber), never a false GREEN.

```jsonc
{"type": "compound", "params": {
  "op": "and",
  "predicates": [
    {"type": "series_decel", "params": {"metric": "revenue", "periods": 2, "threshold_bps": 200}},
    {"type": "series_below", "params": {"metric": "Operating margin (GAAP)", "source": "kpi", "threshold": 25, "periods": 2}}
  ]
}}
```

Evidence keys: `op`, `children` (list of `{type, fired, description}` —
`fired` is `true` / `false` / `null` for unresolved).

### Error handling

Soft-rule evaluation is best-effort: any single rule that throws (missing
param, invalid source, schema typo) is logged + emitted as **UNRESOLVED**
(never GREEN) with the error embedded in `evidence`. The thesis evaluator
pipeline must not crash on a malformed soft rule — other rules in the same
call still evaluate.

## Persistence

The evaluator writes hard-rule results to `thesis_evaluations.rule_evaluations_json`
and soft-rule results to `thesis_evaluations.soft_rule_results_json` (added
in alembic migration `0053_thesis_evaluations_soft_rules`). Older rows
without the soft column read as no soft rules evaluated, which the §2
renderer treats as a silent default rather than a missing-data warning.
