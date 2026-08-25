[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [ValidateSet("Capture", "Plan", "Doctor", "Run")]
  [string]$Action,
  [Parameter(Mandatory = $true)]
  [string]$ConfigPath,
  [switch]$ConfirmSnapshotRestore,
  [switch]$ConfirmRuntimeInstall,
  [switch]$ConfirmVmPasswordProcessExposure
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Import-Module Microsoft.PowerShell.Utility -ErrorAction Stop
Import-Module Microsoft.WSMan.Management -ErrorAction Stop

$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$repositoryRoot = Split-Path -Parent $scriptDirectory
$guestScript = Join-Path $scriptDirectory "windows_vm_lab_guest.ps1"
$bootstrapScript = Join-Path $scriptDirectory "windows_vm_lab_bootstrap.ps1"
$utf8NoBom = [Text.UTF8Encoding]::new($false)
$fixedVmName = "LoLReplayTool-VC-Runtime-Lab"
$captureValue = "capture"

function Get-AbsolutePath {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Path,
    [Parameter(Mandatory = $true)]
    [string]$Label
  )

  if ([string]::IsNullOrWhiteSpace($Path) -or -not [IO.Path]::IsPathRooted($Path)) {
    throw "$Label は絶対pathで指定してください: $Path"
  }
  return [IO.Path]::GetFullPath($Path)
}

function Test-PathWithin {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Path,
    [Parameter(Mandatory = $true)]
    [string]$Root
  )

  $fullPath = [IO.Path]::GetFullPath($Path)
  $fullRoot = [IO.Path]::GetFullPath($Root).TrimEnd(
    [IO.Path]::DirectorySeparatorChar,
    [IO.Path]::AltDirectorySeparatorChar
  ) + [IO.Path]::DirectorySeparatorChar
  return $fullPath.StartsWith($fullRoot, [StringComparison]::OrdinalIgnoreCase)
}

function Assert-NoReparsePointInPath {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Path,
    [Parameter(Mandatory = $true)]
    [string]$Label
  )

  $currentPath = [IO.Path]::GetFullPath($Path)
  if (-not (Test-Path -LiteralPath $currentPath)) {
    throw "$Label が見つかりません: $currentPath"
  }
  while (-not [string]::IsNullOrWhiteSpace($currentPath)) {
    $item = Get-Item -LiteralPath $currentPath -Force -ErrorAction Stop
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
      throw "$Label のpathにreparse pointがあります: $($item.FullName)"
    }
    $parentPath = Split-Path -Parent $item.FullName
    if (
      [string]::IsNullOrWhiteSpace($parentPath) -or
      $parentPath.Equals($item.FullName, [StringComparison]::OrdinalIgnoreCase)
    ) {
      break
    }
    $currentPath = $parentPath
  }
}

function Get-RequiredString {
  param(
    [Parameter(Mandatory = $true)]
    [psobject]$Config,
    [Parameter(Mandatory = $true)]
    [string]$Name
  )

  $property = $Config.PSObject.Properties[$Name]
  if ($null -eq $property -or [string]::IsNullOrWhiteSpace([string]$property.Value)) {
    throw "VM lab configに必須文字列がありません: $Name"
  }
  return [string]$property.Value
}

function Assert-RelativePayloadPath {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Path,
    [Parameter(Mandatory = $true)]
    [string]$Label
  )

  if (
    [IO.Path]::IsPathRooted($Path) -or
    $Path -match '(^|[\\/])\.\.([\\/]|$)'
  ) {
    throw "$Label は親参照を含まない相対pathで指定してください: $Path"
  }
}

function Assert-SamePrivate24Network {
  param(
    [Parameter(Mandatory = $true)]
    [string]$HostAddress,
    [Parameter(Mandatory = $true)]
    [string]$GuestAddress
  )

  $hostIp = $null
  $guestIp = $null
  if (
    -not [Net.IPAddress]::TryParse($HostAddress, [ref]$hostIp) -or
    -not [Net.IPAddress]::TryParse($GuestAddress, [ref]$guestIp) -or
    $hostIp.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or
    $guestIp.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork
  ) {
    throw "host/guest addressはIPv4で指定してください。"
  }
  $hostBytes = $hostIp.GetAddressBytes()
  $guestBytes = $guestIp.GetAddressBytes()
  $same24 = (
    $hostBytes[0] -eq $guestBytes[0] -and
    $hostBytes[1] -eq $guestBytes[1] -and
    $hostBytes[2] -eq $guestBytes[2]
  )
  $private = (
    $hostBytes[0] -eq 10 -or
    ($hostBytes[0] -eq 172 -and $hostBytes[1] -ge 16 -and $hostBytes[1] -le 31) -or
    ($hostBytes[0] -eq 192 -and $hostBytes[1] -eq 168)
  )
  if (-not $same24 -or -not $private -or $HostAddress -ceq $GuestAddress) {
    throw "host/guest addressは同じprivate /24内の異なるaddressにしてください。"
  }
}

function Read-LabConfig {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Path
  )

  $resolvedConfigPath = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
  if (-not (Test-Path -LiteralPath $resolvedConfigPath -PathType Leaf)) {
    throw "VM lab configが見つかりません: $Path"
  }
  $raw = Get-Content -LiteralPath $resolvedConfigPath -Raw -Encoding utf8
  try {
    $config = $raw | ConvertFrom-Json -ErrorAction Stop
  } catch {
    throw "VM lab configをJSONとして解釈できません: $($_.Exception.Message)"
  }

  $allowedProperties = @(
    "schema_version",
    "vmrun_path",
    "vmx_path",
    "vm_encryption_credential_path",
    "expected_vm_uuid",
    "expected_vm_encryption_type",
    "expected_guest_mac",
    "vmx_file_sha256",
    "vm_definition_fingerprint_sha256",
    "snapshot_a",
    "snapshot_uid",
    "snapshot_fingerprint_sha256",
    "vmware_network",
    "vmware_dhcp_config_path",
    "vmware_nat_config_path",
    "host_address",
    "guest_address",
    "guest_credential_path",
    "payload_iso_path",
    "payload_iso_sha256",
    "payload_volume_label",
    "runtime_installer_relative_path",
    "runtime_installer_sha256",
    "minimum_runtime_version",
    "app_relative_path",
    "app_sha256",
    "environment_b_script_relative_path",
    "environment_b_script_sha256",
    "payload_commit",
    "artifact_root"
  )
  $actualProperties = @($config.PSObject.Properties.Name)
  $unknownProperties = @($actualProperties | Where-Object { $_ -notin $allowedProperties })
  $missingProperties = @($allowedProperties | Where-Object { $_ -notin $actualProperties })
  if ($unknownProperties.Count -gt 0) {
    throw "VM lab configに未知のpropertyがあります: $($unknownProperties -join ', ')"
  }
  if ($missingProperties.Count -gt 0) {
    throw "VM lab configに必須propertyがありません: $($missingProperties -join ', ')"
  }
  if ([int]$config.schema_version -ne 2) {
    throw "未対応のVM lab config schemaです: $($config.schema_version)"
  }

  $vmrunPath = Get-AbsolutePath `
    -Path (Get-RequiredString -Config $config -Name "vmrun_path") `
    -Label "vmrun_path"
  $vmxPath = Get-AbsolutePath `
    -Path (Get-RequiredString -Config $config -Name "vmx_path") `
    -Label "vmx_path"
  $vmCredentialPath = Get-AbsolutePath `
    -Path (Get-RequiredString -Config $config -Name "vm_encryption_credential_path") `
    -Label "vm_encryption_credential_path"
  $guestCredentialPath = Get-AbsolutePath `
    -Path (Get-RequiredString -Config $config -Name "guest_credential_path") `
    -Label "guest_credential_path"
  $payloadIsoPath = Get-AbsolutePath `
    -Path (Get-RequiredString -Config $config -Name "payload_iso_path") `
    -Label "payload_iso_path"
  $artifactRoot = Get-AbsolutePath `
    -Path (Get-RequiredString -Config $config -Name "artifact_root") `
    -Label "artifact_root"
  $vmwareDhcpConfigPath = Get-AbsolutePath `
    -Path (Get-RequiredString -Config $config -Name "vmware_dhcp_config_path") `
    -Label "vmware_dhcp_config_path"
  $vmwareNatConfigPath = Get-AbsolutePath `
    -Path (Get-RequiredString -Config $config -Name "vmware_nat_config_path") `
    -Label "vmware_nat_config_path"

  foreach ($outsidePath in @(
    $vmxPath,
    $vmCredentialPath,
    $guestCredentialPath,
    $payloadIsoPath,
    $artifactRoot
  )) {
    if (Test-PathWithin -Path $outsidePath -Root $repositoryRoot) {
      throw "VM、credential、payload、evidenceはrepository外へ配置してください: $outsidePath"
    }
  }

  $snapshot = Get-RequiredString -Config $config -Name "snapshot_a"
  if ($snapshot -cne "A0-runtime-absent") {
    throw "Environment A snapshotは 'A0-runtime-absent' に固定してください。"
  }
  $network = Get-RequiredString -Config $config -Name "vmware_network"
  if ($network -cne "vmnet1") {
    throw "VM labはinternet routeを持たないhost-only vmnet1に固定してください。"
  }
  $hostAddress = Get-RequiredString -Config $config -Name "host_address"
  $guestAddress = Get-RequiredString -Config $config -Name "guest_address"
  Assert-SamePrivate24Network -HostAddress $hostAddress -GuestAddress $guestAddress

  $volumeLabel = Get-RequiredString -Config $config -Name "payload_volume_label"
  if ($volumeLabel -cnotmatch '^[A-Z0-9_-]{1,32}$') {
    throw "payload_volume_labelは1-32文字の英大文字、数字、_、-に限定してください。"
  }
  $runtimeRelativePath = Get-RequiredString `
    -Config $config `
    -Name "runtime_installer_relative_path"
  $appRelativePath = Get-RequiredString -Config $config -Name "app_relative_path"
  $environmentBScriptRelativePath = Get-RequiredString `
    -Config $config `
    -Name "environment_b_script_relative_path"
  Assert-RelativePayloadPath -Path $runtimeRelativePath -Label "runtime installer path"
  Assert-RelativePayloadPath -Path $appRelativePath -Label "application path"
  Assert-RelativePayloadPath `
    -Path $environmentBScriptRelativePath `
    -Label "Environment B script path"

  $payloadIsoSha256 = (Get-RequiredString `
    -Config $config `
    -Name "payload_iso_sha256").ToLowerInvariant()
  $runtimeInstallerSha256 = (Get-RequiredString `
    -Config $config `
    -Name "runtime_installer_sha256").ToLowerInvariant()
  $appSha256 = (Get-RequiredString `
    -Config $config `
    -Name "app_sha256").ToLowerInvariant()
  $environmentBScriptSha256 = (Get-RequiredString `
    -Config $config `
    -Name "environment_b_script_sha256").ToLowerInvariant()
  foreach ($hash in @(
    $payloadIsoSha256,
    $runtimeInstallerSha256,
    $appSha256,
    $environmentBScriptSha256
  )) {
    if ($hash -cnotmatch '^[0-9a-f]{64}$') {
      throw "VM lab configのSHA256が不正です。"
    }
  }
  $minimumRuntimeVersion = Get-RequiredString `
    -Config $config `
    -Name "minimum_runtime_version"
  $parsedVersion = $null
  if (-not [version]::TryParse($minimumRuntimeVersion, [ref]$parsedVersion)) {
    throw "minimum_runtime_versionが不正です: $minimumRuntimeVersion"
  }
  $payloadCommit = Get-RequiredString -Config $config -Name "payload_commit"
  if ($payloadCommit -cnotmatch '^[0-9a-f]{40}$') {
    throw "payload_commitは40文字の小文字Git commit SHAに固定してください。"
  }

  $captureAllowed = $Action -ceq "Capture"
  $expectedVmUuid = (Get-RequiredString `
    -Config $config `
    -Name "expected_vm_uuid").ToLowerInvariant()
  $expectedVmEncryptionType = (Get-RequiredString `
    -Config $config `
    -Name "expected_vm_encryption_type").ToLowerInvariant()
  $expectedGuestMac = (Get-RequiredString `
    -Config $config `
    -Name "expected_guest_mac").ToLowerInvariant()
  $snapshotUid = Get-RequiredString -Config $config -Name "snapshot_uid"
  $vmDefinitionFingerprintSha256 = (Get-RequiredString `
    -Config $config `
    -Name "vm_definition_fingerprint_sha256").ToLowerInvariant()
  $vmxFileSha256 = (Get-RequiredString `
    -Config $config `
    -Name "vmx_file_sha256").ToLowerInvariant()
  $snapshotFingerprintSha256 = (Get-RequiredString `
    -Config $config `
    -Name "snapshot_fingerprint_sha256").ToLowerInvariant()

  if (-not $captureAllowed -or $expectedVmUuid -cne $captureValue) {
    if ($expectedVmUuid -cnotmatch '^(?:[0-9a-f]{2} ){15}[0-9a-f]{2}$') {
      throw "expected_vm_uuidはVMXのuuid.biosを小文字で固定してください。"
    }
  }
  if (-not $captureAllowed -or $expectedVmEncryptionType -cne $captureValue) {
    if ($expectedVmEncryptionType -notin @("full", "partial")) {
      throw "expected_vm_encryption_typeはfullまたはpartialへ固定してください。"
    }
  }
  if (-not $captureAllowed -or $expectedGuestMac -cne $captureValue) {
    if ($expectedGuestMac -cnotmatch '^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$') {
      throw "expected_guest_macはVMXのMAC addressを小文字で固定してください。"
    }
  }
  if (-not $captureAllowed -or $snapshotUid -cne $captureValue) {
    if ($snapshotUid -cnotmatch '^\d+$') {
      throw "snapshot_uidはVMSDの数値UIDへ固定してください。"
    }
  }
  foreach ($fingerprint in @(
    $vmDefinitionFingerprintSha256,
    $vmxFileSha256,
    $snapshotFingerprintSha256
  )) {
    if (
      $fingerprint -cnotmatch '^[0-9a-f]{64}$' -and
      (-not $captureAllowed -or $fingerprint -cne $captureValue)
    ) {
      throw "VM lab definition fingerprintが不正です。"
    }
  }

  return [pscustomobject][ordered]@{
    schema_version = 2
    config_path = $resolvedConfigPath
    config_sha256 = (Get-FileHash -LiteralPath $resolvedConfigPath -Algorithm SHA256).Hash.ToLowerInvariant()
    vmrun_path = $vmrunPath
    vmx_path = $vmxPath
    vm_encryption_credential_path = $vmCredentialPath
    expected_vm_uuid = $expectedVmUuid
    expected_vm_encryption_type = $expectedVmEncryptionType
    expected_guest_mac = $expectedGuestMac
    vmx_file_sha256 = $vmxFileSha256
    vm_definition_fingerprint_sha256 = $vmDefinitionFingerprintSha256
    snapshot_a = $snapshot
    snapshot_uid = $snapshotUid
    snapshot_fingerprint_sha256 = $snapshotFingerprintSha256
    vmware_network = $network
    vmware_dhcp_config_path = $vmwareDhcpConfigPath
    vmware_nat_config_path = $vmwareNatConfigPath
    host_address = $hostAddress
    guest_address = $guestAddress
    guest_credential_path = $guestCredentialPath
    payload_iso_path = $payloadIsoPath
    payload_iso_sha256 = $payloadIsoSha256
    payload_volume_label = $volumeLabel
    runtime_installer_relative_path = $runtimeRelativePath
    runtime_installer_sha256 = $runtimeInstallerSha256
    minimum_runtime_version = $minimumRuntimeVersion
    app_relative_path = $appRelativePath
    app_sha256 = $appSha256
    environment_b_script_relative_path = $environmentBScriptRelativePath
    environment_b_script_sha256 = $environmentBScriptSha256
    payload_commit = $payloadCommit
    artifact_root = $artifactRoot
  }
}

function Get-StringSha256 {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Value
  )

  $algorithm = [Security.Cryptography.SHA256]::Create()
  try {
    $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
    return ([BitConverter]::ToString($algorithm.ComputeHash($bytes))).Replace(
      "-",
      ""
    ).ToLowerInvariant()
  } finally {
    $algorithm.Dispose()
  }
}

function Get-VmwareKeyValueFile {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Path
  )

  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    throw "VMware definition fileが見つかりません: $Path"
  }
  Assert-NoReparsePointInPath -Path $Path -Label "VMware definition file"
  $values = [Collections.Generic.Dictionary[string, string]]::new(
    [StringComparer]::OrdinalIgnoreCase
  )
  foreach ($line in @(Get-Content -LiteralPath $Path -Encoding utf8)) {
    if ($line -notmatch '^\s*([^#][^=]*?)\s*=\s*(.*?)\s*$') {
      continue
    }
    $key = $Matches[1].Trim()
    $value = $Matches[2].Trim()
    if (
      $value.Length -ge 2 -and
      $value[0] -eq '"' -and
      $value[$value.Length - 1] -eq '"'
    ) {
      $value = $value.Substring(1, $value.Length - 2)
    }
    if ($values.ContainsKey($key)) {
      throw "VMware definitionに重複keyがあります: $Path key=$key"
    }
    $values.Add($key, $value)
  }
  return ,$values
}

function Get-VmwareValue {
  param(
    [Parameter(Mandatory = $true)]
    [Collections.Generic.Dictionary[string, string]]$Values,
    [Parameter(Mandatory = $true)]
    [string]$Key,
    [switch]$Optional
  )

  $value = $null
  if ($Values.TryGetValue($Key, [ref]$value)) {
    return $value
  }
  if ($Optional) {
    return $null
  }
  throw "VMware definitionに必須keyがありません: $Key"
}

function Test-TrueVmwareValue {
  param(
    [AllowNull()]
    [string]$Value
  )

  return $null -ne $Value -and $Value.Equals("true", [StringComparison]::OrdinalIgnoreCase)
}

function Get-VmDefinition {
  param(
    [Parameter(Mandatory = $true)]
    [psobject]$Config,
    [switch]$ValidateExpected,
    [switch]$IgnoreRawVmxHash
  )

  $vmxPath = [IO.Path]::GetFullPath($Config.vmx_path)
  if (
    [IO.Path]::GetFileNameWithoutExtension($vmxPath) -cne $fixedVmName -or
    (Split-Path -Leaf (Split-Path -Parent $vmxPath)) -cne $fixedVmName
  ) {
    throw "対象VMは専用directory/name '$fixedVmName' に固定してください。"
  }
  $values = Get-VmwareKeyValueFile -Path $vmxPath
  $vmxFileSha256 = (
    Get-FileHash -LiteralPath $vmxPath -Algorithm SHA256
  ).Hash.ToLowerInvariant()
  $displayName = Get-VmwareValue -Values $values -Key "displayName"
  if ($displayName -cne $fixedVmName) {
    throw "VMX displayNameが専用lab名と一致しません。"
  }
  $uuid = (
    (Get-VmwareValue -Values $values -Key "uuid.bios").ToLowerInvariant() -replace
      '-',
      ' '
  ) -replace '\s+', ' '
  if ($uuid -cnotmatch '^(?:[0-9a-f]{2} ){15}[0-9a-f]{2}$') {
    throw "VMX uuid.biosを解釈できません: $uuid"
  }
  $encryptionType = (
    Get-VmwareValue -Values $values -Key "vmx.encryptionType"
  ).ToLowerInvariant()
  if ($encryptionType -notin @("full", "partial")) {
    throw "VMXは明示password付きのfullまたはpartial encryptionが必要です。"
  }
  if (-not (Test-TrueVmwareValue (Get-VmwareValue -Values $values -Key "vtpm.present"))) {
    throw "VMXにvTPMが存在しません。"
  }

  $nicIndexes = @(
    foreach ($key in @($values.Keys)) {
      if (
        $key -match '^ethernet(\d+)\.present$' -and
        (Test-TrueVmwareValue $values[$key])
      ) {
        $Matches[1]
      }
    }
  )
  if ($nicIndexes.Count -ne 1) {
    throw "VMXのpresent network adapterは1件だけにしてください。"
  }
  $nic = "ethernet$($nicIndexes[0])"
  $connectionType = (
    Get-VmwareValue -Values $values -Key "$nic.connectionType"
  ).ToLowerInvariant()
  $vnet = Get-VmwareValue -Values $values -Key "$nic.vnet"
  $nicStartConnected = Get-VmwareValue -Values $values -Key "$nic.startConnected"
  if (
    $connectionType -cne "custom" -or
    -not $vnet.Equals($Config.vmware_network, [StringComparison]::OrdinalIgnoreCase) -or
    -not (Test-TrueVmwareValue $nicStartConnected)
  ) {
    throw "VMX network adapterはCustom VMnet1の1枚だけに固定してください。"
  }
  $macSource = "$nic.generatedAddress"
  $mac = Get-VmwareValue -Values $values -Key $macSource -Optional
  if ([string]::IsNullOrWhiteSpace($mac)) {
    $macSource = "$nic.address"
    $mac = Get-VmwareValue -Values $values -Key $macSource
  }
  $mac = $mac.ToLowerInvariant()
  if ($mac -cnotmatch '^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$') {
    throw "VMX MAC addressを解釈できません: $mac"
  }

  $cdIndexes = @(
    foreach ($key in @($values.Keys)) {
      if (
        $key -match '^((?:ide|sata)\d+:\d+)\.present$' -and
        (Test-TrueVmwareValue $values[$key])
      ) {
        $device = $Matches[1]
        $deviceType = Get-VmwareValue `
          -Values $values `
          -Key "$device.deviceType" `
          -Optional
        if (
          $null -ne $deviceType -and
          $deviceType.Equals("cdrom-image", [StringComparison]::OrdinalIgnoreCase)
        ) {
          $device
        }
      }
    }
  )
  if ($cdIndexes.Count -ne 1) {
    throw "VMXには固定ISOを接続するcdrom-imageを1件だけ設定してください。"
  }
  $cd = $cdIndexes[0]
  $mountedIso = Get-AbsolutePath `
    -Path (Get-VmwareValue -Values $values -Key "$cd.fileName") `
    -Label "VMX mounted ISO"
  if (
    -not $mountedIso.Equals($Config.payload_iso_path, [StringComparison]::OrdinalIgnoreCase) -or
    -not (Test-TrueVmwareValue (
      Get-VmwareValue -Values $values -Key "$cd.startConnected"
    ))
  ) {
    throw "VMX cdrom-imageはconfigの固定payload ISOへ接続してください。"
  }

  $isolationKeys = @(
    "isolation.tools.copy.disable",
    "isolation.tools.paste.disable",
    "isolation.tools.dnd.disable"
  )
  foreach ($key in $isolationKeys) {
    if (-not (Test-TrueVmwareValue (Get-VmwareValue -Values $values -Key $key))) {
      throw "VMX isolation境界が無効です: $key"
    }
  }
  $hgfsDisabled = Get-VmwareValue `
    -Values $values `
    -Key "isolation.tools.hgfs.disable" `
    -Optional
  if ($null -ne $hgfsDisabled -and -not (Test-TrueVmwareValue $hgfsDisabled)) {
    throw "VMX HGFS isolation境界が無効です。"
  }
  $sharedFolderMaxNum = Get-VmwareValue `
    -Values $values `
    -Key "sharedFolder.maxNum" `
    -Optional
  $sharedFolderKeys = @(
    $values.Keys |
      Where-Object { $_ -match '^sharedFolder\d+\.' }
  )
  if (
    ($null -ne $sharedFolderMaxNum -and $sharedFolderMaxNum -cne "0") -or
    $sharedFolderKeys.Count -ne 0
  ) {
    throw "VMX shared folderは0件へ固定してください。"
  }

  $guestOs = (Get-VmwareValue -Values $values -Key "guestOS").ToLowerInvariant()
  $firmware = (Get-VmwareValue -Values $values -Key "firmware").ToLowerInvariant()
  $memoryMb = Get-VmwareValue -Values $values -Key "memsize"
  $processorCount = Get-VmwareValue -Values $values -Key "numvcpus"
  $virtualHardwareVersion = Get-VmwareValue -Values $values -Key "virtualHW.version"
  $parsedInteger = 0
  if (
    $guestOs -cne "windows11-64" -or
    $firmware -cne "efi" -or
    -not (Test-TrueVmwareValue (
      Get-VmwareValue -Values $values -Key "uefi.secureBoot.enabled"
    )) -or
    -not [int]::TryParse($memoryMb, [ref]$parsedInteger) -or
    $parsedInteger -lt 4096 -or
    -not [int]::TryParse($processorCount, [ref]$parsedInteger) -or
    $parsedInteger -lt 2 -or
    -not [int]::TryParse($virtualHardwareVersion, [ref]$parsedInteger)
  ) {
    throw "VMXのWindows 11 hardware/firmware設定がlab要件と一致しません。"
  }
  $diskDevices = @(
    foreach ($key in @($values.Keys)) {
      if (
        $key -match '^((?:ide|sata|scsi|nvme)\d+:\d+)\.present$' -and
        (Test-TrueVmwareValue $values[$key])
      ) {
        $device = $Matches[1]
        $fileName = Get-VmwareValue `
          -Values $values `
          -Key "$device.fileName" `
          -Optional
        if ($null -ne $fileName -and $fileName.EndsWith(".vmdk", [StringComparison]::OrdinalIgnoreCase)) {
          $device
        }
      }
    }
  )
  if ($diskDevices.Count -ne 1) {
    throw "VMXのpresent virtual diskは1件だけにしてください。"
  }
  $diskDevice = $diskDevices[0]
  $controller = $diskDevice.Split(":")[0]
  if (-not (Test-TrueVmwareValue (
    Get-VmwareValue -Values $values -Key "$controller.present"
  ))) {
    throw "VMX virtual disk controllerがpresentではありません。"
  }
  $activeDiskRelativePath = Get-VmwareValue `
    -Values $values `
    -Key "$diskDevice.fileName"
  Assert-RelativePayloadPath -Path $activeDiskRelativePath -Label "VMX active disk path"
  $activeDiskPath = [IO.Path]::GetFullPath((Join-Path (
    Split-Path -Parent $vmxPath
  ) $activeDiskRelativePath))
  if (
    -not (Test-PathWithin -Path $activeDiskPath -Root (Split-Path -Parent $vmxPath)) -or
    -not (Test-Path -LiteralPath $activeDiskPath -PathType Leaf)
  ) {
    throw "VMX active virtual diskが専用VM directory内に見つかりません。"
  }
  Assert-NoReparsePointInPath -Path $activeDiskPath -Label "VMX active virtual disk"
  $usbLines = @(
    $values.Keys |
      Where-Object { $_ -match '^(?:usb|ehci|usb_xhci)\.' } |
      Sort-Object |
      ForEach-Object { "$($_.ToLowerInvariant())=$($values[$_])" }
  )

  $fingerprintLines = @(
    "display_name=$displayName",
    "uuid=$uuid",
    "encryption_type=$encryptionType",
    "vtpm_present=true",
    "nic=$nic",
    "nic_connection_type=$connectionType",
    "nic_vnet=$($vnet.ToLowerInvariant())",
    "nic_start_connected=true",
    "mac_source=$($macSource.ToLowerInvariant())",
    "mac=$mac",
    "cd=$($cd.ToLowerInvariant())",
    "cd_iso=$($mountedIso.ToLowerInvariant())",
    "cd_start_connected=true",
    "copy_disabled=true",
    "paste_disabled=true",
    "dnd_disabled=true",
    "hgfs_disabled=$(if ($null -eq $hgfsDisabled) { 'absent' } else { 'true' })",
    "shared_folder_max_num=0",
    "guest_os=$guestOs",
    "firmware=$firmware",
    "secure_boot=true",
    "memory_mb=$memoryMb",
    "processor_count=$processorCount",
    "virtual_hardware_version=$virtualHardwareVersion",
    "disk_device=$($diskDevice.ToLowerInvariant())",
    "disk_controller=$($controller.ToLowerInvariant())",
    "active_disk=$($activeDiskRelativePath.ToLowerInvariant())",
    $usbLines
  )
  $fingerprint = Get-StringSha256 -Value (($fingerprintLines -join "`n") + "`n")
  if ($ValidateExpected) {
    if (
      $uuid -cne $Config.expected_vm_uuid -or
      $encryptionType -cne $Config.expected_vm_encryption_type -or
      $mac -cne $Config.expected_guest_mac -or
      $fingerprint -cne $Config.vm_definition_fingerprint_sha256 -or
      (-not $IgnoreRawVmxHash -and $vmxFileSha256 -cne $Config.vmx_file_sha256)
    ) {
      throw "VMX identity/fingerprintがconfigの固定値と一致しません。"
    }
  }

  return [pscustomobject][ordered]@{
    display_name = $displayName
    uuid = $uuid
    encryption_type = $encryptionType
    vtpm_present = $true
    mac = $mac
    network = $vnet
    mounted_iso_path = $mountedIso
    disk_device = $diskDevice
    active_disk_path = $activeDiskPath
    vmx_file_sha256 = $vmxFileSha256
    fingerprint_sha256 = $fingerprint
  }
}

function Get-VmdkFileSet {
  param(
    [Parameter(Mandatory = $true)]
    [string]$InitialPath,
    [Parameter(Mandatory = $true)]
    [string]$VmDirectory
  )

  $queue = [Collections.Generic.Queue[string]]::new()
  $seen = [Collections.Generic.HashSet[string]]::new(
    [StringComparer]::OrdinalIgnoreCase
  )
  $queue.Enqueue([IO.Path]::GetFullPath($InitialPath))
  $files = @()
  while ($queue.Count -gt 0) {
    $path = $queue.Dequeue()
    if (-not $seen.Add($path)) {
      continue
    }
    if (
      -not (Test-PathWithin -Path $path -Root $VmDirectory) -or
      -not (Test-Path -LiteralPath $path -PathType Leaf)
    ) {
      throw "snapshot VMDK chainが専用VM directory内に見つかりません: $path"
    }
    Assert-NoReparsePointInPath -Path $path -Label "snapshot VMDK chain"
    $file = Get-Item -LiteralPath $path
    $relativePath = $file.FullName.Substring(
      [IO.Path]::GetFullPath($VmDirectory).TrimEnd("\").Length + 1
    ).Replace("\", "/")
    $files += [pscustomobject][ordered]@{
      relative_path = $relativePath
      size = $file.Length
      sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }

    if ($file.Length -le 4MB) {
      $descriptor = [Text.Encoding]::ASCII.GetString(
        [IO.File]::ReadAllBytes($file.FullName)
      )
      $references = @(
        [regex]::Matches(
          $descriptor,
          '(?im)(?:parentFileNameHint\s*=\s*|^\s*(?:RW|RDONLY|NOACCESS)\s+\d+\s+\S+\s+)"([^"]+\.vmdk)"'
        ) |
          ForEach-Object { $_.Groups[1].Value }
      )
      foreach ($reference in $references) {
        Assert-RelativePayloadPath -Path $reference -Label "VMDK descriptor reference"
        $queue.Enqueue([IO.Path]::GetFullPath((Join-Path $VmDirectory $reference)))
      }
    }
  }
  return @($files | Sort-Object relative_path)
}

function Get-SnapshotDefinition {
  param(
    [Parameter(Mandatory = $true)]
    [psobject]$Config,
    [switch]$ValidateExpected
  )

  $vmsdPath = [IO.Path]::ChangeExtension($Config.vmx_path, ".vmsd")
  $values = Get-VmwareKeyValueFile -Path $vmsdPath
  $vmsdFileSha256 = (
    Get-FileHash -LiteralPath $vmsdPath -Algorithm SHA256
  ).Hash.ToLowerInvariant()
  $matches = @(
    foreach ($key in @($values.Keys)) {
      if (
        $key -match '^snapshot(\d+)\.displayName$' -and
        $values[$key] -ceq $Config.snapshot_a
      ) {
        $Matches[1]
      }
    }
  )
  if ($matches.Count -ne 1) {
    throw "VMSDのsnapshot名 '$($Config.snapshot_a)' はexact matchで1件必要です。"
  }
  $index = $matches[0]
  $prefix = "snapshot$index."
  $uid = Get-VmwareValue -Values $values -Key "${prefix}uid"
  if ($uid -cnotmatch '^\d+$') {
    throw "VMSD snapshot UIDを解釈できません: $uid"
  }
  $parent = Get-VmwareValue -Values $values -Key "${prefix}parent" -Optional
  if (-not [string]::IsNullOrEmpty($parent)) {
    throw "Environment A snapshotはsnapshot treeのrootにしてください。"
  }
  $vmsnRelativePath = Get-VmwareValue -Values $values -Key "${prefix}filename"
  Assert-RelativePayloadPath -Path $vmsnRelativePath -Label "snapshot state path"
  $vmDirectory = Split-Path -Parent $Config.vmx_path
  $vmsnPath = [IO.Path]::GetFullPath((Join-Path $vmDirectory $vmsnRelativePath))
  if (-not (Test-PathWithin -Path $vmsnPath -Root $vmDirectory)) {
    throw "snapshot state fileがVM directory外を指しています。"
  }
  if (-not (Test-Path -LiteralPath $vmsnPath -PathType Leaf)) {
    throw "snapshot state fileが見つかりません: $vmsnPath"
  }
  Assert-NoReparsePointInPath -Path $vmsnPath -Label "snapshot state file"
  $vmsn = Get-Item -LiteralPath $vmsnPath
  $vmsnHash = (Get-FileHash -LiteralPath $vmsnPath -Algorithm SHA256).Hash.ToLowerInvariant()
  $numDisksValue = Get-VmwareValue -Values $values -Key "${prefix}numDisks"
  $numDisks = 0
  if (-not [int]::TryParse($numDisksValue, [ref]$numDisks) -or $numDisks -ne 1) {
    throw "Environment A snapshotのvirtual diskは1件だけにしてください。"
  }
  $snapshotDisks = @(
    for ($diskIndex = 0; $diskIndex -lt $numDisks; $diskIndex++) {
      $diskRelativePath = Get-VmwareValue `
        -Values $values `
        -Key "${prefix}disk$diskIndex.fileName"
      Assert-RelativePayloadPath -Path $diskRelativePath -Label "snapshot disk path"
      $diskPath = [IO.Path]::GetFullPath((Join-Path $vmDirectory $diskRelativePath))
      [pscustomobject][ordered]@{
        index = $diskIndex
        node = Get-VmwareValue -Values $values -Key "${prefix}disk$diskIndex.node"
        primary_path = $diskPath
        files = @(Get-VmdkFileSet -InitialPath $diskPath -VmDirectory $vmDirectory)
      }
    }
  )
  $snapshotLines = @(
    @($values.Keys) |
      Where-Object { $_.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) } |
      Sort-Object |
      ForEach-Object { "$($_.ToLowerInvariant())=$($values[$_])" }
  )
  $fingerprintLines = @(
    $snapshotLines
    "vmsd_sha256=$vmsdFileSha256"
    "vmsn_sha256=$vmsnHash"
    "vmsn_size=$($vmsn.Length)"
    foreach ($disk in $snapshotDisks) {
      "disk$($disk.index)_node=$($disk.node.ToLowerInvariant())"
      foreach ($file in $disk.files) {
        "disk$($disk.index)_file=$($file.relative_path.ToLowerInvariant())|$($file.size)|$($file.sha256)"
      }
    }
  )
  $fingerprint = Get-StringSha256 -Value (($fingerprintLines -join "`n") + "`n")
  if ($ValidateExpected) {
    if (
      $uid -cne $Config.snapshot_uid -or
      $fingerprint -cne $Config.snapshot_fingerprint_sha256
    ) {
      throw "Environment A snapshot UID/fingerprintがconfigの固定値と一致しません。"
    }
  }

  return [pscustomobject][ordered]@{
    name = $Config.snapshot_a
    uid = $uid
    root = $true
    state_file = $vmsnPath
    state_file_size = $vmsn.Length
    state_file_sha256 = $vmsnHash
    vmsd_file_sha256 = $vmsdFileSha256
    disks = $snapshotDisks
    fingerprint_sha256 = $fingerprint
  }
}

function Get-LabDefinition {
  param(
    [Parameter(Mandatory = $true)]
    [psobject]$Config,
    [switch]$ValidateExpected,
    [switch]$IgnoreRawVmxHash
  )

  $vm = Get-VmDefinition `
    -Config $Config `
    -ValidateExpected:$ValidateExpected `
    -IgnoreRawVmxHash:$IgnoreRawVmxHash
  $snapshot = Get-SnapshotDefinition -Config $Config -ValidateExpected:$ValidateExpected
  if (
    @($snapshot.disks).Count -ne 1 -or
    $snapshot.disks[0].node -cne $vm.disk_device
  ) {
    throw "VMX active diskとEnvironment A snapshot disk nodeが一致しません。"
  }
  return [pscustomobject][ordered]@{
    vm = $vm
    snapshot = $snapshot
  }
}

function Get-LabCapture {
  param(
    [Parameter(Mandatory = $true)]
    [psobject]$Config
  )

  $definition = Get-LabDefinition -Config $Config
  return [ordered]@{
    schema_version = 2
    action = "Capture"
    mutates_vm = $false
    replacement_values = [ordered]@{
      expected_vm_uuid = $definition.vm.uuid
      expected_vm_encryption_type = $definition.vm.encryption_type
      expected_guest_mac = $definition.vm.mac
      vmx_file_sha256 = $definition.vm.vmx_file_sha256
      vm_definition_fingerprint_sha256 = $definition.vm.fingerprint_sha256
      snapshot_uid = $definition.snapshot.uid
      snapshot_fingerprint_sha256 = $definition.snapshot.fingerprint_sha256
    }
    definition = $definition
  }
}

function Get-FileReadiness {
  param(
    [Parameter(Mandatory = $true)]
    [psobject]$Config
  )

  $isoExists = Test-Path -LiteralPath $Config.payload_iso_path -PathType Leaf
  $isoActualHash = if ($isoExists) {
    Assert-NoReparsePointInPath -Path $Config.payload_iso_path -Label "payload ISO"
    (Get-FileHash -LiteralPath $Config.payload_iso_path -Algorithm SHA256).Hash.ToLowerInvariant()
  } else {
    $null
  }
  if ($isoExists -and $isoActualHash -cne $Config.payload_iso_sha256) {
    throw "payload ISOのSHA256が固定値と一致しません。"
  }
  $vmxExists = Test-Path -LiteralPath $Config.vmx_path -PathType Leaf
  $definition = if ($vmxExists) {
    Get-LabDefinition -Config $Config -ValidateExpected
  } else {
    $null
  }
  $bootstrapExists = Test-Path -LiteralPath $bootstrapScript -PathType Leaf
  return [ordered]@{
    vmrun_exists = Test-Path -LiteralPath $Config.vmrun_path -PathType Leaf
    vmware_dhcp_config_exists = Test-Path -LiteralPath $Config.vmware_dhcp_config_path -PathType Leaf
    vmware_nat_config_exists = Test-Path -LiteralPath $Config.vmware_nat_config_path -PathType Leaf
    vmx_exists = $vmxExists
    definition = $definition
    vm_credential_exists = Test-Path -LiteralPath $Config.vm_encryption_credential_path -PathType Leaf
    guest_credential_exists = Test-Path -LiteralPath $Config.guest_credential_path -PathType Leaf
    payload_iso_exists = $isoExists
    payload_iso_sha256 = $isoActualHash
    guest_script_exists = Test-Path -LiteralPath $guestScript -PathType Leaf
    bootstrap_script_exists = $bootstrapExists
    bootstrap_script_sha256 = if ($bootstrapExists) {
      (Get-FileHash -LiteralPath $bootstrapScript -Algorithm SHA256).Hash.ToLowerInvariant()
    } else {
      $null
    }
  }
}

function Get-LabPlan {
  param(
    [Parameter(Mandatory = $true)]
    [psobject]$Config
  )

  return [ordered]@{
    schema_version = 2
    action = "Plan"
    mutates_vm = $false
    config_sha256 = $Config.config_sha256
    payload_commit = $Config.payload_commit
    readiness = Get-FileReadiness -Config $Config
    steps = @(
      "require VM powered off",
      "require exact VMX identity, isolated NIC/CD settings, and A0 fingerprint",
      "revert A0-runtime-absent",
      "start VM without GUI",
      "verify WinRM over host-only vmnet1",
      "verify Environment A has no x64 Redistributable, VMware Tools, or default route",
      "install fixed Microsoft-signed Redistributable from the hashed ISO",
      "verify Environment B Runtime version",
      "run the fixed packaged self-check with isolated data",
      "write JSON evidence",
      "request soft VM shutdown"
    )
    forbidden = @(
      "VMware Tools guest control",
      "Runtime download",
      "hard power off fallback",
      "wildcard TrustedHosts",
      "unconfirmed VM password process-argument exposure",
      "real LoL recording",
      "tag or Release mutation"
    )
  }
}

function Import-LabCredential {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Path,
    [Parameter(Mandatory = $true)]
    [string]$Label
  )

  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    throw "$Label credentialが見つかりません: $Path"
  }
  Assert-NoReparsePointInPath -Path $Path -Label "$Label credential"
  $credential = Import-Clixml -LiteralPath $Path
  if ($credential -isnot [Management.Automation.PSCredential]) {
    throw "$Label credentialはExport-Clixmlで保存したPSCredentialではありません。"
  }
  if ([string]::IsNullOrEmpty($credential.GetNetworkCredential().Password)) {
    throw "$Label credentialのpasswordは空にできません。"
  }
  return $credential
}

function Invoke-Vmrun {
  param(
    [Parameter(Mandatory = $true)]
    [psobject]$Config,
    [Parameter(Mandatory = $true)]
    [string[]]$Arguments,
    [Management.Automation.PSCredential]$VmCredential = $null
  )

  $commandArguments = @("-T", "ws")
  $password = $null
  if ($null -ne $VmCredential) {
    if (-not $ConfirmVmPasswordProcessExposure) {
      throw "VM encryption passwordをvmrun process argumentへ渡す明示確認がありません。"
    }
    $password = $VmCredential.GetNetworkCredential().Password
    $commandArguments += @("-vp", $password)
  }
  $commandArguments += $Arguments
  $output = @(& $Config.vmrun_path @commandArguments 2>&1)
  $exitCode = $LASTEXITCODE
  $text = ($output | Out-String).Trim()
  if (-not [string]::IsNullOrEmpty($password)) {
    $text = $text.Replace($password, "<redacted>")
  }
  $password = $null
  $commandArguments = @()
  if ($exitCode -ne 0) {
    throw "vmrun command failed: exit=$exitCode output=$text"
  }
  return $text
}

function Get-RunningVms {
  param(
    [Parameter(Mandatory = $true)]
    [psobject]$Config
  )

  $output = Invoke-Vmrun -Config $Config -Arguments @("list")
  $lines = @($output -split "`r?`n")
  if ($lines.Count -eq 0 -or $lines[0] -notmatch '^Total running VMs:\s*(\d+)\s*$') {
    throw "vmrun listの出力を解釈できません。"
  }
  $expectedCount = [int]$Matches[1]
  $paths = @($lines | Select-Object -Skip 1 | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
  if ($paths.Count -ne $expectedCount) {
    throw "vmrun listの件数がheaderと一致しません。"
  }
  return $paths
}

function Test-TargetVmRunning {
  param(
    [Parameter(Mandatory = $true)]
    [psobject]$Config
  )

  $target = [IO.Path]::GetFullPath($Config.vmx_path)
  foreach ($running in @(Get-RunningVms -Config $Config)) {
    if ([IO.Path]::GetFullPath($running).Equals($target, [StringComparison]::OrdinalIgnoreCase)) {
      return $true
    }
  }
  return $false
}

function Get-Snapshots {
  param(
    [Parameter(Mandatory = $true)]
    [psobject]$Config,
    [Parameter(Mandatory = $true)]
    [Management.Automation.PSCredential]$VmCredential
  )

  $output = Invoke-Vmrun `
    -Config $Config `
    -VmCredential $VmCredential `
    -Arguments @("listSnapshots", $Config.vmx_path)
  $lines = @($output -split "`r?`n")
  if ($lines.Count -eq 0 -or $lines[0] -notmatch '^Total snapshots:\s*(\d+)\s*$') {
    throw "vmrun listSnapshotsの出力を解釈できません。"
  }
  $expectedCount = [int]$Matches[1]
  $snapshots = @(
    $lines |
      Select-Object -Skip 1 |
      ForEach-Object { $_.Trim() } |
      Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
  )
  if ($snapshots.Count -ne $expectedCount) {
    throw "vmrun listSnapshotsの件数がheaderと一致しません。"
  }
  return $snapshots
}

function Get-TrustedHosts {
  try {
    $item = Get-Item -LiteralPath WSMan:\localhost\Client\TrustedHosts -ErrorAction Stop
    return @(
      ([string]$item.Value).Split(",", [StringSplitOptions]::RemoveEmptyEntries) |
        ForEach-Object { $_.Trim() }
    )
  } catch {
    return @()
  }
}

function Test-HostOnlyNetwork {
  param(
    [Parameter(Mandatory = $true)]
    [psobject]$Config
  )

  if (
    -not (Test-Path -LiteralPath $Config.vmware_dhcp_config_path -PathType Leaf) -or
    -not (Test-Path -LiteralPath $Config.vmware_nat_config_path -PathType Leaf)
  ) {
    return $false
  }
  $networkNumber = $Config.vmware_network.Substring(5)
  $hostIp = [Net.IPAddress]::Parse($Config.host_address)
  $bytes = $hostIp.GetAddressBytes()
  $subnet = "$($bytes[0]).$($bytes[1]).$($bytes[2]).0"
  $dhcp = Get-Content `
    -LiteralPath $Config.vmware_dhcp_config_path `
    -Raw `
    -Encoding utf8
  $nat = Get-Content `
    -LiteralPath $Config.vmware_nat_config_path `
    -Raw `
    -Encoding utf8
  $subnetPattern = (
    '(?im)^\s*subnet\s+' + [regex]::Escape($subnet) +
    '\s+netmask\s+255\.255\.255\.0\s*\{'
  )
  $hostPattern = (
    '(?ims)^\s*host\s+' + [regex]::Escape($Config.vmware_network) +
    '\s*\{.*?^\s*fixed-address\s+' +
    [regex]::Escape($Config.host_address) + '\s*;'
  )
  $natPattern = (
    '(?im)^\s*device\s*=\s*' +
    [regex]::Escape($Config.vmware_network) + '\s*$'
  )
  if (
    $networkNumber -cnotmatch '^\d+$' -or
    $dhcp -cnotmatch $subnetPattern -or
    $dhcp -cnotmatch $hostPattern -or
    $nat -cmatch $natPattern
  ) {
    return $false
  }

  $adapters = @(
    [Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces() |
      Where-Object {
        $_.Name -ceq "VMware Network Adapter VMnet$networkNumber" -or
        $_.Description -ceq "VMware Virtual Ethernet Adapter for VMnet$networkNumber"
      }
  )
  if ($adapters.Count -ne 1) {
    return $false
  }
  $properties = $adapters[0].GetIPProperties()
  $addresses = @(
    $properties.UnicastAddresses |
      Where-Object {
        $_.Address.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork
      } |
      ForEach-Object {
        [pscustomobject]@{
          address = $_.Address.ToString()
          prefix_length = $_.PrefixLength
        }
      }
  )
  $gateways = @(
    $properties.GatewayAddresses |
      Where-Object {
        $_.Address.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork -and
        $_.Address.ToString() -ne "0.0.0.0"
      }
  )
  return (
    $adapters[0].OperationalStatus -eq [Net.NetworkInformation.OperationalStatus]::Up -and
    @(
      $addresses |
        Where-Object {
          $_.address -ceq $Config.host_address -and $_.prefix_length -eq 24
        }
    ).Count -eq 1 -and
    $gateways.Count -eq 0
  )
}

function Open-LabSession {
  param(
    [Parameter(Mandatory = $true)]
    [psobject]$Config,
    [Parameter(Mandatory = $true)]
    [Management.Automation.PSCredential]$GuestCredential,
    [ValidateRange(1, 120)]
    [int]$TimeoutSeconds = 60
  )

  $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
  $lastError = "WinRM endpoint did not respond"
  do {
    try {
      $sessionOption = New-PSSessionOption -OpenTimeout 5000 -OperationTimeout 300000
      return New-PSSession `
        -ComputerName $Config.guest_address `
        -Credential $GuestCredential `
        -Authentication Negotiate `
        -SessionOption $sessionOption `
        -ErrorAction Stop
    } catch {
      $lastError = $_.Exception.Message
      Start-Sleep -Seconds 2
    }
  } while ([DateTime]::UtcNow -lt $deadline)
  throw "WinRM sessionを${TimeoutSeconds}秒以内に確立できませんでした: $lastError"
}

function Invoke-GuestAction {
  param(
    [Parameter(Mandatory = $true)]
    [psobject]$Config,
    [Parameter(Mandatory = $true)]
    [Management.Automation.Runspaces.PSSession]$Session,
    [Parameter(Mandatory = $true)]
    [ValidateSet("Inspect", "InstallRuntime", "SelfCheck")]
    [string]$GuestAction
  )

  $output = @(
    Invoke-Command `
      -Session $Session `
      -FilePath $guestScript `
      -ArgumentList @(
        $GuestAction,
        $Config.payload_volume_label,
        $Config.runtime_installer_relative_path,
        $Config.runtime_installer_sha256,
        $Config.minimum_runtime_version,
        $Config.app_relative_path,
        $Config.app_sha256,
        $Config.environment_b_script_relative_path,
        $Config.environment_b_script_sha256,
        (Get-FileHash -LiteralPath $bootstrapScript -Algorithm SHA256).Hash.ToLowerInvariant(),
        180
      ) `
      -ErrorAction Stop
  )
  $json = ($output | ForEach-Object { [string]$_ }) -join "`n"
  try {
    $result = $json | ConvertFrom-Json -ErrorAction Stop
  } catch {
    throw "guest action '$GuestAction' が有効なJSONを返しませんでした: $json"
  }
  $capturedAt = [DateTimeOffset]::MinValue
  if (
    $result.schema_version -ne 1 -or
    $result.action -cne $GuestAction -or
    [string]::IsNullOrWhiteSpace([string]$result.computer_name) -or
    -not [DateTimeOffset]::TryParse(
      [string]$result.captured_at_utc,
      [ref]$capturedAt
    )
  ) {
    throw "guest action '$GuestAction' のschema/action/computer/timestampが不正です。"
  }
  return $result
}

function Write-EvidenceJson {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Path,
    [Parameter(Mandatory = $true)]
    [object]$Value
  )

  $json = $Value | ConvertTo-Json -Depth 20
  [IO.File]::WriteAllText($Path, $json + "`n", $utf8NoBom)
}

function Assert-EnvironmentA {
  param(
    [Parameter(Mandatory = $true)]
    [psobject]$Config,
    [Parameter(Mandatory = $true)]
    [psobject]$Inspection
  )

  if ($Inspection.runtime.installed) {
    throw "Environment Aにx64 Visual C++ Redistributableが導入済みです。"
  }
  if (
    -not $Inspection.payload.required -or
    -not $Inspection.payload.verified -or
    $Inspection.payload.volume_label -cne $Config.payload_volume_label
  ) {
    throw "Environment Aで固定payload ISOのguest-side hashを確認できません。"
  }
  if (@($Inspection.system_runtime_dlls | Where-Object { $_.present }).Count -ne 0) {
    throw "Environment AのSystem32に対象VC++ Runtime DLLがあります。"
  }
  if (@($Inspection.system_runtime_inventory).Count -ne 0) {
    throw "Environment AのSystem32にVC++ Runtime名のDLLがあります。"
  }
  if (
    $null -eq $Inspection.bootstrap -or
    $Inspection.bootstrap.schema_version -ne 2 -or
    $Inspection.bootstrap.guest_address -cne $Config.guest_address -or
    $Inspection.bootstrap.host_address -cne $Config.host_address -or
    $Inspection.bootstrap.computer_name -cne $Inspection.computer_name -or
    $Inspection.bootstrap.bootstrap_sha256 -cne (
      Get-FileHash -LiteralPath $bootstrapScript -Algorithm SHA256
    ).Hash.ToLowerInvariant() -or
    $Inspection.bootstrap.vmware_tools_present
  ) {
    throw "Environment Aのbootstrap markerが固定lab構成と一致しません。"
  }
  if ($Inspection.vmware_tools.present) {
    throw "Environment AにVMware Toolsが存在します。"
  }
  if (@($Inspection.default_routes).Count -ne 0) {
    throw "Environment Aにdefault IPv4 routeがあります。host-only構成ではありません。"
  }
  if ($Inspection.default_route_source -cne "route-table") {
    throw "Environment Aのdefault routeをroute tableから検査できません。"
  }
  if (
    -not $Inspection.winrm_firewall.available -or
    @($Inspection.winrm_firewall.rules).Count -ne 1 -or
    $Inspection.winrm_firewall.rules[0].name -cne "LoLReplayTool-VM-Lab-WinRM" -or
    -not $Inspection.winrm_firewall.rules[0].exact_scope
  ) {
    throw "Environment AのWinRM firewall境界が固定lab構成と一致しません。"
  }
  if ($Config.guest_address -notin @($Inspection.ipv4_addresses)) {
    throw "Environment AのIPv4 addressが固定値と一致しません。"
  }
}

function Assert-EnvironmentB {
  param(
    [Parameter(Mandatory = $true)]
    [psobject]$Config,
    [Parameter(Mandatory = $true)]
    [psobject]$Inspection
  )

  if (-not $Inspection.runtime.installed) {
    throw "Environment Bでx64 Visual C++ Redistributableを確認できません。"
  }
  if (@($Inspection.system_runtime_dlls | Where-Object { -not $_.present }).Count -ne 0) {
    throw "Environment BのSystem32に必要なVC++ Runtime DLLが揃っていません。"
  }
  $untrustedRuntimeDlls = @(
    $Inspection.system_runtime_inventory |
      Where-Object {
        $_.signature_status -cne "Valid" -or
        $_.signer_subject -notmatch '(^|,\s*)O=Microsoft Corporation(,|$)'
      }
  )
  if ($untrustedRuntimeDlls.Count -ne 0) {
    throw "Environment BのSystem32 Runtime DLLにMicrosoft署名を確認できません。"
  }
  $actual = ([string]$Inspection.runtime.version).TrimStart("v", "V")
  if ([version]$actual -lt [version]$Config.minimum_runtime_version) {
    throw "Environment BのRuntimeが必要version未満です。"
  }
  if ($Inspection.vmware_tools.present) {
    throw "Environment BにVMware Toolsが存在します。"
  }
  if (
    @($Inspection.default_routes).Count -ne 0 -or
    $Inspection.default_route_source -cne "route-table"
  ) {
    throw "Environment Bでhost-only default route境界が変化しました。"
  }
  if (
    -not $Inspection.winrm_firewall.available -or
    @($Inspection.winrm_firewall.rules).Count -ne 1 -or
    -not $Inspection.winrm_firewall.rules[0].exact_scope
  ) {
    throw "Environment BでWinRM firewall境界が変化しました。"
  }
}

function Invoke-Doctor {
  param(
    [Parameter(Mandatory = $true)]
    [psobject]$Config
  )

  try {
    $readiness = Get-FileReadiness -Config $Config
  } catch {
    return [ordered]@{
      schema_version = 2
      action = "Doctor"
      ready_for_run = $false
      files = $null
      reason = "definition validation failed"
      validation_error = $_.Exception.Message
    }
  }
  $requiredFilesReady = (
    $readiness.vmrun_exists -and
    $readiness.vmware_dhcp_config_exists -and
    $readiness.vmware_nat_config_exists -and
    $readiness.vmx_exists -and
    $readiness.vm_credential_exists -and
    $readiness.guest_credential_exists -and
    $readiness.payload_iso_exists -and
    $readiness.guest_script_exists -and
    $readiness.bootstrap_script_exists
  )
  if (-not $requiredFilesReady) {
    return [ordered]@{
      schema_version = 2
      action = "Doctor"
      ready_for_run = $false
      files = $readiness
      reason = "required file is missing"
    }
  }

  try {
    $running = Test-TargetVmRunning -Config $Config
    $trustedHosts = @(Get-TrustedHosts)
    $trusted = (
      $trustedHosts.Count -eq 1 -and
      $trustedHosts[0] -ceq $Config.guest_address -and
      "*" -notin $trustedHosts
    )
    $hostOnlyNetwork = Test-HostOnlyNetwork -Config $Config
    $snapshotIdentityExact = (
      $null -ne $readiness.definition -and
      $readiness.definition.vm.vmx_file_sha256 -cne $null -and
      $readiness.definition.vm.vmx_file_sha256 -ceq $Config.vmx_file_sha256 -and
      $readiness.definition.snapshot.uid -ceq $Config.snapshot_uid -and
      $readiness.definition.snapshot.fingerprint_sha256 -ceq $Config.snapshot_fingerprint_sha256
    )

    return [ordered]@{
      schema_version = 2
      action = "Doctor"
      ready_for_run = (
        -not $running -and
        $trusted -and
        $hostOnlyNetwork -and
        $snapshotIdentityExact
      )
      files = $readiness
      vm_running = $running
      snapshot_identity_exact = $snapshotIdentityExact
      definition = $readiness.definition
      host_only_network = $hostOnlyNetwork
      trusted_hosts = $trustedHosts
      trusted_hosts_exact = $trusted
    }
  } catch {
    return [ordered]@{
      schema_version = 2
      action = "Doctor"
      ready_for_run = $false
      files = $readiness
      reason = "read-only preflight failed"
      validation_error = $_.Exception.Message
    }
  }
}

function Invoke-LabRun {
  param(
    [Parameter(Mandatory = $true)]
    [psobject]$Config
  )

  if (
    -not $ConfirmSnapshotRestore -or
    -not $ConfirmRuntimeInstall -or
    -not $ConfirmVmPasswordProcessExposure
  ) {
    throw "Runには-ConfirmSnapshotRestore、-ConfirmRuntimeInstall、-ConfirmVmPasswordProcessExposureが必要です。"
  }
  $doctor = Invoke-Doctor -Config $Config
  if (-not $doctor.ready_for_run) {
    throw "DoctorがRun可能状態を確認できませんでした: $($doctor | ConvertTo-Json -Depth 10 -Compress)"
  }

  $vmCredential = Import-LabCredential `
    -Path $Config.vm_encryption_credential_path `
    -Label "VM encryption"
  $guestCredential = Import-LabCredential `
    -Path $Config.guest_credential_path `
    -Label "guest"
  $snapshots = @(Get-Snapshots -Config $Config -VmCredential $vmCredential)
  $snapshotMatches = @($snapshots | Where-Object { $_ -ceq $Config.snapshot_a })
  if ($snapshotMatches.Count -ne 1) {
    throw "vmrunがexact matchのEnvironment A snapshotを1件確認できません。"
  }
  $runDirectory = Join-Path $Config.artifact_root (
    [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ") + "-" +
    $Config.config_sha256.Substring(0, 12)
  )
  if (Test-Path -LiteralPath $runDirectory) {
    throw "VM lab evidence directoryが既に存在します: $runDirectory"
  }
  New-Item -ItemType Directory -Path $runDirectory -ErrorAction Stop | Out-Null

  $manifestPath = Join-Path $runDirectory "manifest.json"
  $manifest = [ordered]@{
    schema_version = 2
    status = "running"
    started_at_utc = [DateTime]::UtcNow.ToString("o")
    finished_at_utc = $null
    config_sha256 = $Config.config_sha256
    payload_commit = $Config.payload_commit
    payload_iso_sha256 = $Config.payload_iso_sha256
    snapshot = $Config.snapshot_a
    vm_definition_fingerprint_sha256 = $Config.vm_definition_fingerprint_sha256
    snapshot_uid = $Config.snapshot_uid
    snapshot_fingerprint_sha256 = $Config.snapshot_fingerprint_sha256
    guest_script_sha256 = (
      Get-FileHash -LiteralPath $guestScript -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    bootstrap_script_sha256 = (
      Get-FileHash -LiteralPath $bootstrapScript -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    vmx_path = $Config.vmx_path
    evidence_directory = $runDirectory
    risk_acceptance = [ordered]@{
      vm_password_process_argument_confirmed = [bool]$ConfirmVmPasswordProcessExposure
      password_scope_required = "dedicated disposable VM only"
      observed_by = "same-host principals able to read process command lines"
    }
    lifecycle = @()
    evidence_files = @()
    final_vm_state = "unknown"
    error = $null
  }
  Write-EvidenceJson -Path $manifestPath -Value $manifest

  $session = $null
  $vmStarted = $false
  $runError = $null
  $stopError = $null
  $lifecycle = @()
  try {
    $definitionBefore = Get-LabDefinition -Config $Config -ValidateExpected
    Write-EvidenceJson `
      -Path (Join-Path $runDirectory "definition-before-revert.json") `
      -Value $definitionBefore
    $lifecycle += [ordered]@{
      at_utc = [DateTime]::UtcNow.ToString("o")
      operation = "validated-definition-before-revert"
    }
    Invoke-Vmrun `
      -Config $Config `
      -VmCredential $vmCredential `
      -Arguments @("revertToSnapshot", $Config.vmx_path, $Config.snapshot_a) |
      Out-Null
    $lifecycle += [ordered]@{
      at_utc = [DateTime]::UtcNow.ToString("o")
      operation = "revertToSnapshot"
      snapshot = $Config.snapshot_a
    }
    if (Test-TargetVmRunning -Config $Config) {
      throw "A0 snapshotはpowered-off状態で作成してください。revert後にVMが起動しています。"
    }
    $definitionAfterRevert = Get-LabDefinition `
      -Config $Config `
      -ValidateExpected `
      -IgnoreRawVmxHash
    Write-EvidenceJson `
      -Path (Join-Path $runDirectory "definition-after-revert.json") `
      -Value $definitionAfterRevert
    if (
      $definitionBefore.vm.fingerprint_sha256 -cne $definitionAfterRevert.vm.fingerprint_sha256 -or
      $definitionBefore.snapshot.fingerprint_sha256 -cne $definitionAfterRevert.snapshot.fingerprint_sha256
    ) {
      throw "snapshot revert前後で固定VM definitionが変化しました。"
    }
    Invoke-Vmrun `
      -Config $Config `
      -VmCredential $vmCredential `
      -Arguments @("start", $Config.vmx_path, "nogui") |
      Out-Null
    $vmStarted = $true
    $lifecycle += [ordered]@{
      at_utc = [DateTime]::UtcNow.ToString("o")
      operation = "start-nogui"
    }

    $session = Open-LabSession `
      -Config $Config `
      -GuestCredential $guestCredential `
      -TimeoutSeconds 60
    $environmentA = Invoke-GuestAction `
      -Config $Config `
      -Session $session `
      -GuestAction "Inspect"
    Assert-EnvironmentA -Config $Config -Inspection $environmentA
    Write-EvidenceJson `
      -Path (Join-Path $runDirectory "environment-a.json") `
      -Value $environmentA

    $runtimeInstall = Invoke-GuestAction `
      -Config $Config `
      -Session $session `
      -GuestAction "InstallRuntime"
    Write-EvidenceJson `
      -Path (Join-Path $runDirectory "runtime-install.json") `
      -Value $runtimeInstall
    if ($runtimeInstall.reboot_required) {
      throw "Runtime installerがreboot requiredを返しました。自動再起動せず停止します。"
    }

    $environmentB = Invoke-GuestAction `
      -Config $Config `
      -Session $session `
      -GuestAction "Inspect"
    Assert-EnvironmentB -Config $Config -Inspection $environmentB
    Write-EvidenceJson `
      -Path (Join-Path $runDirectory "environment-b.json") `
      -Value $environmentB

    $selfCheck = Invoke-GuestAction `
      -Config $Config `
      -Session $session `
      -GuestAction "SelfCheck"
    Write-EvidenceJson `
      -Path (Join-Path $runDirectory "packaged-self-check.json") `
      -Value $selfCheck
  } catch {
    $runError = $_.Exception.Message
  } finally {
    if ($null -ne $session) {
      Remove-PSSession -Session $session -ErrorAction SilentlyContinue
    }
    $shouldRequestStop = $vmStarted
    if (-not $shouldRequestStop) {
      try {
        $shouldRequestStop = Test-TargetVmRunning -Config $Config
      } catch {
        $stopError = "VM running state before cleanup is unknown: $($_.Exception.Message)"
      }
    }
    if ($shouldRequestStop -and $null -eq $stopError) {
      try {
        Invoke-Vmrun `
          -Config $Config `
          -VmCredential $vmCredential `
          -Arguments @("stop", $Config.vmx_path, "soft") |
          Out-Null
        $lifecycle += [ordered]@{
          at_utc = [DateTime]::UtcNow.ToString("o")
          operation = "stop-soft-requested"
        }
        $deadline = [DateTime]::UtcNow.AddSeconds(30)
        do {
          if (-not (Test-TargetVmRunning -Config $Config)) {
            $manifest.final_vm_state = "stopped"
            break
          }
          Start-Sleep -Seconds 2
        } while ([DateTime]::UtcNow -lt $deadline)
        if ($manifest.final_vm_state -cne "stopped") {
          throw "soft stop後30秒以内にpowered-offを確認できません。hard stopは行いません。"
        }
      } catch {
        $stopError = $_.Exception.Message
      }
    } elseif (-not $shouldRequestStop -and $null -eq $stopError) {
      $manifest.final_vm_state = "stopped"
    }
    if ($manifest.final_vm_state -ceq "stopped" -and $null -eq $stopError) {
      try {
        $definitionAfterStop = Get-LabDefinition `
          -Config $Config `
          -ValidateExpected `
          -IgnoreRawVmxHash
        Write-EvidenceJson `
          -Path (Join-Path $runDirectory "definition-after-stop.json") `
          -Value $definitionAfterStop
      } catch {
        $stopError = "post-stop definition validation failed: $($_.Exception.Message)"
      }
    }
  }

  $manifest.finished_at_utc = [DateTime]::UtcNow.ToString("o")
  $manifest.lifecycle = $lifecycle
  try {
    $manifest.evidence_files = @(
      Get-ChildItem -LiteralPath $runDirectory -File |
        Where-Object { $_.Name -cne "manifest.json" } |
        Sort-Object Name |
        ForEach-Object {
          [ordered]@{
            name = $_.Name
            size = $_.Length
            sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
          }
        }
    )
  } catch {
    if ($null -eq $runError) {
      $runError = "evidence hash finalization failed: $($_.Exception.Message)"
    }
  }
  if ($null -eq $runError -and $null -eq $stopError) {
    $manifest.status = "passed"
  } else {
    $manifest.status = "failed"
    $manifest.error = [ordered]@{
      run = $runError
      soft_stop = $stopError
    }
  }
  Write-EvidenceJson -Path $manifestPath -Value $manifest
  if ($null -ne $runError -or $null -ne $stopError) {
    throw "VM lab run failed: run=$runError soft_stop=$stopError evidence=$runDirectory"
  }
  return $manifest
}

$labConfig = Read-LabConfig -Path $ConfigPath
$result = switch ($Action) {
  "Capture" { Get-LabCapture -Config $labConfig }
  "Plan" { Get-LabPlan -Config $labConfig }
  "Doctor" { Invoke-Doctor -Config $labConfig }
  "Run" { Invoke-LabRun -Config $labConfig }
}

$result | ConvertTo-Json -Depth 20 -Compress
