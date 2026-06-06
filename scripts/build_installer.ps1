param(
  [string]$Version = "",
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
$pythonExe = if (Test-Path $venvPython) {
  $venvPython
} elseif ($pythonCmd) {
  $pythonCmd.Source
} else {
  $null
}

if (-not $SkipTests) {
  if (-not $pythonExe) {
    throw "Python が見つからないためテストを実行できません。"
  }
  $testTemp = Join-Path $repoRoot ("tests\_tmp\installer-build-" + [guid]::NewGuid().ToString("N"))
  & $pythonExe -m pytest -p no:cacheprovider "--basetemp=$testTemp" tests
  if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
  }
}

if (-not $SkipBuild) {
  & (Join-Path $scriptDir "build.ps1")
  if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
  }
}

$appExe = Join-Path $repoRoot "dist\LoLReplayTool\LoLReplayTool.exe"
if (-not (Test-Path $appExe)) {
  throw "アプリのビルド成果物が見つかりません: $appExe"
}

$previousDataDir = $env:LOL_REPLAY_TOOL_DATA_DIR
$selfCheckDir = Join-Path $repoRoot ("tests\_tmp\installer-self-check-" + [guid]::NewGuid().ToString("N"))
try {
  $env:LOL_REPLAY_TOOL_DATA_DIR = $selfCheckDir
  $selfCheckProcess = Start-Process `
    -FilePath $appExe `
    -ArgumentList "--self-check" `
    -WindowStyle Hidden `
    -Wait `
    -PassThru
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
