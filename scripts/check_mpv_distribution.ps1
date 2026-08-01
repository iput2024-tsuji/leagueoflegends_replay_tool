param(
  [Parameter(Mandatory = $true)]
  [string]$DistributionRoot
)

$ErrorActionPreference = "Stop"

$resolvedRoot = (Resolve-Path -LiteralPath $DistributionRoot -ErrorAction Stop).Path
if (-not (Test-Path -LiteralPath $resolvedRoot -PathType Container)) {
  throw "配布成果物フォルダが見つかりません: $resolvedRoot"
}

$mpvDllPattern = '^(lib)?mpv-.*\.dll$'
$bundledMpvDlls = @(
  Get-ChildItem `
    -LiteralPath $resolvedRoot `
    -Recurse `
    -File `
    -Force `
    -ErrorAction Stop |
    Where-Object { $_.Name -match $mpvDllPattern } |
    Sort-Object -Property FullName
)

if ($bundledMpvDlls.Count -gt 0) {
  $dllList = ($bundledMpvDlls | ForEach-Object { $_.FullName }) -join [Environment]::NewLine
  throw (
    "利用者が用意するmpv DLLが成果物へ混入しています。ビルドを中止します。" +
    "`n対象ファイル:`n$dllList" +
    "`nmpv DLLは配布物へ含めず、利用者が%LOCALAPPDATA%\LoLReplayTool\binへ手動配置してください。"
  )
}
