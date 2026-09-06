# Generated from cron/task_manifest.json. Do not edit by hand.
param(
    [Parameter(Mandatory=$true)][string]$Python,
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path,
    [PSCredential]$BackgroundCredential
)
$ErrorActionPreference = 'Stop'
$renderDir = Join-Path $RepoRoot '.tmp\scheduler_tasks'
$taskSecurityScript = Join-Path $RepoRoot 'cron\apply_task_security_descriptor.ps1'
& $Python (Join-Path $RepoRoot 'execution\generate_cron_artifacts.py') --project-root $RepoRoot --render-dir $renderDir --check
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$rollbackDir = Join-Path $RepoRoot ('.tmp\scheduler_rollback\' + (Get-Date -Format 'yyyyMMddTHHmmss'))
New-Item -ItemType Directory -Path $rollbackDir -Force | Out-Null
function Register-ManifestTask {
    param([string]$TaskName, [string]$TaskPath, [string]$XmlPath, [switch]$System)
    if ($System -or -not $BackgroundCredential) {
        & schtasks.exe /Create /TN ($TaskPath + $TaskName) /XML $XmlPath /F
        if ($LASTEXITCODE -ne 0) { throw "Failed to register scheduled task $TaskPath$TaskName" }
        return
    }
    $password = $BackgroundCredential.GetNetworkCredential().Password
    try {
        Register-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Xml (Get-Content -LiteralPath $XmlPath -Raw) -User $BackgroundCredential.UserName -Password $password -Force | Out-Null
    } finally {
        $password = $null
    }
}
$existing = Get-ScheduledTask -TaskName 'backfill_earnings_surprises' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'backfill_earnings_surprises' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'backfill_earnings_surprises.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'backfill_earnings_surprises' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'backfill_earnings_surprises.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\backfill_earnings_surprises' -RenderedXmlPath (Join-Path $renderDir 'backfill_earnings_surprises.task.xml')
$existing = Get-ScheduledTask -TaskName 'backfill_transcripts' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'backfill_transcripts' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'backfill_transcripts.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'backfill_transcripts' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'backfill_transcripts.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\backfill_transcripts' -RenderedXmlPath (Join-Path $renderDir 'backfill_transcripts.task.xml')
$existing = Get-ScheduledTask -TaskName 'backup_db' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'backup_db' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'backup_db.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'backup_db' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'backup_db.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\backup_db' -RenderedXmlPath (Join-Path $renderDir 'backup_db.task.xml')
$existing = Get-ScheduledTask -TaskName 'check_comp_set_drift' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'check_comp_set_drift' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'check_comp_set_drift.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'check_comp_set_drift' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'check_comp_set_drift.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\check_comp_set_drift' -RenderedXmlPath (Join-Path $renderDir 'check_comp_set_drift.task.xml')
$existing = Get-ScheduledTask -TaskName 'coach_pings' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'coach_pings' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'coach_pings.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'coach_pings' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'coach_pings.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\coach_pings' -RenderedXmlPath (Join-Path $renderDir 'coach_pings.task.xml')
$existing = Get-ScheduledTask -TaskName 'collect_operations_runtime_observations' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'collect_operations_runtime_observations' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'collect_operations_runtime_observations.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'collect_operations_runtime_observations' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'collect_operations_runtime_observations.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\collect_operations_runtime_observations' -RenderedXmlPath (Join-Path $renderDir 'collect_operations_runtime_observations.task.xml')
$existing = Get-ScheduledTask -TaskName 'daily_fetch_and_brief' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'daily_fetch_and_brief' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'daily_fetch_and_brief.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'daily_fetch_and_brief' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'daily_fetch_and_brief.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\daily_fetch_and_brief' -RenderedXmlPath (Join-Path $renderDir 'daily_fetch_and_brief.task.xml')
$existing = Get-ScheduledTask -TaskName 'db_gc' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'db_gc' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'db_gc.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'db_gc' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'db_gc.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\db_gc' -RenderedXmlPath (Join-Path $renderDir 'db_gc.task.xml')
$existing = Get-ScheduledTask -TaskName 'decision_nudge' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'decision_nudge' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'decision_nudge.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'decision_nudge' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'decision_nudge.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\decision_nudge' -RenderedXmlPath (Join-Path $renderDir 'decision_nudge.task.xml')
$existing = Get-ScheduledTask -TaskName 'disclosure_change_sweep' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'disclosure_change_sweep' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'disclosure_change_sweep.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'disclosure_change_sweep' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'disclosure_change_sweep.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\disclosure_change_sweep' -RenderedXmlPath (Join-Path $renderDir 'disclosure_change_sweep.task.xml')
$existing = Get-ScheduledTask -TaskName 'discover_ir_documents' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'discover_ir_documents' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'discover_ir_documents.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'discover_ir_documents' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'discover_ir_documents.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\discover_ir_documents' -RenderedXmlPath (Join-Path $renderDir 'discover_ir_documents.task.xml')
$existing = Get-ScheduledTask -TaskName 'discover_ir_failing' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'discover_ir_failing' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'discover_ir_failing.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'discover_ir_failing' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'discover_ir_failing.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\discover_ir_failing' -RenderedXmlPath (Join-Path $renderDir 'discover_ir_failing.task.xml')
$existing = Get-ScheduledTask -TaskName 'fetch_fmp_earnings_calendar' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'fetch_fmp_earnings_calendar' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'fetch_fmp_earnings_calendar.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'fetch_fmp_earnings_calendar' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'fetch_fmp_earnings_calendar.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\fetch_fmp_earnings_calendar' -RenderedXmlPath (Join-Path $renderDir 'fetch_fmp_earnings_calendar.task.xml')
$existing = Get-ScheduledTask -TaskName 'fetch_macro_series' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'fetch_macro_series' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'fetch_macro_series.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'fetch_macro_series' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'fetch_macro_series.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\fetch_macro_series' -RenderedXmlPath (Join-Path $renderDir 'fetch_macro_series.task.xml')
$existing = Get-ScheduledTask -TaskName 'fetch_sec_xbrl' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'fetch_sec_xbrl' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'fetch_sec_xbrl.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'fetch_sec_xbrl' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'fetch_sec_xbrl.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\fetch_sec_xbrl' -RenderedXmlPath (Join-Path $renderDir 'fetch_sec_xbrl.task.xml')
$existing = Get-ScheduledTask -TaskName 'grade_calibration' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'grade_calibration' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'grade_calibration.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'grade_calibration' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'grade_calibration.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\grade_calibration' -RenderedXmlPath (Join-Path $renderDir 'grade_calibration.task.xml')
$existing = Get-ScheduledTask -TaskName 'ledger_synthesis' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'ledger_synthesis' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'ledger_synthesis.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'ledger_synthesis' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'ledger_synthesis.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\ledger_synthesis' -RenderedXmlPath (Join-Path $renderDir 'ledger_synthesis.task.xml')
$existing = Get-ScheduledTask -TaskName 'model_eval_sweep' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'model_eval_sweep' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'model_eval_sweep.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'model_eval_sweep' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'model_eval_sweep.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\model_eval_sweep' -RenderedXmlPath (Join-Path $renderDir 'model_eval_sweep.task.xml')
$existing = Get-ScheduledTask -TaskName 'monthly_advisor_memos' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'monthly_advisor_memos' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'monthly_advisor_memos.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'monthly_advisor_memos' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'monthly_advisor_memos.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\monthly_advisor_memos' -RenderedXmlPath (Join-Path $renderDir 'monthly_advisor_memos.task.xml')
$existing = Get-ScheduledTask -TaskName 'monthly_calibration_scorecard' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'monthly_calibration_scorecard' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'monthly_calibration_scorecard.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'monthly_calibration_scorecard' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'monthly_calibration_scorecard.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\monthly_calibration_scorecard' -RenderedXmlPath (Join-Path $renderDir 'monthly_calibration_scorecard.task.xml')
$existing = Get-ScheduledTask -TaskName 'onboard_pending' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'onboard_pending' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'onboard_pending_tickers.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'onboard_pending' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'onboard_pending_tickers.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\onboard_pending' -RenderedXmlPath (Join-Path $renderDir 'onboard_pending_tickers.task.xml')
$existing = Get-ScheduledTask -TaskName 'portfolio_tracker_api' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'portfolio_tracker_api' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'portfolio_tracker_api.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'portfolio_tracker_api' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'portfolio_tracker_api.task.xml') -System
& $taskSecurityScript -TaskPath '\earnings-summary\portfolio_tracker_api' -RenderedXmlPath (Join-Path $renderDir 'portfolio_tracker_api.task.xml')
$existing = Get-ScheduledTask -TaskName 'prepare_kpi_semantic_review' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'prepare_kpi_semantic_review' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'prepare_kpi_semantic_review.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'prepare_kpi_semantic_review' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'prepare_kpi_semantic_review.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\prepare_kpi_semantic_review' -RenderedXmlPath (Join-Path $renderDir 'prepare_kpi_semantic_review.task.xml')
$existing = Get-ScheduledTask -TaskName 'red_team' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'red_team' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'red_team.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'red_team' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'red_team.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\red_team' -RenderedXmlPath (Join-Path $renderDir 'red_team.task.xml')
$existing = Get-ScheduledTask -TaskName 'refresh_business_factors' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'refresh_business_factors' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'refresh_business_factors.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'refresh_business_factors' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'refresh_business_factors.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\refresh_business_factors' -RenderedXmlPath (Join-Path $renderDir 'refresh_business_factors.task.xml')
$existing = Get-ScheduledTask -TaskName 'refresh_cache' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'refresh_cache' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'refresh_cache.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'refresh_cache' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'refresh_cache.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\refresh_cache' -RenderedXmlPath (Join-Path $renderDir 'refresh_cache.task.xml')
$existing = Get-ScheduledTask -TaskName 'refresh_dirty_artifacts' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'refresh_dirty_artifacts' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'refresh_dirty_artifacts.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'refresh_dirty_artifacts' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'refresh_dirty_artifacts.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\refresh_dirty_artifacts' -RenderedXmlPath (Join-Path $renderDir 'refresh_dirty_artifacts.task.xml')
$existing = Get-ScheduledTask -TaskName 'refresh_ir_kpis' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'refresh_ir_kpis' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'refresh_ir_kpis.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'refresh_ir_kpis' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'refresh_ir_kpis.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\refresh_ir_kpis' -RenderedXmlPath (Join-Path $renderDir 'refresh_ir_kpis.task.xml')
$existing = Get-ScheduledTask -TaskName 'refresh_portfolio_tracker' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'refresh_portfolio_tracker' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'refresh_portfolio_tracker.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'refresh_portfolio_tracker' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'refresh_portfolio_tracker.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\refresh_portfolio_tracker' -RenderedXmlPath (Join-Path $renderDir 'refresh_portfolio_tracker.task.xml')
$existing = Get-ScheduledTask -TaskName 'refresh_scenario_priors' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'refresh_scenario_priors' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'refresh_scenario_priors.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'refresh_scenario_priors' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'refresh_scenario_priors.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\refresh_scenario_priors' -RenderedXmlPath (Join-Path $renderDir 'refresh_scenario_priors.task.xml')
$existing = Get-ScheduledTask -TaskName 'restore_drill' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'restore_drill' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'restore_drill.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'restore_drill' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'restore_drill.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\restore_drill' -RenderedXmlPath (Join-Path $renderDir 'restore_drill.task.xml')
$existing = Get-ScheduledTask -TaskName 'run_morning_pipeline' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'run_morning_pipeline' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'run_morning_pipeline.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'run_morning_pipeline' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'run_morning_pipeline.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\run_morning_pipeline' -RenderedXmlPath (Join-Path $renderDir 'run_morning_pipeline.task.xml')
$existing = Get-ScheduledTask -TaskName 'scan_ir_transcripts' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'scan_ir_transcripts' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'scan_ir_transcripts.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'scan_ir_transcripts' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'scan_ir_transcripts.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\scan_ir_transcripts' -RenderedXmlPath (Join-Path $renderDir 'scan_ir_transcripts.task.xml')
$existing = Get-ScheduledTask -TaskName 'senior_partner_brief' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'senior_partner_brief' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'senior_partner_brief.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'senior_partner_brief' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'senior_partner_brief.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\senior_partner_brief' -RenderedXmlPath (Join-Path $renderDir 'senior_partner_brief.task.xml')
$existing = Get-ScheduledTask -TaskName 'submit_saydo_batch' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'submit_saydo_batch' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'submit_saydo_batch.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'submit_saydo_batch' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'submit_saydo_batch.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\submit_saydo_batch' -RenderedXmlPath (Join-Path $renderDir 'submit_saydo_batch.task.xml')
$existing = Get-ScheduledTask -TaskName 'tenet_accountability' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'tenet_accountability' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'tenet_accountability.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'tenet_accountability' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'tenet_accountability.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\tenet_accountability' -RenderedXmlPath (Join-Path $renderDir 'tenet_accountability.task.xml')
$existing = Get-ScheduledTask -TaskName 'thesis_collision' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'thesis_collision' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'thesis_collision.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'thesis_collision' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'thesis_collision.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\thesis_collision' -RenderedXmlPath (Join-Path $renderDir 'thesis_collision.task.xml')
$existing = Get-ScheduledTask -TaskName 'track_comp_metrics' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'track_comp_metrics' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'track_comp_metrics.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'track_comp_metrics' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'track_comp_metrics.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\track_comp_metrics' -RenderedXmlPath (Join-Path $renderDir 'track_comp_metrics.task.xml')
$existing = Get-ScheduledTask -TaskName 'verify_cron' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'verify_cron' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'verify_cron.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'verify_cron' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'verify_cron.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\verify_cron' -RenderedXmlPath (Join-Path $renderDir 'verify_cron.task.xml')
$existing = Get-ScheduledTask -TaskName 'weekly_cleanup' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'weekly_cleanup' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'weekly_cleanup.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'weekly_cleanup' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'weekly_cleanup.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\weekly_cleanup' -RenderedXmlPath (Join-Path $renderDir 'weekly_cleanup.task.xml')
$existing = Get-ScheduledTask -TaskName 'weekly_p2_lens_refresh' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'weekly_p2_lens_refresh' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'weekly_p2_lens_refresh.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'weekly_p2_lens_refresh' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'weekly_p2_lens_refresh.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\weekly_p2_lens_refresh' -RenderedXmlPath (Join-Path $renderDir 'weekly_p2_lens_refresh.task.xml')
$existing = Get-ScheduledTask -TaskName 'weekly_packet' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'weekly_packet' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'weekly_packet.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'weekly_packet' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'weekly_packet.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\weekly_packet' -RenderedXmlPath (Join-Path $renderDir 'weekly_packet.task.xml')
$existing = Get-ScheduledTask -TaskName 'weekly_score_stances' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'weekly_score_stances' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'weekly_score_stances.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'weekly_score_stances' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'weekly_score_stances.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\weekly_score_stances' -RenderedXmlPath (Join-Path $renderDir 'weekly_score_stances.task.xml')
$existing = Get-ScheduledTask -TaskName 'weekly_synthesis' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'weekly_synthesis' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'weekly_synthesis.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'weekly_synthesis' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'weekly_synthesis.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\weekly_synthesis' -RenderedXmlPath (Join-Path $renderDir 'weekly_synthesis.task.xml')
$existing = Get-ScheduledTask -TaskName 'weekly_validation' -TaskPath '\earnings-summary\' -ErrorAction SilentlyContinue
if ($existing) { Export-ScheduledTask -TaskName 'weekly_validation' -TaskPath '\earnings-summary\' | Set-Content -LiteralPath (Join-Path $rollbackDir 'weekly_validation.task.xml') -Encoding Unicode }
Register-ManifestTask -TaskName 'weekly_validation' -TaskPath '\earnings-summary\' -XmlPath (Join-Path $renderDir 'weekly_validation.task.xml')
& $taskSecurityScript -TaskPath '\earnings-summary\weekly_validation' -RenderedXmlPath (Join-Path $renderDir 'weekly_validation.task.xml')
if ($BackgroundCredential) {
    $remaining = @(Get-ScheduledTask -TaskPath '\earnings-summary\' | Where-Object { $_.State -ne 'Disabled' -and $_.TaskName -ne 'portfolio_tracker_api' -and $_.Principal.LogonType -ne 'Password' })
    if ($remaining.Count -ne 0) { throw "Background registration verification failed for $($remaining.Count) enabled task(s)" }
}
Write-Host "Scheduler rollback XML: $rollbackDir"
