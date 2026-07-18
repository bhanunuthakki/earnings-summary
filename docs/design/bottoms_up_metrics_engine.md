# Bottoms-Up Derived Metrics Engine — Design

Status: proposed. Author: agent design pass, 2026-07-17. No code changes accompany this
document; it is the spec an implementation agent executes against, phase by phase.

## 0. Why, and what already exists

FMP Starter gates the *derived* quarterly series (`key-metrics`, `ratios`) to annual-only.
Raw quarterly statements (income/balance/cash-flow, `financial_facts`), quarterly
`enterprise-values`, `historical-market-capitalization`, and shares/EOD price data remain
on Starter (see `execution/save_fmp_data.py` catalog — `FMP_ENTERPRISE_VALUES`,
`FMP_HISTORICAL_MARKET_CAP`, `FMP_KEY_METRICS`, `FMP_FINANCIAL_RATIOS` are all existing
`DocType` members already cached to `data/historical/fmp/`). The platform pivots to
computing every ratio bottoms-up from raw facts, keeping FMP's own derived numbers only as
a parity cross-check.

**This is not a greenfield build.** `src/compute/fmp_derived_kpis.py` +
`src/pipeline/kpi_persistence.py` already implement a smaller version of exactly this
pattern: read `financial_facts`, compute margins/YoY/ROE, persist into `kpi_facts` with a
`computed_from` JSON lineage blob (schema in alembic `0087_kpi_computed_from`, docstring:
`{"display": "...", "inputs": [{"ref": "financial_fact", "item": "...", "period_end":
"...", "doc_id": N, "tier": "..."}]}`), routed through `find_or_create_kpi_definition` +
`insert_kpi_with_restatement_detection`. `execution/derive_kpis_from_fmp.py` is the CLI
wrapper, using `StageName.COMPUTE` in the run-accounting ledger. **This engine formalizes
and extends that pattern — it does not replace its storage substrate.** The gaps versus
the quality bar: no versioned formula registry (metric identity is a bare string constant,
e.g. `KPI_ROE = "ROE"`, with exactly one silent method choice baked in), no "not
computable" outcome (a metric that can't be computed for a ticker just never gets a row —
indistinguishable from "not yet ingested"), no applicability/business-model gating (ROE
would be computed for a REIT the same as a SaaS company), no IFRS input-mapping layer, and
no parity harness against FMP's own ratios.

**Landmine already hit once, do not repeat it:** the `metrics`/`ratios` SQL views
(alembic `0012_metrics_and_ratios_views`, still present) window-scan the entire
`financial_facts` table on every query (~4-12s warm, 80s+ cold on prod's 726k rows) AND
have a real correctness bug — grouping by `fiscal_period_type` label instead of the
calendar quarter derived from `period_end` mis-groups off-calendar reporters (FMP vs SEC
disagree on quarter labels for names like AMAT/MU/TOL/VEEV), shifting revenue a quarter
and colliding Q3/Q4. The fix already landed for one consumer:
`src/report/sections/financials.py` reads `financial_facts` directly via
`_QUARTERLY_FACTS_SQL`/`_ANNUAL_FACTS_SQL`, keyed off `calendar_quarter_key(period_end)`
(from `compute.kpi_resolver`), not off `fiscal_period_type`. **The metrics engine's period
grid must use the same calendar-quarter keying, computed once by a batch job and
persisted — never a view, never computed on a render/API path.**

## 1. Metric catalog v1

Every row below becomes one `FormulaDef` in the registry (§2). `formula_id` is the stable
string key; version starts at 1. "Applicability" defaults to `ALL` (operating companies);
`BANK`/`INSURANCE`/`HOLDCO` exclusions are enforced by `applicability.py` (§2) against a
new `tracked_companies.business_model_class` column (§5) — the roster carries NU (bank),
BN (holding company / insurance-adjacent), so this is not a hypothetical.

Canonical input concepts (§2 `inputs.py`) are written as `CONCEPT_NAME`; where a formula
needs a US-GAAP `financial_facts.line_item` that already exists 1:1, it's noted directly.

### Margins

| formula_id | formula | inputs | ambiguity / method note | applicability |
|---|---|---|---|---|
| `gross_margin` | `gross_profit / revenue` | REVENUE, GROSS_PROFIT | none — both are as-filed lines | N/A: BANK, INSURANCE (no COGS concept) |
| `operating_margin` | `operating_income / revenue` | REVENUE, OPERATING_INCOME | none | N/A: BANK (use `net_interest_margin`-family KPIs instead, already IR-extracted) |
| `net_margin` | `net_income / revenue` | REVENUE, NET_INCOME | net income here is *attributable to parent* if minority interest exists; a `net_margin_incl_minority` variant is NOT created in v1 — flag as future if a roster name has material NCI | ALL |
| `ebitda_margin` | `ebitda / revenue` | REVENUE, EBITDA | EBITDA computed as `operating_income + depreciation_and_amortization`, NOT `net_income + interest + tax + D&A` (the two can differ when there's non-operating income/expense above operating_income) — this is a documented, disclosed method choice, see `ebitda` under Leverage inputs below | N/A: BANK, INSURANCE |
| `fcf_margin` | `free_cash_flow / revenue` | REVENUE, FREE_CASH_FLOW | `free_cash_flow` = `operating_cash_flow - capex` as filed/derived; matches existing `fmp_derived_kpis.KPI_FCF_MARGIN_GAAP` | ALL |

### Growth

| formula_id | formula | inputs | ambiguity / method note | applicability |
|---|---|---|---|---|
| `revenue_yoy` | `(rev_t - rev_t-4q) / rev_t-4q` | REVENUE (t, t-4 calendar quarters) | same-calendar-quarter comparison, never fiscal-label comparison (the §0 landmine) | ALL |
| `revenue_qoq` | `(rev_t - rev_t-1q) / rev_t-1q` | REVENUE (t, t-1) | sequential — noisy for seasonal businesses; documented, not hidden | ALL |
| `eps_diluted_yoy` | `(eps_t - eps_t-4q) / eps_t-4q` | EPS_DILUTED | skipped when prior EPS ≤ 0 (sign flip makes % meaningless) — `not_computable: denominator_le_zero` | ALL |
| `revenue_cagr_3y` | `(rev_FY / rev_FY-3)^(1/3) - 1` | REVENUE (FY, FY-3) | annual-cadence only; requires 3 full FY gaps, no interpolation across a stub year | ALL |
| `ebitda_cagr_3y` | same shape over EBITDA (FY) | EBITDA (FY, FY-3) | inherits the ebitda method note above | N/A: BANK, INSURANCE |

### Returns

| formula_id | formula | inputs | ambiguity / method note | applicability |
|---|---|---|---|---|
| `roe` | `ttm_net_income / equity_t` | NET_INCOME (ttm sum), TOTAL_STOCKHOLDERS_EQUITY (point-in-time) | matches existing `fmp_derived_kpis.KPI_ROE`; equity is period-END, not average — a documented choice (avg-of-2-quarters is the alternate, not built in v1) | N/A: none typically excluded, but a bank's ROE has a different capital-adequacy context — still computed, just annotated |
| `roa` | `ttm_net_income / total_assets_t` | NET_INCOME (ttm), TOTAL_ASSETS | period-end assets, not average — same choice as ROE | ALL |
| `roic_strict` | `nopat / invested_capital_strict` | see NOPAT/invested-capital defs below | **method-variant metric** — excludes operating leases from invested capital | N/A: BANK, INSURANCE (capital structure concept doesn't transfer) |
| `roic_lease_adjusted` | `nopat / invested_capital_lease_adj` | adds OPERATING_LEASE_LIABILITY to invested capital | **alt of `roic_strict`** — both stored, both queryable, neither silently preferred | N/A: BANK, INSURANCE |
| `roce` | `ebit / (total_assets - total_current_liabilities)` | EBIT, TOTAL_ASSETS, TOTAL_CURRENT_LIABILITIES | "capital employed" = total assets minus current liabilities (the common textbook definition); an alternate `equity + total_debt` definition is NOT built in v1, noted as a future alt | N/A: BANK, INSURANCE |

**NOPAT** (shared by both ROIC variants) = `operating_income * (1 - effective_tax_rate)`,
where `effective_tax_rate = clip(income_tax_expense / pretax_income, 0.0, 1.0)`; when
`pretax_income <= 0` the rate falls back to a documented flat statutory proxy (21% US
federal) and the row is tagged `method_flag: "statutory_tax_rate_fallback"` inside
`computed_from` (still a `kpi_facts` row, not `not_computable` — the value is still
meaningful, just flagged).

**Invested capital (strict)** = `TOTAL_DEBT + TOTAL_STOCKHOLDERS_EQUITY -
CASH_AND_EQUIVALENTS`. **Invested capital (lease-adjusted)** additionally adds
`OPERATING_LEASE_LIABILITY`. This is the same category of ambiguity as the net-debt
variants below and is intentionally named identically to that split so an owner reading
the registry recognizes the pattern once.

### Liquidity

| formula_id | formula | inputs | ambiguity / method note | applicability |
|---|---|---|---|---|
| `current_ratio` | `total_current_assets / total_current_liabilities` | TOTAL_CURRENT_ASSETS, TOTAL_CURRENT_LIABILITIES | none | N/A: BANK, INSURANCE (no current/non-current balance-sheet split) |
| `quick_ratio` | `(total_current_assets - inventory) / total_current_liabilities` | + INVENTORY | `inventory` = 0 when the concept doesn't exist for the filer (e.g. pure-services) rather than `not_computable` — a services business legitimately has zero inventory, this is NOT a missing input | N/A: BANK, INSURANCE |
| `cash_ratio` | `cash_and_equivalents / total_current_liabilities` | CASH_AND_EQUIVALENTS, TOTAL_CURRENT_LIABILITIES | none | N/A: BANK, INSURANCE |

### Leverage

| formula_id | formula | inputs | ambiguity / method note | applicability |
|---|---|---|---|---|
| `debt_to_equity` | `total_debt / total_stockholders_equity` | TOTAL_DEBT, TOTAL_STOCKHOLDERS_EQUITY | `not_computable: denominator_le_zero` when equity ≤ 0 (common for buyback-heavy names) rather than a nonsensical negative ratio | N/A: BANK (regulatory capital ratios are the right lens, already IR-extracted) |
| **`net_debt_strict`** | `total_debt - cash_and_equivalents - short_term_investments` | TOTAL_DEBT, CASH_AND_EQUIVALENTS, SHORT_TERM_INVESTMENTS | **the AAPL-scale ambiguity the owner flagged**: strict variant nets only cash + short-term (near-cash) investments | ALL |
| **`net_debt_incl_lt_securities`** | `net_debt_strict - long_term_investments` | + LONG_TERM_INVESTMENTS (non-current marketable securities) | **alt of `net_debt_strict`** — this is the ~$78B AAPL swing case verbatim: whether long-term marketable securities count as "cash-like" is a real analyst judgment call, so both are stored, neither is "the" net debt | ALL |
| `net_debt_to_ebitda` | `net_debt_strict / ebitda_ttm` | + EBITDA (ttm) | uses the strict net-debt variant by convention (documented); a `_lease_adj` sibling using `net_debt_incl_lt_securities` is a Phase 2 addition if useful | N/A: BANK, INSURANCE |
| `interest_coverage` | `ebit / interest_expense` | EBIT, INTEREST_EXPENSE | `not_computable: missing_input` when `interest_expense` is 0/absent (debt-free names) — not a divide-by-zero crash, not an infinite ratio silently shown | N/A: BANK, INSURANCE |

### Efficiency

| formula_id | formula | inputs | ambiguity / method note | applicability |
|---|---|---|---|---|
| `asset_turnover` | `revenue / total_assets_avg` | REVENUE (ttm), TOTAL_ASSETS (avg of period start/end) | uses AVERAGE assets (2-point avg), unlike ROE/ROA above which use period-end — documented per-metric, not a blanket convention | N/A: BANK |
| `receivables_turnover` | `revenue / accounts_receivable_avg` | REVENUE (ttm), ACCOUNTS_RECEIVABLE | `not_computable: not_applicable_business_model` for subscription/consumer businesses with no material AR (documented per-ticker via applicability, not inferred) | N/A: BANK, subscription-consumer names (case-by-case, see §2 applicability.py) |
| `inventory_turnover` | `cost_of_revenue / inventory_avg` | COST_OF_REVENUE, INVENTORY | `not_computable: not_applicable_business_model` for services/software/bank — inventory=0 companies get this metric suppressed entirely (unlike `quick_ratio` above, where inventory=0 still produces a valid ratio) | N/A: services, SaaS, BANK, INSURANCE |
| `cash_conversion_cycle` | `DIO + DSO - DPO` | derived from inventory/receivables/payables turnovers | composite of 3 already-ambiguous inputs — `not_computable` propagates if any leg is `not_computable` | same exclusions as its 3 legs |
| `sbc_pct_revenue` | `stock_based_compensation / revenue` | REVENUE, STOCK_BASED_COMPENSATION | none — always computable when SBC is disclosed (0 for banks/older-economy names, that's a real number not a gap) | ALL |

### Per-share

| formula_id | formula | inputs | ambiguity / method note | applicability |
|---|---|---|---|---|
| `eps_basic` | as-filed | EPS_BASIC | pass-through of a filed line (not really "derived" but registered for lineage uniformity) | ALL |
| `eps_diluted` | as-filed | EPS_DILUTED | same | ALL |
| `eps_adjusted_ex_sbc` | `(net_income + stock_based_compensation) / diluted_shares` | NET_INCOME, SBC, WEIGHTED_AVG_SHARES_DILUTED | **method-flagged**: adding back SBC pre-tax overstates adjusted EPS versus a proper tax-effected add-back; v1 documents this as the known simplification (`method_flag: "sbc_addback_pretax"`) rather than computing a marginal tax adjustment (Phase 2 candidate) | ALL |
| `fcf_per_share` | `free_cash_flow / diluted_shares` | FREE_CASH_FLOW, WEIGHTED_AVG_SHARES_DILUTED | none | ALL |
| `bvps` | `total_stockholders_equity / diluted_shares` | TOTAL_STOCKHOLDERS_EQUITY, WEIGHTED_AVG_SHARES_DILUTED | uses period-end diluted share count, not spot shares outstanding — documented | ALL |

### Valuation (Phase 3 — needs live price/shares/market-cap, see §6)

| formula_id | formula | inputs | ambiguity / method note | applicability |
|---|---|---|---|---|
| `pe_ttm` | `price / eps_diluted_ttm` | PRICE (spot), EPS_DILUTED (ttm sum) | `not_computable: denominator_le_zero` when ttm EPS ≤ 0 (never show a negative or inverted P/E) | ALL |
| `ps_ttm` | `market_cap / revenue_ttm` | MARKET_CAP, REVENUE (ttm) | none | ALL |
| `pb` | `market_cap / total_stockholders_equity` | MARKET_CAP, TOTAL_STOCKHOLDERS_EQUITY | `not_computable` when equity ≤ 0 | ALL |
| `ev_ebitda` | `enterprise_value_strict / ebitda_ttm` | see EV def below | inherits the net-debt ambiguity — EV uses `net_debt_strict` by convention (documented); alt EV using `net_debt_incl_lt_securities` computable on demand | N/A: BANK, INSURANCE |
| `ev_sales` | `enterprise_value_strict / revenue_ttm` | | same EV convention | ALL |
| `fcf_yield` | `free_cash_flow_ttm / market_cap` | | none | ALL |
| `earnings_yield` | `net_income_ttm / market_cap` | | none (inverse of trailing P/E, kept as its own formula for direct comparability with fcf_yield) | ALL |

`enterprise_value_strict` = `market_cap + net_debt_strict + minority_interest +
preferred_equity`. Computed bottoms-up from `MARKET_CAP` (cached
`historical_market_cap.json`, still Starter-available) + `financial_facts`, **not** read
from FMP's own `enterprise-values` endpoint — that endpoint is retained only as the
parity-check row (§4), consistent with the whole engine's "FMP-derived becomes a
cross-check, not a dependency" mandate, even for the handful of derived series Starter
still serves quarterly.

## 2. Architecture

```
src/compute/metrics_engine/
    __init__.py
    registry.py       # FormulaDef (typed), REGISTRY: dict[(formula_key, version), FormulaDef]
    inputs.py         # CanonicalConcept enum + per-accounting-standard field mappings
    applicability.py  # business_model_class -> which formula_ids are in-scope
    engine.py         # pure compute(formula, inputs) -> ComputedValue | NotComputable
    io.py             # DB reads (financial_facts/kpi_facts/documents) + writes (kpi_facts, metric_computation_attempts)
    parity_known_mismatches.py  # expected-mismatch registry for §4
```

### `registry.py` — `FormulaDef`

```python
class MetricCategory(StrEnum):
    MARGIN = "margin"
    GROWTH = "growth"
    RETURNS = "returns"
    LIQUIDITY = "liquidity"
    LEVERAGE = "leverage"
    EFFICIENCY = "efficiency"
    PER_SHARE = "per_share"
    VALUATION = "valuation"

class ReasonCode(StrEnum):
    MISSING_INPUT = "missing_input"
    MISSING_INPUT_MAPPING = "missing_input_mapping"   # IFRS/standard has no mapped field
    NOT_APPLICABLE_BUSINESS_MODEL = "not_applicable_business_model"
    DENOMINATOR_LE_ZERO = "denominator_le_zero"

class FormulaDef(BaseModel):
    formula_key: str            # "net_debt_strict"
    version: int                # bump on ANY change to the math or inputs; never mutate in place
    category: MetricCategory
    display_formula: str        # human string for the lineage/tooltip, e.g. "total_debt - cash_and_equivalents - short_term_investments"
    method_notes: str           # the written method definition — ambiguity, treatment of leases/SBC/minority interest/one-timers
    required_inputs: tuple[CanonicalConcept, ...]
    optional_inputs: tuple[CanonicalConcept, ...] = ()   # zero-default concepts (e.g. INVENTORY for quick_ratio)
    alt_of: str | None = None    # groups method-variant siblings, e.g. both net_debt_* share alt_of="net_debt"
    period_grid: Literal["quarterly", "ttm", "fy"]
    unit: Unit                   # reuse models.facts.Unit
    excluded_business_models: frozenset[BusinessModelClass] = frozenset()
```

`REGISTRY` is append-only in the literal sense: once a `(formula_key, version)` pair is
committed, its `display_formula`/`method_notes`/inputs never change — a formula change is
always a new `version` entry, mirroring the "evals are per-version artifacts" instruction
and the existing `find_or_create_kpi_definition` idempotency pattern. A DB-mirror table
(§5 migration) is upserted from this module at engine-startup via
`find_or_create_formula_definition(conn, defn)` — same shape as
`pipeline.kpi_persistence.find_or_create_kpi_definition` — so `formula_id` (the DB row's
integer id) survives even if the Python `FormulaDef` is later deleted from the registry
(deprecated formulas keep their historical rows queryable).

### `inputs.py` — canonical concepts + standard mapping

```python
class CanonicalConcept(StrEnum):
    REVENUE = "revenue"
    GROSS_PROFIT = "gross_profit"
    OPERATING_INCOME = "operating_income"
    NET_INCOME = "net_income"
    EBITDA = "ebitda"                # itself a derived concept, computed not read (see below)
    TOTAL_DEBT = "total_debt"
    CASH_AND_EQUIVALENTS = "cash_and_equivalents"
    SHORT_TERM_INVESTMENTS = "short_term_investments"
    LONG_TERM_INVESTMENTS = "long_term_investments"
    TOTAL_STOCKHOLDERS_EQUITY = "total_stockholders_equity"
    TOTAL_ASSETS = "total_assets"
    TOTAL_CURRENT_ASSETS = "total_current_assets"
    TOTAL_CURRENT_LIABILITIES = "total_current_liabilities"
    INVENTORY = "inventory"
    ACCOUNTS_RECEIVABLE = "accounts_receivable"
    ACCOUNTS_PAYABLE = "accounts_payable"
    COST_OF_REVENUE = "cost_of_revenue"
    OPERATING_LEASE_LIABILITY = "operating_lease_liability"
    STOCK_BASED_COMPENSATION = "stock_based_compensation"
    INCOME_TAX_EXPENSE = "income_tax_expense"
    PRETAX_INCOME = "pretax_income"
    INTEREST_EXPENSE = "interest_expense"
    EPS_BASIC = "eps_basic"
    EPS_DILUTED = "eps_diluted"
    WEIGHTED_AVG_SHARES_DILUTED = "weighted_avg_shares_diluted"
    FREE_CASH_FLOW = "free_cash_flow"
    OPERATING_CASH_FLOW = "operating_cash_flow"
    CAPITAL_EXPENDITURE = "capital_expenditure"
    MARKET_CAP = "market_cap"          # Phase 3 only, from historical_market_cap cache
    PRICE = "price"                    # Phase 3 only, from EOD price cache

# US-GAAP: financial_facts.line_item already uses this exact vocabulary for
# most concepts (confirmed against alembic 0012's pivot and fmp_derived_kpis'
# _REQUIRED_LINE_ITEMS/_BALANCE_LINE_ITEMS) — the mapping is the identity function.
US_GAAP_FIELD_MAP: dict[CanonicalConcept, str] = {
    CanonicalConcept.REVENUE: "revenue",
    CanonicalConcept.TOTAL_STOCKHOLDERS_EQUITY: "total_stockholders_equity",
    # ... one entry per concept whose FMP-normalized line_item name already matches
}

# IFRS: only entries actually verified against a roster filer's normalized facts go
# here in Phase 1/2. An unmapped concept for a ticker whose accounting_standard is
# IFRS returns None from resolve_concept() -> engine emits
# ReasonCode.MISSING_INPUT_MAPPING, NEVER a guessed field name.
IFRS_FIELD_MAP: dict[CanonicalConcept, str] = {
    # populated incrementally in Phase 2 per verified filer (NU, BN, ASML, NVO)
}

def resolve_concept(standard: AccountingStandard, concept: CanonicalConcept) -> str | None:
    ...
```

`EBITDA` is not a raw line item; `engine.py` computes it as a first-class intermediate
(`operating_income + depreciation_and_amortization`) with its OWN method note, so any
metric consuming `EBITDA` inherits one documented definition rather than each formula
re-deriving it slightly differently.

### `applicability.py`

Reads `tracked_companies.business_model_class` (new column, §5) and returns the subset of
`REGISTRY` formulas in scope for a ticker: `excluded_business_models` on the `FormulaDef`
is checked directly; `not_applicable_business_model` for the finer-grained per-ticker
cases (e.g. `receivables_turnover` for a subscription consumer business) is a small
per-ticker override table living in code (`_TICKER_EFFICIENCY_OVERRIDES: dict[str,
frozenset[str]]`) next to `applicability.py` — same "owner-maintained exception list"
pattern the repo already uses for the AMZN segment-KPI defaults in
`fmp_derived_kpis._derive_segment_kpis`.

### `engine.py`

Pure functions only — no DB I/O, no logging side effects beyond returning a typed result:

```python
class ComputedValue(BaseModel):
    value: Decimal
    method_flags: tuple[str, ...] = ()   # e.g. "statutory_tax_rate_fallback"

class NotComputable(BaseModel):
    reason_code: ReasonCode
    reason_detail: str

def compute(formula: FormulaDef, resolved_inputs: dict[CanonicalConcept, Decimal | None]) -> ComputedValue | NotComputable:
    ...
```

`io.py` is the only module touching `sqlite3.Connection`: it resolves the canonical
inputs for a (ticker, period_end, fiscal_period_type) cell from `financial_facts` (+
`MARKET_CAP`/`PRICE` from their cached JSON for Phase-3 valuation formulas), calls
`engine.compute`, and persists via §3's storage shape.

### Storage decision: extend `kpi_facts`, do not create a parallel value table

**Recommendation: computed numeric outcomes land in `kpi_facts`** (via
`find_or_create_kpi_definition` + `insert_kpi_with_restatement_detection`, exactly the
existing `fmp_derived_kpis.py` pattern), tagged with two new nullable columns
(`formula_id`, `formula_version` — real columns, not just JSON, because the parity
harness and "recompute needed?" checks need to `GROUP BY`/`JOIN` on them cheaply).
Justification: `kpi_facts` is already the platform's universal askable/DCF-resolvable
substrate — the Ask engine, thesis break rules, the DIY picker, and DCF assumption sync
all resolve through `canonical_metric_name`/`resolve_kpi_definition_name` against this one
table. Landing derived ratios anywhere else means re-plumbing every consumer; landing them
here means every new metric is automatically visible platform-wide the moment it's
computed, which is the explicit reason the mission brief weighs this option in.

**"Not computable" cannot live in `kpi_facts`** — `kpi_facts.value` is `NOT NULL`
(alembic `0004_facts_tables`), so there is no way to store "attempted, and here's why it
failed" as a `kpi_facts` row. A **new table, `metric_computation_attempts`**, is the home
for that first-class outcome (and doubles as the idempotency/recompute ledger, §2
"recompute triggers" below):

```sql
CREATE TABLE metric_computation_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    period_end DATETIME NOT NULL,
    fiscal_period_type TEXT NOT NULL,
    formula_id INTEGER NOT NULL REFERENCES formula_definitions(id),
    status TEXT NOT NULL CHECK(status IN ('ok', 'not_computable')),
    reason_code TEXT,                 -- NULL when status='ok'
    reason_detail TEXT,
    kpi_fact_id INTEGER REFERENCES kpi_facts(id),  -- set when status='ok'
    input_fingerprint TEXT NOT NULL,  -- sha256 of resolved input (concept, value, doc_id) tuples
    engine_version TEXT NOT NULL,
    computed_at DATETIME NOT NULL
);
CREATE UNIQUE INDEX uq_metric_computation_attempts_logical
    ON metric_computation_attempts (ticker, period_end, fiscal_period_type, formula_id);
```

One logical row per (ticker, period, formula) — a re-run upserts this row (replacing the
prior attempt) rather than accumulating history; the *value* history (restatements) is
still fully preserved on the `kpi_facts` side via `supersedes_id`, so nothing about the
existing restatement chain is lost. `input_fingerprint` is the recompute-skip key (§2
idempotency, mirrors `compute.key_metrics._inputs_sha`).

A third new table, `formula_definitions`, is the DB mirror of the code registry
(`formula_key`, `version`, `category`, `display_formula`, `method_notes`,
`period_grid`, `unit`, `created_at`) — append-only, upserted from `registry.py` at
startup, `UNIQUE(formula_key, version)`.

### Period grid

- **Quarterly (discrete)**: one `financial_facts` row per calendar quarter, deduped via
  the *already-fixed* `_dedupe_by_calendar_quarter` / `calendar_quarter_key` pattern from
  `report/sections/financials.py` — reused directly, not reimplemented, so the metrics
  engine can never regress into the `fiscal_period_type`-label bug the views have.
- **TTM**: sum (flow items) or point-in-time-latest (stock items) over the trailing 4
  calendar quarters, computed on write and persisted as a `kpi_facts` row with
  `fiscal_period_type='TTM'` (the enum already has this value — `models.facts.FiscalPeriodType.TTM`)
  — never summed on read. A TTM window with fewer than 4 quarters or a >400-day span
  (the existing ROE gap-guard threshold in `fmp_derived_kpis.py`) is `not_computable:
  missing_input`.
- **FY**: annual `financial_facts` rows (`fiscal_period_type='FY'`), used directly for
  the CAGR metrics and as the CAGR/YoY comparison base for annual-cadence tickers.

### Recompute triggers

1. **Post-cacher hook**: after `execution/save_fmp_data.py` (or any statement-ingesting
   backfill) commits new `financial_facts` rows for a ticker, invoke the engine for that
   ticker's affected period range. Wired the same way `execution/derive_kpis_from_fmp.py`
   is today — as a follow-on stage in the morning pipeline sequence, using the existing
   `StageName.COMPUTE` stage.
2. **On-demand CLI**: `execution/compute_derived_metrics.py --ticker X` for ad hoc
   recompute (e.g. after a formula-version bump).
3. **Idempotency**: for each (ticker, period, formula), resolve inputs, hash them into
   `input_fingerprint`, and skip the write entirely if an existing
   `metric_computation_attempts` row matches on `(logical key, input_fingerprint,
   engine_version)` — a re-run over unchanged data is a fast no-op, exactly the contract
   `derive_for_ticker`'s "UNIQUE index dedupes" already promises, made explicit and cheap
   via the fingerprint instead of relying on the insert failing silently.

## 3. Provenance model

Per computed value, the following must be answerable without re-deriving anything:

| Question | Where it's answered |
|---|---|
| What formula produced this number, and which version? | `kpi_facts.formula_id` → `formula_definitions.formula_key`/`version`/`display_formula`/`method_notes` |
| What raw facts fed it? | `kpi_facts.computed_from` JSON `inputs[]` — `{ref, item, period_end, doc_id, tier}` per input, same shape `fmp_derived_kpis._lineage`/`_input_ref` already produce |
| Which filing(s) do those facts trace to? | Each input's `doc_id` → `documents.file_path`, `documents.sha256`, `documents.accession_number`, `documents.filing_date` — no new hash storage needed, `documents.sha256` already exists per alembic history (`models.documents.Document.sha256`) |
| When was it computed, by what engine build? | `metric_computation_attempts.computed_at` + `.engine_version` (a short git-describe-style string set at import time, e.g. `metrics_engine==2026.07.1`) |
| Was this a clean compute or a flagged method choice? | `kpi_facts.computed_from.method_flags[]` (e.g. `"statutory_tax_rate_fallback"`, `"sbc_addback_pretax"`) — always present in the JSON even on success, empty tuple when no flag applies |
| Why is there NO value for this (ticker, period, metric)? | `metric_computation_attempts` row with `status='not_computable'`, `reason_code`, `reason_detail` — queryable directly, never inferred from absence |
| Is this the "the" net debt or one of two documented variants? | `formula_definitions.alt_of` groups siblings; the UI/Ask layer that surfaces a leverage number always shows both `net_debt_strict` and `net_debt_incl_lt_securities` side by side rather than picking a default (Phase 2/3 surfacing rule) |

This closes the "which filing fed this number" query end-to-end: `kpi_facts` row →
`computed_from.inputs[].doc_id` → `documents` row → `file_path`/`sha256`/
`accession_number`. Multi-input TTM formulas will legitimately point at up to 4 distinct
source documents (one per quarter summed) — the JSON list already supports this; nothing
new is needed there.

## 4. Parity eval harness

**Goal**: catch formula regressions by comparing computed values against FMP's own
`ratios-ttm`/`key-metrics-ttm` cached JSON (still Starter-served) for every metric that
has an FMP equivalent.

**Design**:
- `src/compute/metrics_engine/parity_known_mismatches.py` — an explicit registry of
  `(ticker, formula_key) -> reason` entries for expected divergence (e.g. `("AAPL",
  "net_debt_strict"): "FMP's net_debt includes long-term marketable securities; we
  report the strict variant by design — compare against net_debt_incl_lt_securities
  instead"`). A pair not in this registry that fails tolerance is a **real regression**,
  not noise.
- Tolerance bands per metric category (documented alongside the registry, not
  hardcoded inline):
  - Margins/growth/returns (%): ±50 bps absolute OR ±3% relative, whichever is looser.
  - Leverage/liquidity ratios: ±5% relative (denominators can differ by rounding/currency
    conversion timing).
  - Per-share figures: ±1% relative (share-count timing drift between snapshot dates).
  - Valuation multiples (Phase 3): ±5% relative (price-snapshot timing).
- **Runner**: `tests/test_metrics_engine_parity.py`, pytest-collected so it's runnable
  on demand (`python -m pytest tests/test_metrics_engine_parity.py -q`) after any formula
  change — mirrors the existing `GOLDEN_REGEN=1 python -m pytest
  tests/test_workspace_golden.py` on-demand-diff convention. It reads the already-cached
  `data/historical/fmp/{TICKER}_ratios_ttm.json` / `{TICKER}_key_metrics_ttm.json` files
  directly (no network call, no Starter dependency at test time) and the ticker's
  `kpi_facts` TTM rows for the mapped `formula_id`s.
- **Output**: a JSON summary (per-metric pass/fail counts, list of *unexpected*
  mismatches with the actual vs. FMP values and delta) written to `.tmp/` — same
  "large output goes to `.tmp/`, stdout gets a summary" convention as every other
  `execution/` script.
- **Cadence**: bundled into the existing weekly eval rung (Sun ~10:30 America/Los_Angeles
  — the same slot the model-downgrade eval loop already uses, per
  `directives/llm_quota_scheduling.md`'s protected-window list) as an additional CI/cron
  job, plus mandatory on-demand after any `FormulaDef` version bump (a PR touching
  `registry.py` should not merge without a fresh parity run attached).

## 5. Pipeline plan

**Migrations** (additive/nullable only — no destructive changes to `kpi_facts` or
`financial_facts`, guarded the same way `0087_kpi_computed_from` guards a missing table):

1. `NNN_formula_definitions.py` — new table (§2/§3).
2. `NNN_metric_computation_attempts.py` — new table (§2).
3. `NNN_kpi_facts_formula_columns.py` — `ALTER TABLE kpi_facts ADD COLUMN formula_id
   INTEGER REFERENCES formula_definitions(id)`, `ADD COLUMN formula_version INTEGER`,
   both nullable (existing `fmp_derived_kpis`-authored rows keep `formula_id IS NULL`
   until/unless that module is migrated onto the registry — see Phase 1 note below).
4. `NNN_tracked_companies_business_model_class.py` — `ALTER TABLE tracked_companies ADD
   COLUMN business_model_class TEXT NOT NULL DEFAULT 'operating_company' CHECK(...)`,
   with a data-migration seeding the known non-default roster names (NU → `bank`, BN →
   `holdco`; extend as other bank/insurance/REIT names enter the book).
5. `NNN_tracked_companies_accounting_standard.py` — `ALTER TABLE tracked_companies ADD
   COLUMN accounting_standard TEXT NOT NULL DEFAULT 'us_gaap' CHECK(...)`, seeded
   explicitly (not inferred from `filing_regime`, because a `20-F` filer is not reliably
   IFRS — some file US GAAP-reconciled) for the known IFRS roster names: NU, BN, ASML,
   NVO (extend per directive `reference_onboarding_and_quirks.md`'s per-company quirks
   list as new IFRS names are onboarded).

**CLI**: `execution/compute_derived_metrics.py`, modeled directly on
`execution/derive_kpis_from_fmp.py`'s shape — `--ticker`/`--all`, `--db`, plus
`--scope portfolio,evaluation` (default; matches `pipeline.queries.BRIEFED_LIST_TYPES` —
the same portfolio+evaluation scope the mission brief names as priority) and `--force`
to bypass the input-fingerprint skip. Wraps every ticker in `start_run`/`record_stage`/
`end_run` exactly like the existing derive CLI, `StageName.COMPUTE`.

**Cadence**: runs as a stage immediately after the statement-ingestion stages in the
morning pipeline (after `financial_facts` is fresh for the day, before any
thesis/break-rule evaluation that reads `kpi_facts` — mirrors where
`derive_kpis_from_fmp` already sits today). No new protected-window conflict: this is a
pure-Python/SQLite compute stage with zero LLM calls, so it does not compete for the
shared Claude-CLI quota the 04:00 window protects.

**Scope control & backfill**: Phase 1 backfill runs over `tracked_companies_for_user(conn,
list_types=BRIEFED_LIST_TYPES)` (portfolio + evaluation) only, against the 5 years of
already-cached quarterly `financial_facts` (no new HTTP calls — this reads facts already
ingested by the existing statement pipeline, satisfying the "same cached artifacts, not
new fetches" input-substrate decision below). Watchlist/ETF/index_member tickers are
explicitly out of scope until a later phase, if ever — those cohorts already skip
brief-generation (`BRIEFED_LIST_TYPES` excludes them) so there is no existing consumer
waiting on their derived ratios.

**Input substrate decision**: read `financial_facts` (SQLite rows), **not** the raw
cached FMP JSON files directly. Justification: `financial_facts` is already the
tier-resolved, restatement-aware, provenance-tagged normalization of every statement
source (FMP + SEC XBRL + IR overrides) — reading the raw JSON again would mean
re-implementing the tier-ranking/override/restatement logic `timeseries.loaders` and
`provenance.overrides` already own, and would silently diverge from what every other
consumer (financials section, thesis evaluator, DCF) sees for the same period. The only
exception is Phase-3 valuation inputs (`MARKET_CAP`, `PRICE`) which have no
`financial_facts` equivalent today and are read directly from their cached JSON
(`historical_market_cap.json`, the EOD price cache) via a small reader in `io.py`.

## 6. Phasing

### Phase 1 — engine skeleton + ~15 unambiguous metrics + parity harness

Scope: `registry.py`/`inputs.py` (US-GAAP mapping only)/`engine.py`/`applicability.py`
skeletons; all 3 new tables + the `business_model_class`/`accounting_standard` columns
(both needed from day one — without them Phase 1 would compute nonsense like "Gross
Margin" for NU); the ~15 metrics with no method-variant ambiguity: `gross_margin`,
`operating_margin`, `net_margin`, `ebitda_margin`, `fcf_margin`, `revenue_yoy`,
`revenue_qoq`, `current_ratio`, `quick_ratio`, `cash_ratio`, `debt_to_equity`, `roe`,
`roa`, `eps_diluted_yoy`, `sbc_pct_revenue`. Explicitly **excludes** `net_debt_*`,
`roic_*`, and any other `alt_of`-grouped pair (those need the variant-surfacing
convention settled first, Phase 2). Includes the parity harness wired for exactly these
15 metrics.

**Migration-from-existing-code sub-task**: `fmp_derived_kpis.py`'s category-1
derivations (`KPI_OPERATING_MARGIN_GAAP`, `KPI_NET_MARGIN_GAAP`, `KPI_GROSS_MARGIN_GAAP`,
`KPI_FCF_MARGIN_GAAP`, `KPI_REVENUE_YOY_USD`, `KPI_ROE`) overlap this list by name.
Retire them in favor of the registry-driven equivalents **only after** a side-by-side
value diff proves parity (same pattern as the parity harness itself, run once
internally) — do this as an explicit, reviewed step, not a silent swap, since
`fmp_derived_kpis` rows are already read by existing consumers under their current string
names. Category-2 transforms (`derive_kpi_transforms`, the same-fiscal-quarter YoY-of-a-
level-series derivers) are a distinct concern layered on top of a base series and are
**out of scope** for this engine — they stay as-is.

Blast radius: 3 new tables + 2 new columns (all additive/nullable, zero risk to existing
rows), one new `src/compute/metrics_engine/` package, one new CLI, one new pytest file.
No existing renderer, UI surface, or `kpi_facts` consumer changes behavior — new rows are
additive under new `kpi_definitions` names until the explicit retirement step above.

### Phase 2 — full catalog incl. method-variant metrics + IFRS mappings

Scope: remaining metrics from §1 (`roic_strict`/`roic_lease_adjusted`, `roce`,
`net_debt_strict`/`net_debt_incl_lt_securities`, `net_debt_to_ebitda`,
`interest_coverage`, `asset_turnover`, `receivables_turnover`, `inventory_turnover`,
`cash_conversion_cycle`, `eps_adjusted_ex_sbc`, `bvps`, `fcf_per_share`,
`revenue_cagr_3y`, `ebitda_cagr_3y`); `IFRS_FIELD_MAP` populated per verified filer (NU,
BN, ASML, NVO — cross-checked against each ticker's actual `financial_facts.line_item`
rows, since the FMP normalization layer may already collapse some IFRS-specific fields
into the shared vocabulary and only the genuine gaps need an explicit IFRS mapping
entry); full `applicability.py` per-ticker override table populated (turnovers,
inventory); parity harness extended to the full catalog.

Blast radius: no new schema beyond Phase 1's; adds registry entries and IFRS mapping
rows only. Any concept still unmapped for an IFRS ticker at Phase 2 close produces
`not_computable: missing_input_mapping` (never a guess) and is a tracked backlog item,
not a blocker.

### Phase 3 — valuation metrics wired to live prices + platform surfacing

Scope: `MARKET_CAP`/`PRICE` readers in `io.py`; the 7 valuation formulas (`pe_ttm`,
`ps_ttm`, `pb`, `ev_ebitda`, `ev_sales`, `fcf_yield`, `earnings_yield`) plus
`enterprise_value_strict`; wiring computed metrics into consumer-facing surfaces —
`compute.kpi_resolver`/the DIY metric picker vocabulary (`viewspec.engine.metric_catalog`,
already the reuse point `compute/key_metrics.py` hits), the Ask engine's resolvable-metric
set, and any research-cockpit column that currently shows an FMP-sourced ratio directly.
Full parity coverage against FMP's `enterprise-values`/`ratios-ttm`/`key-metrics-ttm`.

Blast radius: first phase that touches rendered UI surfaces — any new chip/column must go
through `src/ui/controls.py` primitives per the repo's design-language gate
(`tests/test_ui_controls.py`), though the expected shape is reusing the existing KPI-chip
rendering path (numbers resolved through `kpi_facts` already render via existing chip
components), so no new component should be needed — verify against
`tests/test_ui_controls.py` regardless per the standing repo rule for any frontend
change.
