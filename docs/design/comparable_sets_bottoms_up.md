# Bottoms-Up Comparable-Set + Sector/Industry Tracking — Design

Status: **design only, not yet implemented.** This doc is the complete, decision-closed
spec an implementation agent should build from without re-deriving anything. Where a
judgment call was made, the rationale is inline — do not re-litigate it; if a real build
constraint forces a deviation, update this doc in the same PR (Directive Maintenance
convention: refine, don't append).

## 0. Problem this replaces

FMP Starter caps `sector-pe-snapshot` / `industry-pe-snapshot` / `historical-sector-pe` /
`historical-industry-pe` to a ~1-year window (full history is Ultimate-only,
`execution/save_fmp_data.py::run_sector_industry`, `data/historical/sector_industry/`).
**Surprise finding, verified by grep: nothing in `src/` or `execution/` reads those
snapshot files today.** `run_sector_industry` writes them and nothing downstream
consumes them — there is no existing render surface or drop-in output shape to preserve
compatibility with. That removes a constraint the mission anticipated; the new bottoms-up
tables are free to define their own contract. The FMP snapshot still earns its keep as
the QA drift-check's independent reference (§7).

## 1. Data inventory (verified in this repo, not assumed)

All of the following is fetched **today**, unconditionally, for every ticker whose
`tracked_companies.list_type` is `portfolio`, `watchlist`, `evaluation`, `index_member`,
or `etf` — confirmed in `execution/save_fmp_data.py::per_ticker_jobs` (the ~67-endpoint
job list; `index_member`/`etf` skip only the 10 `financial-reports-form-10-k-json` calls,
~1,700 calls saved across ~170 index members, per the docstring at line 612-618). That's
the ~170 `index_member` + ~100 tracked (`portfolio`+`watchlist`+`evaluation`) + ETF names
the mission refers to — call this **the universe pool**.

Per-ticker cache files under `data/historical/fmp/{TICKER}_*.json` (verified field names
via `src/discovery/screens.py`, `src/compute/valuation_basis.py`,
`src/report/sections/p3_data.py`, `src/allocation/price_history.py`):

| File suffix | Cadence/history | Key fields used here |
|---|---|---|
| `_profile.json` | refreshed each cycle; single record | `companyName`, `sector`, `industry`, `marketCap`, `price`, `isActivelyTrading` (absent ⇒ assumed `True`), `exchangeShortName`/`exchange`, `ipoDate`, `isEtf`/`isFund` (per `src/pipeline/fmp_doc_index.py:206-214`) |
| `_income_statement_quarterly.json` | quarterly, `limit=100` (~25y if FMP has it) | `date`, `revenue`, `netIncome`, `operatingIncome`, `grossProfit`, `ebitda` (confirmed field, `src/compute/income_statement.py:35`), `eps` |
| `_key_metrics_quarterly.json` | quarterly, `limit=100` | `date`, `marketCap`, `enterpriseValue`, `returnOnInvestedCapital`, `freeCashFlowYield`, `netDebtToEBITDA` (all **per-quarter fractions**, TTM-ize by summing last 4 — see `screens.py::_sum_last4`) |
| `_financial_ratios_quarterly.json` | quarterly | `priceToBookRatio`, `peRatio`, `priceToFreeCashFlowsRatio`, `evToSales`, `evToEBITDA` (per-quarter snapshot ratios) |
| `_key_metrics_ttm.json` / `_financial_ratios_ttm.json` | vendor-computed TTM snapshot, refreshed each cycle | `revenueTTM`, `netProfitMarginTTM` (legacy `netIncomePerRevenueTTM`), `returnOnInvestedCapitalTTM` (legacy `roicTTM`) — dual-key fallback pattern lives in `src/report/sections/p3_data.py:887-933`; **this design computes PE/EV-EBITDA from raw components instead of trusting these fields — see §5 rationale** |
| `_balance_sheet_quarterly.json` | quarterly | `totalStockholdersEquity` (fallback `totalEquity`), `goodwill`, `intangibleAssets` — used for P/B and P/TBV exactly as `valuation_basis.py::_manual_book_multiple` already does |
| `_historical_market_cap.json` | **10-year daily-ish series**, `from=TEN_YEARS_AGO` | `date`, `marketCap` — the honest backfill spine (§9) |
| `_price_chart_10y_div_adj.json` | **10-year daily**, dividend-adjusted | `date`, `close`/`adjClose` — read via `src/allocation/price_history.py::load_daily_closes` |
| `_peers.json` | FMP `stock_peers` screen | not used here directly — superseded by `data/peer_selection/{T}.json` for LLM-ratified peers |

`data/peer_selection/{TICKER}.json` (`src/compute/peer_selection.py`) already exists and
is directly reusable (§3.3): it holds `suggestions: [{ticker, name, why}]` plus
`fetched_peers` / `fetched_complete` — the subset whose `profile` + `key-metrics-ttm` +
`ratios-ttm` + `income-statement` all actually resolved.

**Load-bearing constraint, confirmed in `directives/peer_selection_llm.md:140-150`:**
for a ticker that is *not already in the universe pool* (i.e. a brand-new symbol FMP has
never served this account before), `/stable/key-metrics-ttm`, `/stable/ratios-ttm`, and
`/stable/income-statement` return **HTTP 402** ("not available under your current
subscription") — only `/stable/profile` resolves. This is why `peer_selection.py`'s
40-call fetch budget so often lands "market cap only" peers (documented example: TCOM
in the ABNB report). **Therefore: comparable-set candidates must be drawn primarily from
the universe pool.** An LLM-suggested peer outside the pool contributes a name + `why` +
(if lucky) a market cap for context, never a PE/EV-EBITDA/growth data point — it must be
tagged `context_only` and excluded from every aggregate/median computation in §5, not
silently coerced into a data point that doesn't exist.

## 2. Universe & pool boundary

```
pool(ticker) := tracked_companies WHERE list_type IN
    ('portfolio','watchlist','evaluation','index_member') AND user_id = :user_id
```

`etf` list_type tickers are excluded from the pool as **comparable-set members** (an ETF
is not a comparable operating company) but are the vehicle for the benchmark proxy (§4).
A ticker with `instrument_type='etf'` (or profile `isEtf`/`isFund` truthy per
`fmp_doc_index.py`) is filtered out even if some list-type migration ever mislabels it —
belt and suspenders, since `list_type='etf'` is a known legacy overlap with
`instrument_type` (`alembic/versions/0044_etf_profile_and_holdings.py:27-34`).

This pool is ~270 names today. It is the entire addressable universe for bottoms-up
math — there is no path to silently grow it by pulling financials for arbitrary tickers
(that's the 402 wall above). Growing the pool is a deliberate action: add a ticker to
`tracked_companies` with `list_type='index_member'` (or onboard it properly), let
`save_fmp_data.py` backfill its financials, *then* it's eligible.

## 3. Deterministic comparable-set identification

New module: **`src/compute/comparable_sets.py`** (mirrors `peer_selection.py`'s shape:
pure resolver functions + a cache/freeze step). New CLI: **`execution/build_comparable_sets.py`**.

### 3.1 Rule ladder (run in this order, each step only firing if the prior under-fills)

**Step A — industry + size-band seed.**
```
candidates = { m in pool(subject) :
    m.industry == subject.industry            # exact FMP profile.industry string match
    AND subject.marketCap/4 <= m.marketCap <= subject.marketCap*4
    AND m.exchangeShortName in {NYSE, NASDAQ, AMEX, NYSEARCA}   # US-listed guard
    AND m.isActivelyTrading is not False
    AND m.instrument_type != 'etf'
    AND m != subject
}
```
`industry` is FMP's granular field (e.g. "Software - Application", "Banks - Regional"),
not the coarser `sector` — start narrow, widen deliberately.

**Step B — sector widen, only if `len(candidates) < 8` after Step A.**
```
candidates |= { m in pool(subject) :
    m.sector == subject.sector
    AND subject.marketCap/10 <= m.marketCap <= subject.marketCap*10
    AND (same US-listed / active / non-ETF guards as Step A)
}
```
8 is the floor because the median calculations in §5 are not meaningful below single
digits; document this constant as `MIN_COMPARABLE_SET_SIZE = 8` in the module so it's a
named, greppable knob, not a magic number.

**Step C — union LLM-ratified business-model peers.**
Read `data/peer_selection/{TICKER}.json` (existing cache, no new LLM call). For each
suggested peer:
- if `ticker in fetched_complete` **and** `ticker in pool` → full member, contributes to
  every metric in §5.
- if `ticker in fetched_complete` **and** `ticker not in pool` → this can't actually
  happen today (fetch only ever gets `profile`-only for out-of-pool tickers per §1's 402
  wall) but code defensively for the day the plan upgrades.
- if `ticker in fetched_peers` only (market-cap-only, per the 402 finding) → included in
  the **roster** (visible, with its `why`) but flagged `context_only=True`; excluded from
  every §5 median/aggregate.
- **Ratification gate**: an LLM-suggested peer only survives into the frozen set if the
  suggestion was produced by the *current* `peer_selection` cache (i.e. not a stale
  suggestion from before an owner correction) **and** is not present in a
  `comparable_set_overrides.py` `exclude` list (§3.2) — the owner's veto always wins,
  consistent with the S5 `peer_exclude` precedent in `directives/peer_selection_llm.md`.
  There is no separate new LLM call here — this reuses `peer_selection`'s existing
  Sonnet-4.6-pinned, evaluated (`evals/golden/peer_selection.json`, 0.972 avg recall)
  generator. Do not add a second, redundant LLM call for comp-set membership.

**Step D — per-ticker pinned override, always wins.**
New tracked module **`src/compute/comparable_set_overrides.py`**, same pattern as the
existing `src/ir_pipeline/ir_url_overrides.py` (a plain version-controlled Python dict,
edited by hand, "adding a name = adding one dict entry"):

```python
# ticker -> override directive. Any key present here short-circuits or amends the
# rule-ladder output for that ticker. Never auto-written by code or an LLM — owner-edited
# only, per the LLM-governance rule (proposals are ratified into this file by a human,
# same as sector_benchmark_map.py in §4).
COMPARABLE_SET_OVERRIDES: dict[str, "ComparableSetOverride"] = {
    "BN": ComparableSetOverride(
        force_include=["BAM", "BX", "KKR", "APO"],
        force_exclude=[],
        method_flags={"whole_co_pe_not_meaningful": True, "whole_co_ev_ebitda_not_meaningful": True},
        note="Holdco — consolidated multiples are noise from minority-interest and "
             "look-through accounting; whole-co PE/EV-EBITDA excluded from aggregates, "
             "see directives/holdco_sotp_model.md for the SOTP alternative.",
    ),
    # NU, RBRK, ... added as the owner reviews Step A/B/C output per ticker.
}
```
`force_include`/`force_exclude` splice directly into the frozen membership list
(§3.4) before it's written; `method_flags` propagate into `comparable_sets.method_flags`
(§6) so §5's aggregate logic can skip a metric class for that subject without a second
lookup table. This is the ONE place holdco/idiosyncratic judgment calls live — no
generic "detect a holdco" heuristic is attempted (too fragile; a wrong auto-classification
silently poisons an aggregate, which is worse than requiring one manual entry per known
holdco).

### 3.2 Special-case classification (deterministic, before the ladder runs)

A subject or candidate's **metric-class** is derived from its `industry` string via a
keyword-blob match, mirroring the existing precedent `valuation_basis.py::_sector_fallback`
(same technique, new keyword set — don't invent a new pattern):

```python
def _metric_class(sector: str | None, industry: str | None) -> str:
    blob = f"{sector or ''} {industry or ''}".lower()
    if any(t in blob for t in ("bank", "diversified financial", "insurance",
                               "asset management", "credit services", "capital markets")):
        return "financial"   # primary metrics: P/B, P/TBV — see §5.4
    if any(t in blob for t in ("reit", "real estate")):
        return "reit"        # primary metric: P/FFO-proxy = P / (netIncome + D&A); flag EV/EBITDA n/m
    return "operating"       # primary metrics: P/E, EV/EBITDA — see §5
```
This governs which metrics are **primary** for a member/set (drives which columns render
"headline" vs "secondary") — it does NOT exclude a member from the set; a bank sitting in
an otherwise-operating comp set still contributes its P/B, just not its PE to the
industry's headline PE line (and vice versa).

**Recently-IPO'd members**: no special detection needed beyond what §5 already does
naturally — `_sum_last4`-style "all 4 trailing quarters present or the metric is `None`"
(exact pattern from `screens.py::_sum_last4` / `_ttm_margin`) already excludes a ticker
with <4 quarters on file from every TTM aggregate. Portfolio/watchlist/evaluation names
additionally carry `recently_ipod`/`ipo_date` in `micro_thesis/holdings/{T}.json`
(`directives/holdings_json_schema.md`) — read it opportunistically to annotate the roster
("recently IPO'd, thin history" tag) but do not gate on it; the natural 4-quarter gate is
the robust, universal mechanism (works for `index_member` names with no holdings JSON too).

### 3.3 Versioning & freezing

New tables (full DDL in §6): `comparable_sets` (one row per resolved set) and
`comparable_set_members` (membership, with `valid_from`/`valid_to`).

- `method_version` is a plain incrementing integer, bumped whenever the rule ladder,
  size-band constants, or metric-class keyword list changes. **Never mutate rows under an
  old `method_version`** — a resolve under a new version always inserts a new
  `comparable_sets` row; the old one's members get `valid_to = today` if superseded, but
  the row and its historical `comp_set_metrics_daily` rows (§5) stay queryable forever.
  This is what makes historical tracking reproducible: a chart of "NU's comp-set PE over
  the last 2 years" is unambiguous even if membership changed in between, because every
  daily metric row carries the `comparable_set_id` (hence `method_version`) it was
  computed under.
- `comparable_set_id` = `f"{ticker}_{method_version}"` (deterministic, human-legible —
  no need for a surrogate hash; collisions are impossible by construction).
- Re-running `build_comparable_sets.py` for a ticker whose current-`method_version` set
  has `valid_to IS NULL` (still open) is an **idempotent no-op** unless membership would
  actually differ (compare the resolved candidate set against the frozen one) or
  `--refresh` is passed — mirrors `peer_selection.py`'s `inputs_sha256` cache-hit pattern,
  but the "input" here is the resolved candidate list itself, not an LLM prompt hash.

## 4. Sector/industry benchmark proxy

New tracked module **`src/compute/sector_benchmark_map.py`**, same pattern as
`ir_url_overrides.py` — a plain dict, owner-ratified, no DB table needed for something
this small and this rarely edited:

```python
# FMP profile.industry (exact string) -> benchmark proxy. `etf` is the tightest-fit
# published index ETF; `sector_etf` is the coarser GICS-sector fallback for an industry
# with no dedicated ETF. Both must already be onboarded as list_type='etf' so the
# cacher (save_fmp_data.py --sector-industry / normal per-ticker run) has price history
# for it — adding a row here without onboarding the ETF ticker gets you a proxy with no
# price data; onboard first.
SECTOR_BENCHMARK_MAP: dict[str, BenchmarkProxy] = {
    "Semiconductors": BenchmarkProxy(etf="SMH", sector_etf="XLK", note="also SOX index, no direct ETF wrapper on FMP"),
    "Software - Application": BenchmarkProxy(etf="IGV", sector_etf="XLK"),
    "Software - Infrastructure": BenchmarkProxy(etf="IGV", sector_etf="XLK"),
    "Banks - Regional": BenchmarkProxy(etf="KRE", sector_etf="XLF"),
    "Banks - Diversified": BenchmarkProxy(etf="KBE", sector_etf="XLF"),
    "Insurance - Diversified": BenchmarkProxy(etf="KIE", sector_etf="XLF"),
    "Biotechnology": BenchmarkProxy(etf="XBI", sector_etf="XLV"),
    "Internet Retail": BenchmarkProxy(etf="IBUY", sector_etf="XLY"),
    "Credit Services": BenchmarkProxy(etf=None, sector_etf="XLF", note="no clean fintech-lending ETF; sector fallback only"),
    # ... owner extends per-industry as tickers are onboarded; unmapped industry falls
    # back to sector_etf, then to no benchmark line at all (never fabricated).
}
```

**Ratification flow for an unmapped industry**: reuse the exact `peer_selection`
governance shape (LLM proposes, human ratifies, then frozen) — a small, separate LLM
purpose `sector_benchmark_proposal` (new `LLM_MODELS` entry, Haiku-class is plenty; this
is "which ETF tracks X" factual lookup, not judgment) proposes `{etf, sector_etf, why}`
for an industry with no entry, written to a review queue (mirrors the peer-selection
suggestion cache shape, e.g. `data/sector_benchmark_proposals/{industry_key}.json`) —
**never auto-applied**. The owner reviews and pastes the ratified line into
`SECTOR_BENCHMARK_MAP` by hand. This is a small, rare (~50-150 total industries),
one-time-per-industry task — do not over-engineer it into a standing pipeline stage; a
manual `execution/propose_sector_benchmarks.py --industry "X"` one-off CLI is sufficient.

### 4.1 What the benchmark proxy is actually used for (the key resolution)

Two different jobs, two different data sources — **do not conflate them**:

1. **Performance/relative-strength benchmark** (the ETF's own price series): use
   `_price_chart_10y_div_adj.json` for the mapped ETF ticker directly (already fetched
   today for any `list_type='etf'` ticker per §1's table — confirmed, no gap). High
   confidence, always available once the ETF is onboarded. This answers "how did NU's
   industry do vs SPY the last 90 days" — a pure price ratio, no multiples involved.
2. **Multiples/fundamentals benchmark** (industry PE / EV-EBITDA / growth): **do NOT**
   try to derive this from the ETF's `etf_holdings` table. `directives/etf_data.md:16`
   is explicit that FMP `/stable/etf/holdings` is "Plan-gated; 402s tolerated" — coverage
   is unreliable and the table (`alembic/versions/0044_etf_profile_and_holdings.py`) may
   simply be empty for a given ETF. Instead, the industry-level multiples benchmark is
   computed the *same bottoms-up way* as a per-ticker comparable set, just scoped to
   `industry` (or `sector`) across the whole pool rather than a market-cap band around
   one subject — see §5's `scope_type='industry'` rows. The ETF is never in that
   computation's inputs; it only supplies the performance line.

This also resolves the "ETF price coverage the cacher already has" question directly:
price — yes, solid, reuse as-is. Holdings for multiples — no, don't depend on it.

## 5. Bottoms-up aggregate construction

### 5.1 Why not simple-average the PEs

A simple mean of member PE ratios is dominated by whichever member's earnings happen to
be near zero (PE is unbounded as earnings → 0⁺), and it implicitly equal-weights a
$2B name the same as a $200B name — neither is what "the industry's PE" should mean. The
economically correct portfolio-level statistic is the **cap-weighted harmonic
aggregate**: if you held every member in proportion to its market cap, your blended P/E
is `(Σ market_cap) / (Σ TTM earnings)` — this is the same construction S&P uses for
index-level "bottom-up" earnings multiples. Compute **both**:
- `aggregate` = `Σ market_cap_i / Σ ttm_earnings_i` (cap-weighted, earnings-weighted
  denominator) — the "if you owned the whole set" number.
- `median` = the median of individual members' own PE — the "typical name" number,
  robust to a single mega-cap dominating.
Report both, always, side by side; never collapse to one number. Same construction for
EV/EBITDA (`Σ enterprise_value_i / Σ ttm_ebitda_i`).

### 5.2 Negative-earnings / negative-EBITDA handling (exact rule, per metric)

| Metric | Median | Aggregate |
|---|---|---|
| PE | **exclude** members with `ttm_net_income <= 0` (PE undefined/meaningless negative) | **include** — a lossmaking member's negative earnings genuinely reduce `Σ ttm_earnings`; that's the honest blended yield of owning the whole set. If `Σ ttm_earnings <= 0` for the whole set, the aggregate PE itself is undefined — write `NULL` and set a `method_flags: {aggregate_pe_undefined_negative_denominator: true}` note on that row rather than emitting a nonsense negative or infinite number. |
| EV/EBITDA | **exclude** members with `ttm_ebitda <= 0` | **include** in `Σ ttm_ebitda`; same undefined-denominator guard as above |
| Rev YoY | median of members with both current and year-ago revenue present (no sign issue — growth can be legitimately negative) | not computed as a Σ/Σ aggregate (revenue isn't a per-share ratio problem the same way); if a cap-weighted growth figure is wanted later, `Σ(market_cap_i × growth_i) / Σ market_cap_i` is the correct construction — **deferred, not built in phase 1** (median is what the mission asks for) |
| FCF yield | median of members with all 4 trailing quarters' `freeCashFlowYield` present (reuses `screens.py::_sum_last4` exactly) | not aggregated (yield is already per-market-cap; a cap-weighted mean of yields is defensible but adds a second construction to review — deferred to phase 2 alongside growth) |

### 5.3 Computing PE / EV-EBITDA from raw components, not vendor TTM fields

**Decision: derive `ttm_net_income` and `ttm_ebitda` by summing the last 4
`income_statement_quarterly` rows' `netIncome` / `ebitda` fields (all 4 present or the
member contributes `None`, exact `_sum_last4` pattern) — do not read a pre-computed
`peRatioTTM`/`evToEBITDATTM`-style field from `key_metrics_ttm`/`financial_ratios_ttm`.**
Two reasons: (1) a vendor-precomputed *ratio* can't be un-computed to recover the raw
earnings figure the aggregate needs to sum — you'd need the raw number anyway; (2)
different members' vendor TTM cutoffs can silently drift a few days apart, whereas
summing our own 4 quarterly rows keeps every member's "TTM" defined the same way (last 4
*reported* quarters, whatever their fiscal calendar). `market_cap_i` and
`enterprise_value_i` come from the **daily** `historical_market_cap.json` series
(§9) rather than the quarterly `key_metrics_quarterly` snapshot, so the PE/EV-EBITDA
figure moves with the market every day even though earnings only update on the
member's own earnings date. Approximate daily EV when only a quarterly EV snapshot
exists: `ev_daily ≈ ev_at_last_quarter_close + (market_cap_daily − market_cap_at_last_quarter_close)`
(net debt assumed sticky intra-quarter) — flag this row `ev_daily_approximated: true` in
`method_flags` so a consumer can tell interpolated-EV days from quarter-end-exact ones.

### 5.4 Financial-industry metric class (`_metric_class == "financial"`)

Primary metrics: **P/B** and **P/TBV**, computed exactly as
`valuation_basis.py::_manual_book_multiple` already does
(`market_cap / totalStockholdersEquity`, and the tangible variant subtracting `goodwill`
+ `intangibleAssets`). PE is still computed and stored (never suppressed) but
`method_flags: {"primary_metric": "p_tbv"}` tells a consumer which column to headline.
EV/EBITDA is **not computed at all** for this class — enterprise value is not a
meaningful construct when debt is the raw material of the business, not leverage on
operations (mirrors why `screens.py` already skips `netDebtToEBITDA` for
non-positive-EBITDA/financial names).

### 5.5 Coverage honesty

Every `comp_set_metrics_daily` row (schema in §6) carries:
- `n_members` — set size at that `valid_from`/`valid_to` window.
- `n_valid` — how many members actually contributed a value to *this specific metric*
  (a member can be in the set but miss one metric while contributing another).
- `coverage_pct = n_valid / n_members`.

**No row is ever silently computed and hidden for being thin.** A row with
`coverage_pct < 0.5` is written anyway (never dropped) and tagged
`method_flags: {"coverage": "thin"}` — consumers decide how to render a thin row (e.g.
grey it out), the pipeline never decides for them by omission. This mirrors the repo's
existing "hide-don't-stub" *rendering* convention (§ hides at the display layer, not the
data layer) — the data layer's job is to record the truth, including "we only had 3 of 9
members" truth.

## 6. Schema

New Alembic migration (single revision, additive-only; follow the
`sa.inspect(bind).get_table_names()` existing-table guard convention from
`alembic/versions/0044_etf_profile_and_holdings.py`):

```python
op.create_table(
    "comparable_sets",
    sa.Column("comparable_set_id", sa.String(64), primary_key=True),  # f"{ticker}_{method_version}"
    sa.Column("ticker", sa.String(16), nullable=False),
    sa.Column("method_version", sa.Integer, nullable=False),
    sa.Column("resolved_at", sa.DateTime, nullable=False),
    sa.Column("metric_class", sa.String(16), nullable=False),   # 'operating'|'financial'|'reit'
    sa.Column("method_flags", sa.Text, nullable=True),          # JSON blob, see §3.1/5.4
    sa.Column("source_summary", sa.Text, nullable=True),        # JSON: {step_a_n, step_b_n, step_c_n, override_applied}
)
op.create_index("idx_comparable_sets_ticker", "comparable_sets", ["ticker"])

op.create_table(
    "comparable_set_members",
    sa.Column("comparable_set_id", sa.String(64), sa.ForeignKey("comparable_sets.comparable_set_id"), nullable=False),
    sa.Column("member_ticker", sa.String(16), nullable=False),
    sa.Column("membership_reason", sa.String(24), nullable=False),  # 'industry_seed'|'sector_widened'|'llm_ratified'|'pinned_override'
    sa.Column("context_only", sa.Boolean, nullable=False, server_default=sa.false()),  # market-cap-only peer, §3.1 Step C
    sa.Column("valid_from", sa.Date, nullable=False),
    sa.Column("valid_to", sa.Date, nullable=True),  # NULL = still current
    sa.PrimaryKeyConstraint("comparable_set_id", "member_ticker", "valid_from", name="pk_comparable_set_members"),
)
op.create_index("idx_csm_member", "comparable_set_members", ["member_ticker", "valid_from"])

op.create_table(
    "comp_set_metrics_daily",
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("scope_type", sa.String(16), nullable=False),   # 'comparable_set'|'industry'|'sector'|'fmp_snapshot'
    sa.Column("scope_key", sa.String(64), nullable=False),    # comparable_set_id, or industry/sector string, or same for the fmp_snapshot row
    sa.Column("as_of_date", sa.Date, nullable=False),
    sa.Column("metric", sa.String(24), nullable=False),       # 'pe_ttm'|'ev_ebitda_ttm'|'p_b'|'p_tbv'|'rev_yoy'|'fcf_yield_ttm'
    sa.Column("stat_type", sa.String(16), nullable=False),    # 'median'|'aggregate'
    sa.Column("value", sa.Float, nullable=True),              # NULL when undefined (see §5.2), never a sentinel like -1/0
    sa.Column("n_members", sa.Integer, nullable=False),
    sa.Column("n_valid", sa.Integer, nullable=False),
    sa.Column("coverage_pct", sa.Float, nullable=False),
    sa.Column("method_version", sa.Integer, nullable=False),
    sa.Column("method_flags", sa.Text, nullable=True),        # JSON blob
    sa.Column("computed_at", sa.DateTime, nullable=False),
    sa.UniqueConstraint("scope_type", "scope_key", "as_of_date", "metric", "stat_type", "method_version",
                         name="uq_comp_set_metrics_daily"),
)
op.create_index("idx_csmd_scope_date", "comp_set_metrics_daily", ["scope_type", "scope_key", "as_of_date"])
```

Notes:
- `scope_type='fmp_snapshot'` rows store the FMP `sector-pe-snapshot`/`industry-pe-snapshot`
  values themselves (re-keyed into the same table) purely so §7's drift check is a
  same-table join, not a cross-format comparison. `stat_type` for these rows is always
  `'median'` (FMP's snapshot doesn't publish a cap-weighted variant) — the drift check
  only ever compares FMP's median-shaped number against our `median` row, never against
  `aggregate` (comparing different constructions would manufacture fake drift).
- No new SQL **views** — per repo precedent (`reference_metrics_ratios_views_slow.md`:
  views here are a known slow/buggy landmine), `comp_set_metrics_daily` is a **materialized,
  persisted table** written by the CLI in §8, never a view computed on read.
- `industry`/`sector` scope rows use the pool-wide slice (every pool member in that
  industry/sector, not a per-ticker market-cap band) — this is what §4.1's "multiples
  benchmark" resolves to.

## 7. Parity/QA drift check

New CLI **`execution/check_comp_set_drift.py`**, weekly cadence (mirrors the existing
Sunday eval-rung slot — no LLM leg, so it doesn't need to respect the 03:00-05:00 PT
protected windows, but keep it off that slot anyway as a matter of hygiene since the
morning pipeline is also touching the FMP cache then).

Logic: for each pool `industry` with both a `scope_type='industry'` row and a
`scope_type='fmp_snapshot'` row on the same (or nearest prior) `as_of_date`, compute
`drift_pct = (bottoms_up_median - fmp_snapshot_median) / fmp_snapshot_median`. Write one
row per (industry, date) to a lightweight `comp_set_drift_checks` table (or, cheaper:
just query `comp_set_metrics_daily` directly for both scope_types and diff at read time —
no new table needed since both numbers already live in the same table; **prefer this,
skip a dedicated drift table entirely**).

**Documented expected deviations** (do not treat these as bugs):
- Universe size/composition: FMP's industry PE draws from its full global coverage
  (thousands of names per industry, ex-US included); our bottoms-up number is ~270-name,
  US-listed-only, `index_member`+tracked-only. A US-mega-cap-heavy industry will show
  bottoms-up *richer* than FMP's broader, more-value-dilutive universe; a US-thin,
  EM-heavy industry could show the opposite.
- TTM cutoff timing: FMP's snapshot updates on its own vendor cadence; ours updates the
  moment a member's `income_statement_quarterly` cache refreshes. A few days of drift
  around earnings season is expected, not an error.
- Threshold: a documented, named constant `DRIFT_ALERT_THRESHOLD = 0.25` (25%) — beyond
  that, without an explainable composition reason (log the two universes' member counts
  alongside the drift number so "explainable" is a one-glance check, not a re-investigation),
  surface it as a data-quality flag (`log.warning` + a row in the existing dashboard status
  panel, `src/pipeline/dashboard_status.py`, not a hard failure — this is a QA signal for
  the owner, not a pipeline-breaking assertion).

## 8. Pipeline CLIs, cadence, idempotency

**`execution/build_comparable_sets.py`** — resolve + freeze.
```
python execution/build_comparable_sets.py --ticker NU
python execution/build_comparable_sets.py --all-portfolio      # phase 1 scope
python execution/build_comparable_sets.py --all-tracked        # phase 2 scope (+watchlist+evaluation)
python execution/build_comparable_sets.py --ticker NU --refresh
```
Idempotent: skips a ticker whose current-`method_version` set is `valid_to IS NULL` and
whose freshly-resolved candidate list is unchanged from the frozen one, unless
`--refresh`. Writes one `ingestion_runs` row per invocation via
`src/pipeline/run_accounting.py::start_run`/`end_run` (exact existing helper, same
pattern as `execution/extract_facts.py`), one `stage_transitions` row per ticker
(`StageName`/`StageStatus` from `src/models/runs.py` — add a new `StageName` member,
e.g. `COMPARABLE_SET_RESOLVE`, rather than overloading an existing one).

**`execution/track_comp_metrics.py`** — periodic aggregate computation.
```
python execution/track_comp_metrics.py --date 2026-07-17
python execution/track_comp_metrics.py --backfill --from 2026-01-01 --to 2026-07-17
```
Cadence: **daily**, since `market_cap` moves daily even though quarterly financials don't
— run as a lightweight new stage appended to the existing morning pipeline (or as its own
cron entry; either is fine since it's pure deterministic math with no LLM leg — no
`llm_quota_scheduling.md` window conflict to register). Idempotent via the
`(scope_type, scope_key, as_of_date, metric, stat_type, method_version)` unique
constraint — re-running a date that's already fully written is a no-op per-row upsert
(`INSERT ... ON CONFLICT DO UPDATE`, refreshing `value`/`coverage_pct`/`computed_at` in
case a late-arriving cache file changes the answer for that date).

**`execution/check_comp_set_drift.py`** — weekly QA (§7).

## 9. Backfill strategy — how far back is honest

The raw material (`historical_market_cap.json`, `price_chart_10y_div_adj.json`,
`income_statement_quarterly.json` with `limit=100`) genuinely goes back up to **10
years** for most pool members (per §1's confirmed fetch job list). But backfilling
`comp_set_metrics_daily` 10 years using **today's** comparable-set membership applied to
each member's historical financials is **not** a true historical-universe
reconstruction — it's "what would this metric have read, for the industry as it's
composed *today*, using each member's real financials at each past date." That's a
useful, honest series (no survivorship bias in the sense of dropping delisted names,
since we're not trying to reconstruct who was in the industry in 2018) but it is NOT
"what was the industry's PE in 2018" in the strict sense, because company X might not
have existed, been in a different industry, or been a different size back then.

**Decision: label it honestly, don't pretend otherwise.**
- `comparable_sets.source_summary` (or a dedicated column, `membership_asof`) records
  that every backfilled row was computed under the **current** frozen membership, not a
  period-appropriate one.
- Phase 1 ships **forward-only**: `track_comp_metrics.py` starts accumulating real daily
  rows from its first run date onward; no historical backfill. This is the simplest,
  fully-honest starting point — every row's membership and computed value both belong to
  the date they claim to.
- Phase 2 (optional, only if the owner wants a longer trend line for a new position
  review): `--backfill --from <date>` applies the *current* frozen membership's
  financials/market-caps at each historical date, clearly labeled per the point above.
  Do not backfill further back than the `pool`'s youngest member's IPO date for
  industry/sector-scope rows (a member with no history at date D is simply excluded from
  that date's `n_members`/coverage, same mechanism as always — never fabricated).

## 10. Special-case handling summary (cross-reference)

| Case | Handling | Where |
|---|---|---|
| Banks/insurers/diversified financials | `_metric_class == "financial"`; P/B & P/TBV primary, EV/EBITDA not computed | §3.2, §5.4 |
| Holdcos (BN, …) | Manual pin in `comparable_set_overrides.py`, `method_flags.whole_co_pe_not_meaningful` | §3.1 Step D |
| Recently-IPO'd (<4 trailing quarters on file) | Naturally excluded from every TTM aggregate by the `_sum_last4` all-4-present gate; annotated (not gated) via `micro_thesis/holdings/{T}.json` `recently_ipod` where available | §3.2 |
| ETFs | Excluded as comparable-set members (§2); used only for the price-performance benchmark line, never for multiples (§4.1) | §2, §4.1 |
| REITs | `_metric_class == "reit"`, P/FFO-proxy primary — **flagged as a phase-2 metric**, not built in phase 1 (FFO isn't a raw FMP field; needs `netIncome + realEstate D&A` reconstruction, deferred to keep phase 1 scoped) | §3.2 |
| Out-of-pool LLM peer (market-cap only) | `context_only=True`; roster-visible, excluded from every median/aggregate | §3.1 Step C, §1 |

## 11. Phasing & blast radius

**Phase 1 — foundation, zero existing-file changes.**
New files only: migration, `src/compute/comparable_sets.py`,
`src/compute/comparable_set_overrides.py`, `src/compute/sector_benchmark_map.py`,
`execution/build_comparable_sets.py` (scope: `--all-portfolio`, ~15 names, hand-verifiable
by the owner), `execution/track_comp_metrics.py` (comparable-set scope only, no
industry/sector rows yet). No cron registration yet — run manually. Blast radius: none;
nothing existing reads these new tables, so a bug here cannot regress anything shipped.

**Phase 2 — widen + industry scope + drift check.**
Extend `build_comparable_sets.py` to `--all-tracked` (watchlist+evaluation, ~100 names);
add `scope_type='industry'`/`'sector'` rows to `track_comp_metrics.py`; add
`execution/check_comp_set_drift.py`; register both CLIs' cadence in
`directives/llm_quota_scheduling.md` (even though neither has an LLM leg — keep the
scheduling registry complete per the repo convention: "every NEW scheduled job... registers
its window"). Blast radius: low — new cron entries, no existing stage touched; if a job
fails it degrades per the standard per-item pattern (log + skip + retry next run), it
does not block the existing morning pipeline.

**Phase 3 — render consumer + benchmark ratification.**
Ratify `sector_benchmark_map.py` entries for the pool's actual industries (run the
one-off `propose_sector_benchmarks.py`, owner reviews, hand-edits the module); wire the
first UI surface reading `comp_set_metrics_daily` (a "Sector context" card — likely on
the Company tab alongside the peer-comp panel, or the research cockpit). **This is the
only phase touching UI** — it must use `src/ui/controls.py` primitives only
(`.k-pill`/`.k-well`/`ticker_label()`), register in `tests/test_ui_controls.py`'s
`REGISTERED` set if it emits `var(--`, and pass `python -m pytest
tests/test_ui_controls.py -q` before merge, per `AGENTS.md` §UI. Blast radius: a new,
additive card — does not touch the existing peer-comp panel or any existing renderer
logic; a bug here is cosmetic/isolated, not a data-pipeline regression.

## 12. Worked example (NU, illustrative — implementer should run this for real once §3 ships)

1. Subject `NU`: `profile.industry` (say) "Credit Services", `marketCap` ≈ $60B, US-listed
   (NYSE ADR).
2. Step A: pool members with `industry == "Credit Services"` and mcap in [$15B, $240B] —
   likely thin (Credit Services in the US-listed pool is small); expect `n < 8`.
3. Step B fires: widen to `sector == "Financial Services"`, mcap [$6B, $600B] — pulls in
   regional/diversified banks that aren't true peers by business model, but they're
   corroborating scale/sector context, not the headline set.
4. Step C: read `data/peer_selection/NU.json` — per `directives/peer_selection_llm.md`'s
   confirmed production output, suggestions are `SOFI, MELI, INTR, GRAB, KSPI, SE, …`.
   `fetched_complete` likely includes `SOFI` (in-pool, full data) and marks
   `GRAB/INTR/KSPI` `context_only` (documented 402-on-new-symbol behavior) unless those
   specific tickers happen to already be onboarded in the pool.
5. Step D: no override needed for NU today (not a holdco case); an override entry gets
   added only if the owner later disagrees with the resolved set.
6. `_metric_class("Financial Services", "Credit Services")` → keyword blob contains
   "credit services" → `"financial"`. Primary metrics for NU's comp-set row: P/B, P/TBV;
   PE computed and stored but flagged secondary.
7. `sector_benchmark_map` lookup for "Credit Services" → no dedicated entry yet (per the
   seed table in §4, it's an open TODO) → falls back to `sector_etf` for "Financial
   Services" (KBE or XLF, owner picks at ratification time) for the performance line only.

## 13. Explicitly deferred (not blocking, do not build in phase 1)

- Cap-weighted revenue-growth and cap-weighted FCF-yield aggregates (§5.2 table) — median
  covers the mission's ask; the aggregate constructions are sound but add review surface
  without being requested.
- REIT P/FFO-proxy metric (§10) — needs a D&A reconstruction not directly on file.
- A dedicated `comp_set_drift_checks` table — start by querying `comp_set_metrics_daily`
  directly for both scope types; add a dedicated table only if the query proves awkward
  in practice.
- Any render surface beyond the single Phase-3 card — this doc specifies the data layer
  completely; UI iteration is intentionally left to whoever builds Phase 3 with real data
  in front of them.

## 14. Phase 1 implementation notes (added post-build, Directive Maintenance: refine in place)

**Schema deviation from §6's literal DDL**: `comparable_set_members.comparable_set_id`
is shipped as a plain `sa.String(64)` column with **no** `sa.ForeignKey`, not the
`ForeignKey("comparable_sets.comparable_set_id")` the snippet shows. This repo's
FK-poisoning invariant (`reference_platform_invariants.md`; the identical call already
made in migrations 0160/0161 for `formula_definitions`/`metric_computation_attempts`)
means a real FK fails every child insert under a test fixture stamped at an earlier
alembic revision, because `db.open_conn` runs `PRAGMA foreign_keys=ON`. Validated at the
code layer instead — `compute.comparable_sets.freeze_comparable_set` only ever writes a
`comparable_set_id` it just created/looked up itself in the same transaction. Landed as
migration `alembic/versions/0165_comparable_sets.py` (renumbered at push time per the
repo's collision protocol if another PR lands a migration first).

**Module boundary not named in §3/§8**: the aggregate math (§5) lives in a new
`src/compute/comp_set_metrics.py`, separate from `src/compute/comparable_sets.py` (which
owns only the rule ladder + freeze/versioning, §3). `execution/track_comp_metrics.py`
is a thin CLI over `comp_set_metrics.py`, consistent with the repo's Layer-3 rule that
`execution/` scripts hold no business logic.

**`n_members` denominator clarified**: for every `comp_set_metrics_daily` row,
`n_members`/`n_valid`/`coverage_pct` are computed over **full (non-`context_only`)
members only**. `context_only` peers (§3.1 Step C, out-of-pool LLM suggestions with only
a market-cap-level fetch) structurally can never resolve a value for any metric here;
counting them in the denominator would permanently cap every affected set's
`coverage_pct` below 100% for a reason unrelated to real data thinness. They remain
roster-visible (frozen into `comparable_set_members` with `context_only=1`) but are
excluded from every metric computation's `n_members` base, not merely its numerator.

**REIT metric-class simplification (Phase 1 only)**: §3.2 says a REIT set's EV/EBITDA
should be "flagged n/m" and its primary metric is a P/FFO-proxy — but P/FFO is explicitly
deferred to Phase 2 (§10/§13, no raw FMP field). Phase 1's `compute_metrics_for_set`
therefore does not compute EV/EBITDA *or* P/B-P/TBV for `metric_class='reit'` (mirroring
the `financial` class's EV/EBITDA exclusion, since neither an EV/EBITDA number nor a
book-multiple number is what the doc specifies as REIT-primary); it still computes PE,
rev-YoY, and FCF-yield for REIT-class sets — nothing REIT-specific is fabricated beyond
what §1's metric catalog already covers. No roster ticker classifies as `reit` today, so
this path is exercised only by direct unit test, not by the current portfolio.

**Pinned-override `method_flags` propagation**: `whole_co_pe_not_meaningful`/
`whole_co_ev_ebitda_not_meaningful` (the BN holdco entry) are propagated as
**passthrough annotations** onto every emitted `comp_set_metrics_daily` row's own
`method_flags`, not as a computation-suppressing gate — consistent with §5.5's
"never hide by omission" principle: the number is still computed and stored if the raw
inputs resolve; the flag tells a future consumer not to headline it.
