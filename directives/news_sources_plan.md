# News sources and deferred-source disposition

**Reconciled:** 2026-09-19 (BHA-61). This document describes the actual dispatcher,
not a separate policy implementation. The former `news.news_ladder` module had no
production consumers, used different source names, and could collapse distinct
SEC query-document URLs; it and its self-only tests were retired after a complete
source census. The canonical authorities remain `execution/fetch_news.py` and
`src/news/store.py`.

## Actual collection and consumers

| Leg | Actual producer | Current behavior |
|---|---|---|
| General journalism | `execution/fetch_yf_news.py` | Implemented since July; additive by default. Maps modern nested and legacy Yahoo payloads into `NewsRow`. |
| FMP stock news | `execution/fetch_fmp_news.py` | Attempted under `--source auto` or `fmp`; provider refusals remain visible. |
| Web search | `execution/fetch_news_websearch.py` | Explicit source mode or deliberately enabled fallback. CLI defaults `--websearch-scope none`, so paid discovery is not the standing fallback. |
| Material disclosures / stakes | `execution/fetch_edgar_news.py` | Additive 8-K/13D/13G, with SEC pacing and source filing URLs. |
| Analyst actions | `execution/fetch_yf_grades.py` | Additive; per-ticker upstream gaps do not prove no rating activity. |
| Competitor IPO watch | `competitive.sec_watch` | Existing bounded S-1 watch, independently opt-out. |
| 13F / congressional disclosures | Owner-invoked ownership-disclosure workflow | On demand; no recurring product feed, writer, or notification activation. Historical stored 13F rows do not prove an active feed. |

All current ingestion persists through `news.store.upsert_news_rows` into the
canonical `news` table. Exact `(ticker,url)` identity is idempotent; meaningful
query parameters remain part of source identity. Additive cross-source stories
are deduplicated by ticker, normalized headline and publication date against both
the current batch and retained rows. Consumers include `triggers.material_news`,
news diet scoring and the dashboard's news/ratings/disclosures projections.
The store validates UTC publication format and plausible dates. HTTP(S) source
URLs must have a hostname and no embedded credentials or whitespace. An article
cannot be made current by inventing a timestamp; undated Yahoo strings are refused.

## Current measured evidence

A read-only census of the canonical Windows database on 2026-09-19 enumerated the
entire active portfolio/watchlist/evaluation population and per-source stored
presence for 7-day and 30-day windows. It confirms substantial current Yahoo
journalism and additive grades/EDGAR rows; Yahoo general news is **implemented**,
not a deferred leg. The private, source-bound receipt is retained with the
completion-fixes audit (`news-source-evidence.json`), including the population,
window counts, latest publication times and recorded news-structuring costs.
The public source tree does not carry the owner's ticker population.

These measurements establish stored presence only. Zero rows may mean no story,
failed acquisition, or an unserved ticker. Publication recency is not a successful
collection receipt or archive-completeness proof. Historical dollar-per-row figures
in the July implementation are historical rationale, not current measured prices
or a guaranteed forecast of marginal operating cost. No paid provider call or new
source canary was run for this reconciliation.

## Candidate decision and primary-source evidence

| Candidate | Disposition | Evidence and limit |
|---|---|---|
| Yahoo general news | Retain existing implementation; remove obsolete deferred checkbox | Current live stored evidence and existing deterministic adapter/tests establish actual use. No duplicate source is needed. |
| Finnhub company news / ratings | Do not add or schedule; deferred pending a demonstrated unmet gap | No measured incremental coverage advantage over current sources is established. Official company-news contract limits coverage to North American companies and requires an API key. Current plan rights, quotas and price were not verifiable from the fetched pricing page; prior fixed 60/min and redundancy assertions are withdrawn as unproven for this selection. |
| Generic RSS/scrapers | Do not add | No selected publisher contract, supported schema, relevance benchmark or incremental coverage evidence. This is not a universal assertion that RSS lacks provenance. |
| AlphaVantage sentiment | Do not add | Not required by the current news task; no supported incremental-value case. Prior universal quota assertion is withdrawn. |

Sources checked 2026-09-19:

- [yfinance project and data-rights boundary](https://github.com/ranaroussi/yfinance):
  unofficial and unaffiliated with Yahoo; software licensing does not grant rights
  to redistributed provider content. The project directs users to Yahoo terms and
  characterizes the API as personal-use. This localhost research tool does not
  acquire redistribution rights through its adapter.
- [yfinance get_news contract](https://ranaroussi.github.io/yfinance/reference/api/yfinance.Ticker.get_news.html):
  ticker-scoped list response, default count 10, selectable news/all/press-releases
  tab. No guaranteed completeness, rate allowance or SLA is specified there.
- [Finnhub maintained OpenAPI](https://github.com/Finnhub-Stock-API/finnhub-go/blob/master/api/openapi.yaml):
  `/company-news` takes symbol/from/to, returns article identity, publication time,
  headline, source and URL, and uses API-key authentication. Its stated geographic
  scope does not itself prove ticker coverage for this book.
- [Finnhub pricing](https://finnhub.io/pricing) and [rate-limit documentation](https://finnhub.io/docs/api/rate-limit):
  fetched pages exposed no readable plan details in this check. No account was
  created or key provisioned; cost, retention/redistribution rights and exact plan
  allowance remain unknown rather than guessed.

Reopen Finnhub selection only for a named material missed event/ticker or sustained
current-source outage, with a bounded side-by-side canary demonstrating net-new
useful stories, measured failures/latency and complete source identity. Before any
implementation/activation, bind current plan rights, auth, quotas, timestamp/issuer
semantics, retention and marginal operating cost. A positive canary is evidence
for a separate scheduling decision, never automatic activation.

## Failure and retention behavior

Yahoo transport failure or a non-array payload is unavailable, not a successful
empty response. The dispatcher retains other sources' valid rows, emits a partial
collection disposition and returns nonzero. Any persistence failure also returns
nonzero, including when another batch succeeded. The existing dead-man freshness
alert is only a last-stored-row check; it cannot establish per-ticker collection
coverage. Deterministic tests cover malformed/unsafe URLs, timestamp rejection,
unavailable collection, genuine empty responses, duplicates and cross-source
stories. Operational receipts must retain the degraded state instead of inferring
success from old news rows.

Fetch windows bound retrieval; they are not a retention policy. Existing retained
news is not deleted by this repair. New-provider selection must establish retention
before activation; a cleanup run is a separate owner-authorized operation.
