# Setting up the earnings-summary crons on Windows Task Scheduler

This is the operator runbook for the scheduled tasks defined in this folder.
The manifest owns each task's principal: most jobs run as the interactive user,
while `portfolio_tracker_api` runs as LOCAL SYSTEM. Jobs log to
`.tmp/cron_logs/<task>_<TS>.log` and Scheduler-backed declarations are grouped
under the `\earnings-summary\` namespace.

## Active crons

46 operational declarations total — 45 Task Scheduler registrations and one
separately managed Windows service. The authoritative set is
`cron/task_manifest.json`; `cron/TASKS.generated.md` is its deterministic
human-readable inventory. Run
`python execution/sqlite_bootstrap.py execution/generate_cron_artifacts.py --check`
to validate exact XML/wrapper coverage and generated artifacts, then
`python execution/sqlite_bootstrap.py execution/verify_cron_registration.py` to
compare the manifest with live Task Scheduler state. Registration uses
`cron/register_tasks.generated.ps1`, which renders every XML action against the
checkout invoking it before calling `schtasks`; adding a declaration here does
not register or run it. A monthly disaster-recovery drill (15th, 09:00)
restores the latest backup to a throwaway path and verifies it (see
**Disaster-recovery drill** below). The five daily data-chain tasks run in
sequence (03:00 → 06:30); a sixth daily task drains the LLM artifact queue at
05:00 and a seventh runs the Personal CIO morning pipeline at 04:00 (typed
20-stage manifest with atomic checkpoint/resume); an eighth (02:45) backs up
the database before the chain starts; the hourly catch-up is independent; the
weekly + monthly tasks run off-cycle and refresh the synthesis / lens layer,
the IR-spreadsheet KPI series, and the IR-document corpus — including a
twice-weekly rescan of the names whose IR crawl is still failing (a
bot-protected site that may start cooperating); and a weekly Thursday audit
verifies every declared task is registered and on schedule. Session
distillation remains available only as an explicit, operator-reviewed
maintenance CLI.

The retired `\earnings-summary\monthly_p3_refresh` task is no longer declared or registered by this checkout. On each Windows host where it was previously installed, run this one-time manual cleanup. This repository change does not execute the command or mutate Windows Task Scheduler:

```text
schtasks /Delete /TN "\earnings-summary\monthly_p3_refresh" /F
```

Retained scheduler logs and other historical receipts remain historical evidence; deleting the retired task does not delete them.

The podcast prototype is retired and 13F discovery is planned/dormant. Neither
is declared by the authoritative manifest. On a host that previously installed
either task, remove the stale registrations once:

```text
schtasks /Delete /TN "\earnings-summary\fetch_podcast_rss" /F
schtasks /Delete /TN "\earnings-summary\fetch_13f" /F
```

This cleanup removes scheduler controls only. Existing logs, historical signal
rows, and archived migrations remain retained evidence.

### Portfolio Tracker runtime

| Task name | Cadence | XML | Wrapper | What it does |
|---|---|---|---|---|
| `earnings-summary\portfolio_tracker_api` | Boot, LOCAL SYSTEM | `portfolio_tracker_api.task.xml` | `run_portfolio_tracker_api.bat` | The sole always-on API owner. It requires system-visible `PORTFOLIO_TRACKER_ROOT`, loopback-only `PORTFOLIO_TRACKER_API_URL`, and canonical `EARNINGS_SUMMARY_DB_PATH`; a missing/unsafe value fails instead of guessing a checkout, state root, interpreter, or bind address. It proves its child PID owns the configured loopback endpoint and writes a state-root receipt heartbeat every five minutes; lost proof stops the child before Scheduler retries. |
| `earnings-summary\refresh_portfolio_tracker` | Daily 07:30 | `refresh_portfolio_tracker.task.xml` | `run_refresh_portfolio_tracker.bat` | Runs the read-only receipt producer: typed HealthV1/database, snapshot, currency, reconciliation, and account-coverage evidence. It does not start the API or claim listener ownership; before attribution it verifies through Task Scheduler COM that the exact registered task and code-root wrapper are running, then writes its refresh and Scheduler planes to the same state-root receipt derived from `EARNINGS_SUMMARY_DB_PATH`. Those planes are expected current for 26 hours, while the API listener remains on the five-minute/15-minute heartbeat contract. |

The generated registration script creates both declarations. Run it elevated and pass the intended checkout as `-RepoRoot`; it renders the registered action to that absolute checkout path before calling `schtasks`. LOCAL SYSTEM reads only machine-visible environment values, so user-scoped variables and a user `.env` are not a configuration source for the API task. Roll back a bad Scheduler change by restoring the last known-good manifest, XML, wrappers, and generated artifacts in the canonical runtime checkout, rerunning **Install or re-register**, and completing **Verify live state**. Deleting these required tasks is retirement, not rollback.

Operations surface disposition: the supervisor receipt remains the primary live-health surface. The
separate dashboard activation receipt is deliberately excluded from current-health projection because
it records one operator action attempt, not listener health; the action response reports that attempt's
typed result, while the supervisor receipt remains the only authority for successful listener ownership.

### KPI semantic-review export

| Task name | Cadence | XML | Wrapper | What it does |
|---|---|---|---|---|
| `earnings-summary\prepare_kpi_semantic_review` | Every 10 minutes | `prepare_kpi_semantic_review.task.xml` | `run_prepare_kpi_semantic_review.bat` | Opens the canonical `EARNINGS_SUMMARY_DB_PATH` read-only and publishes bounded, source-safe, content-addressed per-ticker review artifacts plus one atomic current index under the product-state root. Code identity comes only from the deployed code root; mutable export state comes only from the database-derived product-state root. Missing identity, truncation, incomplete evidence, or split-root mismatch fails the task closed. |

Operations surface disposition: `primary surface`. The canonical manifest owns this task, so the
existing dynamic Jobs projection shows its declaration, ten-minute cadence, registered identity,
latest attempt, and failure state. This adds no dashboard control or request-time producer; the
read-only review endpoint serves only the producer's precomputed current artifact.

### Pre-chain backup

| Task name | Cadence | XML | Wrapper | What it does |
|---|---|---|---|---|
| `earnings-summary\backup_db` | Daily 02:45 | `backup_db.task.xml` | `run_backup_db.bat` | **SQLite online backup.** Runs `cron/backup_db.py`, which snapshots the canonical database with SQLite's online-backup API, compresses it locally, and publishes only an authenticated AES-256-GCM `.gz.enc` envelope to `ES_DB_BACKUP_DIR`. When that variable is unset, both backup and restore choose the first existing mounted `<drive>:\My Drive` from `D:` through `Z:` (for example, `G:\My Drive`), then fall back to `C:\Users\Bhanu\My Drive`; the backup folder is `earnings-summary-db-backups` beneath that root. The key stays in the external secrets directory. Fires 15 minutes before the 03:00 refresh chain and retains the newest `ES_DB_BACKUP_RETAIN` encrypted snapshots (default 14). |

### Daily chain (P1 tier — portfolio refreshed every day)

| Task name | Cadence | XML | Wrapper | What it does |
|---|---|---|---|---|
| `earnings-summary\refresh_cache` | Daily 03:00 | `refresh_cache.task.xml` | `run_refresh_cache.bat` | **Tier-aware FMP refresh queue.** Reads `FMP_TIER` from `.env` (defaults to `basic` = 250/day) and drains the highest-priority stale endpoints up to the cap. Failed endpoints (403 / Legacy Endpoint) get a 30-day retry window so a downgrade builds a backlog automatically; an upgrade catches up across following days. See `## Switching FMP tier` below. |
| `earnings-summary\refresh_dirty_artifacts` | Daily 05:00 | `refresh_dirty_artifacts.task.xml` | `run_refresh_dirty_artifacts.bat` | **LLM artifact cache drain.** Picks up every `llm_artifacts` row with `dirty=1` (upstream-fact trigger) or `expires_at < now` (per-purpose TTL elapsed — see `_DEFAULT_TTL_DAYS` in `src/llm_artifact_store.py`). For each (ticker, purpose), shells out to the regenerator script defined in `execution/refresh_dirty_artifacts._PURPOSE_TO_REGENERATOR`. Halts gracefully (exit 0) once accumulated `llm_calls.cost_estimate_usd` for this run reaches `--max-cost-usd 5`. |
| `earnings-summary\run_morning_pipeline` | Daily 04:00 | `run_morning_pipeline.task.xml` | `run_morning_pipeline.bat` | **Personal CIO morning pipeline.** Executes the typed 20-stage manifest in `src/pipeline/morning_manifest.py`, from environment preflight through news/state reconciliation, deterministic portfolio materializations, triggers/standup, pre/post-earnings artifacts, feed, and the final validation gate. Every child uses the verified SQLite bootstrap. Day + manifest-scoped atomic checkpoints resume only compatible successful stages; dependency failures block consumers while independent later stages may continue. |
| `earnings-summary\backfill_transcripts` | Daily 02:00 | `backfill_transcripts.task.xml` | `run_backfill_transcripts.bat` | Portfolio-only automatic text-transcript backfill, bounded to the canonical last 5 reported fiscal quarters. Evaluation requires an explicit owner `--ticker` request; watchlist/index/ETF/unknown identities fail closed before network access. Runs ingest and commitment extraction with exact evidence/SHA idempotency. |
| `earnings-summary\scan_ir_transcripts` | Daily 02:15 | `scan_ir_transcripts.task.xml` | `run_scan_ir_transcripts.bat` | **Post-earnings IR-transcript scan.** Portfolio-only automatic re-check for the latest reported quarter within 14 days of earnings. Evaluation requires explicit owner `--ticker`; lower-priority or unknown identities fail closed. Stops on exact DB/path/SHA evidence, then ingests any newly published text transcript. |
| `earnings-summary\fetch_fmp_earnings_calendar` | Daily 05:45 | `fetch_fmp_earnings_calendar.task.xml` | `run_fetch_fmp_earnings_calendar.bat` | Two steps. (1) Refreshes `data/historical/fmp/<TICKER>_earnings_calendar.json` for every portfolio + watchlist + evaluation ticker — on free/basic tier FMP refuses (402 since 2026-06-10) and the cache stays at its last good state. (2) Runs `execution/refresh_expected_earnings.py`, which materializes the **canonical `expected_earnings` table** via `next_earnings_date` (FMP cache → yfinance fallback); the Home rail's upcoming-earnings strip, the Home cockpit, and the portfolio-tracker's read-only bridge all read that table. Step 2 runs even when step 1 fails. |
| `earnings-summary\backfill_earnings_surprises` | Daily 06:15 | `backfill_earnings_surprises.task.xml` | `run_backfill_earnings_surprises.bat` | For every active-universe ticker, merges `<TICKER>_earnings_calendar.json` (FMP primary, full EPS + Revenue surprise) with `yfinance.Ticker.earnings_dates` (fallback, EPS-only) into `data/surprise/<TICKER>_surprises.json`, then upserts into `earnings_surprises`. Idempotent. Revenue surprise degrades to NULL when FMP coverage lapses. |
| `earnings-summary\daily_fetch_and_brief` | Daily 06:30 | `daily_fetch_and_brief.task.xml` | `run_daily_fetch_and_brief.bat` | Drains active portfolio/evaluation `brief_dirty` rows only: P1 daily and P2 when at least 7 days old, then material-change and evaluation-cadence gates. Watchlist/P3 rows never enter the full brief, DCF, or LLM builder. |

The daily fetch/brief crons run as a chain: backfill_transcripts (02:00) pulls
fresh Q&A transcripts + commitments, scan_ir_transcripts (02:15) catches the
latest issuer-published transcript for any ticker inside its 14-day
post-earnings window, refresh_cache (03:00) drains the FMP priority queue under
the configured tier, and fetch_fmp_earnings_calendar (05:45)
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

These regenerate LLM lens artifacts only where the coverage contract permits them. P1 is governed daily work; P2 receives weekly narrow monitoring lenses; P3 is deterministic-only and has no scheduled LLM plan.

| Task name | Cadence | XML | Wrapper | What it does |
|---|---|---|---|---|
| `earnings-summary\weekly_p2_lens_refresh` | Weekly, Friday 22:00 | `weekly_p2_lens_refresh.task.xml` | `run_weekly_p2_lens_refresh.bat` | Regenerates the narrow P2 lens set (`five_min_reread`, `thesis_drift_qoq`) for active watchlist/evaluation names at a 7-day due age. The plan is bounded at 128 pairs and dispatches only from 21:30 through 01:35 PT; transient items defer with retry result 75 and hard stops remain loud. It does not build watchlist DCFs or full briefs. |
| `earnings-summary\weekly_synthesis` | Weekly, Sunday 23:00 | `weekly_synthesis.task.xml` | `run_weekly_synthesis.bat` | **The "Sunday-night portfolio review" pipeline.** Four steps in order: (1) `refresh_dirty_artifacts.py --manifest-only` drains the LLM-artifact dirty queue so lens reads see fresh facts; (2) `run_lens.py --tickers AMZN,BN,GOOG,MELI,META,NOW,NU,NVO,RBRK,VEEV,WIX --all` regenerates every per-ticker lens for the full portfolio; (3) `run_lens.py --lens cross_portfolio_synthesis` runs the Opus cross-portfolio convergence read (~$0.25); (4) `build_analytical_dashboard.py` rebuilds `output/dashboard/<DATE>_portfolio_dashboard.html` with the new artifacts. Sequential — any step's failure halts the rest. **Bear-case grading was removed from this task** (#675) — it is owned by the dedicated weekly `grade_calibration` cron (Sun 10:30); grading is idempotent so a single weekly pass suffices. |
| `earnings-summary\submit_saydo_batch` | Weekly, Saturday 02:00 | `submit_saydo_batch.task.xml` | `run_submit_saydo_batch.bat` | **SayDo verdicts through the governed subscription-backed LLM entrypoint.** Two steps: (1) `build_saydo_pairs.py --all --prepare-batch` writes a JSONL of management-commitment (say, do) verdict requests whose check-date has arrived; (2) `submit_saydo_batch.py` processes each pending item through central purpose routing, writes successful verdicts, and leaves transient failures pending for the next run. No-op when nothing is due; the pre-flight budget gate and central hard stops fail loudly. |
| `earnings-summary\red_team` | Weekly Saturday 10:00 (self-gated to the month's FIRST Saturday) | `red_team.task.xml` | `run_red_team.bat` | **Monthly First-Saturday adversarial review** (`directives/monthly_red_team.md` Phase 2). Runs `execution/run_red_team.py`, which generates one rotating-lens adversarial attack per held name (weight > 0.5%, cash-likes/index-ETFs excluded) plus the three cross-book passes (factor-block detection, style drift, human-capital overlay), persists them to `red_team_items`, and writes a brief snapshot to `.tmp/red_team_briefs/`. Windows Task Scheduler has no native "Nth weekday of month" trigger, so the XML fires every Saturday and the script itself no-ops (exit 0, logged `red_team_skipped_not_first_saturday`) unless today is the month's first Saturday. Idempotent on `red_team_{YYYY_MM}` — a re-run of an already-generated month is a no-op unless `--force`. Per-item degrade (a transient LLM failure defers that one item and retries next run); a hard stop (budget cap / missing CLI) halts loudly. Daytime weekend slot, clear of every protected window in `directives/llm_quota_scheduling.md`. |

The two weekly tasks deliberately bracket the weekend: `weekly_p2_lens_refresh` runs Friday 22:00, clear of Saturday's eval and data jobs, while `weekly_synthesis` runs Sunday 23:00 so the portfolio dashboard reflects everything that landed during the weekend ahead of Monday open. They do not depend on each other — `weekly_synthesis` step 1 (`refresh_dirty_artifacts`) guarantees its own current inputs.

### IR-spreadsheet KPI refresh (weekly)

| Task name | Cadence | XML | Wrapper | What it does |
|---|---|---|---|---|
| `earnings-summary\refresh_ir_kpis` | Weekly, Sunday 01:00 | `refresh_ir_kpis.task.xml` | `run_refresh_ir_kpis.bat` | **IR-spreadsheet KPI refresh.** Runs `execution/refresh_ir_kpis_all.py`, which loops every ticker with a parser config (`micro_thesis/ir_config/<T>.json`, enumerated via `ir_pipeline.config.configured_tickers`) and, per ticker, shells out to `refresh_ir_kpis.py --ticker <T> --discover`: headless-renders the issuer's IR results-center, downloads the current historical-data spreadsheet, parses it, and ingests the KPI series at IR_DOC tier — superseding the lower-tier LLM brief values the report charts read. Subprocess-isolated per ticker with a 5-min cap; never aborts on one ticker's failure; exit code = count of failed tickers. Idempotent: an unchanged spreadsheet re-ingests as a no-op (the document is sha256-keyed), so the weekly poll simply catches each new quarter's file within a week of publication. Tickers without a config are skipped (a ticker's first refresh must run `refresh_ir_kpis.py --url`/`--file` to generate its config). **Requires the optional `ir` extra** in the task's Python — see Prerequisites. |

Runs Sunday 01:00 — ahead of the Sunday-night `weekly_synthesis` (23:00) — so freshly-ingested issuer KPIs are in `kpi_facts` before the portfolio synthesis read. The independent P2 lens sweep runs Friday 22:00. To change the cadence, edit the trigger in `refresh_ir_kpis.task.xml`; the script is cadence-agnostic and idempotent either way.

### Cron-registration audit (weekly)

| Task name | Cadence | XML | Wrapper | What it does |
|---|---|---|---|---|
| `earnings-summary\verify_cron` | Weekly, Thursday 07:00 | `verify_cron.task.xml` | `run_verify_cron.bat` | **Cron self-audit.** Runs `execution/verify_cron_registration.py`, which reads the manifest-declared `cron/*.task.xml`, validates the manifest and XML metadata, then queries `schtasks /query /fo csv` for the live state. Reports eight problem categories: **manifest** (coverage, registration identity, action, or schedule errors), **unparseable** (a task XML cannot be read or parsed), **no_uri** (parsed XML lacks a `<URI>`), **missing** (XML exists but task is not registered), **extra** (a live task in the product namespace is not declared by the manifest), **disabled** (registered but not Ready/Running), **mismatch** (scheduled time differs from XML), and **wrong_root** (the registered wrapper targets a different checkout). Exit 0 = all OK, 1 = at least one problem category, 2 = `schtasks` unreachable. Fires Thursday morning so any drift from the weekend's task-scheduler maintenance or a failed install surfaces before the next weekly synthesis run. Output in `.tmp/cron_logs/verify_cron_*.log`. |

### IR-document discovery + fetch (weekly)

| Task name | Cadence | XML | Wrapper | What it does |
|---|---|---|---|---|
| `earnings-summary\discover_ir_documents` | Weekly, Sunday 01:30 | `discover_ir_documents.task.xml` | `run_discover_ir_documents.bat` | **IR-document corpus refresh.** Portfolio-only automatic discovery/download, bounded to the canonical last 5 reported quarters. Each child rebinds the ticker to its active stored role, so direct `--url`, `--ticker`, or `--all` invocation cannot widen scope; evaluation is owner-requested only and watchlist/index/ETF/unknown identities fail closed. URL and content-SHA idempotency, per-stage timeouts, per-ticker failure isolation, and the existing registration/evidence path remain unchanged. **Requires the optional `ir` extra.** |

Runs Sunday 01:30 — just after `refresh_ir_kpis` (01:00) and before Sunday-night synthesis — so freshly-registered IR documents and narrative anchors precede that synthesis. The independent P2 lens sweep runs Friday 22:00. To change the cadence, edit the trigger in `discover_ir_documents.task.xml`. The same per-ticker chain also runs best-effort on onboard (`execution/onboard_ticker.py`, `--skip-ir` to disable).

| Task name | Cadence | XML | Wrapper | What it does |
|---|---|---|---|---|
| `earnings-summary\discover_ir_failing` | Twice weekly, Wed + Sat 02:30 | `discover_ir_failing.task.xml` | `run_discover_ir_failing.bat` | **Failing-crawler rescan.** Rechecks only portfolio names with zero registered IR documents through the same stored-role and 5-quarter policy. A recovered name drops out; persistent source blocks remain visible. Idempotent and per-ticker isolated. **Requires the optional `ir` extra.** |

The failing-only rescan is the cheap mid-week companion to the Sunday full sweep: the full sweep re-checks every name weekly, this re-checks only the still-failing ones on Wednesday + Saturday so a recovered site is picked up within ~3 days. Coverage + each name's last crawl outcome are visible in the command center's **IR Docs** tab (`GET /api/panel/ir_coverage`), which also shows where to drop a manually-pulled file for the names that stay blocked.

### Prototype scheduler status

`task_manifest.json` remains the sole authority for installed operations; no
parallel prototype scheduler exists. 13F discovery and SEC-delta planning are
**planned/dormant**: their implementation remains available for manual development
and validation, but neither is registered. Podcast RSS/takeaway is **retired**:
its executable, eval, model budget, and rendered Diet paths are removed, while
historical migrations, logs, and persisted rows remain evidence. Re-activation is
a separate product and operations decision, not a scheduler toggle.

### Additional standalone + analytics tasks

These run off the daily chain. They were present on disk but previously undocumented here; this table closes that gap (and they are now in the Install list below).

| Task name | Cadence | XML | Wrapper | What it does |
|---|---|---|---|---|
| `earnings-summary\fetch_macro_series` | Daily 05:35 | `fetch_macro_series.task.xml` | `run_fetch_macro_series.bat` | Populates `macro_series` from timeout-bounded Yahoo candidates; direct FMP macro calls remain disabled until admitted by the shared recovery service. Recomputes portfolio `macro_sensitivities` only when every requested series is fresh or explicitly cached-degraded within 45 days, preserving warning/failure exit codes otherwise. |
| `earnings-summary\model_eval_sweep` | Weekly, Saturday 20:00 | `model_eval_sweep.task.xml` | `run_weekly_model_eval.bat` | The activated model-downgrade eval loop (`directives/model_eval_loop.md`): harvest a rotating 2-ticker sample → sweep every cheaper candidate per active purpose → auto-switch a `model_pin_overrides` row after 3 consecutive SWITCH_DOWN verdicts (revert after 3 KEEP_INCUMBENT). Conservative + reversible; every decision audited via `model_eval_verdicts` + alerts. (The task runs `run_weekly_model_eval.bat`; the older `run_model_eval_sweep.bat` is unused.) |
| `earnings-summary\grade_calibration` | Weekly, Sunday 10:30 | `grade_calibration.task.xml` | `run_grade_calibration.bat` | Feeds the prompt-calibration loop: `run_calibration_grading.py` runs 3 outcome graders (predictions → decisions → bear_cases, the last over `--all-portfolio`) + 5 eval-audit rungs (bear_case, transcript_summary, advisor_next_dollar, ask_advisory_answer, calibration_coach), writing `prompt_calibration_scores`. Never aborts early; exit code = count of failed rungs. **Owns bear-case grading** (moved here from `weekly_synthesis` in #675). |
| `earnings-summary\weekly_validation` | Weekly, Sunday 03:00 | `weekly_validation.task.xml` | `run_weekly_validation.bat` | **Confidence backfill.** Rescores `financial_facts`/`kpi_facts` confidence `--apply`, folding fresh validation-issue penalties into per-fact scores (idempotent). The validation-engine SCAN runs DAILY in `run_morning_pipeline` (stage 3), so it is **not** repeated here (#679 dropped the duplicate weekly scan — the backfill reads the issues the daily run already inserted). Recorded in `ingestion_runs` for the cron-health panel. |
| `earnings-summary\weekly_cleanup` | Weekly, Sunday 13:00 | `weekly_cleanup.task.xml` | `run_weekly_cleanup.bat` | Runs `run_weekly_cleanup.py --apply` against the allowlist in `directives/weekly_cleanup.md`, then `expire_stale_research.py --apply`. It has no network dependency, stops after 15 minutes, skips missed starts, and uses the shared `portfolio-db` lock for each stage. A cleanup failure prevents research expiry. |
| `earnings-summary\weekly_score_stances` | Weekly, Sunday 06:30 | `weekly_score_stances.task.xml` | `run_weekly_score_stances.bat` | Grades matured advisor memos/stances (master build P2.5): Socratic stances vs price (SPY-relative via the tracker) + swap checks vs realized pair margin. Deferrals (immature horizons / price-cache gaps) are normal; idempotent over the pending set. |
| `earnings-summary\monthly_advisor_memos` | Monthly, 1st @ 07:30 | `monthly_advisor_memos.task.xml` | `run_monthly_advisor_memos.bat` | Advisor memo run (master build P2.3): the next-dollar allocation memo + swap-discipline checks. Runs after the morning pipeline so FMP prices/DCFs are fresh; degrades gracefully if the tracker is offline. Each memo persists to `advisor_memos` + an analyst note (+ ledger entry when ticker-scoped). |
| `earnings-summary\monthly_calibration_scorecard` | Monthly, 2nd @ 07:30 | `monthly_calibration_scorecard.task.xml` | `run_monthly_calibration_scorecard.bat` | Calibration scorecard (close_the_loops L8): the compounding loop's personal readout — deterministic hit-rate/skill split + eval-gated coach prose (named biases + a falsifiable experiment), grounded only in the owner's graded history. Below the min-n floor it renders the substrate with a "too thin to coach yet" note and makes no LLM call. Persists to `data/calibration_scorecard/<period>.json`. |

### Disaster-recovery drill

The scheduled counterpart to the daily `backup_db` — a backup you have never restored is not a backup.

| Task name | Cadence | XML | Wrapper | What it does |
|---|---|---|---|---|
| `earnings-summary\restore_drill` | Monthly, 15th @ 09:00 | `restore_drill.task.xml` | `run_restore_drill.bat` | **DB restore drill.** Runs `execution/restore_drill.py`, which restores the **latest authenticated encrypted** backup to a throwaway temp path and verifies AES-GCM authentication, decryption, gunzip, `PRAGMA integrity_check`, core-table row counts, and an exact schema-version match against the live DB when its Alembic revision is available (otherwise requiring a valid versioned snapshot). **Never touches the live DB** except to record one `ingestion_runs` row. Exit 0 = passed, 1 = a hard check failed, 2 = no encrypted snapshot found. Plain `.gz` files are ignored except by the explicit one-time migration utility. |

### Backup inventory and non-destructive restore verification

Run these commands from the canonical runtime checkout. The durable state stays
at `C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary\data\portfolio.db`;
the runtime checkout is code, not a second database authority. The restore host
must have `ES_DB_BACKUP_KEY_FILE` pointing at the escrowed key and, when the
default Google Drive directory is not used, `ES_DB_BACKUP_DIR` pointing at the
encrypted backup directory.

```powershell
$EarningsSummaryCodeRoot = 'C:\Users\Bhanu\.gemini\antigravity\runtime\earnings-summary'
$EarningsSummaryDataRoot = 'C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary'
$EarningsSummaryDbPath = Join-Path $EarningsSummaryDataRoot 'data\portfolio.db'
$env:EARNINGS_SUMMARY_DB_PATH = $EarningsSummaryDbPath
$EarningsSummaryAttemptId = [guid]::NewGuid().ToString('N')
$EarningsSummaryRecoveryRoot = Join-Path $env:TEMP "earnings-summary-recovery-$EarningsSummaryAttemptId"
$EarningsSummaryRecoveryDbPath = Join-Path $EarningsSummaryRecoveryRoot 'portfolio.db'
New-Item -ItemType Directory -Path $EarningsSummaryRecoveryRoot -ErrorAction Stop | Out-Null
Set-Location $EarningsSummaryCodeRoot

python execution/sqlite_bootstrap.py cron/restore_db.py --list
if ($LASTEXITCODE -ne 0) { throw 'backup listing failed' }

python execution/sqlite_bootstrap.py cron/restore_db.py --latest `
  --to $EarningsSummaryRecoveryDbPath --schema-policy exact
if ($LASTEXITCODE -ne 0) { throw 'non-destructive restore verification failed' }
```

`--latest` authenticates and decrypts the selected `.gz.enc`, restores only to
the unique sibling/temp path, and verifies integrity, quick-check, foreign keys,
and the exact Alembic head. `cron/restore_db.py` intentionally refuses the
canonical live path. Do not use `--force` against the live database or turn the
verified sibling into a live replacement while any service or scheduled writer
is running.

### Guarded recovery and rollback

A verified sibling is the recovery input, not permission to overwrite live
state. For an owner-approved recovery window:

1. Record the chosen encrypted backup name and the successful restore receipt.
2. Stop `es-dashboard`, stop the `portfolio_tracker_api` task, disable the
   `\earnings-summary\` scheduled writers, and verify no process owns
   `portfolio.db`, `portfolio.db-wal`, or `portfolio.db-shm`.
3. Preserve the current canonical database as an attempt-identified rollback
   file beside it. Keep its WAL/SHM state with that rollback set; do not copy a
   live SQLite file independently.
4. Only while every writer remains stopped, replace the canonical database with
   the already verified recovery database. Re-enable/register the fleet from
   the current runtime checkout and run the live checks in **Verify live state**.
5. If service health, schema, Scheduler actions, or dashboard hydration fails,
   stop all writers again and restore the preserved rollback set. Do not delete
   either recovery receipt until the owner accepts the recovered state.

The repository deliberately has no unattended live-replacement command. The
offline writer-stop proof and retained rollback copy are required guards.

### Durable-state export

For an owner-requested plaintext export, create a verified reader snapshot at a
named protected destination outside both checkouts:

```powershell
$EarningsSummaryExportRoot = Join-Path $env:USERPROFILE 'Documents\earnings-summary-exports'
$EarningsSummaryExportDbPath = Join-Path $EarningsSummaryExportRoot "portfolio-$EarningsSummaryAttemptId.db"
New-Item -ItemType Directory -Path $EarningsSummaryExportRoot -ErrorAction Stop | Out-Null
python execution/sqlite_bootstrap.py execution/create_sqlite_snapshot.py `
  --source-path $EarningsSummaryDbPath `
  --destination-path $EarningsSummaryExportDbPath
if ($LASTEXITCODE -ne 0) { throw 'durable-state export failed' }
```

The export contains the canonical SQLite application state and the adjacent
strict `<snapshot>.manifest.json` provenance/integrity receipt. It does not
include `.env`, encryption keys, external Portfolio Tracker state, loose source
corpora, or generated outputs. This export is sensitive plaintext, unlike the
encrypted backup envelope: restrict access, move both files together, and use
the encrypted backup workflow for unattended or cloud retention.

### Re-registering after an XML or security change

Editing a `*.task.xml`, wrapper, trigger, principal, or security descriptor does
not change the live Windows task. Re-run the complete generated registration
command in **Install or re-register** after every such change. The generated
script uses `/F` and replaces every Scheduler-backed manifest declaration, so a
partial hand-maintained import cannot leave old triggers or ACLs behind.

## Shared job runtime — exit codes & the schema-drift guard

Every cron `run_*.bat` routes through `cron/run_python.bat` → `cron/job_runtime.py --scheduler-wrapper` (the shared runtime in `src/runtime/job_runtime.py`). It serializes overlapping portfolio-DB writes, writes a JSON job-health record under `.tmp/job_health/<job>/`, and — before running any work — refuses to start on schema drift. The exit code Task Scheduler shows as **Last Result** is therefore meaningful on its own; you should rarely need to open a log to know *why* a job stopped:

| Exit code | Meaning | Job-health `status` | What to do |
|---|---|---|---|
| `0` | Succeeded (or the wrapped script's own success) | `ok` | — |
| `2` from `refresh_cache` | Live FMP was unavailable, but existing corpus data was served and recovery work was queued | `degraded_corpus` (`severity=warning`) | No immediate action; the scheduled probe will retry |
| `3` from `refresh_cache` | Some FMP work succeeded or used corpus, while unresolved work remains queued | `partial` (`severity=warning`) | Review the Settings recovery backlog if it persists |
| non-zero from the script | The wrapped script failed; the code is the script's own (e.g. count of failed stages/tickers) | `failed` | Read the job's `.tmp/cron_logs/*.log` |
| `75` | Another live process already owns the same write set (`portfolio-db`); safe, retryable scheduler contention | `skipped_locked` | Nothing — the next scheduled run retries |
| `78` | **Schema drift** — the portfolio DB's Alembic revision disagrees with this checkout, so the job refused to run *before* touching anything | `blocked_schema_drift` | See below |

### Exit 78 (schema drift)

The guard exists because a lagging DB revision used to fail **silently**: guarded writers refused correctly, but the best-effort LLM cost ledger (`src/llm_call_ledger.py`) swallowed the refusal by design, so cost rows were dropped while Task Scheduler still recorded success (incident 2026-08-02, seven rows lost). Now a non-exempt job stops loudly with exit 78 and a `blocked_schema_drift` health record, and the dashboard's **Cron Health** tab shows a red drift banner naming the fix.

The health-record `detail` (and the drift banner) names **which side moved**:

- **database behind checkout** → `alembic upgrade head` (migrate the DB to this checkout's head).
- **revision unknown to the checkout** → the *checkout* is stale; `git pull` it. Do **not** `alembic upgrade head` — it would try to apply migrations that no longer lead anywhere (see the 2026-07-30 stale-checkout incident).
- **checkout has multiple Alembic heads** → merge the heads in `alembic/versions` before running cron.

**Exempt jobs** run even while drifted, because a drifted DB is exactly when they matter most: `backup_db` (capture a snapshot), `restore-drill` (prove the restore path), `verify-cron` (audit registration), and `collect-operations-runtime-observations` (record the current Scheduler/service evidence needed to diagnose the fleet). The exempt set lives in `SCHEMA_DRIFT_TOLERANT_JOBS` in `src/runtime/job_runtime.py`.

**Escape hatch (interactive only):** pass `--allow-schema-drift` to `job_runtime.py` to run a job against a drifted DB on purpose. Scheduled jobs that legitimately need this belong in `SCHEMA_DRIFT_TOLERANT_JOBS`, not the flag.

Note: every non-exempt job preflights the DB, so a genuinely forked or behind-DB state stops the **whole fleet** at once — that is intended (don't run cron against an inconsistent DB), but it means one drift event reads as many red jobs, not one.

### Dropped ledger rows that slipped past the guard

The exit-78 gate stops drift-caused drops, but the cost ledger can still drop a row for other reasons (a momentarily locked DB, a missing table). Those are counted — one JSON line per drop — in `data/.health/dropped_llm_ledger_write.jsonl` (beside the DB, so the dashboard and cron fleet, which run from different checkouts, share one counter). `verify_daily_chain.py` surfaces the 24h count in `.tmp/daily_chain_status.json` and prints a `!!!` marker when non-zero, and the **Cron Health** tab shows a "N LLM cost rows lost" banner. A non-zero count is not recoverable — the rows are gone — but it tells you the ledger is losing writes and needs attention.

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
   python execution/sqlite_bootstrap.py execution/refresh_cache.py run --tier premium --force
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
- Canonical code checkout at
  `C:\Users\Bhanu\.gemini\antigravity\runtime\earnings-summary`. Wrappers resolve
  their code root from their own registered action; do not edit them to point at
  the scratch directory.
- Canonical durable-state root at
  `C:\Users\Bhanu\.gemini\antigravity\scratch\earnings-summary`. Keep
  `data\portfolio.db` there; never create a runtime-checkout database as a
  fallback.
- A machine-visible `EARNINGS_SUMMARY_DB_PATH` set to that canonical database
  path for scheduled/service processes. The LOCAL SYSTEM tracker API also
  requires its documented machine-visible `PORTFOLIO_TRACKER_ROOT` and
  loopback-only `PORTFOLIO_TRACKER_API_URL` values.
- Claude Code CLI on PATH and authed (only required by `daily_fetch_and_brief`
  for §8/§9 generation; the worker falls back to Gemini if the CLI fails).
- The optional `ir` extra (required by `refresh_ir_kpis` and
  `discover_ir_documents`, for the headless browser that resolves each issuer's
  spreadsheet / document URLs): from the repo root, run
  `pip install -e .[ir] && playwright install chromium` in the same Python
  `python` resolves to. Without it those tasks' per-ticker discovery children
  exit non-zero (ImportError) and are logged as failures, but the batch still
  completes — every other task is unaffected.

## Install or re-register

From an **elevated PowerShell** window, validate the canonical manifest and run
the generated installer from the runtime checkout. Always pass `-RepoRoot`
explicitly so every rendered action points at that checkout:

```powershell
$EarningsSummaryCodeRoot = 'C:\Users\Bhanu\.gemini\antigravity\runtime\earnings-summary'
$EarningsSummaryPython = (Get-Command python.exe -ErrorAction Stop).Source
Set-Location $EarningsSummaryCodeRoot

& $EarningsSummaryPython execution/sqlite_bootstrap.py `
  execution/generate_cron_artifacts.py --check
if ($LASTEXITCODE -ne 0) { throw 'scheduler source validation failed' }

& (Join-Path $EarningsSummaryCodeRoot 'cron\register_tasks.generated.ps1') `
  -Python $EarningsSummaryPython `
  -RepoRoot $EarningsSummaryCodeRoot
if ($LASTEXITCODE -ne 0) { throw 'scheduler registration failed' }
```

This is the only supported bulk registration path. It registers every
Scheduler-backed declaration in `cron/task_manifest.json`, including
`\earnings-summary\portfolio_tracker_api` and
`\earnings-summary\refresh_portfolio_tracker`; the separately managed Windows
service declaration remains identified as such in `cron/TASKS.generated.md`.

### Migrating from PR #172's `run_triggers` cron

`run_morning_pipeline` replaces the standalone `run_triggers` task: the pipeline
runs `run_triggers.py` after the news fetch, then rebuilds the feed in the
same run. If you ever registered `\earnings-summary\run_triggers` (it was never in
this doc's install list, so most setups won't have it), delete it so the trigger
stage doesn't double-fire:

```cmd
schtasks /delete /tn "earnings-summary\run_triggers" /f
```

## Verify live state

Registration success is not live proof. In the same elevated PowerShell, run
the manifest-wide verifier and then inspect the two Portfolio Tracker XML
registrations whose ownership and ACL are operationally significant:

```powershell
Set-Location $EarningsSummaryCodeRoot
& $EarningsSummaryPython execution/sqlite_bootstrap.py `
  execution/verify_cron_registration.py
if ($LASTEXITCODE -ne 0) { throw 'live Scheduler manifest verification failed' }

$TrackerApiXml = (schtasks.exe /Query `
  /TN '\earnings-summary\portfolio_tracker_api' /XML | Out-String)
if ($LASTEXITCODE -ne 0) { throw 'portfolio_tracker_api is not registered' }
$ExpectedTrackerApiAction = Join-Path $EarningsSummaryCodeRoot 'cron\run_portfolio_tracker_api.bat'
foreach ($Expected in @(
  'D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGX;;;IU)',
  '<UserId>S-1-5-18</UserId>',
  "<Command>$ExpectedTrackerApiAction</Command>"
)) {
  if (-not $TrackerApiXml.Contains($Expected)) {
    throw "portfolio_tracker_api live XML mismatch: $Expected"
  }
}

& (Join-Path $EarningsSummaryCodeRoot 'cron\apply_task_security_descriptor.ps1') `
  -TaskPath '\earnings-summary\portfolio_tracker_api' `
  -RenderedXmlPath (Join-Path $EarningsSummaryCodeRoot 'cron\portfolio_tracker_api.task.xml') `
  -VerifyOnly

$TrackerRefreshXml = (schtasks.exe /Query `
  /TN '\earnings-summary\refresh_portfolio_tracker' /XML | Out-String)
if ($LASTEXITCODE -ne 0) { throw 'refresh_portfolio_tracker is not registered' }
$ExpectedTrackerRefreshAction = Join-Path $EarningsSummaryCodeRoot 'cron\run_refresh_portfolio_tracker.bat'
if (-not $TrackerRefreshXml.Contains("<Command>$ExpectedTrackerRefreshAction</Command>")) {
  throw 'refresh_portfolio_tracker live action does not target the runtime checkout'
}
```

`GRGX` grants the Task Scheduler read/query access required by the unprivileged
Operations collector. The grant is limited to `IU` (Interactive Users), matching
the collector's `InteractiveToken`; do not broaden it to Builtin or Authenticated
Users. `GR` alone can expose the folder but still deny `GetTask`, and the
file-specific `FR` token is also insufficient. Windows treats execute access as
capable of task control, so the Operations collector must remain query-only and
must not expose Scheduler mutation through its API surface.

`schtasks.exe /Create /XML` retains the XML `SecurityDescriptor` property but
does not apply it as the registered task's actual task DACL on the production Windows
host. The generated registration script therefore calls the Task Scheduler COM
`SetSecurityDescriptor` method after creation, using
`TASK_DONT_ADD_PRINCIPAL_ACE`, and fails unless an immediate
`GetSecurityDescriptor` readback proves the protected, allow-only actual task
DACL. Windows maps the declaration's generic rights when attaching the
descriptor: `GA` becomes mask `0x1f01ff`, and `GRGX` becomes `0x1200a9`. A failed
postcondition restores and verifies the prior actual task access semantics before
failing loudly. Do not register this task with a raw `schtasks.exe /Create`
command outside that generated path.

The verifier must report no missing, extra, disabled, schedule-mismatched, or
wrong-checkout Scheduler declarations. The live XML checks prove the API's
declared SDDL, LOCAL SYSTEM principal (`S-1-5-18`), and both tracker actions; only
the COM `GetSecurityDescriptor(4)` check proves the actual task DACL. Do not infer
those properties from source XML or a successful health endpoint.

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
- For `run_morning_pipeline`: the log shows one header for every entry in the
  typed 20-stage manifest and a final JSON status map. A failed/timed-out stage
  blocks declared dependents while independent stages may continue. The atomic
  checkpoint identity includes UTC day plus the manifest digest, so a changed
  manifest cannot reuse incompatible success receipts. On success expect a
  fresh `data/dashboard/feed.html`. Use
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
C:\Users\Bhanu\.gemini\antigravity\runtime\earnings-summary\cron\run_<task>.bat
```

## Uninstall

```cmd
schtasks /delete /tn "earnings-summary\<task>" /f
```

## Edit the schedule

Edit the canonical XML, regenerate/check artifacts, and rerun **Install or
re-register**. Do not hand-edit live Scheduler properties: the next generated
registration would correctly replace them from source.
