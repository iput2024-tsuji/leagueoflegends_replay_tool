import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import obs_process as obs_process_module
from src.obs_process import (
    OBSProcessInfo,
    OBSProcessManager,
    OBSProcessQueryError,
    OBSProcessQuerySnapshot,
)


def test_process_manager_only_targets_managed_obs(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/managed_obs").resolve())
    managed_exe = manager.obs_exe
    other_exe = Path("C:/Program Files/obs-studio/bin/64bit/obs64.exe")
    calls = []

    monkeypatch.setattr(
        manager,
        "list_obs_processes",
        lambda: [
            OBSProcessInfo(pid=100, executable_path=managed_exe, creation_time=10.0),
            OBSProcessInfo(pid=200, executable_path=other_exe),
            OBSProcessInfo(pid=300, executable_path=None),
        ],
    )
    monkeypatch.setattr(manager, "_terminate_pid", lambda pid, force: calls.append((pid, force)) or True)

    manager.kill_stale_managed_processes(timeout_sec=0)

    assert calls == [(100, False), (100, True)]


def test_process_manager_owned_kill_requires_lease(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/owned_obs_no_lease").resolve())
    calls = []
    monkeypatch.setattr(
        manager,
        "list_obs_processes",
        lambda: [OBSProcessInfo(pid=100, executable_path=manager.obs_exe, creation_time=10.0)],
    )
    monkeypatch.setattr(manager, "_terminate_pid", lambda pid, force: calls.append((pid, force)) or True)

    killed = manager.kill_stale_owned_processes(timeout_sec=0)

    assert killed == []
    assert calls == []


def test_process_manager_owned_kill_targets_leased_pid_only(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/owned_obs_with_lease").resolve())
    manager.write_process_lease(SimpleNamespace(pid=100))
    other_exe = Path("C:/Program Files/obs-studio/bin/64bit/obs64.exe")
    calls = []

    monkeypatch.setattr(
        manager,
        "list_obs_processes",
        lambda: [
            OBSProcessInfo(pid=100, executable_path=manager.obs_exe, creation_time=10.0),
            OBSProcessInfo(pid=200, executable_path=manager.obs_exe, creation_time=20.0),
            OBSProcessInfo(pid=300, executable_path=other_exe),
        ],
    )
    monkeypatch.setattr(manager, "_terminate_pid", lambda pid, force: calls.append((pid, force)) or True)

    killed = manager.kill_stale_owned_processes(timeout_sec=0)

    assert killed == [100]
    assert calls == [(100, False), (100, True)]


def test_process_manager_owned_kill_ignores_reused_pid_with_different_creation_time(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/owned_obs_reused_pid").resolve())
    manager.write_process_lease(SimpleNamespace(pid=100))
    calls = []

    monkeypatch.setattr(
        manager,
        "read_process_lease",
        lambda: SimpleNamespace(
            pid=100,
            executable_path=manager.obs_exe,
            created_at=1.0,
            process_creation_time=10.0,
        ),
    )
    monkeypatch.setattr(
        manager,
        "list_obs_processes",
        lambda: [OBSProcessInfo(pid=100, executable_path=manager.obs_exe, creation_time=99.0)],
    )
    monkeypatch.setattr(manager, "_terminate_pid", lambda pid, force: calls.append((pid, force)) or True)
    monkeypatch.setattr(manager, "clear_process_lease", lambda process=None: None)

    killed = manager.kill_stale_owned_processes(timeout_sec=0)

    assert killed == []
    assert calls == []


def test_process_manager_reports_owned_process_from_lease(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/owned_obs_detect").resolve())
    manager.write_process_lease(SimpleNamespace(pid=100))

    monkeypatch.setattr(
        manager,
        "read_process_lease",
        lambda: SimpleNamespace(
            pid=100,
            executable_path=manager.obs_exe,
            created_at=1.0,
            process_creation_time=10.0,
        ),
    )
    monkeypatch.setattr(
        manager,
        "list_obs_processes",
        lambda: [OBSProcessInfo(pid=100, executable_path=manager.obs_exe, creation_time=10.0)],
    )

    assert manager.has_owned_process() is True


def test_process_manager_reads_latest_portable_mode_log(tmp_path):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    logs_dir = manager.obs_dir / "config" / "obs-studio" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    old_log = logs_dir / "2026-01-01 00-00-00.txt"
    new_log = logs_dir / "2026-01-01 00-00-01.txt"
    old_log.write_text("Portable mode: false\n", encoding="utf-8")
    new_log.write_text("Portable mode: true\n", encoding="utf-8")
    os.utime(old_log, (1, 1))
    os.utime(new_log, (2, 2))

    assert manager.latest_log_portable_mode() is True


def test_process_manager_reads_available_encoder_kinds_from_latest_log(tmp_path):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    logs_dir = manager.obs_dir / "config" / "obs-studio" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "latest.txt").write_text(
        "12:00:00.001: Available Encoders:\n"
        "12:00:00.001:   Video Encoders:\n"
        "12:00:00.001: \t- obs_nvenc_hevc_tex (NVIDIA NVENC HEVC)\n"
        "12:00:00.001: \t- obs_nvenc_h264_tex (NVIDIA NVENC H.264)\n"
        "12:00:00.001: \t- obs_x264 (x264)\n"
        "12:00:00.001:   Audio Encoders:\n"
        "12:00:00.001: \t- ffmpeg_aac (FFmpeg AAC)\n"
        "12:00:00.001: Selected encoder: obs_nvenc_h264_tex\n",
        encoding="utf-8",
    )

    assert manager.latest_log_encoder_kinds() == [
        "obs_nvenc_hevc_tex",
        "obs_nvenc_h264_tex",
        "obs_x264",
    ]


def test_process_manager_reads_recording_diagnostics_after_timestamp(tmp_path):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    logs_dir = manager.obs_dir / "config" / "obs-studio" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "2026-06-20 14-25-44.txt"
    log_path.write_text(
        "14:25:48.901: Available Encoders:\n"
        "14:25:48.901: \t- obs_nvenc_h264_tex (NVIDIA NVENC H.264)\n"
        "14:30:12.000: ==== Recording Start ===============================================\n"
        "14:30:13.000: [jim-nvenc: 'simple_video_recording'] failed to start\n",
        encoding="utf-8",
    )

    diagnostics = manager.latest_log_recording_diagnostics(
        since=datetime(2026, 6, 20, 14, 30, 11).timestamp()
    )

    assert diagnostics == [
        "14:30:12.000: ==== Recording Start ===============================================",
        "14:30:13.000: [jim-nvenc: 'simple_video_recording'] failed to start",
    ]


def test_process_manager_reports_unmanaged_obs_process(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/unmanaged_obs_detect").resolve())
    other_exe = Path("C:/Program Files/obs-studio/bin/64bit/obs64.exe")
    monkeypatch.setattr(
        manager,
        "list_obs_processes",
        lambda: [
            OBSProcessInfo(pid=100, executable_path=manager.obs_exe, creation_time=10.0),
            OBSProcessInfo(pid=200, executable_path=other_exe, creation_time=20.0),
        ],
    )

    unmanaged = manager.unmanaged_processes()

    assert [process.pid for process in unmanaged] == [200]


def test_hide_main_windows_noops_outside_windows(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/hide_obs_non_windows").resolve())
    monkeypatch.setattr("src.obs_process.os.name", "posix")

    assert manager.hide_main_windows(SimpleNamespace(pid=1234), timeout_sec=0) == 0


def test_windows_process_queries_run_hidden(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/hidden_obs_query").resolve())
    calls = []

    class FakeStartupInfo:
        def __init__(self):
            self.dwFlags = 0
            self.wShowWindow = None

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout="Node,CreationDate,ExecutablePath,ProcessId\nhost,,C:/obs/bin/64bit/obs64.exe,123\n",
        )

    monkeypatch.setattr("src.obs_process.os.name", "nt")
    monkeypatch.setattr("src.obs_process.subprocess.STARTUPINFO", FakeStartupInfo, raising=False)
    monkeypatch.setattr("src.obs_process.subprocess.STARTF_USESHOWWINDOW", 1, raising=False)
    monkeypatch.setattr("src.obs_process.subprocess.CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr("src.obs_process.subprocess.run", fake_run)

    processes = manager._list_obs_processes_windows()

    assert [process.pid for process in processes] == [123]
    assert len(calls) == 1
    _command, kwargs = calls[0]
    assert kwargs["creationflags"] == 0x08000000
    assert kwargs["startupinfo"].dwFlags & 1
    assert kwargs["startupinfo"].wShowWindow == 0


def test_strict_process_query_distinguishes_successful_empty_result(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/strict_obs_query_empty").resolve())
    monkeypatch.setattr("src.obs_process.os.name", "nt")
    monkeypatch.setattr(
        manager,
        "_run_hidden",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=""),
    )

    snapshot = manager.query_obs_processes_strict()

    assert snapshot.processes == ()
    assert snapshot.queried_at > 0


def test_strict_process_query_returns_complete_process_snapshot(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/strict_obs_query_process").resolve())
    handle_identity = OBSProcessInfo(
        pid=123,
        executable_path=Path("C:/obs/bin/64bit/obs64.exe"),
        creation_time=123.5,
        creation_time_filetime=116444737235000000,
    )
    closed = []
    monkeypatch.setattr("src.obs_process.os.name", "nt")
    monkeypatch.setattr(
        manager,
        "_run_hidden",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"ProcessId":123,"ExecutablePath":"C:/obs/bin/64bit/obs64.exe"}',
        ),
    )
    monkeypatch.setattr(manager, "_open_process_identity_handle", lambda pid: "handle-123")
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: handle_identity,
    )
    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        lambda handle, timeout_ms: False,
    )
    monkeypatch.setattr(
        manager,
        "_close_process_identity_handle",
        lambda handle: closed.append(handle),
    )

    snapshot = manager.query_obs_processes_strict()

    assert snapshot.processes == (
        handle_identity,
    )
    assert closed == ["handle-123"]


@pytest.mark.parametrize("failure", ["open", "path", "filetime", "close"])
def test_strict_process_query_fails_closed_when_row_handle_binding_fails(
    monkeypatch,
    failure,
):
    manager = OBSProcessManager(Path("tests/_tmp/strict_obs_query_binding").resolve())
    closed = []
    monkeypatch.setattr("src.obs_process.os.name", "nt")
    monkeypatch.setattr(
        manager,
        "_run_hidden",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"ProcessId":123,"ExecutablePath":"C:/obs/bin/64bit/obs64.exe"}',
        ),
    )

    def open_handle(pid):
        if failure == "open":
            raise OSError("OpenProcess failed")
        return "handle-123"

    executable = (
        Path("C:/other/obs64.exe")
        if failure == "path"
        else Path("C:/obs/bin/64bit/obs64.exe")
    )
    identity = OBSProcessInfo(
        123,
        executable,
        123.5,
        None if failure == "filetime" else 116444737235000000,
    )

    def close_handle(handle):
        closed.append(handle)
        if failure == "close":
            raise OSError("CloseHandle failed")

    monkeypatch.setattr(manager, "_open_process_identity_handle", open_handle)
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: identity,
    )
    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        lambda handle, timeout_ms: False,
    )
    monkeypatch.setattr(manager, "_close_process_identity_handle", close_handle)

    with pytest.raises((OSError, OBSProcessQueryError)):
        manager.query_obs_processes_strict()

    assert closed == ([] if failure == "open" else ["handle-123"])


def test_strict_process_query_filters_cim_row_whose_handle_already_exited(
    monkeypatch,
):
    manager = OBSProcessManager(Path("tests/_tmp/strict_obs_query_zombie").resolve())
    identity = OBSProcessInfo(
        123,
        Path("C:/obs/bin/64bit/obs64.exe"),
        123.5,
        116444737235000000,
    )
    closed = []
    monkeypatch.setattr("src.obs_process.os.name", "nt")
    monkeypatch.setattr(
        manager,
        "_run_hidden",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"ProcessId":123,"ExecutablePath":"C:/obs/bin/64bit/obs64.exe"}',
        ),
    )
    monkeypatch.setattr(manager, "_open_process_identity_handle", lambda pid: "handle-123")
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: identity,
    )
    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        lambda handle, timeout_ms: True,
    )
    monkeypatch.setattr(
        manager,
        "_close_process_identity_handle",
        lambda handle: closed.append(handle),
    )

    snapshot = manager.query_obs_processes_strict()

    assert snapshot.processes == ()
    assert closed == ["handle-123"]


def test_strict_process_query_raises_when_query_fails(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/strict_obs_query_failure").resolve())
    monkeypatch.setattr("src.obs_process.os.name", "nt")
    monkeypatch.setattr(
        manager,
        "_run_hidden",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=""),
    )

    with pytest.raises(OBSProcessQueryError, match="exit code 1"):
        manager.query_obs_processes_strict()


def test_strict_process_query_rejects_stderr_only_cim_failure(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/strict_obs_query_stderr").resolve())
    commands = []
    monkeypatch.setattr("src.obs_process.os.name", "nt")

    def fail_on_stderr(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout="",
            stderr="Get-CimInstance: access denied",
        )

    monkeypatch.setattr(manager, "_run_hidden", fail_on_stderr)

    with pytest.raises(OBSProcessQueryError, match="stderr"):
        manager.query_obs_processes_strict()

    script = commands[0][-1]
    assert "$ErrorActionPreference = 'Stop'" in script
    assert "Get-CimInstance" in script and "-ErrorAction Stop" in script
    assert "Name='ProcessId'" in script and "[long]$_.ProcessId" in script
    assert "Get-Process" not in script


@pytest.mark.parametrize(
    "stdout",
    [
        "not-json",
        "null",
        '"unexpected"',
        '{"ExecutablePath":"C:/obs/bin/64bit/obs64.exe"}',
        '{"ProcessId":"invalid","ExecutablePath":"C:/obs/bin/64bit/obs64.exe"}',
        '{"ProcessId":true,"ExecutablePath":"C:/obs/bin/64bit/obs64.exe"}',
        '{"ProcessId":123,"ExecutablePath":null,"CreationDate":"invalid"}',
        '{"ProcessId":123,"ExecutablePath":null,"CreationDate":"123.5"}',
        (
            '{"ProcessId":123,"ExecutablePath":"relative/obs64.exe",'
            '"CreationDate":"123.5"}'
        ),
        (
            '[{"ProcessId":123,"ExecutablePath":"C:/obs/bin/64bit/obs64.exe",'
            '"CreationDate":"123.5"},'
            '{"ProcessId":123,"ExecutablePath":"C:/obs/bin/64bit/obs64.exe",'
            '"CreationDate":"123.5"}]'
        ),
    ],
)
def test_strict_process_query_raises_for_malformed_results(monkeypatch, stdout):
    manager = OBSProcessManager(Path("tests/_tmp/strict_obs_query_malformed").resolve())
    monkeypatch.setattr("src.obs_process.os.name", "nt")
    monkeypatch.setattr(
        manager,
        "_run_hidden",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=stdout),
    )

    with pytest.raises(OBSProcessQueryError):
        manager.query_obs_processes_strict()


def test_taskkill_runs_hidden_on_windows(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/hidden_taskkill").resolve())
    calls = []

    class FakeStartupInfo:
        def __init__(self):
            self.dwFlags = 0
            self.wShowWindow = None

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr("src.obs_process.os.name", "nt")
    monkeypatch.setattr("src.obs_process.subprocess.STARTUPINFO", FakeStartupInfo, raising=False)
    monkeypatch.setattr("src.obs_process.subprocess.STARTF_USESHOWWINDOW", 1, raising=False)
    monkeypatch.setattr("src.obs_process.subprocess.CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr("src.obs_process.subprocess.run", fake_run)

    assert manager._terminate_pid(123, force=True) is True

    command, kwargs = calls[0]
    assert command == ["taskkill", "/pid", "123", "/f"]
    assert kwargs["creationflags"] == 0x08000000
    assert kwargs["startupinfo"].dwFlags & 1


def test_taskkill_nonzero_exit_is_not_reported_as_signaled(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/taskkill_failure").resolve())
    monkeypatch.setattr(
        manager,
        "_run_hidden",
        lambda *args, **kwargs: SimpleNamespace(returncode=5),
    )

    assert manager._terminate_pid(123, force=False) is False


def test_strict_termination_signals_each_exact_identity(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/strict_termination").resolve())
    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: False)
    first = OBSProcessInfo(101, manager.obs_exe, 10.0)
    second = OBSProcessInfo(202, manager.obs_exe, 20.0)
    current = {first.pid: first, second.pid: second}
    query_clock = 100.0
    signals = []

    def query():
        nonlocal query_clock
        query_clock += 1.0
        return OBSProcessQuerySnapshot(
            processes=tuple(current.values()),
            queried_at=query_clock,
        )

    def terminate(pid, force):
        signals.append((pid, force))
        current.pop(pid)
        return True

    monkeypatch.setattr(manager, "query_obs_processes_strict", query)
    monkeypatch.setattr(manager, "_terminate_pid", terminate)

    result = manager.terminate_expected_obs_processes_strict((first, second))

    assert result.signaled_processes == (first, second)
    assert result.after.processes == ()
    assert signals == [(101, False), (202, False)]


def test_strict_termination_rejects_same_pid_replacement_before_first_signal(
    monkeypatch,
):
    manager = OBSProcessManager(Path("tests/_tmp/strict_termination_reuse").resolve())
    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: False)
    expected = OBSProcessInfo(101, manager.obs_exe, 10.0)
    replacement = OBSProcessInfo(101, manager.obs_exe, 99.0)
    signals = []
    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: OBSProcessQuerySnapshot((replacement,), 100.0),
    )
    monkeypatch.setattr(
        manager,
        "_terminate_pid",
        lambda pid, force: signals.append((pid, force)) or True,
    )

    with pytest.raises(OBSProcessQueryError, match="replaced|disappeared"):
        manager.terminate_expected_obs_processes_strict((expected,))

    assert signals == []


def test_strict_termination_rechecks_later_identity_before_each_signal(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/strict_termination_mid_reuse").resolve())
    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: False)
    first = OBSProcessInfo(101, manager.obs_exe, 10.0)
    second = OBSProcessInfo(202, manager.obs_exe, 20.0)
    replacement = OBSProcessInfo(202, manager.obs_exe, 99.0)
    current = {first.pid: first, second.pid: second}
    signals = []

    def query():
        return OBSProcessQuerySnapshot(tuple(current.values()), 100.0)

    def terminate(pid, force):
        signals.append((pid, force))
        if pid == first.pid:
            current.pop(first.pid)
            current[second.pid] = replacement
        return True

    monkeypatch.setattr(manager, "query_obs_processes_strict", query)
    monkeypatch.setattr(manager, "_terminate_pid", terminate)

    with pytest.raises(OBSProcessQueryError, match="replaced"):
        manager.terminate_expected_obs_processes_strict((first, second))

    assert signals == [(first.pid, False)]


def test_strict_termination_rejects_nonmanaged_expected_path_before_query(
    monkeypatch,
):
    manager = OBSProcessManager(Path("tests/_tmp/strict_termination_path").resolve())
    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: False)
    unexpected = OBSProcessInfo(
        101,
        Path("C:/Program Files/obs-studio/bin/64bit/obs64.exe"),
        10.0,
    )
    query_calls = 0

    def query():
        nonlocal query_calls
        query_calls += 1
        return OBSProcessQuerySnapshot((unexpected,), 100.0)

    monkeypatch.setattr(manager, "query_obs_processes_strict", query)

    with pytest.raises(OBSProcessQueryError, match="non-managed"):
        manager.terminate_expected_obs_processes_strict((unexpected,))

    assert query_calls == 0


def test_strict_termination_does_not_attribute_failed_signal_to_natural_exit(
    monkeypatch,
):
    manager = OBSProcessManager(Path("tests/_tmp/strict_termination_signal_failure").resolve())
    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: False)
    expected = OBSProcessInfo(101, manager.obs_exe, 10.0)
    current = {expected.pid: expected}

    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: OBSProcessQuerySnapshot(tuple(current.values()), 100.0),
    )

    def failed_signal(pid, force):
        current.pop(pid)
        return False

    monkeypatch.setattr(manager, "_terminate_pid", failed_signal)

    with pytest.raises(OBSProcessQueryError, match="signal failed"):
        manager.terminate_expected_obs_processes_strict((expected,))


def test_windows_handle_bound_termination_holds_identity_through_zero_snapshot(
    monkeypatch,
):
    manager = OBSProcessManager(Path("tests/_tmp/windows_handle_stop").resolve())
    expected = OBSProcessInfo(101, manager.obs_exe, 1000.1234, 116444746001234000)
    current = {expected.pid: expected}
    exited = {"handle-101": False}
    events = []
    query_clock = 100.0

    def query():
        nonlocal query_clock
        query_clock += 1.0
        events.append(("query", tuple(current)))
        return OBSProcessQuerySnapshot(tuple(current.values()), query_clock)

    def terminate(pid, force):
        events.append(("graceful", pid))
        current.pop(pid)
        exited[f"handle-{pid}"] = True
        return True

    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)
    monkeypatch.setattr(manager, "query_obs_processes_strict", query)
    monkeypatch.setattr(
        manager,
        "_open_process_identity_handle",
        lambda pid: events.append(("open", pid)) or f"handle-{pid}",
    )
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: OBSProcessInfo(
            pid,
            Path("\\\\?\\" + str(manager.obs_exe)),
            1000.12349,
            116444746001234000,
        ),
    )
    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        lambda handle, timeout_ms: exited[handle],
    )
    monkeypatch.setattr(manager, "_terminate_pid", terminate)
    monkeypatch.setattr(
        manager,
        "_close_process_identity_handle",
        lambda handle: events.append(("close", handle)),
    )

    result = manager.terminate_expected_obs_processes_strict((expected,))

    assert result.signaled_processes == (expected,)
    assert result.after.processes == ()
    assert events[-2:] == [("query", ()), ("close", "handle-101")]


def test_windows_handle_bound_termination_fails_closed_when_final_close_fails(
    monkeypatch,
):
    manager = OBSProcessManager(Path("tests/_tmp/windows_handle_close").resolve())
    expected = OBSProcessInfo(101, manager.obs_exe, 1000.1234, 116444746001234000)
    current = {expected.pid: expected}
    exited = {"handle-101": False}
    signals = []
    closed = []
    query_clock = 100.0

    def query():
        nonlocal query_clock
        query_clock += 1.0
        return OBSProcessQuerySnapshot(tuple(current.values()), query_clock)

    def terminate(pid, force):
        signals.append((pid, force))
        current.pop(pid)
        exited[f"handle-{pid}"] = True
        return True

    def fail_close(handle):
        closed.append(handle)
        raise OSError("CloseHandle failed")

    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)
    monkeypatch.setattr(manager, "query_obs_processes_strict", query)
    monkeypatch.setattr(
        manager,
        "_open_process_identity_handle",
        lambda pid: f"handle-{pid}",
    )
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: expected,
    )
    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        lambda handle, timeout_ms: exited[handle],
    )
    monkeypatch.setattr(manager, "_terminate_pid", terminate)
    monkeypatch.setattr(manager, "_close_process_identity_handle", fail_close)

    with pytest.raises(
        OBSProcessQueryError,
        match="identity handle could not be closed",
    ):
        manager.terminate_expected_obs_processes_strict((expected,))

    assert signals == [(101, False)]
    assert current == {}
    assert closed == ["handle-101"]


def test_windows_handle_close_failure_does_not_replace_termination_error(
    monkeypatch,
):
    manager = OBSProcessManager(Path("tests/_tmp/windows_handle_unwind").resolve())
    expected = OBSProcessInfo(101, manager.obs_exe, 1000.1234, 116444746001234000)
    closed = []
    critical_messages = []
    manager.logger = SimpleNamespace(
        critical=lambda message, *args: critical_messages.append(
            message % args if args else message
        )
    )

    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)
    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: OBSProcessQuerySnapshot((expected,), 100.0),
    )
    monkeypatch.setattr(
        manager,
        "_open_process_identity_handle",
        lambda pid: f"handle-{pid}",
    )
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: expected,
    )
    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        lambda handle, timeout_ms: False,
    )
    monkeypatch.setattr(manager, "_terminate_pid", lambda pid, force: False)

    def fail_close(handle):
        closed.append(handle)
        raise OSError("CloseHandle failed during unwind")

    monkeypatch.setattr(manager, "_close_process_identity_handle", fail_close)

    with pytest.raises(
        OBSProcessQueryError,
        match="termination signal failed",
    ) as raised:
        manager.terminate_expected_obs_processes_strict((expected,))

    assert closed == ["handle-101"]
    assert any(
        "CloseHandle also failed while unwinding strict OBS termination" in message
        for message in critical_messages
    )
    notes = getattr(raised.value, "__notes__", None)
    if notes is not None:
        assert any("CloseHandle also failed" in note for note in notes)


def test_windows_handle_bound_termination_requires_expected_filetime(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/windows_handle_filetime").resolve())
    incomplete = OBSProcessInfo(101, manager.obs_exe, 1000.1234)
    query_calls = 0

    def query():
        nonlocal query_calls
        query_calls += 1
        return OBSProcessQuerySnapshot((incomplete,), 100.0)

    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)
    monkeypatch.setattr(manager, "query_obs_processes_strict", query)

    with pytest.raises(OBSProcessQueryError, match="FILETIME"):
        manager.terminate_expected_obs_processes_strict((incomplete,))

    assert query_calls == 0


def test_popen_identity_uses_owned_windows_handle_without_closing_it(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/popen_handle_identity").resolve())
    identity = OBSProcessInfo(
        101,
        manager.obs_exe,
        1000.1234,
        116444746001234000,
    )
    process = SimpleNamespace(pid=101, _handle="popen-owned", poll=lambda: None)
    queried = []
    monkeypatch.setattr("src.obs_process.os.name", "nt")
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: queried.append((handle, pid)) or identity,
    )
    monkeypatch.setattr(
        manager,
        "_close_process_identity_handle",
        lambda handle: pytest.fail("Popen owns this handle; it must not be closed here"),
    )

    assert manager.query_popen_process_identity(process) == identity
    assert queried == [("popen-owned", 101)]


@pytest.mark.parametrize("failure", ["open", "path", "creation", "natural-exit", "signal"])
def test_windows_handle_bound_termination_fails_closed_and_closes_handle(
    monkeypatch,
    failure,
):
    manager = OBSProcessManager(Path("tests/_tmp/windows_handle_failure").resolve())
    expected = OBSProcessInfo(101, manager.obs_exe, 1000.1234, 116444746001234000)
    signals = []
    closed = []

    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)
    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: OBSProcessQuerySnapshot((expected,), 100.0),
    )

    def open_handle(pid):
        if failure == "open":
            raise OSError("OpenProcess failed")
        return f"handle-{pid}"

    actual = expected
    if failure == "path":
        actual = OBSProcessInfo(
            expected.pid,
            Path("C:/other/obs64.exe"),
            expected.creation_time,
            expected.creation_time_filetime,
        )
    elif failure == "creation":
        actual = OBSProcessInfo(
            expected.pid,
            expected.executable_path,
            expected.creation_time,
            expected.creation_time_filetime + 1,
        )
    monkeypatch.setattr(manager, "_open_process_identity_handle", open_handle)
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: actual,
    )
    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        lambda handle, timeout_ms: failure == "natural-exit",
    )
    monkeypatch.setattr(
        manager,
        "_terminate_pid",
        lambda pid, force: signals.append((pid, force)) or failure != "signal",
    )
    monkeypatch.setattr(
        manager,
        "_close_process_identity_handle",
        lambda handle: closed.append(handle),
    )

    with pytest.raises((OSError, OBSProcessQueryError)):
        manager.terminate_expected_obs_processes_strict((expected,))

    assert signals == ([] if failure in {"open", "path", "creation", "natural-exit"} else [(101, False)])
    assert closed == ([] if failure == "open" else ["handle-101"])


def test_windows_handle_detects_replacement_between_targets_before_signal(
    monkeypatch,
):
    manager = OBSProcessManager(Path("tests/_tmp/windows_handle_mid_reuse").resolve())
    first = OBSProcessInfo(101, manager.obs_exe, 1000.100, 116444746001000000)
    second = OBSProcessInfo(202, manager.obs_exe, 1000.200, 116444746002000000)
    replacement = OBSProcessInfo(202, manager.obs_exe, 1000.999, 116444746009990000)
    current = {first.pid: first, second.pid: second}
    exited = {"handle-101": False, "handle-202": False}
    opened = []
    closed = []
    signals = []

    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)
    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: OBSProcessQuerySnapshot(tuple(current.values()), 100.0),
    )

    def open_handle(pid):
        opened.append(pid)
        return f"handle-{pid}"

    def query_handle(handle, pid):
        if pid == second.pid:
            return replacement
        return first

    def terminate(pid, force):
        signals.append((pid, force))
        current.pop(pid)
        exited[f"handle-{pid}"] = True
        return True

    monkeypatch.setattr(manager, "_open_process_identity_handle", open_handle)
    monkeypatch.setattr(manager, "_query_process_identity_from_handle", query_handle)
    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        lambda handle, timeout_ms: exited[handle],
    )
    monkeypatch.setattr(manager, "_terminate_pid", terminate)
    monkeypatch.setattr(
        manager,
        "_close_process_identity_handle",
        lambda handle: closed.append(handle),
    )

    with pytest.raises(OBSProcessQueryError, match="handle identity"):
        manager.terminate_expected_obs_processes_strict((first, second))

    assert opened == [101, 202]
    assert signals == [(101, False)]
    assert closed == ["handle-202", "handle-101"]


def test_windows_handle_force_uses_validated_handle_instead_of_pid(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/windows_handle_force").resolve())
    expected = OBSProcessInfo(101, manager.obs_exe, 1000.123, 116444746001230000)
    current = {expected.pid: expected}
    exited = {"handle-101": False}
    graceful = []
    forced = []
    closed = []

    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)
    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: OBSProcessQuerySnapshot(tuple(current.values()), 100.0),
    )
    monkeypatch.setattr(manager, "_open_process_identity_handle", lambda pid: f"handle-{pid}")
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: expected,
    )
    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        lambda handle, timeout_ms: exited[handle],
    )
    monkeypatch.setattr(
        manager,
        "_terminate_pid",
        lambda pid, force: graceful.append((pid, force)) or True,
    )

    def force_handle(handle):
        forced.append(handle)
        exited[handle] = True
        current.clear()
        return True

    monkeypatch.setattr(manager, "_terminate_process_identity_handle", force_handle)
    monkeypatch.setattr(
        manager,
        "_close_process_identity_handle",
        lambda handle: closed.append(handle),
    )

    result = manager.terminate_expected_obs_processes_strict(
        (expected,),
        timeout_sec=0,
    )

    assert result.after.processes == ()
    assert graceful == [(101, False)]
    assert forced == ["handle-101"]
    assert closed == ["handle-101"]


def test_windows_handle_requeries_one_transitional_exited_identity_row(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/windows_handle_zombie_transition").resolve())
    expected = OBSProcessInfo(101, manager.obs_exe, 1000.123, 116444746001230000)
    exited = {"handle-101": False}
    query_calls = 0
    closed = []

    def query():
        nonlocal query_calls
        query_calls += 1
        processes = (expected,) if query_calls <= 2 else ()
        return OBSProcessQuerySnapshot(processes, 100.0 + query_calls)

    def terminate(pid, force):
        exited["handle-101"] = True
        return True

    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)
    monkeypatch.setattr(manager, "query_obs_processes_strict", query)
    monkeypatch.setattr(manager, "_open_process_identity_handle", lambda pid: "handle-101")
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: expected,
    )
    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        lambda handle, timeout_ms: exited[handle],
    )
    monkeypatch.setattr(manager, "_terminate_pid", terminate)
    monkeypatch.setattr(
        manager,
        "_close_process_identity_handle",
        lambda handle: closed.append(handle),
    )

    result = manager.terminate_expected_obs_processes_strict((expected,))

    assert query_calls == 3
    assert result.after.processes == ()
    assert closed == ["handle-101"]


def test_windows_handle_bounds_exited_row_requery_per_identity(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/windows_handle_zombie_multi").resolve())
    first = OBSProcessInfo(101, manager.obs_exe, 1000.1, 116444746001000000)
    second = OBSProcessInfo(202, manager.obs_exe, 1000.2, 116444746002000000)
    exited = {"handle-101": False, "handle-202": False}
    snapshots = iter(
        (
            (first, second),
            (first, second),
            (first,),
            (second,),
            (),
        )
    )
    closed = []

    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)
    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: OBSProcessQuerySnapshot(next(snapshots), 100.0),
    )
    monkeypatch.setattr(
        manager,
        "_open_process_identity_handle",
        lambda pid: f"handle-{pid}",
    )
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: first if pid == first.pid else second,
    )
    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        lambda handle, timeout_ms: exited[handle],
    )

    def terminate(pid, force):
        exited[f"handle-{pid}"] = True
        return True

    monkeypatch.setattr(manager, "_terminate_pid", terminate)
    monkeypatch.setattr(
        manager,
        "_close_process_identity_handle",
        lambda handle: closed.append(handle),
    )

    result = manager.terminate_expected_obs_processes_strict((first, second))

    assert result.after.processes == ()
    assert closed == ["handle-202", "handle-101"]


def test_windows_handle_rejects_repeated_exited_identity_row(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/windows_handle_zombie_repeat").resolve())
    expected = OBSProcessInfo(101, manager.obs_exe, 1000.1, 116444746001000000)
    exited = {"handle-101": False}

    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)
    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: OBSProcessQuerySnapshot((expected,), 100.0),
    )
    monkeypatch.setattr(manager, "_open_process_identity_handle", lambda pid: "handle-101")
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: expected,
    )
    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        lambda handle, timeout_ms: exited[handle],
    )

    def terminate(pid, force):
        exited["handle-101"] = True
        return True

    monkeypatch.setattr(manager, "_terminate_pid", terminate)
    monkeypatch.setattr(manager, "_close_process_identity_handle", lambda handle: None)

    with pytest.raises(OBSProcessQueryError, match="repeatedly retained"):
        manager.terminate_expected_obs_processes_strict((expected,))


def test_windows_creation_time_parser_preserves_fraction_and_timezone():
    value = "20260803123456.123456+540"
    expected = datetime(
        2026,
        8,
        3,
        12,
        34,
        56,
        123456,
        tzinfo=timezone(timedelta(hours=9)),
    ).timestamp()

    assert obs_process_module._parse_windows_process_creation_time(value) == expected
    assert obs_process_module._parse_windows_process_creation_time("/Date(1234567890123)/") == 1234567890.123
