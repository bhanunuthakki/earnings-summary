# Quarterly Segment / Detailed-Financials Framework — Design

**Status:** design only — no code in this session. Written for a Sonnet implementation
agent to execute phase-by-phase without re-deriving decisions.
**Trigger:** FMP Starter-plan migration. Quarterly raw statements = full access.
FMP's *derived* revenue-segmentation endpoint (`revenue-product-segmentation` /
`revenue-geographic-segmentation`) drops to **annual-only** going forward.
`financial-reports-json` (as-filed SEC Financial Report JSON, both 10-K and 10-Q) =
Full Access, 5-year window. Quarterly segments must therefore come from as-filed
10-Q JSONs + annual anchors, not the derived endpoint.
**Companion directives:** `directives/data_provenance.md` (provenance contract this
framework extends), `directives/capture_every_number_program.md` (sibling program;
this framework is segment-scoped, not a superset of it), `directives/per_ticker_segment_extraction_notes.md`
(per-ticker segment shape inventory — read before Phase 1 per-ticker work),
`directives/data_pipeline_dag.md`, `directives/llm_quota_scheduling.md`.

## 0. Grounding — what already exists (read this before touching anything)

This framework is **additive to a lot of existing machinery**, not a green field.
Get this wrong and you'll duplicate something that already works.

| Concern | Already exists at | Notes |
|---|---|---|
| Per-ticker filing-regime registry | `tracked_companies.filing_regime` (`src/models/companies.py::FilingRegime` = `10-K`/`20-F`/`40-F`), hand-backfilled for 28 names in `alembic/versions/0001_companies_provenance.py` | **No self-heal today** — new tickers land with `filing_regime=NULL`. §1 designs the self-heal. |
| Routing consumer | `src/pipeline/source_routing.py::plan_for_ticker` | Reads `filing_regime` + `instrument_type` today only to pick `SourceType` sets (fmp/ir_doc/sec_xbrl); doesn't yet branch *segment* pipeline choice. §1 extends it. |
| 10-Q JSON fetch | `execution/fetch_fmp_10q_json.py` → `data/historical/fmp/{T}_form_10q_{Y}_{Q}.json`, Q1–Q3 only (Q4 has no 10-Q) | Already wired to `fmp_endpoint_status`. Same `financial-reports-json` endpoint as the 10-K path, same section-keyed JSON shape. |
| 10-Q doc indexing | `src/pipeline/fmp_doc_index.py::_FORM_10Q_RX` → `doc_type='fmp_10q_json'` | Already classifies these files into `documents`. Nothing new needed here. |
| Section/row walker | `src/table_extractors/base.py::iter_xbrl_table`, `parse_units` | Shape-generic — same walker works on 10-Q sections verbatim, no 10-Q-specific parsing needed at the row/axis level. |
| 10-K capture-all (Stage A) | `src/table_extractors/generic_xbrl_capture.py` | **Deliberately 10-K-only** — its own docstring: a 10-Q section "interleaves duration columns (3/6/9-months-ended) that share an end-date, so the period axis can't be derived deterministically from the column label alone." §2 solves exactly this for the segment path (not for the general capture-all path — out of scope here, see §2.5). |
| 2-axis segment cross-tabs (10-K, LLM) | `src/compute/segment_crosstabs_llm.py` + `execution/extract_segment_crosstabs.py` | 10-K only. Writes via `write_segment_facts_junction`. Same LLM-contract pattern reused for the 10-Q Stage-B fallback (§2.4). |
| 1-axis FMP-derived segments (product/geo) | `src/compute/segments.py` reads `{T}_product_segments_quarterly.json` / `{T}_geo_segments_quarterly.json` (`doc_type` `fmp_segment_product`/`fmp_segment_geographic`), writes `dim_type=PRODUCT/GEOGRAPHY`, `metric=revenue_by_product/revenue_by_geography` | **This is the path that goes quiet post-Starter.** The new 10-Q pipeline's output contract must match this shape exactly so no downstream reader (DCF segment builder, dashboards) needs to change — see the Finding below. |
| Junction schema | `segment_periods` + `segment_dimensions` (migration `0055_segment_junction`, extended `0057_drop_segment_facts`) | Anchor+cell shape. `segment_dimensions` has `unit` (nullable override) + `segment_entity_id` from 0057 — **no** `confidence`/`extracted_by`/`locator`/`derived_from`/`supersedes_id`. See Finding 2. |
| Segment-name canonicalization | `execution/canonicalize_segments.py` (LLM) + `entity_store.py` | Maps raw `dim_name` → `segment_entity_id`. Only ~14% of rows mapped as of 0057. Q4/recast matching (§3) must tolerate this. |
| Recast/restatement chain pattern | `src/pipeline/restatement_detector.py` (`financial_facts`/`kpi_facts` only) | `supersedes_id` self-FK + `is_later_filing` (compares `documents.period_end` year, then `fetched_at`). §3/§4 port this pattern onto `segment_dimensions`, which has no such column today. |
| Confidence formula | `directives/data_provenance.md` §8, `src/pipeline/confidence.py::score_confidence` | `financial_facts`/`kpi_facts` already fully wired (tier + method + issue penalties + self-report). `segment_dimensions` has **no `confidence` column at all** — a genuine gap, not a parity nice-to-have. |
| Derived-fact lineage | `directives/data_provenance.md` §9, `kpi_facts.computed_from` (migration 0087) | Exact pattern to port onto `segment_dimensions` for Q4/Q2/Q3 derivation lineage (§4). |
| Capture-run coverage log (non-DB) | `src/pipeline/capture_coverage.py::CaptureCoverageRecord` / `record_coverage` | Good pattern for *run-level* tallies; **not** a queryable per-cell matrix — §5 needs a DB table instead, argued there. |
| FTS-trigger / batch_alter_table gotcha | `alembic/versions/0128_restore_analyst_notes_fts_triggers.py` | `segment_dimensions` itself carries **two non-FTS triggers** from `0057` (`trg_dirty_segment_dimensions_ins`, `trg_artifacts_dirty_on_segment_dimensions_insert`). The general footgun (`batch_alter_table` silently drops **all** triggers on the rebuilt table, not just FTS ones) applies to this table too. See §4.4. |
| `FiscalPeriodType` enum | `src/models/facts.py` — already has `Q1..Q4, H1, H2, FY, TTM` | **H1/H2 already exist** — the model already anticipated half-year cumulative periods. It's missing the 9-months-ended sibling (`9M`) for the Q3 filing's cumulative column. §4.2 adds it. |
| FMP profile → self-heal precedent | `src/pipeline/fmp_doc_index.py::set_instrument_type_from_fmp` (write-only-when-NULL) | Exact pattern to mirror for `filing_regime` self-heal (§1.3) — no such function exists for `filing_regime` today. |
| LLM purpose registration | `directives/journaling_thought_partner_2026_06.md` — "new purpose → 4 registries in lockstep" (`run_llm_evals.PURPOSES`, `evals_panel.RUNNABLE_PURPOSES`, `coverage`, `prompt_versions`) + `LLM_MODELS` in `src/llm/cli.py` + an `llm_budgets` seed row | Any new LLM purpose this framework introduces (§2.4, §2.5) must land in all of these, not just `LLM_MODELS`. |

### Finding 1 — legacy quarterly segment cache is a historical asset, not dead weight

`data/historical/fmp/MELI_product_segments_quarterly.json` and
`MELI_geo_segments_quarterly.json` **already exist in this repo** with quarterly
rows back to `2013 Q4` (confirmed by inspection during this design pass — MELI is
not an isolated case; any actively-tracked ticker fetched under the pre-Starter
plan tier will have the same shape). This is genuine quarterly data fetched under
whatever FMP plan predated the Starter downgrade. **Do not delete, backfill-overwrite,
or treat this file as a placeholder.** It remains the authoritative historical
anchor for every quarter it already covers. The new pipeline (§2–§4) only needs to
fill the **going-forward gap**: quarters reported *after* the plan transitioned to
annual-only granularity for that endpoint.

Practical consequence for Phase 1 (§6): the ingestion trigger for "does this ticker
need the new 10-Q route" is **not** a hardcoded transition date (FMP's plan change
isn't necessarily synchronized to our calendar) but an **empirical staleness check**:
after `execution/fetch_fmp_historical_data.py` refreshes
`{T}_product_segments_quarterly.json` / `{T}_geo_segments_quarterly.json`, if the
latest row's `period` field is `"FY"`-only for the two most recent fiscal years
where a quarterly row would be expected (i.e., the file stopped advancing on the
quarterly axis), the new 10-Q pipeline takes over as the primary quarterly writer
for periods after the last quarterly row already on file. This detection lives in
`execution/audit_segment_quarterly_coverage.py` (§5) as reason code
`legacy_endpoint_annual_only` and is what flips a ticker from "legacy path still
serving us" to "10-Q path required."

### Finding 2 — `segment_dimensions` is the young sibling schema; this framework brings it to `kpi_facts` parity

`kpi_facts` already carries `confidence`, `extracted_by`, `locator` (0075),
`computed_from` (0087), and `supersedes_id`/restatement chaining (0054 + the
restatement detector). `segment_periods`/`segment_dimensions` (migration 0055,
extended 0057) never got the same treatment — they only have `unit` and
`segment_entity_id` beyond the bare anchor/cell shape. **This framework is
substantially "backfill segment_* up to the provenance bar `kpi_facts` already
clears," not invention of new provenance concepts.** §4 mirrors existing,
already-shipped patterns column-for-column rather than designing new ones.

### Finding 3 — the period-axis problem is more tractable for segments than the docstring's own framing suggests

`generic_xbrl_capture.py`'s docstring treats 10-Q period-axis resolution as
underdetermined "from the column label alone." That's true for a *cold* 10-K-style
walker with no external context. But the **segment quarterly pipeline is never
cold**: `fetch_fmp_10q_json.py`'s filename (`{T}_form_10q_{Y}_{Q}.json`) already
tells us which nominal fiscal quarter this filing is *for*, and
`tracked_companies.fiscal_year_end` (populated by
`fmp_doc_index.set_fiscal_year_end_from_fmp`) gives the calendar to project expected
quarter-end dates from. Combined with parsing the duration prefix that
`generic_xbrl_capture._parse_period` currently **discards** (it only extracts the
trailing date, e.g. `"6 Months Ended June 30, 2025"` → `2025-06-30`, throwing away
the `"6 Months"` half), the axis is deterministically resolvable in the large
majority of cases. §2 is the concrete algorithm.

---

## 1. Source routing table

### 1.1 Per filing-regime routing

| Filing regime | Primary quarterly segment source | Fallback | Serving pipeline |
|---|---|---|---|
| `10-K` (US domestic — AMZN, GOOG, META, VEEV, NOW, RBRK, ABNB, SOFI, LMND, JPM, LLY, AMAT, FCX, WY, TOL) | As-filed 10-Q JSON (`financial-reports-json`, cached as `fmp_10q_json`) via the new deterministic period-resolver + segment extractor (§2, §4) | LLM Stage B over the same 10-Q JSON when the deterministic period axis can't be resolved (§2.4) | New: `src/compute/segment_quarterly_10q.py` + `execution/extract_segment_quarterly.py` |
| `20-F` (FPI — MELI, WIX, NVO, NU, ASML, BHP, RIO, VALE, HDB) | **Open question, Phase-3 spike required**: does `financial-reports-json` return anything for `period=Q1..Q3` for a 20-F filer? FPIs file 6-Ks, not 10-Qs; FMP's endpoint is documented against US-GAAP forms. Assume **no** until the spike proves otherwise. | (a) Legacy FMP quarterly cache as a frozen historical anchor (Finding 1) for periods already covered; (b) 6-K/IR-doc narrative extraction via an LLM read scoped to whatever 6-K exhibits get fetched — reuses the `segment_crosstabs_llm.py` prompt pattern against `ir_doc`-sourced 6-K text instead of a `fmp_10k_json` payload; (c) NU keeps its existing bespoke IR-spreadsheet KPI pipeline (`refresh_ir_kpis.py`) **untouched** — this framework does not replace it | Phase 3: new `src/compute/segment_quarterly_6k.py` (spike-gated) + existing NU IR pipeline unchanged |
| `40-F` (Canadian MJDS — BN, CNQ, FNV) | Quarterly **Supplemental Information PDF** (BN) / equivalent IR PDF, per `directives/per_ticker_segment_extraction_notes.md` — richer than the 40-F itself and almost certainly richer than anything `financial-reports-json` would return for a 40-F filer (same Phase-3 spike question as 20-F, lower expected yield since 40-F filers are the least US-GAAP-shaped of the three regimes) | IR-doc PDF extraction, reusing the `extract_kpis_from_ir.py` LLM-readout pattern, scoped to segment/asset-class tables instead of KPI lines | Phase 3: new `src/compute/segment_quarterly_ir_pdf.py` (BN-first; CNQ/FNV follow with per-ticker table shapes per the directive) |

**Design rule**: routing dispatch keys off `tracked_companies.filing_regime`
exclusively — never off `instrument_type` (an ADR can be `20-F` *or* `40-F`; BN is
`equity`/`40-F`, not `adr`) and never off ticker allowlists scattered across
scripts. Extend `src/pipeline/source_routing.py::SourcePlan` with one new field:

```python
class SourcePlan(BaseModel):
    ...
    segment_quarterly_pipeline: Literal["tenq_10k_regime", "fpi_6k", "mjds_ir_pdf", "unsupported"] | None
```

populated by a pure function `segment_pipeline_for_regime(filing_regime: FilingRegime | None) -> ...`
colocated in `source_routing.py` (10-K → `tenq_10k_regime`, 20-F → `fpi_6k`,
40-F → `mjds_ir_pdf`, `None`/unset → `unsupported`, logged not silently skipped).
Every orchestrator in §6 calls this instead of re-deriving the mapping.

### 1.2 Per-ticker registry — reuse, don't invent

`tracked_companies.filing_regime` is already the per-ticker registry (§0 table).
No new table. What's missing is self-heal for tickers onboarded after the 28-name
hand backfill.

### 1.3 Self-heal design

New function `set_filing_regime_from_profile()` in `src/pipeline/fmp_doc_index.py`
(sibling to the existing `set_instrument_type_from_fmp`, same signature shape,
same "write only when NULL" contract — never clobbers the hand-curated 0001
backfill):

1. **Primary signal (authoritative): SEC submissions.** If the ticker's CIK is
   resolvable (existing SEC-lookup helper — check `src/pipeline/` for a CIK
   resolver before writing a new one; `fetch_sec_xbrl.py` likely already resolves
   CIKs for the XBRL fetch path), fetch
   `https://data.sec.gov/submissions/CIK{cik:010d}.json` and inspect
   `filings.recent.form` for the most recent annual-report form value: presence of
   `"10-K"` → `FORM_10K`; `"20-F"` → `FORM_20F`; `"40-F"` → `FORM_40F`. This is
   ground truth, not a heuristic — prefer it whenever the CIK resolves and the
   submissions fetch succeeds.
2. **Fallback signal (best-effort): FMP profile heuristic.** When (1) fails
   (network, CIK not found, throttled), read `{T}_profile.json`
   (`classify_instrument_type_from_profile` already parses this file for
   `isAdr`/`isEtf`): if `country == "CA"` and `isAdr` → `FORM_40F`; elif `isAdr`
   → `FORM_20F`; else → `FORM_10K`. This heuristic conflates "Canadian ADR" with
   "MJDS 40-F filer," which is correct for the current roster (BN/CNQ/FNV) but is
   a genuine approximation — log which branch fired (`event: filing_regime_selfheal`,
   `method: sec_submissions|fmp_profile_heuristic`) so a future audit can tell a
   ground-truth classification from a guess.
3. Called from `execution/onboard_ticker.py` immediately after
   `set_instrument_type_from_fmp`, same call site, same idempotency (a re-run on an
   already-classified ticker is a no-op via the `WHERE filing_regime IS NULL` guard).
4. **Never** auto-corrects an existing non-NULL value, even if SEC submissions
   later disagrees with a hand-entered one — surfaces a `validation_issues` WARN
   row instead (mirrors `guard_llm_extracted_parent`'s degrade-not-clobber
   philosophy) so a human resolves the conflict.

---

## 2. 10-Q period-axis disambiguation

### 2.1 What context we already have (never re-derive blind)

For a given `{T}_form_10q_{Y}_{Q}.json`:
- **Nominal quarter** `Q ∈ {Q1, Q2, Q3}` — from the filename, already known before
  opening the file. (Q4 is never fetched this way — see §3.)
- **Fiscal-year-end** `(month, day)` — `tracked_companies.fiscal_year_end`, or (for
  a ticker missing it) the same modal-detection fallback
  `generic_xbrl_capture._detect_fye_monthday` already implements, run once against
  the ticker's most recent 10-K.
- **Non-calendar-FYE override table** — `directives/data_provenance.md` §2.1
  documents that *multiple* modules keep their own copy of a
  `_FYE_OFFSETS`/`_TICKER_QUARTER_PERIOD_END`-shaped table and they drift. **Do
  not add a fourth copy.** `grep -rn "_FYE_OFFSETS\|_TICKER_QUARTER_PERIOD_END"
  src/compute/` before writing this module and either import an existing table or,
  if none is import-safe from `table_extractors`, raise this as a blocking finding
  rather than forking a new copy silently.

### 2.2 Column-header parsing — the actual fix

`table_extractors/base.py::iter_xbrl_table` already yields `period_labels` per
column, unchanged. The fix is a **new, shared** function (does not touch
`generic_xbrl_capture.py`, since that module is explicitly out of scope/frozen —
"COMPLETE — record-only" per its companion directive):

```python
# src/table_extractors/period_axis.py  (new module)

@dataclass(slots=True)
class ParsedPeriodLabel:
    raw: str
    duration_months: int | None   # 3 / 6 / 9 / 12, or None if unparseable
    end_date: datetime | None

_DURATION_RX = re.compile(r"(\d{1,2})\s*Months?\s+Ended", re.IGNORECASE)

def parse_period_label(label: str) -> ParsedPeriodLabel:
    """Extract BOTH the duration-months prefix and the trailing end-date.
    generic_xbrl_capture._parse_period only extracts the trailing date and
    discards the duration prefix — that's the whole ambiguity this framework
    resolves. Reuses the same _DATE_FORMATS trailing-date logic (import, don't
    refork it — see the reuse-vs-duplicate note below)."""
```

Reuse note: `generic_xbrl_capture.py` currently keeps its scale/rate/count/
equity-ambiguity guards (`_SCALE_FACTOR`, `_RATE_TOKENS`, `_COUNT_TOKENS`,
`_EQUITY_HEAD_RX`, `_classify_value`, `_is_unit_ambiguous_section`, magnitude
ceilings) as **private, module-local** functions. The 10-Q segment extractor
(§2.3) needs the identical guards (same FMP JSON shape, same mis-scale traps).
This repo's general preference is "duplicate simple shared logic, don't
modularize" — but this is **not** simple logic: it's ~150 lines of hard-won,
subtly-tuned constants where a bugfix in one copy and not the other is exactly
the kind of silent drift the quality bar exists to prevent. **Recommendation**:
extract `_classify_value`, `_SCALE_FACTOR`, `_RATE_TOKENS`, `_COUNT_TOKENS`,
`_PER_SHARE_TOKENS`, `_is_unit_ambiguous_section`, `_EQUITY_HEAD_RX`, and
`_semantic_section_title`/`_build_name` into a new public module
`src/table_extractors/xbrl_value_classify.py`, and have both
`generic_xbrl_capture.py` and the new `segment_quarterly_10q.py` import from it.
Flag this explicitly to the implementing agent as an exception to the
duplicate-over-modularize default, not a silent judgment call.

### 2.3 Classification algorithm (deterministic path)

For each section that survives the same section-level guards as
`generic_xbrl_capture` (detail-section check, trustworthy scale, not
unit-ambiguous — reused from §2.2's shared module):

For each column `i`, `parse_period_label(period_labels[i])` →
`(duration_months, end_date)`. Compute, from `(Y, Q, fiscal_year_end)`:
- `current_end` — this quarter's expected end-date.
- `current_cumulative_months` — 3 for Q1, 6 for Q2, 9 for Q3.
- `prior_year_end` — `current_end` shifted back one fiscal year (same
  month/day).

Classify column `i`:

| Condition | Classification | Action |
|---|---|---|
| `duration_months == 3` and `end_date == current_end` | `CURRENT_DISCRETE` | Q1 only (its 3-month column *is* the discrete quarter). Persist directly, `period_basis='discrete'`. |
| `duration_months == current_cumulative_months` and `end_date == current_end` | `CURRENT_CUMULATIVE` | Persist as-filed at full fidelity (`period_basis='cumulative'`, `fiscal_period_type=H1` for Q2 / `9M` for Q3 — new enum value, §4.2). Then attempt discrete derivation (§3-style subtraction, one-hop). |
| `duration_months in {3, current_cumulative_months}` and `end_date == prior_year_end` | `PRIOR_YEAR_COMPARATIVE` | Persist as its own period anchor for the prior fiscal year/quarter **if** no anchor already exists for that logical key; if one **does** exist and the value disagrees beyond tolerance, this is a recast signal (§3) — resolve via the recast/supersede path, not a silent overwrite. |
| `duration_months is None` and `end_date == current_end` | `AMBIGUOUS_SAME_DATE` | Defer to LLM Stage B (§2.4). Log `skip("period_axis_ambiguous_no_duration_prefix")`. |
| `end_date` matches neither `current_end` nor `prior_year_end` | `OFF_CYCLE` | Skip, `skip("off_cycle_or_unparseable_period")` (same reason vocabulary as `generic_xbrl_capture`, deliberately — one shared skip-reason taxonomy across both capture passes makes the coverage log query-able uniformly). |

Q1 filings never need derivation — `CURRENT_DISCRETE` is already the answer. Q2/Q3
need the one-hop subtraction in §3 (same mechanism as Q4, just against `H1`/`9M`
instead of `FY`).

### 2.4 LLM Stage B fallback

Triggered when: (a) any column in a would-be-captured section lands
`AMBIGUOUS_SAME_DATE`, or (b) the section shape doesn't match the standard FMP
XBRL note-table structure at all (rare; a genuinely nonstandard filing). Reuses
`segment_crosstabs_llm.py`'s prompt-construction pattern (`_extract_relevant_text`,
same section-keyword filter) but with one addition: **the prompt is handed the
resolved period-shape hint** (nominal quarter, cumulative-months expectation, both
computed `current_end`/`prior_year_end` dates) and asked to return, per column, an
explicit `(duration_months, end_date, is_cumulative)` triple **alongside** the
values — i.e., the LLM's job is column-header English-prose disambiguation as a
named sub-task, not free-form number extraction. This matches the owner's
LLM-maximalist stance ("LLM where semantics beat keyword/regex") precisely at the
point where regex has already been tried and failed, not as a first resort.

New LLM purpose: `segment_10q_period_disambiguate`. Must be registered in **all
four** lockstep registries (`run_llm_evals.PURPOSES`, `evals_panel.RUNNABLE_PURPOSES`,
`coverage`, `prompt_versions`) plus `LLM_MODELS` (pin to the fast-classifier tier,
same as `canonicalize_segments`) plus a seed row in `llm_budgets` (mode: `skip` —
this is a batch pipeline, never block interactive use) — do not ship this as a bare
`call_llm` without the registrations; that's exactly the drift the 4-registry rule
exists to prevent.

### 2.5 Explicitly out of scope

Extending `generic_xbrl_capture.py` itself (the general capture-every-number
10-K walker) to run over 10-Qs is **not** part of this framework, even though the
period-axis resolver in §2.2–2.3 would technically unlock it. That program is
status COMPLETE / record-only per its directive. If a future session wants
10-Q capture-all, it should reuse `src/table_extractors/period_axis.py` as a new
dependency of a *new* capture pass — flag it, don't fold it in here.

### 2.6 Tolerance guard for derived discrete quarters

`derived = cumulative − prior_cumulative_component`, where the "prior component"
is either the already-persisted discrete Q1 (deriving Q2) or the sum of persisted
Q1+Q2 (deriving Q3). Guards, all **recorded, never silent**:

1. **Missing anchor.** If the matching prior discrete cell doesn't exist yet
   (Q1 wasn't captured, segment renamed with no canonical match — §3.1), do
   **not** derive. Write a `segment_quarterly_coverage` row (§5)
   `status='not_computable'`, `reason_code='missing_prior_anchor_for_subtraction'`.
   Never fabricate a value.
2. **Sign sanity.** A derived value that goes negative for a metric expected
   non-negative (revenue-shaped metrics) beyond a small rounding epsilon is
   **still persisted** (never silently dropped — the quality bar is explicit
   about this) but: confidence floors at `0.3`, and a `segment_quarterly_coverage`
   row `status='tolerance_breach'`, `reason_code='negative_derived_value'` is
   written alongside it so the audit matrix (§5) surfaces it.
3. **Confidence decays per derivation hop.** `confidence = min(input confidences)
   × 0.97^hops` where `hops` counts how many subtractions separate this cell from
   as-filed numbers (Q2/Q3 discrete = 1 hop; Q4 = up to 3 hops, since it nets out
   FY minus three already-once-derived quarters in the worst case). This makes a
   3-hop-removed Q4 segment number visibly less certain than a directly-reported
   H1 cumulative — the "silent method choice" failure mode the quality bar names
   explicitly is exactly a Q4 number rendered with the same confidence as a
   reported one.
4. **Cross-check against consolidated revenue** (not just segment-internal):
   compare `Σ segments' derived Q_n value` against the consolidated Q_n revenue
   already available from `financial_facts`/`kpi_facts` (itself possibly also
   FY−ΣQ derived at the consolidated level — check `compute/segments.py`'s
   existing `RECONCILE_TOLERANCE_OVER` convention and reuse the same tolerance
   band, currently 10%, for consistency rather than inventing a second threshold).
   Breach → `tolerance_breach` row, value still persisted.

---

## 3. Q4 derivation

`Q4_segment = FY_segment − (Q1_segment + Q2_segment + Q3_segment)`, applied
per matching key, at the segment-dimension level — same one-hop-subtraction
mechanism as §2.6, generalized to 4 inputs instead of 2 and reusing the exact
same guard set (missing-anchor, sign-sanity, confidence-decay,
cross-check-against-consolidated). This section is the additional matching-key
and recast logic specific to combining a 10-K annual anchor with three 10-Q
anchors from three separate documents.

### 3.1 Matching key

Segment cells across the four source documents (10-K FY + three 10-Qs) must be
joined on a **stable segment identity**, not literal string equality — names
drift ("International" → "North America excl. US" mid-year is a real pattern).
Matching key, in priority order:

1. `segment_entity_id` when non-NULL on **both** sides (the canonicalizer's
   mapping — reliable but sparse, ~14% coverage as of migration 0057).
2. `(dim_type, dim_name)` literal match, scoped per ticker, when (1) is
   unavailable on either side — the majority case today.
3. Neither matches → **new/renamed segment**. Do not force a match. Emit
   `segment_quarterly_coverage` `status='not_computable'`,
   `reason_code='unmatched_segment_identity'`. This is also the primary *signal*
   for recast detection (§3.2) — a segment appearing in the FY filing with no
   match among the three 10-Qs' segments (or vice versa) is the first evidence
   something changed mid-year.

### 3.2 Recast detection

**Trigger**: when ingesting a new filing (10-K or a later comparative-carrying
10-Q) for period `P`, if the **set** of matched segment identities for `P` in the
new filing differs from the set already stored for `P` from an earlier filing —
segments added, removed, or split/merged — that is a recast.

**Response** ("the latest filing's comparative columns are canonical," per the
task's own framing, and matching `restatement_detector.is_later_filing`'s
existing "later filing wins" rule generalized from scalar facts to segment
cells):

1. For every historical quarter whose *original* derivation used the
   now-superseded segment definitions, and that the new filing's comparative
   columns actually cover (10-Qs carry current + prior-year comparatives; 10-Ks
   carry FY + 1–2 prior years — recast propagation reaches exactly as far back as
   a single document's comparative window, no further), **re-derive** using the
   new filing's comparative-column values as inputs instead of the original
   filing's.
2. Chain the new derived `segment_dimensions` rows to the old ones via
   `supersedes_id` (§4.1) — mirroring `restatement_detector`'s pattern exactly:
   never delete/mutate the old row (data_provenance.md §2's "we never mutate, we
   add"), the new row points back at what it replaces, readers pick the head of
   the chain by default, time-travel queries remain possible.
3. `is_later_filing` from `restatement_detector.py` is already generic enough
   (compares `documents.period_end` year, then `fetched_at`) to reuse verbatim
   for this decision — do not write a parallel "is this segment filing newer"
   function.
4. **Bounded, not unbounded, propagation**: a recast whose effect reaches further
   back than any single filing's comparative window (e.g., 3+ fiscal years) is
   **out of algorithmic reach**. Record it: `status='not_computable'`,
   `reason_code='recast_beyond_comparative_window'`. Never guess a
   re-derivation from stale, pre-recast prior-quarter figures.

### 3.3 Tolerance policy

Same band as §2.6 point 4 (currently the existing `RECONCILE_TOLERANCE_OVER`
convention in `compute/segments.py`, ~10% relative) applied to
`Σ derived-Q4-per-segment` vs. the consolidated Q4 figure (itself likely also
FY−ΣQ derived at the whole-company level — verify against whatever module
already does that for `financial_facts` before assuming it needs deriving here
too). Breach → persist + `tolerance_breach` coverage row, never silently drop.

### 3.4 Provenance marking

Every Q4-derived `segment_dimensions` row carries:
- `disclosure_status='derived'`
- `derived_from` JSON (§4.1) listing all four input cells
  (`{ref: "segment_dimension", id, period_end, doc_id}` per input, `kpi_facts.computed_from`
  shape verbatim) — so `derived_from` fully reconstructs FY, Q1, Q2, Q3 lineage
  even if a later recast supersedes one of the inputs.
- `method_version` tagging the deriver's version string (e.g. `"segment_q4_derive_v1"`)
  so a future method change is distinguishable from a data change in the audit
  matrix.

---

## 4. Schema deltas

### 4.1 `segment_dimensions` — additive columns

All plain `op.add_column` (never `batch_alter_table` — see §4.4). All nullable
or safely defaulted so existing rows need no backfill logic beyond a default.

| Column | Type | Default | Purpose |
|---|---|---|---|
| `disclosure_status` | `String(16)` | `'reported'` | `'reported'` \| `'derived'`. (`'not_disclosed'`/`'not_applicable'` are deliberately **not** values here — see §4.3 rationale: a non-disclosure never gets a `segment_dimensions` row at all, since `value` stays `NOT NULL`.) |
| `method_version` | `String(32)`, nullable | `NULL` | e.g. `"tenq_discrete_v1"`, `"segment_q4_derive_v1"`, `"llm:segment_crosstabs_v1"`. Existing rows (from `compute/segments.py`, `segment_crosstabs_llm.py`) backfill to a one-time migration-time tag identifying the pre-framework writer, not `NULL` — a `NULL` method_version should mean "genuinely unknown," not "predates this migration." |
| `confidence` | `Numeric` (`REAL`), `NOT NULL` | `1.0` | Mirrors `financial_facts`/`kpi_facts.confidence`. Existing rows default `1.0` (they were reported-as-filed, deterministic parses — consistent with those tables' own historical default). |
| `extracted_by` | `String(64)`, nullable | `NULL` (backfilled per §above) | Deterministic tag or `"llm:<model>"`, same convention as `kpi_facts.extracted_by`. |
| `locator` | `TEXT` (JSON), nullable | `NULL` | `FactLocator`-shaped (`section`, `json_path`) pointing into the source 10-Q/10-K JSON. Serialize via the **existing** `models.facts.FactLocator.to_json()` — do not invent a second locator shape for segments. |
| `derived_from` | `TEXT` (JSON), nullable | `NULL` | Exact shape of `kpi_facts.computed_from` (`directives/data_provenance.md` §9): `{"display": "...", "inputs": [{"ref": "segment_dimension", "id": .., "period_end": .., "doc_id": .., "tier": ..}, ...]}`. |
| `supersedes_id` | `Integer`, nullable, self-`ForeignKey("segment_dimensions.id")` | `NULL` | Recast chain link, mirrors `financial_facts`/`kpi_facts.supersedes_id`. |

### 4.2 `segment_periods` — additive columns + one enum extension

| Column | Type | Default | Purpose |
|---|---|---|---|
| `period_basis` | `String(16)`, `NOT NULL` | `'discrete'` | `'discrete'` \| `'cumulative'` \| `'derived'`. Existing rows (all reported-discrete or FY-annual today) default `'discrete'`; a one-time data migration sets `'cumulative'`... **there are no existing cumulative rows** (H1/H2/9M never used before this framework), so no backfill ambiguity — the default only ever applies to genuinely-discrete legacy rows. |
| `raw_period_label` | `TEXT`, nullable | `NULL` | The exact as-filed column header (e.g. `"Six Months Ended June 30, 2025"`) — audit trail back to the literal filing text, independent of the parsed `period_end`/`fiscal_period_type`. |
| `method_version` | `String(32)`, nullable | `NULL` | Resolver version, e.g. `"period_axis_v1"`. |

**Model change (not a migration — a Python enum addition):**
`src/models/facts.py::FiscalPeriodType` gains `NINE_MONTHS = "9M"`, sibling to the
already-existing `H1`/`H2`. This is required, not optional: without it, a Q3
10-Q's as-filed 9-months-ended cumulative column and its derived discrete-Q3
value would both want `fiscal_period_type='Q3'` on the **same** `(ticker,
period_end, fiscal_period_type, source_doc_id)` tuple (same source document,
same period-end date) — a direct collision against
`uq_segment_periods_provenance`. `H1`/`H2` already establish this exact pattern
for Q2; `9M` is the missing sibling for Q3, not a new concept.

**Blast-radius flag**: `FiscalPeriodType` is a plain `StrEnum` stored as free
`TEXT`, so adding a member is source-safe by construction — but **any exhaustive
`if/elif`/`match` over `FiscalPeriodType` elsewhere in the codebase that doesn't
have a fallthrough `else` will silently mis-handle (or drop) a `9M` row.** Before
merging: `grep -rn "FiscalPeriodType\." src/ execution/ | grep -v "\.py:.*#"` and
manually check every exhaustive branch (rendering code, DCF segment coverage,
any `match FiscalPeriodType` block) for a safe default arm. This is exactly the
kind of change the codebase's general "non-exhaustive match" footgun applies to.

### 4.3 New table — `segment_quarterly_coverage`

Rationale for a **new DB table** rather than extending
`pipeline/capture_coverage.py`'s JSON log: that log is a good fit for *run-level*
tallies (seen/captured/skip-histogram per run) but is not queryable at
per-(ticker, quarter, segment) cell granularity, and the task's own ask is
explicitly a "per-ticker × per-quarter coverage matrix" — that needs stable,
indexed rows, not a JSON blob rolled up post-hoc. It's also the mechanism that
makes `not_disclosed`/`not_applicable` a **first-class recorded outcome** without
loosening `segment_dimensions.value`'s `NOT NULL` constraint (which would force a
`batch_alter_table` rebuild and the trigger-drop risk in §4.4, for a marginal
gain — cleaner to keep `segment_dimensions` strictly "cells that have a numeric
value" and let this table carry "things we looked for and didn't get").

```sql
CREATE TABLE segment_quarterly_coverage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker VARCHAR(16) NOT NULL,
    period_end DATETIME NOT NULL,
    fiscal_period_type VARCHAR(8) NOT NULL,
    dim_type VARCHAR(16),              -- NULL = whole-filing-level gap
    dim_name VARCHAR(128),             -- NULL = whole-filing-level gap
    status VARCHAR(24) NOT NULL,       -- reported | derived | not_disclosed |
                                       -- not_applicable | unresolved_period_axis |
                                       -- source_missing | tolerance_breach |
                                       -- not_computable
    reason_code VARCHAR(64),
    source_doc_id INTEGER REFERENCES documents(id),
    method_version VARCHAR(32),
    checked_at DATETIME NOT NULL,
    UNIQUE (ticker, period_end, fiscal_period_type, dim_type, dim_name, method_version)
);
CREATE INDEX ix_segment_qcov_ticker_period ON segment_quarterly_coverage (ticker, period_end);
```

The `UNIQUE` constraint is the upsert key: re-running the audit (§5) for the same
ticker/period/segment/method-version updates `status`/`reason_code` in place
rather than accumulating duplicate rows across every audit run.

### 4.4 FTS-trigger / `batch_alter_table` gotcha — explicit guidance

`segment_dimensions` already carries two triggers from migration `0057`
(`trg_dirty_segment_dimensions_ins`, `trg_artifacts_dirty_on_segment_dimensions_insert`).
Neither is FTS-related — the known gotcha (`0128_restore_analyst_notes_fts_triggers.py`)
is specifically about `analyst_notes`'s FTS triggers, but the **general SQLite
behavior it demonstrates is not FTS-specific**: `batch_alter_table` rebuilds the
table (`CREATE TABLE ... AS SELECT ...` + rename), and SQLite drops **every**
trigger on the original table as a side effect, silently, regardless of what kind
of trigger it is.

**Rule for every migration in this framework**: §4.1/§4.2's additive columns use
plain `op.add_column` (SQLite's native `ALTER TABLE ... ADD COLUMN` — does not
rebuild the table, does not touch triggers). **Never** wrap them in
`batch_alter_table` "for convenience" or "to batch several ALTERs" — that would
silently drop `trg_dirty_segment_dimensions_ins` and
`trg_artifacts_dirty_on_segment_dimensions_insert`, breaking `brief_dirty` /
`llm_artifacts.dirty` invalidation for every future segment write with no error
anywhere. If a **future** migration genuinely needs `batch_alter_table` on
`segment_dimensions` (e.g., to drop a column, add a real FK, or change a type),
it **must** re-create both triggers verbatim (copy the `CREATE TRIGGER` bodies
from `0057_drop_segment_facts.py` lines ~168–199) in the same migration's
`upgrade()`, exactly as `0128` restored `analyst_notes`'s FTS triggers after a
prior migration dropped them.

### 4.5 `kpi_facts` — no changes needed

Called out explicitly because the task asked "what (if anything) ... `kpi_facts`
need": **nothing**. `kpi_facts` already has `confidence`, `extracted_by`,
`locator`, `computed_from`, and restatement chaining. This framework's schema
work is entirely `segment_periods`/`segment_dimensions` catching up to a bar
`kpi_facts` already clears (Finding 2). If a future extension of this framework
starts writing arbitrary detailed-financials line items (not segment cells) out
of 10-Q note tables, that's `generic_xbrl_capture`-shaped work and lands in
`kpi_facts` via the existing `persist_manifest` contract unchanged — explicitly
out of scope here (§2.5).

---

## 5. Pipeline plan

### 5.1 New / modified `execution/` scripts

| Script | Role | CLI contract | Idempotency key |
|---|---|---|---|
| `src/table_extractors/period_axis.py` (new) | Pure period-label parser/classifier, no I/O (§2.2–2.3) | n/a (library) | n/a |
| `src/table_extractors/xbrl_value_classify.py` (new, extracted from `generic_xbrl_capture.py`) | Shared scale/rate/count/equity guards (§2.2 reuse note) | n/a (library) | n/a |
| `src/compute/segment_quarterly_10q.py` (new) | Deterministic Stage-A 10-Q segment extractor (10-K regime) + LLM Stage-B fallback dispatch | `extract_for_ticker(ticker, year, quarter, repo_root, conn, refresh=False) -> SegmentQuarterlyResult` | `(ticker, year, quarter, source_sha256)` — cache at `data/segment_quarterly/{T}_{Y}_{Q}.json`, same cache-invalidate-on-sha256-change pattern as `segment_crosstabs_llm.py` |
| `execution/extract_segment_quarterly.py` (new) | Orchestrator CLI, one process per invocation, loops tickers × available Q1–Q3 files | `python execution/extract_segment_quarterly.py --ticker MELI [--year 2026] [--quarter Q2] \| --all [--refresh]` | Same run_id/`ingestion_runs` + `stage_transitions` wiring as `extract_segment_crosstabs.py` (`record_stage(..., StageName.COMPUTE, ...)`) |
| `src/compute/segment_q4_derive.py` (new) | Q4 = FY − ΣQ1Q3 at segment level, matching + recast (§3) | `derive_for_ticker(ticker, fiscal_year, repo_root, conn) -> Q4DeriveResult` | `(ticker, fiscal_year, {input source_doc_ids})` — re-running with the same four input documents is a no-op; a changed input (recast, §3.2) re-derives and chains via `supersedes_id` |
| `execution/derive_q4_segments.py` (new) | Orchestrator CLI for the above | `python execution/derive_q4_segments.py --ticker MELI [--year 2025] \| --all` | Same run-accounting wiring |
| `src/pipeline/fmp_doc_index.py` (modified) | Add `set_filing_regime_from_profile()` (§1.3) | n/a (library function, called from `onboard_ticker.py`) | Write-once (NULL-guarded) |
| `src/pipeline/source_routing.py` (modified) | Add `segment_quarterly_pipeline` field + `segment_pipeline_for_regime()` (§1.1) | n/a (library) | n/a |
| `execution/audit_segment_quarterly_coverage.py` (new) | Per-ticker × per-quarter coverage matrix, reason codes, upserts `segment_quarterly_coverage` (§5.2) | `python execution/audit_segment_quarterly_coverage.py [--tickers T1,T2] [--since-year 2022]` | Upsert on the table's own `UNIQUE` key (§4.3) — safe to re-run any time, always reflects current DB state |

Note on "extend `audit_segment_coverage.py`": the existing script's audit target
(a DCF-build coverage ratio, driven by subprocess-running `build_redesigned_dcf.py`
and parsing its stdout) is mechanically unrelated to a per-quarter presence
matrix. Rather than overload that script's loop, the "extension" takes the form
of a **new sibling script sharing the naming convention**
(`audit_segment_quarterly_coverage.py`), cross-referenced from
`audit_segment_coverage.py`'s docstring so a future reader finds both. Do not
literally add new CLI modes to the existing file — its subprocess-per-ticker
architecture and this table-upsert architecture don't share enough machinery to
justify forcing them into one entrypoint.

### 5.2 Coverage matrix contract

`audit_segment_quarterly_coverage.py`, per (ticker, fiscal_year):
1. Determine expected quarters from `financial_reports_dates` (already cached
   per ticker, §0/Finding — this file enumerates every `{FY, period}` FMP has a
   `linkJson` for, i.e., the ground-truth roster of filings that should exist)
   intersected with `segment_pipeline_for_regime(filing_regime)`.
2. For each expected `(period_end, fiscal_period_type)`, check:
   - Does a `segment_periods` row exist with the matching key? → `status='reported'`
     or `'derived'` per its `period_basis`.
   - Does `segment_quarterly_coverage` already carry a `not_disclosed` /
     `not_applicable` / `not_computable` row for it (written by the extractor
     itself when it explicitly determined non-disclosure, e.g. NOW/RBRK/LMND's
     single-reportable-segment tickers — every quarter is legitimately
     `not_applicable`, not a gap)? → surface as-is.
   - Neither → `status='source_missing'`, `reason_code` one of
     `no_10q_json_fetched` / `legacy_endpoint_annual_only` (Finding 1) /
     `unresolved_period_axis` / `fpi_route_unproven` (20-F/40-F Phase-3 spike
     pending).
3. Print (and optionally persist to `.tmp/`, per the repo's >2000-line /
   >100KB output rule) a ticker × quarter grid, one row per ticker, one column
   per (fiscal_year, quarter), cell = status/reason_code — the eval-loop
   consumable artifact.

### 5.3 Cadence — post-earnings trigger

Reuse `execution/schedule_pre_earnings_refresh.py`'s existing `forced_stale.json`
hint mechanism (`.tmp/cacher/forced_stale.json`) — **do not build a second
scheduling primitive**. Add the new 10-Q fetch + extraction step to whatever
consumes those hints today (the cacher's daily audit): when a ticker's hint fires
post-print, after `fetch_fmp_10q_json.py` (existing) refreshes that ticker's
current-quarter file, chain `extract_segment_quarterly.py --ticker T --year Y
--quarter Q` for the just-reported quarter, then (only if `Q == Q4`... but Q4
never has a 10-Q — so instead: after the FY 10-K lands) chain
`derive_q4_segments.py --ticker T --year Y`.

**Quota-window compliance** (`directives/llm_quota_scheduling.md`): the LLM
Stage-B fallback (§2.4) and the 6-K/IR-doc LLM path (§1.1, FPI/MJDS) both burn the
shared `claude` CLI quota. New scheduled legs from this framework must follow the
existing per-item degrade pattern (transient CLI failure → defer + tally + retry
next run; hard stops loud) and must **not** run inside the protected windows
(04:00 morning pipeline, 03:00 on the 1st, Sun ~10:30 weekly evals) — register the
new post-earnings segment-extraction leg's typical run window in
`directives/llm_quota_scheduling.md` once implemented, per that directive's own
registration rule.

### 5.4 Audit / eval loop

- **Coverage**: §5.2's matrix, run on-demand and after every batch fetch.
- **Recast audit**: a lightweight companion query (not a new script) —
  `SELECT * FROM segment_dimensions WHERE supersedes_id IS NOT NULL ORDER BY id DESC`
  surfaces every recast chain link for spot review, mirroring how
  `restatement_detector`'s chains are inspected today (no dedicated UI needed for
  Phase 1/2 — a raw query is enough at this data volume).
- **Tolerance-breach review**: `segment_quarterly_coverage WHERE status =
  'tolerance_breach'` is the actionable backlog — surfaced the same way
  `validation_issues` rows are today (System → Provenance, or a future panel;
  not designed here, out of scope for a backend framework doc).

---

## 6. Phasing

### Phase 1 — MELI-class... (correction: 10-K-regime domestic filers), deterministic + LLM fallback

**Scope**: §2 (period-axis resolver + shared classify module), §4.1/4.2/4.3
migrations, `segment_quarterly_10q.py` + `extract_segment_quarterly.py` for
`filing_regime='10-K'` tickers only (AMZN, GOOG, META, VEEV, NOW, RBRK, ABNB,
SOFI, LMND, JPM, LLY, AMAT, FCX, WY, TOL — the roster already has 10-K segment
extraction precedent per `per_ticker_segment_extraction_notes.md`, so the
10-Q version is "the same segment shapes, one axis harder"). LLM Stage-B
fallback (§2.4) included since the deterministic path won't clear 100% on day
one and the quality bar forbids silent drops.

**Not in Phase 1**: Q4 derivation (Phase 2), recast handling (Phase 2), FPI/40-F
routes (Phase 3), the `9M` enum addition is needed in Phase 1 already (Q3 10-Qs
appear in Phase 1's own roster) — call this out so it isn't deferred by mistake.

**Blast radius**: ~6 new files (`period_axis.py`, `xbrl_value_classify.py`,
`segment_quarterly_10q.py`, `extract_segment_quarterly.py`, plus the
`source_routing.py`/`fmp_doc_index.py` additions are edits not new files) + 1
edit to `generic_xbrl_capture.py`'s imports (switch to the extracted shared
module, behavior-preserving) + 3 migrations (`segment_dimensions` additive
columns, `segment_periods` additive columns, `9M` enum addition is a
non-migration Python change) + `models/facts.py` one-line enum edit + LLM
4-registry additions for `segment_10q_period_disambiguate`.

### Phase 2 — Q4 derivation + recasts

**Scope**: §3 in full, §4.3 (`segment_quarterly_coverage` table — could land in
Phase 1 instead if the coverage matrix is wanted earlier for Phase 1 QA; listed
here because Q4/recast is where `not_computable`/`tolerance_breach` statuses get
real exercise), `segment_q4_derive.py` + `derive_q4_segments.py`,
`audit_segment_quarterly_coverage.py`.

**Blast radius**: ~3 new files + 1 migration (the coverage table, if not already
landed in Phase 1) + 0 further model/enum changes (Phase 1's `9M` addition and
`derived_from`/`supersedes_id` columns already cover what Phase 2 needs).

### Phase 3 — FPI (20-F) / MJDS (40-F) route

**Scope**: §1.1's Phase-3 spike (does `financial-reports-json` return anything
useful for `period=Q1..Q3` on a 20-F/40-F filer — a half-day empirical check,
**do this first**, before writing any extraction code, since it determines
whether §1.1's "primary source" column is real or the fallback is actually
primary), then whichever of `segment_quarterly_6k.py` /
`segment_quarterly_ir_pdf.py` the spike result calls for. BN's Supplemental PDF
route and NU's untouched IR-spreadsheet pipeline are the two most concrete,
already-scoped sub-paths (per `per_ticker_segment_extraction_notes.md`); ASML/
BHP/RIO/VALE/HDB/WIX/NVO/CNQ/FNV follow per-ticker per that same directive's
existing inventory (do not re-derive their per-ticker shape notes — they're
already written down).

**Blast radius**: highest uncertainty of the three phases — contingent on the
spike result. Rough estimate assuming the spike is negative (financial-reports-json
has nothing for 20-F/40-F, the expected outcome): 2 new extraction modules + 1
new IR-doc fetch/categorization wiring for BN's Supplemental PDF specifically
(reusing `extract_kpis_from_ir.py`'s pattern, not the financial-reports-json path
at all) + 0 new migrations (reuses Phase 1/2's schema — an IR-doc-sourced segment
cell is still a `segment_dimensions` row with `source_doc_id` pointing at an
`ir_doc` document instead of an `fmp_10q_json` one) + LLM 4-registry additions for
whatever new purpose(s) the 6-K/IR-PDF prompts need.

---

## 7. Open risks / unresolved questions for the implementing agent

1. **Phase-3 spike is a hard prerequisite, not an optimization.** §1.1's FPI/MJDS
   routing is written as best-guess; do not build extraction code against it
   before confirming empirically whether `financial-reports-json` returns
   anything for `period=Q1..Q3` given a 20-F/40-F filer's symbol. A wasted
   extractor built against an endpoint that returns nothing is the single
   biggest wasted-effort risk in this plan.
2. **CIK resolver reuse** (§1.3) — this design assumes `fetch_sec_xbrl.py` or a
   sibling module already resolves ticker→CIK; verify before writing a new
   resolver. If none exists, that's a small but real addition to Phase 1's
   blast radius.
3. **`_FYE_OFFSETS`-style table reuse** (§2.1) — `data_provenance.md` §2.1
   already documents drift risk across *existing* copies of this table; adding
   a period-axis resolver that needs the same calendar is exactly the kind of
   change that directive warns about. Resolve by import, not by forking, or
   flag the conflict rather than quietly forking a fourth copy.
4. **Confidence-decay constant (`0.97^hops`, §2.6)** is a reasonable placeholder,
   not a calibrated number — no historical data exists yet to calibrate against
   (this is a brand-new derivation path). Revisit once Phase 2 has produced a
   population of derived Q4 values to sanity-check against actually-reported
   figures where they later become available (e.g., a company that later
   discloses a true quarterly segment breakdown in an investor deck, providing
   a natural check against the derived figure).
5. **Legacy quarterly cache freshness detection (Finding 1)** assumes
   `fetch_fmp_historical_data.py` continues to be run periodically against the
   now-restricted endpoint so the "did it go annual-only" signal is observable;
   if that fetch is ever skipped/disabled for cost reasons post-Starter, the
   `legacy_endpoint_annual_only` detection in §5.2 goes stale and under-reports
   the gap. Worth a one-line check in the audit script: also flag when the
   legacy file's own `mtime`/last-fetch timestamp is older than N days, as a
   distinct reason code, so a stale-detector-itself doesn't masquerade as
   "still covered."
