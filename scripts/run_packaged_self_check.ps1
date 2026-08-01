param(
  [Parameter(Mandatory = $true)]
  [string]$AppExe,
  [string]$TempRoot = [IO.Path]::GetTempPath(),
  [ValidateRange(1, 3600)]
  [int]$TimeoutSeconds = 60,
  [string[]]$SelfCheckArguments = @("--self-check"),
  [ValidateNotNullOrEmpty()]
  [string]$TaskkillExe = "taskkill.exe",
  [ValidateRange(1, 60)]
  [int]$TaskkillTimeoutSeconds = 10,
  [string[]]$TaskkillPrefixArguments = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$resolvedAppExe = (Resolve-Path -LiteralPath $AppExe -ErrorAction Stop).Path
if (-not (Test-Path -LiteralPath $resolvedAppExe -PathType Leaf)) {
  throw "packaged self-check の実行ファイルが見つかりません: $AppExe"
}
$resolvedTempRoot = (Resolve-Path -LiteralPath $TempRoot -ErrorAction Stop).Path
if (-not (Test-Path -LiteralPath $resolvedTempRoot -PathType Container)) {
  throw "packaged self-check の一時領域が見つかりません: $TempRoot"
}
if ($SelfCheckArguments.Count -eq 0) {
  throw "packaged self-check の引数が指定されていません。"
}

function Resolve-TaskkillExecutable {
  param(
    [Parameter(Mandatory = $true)]
    [string]$FilePath
  )

  if (Test-Path -LiteralPath $FilePath -PathType Leaf) {
    return (Resolve-Path -LiteralPath $FilePath -ErrorAction Stop).Path
  }
  $command = Get-Command $FilePath -CommandType Application -ErrorAction SilentlyContinue |
    Select-Object -First 1
  if ($null -eq $command) {
    throw "taskkill実行ファイルが見つかりません: $FilePath"
  }
  return $command.Source
}

function Stop-ProcessBestEffort {
  param(
    [Parameter(Mandatory = $true)]
    [System.Diagnostics.Process]$Process
  )

  $details = [System.Collections.Generic.List[string]]::new()
  try {
    if ($Process.HasExited) {
      $details.Add("target process already exited")
      return ($details -join "; ")
    }
  } catch {
    $details.Add("target state unavailable: $($_.Exception.Message)")
  }

  try {
    $Process.Kill($true)
    $details.Add("Kill(Boolean) requested")
  } catch [System.Management.Automation.MethodException] {
    try {
      if (-not $Process.HasExited) {
        $Process.Kill()
        $details.Add("Kill() requested because Kill(Boolean) is unavailable")
      }
    } catch {
      $details.Add("Kill() failed: $($_.Exception.Message)")
    }
  } catch {
    $details.Add("Kill(Boolean) failed: $($_.Exception.Message)")
    try {
      if (-not $Process.HasExited) {
        $Process.Kill()
        $details.Add("Kill() fallback requested")
      }
    } catch {
      $details.Add("Kill() fallback failed: $($_.Exception.Message)")
    }
  }

  try {
    if ($Process.HasExited -or $Process.WaitForExit(10000)) {
      $details.Add("target process exited")
    } else {
      $details.Add("target process still running after 10 seconds")
    }
  } catch {
    $details.Add("target exit wait failed: $($_.Exception.Message)")
  }
  return ($details -join "; ")
}

function Stop-SelfCheckProcessTree {
  param(
    [Parameter(Mandatory = $true)]
    [System.Diagnostics.Process]$Process,
    [Parameter(Mandatory = $true)]
    [string]$DiagnosticDirectory,
    [Parameter(Mandatory = $true)]
    [string]$TaskkillFilePath,
    [Parameter(Mandatory = $true)]
    [int]$TaskkillTimeout,
    [string[]]$TaskkillArgumentPrefix = @()
  )

  $taskkillStdout = Join-Path $DiagnosticDirectory "taskkill-stdout.txt"
  $taskkillStderr = Join-Path $DiagnosticDirectory "taskkill-stderr.txt"
  $taskkillProcess = $null
  $taskkillExitCode = $null
  $taskkillTimedOut = $false
  $taskkillExecutionError = $null
  $taskkillStdoutText = ""
  $taskkillStderrText = ""

  try {
    $resolvedTaskkill = Resolve-TaskkillExecutable -FilePath $TaskkillFilePath
    $taskkillArguments = @($TaskkillArgumentPrefix) + @(
      "/PID",
      $Process.Id.ToString([Globalization.CultureInfo]::InvariantCulture),
      "/T",
      "/F"
    )
    $taskkillProcess = Start-Process `
      -FilePath $resolvedTaskkill `
      -ArgumentList $taskkillArguments `
      -WindowStyle Hidden `
      -RedirectStandardOutput $taskkillStdout `
      -RedirectStandardError $taskkillStderr `
      -PassThru
    if (-not $taskkillProcess.WaitForExit($TaskkillTimeout * 1000)) {
      $taskkillTimedOut = $true
      try {
        $taskkillProcess.Kill()
      } catch {
        $taskkillExecutionError = "timeout後のtaskkill停止に失敗しました: $($_.Exception.Message)"
      }
      try {
        if (-not $taskkillProcess.WaitForExit(5000)) {
          $message = "timeout後もtaskkillが5秒以内に終了しませんでした。"
          if ($null -eq $taskkillExecutionError) {
            $taskkillExecutionError = $message
          } else {
            $taskkillExecutionError = "$taskkillExecutionError $message"
          }
        }
      } catch {
        $message = "taskkillの終了待機に失敗しました: $($_.Exception.Message)"
        if ($null -eq $taskkillExecutionError) {
          $taskkillExecutionError = $message
        } else {
          $taskkillExecutionError = "$taskkillExecutionError $message"
        }
      }
    }
    if ($taskkillProcess.HasExited) {
      $taskkillProcess.Refresh()
      $taskkillExitCode = $taskkillProcess.ExitCode
    }
  } catch {
    $taskkillExecutionError = $_.Exception.Message
  } finally {
    try {
      if (Test-Path -LiteralPath $taskkillStdout -PathType Leaf) {
        $taskkillStdoutText = Get-Content -LiteralPath $taskkillStdout -Raw
      }
      if (Test-Path -LiteralPath $taskkillStderr -PathType Leaf) {
        $taskkillStderrText = Get-Content -LiteralPath $taskkillStderr -Raw
      }
    } catch {
      $message = "taskkill diagnosticsの読み取りに失敗しました: $($_.Exception.Message)"
      if ($null -eq $taskkillExecutionError) {
        $taskkillExecutionError = $message
      } else {
        $taskkillExecutionError = "$taskkillExecutionError $message"
      }
    }
    if ($null -ne $taskkillProcess) {
      try {
        $taskkillProcess.Dispose()
      } catch {
        $message = "taskkill process handleの解放に失敗しました: $($_.Exception.Message)"
        if ($null -eq $taskkillExecutionError) {
          $taskkillExecutionError = $message
        } else {
          $taskkillExecutionError = "$taskkillExecutionError $message"
        }
      }
    }
  }

  if (
    -not $taskkillTimedOut -and
    $null -eq $taskkillExecutionError -and
    $taskkillExitCode -eq 0
  ) {
    try {
      if ($Process.HasExited -or $Process.WaitForExit(10000)) {
        return
      }
      $taskkillExecutionError = "taskkill成功後もtarget processが10秒以内に終了しませんでした。"
    } catch {
      $taskkillExecutionError = "target processの終了確認に失敗しました: $($_.Exception.Message)"
    }
  }

  $fallbackDetail = Stop-ProcessBestEffort -Process $Process
  $taskkillExitText = if ($null -eq $taskkillExitCode) {
    "unavailable"
  } else {
    $taskkillExitCode.ToString([Globalization.CultureInfo]::InvariantCulture)
  }
  $taskkillStdoutDetail = if ([string]::IsNullOrWhiteSpace($taskkillStdoutText)) {
    "<empty>"
  } else {
    $taskkillStdoutText.Trim()
  }
  $taskkillStderrDetail = if ([string]::IsNullOrWhiteSpace($taskkillStderrText)) {
    "<empty>"
  } else {
    $taskkillStderrText.Trim()
  }
  $taskkillErrorDetail = if ($null -eq $taskkillExecutionError) {
    "<none>"
  } else {
    $taskkillExecutionError
  }
  throw (
    "taskkillによるprocess tree停止を確認できませんでした。" +
    " taskkill timedOut=$taskkillTimedOut exit=$taskkillExitText" +
    " error=$taskkillErrorDetail stdout=$taskkillStdoutDetail" +
    " stderr=$taskkillStderrDetail; best-effort fallback: $fallbackDetail"
  )
}

$selfCheckDir = Join-Path $resolvedTempRoot (
  "lol-replay-tool-self-check-" + [guid]::NewGuid().ToString("N")
)
$selfCheckStdout = Join-Path $selfCheckDir "stdout.txt"
$selfCheckStderr = Join-Path $selfCheckDir "stderr.txt"
$hadPreviousDataDir = Test-Path Env:LOL_REPLAY_TOOL_DATA_DIR
$previousDataDir = $env:LOL_REPLAY_TOOL_DATA_DIR
$selfCheckProcess = $null
$selfCheckExitCode = $null
$selfCheckStdoutText = ""
$selfCheckStderrText = ""
$selfCheckTimedOut = $false
$selfCheckExecutionError = $null
$selfCheckDirRemoved = $false
$selfCheckProcessDisposed = $false
$cleanupErrors = [System.Collections.Generic.List[string]]::new()

Write-Host "packaged self-check data directory: $selfCheckDir"
try {
  New-Item -ItemType Directory -Path $selfCheckDir -ErrorAction Stop | Out-Null
  $env:LOL_REPLAY_TOOL_DATA_DIR = $selfCheckDir
  try {
    $selfCheckProcess = Start-Process `
      -FilePath $resolvedAppExe `
      -ArgumentList $SelfCheckArguments `
      -WindowStyle Hidden `
      -RedirectStandardOutput $selfCheckStdout `
      -RedirectStandardError $selfCheckStderr `
      -PassThru
    if (-not $selfCheckProcess.WaitForExit($TimeoutSeconds * 1000)) {
      $selfCheckTimedOut = $true
      try {
        Stop-SelfCheckProcessTree `
          -Process $selfCheckProcess `
          -DiagnosticDirectory $selfCheckDir `
          -TaskkillFilePath $TaskkillExe `
          -TaskkillTimeout $TaskkillTimeoutSeconds `
          -TaskkillArgumentPrefix $TaskkillPrefixArguments
      } catch {
        $selfCheckExecutionError = "timeout後のprocess tree停止に失敗しました: $($_.Exception.Message)"
      }
    }
  } catch {
    $selfCheckExecutionError = $_.Exception.Message
  }
} finally {
  if ($null -ne $selfCheckProcess) {
    try {
      if (-not $selfCheckProcess.HasExited) {
        Stop-SelfCheckProcessTree `
          -Process $selfCheckProcess `
          -DiagnosticDirectory $selfCheckDir `
          -TaskkillFilePath $TaskkillExe `
          -TaskkillTimeout $TaskkillTimeoutSeconds `
          -TaskkillArgumentPrefix $TaskkillPrefixArguments
      }
      $selfCheckProcess.Refresh()
      $selfCheckExitCode = $selfCheckProcess.ExitCode
    } catch {
      $cleanupErrors.Add("process cleanup: $($_.Exception.Message)")
    }
  }

  try {
    if (Test-Path -LiteralPath $selfCheckStdout -PathType Leaf) {
      $selfCheckStdoutText = Get-Content -LiteralPath $selfCheckStdout -Raw -Encoding utf8
    }
    if (Test-Path -LiteralPath $selfCheckStderr -PathType Leaf) {
      $selfCheckStderrText = Get-Content -LiteralPath $selfCheckStderr -Raw -Encoding utf8
    }
  } catch {
    $cleanupErrors.Add("diagnostic capture: $($_.Exception.Message)")
  }

  try {
    if ($hadPreviousDataDir) {
      $env:LOL_REPLAY_TOOL_DATA_DIR = $previousDataDir
    } else {
      Remove-Item Env:LOL_REPLAY_TOOL_DATA_DIR -ErrorAction SilentlyContinue
    }
  } catch {
    $cleanupErrors.Add("environment restore: $($_.Exception.Message)")
  }

  try {
    if (Test-Path -LiteralPath $selfCheckDir) {
      Remove-Item -LiteralPath $selfCheckDir -Recurse -Force -ErrorAction Stop
    }
    if (Test-Path -LiteralPath $selfCheckDir) {
      throw "一時領域が残っています: $selfCheckDir"
    }
    $selfCheckDirRemoved = $true
  } catch {
    $cleanupErrors.Add("temporary directory cleanup: $($_.Exception.Message)")
  }

  if ($null -ne $selfCheckProcess) {
    try {
      $selfCheckProcess.Dispose()
      $selfCheckProcessDisposed = $true
    } catch {
      $cleanupErrors.Add("process handle cleanup: $($_.Exception.Message)")
    }
  }
}

Write-Host "packaged self-check stdout:"
if (-not [string]::IsNullOrEmpty($selfCheckStdoutText)) {
  Write-Host $selfCheckStdoutText.TrimEnd()
}
Write-Host "packaged self-check stderr:"
if (-not [string]::IsNullOrEmpty($selfCheckStderrText)) {
  [Console]::Error.WriteLine($selfCheckStderrText.TrimEnd())
}
if ($null -eq $selfCheckExitCode) {
  Write-Host "packaged self-check exit code: unavailable"
} else {
  Write-Host "packaged self-check exit code: $selfCheckExitCode"
}
if ($selfCheckDirRemoved) {
  Write-Host "packaged self-check cleanup: removed $selfCheckDir"
} else {
  Write-Host "packaged self-check cleanup: failed $selfCheckDir"
}
if ($selfCheckProcessDisposed) {
  Write-Host "packaged self-check process handle: disposed"
}

$failureMessages = [System.Collections.Generic.List[string]]::new()
if ($selfCheckTimedOut) {
  $failureMessages.Add("packaged self-check が${TimeoutSeconds}秒以内に終了しませんでした。")
} elseif ($null -ne $selfCheckExecutionError) {
  $failureMessages.Add("packaged self-check の実行に失敗しました: $selfCheckExecutionError")
} elseif ($null -eq $selfCheckExitCode) {
  $failureMessages.Add("packaged self-check の終了コードを取得できませんでした。")
} elseif ($selfCheckExitCode -ne 0) {
  $failureMessages.Add("packaged self-check が終了コード $selfCheckExitCode で失敗しました。")
}
if ($null -ne $selfCheckExecutionError -and $selfCheckTimedOut) {
  $failureMessages.Add($selfCheckExecutionError)
}
foreach ($cleanupError in $cleanupErrors) {
  $failureMessages.Add($cleanupError)
}
if ($failureMessages.Count -gt 0) {
  throw ($failureMessages -join " | ")
}

Write-Host "packaged self-check passed."
