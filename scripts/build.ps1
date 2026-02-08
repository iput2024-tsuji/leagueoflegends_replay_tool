$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location (Join-Path $scriptDir "..")

$pyArgs = @(
  "--noconsole",
  "--onedir",
  "--name", "LoLReplayTool",
  "--clean",
  "--add-data", "config\\setting.sample.json;config",
  "--add-data", "config\\champion_aliases.json;config",
  "--add-data", "assets\\champions\\icons;assets\\champions\\icons"
)

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
pyinstaller @pyArgs
