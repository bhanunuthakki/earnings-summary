# Directive: Fetch IR Documents

## Goal

Download investor relations documents (press releases, earnings presentations, and transcripts)
from official company IR websites for the 11 tracked portfolio holdings, covering the last 8 quarters.
All documents are saved to `ir_documents/` and registered in `.tmp/document_index.json`.

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

| doc_type | Description | Priority |
|---|---|---|
| `press_release` | Quarterly earnings press release or financial results PDF | High |
| `presentation` | Earnings slide deck / investor presentation PDF | High |
| `transcript` | IR-published earnings call transcript PDF | High (YouTube/Whisper fallback) |

## URL Discovery → Download Flow

1. **Browser discovery** (one session per company, run once): Navigate IR pages, extract direct PDF URLs, save to `.tmp/ir_url_manifest/<TICKER>_urls.json`.
2. **Download** (`execution/fetch_ir_documents.py --ticker <X>`): Reads manifest, downloads PDFs to `ir_documents/<TICKER>/<YEAR>_<QUARTER>/<doc_type>.pdf`, registers in `document_index.json`.
3. **Idempotency**: If file already exists locally, skip. Manifest can be re-run safely.

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
