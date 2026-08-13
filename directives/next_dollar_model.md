# Next-dollar allocation model

**Decision (2026-06-11); final-answer role superseded 2026-07-23 (PRD §7.4/§17;
P0.4a/P0.4b, PRs #958/#961).** This directive's three-factor scoring model is now
the deterministic factor *library* that feeds one leg of the Incremental Dollar
Recommendation's frontier (`src/allocation/recommendation.py`). It is no longer
itself the platform's final next-dollar answer, and the raw distribution it used to
render is no longer on the primary Portfolio page.

The primary next-dollar answer is the governed `IncrementalDollarRecommendation`
artifact on Portfolio → Allocation: it composes this model's per-holding factor
scores together with candidate-fit, decision-ready eligibility, Concentration
Zones, the Risk Budget, and owner context, then a governed LLM selects one
preferred plan from the resulting deterministic frontier. See
`docs/design/personal_investment_partner_prd.md` §7.4 for the full contract.

Code: `src/allocation/` (`price_history.py`, `covariance.py`, `model.py`).
Primary consumer: `src/allocation/recommendation.py::build_next_dollar_model` call
site (one input among several to the frontier).
Legacy panel — **peek/test surface only, no longer on the primary render path**:
`src/pipeline/portfolio_panel.py::render_next_dollar_panel`. Portfolio → Health's
Synthesis page (`compose_synthesis_page`) shows only a one-line doorway to
Portfolio → Allocation instead of this panel's distribution (P0.4b).

## The question this factor library answers

Across the current portfolio holdings (`tracked_companies.list_type = 'portfolio'`),
which look most attractive by DCF upside, diversification value, and macro tilt?
Output is a probability-style distribution (softmax over blended factor scores) — a
*tilt ranking with magnitudes*, not a trade order, an optimizer weight, or (since
P0.4) the platform's incremental-dollar recommendation on its own. It is one input
to that recommendation's deterministic frontier.

## The three factors

Every factor is per-holding, visible in the panel's waterfall, and z-scored
cross-sectionally before blending. Nothing is a black-box composite.

### 1. `ret` — absolute expected return (blend weight 0.50)

DCF fair-value upside on price from the latest `dcf_runs` row per ticker:

```
upside = npv_per_share / live_price − 1
```

- Recomputed from the row's own two fields — **never** read from `over_under_pct`
  (the bank/holdco builders stored that column in a different convention). Same rule
  as `research_cockpit.latest_dcf_runs`, so the cockpit and this panel can't disagree.
- `live_price` is the price snapshotted at valuation time (typically ≤ a week old) —
  self-consistent with the fair value it's compared against.
- **Winsorized at ±100 %** before z-scoring (`RET_CLAMP`): a 4× mispricing signal is
  treated as no stronger than 2× (MELI-style DCF outliers would otherwise own the
  whole cross-section). The raw, unclamped upside stays visible in the waterfall.

### 2. `div` — diversification / marginal risk (blend weight 0.30)

The risk leg of marginal Sharpe: how much portfolio volatility the next dollar of the
name adds, at current weights.

```
div_raw_i = −(∂σ_p/∂w_i) · √252 = −(Σw)_i / σ_p · √252
```

- **Returns:** daily log returns from the FMP 10-year dividend-adjusted price cache
  (`data/historical/fmp/<T>_price_chart_10y_div_adj.json`), `adjClose` preferred over
  `close` — the same series the macro betas are fit on
  (`execution/compute_macro_sensitivities.py`).
- **Alignment:** tickers intersected onto a common trading calendar; a name needs
  ≥ 120 common days (`MIN_OVERLAP_OBS`) or it's dropped (greedily, shortest history
  first — the recently-IPO'd case) and named in a panel note. The matrix keeps the
  latest 252 common dates (`COV_LOOKBACK_OBS`, ~1 trading year).
- **Covariance:** Ledoit–Wolf (2004) shrinkage toward the scaled identity μI, with the
  standard data-driven intensity δ = min(b̄², d²)/d². With ~250 obs on ~11 names the
  sample estimate is decent; shrinkage guards short overlapping windows from turning
  sampling noise into spurious "diversification". δ is surfaced in the panel sub-line.
- **Weights:** tracker market values restricted to the holdings and renormalized
  (`weights_source = "tracker"`); equal weights when the tracker is down
  (`"equal"`, labelled in the panel). A tracked holding the tracker doesn't own gets
  weight 0 — it still gets scored; that's the point of a next-dollar model.
- Negated so that *higher = better diversifier*. The waterfall shows the underlying
  correlation-to-book and signed marginal vol.

The book here is the **modeled equity book** (the portfolio-list names), not the full
brokerage book — ETFs/cash in the tracker are outside the covariance. Documented
simplification; the panel says "modeled book".

### 3. `macro` — macro sentiment tilt (blend weight 0.20)

```
tilt_i = Σ_series β(i, s) × momentum(s)
momentum(s) = ln(latest / level ~90 calendar days earlier)
```

- Betas from `macro_sensitivities` (alembic 0045; OLS of weekly ticker log-returns on
  weekly series log-returns; the 252-day-lookback rows preferred when present).
- Momentum is the **cumulative version of the same weekly-log-return space the betas
  were fit in**, so β × momentum is an expected ticker-return contribution and the
  per-series products sum meaningfully (units: ~90-day log return).
- A series whose latest point is > 45 days old (`MACRO_STALE_DAYS`) is skipped — no
  tilting on dead data. No r²-weighting for now (kept literal/inspectable); the two
  largest |contributions| are named in the waterfall.
- Caveat (documented, accepted): macro series are mutually correlated (usd_brl and
  brent both carry global risk appetite), so the sum double-counts shared variance.
  It's a *tilt*, not a forecast.

**Populating the substrate:** `python execution/fetch_macro_series.py` uses
timeout-bounded Yahoo candidates and explicitly disables direct FMP macro calls
until they are admitted by the shared FMP circuit/budget/recovery service. It
then runs `python execution/compute_macro_sensitivities.py --portfolio` only when
all requested series are fresh or explicitly cached-degraded under the same
45-day staleness guard used by the allocation reader. Both tables ship empty
until those run; the factor hides itself meanwhile.
**Refresh cadence:** the `earnings-summary\fetch_macro_series` scheduled task
(`cron/run_fetch_macro_series.bat`, daily 05:35 local) preserves typed degraded or
partial acquisition exit codes. If a source returns no current rows, the panel
keeps the factor hidden (the staleness guard caps drift at 45 days).

## Standardize → blend → softmax

1. Each factor is z-scored across the holdings that carry it (population sd;
   degenerate sd ⇒ all zeros, factor present but inert).
2. Per holding, the blend weights renormalize over the factors it actually has
   (e.g. no DCF ⇒ div/macro reweight to 0.6/0.4). The waterfall shows the effective
   weight and `weight × z` contribution per factor; missing factors show as missing.
3. A factor with **fewer than two carriers model-wide is hidden entirely** and the
   blend renormalizes (e.g. macro empty ⇒ ret/div at 0.625/0.375) — hide-don't-stub,
   with the reason in the panel sub-line.
4. `score_i = Σ_k w_k(i) · z_k(i)`, then `allocation_i = softmax(score / τ)`,
   τ = `SOFTMAX_TEMPERATURE` = 1.0. With z-scale scores the best-to-worst spread is
   roughly e³ ≈ 20× — opinionated but never zeroing a name out.

Changing the blend: edit `BLEND_WEIGHTS` in `src/allocation/model.py` (one dict, the
panel sub-line reads from it). Temperature, lookbacks, clamps are sibling constants.

## Degradation ladder

| Missing                          | Behavior                                                        |
| -------------------------------- | --------------------------------------------------------------- |
| Tracker down                     | equal weights, labelled "equal-weight (tracker offline)"        |
| A holding's DCF / prices / betas | per-holding renormalization; factor chip shows "—" + reason     |
| A factor model-wide (< 2 names)  | factor hidden, blend renormalized, reason in sub-line           |
| Everything                       | `build_next_dollar_model` returns None ⇒ panel falls back to the memo excerpt only |

## Performance

Computed at panel render (`/api/panel/portfolio`): ~11 price JSONs (~4.5 MB) parsed +
a 252×11 covariance ≈ well under half a second, dwarfed by the tracker fetches the
page already awaits. No cache table; revisit only if the holdings list grows ~5×.

## Known limitations (accepted 2026-06-11)

- DCF `live_price` is at-valuation, not at-render — gaps drift a few days of price.
- Historical covariance ⇒ regime-blind; shrinkage helps conditioning, not stationarity.
- Macro tilt double-counts correlated series; no significance gating (r² visible in
  the DB if a gate is ever wanted).
- Softmax is a ranking device, not portfolio optimization — no constraints, no
  transaction costs, no tax awareness (the memo layer carries that judgment).
