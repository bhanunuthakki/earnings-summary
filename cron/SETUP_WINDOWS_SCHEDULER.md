# Setting up the earnings-summary crons on Windows Task Scheduler

This is the one-time wiring for the scheduled tasks defined in this folder.
All crons run as `InteractiveToken` under `%USERNAME%`, log to
`.tmp/cron_logs/<task>_<TS>.log`, and are registered under the
`\earnings-summary\` namespace so they show up grouped in the Task Scheduler
GUI.

## Active crons

29 scheduled tasks total — the authoritative set is the `cron/*.task.xml` files on disk; run `python execution/verify_cron_registration.py` to see which are actually registered vs. present on disk. A monthly disaster-recovery drill (15th, 09:00) restores the latest backup to a throwaway path and verifies it (see **Disaster-recovery drill** below). The five daily data-chain tasks run in sequence (03:00 → 06:30); a sixth daily task drains the LLM artifact queue at 04:00 and a seventh runs the Personal CIO morning pipeline at 04:00 (news → triggers → feed → validation); an eighth (02:45) backs up the database before the chain starts; the hourly catch-up is independent; the weekly + monthly tasks run off-cycle and refresh the synthesis / lens layer, the IR-spreadsheet KPI series, and the IR-document corpus — including a twice-weekly rescan of the names whose IR crawl is still failing (a bot-protected site that may start cooperating); a quarterly task mines the rostered 13F managers one to two days after each 13F filing deadline; and a weekly Thursday audit verifies every task XML is registered and on schedule. Beyond those, several standalone + analytics tasks run off the chain — tabled under **Additional standalone + analytics tasks** below: a daily macro-series fetch (05:35) and podcast-RSS poller (06:30); the weekly model-eval sweep (Sun 02:00), calibration grading (Sun 03:30), stance scoring (Sun 06:30), and cross-source validation (Sun 03:00); and two monthly analytics tasks — advisor memos (1st, 07:30) and the calibration scorecard (2nd, 07:30).

### Pre-chain backup

| Task name | Cadence | XML | Wrapper | What it does |
|---|---|---|---|---|
| `earnings-summary\backup_db` | Daily 02:45 | `backup_db.task.xml` | `run_backup_db.bat` | **SQLite online backup.** Runs `cron/backup_db.py`, which copies `data/portfolio.db` to `ES_DB_BACKUP_DIR` (default: `%USERPROFILE%\My Drive\earnings-summary-db-backups`) as a gzip-compressed snapshot via SQLite's online-backup API. Fires 15 minutes before the 03:00 refresh chain so a consistent snapshot exists before the day's writes begin. Keeps the most recent `ES_DB_BACKUP_RETAIN` snapshots (default 14). |

### Daily chain (P1 tier — portfolio refreshed every day)

| Task name | Cadence | XML | Wrapper | What it does |
|---|---|---|---|---|
| `earnings-summary\refresh_cache` | Daily 03:00 | `refresh_cache.task.xml` | `run_refresh_cache.bat` | **Tier-aware FMP refresh queue.** Reads `FMP_TIER` from `.env` (defaults to `basic` = 250/day) and drains the highest-priority stale endpoints up to the cap. Failed endpoints (403 / Legacy Endpoint) get a 30-day retry window so a downgrade builds a backlog automatically; an upgrade catches up across following days. See `## Switching FMP tier` below. |
| `earnings-summary\refresh_dirty_artifacts` | Daily 04:00 | `refresh_dirty_artifacts.task.xml` | `run_refresh_dirty_artifacts.bat` | **LLM artifact cache drain.** Picks up every `llm_artifacts` row with `dirty=1` (upstream-fact trigger) or `expires_at < now` (per-purpose TTL elapsed — see `_DEFAULT_TTL_DAYS` in `src/llm_artifact_store.py`). For each (ticker, purpose), shells out to the regenerator script defined in `execution/refresh_dirty_artifacts._PURPOSE_TO_REGENERATOR`. Halts gracefully (exit 0) once accumulated `llm_calls.cost_estimate_usd` for this run reaches `--max-cost-usd 5`. |
| `earnings-summary\run_morning_pipeline` | Daily 04:00 | `run_morning_pipeline.task.xml` | `run_morning_pipeline.bat` | **Personal CIO morning pipeline.** One orchestrated run chaining four subprocess stages: (0) `fetch_news.py` ingests fresh per-ticker news for the material_news trigger; (1) `run_triggers.py` fans registered triggers across the portfolio + watchlist + evaluation list, persisting fresh alerts + drafted actions (cost-capped at `--max-cost-usd 10`); (2) `build_alert_feed.py` rebuilds the chronological feed HTML (`data/dashboard/feed.html`); (3) `run_validation_engine.py --gate` runs the population-level data checks. Never aborts early — a trigger-stage failure/timeout still rebuilds the read-only feed over existing alerts. Process exit code = count of failed stages. **Supersedes the standalone `run_triggers` cron from PR #172**; the morning-digest render stage retired with the `/digest` page (2026-06-11) — the live Home rail serves that view straight from the DB. |
| `earnings-summary\scan_ir_transcripts` | Daily 04:15 | `scan_ir_transcripts.task.xml` | `run_scan_ir_transcripts.bat` | **Post-earnings IR-transcript scan.** For each active-universe ticker within 14 days of its last earnings date (`sources.earnings_calendar.last_earnings_date`), re-checks the issuer's OWN IR site (the `issuer_ir` source — `ir_pipeline.transcript`) for the latest reported quarter's transcript and fetches + ingests it. Idempotent on `transcripts/processed/<T>_Q<n>_<Y>.txt` — stops once that quarter is ingested. A tighter, windowed companion to `backfill_transcripts` that catches the issuer's official transcript days-to-weeks before the aggregators index it. |
| `earnings-summary\backfill_transcripts` | Daily 04:30 | `backfill_transcripts.task.xml` | `run_backfill_transcripts.bat` | For every active-universe ticker (`db.ACTIVE_LIST_TYPES`), fetches the last 6 fiscal quarters of Q&A from the free aggregator chain, runs ingest, extracts commitments. Idempotent — re-running with no missing quarters is a no-op. |
| `earnings-summary\fetch_fmp_earnings_calendar` | Daily 05:45 | `fetch_fmp_earnings_calendar.task.xml` | `run_fetch_fmp_earnings_calendar.bat` | Two steps. (1) Refreshes `data/historical/fmp/<TICKER>_earnings_calendar.json` for every portfolio + watchlist + evaluation ticker — on free/basic tier FMP refuses (402 since 2026-06-10) and the cache stays at its last good state. (2) Runs `execution/refresh_expected_earnings.py`, which materializes the **canonical `expected_earnings` table** via `next_earnings_date` (FMP cache → yfinance fallback); the Home rail's upcoming-earnings strip, the Home cockpit, and the portfolio-tracker's read-only bridge all read that table. Step 2 runs even when step 1 fails. |
| `earnings-summary\backfill_earnings_surprises` | Daily 06:15 | `backfill_earnings_surprises.task.xml` | `run_backfill_earnings_surprises.bat` | For every active-universe ticker, merges `<TICKER>_earnings_calendar.json` (FMP primary, full EPS + Revenue surprise) with `yfinance.Ticker.earnings_dates` (fallback, EPS-only) into `data/surprise/<TICKER>_surprises.json`, then upserts into `earnings_surprises`. Idempotent. Revenue surprise degrades to NULL when FMP coverage lapses. |
| `earnings-summary\daily_fetch_and_brief` | Daily 06:30 | `daily_fetch_and_brief.task.xml` | `run_daily_fetch_and_brief.bat` | Drains `tracked_companies.brief_dirty` with three gates: **A** tier cadence (P1 daily, P2 if >7d old, P3 if >30d old), **B** material-change hash (skip if content unchanged AND last build < 7d), **C** evaluation cadence (skip if list_type=evaluation AND last build < 7d). For un-skipped tickers, runs thesis evaluator + DCF refresh + brief regen with `--enable-llm` so §8/§9 populate via the Claude CLI (Gemini fallback). |

The daily fetch/brief crons run as a chain: refresh_cache (03:00) drains the
FMP priority queue under the configured tier, scan_ir_transcripts (04:15)
catches the latest issuer-published transcript for any ticker inside its 14-day
post-earnings window, backfill_transcripts (04:30) pulls
fresh Q&A transcripts + commitments, fetch_fmp_earnings_calendar (05:45)
refreshes the calendar JSON cache + the canonical `expected_earnings` table,
backfill_earnings_surprises (06:15) writes
the merged EPS/Revenue beat-rate cache + DB, and daily_fetch_and_brief (06:30)
drains `brief_dirty=1` and regenerates briefs (gated by content-change + eval
cadence). The staggered gaps absorb slow aggregator/FMP responses and
let each step's writes commit before the next reads.

### Hourly catch-up

| Task name | Cadence | XML | Wrapper | What it does |
|---|---|---|---|---|
| `earnings-summary\onboard_pending` | Hourly at :17 | `onboard_pending_tickers.task.xml` | `run_onboard_pending.bat` | Catches up tickers that bypassed `db.track_company`'s auto-onboard hook (raw SQL / external API inserts). Idempotent — no-op when nothing is pending. |

### Weekly + monthly synthesis layer

These regenerate the LLM "lens" artifacts (`five_min_reread`, `thesis_drift_qoq`, `bull_case`, `mgmt_credibility_score`, `cross_portfolio_synthesis`, etc.) that the analytical dashboard and per-section briefs consume. Tier-cadence rule: **P1 = daily, P2 = weekly, P3 = monthly.** The daily chain handles P1; these three tasks handle P2 + P3.

| Task name | Cadence | XML | Wrapper | What it does |
|---|---|---|---|---|
| `earnings-summary\weekly_p2_lens_refresh` | Weekly, Sunday 02:00 | `weekly_p2_lens_refresh.task.xml` | `run_weekly_p2_lens_refresh.bat` | Regenerates P2-tier (watchlist + evaluation) lens artifacts drifted past their cadence. Wraps `python execution/run_due_lenses.py --cadence weekly`. Idempotent via `artifact_store.cached_inputs` hash dedup — stable tickers cost nothing. |
| `earnings-summary\weekly_synthesis` | Weekly, Sunday 23:00 | `weekly_synthesis.task.xml` | `run_weekly_synthesis.bat` | **The "Sunday-night portfolio review" pipeline.** Four steps in order: (1) `refresh_dirty_artifacts.py --manifest-only` drains the LLM-artifact dirty queue so lens reads see fresh facts; (2) `run_lens.py --tickers AMZN,BN,GOOG,MELI,META,NOW,NU,NVO,RBRK,VEEV,WIX --all` regenerates every per-ticker lens for the full portfolio; (3) `run_lens.py --lens cross_portfolio_synthesis` runs the Opus cross-portfolio convergence read (~$0.25); (4) `build_analytical_dashboard.py` rebuilds `output/dashboard/<DATE>_portfolio_dashboard.html` with the new artifacts. Sequential — any step's failure halts the rest. **Bear-case grading was removed from this task** (#675) — it is owned by the dedicated weekly `grade_calibration` cron (Sun 03:30); grading is idempotent so a single weekly pass suffices. |
| `earnings-summary\monthly_p3_refresh` | Monthly, 1st @ 03:00 | `monthly_p3_refresh.task.xml` | `run_monthly_p3_refresh.bat` | Regenerates P3-tier (index_member / etf / `none`) lens artifacts drifted past their 90-day cadence. Wraps `python execution/run_due_lenses.py --cadence monthly`. The P3 lens set is minimal (`five_min_reread` only). LLM cost stays near zero because `five_min_reread.build_context` returns `None` for a ticker with no DCF / earnings summary / insider data — so although the plan iterates the full P3 universe, only the handful of P3 names that actually carry data reach an LLM call. **Single-user note:** the P3/index-member universe is a vestige of the broader-coverage era; for one user (who follows ~P1+P2 names) consider whether this monthly sweep earns its keep vs. on-demand generation (see the audit's open scope question). |
| `earnings-summary\submit_saydo_batch` | Weekly, Saturday 02:00 | `submit_saydo_batch.task.xml` | `run_submit_saydo_batch.bat` | **SayDo verdicts via the Anthropic Message Batches API (50% off-hours discount).** Two steps: (1) `build_saydo_pairs.py --all --prepare-batch` writes a JSONL of management-commitment (say, do) verdict requests whose check-date has arrived; (2) `submit_saydo_batch.py` submits it, polls until the batch ends, writes each verdict, and ledgers results at the 50% batch rate. No-op when nothing is due; a pre-flight cost gate hard-halts if the projection would breach the `pairwise_analysis` cap. Own task (6h `ExecutionTimeLimit`) because the batch poll can outlast `weekly_synthesis`'s 2h cap. |
| `earnings-summary\red_team` | Weekly Saturday 10:00 (self-gated to the month's FIRST Saturday) | `red_team.task.xml` | `run_red_team.bat` | **Monthly First-Saturday adversarial review** (`directives/monthly_red_team.md` Phase 2). Runs `execution/run_red_team.py`, which generates one rotating-lens adversarial attack per held name (weight > 0.5%, cash-likes/index-ETFs excluded) plus the three cross-book passes (factor-block detection, style drift, human-capital overlay), persists them to `red_team_items`, and writes a brief snapshot to `.tmp/red_team_briefs/`. Windows Task Scheduler has no native "Nth weekday of month" trigger, so the XML fires every Saturday and the script itself no-ops (exit 0, logged `red_team_skipped_not_first_saturday`) unless today is the month's first Saturday. Idempotent on `red_team_{YYYY_MM}` — a re-run of an already-generated month is a no-op unless `--force`. Per-item degrade (a transient LLM failure defers that one item and retries next run); a hard stop (budget cap / missing CLI) halts loudly. Daytime weekend slot, clear of every protected window in `directives/llm_quota_scheduling.md`. |

The two weekly tasks deliberately bracket the trading week: `weekly_p2_lens_refresh` runs Sunday 02:00 (early) so any P2-tier reads are fresh before the analyst checks in, then `weekly_synthesis` runs Sunday 23:00 (late) so the portfolio dashboard reflects everything that landed during the week, ahead of Monday open. They don't depend on each other — `weekly_synthesis` step 1 (`refresh_dirty_artifacts`) is what guarantees current data, not the earlier weekly run.

### IR-spreadsheet KPI refresh (weekly)

| Task name | Cadence | XML | Wrapper | What it does |
|---|---|---|---|---|
| `earnings-summary\refresh_ir_kpis` | Weekly, Sunday 01:00 | `refresh_ir_kpis.task.xml` | `run_refresh_ir_kpis.bat` | **IR-spreadsheet KPI refresh.** Runs `execution/refresh_ir_kpis_all.py`, which loops every ticker with a parser config (`micro_thesis/ir_config/<T>.json`, enumerated via `ir_pipeline.config.configured_tickers`) and, per ticker, shells out to `refresh_ir_kpis.py --ticker <T> --discover`: headless-renders the issuer's IR results-center, downloads the current historical-data spreadsheet, parses it, and ingests the KPI series at IR_DOC tier — superseding the lower-tier LLM brief values the report charts read. Subprocess-isolated per ticker with a 5-min cap; never aborts on one ticker's failure; exit code = count of failed tickers. Idempotent: an unchanged spreadsheet re-ingests as a no-op (the document is sha256-keyed), so the weekly poll simply catches each new quarter's file within a week of publication. Tickers without a config are skipped (a ticker's first refresh must run `refresh_ir_kpis.py --url`/`--file` to generate its config). **Requires the optional `ir` extra** in the task's Python — see Prerequisites. |

Runs Sunday 01:00 — ahead of `weekly_p2_lens_refresh` (02:00) and the Sunday-night `weekly_synthesis` (23:00) — so the freshly-ingested issuer KPIs are in `kpi_facts` before any lens/synthesis read. To change the cadence (e.g. monthly), edit the trigger in `refresh_ir_kpis.task.xml`; the script is cadence-agnostic and idempotent either way. Today only `NU` has a config, so the run is a single ticker; it scales automatically as more configs are generated.

### Cron-registration audit (weekly)

| Task name | Cadence | XML | Wrapper | What it does |
|---|---|---|---|---|
| `earnings-summary\verify_cron` | Weekly, Thursday 07:00 | `verify_cron.task.xml` | `run_verify_cron.bat` | **Cron self-audit.** Runs `execution/verify_cron_registration.py`, which reads every `cron/*.task.xml`, extracts the registered task name + trigger time, then queries `schtasks /query /fo csv` for the live state. Reports three problem categories: **missing** (XML exists but task not registered), **disabled** (registered but Status ≠ Ready/Running), **mismatch** (scheduled time differs from XML). Exit 0 = all OK, 1 = at least one problem, 2 = schtasks unreachable. Fires Thursday morning so any drift from the weekend's task-scheduler maintenance or a failed install surfaces before the next weekly synthesis run. Output in `.tmp/cron_logs/verify_cron_*.log`. |

### IR-document discovery + fetch (weekly)

| Task name | Cadence | XML | Wrapper | What it does |
|---|---|---|---|---|
| `earnings-summary\discover_ir_documents` | Weekly, Sunday 01:30 | `discover_ir_documents.task.xml` | `run_discover_ir_documents.bat` | **IR-document corpus refresh.** Runs `execution/discover_ir_documents_all.py`, which reads the active-universe roster from the DB (`tracked_companies.list_type` in `portfolio`/`evaluation`) and, per ticker, shells out to two subprocess-isolated stages: (1) `discover_ir_documents.py --ticker <T>` headless-crawls the issuer's IR site (curated override → `ir_config` → `tracked_companies.ir_url`) and writes its URL manifest (`.tmp/ir_url_manifest/<T>_urls.json`); (2) `fetch_ir_documents.py --ticker <T> --categorize --calendar <id>` downloads the manifest's documents into staging and registers them at the canonical path (`documents` table, `source_type='ir_doc'` + the ir_narrative-visible layout). **Roster is read at run time, so newly-added evaluation companies are auto-included.** Subprocess-isolated per ticker with per-stage timeouts; never aborts on one ticker's failure; a ticker with no resolvable IR URL is `SKIPPED` (not a failure); exit code = count of FAILED tickers. Idempotent: a URL already in `documents.source_url` is skipped, and the append-only manifest accumulates history across weekly runs (mz JS-widget sites like NU expose only the current quarter, so history builds up over time). **Requires the optional `ir` extra** (headless browser) — same as `refresh_ir_kpis`. |

Runs Sunday 01:30 — just after `refresh_ir_kpis` (01:00) and before `weekly_p2_lens_refresh` (02:00) — so freshly-registered IR documents (and their `ir_narrative` anchors) precede the lens/synthesis reads. To change the cadence, edit the trigger in `discover_ir_documents.task.xml`. The same per-ticker chain also runs best-effort on onboard (`execution/onboard_ticker.py`, `--skip-ir` to disable) via the shared `run_ticker` entry, so a newly-tracked name gets day-one coverage — discover → fetch+register → `ir_narrative` anchor + `brief_dirty` (so it flows into the next `--enable-llm` brief) → recorded in `ir_fetch_status` — and the weekly run keeps it current.

| Task name | Cadence | XML | Wrapper | What it does |
|---|---|---|---|---|
| `earnings-summary\discover_ir_failing` | Twice weekly, Wed + Sat 02:30 | `discover_ir_failing.task.xml` | `run_discover_ir_failing.bat` | **Failing-crawler rescan.** Runs `execution/discover_ir_documents_all.py --only-failing`, which reads the live document store (`documents.source_type='ir_doc'`), computes the portfolio/evaluation names with **zero** registered IR docs, and runs just those through the normal discover → fetch+register → process chain. Catches a previously bot-protected (HTTP 403), HTTP/2-broken, or load-stalled IR site that starts cooperating — days sooner than the Sunday full sweep, at a fraction of the cost (only the gaps). A name that succeeds drops out of the gap set; one that keeps failing stays surfaced in the dashboard's **IR Docs** coverage tab with its last crawl reason. Idempotent; never aborts on one ticker's failure; exit code = count of FAILED tickers. **Requires the optional `ir` extra** (headless browser). |

The failing-only rescan is the cheap mid-week companion to the Sunday full sweep: the full sweep re-checks every name weekly, this re-checks only the still-failing ones on Wednesday + Saturday so a recovered site is picked up within ~3 days. Coverage + each name's last crawl outcome are visible in the command center's **IR Docs** tab (`GET /api/panel/ir_coverage`), which also shows where to drop a manually-pulled file for the names that stay blocked.

### Quarterly 13F miner

| Task name | Cadence | XML | Wrapper | What it does |
|---|---|---|---|---|
| `earnings-summary\fetch_13f` | Quarterly, 16th of Feb/May/Aug/Nov @ 08:15 | `fetch_13f.task.xml` | `run_fetch_13f.bat` | **EDGAR 13F-HR miner (S6 discovery, investor lane).** Fires 1–2 days after the 45-day 13F filing deadline (Feb 14 / May 15 / Aug 14 / Nov 14) so the quarter's filings are in. Three steps: (1) `execution/fetch_13f.py` polls every active rostered manager (`discovery_sources` rows **with a `cik`**), diffs its two latest `13F-HR` filings, and writes `investor_13f` `discovery_signals` for untracked universe names + `news` rows (`source_feed='edgar_13f'`) for tracked names — a full-class replace, so a sold position drops; (2) `execution/recalibrate_investor_weights.py` snapshots this quarter's new buys into the `investor_calibration` ledger (alembic 0100), measures any whose 180-day horizon has elapsed against the FMP price cache, and nudges each fund's `base_weight` by its realized win/loss hit-rate (EWMA-damped + bounded; a no-op until ~2 quarters of buys clear the horizon); (3) `execution/run_discovery.py` re-scores the queue so the fresh investor signals + tuned weights re-rank immediately (the clamp + corroboration math runs there). Best-effort — an unreachable manager / unparseable filing contributes nothing; a roster with no CIK-resolved managers is a no-op. Only managers whose CIK is set fire; resolve more with `python execution/resolve_manager_ciks.py --apply` (it auto-applies only a single recent, strong-name 13F-HR match and reports the ambiguous/stale ones for the owner to paste via the Sources panel). |

13F is quarterly with a 45-day lag, so a quarterly poll is ample; the miner uses the two latest filings, so a manager that files a few days late is picked up the next quarter. Verify a run via the JSON summary in `.tmp/cron_logs/fetch_13f_*.log` (`{ "signals": N, "untracked_names": N, "tracked_news": N, "managers": N }`) and new `investor_13f` rows in `discovery_signals` / re-ranked rows in the Discovery panel.

### Additional standalone + analytics tasks

These run off the daily chain. They were present on disk but previously undocumented here; this table closes that gap (and they are now in the Install list below).

| Task name | Cadence | XML | Wrapper | What it does |
|---|---|---|---|---|
| `earnings-summary\fetch_macro_series` | Daily 05:35 | `fetch_macro_series.task.xml` | `run_fetch_macro_series.bat` | Populates `macro_series` (12-series FMP fetch, ~25 calls) then recomputes portfolio `macro_sensitivities` (local CPU) — the substrate of the next-dollar panel's macro tilt (`directives/next_dollar_model.md`). Scheduled right after the 00:00-UTC FMP quota reset and **before** the calendar/brief tasks burn the day's budget. |
| `earnings-summary\fetch_podcast_rss` | Daily 06:30 | `fetch_podcast_rss.task.xml` | `run_fetch_podcast_rss.bat` | DIET-lane podcast poller (S11): polls 7 curated shows, matches episodes to tracked names + rostered investors → `media_appearance` signals (idempotent); then a Sonnet pass summarizes new/short episodes into a 2–4 sentence investment briefing, budget-capped at $5/mo (migration 0103). |
| `earnings-summary\model_eval_sweep` | Weekly, Sunday 02:00 | `model_eval_sweep.task.xml` | `run_weekly_model_eval.bat` | The activated model-downgrade eval loop (`directives/model_eval_loop.md`): harvest a rotating 2-ticker sample → sweep every cheaper candidate per active purpose → auto-switch a `model_pin_overrides` row after 3 consecutive SWITCH_DOWN verdicts (revert after 3 KEEP_INCUMBENT). Conservative + reversible; every decision audited via `model_eval_verdicts` + alerts. (The task runs `run_weekly_model_eval.bat`; the older `run_model_eval_sweep.bat` is unused.) |
| `earnings-summary\grade_calibration` | Weekly, Sunday 03:30 | `grade_calibration.task.xml` | `run_grade_calibration.bat` | Feeds the prompt-calibration loop: `run_calibration_grading.py` runs 3 outcome graders (predictions → decisions → bear_cases, the last over `--all-portfolio`) + 5 eval-audit rungs (bear_case, transcript_summary, advisor_next_dollar, ask_advisory_answer, calibration_coach), writing `prompt_calibration_scores`. Never aborts early; exit code = count of failed rungs. **Owns bear-case grading** (moved here from `weekly_synthesis` in #675). |
| `earnings-summary\weekly_validation` | Weekly, Sunday 03:00 | `weekly_validation.task.xml` | `run_weekly_validation.bat` | **Confidence backfill.** Rescores `financial_facts`/`kpi_facts` confidence `--apply`, folding fresh validation-issue penalties into per-fact scores (idempotent). The validation-engine SCAN runs DAILY in `run_morning_pipeline` (stage 3), so it is **not** repeated here (#679 dropped the duplicate weekly scan — the backfill reads the issues the daily run already inserted). Recorded in `ingestion_runs` for the cron-health panel. |
| `earnings-summary\weekly_score_stances` | Weekly, Sunday 06:30 | `weekly_score_stances.task.xml` | `run_weekly_score_stances.bat` | Grades matured advisor memos/stances (master build P2.5): Socratic stances vs price (SPY-relative via the tracker) + swap checks vs realized pair margin. Deferrals (immature horizons / price-cache gaps) are normal; idempotent over the pending set. |
| `earnings-summary\monthly_advisor_memos` | Monthly, 1st @ 07:30 | `monthly_advisor_memos.task.xml` | `run_monthly_advisor_memos.bat` | Advisor memo run (master build P2.3): the next-dollar allocation memo + swap-discipline checks. Runs after the morning pipeline so FMP prices/DCFs are fresh; degrades gracefully if the tracker is offline. Each memo persists to `advisor_memos` + an analyst note (+ ledger entry when ticker-scoped). |
| `earnings-summary\monthly_calibration_scorecard` | Monthly, 2nd @ 07:30 | `monthly_calibration_scorecard.task.xml` | `run_monthly_calibration_scorecard.bat` | Calibration scorecard (close_the_loops L8): the compounding loop's personal readout — deterministic hit-rate/skill split + eval-gated coach prose (named biases + a falsifiable experiment), grounded only in the owner's graded history. Below the min-n floor it renders the substrate with a "too thin to coach yet" note and makes no LLM call. Persists to `data/calibration_scorecard/<period>.json`. |

### Disaster-recovery drill

The scheduled counterpart to the daily `backup_db` — a backup you have never restored is not a backup.

| Task name | Cadence | XML | Wrapper | What it does |
|---|---|---|---|---|
| `earnings-summary\restore_drill` | Monthly, 15th @ 09:00 | `restore_drill.task.xml` | `run_restore_drill.bat` | **DB restore drill.** Runs `execution/restore_drill.py`, which restores the **latest** `backup_db` snapshot to a throwaway temp path and verifies it: gunzip + `PRAGMA integrity_check` (via `cron/restore_db.py`), a core-table row-count sanity check (`tracked_companies`, `financial_facts` non-empty — catches a truncated snapshot that still passes integrity), and a soft schema-version match against the live DB (a mismatch warns; a migration after the last backup is benign). **Never touches the live DB** except to record one `ingestion_runs` row (directive=`restore_drill`) so the cron-health panel shows the drill verdict + clean-streak alongside `backup_db`. Exit 0 = passed, 1 = a hard check failed, 2 = no snapshot found. The CI tests (`tests/test_backup_restore.py`, `tests/test_restore_drill.py`) only exercise synthetic DBs, so this drill is what catches real-world rot (Drive-sync truncation, gzip corruption, a stale snapshot) on the actual `.gz` files. |

### Re-registering after a schedule change

Editing a `*.task.xml` only changes the repo definition — the **live** Windows task keeps its old trigger until you re-import it. After changing a schedule, re-run its `schtasks /create /f` (overwrite) so the registered trigger matches the XML; otherwise `verify_cron` reports a SCHEDULE MISMATCH. The 2026-06 stagger pass moved three jobs to de-conflict same-minute LLM work — re-register these three if they were already installed:

```cmd
schtasks /create /f /tn "earnings-summary\refresh_dirty_artifacts" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\refresh_dirty_artifacts.task.xml"   REM 04:00 -> 05:00

schtasks /create /f /tn "earnings-summary\fetch_podcast_rss" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\fetch_podcast_rss.task.xml"          REM 06:30 -> 07:15

schtasks /create /f /tn "earnings-summary\model_eval_sweep" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\model_eval_sweep.task.xml"           REM Sun 02:00 -> Sat 20:00
```

## Switching FMP tier

The system reads `FMP_TIER` from `.env` (or `--tier` CLI flag). Tiers:

| Tier | Daily cap | Rate | Use when |
|---|---|---|---|
| `basic` | 250 calls/day | 4/sec | FMP free tier (no paid subscription) |
| `starter` | unlimited | 5/sec | Legacy Starter subscription |
| `premium` | unlimited | 12/sec | Premium / Ultimate subscription |

**When you downgrade FMP** (cancel paid sub → fall to free `basic`):

1. Add `FMP_TIER=basic` to `.env` (or change existing value)
2. The `refresh_cache` cron at 03:00 will automatically drain only the top
   250 highest-priority endpoints per day. Tier-restricted endpoints get a
   30-day retry cooldown so they don't burn budget hammering 403s.
3. The other crons (`fetch_fmp_earnings_calendar`, `backfill_earnings_surprises`,
   `onboard_pending`) will keep firing and generating 403 log noise. That's
   intentional — they degrade gracefully (yfinance fallback in surprises +
   earnings calendar; no new onboards is the right behavior).
4. `daily_fetch_and_brief`'s gates B + C prevent redundant LLM-section
   regeneration when no new data lands, so brief-side costs stay bounded.

**When you upgrade FMP** (resubscribe):

1. Set `FMP_TIER=premium` in `.env` (or `starter`).
2. The 30-day retry window on previously-403'd endpoints expires naturally —
   the cacher's `failed_retry_ok` bucket picks them up over the following
   days. To force immediate catch-up, run once with `--force`:
   ```cmd
   python execution\refresh_cache.py run --tier premium --force
   ```

## Source-call provenance log

Every external source call (FMP, yfinance, SEC XBRL) writes one row to
`source_calls` with `source_name`, `kind`, `ticker`, `called_at`, `status`,
`latency_ms`. This is a write-many / read-rarely log today — the intent is
to inform future intelligent routing (e.g. "yfinance is unreachable for
foreign ADRs since 2026-05-01, prefer FMP cache").

To audit recent calls:

```sql
SELECT source_name, kind, status, COUNT(*)
FROM source_calls
WHERE called_at >= date('now', '-7 days')
GROUP BY source_name, kind, status
ORDER BY COUNT(*) DESC;
```

## Prerequisites

- `python` on PATH and resolves to a Python 3.11+ install with the project's
  `requirements.txt` packages installed (incl. `yfinance` for free-tier fallbacks).
- `.env` next to `pyproject.toml` containing `FMP_API_KEY=...` and optionally
  `FMP_TIER=premium|starter|basic` (defaults to `basic` if unset — set
  `FMP_TIER=premium` when you have a paid sub to unlock the full rate).
- The repo cloned at `%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary`
  — or any path you set in `PROJECT_ROOT` at the top of each `.bat`.
- Claude Code CLI on PATH and authed (only required by `daily_fetch_and_brief`
  for §8/§9 generation; the worker falls back to Gemini if the CLI fails).
- The optional `ir` extra (required by `refresh_ir_kpis` and
  `discover_ir_documents`, for the headless browser that resolves each issuer's
  spreadsheet / document URLs): from the repo root, run
  `pip install -e .[ir] && playwright install chromium` in the same Python
  `python` resolves to. Without it those tasks' per-ticker discovery children
  exit non-zero (ImportError) and are logged as failures, but the batch still
  completes — every other task is unaffected.

## Install

From an **admin** PowerShell or `cmd` window, run one `schtasks /create` per
task:

```cmd
schtasks /create /tn "earnings-summary\backup_db" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\backup_db.task.xml"

schtasks /create /tn "earnings-summary\refresh_cache" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\refresh_cache.task.xml" ^
  /ru "%USERNAME%"

schtasks /create /tn "earnings-summary\refresh_dirty_artifacts" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\refresh_dirty_artifacts.task.xml" ^
  /ru "%USERNAME%"

schtasks /create /tn "earnings-summary\run_morning_pipeline" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\run_morning_pipeline.task.xml" ^
  /ru "%USERNAME%"

schtasks /create /tn "earnings-summary\scan_ir_transcripts" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\scan_ir_transcripts.task.xml" ^
  /ru "%USERNAME%"

schtasks /create /tn "earnings-summary\backfill_transcripts" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\backfill_transcripts.task.xml" ^
  /ru "%USERNAME%"

schtasks /create /tn "earnings-summary\fetch_fmp_earnings_calendar" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\fetch_fmp_earnings_calendar.task.xml" ^
  /ru "%USERNAME%"

schtasks /create /tn "earnings-summary\backfill_earnings_surprises" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\backfill_earnings_surprises.task.xml" ^
  /ru "%USERNAME%"

schtasks /create /tn "earnings-summary\daily_fetch_and_brief" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\daily_fetch_and_brief.task.xml" ^
  /ru "%USERNAME%"

schtasks /create /tn "earnings-summary\onboard_pending" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\onboard_pending_tickers.task.xml" ^
  /ru "%USERNAME%"

schtasks /create /tn "earnings-summary\weekly_p2_lens_refresh" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\weekly_p2_lens_refresh.task.xml" ^
  /ru "%USERNAME%"

schtasks /create /tn "earnings-summary\weekly_synthesis" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\weekly_synthesis.task.xml" ^
  /ru "%USERNAME%"

schtasks /create /tn "earnings-summary\monthly_p3_refresh" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\monthly_p3_refresh.task.xml" ^
  /ru "%USERNAME%"

schtasks /create /tn "earnings-summary\refresh_ir_kpis" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\refresh_ir_kpis.task.xml" ^
  /ru "%USERNAME%"

schtasks /create /tn "earnings-summary\discover_ir_documents" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\discover_ir_documents.task.xml" ^
  /ru "%USERNAME%"

schtasks /create /tn "earnings-summary\discover_ir_failing" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\discover_ir_failing.task.xml"

schtasks /create /tn "earnings-summary\verify_cron" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\verify_cron.task.xml"

schtasks /create /tn "earnings-summary\fetch_13f" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\fetch_13f.task.xml"

schtasks /create /tn "earnings-summary\submit_saydo_batch" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\submit_saydo_batch.task.xml"

schtasks /create /tn "earnings-summary\red_team" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\red_team.task.xml"

schtasks /create /tn "earnings-summary\fetch_macro_series" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\fetch_macro_series.task.xml"

schtasks /create /tn "earnings-summary\fetch_podcast_rss" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\fetch_podcast_rss.task.xml"

schtasks /create /tn "earnings-summary\model_eval_sweep" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\model_eval_sweep.task.xml"

schtasks /create /tn "earnings-summary\grade_calibration" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\grade_calibration.task.xml"

schtasks /create /tn "earnings-summary\weekly_validation" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\weekly_validation.task.xml"

schtasks /create /tn "earnings-summary\weekly_score_stances" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\weekly_score_stances.task.xml"

schtasks /create /tn "earnings-summary\monthly_advisor_memos" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\monthly_advisor_memos.task.xml"

schtasks /create /tn "earnings-summary\monthly_calibration_scorecard" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\monthly_calibration_scorecard.task.xml"

schtasks /create /tn "earnings-summary\restore_drill" ^
  /xml "%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\restore_drill.task.xml"
```

The `/tn` value is the registered task name (used by all `schtasks` commands
below); the `/xml` value is the file in this folder. Note that the
`onboard_pending` task name doesn't match its XML filename — that's fine, the
filename is just for humans.

### Migrating from PR #172's `run_triggers` cron

`run_morning_pipeline` replaces the standalone `run_triggers` task: the pipeline
runs `run_triggers.py` after the news fetch, then rebuilds the feed in the
same run. If you ever registered `\earnings-summary\run_triggers` (it was never in
this doc's install list, so most setups won't have it), delete it so the trigger
stage doesn't double-fire:

```cmd
schtasks /delete /tn "earnings-summary\run_triggers" /f
```

## Verify

```cmd
schtasks /query /tn "earnings-summary\<task>" /v /fo LIST
```

You should see `Status: Ready` and a `Next Run Time` consistent with the
cadence in the table above.

## Test fire (without waiting for the schedule)

```cmd
schtasks /run /tn "earnings-summary\<task>"
```

Then check:

- `.tmp\cron_logs\<task>_<TS>.log` — full stdout/stderr of the run.
- For `backfill_transcripts`: new `transcripts/raw/<TICKER>_Q<n>_<YYYY>.txt`
  files (for quarters the aggregators had) + new rows in `transcripts`
  table + new rows in `management_commitments` for the freshly-ingested
  transcripts. The JSON summary at the end of the log lists per-ticker
  fetched/skipped/miss counts.
- For `scan_ir_transcripts`: for any ticker inside its 14-day post-earnings
  window, a new `transcripts/raw/<TICKER>_Q<n>_<YYYY>.txt` (promoted to
  `processed/` by the ingest step) once the issuer posts its transcript. The
  JSON summary's `status_counts` breaks down the run (out_of_window /
  already_ingested / fetched / not_published_yet / pending_ingest / error). An
  all-`out_of_window`/`already_ingested` run is the steady-state no-op.
- For `fetch_fmp_earnings_calendar`: file mtimes on
  `data/historical/fmp/*_earnings_calendar.json` updated to the run time.
- For `backfill_earnings_surprises`: new/refreshed
  `data/surprise/<TICKER>_surprises.json` files (one per active ticker) +
  rows in the `earnings_surprises` table. The JSON summary at the end of the
  log lists per-ticker insert/update/unchanged counts and which source
  contributed each record (fmp_calendar vs yfinance).
- For `daily_fetch_and_brief`: `output/research/<TICKER>/<DATE>_workspace.html`
  for any tickers that had `brief_dirty=1`.
- For `run_morning_pipeline`: the log shows one `=== Stage N - …` header per
  stage (news → triggers → feed → validation) with each child's captured
  output beneath, then a JSON summary `{ "stage_0_news": "ok"|"failed",
  "stage_1_triggers": …, "stage_2_feed": …, "stage_3_validate": …,
  "elapsed_seconds": … }`. A failed/timed-out stage does NOT stop later
  stages; the process exit code is the number of failed stages (0 = all
  good). On success expect a fresh `data/dashboard/feed.html`. Use
  `--skip-triggers` to re-render the feed only (e.g. after
  approving/dismissing alerts) without paying for another trigger sweep.
- For `refresh_dirty_artifacts`: log shows a "Dirty artifact refresh manifest"
  block listing dedup'd `(ticker, command)` pairs, followed by per-job
  `drain_invoke` / `drain_subprocess_ok` / `drain_subprocess_failed` events.
  Closes with either `drain complete: N job(s) run …` (full drain) or
  `halted: cost cap reached at $X.XX …` (graceful budget halt). Re-running
  with no dirty/expired rows logs `No dirty artifacts. Pipeline is fresh.`
  and exits 0.
- For `onboard_pending`: the script exits 0 with an empty results array when
  nothing is pending; otherwise it logs each onboarded ticker.
- For `weekly_p2_lens_refresh`: new rows in the `llm_artifacts` table for
  P2-tier (watchlist + evaluation) tickers whose previous lens runs had
  drifted past the weekly cadence. Stable tickers log "cache hit — skipping"
  via the `cache_inputs` hash; that's the desired no-op behavior.
- For `weekly_synthesis`: five `=== <TIME> ...` step markers in the log
  (drain dirty → per-ticker lenses → cross-portfolio synthesis → dashboard
  rebuild → bear-case grading). On success, expect a fresh
  `output/dashboard/<DATE>_portfolio_dashboard.html`, new `llm_artifacts`
  rows for the eleven portfolio tickers + one `cross_portfolio_synthesis`
  artifact, and updated grades on any bear-case predictions whose
  `target_period` has passed.
- For `monthly_p3_refresh`: new `llm_artifacts` rows for P3-tier
  (index_member / etf / `none`) tickers that drifted past their 90-day
  cadence. Bounded — the P3 lens set is `five_min_reread` only.
- For `refresh_ir_kpis`: per configured ticker, a `=== IR-spreadsheet refresh -
  <T>` header followed by the child's JSON (`rows_inserted`, `doc_id`, …), then a
  final summary `{ "tickers": [...], "skipped_no_config": [...], "ok": N,
  "failed": N, "rows_inserted": N, "elapsed_seconds": … }`. On success, expect a
  refreshed `data/ir_spreadsheets/<T>/…xlsx` and new IR_DOC-tier rows in
  `kpi_facts` (which supersede the LLM brief values for the covered KPI/period
  pairs). A failed/timed-out ticker does NOT stop the others; exit code = number
  of failed tickers. If every ticker shows an ImportError in its captured stderr,
  the `ir` extra isn't installed (see Prerequisites).
- For `discover_ir_documents`: per roster ticker, a `=== IR-document discovery -
  <T>` header followed by the discover child's JSON (`status`, `discovered`) and
  the fetch child's JSON (`downloaded`), then a final summary `{ "tickers": [...],
  "ok": N, "skipped": N, "failed": N, "discovered": N, "downloaded": N, … }`. On
  success, expect new canonical files `ir_documents/<T>/<period_end>/ir_*__<sha8>.<ext>`
  and new `documents` rows (`SELECT doc_type, period_end, source_url FROM documents
  WHERE source_type='ir_doc'`). A ticker with no resolvable IR URL is `SKIPPED`
  (not counted in the exit code); a failed/timed-out ticker does NOT stop the
  others; exit code = number of FAILED tickers. Same `ir`-extra prerequisite as
  `refresh_ir_kpis`.

You can also run any wrapper directly to bypass the scheduler entirely:

```cmd
%USERPROFILE%\.gemini\antigravity\scratch\earnings-summary\cron\run_<task>.bat
```

## Uninstall

```cmd
schtasks /delete /tn "earnings-summary\<task>" /f
```

## Edit the schedule

Open Task Scheduler → Task Scheduler Library → `earnings-summary` →
`<task>` → Properties → Triggers tab. Or edit the XML and re-import with
`/create /f` to overwrite.
