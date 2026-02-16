$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location (Join-Path $scriptDir "..")

$pyArgs = @(
  "-y",
  "--noconsole",
  "--onedir",
  "--contents-directory", ".",
  "--name", "LoLReplayTool",
  "--clean",
  "--add-data", "config\\setting.sample.json;config",
  "--add-data", "config\\champion_aliases.json;config",
  "--add-data", "assets\\champions\\icons;assets\\champions\\icons"
)

$iconPath = "assets\\app\\app.ico"
if (Test-Path $iconPath) {
  $pyArgs += "--icon"
  $pyArgs += $iconPath
  $pyArgs += "--add-data"
  $pyArgs += "$iconPath;assets\\app"
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
New-Item -ItemType Directory -Path $distBinDir -Force | Out-Null

# Keep distribution clean: mpv DLLs must be user-provided, not bundled.
$mpvDllPattern = '^(lib)?mpv-\d+\.dll$'
$bundledMpvDlls = Get-ChildItem -Path $distRootDir -Recurse -File -ErrorAction SilentlyContinue | Where-Object {
  $_.Name -match $mpvDllPattern
}
foreach ($dll in $bundledMpvDlls) {
  Remove-Item -Path $dll.FullName -Force -ErrorAction SilentlyContinue
}
