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

Every row in `documents` must have `(source_type, doc_type, file_path, sha256, fetched_at, fetch_status)` populated. `sha256` is computed from raw bytes and is the idempotence key for that file: re-ingesting the same bytes is a no-op; different bytes for the same `(ticker, source_type, doc_type, period_end)` writes a new `documents` row and supersedes the previous (we never mutate, we add).

LLM-extracted documents must carry `parent_document_id` pointing at the primary document the LLM read from.

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
- Idempotence key: `(ticker, doc_type, period_end)`.
- ETF tickers route to `ETF_*` doc types only — never `FMP_INCOME_STATEMENT` etc.

### `sec_xbrl`
- `doc_type` ∈ `{SEC_10K, SEC_10Q, SEC_20F, SEC_40F, SEC_8K, SEC_6K}`. Files land in `data/historical/sec/{TICKER}/{accession_number}/`.
- Currency from XBRL context; halt if absent.
- Idempotence key: SEC accession number.
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
- Idempotence key: `sha256` (UNIQUE in `documents`). Re-uploading identical bytes is a no-op; modified bytes for the same `(ticker, doc_type, period_end)` write a new row and supersede the previous one — never mutate.
- Manual uploads where ticker, doc_type, **and** period_end cannot all be determined from filename + first-page fingerprint are quarantined to `ir_documents/_unsorted/` with a `.error.json` sidecar. They are **not** registered in `documents` and **not** silently merged with any existing row — the user must repair (rename the file, extend the issuer registry, or delete the upload) and re-run.
- The IR step is **optional**: tickers with no `ir_doc` rows in `documents` proceed through the rest of the pipeline (FMP, SEC, transcripts) without any IR-derived facts. Downstream consumers must `LEFT JOIN` against `ir_doc` rows, never `INNER JOIN`.

### `transcript_audio`
- Audio land in `transcripts/raw/audio/`, transcripts in `transcripts/raw/text/`.
- Speaker attribution **must** be preserved in `transcript_segments`.
- Time codes preserved when available.
- `source_url` of the audio recording stored in `documents.source_url`.
- Whisper transcription writes a new `documents` row with `source_type=transcript_audio` and `parent_document_id` = the audio document.

### `manual_*`
- Idempotence key: `(ticker, doc_type, period_end, user_id, submitted_at)`.
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
