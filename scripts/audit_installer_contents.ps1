param(
  [Parameter(Mandatory = $true)]
  [string]$InstallerPath,
  [Parameter(Mandatory = $true)]
  [string]$DistributionRoot,
  [Parameter(Mandatory = $true)]
  [string]$TempRoot,
  [string]$PythonExe = "",
  [string]$OutputReceipt = "",
  [ValidateRange(1, 1800)]
  [int]$TimeoutSeconds = 600
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not $IsWindows) {
  throw "完成installerの収録内容検査はWindowsでだけ実行できます。"
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $scriptDir "..")).Path

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

  $inputItem = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
  if (-not $inputItem.PSIsContainer -or (Test-ReparsePoint -Item $inputItem)) {
    throw "$Label はreparse pointではない実ディレクトリでなければなりません: $Path"
  }
  $resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
  return [System.IO.Path]::GetFullPath($resolved)
}

function Assert-RealDirectoryChain {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Path,
    [Parameter(Mandatory = $true)]
    [string]$Label
  )

  $fullPath = [System.IO.Path]::GetFullPath($Path)
  $pathRoot = [System.IO.Path]::GetPathRoot($fullPath)
  $relative = [System.IO.Path]::GetRelativePath($pathRoot, $fullPath)
  $current = $pathRoot
  if ($relative -eq ".") {
    throw "$Label にdrive rootは指定できません: $fullPath"
  }
  foreach ($part in $relative.Split(
    [char[]]@(
      [System.IO.Path]::DirectorySeparatorChar,
      [System.IO.Path]::AltDirectorySeparatorChar
    ),
    [System.StringSplitOptions]::RemoveEmptyEntries
  )) {
    $current = Join-Path $current $part
    $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
    if (-not $item.PSIsContainer -or (Test-ReparsePoint -Item $item)) {
      throw "$Label の経路にreparse pointまたは非directoryがあります: $current"
    }
  }
}

function Test-PathWithin {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Root,
    [Parameter(Mandatory = $true)]
    [string]$Candidate
  )

  $relative = [System.IO.Path]::GetRelativePath($Root, $Candidate)
  return (
    $relative -ne "." -and
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
}

function Remove-VerifiedAuditRoot {
  param(
    [Parameter(Mandatory = $true)]
    [string]$AuditRoot,
    [Parameter(Mandatory = $true)]
    [string]$AllowedRoot
  )

  if (-not (Test-Path -LiteralPath $AuditRoot)) {
    return
  }
  $fullAuditRoot = [System.IO.Path]::GetFullPath($AuditRoot)
  if (
    -not (Test-PathWithin -Root $AllowedRoot -Candidate $fullAuditRoot) -or
    -not ([System.IO.Path]::GetFileName($fullAuditRoot)).StartsWith(
      "LoLReplayTool-installer-audit-",
      [System.StringComparison]::Ordinal
    )
  ) {
    throw "監査一時directoryの削除境界を確認できません: $fullAuditRoot"
  }

  $rootItem = Get-Item -LiteralPath $fullAuditRoot -Force -ErrorAction Stop
  if (Test-ReparsePoint -Item $rootItem) {
    throw "reparse pointになった監査一時directoryは再帰走査しません: $fullAuditRoot"
  }
  $items = @(Get-ChildItem `
    -LiteralPath $fullAuditRoot `
    -Force `
    -Recurse `
    -ErrorAction Stop)
  foreach ($item in $items) {
    if (Test-ReparsePoint -Item $item) {
      throw "reparse pointを含む監査一時directoryは再帰削除しません: $($item.FullName)"
    }
  }
  Remove-Item -LiteralPath $fullAuditRoot -Force -Recurse
}

$installerInputItem = Get-Item -LiteralPath $InstallerPath -Force -ErrorAction Stop
if ($installerInputItem.PSIsContainer) {
  throw "完成installerが見つかりません: $InstallerPath"
}
if (Test-ReparsePoint -Item $installerInputItem) {
  throw "完成installerにreparse pointは使用できません: $InstallerPath"
}
$resolvedInstaller = (
  Resolve-Path -LiteralPath $InstallerPath -ErrorAction Stop
).Path

$resolvedDistribution = Resolve-RealDirectory `
  -Path $DistributionRoot `
  -Label "検査済みdist"
$resolvedTempRoot = Resolve-RealDirectory `
  -Path $TempRoot `
  -Label "runner一時root"
Assert-RealDirectoryChain -Path $resolvedTempRoot -Label "runner一時root"

if (
  (Test-PathWithin -Root $resolvedDistribution -Candidate $resolvedTempRoot) -or
  (Test-PathWithin -Root $resolvedTempRoot -Candidate $resolvedDistribution) -or
  $resolvedDistribution -ieq $resolvedTempRoot
) {
  throw "runner一時rootと検査済みdistは互いに独立していなければなりません。"
}

$resolvedReceipt = $null
if (-not [string]::IsNullOrWhiteSpace($OutputReceipt)) {
  $resolvedReceipt = [System.IO.Path]::GetFullPath($OutputReceipt)
  if (-not (Test-PathWithin -Root $resolvedTempRoot -Candidate $resolvedReceipt)) {
    throw "installer監査receiptはrunner一時root内へ出力してください: $resolvedReceipt"
  }
  $receiptParent = Split-Path -Parent $resolvedReceipt
  [void](Resolve-RealDirectory -Path $receiptParent -Label "installer監査receipt parent")
  Assert-RealDirectoryChain -Path $receiptParent -Label "installer監査receipt parent"
  if (Test-Path -LiteralPath $resolvedReceipt) {
    throw "installer監査receiptは新規pathへ出力してください: $resolvedReceipt"
  }
}

$venvPython = Join-Path $repoRoot "venv\Scripts\python.exe"
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not [string]::IsNullOrWhiteSpace($PythonExe)) {
  $selectedPython = (Resolve-Path -LiteralPath $PythonExe -ErrorAction Stop).Path
} elseif (Test-Path -LiteralPath $venvPython -PathType Leaf) {
  $selectedPython = $venvPython
} elseif ($pythonCommand) {
  $selectedPython = $pythonCommand.Source
} else {
  $selectedPython = $null
}
if (-not $selectedPython -or -not (Test-Path -LiteralPath $selectedPython -PathType Leaf)) {
  throw "installer収録内容を検査するPythonが見つかりません。"
}
$pythonItem = Get-Item -LiteralPath $selectedPython -Force
if (Test-ReparsePoint -Item $pythonItem) {
  throw "installer収録内容検査用Pythonにreparse pointは使用できません: $selectedPython"
}

$innoScript = Join-Path $repoRoot "installer\LoLReplayTool.iss"
Push-Location $repoRoot
try {
  & $selectedPython -m scripts.installer_content_audit `
    --inno-script $innoScript `
    --validate-inno-only
  if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup scriptが監査実行前の安全性検査に失敗しました。"
  }
} finally {
  Pop-Location
}

$auditRoot = Join-Path $resolvedTempRoot (
  "LoLReplayTool-installer-audit-" + [guid]::NewGuid().ToString("N")
)
$payloadRoot = Join-Path $auditRoot "展開 内容"
$processTemp = Join-Path $auditRoot "一時 作業"
$setupLog = Join-Path $auditRoot "setup-content-audit.log"
if (Test-Path -LiteralPath $auditRoot) {
  throw "新規監査一時directoryが既に存在します: $auditRoot"
}

$auditCreated = $false
try {
  New-Item -ItemType Directory -Path $auditRoot -ErrorAction Stop | Out-Null
  $auditCreated = $true
  New-Item -ItemType Directory -Path $processTemp -ErrorAction Stop | Out-Null
  if (-not (Test-PathWithin -Root $resolvedTempRoot -Candidate $auditRoot)) {
    throw "監査一時directoryがrunner一時rootの外側です: $auditRoot"
  }

  $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
  $startInfo.FileName = $resolvedInstaller
  $startInfo.WorkingDirectory = $auditRoot
  $startInfo.UseShellExecute = $false
  $startInfo.CreateNoWindow = $true
  foreach ($argument in @(
    "/SP-",
    "/VERYSILENT",
    "/SUPPRESSMSGBOXES",
    "/NOCANCEL",
    "/NORESTART",
    "/NOCLOSEAPPLICATIONS",
    "/NORESTARTAPPLICATIONS",
    "/NOICONS",
    "/TASKS=",
    "/LANG=japanese",
    "/CONTENTAUDIT=1",
    "/DIR=$payloadRoot",
    "/LOG=$setupLog"
  )) {
    $startInfo.ArgumentList.Add($argument)
  }
  $startInfo.Environment["TEMP"] = $processTemp
  $startInfo.Environment["TMP"] = $processTemp
  foreach ($secretName in @("GH_TOKEN", "GITHUB_TOKEN", "ACTIONS_RUNTIME_TOKEN")) {
    [void]$startInfo.Environment.Remove($secretName)
  }

  $process = [System.Diagnostics.Process]::new()
  $process.StartInfo = $startInfo
  try {
    if (-not $process.Start()) {
      throw "完成installer processを開始できません。"
    }
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
      $process.Kill($true)
      $process.WaitForExit()
      throw "完成installerの監査展開がtimeoutしました。"
    }
    if ($process.ExitCode -ne 0) {
      throw "完成installerの監査展開が失敗しました: exit $($process.ExitCode)"
    }
  } finally {
    $process.Dispose()
  }

  if (-not (Test-Path -LiteralPath $payloadRoot -PathType Container)) {
    throw "完成installerが監査payloadを生成しませんでした: $payloadRoot"
  }
  $payloadItem = Get-Item -LiteralPath $payloadRoot -Force
  if (Test-ReparsePoint -Item $payloadItem) {
    throw "完成installerの監査payload rootがreparse pointです: $payloadRoot"
  }

  Push-Location $repoRoot
  try {
    $auditArguments = @(
      "-m", "scripts.installer_content_audit",
      "--distribution-root", $resolvedDistribution,
      "--installed-root", $payloadRoot,
      "--inno-script", $innoScript
    )
    if ($resolvedReceipt) {
      $auditArguments += @(
        "--installer", $resolvedInstaller,
        "--output-receipt", $resolvedReceipt
      )
    }
    & $selectedPython @auditArguments
    if ($LASTEXITCODE -ne 0) {
      throw "完成installerの収録内容が検査済みdistと一致しません。"
    }
  } finally {
    Pop-Location
  }
  Write-Host "Installer content audit complete: $resolvedInstaller"
} finally {
  if ($auditCreated) {
    Remove-VerifiedAuditRoot `
      -AuditRoot $auditRoot `
      -AllowedRoot $resolvedTempRoot
  }
}
