param(
  [string]$PythonExe = "",
  [string]$BuildProvenance = "",
  [string]$BuildProvenanceSha256 = ""
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location (Join-Path $scriptDir "..")

$venvPython = Join-Path (Get-Location) "venv\Scripts\python.exe"
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
if (-not $selectedPython) {
  throw "Python が見つかりません。venv を作成するか、Python を PATH に追加してください。"
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

$makeIconScript = "scripts\make_icon.py"
if (Test-Path $makeIconScript) {
  & $selectedPython $makeIconScript
  if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
  }
}

& $selectedPython -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
  throw "選択した Python 環境に PyInstaller がありません。pip install pyinstaller を実行してください。"
}
& $selectedPython -m PyInstaller --noconfirm --clean "LoLReplayTool.spec"
if ($LASTEXITCODE -ne 0) {
  exit $LASTEXITCODE
}
Assert-BuildProvenance

$distRootDir = Join-Path (Get-Location) "dist\\LoLReplayTool"
$forbiddenRuntimePaths = @()
$forbiddenRuntimePaths += Get-ChildItem -Path $distRootDir -Recurse -Directory -ErrorAction SilentlyContinue | Where-Object {
  $_.Name -in @('obs-portable', 'OBS-Studio')
}
$forbiddenRuntimePaths += Get-ChildItem -Path $distRootDir -Recurse -File -ErrorAction SilentlyContinue | Where-Object {
  $_.Name -in @('obs64.exe', 'ffmpeg.exe', 'ffprobe.exe', 'ffplay.exe') -or
  $_.Name -match '^(OBS-Studio-.*\.(exe|msi|zip|7z)|ffmpeg-.*\.(zip|7z))$'
}
if ($forbiddenRuntimePaths.Count -gt 0) {
  $runtimeList = ($forbiddenRuntimePaths | ForEach-Object { $_.FullName }) -join [Environment]::NewLine
  throw "利用者が用意するOBS／standalone FFmpegが成果物へ混入しています。ビルドを中止します。`n$runtimeList"
}

# Keep distribution clean: OBS, mpv DLLs, and third-party game assets must be user-provided.
& (Join-Path $scriptDir "check_mpv_distribution.ps1") `
  -DistributionRoot $distRootDir

$licensesDir = Join-Path $distRootDir "licenses"
$licenseArgs = @("-m", "scripts.collect_licenses", "--destination", $licensesDir)
if ($resolvedBuildProvenance) {
  $licenseArgs += @(
    "--build-provenance", $resolvedBuildProvenance,
    "--build-provenance-sha256", $BuildProvenanceSha256
  )
}
Assert-BuildProvenance
& $selectedPython @licenseArgs
if ($LASTEXITCODE -ne 0) {
  exit $LASTEXITCODE
}
Assert-BuildProvenance

$collectToc = Join-Path (Get-Location) "build\\LoLReplayTool\\COLLECT-00.toc"
if (-not (Test-Path $collectToc)) {
  throw "PyInstaller COLLECT TOC が見つかりません: $collectToc"
}
$writeComplianceArgs = @(
  "-m", "scripts.check_license_compliance", $distRootDir,
  "--toc", $collectToc,
  "--write-manifest"
)
if ($resolvedBuildProvenance) {
  $writeComplianceArgs += @(
    "--build-provenance-sha256", $BuildProvenanceSha256
  )
}
& $selectedPython @writeComplianceArgs
if ($LASTEXITCODE -ne 0) {
  exit $LASTEXITCODE
}

$verifyComplianceArgs = @(
  "-m", "scripts.check_license_compliance", $distRootDir
)
if ($resolvedBuildProvenance) {
  $verifyComplianceArgs += @(
    "--build-provenance-sha256", $BuildProvenanceSha256
  )
}
& $selectedPython @verifyComplianceArgs
if ($LASTEXITCODE -ne 0) {
  exit $LASTEXITCODE
}
Assert-BuildProvenance

Write-Host "Build complete. Runtime dependencies are stored under dist\\LoLReplayTool\\_internal."
Write-Host "Project and third-party license materials are stored under dist\\LoLReplayTool and its licenses directory."
Write-Host "Portable OBS, mpv DLLs, the standalone FFmpeg executable, and game assets are not bundled."
Write-Host "The OpenCV wheel includes its own FFmpeg DLL and notices."
Write-Host "Users explicitly obtain and place portable OBS and standalone FFmpeg; the application does not download or mirror them."
Write-Host "FFmpeg search order: explicit setting, data bin, application-root fallbacks, then safe absolute PATH entries."
Write-Host "Place mpv DLLs under %LOCALAPPDATA%\\LoLReplayTool\\bin manually."

