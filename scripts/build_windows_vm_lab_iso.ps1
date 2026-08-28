[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$SourceKitPath,
  [Parameter(Mandatory = $true)]
  [string]$OutputPath,
  [ValidatePattern('^[0-9a-f]{40}$')]
  [string]$PayloadCommit = "1d5f79209646edda33911470ed132a9d5f4d440c"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Import-Module Microsoft.PowerShell.Utility -ErrorAction Stop

$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$bootstrapCmd = Join-Path $scriptDirectory "windows_vm_lab_bootstrap.cmd"
$bootstrapScript = Join-Path $scriptDirectory "windows_vm_lab_bootstrap.ps1"
$selfCheckRunner = Join-Path $scriptDirectory "run_packaged_self_check.ps1"
$requiredPaths = @(
  "vc_redist.x64.exe",
  "LoLReplayTool-external-build\LoLReplayTool.exe",
  "02-test-environment-b.ps1",
  "run_packaged_self_check.ps1",
  "evidence\package-sha256.csv",
  "evidence\pe-runtime-audit.json",
  "evidence\external-vc-runtime-wheel-provenance.json"
)

function Get-Sha256 {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Path
  )

  $stream = [IO.File]::Open(
    [IO.Path]::GetFullPath($Path),
    [IO.FileMode]::Open,
    [IO.FileAccess]::Read,
    [IO.FileShare]::Read
  )
  try {
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
      return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace(
        "-",
        ""
      ).ToLowerInvariant()
    } finally {
      $algorithm.Dispose()
    }
  } finally {
    $stream.Dispose()
  }
}

function Test-PathWithin {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Path,
    [Parameter(Mandatory = $true)]
    [string]$Root
  )

  $fullPath = [IO.Path]::GetFullPath($Path)
  $fullRoot = [IO.Path]::GetFullPath($Root).TrimEnd(
    [IO.Path]::DirectorySeparatorChar,
    [IO.Path]::AltDirectorySeparatorChar
  ) + [IO.Path]::DirectorySeparatorChar
  return $fullPath.StartsWith($fullRoot, [StringComparison]::OrdinalIgnoreCase)
}

function Assert-RegularTree {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Root
  )

  foreach ($item in @(
    Get-Item -LiteralPath $Root -Force
    Get-ChildItem -LiteralPath $Root -Recurse -Force
  )) {
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
      throw "Source kit contains a reparse point: $($item.FullName)"
    }
  }
}

Add-Type -TypeDefinition @"
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

public static class VmLabImapiStreamWriter
{
    public static void CopyToFile(object source, string path)
    {
        IStream stream = (IStream)source;
        byte[] buffer = new byte[65536];
        IntPtr bytesReadPointer = Marshal.AllocCoTaskMem(sizeof(int));
        try
        {
            using (FileStream output = new FileStream(
                path, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            {
                while (true)
                {
                    stream.Read(buffer, buffer.Length, bytesReadPointer);
                    int bytesRead = Marshal.ReadInt32(bytesReadPointer);
                    if (bytesRead == 0)
                    {
                        break;
                    }
                    output.Write(buffer, 0, bytesRead);
                }
            }
        }
        finally
        {
            Marshal.FreeCoTaskMem(bytesReadPointer);
        }
    }
}
"@

$source = (Resolve-Path -LiteralPath $SourceKitPath -ErrorAction Stop).Path
if (-not (Test-Path -LiteralPath $source -PathType Container)) {
  throw "Source kit directory does not exist: $SourceKitPath"
}
foreach ($required in $requiredPaths) {
  $candidate = Join-Path $source $required
  if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
    throw "Required source kit file does not exist: $required"
  }
}
foreach ($tracked in @($bootstrapCmd, $bootstrapScript, $selfCheckRunner)) {
  if (-not (Test-Path -LiteralPath $tracked -PathType Leaf)) {
    throw "Tracked VM lab file does not exist: $tracked"
  }
}
Assert-RegularTree -Root $source

$resolvedOutput = [IO.Path]::GetFullPath($OutputPath)
$outputDirectory = Split-Path -Parent $resolvedOutput
if (-not (Test-Path -LiteralPath $outputDirectory -PathType Container)) {
  throw "ISO output directory does not exist: $outputDirectory"
}
if (Test-Path -LiteralPath $resolvedOutput) {
  throw "ISO output already exists: $resolvedOutput"
}

$staging = Join-Path ([IO.Path]::GetTempPath()) (
  "lol-vm-lab-iso-" + [guid]::NewGuid().ToString("N")
)
$partialOutput = "$resolvedOutput.partial-$([guid]::NewGuid().ToString('N'))"
$image = $null
$result = $null
$rawStream = $null
$completed = $false
try {
  New-Item -ItemType Directory -Path $staging -ErrorAction Stop | Out-Null
  Get-ChildItem -LiteralPath $source -Force |
    Copy-Item -Destination $staging -Recurse -Force
  foreach ($localOnly in @("create-test-iso.ps1")) {
    $candidate = Join-Path $staging $localOnly
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
      Remove-Item -LiteralPath $candidate -Force
    }
  }
  Copy-Item -LiteralPath $bootstrapCmd -Destination (
    Join-Path $staging "00-Bootstrap-VM-Lab.cmd"
  ) -Force
  Copy-Item -LiteralPath $bootstrapScript -Destination (
    Join-Path $staging "windows_vm_lab_bootstrap.ps1"
  ) -Force
  Copy-Item -LiteralPath $selfCheckRunner -Destination (
    Join-Path $staging "run_packaged_self_check.ps1"
  ) -Force

  # Windows PowerShell 5.1 treats a BOM-less script as the active ANSI code
  # page. The clean guest uses powershell.exe, so normalize every directly
  # executed payload script to deterministic UTF-8 with BOM.
  $utf8WithBom = [Text.UTF8Encoding]::new($true)
  foreach ($relativePath in @(
    "02-test-environment-b.ps1",
    "run_packaged_self_check.ps1",
    "windows_vm_lab_bootstrap.ps1"
  )) {
    $scriptPath = Join-Path $staging $relativePath
    # A source kit extracted from read-only media can retain the ReadOnly bit.
    # Only the disposable staging copy is made writable for normalization.
    $scriptItem = Get-Item -LiteralPath $scriptPath -ErrorAction Stop
    if ($scriptItem.IsReadOnly) {
      $scriptItem.IsReadOnly = $false
    }
    $scriptText = [IO.File]::ReadAllText($scriptPath, [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText($scriptPath, $scriptText, $utf8WithBom)
  }

  $manifestFiles = @(
    foreach ($relativePath in @(
      $requiredPaths +
      @("00-Bootstrap-VM-Lab.cmd", "windows_vm_lab_bootstrap.ps1")
    )) {
      $file = Get-Item -LiteralPath (Join-Path $staging $relativePath)
      [ordered]@{
        path = $relativePath.Replace("\", "/")
        size = $file.Length
        sha256 = Get-Sha256 -Path $file.FullName
      }
    }
  )
  $mediaManifest = [ordered]@{
    schema = "lol-vm-lab-media/v1"
    volume_label = "LOL_VC_PR134"
    payload_commit = $PayloadCommit
    files = $manifestFiles
  }
  $mediaManifestPath = Join-Path $staging "vm-lab-media-manifest.json"
  if (Test-Path -LiteralPath $mediaManifestPath -PathType Leaf) {
    $oldManifest = Get-Item -LiteralPath $mediaManifestPath -ErrorAction Stop
    if ($oldManifest.IsReadOnly) {
      $oldManifest.IsReadOnly = $false
    }
    Remove-Item -LiteralPath $mediaManifestPath -Force -ErrorAction Stop
  }
  $mediaManifest |
    ConvertTo-Json -Depth 6 |
    Set-Content `
      -LiteralPath $mediaManifestPath `
      -Encoding utf8

  $image = New-Object -ComObject IMAPI2FS.MsftFileSystemImage
  $image.FileSystemsToCreate = 4
  $image.VolumeName = "LOL_VC_PR134"
  $image.Root.AddTree($staging, $false)
  $result = $image.CreateResultImage()
  $rawStream = $result.ImageStream
  [VmLabImapiStreamWriter]::CopyToFile($rawStream, $partialOutput)
  Move-Item -LiteralPath $partialOutput -Destination $resolvedOutput
  $completed = $true
} finally {
  foreach ($comObject in @($rawStream, $result, $image)) {
    if ($null -ne $comObject) {
      [Runtime.InteropServices.Marshal]::ReleaseComObject($comObject) | Out-Null
    }
  }
  [GC]::Collect()
  [GC]::WaitForPendingFinalizers()
  if (-not $completed -and (Test-Path -LiteralPath $partialOutput)) {
    Remove-Item -LiteralPath $partialOutput -Force
  }
  $tempRoot = [IO.Path]::GetTempPath()
  if (
    (Test-Path -LiteralPath $staging -PathType Container) -and
    (Test-PathWithin -Path $staging -Root $tempRoot)
  ) {
    try {
      Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction Stop
    } catch {
      Write-Warning "Temporary ISO staging could not be removed: $staging"
    }
  }
}

$iso = Get-Item -LiteralPath $resolvedOutput
[ordered]@{
  path = $iso.FullName
  size = $iso.Length
  sha256 = Get-Sha256 -Path $iso.FullName
  volume_label = "LOL_VC_PR134"
  payload_commit = $PayloadCommit
} | ConvertTo-Json -Compress
