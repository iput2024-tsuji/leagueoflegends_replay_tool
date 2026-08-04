param(
  [string]$Version = "",
  [string]$PythonExe = "",
  [string]$BuildProvenance = "",
  [string]$BuildProvenanceSha256 = "",
  [string]$InnoSetupRoot = "",
  [string]$InnoSetupProvenance = "",
  [switch]$SkipTests,
  [switch]$SkipBuild,
  [switch]$SkipSelfCheck
)

$ErrorActionPreference = "Stop"

if ($SkipSelfCheck -and -not $SkipBuild) {
  throw "-SkipSelfCheck は、検証済みの既存成果物を使う -SkipBuild との併用時だけ指定できます。"
}

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
if (-not $resolvedBuildProvenance -and -not [string]::IsNullOrWhiteSpace($BuildProvenanceSha256)) {
  throw "build provenance を指定せずに固定SHA256だけを指定することはできません。"
}

function Assert-BuildProvenance {
  if (-not $resolvedBuildProvenance) {
    return
  }
  if ($BuildProvenanceSha256 -cnotmatch '^[0-9a-f]{64}$') {
    throw "build provenance の固定SHA256が不正です。"
  }
  $actual = (Get-FileHash -LiteralPath $resolvedBuildProvenance -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($actual -cne $BuildProvenanceSha256) {
    throw "build provenance が固定SHA256と一致しません。"
  }
}

Assert-BuildProvenance

$resolvedInnoSetupRoot = $null
$resolvedInnoSetupProvenance = $null
if (
  [string]::IsNullOrWhiteSpace($InnoSetupRoot) -xor
  [string]::IsNullOrWhiteSpace($InnoSetupProvenance)
) {
  throw "検証済みInno Setupはrootとprovenanceを必ず同時に指定してください。"
}
if (-not [string]::IsNullOrWhiteSpace($InnoSetupRoot)) {
  $resolvedInnoSetupRoot = (
    Resolve-Path -LiteralPath $InnoSetupRoot -ErrorAction Stop
  ).Path
  $resolvedInnoSetupProvenance = (
    Resolve-Path -LiteralPath $InnoSetupProvenance -ErrorAction Stop
  ).Path
  if (-not (Test-Path -LiteralPath $resolvedInnoSetupRoot -PathType Container)) {
    throw "Inno Setup rootが見つかりません: $InnoSetupRoot"
  }
  if (-not (Test-Path -LiteralPath $resolvedInnoSetupProvenance -PathType Leaf)) {
    throw "Inno Setup provenanceが見つかりません: $InnoSetupProvenance"
  }
}
if ($resolvedBuildProvenance -and -not $resolvedInnoSetupRoot) {
  throw "build provenanceを使うinstaller buildには検証済みInno Setup rootとprovenanceが必要です。"
}

function Assert-InnoSetupProvenance {
  if (-not $resolvedInnoSetupRoot) {
    return
  }
  if (-not $selectedPython) {
    throw "Inno Setup provenanceを検査するPythonが見つかりません。"
  }
  $arguments = @(
    "-m", "scripts.inno_setup_provenance", "verify",
    "--components", (Join-Path $repoRoot "compliance\components.json"),
    "--install-root", $resolvedInnoSetupRoot,
    "--provenance", $resolvedInnoSetupProvenance
  )
  if ($resolvedBuildProvenance) {
    $arguments += @("--build-provenance", $resolvedBuildProvenance)
  }
  & $selectedPython @arguments
  if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
  }
}

Assert-InnoSetupProvenance

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
  $buildArgs = @{}
  if ($selectedPython) {
    $buildArgs["PythonExe"] = $selectedPython
  }
  if ($resolvedBuildProvenance) {
    $buildArgs["BuildProvenance"] = $resolvedBuildProvenance
    $buildArgs["BuildProvenanceSha256"] = $BuildProvenanceSha256
  }
  & (Join-Path $scriptDir "build.ps1") @buildArgs
  if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
  }
  Assert-BuildProvenance
}

$appExe = Join-Path $repoRoot "dist\LoLReplayTool\LoLReplayTool.exe"
if (-not (Test-Path $appExe)) {
  throw "アプリのビルド成果物が見つかりません: $appExe"
}
if (-not $selectedPython) {
  throw "Python が見つからないためライセンス資料を検査できません。"
}

$complianceArgs = @(
  "-m", "scripts.check_license_compliance",
  (Join-Path $repoRoot "dist\LoLReplayTool")
)
if ($resolvedBuildProvenance) {
  $complianceArgs += @(
    "--build-provenance-sha256", $BuildProvenanceSha256
  )
}
& $selectedPython @complianceArgs
if ($LASTEXITCODE -ne 0) {
  exit $LASTEXITCODE
}
Assert-BuildProvenance

if (-not $SkipSelfCheck) {
  & (Join-Path $scriptDir "run_packaged_self_check.ps1") `
    -AppExe $appExe `
    -TempRoot (Join-Path $repoRoot "dist") `
    -TimeoutSeconds 60
}
Assert-BuildProvenance

$isccPath = $null
if ($resolvedInnoSetupRoot) {
  $isccPath = Join-Path $resolvedInnoSetupRoot "ISCC.exe"
} else {
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
}
if (-not $isccPath) {
  throw "Inno Setup 6 が見つかりません。winget install --id JRSoftware.InnoSetup -e を実行してください。"
}
if (-not (Test-Path -LiteralPath $isccPath -PathType Leaf)) {
  throw "ISCC.exeが見つかりません: $isccPath"
}
Assert-InnoSetupProvenance

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
Assert-BuildProvenance
Assert-InnoSetupProvenance

$expectedInnoCopyright = "Copyright © 1997-2026 Jordan Russell. Portions Copyright © 2000-2026 Martijn Laan."
$expectedInnoWebsite = "https://www.innosetup.com"
$expectedInnoDescription = "LoL Replay Tool Setup - Inno Setup $expectedInnoWebsite"
$expectedPublisher = "LoL Replay Tool Contributors"
$installerVersionInfo = (Get-Item -LiteralPath $installerPath).VersionInfo
if (
  $installerVersionInfo.LegalCopyright.Trim() -cne $expectedInnoCopyright -or
  $installerVersionInfo.FileDescription.Trim() -cne $expectedInnoDescription -or
  $installerVersionInfo.CompanyName.Trim() -cne $expectedPublisher
) {
  throw "生成installerのpublisher、Inno Setup copyrightまたはwebsite表示が一致しません。"
}

Write-Host "Installer build complete: $installerPath"
