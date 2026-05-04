# Data Provenance Contract

**Status**: Layer 1 baseline. Cross-cutting — every Layer 2 orchestrator and Layer 3 execution script must comply. Immutable without explicit user authorization.

**Why this exists**: The portfolio aims (DCF, segment OI, ROE/ROA, leading-indicator KPIs) require fusing data from FMP, SEC XBRL, IR PDFs, audio transcripts, and manual entries. Without per-fact provenance, you cannot resolve disagreements between sources, you cannot tell whether a "Cloud OI" figure came from the 8-K supplement or the 10-K segment note, and you cannot defensibly audit the thesis tracker.

## 1. Source-type taxonomy (closed enum)

Defined in `src/models/documents.py::SourceType`. Never substring-match. Never store as freeform text.

| value | meaning |
|---|---|
| `fmp` | Financial Modeling Prep API endpoint response (JSON) |
| `sec_xbrl` | SEC EDGAR XBRL filing (10-K, 10-Q, 20-F, 40-F, 8-K, 6-K) — primary issuer source |
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

### `ir_doc`
- `doc_type` ∈ `{IR_PRESS_RELEASE, IR_PRESENTATION, IR_SUPPLEMENT, IR_INVESTOR_UPDATE}`. Files land in `ir_documents/{TICKER}/{period_end_iso}/`.
- `source_url` **required** in `documents.source_url`.
- Idempotence key: `(ticker, doc_type, period_end, sha256)`.

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

Subsequent migrations backfill from `data/historical/sec/`, `ir_documents/`, and `transcripts/processed/` similarly.
