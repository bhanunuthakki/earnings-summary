# Capture Every Reported Number — program directive

**Status:** in-progress (program kicked off 2026-06-14)
**Owner:** bhanu · **Initiative note:** `project_capture_every_number`
**Companion directives:** `data_provenance.md`, `data_pipeline_dag.md`,
`provenance_override_2026_06.md` (fact_overrides), `dcf_damodaran_redesign` notes (DCF).

---

## 0. The ask (verbatim intent)

> Make IR-doc + 10-K/10-Q/8-K extraction robust so **every reported number** —
> including periodic investor-day decks and IR supplements — feeds the DB and is
> queryable via **ask**, via **raw DB query**, and selectable as a
> **company-specific DIY metric**. Make the DIY metric picker pull up **anything**
> from extracted facts (not just generic metrics). Add DIY buttons to **inject a
> picked fact into the company's DCF** as a model driver or a reference sheet.

---

## 1. The storage decision (LOCKED — do not re-litigate)

The storage substrate is **already universal**. This program is mostly *additive
extraction + UX*, not a schema redesign. Three facts establish this:

1. **`financial_facts.line_item` is free-form** — a plain `TEXT` column, no FK to
   any controlled vocabulary. Any string is a legal line item.
2. **`kpi_facts` auto-registers any name.** `src/pipeline/kpi_persistence.py`
   `persist_manifest` → `find_or_create_kpi_definition` (~`:129`) does a
   lookup-or-insert on `(ticker, name)` and **creates a `kpi_definitions` row for
   ANY name**. There is no allowlist *at the storage layer*.
3. **Anything in `kpi_facts` / `financial_facts` is auto-exposed downstream:**
   - **ask** grounding name-matches stored facts;
   - the **DIY `metric_catalog`** (`src/viewspec/engine.py:271`) reads the live
     fact tables per ticker (`kd.name` from `kpi_facts`, `line_item` from
     `financial_facts`, segment slices) — no static metric list;
   - the **DCF resolver** reaches a stored series via `src/timeseries/loaders`.

### Decision

- **`kpi_facts` (via `find_or_create_kpi_definition`) is the single capture-all
  target** for arbitrary reported numbers that aren't already first-class
  `financial_facts` line items. No new fact table.
- **`numerical_claims` is DEAD** (alembic 0040, zero writers). Do **not** route
  captured numbers there.
- **The `concepts` / `concept_aliases` spine (alembic 0036/0037) is NOT wired
  here.** Cross-ticker metric *identity* (one canonical concept for "NIM" across
  every bank) is a separate, deferred question — owned by **S8**, which decides
  whether/how to light it up. S1–S7 use **per-ticker surface canonicalization**
  only (see §2). Do not backfill `concept_id` columns in this program.

### The real blocker (what this program actually fixes)

The three **numeric extractors** are each handed a **fixed allowlist** — the
holdings `tier_*_kpis` — and silently drop everything else into prose:

| Extractor | Allowlist site |
|---|---|
| LLM-summary KPI extractor | `src/compute/kpi_extract_summaries.py:176` (`_tier_1_names`) |
| KPI-registry seeder | `execution/seed_kpi_definitions.py:172` |
| IR-spreadsheet config | `src/ir_pipeline/config_builder.py:115` |

Widening these to emit **every** labeled number — routed through a deterministic
canonicalizer so the registry doesn't explode with near-duplicates — is the core
of the program.

---

## 2. The canonicalizer (S1 — this session's primitive)

Capturing *every* number means the same metric arrives under many surface forms
across quarters and document kinds: `Monthly ARPAC`, `Monthly ARPAC (USD)`,
`monthly arpac`, `Monthly ARPAC (US$)`. Without control, capture-all would mint a
fresh `kpi_definitions` row for each, fragmenting the series and bloating the
picker.

`src/compute/kpi_resolver.py` already solved the *read* side of this
(`resolve_kpi_definition_name` — parenthetical-insensitive match + most-
observations defragmentation). S1 adds the **write-side** primitive:

```python
canonical_metric_name(conn, ticker, raw_label, unit) -> str
```

**Contract (what S3/S4 call):** given a freshly-extracted `(raw_label, unit)` for
`ticker`, return the `kpi_definitions.name` the value should be stored under —
either an **existing** definition's name (reuse → no duplicate) or a **cleaned new
name** (mint). The caller then passes the returned name straight to
`find_or_create_kpi_definition`. See the function docstring for the precise
matching rules; the design invariant is:

> **A false merge of two genuinely distinct metrics is worse than a duplicate.**

So matching is **conservative**: it collapses only *unit / casing / whitespace*
surface variants (a unit-only parenthetical like `(USD)`, a trailing `%`/`bps`),
**keeps semantic qualifiers** (`(gross)` vs `(net)`, `(annualized)`), and refuses
to merge across **incompatible unit families** (a dollar *level* never absorbs a
*rate* that shares a stem). The read-path resolver stays *more lenient* (it
unifies fragmented duplicates at query time) — the two paths are intentionally
asymmetric: split cautiously on write, unify generously on read.

**Origin marker.** `kpi_definitions.definition_origin` (`'analyst'` |
`'capture'`, migration 0113, default `'analyst'`) distinguishes the curated
holdings/tier KPI registry from the capture-all long tail. Set once at row
creation via `find_or_create_kpi_definition(..., origin=DefinitionOrigin.CAPTURE)`.
Lets S5 badge/filter captured metrics in the picker and lets audits scope the
long tail without touching the analyst watchlist.

---

## 3. The 8-session program map

One PR (or PR-stack) per session; one PR per phase; gate lint on **touched files
only**; push+merge when green. `data/`, `ir_documents/`, and
`micro_thesis/ir_config/` live in the **MAIN** checkout, not the worktree.

| S | Model | Size | Depends on | Objective |
|---|---|---|---|---|
| **S1** | opus | M | — | **(THIS)** Storage-decision directive (this file) + write-path `canonical_metric_name` over `kpi_resolver.py` (conservative dedup) + `definition_origin` marker (enum + migration 0113). **Foundation for S3/S4.** |
| **S2** | sonnet | S | — | Standalone correctness fix: `fact_overrides` are invisible to **ask** on both paths (`grounding.py` narrative; `loaders.py` `_with_provenance` skips `_overlay_scalar_series`), so ask answers stale FMP instead of the report's corrected figure. Ship early; independent of the canonicalizer. |
| **S3** | opus | XL | **S1** | **Capture-all extractor → `kpi_facts`.** Stage A: deterministic XBRL table walker (`table_extractors/base.py iter_xbrl_table`). Stage B: bounded LLM "enumerate every labeled number, no allowlist" in `kpi_extract_summaries.py`. Every emitted label routed through `canonical_metric_name`; defs stamped `origin='capture'`. Coverage audit; pilot 2–3 tickers before `--all`. Riskiest piece = label naming → leans hardest on S1. |
| **S4** | sonnet | L | **S1** | De-allowlist the **IR-spreadsheet** path (`ir_pipeline/config_builder.py` maps EVERY row, not just tier KPIs) + auto-build `ir_config` beyond NU. Route labels through `canonical_metric_name`. Guard against NU regression. |
| **S5** | sonnet | M | **S3** | **DIY picker** surfaces the long tail: lift `limit_per_domain=300` (`viewspec/engine.py:275`) + NL caps, add type-ahead search, UNION override-only facts, expose `origin` for badge/filter. |
| **S6** | opus | L | **S2 + S5** | **DIY→DCF DRIVER button.** Map a picked fact → `RedesignInputs` field → `apply_edits` (atomic / clobber-safe). Unit/scale converter (rates=decimal, capex=$M, multiples=turns) is load-bearing. Never write `over_under_pct`; WACC has no input cell. |
| **S7** | opus | L | **S6** | **DCF reference SHEET** that survives a rebuild (the workbook is rebuilt fresh each refresh; only the Dashboard is preserved). Prefer a separate companion workbook over surgical preservation. |
| **S8** | opus | exploration | — | Decide whether/how to wire the dormant `concepts` / `concept_aliases` spine (alembic 0036/0037) for **cross-ticker** metric identity → `directives/concepts_spine_decision.md`. Informs but does not block S1; S1's per-ticker canonicalizer is deliberately the *narrower* mechanism that S8 may later subsume. |

**Critical path:** S1 → S3 → S5 → S6 → S7.
**Parallelizable immediately:** S1 ∥ S2 ∥ S8. **After S1:** S3 ∥ S4.

**Owner decisions (locked):** DCF injection = **both** driver (S6) **and**
reference sheet (S7). Capture bar = **hybrid, staged** (deterministic
XBRL/spreadsheet tables first; bounded LLM prose/slide pass second; pilot before
`--all`). Concepts spine = **explored** in S8, not silently deferred.

---

## 4. Invariants for every session in this program

- **Capture-all writes land in `kpi_facts`** via `find_or_create_kpi_definition`.
  Never `numerical_claims`. Never restructure `kpi_facts`/`financial_facts`.
- **Every captured label is canonicalized** through `canonical_metric_name`
  before it becomes a `kpi_definitions.name`. A duplicate is an acceptable
  failure; a false merge is not.
- **Captured definitions carry `origin='capture'`.** The analyst watchlist
  (holdings tier KPIs, the 0007 seed) stays `origin='analyst'`.
- **Provenance is preserved per fact** (`source_doc_id`, `extracted_by`,
  `locator`) — the long tail is only useful if every number traces to its source.
- See `reference_platform_invariants` for the platform-wide dead-ends (FK
  poisoning, native-cache, the CI-gate trap) that bound all of this.
