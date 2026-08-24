# Generated from cron/task_manifest.json. Do not edit by hand.
param(
    [Parameter(Mandatory=$true)][string]$Python,
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
)
$ErrorActionPreference = 'Stop'
$renderDir = Join-Path $RepoRoot '.tmp\scheduler_tasks'
& $Python (Join-Path $RepoRoot 'execution\generate_cron_artifacts.py') --project-root $RepoRoot --render-dir $renderDir --check
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& schtasks.exe /Create /TN '\earnings-summary\backfill_earnings_surprises' /XML (Join-Path $renderDir 'backfill_earnings_surprises.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\backfill_earnings_surprises' }
& schtasks.exe /Create /TN '\earnings-summary\backfill_transcripts' /XML (Join-Path $renderDir 'backfill_transcripts.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\backfill_transcripts' }
& schtasks.exe /Create /TN '\earnings-summary\backup_db' /XML (Join-Path $renderDir 'backup_db.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\backup_db' }
& schtasks.exe /Create /TN '\earnings-summary\check_comp_set_drift' /XML (Join-Path $renderDir 'check_comp_set_drift.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\check_comp_set_drift' }
& schtasks.exe /Create /TN '\earnings-summary\coach_pings' /XML (Join-Path $renderDir 'coach_pings.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\coach_pings' }
& schtasks.exe /Create /TN '\earnings-summary\collect_operations_runtime_observations' /XML (Join-Path $renderDir 'collect_operations_runtime_observations.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\collect_operations_runtime_observations' }
& schtasks.exe /Create /TN '\earnings-summary\daily_fetch_and_brief' /XML (Join-Path $renderDir 'daily_fetch_and_brief.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\daily_fetch_and_brief' }
& schtasks.exe /Create /TN '\earnings-summary\db_gc' /XML (Join-Path $renderDir 'db_gc.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\db_gc' }
& schtasks.exe /Create /TN '\earnings-summary\decision_nudge' /XML (Join-Path $renderDir 'decision_nudge.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\decision_nudge' }
& schtasks.exe /Create /TN '\earnings-summary\disclosure_change_sweep' /XML (Join-Path $renderDir 'disclosure_change_sweep.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\disclosure_change_sweep' }
& schtasks.exe /Create /TN '\earnings-summary\discover_ir_documents' /XML (Join-Path $renderDir 'discover_ir_documents.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\discover_ir_documents' }
& schtasks.exe /Create /TN '\earnings-summary\discover_ir_failing' /XML (Join-Path $renderDir 'discover_ir_failing.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\discover_ir_failing' }
& schtasks.exe /Create /TN '\earnings-summary\fetch_fmp_earnings_calendar' /XML (Join-Path $renderDir 'fetch_fmp_earnings_calendar.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\fetch_fmp_earnings_calendar' }
& schtasks.exe /Create /TN '\earnings-summary\fetch_macro_series' /XML (Join-Path $renderDir 'fetch_macro_series.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\fetch_macro_series' }
& schtasks.exe /Create /TN '\earnings-summary\fetch_sec_xbrl' /XML (Join-Path $renderDir 'fetch_sec_xbrl.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\fetch_sec_xbrl' }
& schtasks.exe /Create /TN '\earnings-summary\grade_calibration' /XML (Join-Path $renderDir 'grade_calibration.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\grade_calibration' }
& schtasks.exe /Create /TN '\earnings-summary\ledger_synthesis' /XML (Join-Path $renderDir 'ledger_synthesis.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\ledger_synthesis' }
& schtasks.exe /Create /TN '\earnings-summary\model_eval_sweep' /XML (Join-Path $renderDir 'model_eval_sweep.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\model_eval_sweep' }
& schtasks.exe /Create /TN '\earnings-summary\monthly_advisor_memos' /XML (Join-Path $renderDir 'monthly_advisor_memos.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\monthly_advisor_memos' }
& schtasks.exe /Create /TN '\earnings-summary\monthly_calibration_scorecard' /XML (Join-Path $renderDir 'monthly_calibration_scorecard.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\monthly_calibration_scorecard' }
& schtasks.exe /Create /TN '\earnings-summary\monthly_p3_refresh' /XML (Join-Path $renderDir 'monthly_p3_refresh.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\monthly_p3_refresh' }
& schtasks.exe /Create /TN '\earnings-summary\onboard_pending' /XML (Join-Path $renderDir 'onboard_pending_tickers.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\onboard_pending' }
& schtasks.exe /Create /TN '\earnings-summary\portfolio_tracker_api' /XML (Join-Path $renderDir 'portfolio_tracker_api.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\portfolio_tracker_api' }
& schtasks.exe /Create /TN '\earnings-summary\red_team' /XML (Join-Path $renderDir 'red_team.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\red_team' }
& schtasks.exe /Create /TN '\earnings-summary\refresh_business_factors' /XML (Join-Path $renderDir 'refresh_business_factors.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\refresh_business_factors' }
& schtasks.exe /Create /TN '\earnings-summary\refresh_cache' /XML (Join-Path $renderDir 'refresh_cache.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\refresh_cache' }
& schtasks.exe /Create /TN '\earnings-summary\refresh_dirty_artifacts' /XML (Join-Path $renderDir 'refresh_dirty_artifacts.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\refresh_dirty_artifacts' }
& schtasks.exe /Create /TN '\earnings-summary\refresh_ir_kpis' /XML (Join-Path $renderDir 'refresh_ir_kpis.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\refresh_ir_kpis' }
& schtasks.exe /Create /TN '\earnings-summary\refresh_portfolio_tracker' /XML (Join-Path $renderDir 'refresh_portfolio_tracker.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\refresh_portfolio_tracker' }
& schtasks.exe /Create /TN '\earnings-summary\refresh_scenario_priors' /XML (Join-Path $renderDir 'refresh_scenario_priors.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\refresh_scenario_priors' }
& schtasks.exe /Create /TN '\earnings-summary\restore_drill' /XML (Join-Path $renderDir 'restore_drill.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\restore_drill' }
& schtasks.exe /Create /TN '\earnings-summary\run_morning_pipeline' /XML (Join-Path $renderDir 'run_morning_pipeline.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\run_morning_pipeline' }
& schtasks.exe /Create /TN '\earnings-summary\scan_ir_transcripts' /XML (Join-Path $renderDir 'scan_ir_transcripts.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\scan_ir_transcripts' }
& schtasks.exe /Create /TN '\earnings-summary\senior_partner_brief' /XML (Join-Path $renderDir 'senior_partner_brief.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\senior_partner_brief' }
& schtasks.exe /Create /TN '\earnings-summary\submit_saydo_batch' /XML (Join-Path $renderDir 'submit_saydo_batch.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\submit_saydo_batch' }
& schtasks.exe /Create /TN '\earnings-summary\tenet_accountability' /XML (Join-Path $renderDir 'tenet_accountability.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\tenet_accountability' }
& schtasks.exe /Create /TN '\earnings-summary\thesis_collision' /XML (Join-Path $renderDir 'thesis_collision.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\thesis_collision' }
& schtasks.exe /Create /TN '\earnings-summary\track_comp_metrics' /XML (Join-Path $renderDir 'track_comp_metrics.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\track_comp_metrics' }
& schtasks.exe /Create /TN '\earnings-summary\verify_cron' /XML (Join-Path $renderDir 'verify_cron.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\verify_cron' }
& schtasks.exe /Create /TN '\earnings-summary\weekly_cleanup' /XML (Join-Path $renderDir 'weekly_cleanup.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\weekly_cleanup' }
& schtasks.exe /Create /TN '\earnings-summary\weekly_p2_lens_refresh' /XML (Join-Path $renderDir 'weekly_p2_lens_refresh.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\weekly_p2_lens_refresh' }
& schtasks.exe /Create /TN '\earnings-summary\weekly_packet' /XML (Join-Path $renderDir 'weekly_packet.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\weekly_packet' }
& schtasks.exe /Create /TN '\earnings-summary\weekly_score_stances' /XML (Join-Path $renderDir 'weekly_score_stances.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\weekly_score_stances' }
& schtasks.exe /Create /TN '\earnings-summary\weekly_synthesis' /XML (Join-Path $renderDir 'weekly_synthesis.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\weekly_synthesis' }
& schtasks.exe /Create /TN '\earnings-summary\weekly_validation' /XML (Join-Path $renderDir 'weekly_validation.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\weekly_validation' }
