$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location (Join-Path $scriptDir "..")

pyinstaller `
  --noconsole `
  --onedir `
  --name "LoLReplayTool" `
  --clean `
  --add-data "config\\setting.sample.json;config" `
  --add-data "config\\champion_aliases.json;config" `
  --add-data "assets\\champions\\icons;assets\\champions\\icons" `
  --add-binary "bin\\*.dll;bin" `
  main.py
