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
            OBSProcessInfo(pid=100, executable_path=managed_exe),
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
        lambda: [OBSProcessInfo(pid=100, executable_path=manager.obs_exe)],
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
            OBSProcessInfo(pid=100, executable_path=manager.obs_exe),
            OBSProcessInfo(pid=200, executable_path=manager.obs_exe),
            OBSProcessInfo(pid=300, executable_path=other_exe),
        ],
    )
    monkeypatch.setattr(manager, "_terminate_pid", lambda pid, force: calls.append((pid, force)) or True)

    killed = manager.kill_stale_owned_processes(timeout_sec=0)

    assert killed == [100]
    assert calls == [(100, False), (100, True)]
