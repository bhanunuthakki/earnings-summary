# Directive: Fetch IR Documents

## Goal

Acquire investor relations documents (press releases, earnings presentations, supplements,
shareholder letters, transcripts) for tracked names through two parallel paths:

1. **Auto-fetch** — download PDFs from official IR websites for the 11 tracked portfolio
   holdings, covering the last 8 quarters. Files land in `ir_documents/{TICKER}/{period}/`
   and are registered in the canonical `documents` SQLite table (`source_type='ir_doc'`).
2. **Manual upload** — the user drops PDFs/XLSX at the root of `ir_documents/` for a
   subset of names (typically portfolio + selected watchlist). The
   `categorize_ir_uploads.py` step classifies and routes them through the same
   provenance contract as auto-fetch.

The IR pipeline as a whole is **optional** on the broader project: when neither path
yields any documents for a ticker, the rest of the analysis (FMP, SEC, transcripts) runs
without IR data — IR rows simply don't appear in `documents` for that ticker.

## Auto-fetch coverage (validated live 2026-06-04, full portfolio + evaluation list)

The headless crawler was empirically validated against every tracked portfolio +
evaluation IR site. URLs live in `src/ir_pipeline/ir_url_overrides.py`.

**25 of 32 auto-fetch multi-quarter IR docs** (✓): AMZN, GOOG, META, MELI, NU, NVO, BN,
RBRK, VEEV, WIX (portfolio) + V, ORCL, FCX, BKNG, UBER, ABNB, NTDOY, SOFI, NTRA, TMO,
CGEH, CRWV, DLO, NSP, NBIS (evaluation). Mostly q4cdn / Investis / mz platforms; the
crawler walks the IR landing → quarterly/financials page and harvests the PDFs.

**7 of 32 do NOT auto-fetch — documented reason: issuer IR site employs bot-protection /
anti-headless measures** (verified by direct probe; we respect it, no evasion):

| Ticker | Probe result | Reason |
|---|---|---|
| NOW (ServiceNow) | HTTP **403** to headless | `investors.servicenow.com` blocks automated requests |
| LLY (Lilly) | load-stall / timeout | IR site never completes load under headless chromium |
| TEM (Tempus) | HTTP2 error + timeout | server mis-negotiates + stalls headless |
| WGS (GeneDx) | timeout | anti-headless stall |
| FIGR (Figure) | timeout | anti-headless stall |
| FRVO (Fervo) | timeout | anti-headless stall (recent IPO) |
| BHP | timeout | foreign (half-yearly) + anti-headless stall |

For these 7, financial data still flows via the existing **FMP/SEC pipeline** (the IR
auto-fetch is an optional enhancement). Evading bot-protection (stealth browsers, proxies)
is deliberately out of scope. Re-validate periodically — sites change their protections.

## Target Holdings & IR Pages

| Ticker | IR URL | Notes |
|---|---|---|
| AMZN | https://ir.aboutamazon.com/quarterly-results/default.aspx | Calendar year; press releases + 10-Q/K filings + slide decks |
| GOOG | https://abc.xyz/investor/ | Alphabet; press releases + slide decks in Events section |
| META | https://investor.atmeta.com/investor-news/ | Clean quarterly pages |
| MELI | https://investor.mercadolibre.com/financial-information/quarterly-results | Calendar year |
| NU | https://ir.nu.com.br/en/financial-information/quarterly-results/ | Calendar year; best-effort historical coverage |
| NVO | https://investor.novonordisk.com/financial-reports | Semi-annual reports (map H1→Q2, 9M→Q3, FY→Q4) |
| NOW | https://investors.servicenow.com/financial-information/quarterly-results | Calendar year |
| WIX | https://investors.wix.com/financial-information/quarterly-results | Calendar year |
| RBRK | https://ir.rubrik.com/financial-information/quarterly-results | Jan FY-end; IPO May 2024; best-effort for pre-IPO quarters |
| VEEV | https://ir.veeva.com/ | Jan FY-end; map FY quarters to calendar year |
| BN | https://bam.brookfield.com/investors | Brookfield Corp; supplemental packages |
| LLY | https://investor.lilly.com/financial-information | **Watchlist (not held)** — tracked as competitive cross-check for NVO thesis (tirzepatide/retatrutide vs semaglutide). Calendar year. See `directives/nvo_external_sources.md`. |

## Fiscal Calendar Notes

| Ticker | Fiscal Year End | Quarter Mapping |
|---|---|---|
| AMZN / GOOG / META / MELI / NU / NVO / NOW / WIX | Calendar (Dec 31) | Q1=Jan-Mar, Q2=Apr-Jun, Q3=Jul-Sep, Q4=Oct-Dec |
| VEEV | January 31 | FY26 Q1=Feb-Apr 2025, FY26 Q2=May-Jul 2025, FY26 Q3=Aug-Oct 2025, FY26 Q4=Nov 2025-Jan 2026 |
| RBRK | January 31 | FY26 Q1=May-Jul 2025, FY26 Q2=Aug-Oct 2025, FY26 Q3=Nov 2025-Jan 2026, FY26 Q4=Feb-Apr 2026 |
| BN | Calendar (Dec 31) | Standard |
| NVO | Calendar (Dec 31) | Publishes Q1, H1, 9M (Q3), FY (Q4) releases |

## Target Quarters

8 quarters back from Q1 2026 (current): Q2 2024, Q3 2024, Q4 2024, Q1 2025, Q2 2025, Q3 2025, Q4 2025, Q1 2026.

## Document Types

`source_type` is always `ir_doc` for both paths. The DB-canonical `doc_type` enum
(`src/models/documents.py::DocType`) covers:

| DocType (canonical) | Legacy index alias | Description | Priority |
|---|---|---|---|
| `IR_PRESS_RELEASE` | `press_release` | Quarterly earnings press release or financial results PDF | High |
| `IR_PRESENTATION`  | `presentation`  | Earnings slide deck / investor presentation PDF | High |
| `IR_TRANSCRIPT`    | `transcript`    | IR-published earnings call transcript PDF (text) | High (YouTube/Whisper fallback) |
| `IR_SUPPLEMENT`    | (none)          | Financial supplement / data workbook (XLSX or PDF) | Medium |
| `IR_INVESTOR_UPDATE`| (none)         | Shareholder letter, annual report, investor day deck | Medium |

`IR_SUPPLEMENT` and `IR_INVESTOR_UPDATE` aren't yet wired into the legacy
`process_ir_documents.py` LLM step, but they are first-class citizens of the
`documents` table for downstream analytical queries.

## Path A: Auto-fetch (headless discovery → download → register)

Fully automated and headless — no per-company manual browser session. Wired into
the weekly cron (`cron/discover_ir_documents.task.xml`, Sunday 01:30) and a
best-effort step on onboard (`execution/onboard_ticker.py`, `--skip-ir` to skip).

1. **Headless discovery** (`execution/discover_ir_documents.py --ticker <X>`):
   resolves the issuer's IR URL (curated map in `src/ir_pipeline/ir_url_overrides.py`
   → `ir_config.results_center_url` → `tracked_companies.ir_url`), then runs the
   **hybrid** discovery in `src/ir_pipeline/discover/`: the precise `mz` adapter's
   current-quarter links **plus** a generic Playwright crawler
   (`discover/generic.py`) that renders ANY IR page, harvests every document link
   (.pdf/.xlsx/CDN/filemanager), follows obvious "quarterly results / archive"
   sub-links one hop, and classifies each by filename + link text. Merges the
   results into `.tmp/ir_url_manifest/<TICKER>_urls.json` (append-only, URL-keyed).
   Best-effort: no IR URL → `no_ir_url` (exit 0); a JS-widget site that exposes
   only the current quarter accumulates history across weekly runs.
2. **Download + register** (`execution/fetch_ir_documents.py --ticker <X> --categorize`):
   downloads each manifest URL into the staging folder `ir_documents/<TICKER>/`
   (extension from the response headers), records the source URL in
   `.tmp/ir_incoming_urls.json`, then hands the ticker to `categorize_ir_uploads.py`
   — which **content-classifies** each file (authoritative), moves it to the
   canonical `ir_documents/<TICKER>/<period_end_iso>/<doc_type>__<sha8>.<ext>`, and
   inserts the `documents` row (`source_type='ir_doc'`, real `source_url`) + the
   legacy JSON-index mirror. `--calendar <id>` (FYE-derived) lets a ticker not yet
   in `ISSUER_REGISTRY` register best-effort.
3. **Batch** (`execution/discover_ir_documents_all.py`): the scheduled entry point.
   Reads the portfolio+evaluation roster from the DB at run time (so new eval names
   are auto-included), runs steps 1–2 per ticker subprocess-isolated, never aborts
   on one ticker's failure; exit code = count of FAILED tickers.
4. **Idempotency**: a URL already in `documents.source_url` is skipped; identical
   bytes are a sha256-keyed no-op. Re-running discovery or the batch is safe.

## Path B: Manual upload (Categorize → Register)

1. **Drop**: User places any combination of `.pdf` / `.xlsx` files at the root of `ir_documents/`. Filenames may be arbitrary — UUID exports, IR-CDN hex names, vendor-specific conventions — the categorizer doesn't need them to follow a schema.
2. **Categorize** (`execution/categorize_ir_uploads.py`):
   - Filename heuristics (RBRK-/ServiceNow-/novo-nordisk- prefixes, Wix CDN hex, NU's `NQYY Results Presentation` convention) decide ticker when possible.
   - First-page content fingerprint (first ~2 PDF pages or first xlsx sheet) confirms ticker via the issuer-name registry, identifies doc_type via cover-page phrase rules, and locates period via quarter/date/fiscal regex.
   - Files where ticker, doc_type, and period all resolve are moved to `ir_documents/{TICKER}/{period_end_iso}/{doc_type}__{sha8}.{ext}` and a `documents` row is inserted (`source_type='ir_doc'`, `source_url='manual_upload:{original-filename}'`).
   - Files that fail to resolve are quarantined under `ir_documents/_unsorted/` next to a `.error.json` sidecar carrying the failure reason and partial evidence — never silently dropped, never guessed.
3. **Idempotency**: sha256-keyed unique constraint on `documents`. Re-uploading identical bytes is a no-op; modified content writes a new row and supersedes (never mutates).
4. **Optionality**: When the root has no uncategorized files, the step prints `{"status":"empty",...}` and exits 0 — orchestrator continues.

## Issuer-name registry (manual-upload classifier)

The classifier substring-matches first-page text against this closed registry
(`src/ir_uploads.py::ISSUER_REGISTRY`). Adding a name = adding a tuple. No LLM,
no fuzzy matching — a name either appears or doesn't.

| Ticker | Cover-page substrings used as issuer signal | Calendar |
|---|---|---|
| MELI | `MercadoLibre`, `Mercado Libre` | calendar |
| NU   | `Nu Holdings`, `Nu's Investor`, `Nubank` | calendar |
| RBRK | `Rubrik` | rubrik (FY-end Apr 30) |
| NOW  | `ServiceNow` | calendar |
| WIX  | `Wix.com`, `Wix Ltd`, `Wix's`, `Wix ` | calendar |
| NVO  | `Novo Nordisk`, `Amounts in DKK million` | nvo (Q1/H1/9M/FY) |
| GOOG | `Alphabet Inc`, `Alphabet's` | calendar |
| META | `Meta Platforms`, `Meta Reports` | calendar |
| AMZN | `Amazon.com`, `AMAZON.COM` | calendar |
| VEEV | `Veeva Systems`, `Veeva ` | veeva (FY-end Jan 31) |
| BN   | `Brookfield Corporation`, `Brookfield Asset Management` | calendar |

## Output Schema (URL Manifest JSON)

```json
[
  {
    "ticker": "GOOG",
    "year": 2025,
    "quarter": "Q3",
    "doc_type": "press_release",
    "url": "https://...",
    "fiscal_label": null,
    "note": null
  }
]
```

## Edge Cases & Constraints

- **Rate limiting**: 0.5 second pause between downloads. Never more than 10 requests per minute to any single domain.
- **robots.txt**: Always respect. These are all major public company IR pages; PDF downloads are explicitly intended for investor use.
- **Auth**: Never attempt to bypass authentication. If a page requires login, skip and log.
- **Content type**: Some PDF links may serve `text/html`. Download anyway and attempt to parse; if PyPDF fails, log and skip processing.
- **RBRK/NU sparse history**: Skip gracefully for quarters before the company's IPO or IR page launch. Do not fabricate entries.
- **NVO H1/FY structure**: Map: H1 interim → Q2, 9-month → Q3, Full Year annual → Q4. Q1 release (sales update only) → Q1.

## Verification

After running `fetch_ir_documents.py --verify --ticker <X>`:
- All registered documents have a `✓` (file exists locally).
- No `✗ missing` entries without an explanation (e.g., pre-IPO).
