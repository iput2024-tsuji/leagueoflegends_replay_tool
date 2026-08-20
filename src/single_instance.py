from __future__ import annotations

import ctypes
import os
from pathlib import Path
from typing import Any

try:
    from .app_paths import APP_NAME, get_user_data_root
except ImportError:
    from app_paths import APP_NAME, get_user_data_root

ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0
WAIT_FAILED = 0xFFFFFFFF
UPDATE_SHUTDOWN_REQUEST = f"Local\\{APP_NAME}.UpdateShutdown"
UPDATE_SHUTDOWN_BLOCKED = f"Local\\{APP_NAME}.UpdateShutdownBlocked"
UPDATE_SHUTDOWN_COMPLETE = f"Local\\{APP_NAME}.UpdateShutdownComplete"


def _create_windows_mutex(name: str) -> Any | None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool

    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return handle


def _create_windows_event(name: str) -> Any | None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateEventW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool

    ctypes.set_last_error(0)
    handle = kernel32.CreateEventW(None, False, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        if not kernel32.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())
        return None
    return handle


def _close_windows_handle(handle: Any) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    if not kernel32.CloseHandle(handle):
        raise ctypes.WinError(ctypes.get_last_error())


class SingleInstanceGuard:
    """Keeps one application process active per signed-in user."""

    def __init__(self, *, name: str | None = None, lock_path: Path | None = None) -> None:
        self.name = name or f"Local\\{APP_NAME}.SingleInstance"
        self.lock_path = lock_path or (get_user_data_root() / ".app-instance.lock")
        self._windows_handle = None
        self._update_shutdown_request_handle = None
        self._update_shutdown_blocked_handle = None
        self._update_shutdown_complete_handle = None
        self._lock_file = None

    def acquire(self) -> bool:
        if self._windows_handle is not None or self._lock_file is not None:
            return True
        if os.name == "nt":
            mutex_handle = _create_windows_mutex(self.name)
            if mutex_handle is None:
                return False
            request_handle = None
            blocked_handle = None
            complete_handle = None
            creation_failed = False
            try:
                request_handle = _create_windows_event(UPDATE_SHUTDOWN_REQUEST)
                blocked_handle = _create_windows_event(UPDATE_SHUTDOWN_BLOCKED)
                complete_handle = _create_windows_event(UPDATE_SHUTDOWN_COMPLETE)
            except Exception:
                creation_failed = True
            if (
                creation_failed
                or request_handle is None
                or blocked_handle is None
                or complete_handle is None
            ):
                try:
                    if request_handle is not None:
                        _close_windows_handle(request_handle)
                finally:
                    try:
                        if blocked_handle is not None:
                            _close_windows_handle(blocked_handle)
                    finally:
                        try:
                            if complete_handle is not None:
                                _close_windows_handle(complete_handle)
                        finally:
                            _close_windows_handle(mutex_handle)
                return False
            self._windows_handle = mutex_handle
            self._update_shutdown_request_handle = request_handle
            self._update_shutdown_blocked_handle = blocked_handle
            self._update_shutdown_complete_handle = complete_handle
            return True
        return self._acquire_posix_lock()

    def _acquire_posix_lock(self) -> bool:
        import fcntl

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = open(self.lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_file.close()
            return False
        self._lock_file = lock_file
        return True

    def release(self) -> None:
        close_error: Exception | None = None
        if self._windows_handle is not None:
            if self._update_shutdown_request_handle is not None:
                try:
                    _close_windows_handle(self._update_shutdown_request_handle)
                except Exception as exc:
                    close_error = exc
                self._update_shutdown_request_handle = None
            if self._update_shutdown_blocked_handle is not None:
                try:
                    _close_windows_handle(self._update_shutdown_blocked_handle)
                except Exception as exc:
                    if close_error is None:
                        close_error = exc
                self._update_shutdown_blocked_handle = None
            if self._update_shutdown_complete_handle is not None:
                try:
                    _close_windows_handle(self._update_shutdown_complete_handle)
                except Exception as exc:
                    if close_error is None:
                        close_error = exc
                self._update_shutdown_complete_handle = None
            try:
                _close_windows_handle(self._windows_handle)
            except Exception as exc:
                if close_error is None:
                    close_error = exc
            self._windows_handle = None
        if self._lock_file is not None:
            import fcntl

            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()
            self._lock_file = None
        if close_error is not None:
            raise close_error

    def consume_update_shutdown_request(self) -> bool:
        if os.name != "nt" or self._update_shutdown_request_handle is None:
            return False
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        result = kernel32.WaitForSingleObject(self._update_shutdown_request_handle, 0)
        if result == WAIT_FAILED:
            raise ctypes.WinError(ctypes.get_last_error())
        return result == WAIT_OBJECT_0

    def signal_update_shutdown_blocked(self) -> bool:
        if os.name != "nt" or self._update_shutdown_blocked_handle is None:
            return False
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetEvent.argtypes = [ctypes.c_void_p]
        kernel32.SetEvent.restype = ctypes.c_bool
        return bool(kernel32.SetEvent(self._update_shutdown_blocked_handle))

    def signal_update_shutdown_complete(self) -> bool:
        if os.name != "nt" or self._update_shutdown_complete_handle is None:
            return False
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetEvent.argtypes = [ctypes.c_void_p]
        kernel32.SetEvent.restype = ctypes.c_bool
        return bool(kernel32.SetEvent(self._update_shutdown_complete_handle))

    def __enter__(self) -> SingleInstanceGuard:
        if not self.acquire():
            raise RuntimeError("another application instance is already running")
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.release()
