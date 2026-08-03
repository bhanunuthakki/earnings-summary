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
To skip the ~12 GB re-pullable FMP cache, add 'data\historical\fmp' to $xd below.
#>
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$py   = Join-Path $repo 'venv\Scripts\python.exe'
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
& $py (Join-Path $repo 'cron\backup_db.py')
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
    (Join-Path $repo 'data\secrets')
)
$xf = @('*.pyc', '*.db', '*.db-wal', '*.db-shm', 'portfolio.db.bak*', '.env', '.env.*', 'credentials.json', 'token.json', '*credential*', '*secret*', '*.pem', '*.key', '*.pfx', '*.p12')
robocopy $repo $dst /MIR /XD $xd /XF $xf /R:1 /W:1 /NP /NFL /NDL
if ($LASTEXITCODE -ge 8) {
    throw "robocopy ERROR (exit $LASTEXITCODE)"
}
Write-Host "mirror OK (robocopy exit $LASTEXITCODE)"
