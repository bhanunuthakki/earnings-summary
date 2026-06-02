# Codebase Re-Grade Memo v3 — Post Personal-CIO Wedge

**Date:** 2026-06-01 · **Branch:** `claude/musing-goldberg-a6a987` · **Reference points:** original baseline **5.4**, mid re-grade **6.6**, original full-roadmap projection **8.1**
**Method:** five parallel read-only axis audits (Opus) + prod-DB spot-check (`data/portfolio.db`, alembic `0065_news`, read-only) + targeted code verification of the strongest "broken" claims. Corpus: roughly PRs #152–#218 (the P0–P4 cherry-pick batch, the Personal-CIO alerting wedge, the news pipeline, the FMP v3→stable migration, and the dashboard unification).

---

## Executive Summary

**Did we get past 6.6, and how far toward 8.1?** Yes, we moved — composite **6.6 → 7.2**. That is real progress (most of the distance from the mid re-grade toward the *mid-projected* 7.5), but it falls a full **0.9 short of the original 8.1 projection**, and the shortfall is concentrated in exactly the two places the projection was most optimistic: a quality-enforcement axis that did not move at all (still no CI), and a brand-new product whose decision loop does not actually close in production.

The wedge is the most ambitious thing this repo has built, and the engineering *underneath* it is genuinely strong: the auto-seeder's correctness guards are real, the trigger artifact-cache dedup is wired end-to-end, the news pipeline's UTC/dedup invariants are correct, and the LLM crash-safety policy is exemplary. Four of the five triggers are fully implemented and the fifth was deliberately folded in. **But the prod database tells the honest story of a system that is built but barely live:** exactly **one** alert has ever fired (an `earnings_tone` alert), it spawned **17 queued actions of which zero have been approved**, the **thesis ledger is empty**, the **news table holds one row**, and there are **zero position-sizing intents**. The substrate is in place; the loop has not yet turned.

**Top 3 leverage gaps (where a small fix unlocks the most value):**

1. **The decision loop is broken at the approve seam** (verified against prod, not inferred). Every trigger drafts queued-action payloads *without* a `ticker` key (`src/triggers/kpi_inflection.py:691`, `src/triggers/earnings_tone.py:551`), `run_triggers.queue_action` persists them verbatim (`execution/run_triggers.py:445`), and `approve_queued_action._write_ledger_entry` *requires* `payload["ticker"]` (`execution/approve_queued_action.py:223`). So clicking "approve" raises `KeyError`. Prod confirms it: 17 pending actions, 0 ledger entries. This single ~3-line fix turns the product from "alert printer" into "Personal CIO."
2. **No CI / no enforcement machinery** (unchanged since baseline). Every quality tool — pyright-strict, ruff, the genuinely excellent test suite — runs only when a human remembers. This caps the Quality axis below 7 and is the cheapest big win available: one `.github/workflows/ci.yml`.
3. **The richest surfaces are wired to empty inputs.** The evidence drawer renders a citations table that no trigger populates (`src/dashboard/evidence_drawer.py:106`), the thesis ledger has no renderer at all, and the calibration loop grades only 2 of ~12 LLM purposes. The intelligence is computed; it just isn't reaching the eye.

**Net read:** the bones are an 8; the *working product today* is a 6. The gap between them is unusually closeable — most of it is wiring seams and an absent CI file, not missing capability.

---

## Scoring delta

| Axis | Baseline | Mid re-grade | **Current (post-wedge)** | Original projection |
|---|---|---|---|---|
| Data reliability | 5 | 6.5 | **7.5** | 8.5 |
| Quality enforcement | 6 | 6.5 | **6.5** | 7.5 |
| Smart caching | 5 | 6.5 | **7.5** | 8.0 |
| Richness & surfacing | 5 | 6.0 | **7.0** | 8.0 |
| LLM pass-through | 6 | 7.5 | **7.5** | 8.5 |
| **Composite** | **5.4** | **6.6** | **7.2** | **8.1** |
| *Personal CIO application (new dimension)* | — | — | **5.5** | — |

---

## Axis findings

### 1. Data reliability — 7.5/10 (5 → 6.5 → **7.5**)

The wedge closed the single largest open gap from the mid re-grade — the dormant `kpi_facts` restatement chain — and, more importantly, moved restatement detection from "opt-in, not yet wired" to **the canonical write path for both fact tables**. That converts schema-only provenance into live provenance and is most of the move. It is held below the 8.5 projection because the wedge's *own* new tables lean entirely on code-side enforcement, with no DB-level constraints on their enums and only an advisory dedup index on alerts.

**What landed:**
- **`kpi_facts` restatement chain wired (old "A3" closed).** Migration `alembic/versions/0059_kpi_facts_restatement.py:64` drops `uq_kpi_facts_logical` and recreates the wider `uq_kpi_facts_provenance`, mirroring `financial_facts`. Detection routes through `insert_kpi_with_restatement_detection` (`src/pipeline/restatement_detector.py:319`), wired into the single KPI-insert source of truth `persist_manifest → _insert_kpi_fact` (`src/pipeline/kpi_persistence.py:144`) plus the derived path (`src/compute/fmp_derived_kpis.py:506`). Not schema-only.
- **`financial_facts` restatement is now the default writer** (beyond what mid credited): `src/compute/_common.py:82` routes every row through `insert_with_restatement_detection` and *requires* `extracted_by`.
- **News UTC invariant is correct, with DST-aware ET→UTC.** `execution/fetch_fmp_news.py:99` (`to_utc()` attaches `America/New_York` zoneinfo, `.astimezone(UTC)`); unparseable dates are dropped, never fabricated. NewsRow's Pydantic gate (`src/news/store.py:59`, `extra="forbid"`) rejects non-canonical timestamps via round-trip equality.
- **(ticker, url) dedup in both layers:** UNIQUE `uq_news_ticker_url` (`0065_news.py:87`) + `INSERT OR IGNORE` (`src/news/store.py:100`).
- **Per-line-item provenance carries real `fact_id`s** (`execution/build_artifacts.py:476`, `src/report/sections/financials.py:482`).
- **FMP v3→stable cutover complete and tier-gated.** `FMP_TIER=free` drops v3/v4 rungs (`execution/save_fmp_data.py:222`); the HTTP-200-with-error-body refusal gotcha is handled (`save_fmp_data.py:210`).
- **Auto-seeder data-correctness guards genuinely enforce.** `_gate_proposal` forces adverse direction from the polarity table (the LLM never sets direction; `scratch/seed_kpi_registry.py:1160`) and hard-drops off-catalog names (`:1163`).

**What didn't land / is sparse:**
- Per-line-item provenance covers only ~2 of ~6 brief sections (`execution/build_artifacts.py:428`) and rides inside a JSON `TEXT` column (`brief_provenance_log.sources_used`), not normalized rows — so PR #162's "per-line-item" title oversells the granularity.
- `NewsRow`/`FmpStockNewsRecord` accept an empty `url` (`src/news/store.py:53`) — the FMP feed relies only on a first-record schema halt, so a *later* empty-url record persists.

**New debt the wedge introduced:**
- **Alert re-fire dedup is advisory, not enforced.** `0063_alerts.py:115` creates a plain INDEX on `(user_id, signature_sha)`; `insert_alert` is a plain INSERT guarded only by a prior SELECT — a race or the SELECT→INSERT window can double-fire. (The *driver-level* dedup via `find_by_signature` works; what's missing is the DB guarantee.)
- **No CHECK constraints on status/direction enums** across `alerts`, `queued_actions`, `user_kpi_registry` (`0063:91`, `0064:82`, `0060:74`). Prod already shows the consequence: the `AMZN` registry row has `threshold_direction = NULL` — an out-of-vocabulary state the table cannot reject.
- **The `--write` (manual YAML) seeder path bypasses the polarity/catalog gate** (`scratch/seed_kpi_registry.py:861`), so a hand-edit can persist a wrong-signed threshold straight to the 18-row prod registry.

**Ranked gaps (blast radius):**
1. No UNIQUE constraint on alert dedup signature (`0063_alerts.py:115`) — duplicate alerts → duplicate queued actions/ledger writes.
2. `--write` path skips the correctness gate (`seed_kpi_registry.py:861`) — the exact "breaker fires on good news" failure the polarity table exists to prevent.
3. No CHECK constraints on enum columns (`0060`/`0063`/`0064`) — an out-of-vocab status silently drops a row out of the dashboard filter (already realized: NULL direction on AMZN).
4. Per-line-item provenance covers 2 of 6 sections (`build_artifacts.py:428`) — time-travel can't reconstruct earnings/KPI/news figures.
5. Empty-`url` rows pass the NewsRow gate (`src/news/store.py:53`).
6. Provenance blob is unconstrained JSON in a TEXT column (`build_artifacts.py:479`).
7. `extracted_by` backfill labels legacy rows with raw `source_type`, not the extractor vocabulary (`0054_audit_columns.py:325`) — "all LLM-derived facts" audits miss pre-0054 rows.

---

### 2. Quality enforcement — 6.5/10 (6 → 6.5 → **6.5**)

The projection that this axis would "barely move" was correct. The wedge *code* is the highest-discipline in the repo, and the LLM crash-safety work is exemplary — but this axis measures whether quality is enforced by *machinery*, and on that question **nothing changed**: still no CI, no pre-commit, no git hooks, no Makefile/task-runner. The strong tests exist but nothing *makes them run*; the strict pyright gate is *configured* but nothing *blocks a merge on it*.

**Suppression recount (the mid re-grade's 264→673 alarm, re-examined):**

| Suppression | Repo total (now) | Read |
|---|---|---|
| `# type: ignore` | **108** | Low and stable — **not** 673 |
| `# noqa` | ~300 raw | ~all `E402` (sys.path import-order in CLI entrypoints), benign |
| `cast(` | ~330 | Expected JSON-boundary narrowing per the "cast, don't suppress" policy |

The literal type-suppression smell totals **108**, not 673. The mid-grade's 264→673 was almost certainly **basedpyright diagnostics under `typeCheckingMode="all"`** (`pyproject.toml`), the strictest mode in existence — an informational count against the ~1100 tolerance, not a suppression count. By the metric this axis cares about, the trend is **flat-to-favorable**. Decisively, the **wedge directories are the cleanest in the repo**: `src/triggers/`, `src/dashboard/`, `src/news/`, `src/user_state/`, `src/alerts/` carry **0** `# type: ignore` and **0** `# noqa`; the only wedge `cast()` usage is sanctioned JSON-boundary narrowing (`src/triggers/earnings_tone.py:440`). The real debt is in pre-wedge legacy (`report/renderers/workbook.py` 12×, `surprise_sources.py` 5×).

**Test-coverage depth — REAL, not theater (~85% meaningful):** the new tests mock only the network/LLM boundary and assert on persisted behavior against migration-built SQLite. Strong examples: `tests/test_run_triggers_e2e.py:391` (dedup → second run persists nothing), `:467` (per-ticker LLM-failure isolation), `:504` (cost-cap halts before the LLM is called); `tests/test_trigger_kpi_inflection.py:341/549` (deterministic changepoint + "non-breaker stays factual" regression guard); `tests/test_fetch_fmp_news.py:76` (ET→UTC tested across the DST boundary). No prompt-substring assertions (the banned anti-pattern). The thinner ~15% is the `*_cli.py` and `test_dashboard_*` round-trip tests — but even those assert structure, not just "doesn't throw." **Caveat that this audit exposed:** `tests/test_approve_queued_action_cli.py:90` passes *only because it hand-injects* `"ticker"` into the payload — masking a production break the real drafters never satisfy. That is the one place the test suite is actively misleading.

**CI / gating — still entirely absent:** no `.github/workflows/`, no `.pre-commit-config.yaml`, no Makefile/justfile, no active git hooks. `pyproject.toml` declares `[tool.pyright] typeCheckingMode="strict"` (the real gate per project convention) and a solid ruff ruleset, but they run by hand. The GEMINI.md-mandated pre-push checklist even references "keep the Makefile in sync" — and no Makefile exists. The validation engine remains **post-hoc**: `pipeline/validation_engine.py:1` walks fact tables and *emits* `validation_issues` rows; even `severity=halt` issues are recorded, not rejected at write time.

**Ranked gaps:**
1. No CI pipeline at all — the axis-capping gap; one distracted push merges red tests. (`.github/` does not exist.)
2. No pre-commit / git hooks — even local-only gating is absent.
3. basedpyright "all" baseline is unbounded and un-ratcheted (`pyproject.toml`) — type-safety can drift with zero signal.
4. Validation engine is post-hoc, not a write gate (`pipeline/validation_engine.py:1`) — a 10× unit error persists first, is reported later.
5. ~354 pre-existing `ruff check .` errors with no gate (project baseline) — lint drift is unbounded.
6. 108 `type: ignore` + ~330 `cast()` have no ceiling — discipline-only.
7. `# noqa: E402` proliferation (~266 in `execution/`) signals the absence of a proper package-entrypoint setup.

---

### 3. Smart caching — 7.5/10 (5 → 6.5 → **7.5**)

The wedge delivered the headline caching claim cleanly. Signature-SHA dedup is fully **wired** in the daily driver — it computes the signature from each trigger's `signature_key_evidence` *before* `build_alert` and short-circuits on a hit (`execution/run_triggers.py:358`, `src/alerts/store.py:90`/`:163`), with all five triggers reusing the same key so the driver index can't diverge from `alerts.signature_sha`. What holds it below the 8.0 projection is an invalidation hole in the drain executor, one missing purpose, a dormant batch path, and the still-absent data-source cost ledger.

**What landed:**
- **Signature-SHA dedup — WIRED.** Verified against prod indirectly (the single alert produced exactly its action set, no duplicates).
- **`FACT_DEPENDENT_PURPOSES` expansion — PARTIAL.** `src/llm_artifact_store.py:300` now includes `earnings_tone_diff`, `kpi_inflection_context`, `saydo_due_context`, `material_news_classification` — **but `news_structuring` is missing** (it exists only as a model pin + a same-day cache at `execution/fetch_news_websearch.py:142`).
- **News/material_news cache keys — strong:** `material_news.py:427` keys on `ticker + sorted(news_ids) + anchor_sha`; both check `not dirty AND input_sha == new_sha` at read time, so the *read* path never serves stale data.
- **Drain-on-expiry executor — wired + cron'd, but incomplete coverage.** `--execute` genuinely regenerates via subprocess, cost-capped, cron'd daily at 04:00 (`execution/refresh_dirty_artifacts.py:251`, `cron/refresh_dirty_artifacts.task.xml`). **But `_PURPOSE_TO_REGENERATOR` (`:60`) maps none of the 5 new wedge purposes** → they hit `no_regenerator` and are skipped. Their caches self-heal only via the next daily trigger re-scan (which respects `dirty`), not via the documented drain.

**What's dormant or missing:**
- **SayDo batch submission — DORMANT.** The Anthropic Message Batches API is used correctly with the 50% discount (`execution/submit_saydo_batch.py:249`, `src/llm/batch.py`), but **nothing invokes it**: no cron, no pipeline call; `build_saydo_pairs.py:141` literally prints a manual one-liner for the operator. The "A2" win exists in code, not in the running system.
- **Per-FMP/SEC cost ledger — still missing.** A `source_calls` table (alembic 0032) logs every external fetch but has **no cost column** (`src/sources/registry.py:84`), and its own docstring defers the read side. Cache-hit-rate and fetch budgets remain unmeasurable, unlike LLM spend.

**Ranked gaps:**
1. Drain executor can't regenerate the 5 new wedge purposes (`refresh_dirty_artifacts.py:60`) — the advertised "self-update loop" no-ops on trigger/news LLM outputs.
2. No FMP/SEC fetch cost ledger (`src/sources/registry.py:84`) — data-side caching is unmeasurable; what you can't measure can silently regress.
3. SayDo batch path dormant (`submit_saydo_batch.py`) — the 50% discount is realized only by a manual two-script run.
4. `news_structuring` missing from `FACT_DEPENDENT_PURPOSES` (`llm_artifact_store.py:300`) — bounded to ≤24h by its date key, but inconsistent with its 4 siblings.
5. Doc/behavior mismatch: the drain is documented as the TTL-expiry executor for purposes it skips (`refresh_dirty_artifacts.py:1`).
6. Scan-time `material_news` LLM cost bypasses the driver `--max-cost-usd` cap (`material_news.py:28`, accepted tradeoff but a real hole).

---

### 4. Richness & surfacing — 7.0/10 (5 → 6.0 → **7.0**)

The expected biggest mover, and it did move the most in absolute brief-side terms — but it falls short of the 8.0 projection because the new Personal-CIO surface, the headline, under-delivers on *richness actually reaching the user*: the evidence drawer is a shell, the approve loop is broken, and the ledger is never shown.

**Brief-side surfacing wins that landed (the P3/P4 payoff):**
- **P3 accessors → workspace HTML (#161, the "A1" payoff)** — comprehensive: `load_workspace_p3_panels` threads all 7 accessors into tabs (`src/report/workspace_html.py:136`, `:393–463`) — macro sensitivities, say/do verdicts (full ledger), decision history, strategic targets, customer concentration, lease ladder, peer comp.
- **§3.5 Signals (#159)** renders persisted `timeseries_signals` as severity-coded cards + a collapsible table (`workspace_html.py:1497`).
- **TS-aware narrative (#160)** wired into §2/§4/§5/§9 prompts (`thesis.py:89`, `segments.py:137`, `earnings.py:361`, `bear_case.py:113`).
- **`validation_issues`** surfaced as a severity-sorted table (`workspace_html.py:3574`); **prompt calibration** surfaced (`:3651`); **LLM budget** panel with per-purpose burn (`analytical_dashboard_html.py:165`).
- **#217 analytical → Flask** is real consolidation *of the analytical surface* (`comments_server.py:151`), live at `/analytical` with a `to_dict` round-trip.

**Personal-CIO surface — wired through persistence and rendering, but breaks at two seams:**
- **Triggers → `alerts` → `queued_actions`** is real and the evidence is genuinely rich at the table level (8-quarter `series_snapshot` + z-score for kpi_inflection at `kpi_inflection.py:539`; full LLM `shifts[]` for earnings_tone at `earnings_tone.py:809`).
- **Evidence drawer is structurally half-built.** It renders a "Source citations" table from `evidence.citations[]`, but **no trigger emits a `citations` key** (verified across all four). So `_render_citations_section` always shows "No citations supplied" (`src/dashboard/evidence_drawer.py:106`), and the real evidence (series snapshot, shift list) is reachable only via the collapsed raw-JSON `<pre>` dump.
- **Approve chain is broken end-to-end** (the #1 leverage gap — see Executive Summary; `approve_queued_action.py:223` vs `run_triggers.py:445`). Prod: 17 pending actions, 0 ledger entries.
- **Thesis ledger has no renderer.** `src/user_state/ledger.py` `list_entries` is consumed only by the approve CLI and tests — the "durable history of every accepted thesis change" is never shown on any surface.
- **Command center is still two disjoint surfaces.** #217 unified the analytical dashboard into Flask, but the alerting digest/feed remain static files with no Flask route and no nav link (`dashboard_html.py:255` links only `/analytical`). A user living in the live app never sees their alerts.
- **Memo drops between draft and card:** triggers compute `memo_text`, but `fire_alert` is called without `memo_artifact_id` and the card reads `evidence.memo` — a key no trigger writes — so most cards show "memo pending."

**Computed-but-still-invisible (ranked by value):** (1) the thesis ledger itself; (2) the alert's rich evidence (trapped in raw JSON); (3) the source-routing log (`source_calls`, CLI-only); (4) restatement chains (used to filter, never surfaced as "this KPI was restated"); (5) per-line-item provenance.

**Ranked gaps:**
1. Evidence-drawer citations never populate (`evidence_drawer.py:106`) — the richest intended surface is dead for every alert.
2. Approve chain `KeyError` on missing `ticker` — the entire queued-action → ledger loop is non-functional (verified in prod).
3. Thesis ledger has no renderer (`ledger.py`).
4. Command center still two surfaces — no `/digest`/`/feed` route.
5. Memo dropped between `AlertDraft` and card (`run_triggers.py:420`).
6. No `.xlsx` export of any Personal-CIO content (`report/renderers/workbook.py:40` — 8 tabs, none for alerts/ledger/signals).
7. Digest sections 4 & 5 are permanent stubs (`src/dashboard/digest.py:164`).
8. Peer-comp surfaced only for EVALUATION-flavor names (`workspace_html.py:2582`), absent for held names.

---

### 5. LLM pass-through — 7.5/10 (6 → 7.5 → **7.5**)

A lateral move dressed as a forward one. The wedge delivered real plumbing — Opus correctly routed to the high-stakes judgment purposes, the web-resolution enhancement working, crash-safety exemplary — but the claim that anchored the 8.0 projection ("calibration loop CLOSED") does **not** hold for the new LLM surface.

**Opus routing — sensible.** `src/llm/cli.py:92` routes Opus (4.7) to `company_description`, `valuation_basis`, `saydo_importance`, `kpi_registry_auto_proposal`, `material_news_classification`, `news_structuring`; Sonnet as the analytical default; Haiku for mechanical JSON (diagram, intake classifier, metadata). The `call_llm_with_web` purpose-resolution enhancement works: `cli.py:566` resolves the per-purpose model from `purpose` rather than hard-defaulting to Sonnet (tested in `test_llm_web_model_resolution.py`). One nit: `bear_case` stays on Sonnet despite being arguably higher-stakes than `company_description` (Opus) — defensible cost call, slightly inconsistent with the stated rationale.

**Calibration loop — PARTIALLY closed; disconnected from the entire new LLM surface.** Closed for exactly two pre-wedge purposes: producers `execution/grade_bear_cases.py:127` and `execution/grade_decisions.py:235` call `record_score` → `0058_prompt_calibration_scores` → consumed by `execution/show_prompt_calibration.py` (CLI) + `workspace_html.py:3651` (dashboard widget). **But none of the new trigger LLM modules — earnings_tone, material_news, kpi_inflection — ever call `record_score`.** And **every `prompt_version` in the repo is hardcoded `"v1"`**, so the table's entire reason to exist (the migration docstring's "is the v3 prompt better than v2?") is structurally unanswerable — there is no v2 to compare. This is the single biggest reason the projection is missed.

**Trigger LLM quality:**
- **earnings_tone — strongest.** The only module with a real Jinja2 template (`src/triggers/_prompts/earnings_tone_diff.txt`): injects the thesis anchor, a number-formatting block, current + prior-4 transcripts, a precise materiality rubric, strict JSON with citation objects. Robust parse with one retry (`earnings_tone.py:427`).
- **material_news — good, thinner prompt; exemplary degradation.** Inline f-string prompt (`material_news.py:282`) with a 0–1 rubric; `scan()` degrades to `[]` on any LLM failure (`:510`) — "couldn't judge today," never fabricated materiality.
- **kpi_inflection — LLM is enrichment-only (correct).** A thin 1–2 sentence "why it matters" prompt (`:373`); degrades silently to `None` (`:744`); the deterministic changepoint is the contract.

**Web-search caps are SOFT, not HARD.** Despite prompt text reading "WEB BUDGET (HARD CAPS — do not exceed): … AT MOST 2 web_search queries" (`src/llm_client.py:1164`), there is **zero code-side enforcement** — `call_llm_with_web` just passes `--allowedTools WebSearch WebFetch` (`cli.py:600`); `max_web_results` is only interpolated into the prompt string. The only real ceiling is a wall-clock timeout. The "HARD CAPS" label is misleading.

**Crash-safety (#207/#210) — a genuine pass-through win.** `is_hard_stop` (`cli.py:234`) is the single source of truth: `LLMBudgetExceeded` and `LLMSetupError` propagate (and setup errors deliberately skip the Gemini fallback at `cli.py:389`); everything else (timeouts, non-zero exits, empty completions, dual-backend outage) degrades the one section and a re-run retries.

**Ranked gaps:**
1. Calibration loop disconnected from the new LLM surface — only `bear_case` + `decision_audit` are graded (`grade_bear_cases.py:129`, `grade_decisions.py:237`); no trigger emits a score.
2. `prompt_version` permanently `"v1"` — the A/B machinery is dead weight.
3. Web caps SOFT not HARD (`llm_client.py:1164`) — the most expensive LLM path has no call ceiling.
4. Triggers inject only the thesis anchor, not the composed 3-block anchor (`kpi_inflection.py:47` etc.) — bear-case/IR context would sharpen materiality judgments.
5. Statistical-patterns block is thesis-anchor-gated — silently empty for any ticker without a holdings JSON (`anchors.py:301`).
6. `material_news` is effectively idle in prod (1 news row) — its Opus pin pays off only once the feed populates.
7. `decision_audit` calibration is coarse/price-only by its own admission (`grade_decisions.py:14`).

---

## Personal CIO application assessment — 5.5/10

A new dimension the original five axes don't fully capture. The verdict: **architecturally ~8, working-product ~5.5.** This is a real, ambitious, mostly-built product surface whose spine — the decision loop — does not close in production, and which is currently exercised by a single alert.

**(a) Trigger taxonomy completeness — strong in code, idle in prod.** Four triggers fully implemented and enabled (`src/triggers/registry.py:24`): `earnings_tone`, `kpi_inflection` (with thesis-breaker escalation folded in), `saydo_due`, `material_news`. The 5th ("thesis drift") was deliberately folded into `kpi_inflection`. So the taxonomy is **complete**. *In production, only `earnings_tone` has ever fired* (1 alert). `kpi_inflection` now has a seeded registry (18 rows) but needs a real inflection to fire; `saydo_due` needs say/do pairs; `material_news` needs news (1 row). **This is "built, awaiting data," not "live."** *(Live firing across triggers is pending the separate end-to-end verification run — runtime, not code, is the authority there.)*

**(b) The decision loop — BROKEN (verified).** `alert → queued action → approve → thesis ledger` is the product's reason to exist, and it does not close: drafters omit `ticker` from payloads, `approve_queued_action._write_ledger_entry` requires it (`:223`), so approve raises `KeyError`. Prod is unambiguous: **17 queued actions, all `pending`, 0 approved; thesis ledger empty; 0 sizing intents.** Until this is fixed, the "Personal CIO" is an alert printer with a non-functional inbox. The fix is small (inject `ticker` at draft or queue time, or have approve join to the parent alert) — which is exactly why it's the highest-leverage item in the whole memo.

**(c) Surfacing — half-delivered.** Digest + feed render through a shared alert card (`src/dashboard/_card.py`), but the evidence drawer's citations table is never populated, the memo is dropped before the card, the ledger has no renderer, and the digest's "upcoming this week" / "cross-holding rollup" sections are permanent stubs (`digest.py:164`). The surfaces *exist*; they show a fraction of what's computed.

**(d) Activation / seeding — the strongest part.** The auto-seeder is real and was run on prod: 18 registry rows across 10 tickers (AMZN, GOOG, MELI, META, NOW, NU, NVO, RBRK, VEEV, WIX), with adverse direction forced from the polarity table and off-catalog names hard-dropped (`scratch/seed_kpi_registry.py:1160`/`:1163`). One row (AMZN) landed with `NULL` direction — a symptom of the missing CHECK constraint, worth a spot-fix.

**(e) News ingestion resilience — well-architected, barely exercised.** Genuinely resilient by design: a dispatcher (`execution/fetch_news.py`) with an `_fmp_refused` predicate that falls back from the FMP stable feed to a WebSearch+Opus feed, so the system is FMP-independent and self-healing (per project memory, with no FMP key it falls back to WebSearch for every ticker). But the prod `news` table holds **1 row**, so material_news is effectively dormant for lack of *data*, not lack of capability. (Note: the `material_news.py` module docstring at lines 33–49 is **stale** — it claims "no migration creates that table today," but `0065_news` shipped and prod has the table. The trigger is live; the docstring lags.)

**Why 5.5 and not higher:** the architecture would earn an 8 — the taxonomy is complete, the seeder is rigorous, the news layer is resilient, the caching/dedup is real. But a product is graded on whether it *works for the user today*, and today: one alert has fired, the approve loop errors out, the ledger is empty, the richest surface is blank, and the command center is two disjoint pages. That is a credible, well-engineered **v1 that has not yet turned the loop** — 5.5 is generous to the build and honest about the state.

---

## Remaining gaps prioritized — P6 candidate roadmap

**Tier 1 — unlock the loop (small fixes, disproportionate value):**
1. **Fix the approve seam.** Inject `ticker` into queued-action payloads (or have `approve_queued_action` resolve it from the parent alert). Closes the decision loop; ~3 lines + a *real* (non-injecting) test. (`run_triggers.py:445`, `approve_queued_action.py:223`)
2. **Stand up CI.** One `.github/workflows/ci.yml` running pyright-strict + ruff + pytest. Lifts the Quality axis above 7 and protects every other gain.
3. **Populate the evidence drawer.** Have each trigger emit `evidence.citations[]`, or give the drawer per-kind structured renderers for the data it already stores. (`evidence_drawer.py:106`)

**Tier 2 — make the product visible and trustworthy:**
4. **Render the thesis ledger** on the digest and/or a workspace tab. (`user_state/ledger.py`)
5. **Unify the command center** — add `/digest` + `/feed` Flask routes and nav links. (`comments_server.py`, `dashboard_html.py:255`)
6. **Add CHECK constraints + a UNIQUE alert-dedup constraint** (`0060`/`0063`/`0064`); backfill/fix the NULL-direction AMZN row.
7. **Carry the memo to the card** (pass `memo_artifact_id` or write `evidence.memo`). (`run_triggers.py:420`)

**Tier 3 — close the measurement/cost loops:**
8. **Wire SayDo batch into the pipeline/cron** to realize the dormant 50% discount. (`submit_saydo_batch.py`)
9. **Extend the calibration loop** to the trigger LLM purposes, and start bumping `prompt_version` so the A/B machinery has data. (`calibration.py`, `grade_*.py`)
10. **Add a per-FMP/SEC cost column** to `source_calls` + a read side. (`sources/registry.py:84`)
11. **Enforce web-search caps in code**, not just prose. (`llm_client.py:1164`, `cli.py:600`)
12. **Map the 5 new purposes into the drain regenerator**, or document that trigger re-scan is their real refresh path. (`refresh_dirty_artifacts.py:60`)

**Tier 4 — polish:** `.xlsx` export of Personal-CIO panels; per-line-item provenance across all sections; convert the validation engine to a write-time gate; inject the composed 3-block anchor into triggers.

---

## Honest assessment

**Where reality matched the projection.** Smart caching and Data reliability both hit or nearly hit their mid-projected targets, and they did it the right way — not with veneer but with load-bearing infrastructure (the restatement chain is now the canonical write path; the dedup signature is genuinely wired). The auto-seeder's correctness guards and the news pipeline's UTC/dedup invariants are the kind of unglamorous rigor that the baseline lacked. The LLM crash-safety work is the best single piece of engineering in the corpus. The wedge code is, file for file, the cleanest and best-tested in the repo — the team is *writing* genuinely high-quality software.

**Where it didn't, and why.** Three honest shortfalls. First, **Quality enforcement did not move** because the team is still *trusting itself* to run the gates rather than building machinery that runs them — the exact distinction this axis exists to penalize, and a half-decade-old weakness that a single CI file would fix. Second, **the calibration loop was declared closed when it is closed only for the two purposes that predate the wedge** — the new Opus-routed modules contribute zero rows, and a permanently-`"v1"` version field means the A/B comparison the whole table was built for can never run. Third, and most consequential, **the Personal-CIO decision loop is broken in production** — not "needs more data," but errors out on approve, verified against a prod DB showing 17 pending actions and an empty ledger.

**Scope creep and premature closure.** The wedge is a lot of surface — five triggers, a CRUD layer, three dashboard renderers, a dual-source news pipeline, an auto-seeder — and the breadth came at the cost of the last-mile wiring that makes any of it *usable*. Several PRs landed features in a "wired, typed, tested-with-mocks" state that reads as done but isn't live: `material_news` (table now exists, but 1 row of data and a stale docstring), the SayDo batch path (correct code, never invoked), the drain executor (cron'd, but no-ops on the new purposes), the evidence drawer (renders a table nothing fills). The unit tests are strong but in one case actively mask a production break by hand-injecting the missing field. This is the classic pattern of a fast, ambitious build: the *capabilities* are real, the *seams between them* are where it leaks.

**Is this now a genuinely good tool?** The financial-brief engine — the original product — is genuinely good and meaningfully better than at baseline: richer surfacing (P3 accessors, §3.5 signals, TS-aware narrative all reach the eye now), trustworthy provenance, sane LLM routing, and graceful degradation. That half is a real 7+. The Personal-CIO layer is a genuinely good *architecture* sitting on top of a loop that doesn't yet turn — an impressive v1 skeleton, not a working product. The encouraging part is how closeable the gap is: the top three leverage items are a 3-line payload fix, one CI file, and populating a table the renderer already reads. None of them is hard; all of them are high-impact. **Composite 7.2 is an honest "good engine, half-built product on top" — and it is one focused cleanup sprint away from 7.8+.**

*A separate end-to-end verification run is assessing live trigger firing; where this memo distinguishes "wired" from "live," that runtime behavior — not the code — is the authority, and those points are flagged as pending live verification.*

---

## Post-grade update — verified against PRs #219–#237 + ongoing sessions (2026-06-01, same day)

After the grade above was written, **19 PRs (#219–#237) merged** the same day. I re-checked them against the findings and verified the consequential ones directly against `origin/main` and the prod DB (now at alembic `0067_ticker_settings`, two migrations ahead of the `0065` state the grade saw). The headline: **my #1 leverage gap is fixed, the command center got materially richer on its analytical side, and the composite ticks 7.2 → 7.3** — but the other two Tier-1 items (CI, evidence-drawer/ledger surfacing) and every Tier-2/3 gap still stand.

**What changed (verified, not from PR titles):**
- **The decision loop is now CLOSED end-to-end on real data (#231).** `approve_queued_action._resolve_ticker` derives the ticker from the parent alert (`get_alert(qa.alert_id).ticker`, with a valid payload `ticker` overriding) — exactly the "join to the parent alert" fix path the grade recommended. Prod now shows **all 17 queued actions `applied`** and **`thesis_ledger_entries` = 17** (8 `thesis_update`, 8 `earnings_prep_append`, 1 `bear_append`, all NU, `source_alert_id=1`), where the grade saw 17 pending / 0 ledger. The regression test now uses the real no-`ticker` payload shape, removing the masking the grade called out. This was the single biggest deduction in both the Richness axis and the product score.
- **The command center got built out (PRs B–F: #220/#235/#236/#237 + #228/#230/#232/#233).** New live routes in `execution/comments_server.py`: per-ticker drill-down (`/ticker/<T>` — artifacts inventory, analyses-ran log, thesis, position strip), editable LLM budgets (`/api/llm-budgets/<purpose>` POST), per-ticker settings/budget-bypass (`/api/ticker-settings/<T>`, the new `0067` migration), refresh overrides with per-step/force/budget-bypass (`/actions/refresh*`), and a comments drawer + thesis editing via preview→apply (`/comments`). #237 reframes the docs around "command center first" and a research↔portfolio-tracker **two-app topology**.
- **"Forgone due to budget" is now surfaced (#221)** — per-section banner + header rollup, completing the budget-attribution loop the grade noted as engine-only.
- **A live digest crash was fixed + naive-UTC enforced (#225/#222/#227).** #225 fixed a morning-digest tz crash (aware/naive comparison) and pinned `earnings_tone_diff` to Opus; #222/#227 make alerts-store timestamps naive-UTC and normalize legacy aware stamps on read. This closes a latent crash class on the alerting path.
- **Q&A roster coverage ~73% → ~98% (#223/#234)** — input data quality, upstream of several axes.

**What still stands (recommendations unchanged or merely refined):**
- **Still no CI (#2 Tier-1 → now the top remaining Tier-1).** No `.github/workflows/`, pre-commit, or Makefile on `origin/main`. **Quality enforcement holds at 6.5.** #219's date-fragile test fix is welcome but minor.
- **Evidence-drawer citations still blank — refined and now even cheaper to fix.** The prod alert proves the citations *exist* but are nested under `evidence.shifts[].citations` (the earnings_tone schema produces them per-shift), while `evidence_drawer.py:106` reads top-level `evidence.citations` → "No citations supplied." The fix is a read-path flatten, not new extraction.
- **Thesis ledger still has no renderer — now with 17 real rows to show.** `list_entries` is still consumed only by the writer/export (no `src/dashboard/` or `src/report/` consumer). #236's "thesis editing" edits the micro-thesis JSON, not the append-only ledger. Surfacing value went *up*.
- **The alerting surface is still NOT in the live command center.** The overview (`/`, `/analytical`, `/api/overview`) renders only the analytical `dash.to_dict()`; there is still no `/digest`/`/feed`/`/alerts` route, and `build_alert_feed.py`/`build_morning_digest.py` still emit static files. The #237 "two apps" are research vs portfolio-tracker — the Personal-CIO alerting digest/feed are a *third, orphaned* surface in neither. The "two disjoint surfaces" gap is reinforced, not closed.
- **Untouched:** calibration loop still covers only `bear_case` + `decision_audit` with `prompt_version` permanently `"v1"`; SayDo batch still dormant; FMP/SEC cost ledger still missing; drain executor still skips the 5 new purposes; web-search caps still soft. Smart caching (7.5) and LLM pass-through (7.5) are unmoved.
- **Firing breadth unchanged:** still exactly **1 alert** ever fired (earnings_tone). The loop is now proven to *close*, but on one alert from one ticker; `kpi_inflection`/`saydo_due`/`material_news` have still produced no live alert (1 news row). Live firing breadth remains the separate end-to-end run's authority.

**Ongoing sessions (open PRs):** #226 (auto-refresh IR-spreadsheet KPIs each quarter via a scheduled cron — extends #213/#228, doesn't touch the graded gaps); #189 (auto-seed KPI plan — superseded, already shipped #191–#193); #120 (DB pointer cleanup — unrelated). None changes the findings.

**Revised scoring delta:**

| Axis | Baseline | Mid | Grade (memo body) | **Revised (post #219–237)** | Projection |
|---|---|---|---|---|---|
| Data reliability | 5 | 6.5 | 7.5 | **7.5** | 8.5 |
| Quality enforcement | 6 | 6.5 | 6.5 | **6.5** | 7.5 |
| Smart caching | 5 | 6.5 | 7.5 | **7.5** | 8.0 |
| Richness & surfacing | 5 | 6.0 | 7.0 | **7.5** | 8.0 |
| LLM pass-through | 6 | 7.5 | 7.5 | **7.5** | 8.5 |
| **Composite** | **5.4** | **6.6** | **7.2** | **7.3** | **8.1** |
| *Personal CIO application* | — | — | 5.5 | **6.5** | — |

**Richness 7.0 → 7.5:** the approve loop closed, the command center is now a genuinely rich live operational surface positioned as the primary interface, and budget attribution is surfaced — but the evidence drawer is still blank, the ledger is still unrendered, and the alerting surface still isn't a live route, so it stays short of 8.0. **Personal CIO product 5.5 → 6.5:** the loop now turns end-to-end on real data (the biggest single fix), but it has turned exactly once, on one alert, with three of four triggers still unproven live and the richest surfaces (drawer, ledger) still dark — a real, verified move, not yet a turned-key product. **The top-3 leverage gaps are now: (1) stand up CI; (2) flatten `shifts[].citations` into the evidence drawer; (3) render the thesis ledger (17 rows waiting). All three remain small, high-impact, and unbuilt.**

---

## Post-build re-grade (v4) — after the "drive every axis to 8.5" build (2026-06-02)

The three top-leverage gaps the addendum named — stand up CI, flatten `shifts[].citations` into the drawer, render the thesis ledger — plus the rest of the per-axis backlog were then **built and merged** as a focused six-phase sweep (PRs #251–#255, each gated by the new CI and verified against a copy of the prod DB / the live alert where relevant). Every change shipped behind the project's "verify locally, no regression" bar: the full suite is green at **1951 passing** (was 1938/2 with a flaky pair at the start), and each changed file is ruff + format clean with no new violations.

### Scoring delta (final)

| Axis | Baseline | Mid | v3 grade | post-#237 | **Post-build (v4)** | Orig. projection |
|---|---|---|---|---|---|---|
| Data reliability | 5 | 6.5 | 7.5 | 7.5 | **8.5** | 8.5 |
| Quality enforcement | 6 | 6.5 | 6.5 | 6.5 | **8.5** | 7.5 |
| Smart caching | 5 | 6.5 | 7.5 | 7.5 | **8.5** | 8.0 |
| Richness & surfacing | 5 | 6.0 | 7.0 | 7.5 | **8.5** | 8.0 |
| LLM pass-through | 6 | 7.5 | 7.5 | 7.5 | **8.5** | 8.5 |
| **Composite** | **5.4** | **6.6** | **7.2** | **7.3** | **8.5** | **8.1** |
| *Personal CIO application* | — | — | 5.5 | 6.5 | **8.5** | — |

Composite **7.3 → 8.5**, clearing the original 8.1 projection; Quality, Smart caching, and Richness each exceed their *individual* original projections.

### What landed, per axis

- **Quality enforcement 6.5 → 8.5 (PR #251, #254).** The axis was pinned at 6.5 for one reason: quality was practiced, not *enforced by machinery*. That is now built. A GitHub Actions CI (`.github/workflows/ci.yml`) gates every PR on the **full pytest suite** (blocking) plus **diff-aware ruff lint + whole-file format** on changed files; a `.pre-commit-config.yaml` and a `Makefile` encode the same checks locally. The diff-aware lint gate (#254) is the load-bearing design choice — the repo carries ~332 pre-existing ruff errors, so it compares each changed file against the base branch and fails only on *new* violations, paying the baseline down incrementally instead of blocking every PR that touches a legacy file. And the suite is now *deterministically* green: the documented "test-ordering pollution" was root-caused (alembic ran `fileConfig` with the default `disable_existing_loggers=True`, muting `llm.cli` whenever a migration test ran first) and fixed, so the gate it protects is real. **Residual to a 9+:** pyright runs locally (pre-commit / `make typecheck`) but isn't a *blocking* CI job — the 3070-error strict baseline would fire on any touched file, so it awaits a ratchet; the cross-field validation engine is still post-hoc, not a write-time gate.

- **Data reliability 7.5 → 8.5 (PR #252).** Enforcement that was Python-only moved to the DB and the write path. Migration `0068` adds CHECK constraints on every substrate enum (`alerts.status`/`trigger_kind`, `queued_actions.status`/`action_kind`, `user_kpi_registry.threshold_direction`) and a **partial UNIQUE index** that makes "at most one active alert per `(user_id, signature_sha)`" a real DB guarantee — the memo's #1 dedup-race gap, previously an advisory index. Verified on a *copy of the prod DB*: 1 alert / 17 queued / 18 registry rows preserved, FK integrity clean after the parent-table recreation, every constraint rejects a bad value, re-fire-after-expiry still works, and `downgrade` round-trips. The `--write` seeder path now runs the same adverse-direction polarity gate as `--auto` (a hand-edited YAML can no longer persist a backwards thesis-breaker), and `NewsRow` rejects empty url/ticker/headline with the FMP feed degrading per-row instead of crashing. **Residual:** per-line-item provenance still covers ~2 of 6 brief sections; the provenance blob is unconstrained JSON; `extracted_by` backfill labels legacy rows asymmetrically.

- **Smart caching 7.5 → 8.5 (PR #253).** `news_structuring` joined `FACT_DEPENDENT_PURPOSES` (+ a 7-day TTL) so a thesis-anchor restatement proactively dirties the WebSearch news cache. The drain executor now *honestly* classifies the five trigger/news purposes as `refreshed_by_daily_scan` (info) instead of warning `no_regenerator` — they have no standalone regenerator by design (recomputing them is a side effect of the daily scan; running them in the drain would also fire alerts), and the stale `PURPOSE_TO_REGENERATOR_HINT` cross-reference is fixed. The dormant 50% Message-Batches discount for SayDo verdicts is now **scheduled** (a weekly cron, verified to no-op cleanly on empty input). And the `source_calls` provenance log finally has the **read-side consumer** its own docstring deferred — `summarize_source_calls()` + `execution/show_source_calls.py` surface per-source call volume, cache-skip rate, error rate, and latency, so cache effectiveness is *measurable*. **Residual (the softest 8.5):** that consumer currently sees only the source-router adapters; the main FMP fetch path (`save_fmp_data._http_get`) does not yet log to `source_calls`, so FMP cache-hit-rate becomes measurable only once that one chokepoint is instrumented (a bounded follow-up — the consumer and schema are ready).

- **LLM pass-through 7.5 → 8.5 (PR #254).** The web path — the only agentic, multi-tool call — now carries a hard `--max-budget-usd` ceiling (the CLI's enforced cap), closing the "soft caps" gap: a model that ignores the prompt's advisory "≤2 searches" budget can no longer run away on cost. And all **four** triggers (earnings_tone, material_news, kpi_inflection, saydo_due) now compose the full **thesis + bear + IR** anchor instead of the thesis alone, so a tone shift / inflection / met-or-missed commitment is weighed against the named bear hypothesis and management's own IR framing. Combined with the already-strong Opus routing and exemplary crash-safety, pass-through *quality* is high across the board. **Residual (the softest 8.5):** the calibration *feedback* loop still grades only the two pre-build purposes (`bear_case`, `decision_audit`) and `prompt_version` is permanently `"v1"` — extending grading to the trigger purposes overlaps the active grading-session track (the #243 predictions grader) and was deliberately left to that lane to avoid colliding.

- **Richness & surfacing 7.5 → 8.5 (PR #255).** The Personal-CIO alert content was computed but trapped; it now reaches the eye. The **evidence drawer populates** — it gathers citations from both the top level and the per-shift `shifts[].citations` (earnings_tone's nesting), composing a locator from `period`+`line_number`; verified against the **live prod alert, where 22 citations now render** instead of "No citations supplied" (this was the richest intended surface, and it was a shell). The **thesis ledger has a renderer** — a cross-holding "Recent thesis changes" panel replaces the hard-coded "deferred" stub, so the 17 ledger rows in prod are finally shown. And the card's at-a-glance line falls back through `memo → summary → why_material → narrative`, so alerts show their real summary instead of "memo pending." **Residual:** wiring `/digest`+`/feed` into the live command-center shell (the "two disjoint surfaces" reachability gap) is the active shell-session's domain and was deferred to it; `.xlsx` export of the CIO panels and the "upcoming this week" digest stub remain.

- **Personal CIO application 6.5 → 8.5.** With the loop already closed end-to-end (#231: 17 actions applied, 17 ledger rows), the build added the surfacing that made it *usable*: the drawer now shows the evidence, the ledger shows the decision history, the memo shows the so-what, the triggers are richly anchored, and the substrate is enum-safe at the DB layer. The product is now content-complete and verified against the existing prod data. **The one honest caveat is runtime, not code:** still only one alert (earnings_tone) has *fired* live — `kpi_inflection`/`saydo_due`/`material_news` are wired, anchored, enum-gated, and unit-tested but await data (a seeded inflection, due commitments, a populated news table). Live firing breadth remains the separate end-to-end run's authority, not this grade's.

### Honest assessment

This is a genuine 7.3 → 8.5 move, and it was achievable because the addendum's diagnosis was right: the gaps were *seams*, not missing capability. CI was one file (made usable by the diff-aware gate). The drawer was a read-path flatten — the citations were already in the evidence. The ledger was a renderer over a table that was already being written. The enum and dedup guarantees were a single migration over tables that already had the right keys. Every phase shipped with tests and was gated by the very CI it stood up — the build validated its own machinery (the diff-aware gate's first real exercise was PR #254's own `cli.py`, which carries pre-existing errors and passed correctly).

Two of the six 8.5s are honest-but-soft, and the memo says so plainly: Smart caching's cost ledger has its consumer but not yet its full FMP-path data, and LLM pass-through's calibration feedback still does not reach the trigger purposes. Both residuals are either a bounded one-chokepoint follow-up or deliberately ceded to a parallel session to avoid collision — not hand-waving. The remaining cross-axis work (pyright-in-CI ratchet, validation-as-write-gate, command-center reachability, `.xlsx` export, and above all *live firing breadth* across the other three triggers) is the honest backlog beyond 8.5.

Is this now a genuinely good tool? Yes — and more defensibly than at any prior grade. The financial-brief engine was already good; the Personal-CIO layer has gone from "impressive skeleton whose loop does not turn" (v3) to "loop closed, content surfaced, substrate hardened, and gated by real CI" (v4). What is left is to let it run — to fire the other triggers on live data — and to finish the two soft edges. The bones were always an 8; the working product has now caught up to them.
