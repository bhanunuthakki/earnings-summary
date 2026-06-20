<#
Monthly project backup of earnings-summary, delivered via the Drive-synced
Downloads folder.

The `scratch` tree is intentionally NOT continuously synced to Google Drive
(continuous sync corrupted git/worktrees). The Downloads folder IS Drive-synced.
So once a month this produces ONE compressed archive of the important data and
drops it into Downloads, where Drive uploads a single file instead of watching
~200k repo files live.

WHAT IT DOES
  1. Takes a CONSISTENT snapshot of data/portfolio.db (SQLite online backup API,
     via cron/snapshot_db.py) so a mid-write live DB is never torn.
  2. Streams a Zip64 archive of the project (.NET ZipArchive, NOT Compress-Archive
     which is capped at 2 GB in PowerShell 5.1) into a LOCAL staging dir.
  3. HARD-ABORTS if any archive entry looks like a credential (belt-and-suspenders
     on top of the up-front exclude list) so secrets never reach the cloud.
  4. Atomically moves the finished zip into Downloads, OVERWRITING last month's
     file (single stable name; a failed run never clobbers the prior good backup).
  5. Enforces retention (default keep 1) and writes a manifest + run log.

SCOPE
  full (default) : whole project incl. the re-fetchable FMP lake + ir_documents
                   + transcripts (~8 GB zip).
  lean           : excludes data/historical, ir_documents, transcripts (fast;
                   used for quick verification).

Run manually:
  powershell -ExecutionPolicy Bypass -File cron\backup_es_monthly.ps1
  powershell -ExecutionPolicy Bypass -File cron\backup_es_monthly.ps1 -Scope lean

Env overrides: ES_REPO_ROOT, ES_BACKUP_DEST, ES_BACKUP_SCOPE, ES_BACKUP_NAME,
ES_BACKUP_RETAIN.
#>
[CmdletBinding()]
param(
    [ValidateSet('full', 'lean')] [string]$Scope,
    [string]$RepoRoot,
    [string]$Dest,
    [string]$Name,
    [int]$Retain
)

$ErrorActionPreference = 'Stop'
$startedAt = Get-Date

# ---- configuration (param > env > default) ---------------------------------
if (-not $RepoRoot) { $RepoRoot = $env:ES_REPO_ROOT }
if (-not $RepoRoot) { $RepoRoot = Join-Path $env:USERPROFILE '.gemini\antigravity\scratch\earnings-summary' }
if (-not $Scope)    { $Scope = $env:ES_BACKUP_SCOPE }
if (-not $Scope)    { $Scope = 'full' }
if (-not $Dest)     { $Dest = $env:ES_BACKUP_DEST }
if (-not $Dest)     { $Dest = Join-Path $env:USERPROFILE 'Downloads\earnings-summary-backups' }
if (-not $Name)     { $Name = $env:ES_BACKUP_NAME }
if (-not $Name)     { $Name = 'earnings-summary-backup' }
if (-not $Retain -or $Retain -lt 1) {
    $Retain = if ($env:ES_BACKUP_RETAIN) { [int]$env:ES_BACKUP_RETAIN } else { 1 }
}

$RepoRoot = [System.IO.Path]::GetFullPath($RepoRoot)
$Scope    = $Scope.ToLowerInvariant()

# venv lives only in the data-root (main repo); the snapshot helper is co-located
# with THIS script (so verification from a worktree finds it, and so does main).
$py        = Join-Path $RepoRoot 'venv\Scripts\python.exe'
$snapTool  = Join-Path $PSScriptRoot 'snapshot_db.py'
$srcDb     = Join-Path $RepoRoot 'data\portfolio.db'
$stageDir  = Join-Path $RepoRoot '.tmp\backup_staging'
$logDir    = Join-Path $RepoRoot '.tmp\cron_logs'
$historyLog = Join-Path $logDir 'backup_es_monthly_history.log'

function Write-Log { param([string]$Msg) Write-Host ("[{0}] {1}" -f (Get-Date).ToString('HH:mm:ss'), $Msg) }
function Fail { param([string]$Msg) Write-Host "ERROR: $Msg"; exit 1 }

Write-Host "=== earnings-summary monthly backup ==="
Write-Log  "repo   : $RepoRoot"
Write-Log  "scope  : $Scope"
Write-Log  "dest   : $Dest"
Write-Log  "retain : $Retain"

if (-not (Test-Path -LiteralPath $RepoRoot)) { Fail "repo root not found: $RepoRoot" }
if (-not (Test-Path -LiteralPath $py))       { Fail "venv python not found: $py" }
if (-not (Test-Path -LiteralPath $srcDb))    { Fail "portfolio.db not found: $srcDb" }

New-Item -ItemType Directory -Force -Path $stageDir, $logDir, $Dest | Out-Null

# ---- 1. consistent DB snapshot ---------------------------------------------
$dbSnap = Join-Path $stageDir 'portfolio.db.snapshot'
if (Test-Path -LiteralPath $dbSnap) { Remove-Item -LiteralPath $dbSnap -Force }
Write-Log "snapshotting portfolio.db (online backup API) ..."
& $py $snapTool $srcDb $dbSnap
if ($LASTEXITCODE -ne 0) { Fail "DB snapshot failed (exit $LASTEXITCODE)" }
if (-not (Test-Path -LiteralPath $dbSnap)) { Fail "DB snapshot did not produce a file" }
$dbSnapMb = (Get-Item -LiteralPath $dbSnap).Length / 1MB
Write-Log ("DB snapshot OK ({0:N1} MB)" -f $dbSnapMb)

# ---- 2. build the include/exclude rules ------------------------------------
$excludeDirNames = @(
    'venv', '.git', '.tmp', 'output', '__pycache__', '.ruff_cache', '.pytest_cache',
    '.mypy_cache', '.claude', 'node_modules', '.cache', 'cache', 'logs', 'secrets', '_inbox'
)
# Relative dirs (from repo root) excluded outright.
$excludeRelDirs = New-Object System.Collections.Generic.HashSet[string] ([System.StringComparer]::OrdinalIgnoreCase)
[void]$excludeRelDirs.Add('data\secrets')
if ($Scope -eq 'lean') {
    [void]$excludeRelDirs.Add('data\historical')
    [void]$excludeRelDirs.Add('ir_documents')
    [void]$excludeRelDirs.Add('transcripts')
}
# Filename globs always dropped (regenerable bulk + raw/torn DBs + credentials).
$excludeFileGlobs = @(
    '*.pyc', '*.db', '*.db-wal', '*.db-shm', '*.db-journal', 'portfolio.db.*', '*.bak',
    'Thumbs.db', '.DS_Store',
    '*.pem', '*.key', '*.p12', '*.pfx', '*.pkcs12', '*.jks', '*.keystore', '*.ppk', '*.netrc',
    'gsheets_*', 'credentials.json', 'token.json', 'client_secret*', 'service_account*',
    '.env', '.env.*'
)
# Keyword+extension credential rule (avoids dropping source like ui/tokens.py).
$secretKeywords = @('secret', 'token', 'credential', 'password', 'apikey', 'api_key')
$credExts = New-Object System.Collections.Generic.HashSet[string] ([System.StringComparer]::OrdinalIgnoreCase)
'.json', '.txt', '.yaml', '.yml', '.ini', '.cfg', '.conf', '.xml', '.env', '.csv', '.tsv',
'.b64', '.pkl', '.pickle', '.pem', '.key', '.secret', '.credentials' | ForEach-Object { [void]$credExts.Add($_) }

# Store (no recompress) already-compressed formats.
$storeExts = New-Object System.Collections.Generic.HashSet[string] ([System.StringComparer]::OrdinalIgnoreCase)
'.pdf', '.zip', '.gz', '.gzip', '.tgz', '.7z', '.rar', '.xz', '.bz2', '.zst', '.br',
'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.ico', '.mp3', '.mp4', '.mov', '.avi',
'.mkv', '.m4a', '.aac', '.ogg', '.flac', '.xlsx', '.xlsm', '.docx', '.pptx', '.parquet',
'.woff', '.woff2' | ForEach-Object { [void]$storeExts.Add($_) }

function Test-MatchesGlob {
    param([string]$Name, [string[]]$Globs)
    foreach ($g in $Globs) { if ($Name -like $g) { return $true } }
    return $false
}
function Test-IsExcludedFile {
    param([string]$Name)
    if (Test-MatchesGlob $Name $excludeFileGlobs) { return $true }
    $lower = $Name.ToLowerInvariant()
    foreach ($kw in $secretKeywords) {
        if ($lower.Contains($kw)) {
            $ext = [System.IO.Path]::GetExtension($Name)
            if ([string]::IsNullOrEmpty($ext) -or $credExts.Contains($ext)) { return $true }
        }
    }
    return $false
}

# ---- 3. prune-walk the tree to the include list ----------------------------
Write-Log "walking tree ..."
$files = New-Object System.Collections.Generic.List[string]
$walkErrors = New-Object System.Collections.Generic.List[string]
$stack = New-Object System.Collections.Generic.Stack[string]
$stack.Push($RepoRoot)
$rootLen = $RepoRoot.Length
while ($stack.Count -gt 0) {
    $dir = $stack.Pop()
    $entries = $null
    try { $entries = [System.IO.Directory]::EnumerateFileSystemEntries($dir) }
    catch { $walkErrors.Add("enumerate failed: $dir :: $($_.Exception.Message)"); continue }
    foreach ($entry in $entries) {
        $base = [System.IO.Path]::GetFileName($entry)
        $isDir = $false
        try { $isDir = [System.IO.Directory]::Exists($entry) } catch { $isDir = $false }
        if ($isDir) {
            if ($excludeDirNames -contains $base) { continue }
            $rel = $entry.Substring($rootLen).TrimStart('\')
            if ($excludeRelDirs.Contains($rel)) { continue }
            $stack.Push($entry)
        }
        else {
            if (Test-IsExcludedFile $base) { continue }
            $files.Add($entry)
        }
    }
}
Write-Log ("included files: {0}  (walk errors: {1})" -f $files.Count, $walkErrors.Count)

# ---- 4. stream the archive into staging ------------------------------------
Add-Type -AssemblyName System.IO.Compression | Out-Null
Add-Type -AssemblyName System.IO.Compression.FileSystem | Out-Null

$stageZip = Join-Path $stageDir ("{0}.building.zip" -f $Name)
if (Test-Path -LiteralPath $stageZip) { Remove-Item -LiteralPath $stageZip -Force }

function ConvertTo-LongPath {
    param([string]$Path)
    if ($Path.StartsWith('\\?\')) { return $Path }
    if ($Path.StartsWith('\\'))   { return '\\?\UNC\' + $Path.Substring(2) }
    return '\\?\' + $Path
}

$opt   = [System.IO.Compression.CompressionLevel]::Optimal
$store = [System.IO.Compression.CompressionLevel]::NoCompression
$added = 0
$skips = New-Object System.Collections.Generic.List[string]
$uncompressed = [long]0

$fs = [System.IO.File]::Open($stageZip, [System.IO.FileMode]::Create)
$zip = New-Object System.IO.Compression.ZipArchive($fs, [System.IO.Compression.ZipArchiveMode]::Create)
try {
    # the consistent DB snapshot, stored at its real repo-relative path
    [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $dbSnap, 'data/portfolio.db', $opt)
    $uncompressed += (Get-Item -LiteralPath $dbSnap).Length
    $added++

    foreach ($file in $files) {
        $entryName = $file.Substring($rootLen).TrimStart('\').Replace('\', '/')
        $ext = [System.IO.Path]::GetExtension($file)
        $level = if ($storeExts.Contains($ext)) { $store } else { $opt }
        try {
            [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, (ConvertTo-LongPath $file), $entryName, $level)
            $uncompressed += (Get-Item -LiteralPath (ConvertTo-LongPath $file)).Length
            $added++
            if (($added % 5000) -eq 0) { Write-Log ("  ... {0} entries" -f $added) }
        }
        catch { $skips.Add("$entryName :: $($_.Exception.Message)") }
    }
}
finally {
    $zip.Dispose()
    $fs.Dispose()
}
Write-Log ("archive built: {0} entries, {1} skipped" -f $added, $skips.Count)

# ---- 5. HARD secret guard: refuse to publish a credential-shaped entry ------
function Test-IsCredentialEntry {
    param([string]$EntryPath)  # forward-slash, repo-relative
    $lower = $EntryPath.ToLowerInvariant()
    $segs = $lower.Split('/')
    if ($segs -contains 'secrets') { return $true }
    $base = $segs[-1]
    if ($base -eq '.env' -or $base.StartsWith('.env.')) { return $true }
    if ($base -in @('credentials.json', 'token.json')) { return $true }
    if ($base -like 'gsheets_*' -or $base -like 'client_secret*' -or $base -like 'service_account*') { return $true }
    $ext = [System.IO.Path]::GetExtension($base)
    if ($ext -in @('.pem', '.key', '.p12', '.pfx', '.pkcs12', '.jks', '.keystore', '.ppk', '.netrc')) { return $true }
    foreach ($kw in $secretKeywords) {
        if ($base.Contains($kw) -and $credExts.Contains($ext)) { return $true }
    }
    return $false
}

$offending = New-Object System.Collections.Generic.List[string]
$fsv = [System.IO.File]::Open($stageZip, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read)
$zipv = New-Object System.IO.Compression.ZipArchive($fsv, [System.IO.Compression.ZipArchiveMode]::Read)
try {
    $entryCount = $zipv.Entries.Count
    foreach ($e in $zipv.Entries) { if (Test-IsCredentialEntry $e.FullName) { $offending.Add($e.FullName) } }
}
finally { $zipv.Dispose(); $fsv.Dispose() }

if ($offending.Count -gt 0) {
    Remove-Item -LiteralPath $stageZip -Force
    Remove-Item -LiteralPath $dbSnap -Force -ErrorAction SilentlyContinue
    Write-Host "ERROR: secret guard tripped - archive contains credential-shaped entries:"
    $offending | Select-Object -First 20 | ForEach-Object { Write-Host "  - $_" }
    Fail "aborting; last good backup left untouched"
}
if ($entryCount -lt 1) { Remove-Item -LiteralPath $stageZip -Force; Fail "archive is empty" }
Write-Log "secret guard: clean ($entryCount entries scanned)"

# ---- 6. atomic publish (overwrite last month's file) -----------------------
$finalZip = Join-Path $Dest ("{0}.zip" -f $Name)
Move-Item -LiteralPath $stageZip -Destination $finalZip -Force
$zipBytes = (Get-Item -LiteralPath $finalZip).Length
Remove-Item -LiteralPath $dbSnap -Force -ErrorAction SilentlyContinue

# ---- 7. retention: keep the newest $Retain zips ----------------------------
$zips = Get-ChildItem -LiteralPath $Dest -Filter '*.zip' -File | Sort-Object LastWriteTime -Descending
$removed = 0
foreach ($old in ($zips | Select-Object -Skip $Retain)) { Remove-Item -LiteralPath $old.FullName -Force; $removed++ }

# ---- 8. manifest + run log -------------------------------------------------
$durationSec = [int]((Get-Date) - $startedAt).TotalSeconds
$nowUtc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
$manifest = Join-Path $Dest ("{0}.MANIFEST.txt" -f $Name)
@(
    "earnings-summary monthly backup"
    "built_utc      : $nowUtc"
    "built_local    : $((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))"
    "scope          : $Scope"
    "repo_root      : $RepoRoot"
    "archive        : $finalZip"
    ("archive_size   : {0:N1} MB ({1:N2} GB)" -f ($zipBytes / 1MB), ($zipBytes / 1GB))
    ("uncompressed   : {0:N1} MB" -f ($uncompressed / 1MB))
    "entries        : $entryCount"
    "skipped_files  : $($skips.Count)"
    "walk_errors    : $($walkErrors.Count)"
    "retention_kept : $Retain"
    "duration_sec   : $durationSec"
    "db_snapshot    : data/portfolio.db (SQLite online backup, consistent)"
    "excludes       : venv .git .tmp output caches .claude node_modules _inbox secrets *.db *.bak *.pem *.key .env credentials/tokens"
) -join "`r`n" | Out-File -FilePath $manifest -Encoding utf8

if ($skips.Count -gt 0) {
    $skipLog = Join-Path $logDir 'backup_es_monthly_skips.log'
    $skips | Out-File -FilePath $skipLog -Encoding utf8
    Write-Log "WARNING: $($skips.Count) files skipped (see $skipLog)"
}

$summary = "{0} scope={1} entries={2} size={3:N0}MB skips={4} walkerr={5} removed={6} dur={7}s OK" -f `
    $nowUtc, $Scope, $entryCount, ($zipBytes / 1MB), $skips.Count, $walkErrors.Count, $removed, $durationSec
Add-Content -LiteralPath $historyLog -Value $summary

Write-Host "=== DONE ==="
Write-Log ("archive : {0}" -f $finalZip)
Write-Log ("size    : {0:N1} MB ({1:N2} GB)" -f ($zipBytes / 1MB), ($zipBytes / 1GB))
Write-Log ("entries : {0}   removed old zips: {1}   took {2}s" -f $entryCount, $removed, $durationSec)
exit 0
