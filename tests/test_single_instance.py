import shutil
from pathlib import Path

from src import single_instance


def runtime_dir(name: str) -> Path:
    path = Path("tests") / "_tmp" / name
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_single_instance_guard_rejects_existing_windows_mutex(monkeypatch):
    root = runtime_dir("single_instance_existing")
    monkeypatch.setattr(single_instance.os, "name", "nt")
    monkeypatch.setattr(single_instance, "_create_windows_mutex", lambda name: None)
    guard = single_instance.SingleInstanceGuard(lock_path=root / "instance.lock")

    assert guard.acquire() is False


def test_single_instance_guard_releases_windows_mutex(monkeypatch):
    root = runtime_dir("single_instance_release")
    handle = object()
    released = []
    monkeypatch.setattr(single_instance.os, "name", "nt")
    monkeypatch.setattr(single_instance, "_create_windows_mutex", lambda name: handle)
    monkeypatch.setattr(single_instance, "_close_windows_handle", released.append)
    guard = single_instance.SingleInstanceGuard(lock_path=root / "instance.lock")

    assert guard.acquire() is True
    assert guard.acquire() is True

    guard.release()

    assert released == [handle]
