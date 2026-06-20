import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from src.obs_process import OBSProcessInfo, OBSProcessManager


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
