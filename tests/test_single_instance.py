import shutil
from pathlib import Path

import pytest

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
    event_handles = {
        single_instance.UPDATE_SHUTDOWN_REQUEST: object(),
        single_instance.UPDATE_SHUTDOWN_BLOCKED: object(),
        single_instance.UPDATE_SHUTDOWN_COMPLETE: object(),
    }
    monkeypatch.setattr(single_instance, "_create_windows_event", event_handles.get)
    monkeypatch.setattr(single_instance, "_close_windows_handle", released.append)
    guard = single_instance.SingleInstanceGuard(lock_path=root / "instance.lock")

    assert guard.acquire() is True
    assert guard.acquire() is True

    guard.release()

    assert released == [
        event_handles[single_instance.UPDATE_SHUTDOWN_REQUEST],
        event_handles[single_instance.UPDATE_SHUTDOWN_BLOCKED],
        event_handles[single_instance.UPDATE_SHUTDOWN_COMPLETE],
        handle,
    ]


def test_single_instance_guard_rolls_back_when_named_event_exists(monkeypatch):
    root = runtime_dir("single_instance_event_exists")
    mutex = object()
    request = object()
    released = []
    monkeypatch.setattr(single_instance.os, "name", "nt")
    monkeypatch.setattr(single_instance, "_create_windows_mutex", lambda name: mutex)
    monkeypatch.setattr(
        single_instance,
        "_create_windows_event",
        lambda name: request if name == single_instance.UPDATE_SHUTDOWN_REQUEST else None,
    )
    monkeypatch.setattr(single_instance, "_close_windows_handle", released.append)
    guard = single_instance.SingleInstanceGuard(lock_path=root / "instance.lock")

    assert guard.acquire() is False
    assert released == [request, mutex]
    assert guard._windows_handle is None


def test_single_instance_guard_rolls_back_when_named_event_access_fails(monkeypatch):
    root = runtime_dir("single_instance_event_access_denied")
    mutex = object()
    request = object()
    released = []

    def create_event(name):
        if name == single_instance.UPDATE_SHUTDOWN_REQUEST:
            return request
        raise OSError("access denied")

    monkeypatch.setattr(single_instance.os, "name", "nt")
    monkeypatch.setattr(single_instance, "_create_windows_mutex", lambda name: mutex)
    monkeypatch.setattr(single_instance, "_create_windows_event", create_event)
    monkeypatch.setattr(single_instance, "_close_windows_handle", released.append)
    guard = single_instance.SingleInstanceGuard(lock_path=root / "instance.lock")

    assert guard.acquire() is False
    assert released == [request, mutex]
    assert guard._windows_handle is None


def test_single_instance_release_closes_all_handles_after_one_close_failure(
    monkeypatch,
):
    root = runtime_dir("single_instance_close_failure")
    mutex = object()
    request = object()
    blocked = object()
    complete = object()
    released = []

    def close_handle(handle):
        released.append(handle)
        if handle is request:
            raise OSError("request close failed")

    monkeypatch.setattr(single_instance.os, "name", "nt")
    monkeypatch.setattr(single_instance, "_create_windows_mutex", lambda name: mutex)
    monkeypatch.setattr(
        single_instance,
        "_create_windows_event",
        lambda name: {
            single_instance.UPDATE_SHUTDOWN_REQUEST: request,
            single_instance.UPDATE_SHUTDOWN_BLOCKED: blocked,
            single_instance.UPDATE_SHUTDOWN_COMPLETE: complete,
        }[name],
    )
    monkeypatch.setattr(single_instance, "_close_windows_handle", close_handle)
    guard = single_instance.SingleInstanceGuard(lock_path=root / "instance.lock")
    assert guard.acquire() is True

    with pytest.raises(OSError, match="request close failed"):
        guard.release()

    assert released == [request, blocked, complete, mutex]
    assert guard._windows_handle is None
    assert guard._update_shutdown_request_handle is None
    assert guard._update_shutdown_blocked_handle is None
    assert guard._update_shutdown_complete_handle is None


class _FakeWin32Function:
    def __init__(self, result):
        self.result = result

    def __call__(self, *args):
        return self.result


def test_update_shutdown_event_methods_are_nonblocking_and_signal(monkeypatch):
    lock_path = runtime_dir("single_instance_event_signal") / "instance.lock"
    request_handle = object()
    blocked_handle = object()
    complete_handle = object()
    kernel = type("Kernel", (), {})()
    kernel.WaitForSingleObject = _FakeWin32Function(single_instance.WAIT_OBJECT_0)
    kernel.SetEvent = _FakeWin32Function(1)
    monkeypatch.setattr(single_instance.os, "name", "nt")
    monkeypatch.setattr(single_instance.ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)
    guard = single_instance.SingleInstanceGuard(lock_path=lock_path)
    guard._update_shutdown_request_handle = request_handle
    guard._update_shutdown_blocked_handle = blocked_handle
    guard._update_shutdown_complete_handle = complete_handle

    assert guard.consume_update_shutdown_request() is True
    assert guard.signal_update_shutdown_blocked() is True
    assert guard.signal_update_shutdown_complete() is True
    assert kernel.WaitForSingleObject.result == single_instance.WAIT_OBJECT_0


def test_update_shutdown_event_methods_are_safe_noops_on_posix(monkeypatch):
    lock_path = runtime_dir("single_instance_posix") / "instance.lock"
    monkeypatch.setattr(single_instance.os, "name", "posix")
    guard = single_instance.SingleInstanceGuard(lock_path=lock_path)

    assert guard.consume_update_shutdown_request() is False
    assert guard.signal_update_shutdown_blocked() is False
    assert guard.signal_update_shutdown_complete() is False
