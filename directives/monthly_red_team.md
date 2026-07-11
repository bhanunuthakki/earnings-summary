# Monthly Red Team — adversarial portfolio review program

Owner-authorized 2026-07-10 (adversarial portfolio review session). Goal: the platform must
*surface* silent risk and *force* an owner response on a ~monthly cadence. The owner's stated
objective is calibration ("a better investor each year"), not alpha — so the program measures
decision quality, never returns-vs-benchmark.

**Design principle.** The 2026-07 adversarial review found the platform fails silently in exactly
three ways: (1) coverage gaps render green (tail stress modeled 42% of the book and still showed a
healthy headline), (2) prose-only rules never fire (NU's compound net-adds/penetration tripwire
existed only in thesis text; one leg lit with zero panel signal), and (3) rules that would force
realizing a loss don't get written (upside-only FLKR ladder; NVO re-underwritten unscored; fake
`BEAR_SEED` bears sitting ABOVE live price for UBER/WIX/NVO/BKNG). Each phase below closes one
failure mode.

## Phase 1 — Deterministic guards (no LLM)

| Guard | Contract |
|---|---|
| Coverage gate | Any book-level risk read (tail stress, scenario reward) leads with `X% of book UNMODELED` when modeled weight < 90%; the headline number is visually subordinate to the coverage warning. Never render an aggregate as healthy when majority-unmodeled. |
| Bear-realism lint | For every `is_latest` top-level dcf_run with scenarios: `bear_fv >= live_price` → `NOT A BEAR` flag in the Risk panel, with the seed-vs-owner provenance shown. |
| Per-name bear deltas | `BEAR_SEED` generic deltas (−3pt growth / −1pt margin / −2x exit) are a *labeled fallback only*. Holdings JSON may carry `bear_deltas` (thesis-break-calibrated); scenario snapshots record `bear_provenance: seed | owner | thesis`. Seed-only bears on portfolio names are lint findings. |
| Trajectory WARN | `break_rules_soft` supports slope rules: last 2–4 prints project a hard-rule threshold crossing within ≤2 prints → WARN with the projected trip date. (MELI NIMAL −490bps/yr toward the 15% floor must not read plain OK.) |
| Prose-rule encoding | Compound/derived tripwires in thesis text MUST exist as machine rules (soft-rule `compound` predicate). A rule leg with no data renders UNRESOLVED — visible, never silently green. |
| KPI series sanity | Cumulative series (customers, etc.) get a monotonicity + unit-jump guard at persist time; violations halt-and-surface per the schema-drift rule, never guess-fixed. |
| Naked-position gate | Nightly: every held name needs (a) an encoded downside trigger (sizing-intent downside rung or non-empty break rules), (b) a persisted realistic bear (present + not lint-flagged), (c) thesis updated ≤90d. Violations are standing chips that block the monthly close. |

## Phase 2 — First-Saturday Red Team (the forcing function)

- **Cadence**: first Saturday, 10:00 America/Los_Angeles (clear of the 04:00 pipeline, the 03:00
  monthly prior refresh, and Sun 10:30 eval rungs). Window registered in
  `llm_quota_scheduling.md`. Idempotency key: `red_team_{YYYY_MM}`.
- **Per-name pass**: one adversarial LLM call per held name with a **rotating lens** —
  `shared_factor`, `fx_translation`, `competitive_encroachment`, `model_vs_market` (fair value
  disagrees with market >2x → attack the model), `behavioral_consistency`. Rotation state
  persisted; the same name must not get the same lens twice in a row.
- **Cross-book passes**: factor-block detection (e.g. the MELI+NU Brazil-credit pair), style drift
  vs stated strategy (value/size/momentum loadings vs the owner's GARP+index+momentum-sleeve
  statement), human-capital overlay (owner income is tech/AI-correlated; flag book bets that
  compound that factor).
- **Output**: one dense Red Team Brief (ui-kit composed): each item = falsifiable attack + one
  question + a proposed rule/scenario change. Items persist to `red_team_items`.
- **Forced response**: every item requires REFUTE (reasoning → ledger entry), ACCEPT
  (auto-creates the sizing intent / scenario edit / rule change), or DEFER (allowed once; second
  defer escalates to a persistent Home-band banner). Telegram `/redteam` mirrors `/review`. The
  month is not closed until all items are answered (say-do tracked).
- **Failure policy**: per-item degrade (transient CLI failure → defer item + tally + retry next
  run; hard stops loud) — the post-#814 `attach_conditions` pattern.

## Phase 3 — Learning loop

- **Scored-miss gate**: re-underwriting a thesis whose breach fired (the NVO path) requires a
  Brier-scorable calibration entry FIRST — belief, probability, outcome — before the new thesis is
  admitted. Honest-but-unscored re-underwrites are how thesis migration compounds.
- **Decision P&L**: every REFUTE/ACCEPT/DEFER and trim/hold from the monthly briefs is scored 2–4
  quarters later against the counterfactual; wired to the Coach P&L surface.
- **Fat-tail book MC**: multivariate-t Monte Carlo from the local price cache + event-correlation
  stresses (joint-LatAm at 0.9 event corr) as a Risk-panel leg; the wealthplan CMA gets a
  quarterly export of realized book vol + tail stats (the plan's 16%-vol assumption materially
  understates the ~22–27% book).
- **Annual letter**: each January, auto-draft a letter-to-self from the ledger + trades + Brier
  trajectory; the owner edits and signs. The yearly scorecard is three numbers: Brier trend,
  cut-discipline hit rate, and rule-execution fidelity in drawdowns.

## Component map (build order)

PR1 coverage gate + bear lint + per-name deltas → PR2 trajectory soft rules + prose-rule encoding
+ KPI sanity → PR3 naked-position gate → PR4 fat-tail MC + CMA export → PR5 red-team engine +
schedule → PR6 response loop + Telegram → PR7 scored-miss gate + Decision P&L + annual letter.
Data pass after PR1/PR2: owner-pending honest bears for MELI (~−60% NIMAL-floor) and NU (~−69%
carry-unwind) replace the missing scenario blocks.
