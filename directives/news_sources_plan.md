# Non-FMP news sources — scoping + fallback ladder

**Status:** pilot landed (EDGAR 8-K / 13D / 13G + yfinance grades, additive). This doc is the
source evaluation behind that pilot and the design sketch for the deferred legs (13F diffs,
yfinance general news, Finnhub).

**Ask (verbatim):** "Can you also have a fallback news source that is not FMP, where ratings
changes by sell-side analysts, key position changes at top hedge funds, product releases,
acquisitions, and other key news can be pulled in."

**Context.** The `news` table (alembic `0065_news`) is populated by the daily Stage-0 fetch
(`execution/fetch_news.py`, invoked by `run_morning_pipeline.py`). Today's ladder is FMP
stock-news (FMP_TIER=free, quota-fragile) → WebSearch+Opus per ticker on FMP refusal. The
dedicated FMP `/stable/grades` endpoint could not be verified on the free tier (probe hit the
daily quota; see `src/dashboard/inbox_rank.py` docstring). Everything below is evaluated for
the active tracked book (`tracked_companies`, list_type ∈ portfolio/watchlist/evaluation,
~62 tickers — mostly US filers, plus FPIs: NU, MELI*, ASML, NVO, TSM, BHP, RIO, VALE, HDB…
*MELI files domestic forms 10-K/8-K despite being LatAm).

---

## 1. Source evaluation by category

Ranking criteria, in order: **cost** (free first, no paid signups), **ToS/licensing**,
**latency**, **coverage of our tracked tickers**, **maintenance burden**.

### (a) Sell-side rating changes

| Source | Cost | ToS / licensing | Latency | Coverage | Maintenance | Verdict |
|---|---|---|---|---|---|---|
| **yfinance `Ticker.upgrades_downgrades`** | Free, **no key** | Unofficial Yahoo endpoints; gray zone, fine for personal research, no redistribution | Minutes–hours after the event | **Verified live 2026-06-11**: AAPL latest 2026-06-09, NU 2026-06-03, NOW 2026-05-06 — but **per-ticker gaps** (META frozen at 2024-09-30; upstream Yahoo data gap) | Low–moderate (pin yfinance, degrade to `[]` on breakage) | **PILOT — implemented** as `source_feed='yf_grades'` |
| Finnhub `/stock/upgrade-downgrade` | Free **key** (signup), 60 req/min | Attribution required; no redistribution; ratings endpoint is marked premium-ish in docs — needs post-signup verification | Near-real-time | Unverified | Low | **Backup rung** if yfinance breaks; needs a (free) key signup first |
| Benzinga APIs | **Paid** (Benzinga Pro; powers FMP's grades and used to power Yahoo's) | Commercial license | Real-time, canonical firehose | Full | Low | **Note only** — the upgrade path if ratings ever become load-bearing |
| FMP stock-news headline regex (existing) | Free tier | Already in use | Hours | Partial (only stories FMP carries) | Zero (exists) | Remains the in-feed refinement (`_RATING_HEADLINE_RX`) |

### (b) Hedge-fund position changes

| Source | Cost | ToS / licensing | Latency | Coverage | Maintenance | Verdict |
|---|---|---|---|---|---|---|
| **EDGAR 13D / 13G (incl. amendments)** | Free | **Public domain** (SEC fair-access rules: descriptive UA + contact, ≤10 req/s) | 13D: ≤5 business days after crossing 5% (activist intent); 13G: periodic/quarterly (passive) | **Verified live 2026-06-11**: filings appear under the **subject company's** submissions JSON. Note EDGAR's Dec-2024 renaming: new filings are `SCHEDULE 13D` / `SCHEDULE 13G(/A)`, legacy are `SC 13D` / `SC 13G(/A)` — match both | Low (one submissions JSON per ticker, already cached) | **PILOT — implemented** as `edgar_13d` / `edgar_13g` |
| EDGAR 13F-HR quarterly diffs (curated top-fund list) | Free | Public domain | **~45-day lag** after quarter end (quarterly cadence = low urgency) | Full for US-listed equities | **Not small** — see §4 design sketch | **Scoped, deferred** to its own PR |
| WhaleWisdom / HedgeFollow / Dataroma scrapes | Free-ish | Scraping against ToS | Same 13F lag | Full | High, fragile | Rejected — EDGAR is the same data, canonical and legal |

### (c) Product releases / acquisitions / material events

| Source | Cost | ToS / licensing | Latency | Coverage | Maintenance | Verdict |
|---|---|---|---|---|---|---|
| **EDGAR 8-K with item codes** | Free | Public domain (fair-access) | The fastest *legal* channel — most items due within 4 business days, usually filed same day; earnings 2.02 lands minutes after the press release | **Verified live 2026-06-11**: `filings.recent.items` carries codes (e.g. `"2.02,9.01"`). Covers acquisitions (1.01/2.01), exec changes (5.02), restatements (4.02), cyber incidents (1.05), Reg-FD/press (7.01/8.01). **FPI caveat:** 20-F/6-K filers don't file 8-K, and 6-K carries no item codes → FPIs get 13D/G coverage only for now | Low | **PILOT — implemented** as `edgar_8k` |
| Company press RSS (per-issuer IR feeds) | Free | Generally fine (published feeds) | Real-time | ~62 bespoke URLs to discover and babysit; many issuers have none | **High** — breakage-prone | Deferred; the weekly `ir_pipeline` already covers IR documents |
| PR-wire public RSS (GlobeNewswire/BusinessWire/PRN) | Free | Wire ToS allow personal RSS consumption | Real-time | Firehose; per-ticker filtering is weak on the free feeds | Moderate | Noted; FMP already syndicates the wires when its quota holds |

### (d) General news

| Source | Cost | ToS / licensing | Latency | Coverage | Maintenance | Verdict |
|---|---|---|---|---|---|---|
| FMP stock-news (existing primary) | Free tier, quota-fragile | OK | Hours | Good while quota holds | Zero | Stays primary |
| **yfinance `Ticker.news`** | Free, no key | Same unofficial-API gray zone as (a) | Minutes–hours | ~10 recent stories/ticker, title+link+publisher+timestamp — maps cleanly onto `NewsRow` | Low | **Recommended next rung** (follow-up PR): slots between FMP and WebSearch+Opus, would absorb most of the Opus fallback cost |
| AlphaVantage `NEWS_SENTIMENT` | Free key, **25 req/day** | Personal use | Hours | 25 req/day cannot cover 62 tickers daily | Low | Rejected as a daily rung; usable only for spot-checks |
| Google News RSS (`news.google.com/rss/search?q=<ticker>`) | Free, no key | Gray; Google tolerates personal RSS use, no SLAs | Minutes | Broad but noisy (needs publisher allowlist) | Moderate | Backup of last resort before Opus |
| WebSearch+Opus (existing) | LLM tokens | n/a | n/a | n/a | Zero (exists) | Stays the last rung |

---

## 2. Recommended default stack + fallback ladder

Mirror of the `expected_earnings` FMP→yfinance ladder pattern, with one structural difference:
EDGAR is not a *fallback* — it is canonical primary-source disclosure that FMP merely
paraphrases, so it runs **additively every day**, not only on FMP refusal.

```
Category            Always-on (additive)            Ladder on top
-----------------   -----------------------------   ------------------------------------------
Rating changes      yf_grades (free, keyless)       FMP headline regex (existing)
                                                    → [Finnhub free key, if yfinance breaks]
Hedge-fund moves    edgar_13d / edgar_13g           13F-HR quarterly diffs (deferred, §4)
Material events     edgar_8k (item-coded)           FMP stock-news (existing)
General news        —                               FMP → [yfinance news, follow-up] → WebSearch+Opus
```

Dedup policy: the table's `UNIQUE(ticker, url)` dedupes re-runs within a feed; **cross-feed**
the additive sources are filtered by `(ticker, normalized headline, published date)` against
both the same batch and rows already in the table (`drop_duplicate_stories` in
`src/news/store.py`) — so an FMP story and the matching EDGAR filing never double-post, and
the additive feeds *never replace* FMP/WebSearch rows.

---

## 3. Pilot implementation (landed with this doc)

### EDGAR leg — `execution/fetch_edgar_news.py`

* **Fair access:** descriptive `User-Agent` with contact email (`EDGAR_USER_AGENT` env
  override), global ≥0.15s spacing between SEC requests (≤10 req/s policy), on-disk cache
  under `data/edgar/`:
  * `company_tickers.json` (ticker→CIK registry, TTL 7 days)
  * `submissions/CIK##########.json` (per-CIK, TTL 6h — the daily run refetches, same-day
    re-runs hit cache)
* **One request per ticker per day** (the submissions JSON carries form type, 8-K item
  codes, filing date and acceptance time — no per-filing fetches needed).
* Maps, for filings within the `--days` window (default 3):
  * `8-K`/`8-K/A` → `source_feed='edgar_8k'`, headline `"8-K 2.01, 9.01: completed
    acquisition or disposition — <Company>"` (item-code descriptions from the Reg-S-K map
    inside the fetcher; first non-boilerplate item names the filing)
  * `SC 13D(/A)` + `SCHEDULE 13D(/A)` → `edgar_13d`, headline `"SC 13D: activist stake
    (>5%) disclosed — <Company>"` (filer name is not in the subject's submissions arrays;
    fetching each filing's primary doc to name the filer is a possible enhancement at +1
    request per filing)
  * `SC 13G(/A)` + `SCHEDULE 13G(/A)` → `edgar_13g` (passive ≥5% stakes)
* `published_at` = `acceptanceDateTime` (already UTC) → canonical `'YYYY-MM-DD HH:MM:SS'`;
  falls back to `filingDate 00:00:00` (date real, midnight = conservative floor); a row with
  neither parseable is dropped, never fabricated.
* URL = the primary document under `https://www.sec.gov/Archives/edgar/data/<cik>/<accession>/`
  (unique per filing → the `(ticker, url)` key keeps re-runs idempotent).

### Ratings leg — `execution/fetch_yf_grades.py`

* `yf.Ticker(t).upgrades_downgrades`, keyless; **manually verified 2026-06-11** (shape:
  index `GradeDate` UTC, columns Firm/ToGrade/FromGrade/Action/priceTargetAction/
  currentPriceTarget/priorPriceTarget; actions `up|down|init|main|reit`).
* Maps rows within the window to `source_feed='yf_grades'` with headlines like
  `"Morgan Stanley upgrades META to Overweight from Equal-Weight; PT $620 → $700"` —
  deliberately shaped so the existing `_RATING_HEADLINE_RX` also matches them.
* Synthetic-but-clickable stable URL (`https://finance.yahoo.com/quote/<T>/analysis#grade-…`)
  provides the per-event uniqueness the `(ticker, url)` key needs.
* Degrades to `[]` on any yfinance import/transport/shape error. Known gap: some tickers
  (META) are frozen upstream — the FMP headline-regex leg still catches those.

### Wiring — `execution/fetch_news.py` (dispatcher)

* Additive collection runs **after** the FMP/WebSearch policy rows, for every `--source`
  mode; `--skip-edgar` / `--skip-grades` opt out. Failures log (`news_edgar_failed` /
  `news_grades_failed`) and degrade — they can never block the primary feeds or the
  morning pipeline's trigger stage.
* Grades fetch is threaded (8 workers); EDGAR stays sequential under the SEC throttle.
  Stage-0 timeout in `run_morning_pipeline.py` bumped 600s → 900s for headroom.

### Inbox categorization — `src/dashboard/inbox_rank.py`

* `yf_grades` joins `fmp_grades` as a grades feed → **Rating changes**.
* `edgar_8k`: disclosure-only filings (items ⊆ {7.01, 8.01, 9.01}) → **Press releases**;
  anything with a material item (2.01, 5.02, 4.02, …) → **News**. No new category.
* `edgar_13d` / `edgar_13g` → **News** (default), deliberately: a 5% activist stake is
  news, not PR.

---

## 4. 13F-HR quarterly diffs — design sketch (deferred)

Scope kept out of the pilot per the "implement only if it stays small" rule — it is not small:

1. **Curated fund list** (CIK-keyed, config file, ~15–25 funds): Berkshire 0001067983,
   Pershing Square 0001336528, Third Point, Tiger Global, Coatue, Altimeter, Lone Pine,
   Viking, Appaloosa, Baupost, Greenlight, Duquesne, Bridgewater, …
2. Per fund: submissions JSON → two most recent `13F-HR` accessions → fetch each filing's
   **information table XML** (one extra request per fund per quarter; small).
3. Aggregate by issuer; **map issuer → tracked ticker**. This is the hard part: info tables
   key on **CUSIP + issuer name**, not ticker. Options: OpenFIGI API (free key, another
   dependency) or normalized issuer-name matching against `tracked_companies` (fuzzy,
   needs a manual alias table for the ~62 names — feasible but fiddly).
4. Diff latest vs prior quarter per (fund, tracked ticker): new position / full exit /
   share-count change ≥25% → one `NewsRow` (`source_feed='edgar_13f'`,
   `published_at` = filing acceptance, headline `"13F: Berkshire Hathaway exits SNOW
   (-100%, was $1.1B)"`).
5. State: no extra table needed — recompute the diff from the two cached filings each run;
   `(ticker, url#fund-quarter)` keys idempotency. Amendments (`13F-HR/A`) must replace, not
   stack (RESTATEMENT vs NEW HOLDINGS amendment types).

Why deferred: CUSIP/name mapping + amendment semantics are each a day of careful work and
test fixtures; the cadence is quarterly with a 45-day lag, so there is no urgency premium.
Effort: one focused PR.

---

## 5. Risks & watch items

* **yfinance is an unofficial API** — Yahoo can break it any week. Pinned via
  `requirements.txt`; every failure path degrades to `[]`; the plan's backup is a Finnhub
  free key (signup decision deferred to the user).
* **Per-ticker grade gaps** (META frozen 2024-09): treat `yf_grades` as additive coverage,
  never as proof of "no rating activity".
* **EDGAR `filings.recent` window** holds ~1,000 filings (META: back to 2024-04) — orders of
  magnitude more than any 3-day window needs; older pages exist under `filings.files` if a
  backfill ever wants them.
* **Form-name drift**: EDGAR renamed `SC 13D/G` → `SCHEDULE 13D/G` in Dec 2024; the fetcher
  normalizes both spellings. Watch for the same rename pattern on other forms.
* **FPIs file 6-K, not 8-K** (NU, ASML, NVO, TSM, BHP, RIO, VALE, HDB): no item codes, so
  deterministic headlines/categorization aren't possible — 6-K ingestion would need an LLM
  classification pass (deferred; those names keep FMP/WebSearch + 13D/G coverage).
* **Trigger flooding**: material_news reads the latest 15 stories/ticker/24h. Realistic
  additive volume is ≤3 filings+grades per ticker per day; 13G amendment season (Feb) is the
  worst case and still small. No cap implemented; revisit if a ticker ever floods.
* **Contact email in the default UA** lives in `execution/fetch_edgar_news.py`
  (`EDGAR_USER_AGENT` env var overrides). SEC policy requires a real contact; the repo is
  private, so the default carries one.
