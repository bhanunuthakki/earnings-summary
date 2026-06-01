# Plan: migrate the repo's FMP fetchers off deprecated `/api/v3` onto `/stable`

> Status: PLAN FOR REVIEW — not implemented. This document is the only artifact in this branch.
> Author: planning session, 2026-05-31.
> Sibling to `scratch/plans/news_table_plan.md`. Trigger: FMP is moving the account to a free/limited tier,
> and FMP **already deprecated all `/api/v3/*` endpoints on 2025-08-31** (they 403 for non-legacy accounts —
> see [[fmp-v3-deprecated-2025-08-31]]). Any fetcher still calling v3 breaks the moment the account downgrades.

---

## 1. Summary

Investigation shows the v3→stable migration is **already largely done and in-flight**, not greenfield:

- **The live production statement path is already `/stable`.** `execution/save_fmp_data.py` is a stable-first
  try-ladder cacher (stable -> v3 -> v4, first success wins) driven daily by the `refresh_cache.py` cacher
  (`cron/run_refresh_cache.bat`) and by `onboard_ticker.py`. It already handles the FMP failure modes that
  bit us in the news plan: the **HTTP-200-with-`Error Message` body** ([save_fmp_data.py:203-204](execution/save_fmp_data.py)),
  the **200-empty-list** IPO case ([:288-296](execution/save_fmp_data.py)), and **401/402/403** tier
  restriction ([:298-304](execution/save_fmp_data.py)).
- **The persisted data is already stable-shaped.** The Pydantic models use stable field names (`fiscalYear`,
  `ebit`, `accountsReceivables`, `netDividendsPaid`, `commonStockRepurchased` — [src/models/fmp_payloads.py:21-131](src/models/fmp_payloads.py)),
  not the v3 names (`calendarYear`, `dividendsPaid`). So **this migration does NOT reshape data** and the
  downstream blast radius is near-zero.
- **A code comment confirms the cutover already started**: "After v3 started returning 403 in May 2026,
  refreshes moved to /stable" ([src/compute/fmp_derived_kpis.py:106](src/compute/fmp_derived_kpis.py)).

So this is a **mop-up + hardening** plan, not a rewrite. Three real loose ends remain:

1. **`execution/fetch_fmp_statements.py` is a v3-ONLY orphan** ([:60](execution/fetch_fmp_statements.py)) —
   nothing schedules or imports it (only its own test). It is the clearest v3 liability, and it duplicates
   what the stable path already does — *except* it has a **Pydantic pre-write validation gate the live path
   lacks**. Retire it, and port that gate into `save_fmp_data.py`.
2. **`execution/fetch_macro_series.py` still pins v3/v4** for some series ([:52-53](execution/fetch_macro_series.py)).
3. **Dead v3/v4 rungs + stale docs**: the ladders in `save_fmp_data.py`/`fetch_etf_data.py` keep v3/v4
   fallback rungs that will *always* 403 on free FMP (wasted quota), and `fmp_payloads.py` docstrings still
   say `/api/v3/...`.

Plus the big operational consequence: **free FMP is 250 requests/day + 500 MB/30-day bandwidth**, and the
per-ticker catalog is ~67 endpoints — so a full refresh of even a handful of tickers blows the daily budget.
The `refresh_cache.py` cacher is already tier-aware and budgets calls, so the mechanism exists; the plan
makes sure it is set to the free budget and documents the stretched cadence.

---

## 2. Inventory — every FMP base-URL reference

From a full-repo grep (`financialmodelingprep|api/v3|api/v4|/stable`). **L** = live, **O** = orphan/legacy.

| File:line | Base used | Status | Action |
| :-------- | :-------- | :----- | :----- |
| [save_fmp_data.py:249,259](execution/save_fmp_data.py) | `stable` (rung 1) | L | keep (primary) |
| [save_fmp_data.py:252,262](execution/save_fmp_data.py) | `api/v3` (rung 2) | L | **prune (tier-aware)** — dead on free |
| [save_fmp_data.py:255,265](execution/save_fmp_data.py) | `api/v4` (rung 3) | L | **prune (tier-aware)** — dead on free |
| [fetch_fmp_statements.py:60](execution/fetch_fmp_statements.py) | `api/v3` ONLY | O | **retire** (port Pydantic gate first) |
| [fetch_etf_data.py:222](execution/fetch_etf_data.py) | `stable` then `api/v3` | L | prune the v3 rung (tier-aware) |
| [fetch_macro_series.py:52-53](execution/fetch_macro_series.py) | `stable`+`v3`+`v4` | L | **migrate v3/v4 series to stable** |
| [fetch_fmp_10q_json.py:45](execution/fetch_fmp_10q_json.py) | `stable` | L | none (already stable) |
| [fetch_fmp_earnings_calendar.py:50](execution/fetch_fmp_earnings_calendar.py) | `stable` | L | none |
| [fetch_fmp_historical_data.py:37](execution/fetch_fmp_historical_data.py) | `stable` | L | none |
| [schedule_pre_earnings_refresh.py:83](execution/schedule_pre_earnings_refresh.py) | `stable` | L | none |
| [fmp_payloads.py:22,53,98,134](src/models/fmp_payloads.py) | `/api/v3` (docstrings) | — | fix docstrings -> `/stable` |
| [fmp_derived_kpis.py:106](src/compute/fmp_derived_kpis.py) | comment | — | reconcile suffixed/unsuffixed (see §6) |

Topology (confirmed): **`save_fmp_data.py`** <- `refresh_cache.py` (the cacher; `cron/run_refresh_cache.bat`
daily 03:00) and `onboard_ticker.py`/`refresh_dispatch.py`. **`fetch_fmp_statements.py`** <- nothing (only
`tests/test_fetch_fmp_statements_validation.py`). Already-migrated stable-only fetchers need no work.

Downstream consumers of the statement JSON (blast radius if shapes changed — they will NOT change):
`src/compute/{income_statement,balance_sheet,cashflow}.py` (Pydantic-validated extraction into
`financial_facts`), `src/dcf/seeder.py` (direct JSON reads of `*_quarterly.json`),
`src/compute/valuation_basis.py`, `src/compute/fmp_derived_kpis.py` (via `financial_facts`).

---

## 3. The core decision: retire the v3 orphan, port its gate to the live path

Three ways to "migrate the statement fetchers":

- **(A) Retire `fetch_fmp_statements.py`; port its Pydantic gate into `save_fmp_data.py`.** It is orphaned
  dead code whose capability the stable cacher already covers — *except* validation. Porting the gate gives
  the live path the schema-drift protection it lacks, then the orphan is deleted. **Smallest change, removes
  the v3 liability, and a net hardening of the live path.**
- **(B) Rewrite `fetch_fmp_statements.py` to call `/stable`.** Keeps a second, redundant statement fetcher
  alive (two ladders, two code paths to maintain) for no caller. Rejected.
- **(C) Extract a shared `src/sources/fmp_client.py`** (stable-first ladder + `Error Message` guard + token
  bucket + optional Pydantic gate) and route every fetcher through it. This is the correct *long-term*
  design — there is no shared FMP client today; each fetcher rolls its own — but it touches 6+ fetchers and
  is far more than the free-tier deadline needs. **Recommend as a deferred Phase 2, not part of this plan.**

**Recommendation: (A) now, (C) later.** (A) directly answers "migrate the v3 statement fetchers" with the
least risk: the live stable path already produces the data, so retiring the orphan changes no output; porting
the gate strictly improves the live path.

---

## 4. Port the Pydantic validation gate into the live stable path

`fetch_fmp_statements.py` validates `data[0]` against an endpoint→model map before persisting, dumping the
raw body to `.tmp/fmp_validation_failures/` on drift (`_validate_response` / `_dump_validation_failure`,
[fetch_fmp_statements.py:167-204](execution/fetch_fmp_statements.py)). `save_fmp_data.py` currently writes the
body unvalidated ([:580-582](execution/save_fmp_data.py)). Port the gate:

- Add a stable-endpoint→model map in `save_fmp_data.py` (keyed by the catalog `path`, not the v3 path):
  ```python
  _STABLE_VALIDATORS: dict[str, type[BaseModel]] = {
      "income-statement": FmpIncomeStatementRecord,
      "balance-sheet-statement": FmpBalanceSheetRecord,
      "cashflow-statement": FmpCashFlowRecord,   # note: stable spelling is 'cashflow-statement' (:338)
  }
  ```
- In `run_ticker`, **before** `file_path.write_text(...)` ([:580-582](execution/save_fmp_data.py)): if the
  winning rung is a **`stable:` rung** (`kind.startswith("stable:")`) and the endpoint has a model, validate
  `body[0]`; on `ValidationError`, dump to `.tmp/fmp_validation_failures/`, record the endpoint status as
  `error` (`schema_drift`), and **skip the write** (don't cache a malformed envelope) — mirroring
  [fetch_fmp_statements.py:231-247](execution/fetch_fmp_statements.py).
- **Validate stable rungs only.** v3/v4 fallback rungs return v3-shaped bodies (`calendarYear`, etc.) that
  would fail the stable-shaped model; since those rungs are being pruned anyway (§5), skip validation for
  non-stable rungs with a logged note rather than halting on legacy data.
- The models already exist and already match stable ([fmp_payloads.py:21-131](src/models/fmp_payloads.py));
  this is wiring, not new schema. (Also fix those four docstrings from `/api/v3/...` to `/stable/...`.)

Net effect: the daily cacher now refuses to overwrite good statement JSON with a drifted stable response —
the protection that today only the dead orphan had.

---

## 5. Tier-aware ladder + the free-tier quota reality

**Prune the dead rungs (tier-aware).** On free FMP every `/api/v3` and `/api/v4` call 403s (global
deprecation, not just this account), so the ladder's rungs 2-3 ([save_fmp_data.py:252,255,262,265](execution/save_fmp_data.py))
are pure waste: 2 extra failed calls per stable-miss, each burning one of only 250 daily requests. Make the
ladder tier-aware:

- When the tier is free (env flag from `refresh_cache.py`, e.g. `FMP_TIER=free`, or a `--stable-only` arg on
  `save_fmp_data.py`), `_candidates` emits the **stable rung + stable `PATH_ALIASES` only**, dropping the
  v3/v4 rungs. The stable-to-stable aliases ([:209-240](execution/save_fmp_data.py)) stay — they are valuable
  alternate stable spellings, not v3.
- **Do not hard-delete the v3/v4 rungs unconditionally**: a few of the 67 catalog endpoints may not yet have
  a stable equivalent, and the account is *legacy until the downgrade*. Gate by tier so legacy keeps the
  fallback and free skips it. **Build step:** probe each of the 67 catalog endpoints on stable with the free
  key and record which 403 even on stable (those need a stable alias added to `PATH_ALIASES` or are
  genuinely unavailable on free — accept + document).
- Same one-line v3-rung prune for `fetch_etf_data.py` ([:222](execution/fetch_etf_data.py)) under the free
  tier.

**The quota reality (document + verify, mostly already handled).** Free FMP = **250 requests/day + 500 MB /
30-day** bandwidth. `per_ticker_jobs` is ~67 endpoints ([save_fmp_data.py:311-427](execution/save_fmp_data.py)),
so ~3-4 tickers exhaust a day's budget. The cacher already budgets per tier (`refresh_cache.py` sets
`--max-calls` and `FMP_RATE_LIMIT_PER_SEC`, the agent found basic=250/day) and works a **priority queue**, so
the mechanism exists — but on free the full-universe refresh stretches from "daily" to "many days," and
time-sensitive endpoints (DCF, ratings, consensus) will be stale between visits. **Actions:** (a) confirm/set
the free-tier budget in `refresh_cache.py` to 250/day; (b) confirm the cacher prioritizes active tickers
(portfolio/watchlist/evaluation) and the statement endpoints over the long tail; (c) document the stretched
cadence so stale data is expected, not alarming. (Endpoints FMP drops entirely on free degrade to the SEC
source-of-truth path [[sec-filings-source-of-truth]] — out of scope here, noted for the roadmap.)

---

## 6. Secondary clean-ups

- **`fetch_macro_series.py` v3/v4 series -> stable** ([:52-53,69-70](execution/fetch_macro_series.py)). It
  pins `historical-price-full` (v3) and others; map each to its stable equivalent
  (`historical-price-eod/full` etc., already aliased in [save_fmp_data.py:231-234](execution/save_fmp_data.py))
  and validate the shape. Smaller surface than statements; same retire-the-v3-pin pattern.
- **Reconcile suffixed vs unsuffixed statement files.** [fmp_derived_kpis.py:106](src/compute/fmp_derived_kpis.py)
  claims stable "writes the unsuffixed `{TICKER}_income_statement.json`", but `save_fmp_data.py` writes
  **suffixed** `_annual`/`_quarterly` ([:340-341](execution/save_fmp_data.py)) and no code writes unsuffixed.
  The defensive `fiscal_period_type IN ('Q1'..'Q4')` filter makes both safe, so this is a doc/consistency
  bug, not a data bug: **either** correct the comment to say stable still writes suffixed, **or** (if the
  unsuffixed scheme is actually intended) make `save_fmp_data.py` write unsuffixed and update the readers.
  Recommend: correct the comment (least churn); the suffixed scheme is what every reader expects.
- **Docstrings** in `fmp_payloads.py` ([:22,53,98,134](src/models/fmp_payloads.py)) -> `/stable/...`.

---

## 7. Test strategy

Reuse the existing ladder regression suite ([tests/test_save_fmp_data_empty_classification.py](tests/test_save_fmp_data_empty_classification.py),
which already asserts "stop at first stable probe, no v3/v4 fallback for empty-list").

1. **Stable-only ladder (free tier).** With the free flag set, `_candidates` emits only stable (+ stable
   aliases) — assert NO `api/v3`/`api/v4` URL is ever requested (mock the session, inspect called URLs).
   Legacy tier still emits the v3/v4 rungs.
2. **Ported validation gate.** A stable response with a drifted shape (e.g. missing `date`/`symbol`, or
   `calendarYear` instead of `fiscalYear` as the *only* period key) -> `ValidationError` -> dump written to
   `.tmp/fmp_validation_failures/`, status `error`, and **no statement file written**. A well-formed stable
   response -> validated and written.
3. **Validation scoped to stable rungs.** A v3 fallback response (legacy tier) is written WITHOUT being run
   through the stable-shaped validator (no false schema-drift halt on legacy data).
4. **Refusal modes still handled** (regression): 403, 200-`{"Error Message"}`, 200-`[]` each classified as
   forbidden / error / empty respectively (guard [save_fmp_data.py:203-204,288-304](execution/save_fmp_data.py)).
5. **Orphan retirement.** Delete `fetch_fmp_statements.py` + its test; grep-assert no remaining import or
   subprocess reference anywhere (a CI grep guard so it can't silently come back).
6. **macro_series stable migration.** Each migrated series fetches from a stable URL; mocked round-trip
   produces the same parsed series as before.
7. **No-data-shape-change guard.** Extraction over a real persisted `*_income_statement_annual.json` still
   yields the same `financial_facts` rows (the migration must not change on-disk shapes — this pins that).

---

## 8. Risks + open questions

- **R1 — Not every catalog endpoint may exist on stable.** A few of the 67 endpoints could still be v3-only.
  **Mitigation:** the build-step stable probe (§5) enumerates which 403 on stable; add a stable alias or
  document the loss. Do NOT prune v3/v4 rungs before that probe passes.
- **R2 — Free-tier quota makes full refresh impossible daily.** 250/day vs ~67/ticker. The cacher's
  budget+queue already handles partial progress, but cadence stretches and time-sensitive data goes stale.
  Confirm the free budget and active-ticker prioritization; document the new cadence. This is operational,
  not a code bug.
- **R3 — Bandwidth cap (500 MB/30-day).** 10-K JSON pulls (10/ticker) and 10y price histories are large;
  on free, bandwidth may bind before the call count does. Consider trimming history depth (e.g. `limit`,
  fewer 10-K years) on free tier. Verify against the real free account.
- **R4 — Validation on stable rung could reject a genuine stable shape FMP tweaks.** The gate halts the write
  on drift (by design — schema-drift defense). If FMP renames a stable field, statement refreshes stop until
  the model is updated. Accepted tradeoff (same as the existing news/statements gate); the dump makes the fix
  a one-liner.
- **R5 — Legacy-vs-free timing.** Until the downgrade actually happens, the account is legacy and v3 still
  works; the tier-aware gating must default correctly. Drive it off an explicit `FMP_TIER` (set by
  `refresh_cache.py` / env), not auto-detection, so the cutover is a deliberate flip.
- **R6 — Deleting the orphan loses its ThreadPool parallelism.** `fetch_fmp_statements.py` fanned statements
  across 16 workers; `save_fmp_data.py` is single-threaded behind a token bucket. On free tier the 250/day
  cap dominates anyway, so parallelism is moot; on legacy, the cacher's rate is deliberate. No action.

---

## 9. Build sequence (ordered, each PR independently shippable)

1. **PR 1 — Port the validation gate + fix docstrings.** Add `_STABLE_VALIDATORS` + stable-rung validation to
   `save_fmp_data.py` (§4); fix `fmp_payloads.py` docstrings to `/stable`. Tests §7.2-§7.4. Ships pure
   hardening of the live path; no behavior change on valid data.
2. **PR 2 — Retire the v3 orphan.** Delete `execution/fetch_fmp_statements.py` + its test; add the CI grep
   guard (§7.5). Safe because it is unreferenced and PR 1 moved its one unique value (the gate) into the live
   path. Confirm zero callers first.
3. **PR 3 — Tier-aware ladder (stable-only on free).** The stable probe of all 67 endpoints (record results),
   then gate v3/v4 rungs off `FMP_TIER` in `save_fmp_data.py` + `fetch_etf_data.py` (§5); confirm/set the
   free budget in `refresh_cache.py`. Tests §7.1. After this, a free-tier run makes zero v3/v4 calls.
4. **PR 4 — macro_series + doc reconcile.** Migrate `fetch_macro_series.py` v3/v4 series to stable (§6);
   reconcile the suffixed/unsuffixed comment in `fmp_derived_kpis.py`. Tests §7.6-§7.7.
5. **(Deferred) PR 5 — shared `src/sources/fmp_client.py`.** Extract the stable ladder + `Error Message`
   guard + token bucket + Pydantic gate into one client used by all fetchers (Option C). Out of scope for the
   free-tier deadline; queue for the roadmap.

End state: no scheduled or reachable code calls `/api/v3` or `/api/v4`; the live cacher validates stable
statement writes; the free-tier account makes only stable calls within a documented 250/day budget — and the
news plan's WebSearch+Opus fallback covers what FMP drops entirely.
