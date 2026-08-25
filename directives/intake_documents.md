# Directive: Intake User-Dropped Documents

## Goal

Provide a single drop folder where the user can place any user-supplied artifact
(IR PDF, earnings transcript text, or earnings-call audio) and have it classified,
renamed, and filed into the canonical pipeline layout — without thinking about
ticker conventions, period-end dates, or doc-type slugs.

## Inputs

| Input | Where |
|---|---|
| Any PDF / .txt / .mp3 / .m4a / .wav | `_inbox/` |
| Gemini API key | `.env` → `GEMINI_API_KEY` |

The user does **not** pre-format the filename. The intake handler infers
`(ticker, period_end, doc_type)` from filename heuristics plus an LLM read of the
first ~6 KB of document text.

## Tools / Scripts

| Purpose | Script |
|---|---|
| **Primary intake CLI** | `execution/intake_documents.py` |
| Classification + filing logic | `src/intake.py` |
| LLM classifier | `llm_client.classify_intake_document` |

### intake_documents.py responsibilities

1. Scan `_inbox/` (default) or `--inbox <path>`.
2. For each supported file:
   - **PDF / TXT** → classify via `classify_intake_document` → file into
     `ir_documents/<TICKER>/<period_end_iso>/ir_<doctype>__<sha8>.<ext>` and
     register in `document_index.json` with `source = USER_INTAKE`.
   - **Audio** → derive `(ticker, quarter, year)` from filename only → file
     into `transcripts/raw/<TICKER>_Q<n>_<YYYY>.<ext>` for the legacy whisper
     pipeline. (We do not block intake on whisper — that's a downstream stage.)
3. Files with confidence < 0.6 stay in `_inbox/` next to a `.error.json`
   marker. The user can rename and re-run.
4. With `--process`, chain into `execution/process_ir_documents.py --ticker <T>`
   for each ticker that received new IR documents.

## Doc-Type Vocabulary

Closed enum, matches `src/models/documents.py::DocType`:

| DocType | Filename stem | Index key | Cache suffix | Storage |
|---|---|---|---|---|
| `IR_PRESS_RELEASE` | `ir_press_release` | `press_release` | `press_release_summary.txt` | `ir_documents/<TICKER>/<period_end>/` |
| `IR_PRESENTATION` | `ir_presentation` | `presentation` | `presentation_brief.txt` | `ir_documents/<TICKER>/<period_end>/` |
| `IR_SUPPLEMENT` | `ir_supplement` | `supplement` | _capture-every-number: `extract_kpis_from_ir.py --capture` (PDF) / `refresh_ir_kpis.py` (xlsx)_ | `ir_documents/<TICKER>/<period_end>/` |
| `IR_INVESTOR_UPDATE` | `ir_investor_update` | `investor_update` | `investor_update_summary.txt` | `ir_documents/<TICKER>/<period_end>/` |
| `EARNINGS_CALL_TRANSCRIPT` | `ir_transcript` | `transcript` | `summary.txt` | `ir_documents/<TICKER>/<period_end>/` |
| `IR_EVENT` | `ir_event` | `event` | `event_brief.txt` | `ir_documents/_events/<TICKER>/<event_date>/` |

`investor_update` reuses the press-release LLM path (semantically similar:
quarterly results announcement / letter to shareholders).

`ir_event` covers non-quarterly artifacts: investor days, AGMs, capital markets days,
conference decks, ad-hoc strategic announcements, M&A or stock-split decks. These
are stored under `ir_documents/_events/<TICKER>/<event_date>/` (not the quarterly
period-end tree) and indexed under their own keyspace
(`{TICKER}_event_{event_date}_{sha8}`) since multiple PDFs can share an event date.

## Outputs

| Artifact | Location |
|---|---|
| Filed IR document | `ir_documents/<TICKER>/<YYYY-MM-DD>/ir_<doctype>__<sha8>.<ext>` |
| Filed audio | `transcripts/raw/<TICKER>_Q<n>_<YYYY>.<ext>` |
| Index entry | `.tmp/document_index.json` (`source = USER_INTAKE`) |
| Per-file error marker | `<source>.error.json` next to source in `_inbox/` |

## Error Markers

A file that cannot be classified stays in `_inbox/` next to a `.error.json`:

```json
{
  "reason": "low_confidence" | "classification_failed" | "validation_failed" |
            "empty_no_filename_hint" | "audio_filename_unclassifiable",
  "ticker_hint": "BN" | null,
  "quarter_hint": 3 | null,
  "year_hint": 2025 | null,
  "text_sample": "first 200 chars of extracted text"
}
```

The user fixes the filename (`mv "ambiguous.pdf" "BN Q3 2025 Letter.pdf"`) and
re-runs `intake_documents.py`. The marker is cleared on successful intake.

## Edge Cases & Constraints

- **Strict typing**: `IntakeClassification` is a Pydantic model. Any LLM response
  that fails schema validation is treated as a classification failure (no silent
  fallback). The doc_type field is the closed `DocType` enum — never a free string.
- **No substring classification**: filename keyword matching (`"transcript" in filename.lower()`)
  is forbidden. Use the LLM with structured output for `doc_type` decisions.
- **Identity and repeat safety**: the Logical Idempotency Key is the stable intake
  submission plus resolved `(ticker, doc_type, period_end)`; SHA-256 of exact bytes is
  the Content Identity; resolved source/period plus fetched-at knowledge time is the
  Observation Version; each CLI invocation has a unique Attempt Identity. The
  destination's `<sha8>` is a Content Identity duplicate guard, so re-dropping the
  same bytes is a no-op.
- **Fiscal-calendar quirks**: VEEV / RBRK (Jan FYE), NVO (H1/9M periods) — handled
  by the LLM via the period-end mapping in the prompt. The classifier returns a
  date, not a fiscal label, so downstream code never has to translate.
- **Empty PDFs**: scanned-image PDFs with no extractable text → `empty_no_filename_hint`
  unless the filename has a strong ticker hint, in which case the LLM still tries
  on filename + hint alone.
- **Audio**: no LLM call. Filename must contain a parseable `(ticker, Qn, YYYY)`.
  If it doesn't, the file gets an error marker. (Whisper transcription is part of
  the legacy pipeline, not intake.)

## Verification

After running `python execution/intake_documents.py`:

- [ ] `_inbox/` contains only files with `.error.json` markers (or is empty).
- [ ] Each filed PDF/TXT is at `ir_documents/<TICKER>/<period_end>/ir_<doctype>__*.{ext}`
      and shows `source = USER_INTAKE` in `.tmp/document_index.json`.
- [ ] `--dry-run` prints the planned moves without touching files.
- [ ] Re-running on an already-processed file is a duplicate-skip (no second copy).
