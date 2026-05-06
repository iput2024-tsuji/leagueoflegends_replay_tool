param(
  [switch]$Lint,
  [switch]$Test,
  [switch]$Build,
  [switch]$All
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $repoRoot

function Invoke-Step {
  param(
    [string]$Name,
    [scriptblock]$Command
  )

  Write-Host ""
  Write-Host "==> $Name"
  & $Command
  $exitCode = if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE }
  if ($exitCode -ne 0) {
    throw "$Name failed with exit code $exitCode."
  }
}

function Get-PythonCommand {
  $venvPython = Join-Path $repoRoot "venv\Scripts\python.exe"
  if (Test-Path $venvPython) {
    return $venvPython
  }
  return "python"
}

function Invoke-Lint {
  $python = Get-PythonCommand
  Invoke-Step "Ruff check" { & $python -m ruff check . }
  Invoke-Step "Ruff format check" { & $python -m ruff format --check . }
}

function Invoke-Tests {
  $python = Get-PythonCommand
  Invoke-Step "Pytest" { & $python -m pytest -p no:cacheprovider tests }
}

function Invoke-Build {
  Invoke-Step "Build" { & (Join-Path $repoRoot "scripts\build.ps1") }
}

if (-not ($Lint -or $Test -or $Build -or $All)) {
  Write-Host "Usage: .\scripts\dev.ps1 [-Lint] [-Test] [-Build] [-All]"
  exit 1
}

try {
  if ($All -or $Lint) {
    Invoke-Lint
  }
  if ($All -or $Test) {
    Invoke-Tests
  }
  if ($All -or $Build) {
    Invoke-Build
  }
  Write-Host ""
  Write-Host "All requested tasks completed successfully."
} catch {
  Write-Error $_
  exit 1
}
