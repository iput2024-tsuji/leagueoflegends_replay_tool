$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location (Join-Path $scriptDir "..")

$venvPython = Join-Path (Get-Location) "venv\Scripts\python.exe"
$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
$pythonExe = if (Test-Path $venvPython) { $venvPython } elseif ($pythonCmd) { $pythonCmd.Source } else { $null }
$makeIconScript = "scripts\make_icon.py"
if ($pythonExe -and (Test-Path $makeIconScript)) {
  & $pythonExe $makeIconScript
  if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
  }
}

$pyArgs = @(
  "-y",
  "--noconsole",
  "--onedir",
  "--contents-directory", ".",
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
$pyInstallerCmd = Get-Command pyinstaller -ErrorAction SilentlyContinue
$buildExitCode = 0
if (-not $pyInstallerCmd) {
  $venvPyInstaller = Join-Path (Get-Location) "venv\\Scripts\\pyinstaller.exe"
  if (Test-Path $venvPyInstaller) {
    & $venvPyInstaller @pyArgs
    $buildExitCode = $LASTEXITCODE
  } else {
    throw "pyinstaller が見つかりません。venv を有効化するか、pip install pyinstaller を実行してください。"
  }
} else {
  & $pyInstallerCmd.Source @pyArgs
  $buildExitCode = $LASTEXITCODE
}

if ($buildExitCode -ne 0) {
  exit $buildExitCode
}

$distRootDir = Join-Path (Get-Location) "dist\\LoLReplayTool"
$distBinDir = Join-Path $distRootDir "bin"
$distObsDir = Join-Path $distRootDir "obs-portable"
New-Item -ItemType Directory -Path $distBinDir -Force | Out-Null
New-Item -ItemType Directory -Path $distObsDir -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $distRootDir "assets\\champions\\icons") -Force | Out-Null

# Keep distribution clean: OBS, mpv DLLs, and third-party game assets must be user-provided.
$mpvDllPattern = '^(lib)?mpv-\d+\.dll$'
$bundledMpvDlls = Get-ChildItem -Path $distRootDir -Recurse -File -ErrorAction SilentlyContinue | Where-Object {
  $_.Name -match $mpvDllPattern
}
foreach ($dll in $bundledMpvDlls) {
  Remove-Item -Path $dll.FullName -Force -ErrorAction SilentlyContinue
}

$bundledSetupArchives = Get-ChildItem -Path $distBinDir -File -ErrorAction SilentlyContinue | Where-Object {
  $_.Name -match '^(OBS-Studio|ffmpeg)-.*\.(zip|7z)$'
}
foreach ($archive in $bundledSetupArchives) {
  Remove-Item -Path $archive.FullName -Force -ErrorAction SilentlyContinue
}

Write-Host "Build complete. Portable OBS, mpv DLLs, FFmpeg, and game assets are not bundled."
Write-Host "OBS and FFmpeg are downloaded automatically on first launch with pinned SHA256 verification."
Write-Host "Place mpv DLLs under dist\\LoLReplayTool\\bin manually."

