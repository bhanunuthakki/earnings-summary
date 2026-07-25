# Longitudinal Filing-Language & Metric-Change Tracking — Data Audit + Scope

Status: DRAFT scoping doc (data audit complete, 2026-07-24). No pipeline built yet.
Owner ask: track changes in reporting language and metrics across filings (10-K/10-Q and
20-F/6-K), LLM-assisted, splitting noise from possible signal. Sequenced data-first.

## 1. Data audit — what exists today (measured, prod data)

### 1.1 Section-partitioned filing data by source

| Source | What it is | Partition granularity | Coverage (measured) | Forms |
|---|---|---|---|---|
| FMP `financial-reports-json` (`data/historical/fmp/{T}_form_10k_{Y}.json`, `_form_10q_{Y}_{Q}.json`) | As-filed XBRL **R-file section dump**: statements + footnotes + `(Tables)`/`(Details)` blocks, keyed by the filing's own section headings | 80–130 sections per 10-K/20-F; ~50 per 10-Q | **673 annual files / 89 tickers** (2016–2025 for most US names; NVO/WIX 20-Fs served through the same endpoint). **13 quarterly files / 2 tickers** (META, UBER) — backfillable for 10-K names via `execution/fetch_fmp_10q_json.py` | 10-K, 10-Q, 20-F (normalized under the `form_10k` filename) |
| SEC EDGAR narrative text (`src/filing_text_fetcher.py` → `data/sec_text/`) | Full primary-doc HTML → plain text + regex Item extraction (Item 1A / 7 / 8 only) | Item-level, 3 items, 10-K only | Cache has **6 tickers, none portfolio** (discovery names). No 10-Q, no 20-F, no 6-K support in the fetcher | 10-K (+ DEF 14A, S-1 full-text without item split) |
| SEC EDGAR 6-K exhibits (`pipeline.sec_6k_fetch` → `data/historical/sec/{T}_6k_{date}.html`) | Full earnings-release exhibit HTML, per-ticker filename heuristics | Whole exhibit (no section split) | **NU ×6 quarters, NVO ×7 quarters** (2024-05 → 2026-05), 1.2–2.3 MB each, registered in `documents` | 6-K (NU, NVO only; ASML confirmed image-only ⇒ unusable) |
| SEC XBRL companyfacts (`data/historical/sec/{T}_companyfacts.json`, `pipeline.sec_xbrl`) | Every tagged numeric fact with `form`, `fy`, `fp`, `accn` labels | Tag-level (metric), not section | **113 tickers**. META: 471 tags, 17.7k points (10-Q 11.3k / 10-K 6.4k). NVO: 255 tags incl. **1.85k 6-K-tagged points**. WIX: 365 tags, 20-F only. NU: 151 tags, **20-F only — its 6-Ks carry no XBRL**. BN: 6-K 3.0k + 40-F 3.3k | all forms, incl. 20-F/40-F/6-K where tagged |
| `documents` table (accession registry) | Row per filing accession (mostly synthetic `file_path` pointers into companyfacts — no narrative behind them) | Document-level | sec_10q 3,018 / 76 tk · sec_10k 1,048 / 75 tk · sec_20f 157 / 17 tk · sec_6k 114 / 17 tk · sec_40f 74 / 9 tk · fmp_10k_json 663 / 88 tk · **fmp_10q_json 0 rows** (13 disk files unindexed — `fmp_doc_index` gap) | all |

### 1.2 Existing longitudinal machinery (mostly dormant)

- `execution/extract_risk_factors.py` — full YoY Item 1A diff pipeline (unchanged/new/removed/reworded via body-sha + heading match, LLM reword narration) → `risk_factors` table. **Table is EMPTY in prod — never run.**
- `execution/extract_footnotes.py` — Item 8 → `footnote_facts` via Sonnet. **Table EMPTY.**
- `src/synthesis/lenses/filing_diff_narrative.py` — consumer of `risk_factors` diffs. Dormant (no data).
- Numeric side that IS live: same-doc fact correction + QoQ guard (PR #916), restatement detector, `validation_engine` — these compare **values**, not language or section structure.
- `compute/segment_crosstabs_llm.py` — reads FMP sections by keyword match (segment/geographic/disaggregation) with the known truncated-key blind spot.

### 1.3 Measured consistency of the FMP section partition

- **Keys truncate at ~31 chars** (75% of META FY25 keys hit the cap) with order-dependent `_N` suffixes (23% of keys). Exact-key YoY overlap: 75–91%. After stripping `_N` and comparing stems: **85–98%** (NVO 98%, MELI 91%, META 85%). ⇒ Cross-year section joins must match on normalized stem + content, never raw key. The residual 2–15% stem churn is *real* section add/remove — itself a signal (e.g. META's Cybersecurity Risk Management section appearing post-2023).
- FMP note sections carry **partial footnote prose** (long text cells inside note R-files) but **zero Item 1 / 1A / 7 narrative** — MD&A and Risk Factors are structurally absent from this product.

### 1.4 SEC vs FMP — the decision table

| Dimension | SEC EDGAR | FMP financial-reports-json |
|---|---|---|
| Exhaustiveness | Full document, every Item, every form, full history | Statements + footnotes only; no MD&A/1A/business narrative |
| Section partition | Must be derived (regex per form taxonomy + LLM fallback) — `filing_text_fetcher` proves the pattern | Pre-partitioned by the filer's own R-file headings (this is genuinely valuable — it IS the note structure) |
| Section naming stability | Item numbering is standardized per form: 10-K Items 1–16; 10-Q Part I/II; **20-F Items 3–18 (3.D = risk factors, Item 5 = OFR/MD&A-analog)**; **6-K has NO mandated structure** (free-form exhibits, per-ticker conventions, image-deck risk) | Truncated keys, `_N` instability; stems stable 85–98% |
| FPI interim (Q1–Q3) | 6-K exhibits (only structured source; NU/NVO proven, ASML image-only) | **Structurally absent** — 20-F filers file no 10-Q equivalent (Phase-3 spike finding, segment_quarterly_framework.md) |
| Access | Free, no tier gates, rate-limit etiquette only | Tier-gated: `financial-reports-form-10-k-json` currently returns **forbidden for NU/NVO/WIX** on this key (FPI annual refresh dead at current tier; META fine). FMP Starter purchase still pending owner |
| Metric longitudinal | companyfacts: per-tag, form-labeled, incl. IFRS (`ifrs-full`) for FPIs; dense and already cached for 113 tickers | Statement values normalized (separate endpoints), as-reported R-files for detail |

**Verdict:** EDGAR full text is the only viable substrate for *language* tracking (and the only quarterly narrative for FPIs is the 6-K exhibit we already cache). FMP's R-file partition is the right substrate for *note/table structure* tracking (section appear/disappear, table shape drift) — treat the two as complementary partitions, not alternatives. Metric-level tracking rides companyfacts (tag first-appearance/disappearance/redefinition) — no LLM needed for detection, LLM only for interpretation.

## 2. Gaps blocking longitudinal tracking (data-first order)

1. **No durable section store.** Narrative sections live transiently in `FilingTextResult`; nothing queryable across periods. (`data/sec_text/` covers 6 non-portfolio tickers.)
2. **Fetcher is latest-10-K-only.** No fetch-by-accession, no 10-Q, no 20-F item taxonomy, no historical walk. `documents` already has 4,000+ accession rows to walk.
3. **FPI narrative parity:** 6-K exhibits cached but never section-split; 20-F narrative never fetched (1 ad-hoc NU HTML exists).
4. **Dormant diff pipelines never run** (`risk_factors`, `footnote_facts` empty) — the Item 1A YoY diff design is proven code, just unexecuted and 10-K-only.
5. **`fmp_10q_json` unindexed** in `documents` (13 files) and 10-Q backfill never run beyond META/UBER.
6. **No cross-period section matcher** (needed for both partitions; FMP stems + content similarity, EDGAR item ids + note-title normalization).

## 3. Build

### Phase 0 — durable section-partition store (SHIPPED, migration 0198)

Two tables (`filing_sections`, `filing_section_coverage`), a `src/filings/` package, and two
Layer-3 CLIs. No LLM anywhere in Phase 0 — it is pure extraction, storage, and query.

| Piece | Where |
|---|---|
| Schema | `alembic/versions/0198_filing_sections.py` — no DB-level FKs (FK-poisoning invariant), UNIQUE `(source, source_ref, section_key_raw, ordinal)` |
| Typed contracts | `src/filings/models.py` — `SectionSource`, `FilingForm`, `FiscalPeriod`, `CoverageStatus`, the three error classes, `normalize_stem` |
| Per-form taxonomies | `src/filings/taxonomy.py` — 10-K Items 1–16, part-scoped 10-Q, 20-F Items 1–19 + 3.A–D / 5.A–E sub-items, each carrying a **cross-form `concept`** so a 10-K Item 1A and a 20-F Item 3.D share one `risk_factors` timeline |
| Narrative splitters | `src/filings/edgar_sections.py` — TOC-resistant chain scoring, part-aware 10-Q, 20-F sub-split + preamble, free-form 6-K |
| R-file parser | `src/filings/fmp_sections.py` — declared-`Document Type` wins over filename; drift raises |
| EDGAR fetch | `src/filings/edgar_fetch.py` — by-accession, classified failures (hard-stop / transient / contract) |
| Orchestration | `src/filings/ingest.py` — three independent lanes, reconciliation, coverage |
| Read layer | `src/filings/store.py` — `period_availability`, `section_timeline`, whole-document partition replacement |
| CLIs | `execution/ingest_filing_sections.py`, `execution/filing_sections_report.py` |
| Tests | `tests/test_filing_sections.py` (56, degradation-weighted) |

**Verified against real data** (prod-DB copy, 2026-07-24): META+NU FMP → 1,531 sections / 19 payloads;
NU+NVO 6-K exhibits → 1,301 sections / 12 exhibits; META 10-K ×2 → 36 items incl. Item 1A (196KB) and
Item 7 (61KB); WIX 20-F ×3 → Item 3.D `risk_factors` (233KB) + Item 5.A–D. NU's `form_10k`-named
payloads were correctly flagged `regime_mismatch_resolved_to_declared` and stored as 20-F.

**Header-matching corrections, 2026-07-25.** Re-partitioning all 125 cached EDGAR documents under
`data/sec_text/` and diffing against the stored rows found four defects in `taxonomy.locate_items`,
all of which produced a *silently wrong* partition rather than a flagged one:

| Defect | Effect | Fix |
|---|---|---|
| In-prose item citations accepted as headers | NU's 20-F wraps at ~135 chars, so the 200-char prose guard was inert and hundreds of `"Item 3. Key Information—D. Risk Factors"` references became rival headers | `taxonomy._is_citation` |
| Contents rows earned the per-item bonus | The winning chain took the contents page *and* the body; the item numbers were spent, so real body items were unreachable and their disclosure was absorbed by a neighbour (NU risk factors, FCX Items 1A/7) | `taxonomy._drop_contents_block` |
| Taxonomy order treated as evidence | NU prints Item 4, 4A, then 3 — Items 1–3 became unreachable. Measured across the corpus, order-scoring changed the outcome for NU only, and for the worse | order preference removed; uniqueness enforced explicitly |
| Title variants unmatched | ServiceNow writes "Qualitative and Quantitative" (7A) and "Consolidated Financial Statements" (8), so both boundaries vanished and Item 7 ran 120KB into the segment footnote. NU labels its risk factors "A.Risk Factors", not 3.D | title alternates; sub-item letter matched as *a* letter, not *the* letter |

Corpus effect: risk-factors coverage 112 → 116 of 125 documents, no document lost a section, and
contents-block excision (which fires on 109) never removes an item's last candidate.

**FCX (2026-07-25, follow-up).** FCX answers two items under one plural heading —
"Items 7. and 7A. Management's Discussion and Analysis…", and likewise "Items 1. and 2. Business and
Properties". Neither the plural nor the bridging second item number was tolerated, so the title
keyword was unreachable and the heading matched nothing: FCX had **no `mdna` section at all** for any
cached year, with `missing_mdna` the only hint. `_item_patterns` now accepts `items?` plus one
optional `and <n>` bridge. Corpus effect: FCX gains MD&A for all three years (142K / 141K / 150K),
zero change anywhere else.

**NVO remains unpartitioned, and an attempt to fix it was reverted.** Its 20-F has two compounding
problems: the renderer stamps running page-headers ("ITEM 4 INFORMATION ON THE COMPANY" ×8, "ITEM 5
…" ×6), and it carries a *partial* index (~8 items) that is too small to trip
`_drop_contents_block`'s ≥8-distinct-item bar while still being dense enough to anchor the chain.
A `_drop_running_headers` pass (keep only the first occurrence of a verbatim-repeated header line)
was written and **reverted**: it recovered only NVO's small `mdna` (~6KB) while *regressing* NU
FY2024 badly (risk factors 288,907 → 423,760, MD&A → 0), because "first occurrence is the real
heading" is false whenever an index row survives ahead of the body. NVO stays flagged
`slice_boundaries_suspect` / `missing_risk_factors`. Its risk section is 6.7KB and largely
incorporation-by-reference to the Danish Annual Report, so the disclosure lost is small — but it is
lost, and nothing downstream should treat NVO's absence as "no risk factors changed".

**Reconciliation with #1009**, which landed first and fixed the same NU/FCX symptoms independently.
Kept from it: the FCX fix, the wrapped-header `body_start` correction (found in both), the
trailing-page-number row test, and the `trivial_section_oversized` / `slice_starts_mid_sentence`
integrity warnings. Removed from it: `_looks_like_toc_entry`, a per-row lookahead test for the
dotless contents shape. It is correct row by row, but **a lookahead cannot see a contents block's
tail** — the last rows have no following entry to supply the signature. On NU it stripped 29 of 32
contents rows and left the final two, which is worse than leaving all 32: the density that
identifies the block was gone, so `_drop_contents_block` no longer fired and the survivors consumed
item numbers exactly as before. Measured three ways over the corpus — #1009 alone 115, this change
alone 116, both together **113** (all three NU years lost), with **no filing** where the per-row test
helped. The general rule, now recorded in the code: *a contents block must be removed whole, or not
at all.*
`EXTRACTOR_VERSION` is deliberately **not** bumped — `filing_section_coverage` is keyed by it and
`period_availability` does not filter on it, so a bump would leave stale coverage rows merging into
the same period bucket. **Stored rows written before this change are stale and must be re-ingested**
(`execution/ingest_filing_sections.py --all-portfolio --sources edgar`); the section table replaces a
document's partition wholly, so a re-run is sufficient.

**Robustness contract** (what each failure does):

| Situation | Behavior |
|---|---|
| Only one source has the period | Both lanes record their own verdict; `period_availability` labels it `is_single_source` and names why the other is absent |
| Declared form ≠ filename/DB regime | Filing's own declaration wins; recorded as `regime_mismatch_resolved_to_declared`; sections kept |
| Payload year vs filename year off by >1, or symbol mismatch | **Sections withheld**, `PERIOD_MISMATCH` — a mis-yeared section would corrupt every alignment |
| Two sources disagree on `period_end` for one bucket | `reconcile_sources` flags BOTH coverage rows; never auto-resolved (no principled winner) |
| Payload shape changed | `SCHEMA_DRIFT` + dump under `.tmp/filing_sections/schema_drift/`; never guess-fixed |
| Network / 429 / 5xx | `FETCH_FAILED` for that document; run continues; next run resumes (idempotent) |
| 401/403, missing migration, dangling `doc_id` | `HardStopError` → CLI exit 1 |
| 40-F (MJDS) | `UNSUPPORTED_FORM` — disclosure is incorporated by reference, so there is nothing to partition |
| Coverage claims `ok` but no rows exist | Read layer trusts rows over claims and reports the source absent |
| Table missing | Readers raise unless `missing_ok=True` (the lens case) |

Remaining Phase-0 backfills: `fetch_fmp_10q_json` for the 10-K-regime portfolio names, and registering
`fmp_10q_json` rows in `documents` (the `fmp_doc_index` gap). Respect quota windows (bursts ≥6–7h
apart, clear of 03:00–05:00 PT) and SEC rate etiquette.

### Phase 1 — cross-period section alignment (deterministic core, LLM fallback)
- Canonical taxonomy table per form regime; deterministic matcher (item id / normalized stem / title similarity / content shingle overlap); LLM only for renames-merges-splits, auto-resolved (DERIVE-DON'T-ASK), decisions logged with receipts.

### Phase 2 — change detection + noise/signal split (LLM layer)
- Per aligned section pair: deterministic paragraph diff → LLM classifies each hunk: `{boilerplate_update, legal_recitation, noise} vs {new_risk, softened/hardened language, guidance-adjacent shift, metric definition change, KPI added/discontinued, segment realignment}` with verbatim quote receipts (action-UX bar) and tone mapped to the existing `thesis_status_tone` vocabulary.
- Metric dimension: companyfacts tag lifecycle (first-seen/last-seen per form) + FMP stem lifecycle — deterministic detectors emitting candidate events; LLM interprets materiality against the ticker's thesis/tier-1 break rules.
- Reuse, don't reinvent: run/extend `extract_risk_factors` (extend to 20-F Item 3.D), wake `filing_diff_narrative` lens as the first consumer.

### Phase 3 — surface
- Workspace section + feed chips for confirmed signal events (consequence-first, receipts, batched), linkage into thesis machinery per the ticker-beliefs ruling (break rules + KPI tracking, not stances).

## 4. Open decisions (owner)

1. Roster scope for Phase 0 backfill: portfolio-11 only, or portfolio + top-ten discovery queue?
2. History depth: 5 fiscal years (proposal) vs full available?
3. FMP tier: FPI annual refresh is 403 at current tier — is the pending FMP Starter purchase expected to restore it, or do we accept EDGAR-only for FPI annuals (fine for language; loses R-file partition freshness for NVO/WIX/NU)?
4. New LLM purposes (`section_align_fallback`, `filing_diff_classify`) — registry + eval rungs per the 4-registries-in-lockstep recipe; model class proposal: Haiku for align, Sonnet for diff-classify, weekly eval rung.
