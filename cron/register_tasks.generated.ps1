# Generated from cron/task_manifest.json. Do not edit by hand.
param(
    [Parameter(Mandatory=$true)][string]$Python,
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
)
$ErrorActionPreference = 'Stop'
$renderDir = Join-Path $RepoRoot '.tmp\scheduler_tasks'
$taskSecurityScript = Join-Path $RepoRoot 'cron\apply_task_security_descriptor.ps1'
& $Python (Join-Path $RepoRoot 'execution\generate_cron_artifacts.py') --project-root $RepoRoot --render-dir $renderDir --check
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& schtasks.exe /Create /TN '\earnings-summary\backfill_earnings_surprises' /XML (Join-Path $renderDir 'backfill_earnings_surprises.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\backfill_earnings_surprises' }
& $taskSecurityScript -TaskPath '\earnings-summary\backfill_earnings_surprises' -RenderedXmlPath (Join-Path $renderDir 'backfill_earnings_surprises.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\backfill_transcripts' /XML (Join-Path $renderDir 'backfill_transcripts.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\backfill_transcripts' }
& $taskSecurityScript -TaskPath '\earnings-summary\backfill_transcripts' -RenderedXmlPath (Join-Path $renderDir 'backfill_transcripts.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\backup_db' /XML (Join-Path $renderDir 'backup_db.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\backup_db' }
& $taskSecurityScript -TaskPath '\earnings-summary\backup_db' -RenderedXmlPath (Join-Path $renderDir 'backup_db.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\check_comp_set_drift' /XML (Join-Path $renderDir 'check_comp_set_drift.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\check_comp_set_drift' }
& $taskSecurityScript -TaskPath '\earnings-summary\check_comp_set_drift' -RenderedXmlPath (Join-Path $renderDir 'check_comp_set_drift.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\coach_pings' /XML (Join-Path $renderDir 'coach_pings.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\coach_pings' }
& $taskSecurityScript -TaskPath '\earnings-summary\coach_pings' -RenderedXmlPath (Join-Path $renderDir 'coach_pings.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\collect_operations_runtime_observations' /XML (Join-Path $renderDir 'collect_operations_runtime_observations.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\collect_operations_runtime_observations' }
& $taskSecurityScript -TaskPath '\earnings-summary\collect_operations_runtime_observations' -RenderedXmlPath (Join-Path $renderDir 'collect_operations_runtime_observations.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\daily_fetch_and_brief' /XML (Join-Path $renderDir 'daily_fetch_and_brief.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\daily_fetch_and_brief' }
& $taskSecurityScript -TaskPath '\earnings-summary\daily_fetch_and_brief' -RenderedXmlPath (Join-Path $renderDir 'daily_fetch_and_brief.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\db_gc' /XML (Join-Path $renderDir 'db_gc.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\db_gc' }
& $taskSecurityScript -TaskPath '\earnings-summary\db_gc' -RenderedXmlPath (Join-Path $renderDir 'db_gc.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\decision_nudge' /XML (Join-Path $renderDir 'decision_nudge.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\decision_nudge' }
& $taskSecurityScript -TaskPath '\earnings-summary\decision_nudge' -RenderedXmlPath (Join-Path $renderDir 'decision_nudge.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\disclosure_change_sweep' /XML (Join-Path $renderDir 'disclosure_change_sweep.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\disclosure_change_sweep' }
& $taskSecurityScript -TaskPath '\earnings-summary\disclosure_change_sweep' -RenderedXmlPath (Join-Path $renderDir 'disclosure_change_sweep.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\discover_ir_documents' /XML (Join-Path $renderDir 'discover_ir_documents.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\discover_ir_documents' }
& $taskSecurityScript -TaskPath '\earnings-summary\discover_ir_documents' -RenderedXmlPath (Join-Path $renderDir 'discover_ir_documents.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\discover_ir_failing' /XML (Join-Path $renderDir 'discover_ir_failing.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\discover_ir_failing' }
& $taskSecurityScript -TaskPath '\earnings-summary\discover_ir_failing' -RenderedXmlPath (Join-Path $renderDir 'discover_ir_failing.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\fetch_fmp_earnings_calendar' /XML (Join-Path $renderDir 'fetch_fmp_earnings_calendar.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\fetch_fmp_earnings_calendar' }
& $taskSecurityScript -TaskPath '\earnings-summary\fetch_fmp_earnings_calendar' -RenderedXmlPath (Join-Path $renderDir 'fetch_fmp_earnings_calendar.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\fetch_macro_series' /XML (Join-Path $renderDir 'fetch_macro_series.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\fetch_macro_series' }
& $taskSecurityScript -TaskPath '\earnings-summary\fetch_macro_series' -RenderedXmlPath (Join-Path $renderDir 'fetch_macro_series.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\fetch_sec_xbrl' /XML (Join-Path $renderDir 'fetch_sec_xbrl.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\fetch_sec_xbrl' }
& $taskSecurityScript -TaskPath '\earnings-summary\fetch_sec_xbrl' -RenderedXmlPath (Join-Path $renderDir 'fetch_sec_xbrl.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\grade_calibration' /XML (Join-Path $renderDir 'grade_calibration.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\grade_calibration' }
& $taskSecurityScript -TaskPath '\earnings-summary\grade_calibration' -RenderedXmlPath (Join-Path $renderDir 'grade_calibration.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\ledger_synthesis' /XML (Join-Path $renderDir 'ledger_synthesis.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\ledger_synthesis' }
& $taskSecurityScript -TaskPath '\earnings-summary\ledger_synthesis' -RenderedXmlPath (Join-Path $renderDir 'ledger_synthesis.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\model_eval_sweep' /XML (Join-Path $renderDir 'model_eval_sweep.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\model_eval_sweep' }
& $taskSecurityScript -TaskPath '\earnings-summary\model_eval_sweep' -RenderedXmlPath (Join-Path $renderDir 'model_eval_sweep.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\monthly_advisor_memos' /XML (Join-Path $renderDir 'monthly_advisor_memos.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\monthly_advisor_memos' }
& $taskSecurityScript -TaskPath '\earnings-summary\monthly_advisor_memos' -RenderedXmlPath (Join-Path $renderDir 'monthly_advisor_memos.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\monthly_calibration_scorecard' /XML (Join-Path $renderDir 'monthly_calibration_scorecard.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\monthly_calibration_scorecard' }
& $taskSecurityScript -TaskPath '\earnings-summary\monthly_calibration_scorecard' -RenderedXmlPath (Join-Path $renderDir 'monthly_calibration_scorecard.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\onboard_pending' /XML (Join-Path $renderDir 'onboard_pending_tickers.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\onboard_pending' }
& $taskSecurityScript -TaskPath '\earnings-summary\onboard_pending' -RenderedXmlPath (Join-Path $renderDir 'onboard_pending_tickers.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\portfolio_tracker_api' /XML (Join-Path $renderDir 'portfolio_tracker_api.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\portfolio_tracker_api' }
& $taskSecurityScript -TaskPath '\earnings-summary\portfolio_tracker_api' -RenderedXmlPath (Join-Path $renderDir 'portfolio_tracker_api.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\red_team' /XML (Join-Path $renderDir 'red_team.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\red_team' }
& $taskSecurityScript -TaskPath '\earnings-summary\red_team' -RenderedXmlPath (Join-Path $renderDir 'red_team.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\refresh_business_factors' /XML (Join-Path $renderDir 'refresh_business_factors.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\refresh_business_factors' }
& $taskSecurityScript -TaskPath '\earnings-summary\refresh_business_factors' -RenderedXmlPath (Join-Path $renderDir 'refresh_business_factors.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\refresh_cache' /XML (Join-Path $renderDir 'refresh_cache.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\refresh_cache' }
& $taskSecurityScript -TaskPath '\earnings-summary\refresh_cache' -RenderedXmlPath (Join-Path $renderDir 'refresh_cache.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\refresh_dirty_artifacts' /XML (Join-Path $renderDir 'refresh_dirty_artifacts.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\refresh_dirty_artifacts' }
& $taskSecurityScript -TaskPath '\earnings-summary\refresh_dirty_artifacts' -RenderedXmlPath (Join-Path $renderDir 'refresh_dirty_artifacts.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\refresh_ir_kpis' /XML (Join-Path $renderDir 'refresh_ir_kpis.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\refresh_ir_kpis' }
& $taskSecurityScript -TaskPath '\earnings-summary\refresh_ir_kpis' -RenderedXmlPath (Join-Path $renderDir 'refresh_ir_kpis.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\refresh_portfolio_tracker' /XML (Join-Path $renderDir 'refresh_portfolio_tracker.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\refresh_portfolio_tracker' }
& $taskSecurityScript -TaskPath '\earnings-summary\refresh_portfolio_tracker' -RenderedXmlPath (Join-Path $renderDir 'refresh_portfolio_tracker.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\refresh_scenario_priors' /XML (Join-Path $renderDir 'refresh_scenario_priors.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\refresh_scenario_priors' }
& $taskSecurityScript -TaskPath '\earnings-summary\refresh_scenario_priors' -RenderedXmlPath (Join-Path $renderDir 'refresh_scenario_priors.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\restore_drill' /XML (Join-Path $renderDir 'restore_drill.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\restore_drill' }
& $taskSecurityScript -TaskPath '\earnings-summary\restore_drill' -RenderedXmlPath (Join-Path $renderDir 'restore_drill.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\run_morning_pipeline' /XML (Join-Path $renderDir 'run_morning_pipeline.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\run_morning_pipeline' }
& $taskSecurityScript -TaskPath '\earnings-summary\run_morning_pipeline' -RenderedXmlPath (Join-Path $renderDir 'run_morning_pipeline.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\scan_ir_transcripts' /XML (Join-Path $renderDir 'scan_ir_transcripts.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\scan_ir_transcripts' }
& $taskSecurityScript -TaskPath '\earnings-summary\scan_ir_transcripts' -RenderedXmlPath (Join-Path $renderDir 'scan_ir_transcripts.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\senior_partner_brief' /XML (Join-Path $renderDir 'senior_partner_brief.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\senior_partner_brief' }
& $taskSecurityScript -TaskPath '\earnings-summary\senior_partner_brief' -RenderedXmlPath (Join-Path $renderDir 'senior_partner_brief.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\submit_saydo_batch' /XML (Join-Path $renderDir 'submit_saydo_batch.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\submit_saydo_batch' }
& $taskSecurityScript -TaskPath '\earnings-summary\submit_saydo_batch' -RenderedXmlPath (Join-Path $renderDir 'submit_saydo_batch.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\tenet_accountability' /XML (Join-Path $renderDir 'tenet_accountability.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\tenet_accountability' }
& $taskSecurityScript -TaskPath '\earnings-summary\tenet_accountability' -RenderedXmlPath (Join-Path $renderDir 'tenet_accountability.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\thesis_collision' /XML (Join-Path $renderDir 'thesis_collision.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\thesis_collision' }
& $taskSecurityScript -TaskPath '\earnings-summary\thesis_collision' -RenderedXmlPath (Join-Path $renderDir 'thesis_collision.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\track_comp_metrics' /XML (Join-Path $renderDir 'track_comp_metrics.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\track_comp_metrics' }
& $taskSecurityScript -TaskPath '\earnings-summary\track_comp_metrics' -RenderedXmlPath (Join-Path $renderDir 'track_comp_metrics.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\verify_cron' /XML (Join-Path $renderDir 'verify_cron.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\verify_cron' }
& $taskSecurityScript -TaskPath '\earnings-summary\verify_cron' -RenderedXmlPath (Join-Path $renderDir 'verify_cron.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\weekly_cleanup' /XML (Join-Path $renderDir 'weekly_cleanup.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\weekly_cleanup' }
& $taskSecurityScript -TaskPath '\earnings-summary\weekly_cleanup' -RenderedXmlPath (Join-Path $renderDir 'weekly_cleanup.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\weekly_p2_lens_refresh' /XML (Join-Path $renderDir 'weekly_p2_lens_refresh.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\weekly_p2_lens_refresh' }
& $taskSecurityScript -TaskPath '\earnings-summary\weekly_p2_lens_refresh' -RenderedXmlPath (Join-Path $renderDir 'weekly_p2_lens_refresh.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\weekly_packet' /XML (Join-Path $renderDir 'weekly_packet.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\weekly_packet' }
& $taskSecurityScript -TaskPath '\earnings-summary\weekly_packet' -RenderedXmlPath (Join-Path $renderDir 'weekly_packet.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\weekly_score_stances' /XML (Join-Path $renderDir 'weekly_score_stances.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\weekly_score_stances' }
& $taskSecurityScript -TaskPath '\earnings-summary\weekly_score_stances' -RenderedXmlPath (Join-Path $renderDir 'weekly_score_stances.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\weekly_synthesis' /XML (Join-Path $renderDir 'weekly_synthesis.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\weekly_synthesis' }
& $taskSecurityScript -TaskPath '\earnings-summary\weekly_synthesis' -RenderedXmlPath (Join-Path $renderDir 'weekly_synthesis.task.xml')
& schtasks.exe /Create /TN '\earnings-summary\weekly_validation' /XML (Join-Path $renderDir 'weekly_validation.task.xml') /F
if ($LASTEXITCODE -ne 0) { throw 'Failed to register scheduled task \earnings-summary\weekly_validation' }
& $taskSecurityScript -TaskPath '\earnings-summary\weekly_validation' -RenderedXmlPath (Join-Path $renderDir 'weekly_validation.task.xml')
