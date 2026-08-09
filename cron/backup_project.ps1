<#
Large-aperture backup of the earnings-summary project to Google Drive.

Backs up the WHOLE working tree EXCEPT the parts that caused Drive-sync
corruption (.git, the live SQLite DB + -wal/-shm) plus regenerable bulk
(venv, __pycache__, caches/logs). The live DB is captured separately as a
CONSISTENT encrypted snapshot via cron\backup_db.py.

Everything mirrored is static files, so Google Drive syncs the destination with
zero corruption risk (this is what the live .git/.db could not do safely).

Run once now:
    powershell -ExecutionPolicy Bypass -File cron\backup_project.ps1

Destination defaults to a Google Drive folder; override with ES_BACKUP_ROOT.
The re-pullable FMP cache (data\historical\fmp) is EXCLUDED via $xd: mirroring
it produced 153k files that Drive could only upload 14k of, leaving the backup
~9% complete with no error anywhere. Do not re-add it without a plan for the
upload backlog.
#>
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$runPython = Join-Path $repo 'cron\run_python.bat'
# Drive root works in either sync mode: in Stream mode Drive mounts a virtual
# drive (usually G:) and the old C:\...\My Drive folder lingers on disk stale
# and UNSYNCED, so any mounted "<letter>:\My Drive" must win over the mirror
# path. Mirrors _google_drive_root() in cron/backup_db.py / cron/restore_db.py.
$driveRoot = 'C:\Users\Bhanu\My Drive'
foreach ($l in [char[]]('DEFGHIJKLMNOPQRSTUVWXYZ')) {
    if (Test-Path -LiteralPath "$($l):\My Drive") { $driveRoot = "$($l):\My Drive"; break }
}
$root = if ($env:ES_BACKUP_ROOT) { $env:ES_BACKUP_ROOT } else { Join-Path $driveRoot 'earnings-summary-backup' }

Write-Host "=== 1/2  consistent DB snapshot (AES-256-GCM encrypted) ==="
& $runPython 'backup_project_db' 'db-backup' 'cron\backup_db.py'
if ($LASTEXITCODE -ne 0) {
    throw "Database backup failed with exit code $LASTEXITCODE; project mirror aborted."
}

Write-Host "=== 2/2  mirror working tree -> $root\tree ==="
$dst = Join-Path $root 'tree'
# Credentials are deliberately excluded even though this is a private Drive
# mirror: project backups must never duplicate secret-bearing source files.
# The consistent DB snapshot remains a separate, recoverable artifact.
$xd = @(
    '.git', 'venv', '.tmp', '.cache', 'cache', 'logs', '__pycache__',
    '.pytest_cache', '.claude', 'node_modules',
    (Join-Path $repo 'data\llm_capture'),
    (Join-Path $repo 'data\secrets'),
    # Re-pullable FMP API cache. Measured 2026-08-02: mirroring it made this job
    # emit 153,347 files, of which Google Drive had uploaded only 14,459 — the
    # off-machine copy sat ~9% complete and said nothing, because robocopy
    # succeeded locally and Drive's backlog is invisible from here. Excluding it
    # drops the job to ~14k files so the upload can actually finish. Every file
    # here is re-fetchable from FMP; the irreplaceable state is the DB snapshot,
    # which backup_db.py handles separately.
    (Join-Path $repo 'data\historical\fmp')
)
$xf = @('*.pyc', '*.db', '*.db-wal', '*.db-shm', 'portfolio.db.bak*', '.env', '.env.*', 'credentials.json', 'token.json', '*credential*', '*secret*', '*.pem', '*.key', '*.pfx', '*.p12')
robocopy $repo $dst /MIR /XD $xd /XF $xf /R:1 /W:1 /NP /NFL /NDL
if ($LASTEXITCODE -ge 8) {
    throw "robocopy ERROR (exit $LASTEXITCODE)"
}
Write-Host "mirror OK (robocopy exit $LASTEXITCODE)"
