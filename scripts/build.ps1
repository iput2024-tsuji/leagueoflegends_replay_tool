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

$binCandidates = @(
  "bin\\mpv-1.dll",
  "bin\\libmpv-1.dll",
  "bin\\mpv-2.dll",
  "bin\\libmpv-2.dll"
)

foreach ($path in $binCandidates) {
  if (Test-Path $path) {
    $pyArgs += "--add-binary"
    $pyArgs += "$path;bin"
  }
}

$pyArgs += "main.py"
$pyInstallerCmd = Get-Command pyinstaller -ErrorAction SilentlyContinue
if (-not $pyInstallerCmd) {
  $venvPyInstaller = Join-Path (Get-Location) "venv\\Scripts\\pyinstaller.exe"
  if (Test-Path $venvPyInstaller) {
    & $venvPyInstaller @pyArgs
    exit $LASTEXITCODE
  }
  throw "pyinstaller が見つかりません。venv を有効化するか、pip install pyinstaller を実行してください。"
}

& $pyInstallerCmd.Source @pyArgs
