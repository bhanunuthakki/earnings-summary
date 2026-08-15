# Fed-funds source decision — 2026-08-14

## Decision

Use the Federal Reserve Bank of New York Markets Data API's Effective Federal Funds Rate (EFFR) as the first provider behind the existing macro acquisition boundary. Keep FRED `DFF` unimplemented as a transport fallback unless its API-key and terms gate is separately approved. Do not build a production dependency on the Federal Reserve Board's retiring Data Download Program.

This decision authorizes repository implementation and a bounded isolated canary only. It does not authorize a provider purchase, managed-runtime deployment, production schedule change, retry expansion, or live activation.

## Ranked evidence

| Rank | Source | Rights and attribution | Cadence and date semantics | Units | Access and rate limits | Provenance and fallback decision |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | New York Fed Markets Data API, `EFFR` | The New York Fed permits automated access, storage, use, copying, and derivatives for personal or business purposes subject to its Terms of Use. Reference-rate presentation or distribution must carry the prescribed source, terms, and non-endorsement notice. | Published about 9:00 a.m. ET each New York Fed business day for the preceding business day's transactions. Same-day revisions may publish around 2:30 p.m. ET. `effectiveDate` is the transaction/activity date, not retrieval time. | Percentage points, rounded to one basis point. No currency applies. | Public HTTPS JSON/XML/CSV/XLSX; no key or authentication is documented. No numeric request quota is published. This application budgets one request per run with no blind retry. | Direct administrator and producer. Persist `new_york_fed` and retain the exact effective date. On timeout/unavailable response, preserve cached-degraded/unavailable behavior; on schema/unit/date conflict, fail closed rather than use a proxy. |
| 2 | FRED API, `DFF` | The underlying series is public-domain Board data with citation requested. The FRED API adds its own terms, registered key, non-endorsement notice, and responsibility for underlying rights. | Daily, 7-day presentation. Observation dates can include calendar-day/forward-filled weekend values, unlike the New York Fed business-date series. | Percent, not seasonally adjusted. | Free registered API key; `429` is documented, but the limit may change. | Acceptable transport fallback only after separate key/terms approval. It is not independent economic evidence because it republishes Federal Reserve data. |
| 3 | Board H.15 DDP, `RIFSPFF_N.D` | Board information is public domain unless otherwise indicated; citation is requested. | Daily 7-day H.15 series with calendar-day observations. | Percent per year, multiplier 1. | Public/no key, CSV and SDMX XML. No numeric request quota is documented. | Emergency/manual evidence only. The Board announced removal of the build-your-package feature and eventual DDP retirement, so it is not a sound new production dependency. |

## Provider contract

- Source: `new_york_fed`.
- Endpoint: `https://markets.newyorkfed.org/api/rates/unsecured/effr/search.json`.
- Query budget: one date-bounded request per run, two years of observations, no automatic retry.
- Accepted semantic identity: only records whose `type` is exactly `EFFR`.
- Effective date: `effectiveDate`, a New York Fed business-date transaction/activity date.
- Observed time: timezone-aware UTC acquisition time supplied by the caller.
- Value and units: `percentRate`, percentage points, scale `1.0`; currency is not applicable.
- Freshness: the existing 45-day guard remains authoritative. Endpoint reachability or registry configuration never proves freshness.
- Duplicate dates: identical values coalesce; conflicting values fail closed.
- Malformed, future-dated, non-positive, non-finite, oversized, timed-out, or unavailable responses do not write observations and preserve explicit degraded/non-zero status.
- FMP candidates remain declarative and disabled behind the shared FMP recovery boundary. The one-month Treasury field is not an economic substitute for fed funds.

## External-practice record

| Area | Code seam | Decision | Why drift-sensitive | Owner | Evidence status |
| --- | --- | --- | --- | --- | --- |
| Provider | `src/macro_series.py` and `execution/fetch_macro_series.py` | New York Fed EFFR first | API schema, publication, terms, and availability can change | Tool selector / BHA-52 | Current primary-source review completed 2026-08-14 |
| Contract | Typed New York Fed response model | Validate the live `percentRate` field and fail closed if it changes | The current OpenAPI schema describes `percent`, while the live JSON returns `percentRate` | BHA-52 owner | Fixture-backed; vendor clarification remains open |
| Operations | Macro refresh call budget | One request per run, no blind retry | No numeric provider quota is published | BHA-52 owner | Conservative local policy; production activation not authorized |
| Rights | Any UI/export that presents reference-rate data | Carry the prescribed reference-rate notice and non-endorsement terms | New York Fed terms impose presentation/distribution conditions | Owner/legal | Required before production redistribution |

## Primary sources

- Federal Reserve Bank of New York, [Markets Data APIs](https://markets.newyorkfed.org/static/docs/markets-api.html), API documentation accessed 2026-08-14.
- Federal Reserve Bank of New York, [Effective Federal Funds Rate](https://www.newyorkfed.org/markets/reference-rates/effr), accessed 2026-08-14.
- Federal Reserve Bank of New York, [Reference-rate methodology, publication, revisions, and contingencies](https://www.newyorkfed.org/markets/reference-rates/additional-information-about-reference-rates), updated 2026-04-06 and accessed 2026-08-14.
- Federal Reserve Bank of New York, [Terms of Use](https://www.newyorkfed.org/privacy/termsofuse), updated 2023-06-09 and accessed 2026-08-14.
- Federal Reserve Bank of St. Louis, [FRED `DFF`](https://fred.stlouisfed.org/series/DFF), [observations API](https://fred.stlouisfed.org/docs/api/fred/series_observations.html), and [API terms](https://fred.stlouisfed.org/docs/api/terms_of_use.html), accessed 2026-08-14.
- Board of Governors, [H.15 Data Download Program](https://www.federalreserve.gov/datadownload/Download.aspx?rel=H15) and [DDP transition notice](https://www.federalreserve.gov/data/data-download-fred-information.htm), accessed 2026-08-14.

## Remaining activation gate

Before managed-runtime activation or redistribution, the owner must approve: (1) the required New York Fed attribution/non-endorsement notice on every applicable output surface, and (2) the one-request-per-run/no-blind-retry call budget. A provider clarification should also be sought for the `percent` versus live `percentRate` schema mismatch. These gates do not block the dormant implementation or isolated canary.
