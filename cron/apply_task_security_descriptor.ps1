param(
    [Parameter(Mandatory = $true)][string]$TaskPath,
    [Parameter(Mandatory = $true)][string]$RenderedXmlPath,
    [switch]$VerifyOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$TaskNamespace = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
$DaclSecurityInformation = 4
$TaskDontAddPrincipalAce = 0x10
$DiscretionaryAclProtected = [System.Security.AccessControl.ControlFlags]::DiscretionaryAclProtected
$AccessAllowedAce = [System.Security.AccessControl.AceType]::AccessAllowed
$NoAceFlags = [System.Security.AccessControl.AceFlags]::None

function Get-UnsignedAccessMask {
    param([Parameter(Mandatory = $true)][int]$AccessMask)

    return [BitConverter]::ToUInt32([BitConverter]::GetBytes($AccessMask), 0)
}

function Get-DaclSemanticSignature {
    param([Parameter(Mandatory = $true)][string]$SecurityDescriptor)

    $descriptor = [System.Security.AccessControl.RawSecurityDescriptor]::new(
        $SecurityDescriptor
    )
    $protected = ($descriptor.ControlFlags -band $DiscretionaryAclProtected) -ne 0
    $daclNull = $null -eq $descriptor.DiscretionaryAcl
    $aceSignatures = @(
        @(
            foreach ($ace in $descriptor.DiscretionaryAcl) {
                $bytes = New-Object byte[] $ace.BinaryLength
                $ace.GetBinaryForm($bytes, 0)
                [BitConverter]::ToString($bytes)
            }
        ) | Sort-Object
    )
    return (
        "protected=$protected;daclNull=$daclNull;" +
        "aceCount=$($aceSignatures.Count);aces=$($aceSignatures -join ',')"
    )
}

function Assert-ExactTaskDacl {
    param(
        [Parameter(Mandatory = $true)][string]$SecurityDescriptor,
        [Parameter(Mandatory = $true)][hashtable]$ExpectedMasks,
        [Parameter(Mandatory = $true)][string]$EvidenceLabel
    )

    $descriptor = [System.Security.AccessControl.RawSecurityDescriptor]::new(
        $SecurityDescriptor
    )
    if (($descriptor.ControlFlags -band $DiscretionaryAclProtected) -eq 0) {
        throw "$EvidenceLabel task DACL must be protected from inherited ACEs."
    }
    if ($null -eq $descriptor.DiscretionaryAcl) {
        throw "$EvidenceLabel task DACL is missing."
    }

    $observedTrustees = @{}
    foreach ($ace in $descriptor.DiscretionaryAcl) {
        if ($ace -isnot [System.Security.AccessControl.CommonAce]) {
            throw "$EvidenceLabel task DACL contains a non-common ACE."
        }
        if ($ace.AceType -ne $AccessAllowedAce) {
            throw "$EvidenceLabel task DACL contains an ACE that is not allow-only."
        }
        if ($ace.AceFlags -ne $NoAceFlags) {
            throw "$EvidenceLabel task DACL contains nonzero ACE flags."
        }

        $trustee = $ace.SecurityIdentifier.Value
        if (-not $ExpectedMasks.ContainsKey($trustee)) {
            throw "$EvidenceLabel task DACL contains unexpected trustee $trustee."
        }
        if ($observedTrustees.ContainsKey($trustee)) {
            throw "$EvidenceLabel task DACL contains duplicate trustee $trustee."
        }
        $observedTrustees[$trustee] = $true
        $actualMask = Get-UnsignedAccessMask -AccessMask $ace.AccessMask
        if ($actualMask -ne $ExpectedMasks[$trustee]) {
            throw "$EvidenceLabel task DACL grants an unexpected access mask to $trustee."
        }
    }
    if ($observedTrustees.Count -ne $ExpectedMasks.Count) {
        throw "$EvidenceLabel task DACL is missing a required trustee."
    }
}

function Assert-DeclaredQueryOnlyTaskDacl {
    param([Parameter(Mandatory = $true)][string]$SecurityDescriptor)

    Assert-ExactTaskDacl -SecurityDescriptor $SecurityDescriptor -EvidenceLabel 'Declared' `
        -ExpectedMasks @{
            'S-1-5-18' = [uint32]268435456       # LOCAL SYSTEM: GENERIC_ALL
            'S-1-5-32-544' = [uint32]268435456  # Administrators: GENERIC_ALL
            'S-1-5-4' = [uint32]2684354560       # Interactive: GENERIC_READ | GENERIC_EXECUTE
        }
}

function Assert-ActualQueryOnlyTaskDacl {
    param([Parameter(Mandatory = $true)][string]$SecurityDescriptor)

    Assert-ExactTaskDacl -SecurityDescriptor $SecurityDescriptor -EvidenceLabel 'Actual' `
        -ExpectedMasks @{
            'S-1-5-18' = [uint32]2032127       # Task Scheduler mapped GENERIC_ALL
            'S-1-5-32-544' = [uint32]2032127  # Task Scheduler mapped GENERIC_ALL
            'S-1-5-4' = [uint32]1179817       # Task Scheduler mapped GENERIC_READ | GENERIC_EXECUTE
        }
}

if ($TaskPath -ne $TaskPath.Trim() -or -not $TaskPath.StartsWith('\')) {
    throw 'TaskPath must be an exact absolute Task Scheduler path.'
}
$lastSeparator = $TaskPath.LastIndexOf('\')
if ($lastSeparator -lt 1 -or $lastSeparator -eq ($TaskPath.Length - 1)) {
    throw 'TaskPath must contain both a folder and task name.'
}

[xml]$taskXml = Get-Content -Raw -LiteralPath $RenderedXmlPath
$namespaceManager = [System.Xml.XmlNamespaceManager]::new($taskXml.NameTable)
$namespaceManager.AddNamespace('t', $TaskNamespace)
$descriptorNodes = @(
    $taskXml.SelectNodes(
        '/t:Task/t:RegistrationInfo/t:SecurityDescriptor',
        $namespaceManager
    )
)
$uriNodes = @(
    $taskXml.SelectNodes(
        '/t:Task/t:RegistrationInfo/t:URI',
        $namespaceManager
    )
)
if ($uriNodes.Count -ne 1 -or [string]$uriNodes[0].InnerText -cne $TaskPath) {
    throw 'Rendered task XML URI does not exactly match TaskPath.'
}
if ($descriptorNodes.Count -eq 0) {
    return
}
if ($descriptorNodes.Count -ne 1) {
    throw 'Rendered task XML must contain at most one SecurityDescriptor.'
}
$declaredDescriptor = [string]$descriptorNodes[0].InnerText
if ([string]::IsNullOrWhiteSpace($declaredDescriptor)) {
    throw 'Rendered task XML contains an empty SecurityDescriptor.'
}
Assert-DeclaredQueryOnlyTaskDacl -SecurityDescriptor $declaredDescriptor

$folderPath = $TaskPath.Substring(0, $lastSeparator)
$taskName = $TaskPath.Substring($lastSeparator + 1)
$schedulerService = New-Object -ComObject Schedule.Service
$schedulerService.Connect()
$registeredTask = $schedulerService.GetFolder($folderPath).GetTask($taskName)
if ($registeredTask.Path -cne $TaskPath) {
    throw 'Task Scheduler resolved a task other than the exact requested path.'
}

$previousDescriptor = [string]$registeredTask.GetSecurityDescriptor(
    $DaclSecurityInformation
)
$previousSemanticDacl = Get-DaclSemanticSignature -SecurityDescriptor $previousDescriptor
if ($VerifyOnly) {
    Assert-ActualQueryOnlyTaskDacl -SecurityDescriptor $previousDescriptor
    Write-Output "Verified actual task DACL for $TaskPath."
    return
}
$setAttempted = $false
try {
    $setAttempted = $true
    [void]$registeredTask.SetSecurityDescriptor(
        $declaredDescriptor,
        $TaskDontAddPrincipalAce
    )
    $actualDescriptor = [string]$registeredTask.GetSecurityDescriptor(
        $DaclSecurityInformation
    )
    Assert-ActualQueryOnlyTaskDacl -SecurityDescriptor $actualDescriptor
}
catch {
    $applyError = $_.Exception.Message
    if ($setAttempted) {
        try {
            [void]$registeredTask.SetSecurityDescriptor(
                $previousDescriptor,
                $TaskDontAddPrincipalAce
            )
            $restoredDescriptor = [string]$registeredTask.GetSecurityDescriptor(
                $DaclSecurityInformation
            )
            if (
                (Get-DaclSemanticSignature -SecurityDescriptor $restoredDescriptor) -ne
                $previousSemanticDacl
            ) {
                throw 'Rollback readback did not match the previous task DACL.'
            }
        }
        catch {
            $rollbackError = $_.Exception.Message
            throw "Task DACL apply failed ($applyError) and rollback failed ($rollbackError)."
        }
    }
    throw "Task DACL apply failed and the previous DACL was restored: $applyError"
}

Write-Output "Applied and verified task DACL for $TaskPath."
