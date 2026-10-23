param([Parameter(Mandatory=$true)][string]$Launcher)
$ErrorActionPreference='Stop'
$path=(Resolve-Path -LiteralPath $Launcher).Path
$assembly=[Reflection.Assembly]::LoadFile($path)
$nested=$assembly.GetType('FilingXbrlAppContainerLauncher').GetNestedType('PrivateDesktop',[Reflection.BindingFlags]::NonPublic)
$method=$nested.GetMethod('HasLowMandatoryLabel',[Reflection.BindingFlags]'NonPublic,Static')
$prefix='O:SYG:SYD:P(A;;GA;;;SY)'
$cases=@(
 @{name='low_plain';sddl=$prefix+'S:(ML;;NW;;;LW)';expected=$true},
 @{name='low_auto_inherited';sddl=$prefix+'S:AI(ML;;NW;;;LW)';expected=$true},
 @{name='absent';sddl=$prefix;expected=$false},
 @{name='medium';sddl=$prefix+'S:(ML;;NW;;;ME)';expected=$false},
 @{name='high';sddl=$prefix+'S:(ML;;NW;;;HI)';expected=$false},
 @{name='wrong_mask';sddl=$prefix+'S:(ML;;NR;;;LW)';expected=$false},
 @{name='extra_mask';sddl=$prefix+'S:(ML;;NWNX;;;LW)';expected=$false},
 @{name='duplicate';sddl=$prefix+'S:(ML;;NW;;;LW)(ML;;NW;;;LW)';expected=$false},
 @{name='ace_flags';sddl=$prefix+'S:(ML;OI;NW;;;LW)';expected=$false},
 @{name='wrong_type';sddl=$prefix+'S:(AU;SA;0x1;;;LW)';expected=$false}
)
$result=@()
foreach($case in $cases){
 $rejected=$false
 try {$sd=[Security.AccessControl.RawSecurityDescriptor]::new($case.sddl)}
 catch {if($case.expected){throw};$rejected=$true}
 $observed=$false
 if(-not $rejected){$observed=[bool]$method.Invoke($null,@($sd))}
 if($observed -ne $case.expected){throw "label behavior mismatch: $($case.name)"}
 $result+=@{name=$case.name;expected=$case.expected;observed=$observed;parser_rejected=$rejected}
}
$oldAccepted=($prefix+'S:AI(ML;;NW;;;LW)').EndsWith('S:(ML;;NW;;;LW)',[StringComparison]::Ordinal)
if($oldAccepted){throw 'synthetic red proof missing'}
@{passed=$true;source='synthetic in-memory descriptors; actual compiled launcher method';launcher_sha256=(Get-FileHash -Algorithm SHA256 $path).Hash.ToLowerInvariant();old_oracle_accepts_observed_form=$oldAccepted;cases=$result}|ConvertTo-Json -Depth 5
