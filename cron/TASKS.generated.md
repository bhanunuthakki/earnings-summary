# Scheduled task inventory

Generated from `cron/task_manifest.json`; do not edit by hand.

| Task | Schedule | XML | Wrapper | Owner |
|---|---|---|---|---|
| `\earnings-summary\backfill_earnings_surprises` | daily at 06:15:00 | `backfill_earnings_surprises.task.xml` | `run_backfill_earnings_surprises.bat` | Task Scheduler |
| `\earnings-summary\backfill_transcripts` | daily at 04:30:00 | `backfill_transcripts.task.xml` | `run_backfill_transcripts.bat` | Task Scheduler |
| `\earnings-summary\backup_db` | daily at 02:45:00 | `backup_db.task.xml` | `run_backup_db.bat` | Task Scheduler |
| `\earnings-summary\capture_poller` | LogonTrigger | `capture_poller.task.xml` | `run_capture_poller.bat` | Windows service |
| `\earnings-summary\check_comp_set_drift` | weekly Sunday at 12:15:00 | `check_comp_set_drift.task.xml` | `run_check_comp_set_drift.bat` | Task Scheduler |
| `\earnings-summary\coach_pings` | daily at 07:15:00 | `coach_pings.task.xml` | `run_coach_pings.bat` | Task Scheduler |
| `\earnings-summary\daily_fetch_and_brief` | daily at 06:30:00 | `daily_fetch_and_brief.task.xml` | `run_daily_fetch_and_brief.bat` | Task Scheduler |
| `\earnings-summary\db_gc` | weekly Sunday at 06:00:00 | `db_gc.task.xml` | `run_db_gc.bat` | Task Scheduler |
| `\earnings-summary\decision_nudge` | daily at 17:00:00 | `decision_nudge.task.xml` | `run_decision_nudge.bat` | Task Scheduler |
| `\earnings-summary\disclosure_change_sweep` | weekly Saturday at 14:00:00 | `disclosure_change_sweep.task.xml` | `run_disclosure_change_sweep.bat` | Task Scheduler |
| `\earnings-summary\discover_ir_documents` | weekly Sunday at 01:30:00 | `discover_ir_documents.task.xml` | `run_discover_ir_documents.bat` | Task Scheduler |
| `\earnings-summary\discover_ir_failing` | weekly Wednesday,Saturday at 02:30:00 | `discover_ir_failing.task.xml` | `run_discover_ir_failing.bat` | Task Scheduler |
| `\earnings-summary\fetch_fmp_earnings_calendar` | daily at 05:45:00 | `fetch_fmp_earnings_calendar.task.xml` | `run_fetch_fmp_earnings_calendar.bat` | Task Scheduler |
| `\earnings-summary\fetch_macro_series` | daily at 05:35:00 | `fetch_macro_series.task.xml` | `run_fetch_macro_series.bat` | Task Scheduler |
| `\earnings-summary\fetch_sec_xbrl` | weekly Saturday at 02:00:00 | `fetch_sec_xbrl.task.xml` | `run_fetch_sec_xbrl.bat` | Task Scheduler |
| `\earnings-summary\grade_calibration` | weekly Sunday at 10:30:00 | `grade_calibration.task.xml` | `run_grade_calibration.bat` | Task Scheduler |
| `\earnings-summary\ledger_synthesis` | daily at 06:45:00 | `ledger_synthesis.task.xml` | `run_ledger_synthesis.bat` | Task Scheduler |
| `\earnings-summary\model_eval_sweep` | weekly Saturday at 20:00:00 | `model_eval_sweep.task.xml` | `run_weekly_model_eval.bat` | Task Scheduler |
| `\earnings-summary\monthly_advisor_memos` | monthly day 1 at 07:30:00 | `monthly_advisor_memos.task.xml` | `run_monthly_advisor_memos.bat` | Task Scheduler |
| `\earnings-summary\monthly_calibration_scorecard` | monthly day 2 at 07:30:00 | `monthly_calibration_scorecard.task.xml` | `run_monthly_calibration_scorecard.bat` | Task Scheduler |
| `\earnings-summary\monthly_p3_refresh` | monthly day 1 at 03:20:00 | `monthly_p3_refresh.task.xml` | `run_monthly_p3_refresh.bat` | Task Scheduler |
| `\earnings-summary\onboard_pending` | daily from 00:17:00, repeats PT1H | `onboard_pending_tickers.task.xml` | `run_onboard_pending.bat` | Task Scheduler |
| `\earnings-summary\red_team` | weekly Saturday at 10:00:00 | `red_team.task.xml` | `run_red_team.bat` | Task Scheduler |
| `\earnings-summary\refresh_business_factors` | weekly Sunday at 11:30:00 | `refresh_business_factors.task.xml` | `run_refresh_business_factors.bat` | Task Scheduler |
| `\earnings-summary\refresh_cache` | daily at 03:00:00 | `refresh_cache.task.xml` | `run_refresh_cache.bat` | Task Scheduler |
| `\earnings-summary\refresh_dirty_artifacts` | daily at 05:00:00 | `refresh_dirty_artifacts.task.xml` | `run_refresh_dirty_artifacts.bat` | Task Scheduler |
| `\earnings-summary\refresh_ir_kpis` | weekly Sunday at 01:00:00 | `refresh_ir_kpis.task.xml` | `run_refresh_ir_kpis.bat` | Task Scheduler |
| `\earnings-summary\refresh_scenario_priors` | monthly day 1 at 03:40:00 | `refresh_scenario_priors.task.xml` | `run_refresh_scenario_priors.bat` | Task Scheduler |
| `\earnings-summary\restore_drill` | monthly day 15 at 09:00:00 | `restore_drill.task.xml` | `run_restore_drill.bat` | Task Scheduler |
| `\earnings-summary\run_morning_pipeline` | daily at 04:00:00 | `run_morning_pipeline.task.xml` | `run_morning_pipeline.bat` | Task Scheduler |
| `\earnings-summary\scan_ir_transcripts` | daily at 04:15:00 | `scan_ir_transcripts.task.xml` | `run_scan_ir_transcripts.bat` | Task Scheduler |
| `\earnings-summary\senior_partner_brief` | weekly Sunday at 09:00:00 | `senior_partner_brief.task.xml` | `run_senior_partner_brief.bat` | Task Scheduler |
| `\earnings-summary\session_distill` | daily at 18:00:00 | `session_distill.task.xml` | `run_session_distill.bat` | Task Scheduler |
| `\earnings-summary\submit_saydo_batch` | weekly Saturday at 02:00:00 | `submit_saydo_batch.task.xml` | `run_submit_saydo_batch.bat` | Task Scheduler |
| `\earnings-summary\tenet_accountability` | weekly Saturday at 09:00:00 | `tenet_accountability.task.xml` | `run_tenet_accountability.bat` | Task Scheduler |
| `\earnings-summary\thesis_collision` | weekly Saturday at 11:00:00 | `thesis_collision.task.xml` | `run_thesis_collision.bat` | Task Scheduler |
| `\earnings-summary\track_comp_metrics` | daily at 07:10:00 | `track_comp_metrics.task.xml` | `run_track_comp_metrics.bat` | Task Scheduler |
| `\earnings-summary\verify_cron` | weekly Thursday at 07:00:00 | `verify_cron.task.xml` | `run_verify_cron.bat` | Task Scheduler |
| `\earnings-summary\weekly_cleanup` | weekly Sunday at 13:00:00 | `weekly_cleanup.task.xml` | `run_weekly_cleanup.bat` | Task Scheduler |
| `\earnings-summary\weekly_p2_lens_refresh` | weekly Sunday at 02:00:00 | `weekly_p2_lens_refresh.task.xml` | `run_weekly_p2_lens_refresh.bat` | Task Scheduler |
| `\earnings-summary\weekly_packet` | weekly Sunday at 08:00:00 | `weekly_packet.task.xml` | `run_weekly_packet.bat` | Task Scheduler |
| `\earnings-summary\weekly_score_stances` | weekly Sunday at 06:30:00 | `weekly_score_stances.task.xml` | `run_weekly_score_stances.bat` | Task Scheduler |
| `\earnings-summary\weekly_synthesis` | weekly Sunday at 23:00:00 | `weekly_synthesis.task.xml` | `run_weekly_synthesis.bat` | Task Scheduler |
| `\earnings-summary\weekly_validation` | weekly Sunday at 03:00:00 | `weekly_validation.task.xml` | `run_weekly_validation.bat` | Task Scheduler |

Registration renders each XML action against the checkout invoking the command:

```powershell
powershell -File cron/register_tasks.generated.ps1 -Python <path-to-python.exe> -RepoRoot (Resolve-Path .)
```
