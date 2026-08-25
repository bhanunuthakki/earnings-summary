# Data Provenance Contract

**Status**: Layer 1 baseline. Cross-cutting — every Layer 2 orchestrator and Layer 3 execution script must comply. Immutable without explicit user authorization.

**Why this exists**: The portfolio aims (DCF, segment OI, ROE/ROA, leading-indicator KPIs) require fusing data from FMP, SEC XBRL, IR PDFs, audio transcripts, and manual entries. Without per-fact provenance, you cannot resolve disagreements between sources, you cannot tell whether a "Cloud OI" figure came from the 8-K supplement or the 10-K segment note, and you cannot defensibly audit the thesis tracker.

## 1. Source-type taxonomy (closed enum)

Defined in `src/models/documents.py::SourceType`. Never substring-match. Never store as freeform text.

| value | meaning |
|---|---|
| `fmp` | Financial Modeling Prep API endpoint response (JSON) |
| `sec_xbrl` | SEC EDGAR XBRL filing (10-K, 10-Q, 20-F, 40-F, 8-K, 6-K) — primary issuer source |
| `sec_s1` | Audited statements parsed from an S-1 registration statement (prospectus). **Provisional** anchor for recently-IPO'd issuers with no 10-K yet and still-empty FMP statement endpoints. Lowest precedence (tier `s1_provisional`); superseded by `sec_xbrl`/`fmp` once real filings report the same period. |
| `ir_doc` | Investor-relations document downloaded from the company's IR site (press release, supplement, presentation, investor update PDF) |
| `transcript_audio` | Earnings-call audio file ingested locally (YouTube via yt-dlp, S3, etc.) |
| `manual_csv` | User-supplied CSV upload |
| `manual_entry` | User-typed entry in the front-end (when one exists) |
| `llm_extracted` | Structured payload an LLM produced from a primary document. **Derived, not primary.** Starts in validation quarantine until promoted. |

## 2. The provenance contract

Every row in every fact table — `financial_facts`, `segment_facts`, `kpi_facts`, `transcript_segments`, `dcf_runs.dcf_inputs`, `metric_facts` — **must** include a non-null `source_doc_id` foreign-keyed to `documents.id`.

Every row in `documents` must have `(source_type, doc_type, file_path, sha256, fetched_at, fetch_status)` populated. `sha256` is the Content Identity of the raw bytes: re-ingesting the same bytes is a no-op; different bytes for the same logical source slot create a new Observation Version and supersede the previous row (we never mutate, we add). Source-specific Logical Idempotency Keys below identify the source slot; Attempt Identity belongs to the run ledger, not the document.

LLM-extracted documents must carry `parent_document_id` pointing at the primary document the LLM read from.

### 2.1 Fiscal-period stamping drift (off-cycle-FYE issuers)

`(ticker, period_end)` matching (used by `src/provenance/llm_extracted_parent.py::resolve_parent` and any other exact-date join across `documents`) is only reliable if every ingestion path stamps `period_end` on the **same fiscal calendar** for a given ticker. Multiple independent modules each hardcode their own per-ticker "which tickers have a non-December fiscal year end" override table, keyed off filename conventions like `<TICKER>_Q<N>_<YYYY>` — and those tables can drift out of sync, silently mis-stamping `period_end` for tickers missing from one table but present in another. This is **not** the same failure mode as a genuinely-missing source document (§2's `parent_document_id` can legitimately stay NULL when no primary doc was ever fetched) — it produces an *orphan that looks unresolvable but has a perfectly good source sitting one calendar-quarter away*.

Concretely (audited 2026-07-02, following the #765 backfill): `compute/transcript_ingest.py::_FYE_OFFSETS` correctly covers `AMAT`/`TOL` (October FYE) alongside `RBRK`/`VEEV` (January FYE). `compute/kpi_extract_summaries.py::_TICKER_QUARTER_PERIOD_END` — a separate table driving the LLM-summary synthesis path, reading files off the identical `.tmp/<TICKER>_Q<N>_<YYYY>_*.txt` naming convention — only had `RBRK`/`VEEV`. `AMAT`/`TOL` fell through to the plain calendar-quarter default, stamping e.g. `AMAT_Q4_2025_summary.txt` (fiscal Q4 ending 2025-10-31, matching the registered transcript) as `period_end=2025-12-31` instead. The resolver's exact-date match then correctly reported "no candidate parent" — the transcript it should have matched was one quarter-and-two-months away on the calendar, not missing.

**The #765 PR body's characterization of this as "AMAT/TOL have no registered transcript/IR document at all yet" was incorrect** — the transcripts were on file (`transcripts/processed/AMAT_Q4_2025.txt` id 5190, `AMAT_Q1_2026.txt` id 5189, `TOL_Q4_2025.txt` id 5354, `TOL_Q1_2026.txt` id 5353); the correcting record is this subsection.

Two additional wrinkles worth knowing about, both distinct from the mis-stamping bug above:

- The two modules' filename-year **labels** aren't always numerically identical for the same calendar quarter on the same ticker. `kpi_extract_summaries.py` named VEEV's quarter ending 2026-04-30 `VEEV_Q1_2026`; a later transcript-ingestion pass named the identical calendar quarter `VEEV_Q1_2027`. Both resolve to the correct `period_end` *within their own module* (each module's override table is tuned to its own labeling convention), so this by itself does not cause an orphan — but it means the two label conventions must never be compared to each other directly, only their resolved `period_end` dates.
- A ticker can have its **transcript** registered for a period but never have its **IR press-release/presentation PDF** fetched for that same period (or vice versa) — a genuine missing-source gap, not a stamping bug. `parent_document_id` correctly stays NULL until the IR fetch pipeline (auto-discover or manual upload) actually lands that document.

**Fix applied**: `compute/kpi_extract_summaries.py::_TICKER_QUARTER_PERIOD_END` now carries an explicit `year_offset` per quarter (not inferred from the month — Jan-FYE's Q4 and Oct-FYE's Q1 both land in January but roll to a different calendar year) and covers `AMAT`/`TOL`. `execution/backfill_fiscal_period_stamps.py` re-derives and corrects `documents.period_end` (and any dependent `kpi_facts.period_end` — `fiscal_period_type` is unaffected) for rows stamped before the fix; re-running `execution/backfill_llm_extracted_parents.py --apply` afterward resolves their `parent_document_id`.

**When adding a ticker with a non-calendar fiscal year end**, update every module keeping its own copy of this override — grep for `_FYE_OFFSETS` / `_TICKER_QUARTER_PERIOD_END` (or equivalent) across `src/compute/` before assuming one edit is sufficient.

## 3. Conflict resolution when sources disagree

Order of trust for the same `(ticker, period_end, line_item)`:

1. `sec_xbrl` (primary issuer filing)
2. `fmp` (third-party normalization of #1)
3. `ir_doc` (issuer-published, often summary-level)
4. `transcript_audio` (management commentary; lossy)
5. `llm_extracted` (always quarantined first)
6. `manual_csv` / `manual_entry` (user override; logged with reason)

`sec_s1` sits below all of the above for any `(ticker, period_end, line_item)` it shares: it is a provisional pre-IPO snapshot (tier `s1_provisional`, the lowest rank) and is superseded the moment a real `sec_xbrl` or `fmp` filing reports the same period.

If trust order #1 and #2 disagree by more than 0.5%, write a `validation_issues` row with `severity=warn`, `rule=source_disagreement`, and prefer #1.

Manual override always wins over automated sources, but it must include a reason string in `validation_issues.raw_value`.

## 4. Per-source ingestion rules

### `fmp`
- `doc_type` is one of the `FMP_*` values in `DocType`. Files land in `data/historical/fmp/` named `{TICKER}_{endpoint_slug}.json`.
- Currency from `reportedCurrency` field; halt if absent.
- Period from `date` or `fillingDate`; halt if absent or malformed.
- Logical Idempotency Key: `(ticker, doc_type, period_end)`; payload SHA-256 is its Content Identity and a changed payload is a new Observation Version.
- ETF tickers route to `ETF_*` doc types only — never `FMP_INCOME_STATEMENT` etc.

### `sec_xbrl`
- `doc_type` ∈ `{SEC_10K, SEC_10Q, SEC_20F, SEC_40F, SEC_8K, SEC_6K}`. Files land in `data/historical/sec/{TICKER}/{accession_number}/`.
- Currency from XBRL context; halt if absent.
- Logical Idempotency Key and source Observation Version: SEC accession number; payload SHA-256 remains the Content Identity.
- Filing regime branches by `companies.filing_regime`: 10-K/10-Q for US issuers, 20-F/6-K for foreign private issuers, 40-F for Canadian issuers.

### `sec_s1`
- `doc_type` is `SEC_S1`. The audited "F-pages" of the cached S-1 text (`data/sec_text/{TICKER}_s1_{FY}.txt`) are parsed by `src/compute/s1_financials.py`; facts are written with `extracted_by='s1'`.
- Source-quality tier is `s1_provisional` — the lowest rank. Any `fmp`/`sec_xbrl` row for the same `(ticker, period_end, line_item)` supersedes it.
- Provisional by design: used only for recently-IPO'd issuers (`recently_ipod` / `data_anchor=s1`) until their first 10-Q/10-K lands. Optional — tickers without an S-1 anchor proceed normally.
- `source_type` and `doc_type` both take the value `sec_s1` (the prospectus is at once the provenance origin and the artifact). They live in separate columns and are only ever compared by exact equality, so the identity is safe — never substring-match one against the other.

### `ir_doc`
- `doc_type` ∈ `{IR_PRESS_RELEASE, IR_PRESENTATION, IR_TRANSCRIPT, IR_SUPPLEMENT, IR_INVESTOR_UPDATE}`. Files land in `ir_documents/{TICKER}/{period_end_iso}/`.
- `source_url` **required** in `documents.source_url`. Two flavors:
  - Auto-fetch (URL manifest → download): the original IR-page PDF URL.
  - Manual upload (`categorize_ir_uploads.py`): `manual_upload:{original-filename}`, where the original filename is the basename the user dropped in `ir_documents/` before triage. Preserves the user-visible identity for audit.
- Logical Idempotency Key: `(ticker, doc_type, period_end, source_url)` when known. SHA-256 is the UNIQUE Content Identity: re-uploading identical bytes is a no-op; modified bytes for the same logical slot write a new Observation Version and supersede the previous one — never mutate.
- Manual uploads where ticker, doc_type, **and** period_end cannot all be determined from filename + first-page fingerprint are quarantined to `ir_documents/_unsorted/` with a `.error.json` sidecar. They are **not** registered in `documents` and **not** silently merged with any existing row — the user must repair (rename the file, extend the issuer registry, or delete the upload) and re-run.
- The IR step is **optional**: tickers with no `ir_doc` rows in `documents` proceed through the rest of the pipeline (FMP, SEC, transcripts) without any IR-derived facts. Downstream consumers must `LEFT JOIN` against `ir_doc` rows, never `INNER JOIN`.

### `transcript_audio`
- Audio land in `transcripts/raw/audio/`, transcripts in `transcripts/raw/text/`.
- Speaker attribution **must** be preserved in `transcript_segments`.
- Time codes preserved when available.
- `source_url` of the audio recording stored in `documents.source_url`.
- Whisper transcription writes a new `documents` row with `source_type=transcript_audio` and `parent_document_id` = the audio document.

### `manual_*`
- Logical Idempotency Key: `(ticker, doc_type, period_end, user_id, submitted_at)`; the submission's bytes are its Content Identity.
- Reason string required and stored in `documents.source_url` as `manual:{reason}`.

### `llm_extracted`
- `parent_document_id` required.
- Always inserted with `validation_issues` rows of `severity=warn` for human review.
- Promoted to "trusted" only when explicitly marked.

## 5. What this rules out

- No `dict[str, Any]` flowing between scripts. Use Pydantic models from `src.models`.
- No silent merging across sources. Two FMP files updated on different dates are two `documents` rows; the consumer chooses the latest.
- No "we got this from somewhere" rows. If `source_doc_id` is null, the row is invalid and the run halts.
- No string-substring source detection (`if "fmp" in path:`). Use `SourceType`.

## 6. Backfill from existing files

The Phase 2 migration `0003_backfill_documents_from_fmp_files` walks `data/historical/fmp/` after the live FMP backfill completes, computes sha256 per file, and inserts one `documents` row per file with `source_type=fmp`, `fetched_at=mtime`, `fetch_status=ok`. Idempotent — re-runnable.

For `ir_doc`, backfill is performed by `execution/categorize_ir_uploads.py` rather than an Alembic migration: walking the user's loose uploads requires per-file content fingerprinting (issuer detection, doc-type classification, period extraction) which is more naturally expressed as an idempotent CLI than as a one-shot SQL migration. The CLI uses the same sha256-INSERT-OR-IGNORE semantics as the FMP backfill — running it twice is a no-op for unchanged files.

Subsequent migrations backfill from `data/historical/sec/` and `transcripts/processed/` similarly.

## 7. Filing identity & sub-document locators (alembic 0075)

A fact traces to a *file* via `source_doc_id`, but auditing a number requires two more hops: WHICH regulatory filing the file represents, and WHERE inside the source the value was read. 0075 adds both halves (master build P3.1).

### Filing identity — `documents.accession_number` + `documents.filing_date`

| column | type | meaning |
|---|---|---|
| `accession_number` | TEXT, nullable | SEC EDGAR accession in **canonical dashed form** (`0001628280-21-010389`). Never store the 18-digit no-dash form — normalize on write. |
| `filing_date` | TEXT ISO `YYYY-MM-DD`, nullable | The date the filing hit EDGAR (the companyfacts `filed` field), NOT our `fetched_at`. Orders documents by regulatory time. |

Both are NULL for non-filing documents (FMP endpoint dumps, IR PDFs, transcripts). An FMP statement endpoint response spans many filings, so a *document-level* accession/filing_date is undefined for it by design — per-fact filing attribution rides on the locator (below).

Backfill: `execution/backfill_document_accessions.py` (idempotent — fills NULLs only, never overwrites). Derivation order: the `#accn=` fragment on sec_xbrl `file_path`s → a dashed accession embedded in the `file_path` → an EDGAR (`sec.gov`) `source_url` carrying the accession dashed or as the 18-digit Archives directory. `filing_date` comes from the companyfacts JSON `accn → filed` mapping. Everything underivable stays NULL and is counted in the CLI's report.

### Sub-document locator — `financial_facts.locator` + `kpi_facts.locator`

Nullable TEXT(JSON). Canonical shape (all keys optional — populate whichever the extractor actually knows):

```json
{"section": "<parsed 10-K section key>",
 "transcript_line": <int>,
 "pdf_page": <int>,
 "json_path": "<FMP record pointer, e.g. \"[3].netIncome\">"}
```

- `section` — top-level key in the parsed `data/historical/fmp/{T}_form_10k_{Y}.json` section-keyed text.
- `transcript_line` — line id within the source transcript (`transcript_segments` ordering).
- `pdf_page` — 1-based page in the source PDF (IR decks, supplements).
- `json_path` — record index + field name within the cached FMP endpoint response.

Rules:

- The typed model is `src/models/facts.py::FactLocator`; writers serialize through `FactLocator.to_json()` (an all-empty locator serializes to `None` so the column stays NULL, never `"{}"`). The fact-store insert helpers (`src/pipeline/restatement_detector.py`) take the pre-serialized JSON.
- There is deliberately **no DB-level CHECK** on the JSON: adding one to an existing SQLite table forces a full-table rebuild of the largest tables in the DB. Validity is the write path's job.
- A NULL locator is valid (facts predating 0075, or sources with no meaningful sub-position). Readers must treat locator as enrichment, never a join key.

### Writer wiring (P3.2)

| writer | locator written | source_excerpt |
|---|---|---|
| FMP statement extractors (`compute/income_statement`, `balance_sheet`, `cashflow`) | `json_path = "[<i>].<fmpField>"` — record index + field in the cached endpoint response | n/a (financial_facts has no excerpt column) |
| `compute/as_reported.py` | `json_path = "[<i>].data.<xbrl_tag>"` | n/a |
| `pipeline/sec_xbrl.py` (companyfacts) | `json_path = "facts.<ns>.<tag>.units.<unit>[<i>]"` | n/a |
| `compute/kpi_extract_summaries.py` (LLM over quarterly summaries) | none (the source is a derived summary doc — quote is the anchor) | **yes** — the LLM returns the verbatim snippet per KPI; persisted clipped to 1024 chars |
| `execution/extract_kpis_from_ir.py` (in-session PDF readout) | `pdf_page` via per-value `"locator"` in the manifest JSON | yes, via per-value `"source_excerpt"` |
| `compute/fmp_derived_kpis.py` | none **by design** — derived series have no single source position; provenance is the chain of source facts | n/a |
| `ir_pipeline/ingest.py` (IR spreadsheet) | none — spreadsheet cells have no JSON/PDF position in the contract | n/a (deterministic parse, no quote) |
| `compute/s1_financials.py` | none — the text-region parser carries no stable line anchor; S-1 facts are provisional and superseded by real filings | n/a |

Facts written before P3.2 keep NULL locators; there is no retroactive fact-locator backfill (re-extraction naturally repopulates).

## 8. Per-fact confidence scoring (fund-grade build S2)

`financial_facts.confidence` / `kpi_facts.confidence` is a **scored** value, not a schema default. The single source of truth is `src/pipeline/confidence.py::score_confidence`:

```
confidence = clamp( tier_base + method_delta − unresolved-issue penalties ) × llm_self_report
```

| component | values |
|---|---|
| `tier_base` (documents.source_quality_tier) | sec_official 0.98 · fmp_normalized 0.92 · llm_extracted 0.75 · yfinance_fallback 0.70 · s1_provisional 0.65 · unknown 0.70 |
| `method_delta` (fact's `extracted_by`, via `classify_extraction_method`) | deterministic mapping +0.02 · manual 0.00 · LLM −0.05 · unknown −0.02 |
| issue penalty (per unresolved `validation_issues` row matching the fact) | source_disagreement 0.15 · magnitude_jump 0.10 · plausible_range 0.10 · unit_mismatch 0.10 · other 0.05; `halt` severity doubles; total capped at 0.40 |
| `llm_self_report` | the extractor's per-value confidence (`KpiValue.confidence`), folded in at ingest by `persist_manifest`; 1.0 for deterministic writers |
| clamp | [0.05, 1.0], rounded to 4 decimals |

Reference points: SEC XBRL fact = **1.00**; FMP fact = **0.94**; the same FMP fact under one unresolved cross-source disagreement = **0.79**; plain LLM extraction at default self-report = **0.665**.

Wiring:

- **Ingest** — every writer stores the tier+method prior (`compute/_common.insert_financial_facts(tier=...)`, `pipeline/sec_xbrl.py`, `compute/fmp_derived_kpis.py`, `pipeline/kpi_persistence.persist_manifest`). Issue penalties cannot apply at ingest (validation runs after persist).
- **Reconciliation** — `python execution/backfill_confidence.py --apply` rescores both tables from current tiers + unresolved issues; idempotent (pure formula, second run over an unchanged DB writes nothing). Re-run after validation passes.
- **Preservation** — LLM-method rows already carrying a non-default confidence are self-scored extractions; the backfill never rescores them (the self-report is not recoverable from the stored product).
- **UI** — the source chip (`src/ui/source_chip.py`) shows the % in its popover and hover title; below `LOW_CONFIDENCE_THRESHOLD = 0.8` the chip takes a warn-tinted dashed border (the subtle low-confidence cell affordance).

## 9. Derived-fact lineage — `kpi_facts.computed_from` (alembic 0087)

Derived KPI rows (`extracted_by` `fmp_derived` / `kpi_transform_derived`) carry a nullable JSON TEXT column recording the derivation recipe:

```json
{"display": "operating_income ÷ revenue (%)",
 "inputs": [
   {"ref": "financial_fact", "item": "operating_income", "period_end": "2025-12-31",
    "doc_id": 45, "tier": "fmp_normalized"},
   {"ref": "financial_fact", "item": "revenue", "period_end": "2025-12-31",
    "doc_id": 45, "tier": "fmp_normalized"}
 ]}
```

- `display` — the human-readable formula the chip popover renders as "derived from: …".
- `inputs[*].ref` ∈ `financial_fact` | `kpi_fact` | `segment_fact`; `item` is the line_item / KPI label / `"<segment> <metric>"`; `period_end` ISO date. Two-period derivations (YoY) list both periods.
- `doc_id` + `tier` — the input's source document at derivation time (`tier` injected by `persist_derived_kpis` from `documents.source_quality_tier`), so the popover renders one tier-colored mini-chip per input linking `/source/<doc_id>` with no render-time lookup.

Writers: `compute/fmp_derived_kpis.py` (margins, ratios, YoY growth, ROE, segment KPIs, category-2 transforms). NULL for directly-extracted rows and rows written before 0087 (re-derivation naturally backfills — `derive_for_ticker` is idempotent). No DB-level JSON CHECK, same rationale as §7's locator column.

### Cell-level surfacing (chip popover)

The source chip popover (`src/ui/source_chip.py`) renders, per fact:

- **confidence %** (§8) + the low-confidence affordance below 0.8;
- **unresolved validation issues** as warn rows, formatted by `pipeline.confidence.display_issues_for_fact` — a cross-source disagreement reads from the displayed cell's perspective ("⚠ SEC says $101M, 0.99% delta"); unrecognized rules fall back to `⚠ <rule>: <raw_value>`, which is where a manual-override reason (§3) surfaces;
- **`extracted_by`** ("via fmp_derived") — the extraction-method audit trail;
- **derived-from lineage** — `computed_from.display` plus per-input mini-chips.

---

## 10. Company-doc overrides — `fact_overrides` (alembic 0111)

See the dedicated directive `directives/provenance_override_2026_06.md`. FMP is a
*convenience* tier, not authoritative; when a company-published document (SEC 8-K /
10-Q / 10-K, earnings press release, IR deck) reports a value, it should
**systematically supersede** FMP's cached/derived value for the same logical fact.
The tier ladder (§2) handles `sec_official` > FMP, but the documents the owner most
wants to win — press-release / IR-deck exhibits — ingest as `IR_DOC` (a *tie* with
FMP) or `LLM_EXTRACTED` (which *loses*), and some readers bypass the ladder. So the
durable mechanism is a first-class **override record consulted at read time**.

- **Table:** `fact_overrides` (alembic 0111) — one ACTIVE row per logical key
  `(user_id, ticker, period_end, fiscal_period_type, fact_kind, fact_key)` enforced by
  a partial-unique index; superseded overrides retained as `retired`. Lives in the DB,
  **outside** the FMP cache files, so a `save_fmp_data` re-fetch cannot clobber it.
- **Resolver:** `src/provenance/overrides.py` — `record_override` / `resolve_scalar` /
  `apply_segment_overrides` / `active_scalar_override_map` / `date_override_map`.
  Actions: `replace` (authoritative value), `drop` (spurious record/cell), `qualify`
  (annotate only).
- **Read paths honoring it:** the segment ingest gate + audit + the direct-JSON DCF
  readers (`src/compute/segment_cache.py`); the canonical `timeseries/loaders.py`
  series loaders; and the `MAX(source_doc_id)` KPI readers
  (`thesis_evaluator._fetch_kpi_history`, `fmp_derived_kpis._fetch_full_kpi_series`,
  `report/sections/financials.py`). FMP rows are NOT deleted — the override wins at
  read, so the provenance/audit trail and disagreement signal survive.
- **Populating:** by hand (`execution/record_fact_override.py`) or auto-extracted from
  the filing (`src/provenance/edgar_8k.py` + `execution/extract_8k_overrides.py`).
- **Surfacing:** System → Provenance → **Overrides** (`pipeline/fact_overrides_panel.py`).

Remaining seam: the per-cell source *chip* (§9) still describes the FMP row for an
overridden value — the displayed number is correct, but the chip's source attribution
and a `qualify` → ⚠ annotation are a follow-up.
