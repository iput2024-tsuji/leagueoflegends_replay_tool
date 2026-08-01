from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="packaged self-check runner is Windows-only",
)

RUNNER = Path("scripts/run_packaged_self_check.ps1").resolve()
POWERSHELL = shutil.which("pwsh")


def _run_self_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str,
    body: str,
    timeout_seconds: int = 5,
    taskkill_exe: Path | None = None,
    taskkill_timeout_seconds: int = 2,
    taskkill_prefix_argument: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    if POWERSHELL is None:
        pytest.skip("pwsh is unavailable")
    temp_root = tmp_path / "runner-temp"
    temp_root.mkdir(exist_ok=True)
    fake_app = tmp_path / f"fake-self-check-{name}.py"
    fake_app.write_text(body, encoding="utf-8")
    data_record = tmp_path / f"data-dir-{name}.txt"
    monkeypatch.setenv("SELF_CHECK_DATA_RECORD", str(data_record))
    for key, value in (extra_env or {}).items():
        monkeypatch.setenv(key, value)

    command = [
        POWERSHELL,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER),
        "-AppExe",
        sys.executable,
        "-TempRoot",
        str(temp_root),
        "-TimeoutSeconds",
        str(timeout_seconds),
        "-SelfCheckArguments",
        str(fake_app),
    ]
    if taskkill_exe is not None:
        command.extend(
            [
                "-TaskkillExe",
                str(taskkill_exe),
                "-TaskkillTimeoutSeconds",
                str(taskkill_timeout_seconds),
            ]
        )
    if taskkill_prefix_argument is not None:
        command.extend(["-TaskkillPrefixArguments", str(taskkill_prefix_argument)])

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )
    return result, Path(data_record.read_text(encoding="utf-8"))


def _combined_output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + "\n" + result.stderr


def _is_process_running(pid: int) -> bool:
    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def test_packaged_self_check_uses_unique_data_dir_and_reports_success(
    tmp_path,
    monkeypatch,
):
    body = """
import os
import sys
from pathlib import Path

data_dir = os.environ["LOL_REPLAY_TOOL_DATA_DIR"]
Path(os.environ["SELF_CHECK_DATA_RECORD"]).write_text(data_dir, encoding="utf-8")
print("success-stdout", flush=True)
print("success-stderr", file=sys.stderr, flush=True)
"""

    first, first_data_dir = _run_self_check(
        tmp_path,
        monkeypatch,
        name="success-first",
        body=body,
    )
    second, second_data_dir = _run_self_check(
        tmp_path,
        monkeypatch,
        name="success-second",
        body=body,
    )

    assert first.returncode == 0, _combined_output(first)
    assert second.returncode == 0, _combined_output(second)
    assert first_data_dir != second_data_dir
    for result, data_dir in ((first, first_data_dir), (second, second_data_dir)):
        output = _combined_output(result)
        assert data_dir.parent == tmp_path / "runner-temp"
        assert data_dir.name.startswith("lol-replay-tool-self-check-")
        assert not data_dir.exists()
        assert "success-stdout" in output
        assert "success-stderr" in output
        assert "packaged self-check exit code: 0" in output
        assert "packaged self-check cleanup: removed" in output
        assert "packaged self-check passed" in output


def test_packaged_self_check_captures_utf8_from_cp1252_child_streams(
    tmp_path,
    monkeypatch,
):
    body = """
import io
import os
import sys
from pathlib import Path

data_dir = os.environ["LOL_REPLAY_TOOL_DATA_DIR"]
Path(os.environ["SELF_CHECK_DATA_RECORD"]).write_text(data_dir, encoding="utf-8")
sys.path.insert(0, os.environ["SELF_CHECK_REPO_ROOT"])
sys.stdout = io.TextIOWrapper(sys.stdout.detach(), encoding="cp1252", errors="strict")
sys.stderr = io.TextIOWrapper(sys.stderr.detach(), encoding="cp1252", errors="strict")

import main as app_entrypoint
from src import self_check

self_check.run_self_check = lambda: {"ok": True}
self_check.format_self_check_report = lambda _report: (
    f"child-stdout-encoding={sys.stdout.encoding} 日本語出力"
)
self_check.self_check_exit_code = lambda _report: 0
exit_code = app_entrypoint.main(["--self-check"])
print(f"child-stderr-encoding={sys.stderr.encoding} 日本語エラー", file=sys.stderr)
raise SystemExit(exit_code)
"""

    result, data_dir = _run_self_check(
        tmp_path,
        monkeypatch,
        name="cp1252-child-streams",
        body=body,
        extra_env={"SELF_CHECK_REPO_ROOT": str(Path.cwd())},
    )
    output = _combined_output(result)

    assert result.returncode == 0, output
    assert not data_dir.exists()
    assert "child-stdout-encoding=utf-8 日本語出力" in output
    assert "child-stderr-encoding=utf-8 日本語エラー" in output
    assert "packaged self-check exit code: 0" in output
    assert "packaged self-check cleanup: removed" in output
    assert "packaged self-check passed" in output


def test_packaged_self_check_reports_nonzero_and_cleans_up(tmp_path, monkeypatch):
    body = """
import os
import sys
from pathlib import Path

data_dir = os.environ["LOL_REPLAY_TOOL_DATA_DIR"]
Path(os.environ["SELF_CHECK_DATA_RECORD"]).write_text(data_dir, encoding="utf-8")
print("failure-stdout", flush=True)
print("failure-stderr", file=sys.stderr, flush=True)
raise SystemExit(7)
"""

    result, data_dir = _run_self_check(
        tmp_path,
        monkeypatch,
        name="nonzero",
        body=body,
    )
    output = _combined_output(result)

    assert result.returncode != 0
    assert not data_dir.exists()
    assert "failure-stdout" in output
    assert "failure-stderr" in output
    assert "packaged self-check exit code: 7" in output
    assert "packaged self-check cleanup: removed" in output
    assert "終了コード 7" in output


def test_packaged_self_check_cleans_up_when_app_cannot_start(tmp_path):
    if POWERSHELL is None:
        pytest.skip("pwsh is unavailable")
    temp_root = tmp_path / "runner-temp"
    temp_root.mkdir()
    invalid_app = tmp_path / "invalid-self-check.exe"
    invalid_app.write_bytes(b"")
    result_record = tmp_path / "app-start-failure-result.json"
    wrapper = tmp_path / "invoke-app-start-failure.ps1"
    wrapper.write_text(
        """
param(
  [string]$Runner,
  [string]$AppExe,
  [string]$TempRoot,
  [string]$ResultRecord
)
$ErrorActionPreference = "Stop"
$env:LOL_REPLAY_TOOL_DATA_DIR = "existing-data-root"
$runnerFailed = $false
try {
  & $Runner `
    -AppExe $AppExe `
    -TempRoot $TempRoot `
    -TimeoutSeconds 5
} catch {
  $runnerFailed = $true
  Write-Error $_ -ErrorAction Continue
}
$remainingDirectories = @(
  Get-ChildItem `
    -LiteralPath $TempRoot `
    -Directory `
    -Filter "lol-replay-tool-self-check-*" `
    -ErrorAction Stop |
    ForEach-Object { $_.FullName }
)
[ordered]@{
  runnerFailed = $runnerFailed
  exists = (Test-Path Env:LOL_REPLAY_TOOL_DATA_DIR)
  value = $env:LOL_REPLAY_TOOL_DATA_DIR
  remainingDirectories = $remainingDirectories
} | ConvertTo-Json -Compress |
  Set-Content -LiteralPath $ResultRecord -Encoding utf8
if ($runnerFailed) {
  exit 1
}
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(wrapper),
            "-Runner",
            str(RUNNER),
            "-AppExe",
            str(invalid_app),
            "-TempRoot",
            str(temp_root),
            "-ResultRecord",
            str(result_record),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )
    output = _combined_output(result)
    restored = json.loads(result_record.read_text(encoding="utf-8-sig"))

    assert result.returncode != 0
    assert restored == {
        "runnerFailed": True,
        "exists": True,
        "value": "existing-data-root",
        "remainingDirectories": [],
    }
    assert "packaged self-check exit code: unavailable" in output
    assert "packaged self-check cleanup: removed" in output
    assert "packaged self-check の実行に失敗しました" in output


def test_packaged_self_check_kills_timeout_process_and_cleans_up(
    tmp_path,
    monkeypatch,
):
    pid_record = tmp_path / "timeout-pid.txt"
    child_pid_record = tmp_path / "timeout-child-pid.txt"
    completion_marker = tmp_path / "timeout-completed.txt"
    body = """
import os
import subprocess
import sys
import time
from pathlib import Path

data_dir = os.environ["LOL_REPLAY_TOOL_DATA_DIR"]
Path(os.environ["SELF_CHECK_DATA_RECORD"]).write_text(data_dir, encoding="utf-8")
Path(os.environ["SELF_CHECK_PID_RECORD"]).write_text(str(os.getpid()), encoding="ascii")
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
Path(os.environ["SELF_CHECK_CHILD_PID_RECORD"]).write_text(str(child.pid), encoding="ascii")
print("timeout-stdout", flush=True)
print("timeout-stderr", file=sys.stderr, flush=True)
time.sleep(30)
Path(os.environ["SELF_CHECK_COMPLETION_MARKER"]).write_text("not killed", encoding="utf-8")
"""
    started_at = time.monotonic()

    result, data_dir = _run_self_check(
        tmp_path,
        monkeypatch,
        name="timeout",
        body=body,
        timeout_seconds=1,
        extra_env={
            "SELF_CHECK_PID_RECORD": str(pid_record),
            "SELF_CHECK_CHILD_PID_RECORD": str(child_pid_record),
            "SELF_CHECK_COMPLETION_MARKER": str(completion_marker),
        },
    )
    elapsed = time.monotonic() - started_at
    output = _combined_output(result)
    pid = int(pid_record.read_text(encoding="ascii"))
    child_pid = int(child_pid_record.read_text(encoding="ascii"))

    assert result.returncode != 0
    assert elapsed < 15
    assert not _is_process_running(pid)
    assert not _is_process_running(child_pid)
    assert not completion_marker.exists()
    assert not data_dir.exists()
    assert "timeout-stdout" in output
    assert "timeout-stderr" in output
    assert "packaged self-check exit code:" in output
    assert "packaged self-check cleanup: removed" in output
    assert "1秒以内に終了しませんでした" in output


def test_packaged_self_check_bounds_taskkill_and_fails_closed(
    tmp_path,
    monkeypatch,
):
    target_pid_record = tmp_path / "taskkill-timeout-target-pid.txt"
    taskkill_pid_record = tmp_path / "hanging-taskkill-pid.txt"
    fake_taskkill = tmp_path / "fake-taskkill-hang.py"
    fake_taskkill.write_text(
        """
import os
import time
from pathlib import Path

Path(os.environ["TASKKILL_PID_RECORD"]).write_text(str(os.getpid()), encoding="ascii")
time.sleep(30)
""",
        encoding="utf-8",
    )
    body = """
import os
import time
from pathlib import Path

data_dir = os.environ["LOL_REPLAY_TOOL_DATA_DIR"]
Path(os.environ["SELF_CHECK_DATA_RECORD"]).write_text(data_dir, encoding="utf-8")
Path(os.environ["SELF_CHECK_TARGET_PID_RECORD"]).write_text(
    str(os.getpid()), encoding="ascii"
)
time.sleep(30)
"""
    started_at = time.monotonic()

    result, data_dir = _run_self_check(
        tmp_path,
        monkeypatch,
        name="taskkill-timeout",
        body=body,
        timeout_seconds=1,
        taskkill_exe=Path(sys.executable),
        taskkill_timeout_seconds=1,
        taskkill_prefix_argument=fake_taskkill,
        extra_env={
            "TASKKILL_PID_RECORD": str(taskkill_pid_record),
            "SELF_CHECK_TARGET_PID_RECORD": str(target_pid_record),
        },
    )
    elapsed = time.monotonic() - started_at
    output = _combined_output(result)
    target_pid = int(target_pid_record.read_text(encoding="ascii"))
    taskkill_pid = int(taskkill_pid_record.read_text(encoding="ascii"))

    assert result.returncode != 0
    assert elapsed < 15
    assert not _is_process_running(target_pid)
    assert not _is_process_running(taskkill_pid)
    assert not data_dir.exists()
    assert "taskkillによるprocess tree停止を確認できませんでした" in output
    assert "taskkill timedOut=True" in output
    assert "best-effort fallback" in output
    assert "packaged self-check cleanup: removed" in output


def test_packaged_self_check_fails_closed_when_taskkill_cannot_start(
    tmp_path,
    monkeypatch,
):
    target_pid_record = tmp_path / "taskkill-start-failure-target-pid.txt"
    invalid_taskkill = tmp_path / "invalid-taskkill.exe"
    invalid_taskkill.write_bytes(b"")
    body = """
import os
import time
from pathlib import Path

data_dir = os.environ["LOL_REPLAY_TOOL_DATA_DIR"]
Path(os.environ["SELF_CHECK_DATA_RECORD"]).write_text(data_dir, encoding="utf-8")
Path(os.environ["SELF_CHECK_TARGET_PID_RECORD"]).write_text(
    str(os.getpid()), encoding="ascii"
)
time.sleep(30)
"""

    result, data_dir = _run_self_check(
        tmp_path,
        monkeypatch,
        name="taskkill-start-failure",
        body=body,
        timeout_seconds=1,
        taskkill_exe=invalid_taskkill,
        extra_env={"SELF_CHECK_TARGET_PID_RECORD": str(target_pid_record)},
    )
    output = _combined_output(result)
    target_pid = int(target_pid_record.read_text(encoding="ascii"))

    assert result.returncode != 0
    assert not _is_process_running(target_pid)
    assert not data_dir.exists()
    assert "taskkillによるprocess tree停止を確認できませんでした" in output
    assert "taskkill timedOut=False exit=unavailable" in output
    assert "best-effort fallback" in output
    assert "packaged self-check cleanup: removed" in output


def test_packaged_self_check_fails_closed_if_parent_exits_during_taskkill(
    tmp_path,
    monkeypatch,
):
    fake_taskkill = tmp_path / "fake-taskkill-nonzero.py"
    fake_taskkill.write_text(
        """
import time

time.sleep(2)
raise SystemExit(9)
""",
        encoding="utf-8",
    )
    body = """
import os
import time
from pathlib import Path

data_dir = os.environ["LOL_REPLAY_TOOL_DATA_DIR"]
Path(os.environ["SELF_CHECK_DATA_RECORD"]).write_text(data_dir, encoding="utf-8")
time.sleep(2)
"""

    result, data_dir = _run_self_check(
        tmp_path,
        monkeypatch,
        name="parent-exit-race",
        body=body,
        timeout_seconds=1,
        taskkill_exe=Path(sys.executable),
        taskkill_timeout_seconds=5,
        taskkill_prefix_argument=fake_taskkill,
    )
    output = _combined_output(result)

    assert result.returncode != 0
    assert not data_dir.exists()
    assert "taskkillによるprocess tree停止を確認できませんでした" in output
    assert "taskkill timedOut=False exit=9" in output
    assert "target process" in output
    assert "already exited" in output
    assert "packaged self-check cleanup: removed" in output


@pytest.mark.parametrize(
    ("mode", "expected_exists", "expected_value"),
    [
        pytest.param("defined", True, "existing-data-root", id="defined"),
        pytest.param("undefined", False, None, id="undefined"),
    ],
)
def test_packaged_self_check_restores_process_environment(
    tmp_path,
    monkeypatch,
    mode,
    expected_exists,
    expected_value,
):
    if POWERSHELL is None:
        pytest.skip("pwsh is unavailable")
    temp_root = tmp_path / "runner-temp"
    temp_root.mkdir()
    data_record = tmp_path / "environment-data-dir.txt"
    result_record = tmp_path / "environment-result.json"
    fake_app = tmp_path / "fake-self-check-environment.py"
    fake_app.write_text(
        """
import os
from pathlib import Path

data_dir = os.environ["LOL_REPLAY_TOOL_DATA_DIR"]
Path(os.environ["SELF_CHECK_DATA_RECORD"]).write_text(data_dir, encoding="utf-8")
print("environment-stdout", flush=True)
""",
        encoding="utf-8",
    )
    wrapper = tmp_path / "invoke-self-check.ps1"
    wrapper.write_text(
        """
param(
  [string]$Runner,
  [string]$AppExe,
  [string]$FakeApp,
  [string]$TempRoot,
  [string]$Mode,
  [string]$ResultRecord
)
$ErrorActionPreference = "Stop"
if ($Mode -eq "defined") {
  $env:LOL_REPLAY_TOOL_DATA_DIR = "existing-data-root"
} else {
  Remove-Item Env:LOL_REPLAY_TOOL_DATA_DIR -ErrorAction SilentlyContinue
}
& $Runner `
  -AppExe $AppExe `
  -TempRoot $TempRoot `
  -TimeoutSeconds 5 `
  -SelfCheckArguments $FakeApp
$exists = Test-Path Env:LOL_REPLAY_TOOL_DATA_DIR
$value = if ($exists) { $env:LOL_REPLAY_TOOL_DATA_DIR } else { $null }
[ordered]@{ exists = $exists; value = $value } |
  ConvertTo-Json -Compress |
  Set-Content -LiteralPath $ResultRecord -Encoding utf8
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("SELF_CHECK_DATA_RECORD", str(data_record))

    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(wrapper),
            "-Runner",
            str(RUNNER),
            "-AppExe",
            sys.executable,
            "-FakeApp",
            str(fake_app),
            "-TempRoot",
            str(temp_root),
            "-Mode",
            mode,
            "-ResultRecord",
            str(result_record),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )
    output = _combined_output(result)
    restored = json.loads(result_record.read_text(encoding="utf-8-sig"))
    data_dir = Path(data_record.read_text(encoding="utf-8"))

    assert result.returncode == 0, output
    assert restored == {"exists": expected_exists, "value": expected_value}
    assert not data_dir.exists()
    assert "environment-stdout" in output
    assert "packaged self-check cleanup: removed" in output


def test_packaged_self_check_restores_environment_and_disposes_after_cleanup_failure(
    tmp_path,
    monkeypatch,
):
    if POWERSHELL is None:
        pytest.skip("pwsh is unavailable")
    temp_root = tmp_path / "runner-temp"
    temp_root.mkdir()
    data_record = tmp_path / "cleanup-failure-data-dir.txt"
    result_record = tmp_path / "cleanup-failure-result.json"
    fake_app = tmp_path / "fake-self-check-cleanup-failure.py"
    fake_app.write_text(
        """
import os
from pathlib import Path

data_dir = os.environ["LOL_REPLAY_TOOL_DATA_DIR"]
Path(os.environ["SELF_CHECK_DATA_RECORD"]).write_text(data_dir, encoding="utf-8")
print("cleanup-failure-stdout", flush=True)
""",
        encoding="utf-8",
    )
    wrapper = tmp_path / "invoke-self-check-cleanup-failure.ps1"
    wrapper.write_text(
        """
param(
  [string]$Runner,
  [string]$AppExe,
  [string]$FakeApp,
  [string]$TempRoot,
  [string]$ResultRecord
)
$ErrorActionPreference = "Stop"
$env:LOL_REPLAY_TOOL_DATA_DIR = "existing-data-root"
function Remove-Item {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory = $true)]
    [string]$LiteralPath,
    [switch]$Recurse,
    [switch]$Force
  )
  if ($Recurse -and $LiteralPath -like "*lol-replay-tool-self-check-*") {
    throw "injected temporary directory cleanup failure"
  }
  Microsoft.PowerShell.Management\\Remove-Item @PSBoundParameters
}
$runnerFailed = $false
try {
  & $Runner `
    -AppExe $AppExe `
    -TempRoot $TempRoot `
    -TimeoutSeconds 5 `
    -SelfCheckArguments $FakeApp
} catch {
  $runnerFailed = $true
  Write-Error $_ -ErrorAction Continue
}
[ordered]@{
  runnerFailed = $runnerFailed
  exists = (Test-Path Env:LOL_REPLAY_TOOL_DATA_DIR)
  value = $env:LOL_REPLAY_TOOL_DATA_DIR
} | ConvertTo-Json -Compress |
  Set-Content -LiteralPath $ResultRecord -Encoding utf8
if ($runnerFailed) {
  exit 1
}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("SELF_CHECK_DATA_RECORD", str(data_record))

    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(wrapper),
            "-Runner",
            str(RUNNER),
            "-AppExe",
            sys.executable,
            "-FakeApp",
            str(fake_app),
            "-TempRoot",
            str(temp_root),
            "-ResultRecord",
            str(result_record),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )
    output = _combined_output(result)
    restored = json.loads(result_record.read_text(encoding="utf-8-sig"))
    data_dir = Path(data_record.read_text(encoding="utf-8"))

    try:
        assert result.returncode != 0
        assert restored == {
            "runnerFailed": True,
            "exists": True,
            "value": "existing-data-root",
        }
        assert data_dir.exists()
        assert "cleanup-failure-stdout" in output
        assert "packaged self-check cleanup: failed" in output
        assert "packaged self-check process handle: disposed" in output
        assert "temporary directory cleanup" in output
        assert "injected temporary directory cleanup failure" in output
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)
