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
  [int]$PrefixLength = 24
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Import-Module Microsoft.PowerShell.Utility -ErrorAction Stop
Import-Module Microsoft.WSMan.Management -ErrorAction Stop
Import-Module NetAdapter -ErrorAction Stop
Import-Module NetConnection -ErrorAction Stop
Import-Module NetSecurity -ErrorAction Stop
Import-Module NetTCPIP -ErrorAction Stop

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

function Get-ActiveInboundFirewallInventory {
  $rules = @(
    Get-NetFirewallRule `
      -Direction Inbound `
      -PolicyStore ActiveStore `
      -ErrorAction Stop
  )
  # Read the effective policy once per table. Per-rule CIM calls make a clean
  # bootstrap take several minutes and can hide GPO rules outside local policy.
  $filterTables = [ordered]@{
    port = @(
      Get-NetFirewallPortFilter `
        -All `
        -PolicyStore ActiveStore `
        -ErrorAction Stop
    )
    service = @(
      Get-NetFirewallServiceFilter `
        -All `
        -PolicyStore ActiveStore `
        -ErrorAction Stop
    )
    address = @(
      Get-NetFirewallAddressFilter `
        -All `
        -PolicyStore ActiveStore `
        -ErrorAction Stop
    )
    interface = @(
      Get-NetFirewallInterfaceFilter `
        -All `
        -PolicyStore ActiveStore `
        -ErrorAction Stop
    )
  }
  $ruleIds = @{}
  foreach ($rule in $rules) {
    $instanceId = [string]$rule.InstanceID
    if ([string]::IsNullOrWhiteSpace($instanceId)) {
      throw "ActiveStore inbound ruleのInstanceIDが空です。"
    }
    if ($ruleIds.ContainsKey($instanceId)) {
      throw "ActiveStore inbound ruleのInstanceIDが重複しています: $instanceId"
    }
    $ruleIds[$instanceId] = $true
  }
  $filterIndexes = @{}
  foreach ($entry in $filterTables.GetEnumerator()) {
    $index = @{}
    foreach ($filter in @($entry.Value)) {
      $instanceId = [string]$filter.InstanceID
      if ([string]::IsNullOrWhiteSpace($instanceId)) {
        throw "ActiveStore $($entry.Key) filterのInstanceIDが空です。"
      }
      if ($index.ContainsKey($instanceId)) {
        throw "ActiveStore $($entry.Key) filterのInstanceIDが重複しています: $instanceId"
      }
      $index[$instanceId] = $filter
    }
    foreach ($instanceId in $ruleIds.Keys) {
      if (-not $index.ContainsKey($instanceId)) {
        throw "ActiveStore inbound ruleに$($entry.Key) filterがありません: $instanceId"
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
  $inventory = Get-ActiveInboundFirewallInventory
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

$runtimeBefore = Get-ExternalRuntimeState
if ($runtimeBefore.installed) {
  throw "bootstrap前にx64 Visual C++ Redistributableが既に導入されています。"
}
$presentRuntimeDlls = @(Get-SystemRuntimeInventory)
$blockingRuntimeDlls = @($presentRuntimeDlls)
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
Get-NetRoute `
  -InterfaceAlias $InterfaceAlias `
  -AddressFamily IPv4 `
  -DestinationPrefix "0.0.0.0/0" `
  -PolicyStore ActiveStore `
  -ErrorAction SilentlyContinue |
  Remove-NetRoute -Confirm:$false -ErrorAction Stop
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
Get-WinRmRelatedFirewallRules |
  Disable-NetFirewallRule -ErrorAction Stop
Get-NetFirewallRule -Name $firewallRuleName -ErrorAction SilentlyContinue |
  Remove-NetFirewallRule
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
  -LocalPort 5985 |
  Out-Null

$defaultRoutes = @(
  Get-NetRoute `
    -AddressFamily IPv4 `
    -DestinationPrefix "0.0.0.0/0" `
    -PolicyStore ActiveStore `
    -ErrorAction Stop
)
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

$markerRoot = Join-Path $env:ProgramData "LoLReplayToolVMLab"
$markerPath = Join-Path $markerRoot "bootstrap.json"
New-Item -ItemType Directory -Path $markerRoot -Force | Out-Null
$marker = [ordered]@{
  schema_version = 2
  created_at_utc = [DateTime]::UtcNow.ToString("o")
  computer_name = $env:COMPUTERNAME
  user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
  interface_alias = $InterfaceAlias
  guest_address = $GuestAddress
  host_address = $HostAddress
  prefix_length = $PrefixLength
  runtime_before = $runtimeBefore
  system_runtime_dlls_present = $presentRuntimeDlls
  vmware_tools_present = $false
  default_routes = $defaultRoutes
  winrm_firewall = $firewallEvidence
  bootstrap_sha256 = (Get-FileHash -LiteralPath $MyInvocation.MyCommand.Path -Algorithm SHA256).Hash.ToLowerInvariant()
}
$marker | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $markerPath -Encoding utf8

Test-WSMan -ComputerName localhost -ErrorAction Stop | Out-Null
$marker | ConvertTo-Json -Depth 6
Write-Host "VM lab bootstrap complete. Shut down Windows and create snapshot A0-runtime-absent."
