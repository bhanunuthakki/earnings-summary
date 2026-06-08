# Annual-cadence KPIs — design

First-class support for KPIs an issuer reports **only annually** (bank regulatory
capital ratios, other 10-K/20-F-only disclosures) so they stop being shoehorned
onto a quarterly axis. Proof case: **Nu Holdings (NU) capital adequacy ratio**
(`kpi_definitions` id **636**, a `tier_1_break` rule), disclosed once a year in
the 20-F (Nu Pagamentos prudential conglomerate CAR): Dec'23 **13.7**, Dec'24
**18.1**, Dec'25 **16.6**.

## Why the workaround is lossy (verified)

- `kpi_facts.fiscal_period_type` is Q1–Q4 (plus H1/H2/FY/TTM in the enum, but
  KPI facts only ever used the quarterly buckets). The CAR year-end values were
  backfilled onto **Q4** period_ends (`2023-12-31`, `2024-12-31`, `2025-12-31`),
  with a few genuine interim prints on Q1/Q2/Q3 (`12`, `11`, `15.8`, `16.9`).
- `financials._kpi_series_for` filters `fiscal_period_type IN ('Q1'..'Q4')` and
  aligns to the 12-quarter label axis → CAR renders as a gappy quarterly series,
  9 of 12 cells empty; its YoY heatmap (`workspace_html._kpi_series_yoy_panel`)
  is mostly blank.
- `thesis_evaluator._fetch_kpi_history` pulls the most-recent `consecutive_periods`
  rows across **all** period types — for an annual metric "2 consecutive periods"
  must mean **2 years**, not "the last 2 rows" (which could mix a year-end value
  with an interim print).
- The bank DCF (`build_bank_dcf.load_kpis`) takes latest-by-period_end; works by
  luck post-conversion but isn't cadence-aware.

## Decision: cadence is a property of the **definition**; annual facts use **`FY`**

Two complementary changes (the request's options (a) + (b)), each minimal:

1. **(b) `kpi_definitions.reporting_cadence`** — new `TEXT NOT NULL DEFAULT
   'quarterly'` column, `CHECK IN ('quarterly','annual','ttm')`. This is the
   authoritative declaration of a KPI's native frequency. Everything downstream
   (rendering, break-rule period counting, DCF read, extraction) keys off it.
   `'ttm'` is admitted to the vocabulary now for forward-compat but behaves like
   quarterly until a consumer special-cases it.

2. **(a) annual facts tagged `fiscal_period_type='FY'`** — reuse the **existing**
   `FiscalPeriodType.FY` enum value (no enum change needed) with `period_end` =
   fiscal-year-end (Dec-31 for NU). This mirrors how annual *financials* already
   work (`financials._load_annual` reads `fiscal_period_type IN ('FY','annual')`),
   so the annual KPI axis is consistent with the annual line-item axis.

Why both: the column tells the *system* a KPI is annual (so it never renders the
gappy quarterly series); the `FY` tag tells each *fact* it's a year-end value (so
the annual series, the break-rule history, and the DCF read all select the right
rows). Cadence on the definition alone can't distinguish a year-end CAR print
from an interim one; `FY` on the fact alone wouldn't tell the renderer to switch
axes. Together they're unambiguous.

### Mixed-cadence history

NU CAR has 3 authoritative year-end values **and** 4 genuine interim prints. The
year-end rows (currently `Q4`) are **re-tagged `FY`**; the interim prints stay as
their quarterly rows (preserved, not deleted). The annual series, the §2 ledger,
and the break rule read **`FY` only**, so CAR renders as a clean 3-point annual
series and the interims remain in `kpi_facts` for audit/provenance without
polluting the annual view. (Constant `ANNUAL_FACT_PERIOD_TYPES = ('FY','annual')`
matches the financials annual set.)

## Touch points

| Area | File | Change |
|---|---|---|
| Model | `src/models/kpis.py` | `ReportingCadence` StrEnum; `KpiDefinition.reporting_cadence` |
| Migration | `alembic/versions/0072_kpi_reporting_cadence.py` | add column + named CHECK (down_revision `0071`) |
| Resolver | `src/compute/kpi_resolver.py` | `ANNUAL_FACT_PERIOD_TYPES`; `reporting_cadence_for(conn, ticker, name)` |
| Persistence | `src/pipeline/kpi_persistence.py` | thread `reporting_cadence` through manifest + `find_or_create_kpi_definition` (cadence-aware, authoritative like `canonical_units`) |
| Break rules | `src/compute/thesis_evaluator.py` | `_fetch_kpi_history` annual → `FY` rows only; `consecutive_periods` counts years |
| §3 render | `src/report/sections/financials.py` + `src/report/models.py` | `_resolve_priorities` splits cadence; `_annual_kpi_series_for`; new `AnnualKpiSeries` + `FinancialsSection.annual_kpi_chart_series`/`annual_kpi_years` |
| §3 render | `src/report/renderers/workspace_html.py` | `_annual_kpi_series_yoy_panel` (annual axis); wire into financials tab |
| §3 render | `src/report/renderers/charts_v2.py` | `yoy_heatmap_table` gains `period_stride`/`periods_per_year` (defaults preserve quarterly behaviour) |
| §2 ledger | `src/report/sections/thesis.py` | `_kpi_history_conn` annual → `FY` rows only |
| DCF | `execution/build_bank_dcf.py` | cadence-aware latest-annual CAR read; align CAR guardrail to holdings break |
| Onboarding | `execution/mark_kpi_cadence.py` (new) | idempotent CLI to mark a def annual + re-tag year-end rows |

## Proof / execution

1. alembic `0072` on a prod-copy worktree DB.
2. `mark_kpi_cadence.py --ticker NU --kpi "Capital adequacy ratio" --cadence annual`
   → def 636 `reporting_cadence='annual'`, unit `ratio→percent`, 3 year-end rows
   `Q4→FY`.
3. `run_thesis_evaluator.py --ticker NU` → CAR break rule evaluates on FY rows.
4. `build_artifacts.py --ticker NU --renderer workspace --flavor portfolio` →
   workspace HTML shows a clean **annual** CAR series; bank DCF guardrail reads
   the latest annual CAR.
5. Back up + migrate the prod DB; run the same conversion there.

Gate: ruff + whole-file `ruff format --check` + pyright (strict) on touched files
only. Datetimes naive-UTC. Cast at JSON boundaries.
