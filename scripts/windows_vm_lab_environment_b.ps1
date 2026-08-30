Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-RuntimeRegistryState {
  param([Microsoft.Win32.RegistryView]$View)

  $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
    [Microsoft.Win32.RegistryHive]::LocalMachine,
    $View
  )
  try {
    $key = $base.OpenSubKey("SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64")
    if ($null -eq $key) {
      return [pscustomobject][ordered]@{
        View = $View.ToString()
        Present = $false
        Installed = $null
        Version = $null
      }
    }
    try {
      return [pscustomobject][ordered]@{
        View = $View.ToString()
        Present = $true
        Installed = $key.GetValue("Installed", $null)
        Version = $key.GetValue("Version", $null)
      }
    } finally {
      $key.Dispose()
    }
  } finally {
    $base.Dispose()
  }
}

$minimumVersion = [version]"14.44.35211.0"
$appRoot = Join-Path $PSScriptRoot "LoLReplayTool-external-build"
$appExe = Join-Path $appRoot "LoLReplayTool.exe"
$manifestPath = Join-Path $PSScriptRoot "evidence\package-sha256.csv"
$runner = Join-Path $PSScriptRoot "run_packaged_self_check.ps1"
$resultRoot = Join-Path $env:USERPROFILE "Desktop\VC-Runtime-Test-Results"
$selfCheckTemp = Join-Path $resultRoot "self-check-temp"
New-Item -ItemType Directory -Path $resultRoot -Force | Out-Null
New-Item -ItemType Directory -Path $selfCheckTemp -Force | Out-Null

$registry = @(
  Get-RuntimeRegistryState -View ([Microsoft.Win32.RegistryView]::Registry64)
  Get-RuntimeRegistryState -View ([Microsoft.Win32.RegistryView]::Registry32)
)
$compatibleRegistry = @(
  foreach ($state in $registry) {
    if ($state.Present -and $state.Installed -eq 1 -and $null -ne $state.Version) {
      $parsed = [version]($state.Version.ToString().TrimStart("v"))
      if ($parsed -ge $minimumVersion) {
        $state
      }
    }
  }
)
if ($compatibleRegistry.Count -eq 0) {
  throw "No installed x64 Visual C++ Runtime >= $minimumVersion was found in either registry view."
}

$requiredDllNames = @(
  "concrt140.dll",
  "msvcp140.dll",
  "msvcp140_1.dll",
  "msvcp140_2.dll",
  "vcomp140.dll",
  "vcruntime140.dll",
  "vcruntime140_1.dll"
)
$systemDlls = @(
  foreach ($name in $requiredDllNames) {
    $path = Join-Path $env:WINDIR "System32\$name"
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
      throw "Required external Runtime DLL is missing: $path"
    }
    $file = Get-Item -LiteralPath $path
    [pscustomobject][ordered]@{
      Name = $name
      Path = $path
      Size = $file.Length
      FileVersion = $file.VersionInfo.FileVersion
      SHA256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
    }
  }
)

$appLocalRuntime = @(
  Get-ChildItem -LiteralPath $appRoot -Recurse -File | Where-Object {
    $_.Name -match "^(?i:msvcp|vcruntime|vcomp|concrt).*\.dll$"
  }
)
if ($appLocalRuntime.Count -ne 0) {
  throw "App-local Runtime DLLs found: $($appLocalRuntime.FullName -join ', ')"
}

$manifest = @(Import-Csv -LiteralPath $manifestPath)
$actualFiles = @(Get-ChildItem -LiteralPath $appRoot -Recurse -File)
if ($manifest.Count -ne $actualFiles.Count) {
  throw "Package file count mismatch: manifest=$($manifest.Count) actual=$($actualFiles.Count)"
}
$resolvedAppRoot = (Resolve-Path -LiteralPath $appRoot).Path.TrimEnd("\")

function Resolve-SafeManifestPath {
  param(
    [Parameter(Mandatory = $true)][string]$Root,
    [Parameter(Mandatory = $true)][string]$RelativePath
  )
  if ([string]::IsNullOrWhiteSpace($RelativePath)) {
    throw "Unsafe manifest path: $RelativePath"
  }
  foreach ($character in $RelativePath.ToCharArray()) {
    if ([char]::IsControl($character)) {
      throw "Unsafe manifest path: $RelativePath"
    }
  }
  $normalized = $RelativePath.Replace("\", "/")
  if (
    $normalized.StartsWith("/", [StringComparison]::Ordinal) -or
    $normalized.Contains(":")
  ) {
    throw "Unsafe manifest path: $RelativePath"
  }
  $parts = $normalized -split "/", -1
  if (@($parts | Where-Object { $_ -eq "" -or $_ -eq "." -or $_ -eq ".." }).Count -ne 0) {
    throw "Unsafe manifest path: $RelativePath"
  }
  $rootPath = (Resolve-Path -LiteralPath $Root -ErrorAction Stop).Path.TrimEnd("\")
  $candidate = Join-Path $rootPath ($normalized.Replace("/", "\"))
  $resolved = (Resolve-Path -LiteralPath $candidate -ErrorAction Stop).Path
  if (-not $resolved.StartsWith($rootPath + "\", [StringComparison]::OrdinalIgnoreCase)) {
    throw "Manifest path escapes package root: $RelativePath"
  }
  return $resolved
}

foreach ($entry in $manifest) {
  $resolvedCandidate = Resolve-SafeManifestPath -Root $resolvedAppRoot -RelativePath ([string]$entry.Path)
  $file = Get-Item -LiteralPath $resolvedCandidate
  if ($file.Length -ne [long]$entry.Size) {
    throw "Package size mismatch: $($entry.Path)"
  }
  $hash = (Get-FileHash -LiteralPath $resolvedCandidate -Algorithm SHA256).Hash
  if ($hash -ne $entry.SHA256) {
    throw "Package SHA-256 mismatch: $($entry.Path)"
  }
}

$selfCheckOutput = Join-Path $resultRoot "packaged-self-check.txt"
try {
  & $runner -AppExe $appExe -TempRoot $selfCheckTemp *>&1 |
    Tee-Object -FilePath $selfCheckOutput
} catch {
  $_ | Out-String | Add-Content -LiteralPath $selfCheckOutput -Encoding UTF8
  throw
}

$os = Get-CimInstance Win32_OperatingSystem
$result = [pscustomobject][ordered]@{
  Schema = "vc-runtime-environment-b/v1"
  Timestamp = (Get-Date).ToString("o")
  ComputerName = $env:COMPUTERNAME
  WindowsCaption = $os.Caption
  WindowsVersion = $os.Version
  WindowsBuild = $os.BuildNumber
  MinimumRuntimeVersion = $minimumVersion.ToString()
  Registry = $registry
  SystemDlls = $systemDlls
  PackageFiles = $actualFiles.Count
  PackageManifestSHA256 = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash
  PeAuditSHA256 = (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot "evidence\pe-runtime-audit.json") -Algorithm SHA256).Hash
  WheelProvenanceSHA256 = (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot "evidence\external-vc-runtime-wheel-provenance.json") -Algorithm SHA256).Hash
  SelfCheckOutput = $selfCheckOutput
  SelfCheckOutputSHA256 = (Get-FileHash -LiteralPath $selfCheckOutput -Algorithm SHA256).Hash
  Passed = $true
}
$resultPath = Join-Path $resultRoot "environment-b-result.json"
$result | ConvertTo-Json -Depth 7 | Set-Content -LiteralPath $resultPath -Encoding UTF8
$result | ConvertTo-Json -Depth 7
Write-Host "Result: $resultPath"
Write-Host "Environment B native module and packaged self-check passed."
