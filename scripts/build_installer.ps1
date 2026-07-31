param(
  [string]$Version = "",
  [string]$PythonExe = "",
  [string]$BuildProvenance = "",
  [switch]$SkipTests,
  [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir "..")).Path
Set-Location $repoRoot

if ([string]::IsNullOrWhiteSpace($Version)) {
  $versionFile = Join-Path $repoRoot "VERSION"
  if (-not (Test-Path $versionFile)) {
    throw "VERSION が見つかりません。"
  }
  $Version = (Get-Content $versionFile -Raw).Trim()
}

if ($Version -notmatch '^\d+\.\d+\.\d+(?:\.\d+)?$') {
  throw "バージョンは 1.2.3 または 1.2.3.4 の形式で指定してください: $Version"
}

$venvPython = Join-Path $repoRoot "venv\Scripts\python.exe"
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not [string]::IsNullOrWhiteSpace($PythonExe)) {
  $selectedPython = (Resolve-Path -LiteralPath $PythonExe -ErrorAction Stop).Path
  if (-not (Test-Path -LiteralPath $selectedPython -PathType Leaf)) {
    throw "指定された Python 実行ファイルが見つかりません: $PythonExe"
  }
} elseif (Test-Path -LiteralPath $venvPython -PathType Leaf) {
  $selectedPython = $venvPython
} elseif ($pythonCmd) {
  $selectedPython = $pythonCmd.Source
} else {
  $selectedPython = $null
}

$resolvedBuildProvenance = $null
if (-not [string]::IsNullOrWhiteSpace($BuildProvenance)) {
  $resolvedBuildProvenance = (
    Resolve-Path -LiteralPath $BuildProvenance -ErrorAction Stop
  ).Path
  if (-not (Test-Path -LiteralPath $resolvedBuildProvenance -PathType Leaf)) {
    throw "build provenance が見つかりません: $BuildProvenance"
  }
}

if (-not $SkipTests) {
  if (-not $selectedPython) {
    throw "Python が見つからないためテストを実行できません。"
  }
  $testTemp = Join-Path $repoRoot ("tests\_tmp\installer-build-" + [guid]::NewGuid().ToString("N"))
  & $selectedPython -m pytest -p no:cacheprovider "--basetemp=$testTemp" tests
  if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
  }
}

if (-not $SkipBuild) {
  $buildArgs = @()
  if ($selectedPython) {
    $buildArgs += @("-PythonExe", $selectedPython)
  }
  if ($resolvedBuildProvenance) {
    $buildArgs += @("-BuildProvenance", $resolvedBuildProvenance)
  }
  & (Join-Path $scriptDir "build.ps1") @buildArgs
  if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
  }
}

$appExe = Join-Path $repoRoot "dist\LoLReplayTool\LoLReplayTool.exe"
if (-not (Test-Path $appExe)) {
  throw "アプリのビルド成果物が見つかりません: $appExe"
}
if (-not $selectedPython) {
  throw "Python が見つからないためライセンス資料を検査できません。"
}

& $selectedPython -m scripts.check_license_compliance (Join-Path $repoRoot "dist\LoLReplayTool")
if ($LASTEXITCODE -ne 0) {
  exit $LASTEXITCODE
}

$previousDataDir = $env:LOL_REPLAY_TOOL_DATA_DIR
$selfCheckDir = Join-Path $repoRoot ("dist\installer-self-check-" + [guid]::NewGuid().ToString("N"))
$selfCheckStdout = Join-Path $selfCheckDir "stdout.txt"
$selfCheckStderr = Join-Path $selfCheckDir "stderr.txt"
try {
  New-Item -ItemType Directory -Path $selfCheckDir -Force | Out-Null
  $env:LOL_REPLAY_TOOL_DATA_DIR = $selfCheckDir
  $selfCheckProcess = Start-Process `
    -FilePath $appExe `
    -ArgumentList "--self-check" `
    -WindowStyle Hidden `
    -RedirectStandardOutput $selfCheckStdout `
    -RedirectStandardError $selfCheckStderr `
    -PassThru
  if (-not $selfCheckProcess.WaitForExit(60000)) {
    $selfCheckProcess.Kill($true)
    $selfCheckProcess.WaitForExit()
    throw "packaged self-check が60秒以内に終了しませんでした。"
  }
  if (Test-Path $selfCheckStdout) {
    Get-Content $selfCheckStdout | Write-Host
  }
  if (Test-Path $selfCheckStderr) {
    Get-Content $selfCheckStderr | Write-Error
  }
  if ($selfCheckProcess.ExitCode -ne 0) {
    exit $selfCheckProcess.ExitCode
  }
} finally {
  $env:LOL_REPLAY_TOOL_DATA_DIR = $previousDataDir
}

$isccCandidates = @()
$isccCommand = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if ($isccCommand) {
  $isccCandidates += $isccCommand.Source
}
if (${env:ProgramFiles(x86)}) {
  $isccCandidates += Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"
}
if ($env:ProgramFiles) {
  $isccCandidates += Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"
}
if ($env:LOCALAPPDATA) {
  $isccCandidates += Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"
}
$isccPath = $isccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $isccPath) {
  throw "Inno Setup 6 が見つかりません。winget install --id JRSoftware.InnoSetup -e を実行してください。"
}

$installerDir = Join-Path $repoRoot "dist\installer"
New-Item -ItemType Directory -Path $installerDir -Force | Out-Null
$installerPath = Join-Path $installerDir "LoLReplayTool-Setup-$Version.exe"
if (Test-Path $installerPath) {
  Remove-Item -LiteralPath $installerPath -Force
}

& $isccPath "/DAppVersion=$Version" (Join-Path $repoRoot "installer\LoLReplayTool.iss")
if ($LASTEXITCODE -ne 0) {
  exit $LASTEXITCODE
}
if (-not (Test-Path $installerPath)) {
  throw "インストーラーが生成されませんでした: $installerPath"
}

Write-Host "Installer build complete: $installerPath"
