# Provenance Override — company-published docs supersede FMP

**Status:** design / in-progress (spawned 2026-06-14 from the GOOG segment-fix session)
**Owner:** bhanu · **Initiative note:** `project_fmp_provenance_override`
**Companion directive:** `directives/data_provenance.md` (this adds §9 — Overrides)

---

## 1. Problem

FMP is a *convenience* tier, not authoritative. When a company-published document
(SEC 8-K / 10-Q / 10-K, earnings press release, IR deck / supplement) reports a value
for a metric, that value should **systematically supersede** FMP's cached/derived value
for the same logical fact — keyed by `(ticker, period_end, fiscal_period_type,
line_item / segment / KPI)`. Today this only happens **ad-hoc**: manual raw-cache edits
plus one-off `llm_extracted` `kpi_facts`. Neither is durable.

### Motivating case (real — #592 / #593, 2026-06-14)

FMP's `revenue-product-segmentation` cache served a **corrupt GOOG Q4'25 (2025-12-31)**
record: segment-sum ≈ 1.38× reported revenue, Google Cloud at **$20,941M / +75% YoY**
vs the SEC 8-K's **$17,664M / +48%**, plus inflated YouTube ads, a spurious "Google Inc."
segment, and a missing Subscriptions line. It was corrected **manually** against the 8-K
(accession `0001652044-26-000012`, exhibit `googexhibit991q42025.htm`; corrected leaf-sum
$113,828M). A reconciliation scanner + a quarterly-refresh `VALIDATE_SEGMENT_CACHE` gate
were added.

**But the raw-cache fix is not durable.** A `save_fmp_data` / `fetch_fmp_historical_data`
re-fetch overwrites `data/historical/fmp/GOOG_product_segments_quarterly.json` and
re-introduces FMP's bad source data. The DB junction stays protected (the ingest gate
drops over-cap records) — **but the direct-JSON DCF readers regress.** There is no
mechanism that makes the company-published figure authoritatively *win* at read/resolve
time. This directive builds exactly that.

---

## 2. Why the existing tier ladder is not enough (the crux)

Source tiering already exists (`models/documents.py` → `SourceQualityTier`, mirrored in
`timeseries/loaders.py::SOURCE_QUALITY_TIER_RANK`):

| Tier | Rank | Source types that map to it |
|---|---|---|
| `sec_official` | 4 | `SEC_XBRL` |
| `fmp_normalized` | 3 | `FMP`, **`IR_DOC`**, `TRANSCRIPT_AUDIO`, `MANUAL_CSV`, `MANUAL_ENTRY` |
| `llm_extracted` | 2 | `LLM_EXTRACTED` |
| `yfinance_fallback` | 1 | — |
| `s1_provisional` | 0 | `SEC_S1` |

The tier-aware loaders order `tier_rank DESC, id DESC`, so an `SEC_XBRL` fact already beats
FMP. **The gap:** the GOOG number came from an **8-K *press release* exhibit**, not XBRL.
A press-release / IR-deck fact is ingested as `IR_DOC` (**ties** FMP at rank 3) or, if an
LLM read it, `LLM_EXTRACTED` (**loses** to FMP at rank 2). So the very class of document
the owner wants to win — company-published prose/exhibits — **cannot reliably win through
tiering alone.** Two further problems compound it:

1. **Not every reader is tier-aware.** `load_financial_series` / `load_kpi_series` order by
   tier; but the Financials report `metrics` VIEW, `thesis_evaluator._fetch_kpi_history`,
   and `fmp_derived_kpis` dedup by `MAX(source_doc_id)` (insertion order) — a re-fetched FMP
   row (higher id) would win.
2. **Segment + DCF readers bypass the DB entirely.** `build_redesigned_dcf.py`,
   `dcf_opus_assumptions.py`, and `start_diligence.py` read the raw
   `data/historical/fmp/*_segments_*.json` directly — no tier, no gate.

**Conclusion:** the durable, uniform mechanism is a **first-class override record**
consulted at read/resolve time (owner's scope item #2), not a tier reshuffle. Tiering stays
the cheap default; the override is the authoritative escape hatch for company-doc truth.

---

## 3. Design

### 3.1 The durable primitive — `fact_overrides` table

One row per `(ticker, period, logical-fact)` that a company document authoritatively
qualifies. Lives in the DB (main repo), **outside** the FMP cache files, so a re-fetch
cannot clobber it.

```
fact_overrides(
  id                 INTEGER PRIMARY KEY,
  user_id            TEXT    NOT NULL DEFAULT 'bhanu',   -- tenant (matches confidence_observations)
  ticker             TEXT    NOT NULL,
  period_end         TEXT    NOT NULL,                   -- 'YYYY-MM-DD' (naive-UTC convention)
  fiscal_period_type TEXT    NOT NULL,                   -- Q1..Q4 | FY | H1 | ...
  fact_kind          TEXT    NOT NULL,                   -- 'financial_fact' | 'segment' | 'kpi'
  fact_key           TEXT    NOT NULL,                   -- canonical within-period key (see 3.2)
  action             TEXT    NOT NULL,                   -- 'replace' | 'drop' | 'qualify'
  value              NUMERIC,                            -- scalar authoritative value (replace)
  unit               TEXT,                               -- unit of `value`
  value_json         TEXT,                               -- structured payload (segment record replace)
  -- company-doc provenance:
  source_doc_type    TEXT    NOT NULL,                   -- DocType: sec_8k|sec_10q|sec_10k|ir_press_release|...
  source_accession   TEXT,                               -- EDGAR accession (0001652044-26-000012)
  source_exhibit     TEXT,                               -- googexhibit991q42025.htm
  source_url         TEXT,
  source_excerpt     TEXT,                               -- verbatim quote
  source_doc_id      INTEGER,                            -- FK documents.id when the company doc is ingested
  -- governance:
  status             TEXT    NOT NULL DEFAULT 'active',  -- 'active' | 'retired'
  confidence         REAL,                               -- override-asserted confidence (default 1.0)
  rationale          TEXT,
  created_by         TEXT    NOT NULL,                   -- 'manual:bhanu' | 'agent:<id>' | 'cli'
  created_at         TEXT    NOT NULL,
  retired_at         TEXT
)
-- one ACTIVE override per logical fact; history retained as retired rows:
CREATE UNIQUE INDEX uq_fact_overrides_active
  ON fact_overrides(user_id, ticker, period_end, fiscal_period_type, fact_kind, fact_key)
  WHERE status = 'active';
CREATE INDEX ix_fact_overrides_lookup
  ON fact_overrides(ticker, fact_kind, status);
```

**Actions**
- `replace` — the override value is authoritative; readers return it instead of FMP's.
- `drop` — the FMP record/cell is spurious (e.g. the "Google Inc." segment); readers omit it.
- `qualify` — keep FMP's value but attach a cross-source disagreement annotation (feeds the
  provenance-v2 ⚠ surface; no value change). Bridges to `validation_issues` / `display_issues_for_fact`.

### 3.2 `fact_key` — the canonical within-period key, per kind

| fact_kind | fact_key | `replace` payload |
|---|---|---|
| `financial_fact` | the `line_item` string (`"revenue"`) | scalar `value` + `unit` |
| `kpi` | the **KPI definition NAME** (`"Google Cloud revenue growth"`) — portable across per-ticker `def_id` | scalar `value` + `unit` |
| `segment` (record) | the `dim_type` (`"product"` \| `"geography"`) | `value_json = {segment_name: value, ...}` — replaces the **whole** record's `data` dict |
| `segment` (cell) | `"<dim_type>\|<dim_name>\|<metric>"` (`"product\|Google Cloud\|revenue"`) | scalar `value` |

Record-level segment replace is the robust primitive for the GOOG case: a re-fetched bad
record has *different* segment names (spurious "Google Inc.", missing Subscriptions), so a
whole-`data`-dict replace reconstructs the correct record regardless of FMP's shape.

### 3.3 The resolver — `src/provenance/overrides.py`

Single module every read path consults. Pure-ish (DB-read only); no LLM.

```python
class OverrideAction(StrEnum): REPLACE; DROP; QUALIFY
@dataclass(frozen=True) class FactOverride: ...  # mirrors a row

# write
def record_override(conn, *, ticker, period_end, fiscal_period_type, fact_kind,
                    fact_key, action, value=None, unit=None, value_json=None,
                    source_doc_type, source_accession=None, source_exhibit=None,
                    source_url=None, source_excerpt=None, source_doc_id=None,
                    confidence=1.0, rationale=None, created_by) -> int
    # retires any existing active override for the same logical key, inserts the new one.

# read / resolve
def get_active_overrides(conn, *, ticker, fact_kind=None, period_end=None) -> list[FactOverride]
def resolve_scalar(conn, *, ticker, period_end, fiscal_period_type, fact_kind, fact_key) -> FactOverride | None
def apply_segment_overrides(conn, *, ticker, dim_type, records: list[dict]) -> list[dict]
    # given raw FMP segment records (the cache JSON, list of {date,period,data:{...}}),
    # apply record-level REPLACE + per-cell REPLACE/DROP in-memory; returns corrected records.
def overridden_segment_periods(conn, *, ticker, dim_type) -> set[str]   # period_end set, for the audit
```

### 3.4 Wiring per fact kind

**Segment (the acute, durable fix — owner scope #3).** Introduce one shared cache loader,
`compute/segment_cache.py::load_segment_records(project_root, ticker, suffix, conn)`, that
reads the raw JSON **and applies `apply_segment_overrides`**. Route through it:
- `compute.segments.extract_segment_facts` (DB ingest) — even a re-fetched bad record is
  corrected *before* the reconciliation gate ⇒ the DB gets the 8-K figures, not a drop.
- The three direct-JSON DCF readers (`execution/build_redesigned_dcf.py`,
  `execution/dcf_opus_assumptions.py`, `execution/start_diligence.py`) — they now see the
  corrected `data` dict. **This closes the regression.**
- `pipeline.segment_cache_audit` / the `VALIDATE_SEGMENT_CACHE` stage — a flagged record
  that has an active override is reported **OK (overridden)**, not FAILED; un-overridden
  contamination still FAILS the cron (the "FMP re-served bad data" signal is preserved).

**financial_facts + kpi (owner scope #1).** Two complementary moves:
- *Resolve-time consult:* the canonical `timeseries/loaders.py` series loaders
  (`load_financial_series`, `load_kpi_series`, + provenance variants) apply `resolve_scalar`
  as a thin post-dedup overlay — an active `replace` override substitutes value/unit and
  stamps provenance. This is the master read path (charts, recommendations, ask grounding).
- *Generalized supersession at write:* `record_override` (when the company doc is ingestible)
  also performs a tier-aware **supersession delete/retire** of lower-or-equal-tier FMP/LLM
  incumbents for the logical key — generalizing `ir_pipeline/ingest.py::_supersede_llm_incumbents`
  (today: `DELETE ... WHERE extracted_by LIKE 'llm%'`) to *all* company docs. Defense in
  depth so even non-tier-aware `MAX(source_doc_id)` readers converge.

**Known remaining seam (documented, not silently ignored):** the Financials `metrics` VIEW,
`thesis_evaluator._fetch_kpi_history`, and `fmp_derived_kpis` dedup by `MAX(source_doc_id)`
and are *not* yet override-aware. A guard test asserts the canonical loaders honor overrides;
extending the VIEW-backed readers is a follow-up phase. Segment + DCF (the acute case) are
fully covered in Phase 2.

### 3.5 Precedence rule (the invariant)

> For a given logical fact key, if an **active `fact_overrides` row** exists, it is
> authoritative (`replace`/`drop`) regardless of FMP's cache state or row ids. Otherwise the
> existing `tier_rank DESC, id DESC` ordering decides, with `sec_official` > `fmp_normalized`
> > `llm_extracted` > `yfinance_fallback` > `s1_provisional`.

---

## 4. Phased plan (one PR per phase; cherry-pick onto fresh main)

**Scope decisions (2026-06-14, owner):** automated EDGAR fetch is *in* v1; cover *every*
reader (incl. the `MAX(source_doc_id)` VIEW-backed ones); auto-merge each phase after local
verification, stopping on failure.

- **P1 — core primitive.** Alembic migration `fact_overrides` (+ check constraints per the
  0071 convention) + `src/provenance/overrides.py` (write/read/resolve API) + unit tests.
  No read-path wiring yet. Ships the durable record + the `record_fact_override` CLI
  (`execution/record_fact_override.py`).
- **P2 — segment path + GOOG worked example.** `compute/segment_cache.py` shared loader;
  route ingest gate, audit/stage, and the 3 direct-JSON DCF readers through it; seed the
  GOOG Q4'25 product-segment record override from the 8-K; end-to-end test proving a
  simulated FMP re-fetch (bad data) still resolves to the 8-K figures.
- **P3 — financial_facts + kpi resolve (every reader). [SHIPPED]** A read-time overlay
  (`overrides.active_scalar_override_map` / `date_override_map`) applied at every
  value-determining reader: the canonical tier-aware `timeseries/loaders.py`
  `load_financial_series` + `load_kpi_series`, AND the `MAX(source_doc_id)` readers —
  `thesis_evaluator._fetch_kpi_history`, `fmp_derived_kpis._fetch_full_kpi_series`, and the
  Financials `_kpi_series_for` / `_annual_kpi_raw_for` panels. A `replace` substitutes the
  value; a `drop` omits the period; both match on `(period_end, fiscal_period_type)`.
  **Design choice:** the override is *consulted at read time* — FMP rows are deliberately
  NOT deleted, so the provenance/audit trail and cross-source disagreement signal survive,
  and the override wins regardless of row ids (durable across a re-fetch). The earlier
  "generalized supersession-delete" idea is intentionally dropped as destructive.
  **Deferred to P5 (surface):** reflecting the override's source in the provenance *chip*
  (the displayed value is correct now; the chip still describes the FMP row), and the
  `qualify` → provenance-v2 ⚠ annotation.
- **P4 — automated EDGAR 8-K extraction. [SHIPPED]** `src/provenance/edgar_8k.py`: resolve
  CIK → discover the EX-99.1 exhibit → fetch + strip HTML → LLM-extract the segment
  revenue table into structured JSON (the `extract_8k_overrides` purpose, pinned to the
  cheapest at-parity model — Haiku — per `directives/cheapest_model_routing.md`; cost/latency
  logged by the shared `call_llm_structured` path) → build a record-level `segment` override
  citing accession + exhibit. CLI `execution/extract_8k_overrides.py` (`--apply` records it).
  Every network/LLM seam is injectable, so the pipeline is fully tested offline (no spend).
  Eval: `evals/golden/extract_8k_overrides.json` + `execution/run_8k_extraction_eval.py`
  (real-LLM, run on the weekly cadence — gates promoting to a cheaper backend; a `≥0.95`
  score required). The owner can now auto-populate the GOOG case rather than hand-seeding.
- **P5 — surface + docs. [SHIPPED]** `data_provenance.md` §10 documents the layer; a
  read-only **Overrides** sub-panel (`src/pipeline/fact_overrides_panel.py`, wired into
  System → Provenance next to Restatements) lists every active override with its citing
  filing, so any override is visible & auditable; memory notes updated.

---

## 5. Worked example — GOOG Q4'25 (the acceptance test)

1. Seed: `record_override(ticker=GOOG, period_end=2025-12-31, fiscal_period_type=Q4,
   fact_kind=segment, fact_key="product", action=replace,
   value_json={"Google Search & Other":63073e6, "YouTube Advertising Revenue":11383e6,
   "Google Network":7828e6, "Subscriptions, Platforms, And Devices Revenue":13578e6,
   "Google Cloud":17664e6, "Other Bets":370e6, "Other Segments":-68e6},
   source_doc_type=sec_8k, source_accession="0001652044-26-000012",
   source_exhibit="googexhibit991q42025.htm", rationale="8-K exhibit 99.1 supersedes contaminated FMP segmentation")`.
2. Simulate a re-fetch that writes FMP's *bad* record (Google Cloud $20,941M, spurious
   "Google Inc.", missing Subscriptions) into the cache.
3. Assert: (a) `apply_segment_overrides` returns the 8-K `data` dict; (b) `extract_segment_facts`
   writes Google Cloud = $17,664M to the junction (not dropped, not $20,941M); (c) the audit
   stage reports OK (overridden) rather than FAILED; (d) the DCF readers' `idx()` sees
   $17,664M. Leaf sum reconciles to $113,828M ≈ reported revenue.

---

## 6. Testing & gates

- Per-file CI gates: tests / quality / **strict-pyright ratchet** (no new errors on touched
  files). Cast at JSON boundaries (`value_json`), never `# type: ignore`.
- Unit: resolver round-trip (record → retire-prior → resolve), `apply_segment_overrides`
  (record + cell + drop), `resolve_scalar` precedence.
- Integration: the §5 GOOG end-to-end re-fetch-durability test.
- Guard: canonical loaders honor an active `replace` override; the `MAX(source_doc_id)`
  readers' non-coverage is asserted as a documented xfail/seam, not a silent pass.
