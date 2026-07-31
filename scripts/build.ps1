param(
  [string]$PythonExe = "",
  [string]$BuildProvenance = ""
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

$makeIconScript = "scripts\make_icon.py"
if (Test-Path $makeIconScript) {
  & $selectedPython $makeIconScript
  if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
  }
}

$pyArgs = @(
  "-y",
  "--noconsole",
  "--onedir",
  "--contents-directory", "_internal",
  "--name", "LoLReplayTool",
  "--clean",
  "--hidden-import", "mpv",
  "--add-data", "config\\setting.sample.json;config",
  "--add-data", "config\\champion_aliases.json;config"
)

$iconCandidates = @("assets\\icon.ico", "assets\\app\\app.ico")
$iconPath = $iconCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($iconPath -and (Test-Path $iconPath)) {
  $pyArgs += "--icon=$iconPath"
  $iconDest = if ($iconPath -eq "assets\\icon.ico") { "assets" } else { "assets\\app" }
  $pyArgs += "--add-data"
  $pyArgs += "$iconPath;$iconDest"
}

$pyArgs += "main.py"
& $selectedPython -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
  throw "選択した Python 環境に PyInstaller がありません。pip install pyinstaller を実行してください。"
}
& $selectedPython -m PyInstaller @pyArgs
if ($LASTEXITCODE -ne 0) {
  exit $LASTEXITCODE
}

$distRootDir = Join-Path (Get-Location) "dist\\LoLReplayTool"
$distObsDir = Join-Path $distRootDir "obs-portable"
if (Test-Path $distObsDir) {
  Remove-Item -Path $distObsDir -Recurse -Force -ErrorAction SilentlyContinue
}

# Keep distribution clean: OBS, mpv DLLs, and third-party game assets must be user-provided.
$mpvDllPattern = '^(lib)?mpv-\d+\.dll$'
$bundledMpvDlls = Get-ChildItem -Path $distRootDir -Recurse -File -ErrorAction SilentlyContinue | Where-Object {
  $_.Name -match $mpvDllPattern
}
foreach ($dll in $bundledMpvDlls) {
  Remove-Item -Path $dll.FullName -Force -ErrorAction SilentlyContinue
}

$bundledSetupArchives = Get-ChildItem -Path $distRootDir -Recurse -File -ErrorAction SilentlyContinue | Where-Object {
  $_.Name -match '^(OBS-Studio|ffmpeg)-.*\.(zip|7z)$'
}
foreach ($archive in $bundledSetupArchives) {
  Remove-Item -Path $archive.FullName -Force -ErrorAction SilentlyContinue
}

$licensesDir = Join-Path $distRootDir "licenses"
$licenseArgs = @("scripts\collect_licenses.py", "--destination", $licensesDir)
if ($resolvedBuildProvenance) {
  $licenseArgs += @("--build-provenance", $resolvedBuildProvenance)
}
& $selectedPython @licenseArgs
if ($LASTEXITCODE -ne 0) {
  exit $LASTEXITCODE
}

$collectToc = Join-Path (Get-Location) "build\\LoLReplayTool\\COLLECT-00.toc"
if (-not (Test-Path $collectToc)) {
  throw "PyInstaller COLLECT TOC が見つかりません: $collectToc"
}
& $selectedPython -m scripts.check_license_compliance $distRootDir `
  --toc $collectToc `
  --write-manifest
if ($LASTEXITCODE -ne 0) {
  exit $LASTEXITCODE
}

& $selectedPython -m scripts.check_license_compliance $distRootDir
if ($LASTEXITCODE -ne 0) {
  exit $LASTEXITCODE
}

Write-Host "Build complete. Runtime dependencies are stored under dist\\LoLReplayTool\\_internal."
Write-Host "Project and third-party license materials are stored under dist\\LoLReplayTool and its licenses directory."
Write-Host "Portable OBS, mpv DLLs, the standalone FFmpeg executable, and game assets are not bundled."
Write-Host "The OpenCV wheel includes its own FFmpeg DLL and notices."
Write-Host "OBS is downloaded on first launch. The standalone FFmpeg executable is downloaded on first clip export. Downloads use pinned SHA256 verification."
Write-Host "Place mpv DLLs under %LOCALAPPDATA%\\LoLReplayTool\\bin manually."

