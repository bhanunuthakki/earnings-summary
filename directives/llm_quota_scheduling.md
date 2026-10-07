# LLM quota and scheduling registry

This runbook is the single current repository authority for collision avoidance among scheduled application LLM work and interactive agent bursts. Global delegation and quota principles come from `agent-operations.SCHEDULING.md`; `src/llm/cli.py` owns transport behavior. Historical incidents live in `history/llm_quota_incidents_2026_07.md` and are evidence, not current procedure.

## Scheduling contract

- Timezone is `America/Los_Angeles`.
- Protect 03:00–05:00 daily. A material interactive or multi-agent burst must finish before that window; space planned bursts at least 6–7 hours apart.
- Register every new scheduled LLM leg in the table below in the same change. Verify the live scheduler against the checked-in task definition before relying on its time.
- Every checked-in task wrapper delegates through `cron/run_python.bat`. Provider order comes from the shared fleet policy; the wrapper disables subscription fallback by default through the policy's canonical control. Changing providers must not require editing task definitions or purpose/model semantics.
- Scheduled work degrades per item on transient membership-transport failure: persist an explicit deferred tally and retry next run. Setup failures and configured hard budget stops remain loud and nonzero.
- Resume a quota-interrupted agent when its context remains valid; do not repeat completed work.

## Current window registry

| Job | Schedule | LLM work / constraint |
|---|---|---|
| `run_morning_pipeline` | daily 04:00 | news classification, decision-condition extraction, standup composition/judging, eligible pre-earnings briefs, and portfolio-only post-earnings readouts; canonical protected window. The stage_1 `material_news` classification sweep (2026-10-06, audit F1/F1b) runs inside this same window and leg — not a new window: per-ticker `material_news_classification` Opus calls now run with bounded concurrency (default ≤4 simultaneous calls, `run_triggers.py --scan-workers`), each ticker under a 180s per-ticker budget (`--scan-timeout-s`, deferred explicitly on timeout), with wave admission gated by the run's `--max-cost-usd` over the shared `llm_calls` ledger; a transient failure or budget stop defers the affected tickers explicitly and retries next run |
| `refresh_scenario_priors` | monthly, 1st 03:00 | changed-input scenario-prior refresh |
| `weekly_p2_lens_refresh` | Friday 22:00 | P2 reread and thesis-drift lenses; dispatch ends by 01:35 |
| `backfill_transcripts` | daily 02:00 | `saydo_commitment_extract` commitment scans (pre-existing leg, registered here 2026-10-07 at M3 closeout — documentation only; the 02:00 start is unchanged). `execution/backfill_transcripts.py` fetches/ingests text Q&A transcripts for the active universe, then spawns `execution/extract_commitments_from_transcript.py --auto` per in-window transcript lacking a durable scan receipt, which extracts forward-looking management commitments and novel indicators through the governed `saydo_commitment_extract` purpose (`LLM_MODELS` pin + `llm_budgets` cap + `llm_calls` ledger attribution). Expected maximum duration: the task ceiling is PT60M (`cron/backfill_transcripts.task.xml`); the scan set is bounded by the 5-quarter window and the durable typed scan receipts, so a rerun only pays for newly landed transcripts. Budget behavior: the `llm_budgets` row is $40/month in warn mode — an 80% crossing records an alert, and a blown cap proceeds with a logged warning (spend stays in the ledger); `LLMBudgetExceeded` hard stops apply only to `on_exceed='block'` purposes, not this warn-mode one. Transient degradation: per-item defer/retry — a transcript whose call fails transiently (transport error, quota-window exhaustion) writes no scan receipt, stays in the persisted pending set (`transcripts_without_scan_receipt` over `commitment_scan_log`/coverage receipts), and is retried next run; zero-commitment scans persist their marker so they are never re-paid; setup failures (`LLMSetupError`) and nonzero ingest/extract exits fail the run loudly (nonzero exit), and a quarantined peer ticker cannot block the rest of the batch. Adjacent windows: Sunday 01:30 `discover_ir_documents`, Saturday 01:40 `fetch_sec_xbrl`, daily 02:15 `scan_ir_transcripts`, Saturday 02:20 `submit_saydo_batch`, daily 02:45 `backup_db`; the checked-in manifest agrees with the 02:00 start. The live Windows scheduler has not been verified from this Mac. |
| `submit_saydo_batch` | Saturday 02:20 | `pairwise_analysis` SayDo verdicts for due commitment pairs (`cron/submit_saydo_batch.task.xml`). Moved from 02:00 on 2026-10-06 (audit F8) so it no longer starts together with `backfill_transcripts` (daily 02:00) and `fetch_sec_xbrl` (now Saturday 01:40) on the single-writer database. The prepare step is read-only, and the submit step writes short per-result ledger and verdict rows. Usually a no-op; the 6h task ceiling is an upper bound, not the expected duration. A pre-flight monthly `pairwise_analysis` budget gate hard-halts loudly, and transient failures stay pending for the next run. The live Windows schedule has not been verified from this checkout. |
| `tenet_accountability` | Saturday 09:00 | changed-tenet accountability calls |
| `run_red_team` | first Saturday 10:00 | held-name and cross-book attacks |
| `thesis_collision` | Saturday 11:00 | whole-book clustering, cached on thesis-set hash |
| `disclosure_change_sweep` | Saturday 14:00 | changed-accession detector and materiality gate |
| `run_weekly_model_eval` | Saturday 20:00 | candidate sweep, judging, prompt A/B, and behavior distillation |
| `run_weekly_packet` | Sunday 08:00 | per-item packet predrafts |
| `run_grade_calibration` | Sunday 10:30 | grading calibration and rubric audits |
| `refresh_business_factors` | Sunday 11:30 | changed-input business-factor taxonomy |
| `ledger_synthesis` | daily morning block | cost-capped theme synthesis |
| `capture poller` | continuous/event-driven | bounded capture, triage, reply-intent, and research-routing calls |
| `fetch_news` quality scoring | with news fetch | bounded batches riding the morning-pipeline window when scheduled |
| `session_distill` | manual only | one session-distill purpose; postmortem backfill is a separate explicit action |
| MBGL recording follow-up (Mac app heartbeat) | hourly at :42, excluding 03:00–05:00; temporary until read-back confirms completion | `record-mobility-global-evaluation`; thread-context scheduling only, no application LLM pipeline or subagents; unchanged check target <2 minutes; defer on resource ownership or shared-quota contention; no claim that adjacent long-running jobs cannot overlap |
| Retail research roadmap continuation (thread heartbeat) | one-time October 3, 2026, 05:05 PDT | `continue-research-roadmap`; same-chat continuation after the protected window; first wake observed at 2026-10-03T12:05:28.725Z; heartbeat paused through the app after resuming; bounded existing source work and independently briefed GPT-6.1 Sol reviews only; no application ingestion/provider/production job is scheduled; recheck shared capacity before dispatch |
| `coach_pings` | daily 07:15 | no LLM work |
| `run_decision_nudge` | daily 17:00 | no LLM work |
| myclaw `weekly_review` | Sunday 14:00 | cross-repository scorer and domain reviews |
| myclaw `monthly_curate` | first Saturday 14:00 | cross-repository curation and fitting |

## Registration evidence

For a new or moved job, record the checked-in task definition, verified live schedule, purposes invoked, expected maximum duration, budget behavior, transient-degradation behavior, and adjacent windows. If any are unknown, do not claim the slot is collision-free.
