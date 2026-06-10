<#
Large-aperture backup of the earnings-summary project to Google Drive.

Backs up the WHOLE working tree EXCEPT the parts that caused Drive-sync
corruption (.git, the live SQLite DB + -wal/-shm) plus regenerable bulk
(venv, __pycache__, caches/logs). The live DB is captured separately as a
CONSISTENT gzipped snapshot via cron\backup_db.py.

Everything mirrored is static files, so Google Drive syncs the destination with
zero corruption risk (this is what the live .git/.db could not do safely).

Run once now:
    powershell -ExecutionPolicy Bypass -File cron\backup_project.ps1

Destination defaults to a Google Drive folder; override with ES_BACKUP_ROOT.
To skip the ~12 GB re-pullable FMP cache, add 'data\historical\fmp' to $xd below.
#>
$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent $PSScriptRoot
$py   = Join-Path $repo 'venv\Scripts\python.exe'
$root = if ($env:ES_BACKUP_ROOT) { $env:ES_BACKUP_ROOT } else { 'C:\Users\Bhanu\My Drive\earnings-summary-backup' }

Write-Host "=== 1/2  consistent DB snapshot (gzipped) ==="
& $py (Join-Path $repo 'cron\backup_db.py')

Write-Host "=== 2/2  mirror working tree -> $root\tree ==="
$dst = Join-Path $root 'tree'
# Exclude only: corruption-prone live files + regenerable bulk.
$xd = @('.git', 'venv', '.tmp', '.cache', 'cache', 'logs', '__pycache__', '.pytest_cache', '.claude', 'node_modules')
$xf = @('*.pyc', '*.db', '*.db-wal', '*.db-shm', 'portfolio.db.bak*')
robocopy $repo $dst /MIR /XD $xd /XF $xf /R:1 /W:1 /NP /NFL /NDL
if ($LASTEXITCODE -ge 8) { Write-Host "robocopy ERROR (exit $LASTEXITCODE)" } else { Write-Host "mirror OK (robocopy exit $LASTEXITCODE)" }
