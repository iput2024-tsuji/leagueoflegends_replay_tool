param(
  [Parameter(Mandatory = $true)]
  [string]$InstallerPath,
  [Parameter(Mandatory = $true)]
  [string]$DistributionRoot,
  [Parameter(Mandatory = $true)]
  [string]$TempRoot,
  [string]$PythonExe = "",
  [ValidateRange(1, 1800)]
  [int]$TimeoutSeconds = 600
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not $IsWindows) {
  throw "完成installerの失敗隔離検査はWindowsでだけ実行できます。"
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$auditScript = Join-Path $scriptDir "audit_installer_contents.ps1"

function Test-ReparsePoint {
  param([Parameter(Mandatory = $true)]$Item)
  return [bool]($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)
}

function Resolve-RealDirectory {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Path,
    [Parameter(Mandatory = $true)]
    [string]$Label
  )

  $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
  if (-not $item.PSIsContainer -or (Test-ReparsePoint -Item $item)) {
    throw "$Label はreparse pointではない実directoryでなければなりません: $Path"
  }
  $resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
  $pathRoot = [System.IO.Path]::GetPathRoot($resolved)
  if ($resolved -ieq $pathRoot) {
    throw "$Label にdrive rootは指定できません: $resolved"
  }
  return [System.IO.Path]::GetFullPath($resolved)
}

function Assert-DirectTemporaryChild {
  param(
    [Parameter(Mandatory = $true)]
    [string]$AllowedRoot,
    [Parameter(Mandatory = $true)]
    [string]$Candidate,
    [Parameter(Mandatory = $true)]
    [string]$Prefix
  )

  $fullCandidate = [System.IO.Path]::GetFullPath($Candidate)
  if (
    [System.IO.Path]::GetDirectoryName($fullCandidate) -ine $AllowedRoot -or
    -not ([System.IO.Path]::GetFileName($fullCandidate)).StartsWith(
      $Prefix,
      [System.StringComparison]::Ordinal
    )
  ) {
    throw "失敗隔離検査の一時path境界を確認できません: $fullCandidate"
  }
}

function Test-PathsOverlap {
  param(
    [Parameter(Mandatory = $true)]
    [string]$First,
    [Parameter(Mandatory = $true)]
    [string]$Second
  )

  $firstFull = [System.IO.Path]::GetFullPath($First)
  $secondFull = [System.IO.Path]::GetFullPath($Second)
  foreach ($pair in @(
    @($firstFull, $secondFull),
    @($secondFull, $firstFull)
  )) {
    $relative = [System.IO.Path]::GetRelativePath($pair[0], $pair[1])
    if (
      $relative -eq "." -or
      (
        -not [System.IO.Path]::IsPathRooted($relative) -and
        $relative -ne ".." -and
        -not $relative.StartsWith(
          "..$([System.IO.Path]::DirectorySeparatorChar)",
          [System.StringComparison]::Ordinal
        ) -and
        -not $relative.StartsWith(
          "..$([System.IO.Path]::AltDirectorySeparatorChar)",
          [System.StringComparison]::Ordinal
        )
      )
    ) {
      return $true
    }
  }
  return $false
}

function Get-FileSnapshot {
  param([Parameter(Mandatory = $true)][string]$Path)

  if (-not (Test-Path -LiteralPath $Path)) {
    return "missing"
  }
  $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
  if ($item.PSIsContainer -or (Test-ReparsePoint -Item $item)) {
    throw "隔離状態のsnapshot対象が安全なfileではありません: $Path"
  }
  $hash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
  return "file:$($item.Length):$hash"
}

function Convert-RegistryValue {
  param($Value)

  if ($null -eq $Value) {
    return "null"
  }
  if ($Value -is [byte[]]) {
    return "bytes:" + [System.Convert]::ToHexString($Value)
  }
  if ($Value -is [string[]]) {
    return "strings:" + ($Value | ConvertTo-Json -Compress)
  }
  return "$($Value.GetType().FullName):$Value"
}

function Get-RegistrySnapshot {
  param([Parameter(Mandatory = $true)][string]$Path)

  if (-not (Test-Path -LiteralPath $Path)) {
    return "missing"
  }
  $keys = @(
    Get-Item -LiteralPath $Path -ErrorAction Stop
    Get-ChildItem -LiteralPath $Path -Recurse -ErrorAction Stop
  ) | Sort-Object -Property Name
  $records = foreach ($key in $keys) {
    $values = foreach ($valueName in @($key.GetValueNames()) | Sort-Object) {
      [ordered]@{
        name = $valueName
        kind = [string]$key.GetValueKind($valueName)
        value = Convert-RegistryValue -Value $key.GetValue(
          $valueName,
          $null,
          [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames
        )
      }
    }
    [ordered]@{
      name = $key.Name
      values = @($values)
    }
  }
  return (@($records) | ConvertTo-Json -Compress -Depth 8)
}

function Remove-VerifiedTemporaryTree {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Path,
    [Parameter(Mandatory = $true)]
    [string]$AllowedRoot,
    [Parameter(Mandatory = $true)]
    [string]$Prefix
  )

  if (-not (Test-Path -LiteralPath $Path)) {
    return
  }
  Assert-DirectTemporaryChild `
    -AllowedRoot $AllowedRoot `
    -Candidate $Path `
    -Prefix $Prefix
  $rootItem = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
  if (-not $rootItem.PSIsContainer -or (Test-ReparsePoint -Item $rootItem)) {
    throw "失敗隔離検査の一時rootは安全に再帰削除できません: $Path"
  }
  foreach ($item in Get-ChildItem -LiteralPath $Path -Force -Recurse) {
    if (Test-ReparsePoint -Item $item) {
      throw "reparse pointを含む失敗隔離一時rootは削除しません: $($item.FullName)"
    }
  }
  Remove-Item -LiteralPath $Path -Force -Recurse
}

$resolvedTempRoot = Resolve-RealDirectory -Path $TempRoot -Label "runner一時root"
$resolvedDistribution = Resolve-RealDirectory `
  -Path $DistributionRoot `
  -Label "検査済みdist"
$resolvedDistributionParent = Resolve-RealDirectory `
  -Path ([System.IO.Path]::GetDirectoryName($resolvedDistribution)) `
  -Label "不一致dist一時root"
$installerItem = Get-Item -LiteralPath $InstallerPath -Force -ErrorAction Stop
if ($installerItem.PSIsContainer -or (Test-ReparsePoint -Item $installerItem)) {
  throw "完成installerはreparse pointではない実fileでなければなりません。"
}
$resolvedInstaller = (Resolve-Path -LiteralPath $InstallerPath).Path

$managedDataRoot = Join-Path `
  ([System.Environment]::GetFolderPath(
    [System.Environment+SpecialFolder]::LocalApplicationData
  )) `
  "LoLReplayTool"
if (Test-Path -LiteralPath $managedDataRoot) {
  throw "実ユーザーデータが存在する環境では失敗隔離検査を実行しません: $managedDataRoot"
}

$uninstallRegistry = (
  "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\" +
  "{B8D87E69-41F7-4B28-978D-2F8FA5AF4BE2}_is1"
)
$startMenuShortcut = Join-Path `
  ([System.Environment]::GetFolderPath(
    [System.Environment+SpecialFolder]::Programs
  )) `
  "LoL Replay Tool.lnk"
$desktopShortcut = Join-Path `
  ([System.Environment]::GetFolderPath(
    [System.Environment+SpecialFolder]::DesktopDirectory
  )) `
  "LoL Replay Tool.lnk"

$existingAuditRoots = @(
  Get-ChildItem -LiteralPath $resolvedTempRoot -Force -Directory |
    Where-Object { $_.Name -like "LoLReplayTool-installer-audit-*" }
)
if ($existingAuditRoots.Count -ne 0) {
  throw "runner一時rootに既存のinstaller監査directoryがあります。"
}

$testId = [guid]::NewGuid().ToString("N")
$negativeRoot = Join-Path `
  $resolvedDistributionParent `
  "LoLReplayTool-installer-negative-$testId"
$expectedMismatch = Join-Path $negativeRoot "expected dist 不一致"
$externalSentinel = Join-Path `
  $resolvedTempRoot `
  "LoLReplayTool-installer-sentinel-$testId.bin"
Assert-DirectTemporaryChild `
  -AllowedRoot $resolvedDistributionParent `
  -Candidate $negativeRoot `
  -Prefix "LoLReplayTool-installer-negative-"
Assert-DirectTemporaryChild `
  -AllowedRoot $resolvedTempRoot `
  -Candidate $externalSentinel `
  -Prefix "LoLReplayTool-installer-sentinel-"
if (Test-PathsOverlap -First $expectedMismatch -Second $resolvedTempRoot) {
  throw "不一致distとinstaller監査一時rootは独立していなければなりません。"
}

$negativeRootCreated = $false
$sentinelCreated = $false
try {
  $distributionItems = @(
    Get-Item -LiteralPath $resolvedDistribution -Force
    Get-ChildItem -LiteralPath $resolvedDistribution -Force -Recurse
  )
  foreach ($item in $distributionItems) {
    if (Test-ReparsePoint -Item $item) {
      throw "検査済みdistにreparse pointがあるため安全にcopyできません: $($item.FullName)"
    }
  }

  New-Item -ItemType Directory -Path $negativeRoot | Out-Null
  $negativeRootCreated = $true
  New-Item -ItemType Directory -Path $expectedMismatch | Out-Null
  foreach ($item in Get-ChildItem -LiteralPath $resolvedDistribution -Force) {
    Copy-Item `
      -LiteralPath $item.FullName `
      -Destination $expectedMismatch `
      -Force `
      -Recurse
  }
  [System.IO.File]::WriteAllBytes(
    (Join-Path $expectedMismatch "intentional-content-mismatch.bin"),
    [System.Text.Encoding]::UTF8.GetBytes("intentional mismatch $testId")
  )
  [System.IO.File]::WriteAllBytes(
    $externalSentinel,
    [System.Text.Encoding]::UTF8.GetBytes("external sentinel $testId")
  )
  $sentinelCreated = $true

  $registryBefore = Get-RegistrySnapshot -Path $uninstallRegistry
  $startMenuBefore = Get-FileSnapshot -Path $startMenuShortcut
  $desktopBefore = Get-FileSnapshot -Path $desktopShortcut
  $sentinelBefore = Get-FileSnapshot -Path $externalSentinel

  $expectedFailure = $null
  try {
    & $auditScript `
      -InstallerPath $resolvedInstaller `
      -DistributionRoot $expectedMismatch `
      -TempRoot $resolvedTempRoot `
      -PythonExe $PythonExe `
      -TimeoutSeconds $TimeoutSeconds
  } catch {
    $expectedFailure = $_
  }
  if ($null -eq $expectedFailure) {
    throw "意図的不一致を含むexpected distが監査を通過しました。"
  }
  $expectedFailureMessage = (
    "完成installerの収録内容が検査済みdistと一致しません。"
  )
  if ($expectedFailure.Exception.Message -cne $expectedFailureMessage) {
    throw (
      "意図したpayload不一致以外でinstaller監査が失敗しました: " +
      $expectedFailure.Exception.Message
    )
  }
  Write-Host "Observed expected installer audit failure: $expectedFailureMessage"

  $stateErrors = @()
  if ((Get-RegistrySnapshot -Path $uninstallRegistry) -cne $registryBefore) {
    $stateErrors += "uninstall registry changed"
  }
  if ((Get-FileSnapshot -Path $startMenuShortcut) -cne $startMenuBefore) {
    $stateErrors += "Start Menu shortcut changed"
  }
  if ((Get-FileSnapshot -Path $desktopShortcut) -cne $desktopBefore) {
    $stateErrors += "Desktop shortcut changed"
  }
  if (Test-Path -LiteralPath $managedDataRoot) {
    $stateErrors += "managed user data root was created"
  }
  if ((Get-FileSnapshot -Path $externalSentinel) -cne $sentinelBefore) {
    $stateErrors += "external sentinel changed"
  }
  $remainingAuditRoots = @(
    Get-ChildItem -LiteralPath $resolvedTempRoot -Force -Directory |
      Where-Object { $_.Name -like "LoLReplayTool-installer-audit-*" }
  )
  if ($remainingAuditRoots.Count -ne 0) {
    $stateErrors += "installer audit temp root remains"
  }
  if ($stateErrors.Count -ne 0) {
    throw "完成installer失敗隔離検査に失敗しました: $($stateErrors -join ', ')"
  }
  Write-Host "Installer audit failure isolation passed."
} finally {
  if ($sentinelCreated -and (Test-Path -LiteralPath $externalSentinel)) {
    Assert-DirectTemporaryChild `
      -AllowedRoot $resolvedTempRoot `
      -Candidate $externalSentinel `
      -Prefix "LoLReplayTool-installer-sentinel-"
    $sentinelItem = Get-Item -LiteralPath $externalSentinel -Force
    if ($sentinelItem.PSIsContainer -or (Test-ReparsePoint -Item $sentinelItem)) {
      throw "外部sentinelを安全に削除できません: $externalSentinel"
    }
    Remove-Item -LiteralPath $externalSentinel -Force
  }
  if ($negativeRootCreated) {
    Remove-VerifiedTemporaryTree `
      -Path $negativeRoot `
      -AllowedRoot $resolvedDistributionParent `
      -Prefix "LoLReplayTool-installer-negative-"
  }
}
