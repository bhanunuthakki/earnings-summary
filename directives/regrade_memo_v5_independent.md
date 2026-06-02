# Codebase Re-Grade Memo v5 — Independent Validation of the 8.5 Build

**Date:** 2026-06-01 · **Branch:** `claude/optimistic-kepler-62f167` · **Mode:** independent, read-only re-grade
**Corpus:** the v4 "drive every axis to 8.5" sweep — PRs **#251** (Quality/CI), **#252** (Data reliability / migration 0068), **#253** (Smart caching), **#254** (LLM pass-through), **#255** (Richness/Product), graded by **#256**.
**Method:** five parallel read-only axis auditors (Opus), each reading the merged code and verifying every v4 claim with file:line evidence, plus an independent product assessment — all cross-checked against the **live prod DB** (`data/portfolio.db`, queried `?mode=ro`). No application code was edited; no extractor, pipeline, or migration was run; the prod DB was never modified.

This memo continues the lineage: original baseline **5.4** → mid **6.6** → v3 **7.2** → post-#219–237 addendum **7.3** → **v4 self-grade 8.5 (every axis)**. My job was to validate or dispute that 8.5.

---

## Executive summary

**The 8.5 does not hold under independent scrutiny. The honest composite is 7.9** — a real, defensible improvement over the 7.3 the build started from, but **0.6 below the self-grade**, and **every single axis was graded above what the merged code and the live database actually deliver.** The self-grade is *generous, not fraudulent*: the underlying engineering is genuinely strong and most headline claims are real at the code level. What the self-grade systematically did was **credit capability-as-built rather than behavior-as-enforced-in-production** — the exact failure mode the v3 memo warned about ("the capabilities are real; the seams between them are where it leaks").

Three independent findings drive the correction, none of which the v4 memo disclosed:

1. **The centerpiece of the Data-reliability axis was never applied to prod.** Migration `0068` (substrate CHECK constraints + the alert-dedup UNIQUE index) is correct and merged — but the **live prod DB is at alembic `0067`**, so *none* of those constraints exist in the running schema. I confirmed it directly: the live `alerts`/`queued_actions` DDL carries **no CHECK clause**, `alerts` has only its three old non-unique indexes, and the NULL-direction registry row the migration was supposed to address **is still present** (1 of 18). The v4 memo's own qualifier — "verified on a *copy* of the prod DB" — concedes this, but it then graded the axis as if the constraints enforce. They do not.

2. **Both axes the v4 author flagged as "soft 8.5s" are, on the evidence, 8.0 — and the prod data is harsher than the flags admit.** Smart caching's "cache effectiveness is now measurable" headline rests on a `source_calls` table that in prod holds **60 rows, every one a yfinance/`fmp_cache` live-price read, zero rows from the dominant FMP fetch path, and no cost column at all** — FMP cache state actually lives in a *different* table (`fmp_endpoint_status`, 139,676 rows). LLM pass-through's calibration loop is not merely "limited to two purposes"; the `prompt_calibration_scores` table has **0 rows in prod** and 100% of 246 `llm_artifacts` are `prompt_version='v1'` — the feedback machinery has *never produced a single score in production*.

3. **The product has turned its loop exactly once.** The decision loop genuinely closes end-to-end (17 queued actions → all `applied` → 17 thesis-ledger rows) and the new surfacing renderers are real and correct (I confirmed the evidence drawer renders **all 22 citations** by importing it and feeding it the real alert). But there is **one alert, ever** (NU / `earnings_tone`); three of four triggers have **never fired live**; `news` holds **one row**; and every Personal-CIO surface (digest, feed, drawer, ledger) is reachable only as a **static HTML file** — none is a route in the :7421 command center. A product is graded on whether it works for the user *today*, and today it is a richly-built engine that has run once.

**Net read:** the build did a large amount of real, verifiable work and moved the composite a genuine +0.6 (7.3 → 7.9). But the 8.5 over-credits dormant capability — an unapplied migration, an empty calibration table, an FMP-blind metrics table, an unregistered cron, and a set of surfaces the live app can't reach. Corrected for live behavior, this is a strong **7.9**, with the product dimension an honest **7.0**.

---

## Scoring table

| Axis | baseline | mid | v3 | post-#237 | **v4 self-grade** | **v5 independent** | Δ vs self-grade |
|---|---|---|---|---|---|---|---|
| Data reliability | 5 | 6.5 | 7.5 | 7.5 | **8.5** | **7.5** | **−1.0** |
| Quality enforcement | 6 | 6.5 | 6.5 | 6.5 | **8.5** | **8.0** | **−0.5** |
| Smart caching | 5 | 6.5 | 7.5 | 7.5 | **8.5** | **8.0** | **−0.5** |
| Richness & surfacing | 5 | 6.0 | 7.0 | 7.5 | **8.5** | **8.0** | **−0.5** |
| LLM pass-through | 6 | 7.5 | 7.5 | 7.5 | **8.5** | **8.0** | **−0.5** |
| **Composite** | **5.4** | **6.6** | **7.2** | **7.3** | **8.5** | **7.9** | **−0.6** |
| *Personal-CIO application* | — | — | 5.5 | 6.5 | **8.5** | **7.0** | **−1.5** |

Composite = mean of the five axes, consistent with every prior grade: (7.5 + 8.0 + 8.0 + 8.0 + 8.0) / 5 = **7.9**.

---

## Per-axis findings

### 1. Data reliability — **7.5/10** (v4 claimed 8.5; **−1.0**)

The v4 claim: migration `0068` adds CHECK constraints on every substrate enum plus a partial-UNIQUE alert-dedup index that makes "at most one active alert per `(user_id, signature_sha)`" a real DB guarantee; the `--write` seeder runs the polarity gate; `NewsRow` rejects empty fields.

**What is real and live:** The seeder `--write` path now runs the same adverse-direction polarity gate as `--auto` (`scratch/seed_kpi_registry.py:846-865`, mirroring `_gate_proposal:1209-1213`) — a genuine fix for the "hand-edit persists a backwards thesis-breaker" hole. `NewsRow` rejects empty url/ticker/headline via `Field(min_length=1)` (`src/news/store.py:55-57`), confirmed to raise `ValidationError` on empty strings. Both are code-level guards that run on every write, so they *do* enforce today.

**What is written-but-dormant (the centerpiece):** Migration `0068_substrate_constraints.py` is correctly authored — the CHECK clauses (`:75-100`) and the partial-unique index `uq_alerts_active_signature` with `sqlite_where=sa.text("status <> 'expired'")` (`:79-86`) are syntactically real and well-aligned to the trigger-kind vocabulary. **But the live prod DB is at `0067_ticker_settings`** (verified: `SELECT version_num FROM alembic_version`). The live `alerts` and `queued_actions` table DDL contains **no `CHECK`**, and `alerts` carries only `ix_alerts_user_ticker_fired`, `ix_alerts_user_status`, `ix_alerts_user_signature` — the partial-unique index is **absent**. Reconstructing the live DDL in memory, an `INSERT … status='TOTALLY_INVALID', trigger_kind='BOGUS_KIND'` *succeeds*, and a duplicate active `(user_id, signature_sha)` is *accepted*. The data-reliability guarantee the memo credits to 0068 **provides zero live enforcement.**

**Two findings the v4 memo did not disclose:**
- **The NULL-direction CHECK is a deliberate no-op against NULL.** The constraint text is `"threshold_direction IS NULL OR threshold_direction IN ('above','below')"` (`0068:97-100`), and the docstring (`:23-24`) says so explicitly. So even *if* 0068 were applied, the existing AMZN NULL row (`user_kpi_registry id=1`, still live) would pass, and new NULLs remain reachable via `--write` (`seed_kpi_registry.py:503-512`, gate skipped when direction is None at `:851`). The framing that 0068 hardens against the NULL problem is misleading.
- **Per-line-item provenance writes to a dropped table.** `_log_brief_provenance` (`execution/build_artifacts.py:451-510`) targets `brief_provenance_log`, which was **dropped at `0031_drop_dead_tables.py:43` and never recreated** — so in prod the writer is a guarded no-op (`build_artifacts.py:487`). The residual "covers 2 of 6 sections" undersells: live, it persists nothing.

**Verdict:** The applied work (seeder + NewsRow) is real v3-tier hardening but does not move the axis; the *headline* deliverable (DB-enforced substrate integrity) is inert in production and the NULL "fix" is a non-fix by design. This holds at **7.5**, not 8.5 — no regression, but the centerpiece is dormant.

### 2. Quality enforcement — **8.0/10** (v4 claimed 8.5; **−0.5**)

This is the axis closest to its self-grade, and the work is the most genuinely transformative. `.github/workflows/ci.yml` is real, triggers on `pull_request` (`:19-22`), and runs the **full pytest suite as a blocking gate** (`:52-53`, `pytest -q` with no `|| true`). The **diff-aware ruff gate is genuinely correct, not a no-op** — I read the bash (`ci.yml:109-130`): for each changed file it counts violations in the PR version (`head_n`) versus the base version pulled through `git show "$base:$f" | ruff check --stdin-filename` (`base_n`) and fails only when `head_n > base_n`. `ruff format --check` is a hard gate on changed files (`:95-97`). The flake fix is real and load-bearing: `alembic/env.py:25` now calls `fileConfig(..., disable_existing_loggers=False)`, with a comment naming the exact failure (a migration test muting `llm.cli` warnings the caplog expected). I ran the previously-polluting tests in their failing order: **60/60 pass**; a cross-section ran **80/80**; collection reports **1952 tests** (the claimed 1951, +1 drift).

**Why it's 8.0, not 8.5:** the axis measures enforcement *by machinery*, and two of the project's four quality dimensions remain advisory. **Pyright — the project's own documented strict gate — is not a blocking CI job**; `ci.yml:14-17` deliberately omits it, and it appears only in `.pre-commit-config.yaml:23` (a `pre-push` hook, locally bypassable with `--no-verify`) and `Makefile:40-44`. The **validation engine is still post-hoc**: `src/pipeline/validation_engine.py` *emits* `validation_issues` rows and is imported by only the manual CLI `execution/run_validation_engine.py` — a grep of `src/**/*.py` finds zero callers in any write path. Two minor structural notes: the diff-aware gate is *count-based*, so a new violation can be masked by removing an unrelated pre-existing one in the same file; and both the ruff (331) and pyright (~3070) baselines only ratchet — nothing pays them down. CI that blocks on tests + net-new lint is a large, real step from 6.5; CI that leaves the project's stated type gate and its data-integrity engine outside the gate is **8.0**.

### 3. Smart caching — **8.0/10** (v4 claimed 8.5, flagged soft; **−0.5**)

Two of the three pillars are real wins. **Invalidation:** `news_structuring` joined `FACT_DEPENDENT_PURPOSES` with a 7-day TTL (`src/llm_artifact_store.py:347`, `:399`), and the chain is genuinely end-to-end — the WebSearch news cache keys on `anchor_sha` (`execution/fetch_news_websearch.py:157-158`), reads the dirty flag (`:170`), and writes via `upsert(purpose="news_structuring")` (`:182`), so a thesis-anchor restatement now proactively dirties it. **Batch discount:** the SayDo path correctly applies the 50% Message-Batches discount (`BATCH_DISCOUNT = 0.5`, `execution/submit_saydo_batch.py:62`, applied `:620-626`). The drain reclassification is honest (`refresh_dirty_artifacts.py:126-134` classifies the five trigger/news purposes as `refreshed_by_daily_scan` info, not `no_regenerator` warn).

**Why the soft flag is correct and the score is 8.0:** the *observability* pillar — "cache effectiveness is now measurable via `source_calls`" — is hollow for the dominant path. The read-side consumer is real, tested code (`summarize_source_calls`, `src/sources/registry.py:183-247`; `execution/show_source_calls.py`). But **the main FMP fetch path does not log to it**: a grep of `execution/save_fmp_data.py` for `source_call` returns **zero matches** — `_http_get` (`:188-212`) writes only to `fmp_endpoint_status`. Only two source-router adapters (`src/sources/price.py`, `src/sources/earnings_calendar.py`) ever call `log_call`. The live table proves it: **60 rows total — 58 yfinance ok, 1 yfinance error, 1 `fmp_cache` ok — zero FMP-fetch rows, zero `skipped` rows, and no cost column.** Running `show_source_calls.py` against prod today reports a structural 0% cache-skip rate and says nothing about FMP. Meanwhile the table where FMP cache state *actually* lives, `fmp_endpoint_status`, holds **139,676 rows**. The metric is built but blind to ~95% of fetch volume. Two further dings: the SayDo cron exists as a committed file (`cron/submit_saydo_batch.task.xml`) but is **not registered** in Windows Task Scheduler (its five siblings are), so the discount remains dormant in practice; and the #253 commit message claims to fix `PURPOSE_TO_REGENERATOR_HINT`, a symbol that **does not exist** in the repo. This is exactly the original 8.0 projection — met, not exceeded.

### 4. LLM pass-through — **8.0/10** (v4 claimed 8.5, flagged soft; **−0.5**)

The two plumbing wins are **real and fully verified.** The hard web cap is a genuine CLI-enforced subprocess flag, not prompt prose: `src/llm/cli.py:613-627` builds the command with `"--max-budget-usd", str(CLAUDE_WEB_MAX_BUDGET_USD)`, the value is a real env-backed budget (`:219`, default $2.0), and `claude --help` confirms the flag enforces a dollar ceiling on `--print` calls. The 3-block thesis+bear+IR anchor is composed in **all four** triggers — `earnings_tone.py:776`, `material_news.py:680`, `kpi_inflection.py:747`, `saydo_due.py:623` each call `compose_anchor_block(load_thesis_anchor, load_bear_anchor, load_ir_anchor)`, which joins all three non-empty blocks (`src/llm/anchors.py:447`). Opus routing is sensible and `bear_case` correctly stays on Sonnet (`cli.py:102`); crash-safety (`is_hard_stop`, typed `LLMSetupError`/`LLMBudgetExceeded`) is exemplary.

**Why the soft flag is correct and the score is 8.0:** "pass-through quality" includes the feedback loop that improves prompts over time, and that loop is **structurally dead in production** — worse than the memo's "deferred" framing. Only two producers call `record_score` — `grade_bear_cases.py:129` (`bear_case`) and `grade_decisions.py:237` (`decision_audit`) — and **no trigger module participates**. `prompt_version` is hardcoded `"v1"` at every write site with **no `"v2"` anywhere** in the codebase, so `summarize_by_prompt_version` has nothing to compare. The prod DB confirms it is not just limited but *empty*: `prompt_calibration_scores` has **0 rows**, and **246/246 `llm_artifacts` are `v1`**. The #243 predictions grader (`execution/grade_predictions.py`) grades management predictions against `kpi_facts` and does **not** call `record_score` — it does not extend calibration to any trigger purpose. Deferring "the single biggest LLM gap" to a parallel lane is a legitimate scoping choice, but an open gap cannot also count toward the score. Real plumbing (+0.5) on top of an unchanged dead feedback loop is **7.5 → 8.0**, not 8.5.

### 5. Richness & surfacing — **8.0/10** (v4 claimed 8.5; **−0.5**)

The three new renderers are **genuinely built, correct, and key-matched** — this is the build's most fully-delivered claim. The evidence drawer flattens `shifts[].citations` (`src/dashboard/evidence_drawer.py:114-115`, `:226-237`) and composes a locator from `period`+`line_number` (`:210-223`); the real citation dicts in alert id=1 carry exactly `['excerpt','kind','line_number','period']`, an **exact match** to the code's expected keys. The auditor imported `render_evidence_drawer`, fed it the real evidence read-only, and it rendered **22 citation rows** (sample: `Q1-2026 · line 165`) — the "22 render" claim independently reproduced. The thesis-ledger renderer is real (`list_recent_entries` at `src/user_state/ledger.py:117-143`, rendered by `digest.py:183-220`) and would show all 17 rows. The card memo falls back `memo → summary → why_material → narrative` (`src/dashboard/_card.py:131`), rendering the live alert's `summary` rather than "memo pending."

**Why it's 8.0, not 8.5:** richness measures whether computed intelligence *reaches the eye in the app the user lives in*, and the entire Personal-CIO surface does not. Every one of these renderers feeds only a **static-file CLI** — `build_morning_digest.py` → `data/dashboard/morning/index.html`, `build_alert_feed.py` → `data/dashboard/feed.html`. `execution/comments_server.py` has **no `/digest`, `/feed`, `/alerts`, or `/ledger` route**; the command-center panels are portfolio/holdings/prereads/insiders/predictions/decisions/budget. (Tellingly, the live "decisions" panel reads the `decisions` table, which has **0 rows** in prod — a different table from the 17-row `thesis_ledger_entries`.) There is **no `.xlsx` export** of any CIO content (`report/renderers/workbook.py` has 8 tabs, none for alerts/ledger/signals), and the digest's "Upcoming this week" remains a hard-coded stub (`digest.py:162-174`). Excellent renderers whose only reach is a static file land at the projection — **8.0**.

---

## Verdict on the two flagged-soft 8.5s, and the runtime caveat

**Both flagged-soft 8.5s are confirmed soft and corrected to 8.0 — the v4 author's instinct was right, and the prod data is harsher than the flags.**

- **Smart caching:** the flag said FMP isn't instrumented "yet." The live reality is stronger — `source_calls` is *empty of FMP entirely* (0 of 60 rows), has no cost column, and reports a structural 0% skip rate, so "cache effectiveness is measurable" is true only for peripheral live-price quotes and false for the path driving 139k cached endpoints. **8.0.**
- **LLM pass-through:** the flag said calibration "still grades only two purposes." The live reality is that calibration has produced **zero scores ever** (0-row table) and the A/B field is permanently `v1` (246/246 artifacts). The single biggest LLM gap is not just deferred — it has never run. **8.0.**

**The runtime / firing-breadth caveat is the dominant cap on the product, and it is more limiting than the self-grade allows.** Across the entire system, exactly **one alert has ever fired** (id=1, NU, `earnings_tone`, still `pending`). `kpi_inflection`, `saydo_due`, and `material_news` have **never produced a live alert**; the `news` table holds **one row**. The decision loop closing (17 actions applied, 17 ledger rows) is real and valuable — but it has turned **once, on one ticker, via one of four triggers.** "Wired, anchored, enum-gated, unit-tested" is not "live," and a product grade must weigh the latter.

---

## Personal-CIO product — **7.0/10** (v4 claimed 8.5; **−1.5**)

Judged on whether it works for the user *today*:

- **(a) Taxonomy complete in code, idle in prod.** Four triggers implemented and enabled; one alert ever fired. The gap between built and live is the whole story.
- **(b) The loop closes — once.** 17 queued actions → all `applied` → 17 ledger rows is genuine and was the #1 v3 gap. But it predates this build (#231, already credited in the 6.5), and it has exercised exactly one alert from one ticker. The v4 build's *own* product contribution is the surfacing (drawer/ledger/memo) and the trigger anchoring — real, but in service of a loop that has run once.
- **(c) Surfaces orphaned.** The drawer, ledger, and memo renderers are correct and populated, but unreachable from the :7421 app — a user must know to open a static HTML file. This is the single highest-impact richness/product gap.
- **(d) Substrate not hardened in prod.** The "enum-safe at the DB layer" trust claim rests on 0068, which is not applied (live = 0067). The DB-level guarantees do not exist for the running product.
- **(e) Runtime reality:** 1 alert, 1 news row, 3 of 4 triggers never fired, calibration empty, command-center "decisions" panel empty.

A loop proven once, on one ticker, with surfaces reachable only as static files and constraints not enforced in prod, is a credible **7.0** — a real, well-engineered v1 whose contents have been made rich but whose reach and runtime exercise remain thin. The architecture would earn an 8; the *working product today* is a 7.

---

## Independent findings beyond the v4 author's conceded residuals

These are catches the v4 memo did not disclose — the substance of an independent check:

1. **Migration 0068 is unapplied to prod (live = `0067`).** The substrate-integrity centerpiece provides zero live enforcement; even the v5 task brief assumed "~0068." *(Data reliability, Product)*
2. **0068's NULL-direction CHECK is a no-op against NULL by design** — the live AMZN NULL row would survive the migration. *(Data reliability)*
3. **Per-line-item provenance writes to `brief_provenance_log`, dropped at 0031 and never recreated** — a guarded no-op in prod. *(Data reliability)*
4. **`source_calls` is empty of FMP** (60 rows, all yfinance/`fmp_cache` live-price; no cost column) — "measurable" is hollow for the dominant source; FMP state lives in `fmp_endpoint_status` (139,676 rows). *(Smart caching)*
5. **The SayDo batch cron file is committed but not registered** in the scheduler — the 50% discount stays dormant. *(Smart caching)*
6. **`prompt_calibration_scores` has 0 rows; 246/246 `llm_artifacts` are `v1`** — the feedback/A-B loop has never produced data. *(LLM)*
7. **The command-center "decisions" panel reads the empty `decisions` table** (0 rows), not the 17-row ledger — even the loosely-related live surface is blank. *(Richness/Product)*
8. **The diff-aware ruff gate is count-based**, so a new violation can be masked by deleting an old one in the same file. *(Quality, minor)*
9. **The #253 commit message references `PURPOSE_TO_REGENERATOR_HINT`, which does not exist.** *(Smart caching, cosmetic)*

---

## Honest closing

**Is this a genuinely good tool?** Yes — and more so than at any prior grade. The financial-brief engine was already good, and the v4 sweep added real, verifiable machinery on top: a CI that blocks on the full test suite with a correctly-implemented diff-aware lint ratchet; an evidence drawer that genuinely renders 22 citations from the live alert; a ledger renderer over 17 real rows; a hard, CLI-enforced web-cost cap; a 3-block anchor in all four triggers; an end-to-end news-cache invalidation chain; and live seeder/NewsRow guards. None of these is veneer. The auditors confirmed each at the code level, and several at the data level.

**Was the 8.5 self-grade fair, generous, or conservative? Generous — by about 0.6 composite, and by 1.5 on the product.** Not dishonest: the memo flags its two softest axes accurately and is candid that only one trigger has fired. But it grades on what was *built and merged* rather than what *enforces in production*, and that distinction is precisely where the half-points (and, for data reliability, a full point) live. An unapplied migration is graded as a hardened substrate; an FMP-blind metrics table is graded as measurable caching; a 0-row calibration table is graded as a closing feedback loop; a set of static-file surfaces is graded as intelligence that reaches the eye. Each is real *capability*; none is yet real *behavior*.

The encouraging part — as in every prior memo — is how closeable the gap is. Four of the five corrections are one operational step from vanishing: run `alembic upgrade head` against prod (0067 → 0068, instantly making the substrate claims true), register the SayDo cron, add one `log_call` to `_http_get`, and wire `/digest`+`/feed` into the Flask shell. The fifth — extending calibration to the trigger purposes and minting a `v2` prompt — is the one genuinely deferred piece of capability. **The bones are an 8.5; the running system is a 7.9; the product the user touches is a 7.0. The self-grade described the bones. This memo describes the system.**

*All prod-DB reads in this memo were performed read-only (`?mode=ro`); the bad-enum / duplicate-insert checks ran against in-memory copies reconstructed from the live DDL. Live trigger-firing breadth remains the authority of a separate end-to-end run; where this memo distinguishes "built" from "live," the prod database — not the code — is the source of truth.*
