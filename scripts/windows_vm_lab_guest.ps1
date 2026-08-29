param(
  [Parameter(Mandatory = $true)]
  [ValidateSet("Inspect", "InstallRuntime", "SelfCheck", "InstallerEnvironmentA", "InstallerEnvironmentB")]
  [string]$Action,
  [string]$PayloadVolumeLabel = "",
  [string]$RuntimeInstallerRelativePath = "",
  [string]$RuntimeInstallerSha256 = "",
  [string]$InstallerRelativePath = "",
  [string]$InstallerSha256 = "",
  [string]$MinimumRuntimeVersion = "",
  [string]$AppRelativePath = "",
  [string]$AppSha256 = "",
  [string]$EnvironmentBScriptRelativePath = "",
  [string]$EnvironmentBScriptSha256 = "",
  [string]$BootstrapScriptSha256 = "",
  [ValidateRange(1, 600)]
  [int]$TimeoutSeconds = 180
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Import-Module Microsoft.PowerShell.Utility -ErrorAction Stop
Import-Module NetSecurity -ErrorAction Stop
Import-Module NetTCPIP -ErrorAction Stop
Import-Module ScheduledTasks -ErrorAction SilentlyContinue

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
      return [ordered]@{
        installed = $false
        version = $null
        registry_view = "Registry64"
      }
    }

    $installedValue = $runtimeKey.GetValue("Installed", 0)
    $versionValue = $runtimeKey.GetValue("Version", $null)
    return [ordered]@{
      installed = ([int]$installedValue -eq 1)
      version = if ($null -eq $versionValue) { $null } else { [string]$versionValue }
      registry_view = "Registry64"
    }
  } finally {
    if ($null -ne $runtimeKey) {
      $runtimeKey.Dispose()
    }
    $baseKey.Dispose()
  }
}

function ConvertTo-Version {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Value,
    [Parameter(Mandatory = $true)]
    [string]$Label
  )

  $normalized = $Value.Trim()
  if ($normalized.StartsWith("v", [StringComparison]::OrdinalIgnoreCase)) {
    $normalized = $normalized.Substring(1)
  }
  $parsed = $null
  if (-not [version]::TryParse($normalized, [ref]$parsed)) {
    throw "$Label をversionとして解釈できません: $Value"
  }
  return $parsed
}

function Get-SystemRuntimeDlls {
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
    foreach ($name in $requiredNames) {
      $path = Join-Path $env:WINDIR "System32\$name"
      $present = Test-Path -LiteralPath $path -PathType Leaf
      $file = if ($present) { Get-Item -LiteralPath $path } else { $null }
      [ordered]@{
        name = $name
        present = $present
        version = if ($null -eq $file) { $null } else { $file.VersionInfo.FileVersion }
        sha256 = if ($null -eq $file) {
          $null
        } else {
          Get-Sha256 -Path $path
        }
      }
    }
  )
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
        $signature = Get-AuthenticodeSignature -LiteralPath $_.FullName
        [ordered]@{
          name = $_.Name
          required = $_.Name.ToLowerInvariant() -in $requiredNames
          hashed_name = $_.Name -match '^(?:concrt|msvcp|vcomp|vcruntime)140-[0-9a-f]{8,}\.dll$'
          version = $_.VersionInfo.FileVersion
          size = $_.Length
          sha256 = Get-Sha256 -Path $_.FullName
          signature_status = [string]$signature.Status
          signer_subject = if ($null -eq $signature.SignerCertificate) {
            $null
          } else {
            $signature.SignerCertificate.Subject
          }
        }
      }
  )
}

function Get-Clr0400Evidence {
  $names = @("msvcp140_clr0400.dll", "vcruntime140_clr0400.dll", "vcruntime140_1_clr0400.dll")
  $system32 = Join-Path $env:WINDIR "System32"
  $fsutil = Join-Path $system32 "fsutil.exe"
  $sfcPath = Join-Path $system32 "sfc.exe"
  $records = @(
    foreach ($name in $names) {
      $path = Join-Path $system32 $name
      $file = if (Test-Path -LiteralPath $path -PathType Leaf) { Get-Item -LiteralPath $path } else { $null }
      $signature = if ($null -eq $file) { $null } else { Get-AuthenticodeSignature -LiteralPath $path }
      $links = if ($null -eq $file) { @() } else { @(& $fsutil hardlink list $path 2>&1 | ForEach-Object { [string]$_ }) }
      $linkExit = if ($null -eq $file) { 1 } else { $LASTEXITCODE }
      $sfc = if ($null -eq $file) { @() } else { @(& $sfcPath /verifyfile=$path 2>&1 | ForEach-Object { [string]$_ }) }
      $sfcExit = if ($null -eq $file) { 1 } else { $LASTEXITCODE }
      $linkText = $links -join "`n"
      [ordered]@{
        name = $name; present = $null -ne $file
        version = if ($null -eq $file) { $null } else { $file.VersionInfo.FileVersion }
        size = if ($null -eq $file) { $null } else { $file.Length }
        sha256 = if ($null -eq $file) { $null } else { Get-Sha256 -Path $path }
        signature_status = if ($null -eq $signature) { $null } else { [string]$signature.Status }
        signer_subject = if ($null -eq $signature -or $null -eq $signature.SignerCertificate) { $null } else { $signature.SignerCertificate.Subject }
        original_filename = if ($null -eq $file) { $null } else { $file.VersionInfo.OriginalFilename }
        hardlinks = $links; hardlink_exit_code = $linkExit
        hardlinks_valid = ($null -ne $file -and $linkExit -eq 0 -and $linkText -match '(?im)\\Windows\\System32\\' + [regex]::Escape($name) + '\s*$' -and $linkText -match '(?im)\\WinSxS\\amd64_netfx4-[^\\]+\\' + [regex]::Escape($name) + '\s*$')
        sfc_output = $sfc; sfc_exit_code = $sfcExit
        valid = ($null -ne $file -and [string]$signature.Status -ceq "Valid" -and $signature.SignerCertificate.Subject -match '(^|,\s*)O=Microsoft Corporation(,|$)' -and $file.VersionInfo.OriginalFilename -ieq $name -and $linkExit -eq 0 -and $linkText -match '(?im)\\Windows\\System32\\' + [regex]::Escape($name) + '\s*$' -and $linkText -match '(?im)\\WinSxS\\amd64_netfx4-[^\\]+\\' + [regex]::Escape($name) + '\s*$' -and $sfcExit -eq 0)
      }
    }
  )
  return [ordered]@{ expected_names = $names; observed_names = @($records | Where-Object { $_.present } | ForEach-Object { $_.name }); exact_set = (@($records | Where-Object { $_.present }).Count -eq $names.Count); files = $records; valid = (@($records | Where-Object { -not $_.valid }).Count -eq 0 -and @($records | Where-Object { $_.present }).Count -eq $names.Count) }
}

function Test-Clr0400EvidencePolicy {
  param([Parameter(Mandatory = $true)][psobject]$Evidence)
  $expected = @("msvcp140_clr0400.dll", "vcruntime140_clr0400.dll", "vcruntime140_1_clr0400.dll")
  if (-not $Evidence.valid -or -not $Evidence.exact_set) { return $false }
  return $null -eq (Compare-Object -ReferenceObject $expected -DifferenceObject @($Evidence.observed_names) -CaseSensitive)
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

function Get-DefaultRouteState {
  $routes = @(
    Get-ActiveDefaultIpv4Routes |
      Sort-Object InterfaceIndex, RouteMetric |
      ForEach-Object {
        [ordered]@{
          interface_index = $_.InterfaceIndex
          interface_alias = $_.InterfaceAlias
          next_hop = $_.NextHop
          route_metric = $_.RouteMetric
          state = [string]$_.State
        }
      }
  )
  return [ordered]@{
    source = "route-table"
    routes = $routes
    error = $null
  }
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

function Get-WinRmFirewallEvidence {
  $auditedRuleCount = 0
  $mappedRuleCount = 0
  try {
    $rules = @(
      Get-NetFirewallRule `
        -Enabled True `
        -Direction Inbound `
        -PolicyStore ActiveStore `
        -ErrorAction Stop
    )
    $auditedRuleCount = $rules.Count
    # Query each filter table once. Piping every rule through all four filter
    # cmdlets makes a clean Windows inspection take several minutes. ActiveStore
    # is required so effective GPO and local rules cannot evade this audit.
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
    $mappedRuleCount = $rules.Count
    $related = @(
      foreach ($rule in $rules) {
        $instanceId = [string]$rule.InstanceID
        $portFilters = @($filterIndexes["port"][$instanceId])
        $serviceFilters = @($filterIndexes["service"][$instanceId])
        $addressFilters = @($filterIndexes["address"][$instanceId])
        $interfaceFilters = @($filterIndexes["interface"][$instanceId])
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
        $profile = [string]$rule.Profile
        $exactScope = (
          $rule.Name -ceq "LoLReplayTool-VM-Lab-WinRM" -and
          [string]$rule.Enabled -ceq "True" -and
          [string]$rule.Direction -ceq "Inbound" -and
          [string]$rule.Action -ceq "Allow" -and
          $profile -ceq "Private" -and
          $portFilters.Count -eq 1 -and
          [string]$portFilters[0].Protocol -in @("TCP", "6") -and
          [string]$portFilters[0].LocalPort -ceq "5985" -and
          [string]$portFilters[0].RemotePort -ceq "Any" -and
          $addressFilters.Count -eq 1 -and
          [string]$addressFilters[0].LocalAddress -ceq $bootstrapMarker.guest_address -and
          [string]$addressFilters[0].RemoteAddress -ceq $bootstrapMarker.host_address -and
          $interfaceFilters.Count -eq 1 -and
          [string]$interfaceFilters[0].InterfaceAlias -ceq $bootstrapMarker.interface_alias
        )
        [ordered]@{
          name = $rule.Name
          display_name = $rule.DisplayName
          enabled = [string]$rule.Enabled
          direction = [string]$rule.Direction
          action = [string]$rule.Action
          profile = $profile
          port = @($portFilters | ForEach-Object {
            [ordered]@{
              protocol = [string]$_.Protocol
              local_port = [string]$_.LocalPort
              remote_port = [string]$_.RemotePort
            }
          })
          address = @($addressFilters | ForEach-Object {
            [ordered]@{
              local = [string]$_.LocalAddress
              remote = [string]$_.RemoteAddress
            }
          })
          interface = @($interfaceFilters | ForEach-Object {
            [ordered]@{ alias = [string]$_.InterfaceAlias }
          })
          exact_scope = $exactScope
        }
      }
    )
    return [ordered]@{
      available = $true
      policy_store = "ActiveStore"
      audited_rule_count = $auditedRuleCount
      mapped_rule_count = $mappedRuleCount
      rules = $related
      error = $null
    }
  } catch {
    return [ordered]@{
      available = $false
      policy_store = "ActiveStore"
      audited_rule_count = $auditedRuleCount
      mapped_rule_count = $mappedRuleCount
      rules = @()
      error = $_.Exception.Message
    }
  }
}

function Get-PayloadInspection {
  if ([string]::IsNullOrWhiteSpace($PayloadVolumeLabel)) {
    return [ordered]@{
      required = $false
      verified = $false
      volume_label = $null
      files = @()
    }
  }
  $volumes = @(
    Get-Volume -ErrorAction Stop |
      Where-Object { $_.FileSystemLabel -ceq $PayloadVolumeLabel }
  )
  if ($volumes.Count -ne 1 -or [string]::IsNullOrWhiteSpace($volumes[0].DriveLetter)) {
    throw "fixed payload volumeはexact matchで1件必要です。"
  }
  $driveId = "$($volumes[0].DriveLetter):"
  $logicalDisk = Get-CimInstance `
    -ClassName Win32_LogicalDisk `
    -Filter "DeviceID='$driveId'" `
    -ErrorAction Stop
  $items = @(
    [ordered]@{
      label = "VC++ Redistributable installer"
      relative_path = $RuntimeInstallerRelativePath
      sha256 = $RuntimeInstallerSha256
    },
    [ordered]@{
      label = "completed application installer"
      relative_path = $InstallerRelativePath
      sha256 = $InstallerSha256
    },
    [ordered]@{
      label = "packaged application"
      relative_path = $AppRelativePath
      sha256 = $AppSha256
    },
    [ordered]@{
      label = "Environment B validation script"
      relative_path = $EnvironmentBScriptRelativePath
      sha256 = $EnvironmentBScriptSha256
    },
    [ordered]@{
      label = "VM lab bootstrap script"
      relative_path = "windows_vm_lab_bootstrap.ps1"
      sha256 = $BootstrapScriptSha256
    }
  )
  $files = @(
    foreach ($item in $items) {
      $path = Resolve-PayloadFile `
        -VolumeLabel $PayloadVolumeLabel `
        -RelativePath $item.relative_path
      $actual = Assert-FileHash `
        -Path $path `
        -ExpectedSha256 $item.sha256 `
        -Label $item.label
      [ordered]@{
        label = $item.label
        relative_path = $item.relative_path
        sha256 = $actual
      }
    }
  )
  return [ordered]@{
    required = $true
    verified = $true
    volume_label = $PayloadVolumeLabel
    volume_serial_number = $logicalDisk.VolumeSerialNumber
    file_system = $volumes[0].FileSystem
    size = $volumes[0].Size
    files = $files
  }
}

function Get-Inspection {
  $toolsService = Get-Service -Name "VMTools" -ErrorAction SilentlyContinue
  $toolsExecutable = Join-Path $env:ProgramFiles "VMware\VMware Tools\vmtoolsd.exe"
  $networkInterfaces = @(
    [Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces() |
      Where-Object {
        $_.OperationalStatus -eq [Net.NetworkInformation.OperationalStatus]::Up
      }
  )
  $ipv4Addresses = @(
    foreach ($networkInterface in $networkInterfaces) {
      foreach ($address in $networkInterface.GetIPProperties().UnicastAddresses) {
        if (
          $address.Address.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork -and
          $address.Address.ToString() -ne "127.0.0.1"
        ) {
          $address.Address.ToString()
        }
      }
    }
  )
  $osVersion = [Environment]::OSVersion.Version
  $bootstrapMarkerPath = Join-Path $env:ProgramData "LoLReplayToolVMLab\bootstrap.json"
  $bootstrapMarker = if (Test-Path -LiteralPath $bootstrapMarkerPath -PathType Leaf) {
    Get-Content -LiteralPath $bootstrapMarkerPath -Raw -Encoding utf8 |
      ConvertFrom-Json -ErrorAction Stop
  } else {
    $null
  }
  $script:bootstrapMarker = $bootstrapMarker
  $routeState = Get-DefaultRouteState
  $startupTask = $null
  try {
    $taskMatches = @(
      Get-ScheduledTask `
        -TaskName "LoLReplayTool-VM-Lab-NetworkRepair" `
        -TaskPath "\" `
        -ErrorAction Stop
    )
    if ($taskMatches.Count -ne 1) {
      throw "startup network repair taskがroot task pathにexact matchで1件ではありません。"
    }
    $task = $taskMatches[0]
    $taskInfo = $null
    try {
      $info = Get-ScheduledTaskInfo `
        -TaskName $task.TaskName `
        -TaskPath $task.TaskPath `
        -ErrorAction Stop
      $taskInfo = [ordered]@{ last_task_result = $info.LastTaskResult; last_run_time = $info.LastRunTime.ToString("o") }
    } catch { $taskInfo = $null }
    $taskActions = @($task.Actions)
    $taskAction = if ($taskActions.Count -eq 1) {
      [ordered]@{
        execute = [string]$taskActions[0].Execute
        arguments = [string]$taskActions[0].Arguments
      }
    } else {
      $null
    }
    $taskScriptPath = $null
    if ($null -ne $taskAction) {
      $fileArguments = [regex]::Matches(
        $taskAction.arguments,
        '(?:^|\s)-File\s+"([^"]+)"(?:\s|$)',
        [Text.RegularExpressions.RegexOptions]::CultureInvariant
      )
      if ($fileArguments.Count -eq 1) {
        $taskScriptPath = $fileArguments[0].Groups[1].Value
      }
    }
    $expectedTaskScriptPath = Join-Path $env:ProgramData "LoLReplayToolVMLab\windows_vm_lab_bootstrap.ps1"
    $markerTaskScriptPath = if ($null -ne $bootstrapMarker) {
      [string]$bootstrapMarker.startup_task.script_path
    } else {
      $null
    }
    $taskScriptExists = (
      -not [string]::IsNullOrWhiteSpace($taskScriptPath) -and
      (Test-Path -LiteralPath $taskScriptPath -PathType Leaf)
    )
    $startupTask = [ordered]@{
      present = $true
      name = $task.TaskName
      task_path = $task.TaskPath
      principal = [string]$task.Principal.UserId
      logon_type = [string]$task.Principal.LogonType
      run_level = [string]$task.Principal.RunLevel
      enabled = [string]$task.Settings.Enabled
      triggers = @($task.Triggers | ForEach-Object { $_.CimClass.CimClassName })
      action_count = $taskActions.Count
      action = $taskAction
      info = $taskInfo
      script_path_source = "task-action"
      script_path = $taskScriptPath
      script_path_matches_expected = ($taskScriptPath -ceq $expectedTaskScriptPath)
      marker_script_path_matches_action = ($markerTaskScriptPath -ceq $taskScriptPath)
      script_exists = $taskScriptExists
      script_sha256 = if ($taskScriptExists) { Get-Sha256 -Path $taskScriptPath } else { $null }
    }
  } catch {
    $startupTask = [ordered]@{ present = $false; error = $_.Exception.Message }
  }

  return [ordered]@{
    schema_version = 3
    action = "Inspect"
    captured_at_utc = [DateTime]::UtcNow.ToString("o")
    computer_name = $env:COMPUTERNAME
    os = [ordered]@{
      caption = [Runtime.InteropServices.RuntimeInformation]::OSDescription
      version = $osVersion.ToString()
      build_number = $osVersion.Build.ToString([Globalization.CultureInfo]::InvariantCulture)
      architecture = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
    }
    runtime = Get-ExternalRuntimeState
    payload = Get-PayloadInspection
    system_runtime_dlls = Get-SystemRuntimeDlls
    system_runtime_inventory = Get-SystemRuntimeInventory
    clr0400_evidence = Get-Clr0400Evidence
    bootstrap = $bootstrapMarker
    vmware_tools = [ordered]@{
      present = ($null -ne $toolsService -or (Test-Path -LiteralPath $toolsExecutable -PathType Leaf))
      service_status = if ($null -eq $toolsService) { $null } else { [string]$toolsService.Status }
      executable_present = Test-Path -LiteralPath $toolsExecutable -PathType Leaf
    }
    ipv4_addresses = $ipv4Addresses
    default_route_source = $routeState.source
    default_routes = $routeState.routes
    default_route_error = $routeState.error
    winrm_firewall = Get-WinRmFirewallEvidence
    startup_task = $startupTask
    startup_repair = if ($null -ne $bootstrapMarker) {
      $repairPath = Join-Path $env:ProgramData "LoLReplayToolVMLab\startup-repair.json"
      if (Test-Path -LiteralPath $repairPath -PathType Leaf) { Get-Content -LiteralPath $repairPath -Raw -Encoding utf8 | ConvertFrom-Json -ErrorAction Stop } else { $null }
    } else { $null }
  }
}

function Resolve-PayloadFile {
  param(
    [Parameter(Mandatory = $true)]
    [string]$VolumeLabel,
    [Parameter(Mandatory = $true)]
    [string]$RelativePath
  )

  if ([string]::IsNullOrWhiteSpace($VolumeLabel)) {
    throw "payload volume labelが指定されていません。"
  }
  if (
    [string]::IsNullOrWhiteSpace($RelativePath) -or
    [IO.Path]::IsPathRooted($RelativePath) -or
    $RelativePath -match '(^|[\\/])\.\.([\\/]|$)'
  ) {
    throw "payload内pathは親参照を含まない相対pathで指定してください: $RelativePath"
  }

  $volumes = @(
    Get-Volume -ErrorAction Stop |
      Where-Object { $_.FileSystemLabel -ceq $VolumeLabel }
  )
  if ($volumes.Count -ne 1) {
    throw "volume label '$VolumeLabel' は1件必要です。検出数: $($volumes.Count)"
  }
  if ([string]::IsNullOrWhiteSpace($volumes[0].DriveLetter)) {
    throw "payload volumeにdrive letterがありません: $VolumeLabel"
  }

  $volumeRoot = [IO.Path]::GetFullPath("$($volumes[0].DriveLetter):\")
  $candidate = [IO.Path]::GetFullPath((Join-Path $volumeRoot $RelativePath))
  if (-not $candidate.StartsWith($volumeRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "payload fileがvolume root外を指しています: $RelativePath"
  }
  if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
    throw "payload fileが見つかりません: $candidate"
  }
  return $candidate
}

function Assert-FileHash {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Path,
    [Parameter(Mandatory = $true)]
    [string]$ExpectedSha256,
    [Parameter(Mandatory = $true)]
    [string]$Label
  )

  if ($ExpectedSha256 -cnotmatch '^[0-9a-f]{64}$') {
    throw "$Label の固定SHA256が不正です。"
  }
  $actual = Get-Sha256 -Path $Path
  if ($actual -cne $ExpectedSha256) {
    throw "$Label のSHA256が一致しません。expected=$ExpectedSha256 actual=$actual"
  }
  return $actual
}

function Install-ExternalRuntime {
  $before = Get-ExternalRuntimeState
  if ($before.installed) {
    throw "Environment Aにx64 Visual C++ Redistributableが既に導入されています。"
  }
  $minimum = ConvertTo-Version -Value $MinimumRuntimeVersion -Label "minimum Runtime version"
  $installer = Resolve-PayloadFile `
    -VolumeLabel $PayloadVolumeLabel `
    -RelativePath $RuntimeInstallerRelativePath
  $installerHash = Assert-FileHash `
    -Path $installer `
    -ExpectedSha256 $RuntimeInstallerSha256 `
    -Label "VC++ Redistributable installer"

  $signature = Get-AuthenticodeSignature -LiteralPath $installer
  if (
    $signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
    $null -eq $signature.SignerCertificate -or
    $signature.SignerCertificate.Subject -notmatch '(^|,\s*)O=Microsoft Corporation(,|$)'
  ) {
    throw "VC++ Redistributable installerのMicrosoft署名を確認できません: $($signature.Status)"
  }

  $process = Start-Process `
    -FilePath $installer `
    -ArgumentList @("/install", "/quiet", "/norestart") `
    -WindowStyle Hidden `
    -Wait `
    -PassThru
  if ($process.ExitCode -notin @(0, 3010)) {
    throw "VC++ Redistributable installerが失敗しました: exit=$($process.ExitCode)"
  }

  $after = Get-ExternalRuntimeState
  if (-not $after.installed -or [string]::IsNullOrWhiteSpace($after.version)) {
    throw "VC++ Redistributable導入後のRegistry64状態を確認できません。"
  }
  $actualVersion = ConvertTo-Version -Value $after.version -Label "installed Runtime version"
  if ($actualVersion -lt $minimum) {
    throw "導入Runtimeが必要version未満です: required=$minimum actual=$actualVersion"
  }

  return [ordered]@{
    schema_version = 1
    action = "InstallRuntime"
    captured_at_utc = [DateTime]::UtcNow.ToString("o")
    computer_name = $env:COMPUTERNAME
    installer_sha256 = $installerHash
    installer_signer_subject = $signature.SignerCertificate.Subject
    exit_code = $process.ExitCode
    reboot_required = ($process.ExitCode -eq 3010)
    before = $before
    after = $after
  }
}

function Remove-ValidationCaptureFiles {
  param(
    [Parameter(Mandatory = $true)]
    [string[]]$Paths
  )

  foreach ($path in $Paths) {
    if (Test-Path -LiteralPath $path) {
      Remove-Item -LiteralPath $path -Force -ErrorAction Stop
    }
    if (Test-Path -LiteralPath $path) {
      throw "Environment B validation captureを削除できませんでした: $path"
    }
  }
}

function Invoke-PackagedSelfCheck {
  $runtime = Get-ExternalRuntimeState
  if (-not $runtime.installed -or [string]::IsNullOrWhiteSpace($runtime.version)) {
    throw "packaged self-check前にx64 Visual C++ Redistributableを確認できません。"
  }
  $minimum = ConvertTo-Version -Value $MinimumRuntimeVersion -Label "minimum Runtime version"
  $actualVersion = ConvertTo-Version -Value $runtime.version -Label "installed Runtime version"
  if ($actualVersion -lt $minimum) {
    throw "packaged self-check前のRuntimeが必要version未満です。"
  }

  $app = Resolve-PayloadFile `
    -VolumeLabel $PayloadVolumeLabel `
    -RelativePath $AppRelativePath
  $appHash = Assert-FileHash `
    -Path $app `
    -ExpectedSha256 $AppSha256 `
    -Label "packaged application"
  $environmentBScript = Resolve-PayloadFile `
    -VolumeLabel $PayloadVolumeLabel `
    -RelativePath $EnvironmentBScriptRelativePath
  $environmentBScriptHash = Assert-FileHash `
    -Path $environmentBScript `
    -ExpectedSha256 $EnvironmentBScriptSha256 `
    -Label "Environment B validation script"
  $resultRoot = Join-Path $env:USERPROFILE "Desktop\VC-Runtime-Test-Results"
  $resultPath = Join-Path $resultRoot "environment-b-result.json"
  $validationStdoutPath = Join-Path $resultRoot "environment-b-stdout.txt"
  $validationStderrPath = Join-Path $resultRoot "environment-b-stderr.txt"
  $validationCapturePaths = @($validationStdoutPath, $validationStderrPath)
  New-Item -ItemType Directory -Path $resultRoot -Force | Out-Null
  if (Test-Path -LiteralPath $resultPath -PathType Leaf) {
    Remove-Item -LiteralPath $resultPath -Force
  }
  $windowsPowerShell = Join-Path `
    $env:WINDIR `
    "System32\WindowsPowerShell\v1.0\powershell.exe"
  Remove-ValidationCaptureFiles -Paths $validationCapturePaths
  $validationOutput = @()
  $validationErrorOutput = @()
  try {
    $validationProcess = Start-Process `
      -FilePath $windowsPowerShell `
      -ArgumentList @(
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy Bypass",
        "-File `"$environmentBScript`""
      ) `
      -WindowStyle Hidden `
      -RedirectStandardOutput $validationStdoutPath `
      -RedirectStandardError $validationStderrPath `
      -Wait `
      -PassThru
    $validationExitCode = $validationProcess.ExitCode
    if (Test-Path -LiteralPath $validationStdoutPath -PathType Leaf) {
      $validationOutput = @(
        Get-Content -LiteralPath $validationStdoutPath |
          ForEach-Object { [string]$_ }
      )
    }
    if (Test-Path -LiteralPath $validationStderrPath -PathType Leaf) {
      $validationErrorOutput = @(
        Get-Content -LiteralPath $validationStderrPath |
          ForEach-Object { [string]$_ }
      )
    }
  } finally {
    Remove-ValidationCaptureFiles -Paths $validationCapturePaths
  }
  if ($validationExitCode -ne 0) {
    $failureEvidence = [ordered]@{
      exit_code = $validationExitCode
      stdout = $validationOutput
      stderr = $validationErrorOutput
    } | ConvertTo-Json -Depth 5 -Compress
    throw "Environment B validationが失敗しました: $failureEvidence"
  }
  if (-not (Test-Path -LiteralPath $resultPath -PathType Leaf)) {
    throw "Environment B validation resultが生成されませんでした。"
  }
  $validationResult = Get-Content -LiteralPath $resultPath -Raw -Encoding utf8 |
    ConvertFrom-Json -ErrorAction Stop
  if (-not $validationResult.Passed) {
    throw "Environment B validationが合格を報告しませんでした。"
  }

  return [ordered]@{
    schema_version = 1
    action = "SelfCheck"
    captured_at_utc = [DateTime]::UtcNow.ToString("o")
    computer_name = $env:COMPUTERNAME
    app_sha256 = $appHash
    environment_b_script_sha256 = $environmentBScriptHash
    runtime = $runtime
    validation_exit_code = $validationExitCode
    output = $validationOutput
    error_output = $validationErrorOutput
    validation = $validationResult
  }
}

function Get-InstallerPath {
  return Resolve-PayloadFile -VolumeLabel $PayloadVolumeLabel -RelativePath $InstallerRelativePath
}

function Test-GuestPathWithin {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$Root
  )
  $fullPath = [IO.Path]::GetFullPath($Path)
  $fullRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
  return $fullPath.StartsWith($fullRoot, [StringComparison]::OrdinalIgnoreCase)
}

function Assert-ProfilePath {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$Label
  )
  $profile = [IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd('\')
  $fullPath = [IO.Path]::GetFullPath($Path)
  if (-not (Test-GuestPathWithin -Path $fullPath -Root $profile)) {
    throw "$Label が専用guest profileの外側です: $fullPath"
  }

  $existing = $fullPath
  while (-not (Test-Path -LiteralPath $existing)) {
    $parent = Split-Path -Parent $existing
    if ([string]::IsNullOrWhiteSpace($parent) -or $parent -ceq $existing) {
      throw "$Label の既存親directoryを確認できません: $fullPath"
    }
    $existing = $parent
  }
  while ($true) {
    $item = Get-Item -LiteralPath $existing -Force -ErrorAction Stop
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
      throw "$Label のpath chainにreparse pointがあります: $($item.FullName)"
    }
    if ($item.FullName.TrimEnd('\') -ieq $profile) {
      break
    }
    $parent = Split-Path -Parent $item.FullName
    if ([string]::IsNullOrWhiteSpace($parent) -or $parent -ceq $item.FullName) {
      throw "$Label のpath chainがguest profileへ到達しません: $fullPath"
    }
    $existing = $parent
  }
  return $fullPath
}

function Assert-RegularTree {
  param([Parameter(Mandatory = $true)][string]$Root)
  if (-not (Test-Path -LiteralPath $Root -PathType Container)) { return }
  foreach ($item in @(
    Get-Item -LiteralPath $Root -Force -ErrorAction Stop
    Get-ChildItem -LiteralPath $Root -Force -Recurse -ErrorAction Stop
  )) {
    $linkType = $item.PSObject.Properties["LinkType"]
    if (
      ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
      ($null -ne $linkType -and -not [string]::IsNullOrWhiteSpace([string]$linkType.Value))
    ) {
      throw "検証対象pathにlinkまたはreparse pointがあります: $($item.FullName)"
    }
  }
}

function Get-TreeSnapshot {
  param([Parameter(Mandatory = $true)][string]$Root)
  if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
    return [ordered]@{ exists = $false; entries = @() }
  }
  Assert-RegularTree -Root $Root
  $base = (Get-Item -LiteralPath $Root -Force).FullName.TrimEnd('\')
  $entries = @(
    Get-ChildItem -LiteralPath $Root -Force -Recurse -ErrorAction Stop |
      Sort-Object FullName |
      ForEach-Object {
        if (-not $_.FullName.StartsWith($base + '\', [StringComparison]::OrdinalIgnoreCase)) {
          throw "検証対象entryがtree rootの外側です: $($_.FullName)"
        }
        $relative = $_.FullName.Substring($base.Length + 1).Replace('\', '/')
        if ($_.PSIsContainer) {
          [ordered]@{ path = $relative; type = "directory" }
        } else {
          [ordered]@{
            path = $relative
            type = "file"
            size = $_.Length
            sha256 = Get-Sha256 -Path $_.FullName
          }
        }
      }
  )
  return [ordered]@{ exists = $true; entries = $entries }
}

function Get-RegularFileState {
  param([Parameter(Mandatory = $true)][string]$Path)
  if (-not (Test-Path -LiteralPath $Path)) {
    return [ordered]@{ exists = $false; size = $null; sha256 = $null }
  }
  $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
  $linkType = $item.PSObject.Properties["LinkType"]
  if (
    $item.PSIsContainer -or
    ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
    ($null -ne $linkType -and -not [string]::IsNullOrWhiteSpace([string]$linkType.Value))
  ) {
    throw "検証対象fileがregular fileではありません: $Path"
  }
  return [ordered]@{
    exists = $true
    size = $item.Length
    sha256 = Get-Sha256 -Path $item.FullName
  }
}

function Get-RegistryState {
  param([Parameter(Mandatory = $true)][string]$Path)
  if (-not (Test-Path -LiteralPath $Path)) {
    return [ordered]@{ exists = $false; values = @() }
  }
  $properties = Get-ItemProperty -LiteralPath $Path -ErrorAction Stop
  $values = @(
    $properties.PSObject.Properties |
      Where-Object { $_.Name -cnotmatch '^PS' } |
      Sort-Object Name |
      ForEach-Object {
        [ordered]@{ name = $_.Name; value = [string]$_.Value }
      }
  )
  return [ordered]@{ exists = $true; values = $values }
}

function Get-InstallerTestPaths {
  $profile = [IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd('\')
  if ((Split-Path -Leaf $profile) -ine "vmtest") {
    throw "installer VM検証は専用guest user vmtestでのみ実行できます: $profile"
  }
  $expectedLocalAppData = Join-Path $profile "AppData\Local"
  $expectedRoamingAppData = Join-Path $profile "AppData\Roaming"
  if ([IO.Path]::GetFullPath($env:LOCALAPPDATA) -ine [IO.Path]::GetFullPath($expectedLocalAppData)) {
    throw "LOCALAPPDATAが専用guest profileの固定pathではありません。"
  }
  if ([IO.Path]::GetFullPath($env:APPDATA) -ine [IO.Path]::GetFullPath($expectedRoamingAppData)) {
    throw "APPDATAが専用guest profileの固定pathではありません。"
  }

  $paths = [ordered]@{
    profile = $profile
    local_app_data = $expectedLocalAppData
    install_root = Join-Path $expectedLocalAppData "Programs\LoLReplayTool"
    user_data_root = Join-Path $expectedLocalAppData "LoLReplayTool"
    start_menu = Join-Path $expectedRoamingAppData "Microsoft\Windows\Start Menu\Programs\LoL Replay Tool.lnk"
    desktop = Join-Path ([Environment]::GetFolderPath("Desktop")) "LoL Replay Tool.lnk"
    uninstall_registry = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{B8D87E69-41F7-4B28-978D-2F8FA5AF4BE2}_is1"
    log_parent = Join-Path $expectedLocalAppData "Temp"
  }
  foreach ($name in @("install_root", "user_data_root", "start_menu", "desktop", "log_parent")) {
    $paths[$name] = Assert-ProfilePath -Path $paths[$name] -Label $name
  }
  return [pscustomobject]$paths
}

function Get-InstallState {
  param([Parameter(Mandatory = $true)][psobject]$Paths)
  return [ordered]@{
    install_tree = Get-TreeSnapshot -Root $Paths.install_root
    user_data_tree = Get-TreeSnapshot -Root $Paths.user_data_root
    uninstall_registry = Get-RegistryState -Path $Paths.uninstall_registry
    start_menu = Get-RegularFileState -Path $Paths.start_menu
    desktop = Get-RegularFileState -Path $Paths.desktop
  }
}

function ConvertTo-StateJson {
  param([Parameter(Mandatory = $true)]$Value)
  return ($Value | ConvertTo-Json -Depth 20 -Compress)
}

function Assert-StateEqual {
  param(
    [Parameter(Mandatory = $true)]$Before,
    [Parameter(Mandatory = $true)]$After,
    [Parameter(Mandatory = $true)][string]$Label
  )
  if ((ConvertTo-StateJson -Value $Before) -cne (ConvertTo-StateJson -Value $After)) {
    throw "$Label のbefore/after stateが一致しません。"
  }
}

function Assert-UserDataPreservedAfterUninstall {
  param(
    [Parameter(Mandatory = $true)]$Before,
    [Parameter(Mandatory = $true)]$After,
    [Parameter(Mandatory = $true)][string]$Label
  )
  if ((ConvertTo-StateJson -Value $Before) -ceq (ConvertTo-StateJson -Value $After)) {
    return
  }
  if (-not $Before.exists -or -not $After.exists) {
    throw "$Label のuser-data rootが消失または未作成です。"
  }
  $beforeEntries = @($Before.entries)
  $afterEntries = @($After.entries)
  $bin = @($beforeEntries | Where-Object {
      $_.path -ceq "bin" -and $_.type -ceq "directory"
    })
  $binChildren = @($beforeEntries | Where-Object {
      $_.path.StartsWith("bin/", [StringComparison]::OrdinalIgnoreCase)
    })
  if ($bin.Count -ne 1 -or $binChildren.Count -ne 0) {
    throw "$Label の許可できないuser-data差分です。"
  }
  $expectedEntries = @($beforeEntries | Where-Object { $_.path -cne "bin" })
  $expected = [ordered]@{ exists = $true; entries = $expectedEntries }
  if ((ConvertTo-StateJson -Value $expected) -cne (ConvertTo-StateJson -Value $After)) {
    throw "$Label のuser-data差分が空のbin directory削除だけではありません。"
  }
}

function Assert-InstallerAbsentState {
  param(
    [Parameter(Mandatory = $true)]$State,
    [Parameter(Mandatory = $true)][string]$Label
  )
  if (
    $State.install_tree.exists -or
    $State.uninstall_registry.exists -or
    $State.start_menu.exists -or
    $State.desktop.exists
  ) {
    throw "$Label にapp、registry、またはshortcutが存在します。"
  }
}

function Assert-InstallerPresentState {
  param(
    [Parameter(Mandatory = $true)]$State,
    [Parameter(Mandatory = $true)][string]$Label
  )
  if (
    -not $State.install_tree.exists -or
    -not $State.uninstall_registry.exists -or
    -not $State.start_menu.exists -or
    $State.desktop.exists
  ) {
    throw "$Label のapp、registry、またはshortcut stateが不正です。"
  }
}

function New-RandomSentinel {
  param([Parameter(Mandatory = $true)][string]$Path)
  if (Test-Path -LiteralPath $Path) {
    throw "controlled sentinelが既に存在します: $Path"
  }
  $bytes = [byte[]]::new(32)
  $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
  try {
    $rng.GetBytes($bytes)
  } finally {
    $rng.Dispose()
  }
  [IO.File]::WriteAllBytes($Path, $bytes)
  return Get-RegularFileState -Path $Path
}

function Limit-Text {
  param([AllowNull()][string]$Text)
  if ($null -eq $Text -or $Text.Length -le 12000) { return $Text }
  return $Text.Substring(0, 6000) + "`n...[truncated]...`n" + $Text.Substring($Text.Length - 6000)
}

function Invoke-BoundedProcess {
  param(
    [Parameter(Mandatory = $true)][string]$FilePath,
    [Parameter(Mandatory = $true)][string[]]$Arguments,
    [Parameter(Mandatory = $true)][string]$WorkingDirectory,
    [int]$TimeoutMilliseconds = 300000
  )
  $file = Get-Item -LiteralPath $FilePath -Force -ErrorAction Stop
  $linkType = $file.PSObject.Properties["LinkType"]
  if (
    $file.PSIsContainer -or
    ($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
    ($null -ne $linkType -and -not [string]::IsNullOrWhiteSpace([string]$linkType.Value))
  ) {
    throw "実行対象がregular fileではありません: $FilePath"
  }
  if (-not (Test-Path -LiteralPath $WorkingDirectory -PathType Container)) {
    throw "process working directoryがありません: $WorkingDirectory"
  }

  $startInfo = [Diagnostics.ProcessStartInfo]::new()
  $startInfo.FileName = $file.FullName
  $startInfo.WorkingDirectory = $WorkingDirectory
  $startInfo.UseShellExecute = $false
  $startInfo.CreateNoWindow = $true
  $startInfo.RedirectStandardOutput = $true
  $startInfo.RedirectStandardError = $true
  foreach ($argument in $Arguments) {
    if ([string]::IsNullOrWhiteSpace($argument) -or $argument -match '[\s"]') {
      throw "process argumentに空白またはquoteを含めることはできません: $argument"
    }
  }
  $startInfo.Arguments = $Arguments -join " "
  $utf8 = [Text.UTF8Encoding]::new($false)
  $startInfo.StandardOutputEncoding = $utf8
  $startInfo.StandardErrorEncoding = $utf8

  $process = [Diagnostics.Process]::new()
  $process.StartInfo = $startInfo
  try {
    if (-not $process.Start()) {
      throw "processを開始できません: $FilePath"
    }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit($TimeoutMilliseconds)) {
      $process.Kill()
      $process.WaitForExit()
      throw "processがtimeoutしました: $FilePath"
    }
    $stdout = $stdoutTask.Result
    $stderr = $stderrTask.Result
    return [ordered]@{
      exit_code = $process.ExitCode
      stdout = Limit-Text -Text $stdout
      stderr = Limit-Text -Text $stderr
    }
  } finally {
    $process.Dispose()
  }
}

function Invoke-Installer {
  param(
    [Parameter(Mandatory = $true)][psobject]$Paths,
    [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$Arguments
  )
  $installer = Get-InstallerPath
  $installerHash = Assert-FileHash -Path $installer -ExpectedSha256 $InstallerSha256 -Label "application installer"
  $logRoot = Join-Path $Paths.log_parent ("LoLReplayTool-VM-Installer-" + [guid]::NewGuid().ToString("N"))
  [void](Assert-ProfilePath -Path $logRoot -Label "installer log root")
  if (Test-Path -LiteralPath $logRoot) {
    throw "installer log rootが既に存在します: $logRoot"
  }
  $logCreated = $false
  $processResult = $null
  $logText = $null
  $logCleanup = $false
  try {
    New-Item -ItemType Directory -Path $logRoot -ErrorAction Stop | Out-Null
    $logCreated = $true
    Assert-RegularTree -Root $logRoot
    $logPath = Join-Path $logRoot "setup.log"
    $allArguments = @(
      "/SP-", "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NOCANCEL", "/NORESTART",
      "/NOCLOSEAPPLICATIONS", "/NORESTARTAPPLICATIONS", "/TASKS=", "/LANG=japanese",
      "/LOG=$logPath"
    ) + $Arguments
    $processResult = Invoke-BoundedProcess `
      -FilePath $installer `
      -Arguments $allArguments `
      -WorkingDirectory $logRoot
    if (Test-Path -LiteralPath $logPath -PathType Leaf) {
      $logText = Limit-Text -Text (Get-Content -LiteralPath $logPath -Raw -ErrorAction Stop)
    }
  } finally {
    if ($logCreated) {
      Assert-RegularTree -Root $logRoot
      $resolvedLogRoot = (Resolve-Path -LiteralPath $logRoot -ErrorAction Stop).Path
      if ($resolvedLogRoot -ine $logRoot) {
        throw "installer log rootの物理pathが変化しました。"
      }
      [void](Assert-ProfilePath -Path $resolvedLogRoot -Label "installer log cleanup")
      Remove-Item -LiteralPath $resolvedLogRoot -Force -Recurse -ErrorAction Stop
      if (Test-Path -LiteralPath $logRoot) {
        throw "installer log rootを削除できません。"
      }
      $logCleanup = $true
    }
  }
  return [ordered]@{
    installer = $InstallerRelativePath
    installer_sha256 = $installerHash
    exit_code = $processResult.exit_code
    stdout = $processResult.stdout
    stderr = $processResult.stderr
    log = $logText
    log_cleanup = $logCleanup
  }
}

function Invoke-InstallerEnvironmentA {
  $paths = Get-InstallerTestPaths
  $initial = Get-InstallState -Paths $paths
  Assert-InstallerAbsentState -State $initial -Label "Environment A initial state"
  if ($initial.user_data_tree.exists) {
    throw "Environment Aのcontrolled user-data rootが既に存在します。"
  }
  New-Item -ItemType Directory -Path $paths.user_data_root -ErrorAction Stop | Out-Null
  Assert-RegularTree -Root $paths.user_data_root
  $sentinelPath = Join-Path $paths.user_data_root "vm-a-sentinel.bin"
  $sentinelState = New-RandomSentinel -Path $sentinelPath

  $before = Get-InstallState -Paths $paths
  $result = Invoke-Installer -Paths $paths -Arguments @()
  $after = Get-InstallState -Paths $paths
  if ($result.exit_code -ne 7) {
    throw "Environment A installerが期待するexit 7ではありません: $($result.exit_code)"
  }
  Assert-InstallerAbsentState -State $after -Label "Environment A new-install rejection"
  Assert-StateEqual -Before $before -After $after -Label "Environment A new-install rejection"

  $controlledCreated = $false
  $controlledBefore = $null
  $controlledAfter = $null
  $controlledResult = $null
  $cleanupState = $null
  $cleanup = $false
  $controlledParent = Split-Path -Parent $paths.install_root
  [void](Assert-ProfilePath -Path $controlledParent -Label "controlled install parent")
  $controlledParentCreated = $false
  try {
    if (Test-Path -LiteralPath $paths.install_root) {
      throw "controlled install rootが既に存在します。"
    }
    if (-not (Test-Path -LiteralPath $controlledParent -PathType Container)) {
      New-Item -ItemType Directory -Path $controlledParent -ErrorAction Stop | Out-Null
      $controlledParentCreated = $true
      Assert-RegularTree -Root $controlledParent
    }
    New-Item -ItemType Directory -Path $paths.install_root -ErrorAction Stop | Out-Null
    $controlledCreated = $true
    Assert-RegularTree -Root $paths.install_root
    $controlledSentinelPath = Join-Path $paths.install_root "vm-a-controlled-sentinel.bin"
    [void](New-RandomSentinel -Path $controlledSentinelPath)
    $controlledBefore = Get-InstallState -Paths $paths
    $controlledResult = Invoke-Installer -Paths $paths -Arguments @()
    $controlledAfter = Get-InstallState -Paths $paths
    if ($controlledResult.exit_code -ne 7) {
      throw "Environment A controlled updateが期待するexit 7ではありません: $($controlledResult.exit_code)"
    }
    Assert-StateEqual `
      -Before $controlledBefore `
      -After $controlledAfter `
      -Label "Environment A controlled update rejection"
  } finally {
    if ($controlledCreated) {
      Assert-RegularTree -Root $paths.install_root
      $resolvedRoot = (Resolve-Path -LiteralPath $paths.install_root -ErrorAction Stop).Path
      if ($resolvedRoot -ine $paths.install_root) {
        throw "controlled install rootの物理pathが変化しました。"
      }
      [void](Assert-ProfilePath -Path $resolvedRoot -Label "controlled install root cleanup")
      Remove-Item -LiteralPath $resolvedRoot -Force -Recurse -ErrorAction Stop
      if (Test-Path -LiteralPath $paths.install_root) {
        throw "controlled install rootを削除できません。"
      }
      $cleanup = $true
    }
    if ($controlledParentCreated) {
      Assert-RegularTree -Root $controlledParent
      if (@(Get-ChildItem -LiteralPath $controlledParent -Force -ErrorAction Stop).Count -ne 0) {
        throw "controlled install parentに想定外のentryが残っています。"
      }
      Remove-Item -LiteralPath $controlledParent -Force -ErrorAction Stop
      if (Test-Path -LiteralPath $controlledParent) {
        throw "controlled install parentを削除できません。"
      }
    }
  }
  $cleanupState = Get-InstallState -Paths $paths
  Assert-InstallerAbsentState -State $cleanupState -Label "Environment A cleanup"
  if ((Get-RegularFileState -Path $sentinelPath).sha256 -cne $sentinelState.sha256) {
    throw "Environment A cleanup後にuser-data sentinelが変化しました。"
  }
  return [ordered]@{
    schema_version = 1
    action = "InstallerEnvironmentA"
    captured_at_utc = [DateTime]::UtcNow.ToString("o")
    computer_name = $env:COMPUTERNAME
    passed = $true
    sentinel_sha256 = $sentinelState.sha256
    before = $before
    after = $after
    result = $result
    controlled_before = $controlledBefore
    controlled_after = $controlledAfter
    controlled_result = $controlledResult
    controlled_cleanup = $cleanup
    cleanup_state = $cleanupState
  }
}

function Invoke-InstallerEnvironmentB {
  $paths = Get-InstallerTestPaths
  $precondition = Get-InstallState -Paths $paths
  Assert-InstallerAbsentState -State $precondition -Label "Environment B precondition"
  if (-not $precondition.user_data_tree.exists) {
    throw "Environment B controlled user-data rootがありません。"
  }
  $aSentinelPath = Join-Path $paths.user_data_root "vm-a-sentinel.bin"
  $aSentinelState = Get-RegularFileState -Path $aSentinelPath
  if (-not $aSentinelState.exists) {
    throw "Environment BへA sentinelが引き継がれていません。"
  }

  $install = Invoke-Installer -Paths $paths -Arguments @()
  if ($install.exit_code -ne 0) {
    throw "Environment B installer新規installが失敗しました: $($install.exit_code)"
  }
  $afterInstall = Get-InstallState -Paths $paths
  Assert-InstallerPresentState -State $afterInstall -Label "Environment B install"
  $appPath = Join-Path $paths.install_root "LoLReplayTool.exe"
  $uninstallerPath = Join-Path $paths.install_root "unins000.exe"
  $appBeforeUpdate = Get-RegularFileState -Path $appPath
  $uninstallerState = Get-RegularFileState -Path $uninstallerPath
  if (-not $appBeforeUpdate.exists -or -not $uninstallerState.exists) {
    throw "Environment B installed appまたはuninstallerがありません。"
  }
  if ((Get-RegularFileState -Path $aSentinelPath).sha256 -cne $aSentinelState.sha256) {
    throw "Environment B installがA sentinelを変更しました。"
  }

  $selfCheck = Invoke-BoundedProcess `
    -FilePath $appPath `
    -Arguments @("--self-check", "--json") `
    -WorkingDirectory $paths.install_root
  if ($selfCheck.exit_code -ne 0) {
    throw "installed app self-checkが失敗しました: $($selfCheck.exit_code)"
  }
  try {
    $selfJson = $selfCheck.stdout | ConvertFrom-Json -ErrorAction Stop
  } catch {
    throw "installed app self-check JSONが不正です: $($_.Exception.Message)"
  }
  $analyticsChecks = @($selfJson.checks | Where-Object { $_.name -ceq "analytics_runtime" })
  $nativeChecks = @($selfJson.checks | Where-Object { $_.name -ceq "native_modules" })
  if (
    -not [bool]$selfJson.ok -or
    $analyticsChecks.Count -ne 1 -or
    $analyticsChecks[0].status -cne "ok" -or
    $nativeChecks.Count -ne 1 -or
    $nativeChecks[0].status -cne "ok"
  ) {
    throw "installed app self-checkのnative/analytics検査が不合格です。"
  }

  $updateSentinelPath = Join-Path $paths.user_data_root "vm-b-update-sentinel.bin"
  $updateSentinelState = New-RandomSentinel -Path $updateSentinelPath
  $beforeUpdate = Get-InstallState -Paths $paths
  $update = Invoke-Installer -Paths $paths -Arguments @()
  if ($update.exit_code -ne 0) {
    throw "Environment B installer上書き更新が失敗しました: $($update.exit_code)"
  }
  $afterUpdate = Get-InstallState -Paths $paths
  Assert-InstallerPresentState -State $afterUpdate -Label "Environment B update"
  Assert-StateEqual `
    -Before $beforeUpdate.user_data_tree `
    -After $afterUpdate.user_data_tree `
    -Label "Environment B update user-data"
  if ((Get-RegularFileState -Path $aSentinelPath).sha256 -cne $aSentinelState.sha256) {
    throw "Environment B updateがA sentinelを変更しました。"
  }
  if ((Get-RegularFileState -Path $updateSentinelPath).sha256 -cne $updateSentinelState.sha256) {
    throw "Environment B updateがupdate sentinelを変更しました。"
  }
  $appAfterUpdate = Get-RegularFileState -Path $appPath
  if ($appAfterUpdate.sha256 -cne $appBeforeUpdate.sha256) {
    throw "同一installerによるupdate後にapp hashが変化しました。"
  }

  $uninstallerHash = (Get-RegularFileState -Path $uninstallerPath).sha256
  $uninstall = Invoke-BoundedProcess `
    -FilePath $uninstallerPath `
    -Arguments @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART") `
    -WorkingDirectory $paths.log_parent
  if ($uninstall.exit_code -ne 0) {
    throw "silent uninstallが失敗しました: $($uninstall.exit_code)"
  }
  $deadline = [DateTime]::UtcNow.AddSeconds(15)
  do {
    $installExists = Test-Path -LiteralPath $paths.install_root
    $registryExists = Test-Path -LiteralPath $paths.uninstall_registry
    $startMenuExists = Test-Path -LiteralPath $paths.start_menu
    $desktopExists = Test-Path -LiteralPath $paths.desktop
    if (-not $installExists -and -not $registryExists -and -not $startMenuExists -and -not $desktopExists) {
      break
    }
    Start-Sleep -Milliseconds 200
  } while ([DateTime]::UtcNow -lt $deadline)

  $afterUninstall = Get-InstallState -Paths $paths
  Assert-InstallerAbsentState -State $afterUninstall -Label "Environment B uninstall"
  Assert-UserDataPreservedAfterUninstall `
    -Before $afterUpdate.user_data_tree `
    -After $afterUninstall.user_data_tree `
    -Label "Environment B uninstall user-data"
  if ((Get-RegularFileState -Path $aSentinelPath).sha256 -cne $aSentinelState.sha256) {
    throw "uninstall後にA sentinelが変化しました。"
  }
  if ((Get-RegularFileState -Path $updateSentinelPath).sha256 -cne $updateSentinelState.sha256) {
    throw "uninstall後にupdate sentinelが変化しました。"
  }

  return [ordered]@{
    schema_version = 1
    action = "InstallerEnvironmentB"
    captured_at_utc = [DateTime]::UtcNow.ToString("o")
    computer_name = $env:COMPUTERNAME
    passed = $true
    a_sentinel_sha256 = $aSentinelState.sha256
    update_sentinel_sha256 = $updateSentinelState.sha256
    precondition = $precondition
    install = $install
    after_install = $afterInstall
    self_check_exit_code = $selfCheck.exit_code
    self_check = [ordered]@{
      ok = [bool]$selfJson.ok
      analytics_runtime = $analyticsChecks[0]
      native_modules = $nativeChecks[0]
      stdout = $selfCheck.stdout
      stderr = $selfCheck.stderr
    }
    before_update = $beforeUpdate
    update = $update
    after_update = $afterUpdate
    app_sha256_before_update = $appBeforeUpdate.sha256
    app_sha256_after_update = $appAfterUpdate.sha256
    uninstaller_sha256 = $uninstallerHash
    uninstall_exit_code = $uninstall.exit_code
    uninstall = $uninstall
    after_uninstall = $afterUninstall
  }
}

$result = switch ($Action) {
  "Inspect" { Get-Inspection }
  "InstallRuntime" { Install-ExternalRuntime }
  "SelfCheck" { Invoke-PackagedSelfCheck }
  "InstallerEnvironmentA" { Invoke-InstallerEnvironmentA }
  "InstallerEnvironmentB" { Invoke-InstallerEnvironmentB }
}

$result | ConvertTo-Json -Depth 12 -Compress
