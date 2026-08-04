param(
  [Parameter(Mandatory = $true)]
  [string]$PythonExe,
  [string]$Components = ".\compliance\components.json",
  [Parameter(Mandatory = $true)]
  [string]$DownloadDirectory,
  [Parameter(Mandatory = $true)]
  [string]$InstallDirectory,
  [Parameter(Mandatory = $true)]
  [string]$OutputProvenance,
  [string]$BuildProvenance = ""
)

$ErrorActionPreference = "Stop"
$expectedComponent = "inno-setup"
$expectedVersion = "6.7.3"
$expectedSignerSubject = "CN=Pyrsys B.V., O=Pyrsys B.V., S=Noord-Holland, C=NL"
$expectedSignerThumbprint = "E0AB19C8D38CBF9C44709925122A7A02F8C70CB7"

function Get-FullPath {
  param([Parameter(Mandatory = $true)][string]$Path)
  return [System.IO.Path]::GetFullPath($Path)
}

function Test-SamePath {
  param(
    [Parameter(Mandatory = $true)][string]$Left,
    [Parameter(Mandatory = $true)][string]$Right
  )
  return [string]::Equals(
    (Get-FullPath $Left).TrimEnd([System.IO.Path]::DirectorySeparatorChar),
    (Get-FullPath $Right).TrimEnd([System.IO.Path]::DirectorySeparatorChar),
    [System.StringComparison]::OrdinalIgnoreCase
  )
}

function Test-PathWithin {
  param(
    [Parameter(Mandatory = $true)][string]$Root,
    [Parameter(Mandatory = $true)][string]$Candidate
  )
  $normalizedRoot = (Get-FullPath $Root).TrimEnd(
    [System.IO.Path]::DirectorySeparatorChar
  )
  $rootPath = $normalizedRoot + [System.IO.Path]::DirectorySeparatorChar
  $candidatePath = Get-FullPath $Candidate
  return (
    [string]::Equals(
      $candidatePath.TrimEnd([System.IO.Path]::DirectorySeparatorChar),
      $normalizedRoot,
      [System.StringComparison]::OrdinalIgnoreCase
    ) -or
    $candidatePath.StartsWith(
      $rootPath,
      [System.StringComparison]::OrdinalIgnoreCase
    )
  )
}

function Assert-NotReparsePoint {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$Label
  )
  if (-not (Test-Path -LiteralPath $Path)) {
    return
  }
  $item = Get-Item -LiteralPath $Path -Force
  if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw "$Label must not be a reparse point: $Path"
  }
}

function Assert-LockedFile {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][long]$Size,
    [Parameter(Mandatory = $true)][string]$Sha256,
    [Parameter(Mandatory = $true)][string]$Label
  )
  Assert-NotReparsePoint -Path $Path -Label $Label
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    throw "$Label is missing: $Path"
  }
  $item = Get-Item -LiteralPath $Path -Force
  if ($item.Length -ne $Size) {
    throw "$Label size differs: $($item.Length) != $Size"
  }
  $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($actual -cne $Sha256) {
    throw "$Label SHA256 differs: $actual"
  }
}

function Get-VerifiedAuthenticode {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$Label
  )
  $signature = Get-AuthenticodeSignature -LiteralPath $Path
  $subject = if ($signature.SignerCertificate) {
    $signature.SignerCertificate.Subject
  } else {
    ""
  }
  $thumbprint = if ($signature.SignerCertificate) {
    $signature.SignerCertificate.Thumbprint.ToUpperInvariant()
  } else {
    ""
  }
  if (
    $signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
    $subject -cne $expectedSignerSubject -or
    $thumbprint -cne $expectedSignerThumbprint
  ) {
    throw "$Label Authenticode identity differs: status=$($signature.Status), subject=$subject, thumbprint=$thumbprint"
  }
  return [ordered]@{
    status = "Valid"
    subject = $subject
    thumbprint = $thumbprint
  }
}

$resolvedPython = (Resolve-Path -LiteralPath $PythonExe -ErrorAction Stop).Path
if (-not (Test-Path -LiteralPath $resolvedPython -PathType Leaf)) {
  throw "Python executable is missing: $PythonExe"
}
$resolvedComponents = (Resolve-Path -LiteralPath $Components -ErrorAction Stop).Path
$downloadRoot = Get-FullPath $DownloadDirectory
$installRoot = Get-FullPath $InstallDirectory
$outputPath = Get-FullPath $OutputProvenance
$resolvedBuildProvenance = $null
if (-not [string]::IsNullOrWhiteSpace($BuildProvenance)) {
  $resolvedBuildProvenance = (
    Resolve-Path -LiteralPath $BuildProvenance -ErrorAction Stop
  ).Path
  if (-not (Test-Path -LiteralPath $resolvedBuildProvenance -PathType Leaf)) {
    throw "Build provenance must be a regular file: $resolvedBuildProvenance"
  }
}
if (Test-Path -LiteralPath $outputPath) {
  throw "Inno Setup provenance output must not already exist: $outputPath"
}
$writablePaths = [ordered]@{
  "output provenance" = $outputPath
}
if ($resolvedBuildProvenance) {
  $writablePaths["build provenance"] = $resolvedBuildProvenance
}
foreach ($entry in $writablePaths.GetEnumerator()) {
  if (
    (Test-SamePath -Left $entry.Value -Right $resolvedComponents) -or
    (Test-PathWithin -Root $downloadRoot -Candidate $entry.Value) -or
    (Test-PathWithin -Root $installRoot -Candidate $entry.Value)
  ) {
    throw "Unsafe $($entry.Key) path: $($entry.Value)"
  }
}
if (
  $resolvedBuildProvenance -and
  (Test-SamePath -Left $outputPath -Right $resolvedBuildProvenance)
) {
  throw "Output provenance and build provenance must use distinct paths."
}
Assert-NotReparsePoint -Path $downloadRoot -Label "Inno Setup download directory"
if (Test-Path -LiteralPath $downloadRoot) {
  $existingDownloads = @(Get-ChildItem -LiteralPath $downloadRoot -Force)
  if ($existingDownloads.Count -ne 0) {
    throw "Inno Setup download directory must start empty: $downloadRoot"
  }
} else {
  New-Item -ItemType Directory -Path $downloadRoot | Out-Null
}
if (Test-Path -LiteralPath $installRoot) {
  throw "Inno Setup install directory must not already exist: $installRoot"
}

try {
  $lock = Get-Content -LiteralPath $resolvedComponents -Raw | ConvertFrom-Json -Depth 100
} catch {
  throw "Cannot read component lock: $($_.Exception.Message)"
}
$matches = @($lock.installer_components | Where-Object { $_.component -ceq $expectedComponent })
if ($matches.Count -ne 1) {
  throw "Component lock must contain exactly one inno-setup entry."
}
$component = $matches[0]
if ($component.version -cne $expectedVersion) {
  throw "Pinned Inno Setup version differs: $($component.version)"
}

& $resolvedPython -m scripts.inno_setup_provenance `
  validate-lock `
  --components $resolvedComponents
if ($LASTEXITCODE -ne 0) {
  exit $LASTEXITCODE
}

$installerRecord = $component.official_installer
$installerPath = Join-Path $downloadRoot $installerRecord.filename
Invoke-WebRequest -Uri $installerRecord.url -OutFile $installerPath
Assert-LockedFile `
  -Path $installerPath `
  -Size $installerRecord.size `
  -Sha256 $installerRecord.sha256 `
  -Label "Inno Setup official installer"
$installerSignature = Get-VerifiedAuthenticode `
  -Path $installerPath `
  -Label "Inno Setup official installer"

$ghCommand = Get-Command gh -ErrorAction SilentlyContinue
if (-not $ghCommand) {
  throw "GitHub CLI is required to verify the Inno Setup Release attestation."
}
$hadGhToken = Test-Path Env:GH_TOKEN
$originalGhToken = if ($hadGhToken) { $env:GH_TOKEN } else { $null }
try {
  & $ghCommand.Source release verify-asset `
    $component.official_installer.release_tag `
    $installerPath `
    --repo $component.official_installer.release_repository
  $ghExitCode = $LASTEXITCODE
  [Environment]::SetEnvironmentVariable("GH_TOKEN", $null, "Process")
  if ($ghExitCode -ne 0) {
    throw "Inno Setup GitHub Release attestation verification failed."
  }

$publicKeyPaths = @{}
foreach ($key in $component.public_keys) {
  $keyPath = Join-Path $downloadRoot $key.filename
  Invoke-WebRequest -Uri $key.url -OutFile $keyPath
  Assert-LockedFile `
    -Path $keyPath `
    -Size $key.size `
    -Sha256 $key.sha256 `
    -Label "Inno Setup public key $($key.filename)"
  $keyText = Get-Content -LiteralPath $keyPath -Raw
  if ($keyText -notmatch "(?m)^key-id $([regex]::Escape($key.key_id))\r?$" ) {
    throw "Inno Setup public key identity differs: $($key.filename)"
  }
  $publicKeyPaths[$key.filename] = $keyPath
}

$installerArguments = @(
  "/VERYSILENT",
  "/SUPPRESSMSGBOXES",
  "/NORESTART",
  "/SP-",
  "/PORTABLE=1",
  "/NOICONS",
  "/DIR=`"$installRoot`""
)
$installProcess = Start-Process `
  -FilePath $installerPath `
  -ArgumentList $installerArguments `
  -Wait `
  -PassThru `
  -WindowStyle Hidden
if ($installProcess.ExitCode -ne 0) {
  throw "Inno Setup portable installation failed: $($installProcess.ExitCode)"
}
Assert-NotReparsePoint -Path $installRoot -Label "Inno Setup install directory"
if (-not (Test-Path -LiteralPath $installRoot -PathType Container)) {
  throw "Inno Setup portable installation did not create its output directory."
}

$authenticode = [ordered]@{
  official_installer = $installerSignature
}
foreach ($file in $component.toolchain_files) {
  $relative = [string]$file.path
  $segments = $relative -split "/"
  if (-not $relative -or $segments -contains ".." -or [System.IO.Path]::IsPathRooted($relative)) {
    throw "Unsafe Inno Setup toolchain path: $relative"
  }
  $path = $installRoot
  foreach ($segment in $segments) {
    $path = Join-Path $path $segment
  }
  Assert-LockedFile `
    -Path $path `
    -Size $file.size `
    -Sha256 $file.sha256 `
    -Label "Inno Setup toolchain file $relative"
  if ($file.authenticode -eq $true) {
    $authenticode[$relative] = Get-VerifiedAuthenticode `
      -Path $path `
      -Label "Inno Setup toolchain file $relative"
  }
}

$signatureTool = Join-Path $installRoot "ISSigTool.exe"
$issig = [ordered]@{}
foreach ($file in $component.toolchain_files) {
  if (-not $file.issig_key) {
    continue
  }
  $relative = [string]$file.path
  $path = $installRoot
  foreach ($segment in ($relative -split "/")) {
    $path = Join-Path $path $segment
  }
  $keyPath = $publicKeyPaths[[string]$file.issig_key]
  if (-not $keyPath) {
    throw "Inno Setup toolchain file references an unknown ISSig key: $relative"
  }
  & $signatureTool "--key-file=$keyPath" verify $path
  if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup ISSig verification failed: $relative"
  }
  $issig[$relative] = [ordered]@{
    key = [string]$file.issig_key
    verified = $true
  }
}

$signatureReportPath = Join-Path $downloadRoot "signature-report.json"
$signatureReport = [ordered]@{
  schema_version = 1
  release_attestation = [ordered]@{
    repository = [string]$component.official_installer.release_repository
    tag = [string]$component.official_installer.release_tag
    asset = [string]$component.official_installer.filename
    verified = $true
  }
  authenticode = $authenticode
  issig = $issig
}
$signatureReport | ConvertTo-Json -Depth 20 | Set-Content `
  -LiteralPath $signatureReportPath `
  -Encoding utf8NoBOM

$attestArguments = @(
  "-m", "scripts.inno_setup_provenance", "attest",
  "--components", $resolvedComponents,
  "--installer", $installerPath,
  "--install-root", $installRoot,
  "--signature-report", $signatureReportPath,
  "--output-provenance", $outputPath
)
if ($resolvedBuildProvenance) {
  $attestArguments += @("--build-provenance", $resolvedBuildProvenance)
}
& $resolvedPython @attestArguments
if ($LASTEXITCODE -ne 0) {
  throw "Inno Setup provenance attestation failed: $LASTEXITCODE"
}

Write-Host "Verified Inno Setup prepared: $installRoot"
Write-Host "Inno Setup provenance created: $outputPath"
} finally {
  if ($hadGhToken) {
    [Environment]::SetEnvironmentVariable("GH_TOKEN", $originalGhToken, "Process")
  } else {
    [Environment]::SetEnvironmentVariable("GH_TOKEN", $null, "Process")
  }
}
