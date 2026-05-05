# Directive: NVO External Data Sources (Patent Extensions + Script Volume + Share)

## Goal

Capture external data feeds required by the revised NVO thesis (see `micro_thesis/holdings/NVO.json`) which depend on signals that are not available in standard IR documents:

1. **Global GLP-1 script (TRx) volume** — diabetes + obesity, by molecule
2. **Patent extension status** in 2026 LOE markets (China, India, Brazil, Canada)
3. **Market share** — diabetes (vs Lilly tirzepatide) and obesity (vs Zepbound)
4. **Volume vs price decomposition** — Wegovy/Ozempic units shipped vs CER price/unit

These signals drive the T1 KPIs and break conditions for NVO. Without them, the holding is monitored on lagging revenue numbers only.

## Data Sources & Cost Structure

### Free / scrape-able

| Source | Coverage | Cadence | Implementation |
|---|---|---|---|
| FDA Orange Book API | US patent expiry by NDC | On-demand | `https://api.fda.gov/drug/ndc.json` — query by drug name (Ozempic, Wegovy, Rybelsus) |
| EPO Patent Register | EU patent status | Manual or scrape | `https://register.epo.org/` — semaglutide patent families |
| WIPO PATENTSCOPE | Global patent search | Manual or scrape | `https://patentscope.wipo.int/` — covers most jurisdictions |
| Brazil INPI | Brazil patent register | Manual or scrape | `https://busca.inpi.gov.br/` |
| China CNIPA | China patent register | Manual (CN-only UI) | `https://english.cnipa.gov.cn/` for English summary |
| India IPO | India patent register | Scrape | `https://ipindia.gov.in/` |
| Canada CIPO | Canada patent register | Scrape | `https://www.ic.gc.ca/opic-cipo/cpd/` |
| NVO annual report (existing pipeline) | NVO's own patent expiry timeline | Annual | Already captured in `ir_documents/NVO/` — extract patent table from annual report |
| LLY earnings transcripts (existing pipeline) | Lilly script-volume / share commentary as cross-check | Quarterly | Add LLY to `fetch_ir_documents.md` target list |
| FDA approval calendar | CagriSema NDA status | On-demand | `https://www.fda.gov/drugs/development-approval-process-drugs` |

### Paid (PERMANENTLY DEFERRED per user decision 2026-05-04)

User decision: stick to free search only. Paid sources are out of scope and will not be implemented.

| Source | Coverage | Status |
|---|---|---|
| IQVIA NPA / NSP | Weekly TRx, NRx by molecule, US | Permanently deferred |
| Symphony Health PHAST | Similar to IQVIA, US | Permanently deferred |
| Bloomberg / FactSet drug-script data | Limited script data via terminal | Permanently deferred |
| EvaluatePharma | Forecasted scripts and share | Permanently deferred |

## Recommended Implementation Phasing

### Phase 1: Free sources only (immediate)

1. **`execution/fetch_drug_patent_status.py`** — single-purpose CLI:
   - Args: `--molecule semaglutide --jurisdictions US,EU,CN,IN,BR,CA`
   - Pulls FDA Orange Book + EPO + WIPO + INPI + IPO + CIPO patent records
   - Output: `.tmp/nvo_patents/<molecule>_<run_date>.json` validated against Pydantic model
   - Refresh cadence: monthly (patents don't change daily); idempotency key `{molecule}_{jurisdiction}_{run_date}`
   - Rate limit: 1 req/s per jurisdiction, max 60 req total per run

2. **`execution/extract_nvo_patent_timeline.py`** — extract NVO's own patent expiry table from the latest annual report:
   - Reads `ir_documents/NVO/<latest_year>_FY/press_release.pdf` or annual report
   - Uses LLM to extract structured patent table (molecule, jurisdiction, expiry date, extension status)
   - Output: `.tmp/nvo_patents/nvo_self_disclosed_<run_date>.json`
   - Refresh cadence: annual (after FY release)

3. **Add LLY to `directives/fetch_ir_documents.md`** target list:
   - Cross-check competitive script-volume commentary in LLY earnings calls
   - LLY reports tirzepatide volume + share commentary that's the cleanest competitive proxy

4. **`execution/extract_market_signals_from_transcripts.py`** — LLM-based extraction:
   - Reads NVO + LLY transcripts from existing pipeline
   - Extracts structured signals: script-volume mentions, price/unit commentary, share commentary, manufacturing capacity
   - Output: `.tmp/glp1_market_signals/<ticker>_<period>.json`
   - Refresh cadence: per-quarter (after each transcript fetch)

### Phase 2: Paid sources (CANCELLED)

Cancelled per user decision 2026-05-04. Phase 1 free-source signal quality is what we work with. If phase 1 proves insufficient for a specific KPI, the directive is to weaken the KPI's break threshold or replace it — not to revisit paid licensing.

## Schema (Phase 1 outputs)

### Patent status

```python
class PatentRecord(BaseModel):
    molecule: str  # e.g. "semaglutide"
    jurisdiction: Literal["US", "EU", "CN", "IN", "BR", "CA"]
    patent_number: str
    title: str
    grant_date: date
    expiry_date: date
    extension_status: Literal["original", "extended", "pending_extension", "expired", "unknown"]
    extension_basis: Optional[str]  # e.g. "pediatric exclusivity", "SPC"
    source_url: str
    pulled_at: datetime
```

### Market signal (transcript-extracted)

```python
class MarketSignal(BaseModel):
    ticker: Literal["NVO", "LLY"]
    period: str  # e.g. "2026Q1"
    signal_type: Literal["script_volume", "price_per_unit", "market_share", "manufacturing_capacity", "patent_commentary"]
    molecule: Optional[str]
    geography: Optional[str]
    metric_text: str  # the verbatim quote
    parsed_value: Optional[float]
    parsed_unit: Optional[str]
    confidence: Literal["high", "medium", "low"]
    transcript_filename: str
    extracted_at: datetime
```

## Idempotency & State

- Idempotency key for patent fetches: `{molecule}_{jurisdiction}_{run_date}` (date, not datetime — daily granularity is more than sufficient for patent monitoring)
- Idempotency key for transcript signals: `{ticker}_{period}_{signal_hash}` where signal_hash dedupes if the same transcript is re-extracted

## Failure Modes

- **Jurisdiction site down or schema-changed**: Halt that jurisdiction, dump raw response to `.tmp/nvo_patents/_failures/`, surface to user. Do not silently fail-over to a different source.
- **LLM extraction returns no signals**: Could be valid (no signals in that quarter) or schema drift. Compare to prior-quarter signal count; if drops by >75% YoY, flag for manual review.
- **Patent record contradicts NVO self-disclosure**: Record both, surface discrepancy. Do not auto-resolve.

## User Decisions (resolved 2026-05-04)

| Question | Decision |
|---|---|
| Procurement appetite for IQVIA / Symphony? | **No.** Free sources only. Phase 2 cancelled. |
| Add LLY to watchlist for cross-check? | **Yes.** LLY added to `tracked_companies` (list_type=watchlist) on 2026-05-04 and added to `directives/fetch_ir_documents.md` target list. |
| Cadence for patent monitoring? | **Monthly** (default accepted). |

## Status

- Directive: drafted 2026-05-04
- LLY watchlist + IR target: COMPLETE 2026-05-04
- Phase 1 scripts (`fetch_drug_patent_status.py`, `extract_nvo_patent_timeline.py`, `extract_market_signals_from_transcripts.py`): NOT YET WRITTEN — implementation awaits explicit user sign-off
- Phase 2: CANCELLED
