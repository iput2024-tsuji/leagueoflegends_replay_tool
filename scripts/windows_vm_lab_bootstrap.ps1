[CmdletBinding()]
param(
  [string]$InterfaceAlias = "",
  [Parameter(Mandatory = $true)]
  [ValidateNotNullOrEmpty()]
  [string]$GuestAddress,
  [Parameter(Mandatory = $true)]
  [ValidateNotNullOrEmpty()]
  [string]$HostAddress,
  [ValidateRange(8, 30)]
  [int]$PrefixLength = 24,
  [switch]$StartupRepair
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Import-Module Microsoft.PowerShell.Utility -ErrorAction Stop
Import-Module Microsoft.WSMan.Management -ErrorAction Stop
Import-Module NetAdapter -ErrorAction Stop
Import-Module NetConnection -ErrorAction Stop
Import-Module NetSecurity -ErrorAction Stop
Import-Module NetTCPIP -ErrorAction Stop
Import-Module ScheduledTasks -ErrorAction Stop

function Get-Sha256 {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Path
  )

  $stream = [IO.File]::Open(
    [IO.Path]::GetFullPath($Path),
    [IO.FileMode]::Open,
    [IO.FileAccess]::Read,
    [IO.FileShare]::Read
  )
  try {
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
      return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace(
        "-",
        ""
      ).ToLowerInvariant()
    } finally {
      $algorithm.Dispose()
    }
  } finally {
    $stream.Dispose()
  }
}

function Invoke-StartupNetworkRepair {
  Assert-Administrator
  $deadline = [DateTime]::UtcNow.AddSeconds(60)
  $lastError = "network adapter did not become ready"
  do {
    try {
      $adapter = @(Get-NetAdapter -InterfaceAlias $InterfaceAlias -ErrorAction Stop)
      $physical = @(Get-NetAdapter -Physical -ErrorAction Stop | Where-Object { $_.Status -eq "Up" })
      $addresses = @(Get-NetIPAddress -InterfaceAlias $InterfaceAlias -AddressFamily IPv4 -ErrorAction Stop)
      $routes = @(Get-ActiveDefaultIpv4Routes)
      if ($adapter.Count -ne 1 -or $adapter[0].Status -ne "Up") { throw "固定adapterがUpではありません。" }
      if ($physical.Count -ne 1 -or $physical[0].InterfaceAlias -cne $InterfaceAlias) { throw "physical adapter境界が不成立です。" }
      if ($addresses.Count -ne 1 -or @($addresses | Where-Object { $_.IPAddress -ceq $GuestAddress -and $_.PrefixLength -eq $PrefixLength }).Count -ne 1) { throw "固定IPv4が未準備または余分なIPv4があります。" }
      if ($routes.Count -ne 0) { throw "default routeが存在します。" }
      Set-NetConnectionProfile -InterfaceAlias $InterfaceAlias -NetworkCategory Private -ErrorAction Stop
      $profiles = @(Get-NetConnectionProfile -InterfaceAlias $InterfaceAlias -ErrorAction Stop)
      if ($profiles.Count -ne 1 -or [string]$profiles[0].NetworkCategory -cne "Private") { throw "Private profileへの変更を確認できません。" }
      $markerRoot = Join-Path $env:ProgramData "LoLReplayToolVMLab"
      New-Item -ItemType Directory -Path $markerRoot -Force | Out-Null
      [ordered]@{ schema_version = 1; completed_at_utc = [DateTime]::UtcNow.ToString("o"); interface_alias = $InterfaceAlias; guest_address = $GuestAddress; host_address = $HostAddress; result = "passed" } |
        ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $markerRoot "startup-repair.json") -Encoding utf8
      exit 0
    } catch {
      $lastError = $_.Exception.Message
      Start-Sleep -Seconds 2
    }
  } while ([DateTime]::UtcNow -lt $deadline)
  $markerRoot = Join-Path $env:ProgramData "LoLReplayToolVMLab"
  New-Item -ItemType Directory -Path $markerRoot -Force | Out-Null
  [ordered]@{ schema_version = 1; completed_at_utc = [DateTime]::UtcNow.ToString("o"); interface_alias = $InterfaceAlias; guest_address = $GuestAddress; host_address = $HostAddress; result = "failed"; error = $lastError } |
    ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $markerRoot "startup-repair.json") -Encoding utf8
  throw "startup network repair failed: $lastError"
}

function Assert-Administrator {
  $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
  $principal = [Security.Principal.WindowsPrincipal]::new($identity)
  if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "VM bootstrapは管理者PowerShellで実行してください。"
  }
}

function Assert-Ipv4Address {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Address,
    [Parameter(Mandatory = $true)]
    [string]$Label
  )

  $parsed = $null
  if (
    -not [Net.IPAddress]::TryParse($Address, [ref]$parsed) -or
    $parsed.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork
  ) {
    throw "$Label はIPv4で指定してください: $Address"
  }
  return $parsed
}

function Get-ExternalRuntimeState {
  $baseKey = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
    [Microsoft.Win32.RegistryHive]::LocalMachine,
    [Microsoft.Win32.RegistryView]::Registry64
  )
  $runtimeKey = $null
  try {
    $runtimeKey = $baseKey.OpenSubKey(
      "SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64"
    )
    if ($null -eq $runtimeKey) {
      return [ordered]@{ installed = $false; version = $null }
    }
    return [ordered]@{
      installed = ([int]$runtimeKey.GetValue("Installed", 0) -eq 1)
      version = $runtimeKey.GetValue("Version", $null)
    }
  } finally {
    if ($null -ne $runtimeKey) {
      $runtimeKey.Dispose()
    }
    $baseKey.Dispose()
  }
}

function Get-SystemRuntimeInventory {
  $system32 = Join-Path $env:WINDIR "System32"
  $requiredNames = @(
    "concrt140.dll",
    "msvcp140.dll",
    "msvcp140_1.dll",
    "msvcp140_2.dll",
    "vcomp140.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll"
  )
  return @(
    Get-ChildItem -LiteralPath $system32 -File -ErrorAction Stop |
      Where-Object {
        $_.Name -match '^(?:concrt|msvcp|vcomp|vcruntime)140.*\.dll$'
      } |
      Sort-Object Name |
      ForEach-Object {
        [ordered]@{
          name = $_.Name
          path = $_.FullName
          required = $_.Name.ToLowerInvariant() -in $requiredNames
          hashed_name = $_.Name -match '^(?:concrt|msvcp|vcomp|vcruntime)140-[0-9a-f]{8,}\.dll$'
        }
      }
  )
}

function Get-Clr0400Evidence {
  $names = @(
    "msvcp140_clr0400.dll",
    "vcruntime140_clr0400.dll",
    "vcruntime140_1_clr0400.dll"
  )
  $system32 = Join-Path $env:WINDIR "System32"
  $fsutil = Join-Path $system32 "fsutil.exe"
  $sfc = Join-Path $system32 "sfc.exe"
  $records = @(
    foreach ($name in $names) {
      $path = Join-Path $system32 $name
      $file = if (Test-Path -LiteralPath $path -PathType Leaf) { Get-Item -LiteralPath $path } else { $null }
      $signature = if ($null -eq $file) { $null } else { Get-AuthenticodeSignature -LiteralPath $path }
      $original = if ($null -eq $file) { $null } else { $file.VersionInfo.OriginalFilename }
      $hardlinkOutput = if ($null -eq $file) { @() } else { @(& $fsutil hardlink list $path 2>&1 | ForEach-Object { [string]$_ }) }
      $hardlinkExit = if ($null -eq $file) { 1 } else { $LASTEXITCODE }
      $sfcOutput = if ($null -eq $file) { @() } else { @(& $sfc /verifyfile=$path 2>&1 | ForEach-Object { [string]$_ }) }
      $sfcExit = if ($null -eq $file) { 1 } else { $LASTEXITCODE }
      [ordered]@{
        name = $name
        present = $null -ne $file
        version = if ($null -eq $file) { $null } else { $file.VersionInfo.FileVersion }
        size = if ($null -eq $file) { $null } else { $file.Length }
        sha256 = if ($null -eq $file) { $null } else { Get-Sha256 -Path $path }
        signature_status = if ($null -eq $signature) { $null } else { [string]$signature.Status }
        signer_subject = if ($null -eq $signature -or $null -eq $signature.SignerCertificate) { $null } else { $signature.SignerCertificate.Subject }
        original_filename = $original
        hardlinks = $hardlinkOutput
        hardlink_exit_code = $hardlinkExit
        hardlinks_valid = ($null -ne $file -and $hardlinkExit -eq 0 -and ($hardlinkOutput -join "`n") -match '(?im)\\Windows\\System32\\' + [regex]::Escape($name) + '\s*$' -and ($hardlinkOutput -join "`n") -match '(?im)\\WinSxS\\amd64_netfx4-[^\\]+\\' + [regex]::Escape($name) + '\s*$')
        sfc_output = $sfcOutput
        sfc_exit_code = $sfcExit
        valid = ($null -ne $file -and [string]$signature.Status -ceq "Valid" -and $signature.SignerCertificate.Subject -match '(^|,\s*)O=Microsoft Corporation(,|$)' -and $original -ieq $name -and $hardlinkExit -eq 0 -and ($hardlinkOutput -join "`n") -match '(?im)\\Windows\\System32\\' + [regex]::Escape($name) + '\s*$' -and ($hardlinkOutput -join "`n") -match '(?im)\\WinSxS\\amd64_netfx4-[^\\]+\\' + [regex]::Escape($name) + '\s*$' -and $sfcExit -eq 0)
      }
    }
  )
  return [ordered]@{
    expected_names = $names
    observed_names = @($records | Where-Object { $_.present } | ForEach-Object { $_.name })
    exact_set = (@($records | Where-Object { $_.present }).Count -eq $names.Count)
    files = $records
    valid = (@($records | Where-Object { -not $_.valid }).Count -eq 0 -and @($records | Where-Object { $_.present }).Count -eq $names.Count)
  }
}

function Test-Clr0400EvidencePolicy {
  param([Parameter(Mandatory = $true)][psobject]$Evidence)
  $expected = @("msvcp140_clr0400.dll", "vcruntime140_clr0400.dll", "vcruntime140_1_clr0400.dll")
  if (-not $Evidence.valid -or -not $Evidence.exact_set) { return $false }
  return $null -eq (Compare-Object -ReferenceObject $expected -DifferenceObject @($Evidence.observed_names) -CaseSensitive)
}

function Test-LocalPortIncludesWinRm {
  param(
    [AllowNull()]
    [object]$Value
  )

  foreach ($rawValue in @($Value)) {
    foreach ($token in @(([string]$rawValue) -split '[,\s]+')) {
      $candidate = $token.Trim()
      if ($candidate -ceq "Any" -or $candidate -ceq "5985") {
        return $true
      }
      if ($candidate -match '^(\d+)-(\d+)$') {
        if ([int]$Matches[1] -le 5985 -and [int]$Matches[2] -ge 5985) {
          return $true
        }
      }
    }
  }
  return $false
}

function Get-ActiveDefaultIpv4Routes {
  try {
    return @(
      Get-NetRoute `
        -AddressFamily IPv4 `
        -PolicyStore ActiveStore `
        -ErrorAction Stop |
        Where-Object { [string]$_.DestinationPrefix -ceq "0.0.0.0/0" }
    )
  } catch {
    if (
      [string]$_.CategoryInfo.Category -ceq "ObjectNotFound" -and
      [string]$_.FullyQualifiedErrorId -ceq "CmdletizationQuery_NotFound,Get-NetRoute"
    ) {
      return @()
    }
    throw
  }
}

function Get-ActiveInboundFirewallInventory {
  param([ValidateSet("ActiveStore", "PersistentStore")][string]$PolicyStore = "ActiveStore")
  $rules = @(
    Get-NetFirewallRule `
      -Direction Inbound `
      -PolicyStore $PolicyStore `
      -ErrorAction Stop
  )
  # Read the effective policy once per table. Per-rule CIM calls make a clean
  # bootstrap take several minutes and can hide GPO rules outside local policy.
  $filterTables = [ordered]@{
    port = @(
      Get-NetFirewallPortFilter `
        -All `
        -PolicyStore $PolicyStore `
        -ErrorAction Stop
    )
    service = @(
      Get-NetFirewallServiceFilter `
        -All `
        -PolicyStore $PolicyStore `
        -ErrorAction Stop
    )
    address = @(
      Get-NetFirewallAddressFilter `
        -All `
        -PolicyStore $PolicyStore `
        -ErrorAction Stop
    )
    interface = @(
      Get-NetFirewallInterfaceFilter `
        -All `
        -PolicyStore $PolicyStore `
        -ErrorAction Stop
    )
  }
  $ruleIds = @{}
  foreach ($rule in $rules) {
    $instanceId = [string]$rule.InstanceID
    if ([string]::IsNullOrWhiteSpace($instanceId)) {
      throw "$PolicyStore inbound ruleのInstanceIDが空です。"
    }
    if ($ruleIds.ContainsKey($instanceId)) {
      throw "$PolicyStore inbound ruleのInstanceIDが重複しています: $instanceId"
    }
    $ruleIds[$instanceId] = $true
  }
  $filterIndexes = @{}
  foreach ($entry in $filterTables.GetEnumerator()) {
    $index = @{}
    foreach ($filter in @($entry.Value)) {
      $instanceId = [string]$filter.InstanceID
      if ([string]::IsNullOrWhiteSpace($instanceId)) {
        throw "$PolicyStore $($entry.Key) filterのInstanceIDが空です。"
      }
      if ($index.ContainsKey($instanceId)) {
        throw "$PolicyStore $($entry.Key) filterのInstanceIDが重複しています: $instanceId"
      }
      $index[$instanceId] = $filter
    }
    foreach ($instanceId in $ruleIds.Keys) {
      if (-not $index.ContainsKey($instanceId)) {
        throw "$PolicyStore inbound ruleに$($entry.Key) filterがありません: $instanceId"
      }
    }
    $filterIndexes[$entry.Key] = $index
  }
  return [pscustomobject]@{
    rules = $rules
    filter_indexes = $filterIndexes
  }
}

function Get-WinRmRelatedFirewallRules {
  param([ValidateSet("ActiveStore", "PersistentStore")][string]$PolicyStore = "ActiveStore")
  $inventory = Get-ActiveInboundFirewallInventory -PolicyStore $PolicyStore
  return @(
    foreach ($rule in @($inventory.rules)) {
      $instanceId = [string]$rule.InstanceID
      $portFilters = @($inventory.filter_indexes["port"][$instanceId])
      $serviceFilters = @($inventory.filter_indexes["service"][$instanceId])
      $portRelated = @(
        $portFilters |
          Where-Object {
            [string]$_.Protocol -in @("Any", "TCP", "6", "256") -and
            (Test-LocalPortIncludesWinRm $_.LocalPort)
          }
      ).Count -gt 0
      $serviceRelated = @(
        $serviceFilters |
          Where-Object { [string]$_.Service -ceq "WinRM" }
      ).Count -gt 0
      if ($portRelated -or $serviceRelated) {
        $rule
      }
    }
  )
}

function Disable-PersistentWinRmRules {
  $rules = @(Get-WinRmRelatedFirewallRules -PolicyStore PersistentStore)
  $names = @{}
  foreach ($rule in $rules) {
    $name = [string]$rule.Name
    if ([string]::IsNullOrWhiteSpace($name)) { throw "PersistentStore WinRM ruleのNameが空です。" }
    if ([string]::IsNullOrWhiteSpace([string]$rule.InstanceID)) { throw "PersistentStore WinRM ruleのInstanceIDが空です: Name=$name" }
    if ($names.ContainsKey($name)) { throw "PersistentStore WinRM ruleのNameが重複しています: $name" }
    $names[$name] = $true
  }
  foreach ($rule in $rules) {
    $name = [string]$rule.Name
    try {
      Disable-NetFirewallRule -PolicyStore PersistentStore -Name $name -ErrorAction Stop
    } catch {
      throw "PersistentStore WinRM ruleを無効化できませんでした: Name=$name InstanceID=$($rule.InstanceID) SourceType=$($rule.PolicyStoreSourceType) Source=$($rule.PolicyStoreSource) Error=$($_.Exception.Message)"
    }
  }
  return $rules
}

function Get-WinRmFirewallEvidence {
  param(
    [Parameter(Mandatory = $true)]
    [string]$RuleName,
    [Parameter(Mandatory = $true)]
    [string]$Interface,
    [Parameter(Mandatory = $true)]
    [string]$LocalAddress,
    [Parameter(Mandatory = $true)]
    [string]$RemoteAddress
  )

  $inventory = Get-ActiveInboundFirewallInventory
  return @(
    foreach ($rule in @($inventory.rules)) {
      if ([string]$rule.Enabled -cne "True") {
        continue
      }
      $instanceId = [string]$rule.InstanceID
      $portFilters = @($inventory.filter_indexes["port"][$instanceId])
      $serviceFilters = @($inventory.filter_indexes["service"][$instanceId])
      $addressFilters = @($inventory.filter_indexes["address"][$instanceId])
      $interfaceFilters = @($inventory.filter_indexes["interface"][$instanceId])
      $portRelated = @(
        $portFilters |
          Where-Object {
            [string]$_.Protocol -in @("Any", "TCP", "6", "256") -and
            (Test-LocalPortIncludesWinRm $_.LocalPort)
          }
      ).Count -gt 0
      $serviceRelated = @(
        $serviceFilters |
          Where-Object { [string]$_.Service -ceq "WinRM" }
      ).Count -gt 0
      if (-not $portRelated -and -not $serviceRelated) {
        continue
      }
      $exactScope = (
        $rule.Name -ceq $RuleName -and
        [string]$rule.Direction -ceq "Inbound" -and
        [string]$rule.Action -ceq "Allow" -and
        [string]$rule.Profile -ceq "Private" -and
        $portFilters.Count -eq 1 -and
        [string]$portFilters[0].Protocol -in @("TCP", "6") -and
        [string]$portFilters[0].LocalPort -ceq "5985" -and
        [string]$portFilters[0].RemotePort -ceq "Any" -and
        $addressFilters.Count -eq 1 -and
        [string]$addressFilters[0].LocalAddress -ceq $LocalAddress -and
        [string]$addressFilters[0].RemoteAddress -ceq $RemoteAddress -and
        $interfaceFilters.Count -eq 1 -and
        [string]$interfaceFilters[0].InterfaceAlias -ceq $Interface
      )
      [ordered]@{
        name = $rule.Name
        exact_scope = $exactScope
        enabled = [string]$rule.Enabled
        direction = [string]$rule.Direction
        action = [string]$rule.Action
        profile = [string]$rule.Profile
        local_address = [string]$addressFilters[0].LocalAddress
        remote_address = [string]$addressFilters[0].RemoteAddress
        interface_alias = [string]$interfaceFilters[0].InterfaceAlias
        protocol = [string]$portFilters[0].Protocol
        local_port = [string]$portFilters[0].LocalPort
      }
    }
  )
}

Assert-Administrator
$guestIp = Assert-Ipv4Address -Address $GuestAddress -Label "GuestAddress"
$hostIp = Assert-Ipv4Address -Address $HostAddress -Label "HostAddress"
$guestBytes = $guestIp.GetAddressBytes()
$hostBytes = $hostIp.GetAddressBytes()
if (
  $PrefixLength -ne 24 -or
  $guestBytes[0] -ne $hostBytes[0] -or
  $guestBytes[1] -ne $hostBytes[1] -or
  $guestBytes[2] -ne $hostBytes[2] -or
  $GuestAddress -ceq $HostAddress
) {
  throw "このlabではhost/guestを同一/24内の異なるaddressに固定してください。"
}
if ($StartupRepair) {
  Invoke-StartupNetworkRepair
}

$runtimeBefore = Get-ExternalRuntimeState
if ($runtimeBefore.installed) {
  throw "bootstrap前にx64 Visual C++ Redistributableが既に導入されています。"
}
$presentRuntimeDlls = @(Get-SystemRuntimeInventory)
$clr0400Evidence = Get-Clr0400Evidence
$clr0400Names = @($clr0400Evidence.expected_names)
$blockingRuntimeDlls = @(
  $presentRuntimeDlls | Where-Object {
    $_.name -notin $clr0400Names
  }
)
if (-not (Test-Clr0400EvidencePolicy -Evidence $clr0400Evidence)) {
  throw "bootstrap前のCLR0400 DLL証拠が不成立です。"
}
if ($blockingRuntimeDlls.Count -ne 0) {
  throw "bootstrap前のSystem32に対象VC++ Runtime DLLがあります: $($blockingRuntimeDlls.name -join ', ')"
}
$toolsService = Get-Service -Name "VMTools" -ErrorAction SilentlyContinue
$toolsExecutable = Join-Path $env:ProgramFiles "VMware\VMware Tools\vmtoolsd.exe"
if ($null -ne $toolsService -or (Test-Path -LiteralPath $toolsExecutable -PathType Leaf)) {
  throw "bootstrap前にVMware Toolsが存在します。Environment Aには使用できません。"
}

$upAdapters = @(Get-NetAdapter -Physical -ErrorAction Stop | Where-Object { $_.Status -eq "Up" })
if ([string]::IsNullOrWhiteSpace($InterfaceAlias)) {
  if ($upAdapters.Count -ne 1) {
    throw "Up状態のphysical network adapterが1件ではありません。-InterfaceAliasを明示してください。"
  }
  $InterfaceAlias = $upAdapters[0].InterfaceAlias
}
$adapter = @(Get-NetAdapter -InterfaceAlias $InterfaceAlias -ErrorAction Stop)
if ($adapter.Count -ne 1 -or $adapter[0].Status -ne "Up") {
  throw "指定network adapterがUpの状態で1件必要です: $InterfaceAlias"
}
$otherPhysicalAdapters = @(
  $upAdapters |
    Where-Object { $_.InterfaceAlias -cne $InterfaceAlias }
)
if ($otherPhysicalAdapters.Count -ne 0) {
  throw "host-only adapter以外がUpです: $($otherPhysicalAdapters.InterfaceAlias -join ', ')"
}

Set-NetIPInterface `
  -InterfaceAlias $InterfaceAlias `
  -AddressFamily IPv4 `
  -Dhcp Disabled `
  -ErrorAction Stop
Get-NetIPAddress `
  -InterfaceAlias $InterfaceAlias `
  -AddressFamily IPv4 `
  -ErrorAction SilentlyContinue |
  Where-Object { $_.IPAddress -ne $GuestAddress } |
  Remove-NetIPAddress -Confirm:$false -ErrorAction Stop
if (-not (
  Get-NetIPAddress `
    -InterfaceAlias $InterfaceAlias `
    -AddressFamily IPv4 `
    -IPAddress $GuestAddress `
    -ErrorAction SilentlyContinue
)) {
  New-NetIPAddress `
    -InterfaceAlias $InterfaceAlias `
    -IPAddress $GuestAddress `
    -PrefixLength $PrefixLength `
    -AddressFamily IPv4 `
    -ErrorAction Stop |
    Out-Null
}
Set-DnsClientServerAddress `
  -InterfaceAlias $InterfaceAlias `
  -ResetServerAddresses `
  -ErrorAction Stop
$routesToRemove = @(Get-ActiveDefaultIpv4Routes | Where-Object { $_.InterfaceAlias -ceq $InterfaceAlias })
foreach ($route in $routesToRemove) {
  Remove-NetRoute -InputObject $route -Confirm:$false -ErrorAction Stop
}
Set-NetConnectionProfile `
  -InterfaceAlias $InterfaceAlias `
  -NetworkCategory Private `
  -ErrorAction Stop

Enable-PSRemoting -SkipNetworkProfileCheck -Force
Set-Item -LiteralPath WSMan:\localhost\Service\AllowUnencrypted -Value $false
Set-Item -LiteralPath WSMan:\localhost\Service\Auth\Basic -Value $false
Set-ItemProperty `
  -LiteralPath "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" `
  -Name "LocalAccountTokenFilterPolicy" `
  -Type DWord `
  -Value 1 `
  -Force

$firewallRuleName = "LoLReplayTool-VM-Lab-WinRM"
Disable-PersistentWinRmRules | Out-Null
$existingFirewallRule = @(Get-NetFirewallRule -PolicyStore PersistentStore -Name $firewallRuleName -ErrorAction SilentlyContinue)
if ($existingFirewallRule.Count -ne 0) {
  Remove-NetFirewallRule -Name $firewallRuleName -PolicyStore PersistentStore -ErrorAction Stop
}
New-NetFirewallRule `
  -Name $firewallRuleName `
  -DisplayName "LoL Replay Tool VM Lab WinRM" `
  -Enabled True `
  -Direction Inbound `
  -Action Allow `
  -Profile Private `
  -InterfaceAlias $InterfaceAlias `
  -LocalAddress $GuestAddress `
  -RemoteAddress $HostAddress `
  -Protocol TCP `
  -LocalPort 5985 `
  -PolicyStore PersistentStore `
  -ErrorAction Stop |
  Out-Null

$markerRoot = Join-Path $env:ProgramData "LoLReplayToolVMLab"
$markerPath = Join-Path $markerRoot "bootstrap.json"
$startupRepairPath = Join-Path $markerRoot "startup-repair.json"
$startupTaskScriptPath = Join-Path $markerRoot "windows_vm_lab_bootstrap.ps1"
New-Item -ItemType Directory -Path $markerRoot -Force | Out-Null
if (Test-Path -LiteralPath $startupRepairPath -PathType Leaf) {
  Remove-Item -LiteralPath $startupRepairPath -Force -ErrorAction Stop
}
if (-not [IO.Path]::GetFullPath($MyInvocation.MyCommand.Path).Equals(
  [IO.Path]::GetFullPath($startupTaskScriptPath),
  [StringComparison]::OrdinalIgnoreCase
)) {
  Copy-Item `
    -LiteralPath $MyInvocation.MyCommand.Path `
    -Destination $startupTaskScriptPath `
    -Force `
    -ErrorAction Stop
}
$bootstrapHash = Get-Sha256 -Path $MyInvocation.MyCommand.Path
if ((Get-Sha256 -Path $startupTaskScriptPath) -cne $bootstrapHash) {
  throw "startup network repair scriptの固定copyを確認できませんでした。"
}

$startupTaskName = "LoLReplayTool-VM-Lab-NetworkRepair"
$startupTaskPath = "\"
$startupTaskAction = New-ScheduledTaskAction -Execute (Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe") -Argument (
  "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$startupTaskScriptPath`" -StartupRepair -InterfaceAlias `"$InterfaceAlias`" -GuestAddress `"$GuestAddress`" -HostAddress `"$HostAddress`" -PrefixLength $PrefixLength"
)
$startupTaskPrincipal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$startupTaskTrigger = New-ScheduledTaskTrigger -AtStartup
$startupTask = New-ScheduledTask -Action $startupTaskAction -Principal $startupTaskPrincipal -Trigger $startupTaskTrigger
Register-ScheduledTask -TaskName $startupTaskName -TaskPath $startupTaskPath -InputObject $startupTask -Force -ErrorAction Stop | Out-Null
$startupTaskMatches = @(Get-ScheduledTask -TaskName $startupTaskName -TaskPath $startupTaskPath -ErrorAction Stop)
if ($startupTaskMatches.Count -ne 1) {
  throw "startup network repair taskがroot task pathにexact matchで1件ではありません。"
}
$startupTaskInfo = $startupTaskMatches[0]
$expectedTaskArgs = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$startupTaskScriptPath`" -StartupRepair -InterfaceAlias `"$InterfaceAlias`" -GuestAddress `"$GuestAddress`" -HostAddress `"$HostAddress`" -PrefixLength $PrefixLength"
if (
  $startupTaskInfo.Principal.UserId -cne "SYSTEM" -or
  [string]$startupTaskInfo.Principal.LogonType -cne "ServiceAccount" -or
  [string]$startupTaskInfo.Principal.RunLevel -cne "Highest" -or
  [string]$startupTaskInfo.Settings.Enabled -cne "True" -or
  @($startupTaskInfo.Triggers).Count -ne 1 -or
  [string]$startupTaskInfo.Triggers[0].CimClass.CimClassName -cne "MSFT_TaskBootTrigger" -or
  @($startupTaskInfo.Actions).Count -ne 1 -or
  [string]$startupTaskInfo.Actions[0].Execute -cne (Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe") -or
  [string]$startupTaskInfo.Actions[0].Arguments -cne $expectedTaskArgs
) { throw "startup network repair taskのaction/principal/triggerが不正です。" }

$defaultRoutes = @(Get-ActiveDefaultIpv4Routes)
if ($defaultRoutes.Count -ne 0) {
  throw "bootstrap後にdefault IPv4 routeが残っています。"
}
$firewallEvidence = @(
  Get-WinRmFirewallEvidence `
    -RuleName $firewallRuleName `
    -Interface $InterfaceAlias `
    -LocalAddress $GuestAddress `
    -RemoteAddress $HostAddress
)
if ($firewallEvidence.Count -ne 1 -or -not $firewallEvidence[0].exact_scope) {
  throw "WinRM firewall ruleをhost-onlyの固定scopeへ限定できませんでした。"
}

$marker = [ordered]@{
  schema_version = 3
  created_at_utc = [DateTime]::UtcNow.ToString("o")
  computer_name = $env:COMPUTERNAME
  user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
  interface_alias = $InterfaceAlias
  guest_address = $GuestAddress
  host_address = $HostAddress
  prefix_length = $PrefixLength
  runtime_before = $runtimeBefore
  system_runtime_dlls_present = $presentRuntimeDlls
  clr0400_evidence = $clr0400Evidence
  vmware_tools_present = $false
  default_routes = $defaultRoutes
  winrm_firewall = $firewallEvidence
  startup_task = [ordered]@{
    name = $startupTaskName
    task_path = $startupTaskPath
    script_path = $startupTaskScriptPath
    script_sha256 = Get-Sha256 -Path $startupTaskScriptPath
    principal = [string]$startupTaskInfo.Principal.UserId
    logon_type = [string]$startupTaskInfo.Principal.LogonType
    run_level = [string]$startupTaskInfo.Principal.RunLevel
    enabled = [string]$startupTaskInfo.Settings.Enabled
    trigger = @($startupTaskInfo.Triggers | ForEach-Object { $_.CimClass.CimClassName })
    action = [string]$startupTaskInfo.Actions[0].Arguments
  }
  bootstrap_sha256 = $bootstrapHash
}
$marker | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $markerPath -Encoding utf8

Test-WSMan -ComputerName localhost -ErrorAction Stop | Out-Null
$marker | ConvertTo-Json -Depth 6
Write-Host "VM lab bootstrap complete. Shut down Windows and create snapshot A0-runtime-absent."
